import json
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from .models import AnalysisTask, ExcelUpload
from .tasks import process_excel_upload


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

        def write_processor_outputs(command, *, env, **kwargs):
            output_files = {
                env["SCHEMA_PROFILE_FILE"]: [{"column_name": "SKU", "pandas_dtype": "object"}],
                env["DATABASE_DESIGN_FILE"]: {"tables": []},
                env["PROPOSED_SCHEMA_FILE"]: "CREATE TABLE inventory ();",
                env["REPORT_CATALOGUE_FILE"]: {"reports": []},
            }
            for path, content in output_files.items():
                with open(path, "w", encoding="utf-8") as output_file:
                    if path.endswith(".json"):
                        json.dump(content, output_file)
                    else:
                        output_file.write(content)

        run_processor.side_effect = write_processor_outputs
        process_excel_upload.apply(args=(task.pk,), throw=True)

        task.refresh_from_db()
        self.assertEqual(task.status, AnalysisTask.Status.COMPLETED)
        self.assertEqual(task.column_names[0]["column_name"], "SKU")
        self.assertEqual(task.result_json["database_design"], {"tables": []})
        self.assertEqual(task.result_json["proposed_schema"], "CREATE TABLE inventory ();")

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
        ExcelUpload.objects.create(
            user=another_user,
            file=SimpleUploadedFile("private.xlsx", b"private spreadsheet"),
        )

        response = self.client.get(reverse("analyzer:dashboard"))

        self.assertNotContains(response, "private.xlsx")

    def test_dashboard_requires_login(self):
        self.client.logout()

        response = self.client.get(reverse("analyzer:dashboard"))

        self.assertRedirects(
            response,
            f"{reverse('accounts:login')}?next={reverse('analyzer:dashboard')}",
        )
