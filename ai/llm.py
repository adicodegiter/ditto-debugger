"""Groq client for Agent 3 (requests only; no extra dependency).

Safety properties:
  * The model gets text in and returns text out. It has no tools and executes nothing.
  * The key is read only from the environment or the git-ignored repo-root `.env`.
  * `AI_PROVIDER=none` (default) or a missing key means `available()` is False and callers
    use their deterministic fallback. Calls are capped per process (GROQ_MAX_CALLS).
  * Any HTTP error, rate limit (429), timeout or malformed reply raises LLMError; callers
    fall back to the deterministic result. The error text never contains the key.

Set in `.env` (copy `.env.example`):
  AI_PROVIDER=groq
  GROQ_API_KEY=...              <- the only place the key goes
  GROQ_MODEL=...                <- a model id from Groq's current model list
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[1]
ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"
DEFAULT_MODEL = "llama-3.3-70b-versatile"


class LLMError(RuntimeError):
    pass


def load_env(path: Path | None = None) -> None:
    """Load KEY=VALUE lines from the repo-root .env without overriding real environment variables."""
    path = path or ROOT / ".env"
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


class GroqClient:
    def __init__(self, session: Any = None) -> None:
        load_env()
        self.provider = os.environ.get("AI_PROVIDER", "none").lower()
        self._key = os.environ.get("GROQ_API_KEY", "")
        self.model = os.environ.get("GROQ_MODEL") or DEFAULT_MODEL
        self.max_calls = int(os.environ.get("GROQ_MAX_CALLS", "30"))
        self.calls = 0
        self._session = session or requests

    def available(self) -> bool:
        return self.provider == "groq" and bool(self._key) and self.calls < self.max_calls

    def generate_json(self, prompt: str, *, system: str = "", timeout: float = 60.0) -> Any:
        """Return the parsed JSON object from the model, or raise LLMError. Callers must still validate the content.

        Groq's JSON mode returns a JSON object (not a bare list) and needs the word "JSON" in the prompt.
        """
        if not self.available():
            raise LLMError("Groq unavailable (AI_PROVIDER, GROQ_API_KEY or call budget)")
        messages = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
        body = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.2,
            "max_tokens": 2048,
            "response_format": {"type": "json_object"},
        }
        self.calls += 1
        try:
            response = self._session.post(
                ENDPOINT, json=body, timeout=timeout,
                headers={"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"},
            )
            response.raise_for_status()
            return json.loads(response.json()["choices"][0]["message"]["content"])
        except (requests.RequestException, KeyError, IndexError, TypeError, ValueError) as error:
            status = getattr(getattr(error, "response", None), "status_code", None)
            raise LLMError(f"Groq call failed: {type(error).__name__}{f' (HTTP {status})' if status else ''}") from None


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Check the Groq setup with one tiny request (no run data is sent; the key is never printed).")
    parser.parse_args()
    client = GroqClient()
    print(f"AI_PROVIDER={client.provider}  model={client.model}  key={'set' if client._key else 'missing'}")
    if not client.available():
        print("Groq is off: set AI_PROVIDER=groq and GROQ_API_KEY in .env. The deterministic findings are used meanwhile.")
        return 1
    try:
        reply = client.generate_json('Reply with the JSON object {"ok": true}.')
    except LLMError as error:
        print(f"FAILED: {error}")
        return 2
    print(f"OK: Groq answered {reply!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
