"""Bedrock model construction for Claude Opus 5 with adaptive thinking."""

from __future__ import annotations

from typing import Literal

from strands.models import BedrockModel, CacheConfig

MODEL_ID = "global.anthropic.claude-opus-5"
REGION = "us-east-1"

Effort = Literal["low", "medium", "high", "xhigh", "max"]


def opus5(
    effort: Effort = "xhigh",
    *,
    max_tokens: int = 40_000,
    show_thinking: bool = True,
    cache: bool = True,
) -> BedrockModel:
    """A Claude Opus 5 Bedrock model with adaptive thinking at `effort`.

    Opus 5 rejects `thinking.type=enabled` and `budget_tokens`; depth is
    controlled by `output_config.effort` instead, and `max_tokens` is the only
    ceiling — thinking tokens are drawn from the same budget as the answer.
    Temperature and top_p must stay unset: adaptive thinking rejects any
    temperature but 1 and any top_p below 0.95.

    The default is generous because a tight budget at `xhigh` truncates the
    answer rather than the reasoning: two subagents in an early run died with
    MaxTokensReachedException at 24k while thinking through a dense evidence set.

    **Why `cache` defaults to on.** Bedrock's Converse API does not cache
    Anthropic prompts unless the request carries an explicit `cachePoint`
    block; without one, every request pays full input price even when its
    prefix is byte-identical to the previous call. `CacheConfig(strategy=
    "auto")` has the runtime append `{"cachePoint": {"type": "default"}}` to
    the last user message of every request, so any shared prefix — multi-turn
    history rebuilt from the event log, and in particular the byte-identical
    branches `GatedProposer.propose_k` forks from one seeded root — is served
    from the provider cache from the second request on. Branches run serially
    there, so branch 1's write completes before branch 2's read. The one
    caveat: prefixes below the provider's minimum cacheable length (~1-4k
    tokens) silently skip caching, which is harmless — the cachePoint is
    ignored, never rejected.
    """
    return BedrockModel(
        model_id=MODEL_ID,
        region_name=REGION,
        max_tokens=max_tokens,
        streaming=True,
        cache_config=CacheConfig(strategy="auto") if cache else None,
        additional_request_fields={
            "thinking": {
                "type": "adaptive",
                "display": "summarized" if show_thinking else "omitted",
            },
            "output_config": {"effort": effort},
        },
    )
