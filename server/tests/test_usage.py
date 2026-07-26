"""Token usage accounting: LlmClient.on_usage plumbing and UsageTally."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from app.llm import LlmClient, UsageTally


class _StubCompletions:
    def __init__(self, response: Any) -> None:
        self._response = response
        self.calls = 0

    def create(self, **_kwargs: Any) -> Any:
        self.calls += 1
        return self._response


def _stub_openai(response: Any) -> Any:
    return SimpleNamespace(chat=SimpleNamespace(completions=_StubCompletions(response)))


def _response(content: str = "ok", usage: Any = "default") -> Any:
    if usage == "default":
        usage = SimpleNamespace(prompt_tokens=7, completion_tokens=3)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        usage=usage,
    )


def test_complete_reports_usage_via_per_call_callback() -> None:
    seen: list[tuple[int, int]] = []
    client = LlmClient(_stub_openai(_response()))
    out = client.complete(
        [{"role": "user", "content": "hi"}], on_usage=lambda p, c: seen.append((p, c))
    )
    assert out == "ok"
    assert seen == [(7, 3)]


def test_complete_reports_usage_via_constructor_callback() -> None:
    seen: list[tuple[int, int]] = []
    client = LlmClient(_stub_openai(_response()), on_usage=lambda p, c: seen.append((p, c)))
    client.complete([{"role": "user", "content": "hi"}])
    client.complete([{"role": "user", "content": "again"}])
    assert seen == [(7, 3), (7, 3)]


def test_per_call_callback_overrides_constructor_callback() -> None:
    ctor: list[tuple[int, int]] = []
    per_call: list[tuple[int, int]] = []
    client = LlmClient(_stub_openai(_response()), on_usage=lambda p, c: ctor.append((p, c)))
    client.complete([{"role": "user", "content": "hi"}], on_usage=lambda p, c: per_call.append((p, c)))
    assert per_call == [(7, 3)]
    assert ctor == []


def test_missing_usage_counts_call_with_zero_tokens() -> None:
    """Some OpenAI-compatible providers omit usage; the call must still be counted."""
    seen: list[tuple[int, int]] = []
    client = LlmClient(_stub_openai(_response(usage=None)))
    client.complete([{"role": "user", "content": "hi"}], on_usage=lambda p, c: seen.append((p, c)))
    assert seen == [(0, 0)]


def test_partial_usage_fields_are_guarded() -> None:
    """usage present but with None token fields must not crash accounting."""
    seen: list[tuple[int, int]] = []
    usage = SimpleNamespace(prompt_tokens=None, completion_tokens=5)
    client = LlmClient(_stub_openai(_response(usage=usage)))
    client.complete([{"role": "user", "content": "hi"}], on_usage=lambda p, c: seen.append((p, c)))
    assert seen == [(0, 5)]


def test_no_callback_means_no_usage_side_effects() -> None:
    client = LlmClient(_stub_openai(_response()))
    assert client.complete([{"role": "user", "content": "hi"}]) == "ok"


def test_usage_tally_accumulates() -> None:
    tally = UsageTally()
    tally.add(10, 5)
    tally.add(0, 0)  # provider omitted usage: call still counted
    tally.add(90, 45)
    assert tally.calls == 3
    assert tally.prompt_tokens == 100
    assert tally.completion_tokens == 50
