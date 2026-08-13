import os

import pytest

# Unit tests must never require torch, a network or a database. The real
# IndoBERT/emotion pipelines are disabled via settings; a FakeTransformer
# (see fake_transformer.py) stands in wherever a test needs text-dependent
# polarity, so branching logic (labels, confidence, review routing) can
# still be exercised without loading a model.
os.environ.setdefault("SENTIMENT_ENGINE_MODE", "ensemble")
os.environ.setdefault("SENTIMENT_ENABLE_TRANSFORMER", "false")
os.environ.setdefault("SENTIMENT_ENABLE_EMOTION", "false")
os.environ.setdefault("REDIS_ENABLED", "false")


@pytest.fixture(scope="session")
def engine():
    from brilink.nlp.ensemble import EnsembleSentimentEngine

    from .fake_transformer import FakeTransformer

    return EnsembleSentimentEngine(transformer=FakeTransformer(), emotion=None)
