import os
import sys

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "data_analizer.settings")

app = Celery("data_analizer")
app.config_from_object("django.conf:settings", namespace="CELERY")


if sys.platform == "win32":
    app.conf.worker_pool = "solo"

app.autodiscover_tasks()
