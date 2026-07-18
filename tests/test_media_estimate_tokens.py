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
