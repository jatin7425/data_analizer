import re

from django import forms
from django.core.validators import FileExtensionValidator

from .models import ExcelUpload


class ExcelUploadForm(forms.ModelForm):
    class Meta:
        model = ExcelUpload
        fields = ["file"]
        widgets = {
            "file": forms.ClearableFileInput(attrs={"accept": ".xls,.xlsx"}),
        }


class DatabaseConnectionForm(forms.Form):
    host = forms.CharField(
        max_length=255,
        widget=forms.TextInput(
            attrs={"class": "w-full rounded-lg bg-slate-900 p-2.5 text-white"}
        ),
    )
    port = forms.IntegerField(
        min_value=1,
        max_value=65535,
        initial=5432,
        widget=forms.NumberInput(
            attrs={"class": "w-full rounded-lg bg-slate-900 p-2.5 text-white"}
        ),
    )
    username = forms.CharField(
        max_length=255,
        widget=forms.TextInput(
            attrs={"class": "w-full rounded-lg bg-slate-900 p-2.5 text-white"}
        ),
    )
    password = forms.CharField(
        strip=False,
        widget=forms.PasswordInput(
            render_value=False,
            attrs={"class": "w-full rounded-lg bg-slate-900 p-2.5 text-white"},
        ),
    )
    database_name = forms.CharField(
        max_length=63,
        widget=forms.TextInput(
            attrs={"class": "w-full rounded-lg bg-slate-900 p-2.5 text-white"}
        ),
    )

    def clean_host(self):
        return self.cleaned_data["host"].strip()

    def clean_username(self):
        return self.cleaned_data["username"].strip()

    def clean_database_name(self):
        database_name = self.cleaned_data["database_name"].strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]{0,62}", database_name):
            raise forms.ValidationError(
                "Use 1-63 letters, digits, underscores, or dollar signs; "
                "the first character must be a letter or underscore."
            )
        return database_name


class DataImportForm(forms.Form):
    file = forms.FileField(
        validators=[
            FileExtensionValidator(allowed_extensions=["xls", "xlsx"]),
        ],
        widget=forms.ClearableFileInput(
            attrs={
                "accept": ".xls,.xlsx",
                "class": "w-full rounded-lg bg-slate-900 p-2.5 text-white",
            }
        ),
    )
    use_source_index_for_orders = forms.BooleanField(
        required=False,
        label='Use the Excel "index" column as the orders upsert key',
        initial=True,
    )
