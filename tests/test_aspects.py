from brilink.nlp.aspects import (
    ASPECT_BY_CODE,
    aspect_catalog,
    detect_aspects,
    label_for,
    score_aspect_hits,
)

from .fake_transformer import FakeTransformer


def scored(text: str) -> dict[str, float]:
    """Detect + score in one step, using the inferring FakeTransformer."""
    hits = detect_aspects(text)
    score_aspect_hits([hits], FakeTransformer().score_documents)
    return {hit.aspect.code: hit.score for hit in hits}


def mentioned(text: str) -> set[str]:
    """Aspect detection only - no model call, so this never needs a fake."""
    return {hit.aspect.code for hit in detect_aspects(text)}


def test_catalog_is_complete_and_unique():
    catalog = aspect_catalog()
    assert len(catalog) == len(ASPECT_BY_CODE) == 8
    assert len({a["aspect_code"] for a in catalog}) == len(catalog)


def test_aspect_detection_is_specific():
    result = mentioned("Biaya adminnya mahal sekali")
    assert "biaya_tarif" in result
    assert "akses_inklusi" not in result


def test_aspects_can_disagree_within_one_sentence():
    """The whole point of ABSA: one document, two opposite verdicts."""
    result = scored("Agennya ramah tapi sinyal sering gangguan error")
    assert result["layanan_agen"] > 0
    assert result["jaringan_sistem"] < 0


def test_multi_aspect_document():
    text = (
        "Agennya ramah dan pelayanannya cepat. Sayangnya biaya adminnya mahal banget. "
        "Sinyal sering error sehingga transaksi gagal. "
        "Untungnya sangat membantu warga desa yang jauh dari cabang bank."
    )
    result = scored(text)
    assert result["layanan_agen"] > 0
    assert result["biaya_tarif"] < 0
    assert result["jaringan_sistem"] < 0
    assert result["akses_inklusi"] > 0


def test_word_boundary_prevents_false_matches():
    """'untungnya' as a discourse marker must not register as agent economics."""
    assert "kemitraan_agen" not in mentioned("Untungnya transaksinya lancar")


def test_no_aspects_for_unrelated_text():
    assert mentioned("Cuaca hari ini cerah sekali") == set()


def test_label_for_respects_neutral_band():
    assert label_for(0.5) == "positive"
    assert label_for(-0.5) == "negative"
    assert label_for(0.05) == "neutral"
    assert label_for(0.05, neutral_band=0.01) == "positive"


def test_aspect_payload_shape():
    hits = detect_aspects("biaya adminnya mahal")
    score_aspect_hits([hits], FakeTransformer().score_documents)
    payload = hits[0].as_dict()
    for key in ("aspect_code", "aspect_label", "aspect_group", "score", "score_available"):
        assert key in payload


def test_score_aspect_hits_batches_across_documents():
    """One shared model call scores every aspect window from every document."""
    calls = []

    def fake_scorer(texts):
        calls.append(list(texts))
        return FakeTransformer(0.4).score_documents(texts)

    docs = [
        detect_aspects("Biaya adminnya mahal tapi agennya ramah"),
        detect_aspects("Sinyal sering gangguan error"),
    ]
    score_aspect_hits(docs, fake_scorer)

    assert len(calls) == 1  # exactly one batched model call, not one per document
    assert all(hit.score_available for hits in docs for hit in hits)
