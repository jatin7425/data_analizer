import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from .forms import DatabaseConnectionForm, ExcelUploadForm
from .models import AnalysisTask, ExcelUpload
from .security import encrypt_database_password
from .tasks import generate_mapping_plan, process_excel_upload, provision_database

logger = logging.getLogger(__name__)


@login_required
def dashboard(request):
    tasks = AnalysisTask.objects.filter(
        user=request.user,
        is_deleted=False,
    ).select_related("upload")
    return render(
        request,
        "analyzer/dashboard.html",
        {
            "tasks": tasks,
            "has_active_tasks": tasks.filter(
                status__in=[AnalysisTask.Status.PENDING, AnalysisTask.Status.PROCESSING]
            ).exists()
            or tasks.filter(
                mapping_status__in=[
                    AnalysisTask.MappingStatus.PENDING,
                    AnalysisTask.MappingStatus.PROCESSING,
                ]
            ).exists(),
            "upload_form": ExcelUploadForm(),
        },
    )


@login_required
def task_detail(request, task_id):
    task = get_object_or_404(
        AnalysisTask.objects.filter(user=request.user, is_deleted=False),
        pk=task_id,
    )
    schema_tables = task.database_design.get("tables", [])
    reports = task.report_catalogue.get("reports", [])
    return render(
        request,
        "analyzer/task_detail.html",
        {
            "task": task,
            "schema_tables": schema_tables,
            "reports": reports,
            "database_connection_form": DatabaseConnectionForm(
                initial={
                    "host": task.target_db_host,
                    "port": task.target_db_port or 5432,
                    "username": task.target_db_user,
                    "database_name": task.target_db_name,
                }
            ),
        },
    )


@login_required
@require_POST
def setup_database(request, task_id):
    task = get_object_or_404(
        AnalysisTask.objects.filter(
            user=request.user,
            is_deleted=False,
            status=AnalysisTask.Status.COMPLETED,
            review_status=AnalysisTask.ReviewStatus.APPROVED,
            mapping_status=AnalysisTask.MappingStatus.COMPLETED,
        ),
        pk=task_id,
    )
    if task.database_status in [
        AnalysisTask.DatabaseStatus.PENDING,
        AnalysisTask.DatabaseStatus.PROCESSING,
    ]:
        messages.info(request, "Database setup is already in progress.")
        return redirect("analyzer:task_detail", task_id=task.pk)

    form = DatabaseConnectionForm(request.POST)
    if not form.is_valid():
        for error in form.errors.values():
            for message in error:
                messages.error(request, message)
        return redirect("analyzer:task_detail", task_id=task.pk)

    try:
        encrypted_password = encrypt_database_password(
            form.cleaned_data["password"]
        )
    except Exception as exc:
        logger.exception("Could not encrypt database credentials for task %s", task.pk)
        messages.error(request, str(exc))
        return redirect("analyzer:task_detail", task_id=task.pk)

    task.target_db_host = form.cleaned_data["host"]
    task.target_db_port = form.cleaned_data["port"]
    task.target_db_user = form.cleaned_data["username"]
    task.target_db_name = form.cleaned_data["database_name"]
    task.target_db_password_encrypted = encrypted_password
    task.database_status = AnalysisTask.DatabaseStatus.PENDING
    task.database_result = {}
    task.database_error = ""
    task.save(
        update_fields=[
            "target_db_host",
            "target_db_port",
            "target_db_user",
            "target_db_name",
            "target_db_password_encrypted",
            "database_status",
            "database_result",
            "database_error",
            "updated_at",
        ]
    )
    transaction.on_commit(lambda task_id=task.pk: enqueue_database_task(task_id))
    messages.success(
        request,
        "Database setup was queued. Credentials are encrypted and the password "
        "will be cleared after processing.",
    )
    return redirect("analyzer:task_detail", task_id=task.pk)


@login_required
@require_POST
def review_task(request, task_id):
    task = get_object_or_404(
        AnalysisTask.objects.filter(
            user=request.user,
            is_deleted=False,
            status=AnalysisTask.Status.COMPLETED,
            review_status=AnalysisTask.ReviewStatus.PENDING,
        ),
        pk=task_id,
    )
    decision = request.POST.get("decision")

    if decision == "reject":
        task.review_status = AnalysisTask.ReviewStatus.REJECTED
        task.save(update_fields=["review_status", "updated_at"])
        messages.success(request, "Analysis was rejected.")
        return redirect("analyzer:task_detail", task_id=task.pk)

    if decision == "approve":
        task.review_status = AnalysisTask.ReviewStatus.APPROVED
        task.mapping_status = AnalysisTask.MappingStatus.PENDING
        task.save(update_fields=["review_status", "mapping_status", "updated_at"])
        transaction.on_commit(
            lambda task_id=task.pk: enqueue_mapping_plan_task(task_id)
        )
        messages.success(
            request,
            "Analysis approved. Mapping plan generation was queued.",
        )
        return redirect("analyzer:task_detail", task_id=task.pk)

    messages.error(request, "Choose approve or reject to review this analysis.")
    return redirect("analyzer:task_detail", task_id=task.pk)


@login_required
@require_POST
def delete_task(request, task_id):
    task = get_object_or_404(
        AnalysisTask.objects.filter(user=request.user, is_deleted=False),
        pk=task_id,
    )
    task.is_deleted = True
    task.save(update_fields=["is_deleted", "updated_at"])
    messages.success(request, f"Analysis for {task.excel_sheet_name} was removed.")
    return redirect("analyzer:dashboard")


@login_required
@require_POST
def upload_excel(request):
    form = ExcelUploadForm(request.POST, request.FILES)
    if form.is_valid():
        upload = form.save(commit=False)
        upload.user = request.user
        upload.save()
        analysis_task = AnalysisTask.objects.create(
            user=request.user,
            upload=upload,
            excel_sheet_name=upload.filename,
        )
        transaction.on_commit(
            lambda task_id=analysis_task.pk: enqueue_analysis_task(task_id)
        )
        messages.success(
            request,
            "Your Excel sheet was uploaded. Analysis status is shown below.",
        )
    else:
        for error in form.errors.get("file", []):
            messages.error(request, error)
    return redirect("analyzer:dashboard")


def enqueue_analysis_task(task_id):
    try:
        queued_task = process_excel_upload.delay(task_id)
    except Exception as exc:
        logger.exception("Could not enqueue Excel analysis task %s", task_id)
        AnalysisTask.objects.filter(pk=task_id).update(
            status=AnalysisTask.Status.FAILED,
            error_message=f"Could not enqueue analysis: {exc}",
        )
        return

    AnalysisTask.objects.filter(pk=task_id).update(
        celery_task_id=queued_task.id,
    )


def enqueue_mapping_plan_task(task_id):
    try:
        queued_task = generate_mapping_plan.delay(task_id)
    except Exception as exc:
        logger.exception("Could not enqueue mapping plan task %s", task_id)
        AnalysisTask.objects.filter(pk=task_id).update(
            mapping_status=AnalysisTask.MappingStatus.FAILED,
            mapping_error=f"Could not enqueue mapping plan: {exc}",
        )
        return

    AnalysisTask.objects.filter(pk=task_id).update(
        mapping_celery_task_id=queued_task.id,
    )


def enqueue_database_task(task_id):
    try:
        queued_task = provision_database.delay(task_id)
    except Exception as exc:
        logger.exception("Could not enqueue database setup task %s", task_id)
        AnalysisTask.objects.filter(pk=task_id).update(
            database_status=AnalysisTask.DatabaseStatus.FAILED,
            database_error=f"Could not enqueue database setup: {exc}",
            target_db_password_encrypted="",
        )
        return

    AnalysisTask.objects.filter(pk=task_id).update(
        database_celery_task_id=queued_task.id,
    )
