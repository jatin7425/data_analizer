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

OUTPUT_MARKER = "ANALYZER_DATABASE_OUTPUT:"


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

            environment = os.environ.copy()
            environment.update(
                {
                    "SOURCE_EXCEL_FILE": str(input_path),
                    "OLLAMA_MODEL": settings.ANALYZER_OLLAMA_MODEL,
                    "ANALYZER_OUTPUT_TO_DATABASE": "1",
                }
            )
            processor_result = subprocess.run(
                [sys.executable, str(Path(__file__).with_name("processor_main.py"))],
                cwd=workdir,
                env=environment,
                check=True,
                capture_output=True,
                text=True,
                timeout=settings.ANALYZER_TASK_TIMEOUT,
            )

        _, marker, serialized_outputs = processor_result.stdout.rpartition(OUTPUT_MARKER)
        if not marker:
            raise ValueError("Processor did not return its generated outputs.")
        outputs = json.loads(serialized_outputs.strip())

        analysis_task.schema_profile = outputs["schema_profile"]
        analysis_task.database_design = outputs["database_design"]
        analysis_task.proposed_schema = outputs["proposed_schema"]
        analysis_task.report_catalogue = outputs["report_catalogue"]
        analysis_task.status = AnalysisTask.Status.COMPLETED
        analysis_task.error_message = ""
        analysis_task.save(
            update_fields=[
                "schema_profile",
                "database_design",
                "proposed_schema",
                "report_catalogue",
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
