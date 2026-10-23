from django.urls import path
from django.views.generic import RedirectView

app_name = "analyzer"

urlpatterns = [
    path("", RedirectView.as_view(pattern_name="landing"), name="dashboard"),
]
