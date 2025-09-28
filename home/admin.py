from django.contrib import admin
from .models import Document, Payment, Subscription

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

@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ('user', 'reference', 'amount', 'verified', 'scans_used', 'created_at', 'expiry_date', 'is_active_status')
    list_filter = ('verified', 'created_at', 'expiry_date')
    search_fields = ('user__username', 'reference')
    readonly_fields = ('created_at', 'is_active_status')

    def is_active_status(self, obj):
        return obj.is_active()
    is_active_status.boolean = True
    is_active_status.short_description = 'Active?'


# Subscription Admin
@admin.register(Subscription)
class SubscriptionAdmin(admin.ModelAdmin):
    list_display = ('user', 'scans_remaining', 'expires_at', 'is_active', 'free_trial_used')
    list_filter = ('is_active', 'free_trial_used', 'expires_at')
    search_fields = ('user__username',)
    readonly_fields = ('is_valid',)

    def is_valid(self, obj):
        return obj.is_valid()
    is_valid.boolean = True
    is_valid.short_description = 'Valid?'
