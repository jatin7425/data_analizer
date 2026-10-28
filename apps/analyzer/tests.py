import json
import subprocess
from tempfile import TemporaryDirectory
from unittest.mock import patch
from pathlib import Path

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from cryptography.fernet import Fernet, InvalidToken

from .database_provisioning import normalize_postgres_type
from .models import AnalysisTask, ExcelUpload
from .security import decrypt_database_password, encrypt_database_password
from .tasks import (
    OUTPUT_MARKER,
    generate_mapping_plan,
    process_excel_upload,
    provision_database,
)


class ExcelUploadTests(TestCase):
    def setUp(self):
        self.media_directory = TemporaryDirectory()
        settings_override = override_settings(MEDIA_ROOT=self.media_directory.name)
        settings_override.enable()
        self.addCleanup(settings_override.disable)
        self.addCleanup(self.media_directory.cleanup)
        self.user = User.objects.create_user(username="analyst", password="test-password")
        self.client.force_login(self.user)

    def test_user_can_upload_excel_sheet_and_see_it_on_dashboard(self):
        response = self.client.post(
            reverse("analyzer:upload"),
            {
                "file": SimpleUploadedFile(
                    "sales.xlsx",
                    b"excel content",
                    content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            },
        )

        self.assertRedirects(response, reverse("analyzer:dashboard"))
        upload = ExcelUpload.objects.get(user=self.user)
        self.assertTrue(upload.file.name.endswith("sales.xlsx"))
        task = AnalysisTask.objects.get(user=self.user, upload=upload)
        self.assertEqual(task.status, AnalysisTask.Status.PENDING)
        self.assertEqual(task.excel_sheet_name, "sales.xlsx")

        response = self.client.get(reverse("analyzer:dashboard"))
        self.assertContains(response, "sales.xlsx")
        self.assertContains(response, "Pending")
        self.assertContains(response, "Is deleted")

    def test_upload_enqueues_analysis_after_commit(self):
        with patch("apps.analyzer.views.process_excel_upload.delay") as enqueue:
            enqueue.return_value.id = "celery-task-id"
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.post(
                    reverse("analyzer:upload"),
                    {
                        "file": SimpleUploadedFile(
                            "orders.xlsx",
                            b"excel content",
                        )
                    },
                )

        self.assertRedirects(response, reverse("analyzer:dashboard"))
        task = AnalysisTask.objects.get(excel_sheet_name="orders.xlsx")
        enqueue.assert_called_once_with(task.pk)
        self.assertEqual(task.celery_task_id, "celery-task-id")

    @patch("apps.analyzer.tasks.subprocess.run")
    def test_worker_stores_columns_and_results(self, run_processor):
        upload = ExcelUpload.objects.create(
            user=self.user,
            file=SimpleUploadedFile("inventory.xlsx", b"excel content"),
        )
        task = AnalysisTask.objects.create(
            user=self.user,
            upload=upload,
            excel_sheet_name=upload.filename,
        )

        def return_processor_outputs(command, *, env, **kwargs):
            output_payload = {
                "schema_profile": [{"column_name": "SKU", "pandas_dtype": "object"}],
                "database_design": {"tables": []},
                "proposed_schema": "CREATE TABLE inventory ();",
                "report_catalogue": {"reports": []},
            }
            self.assertEqual(env["ANALYZER_OUTPUT_TO_DATABASE"], "1")
            self.assertNotIn("SCHEMA_PROFILE_FILE", env)
            self.assertEqual(
                {path.name for path in Path(kwargs["cwd"]).iterdir()},
                {"inventory.xlsx"},
            )
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=f"Processor complete\n{OUTPUT_MARKER}{json.dumps(output_payload)}",
                stderr="",
            )

        run_processor.side_effect = return_processor_outputs
        process_excel_upload.apply(args=(task.pk,), throw=True)

        task.refresh_from_db()
        self.assertEqual(task.status, AnalysisTask.Status.COMPLETED)
        self.assertEqual(task.schema_profile[0]["column_name"], "SKU")
        self.assertEqual(task.database_design, {"tables": []})
        self.assertEqual(task.proposed_schema, "CREATE TABLE inventory ();")
        self.assertEqual(task.report_catalogue, {"reports": []})

    @patch("apps.analyzer.tasks.subprocess.run", side_effect=RuntimeError("Ollama is unavailable"))
    def test_worker_records_failed_status(self, run_processor):
        upload = ExcelUpload.objects.create(
            user=self.user,
            file=SimpleUploadedFile("broken.xlsx", b"excel content"),
        )
        task = AnalysisTask.objects.create(
            user=self.user,
            upload=upload,
            excel_sheet_name=upload.filename,
        )

        with self.assertRaises(RuntimeError):
            process_excel_upload.apply(args=(task.pk,), throw=True)

        task.refresh_from_db()
        self.assertEqual(task.status, AnalysisTask.Status.FAILED)
        self.assertIn("Ollama is unavailable", task.error_message)

    def test_upload_rejects_non_excel_files(self):
        response = self.client.post(
            reverse("analyzer:upload"),
            {"file": SimpleUploadedFile("notes.txt", b"not a spreadsheet")},
        )

        self.assertRedirects(response, reverse("analyzer:dashboard"))
        self.assertFalse(ExcelUpload.objects.exists())

    def test_dashboard_only_lists_current_users_uploads(self):
        another_user = User.objects.create_user(username="another", password="test-password")
        upload = ExcelUpload.objects.create(
            user=another_user,
            file=SimpleUploadedFile("private.xlsx", b"private spreadsheet"),
        )
        task = AnalysisTask.objects.create(
            user=another_user,
            upload=upload,
            excel_sheet_name=upload.filename,
        )

        response = self.client.get(reverse("analyzer:dashboard"))

        self.assertNotContains(response, "private.xlsx")
        self.assertNotContains(
            response,
            reverse("analyzer:task_detail", args=[task.pk]),
        )

    def test_completed_task_detail_shows_schema_and_analysis_suggestions(self):
        upload = ExcelUpload.objects.create(
            user=self.user,
            file=SimpleUploadedFile("sales.xlsx", b"spreadsheet"),
        )
        task = AnalysisTask.objects.create(
            user=self.user,
            upload=upload,
            excel_sheet_name=upload.filename,
            status=AnalysisTask.Status.COMPLETED,
            schema_profile=[
                {
                    "column_name": "Order ID",
                    "pandas_dtype": "str",
                    "null_count": 0,
                    "null_percentage": 0,
                    "unique_count": 5,
                }
            ],
            database_design={
                "tables": [
                    {
                        "table_name": "orders",
                        "purpose": "Stores orders",
                        "columns": [
                            {
                                "column_name": "order_id",
                                "postgres_type": "TEXT",
                                "nullable": False,
                                "primary_key": True,
                            }
                        ],
                    }
                ]
            },
            proposed_schema='CREATE TABLE "orders" ("order_id" TEXT NOT NULL);',
            report_catalogue={
                "reports": [
                    {
                        "report_id": "sales_trends",
                        "report_name": "Sales Trends",
                        "category": "Sales",
                        "description": "Review sales over time.",
                        "required_columns": ["Order Date", "Amount"],
                        "metrics": ["Total sales"],
                        "filters": ["Date range"],
                        "visualization": "bar_chart",
                        "feasibility": "available",
                    }
                ]
            },
        )

        response = self.client.get(
            reverse("analyzer:task_detail", args=[task.pk])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "orders")
        self.assertContains(response, "order_id")
        self.assertContains(response, "TEXT")
        self.assertContains(response, 'CREATE TABLE &quot;orders&quot;')
        self.assertContains(response, "Sales Trends")
        self.assertContains(response, "Total sales")
        self.assertContains(response, "Order Date, Amount")

    def test_user_cannot_view_another_users_task_detail(self):
        another_user = User.objects.create_user(username="another", password="test-password")
        upload = ExcelUpload.objects.create(
            user=another_user,
            file=SimpleUploadedFile("private.xlsx", b"spreadsheet"),
        )
        task = AnalysisTask.objects.create(
            user=another_user,
            upload=upload,
            excel_sheet_name=upload.filename,
        )

        response = self.client.get(
            reverse("analyzer:task_detail", args=[task.pk])
        )

        self.assertEqual(response.status_code, 404)

    def test_completed_task_detail_offers_approve_and_reject(self):
        upload = ExcelUpload.objects.create(
            user=self.user,
            file=SimpleUploadedFile("review.xlsx", b"spreadsheet"),
        )
        task = AnalysisTask.objects.create(
            user=self.user,
            upload=upload,
            excel_sheet_name=upload.filename,
            status=AnalysisTask.Status.COMPLETED,
        )

        response = self.client.get(
            reverse("analyzer:task_detail", args=[task.pk])
        )

        self.assertContains(response, reverse("analyzer:review_task", args=[task.pk]))
        self.assertContains(response, 'value="approve"')
        self.assertContains(response, 'value="reject"')

    def test_approving_task_queues_mapping_generation(self):
        upload = ExcelUpload.objects.create(
            user=self.user,
            file=SimpleUploadedFile("approve.xlsx", b"spreadsheet"),
        )
        task = AnalysisTask.objects.create(
            user=self.user,
            upload=upload,
            excel_sheet_name=upload.filename,
            status=AnalysisTask.Status.COMPLETED,
        )

        with patch("apps.analyzer.views.generate_mapping_plan.delay") as enqueue:
            enqueue.return_value.id = "mapping-celery-id"
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.post(
                    reverse("analyzer:review_task", args=[task.pk]),
                    {"decision": "approve"},
                )

        self.assertRedirects(
            response,
            reverse("analyzer:task_detail", args=[task.pk]),
        )
        task.refresh_from_db()
        self.assertEqual(task.review_status, AnalysisTask.ReviewStatus.APPROVED)
        self.assertEqual(task.mapping_status, AnalysisTask.MappingStatus.PENDING)
        self.assertEqual(task.mapping_celery_task_id, "mapping-celery-id")
        enqueue.assert_called_once_with(task.pk)

    def test_rejecting_task_does_not_queue_mapping_generation(self):
        upload = ExcelUpload.objects.create(
            user=self.user,
            file=SimpleUploadedFile("reject.xlsx", b"spreadsheet"),
        )
        task = AnalysisTask.objects.create(
            user=self.user,
            upload=upload,
            excel_sheet_name=upload.filename,
            status=AnalysisTask.Status.COMPLETED,
        )

        with patch("apps.analyzer.views.generate_mapping_plan.delay") as enqueue:
            response = self.client.post(
                reverse("analyzer:review_task", args=[task.pk]),
                {"decision": "reject"},
            )

        self.assertRedirects(
            response,
            reverse("analyzer:task_detail", args=[task.pk]),
        )
        task.refresh_from_db()
        self.assertEqual(task.review_status, AnalysisTask.ReviewStatus.REJECTED)
        self.assertEqual(task.mapping_status, AnalysisTask.MappingStatus.NOT_STARTED)
        enqueue.assert_not_called()

    @patch("apps.analyzer.tasks.subprocess.run")
    def test_mapping_worker_saves_mapping_plan_in_task_row(self, run_mapping):
        upload = ExcelUpload.objects.create(
            user=self.user,
            file=SimpleUploadedFile("mapped.xlsx", b"spreadsheet"),
        )
        task = AnalysisTask.objects.create(
            user=self.user,
            upload=upload,
            excel_sheet_name=upload.filename,
            status=AnalysisTask.Status.COMPLETED,
            review_status=AnalysisTask.ReviewStatus.APPROVED,
            database_design={
                "tables": [
                    {
                        "table_name": "orders",
                        "columns": [
                            {
                                "column_name": "order_id",
                                "source_columns": ["Order ID"],
                                "primary_key": True,
                            }
                        ],
                    }
                ]
            },
        )

        def write_mapping_plan(command, *, env, **kwargs):
            self.assertEqual(
                json.loads(Path(env["DATABASE_DESIGN_FILE"]).read_text()),
                task.database_design,
            )
            mapping_plan = {
                "source_file": "mapped.xlsx",
                "approved": False,
                "tables": [{"table_name": "orders", "columns": []}],
            }
            Path(env["MAPPING_PLAN_FILE"]).write_text(json.dumps(mapping_plan))
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        run_mapping.side_effect = write_mapping_plan
        generate_mapping_plan.apply(args=(task.pk,), throw=True)

        task.refresh_from_db()
        self.assertEqual(task.mapping_status, AnalysisTask.MappingStatus.COMPLETED)
        self.assertEqual(
            task.mapping_plan["tables"][0]["table_name"],
            "orders",
        )

    @override_settings(ANALYZER_CREDENTIAL_ENCRYPTION_KEY=Fernet.generate_key().decode())
    def test_database_setup_submission_encrypts_password_and_queues_task(self):
        upload = ExcelUpload.objects.create(
            user=self.user,
            file=SimpleUploadedFile("database.xlsx", b"spreadsheet"),
        )
        task = AnalysisTask.objects.create(
            user=self.user,
            upload=upload,
            excel_sheet_name=upload.filename,
            status=AnalysisTask.Status.COMPLETED,
            review_status=AnalysisTask.ReviewStatus.APPROVED,
            mapping_status=AnalysisTask.MappingStatus.COMPLETED,
        )

        with patch("apps.analyzer.views.provision_database.delay") as enqueue:
            enqueue.return_value.id = "database-celery-id"
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.post(
                    reverse("analyzer:setup_database", args=[task.pk]),
                    {
                        "host": "db.example.test",
                        "port": "5432",
                        "username": "analyst",
                        "password": "dont-store-this",
                        "database_name": "sales_reporting",
                    },
                )

        self.assertRedirects(
            response,
            reverse("analyzer:task_detail", args=[task.pk]),
        )
        task.refresh_from_db()
        self.assertEqual(task.database_status, AnalysisTask.DatabaseStatus.PENDING)
        self.assertEqual(task.target_db_name, "sales_reporting")
        self.assertNotEqual(task.target_db_password_encrypted, "dont-store-this")
        self.assertEqual(
            decrypt_database_password(task.target_db_password_encrypted),
            "dont-store-this",
        )
        enqueue.assert_called_once_with(task.pk)
        self.assertEqual(task.database_celery_task_id, "database-celery-id")

    @override_settings(ANALYZER_CREDENTIAL_ENCRYPTION_KEY=Fernet.generate_key().decode())
    @patch("apps.analyzer.tasks.ensure_database_and_schema")
    def test_database_worker_stores_result_and_clears_password(self, ensure_database):
        upload = ExcelUpload.objects.create(
            user=self.user,
            file=SimpleUploadedFile("database.xlsx", b"spreadsheet"),
        )
        task = AnalysisTask.objects.create(
            user=self.user,
            upload=upload,
            excel_sheet_name=upload.filename,
            status=AnalysisTask.Status.COMPLETED,
            review_status=AnalysisTask.ReviewStatus.APPROVED,
            mapping_status=AnalysisTask.MappingStatus.COMPLETED,
            target_db_host="db.example.test",
            target_db_port=5432,
            target_db_user="analyst",
            target_db_name="sales_reporting",
            target_db_password_encrypted=encrypt_database_password(
                "temporary-password"
            ),
        )
        ensure_database.return_value = {
            "database_created": True,
            "tables_created": ["orders"],
            "columns_added": [],
            "type_conflicts": [],
            "nullability_notes": [],
            "database_exists": False,
        }

        provision_database.apply(args=(task.pk,), throw=True)

        task.refresh_from_db()
        self.assertEqual(task.database_status, AnalysisTask.DatabaseStatus.COMPLETED)
        self.assertEqual(task.database_result["tables_created"], ["orders"])
        self.assertEqual(task.database_result["host"], "db.example.test")
        self.assertEqual(task.target_db_password_encrypted, "")
        ensure_database.assert_called_once_with(
            host="db.example.test",
            port=5432,
            username="analyst",
            password="temporary-password",
            database_name="sales_reporting",
            database_design=task.database_design,
        )

    def test_database_worker_records_decryption_failure_and_clears_password(self):
        upload = ExcelUpload.objects.create(
            user=self.user,
            file=SimpleUploadedFile("database.xlsx", b"spreadsheet"),
        )
        task = AnalysisTask.objects.create(
            user=self.user,
            upload=upload,
            excel_sheet_name=upload.filename,
            status=AnalysisTask.Status.COMPLETED,
            mapping_status=AnalysisTask.MappingStatus.COMPLETED,
            target_db_password_encrypted="encrypted-with-another-key",
        )

        with override_settings(
            ANALYZER_CREDENTIAL_ENCRYPTION_KEY=Fernet.generate_key().decode()
        ):
            with self.assertRaises(InvalidToken):
                provision_database.apply(args=(task.pk,), throw=True)

        task.refresh_from_db()
        self.assertEqual(task.database_status, AnalysisTask.DatabaseStatus.FAILED)
        self.assertLess(len(task.database_error), 200)
        self.assertEqual(task.target_db_password_encrypted, "")

    def test_database_setup_rejects_invalid_database_name(self):
        from .forms import DatabaseConnectionForm

        form = DatabaseConnectionForm(
            {
                "host": "localhost",
                "port": "5432",
                "username": "analyst",
                "password": "password",
                "database_name": "sales; DROP DATABASE postgres",
            }
        )

        self.assertFalse(form.is_valid())
        with self.assertRaises(ValueError):
            normalize_postgres_type("TEXT; DROP TABLE users")

    def test_user_can_soft_delete_task_from_dashboard(self):
        upload = ExcelUpload.objects.create(
            user=self.user,
            file=SimpleUploadedFile("remove-me.xlsx", b"spreadsheet"),
        )
        task = AnalysisTask.objects.create(
            user=self.user,
            upload=upload,
            excel_sheet_name=upload.filename,
            status=AnalysisTask.Status.COMPLETED,
            database_design={"tables": [{"table_name": "orders"}]},
        )

        response = self.client.post(
            reverse("analyzer:delete_task", args=[task.pk])
        )

        self.assertRedirects(response, reverse("analyzer:dashboard"))
        task.refresh_from_db()
        self.assertTrue(task.is_deleted)
        self.assertEqual(task.database_design, {"tables": [{"table_name": "orders"}]})
        self.assertNotContains(
            self.client.get(reverse("analyzer:dashboard")),
            "remove-me.xlsx",
        )
        self.assertEqual(
            self.client.get(reverse("analyzer:task_detail", args=[task.pk])).status_code,
            404,
        )

    def test_user_cannot_delete_another_users_task(self):
        another_user = User.objects.create_user(username="another", password="test-password")
        upload = ExcelUpload.objects.create(
            user=another_user,
            file=SimpleUploadedFile("private.xlsx", b"spreadsheet"),
        )
        task = AnalysisTask.objects.create(
            user=another_user,
            upload=upload,
            excel_sheet_name=upload.filename,
        )

        response = self.client.post(
            reverse("analyzer:delete_task", args=[task.pk])
        )

        self.assertEqual(response.status_code, 404)
        task.refresh_from_db()
        self.assertFalse(task.is_deleted)

    def test_dashboard_requires_login(self):
        self.client.logout()

        response = self.client.get(reverse("analyzer:dashboard"))

        self.assertRedirects(
            response,
            f"{reverse('accounts:login')}?next={reverse('analyzer:dashboard')}",
        )
