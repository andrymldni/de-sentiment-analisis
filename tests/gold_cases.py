"""Hand-annotated regression set.

These are the cases a naive keyword counter gets wrong - negation, sarcasm,
reported speech, contrastive discourse ("tapi") - which is exactly why the
engine leans on a pretrained IndoBERT model instead of one. They are the
contract the engine is held to: `make bench` scores them and prints an
accuracy report, and `test_gold_set.py` fails the build if accuracy regresses.
"""

GOLD_CASES: list[tuple[str, str, str]] = [
    # (text, expected_label, why_it_is_hard)
    (
        "Agen BRILink sangat membantu warga desa, transaksi lancar dan biaya terjangkau.",
        "positive",
        "straightforward positive",
    ),
    (
        "Pelayanannya buruk sekali, saldo tertahan dan biaya admin mahal.",
        "negative",
        "straightforward negative",
    ),
    ("Agen BRILink buka setiap hari mulai pukul delapan pagi.", "neutral", "factual, no polarity"),
    ("Tidak bagus sama sekali, sudah tiga kali gagal transfer.", "negative", "negated praise"),
    (
        "Aplikasinya tidak buruk kok, cukup membantu.",
        "positive",
        "negated complaint = faint praise",
    ),
    (
        "Pelayanannya ramah, tapi biaya adminnya mahal banget dan sering error.",
        "negative",
        "contrast: stance follows 'tapi'",
    ),
    (
        "Biayanya agak mahal, tapi agennya sangat membantu dan prosesnya cepat.",
        "positive",
        "contrast in the positive direction",
    ),
    (
        "Mantap banget, uang saya hilang dan tidak ada yang tanggung jawab wkwk.",
        "negative",
        "sarcasm",
    ),
    (
        "Hebat sekali ya, sudah antre satu jam ternyata mesinnya rusak.",
        "negative",
        "sarcasm with an intensifier",
    ),
    (
        "BRI membantah tuduhan penipuan yang dikaitkan dengan salah satu agennya.",
        "neutral",
        "reported speech about an allegation",
    ),
    (
        "Kalau biaya adminnya mahal saya pindah, untungnya di sini masih wajar.",
        "positive",
        "conditional clause must not dominate",
    ),
    (
        "Bukan agennya yang salah, sistemnya yang error terus sejak update terakhir.",
        "negative",
        "negation scoped to the wrong target",
    ),
    (
        "Jangan ragu pakai BRILink, tidak ribet dan tidak perlu ke bank.",
        "positive",
        "double negation as endorsement",
    ),
    (
        "Saldo saya terpotong tapi dana tidak masuk ke rekening tujuan.",
        "negative",
        "domain phrase, no obvious sentiment word",
    ),
    (
        "BRI mencatat jumlah agen BRILink tembus 1,18 juta per akhir Maret.",
        "neutral",
        "business news with growth verbs but no evaluation",
    ),
    (
        "Transaksi BRILink tumbuh dan mendorong inklusi keuangan hingga pelosok.",
        "positive",
        "editorial framing, positive",
    ),
    (
        "Aplikasinya lemooot bgt, gk bisa transfer sama sekali!!",
        "negative",
        "heavy slang and elongation",
    ),
    ("Biasa saja, tidak istimewa tapi juga tidak mengecewakan.", "neutral", "explicitly balanced"),
    (
        "Sinyal jelek terus, tapi agennya sabar bantuin sampai transaksinya berhasil.",
        "positive",
        "problem resolved - stance after contrast",
    ),
    (
        "Marak modus penipuan mengatasnamakan agen BRILink di daerah.",
        "negative",
        "fraud reporting, not a denial",
    ),
]
