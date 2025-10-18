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
from openai import OpenAI
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

def home(request):
    return render(request, "home/home.html")
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
    # ✅ Get or create subscription for logged-in user
    subscription, _ = Subscription.objects.get_or_create(user=request.user)

    # Calculate subscription status
    subscription_active = subscription.is_valid()
    days_left = (subscription.expires_at - timezone.now()).days if subscription.expires_at else 0
    scans_left = subscription.scans_remaining

    form = DocumentForm(request.POST or None, request.FILES or None)
    error = None

    if request.method == "POST" and form.is_valid():
        uploaded_file = request.FILES['file']
        with tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(uploaded_file.name)[1]) as tmp_file:
            for chunk in uploaded_file.chunks():
                tmp_file.write(chunk)
            temp_path = tmp_file.name

        extracted_text = extract_text_from_file(temp_path)
        os.remove(temp_path)

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
        "error": error,
        # ✅ Pass subscription details to the template
        "subscription_active": subscription_active,
        "days_left": days_left,
        "scans_left": scans_left,
    })
def safe_score(val):
    """Sanitize score (NaN, string, out of bounds)."""
    try:
        if isinstance(val, str):
            val = int(re.sub(r"[^0-9]", "", val) or 0)
        if not isinstance(val, (int, float)) or val != val:  # NaN
            return 0
        return max(0, min(100, int(val)))
    except Exception:
        return 0


def safe_pie_chart(values, labels, colors, title=""):
    """Create safe pie chart → returns base64 string."""
    # Replace NaN/invalid with 0
    values = [0 if (not isinstance(v, (int, float)) or v != v) else v for v in values]
    total = sum(values)

    if total <= 0:
        values = [1]
        labels = ["No Data"]
        colors = ["gray"]

    plt.figure(figsize=(4, 4))
    plt.pie(values, labels=labels, colors=colors, autopct="%1.0f%%", startangle=90)
    plt.title(title, color="limegreen")

    buf = io.BytesIO()
    plt.savefig(buf, format="png", transparent=True)
    plt.close()
    buf.seek(0)
    return base64.b64encode(buf.getvalue()).decode("utf-8")

def calculate_weighted_score(data):
    """
    Calculate a weighted ATS-style score that sums to 100.
    - Skills & Tools (hard + tools) => 60
    - Soft skills => 20
    - Relevant experience => 10
    - Education relevance => 5
    - ATS formatting compliance => 5
    """
    # Extract values with safe defaults
    hard_matched = data.get("hard_skills_matched", 0)
    hard_missing = data.get("hard_skills_missing", 0)
    soft_matched = data.get("soft_skills_matched", 0)
    soft_missing = data.get("soft_skills_missing", 0)
    tools_matched = data.get("tools_matched", 0)
    tools_missing = data.get("tools_missing", 0)
    exp_matched = data.get("experience_matched", 0)
    exp_missing = data.get("experience_missing", 0)
    education_relevant = data.get("education_relevant", False)
    ats_issues = data.get("ats_formatting_issues", [])

    # Totals (avoid division by zero)
    skills_matched = hard_matched + tools_matched
    skills_total = (hard_matched + hard_missing) + (tools_matched + tools_missing)

    soft_total = soft_matched + soft_missing
    exp_total = exp_matched + exp_missing

    # Weights (sum to 100)
    SKILLS_WEIGHT = 60.0
    SOFT_WEIGHT = 20.0
    EXP_WEIGHT = 10.0
    EDU_WEIGHT = 5.0
    ATS_WEIGHT = 5.0

    # Compute sub-scores
    skills_score = (skills_matched / skills_total) * SKILLS_WEIGHT if skills_total else 0
    soft_score = (soft_matched / soft_total) * SOFT_WEIGHT if soft_total else 0
    exp_score = (exp_matched / exp_total) * EXP_WEIGHT if exp_total else 0
    edu_score = EDU_WEIGHT if education_relevant else 0

    # ATS formatting: map number of issues to a score out of ATS_WEIGHT
    # We'll deduct 1 point (of the ATS_WEIGHT) per issue up to the ATS_WEIGHT.
    ats_issue_count = len(ats_issues) if ats_issues is not None else 0
    ats_score = max(0.0, ATS_WEIGHT - min(ats_issue_count, int(ATS_WEIGHT)))

    final = skills_score + soft_score + exp_score + edu_score + ats_score
    return safe_score(final)

import json
import re
import logging

logger = logging.getLogger(__name__)

def _int_safe(v):
    try:
        if isinstance(v, (int, float)):
            return int(v)
        if isinstance(v, str):
            s = re.sub(r"[^0-9\-]", "", v)
            return int(s) if s not in ("", "-") else 0
        return int(v)
    except Exception:
        return 0

def _bool_safe(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("true", "yes", "1")
    return bool(v)

def _list_safe(v):
    # Normalizes lists returned as lists or CSV/newline strings
    if v is None:
        return []
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    if isinstance(v, (int, float)):
        return []
    if isinstance(v, str):
        text = v.strip()
        if not text:
            return []
        # try JSON array
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return [str(x).strip() for x in parsed if str(x).strip()]
        except Exception:
            pass
        # fallback split by newline or comma or semicolon
        if "\n" in text:
            parts = [p.strip() for p in text.splitlines() if p.strip()]
            if parts:
                return parts
        # comma separated
        parts = [p.strip() for p in re.split(r",|;", text) if p.strip()]
        if parts:
            return parts
    return []

@login_required
def score_cv(request, doc_id):
    # --- Subscription checks ---
    subscription, _ = Subscription.objects.get_or_create(user=request.user)
    if not subscription.free_trial_used:
        subscription.start_subscription(free=True)
    if not subscription.is_valid() or not subscription.deduct_scan():
        return redirect("initiate_payment")

    # --- Get document ---
    doc = get_object_or_404(Document, id=doc_id)

    # --- Enhanced Prompt (counts + arrays required) ---
    system_msg = (
        "You are an ATS extractor. Return ONLY a single JSON object in the response body "
        "and nothing else. Include numeric counts AND arrays for the items. Arrays must "
        "be valid JSON arrays or newline/comma separated strings. Example format:\n\n"
        '{'
        '"hard_skills_matched": 3, "hard_skills_missing": 2, '
        '"soft_skills_matched": 2, "soft_skills_missing": 1, '
        '"tools_matched": 2, "tools_missing": 1, '
        '"experience_matched": 4, "experience_missing": 2, '
        '"education_relevant": false, '
        '"ats_formatting_issues": ["Table detected", "Icons used"], '
        '"matched_skills": ["Excel", "Power BI"], '
        '"missing_skills": ["SQL"], '
        '"matched_keywords": ["reporting", "dashboard"], '
        '"missing_keywords": ["ETL"], '
        '"missing_experience": ["No procurement experience"], '
        '"missing_referees": [], "missing_education": [], '
        '"spelling_errors_count": 1, '
        '"incomplete_text_snippets": ["[left school]"]'
        '}'
    )

    user_prompt = f"""
You are an ATS (Applicant Tracking System) analyzer. Analyze the Job Description and CV below.
Return ONLY a JSON object as described in the system message (no extra explanation).

Job Description:
{doc.job_description}

CV (extracted text):
{doc.extracted_text}
    """

    # --- Call OpenAI (deterministic) ---
    try:
        client = OpenAI(api_key=settings.OPENAI_API_KEY)
        response = client.chat.completions.create(
            model="gpt-5-nano",
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.0,
            max_tokens=800,
        )
        ai_text = response.choices[0].message.content.strip()
    except Exception as e:
        logger.exception("OpenAI error in score_cv")
        ai_text = "{}"

    # --- Debug log for development (remove in production) ---
    logger.debug("AI raw output for score_cv (doc %s): %s", doc_id, ai_text[:4000])

    # --- Parse AI JSON safely (first '{' to last '}' ) ---
    ai_data = {}
    try:
        start = ai_text.find("{")
        end = ai_text.rfind("}")
        if start != -1 and end != -1 and end >= start:
            json_str = ai_text[start:end+1]
            ai_data = json.loads(json_str)
        else:
            ai_data = {}
    except Exception as e:
        logger.warning("Failed to parse AI JSON; ai_text=%s", ai_text[:1000])
        try:
            # fallback: try to load the whole thing (if it's pure JSON)
            ai_data = json.loads(ai_text)
        except Exception:
            ai_data = {}

    # --- Safe defaults and coercion ---
    # Lists
    ai_data["matched_skills"] = _list_safe(ai_data.get("matched_skills"))
    ai_data["missing_skills"] = _list_safe(ai_data.get("missing_skills"))
    ai_data["matched_keywords"] = _list_safe(ai_data.get("matched_keywords"))
    ai_data["missing_keywords"] = _list_safe(ai_data.get("missing_keywords"))
    ai_data["missing_experience"] = _list_safe(ai_data.get("missing_experience"))
    ai_data["missing_referees"] = _list_safe(ai_data.get("missing_referees"))
    ai_data["missing_education"] = _list_safe(ai_data.get("missing_education"))
    ai_data["incomplete_text_snippets"] = _list_safe(ai_data.get("incomplete_text_snippets"))
    ai_data["ats_formatting_issues"] = _list_safe(ai_data.get("ats_formatting_issues"))

    # Numbers / booleans
    ai_data["hard_skills_matched"] = _int_safe(ai_data.get("hard_skills_matched", len(ai_data["matched_skills"])))
    ai_data["hard_skills_missing"] = _int_safe(ai_data.get("hard_skills_missing", len(ai_data["missing_skills"])))
    ai_data["soft_skills_matched"] = _int_safe(ai_data.get("soft_skills_matched"))
    ai_data["soft_skills_missing"] = _int_safe(ai_data.get("soft_skills_missing"))
    ai_data["tools_matched"] = _int_safe(ai_data.get("tools_matched"))
    ai_data["tools_missing"] = _int_safe(ai_data.get("tools_missing"))
    ai_data["experience_matched"] = _int_safe(ai_data.get("experience_matched", len(ai_data["matched_keywords"])))
    ai_data["experience_missing"] = _int_safe(ai_data.get("experience_missing", len(ai_data["missing_experience"])))
    ai_data["education_relevant"] = _bool_safe(ai_data.get("education_relevant", False))
    ai_data["spelling_errors_count"] = _int_safe(ai_data.get("spelling_errors_count", 0))

    # If the model provided arrays but counts were missing/zero, overwrite counts with list lengths
    try:
        if ai_data.get("matched_skills") and ai_data.get("hard_skills_matched", 0) == 0:
            ai_data["hard_skills_matched"] = len(ai_data["matched_skills"])
        if ai_data.get("missing_skills") and ai_data.get("hard_skills_missing", 0) == 0:
            ai_data["hard_skills_missing"] = len(ai_data["missing_skills"])
        if ai_data.get("matched_keywords") and ai_data.get("experience_matched", 0) == 0:
            ai_data["experience_matched"] = len(ai_data["matched_keywords"])
        if ai_data.get("missing_experience") and ai_data.get("experience_missing", 0) == 0:
            ai_data["experience_missing"] = len(ai_data["missing_experience"])
    except Exception:
        pass

    # Ensure lists exist (never None)
    for k in [
        "matched_skills", "missing_skills", "matched_keywords", "missing_keywords",
        "missing_experience", "missing_referees", "missing_education",
        "incomplete_text_snippets", "ats_formatting_issues"
    ]:
        ai_data.setdefault(k, [])

    # --- Final score calculation (Overriding AI) ---
    ai_data["match_percentage"] = calculate_weighted_score(ai_data)

    # --- Pie charts (unchanged) ---
    skills_chart = safe_pie_chart(
        [len(ai_data["matched_skills"]), len(ai_data["missing_skills"])],
        ["Matched Skills", "Missing Skills"],
        ["limegreen", "red"],
        "Skills Analysis"
    )

    experience_chart = safe_pie_chart(
        [len(ai_data["matched_keywords"]), len(ai_data["missing_experience"])],
        ["Relevant Experience", "Missing Experience"],
        ["limegreen", "orange"],
        "Work Experience Analysis"
    )

    education_chart = safe_pie_chart(
        [1 if not ai_data["missing_education"] else 0, 1 if ai_data["missing_education"] else 0],
        ["Present", "Missing"],
        ["limegreen", "red"],
        "Education Analysis"
    )

    overall_chart = safe_pie_chart(
        [ai_data["match_percentage"], 100 - ai_data["match_percentage"]],
        ["Score Achieved", "Remaining"],
        ["limegreen", "gray"],
        "Overall CV Match Score"
    )

    return render(request, "home/cv_score.html", {
        "document": doc,
        "ai_data": ai_data,
        "skills_chart": skills_chart,
        "experience_chart": experience_chart,
        "education_chart": education_chart,
        "overall_chart": overall_chart,
        "scans_left": subscription.scans_remaining,
        "expires_at": subscription.expires_at,
    })

    
@login_required
def document_list(request):
    documents = Document.objects.all().order_by("-uploaded_at")
    return render(request, "home/list.html", {"documents": documents})







