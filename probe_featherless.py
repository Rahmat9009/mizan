from __future__ import annotations

import json
import os
import sys
from typing import Any

from dotenv import load_dotenv
from openai import OpenAI

from app.models import AIRiskAnalysis, Decision
from app.providers.featherless_risk import (
    DEFAULT_FEATHERLESS_BASE_URL,
    DEFAULT_FEATHERLESS_MODEL,
    FeatherlessRiskPayload,
    risk_analysis_contract,
)


PROBE_PROPOSAL_ID = "fictional-nvda-protocol-probe"
HARD_CAP = 40


def _usage(value: Any) -> Any:
    dump = getattr(value, "model_dump", None)
    return dump(mode="json") if callable(dump) else None


def main() -> int:
    load_dotenv()
    api_key = os.getenv("FEATHERLESS_API_KEY", "").strip()
    if not api_key:
        print("FEATHERLESS_API_KEY is not configured; no live probe was made.")
        return 1

    model = os.getenv("FEATHERLESS_MODEL", "").strip() or DEFAULT_FEATHERLESS_MODEL
    base_url = (
        os.getenv("FEATHERLESS_BASE_URL", "").strip() or DEFAULT_FEATHERLESS_BASE_URL
    ).rstrip("/")
    enable_thinking = os.getenv("FEATHERLESS_ENABLE_THINKING", "false").strip().casefold() in {
        "1",
        "true",
        "yes",
        "on",
    }

    client = OpenAI(
        api_key=api_key,
        base_url=base_url,
        timeout=60,
        max_retries=0,
        default_headers={"X-Title": "Portfolio Governor Featherless Probe"},
    )
    prompt = {
        "fictional_trade": "BUY 40 NVDA shares",
        "hard_policy_quantity_cap": HARD_CAP,
        "context": [
            "Existing NVDA exposure",
            "Existing AMD exposure",
            "Elevated volatility",
            "Recent drawdown",
            "Adequate liquidity",
            "Generic thesis",
            "Vague invalidation condition",
        ],
        "output_schema": risk_analysis_contract(),
    }

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a skeptical contextual portfolio risk reviewer. Use only supplied facts; "
                        "do not invent news, prices, measured correlations, or other data. Return exactly one "
                        "JSON object matching output_schema and no markdown."
                    ),
                },
                {"role": "user", "content": json.dumps(prompt, indent=2)},
            ],
            response_format={"type": "json_object"},
            temperature=0,
            max_tokens=1000,
            extra_body={"chat_template_kwargs": {"enable_thinking": enable_thinking}},
        )

        if len(response.choices) != 1:
            raise ValueError("Expected exactly one response choice.")
        choice = response.choices[0]
        message = choice.message
        metadata = {
            "model": model,
            "finish_reason": choice.finish_reason,
            "usage": _usage(response.usage),
            "content_exists": bool(message.content),
            "reasoning_exists": bool(
                getattr(message, "reasoning", None)
                or getattr(message, "reasoning_content", None)
            ),
            "tool_call_names": [call.function.name for call in (message.tool_calls or [])],
        }
        print("Sanitized response metadata")
        print(json.dumps(metadata, indent=2))

        if choice.finish_reason != "stop":
            raise ValueError("Response did not finish normally.")
        if message.tool_calls:
            raise ValueError("JSON mode unexpectedly returned tool calls.")
        if not message.content:
            raise ValueError("Response has no JSON content.")

        payload = json.loads(message.content)
        validated = FeatherlessRiskPayload.model_validate(payload)
        analysis = AIRiskAnalysis(
            proposal_id=PROBE_PROPOSAL_ID,
            model_name=model,
            **validated.model_dump(),
        )
        if analysis.recommended_quantity > HARD_CAP:
            raise ValueError("Recommended quantity exceeds the fictional hard cap.")
        if analysis.recommendation == Decision.REJECT and analysis.recommended_quantity != 0:
            raise ValueError("REJECT must recommend quantity zero.")
        if analysis.recommendation == Decision.REDUCE and not (
            0 < analysis.recommended_quantity < HARD_CAP
        ):
            raise ValueError("REDUCE must recommend a positive quantity below the hard cap.")
        if analysis.recommendation == Decision.APPROVE and analysis.recommended_quantity != HARD_CAP:
            raise ValueError("APPROVE must preserve the proposed quantity.")

        print("\nValidated AIRiskAnalysis")
        print(json.dumps(analysis.model_dump(mode="json"), indent=2))
        print("\nProtocol result: SUCCESS (no trade execution capability is present).")
        return 0
    except Exception as exc:
        print(f"Protocol result: FAILED ({type(exc).__name__}): {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
