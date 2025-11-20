# nlp_engine.py
import re
import gc
import json
import nltk
import hashlib
import numpy as np
from functools import lru_cache
from nltk.corpus import stopwords, wordnet
from nltk.stem import WordNetLemmatizer
from nltk import pos_tag, word_tokenize, RegexpParser
from nltk.util import ngrams
from sentence_transformers import SentenceTransformer, util
import torch

# -----------------------
# NLTK downloads (once)
# -----------------------
try:
    nltk.data.find('tokenizers/punkt')
    nltk.data.find('tokenizers/punkt_tab')
    nltk.data.find('corpora/stopwords')
    nltk.data.find('corpora/wordnet')
    nltk.data.find('taggers/averaged_perceptron_tagger_eng')
except LookupError:
    nltk.download('punkt', quiet=True)
    nltk.download('punkt_tab', quiet=True)
    nltk.download('stopwords', quiet=True)
    nltk.download('wordnet', quiet=True)
    nltk.download('averaged_perceptron_tagger_eng', quiet=True)

# -----------------------
# Lightweight NLP globals
# -----------------------
lemmatizer = WordNetLemmatizer()
stop_words = set(stopwords.words('english'))
grammar = "NP: {<JJ>*<NN.*>+}"
parser = RegexpParser(grammar)

# -----------------------
# SentenceTransformer (load once)
# - device="cpu" to avoid accidental CUDA allocations on Railway
# - keep model small (all-MiniLM-L6-v2) — you had this already
# -----------------------
# Reduce parallelism to avoid extra memory threads
torch.set_num_threads(1)
embedding_model = SentenceTransformer('all-MiniLM-L6-v2', device="cpu")

# -----------------------
# Helpers
# -----------------------
def _truncate_text_for_embedding(text: str, max_chars: int = 4000) -> str:
    """
    Prevent extremely long inputs (huge attention memory). Keep start + end.
    Default max_chars tuned to preserve meaning but cap memory use.
    """
    if not text:
        return ""
    if len(text) <= max_chars:
        return text
    head = text[: int(max_chars * 0.6)]
    tail = text[- int(max_chars * 0.4):]
    return head + "\n\n" + tail

@lru_cache(maxsize=256)
def _cached_embedding(text_hash: str, text_value: str) -> np.ndarray:
    """
    Internal LRU cache keyed by stable hash. Stores NumPy arrays (small footprint).
    lru_cache requires hashable args; we pass hash + text string to avoid collisions.
    """
    # Use no_grad() to prevent any gradient allocations
    with torch.no_grad():
        emb = embedding_model.encode(text_value, convert_to_numpy=True)
    # Ensure float32 numpy array
    emb = np.asarray(emb, dtype=np.float32)
    return emb

def _get_embedding(text: str) -> np.ndarray:
    """
    Public helper to get embedding with caching and truncation.
    """
    if not text:
        return np.zeros((embedding_model.get_sentence_embedding_dimension(),), dtype=np.float32)

    t = _truncate_text_for_embedding(text, max_chars=4000)
    # Use a short hash + original to avoid very long keys in cache while still safe
    h = hashlib.sha256(t.encode("utf-8")).hexdigest()
    return _cached_embedding(h, t)

# -----------------------
# POS helper (same as before)
# -----------------------
def get_wordnet_pos(tag):
    if tag.startswith('J'): return wordnet.ADJ
    elif tag.startswith('V'): return wordnet.VERB
    elif tag.startswith('N'): return wordnet.NOUN
    elif tag.startswith('R'): return wordnet.ADV
    else: return wordnet.NOUN

# -----------------------
# Preprocess pipeline (synonyms removed as requested)
# -----------------------
def preprocess_with_phrases_nouns_synonyms(text: str):
    """
    Returns weighted_terms mapping exactly as before (no synonyms).
    Preserves tokenization, lemmatization, noun phrases, bigrams, trigrams.
    Memory-conscious: removes exact duplicate tokens early.
    """
    if not text:
        return {}

    text = text.lower()
    text = re.sub(r'[^a-z0-9+\-\s]', ' ', text)

    words = word_tokenize(text)
    # remove stopwords and short tokens
    words = [w for w in words if w not in stop_words and len(w) > 1]

    # Early dedupe to reduce ngram explosion while preserving order
    seen = set()
    deduped_words = []
    for w in words:
        if w in seen:
            continue
        seen.add(w)
        deduped_words.append(w)

    tagged = pos_tag(deduped_words)
    lemmatized = [lemmatizer.lemmatize(w, get_wordnet_pos(t)) for w, t in tagged]

    # Noun phrase extraction (identical)
    tree = parser.parse(tagged)
    noun_phrases = []
    for subtree in tree.subtrees(filter=lambda t: t.label() == 'NP'):
        np = ' '.join(word for word, pos in subtree.leaves())
        if len(np.split()) > 1:
            noun_phrases.append(np)

    # Bi / Tri grams (use generators to avoid large temporaries if needed)
    bigrams = [' '.join(bg) for bg in ngrams(lemmatized, 2)] if len(lemmatized) >= 2 else []
    trigrams = [' '.join(tg) for tg in ngrams(lemmatized, 3)] if len(lemmatized) >= 3 else []

    # Weighted term map (no synonyms)
    weighted_terms = {w: 1 for w in lemmatized}
    weighted_terms.update({bg: 2 for bg in bigrams})
    weighted_terms.update({tg: 3 for tg in trigrams})
    weighted_terms.update({np: 4 for np in noun_phrases})

    # free large temporaries ASAP
    del words, deduped_words, tagged, tree, bigrams, trigrams
    gc.collect()

    return weighted_terms

# -----------------------
# Skillmatcher (same logic)
# -----------------------
def calculate_skillmatcher_plus_score(job_description: str, cv_text: str):
    jd_terms = preprocess_with_phrases_nouns_synonyms(job_description or "")
    cv_terms = preprocess_with_phrases_nouns_synonyms(cv_text or "")

    if not jd_terms:
        return 0, set()

    common = set(jd_terms.keys()).intersection(set(cv_terms.keys()))
    matched_weight = sum(jd_terms[t] for t in common)
    total_weight = sum(jd_terms.values())

    score = (matched_weight / total_weight) * 100 if total_weight else 0

    # free temporaries
    del jd_terms, cv_terms
    gc.collect()

    return round(score, 2), common

# -----------------------
# Semantic embedding similarity optimized (numpy + cached embeddings)
# -----------------------
def compute_semantic_similarity(job_description: str, cv_text: str) -> float:
    try:
        # short circuit empty
        if not (job_description or cv_text):
            return 0.0

        jd_text = (job_description or "").strip()
        cv_text = (cv_text or "").strip()

        # get cached numpy embeddings (these calls use no_grad internally)
        jd_emb = _get_embedding(jd_text)
        cv_emb = _get_embedding(cv_text)

        # compute cosine via numpy (small memory footprint)
        jd_norm = np.linalg.norm(jd_emb)
        cv_norm = np.linalg.norm(cv_emb)
        if jd_norm == 0.0 or cv_norm == 0.0:
            return 0.0

        cosine = float(np.dot(jd_emb, cv_emb) / (jd_norm * cv_norm))
        # clamp possible tiny numerical errors
        cosine = max(min(cosine, 1.0), -1.0)
        return cosine * 100.0
    except Exception:
        # safe fallback
        return 0.0
    finally:
        gc.collect()

# -----------------------
# Full hybrid score (same API)
# -----------------------
def compute_hybrid_score(job_description: str, cv_text: str):
    lexical_score, matched_phrases = calculate_skillmatcher_plus_score(job_description, cv_text)
    semantic_score = compute_semantic_similarity(job_description, cv_text)

    hybrid = lexical_score + (semantic_score * 0.4)
    return hybrid, lexical_score, semantic_score, matched_phrases
