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

    class ReviewStatus(models.TextChoices):
        PENDING = "pending", "Awaiting review"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"

    class MappingStatus(models.TextChoices):
        NOT_STARTED = "not_started", "Not started"
        PENDING = "pending", "Pending"
        PROCESSING = "processing", "Processing"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    class DatabaseStatus(models.TextChoices):
        NOT_STARTED = "not_started", "Not started"
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
    schema_profile = models.JSONField(default=list)
    database_design = models.JSONField(default=dict, blank=True)
    proposed_schema = models.TextField(blank=True)
    report_catalogue = models.JSONField(default=dict, blank=True)
    review_status = models.CharField(
        max_length=16,
        choices=ReviewStatus.choices,
        default=ReviewStatus.PENDING,
    )
    mapping_status = models.CharField(
        max_length=16,
        choices=MappingStatus.choices,
        default=MappingStatus.NOT_STARTED,
    )
    mapping_plan = models.JSONField(default=dict, blank=True)
    mapping_error = models.TextField(blank=True)
    mapping_celery_task_id = models.CharField(max_length=255, blank=True)
    target_db_host = models.CharField(max_length=255, blank=True)
    target_db_port = models.PositiveIntegerField(null=True, blank=True)
    target_db_user = models.CharField(max_length=255, blank=True)
    target_db_name = models.CharField(max_length=63, blank=True)
    target_db_password_encrypted = models.TextField(blank=True)
    database_status = models.CharField(
        max_length=16,
        choices=DatabaseStatus.choices,
        default=DatabaseStatus.NOT_STARTED,
    )
    database_result = models.JSONField(default=dict, blank=True)
    database_error = models.TextField(blank=True)
    database_celery_task_id = models.CharField(max_length=255, blank=True)
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


class DataImport(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PROCESSING = "processing", "Processing"
        COMPLETED = "completed", "Completed"
        SCHEMA_MISMATCH = "schema_mismatch", "Schema mismatch"
        FAILED = "failed", "Failed"

    analysis_task = models.ForeignKey(
        AnalysisTask,
        on_delete=models.CASCADE,
        related_name="data_imports",
    )
    file = models.FileField(
        upload_to="data_imports/%Y/%m/%d/",
        validators=[FileExtensionValidator(allowed_extensions=["xls", "xlsx"])],
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
    )
    schema_mismatch = models.JSONField(default=dict, blank=True)
    result = models.JSONField(default=dict, blank=True)
    error_message = models.TextField(blank=True)
    celery_task_id = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Import for task {self.analysis_task_id} ({self.get_status_display()})"
