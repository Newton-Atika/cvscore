from django.shortcuts import render, redirect, get_object_or_404
from django.conf import settings
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import AuthenticationForm
from django.views.decorators.http import require_http_methods
from django.utils import timezone
from datetime import timedelta
import requests, tempfile, os, json, re, io, base64
import matplotlib
import matplotlib.pyplot as plt
import openai
from .forms import DocumentForm, CustomUserCreationForm
from .models import Document, Payment, Subscription
from .utils import extract_text_from_file
import requests
matplotlib.use('Agg')
import certifi

PAYSTACK_SECRET_KEY = settings.PAYSTACK_SECRET_KEY


# ---------- AUTH ----------
def signup_view(request):
    next_url = request.GET.get('next', '/')
    if request.method == "POST":
        form = CustomUserCreationForm(request.POST)
        if form.is_valid():
            user = form.save()
            # create subscription record immediately
            Subscription.objects.create(user=user)
            login(request, user)
            return redirect(request.POST.get('next') or '/')
    else:
        form = CustomUserCreationForm()
    return render(request, 'home/signup.html', {'form': form, 'next': next_url})


def login_view(request):
    next_url = request.GET.get('next', '/')
    if request.method == "POST":
        form = AuthenticationForm(request, data=request.POST)
        if form.is_valid():
            user = form.get_user()
            login(request, user)
            return redirect(request.POST.get('next') or next_url)
    else:
        form = AuthenticationForm()
    return render(request, 'home/login.html', {'form': form, 'next': next_url})


@require_http_methods(["GET", "POST"])
def logout_view(request):
    logout(request)
    return redirect('/')


# ---------- PAYMENTS ----------
PAYSTACK_SECRET_KEY = settings.PAYSTACK_SECRET_KEY

@login_required
def initiate_payment(request):
    """Initiate Paystack payment for subscription (test key, local SSL fix)"""
    user = request.user
    subscription, _ = Subscription.objects.get_or_create(user=user)

    # If subscription is still valid → go to upload
    if subscription.is_valid():
        return redirect('upload_document')

    # First CV free trial
    if not subscription.free_trial_used:
        subscription.start_subscription(free=True)
        return redirect('upload_document')

    # Otherwise → force Paystack payment
    callback_url = request.build_absolute_uri('/verify_payment/')
    reference = f"{user.id}-{timezone.now().timestamp()}"
    data = {
        "email": user.email,
        "amount": 15000,  # KES 150
        "callback_url": callback_url,
        "reference": reference,
    }
    headers = {"Authorization": f"Bearer {PAYSTACK_SECRET_KEY}"}

    try:
        # Use certifi bundle to avoid SSL errors
        response = requests.post(
            "https://api.paystack.co/transaction/initialize",
            headers=headers,
            json=data,
            verify=certifi.where()  # <--- this avoids SSL verify errors
        )
        resp_data = response.json()
    except requests.exceptions.SSLError as e:
        # Handle SSL error gracefully
        return render(request, "home/payment_error.html", {
            "message": f"SSL Error: {e}\nGo back to <a href='/upload_document/'>Upload Page</a>"
        })
    except Exception as e:
        return render(request, "home/payment_error.html", {
            "message": f"Payment request failed: {e}\nGo back to <a href='/upload_document/'>Upload Page</a>"
        })

    if resp_data.get("status"):
        Payment.objects.create(user=user, amount=150, reference=reference, verified=False)
        return redirect(resp_data["data"]["authorization_url"])
    else:
        return render(request, "home/payment_error.html", {"message": resp_data.get("message")})

@login_required
def verify_payment(request):
    """Verify Paystack payment callback"""
    reference = request.GET.get("reference")
    headers = {"Authorization": f"Bearer {PAYSTACK_SECRET_KEY}"}

    try:
        response = requests.get(
            f"https://api.paystack.co/transaction/verify/{reference}",
            headers=headers,
            verify=certifi.where()  # avoid SSL error
        )
        resp_data = response.json()
    except requests.exceptions.SSLError as e:
        return render(request, "home/payment_error.html", {
            "message": f"SSL Error: {e}\nGo back to <a href='/upload_document/'>Upload Page</a>"
        })
    except Exception as e:
        return render(request, "home/payment_error.html", {
            "message": f"Verification failed: {e}\nGo back to <a href='/upload_document/'>Upload Page</a>"
        })

    if resp_data.get("status") and resp_data["data"]["status"] == "success":
        payment = Payment.objects.get(reference=reference)
        payment.verified = True
        payment.expiry_date = timezone.now() + timedelta(days=30)
        payment.save()

        # Activate subscription
        subscription, _ = Subscription.objects.get_or_create(user=payment.user)
        subscription.start_subscription(free=False, payment_ref=reference)

        return redirect('upload_document')
    else:
        return render(request, "home/payment_error.html", {
            "message": "Payment verification failed. Go back to <a href='/upload_document/'>Upload Page</a>"
        })

# ---------- DOCUMENT FLOW ----------
import re

def clean_text(text):
    """Remove NULL bytes and non-printable characters."""
    if not text:
        return ""
    text = text.replace("\x00", "")
    return re.sub(r"[\x00-\x1f\x7f]", "", text)
@login_required(login_url='login')
def upload_document(request):
    subscription, _ = Subscription.objects.get_or_create(user=request.user)

    # Redirect to payment if subscription invalid and free trial already used
    if not subscription.is_valid() and subscription.free_trial_used:
        return redirect("initiate_payment")

    days_left = (subscription.expires_at - timezone.now()).days if subscription.expires_at else 0
    scans_left = subscription.scans_remaining

    form = DocumentForm(request.POST or None, request.FILES or None)
    error = None

    if request.method == "POST" and form.is_valid():
        if not subscription.deduct_scan():
            return redirect("initiate_payment")

        uploaded_file = request.FILES['file']
        with tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(uploaded_file.name)[1]) as tmp_file:
            for chunk in uploaded_file.chunks():
                tmp_file.write(chunk)
            temp_path = tmp_file.name

        extracted_text = extract_text_from_file(temp_path)
        os.remove(temp_path)

        # ✅ Clean the extracted text before saving
        extracted_text = clean_text(extracted_text)

        if not extracted_text.strip():
            error = "Could not extract text from the uploaded file."

        doc = Document.objects.create(
            extracted_text=extracted_text,
            job_description=clean_text(form.cleaned_data.get("job_description", ""))
        )

        return render(request, "home/text_view.html", {
            "document": doc,
            "extracted_text": doc.extracted_text,
            "job_description": doc.job_description
        })

    return render(request, "home/upload.html", {
        "form": form,
        "days_left": days_left,
        "scans_left": scans_left,
        "subscription_active": subscription.is_valid(),
        "error": error
    })


@login_required
def score_cv(request, doc_id):
    subscription, _ = Subscription.objects.get_or_create(user=request.user)

    # First scan free: start subscription if never used
    if not subscription.free_trial_used:
        subscription.start_subscription(free=True)

    # Check if subscription valid
    if not subscription.is_valid():
        return redirect('initiate_payment')  # redirect to pay

    # Deduct scan (only once)
    if not subscription.deduct_scan():
        return redirect('initiate_payment')  # no scans left

    # Get document
    doc = get_object_or_404(Document, id=doc_id)

    # Prompt
    prompt = f"""
    Compare the following CV with the Job Description.
    Output JSON ONLY, no extra text.

    Fields required:
    - match_percentage (number 0-100)
    - matched_skills (list of strings)
    - missing_skills (list of strings)
    - missing_experience (list of strings)

    Job Description:
    {doc.job_description}

    CV:
    {doc.extracted_text}
    """

    try:
        from openai import OpenAI
        client = OpenAI(api_key=settings.OPENAI_API_KEY)
        response = client.chat.completions.create(
            model="gpt-5-nano",
            messages=[{"role": "user", "content": prompt}],
            temperature=0
        )
        ai_text = response.choices[0].message.content.strip()
    except Exception as e:
        print("OpenAI error:", str(e))
        ai_text = '{"match_percentage":0,"matched_skills":[],"missing_skills":[],"missing_experience":[]}'

    # Parse AI response safely
    try:
        json_str = re.search(r"\{.*\}", ai_text, re.DOTALL).group()
        ai_data = json.loads(json_str)
    except Exception:
        ai_data = {"match_percentage": 0, "matched_skills": [], "missing_skills": [], "missing_experience": []}

    # Ensure match_percentage is an integer
    if isinstance(ai_data.get("match_percentage"), str):
        ai_data["match_percentage"] = int(re.sub(r"[^0-9]", "", ai_data["match_percentage"]) or 0)

    # Chart data
    categories = ["Matched Skills", "Missing Skills", "Missing Experience"]
    values = [
        len(ai_data.get("matched_skills", [])),
        len(ai_data.get("missing_skills", [])),
        len(ai_data.get("missing_experience", []))
    ]

    plt.figure(figsize=(6, 4))
    plt.bar(categories, values, color=["limegreen", "orange", "red"])
    plt.title(f"CV vs Job Description Analysis ({ai_data.get('match_percentage', 0)}%)")
    plt.ylabel("Count")

    # Convert plot to base64
    buf = io.BytesIO()
    plt.savefig(buf, format='png')
    plt.close()
    buf.seek(0)
    image_base64 = base64.b64encode(buf.getvalue()).decode('utf-8')

    return render(request, "home/cv_score.html", {
        "document": doc,
        "ai_data": ai_data,
        "chart": image_base64,
        "scans_left": subscription.scans_remaining,
        "expires_at": subscription.expires_at
    })


@login_required
def document_list(request):
    documents = Document.objects.all().order_by("-uploaded_at")
    return render(request, "home/list.html", {"documents": documents})







