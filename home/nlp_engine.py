import re
import nltk
import json
from nltk.corpus import stopwords, wordnet
from nltk.stem import WordNetLemmatizer
from nltk import pos_tag, word_tokenize, RegexpParser
from nltk.util import ngrams
from sentence_transformers import SentenceTransformer, util

# -----------------------
# Load NLTK once at startup
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
# LOAD EMBEDDING MODEL ONCE
# -----------------------
embedding_model = SentenceTransformer('all-MiniLM-L6-v2')


# -----------------------
# HELPERS
# -----------------------

def get_wordnet_pos(tag):
    if tag.startswith('J'): return wordnet.ADJ
    elif tag.startswith('V'): return wordnet.VERB
    elif tag.startswith('N'): return wordnet.NOUN
    elif tag.startswith('R'): return wordnet.ADV
    else: return wordnet.NOUN


def get_synonyms(word):
    synonyms = set()
    for syn in wordnet.synsets(word):
        for lemma in syn.lemmas():
            synonyms.add(lemma.name().replace('_', ' '))
    return synonyms


# -----------------------
# FULL PREPROCESS PIPELINE
# -----------------------

def preprocess_with_phrases_nouns_synonyms(text):
    text = text.lower()
    text = re.sub(r'[^a-z0-9+\-\s]', ' ', text)

    words = word_tokenize(text)
    words = [w for w in words if w not in stop_words and len(w) > 1]

    tagged = pos_tag(words)
    lemmatized = [lemmatizer.lemmatize(w, get_wordnet_pos(t)) for w, t in tagged]

    # Noun phrase extraction
    tree = parser.parse(tagged)
    noun_phrases = []
    for subtree in tree.subtrees(filter=lambda t: t.label() == 'NP'):
        np = ' '.join(word for word, pos in subtree.leaves())
        if len(np.split()) > 1:
            noun_phrases.append(np)

    # Bi / Tri grams
    bigrams = [' '.join(bg) for bg in ngrams(lemmatized, 2)]
    trigrams = [' '.join(tg) for tg in ngrams(lemmatized, 3)]

    # Synonyms
    synonym_terms = set()
    for w in lemmatized:
        synonym_terms |= get_synonyms(w)

    # Weighted term map (same logic as before)
    weighted_terms = {w: 1 for w in lemmatized}
    weighted_terms.update({bg: 2 for bg in bigrams})
    weighted_terms.update({tg: 3 for tg in trigrams})
    weighted_terms.update({np: 4 for np in noun_phrases})
    weighted_terms.update({syn: 1 for syn in synonym_terms})

    return weighted_terms


# -----------------------
# SKILLS + SYNONYMS + PHRASES SCORE
# -----------------------

def calculate_skillmatcher_plus_score(job_description, cv_text):
    jd_terms = preprocess_with_phrases_nouns_synonyms(job_description)
    cv_terms = preprocess_with_phrases_nouns_synonyms(cv_text)

    if not jd_terms:
        return 0, set()

    common = set(jd_terms.keys()).intersection(set(cv_terms.keys()))

    matched_weight = sum(jd_terms[t] for t in common)
    total_weight = sum(jd_terms.values())

    score = (matched_weight / total_weight) * 100 if total_weight else 0

    return round(score, 2), common


# -----------------------
# SEMANTIC EMBEDDING SIMILARITY
# -----------------------

def compute_semantic_similarity(job_description, cv_text):
    try:
        jd_emb = embedding_model.encode(job_description, convert_to_tensor=True)
        cv_emb = embedding_model.encode(cv_text, convert_to_tensor=True)
        return util.cos_sim(jd_emb, cv_emb).item() * 100
    except Exception:
        return 0


# -----------------------
# FULL HYBRID SCORE
# -----------------------

def compute_hybrid_score(job_description, cv_text):
    lexical_score, matched_phrases = calculate_skillmatcher_plus_score(job_description, cv_text)
    semantic_score = compute_semantic_similarity(job_description, cv_text)

    hybrid = lexical_score + (semantic_score * 0.4)
    return hybrid, lexical_score, semantic_score, matched_phrases
