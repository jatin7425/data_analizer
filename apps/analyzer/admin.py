from django.contrib import admin
from .models import AnalysisTask, ExcelUpload

admin.site.register(ExcelUpload)


@admin.register(AnalysisTask)
class AnalysisTaskAdmin(admin.ModelAdmin):
    list_display = ("excel_sheet_name", "user", "status", "is_deleted", "created_at")
    list_filter = ("status", "is_deleted")
