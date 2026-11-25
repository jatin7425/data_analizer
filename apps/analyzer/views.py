import logging

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from .forms import DataImportForm, DatabaseConnectionForm, ExcelUploadForm
from .models import AnalysisTask, DataImport, ExcelUpload
from .progress import current_stage, task_stages
from .security import encrypt_database_password
from .tasks import (
    generate_mapping_plan,
    import_excel_to_database,
    process_excel_upload,
    provision_database,
)

logger = logging.getLogger(__name__)


@login_required
def dashboard(request):
    tasks = AnalysisTask.objects.filter(
        user=request.user,
        is_deleted=False,
    ).select_related("upload")
    task_list = list(tasks)
    for task in task_list:
        latest_import = (
            task.data_imports.first()
            if task.database_status == AnalysisTask.DatabaseStatus.COMPLETED
            else None
        )
        task.stage = current_stage(task_stages(task, latest_import))
    return render(
        request,
        "analyzer/dashboard.html",
        {
            "tasks": task_list,
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
    data_imports = list(task.data_imports.all())
    default_database = settings.DATABASES["default"]
    app_database = {
        "HOST": default_database.get("HOST") or "localhost",
        "PORT": int(default_database.get("PORT") or 5432),
        "USER": default_database.get("USER") or "",
    }
    stages = task_stages(task, data_imports[0] if data_imports else None)
    return render(
        request,
        "analyzer/task_detail.html",
        {
            "task": task,
            "stages": stages,
            "current_stage": current_stage(stages),
            "schema_tables": schema_tables,
            "reports": reports,
            "data_imports": data_imports,
            "has_active_imports": task.data_imports.filter(
                status__in=[DataImport.Status.PENDING, DataImport.Status.PROCESSING]
            ).exists(),
            "database_connection_form": DatabaseConnectionForm(
                initial={
                    # Until a connection is saved, suggest the PostgreSQL server
                    # this app already uses (from .env).
                    "host": task.target_db_host or app_database["HOST"],
                    "port": task.target_db_port or app_database["PORT"],
                    "username": task.target_db_user or app_database["USER"],
                    "database_name": task.target_db_name,
                }
            ),
            "app_database": app_database,
            "data_import_form": DataImportForm(),
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
    if task.data_imports.filter(
        status__in=[DataImport.Status.PENDING, DataImport.Status.PROCESSING]
    ).exists():
        messages.info(request, "Wait for the current Excel import before updating the database connection.")
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
        "is retained encrypted for future imports.",
    )
    return redirect("analyzer:task_detail", task_id=task.pk)


@login_required
@require_POST
def upload_data_import(request, task_id):
    task = get_object_or_404(
        AnalysisTask.objects.filter(
            user=request.user,
            is_deleted=False,
            status=AnalysisTask.Status.COMPLETED,
            review_status=AnalysisTask.ReviewStatus.APPROVED,
            mapping_status=AnalysisTask.MappingStatus.COMPLETED,
            database_status=AnalysisTask.DatabaseStatus.COMPLETED,
        ),
        pk=task_id,
    )
    if not task.target_db_password_encrypted:
        messages.error(
            request,
            "Update the database connection and run setup again before importing data.",
        )
        return redirect("analyzer:task_detail", task_id=task.pk)

    if task.data_imports.filter(
        status__in=[DataImport.Status.PENDING, DataImport.Status.PROCESSING]
    ).exists():
        messages.info(request, "An Excel import is already in progress.")
        return redirect("analyzer:task_detail", task_id=task.pk)

    form = DataImportForm(request.POST, request.FILES)
    if not form.is_valid():
        for error in form.errors.values():
            for message in error:
                messages.error(request, message)
        return redirect("analyzer:task_detail", task_id=task.pk)

    data_import = DataImport.objects.create(
        analysis_task=task,
        file=form.cleaned_data["file"],
    )
    transaction.on_commit(
        lambda import_id=data_import.pk: enqueue_data_import_task(import_id)
    )
    messages.success(request, "Excel schema validation and data import were queued.")
    return redirect("analyzer:task_detail", task_id=task.pk)


@login_required
@require_POST
def retry_task(request, task_id):
    """Re-run a failed step, or re-analyze a design that hasn't been approved."""
    task = get_object_or_404(
        AnalysisTask.objects.filter(user=request.user, is_deleted=False),
        pk=task_id,
    )
    step = request.POST.get("step")

    if step == "analysis":
        # Allowed until a database has been set up from this design.
        can_retry = task.status == AnalysisTask.Status.FAILED or (
            task.status == AnalysisTask.Status.COMPLETED
            and task.database_status
            in (AnalysisTask.DatabaseStatus.NOT_STARTED, AnalysisTask.DatabaseStatus.FAILED)
            and task.mapping_status
            not in (AnalysisTask.MappingStatus.PENDING, AnalysisTask.MappingStatus.PROCESSING)
        )
        if not can_retry:
            messages.error(request, "This analysis can't be run again right now.")
            return redirect("analyzer:task_detail", task_id=task.pk)
        task.status = AnalysisTask.Status.PENDING
        task.error_message = ""
        task.schema_profile = []
        task.database_design = {}
        task.proposed_schema = ""
        task.report_catalogue = {}
        task.review_status = AnalysisTask.ReviewStatus.PENDING
        task.mapping_status = AnalysisTask.MappingStatus.NOT_STARTED
        task.mapping_plan = {}
        task.mapping_error = ""
        task.database_status = AnalysisTask.DatabaseStatus.NOT_STARTED
        task.database_error = ""
        task.database_result = {}
        task.save()
        transaction.on_commit(lambda task_id=task.pk: enqueue_analysis_task(task_id))
        messages.success(request, "Analysis restarted. This page refreshes when it's done.")
        return redirect("analyzer:task_detail", task_id=task.pk)

    if step == "mapping":
        if not (
            task.review_status == AnalysisTask.ReviewStatus.APPROVED
            and task.mapping_status == AnalysisTask.MappingStatus.FAILED
        ):
            messages.error(request, "The mapping plan can't be built again right now.")
            return redirect("analyzer:task_detail", task_id=task.pk)
        task.mapping_status = AnalysisTask.MappingStatus.PENDING
        task.mapping_error = ""
        task.save(update_fields=["mapping_status", "mapping_error", "updated_at"])
        transaction.on_commit(lambda task_id=task.pk: enqueue_mapping_plan_task(task_id))
        messages.success(request, "Building the mapping plan again.")
        return redirect("analyzer:task_detail", task_id=task.pk)

    messages.error(request, "Choose a step to retry.")
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
    task.target_db_password_encrypted = ""
    task.save(
        update_fields=[
            "is_deleted",
            "target_db_password_encrypted",
            "updated_at",
        ]
    )
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


def enqueue_data_import_task(import_id):
    try:
        queued_task = import_excel_to_database.delay(import_id)
    except Exception as exc:
        logger.exception("Could not enqueue Excel data import %s", import_id)
        DataImport.objects.filter(pk=import_id).update(
            status=DataImport.Status.FAILED,
            error_message=f"Could not enqueue Excel import: {exc}",
        )
        return

    DataImport.objects.filter(pk=import_id).update(
        celery_task_id=queued_task.id or "",
    )
