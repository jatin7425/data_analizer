from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("analyzer", "0007_dataimport_use_source_index_for_orders"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="dataimport",
            name="use_source_index_for_orders",
        ),
    ]
