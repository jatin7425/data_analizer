import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pandas as pd
from celery import shared_task
from django.conf import settings

from .database_provisioning import ensure_database_and_schema
from .data_importing import (
    compare_schema,
    load_dataframe,
)
from .models import AnalysisTask, DataImport
from .security import decrypt_database_password

OUTPUT_MARKER = "ANALYZER_DATABASE_OUTPUT:"


@shared_task(bind=True, name="apps.analyzer.provision_database")
def provision_database(self, analysis_task_id):
    analysis_task = AnalysisTask.objects.get(pk=analysis_task_id)
    if analysis_task.mapping_status != AnalysisTask.MappingStatus.COMPLETED:
        raise ValueError("Database setup requires a completed mapping plan.")

    password = ""
    analysis_task.database_status = AnalysisTask.DatabaseStatus.PROCESSING
    analysis_task.database_error = ""
    analysis_task.database_celery_task_id = self.request.id or ""
    analysis_task.save(
        update_fields=[
            "database_status",
            "database_error",
            "database_celery_task_id",
            "updated_at",
        ]
    )

    try:
        password = decrypt_database_password(
            analysis_task.target_db_password_encrypted
        )
        result = ensure_database_and_schema(
            host=analysis_task.target_db_host,
            port=analysis_task.target_db_port,
            username=analysis_task.target_db_user,
            password=password,
            database_name=analysis_task.target_db_name,
            database_design=analysis_task.database_design,
        )
        result["host"] = analysis_task.target_db_host
        result["port"] = analysis_task.target_db_port
        result["username"] = analysis_task.target_db_user
        analysis_task.database_result = result
        analysis_task.database_status = AnalysisTask.DatabaseStatus.COMPLETED
        analysis_task.database_error = ""
        analysis_task.save(
            update_fields=[
                "database_result",
                "database_status",
                "database_error",
                "updated_at",
            ]
        )
    except Exception as exc:
        analysis_task.database_status = AnalysisTask.DatabaseStatus.FAILED
        error_message = str(exc)
        if password:
            error_message = error_message.replace(password, "[redacted]")
        analysis_task.database_error = error_message
        analysis_task.save(
            update_fields=["database_status", "database_error", "updated_at"]
        )
        AnalysisTask.objects.filter(pk=analysis_task_id).update(
            target_db_password_encrypted=""
        )
        raise


@shared_task(bind=True, name="apps.analyzer.import_excel_to_database")
def import_excel_to_database(self, import_id):
    data_import = DataImport.objects.select_related(
        "analysis_task",
        "analysis_task__upload",
    ).get(pk=import_id)
    analysis_task = data_import.analysis_task
    data_import.status = DataImport.Status.PROCESSING
    data_import.error_message = ""
    data_import.celery_task_id = self.request.id or ""
    data_import.save(
        update_fields=["status", "error_message", "celery_task_id", "updated_at"]
    )

    password = ""
    try:
        if analysis_task.review_status != AnalysisTask.ReviewStatus.APPROVED:
            raise ValueError("The analysis must be approved before importing data.")
        if analysis_task.mapping_status != AnalysisTask.MappingStatus.COMPLETED:
            raise ValueError("Mapping plan generation must complete before importing data.")
        if analysis_task.database_status != AnalysisTask.DatabaseStatus.COMPLETED:
            raise ValueError("Database setup must complete before importing data.")
        if not analysis_task.target_db_password_encrypted:
            raise ValueError("Update the database connection before importing data.")
        data_import.file.open("rb")
        try:
            dataframe = pd.read_excel(data_import.file, sheet_name=0)
        finally:
            data_import.file.close()
        dataframe.columns = [str(column).strip() for column in dataframe.columns]

        mismatch = compare_schema(dataframe, analysis_task.schema_profile)
        if any(mismatch.values()):
            data_import.schema_mismatch = mismatch
            data_import.status = DataImport.Status.SCHEMA_MISMATCH
            data_import.error_message = (
                "The uploaded Excel schema does not match the original workbook. "
                "No database rows were changed."
            )
            data_import.save(
                update_fields=[
                    "schema_mismatch",
                    "status",
                    "error_message",
                    "updated_at",
                ]
            )
            return {"status": DataImport.Status.SCHEMA_MISMATCH}

        if dataframe.empty:
            raise ValueError("The uploaded Excel sheet contains no data rows.")

        password = decrypt_database_password(
            analysis_task.target_db_password_encrypted
        )
        data_import.result = {
            "tables": load_dataframe(
                dataframe=dataframe,
                connection_args={
                    "dbname": analysis_task.target_db_name,
                    "host": analysis_task.target_db_host,
                    "port": analysis_task.target_db_port,
                    "user": analysis_task.target_db_user,
                    "password": password,
                    "connect_timeout": 10,
                },
                mapping_plan=analysis_task.mapping_plan,
            )
        }
        data_import.status = DataImport.Status.COMPLETED
        data_import.schema_mismatch = {}
        data_import.error_message = ""
        data_import.save(
            update_fields=[
                "result",
                "status",
                "schema_mismatch",
                "error_message",
                "updated_at",
            ]
        )
        return data_import.result
    except Exception as exc:
        data_import.status = DataImport.Status.FAILED
        error_message = str(exc)
        if password:
            error_message = error_message.replace(password, "[redacted]")
        data_import.error_message = error_message
        data_import.save(
            update_fields=["status", "error_message", "updated_at"]
        )
        raise


@shared_task(bind=True, name="apps.analyzer.generate_mapping_plan")
def generate_mapping_plan(self, analysis_task_id):
    analysis_task = AnalysisTask.objects.get(pk=analysis_task_id)
    if analysis_task.review_status != AnalysisTask.ReviewStatus.APPROVED:
        raise ValueError("A mapping plan can only be generated for an approved analysis.")

    analysis_task.mapping_status = AnalysisTask.MappingStatus.PROCESSING
    analysis_task.mapping_error = ""
    analysis_task.mapping_celery_task_id = self.request.id or ""
    analysis_task.save(
        update_fields=[
            "mapping_status",
            "mapping_error",
            "mapping_celery_task_id",
            "updated_at",
        ]
    )

    try:
        with tempfile.TemporaryDirectory(
            prefix=f"mapping-plan-{analysis_task.pk}-"
        ) as workdir:
            work_path = Path(workdir)
            design_path = work_path / "database_design.json"
            plan_path = work_path / "data_mapping_plan.json"
            design_path.write_text(
                json.dumps(analysis_task.database_design, ensure_ascii=False),
                encoding="utf-8",
            )
            environment = os.environ.copy()
            environment.update(
                {
                    "DATABASE_DESIGN_FILE": str(design_path),
                    "MAPPING_PLAN_FILE": str(plan_path),
                    "SOURCE_EXCEL_FILE": analysis_task.excel_sheet_name,
                    "SOURCE_SHEET_NAME": "0",
                }
            )
            subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).with_name("mapping_plan_main.py")),
                ],
                cwd=workdir,
                env=environment,
                check=True,
                capture_output=True,
                text=True,
                timeout=settings.ANALYZER_TASK_TIMEOUT,
            )
            mapping_plan = json.loads(plan_path.read_text(encoding="utf-8"))

        analysis_task.mapping_plan = mapping_plan
        analysis_task.mapping_status = AnalysisTask.MappingStatus.COMPLETED
        analysis_task.mapping_error = ""
        analysis_task.save(
            update_fields=[
                "mapping_plan",
                "mapping_status",
                "mapping_error",
                "updated_at",
            ]
        )
    except Exception as exc:
        analysis_task.mapping_status = AnalysisTask.MappingStatus.FAILED
        analysis_task.mapping_error = f"{type(exc).__name__}: {exc}"
        analysis_task.save(
            update_fields=["mapping_status", "mapping_error", "updated_at"]
        )
        raise


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
