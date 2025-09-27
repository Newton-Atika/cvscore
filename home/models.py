from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone
from datetime import timedelta


class Document(models.Model):
    extracted_text = models.TextField()  # CV text only
    job_description = models.TextField(blank=True, null=True)  # pasted JD
    uploaded_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"CV uploaded on {self.uploaded_at}"


class Payment(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    reference = models.CharField(max_length=200, blank=True, null=True)
    verified = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    expiry_date = models.DateTimeField(null=True, blank=True)
    scans_used = models.IntegerField(default=0)  # ✅ New field

    def is_active(self):
        """Check if subscription is active and scans are remaining."""
        if not self.verified or not self.expiry_date:
            return False
        return timezone.now() < self.expiry_date and self.scans_used < 120

    def save(self, *args, **kwargs):
        # Automatically set expiry date when verifying payment
        if self.verified and not self.expiry_date:
            self.expiry_date = timezone.now() + timedelta(days=30)
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.user.username} - {self.reference} - Verified: {self.verified}"


class Subscription(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    scans_remaining = models.IntegerField(default=0)
    expires_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=False)
    free_trial_used = models.BooleanField(default=False)

    def start_subscription(self, free=False, payment_ref=None):
        """Start subscription after free trial or payment."""
        if free:
            # Only 1 free scan for first-time users
            self.scans_remaining = 1
            self.expires_at = timezone.now() + timedelta(days=1)
            self.is_active = True
            self.free_trial_used = True
        else:
            # Paid: 120 scans for 30 days
            self.scans_remaining = 120
            self.expires_at = timezone.now() + timedelta(days=30)
            self.is_active = True

            # Record payment if reference given
            if payment_ref:
                Payment.objects.create(
                    user=self.user,
                    amount=150,
                    reference=payment_ref,
                    verified=True
                )
        self.save()

    def deduct_scan(self):
        """Deduct a scan if subscription is valid."""
        if self.is_valid():
            self.scans_remaining -= 1
            if self.scans_remaining <= 0:
                self.is_active = False
            self.save()
            return True
        return False

    def is_valid(self):
        """Check if subscription is active & not expired."""
        return (
            self.is_active
            and self.expires_at
            and self.expires_at >= timezone.now()
            and self.scans_remaining > 0
        )

    def __str__(self):
        return f"{self.user.username} - Active: {self.is_active}, Scans left: {self.scans_remaining}"
