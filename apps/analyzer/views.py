import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from .forms import ExcelUploadForm
from .models import AnalysisTask, ExcelUpload
from .tasks import process_excel_upload

logger = logging.getLogger(__name__)


@login_required
def dashboard(request):
    tasks = AnalysisTask.objects.filter(user=request.user).select_related("upload")
    return render(
        request,
        "analyzer/dashboard.html",
        {
            "tasks": tasks,
            "has_active_tasks": tasks.filter(
                status__in=[AnalysisTask.Status.PENDING, AnalysisTask.Status.PROCESSING]
            ).exists(),
            "upload_form": ExcelUploadForm(),
        },
    )


@login_required
def task_detail(request, task_id):
    task = get_object_or_404(
        AnalysisTask.objects.filter(user=request.user),
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
        },
    )


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
