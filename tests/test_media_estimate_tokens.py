"""tests/test_media_estimate_tokens.py"""
import pytest

from services.media import estimate_tokens


def test_estimate_tokens_nano_banana_2():
    assert estimate_tokens("nano-banana-2") == 80_000


def test_estimate_tokens_with_units():
    assert estimate_tokens("kling-2.1-master", units=5) == 1_400_000


def test_estimate_tokens_unknown_model_raises_key_error():
    with pytest.raises(KeyError):
        estimate_tokens("not-a-real-model")


def test_all_catalog_models_have_jts_rate_and_price_entries():
    from services.media import MEDIA_MODEL_CATALOG, JTS_RATE_KEY, JTS_PRICE_PER_UNIT_USD
    catalog_keys = set(MEDIA_MODEL_CATALOG["image"].keys()) | set(MEDIA_MODEL_CATALOG["video"].keys())
    assert catalog_keys <= set(JTS_RATE_KEY.keys()), f"missing from JTS_RATE_KEY: {catalog_keys - set(JTS_RATE_KEY.keys())}"
    assert catalog_keys <= set(JTS_PRICE_PER_UNIT_USD.keys()), f"missing from JTS_PRICE_PER_UNIT_USD: {catalog_keys - set(JTS_PRICE_PER_UNIT_USD.keys())}"
