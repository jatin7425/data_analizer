from pathlib import PurePosixPath

from django.conf import settings
from django.db import models
from django.core.validators import FileExtensionValidator


class ExcelUpload(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="excel_uploads",
    )
    file = models.FileField(
        upload_to="excel_uploads/%Y/%m/%d/",
        validators=[FileExtensionValidator(allowed_extensions=["xls", "xlsx"])],
    )
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-uploaded_at"]

    def __str__(self):
        return self.file.name

    @property
    def filename(self):
        return PurePosixPath(self.file.name).name


class AnalysisTask(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PROCESSING = "processing", "Processing"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="analysis_tasks",
    )
    upload = models.ForeignKey(
        ExcelUpload,
        on_delete=models.CASCADE,
        related_name="analysis_tasks",
    )
    excel_sheet_name = models.CharField(max_length=255)
    column_names = models.JSONField(default=list)
    result_json = models.JSONField(default=dict, blank=True)
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
    )
    error_message = models.TextField(blank=True)
    is_deleted = models.BooleanField(default=False)
    celery_task_id = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "task"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.excel_sheet_name} ({self.get_status_display()})"
