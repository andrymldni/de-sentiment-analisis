"""Text utilities shared by ingestion and NLP.

Indonesian user-generated text is messy: heavy slang, elongated vowels
("mantaaap"), inconsistent spacing, boilerplate news footers, emoji carrying
most of the polarity.  Normalising once, here, keeps every downstream signal
consistent.
"""

from __future__ import annotations

import hashlib
import html
import re
import unicodedata
from collections.abc import Iterable

# --------------------------------------------------------------------------
# Slang / abbreviation normalisation
# --------------------------------------------------------------------------
SLANG_MAP: dict[str, str] = {
    "gk": "tidak",
    "ga": "tidak",
    "gak": "tidak",
    "nggak": "tidak",
    "ngga": "tidak",
    "enggak": "tidak",
    "kagak": "tidak",
    "gada": "tidak ada",
    "gaada": "tidak ada",
    "gabisa": "tidak bisa",
    "gbs": "tidak bisa",
    "tdk": "tidak",
    "tak": "tidak",
    "bkn": "bukan",
    "blm": "belum",
    "blum": "belum",
    "udh": "sudah",
    "udah": "sudah",
    "sdh": "sudah",
    "dah": "sudah",
    "bgt": "banget",
    "bngt": "banget",
    "bgtt": "banget",
    "sgt": "sangat",
    "skli": "sekali",
    "sy": "saya",
    "gw": "saya",
    "gue": "saya",
    "aq": "saya",
    "aku": "saya",
    "km": "kamu",
    "kmu": "kamu",
    "lu": "kamu",
    "lo": "kamu",
    "dgn": "dengan",
    "dg": "dengan",
    "utk": "untuk",
    "yg": "yang",
    "krn": "karena",
    "krna": "karena",
    "jd": "jadi",
    "jgn": "jangan",
    "hrs": "harus",
    "bs": "bisa",
    "sm": "sama",
    "trs": "terus",
    "tp": "tapi",
    "tpi": "tapi",
    "cm": "cuma",
    "cuman": "cuma",
    "bnr": "benar",
    "bener": "benar",
    "trf": "transfer",
    "tf": "transfer",
    "trnsfr": "transfer",
    "trx": "transaksi",
    "adm": "admin",
    "admn": "admin",
    "biayanya": "biaya",
    "potongan": "biaya",
    "cs": "customer service",
    "kk": "kakak",
    "min": "admin",
    "mimin": "admin",
    "aplikasinya": "aplikasi",
    "apk": "aplikasi",
    "app": "aplikasi",
    "eror": "error",
    "erorr": "error",
    "eror2": "error",
    "bug": "error",
    "lemot": "lambat",
    "lelet": "lambat",
    "lola": "lambat",
    "ngelag": "lambat",
    "lag": "lambat",
    "loading": "memuat",
    "loadingnya": "memuat",
    "mantul": "mantap",
    "keren": "bagus",
    "oke": "bagus",
    "ok": "bagus",
    "amanah": "terpercaya",
    "ribet": "rumit",
    "ribetnya": "rumit",
    "mahal2": "mahal",
    "murah2": "murah",
    "parah": "buruk",
    "zonk": "gagal",
    "wkwkwk": "wkwk",
    "wkwkwkwk": "wkwk",
    "hehehe": "hehe",
    "yaa": "ya",
    "brilink": "brilink",
    "brimo": "brimo",
    "edc": "edc",
    "atm": "atm",
}

# Boilerplate that adds noise to news bodies.
BOILERPLATE_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"baca juga\s*:.*?(?=\.|$)",
        r"simak juga\s*:.*?(?=\.|$)",
        r"lihat juga\s*:.*?(?=\.|$)",
        r"artikel ini telah tayang di.*",
        r"copyright\s*©.*",
        r"\(?(?:adv|advertorial|siaran pers)\)?\s*$",
        r"editor\s*:\s*\w+.*$",
        r"reporter\s*:\s*\w+.*$",
        r"penulis\s*:\s*\w+.*$",
        r"sumber\s*:\s*\w+\s*$",
    )
]

URL_RE = re.compile(r"https?://\S+|www\.\S+")
MENTION_RE = re.compile(r"[@#]\w+")
HTML_TAG_RE = re.compile(r"<[^>]+>")
WHITESPACE_RE = re.compile(r"\s+")
ELONGATION_RE = re.compile(r"([a-z])\1{2,}")
PUNCT_PAD_RE = re.compile(r"([.,!?:;/])")
NON_TEXT_RE = re.compile(r"[^\w\s.,!?%\-/:']", re.UNICODE)
TOKEN_RE = re.compile(r"[a-z0-9']+")
SENTENCE_RE = re.compile(r"(?<=[.!?])\s+|\n+")
# Clause boundaries used by the aspect extractor. Splitting only on sentence
# punctuation is too coarse for UGC, where several aspects are routinely packed
# into one comma-spliced sentence.
CLAUSE_RE = re.compile(
    r"[,;]|\s+(?:tapi|tetapi|namun|sedangkan|sementara|padahal|sehingga|"
    r"karena|meskipun|meski|walaupun|walau|sayangnya|selain itu|cuma|hanya saja)\s+",
    re.IGNORECASE,
)

# Emoji ranges that carry sentiment. Kept explicit so we can weight them.
POSITIVE_EMOJI = set("😀😃😄😁😊🙂😍🥰😘👍👌🙏💪🔥✨⭐🌟❤️💚💙🎉🥳😻🤩💯")
NEGATIVE_EMOJI = set("😞😔😟😕🙁☹️😣😖😫😩😢😭😤😠😡🤬👎💔😑😒🤦🙄😰😨⚠️")


def strip_html(text: str) -> str:
    return HTML_TAG_RE.sub(" ", html.unescape(text or ""))


def remove_boilerplate(text: str) -> str:
    cleaned = text or ""
    for pattern in BOILERPLATE_PATTERNS:
        cleaned = pattern.sub(" ", cleaned)
    return cleaned


def clean_text(text: str | None, keep_case: bool = False) -> str:
    """Light clean used for storage: readable, but free of markup and noise."""
    if not text:
        return ""
    out = strip_html(text)
    out = unicodedata.normalize("NFKC", out)
    out = remove_boilerplate(out)
    out = URL_RE.sub(" ", out)
    out = WHITESPACE_RE.sub(" ", out).strip()
    return out if keep_case else out.lower()


def normalize_for_nlp(text: str | None) -> str:
    """Aggressive normalisation used by the aspect matcher and IndoBERT input prep.

    Order matters: collapse elongation *before* slang lookup so "lemooot"
    reaches the dictionary as "lemot", and detach punctuation so "bgt," is not
    treated as an unknown token.  Doubled letters are preserved because they
    are legitimate in Indonesian ("maaf", "saat").
    """
    if not text:
        return ""
    out = clean_text(text)
    out = MENTION_RE.sub(" ", out)
    out = ELONGATION_RE.sub(r"\1", out)  # mantaaap -> mantap, lemooot -> lemot
    out = NON_TEXT_RE.sub(" ", out)
    out = PUNCT_PAD_RE.sub(r" \1 ", out)

    tokens: list[str] = []
    for token in out.split():
        # Clause punctuation is load-bearing downstream (split_clauses segments
        # on it for aspect scoring), so it survives normalisation as its own token.
        if token in {",", ";"}:
            tokens.append(token)
            continue
        stripped = token.strip(".,!?:;/'-")
        if not stripped:
            continue
        tokens.append(SLANG_MAP.get(stripped, stripped))
    out = " ".join(t for t in tokens if t)
    out = re.sub(r"\s+([,;])", r"\1", out)
    return WHITESPACE_RE.sub(" ", out).strip()


def tokenize(text: str) -> list[str]:
    return TOKEN_RE.findall(text.lower())


def split_sentences(text: str, min_length: int = 3) -> list[str]:
    if not text:
        return []
    parts = [p.strip() for p in SENTENCE_RE.split(text)]
    return [p for p in parts if len(p) >= min_length]


def split_clauses(text: str, min_length: int = 3) -> list[str]:
    """Sentence split, then clause split - the granularity aspect scoring needs.

    "Agennya ramah tapi sinyal sering gangguan" must yield two segments,
    otherwise the positive remark about the agent and the negative remark about
    the network collapse into a single, meaningless average.
    """
    segments: list[str] = []
    for sentence in split_sentences(text, min_length=1) or ([text] if text else []):
        for clause in CLAUSE_RE.split(sentence):
            clause = (clause or "").strip()
            if len(clause) >= min_length:
                segments.append(clause)
    return segments


def emoji_polarity(text: str) -> tuple[int, int]:
    """Count sentiment-bearing emoji without needing the emoji package."""
    positive = sum(1 for ch in text or "" if ch in POSITIVE_EMOJI)
    negative = sum(1 for ch in text or "" if ch in NEGATIVE_EMOJI)
    return positive, negative


def chunk_words(text: str, size: int = 180, overlap: int = 40, max_chunks: int = 6) -> list[str]:
    """Sliding-window chunking so long articles are not truncated to the lede."""
    words = (text or "").split()
    if not words:
        return []
    if len(words) <= size:
        return [" ".join(words)]

    step = max(size - overlap, 1)
    chunks: list[str] = []
    for start in range(0, len(words), step):
        chunk = words[start : start + size]
        if len(chunk) < 20 and chunks:
            break
        chunks.append(" ".join(chunk))
        if len(chunks) >= max_chunks:
            break
    return chunks


def content_hash(*parts: str | None) -> str:
    """Stable identity hash used for exact deduplication."""
    joined = "||".join(normalize_for_nlp(p) for p in parts if p)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def simhash(text: str, bits: int = 64) -> int:
    """64-bit SimHash for near-duplicate detection (syndicated news copy)."""
    tokens = tokenize(normalize_for_nlp(text))
    if not tokens:
        return 0
    shingles = (
        [" ".join(tokens[i : i + 3]) for i in range(len(tokens) - 2)] if len(tokens) > 2 else tokens
    )
    vector = [0] * bits
    for shingle in shingles:
        h = int(hashlib.md5(shingle.encode("utf-8")).hexdigest(), 16)
        for i in range(bits):
            vector[i] += 1 if (h >> i) & 1 else -1
    value = 0
    for i, weight in enumerate(vector):
        if weight > 0:
            value |= 1 << i
    return value


def hamming_distance(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def is_near_duplicate(a: int, b: int, threshold: int = 4) -> bool:
    return hamming_distance(a, b) <= threshold


def detect_language(text: str) -> str:
    """Cheap ID-vs-EN discriminator: good enough to flag off-language reviews."""
    tokens = set(tokenize(text or ""))
    if not tokens:
        return "unknown"
    id_markers = {
        "yang",
        "dan",
        "tidak",
        "saya",
        "untuk",
        "dengan",
        "ini",
        "itu",
        "sudah",
        "bisa",
        "ada",
        "dari",
        "ke",
        "di",
        "adalah",
        "akan",
        "juga",
        "karena",
    }
    en_markers = {
        "the",
        "and",
        "is",
        "to",
        "of",
        "for",
        "with",
        "this",
        "that",
        "not",
        "very",
        "good",
        "bad",
        "app",
        "cannot",
        "please",
    }
    id_hits = len(tokens & id_markers)
    en_hits = len(tokens & en_markers)
    if id_hits == en_hits == 0:
        return "unknown"
    return "id" if id_hits >= en_hits else "en"


def relevance_score(text: str, keywords: Iterable[str]) -> float:
    """Fraction of target keywords present - used to drop off-topic captures."""
    haystack = normalize_for_nlp(text)
    if not haystack:
        return 0.0
    terms = [normalize_for_nlp(k) for k in keywords if k]
    if not terms:
        return 1.0
    hits = sum(1 for term in terms if term and term in haystack)
    return hits / len(terms)


def contains_any(text: str, terms: Iterable[str]) -> bool:
    haystack = normalize_for_nlp(text)
    return any(normalize_for_nlp(t) in haystack for t in terms if t)


def truncate(text: str | None, limit: int) -> str:
    if not text:
        return ""
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"
