"""Bedrock model construction for Claude Opus 5 with adaptive thinking."""

from __future__ import annotations

from typing import Literal

from strands.models import BedrockModel

MODEL_ID = "global.anthropic.claude-opus-5"
REGION = "us-east-1"

Effort = Literal["low", "medium", "high", "xhigh", "max"]


def opus5(
    effort: Effort = "xhigh",
    *,
    max_tokens: int = 40_000,
    show_thinking: bool = True,
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
    """
    return BedrockModel(
        model_id=MODEL_ID,
        region_name=REGION,
        max_tokens=max_tokens,
        streaming=True,
        additional_request_fields={
            "thinking": {
                "type": "adaptive",
                "display": "summarized" if show_thinking else "omitted",
            },
            "output_config": {"effort": effort},
        },
    )
