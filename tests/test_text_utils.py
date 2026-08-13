import pytest

from brilink.utils.text import (
    chunk_words,
    content_hash,
    detect_language,
    emoji_polarity,
    hamming_distance,
    is_near_duplicate,
    normalize_for_nlp,
    relevance_score,
    simhash,
    split_clauses,
    tokenize,
)


@pytest.mark.parametrize(
    "raw,expected_fragment",
    [
        ("Aplikasinya lemooot bgt!!", "lambat banget"),
        ("gk bisa transfer", "tidak bisa transfer"),
        ("Mantaaap bgt", "mantap banget"),
        ("Biaya adm nya mahal", "biaya admin"),
    ],
)
def test_slang_and_elongation_normalisation(raw, expected_fragment):
    assert expected_fragment in normalize_for_nlp(raw)


def test_doubled_letters_are_preserved():
    """Reducing every double letter would corrupt legitimate Indonesian words."""
    out = normalize_for_nlp("maaf saat ini saldo kosong")
    assert "maaf" in out and "saat" in out


def test_urls_and_markup_are_stripped():
    out = normalize_for_nlp("<b>Cek</b> di https://example.com sekarang")
    assert "http" not in out and "<b>" not in out


def test_split_clauses_separates_contrast():
    clauses = split_clauses("Agennya ramah tapi sinyal sering gangguan")
    assert len(clauses) == 2
    assert "ramah" in clauses[0]
    assert "sinyal" in clauses[1]


def test_split_clauses_handles_comma_splice():
    clauses = split_clauses("Aplikasi error, saldo tertahan, biaya mahal")
    assert len(clauses) == 3


def test_emoji_polarity_counts_both_directions():
    assert emoji_polarity("bagus 👍👍 tapi 😡") == (2, 1)


def test_chunking_covers_long_text_with_overlap():
    text = " ".join(["kata"] * 500)
    chunks = chunk_words(text, size=100, overlap=20, max_chunks=10)
    assert len(chunks) > 1
    assert all(len(c.split()) <= 100 for c in chunks)


def test_chunking_short_text_is_single_chunk():
    assert chunk_words("halo dunia", size=100) == ["halo dunia"]


def test_content_hash_is_stable_and_normalisation_aware():
    assert content_hash("Agen BRILink bagus") == content_hash("agen brilink  bagus")
    assert content_hash("a") != content_hash("b")


def test_simhash_detects_near_duplicates():
    a = simhash("Agen BRILink sangat membantu warga desa terpencil")
    b = simhash("Agen BRILink sangat membantu warga desa terpencil.")
    c = simhash("Biaya admin agen dinilai terlalu mahal oleh nasabah")
    assert is_near_duplicate(a, b)
    assert not is_near_duplicate(a, c)
    assert hamming_distance(a, a) == 0


def test_language_detection():
    assert detect_language("saya tidak bisa transfer di agen") == "id"
    assert detect_language("the app is not working for me") == "en"
    assert detect_language("") == "unknown"


def test_relevance_score_bounds():
    assert relevance_score("agen brilink di desa", ["brilink"]) == 1.0
    assert relevance_score("berita politik", ["brilink"]) == 0.0


def test_tokenize_drops_punctuation():
    assert tokenize("halo, dunia!") == ["halo", "dunia"]
