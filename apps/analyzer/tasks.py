import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from celery import shared_task
from django.conf import settings

from .models import AnalysisTask


@shared_task(bind=True, name="apps.analyzer.process_excel_upload")
def process_excel_upload(self, analysis_task_id):
    analysis_task = AnalysisTask.objects.select_related("upload").get(
        pk=analysis_task_id
    )
    analysis_task.status = AnalysisTask.Status.PROCESSING
    analysis_task.error_message = ""
    analysis_task.celery_task_id = self.request.id or ""
    analysis_task.save(
        update_fields=["status", "error_message", "celery_task_id", "updated_at"]
    )

    try:
        with tempfile.TemporaryDirectory(
            prefix=f"excel-analysis-{analysis_task.pk}-"
        ) as workdir:
            work_path = Path(workdir)
            input_path = work_path / analysis_task.upload.filename
            analysis_task.upload.file.open("rb")
            try:
                with input_path.open("wb") as target:
                    shutil.copyfileobj(analysis_task.upload.file, target)
            finally:
                analysis_task.upload.file.close()

            output_paths = {
                "profile": work_path / "excel_schema_profile.json",
                "database_design": work_path / "database_design.json",
                "schema": work_path / "proposed_schema.sql",
                "report_catalogue": work_path / "report_catalogue.json",
            }
            environment = os.environ.copy()
            environment.update(
                {
                    "SOURCE_EXCEL_FILE": str(input_path),
                    "OLLAMA_MODEL": settings.ANALYZER_OLLAMA_MODEL,
                    "SCHEMA_PROFILE_FILE": str(output_paths["profile"]),
                    "DATABASE_DESIGN_FILE": str(output_paths["database_design"]),
                    "PROPOSED_SCHEMA_FILE": str(output_paths["schema"]),
                    "REPORT_CATALOGUE_FILE": str(output_paths["report_catalogue"]),
                }
            )
            subprocess.run(
                [sys.executable, str(Path(__file__).with_name("processor_main.py"))],
                cwd=workdir,
                env=environment,
                check=True,
                capture_output=True,
                text=True,
                timeout=settings.ANALYZER_TASK_TIMEOUT,
            )

            column_profile = json.loads(
                output_paths["profile"].read_text(encoding="utf-8")
            )
            result = {
                "database_design": json.loads(
                    output_paths["database_design"].read_text(encoding="utf-8")
                ),
                "proposed_schema": output_paths["schema"].read_text(encoding="utf-8"),
                "report_catalogue": json.loads(
                    output_paths["report_catalogue"].read_text(encoding="utf-8")
                ),
            }

        analysis_task.column_names = column_profile
        analysis_task.result_json = result
        analysis_task.status = AnalysisTask.Status.COMPLETED
        analysis_task.error_message = ""
        analysis_task.save(
            update_fields=[
                "column_names",
                "result_json",
                "status",
                "error_message",
                "updated_at",
            ]
        )
    except Exception as exc:
        analysis_task.status = AnalysisTask.Status.FAILED
        analysis_task.error_message = f"{type(exc).__name__}: {exc}"
        analysis_task.save(update_fields=["status", "error_message", "updated_at"])
        raise
