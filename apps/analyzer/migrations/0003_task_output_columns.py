from django.db import migrations, models


def move_task_outputs_to_columns(apps, schema_editor):
    AnalysisTask = apps.get_model("analyzer", "AnalysisTask")
    for task in AnalysisTask.objects.all().iterator():
        outputs = task.result_json or {}
        task.database_design = outputs.get("database_design", {})
        task.proposed_schema = outputs.get("proposed_schema", "")
        task.report_catalogue = outputs.get("report_catalogue", {})
        task.save(
            update_fields=[
                "database_design",
                "proposed_schema",
                "report_catalogue",
            ]
        )


def move_task_outputs_to_legacy_json(apps, schema_editor):
    AnalysisTask = apps.get_model("analyzer", "AnalysisTask")
    for task in AnalysisTask.objects.all().iterator():
        task.result_json = {
            "database_design": task.database_design,
            "proposed_schema": task.proposed_schema,
            "report_catalogue": task.report_catalogue,
        }
        task.save(update_fields=["result_json"])


class Migration(migrations.Migration):

    dependencies = [
        ("analyzer", "0002_analysistask"),
    ]

    operations = [
        migrations.RenameField(
            model_name="analysistask",
            old_name="column_names",
            new_name="schema_profile",
        ),
        migrations.AddField(
            model_name="analysistask",
            name="database_design",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="analysistask",
            name="proposed_schema",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="analysistask",
            name="report_catalogue",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.RunPython(
            move_task_outputs_to_columns,
            move_task_outputs_to_legacy_json,
        ),
        migrations.RemoveField(
            model_name="analysistask",
            name="result_json",
        ),
    ]
