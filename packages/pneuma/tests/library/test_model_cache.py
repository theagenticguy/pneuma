"""Prompt-cache wiring for `model.opus5`, and the fork-beam reuse it exists for.

Bedrock's Converse API caches nothing for Anthropic models unless the request carries an
explicit `cachePoint` block, so "the branches share a byte-identical prefix" only turns into
cache reads if the model actually asks for them. The offline tests here pin the request
construction — `opus5()` must produce requests whose last user message ends in a cachePoint,
and `cache=False` must produce requests without one — by calling the runtime's own
`format_request`, which is the exact code path a live call goes through.

The live test is the measured claim: two beam branches from one seeded root, and the second
branch's call reports `cacheReadInputTokens > 0`. It is opt-in behind `PNEUMA_LIVE_CACHE=1`
because it spends real tokens; the seed is padded past the provider's minimum cacheable
prefix (~4k tokens for Opus-class models) because a shorter prefix silently skips caching
and the assertion would fail for a reason that is not a wiring defect.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterable
from dataclasses import dataclass
from typing import Any

import pytest
from ai_functions.testing import RuntimeHarness
from pydantic import BaseModel, Field
from strands.models import Model

from pneuma.gated import GatedProposer
from pneuma.method import ai_method
from pneuma.model import opus5

# ── Offline: the request carries the cache point ──


def _user(text: str) -> dict[str, Any]:
    return {"role": "user", "content": [{"text": text}]}


def _assistant(text: str) -> dict[str, Any]:
    return {"role": "assistant", "content": [{"text": text}]}


def _cache_points(request: dict[str, Any]) -> list[tuple[int, int]]:
    """Every (message, block) position holding a cachePoint in a formatted request."""
    return [
        (m, b)
        for m, message in enumerate(request["messages"])
        for b, block in enumerate(message["content"])
        if "cachePoint" in block
    ]


def test_opus5_appends_one_cache_point_to_the_last_user_message() -> None:
    """The default model asks Bedrock to cache: exactly one cachePoint, trailing the last
    user message, so the entire conversation prefix lands inside the cached span."""
    model = opus5()
    history = [_user("seed question"), _assistant("seed answer"), _user("branch question")]

    request = model.format_request(history)

    positions = _cache_points(request)
    last = len(request["messages"]) - 1
    assert positions == [(last, len(request["messages"][last]["content"]) - 1)]
    assert request["messages"][last]["content"][-1] == {"cachePoint": {"type": "default"}}


def test_opus5_formats_identical_histories_identically() -> None:
    """The fork-beam premise: two branches replaying the same log make byte-identical
    requests, cache point included, so branch 2 reads what branch 1 wrote."""
    model = opus5()
    history = [_user("seed"), _assistant("answer"), _user("go")]

    assert model.format_request(history) == model.format_request(history)


def test_opus5_cache_off_sends_no_cache_point() -> None:
    """`cache=False` is the escape hatch, and it must actually reach the request."""
    model = opus5(cache=False)

    request = model.format_request([_user("hello")])

    assert _cache_points(request) == []


# ── Live: the second branch reads the cache the first one wrote ──
#
# Module level, as everywhere else: `compile_ai_method` resolves annotations against module
# globals, so the output type cannot be function-local.


class Guess(BaseModel):
    """A minimal proposal shape for the live beam."""

    value: int = Field(description="Any integer between 1 and 100.")
    why: str = Field(description="One sentence of reasoning.")


@dataclass(frozen=True)
class Anything:
    """A verdict that admits everything: the live test measures caching, not the gate.

    The class itself is the gate — calling it with the candidate constructs the verdict.
    """

    candidate: Any

    @property
    def ok(self) -> bool:
        return True

    def report_text(self) -> str:
        return "unreachable: this verdict admits everything"


class Guesser(GatedProposer):
    """The smallest live proposer: propose an integer, admit whatever comes back."""

    name = "cache-guesser"

    @ai_method(Guess, description="Propose an integer", max_attempts=2)
    def propose(self, hint: str) -> Guess:
        """Read the briefing and propose one integer. Briefing: {hint}"""


class UsageRecording(Model):
    """Delegate to a real model and keep each call's usage from the metadata stream event.

    Recording from the stream rather than the coordinator log because `propose_k` retires
    every thread in a `finally`, and the model's own stream is the surface that survives.
    """

    def __init__(self, inner: Model) -> None:
        super().__init__()
        self._inner = inner
        self.usages: list[dict[str, Any]] = []

    def update_config(self, **model_config: Any) -> None:
        self._inner.update_config(**model_config)

    def get_config(self) -> Any:
        return self._inner.get_config()

    def structured_output(self, *args: Any, **kwargs: Any) -> Any:
        return self._inner.structured_output(*args, **kwargs)

    def stream(self, *args: Any, **kwargs: Any) -> AsyncIterable[Any]:
        inner = self._inner.stream(*args, **kwargs)

        async def _tap() -> Any:
            async for event in inner:
                if isinstance(event, dict) and "usage" in event.get("metadata", {}):
                    self.usages.append(dict(event["metadata"]["usage"]))
                yield event

        return _tap()


def _binding(agent: GatedProposer, model: Model) -> Any:
    """Bind `model` by replacing `compiled` on the instance, as `test_gated.py` does."""
    original = type(agent).compiled

    def compiled(name: str, **overrides: Any) -> Any:
        overrides.setdefault("model", model)
        return original(agent, name, **overrides)

    return compiled


# Long enough that the seeded prefix clears the provider's minimum cacheable length; content
# is irrelevant, only its stability across branches matters.
_BRIEFING = "\n".join(
    f"Constraint {i}: the proposed integer must not equal {i} plus any prior rejection."
    for i in range(700)
)

live = pytest.mark.skipif(
    os.environ.get("PNEUMA_LIVE_CACHE") != "1",
    reason="needs Bedrock; set PNEUMA_LIVE_CACHE=1 to measure fork-beam cache reuse for real",
)


@live
async def test_fork_beam_branch_two_reads_the_cache_branch_one_wrote() -> None:
    """The measured claim: on a k=2 beam from one seeded root, the last branch's model call
    reports cache-read tokens, so the shared prefix was served from the provider cache."""
    async with RuntimeHarness() as harness:
        model = UsageRecording(opus5("low", max_tokens=8_192, show_thinking=False))
        guesser = Guesser(Anything)
        guesser.compiled = _binding(guesser, model)  # type: ignore[method-assign]

        admitted = await guesser.propose_k(
            2, harness.coordinator, hint="Answer now.", seed=[{"hint": _BRIEFING}]
        )

        assert len(admitted) == 2, "the gate admits everything, so both branches must land"
        # Calls: seed cycle, branch 0, branch 1 (plus any validation retries). The last call
        # is branch 1's, whose prefix branch 0 just wrote to the cache.
        assert len(model.usages) >= 3
        assert model.usages[-1].get("cacheReadInputTokens", 0) > 0, (
            f"branch 2 read nothing from the cache: {model.usages}"
        )
