"""Optional Groq rewrite of finding text. The deterministic findings stay the source of truth.

The model may only change `summary` and `recommendation` of findings that already exist.
Ids, severity, evidence and confidence always come from the deterministic finding. Any
invalid or unavailable model output returns the findings unchanged.
"""
from __future__ import annotations

import json
from typing import Any

from .llm import GroqClient, LLMError

SYSTEM = (
    "You are a reliability analyst for an Eclipse Ditto chaos experiment. Use ONLY the evidence given. "
    "Do not invent numbers, services or causes. Reply with JSON only."
)
MAX_TEXT = 600


def _valid(payload: Any, ids: set[str]) -> dict[str, dict[str, str]] | None:
    items = payload.get("findings") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return None
    out: dict[str, dict[str, str]] = {}
    for item in items:
        if not isinstance(item, dict) or item.get("id") not in ids or item["id"] in out:
            return None
        summary, recommendation = item.get("summary"), item.get("recommendation")
        if not all(isinstance(text, str) and 0 < len(text.strip()) <= MAX_TEXT for text in (summary, recommendation)):
            return None
        out[item["id"]] = {"summary": summary.strip(), "recommendation": recommendation.strip()}
    return out if set(out) == ids else None


def explain_findings(findings: list[dict[str, Any]], run: dict[str, Any], client: GroqClient | None = None) -> list[dict[str, Any]]:
    client = client or GroqClient()
    if not findings or not client.available() or run.get("dry_run") or run.get("forced_test"):
        return findings
    prompt = (
        "Rewrite each finding's summary and recommendation to be specific and actionable for an engineer. "
        'Return a JSON object {"findings": [{"id","summary","recommendation"}, ...]}, one entry per input id, each text under 600 characters.\n'
        + json.dumps({"findings": [{k: f[k] for k in ("id", "severity", "summary", "evidence", "recommendation")} for f in findings],
                      "diagnosis": run.get("diagnosis"), "abort_reason": run.get("abort_reason"),
                      "recovery_verified": run.get("recovery_verified")}, default=str)
    )
    try:
        rewritten = _valid(client.generate_json(prompt, system=SYSTEM), {f["id"] for f in findings})
    except LLMError:
        return findings
    if rewritten is None:
        return findings
    return [{**f, **rewritten[f["id"]], "ai_generated": True} for f in findings]
