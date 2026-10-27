from django.urls import path
from . import views

app_name = "analyzer"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("upload/", views.upload_excel, name="upload"),
]
