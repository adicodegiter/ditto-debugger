import os
import unittest
from unittest import mock

import requests

from ai.explain import explain_findings
from ai.llm import ENDPOINT, GroqClient, LLMError

FINDINGS = [{"schema_version": "1.0", "id": "r1-availability", "run_id": "r1", "severity": "high",
             "summary": "orig", "evidence": ["run:r1"], "recommendation": "orig rec", "confidence": 0.9, "ai_generated": False}]
GOOD = '{"findings": [{"id": "r1-availability", "summary": "new", "recommendation": "new rec"}]}'


class FakeResponse:
    def __init__(self, text, status=200):
        self._text, self.status_code = text, status

    def raise_for_status(self):
        if self.status_code >= 400:
            error = requests.HTTPError(f"HTTP {self.status_code}")
            error.response = self
            raise error

    def json(self):
        return {"choices": [{"message": {"content": self._text}}]}


class FakeSession:
    def __init__(self, text, status=200):
        self.text, self.status, self.calls = text, status, []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeResponse(self.text, self.status)


def client(text="{}", key="k", provider="groq", max_calls="30", status=200, model=""):
    env = {"AI_PROVIDER": provider, "GROQ_API_KEY": key, "GROQ_MAX_CALLS": max_calls, "GROQ_MODEL": model}
    with mock.patch.dict(os.environ, env), mock.patch("ai.llm.load_env"):
        return GroqClient(session=FakeSession(text, status))


class GroqAdapterTests(unittest.TestCase):
    def test_no_key_or_provider_none_falls_back_without_calling(self):
        for c in (client(key=""), client(provider="none")):
            self.assertFalse(c.available())
            self.assertEqual(explain_findings(FINDINGS, {"run_id": "r1"}, c), FINDINGS)
            self.assertEqual(c._session.calls, [])

    def test_template_placeholder_key_counts_as_missing(self):
        c = client(key="INSERT_GROQ_API_KEY_HERE")
        self.assertFalse(c.available())
        self.assertEqual(explain_findings(FINDINGS, {"run_id": "r1"}, c), FINDINGS)
        self.assertEqual(c._session.calls, [])

    def test_request_shape_uses_bearer_key_json_mode_and_configured_model(self):
        c = client(GOOD, model="some-model")
        explain_findings(FINDINGS, {"run_id": "r1"}, c)
        url, kwargs = c._session.calls[0]
        self.assertEqual(url, ENDPOINT)
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer k")
        self.assertEqual(kwargs["json"]["model"], "some-model")
        self.assertEqual(kwargs["json"]["response_format"], {"type": "json_object"})
        self.assertIn("JSON", kwargs["json"]["messages"][-1]["content"])
        self.assertNotIn("k", url.split("/")[-1])
        self.assertEqual(client().model, "llama-3.3-70b-versatile")

    def test_valid_output_rewrites_text_only(self):
        out = explain_findings(FINDINGS, {"run_id": "r1"}, client(GOOD))[0]
        self.assertEqual((out["summary"], out["recommendation"], out["ai_generated"]), ("new", "new rec", True))
        self.assertEqual((out["severity"], out["evidence"], out["confidence"]), ("high", ["run:r1"], 0.9))

    def test_invalid_output_falls_back(self):
        bad = ['not json', '{}', '[]', '{"findings": []}', '{"findings": "x"}',
               '{"findings": [{"id": "unknown", "summary": "a", "recommendation": "b"}]}',
               '{"findings": [{"id": "r1-availability", "summary": "", "recommendation": "b"}]}',
               '{"findings": [{"id": "r1-availability", "summary": "%s", "recommendation": "b"}]}' % ("x" * 700)]
        for text in bad:
            self.assertEqual(explain_findings(FINDINGS, {"run_id": "r1"}, client(text)), FINDINGS, text)

    def test_http_errors_and_rate_limits_fall_back_and_do_not_leak_the_key(self):
        for status in (401, 429, 500):
            c = client(GOOD, key="SECRETKEY", status=status)
            self.assertEqual(explain_findings(FINDINGS, {"run_id": "r1"}, c), FINDINGS)
            with self.assertRaises(LLMError) as caught:
                client(GOOD, key="SECRETKEY", status=status).generate_json("p")
            self.assertNotIn("SECRETKEY", str(caught.exception))
            self.assertIn(f"HTTP {status}", str(caught.exception))

    def test_dry_run_and_forced_test_are_not_sent_to_the_model(self):
        c = client(GOOD)
        for run in ({"dry_run": True}, {"forced_test": True}):
            self.assertEqual(explain_findings(FINDINGS, run, c), FINDINGS)
        self.assertEqual(c._session.calls, [])

    def test_call_budget(self):
        c = client('{"ok": 1}', max_calls="1")
        self.assertEqual(c.generate_json("p"), {"ok": 1})
        self.assertFalse(c.available())
        with self.assertRaises(LLMError):
            c.generate_json("p")


if __name__ == "__main__":
    unittest.main()
