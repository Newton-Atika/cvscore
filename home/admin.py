from django.contrib import admin
from .models import Document

@admin.register(Document)
class DocumentAdmin(admin.ModelAdmin):
    list_display = ('short_cv_text', 'short_job_description', 'uploaded_at')
    search_fields = ('extracted_text', 'job_description')

    def short_cv_text(self, obj):
        return (obj.extracted_text[:100] + "...") if obj.extracted_text else ""
    short_cv_text.short_description = "Extracted CV Text"

    def short_job_description(self, obj):
        return (obj.job_description[:100] + "...") if obj.job_description else ""
    short_job_description.short_description = "Job Description"
