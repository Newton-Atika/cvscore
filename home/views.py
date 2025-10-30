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
import nltk
from nltk.corpus import stopwords, wordnet
from nltk.tokenize import word_tokenize
from nltk.util import ngrams
from nltk import pos_tag, RegexpParser
from nltk.stem import WordNetLemmatizer

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
import re, io, json, base64
import matplotlib.pyplot as plt
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from openai import OpenAI
import nltk
from nltk.corpus import stopwords
from nltk.tokenize import word_tokenize
from nltk.util import ngrams
from nltk import pos_tag, RegexpParser
from nltk.stem import WordNetLemmatizer

# --- Sentence Transformers for semantic similarity ---
from sentence_transformers import SentenceTransformer, util

# Initialize SBERT model once
_sbert_model = SentenceTransformer("all-MiniLM-L6-v2")

# --- Utility functions ---
def safe_score(val):
    try:
        if isinstance(val, str):
            val = int(re.sub(r"[^0-9]", "", val) or 0)
        if not isinstance(val, (int, float)) or val != val:
            return 0
        return max(0, min(100, int(val)))
    except Exception:
        return 0


def safe_pie_chart(values, labels, colors, title=""):
    values = [0 if (not isinstance(v, (int, float)) or v != v) else v for v in values]
    total = sum(values)
    if total <= 0:
        values, labels, colors = [1], ["No Data"], ["gray"]

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
    from .models import Document, Subscription
    subscription, _ = Subscription.objects.get_or_create(user=request.user)
    if not subscription.free_trial_used:
        subscription.start_subscription(free=True)
    if not subscription.is_valid() or not subscription.deduct_scan():
        return redirect("initiate_payment")

    doc = get_object_or_404(Document, id=doc_id)

    # --- Call OpenAI (optional; just for structure consistency) ---
    prompt = f"""
You are an **ATS Scoring Engine** modeled after SkillSyncer.
Analyze the following CV against the job description and return strict JSON only.

Job Description:
{doc.job_description}

CV:
{doc.extracted_text}
"""
    try:
        client = OpenAI(api_key=settings.OPENAI_API_KEY)
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=1,
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

    try:
        json_str = re.search(r"\{.*\}", ai_text, re.DOTALL).group()
        ai_data = json.loads(json_str)
    except Exception:
        ai_data = {}

    # --- Default structure ---
    for k in [
        "match_percentage","matched_skills","missing_skills",
        "matched_keywords","missing_keywords","missing_experience",
        "missing_referees","missing_education","spelling_errors_count",
        "incomplete_text_snippets"
    ]:
        ai_data.setdefault(k, [] if "list" in str(type(ai_data.get(k))) else 0)

    # --- Ensure NLTK data is available ---
# --- Ensure NLTK data is available (handles all 3.9+ updates) ---
    try:
        nltk.data.find('tokenizers/punkt')
        nltk.data.find('tokenizers/punkt_tab')
        nltk.data.find('corpora/stopwords')
    # Try both tagger names (older/newer)
        try:
            nltk.data.find('taggers/averaged_perceptron_tagger_eng')
        except LookupError:
            nltk.data.find('taggers/averaged_perceptron_tagger')
    except LookupError:
        nltk.download('punkt', quiet=True)
        nltk.download('punkt_tab', quiet=True)
        nltk.download('stopwords', quiet=True)
    # Download both possible taggers (safe for all versions)
        nltk.download('averaged_perceptron_tagger', quiet=True)
        nltk.download('averaged_perceptron_tagger_eng', quiet=True)


    # --- Semantic similarity-based SkillSyncer+ scoring ---
    lemmatizer = WordNetLemmatizer()

    def preprocess_with_phrases(text):
        """Extract key words and phrases for semantic comparison."""
        text = (text or "").lower()
        text = re.sub(r'[^a-z0-9+\-\s]', ' ', text)
        tokens = word_tokenize(text)
        stop_words = set(stopwords.words('english'))
        tokens = [t for t in tokens if t not in stop_words and len(t) > 1]

        tagged = pos_tag(tokens)
        lemmatized = [lemmatizer.lemmatize(w) for w, _ in tagged]

        # Extract noun phrases (for meaningful phrases)
        grammar = "NP: {<JJ>*<NN.*>+}"
        cp = RegexpParser(grammar)
        tree = cp.parse(tagged)
        noun_phrases = [
            ' '.join(w for w, _ in subtree.leaves())
            for subtree in tree.subtrees(lambda t: t.label() == 'NP')
            if len(subtree.leaves()) > 1
        ]

        # Add bigrams/trigrams
        bigrams = [' '.join(bg) for bg in ngrams(lemmatized, 2)]
        trigrams = [' '.join(tg) for tg in ngrams(lemmatized, 3)]

        all_terms = set(lemmatized + bigrams + trigrams + noun_phrases)
        return list(all_terms)

    def semantic_match_score(jd_text, cv_text, threshold=0.72):
        jd_terms = preprocess_with_phrases(jd_text)
        cv_terms = preprocess_with_phrases(cv_text)

        if not jd_terms or not cv_terms:
            return 0.0, set()

        jd_emb = _sbert_model.encode(jd_terms, convert_to_tensor=True)
        cv_emb = _sbert_model.encode(cv_terms, convert_to_tensor=True)
        cos_scores = util.cos_sim(jd_emb, cv_emb)

        matched_terms = set()
        for i, jd_term in enumerate(jd_terms):
            if float(max(cos_scores[i])) >= threshold:
                matched_terms.add(jd_term)

        match_percentage = (len(matched_terms) / len(jd_terms)) * 100
        return match_percentage, matched_terms

    # --- Compute semantic match ---
    jd_text = doc.job_description or ""
    cv_text = doc.extracted_text or ""
    match_percentage, matched_terms = semantic_match_score(jd_text, cv_text)
    ai_data["match_percentage"] = safe_score(match_percentage)

    print(f"Semantic Match Score: {match_percentage:.2f}%")
    print(f"Matched Terms: {list(matched_terms)[:30]}")

    # --- Charts ---
    skills_chart = safe_pie_chart(
        [len(ai_data["matched_skills"]), len(ai_data["missing_skills"])],
        ["Matched Skills", "Missing Skills"], ["limegreen", "red"], "Skills Analysis"
    )
    experience_chart = safe_pie_chart(
        [len(ai_data["matched_keywords"]), len(ai_data["missing_experience"])],
        ["Relevant Experience", "Missing Experience"], ["limegreen", "orange"], "Work Experience Analysis"
    )
    education_chart = safe_pie_chart(
        [1 if not ai_data["missing_education"] else 0, 1 if ai_data["missing_education"] else 0],
        ["Present", "Missing"], ["limegreen", "red"], "Education Analysis"
    )
    overall_chart = safe_pie_chart(
        [ai_data["match_percentage"], 100 - ai_data["match_percentage"]],
        ["Score Achieved", "Remaining"], ["limegreen", "gray"], "Overall CV Match Score"
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









































