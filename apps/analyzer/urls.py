from django.urls import path
from . import views

app_name = "analyzer"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("upload/", views.upload_excel, name="upload"),
    path("tasks/<int:task_id>/", views.task_detail, name="task_detail"),
    path("tasks/<int:task_id>/database/", views.setup_database, name="setup_database"),
    path(
        "tasks/<int:task_id>/import/",
        views.upload_data_import,
        name="upload_data_import",
    ),
    path("tasks/<int:task_id>/review/", views.review_task, name="review_task"),
    path("tasks/<int:task_id>/retry/", views.retry_task, name="retry_task"),
    path("tasks/<int:task_id>/delete/", views.delete_task, name="delete_task"),
]
