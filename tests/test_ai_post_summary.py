import json
from fastapi.testclient import TestClient
from app.main import app
from app.api import ai_factor, ai_post_summary, llm_provider

client = TestClient(app)
PAYLOAD = {"title": "虚构种植记录", "body": "周一种薄荷，周三浇水，周五靠窗的一盆长得更快。观察不足，无法确定原因。"}


def configure(monkeypatch):
    monkeypatch.setenv("AI_FACTOR_API_KEY", "test-summary-secret")
    monkeypatch.setenv("AI_FACTOR_BASE_URL", "https://approved-model.test/v1")
    monkeypatch.setenv("AI_FACTOR_MODEL_ALIAS", "shared-generation-model")


def test_summary_reuses_factor_provider_and_preserves_usage(monkeypatch):
    configure(monkeypatch)
    seen = []
    def complete(settings, messages, **kwargs):
        seen.append((settings, messages, kwargs))
        return {"content": json.dumps({"summary_md": "## 结论\n薄荷长势不同，尚不能确定原因。"}), "model": settings.model, "tokens_in": 32, "tokens_out": 12}
    monkeypatch.setattr(llm_provider, "complete_result", complete)
    response = client.post("/api/ai/post-summaries", json=PAYLOAD)
    assert response.status_code == 200
    data = response.json()
    assert data["summary_source"] == "llm" and data["tokens_in"] == 32 and data["tokens_out"] == 12
    assert seen[0][0] == ai_factor._settings_from_env()
    assert data["model"] == "shared-generation-model"
    assert "test-summary-secret" not in response.text
    assert "test-summary-secret" not in str(seen[0][1])
    assert seen[0][2]["response_format"]["json_schema"]["strict"] is True


def test_summary_keeps_grounding_and_truncation_in_model_input(monkeypatch):
    configure(monkeypatch)
    seen = []
    def complete(settings, messages, **kwargs):
        seen.append(messages)
        return {"content": '{"summary_md":"引用指标不变。"}', "model": "m", "tokens_in": 1, "tokens_out": 1}
    monkeypatch.setattr(llm_provider, "complete_result", complete)
    grounding = {"platform_ref": {"metrics_display": {"年化": "12.34%"}}}
    response = client.post("/api/ai/post-summaries", json={**PAYLOAD, "grounding": grounding, "truncated": True})
    assert response.status_code == 200
    source = json.loads(seen[0][1]["content"])
    assert source["grounding"] == grounding and source["truncated"] is True
    assert "不能改数" in seen[0][0]["content"]


def test_summary_rejects_empty_oversized_or_credentials_in_request():
    for payload in [{"body": " "}, {**PAYLOAD, "body": "x" * 20001},
                    {**PAYLOAD, "api_key": "should-not-be-accepted"},
                    {**PAYLOAD, "messages": []}, {**PAYLOAD, "grounding": {"x": "x" * 12001}}]:
        assert client.post("/api/ai/post-summaries", json=payload).status_code == 422


def test_summary_provider_failures_are_explicit_without_mock_or_secret(monkeypatch):
    configure(monkeypatch)
    for error, expected in [(TimeoutError("private vendor details"), 504), (llm_provider.ProviderUnavailable("private vendor details"), 503)]:
        def fail(*args, **kwargs):
            raise error
        monkeypatch.setattr(llm_provider, "complete_result", fail)
        response = client.post("/api/ai/post-summaries", json=PAYLOAD)
        assert response.status_code == expected
        assert "private" not in response.text and "summary_md" not in response.json()


def test_summary_malformed_or_blank_model_output_is_not_success(monkeypatch):
    configure(monkeypatch)
    for content in ['not json', '{}', '{"summary_md":"  "}', '{"summary_md": 42}']:
        monkeypatch.setattr(llm_provider, "complete_result", lambda *args, **kwargs: {"content": content, "model": "m", "tokens_in": 1, "tokens_out": 1})
        assert client.post("/api/ai/post-summaries", json=PAYLOAD).status_code == 502


def test_summary_missing_provider_is_reported(monkeypatch):
    monkeypatch.delenv("AI_FACTOR_API_KEY", raising=False)
    monkeypatch.delenv("ARK_API_KEY", raising=False)
    from app.config import default_runtime_settings
    monkeypatch.setattr(ai_post_summary, "get_runtime_settings", default_runtime_settings)
    assert client.post("/api/ai/post-summaries", json=PAYLOAD).status_code == 503


def test_summary_route_uses_real_provider_protocol_and_counts_usage(monkeypatch):
    configure(monkeypatch)
    sent = []
    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def read(self):
            return json.dumps({"model": "reported-model", "choices": [{"message": {"content": '{"summary_md":"真实协议测试摘要"}'}}], "usage": {"prompt_tokens": 123, "completion_tokens": 45}}).encode()
    def urlopen(request, timeout):
        sent.append(request)
        return Response()
    monkeypatch.setattr(llm_provider.urllib.request, "urlopen", urlopen)
    response = client.post("/api/ai/post-summaries", json=PAYLOAD)
    assert response.status_code == 200
    assert response.json()["tokens_in"] == 123 and response.json()["tokens_out"] == 45
    assert response.json()["model"] == "reported-model"
    assert sent[0].full_url == "https://approved-model.test/v1/chat/completions"
    assert json.loads(sent[0].data)["model"] == "shared-generation-model"
    assert "test-summary-secret" not in response.text
