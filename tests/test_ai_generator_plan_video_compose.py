"""tests/test_ai_generator_plan_video_compose.py

Cheap unit coverage for plan_video_compose's new return shape: the plan dict
must carry a "_usage" key with the real reported token counts, and that key
must be pop-able by the caller (routes/media_routes.py's compose_ai_plan)
without a KeyError — i.e. every code path through the function attaches it.
"""
import json
from types import SimpleNamespace

from services import ai_generator


class _FakeTextBlock:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _FakeResponse:
    def __init__(self, text, input_tokens=123, output_tokens=456):
        self.content = [_FakeTextBlock(text)]
        self.usage = SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens)


class _FakeMessages:
    def __init__(self, response):
        self._response = response

    def create(self, **kwargs):
        return self._response


class _FakeClient:
    def __init__(self, response):
        self.messages = _FakeMessages(response)


_PLAN_JSON = json.dumps({
    "narrative": "A test narrative.",
    "clip_options": [{"clip_index": 0, "crop_aspect": "9:16", "reverse": False,
                       "speed": 1.0, "rationale": "fits"}],
    "transitions": [],
    "text_overlays": [],
    "music_prompt": None,
})


def test_plan_video_compose_attaches_usage_on_clean_json(monkeypatch):
    fake_response = _FakeResponse(_PLAN_JSON, input_tokens=1000, output_tokens=200)
    monkeypatch.setattr(ai_generator, "_client", lambda: _FakeClient(fake_response))

    result = ai_generator.plan_video_compose(
        brief="A cool brief", clip_meta=[{"clip_index": 0, "duration_seconds": 5}],
    )

    assert result["_usage"] == {"input_tokens": 1000, "output_tokens": 200}
    # Caller does result.pop("_usage") then returns the rest as the plan —
    # must not raise, and the remaining keys must be the real plan fields.
    usage = result.pop("_usage")
    assert usage == {"input_tokens": 1000, "output_tokens": 200}
    assert result["narrative"] == "A test narrative."
    assert "_usage" not in result


def test_plan_video_compose_attaches_usage_on_fenced_json(monkeypatch):
    """The model sometimes wraps JSON in markdown fences despite instructions
    not to — the fallback parse branch must also attach _usage."""
    fenced = "```json\n" + _PLAN_JSON + "\n```"
    fake_response = _FakeResponse(fenced, input_tokens=50, output_tokens=75)
    monkeypatch.setattr(ai_generator, "_client", lambda: _FakeClient(fake_response))

    result = ai_generator.plan_video_compose(
        brief="Another brief", clip_meta=[{"clip_index": 0, "duration_seconds": 5}],
    )

    assert result["_usage"] == {"input_tokens": 50, "output_tokens": 75}
    result.pop("_usage")  # must not raise KeyError
    assert result["narrative"] == "A test narrative."
