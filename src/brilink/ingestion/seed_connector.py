"""Deterministic synthetic corpus generator.

Purpose is narrow and explicit: a portfolio project must be demonstrable on a
laptop with no network, no API keys and no waiting.  When live connectors
return nothing, this generator backfills a realistic corpus so the warehouse,
the dbt models and the Metabase dashboard all have something to show.

It is *not* a toy.  The generator composes text from aspect-specific clause
banks with controlled polarity, then deliberately injects the linguistic
constructions the sentiment engine is built to handle - negation, contrast,
sarcasm, reported speech, conditionals - so the dashboard demonstrates the
engine's behaviour rather than hiding it.  Every synthetic document is tagged
``is_synthetic`` in ``raw_payload`` and is filtered out of headline metrics by
a dedicated dbt flag, so synthetic and live data are never silently mixed.
"""

from __future__ import annotations

import random
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone

from ..logging_config import get_logger
from .base import BaseConnector, Document

logger = get_logger(__name__)

NEWS_OUTLETS = [
    ("detik", 0.16),
    ("kompas", 0.14),
    ("antaranews", 0.12),
    ("cnbcindonesia", 0.10),
    ("bisnis", 0.09),
    ("kontan", 0.08),
    ("tempo", 0.07),
    ("liputan6", 0.07),
    ("republika", 0.06),
    ("okezone", 0.05),
    ("tribunnews", 0.06),
]

# -------------------------------------------------------------------------
# Clause banks: (text, polarity_hint) grouped by aspect
# -------------------------------------------------------------------------
POSITIVE_CLAUSES: dict[str, list[str]] = {
    "biaya_tarif": [
        "biaya adminnya wajar dan sesuai ketentuan",
        "tarifnya jelas, tidak ada biaya tersembunyi",
        "biaya transfer terjangkau untuk warga desa",
        "potongannya kecil dibanding harus ke kota",
    ],
    "jaringan_sistem": [
        "jaringannya stabil, transaksi cepat selesai",
        "mesin EDC-nya responsif dan struk langsung keluar",
        "sistemnya lancar walau jam sibuk",
    ],
    "layanan_agen": [
        "agennya ramah dan sabar menjelaskan",
        "pelayanan memuaskan, tidak perlu antre lama",
        "petugasnya sigap membantu orang tua yang bingung",
        "agennya jujur dan amanah",
    ],
    "dana_transaksi": [
        "transaksi lancar, saldo langsung masuk",
        "setor tunai cepat dan nominalnya sesuai",
        "transfer berhasil dalam hitungan detik",
    ],
    "keamanan_fraud": [
        "prosesnya aman, struk selalu diberikan",
        "agen selalu mengingatkan untuk menjaga PIN",
    ],
    "akses_inklusi": [
        "sangat membantu warga desa yang jauh dari cabang bank",
        "menjangkau pelosok sehingga tidak perlu ke kota",
        "mendorong inklusi keuangan di daerah terpencil",
        "membantu UMKM setempat bertransaksi",
    ],
    "kemitraan_agen": [
        "komisinya lumayan untuk penghasilan tambahan",
        "syarat pendaftarannya mudah dan cepat disetujui",
    ],
    "aplikasi_digital": [
        "aplikasinya mudah dipakai dan ringan",
        "fitur di BRImo lengkap dan tampilannya jelas",
    ],
}

NEGATIVE_CLAUSES: dict[str, list[str]] = {
    "biaya_tarif": [
        "biaya adminnya mahal banget, jauh di atas ketentuan",
        "potongannya tidak transparan dan berubah-ubah",
        "dipungut biaya tambahan tanpa penjelasan",
        "tarifnya mencekik untuk transaksi kecil",
    ],
    "jaringan_sistem": [
        "sinyalnya jelek sehingga transaksi sering gagal",
        "mesin EDC rusak dan struk tidak keluar",
        "server down berjam-jam tanpa pemberitahuan",
        "sistemnya lambat sekali saat awal bulan",
    ],
    "layanan_agen": [
        "agennya judes dan tidak mau menjelaskan",
        "pelayanan buruk, saya dilempar sana sini",
        "agennya susah dihubungi kalau ada masalah",
    ],
    "dana_transaksi": [
        "saldo tertahan sudah tiga hari dan belum kembali",
        "dana tidak masuk padahal saldo sudah terpotong",
        "transaksi gagal tapi uang sudah terdebet",
        "refund lama sekali, tidak ada kejelasan",
    ],
    "keamanan_fraud": [
        "ada modus penipuan mengatasnamakan agen BRILink",
        "rekening dikuras setelah transaksi di agen",
        "oknum agen diduga melakukan penggelapan dana nasabah",
    ],
    "akses_inklusi": [
        "di kampung saya belum ada agen yang aktif",
        "agen terdekat tutup terus sehingga warga kesulitan",
    ],
    "kemitraan_agen": [
        "target transaksinya memberatkan dan komisinya kecil",
        "sudah memenuhi target tapi mesin EDC belum juga diberikan",
    ],
    "aplikasi_digital": [
        "aplikasinya sering error dan force close",
        "setelah update malah tidak bisa login",
        "verifikasi OTP tidak pernah masuk",
    ],
}

NEUTRAL_CLAUSES = [
    "BRI melaporkan jumlah agen BRILink mencapai 1,18 juta per akhir kuartal",
    "layanan mencakup transfer, setor tarik tunai, dan pembayaran tagihan",
    "agen tersebar di lebih dari 66 ribu desa di seluruh Indonesia",
    "BRI menyatakan akan menambah pelatihan bagi agen baru tahun ini",
    "volume transaksi tercatat sebesar Rp420 triliun pada triwulan pertama",
    "kegiatan sosialisasi digelar bersama pemerintah daerah",
]

NEWS_HEADLINES_POSITIVE = [
    "Agen BRILink Dorong Inklusi Keuangan hingga Pelosok Desa",
    "Transaksi BRILink Tumbuh, UMKM Daerah Ikut Terbantu",
    "BRI Perkuat Jaringan BRILink, Layanan Perbankan Makin Dekat ke Warga",
    "BRILink Jadi Andalan Warga Desa untuk Transaksi Harian",
]
NEWS_HEADLINES_NEGATIVE = [
    "Nasabah Keluhkan Biaya Admin Agen BRILink di Atas Ketentuan",
    "Marak Modus Penipuan Mengatasnamakan Agen BRILink",
    "Gangguan Sistem Bikin Transaksi di Agen BRILink Gagal",
    "Oknum Agen BRILink Diduga Gelapkan Dana Nasabah",
]
NEWS_HEADLINES_NEUTRAL = [
    "BRI Catat Jumlah Agen BRILink Tembus 1,18 Juta",
    "BRILink Perluas Layanan Pembayaran Digital",
    "OJK Soroti Tata Kelola Layanan Laku Pandai",
]

# Deliberately tricky constructions - the reason this engine is not a keyword
# counter. Each carries the label a careful human annotator would assign.
ADVERSARIAL_TEMPLATES: list[tuple[str, str]] = [
    (
        "Awalnya saya pikir biayanya mahal, ternyata tidak mahal sama sekali dan agennya ramah.",
        "positive",
    ),
    (
        "Pelayanannya bagus sih, tapi saldo saya tertahan dua minggu dan tidak ada kejelasan.",
        "negative",
    ),
    ("Mantap banget nih, uang saya hilang dan tidak ada yang tanggung jawab wkwk.", "negative"),
    ("Kalau biaya adminnya mahal saya pasti pindah, untungnya di sini masih wajar.", "positive"),
    ("BRI membantah tuduhan penipuan yang dikaitkan dengan salah satu agennya.", "neutral"),
    ("Tidak buruk, tapi juga tidak istimewa. Biasa saja.", "neutral"),
    ("Bukan agennya yang salah, sistemnya yang error terus sejak update terakhir.", "negative"),
    ("Hebat sekali ya, sudah antre satu jam ternyata mesinnya rusak.", "negative"),
    ("Jangan ragu pakai BRILink, tidak ribet dan tidak perlu ke bank.", "positive"),
    ("Semoga jaringannya diperbaiki, kalau lancar pasti sangat membantu.", "neutral"),
]

REVIEW_OPENERS = ["", "Jujur ", "Sejauh ini ", "Baru coba, ", "Sudah lama pakai, "]
REVIEW_CLOSERS = [
    "",
    " Semoga diperbaiki.",
    " Terima kasih.",
    " Tolong ditindaklanjuti.",
    " Recommended.",
]
SOCIAL_PREFIXES = [
    "Ada yang pernah ngalamin juga?",
    "Sekadar sharing pengalaman ya,",
    "Nanya dong,",
    "Update dari kemarin,",
    "",
]


class SeedConnector(BaseConnector):
    name = "seed"
    platform = "seed"
    description = "Deterministic synthetic corpus used as demo fallback"
    default_enabled = False

    def __init__(self, settings=None, total: int | None = None) -> None:
        super().__init__(settings)
        self.total = total or self.settings.ingestion.seed_documents
        self.days = self.settings.ingestion.seed_days
        self.rng = random.Random(self.settings.ingestion.seed_random_state)

    @property
    def max_items(self) -> int:
        # The seed corpus is sized by its own setting, not the per-connector
        # politeness budget that exists to protect live upstreams.
        return self.total

    # ------------------------------------------------------------------
    def fetch(self, since: datetime, limit: int) -> Iterable[Document]:
        target = min(self.total, limit) if limit else self.total
        logger.info("Generating %d synthetic documents over %d days", target, self.days)

        mix = {
            "news": 0.24,
            "playstore": 0.38,
            "appstore": 0.06,
            "reddit": 0.12,
            "youtube": 0.10,
            "twitter": 0.10,
        }
        emitted = 0
        for platform, share in mix.items():
            count = max(int(target * share), 1)
            for _ in range(count):
                if emitted >= target:
                    return
                yield self._make_document(platform, emitted)
                emitted += 1

        # Always include the adversarial set so the review queue and the
        # explainability drill-down have something interesting in them.
        for idx, (text, expected) in enumerate(ADVERSARIAL_TEMPLATES):
            yield self._adversarial_document(idx, text, expected)

    # ------------------------------------------------------------------
    def _published_at(self) -> datetime:
        """Recent-heavy distribution with a weekly rhythm, like real coverage."""
        u = self.rng.random()
        days_ago = int(self.days * (u**1.7))
        moment = datetime.now(timezone.utc) - timedelta(days=days_ago)
        # Weekends are quieter for editorial, busier for UGC - approximate with
        # an hour-of-day skew that keeps timestamps plausible.
        hour = int(min(23, max(0, self.rng.gauss(13, 4))))
        return moment.replace(hour=hour, minute=self.rng.randrange(60), second=0, microsecond=0)

    def _pick_polarity(self, platform: str) -> str:
        # UGC skews negative (people review when annoyed); editorial skews
        # neutral/positive. This asymmetry is real and worth reproducing.
        if platform in {"playstore", "appstore"}:
            weights = [("negative", 0.46), ("neutral", 0.18), ("positive", 0.36)]
        elif platform in {"reddit", "youtube", "twitter"}:
            weights = [("negative", 0.42), ("neutral", 0.26), ("positive", 0.32)]
        else:
            weights = [("negative", 0.22), ("neutral", 0.38), ("positive", 0.40)]
        roll = self.rng.random()
        cumulative = 0.0
        for label, weight in weights:
            cumulative += weight
            if roll <= cumulative:
                return label
        return "neutral"

    def _clauses(self, polarity: str, count: int) -> tuple[list[str], list[str]]:
        if polarity == "positive":
            bank = POSITIVE_CLAUSES
        elif polarity == "negative":
            bank = NEGATIVE_CLAUSES
        else:
            return self.rng.sample(NEUTRAL_CLAUSES, k=min(count, len(NEUTRAL_CLAUSES))), []

        aspects = self.rng.sample(list(bank), k=min(count, len(bank)))
        return [self.rng.choice(bank[a]) for a in aspects], aspects

    def _make_document(self, platform: str, index: int) -> Document:
        polarity = self._pick_polarity(platform)
        published = self._published_at()

        if platform == "news":
            return self._news_document(polarity, published, index)
        if platform in {"playstore", "appstore"}:
            return self._review_document(platform, polarity, published, index)
        return self._social_document(platform, polarity, published, index)

    def _news_document(self, polarity: str, published: datetime, index: int) -> Document:
        headlines = {
            "positive": NEWS_HEADLINES_POSITIVE,
            "negative": NEWS_HEADLINES_NEGATIVE,
            "neutral": NEWS_HEADLINES_NEUTRAL,
        }[polarity]
        headline = self.rng.choice(headlines)

        clauses, aspects = self._clauses(polarity, self.rng.randint(2, 3))
        filler = self.rng.sample(NEUTRAL_CLAUSES, k=2)
        body_parts = [
            f"JAKARTA - {filler[0].capitalize()}.",
            f"Sejumlah pihak menilai {clauses[0]}." if clauses else "",
            *[f"Selain itu, {c}." for c in clauses[1:]],
            f"{filler[1].capitalize()}.",
        ]
        outlet = self._weighted_outlet()

        return Document(
            source_platform="news",
            source_name=outlet,
            external_id=f"seed-news-{index}",
            title=headline,
            body=" ".join(p for p in body_parts if p),
            url=f"https://{outlet}.example/berita/brilink-{index}",
            author=self.rng.choice(["Redaksi", "Tim Ekonomi", "Kontributor Daerah"]),
            published_at=published,
            raw_payload={
                "is_synthetic": True,
                "intended_polarity": polarity,
                "intended_aspects": aspects,
                "collector": "seed",
            },
        )

    def _review_document(
        self, platform: str, polarity: str, published: datetime, index: int
    ) -> Document:
        clauses, aspects = self._clauses(polarity, self.rng.randint(1, 3))

        if polarity == "positive":
            rating = self.rng.choices([4, 5], weights=[0.35, 0.65])[0]
            body = self.rng.choice(REVIEW_OPENERS) + ", ".join(clauses).capitalize() + "."
        elif polarity == "negative":
            rating = self.rng.choices([1, 2], weights=[0.7, 0.3])[0]
            body = self.rng.choice(REVIEW_OPENERS) + ", ".join(clauses).capitalize() + "."
            if self.rng.random() < 0.25:
                body = body.upper()  # shouty one-star reviews exist
        else:
            rating = 3
            body = self.rng.choice(
                [
                    "Biasa saja, tidak istimewa tapi juga tidak buruk.",
                    "Cukup membantu walau kadang harus menunggu.",
                    "Fiturnya sudah cukup, mungkin bisa ditingkatkan lagi.",
                ]
            )

        # A minority of reviews mix polarity across a contrastive conjunction.
        if polarity != "neutral" and self.rng.random() < 0.3:
            opposite = NEGATIVE_CLAUSES if polarity == "positive" else POSITIVE_CLAUSES
            aspect = self.rng.choice(list(opposite))
            body = f"{body.rstrip('.')}, tapi {self.rng.choice(opposite[aspect])}."
            aspects = list(aspects) + [aspect]

        body += self.rng.choice(REVIEW_CLOSERS)
        app = self.rng.choice(["brimo", "brilink_mobile"]) if platform == "playstore" else "brimo"

        return Document(
            source_platform=platform,
            source_name=app,
            external_id=f"seed-{platform}-{index}",
            body=body,
            url=f"https://play.google.com/store/apps/details?id={app}",
            author=f"pengguna{self.rng.randrange(1000, 99999)}",
            rating=rating,
            published_at=published,
            engagement={"thumbs_up": max(0, int(self.rng.gauss(4, 6)))},
            raw_payload={
                "is_synthetic": True,
                "intended_polarity": polarity,
                "intended_aspects": aspects,
                "app_version": f"3.{self.rng.randrange(0, 9)}.{self.rng.randrange(0, 9)}",
                "collector": "seed",
            },
        )

    def _social_document(
        self, platform: str, polarity: str, published: datetime, index: int
    ) -> Document:
        clauses, aspects = self._clauses(polarity, self.rng.randint(1, 2))
        prefix = self.rng.choice(SOCIAL_PREFIXES)
        body = f"{prefix} agen BRILink dekat rumah {' dan '.join(clauses)}.".strip()
        if polarity == "negative" and self.rng.random() < 0.35:
            body += " " + self.rng.choice(["😡", "😤", "🙄"])
        if polarity == "positive" and self.rng.random() < 0.35:
            body += " " + self.rng.choice(["👍", "🙏", "🔥"])

        source_map = {
            "reddit": self.rng.choice(["r/indonesia", "r/finansial"]),
            "youtube": f"youtube:vid{self.rng.randrange(100, 999)}",
            "twitter": "x.com",
        }

        return Document(
            source_platform=platform,
            source_name=source_map[platform],
            external_id=f"seed-{platform}-{index}",
            title="Pengalaman pakai agen BRILink" if platform == "reddit" else None,
            body=body,
            url=f"https://{platform}.example/post/{index}",
            author=f"user{self.rng.randrange(100, 9999)}",
            published_at=published,
            engagement={
                "score": max(0, int(self.rng.gauss(12, 20))),
                "comments": max(0, int(self.rng.gauss(3, 5))),
            },
            raw_payload={
                "is_synthetic": True,
                "intended_polarity": polarity,
                "intended_aspects": aspects,
                "collector": "seed",
            },
        )

    def _adversarial_document(self, index: int, text: str, expected: str) -> Document:
        return Document(
            source_platform="playstore",
            source_name="brimo",
            external_id=f"seed-adversarial-{index}",
            body=text,
            url="https://play.google.com/store/apps/details?id=id.co.bri.brimo",
            author="anotator_qa",
            rating={"positive": 5, "negative": 1, "neutral": 3}[expected],
            published_at=self._published_at(),
            raw_payload={
                "is_synthetic": True,
                "is_adversarial": True,
                "intended_polarity": expected,
                "note": "gold-standard case for engine regression checks",
                "collector": "seed",
            },
        )

    def _weighted_outlet(self) -> str:
        roll = self.rng.random()
        cumulative = 0.0
        for outlet, weight in NEWS_OUTLETS:
            cumulative += weight
            if roll <= cumulative:
                return outlet
        return NEWS_OUTLETS[0][0]
