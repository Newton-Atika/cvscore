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
# NLTK setup once
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

lemmatizer = WordNetLemmatizer()
stop_words = set(stopwords.words('english'))
grammar = "NP: {<JJ>*<NN.*>+}"
parser = RegexpParser(grammar)

# -----------------------
# SentenceTransformer
# -----------------------
torch.set_num_threads(1)
embedding_model = SentenceTransformer('all-MiniLM-L6-v2', device="cpu")

# -----------------------
# Helpers
# -----------------------
def _truncate_text_for_embedding(text: str, max_chars: int = 8000):
    if not text:
        return ""
    if len(text) <= max_chars:
        return text
    head = text[: int(max_chars * 0.6)]
    tail = text[- int(max_chars * 0.4):]
    return head + "\n" + tail

def get_wordnet_pos(tag):
    if tag.startswith('J'): return wordnet.ADJ
    elif tag.startswith('V'): return wordnet.VERB
    elif tag.startswith('N'): return wordnet.NOUN
    elif tag.startswith('R'): return wordnet.ADV
    else: return wordnet.NOUN

def get_synonyms(word):
    syns = set()
    for syn in wordnet.synsets(word):
        for lemma in syn.lemmas():
            syns.add(lemma.name().replace("_", " "))
    return syns

@lru_cache(maxsize=200)
def _cached_embedding(hash_key, text_value):
    with torch.no_grad():
        emb = embedding_model.encode(text_value, convert_to_numpy=True)
    return np.asarray(emb, dtype=np.float32)

def _get_embedding(text):
    if not text:
        return np.zeros((embedding_model.get_sentence_embedding_dimension(),), dtype=np.float32)

    t = _truncate_text_for_embedding(text, max_chars=8000)
    h = hashlib.sha256(t.encode()).hexdigest()
    return _cached_embedding(h, t)

# -----------------------
# Preprocess (fully restored)
# -----------------------
def preprocess_with_phrases_nouns_synonyms(text):
    if not text:
        return {}

    text = text.lower()
    text = re.sub(r'[^a-z0-9+\-\s]', ' ', text)

    words = word_tokenize(text)
    words = [w for w in words if w not in stop_words and len(w) > 1]

    tagged = pos_tag(words)
    lemmatized = [lemmatizer.lemmatize(w, get_wordnet_pos(t)) for w, t in tagged]

    tree = parser.parse(tagged)
    noun_phrases = []
    for subtree in tree.subtrees(lambda t: t.label() == "NP"):
        np_phrase = " ".join(w for w, p in subtree.leaves())
        if len(np_phrase.split()) > 1:
            noun_phrases.append(np_phrase)

    # ngrams EXACTLY as original
    bigrams = [' '.join(bg) for bg in ngrams(lemmatized, 2)]
    trigrams = [' '.join(tg) for tg in ngrams(lemmatized, 3)]

    # restore synonyms
    synonym_terms = set()
    for w in lemmatized:
        synonym_terms |= get_synonyms(w)

    weighted = {w: 1 for w in lemmatized}
    weighted.update({bg: 2 for bg in bigrams})
    weighted.update({tg: 3 for tg in trigrams})
    weighted.update({np: 4 for np in noun_phrases})
    weighted.update({syn: 1 for syn in synonym_terms})

    return weighted

# -----------------------
# Scoring (unchanged)
# -----------------------
def calculate_skillmatcher_plus_score(job_description, cv_text):
    jd_terms = preprocess_with_phrases_nouns_synonyms(job_description)
    cv_terms = preprocess_with_phrases_nouns_synonyms(cv_text)

    if not jd_terms:
        return 0, set()

    common = set(jd_terms.keys()).intersection(cv_terms.keys())
    matched = sum(jd_terms[t] for t in common)
    total = sum(jd_terms.values())

    return round((matched / total) * 100, 2), common

def compute_semantic_similarity(job_description, cv_text):
    jd = job_description.strip()
    cv = cv_text.strip()

    jd_emb = _get_embedding(jd)
    cv_emb = _get_embedding(cv)

    norm_j = np.linalg.norm(jd_emb)
    norm_c = np.linalg.norm(cv_emb)
    if norm_j == 0 or norm_c == 0:
        return 0

    cosine = float(np.dot(jd_emb, cv_emb) / (norm_j * norm_c))
    return max(min(cosine, 1.0), -1.0) * 100

def compute_hybrid_score(job_description, cv_text):
    lexical, match = calculate_skillmatcher_plus_score(job_description, cv_text)
    semantic = compute_semantic_similarity(job_description, cv_text)
    hybrid = lexical + semantic * 0.4
    return hybrid, lexical, semantic, match
