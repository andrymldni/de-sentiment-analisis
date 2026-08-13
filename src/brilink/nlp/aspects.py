"""Aspect-Based Sentiment Analysis (ABSA) for the BRILink domain.

A document-level label answers "is this good or bad?".  A product owner needs
"good or bad *at what*?".  We therefore detect which of eight business aspects
a text touches and isolate the sentence window around each mention.

Aspects were derived from the actual complaint/coverage landscape: pricing,
network reliability, agent service, funds settlement, fraud/security, reach and
financial inclusion, agent economics, and the digital apps.

This module only does *detection* (which aspect, which sentences). Scoring the
polarity of each window is delegated to the same IndoBERT model that scores
the whole document - see ``score_aspect_hits`` below and
``EnsembleSentimentEngine`` in ``ensemble.py``, which batches every aspect
window from a batch of documents into a single model call.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache

from ..utils.text import normalize_for_nlp, split_clauses


@dataclass(frozen=True)
class Aspect:
    code: str
    label: str
    group: str
    description: str
    keywords: tuple[str, ...]
    # Terms that, if present, veto the match (guards against homonyms).
    exclusions: tuple[str, ...] = ()


ASPECTS: tuple[Aspect, ...] = (
    Aspect(
        code="biaya_tarif",
        label="Biaya & Tarif",
        group="Komersial",
        description="Biaya admin, potongan, fee transaksi, transparansi tarif",
        keywords=(
            "biaya",
            "biaya admin",
            "admin",
            "tarif",
            "potongan",
            "fee",
            "ongkos",
            "mahal",
            "murah",
            "gratis",
            "pungli",
            "pungutan",
            "harga",
            "bayar lebih",
            "melebihi ketentuan",
            "biaya siluman",
            "transparansi biaya",
            "biaya tambahan",
        ),
    ),
    Aspect(
        code="jaringan_sistem",
        label="Jaringan & Sistem",
        group="Teknis",
        description="Sinyal, koneksi, server, EDC, downtime, error sistem",
        keywords=(
            "sinyal",
            "jaringan",
            "koneksi",
            "internet",
            "server",
            "sistem",
            "offline",
            "down",
            "gangguan",
            "error",
            "maintenance",
            "edc",
            "mesin edc",
            "perangkat",
            "struk",
            "printer",
            "timeout",
            "tidak stabil",
            "lambat",
            "lelet",
        ),
    ),
    Aspect(
        code="layanan_agen",
        label="Layanan Agen",
        group="Operasional",
        description="Sikap, keramahan, kompetensi, dan responsivitas agen",
        keywords=(
            "agen",
            "pelayanan",
            "layanan",
            "petugas",
            "ramah",
            "sopan",
            "judes",
            "cs",
            "customer service",
            "respon",
            "dilayani",
            "antre",
            "antri",
            "buka",
            "tutup",
            "jam operasional",
            "kasir",
        ),
    ),
    Aspect(
        code="dana_transaksi",
        label="Dana & Penyelesaian Transaksi",
        group="Operasional",
        description="Saldo, dana tertahan, gagal transfer, refund, rekonsiliasi",
        keywords=(
            "saldo",
            "dana",
            "uang",
            "transfer",
            "transaksi",
            "setor",
            "tarik tunai",
            "penarikan",
            "top up",
            "refund",
            "pengembalian",
            "tertahan",
            "terpotong",
            "gagal",
            "pending",
            "mutasi",
            "rekening",
            "nominal",
            "selisih",
        ),
    ),
    Aspect(
        code="keamanan_fraud",
        label="Keamanan & Fraud",
        group="Risiko",
        description="Penipuan, skimming, pembobolan rekening, penyalahgunaan data",
        keywords=(
            "penipuan",
            "penipu",
            "menipu",
            "fraud",
            "skimming",
            "bobol",
            "dibobol",
            "pembobolan",
            "keamanan",
            "aman",
            "otp",
            "pin",
            "data pribadi",
            "modus",
            "oknum",
            "kriminal",
            "lapor polisi",
            "kasus",
            "tersangka",
            "penggelapan",
        ),
    ),
    Aspect(
        code="akses_inklusi",
        label="Akses & Inklusi Keuangan",
        group="Dampak Sosial",
        description="Jangkauan desa/pelosok, kemudahan akses, inklusi keuangan",
        keywords=(
            "desa",
            "pelosok",
            "daerah",
            "terpencil",
            "jangkauan",
            "menjangkau",
            "akses",
            "dekat",
            "inklusi keuangan",
            "unbanked",
            "masyarakat",
            "warga",
            "umkm",
            "pemberdayaan",
            "literasi keuangan",
            "cabang",
        ),
    ),
    Aspect(
        code="kemitraan_agen",
        label="Kemitraan & Ekonomi Agen",
        group="Komersial",
        description="Komisi, penghasilan agen, syarat pendaftaran, target transaksi",
        keywords=(
            "komisi",
            "penghasilan",
            "pendapatan",
            "untung",
            "keuntungan",
            "modal",
            "daftar",
            "pendaftaran",
            "syarat",
            "target",
            "kemitraan",
            "mitra",
            "bagi hasil",
            "sharing fee",
            "insentif",
            "bonus",
        ),
    ),
    Aspect(
        code="aplikasi_digital",
        label="Aplikasi Digital",
        group="Teknis",
        description="BRImo, aplikasi BRILink Mobile, update, login, UI/UX",
        keywords=(
            "aplikasi",
            "brimo",
            "brilink mobile",
            "apk",
            "update",
            "versi",
            "login",
            "masuk",
            "daftar akun",
            "ui",
            "tampilan",
            "fitur",
            "notifikasi",
            "force close",
            "crash",
            "install",
            "verifikasi",
        ),
    ),
)

ASPECT_BY_CODE = {aspect.code: aspect for aspect in ASPECTS}


@lru_cache(maxsize=64)
def _keyword_pattern(keywords: tuple[str, ...]) -> re.Pattern[str]:
    """Word-boundary matcher so "aman" does not fire inside "keamanan"."""
    alternatives = sorted(keywords, key=len, reverse=True)
    joined = "|".join(re.escape(k) for k in alternatives)
    return re.compile(rf"(?<!\w)(?:{joined})(?!\w)")


@dataclass
class AspectHit:
    aspect: Aspect
    mentions: int
    matched_keywords: list[str]
    windows: list[str]
    # Filled in later by score_aspect_hits(); 0.0/unavailable until then.
    score: float = 0.0
    score_available: bool = False

    @property
    def window_text(self) -> str:
        return " ".join(self.windows)

    def as_dict(self) -> dict:
        return {
            "aspect_code": self.aspect.code,
            "aspect_label": self.aspect.label,
            "aspect_group": self.aspect.group,
            "mentions": self.mentions,
            "matched_keywords": self.matched_keywords[:8],
            "score": round(self.score, 4),
            "score_available": self.score_available,
            "window_sample": self.windows[0][:220] if self.windows else "",
        }


def _keyword_hits(normalized: str, aspect: Aspect) -> list[str]:
    if aspect.exclusions and _keyword_pattern(aspect.exclusions).search(normalized):
        return []
    return _keyword_pattern(aspect.keywords).findall(normalized)


def detect_aspects(text: str | None) -> list[AspectHit]:
    """Return every aspect a text mentions, with the sentence window around it.

    Scores are *not* computed here - callers run the returned windows through
    ``score_aspect_hits`` (backed by IndoBERT) once they've collected windows
    for a whole batch of documents, so the model only has to run once instead
    of once per aspect.
    """
    raw = text or ""
    if not raw.strip():
        return []

    sentences = split_clauses(raw) or [raw]
    normalized_sentences = [normalize_for_nlp(s) for s in sentences]
    whole = " ".join(normalized_sentences)

    hits: list[AspectHit] = []
    for aspect in ASPECTS:
        if not _keyword_hits(whole, aspect):
            continue

        indices: list[int] = []
        matched: list[str] = []
        mentions = 0
        for idx, sentence in enumerate(normalized_sentences):
            found = _keyword_hits(sentence, aspect)
            if found:
                indices.append(idx)
                matched.extend(found)
                mentions += len(found)

        if not indices:
            # Keyword spans a sentence boundary - fall back to the whole text.
            indices = list(range(len(sentences)))
            matched = _keyword_hits(whole, aspect)
            mentions = len(matched)

        # Deliberately *not* widened to neighbouring clauses: two aspects in
        # the same sentence ("agennya ramah tapi sinyal error") must stay on
        # their own clause, or the transformer would see both polarities at
        # once and wash them out to neutral for both aspects.
        windows = [sentences[i] for i in indices]

        hits.append(
            AspectHit(
                aspect=aspect,
                mentions=mentions,
                matched_keywords=sorted(set(matched)),
                windows=windows,
            )
        )

    return hits


def score_aspect_hits(
    hits_per_document: Sequence[Sequence[AspectHit]],
    scorer,
) -> None:
    """Fill in ``score``/``score_available`` on every hit, in one model call.

    ``scorer`` is a callable ``list[str] -> list[TransformerOutput]``, i.e.
    ``SentimentTransformer.score_documents``. Flattening every window across
    every document into a single batch keeps this to one model call per
    ``score_batch()`` regardless of how many aspects were mentioned.
    """
    flat_windows: list[str] = []
    spans: list[tuple[int, int]] = []
    for hits in hits_per_document:
        start = len(flat_windows)
        flat_windows.extend(hit.window_text for hit in hits)
        spans.append((start, len(flat_windows)))

    if not flat_windows:
        return

    outputs = scorer(flat_windows)

    for hits, (start, end) in zip(hits_per_document, spans, strict=False):
        for hit, output in zip(hits, outputs[start:end], strict=False):
            hit.score = output.score if output.available else 0.0
            hit.score_available = output.available


def label_for(score: float, neutral_band: float = 0.12) -> str:
    if score > neutral_band:
        return "positive"
    if score < -neutral_band:
        return "negative"
    return "neutral"


def aspect_catalog() -> list[dict]:
    return [
        {
            "aspect_code": a.code,
            "aspect_label": a.label,
            "aspect_group": a.group,
            "description": a.description,
            "keyword_count": len(a.keywords),
        }
        for a in ASPECTS
    ]


def summarize(hits: Sequence[AspectHit], neutral_band: float = 0.12) -> dict:
    """Compact per-aspect summary used inside the document-level explanation."""
    return {
        hit.aspect.code: {
            "score": round(hit.score, 3),
            "label": label_for(hit.score, neutral_band),
            "mentions": hit.mentions,
        }
        for hit in hits
    }
