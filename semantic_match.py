"""
Semantic-match decision logic for the Q&A cache (query_cache.py).

Three independent signals must ALL agree before a new question is
treated as "the same question" as a previously cached one:

1. Cosine similarity between their embeddings, above SIMILARITY_THRESHOLD.
2. An "entity guard" -- both questions must reference the same numbers,
   quarters, years, months, and relative-time words (or neither
   references any).
3. A "content-word guard" -- both questions must substantially agree on
   which topic words they use (donors vs. beneficiaries, campaign vs.
   city, etc.), above CONTENT_OVERLAP_THRESHOLD.

Why three, and why the thresholds are what they are: this was NOT tuned
by guessing. Live-testing against the real embedding model
(nomic-embed-text) showed the match/non-match cosine-similarity
distributions actually OVERLAP -- e.g. "total donations this year" vs.
"...last year" scored 0.978 (should NOT match), while a genuine
paraphrase ("Show all employees" vs. "Can you list every employee?")
scored only 0.82 (SHOULD match). A single cosine threshold cannot
separate these; there is no value that admits the low-scoring true match
without also admitting the high-scoring false one.

The entity guard alone doesn't fully close this gap either: it catches
numeric/date drift ("top 5" vs "top 10", "this year" vs "last year") but
does nothing for topic drift between two entity-free questions --
"donations by campaign" vs "donations by city" scored 0.81, and "active
beneficiaries" vs "active donors" scored 0.82, both landing right in the
genuine-paraphrase range. Those are exactly the queries this guard has
to catch, because they'd otherwise serve one dimension's data in answer
to a question about a completely different one.

Adding the content-word guard (Jaccard overlap of topic words, after
stripping generic query scaffolding like "show"/"list"/"how many") is
what actually separates the two classes cleanly: every genuine paraphrase
in that same test scored >= 0.67 on this measure, every topic-drift
pair scored 0.33. That clean separation is what let the cosine threshold
come down from an untested 0.93 (which rejected every real paraphrase
tried) to 0.80 (which accepts them) -- lowering the cosine bar is only
safe because the content-word guard now independently protects against
the false positives a lower bar would otherwise let through.

This module has no knowledge of SQL, roles, or storage -- it is pure
text-in/decision-out so it can be tested and reasoned about in isolation
from query_cache.py's persistence concerns.
"""

import re

SIMILARITY_THRESHOLD = 0.80
CONTENT_OVERLAP_THRESHOLD = 0.5

_NUMBER_PATTERN = re.compile(r"\b\d+(?:\.\d+)?\b")
_QUARTER_PATTERN = re.compile(r"(?i)\bq([1-4])\b")
_MONTH_PATTERN = re.compile(
    r"(?i)\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b"
)
_RELATIVE_TIME_PATTERN = re.compile(
    r"(?i)\b(today|yesterday|this week|last week|this month|last month|"
    r"this year|last year|this quarter|last quarter|all time|year to date|ytd)\b"
)


def extract_entity_signature(text):
    """
    Returns a frozenset of normalized "facts that change the answer" found
    in the text: bare numbers, quarter labels, month names, and relative
    time phrases. Two questions with different signatures are never
    considered the same question, regardless of how similar they read.
    """
    lowered = text.lower()
    signature = set()
    signature.update(f"num:{n}" for n in _NUMBER_PATTERN.findall(lowered))
    signature.update(f"quarter:q{n}" for n in _QUARTER_PATTERN.findall(lowered))
    signature.update(f"month:{m[:3]}" for m in _MONTH_PATTERN.findall(lowered))
    signature.update(f"time:{t}" for t in _RELATIVE_TIME_PATTERN.findall(lowered))
    return frozenset(signature)


def entity_signatures_compatible(sig_a, sig_b):
    """True only if both questions agree on every number/date/time entity."""
    return sig_a == sig_b


# Generic question scaffolding -- verbs/articles/quantifier words that
# carry no topic information ("show", "how many", "the"). Deliberately
# NOT a general-purpose English stopword list: it's scoped to the kind of
# phrasing this app's questions actually use, so it doesn't accidentally
# strip a word that happens to be topically meaningful in this domain.
_GENERIC_QUERY_WORDS = {
    "show", "list", "can", "you", "please", "what", "is", "are", "was", "were",
    "the", "a", "an", "of", "in", "by", "for", "from", "to", "my", "how", "many",
    "much", "total", "number", "count", "display", "get", "give", "every", "all",
    "each", "there", "do", "does", "did", "me", "who", "it", "this", "that",
}
_WORD_PATTERN = re.compile(r"[a-zA-Z]+")


def extract_content_words(text):
    """
    Returns a frozenset of normalized topic words -- what the question is
    actually ABOUT, as opposed to how it's phrased. Strips generic query
    scaffolding and does a crude plural-stripping (trailing "s" on words
    longer than 3 letters) so "donors"/"donor" and "employees"/"employee"
    land on the same token; this doesn't need to be linguistically
    precise, only CONSISTENT between two phrasings of the same question.
    """
    tokens = _WORD_PATTERN.findall(text.lower())
    words = {t for t in tokens if t not in _GENERIC_QUERY_WORDS and len(t) > 1}
    return frozenset(w[:-1] if w.endswith("s") and len(w) > 3 else w for w in words)


def content_overlap_score(words_a, words_b):
    """
    Jaccard overlap of topic words. Two entity-free questions with
    completely different subjects -- "donations by campaign" vs.
    "donations by city" -- can still embed close together (same
    structure); this is what actually tells them apart, since neither
    contains a number/date for the entity guard to catch. Both-empty is
    treated as compatible (nothing to disagree about), matching the
    entity guard's philosophy for the same reason.
    """
    if not words_a and not words_b:
        return 1.0
    union = words_a | words_b
    if not union:
        return 1.0
    return len(words_a & words_b) / len(union)


def cosine_similarity(vec_a, vec_b):
    """
    Plain-Python cosine similarity (no numpy dependency -- embedding
    vectors here are at most ~1k floats and comparisons happen against a
    handful to a few hundred cached entries per role+donor partition, so
    a pure-Python loop is fast enough and keeps the dependency footprint
    unchanged).
    """
    if len(vec_a) != len(vec_b):
        return 0.0
    dot = sum(a * b for a, b in zip(vec_a, vec_b))
    norm_a = sum(a * a for a in vec_a) ** 0.5
    norm_b = sum(b * b for b in vec_b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def find_best_match(query_text, query_embedding, candidates):
    """
    candidates: iterable of dicts, each with at least
        {"embedding": list[float], "entity_signature": frozenset,
         "content_words": frozenset, ...}
    Returns the candidate dict with the highest cosine similarity among
    those that pass BOTH the entity guard and the content-word guard and
    clear SIMILARITY_THRESHOLD, plus its score, or (None, 0.0) if nothing
    qualifies. See module docstring for why all three checks exist.
    """
    query_signature = extract_entity_signature(query_text)
    query_content_words = extract_content_words(query_text)

    best_candidate, best_score = None, 0.0
    for candidate in candidates:
        if not entity_signatures_compatible(query_signature, candidate["entity_signature"]):
            continue
        if content_overlap_score(query_content_words, candidate["content_words"]) < CONTENT_OVERLAP_THRESHOLD:
            continue
        score = cosine_similarity(query_embedding, candidate["embedding"])
        if score > best_score:
            best_candidate, best_score = candidate, score

    if best_candidate is not None and best_score >= SIMILARITY_THRESHOLD:
        return best_candidate, best_score
    return None, 0.0
