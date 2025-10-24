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

    # --- Prepare AI prompt with explicit example ---
    prompt = f"""
You are an **ATS Scoring Engine** modeled strictly after SkillSyncer.
You MUST be conservative and deterministic. Always apply the same scoring logic.
Never use creative interpretation. Never assume a match unless it is explicit.
Do not paraphrase or rename skills, experiences, or keywords — use them exactly as written.

Skills scoring criteria.
- You must look at every single word and phrase used.
- Look at every single word and phrase used both in the CV and job description. If a word or phrase is a skill, then count it as a skill.
- Those skills in the Job description and not in the CV will count as missing skills.
- Those skills in both the job description and Cv, they will count as match skills.

## STRICT RULES
- Count a skill as matched **only if explicitly written** and used in a context sentence.
- If a JD duty lacks proof of execution in the CV, it is **missing experience**.
- Keywords only count if used in context; isolated mentions = weak match.
- Education only counts if the degree and field directly match.
- Referees: must have at least 2 with both name and contact info.
- Penalize missing section headers, dense paragraphs, inconsistent dates.
- Deduct for phrases like “responsible for” without measurable proof.
- Never give benefit of doubt; if unsure → count as missing.

## OUTPUT FORMAT (strict JSON only)
{{
    "match_percentage": <integer>,
    "matched_skills": [...],
    "missing_skills": [...],
    "matched_keywords": [...],
    "missing_keywords": [...],
    "missing_experience": [...],
    "missing_referees": [...],
    "missing_education": [...],
    "spelling_errors_count": <integer>,
    "incomplete_text_snippets": [...]
}}
Analyze the following CV against the Job Description and return **ONLY valid JSON** — no commentary, no markdown.

Job Description:
{doc.job_description}

CV:
{doc.extracted_text}
"""




    # --- Call OpenAI ---
    try:
        client = OpenAI(api_key=settings.OPENAI_API_KEY)
        response = client.chat.completions.create(
            model="gpt-4o-mini",  # use a valid model
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            top_p=0.1,
            timeout=20,
            seed=1234,
            stream=True
         )

        full_response = ""
        for chunk in response:
            delta = chunk.choices[0].delta
            if delta and delta.content:
                full_response += delta.content

        ai_text = full_response.strip()

    except Exception as e:
        print("OpenAI error:", str(e))
        ai_text = "{}"


    # --- Parse AI JSON safely ---
    try:
        json_str = re.search(r"\{.*\}", ai_text, re.DOTALL).group()
        ai_data = json.loads(json_str)
    except Exception:
        ai_data = {}

    # --- Safe defaults ---
    ai_data.setdefault("match_percentage", 0)
    ai_data.setdefault("matched_skills", [])
    ai_data.setdefault("missing_skills", [])
    ai_data.setdefault("matched_keywords", [])
    ai_data.setdefault("missing_keywords", [])
    ai_data.setdefault("missing_experience", [])
    ai_data.setdefault("missing_referees", [])
    ai_data.setdefault("missing_education", [])
    ai_data.setdefault("spelling_errors_count", 0)
    ai_data.setdefault("incomplete_text_snippets", [])

    matched_skills = ai_data["matched_skills"]
    missing_skills = ai_data["missing_skills"]
    matched_keywords = ai_data["matched_keywords"]
    missing_keywords = ai_data["missing_keywords"]
    missing_experience = ai_data["missing_experience"]
    missing_referees = ai_data["missing_referees"]
    missing_education = ai_data["missing_education"]
    spelling_errors_count = ai_data["spelling_errors_count"]
    incomplete_text_snippets = ai_data["incomplete_text_snippets"]
    # Skills (35%)
    skills_total = len(matched_skills) + len(missing_skills)
    skills_raw_score = (len(matched_skills) / skills_total) * 100 if skills_total > 0 else 0

# Experience (40%)
    experience_total = len(matched_keywords) + len(missing_experience)
    experience_raw_score = (len(matched_keywords) / experience_total) * 100 if experience_total > 0 else 0

# Keywords (10%)
    keywords_total = len(matched_keywords) + len(missing_keywords)
    keywords_raw_score = (len(matched_keywords) / keywords_total) * 100 if keywords_total > 0 else 0

# Education (5%)
    education_raw_score = 100 if not missing_education else 0

# Completion / ATS Health (10%)
    completion_raw_score = 100
    if missing_referees: completion_raw_score -= 40  # -40% if referees don't meet 2-with-contact rule
    if spelling_errors_count > 3: completion_raw_score -= 20  # penalize
    if incomplete_text_snippets: completion_raw_score -= 20  # penalize for broken formatting

# Final Weighted Score
    final_score = (
        skills_raw_score * 0.50 +
        experience_raw_score * 0.10 +
        keywords_raw_score * 0.30 +
        education_raw_score * 0.05 +
        completion_raw_score * 0.05
    )

    #ai_data["match_percentage"] = safe_score(ai_data["match_percentage"])
    calculated_score = round(final_score)  # Or int(final_score) for floor

# Override AI score with backend authoritative score
    ai_data["match_percentage"] = safe_score(calculated_score)
    # --- Pie charts ---
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






















