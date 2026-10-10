"""AI helpers against a local fake of an OpenAI-compatible API (real SDK, real HTTP)."""

import json
import threading
from datetime import timedelta
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.core.models import User
from apps.insights import ai
from apps.insights.models import AIRequest
from apps.platform import conf


class FakeLLM(BaseHTTPRequestHandler):
    requests: list = []
    reply: dict | str = {}
    reject_schema = False
    cost = None

    def _send(self, status, payload):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        if self.path.endswith("/models"):
            return self._send(200, {"object": "list", "data": [{"id": "acme/model-1", "object": "model"}]})
        if self.path.endswith("/key"):
            ok = self.headers.get("authorization") == "Bearer sk-good"
            return self._send(
                200 if ok else 401, {"data": {"label": "test key"}} if ok else {"error": {"message": "bad"}}
            )
        self._send(404, {})

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        FakeLLM.requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
        fmt = (body.get("response_format") or {}).get("type")
        if FakeLLM.reject_schema and fmt == "json_schema":
            return self._send(400, {"error": {"message": "response_format json_schema is not supported"}})
        content = FakeLLM.reply if isinstance(FakeLLM.reply, str) else json.dumps(FakeLLM.reply)
        usage = {"prompt_tokens": 1200, "completion_tokens": 300, "total_tokens": 1500}
        if FakeLLM.cost is not None:
            usage["cost"] = FakeLLM.cost
        self._send(
            200,
            {
                "id": "gen-123",
                "object": "chat.completion",
                "created": 0,
                "model": body["model"],
                "choices": [
                    {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": content}}
                ],
                "usage": usage,
            },
        )

    def log_message(self, *args):
        pass


@pytest.fixture
def fake_llm(settings):
    server = HTTPServer(("127.0.0.1", 0), FakeLLM)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    settings.AI_ENABLED = True
    settings.AI_PROVIDER = "openai_compatible"
    settings.AI_BASE_URL = f"http://127.0.0.1:{server.server_port}/v1"
    settings.AI_API_KEY = "sk-good"
    settings.AI_MODEL = "acme/model-1"
    settings.AI_EFFORT = ""
    settings.AI_STRUCTURED_OUTPUT = "json_schema"
    FakeLLM.requests, FakeLLM.reject_schema, FakeLLM.cost = [], False, None
    conf.invalidate()
    yield FakeLLM
    server.shutdown()
    conf.invalidate()


def _future_local(workspace, days=2, hour=10, minute=30):
    when = (timezone.now() + timedelta(days=days)).astimezone(workspace.tz)
    return when.replace(hour=hour, minute=minute, second=0, microsecond=0)


def test_custom_endpoint_with_json_schema(fake_llm, workspace, channel, user):
    when = _future_local(workspace)
    fake_llm.reply = {
        "summary": "Stagger",
        "choices": [
            {"channel_id": channel.id, "local_time": when.strftime("%Y-%m-%dT%H:%M"), "rationale": "Before the event"},
        ],
    }
    out = ai.suggest_times(workspace, user, [channel], "Webinar on Friday at 15:00!")
    s = out["suggestions"][0]
    assert s["source"] == "ai" and s["scheduled_at"] == when and s["rationale"] == "Before the event"
    req = fake_llm.requests[0]
    assert req["path"] == "/v1/chat/completions"
    assert req["headers"]["authorization"] == "Bearer sk-good"
    body = req["body"]
    assert body["model"] == "acme/model-1"
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["schema"]["title"] == "TimeSuggestions"
    assert body["messages"][0]["role"] == "system" and "Webinar on Friday" in body["messages"][1]["content"]
    assert "usage" not in body and "reasoning" not in body
    log = AIRequest.objects.get()
    assert (log.kind, log.input_tokens, log.request_id, log.cost_usd) == ("suggest_times", 1200, "gen-123", None)


def test_openrouter_sends_usage_reasoning_and_records_cost(fake_llm, settings, workspace, channel, user):
    settings.AI_PROVIDER = "openrouter"  # AI_BASE_URL still points at the fake
    settings.AI_EFFORT = "high"
    fake_llm.cost = 0.0123
    fake_llm.reply = {"variants": [{"channel_id": channel.id, "body": "short", "title": "", "notes": ""}]}
    ai.adapt_content(workspace, user, [channel], "Long text")
    req = fake_llm.requests[0]
    assert req["body"]["usage"] == {"include": True}
    assert req["body"]["reasoning"] == {"effort": "high"}
    assert req["headers"]["x-title"] == "Markette"
    assert AIRequest.objects.get().cost_usd == Decimal("0.012300")


def test_falls_back_to_json_object_when_schema_unsupported(fake_llm, workspace, channel, user):
    fake_llm.reject_schema = True
    fake_llm.reply = (
        "```json\n"
        + json.dumps({"variants": [{"channel_id": channel.id, "body": "ok", "title": "", "notes": ""}]})
        + "\n```"
    )
    out = ai.adapt_content(workspace, user, [channel], "Text")
    assert out["variants"][0]["body"] == "ok"
    assert [r["body"].get("response_format", {}).get("type") for r in fake_llm.requests] == [
        "json_schema",
        "json_object",
    ]


def test_unreadable_answer_is_reported(fake_llm, workspace, channel, user):
    fake_llm.reply = "I think Tuesday is nice."
    with pytest.raises(ai.AIUnavailable, match="could not read"):
        ai.adapt_content(workspace, user, [channel], "Text")
    assert AIRequest.objects.get().response == {"raw": "I think Tuesday is nice."}


def test_invalid_ai_time_falls_back_to_engine(fake_llm, workspace, channel, user):
    fake_llm.reply = {
        "summary": "x",
        "choices": [
            {"channel_id": channel.id, "local_time": "2001-01-01T09:00", "rationale": "in the past"},
        ],
    }
    s = ai.suggest_times(workspace, user, [channel], "Hello")["suggestions"][0]
    assert s["source"] == "engine" and s["scheduled_at"] > timezone.now()


def test_suggest_endpoint_works_without_ai(api, channel):
    r = api.post("/api/insights/suggest/", {"channels": [channel.id], "body": "hi"}, format="json")
    assert r.status_code == 200, r.data
    assert r.data["suggestions"][0]["source"] == "engine"


def test_adapt_flags_over_limit(fake_llm, workspace, channel, user):
    fake_llm.reply = {"variants": [{"channel_id": channel.id, "body": "y" * 300, "title": "", "notes": ""}]}
    out = ai.adapt_content(workspace, user, [channel], "Long text")
    assert out["variants"][0]["over_limit"] is True and out["variants"][0]["length"] == 300


def test_platform_test_key(fake_llm, settings, db):
    root = APIClient()
    root.force_authenticate(User.objects.create_superuser("root@example.com", "pw-pw-pw-pw-1"))
    r = root.post("/api/platform/settings/test-ai/").data
    assert r["ok"] is True and "acme/model-1" in r["detail"]
    settings.AI_MODEL = "acme/missing"
    conf.invalidate()
    r = root.post("/api/platform/settings/test-ai/").data
    assert r["ok"] is False and r["models"] == ["acme/model-1"]
    settings.AI_PROVIDER, settings.AI_API_KEY, settings.AI_MODEL = "openrouter", "sk-bad", "openrouter/auto"
    conf.invalidate()
    assert root.post("/api/platform/settings/test-ai/").data["detail"] == "The API key was rejected."
