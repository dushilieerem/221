"""因子需求边界、说明完整性和白名单失败后的单次修正。"""
import json
from uuid import uuid4

from fastapi.testclient import TestClient
from app.main import app
from app.api import ai_factor
from app.schemas.backtest import GeneratedFactor

client = TestClient(app)


def draft(code="def volatility(self):\n    return self.close.pct_change(1, fill_method=None).rolling(20, min_periods=20).std(ddof=1) * (252 ** 0.5)"):
    return {
        "outcome": "factor", "reason": "", "factor_name": "volatility", "factor_code": code,
        "description": "20日年化历史波动率", "used_fields": ["close"], "used_library_methods": [],
        "assumptions": [], "warnings": [], "minimum_history": "至少21个连续收盘价观察值",
        "missing_value_handling": "价格缺失不填充，20个收益率不足时保持空值",
    }


def configure(monkeypatch, outputs):
    calls = []
    monkeypatch.setenv("AI_FACTOR_API_KEY", "test-key")
    ai_factor._IDEMPOTENCY.clear()
    def provider(settings, messages):
        calls.append(messages)
        output = outputs[min(len(calls) - 1, len(outputs) - 1)]
        if isinstance(output, Exception):
            raise output
        return json.dumps(output)
    monkeypatch.setattr(ai_factor, "_call_provider", provider)
    return calls


def generate(key=None):
    return client.post("/api/ai/factor-generations", json={
        "prompt": "20日收益率样本标准差乘252平方根，请说明缺失值和最少数据要求", "idempotency_key": str(key or uuid4())})


def test_repairs_disallowed_sqrt_without_weakening_whitelist(monkeypatch):
    bad = draft("def volatility(self):\n    return self.close.rolling(20).std().pow(0.5)")
    calls = configure(monkeypatch, [bad, draft()])
    key = uuid4()
    body = generate(key).json()
    assert body["status"] == "ready"
    assert "** 0.5" in body["factor"]["factor_code"]
    assert len(calls) == 2
    assert "白名单" in calls[1][-1]["content"]
    assert generate(key).json()["generation_id"] == body["generation_id"]
    assert len(calls) == 2
    assert ai_factor._validate_factor_code(GeneratedFactor.model_validate(bad))


def test_never_retries_indefinitely_or_returns_rejected_code(monkeypatch):
    calls = configure(monkeypatch, [draft("def volatility(self):\n    import os\n    return self.close")])
    body = generate().json()
    assert body["status"] == "rejected" and body["factor"] is None
    assert len(calls) == 2


def test_repair_timeout_preserves_first_validation_failure(monkeypatch):
    calls = configure(monkeypatch, [draft("def volatility(self):\n    return self.close.sqrt()"), TimeoutError()])
    response = generate()
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "rejected" and body["validation"]["errors"]
    assert body["validation"]["warnings"] and len(calls) == 2


def test_unsupported_or_ambiguous_request_has_reason_and_no_code(monkeypatch):
    for outcome, reason in [("unsupported", "当前只有日频价量字段，没有逐笔撤单数据"), ("needs_clarification", "请提供因子计算公式或周期")]:
        data = {**draft(), "outcome": outcome, "reason": reason, "factor_code": ""}
        calls = configure(monkeypatch, [data])
        body = generate().json()
        assert body["status"] == "rejected" and body["factor"] is None
        assert reason in body["validation"]["errors"][0]
        assert len(calls) == 1


def test_missing_explanation_repaired_and_exposed_in_assumptions(monkeypatch):
    incomplete = {**draft(), "minimum_history": ""}
    calls = configure(monkeypatch, [incomplete, draft()])
    body = generate().json()
    assert body["status"] == "ready" and len(calls) == 2
    notes = body["factor"]["assumptions"]
    assert any("21" in note and "历史数据要求" in note for note in notes)
    assert any("缺失值处理" in note for note in notes)


def test_prompt_uses_actual_whitelist_and_structured_capability_outcomes():
    from app.schemas.backtest import FactorGenerationRequest
    messages = ai_factor._messages(FactorGenerationRequest(prompt="生成20日波动率因子", idempotency_key=uuid4()))
    system = messages[0]["content"]
    assert all(method in system for method in ai_factor.ALLOWED_METHODS)
    assert all(field in system for field in ai_factor.ALLOWED_FIELDS)
    assert "unsupported" in system and "needs_clarification" in system
    assert "minimum_history" in system and "missing_value_handling" in system


def test_validator_rejects_forward_prices_and_centered_windows():
    for source in [
        "def volatility(self):\n    return self.close.shift(-1)",
        "def volatility(self, window=2):\n    return self.close.pct_change(-window)",
        "def volatility(self):\n    offset = -2\n    return self.close.shift(periods=offset)",
        "def volatility(self, window=-2):\n    return self.close.shift(window)",
        "def volatility(self):\n    return self.close.rolling(20, center=True).mean()",
        "def volatility(self):\n    return self.close.rolling(20, 20, True).mean()",
        "def volatility(self):\n    return self.close.fillna(method='bfill')",
        "def volatility(self):\n    return self.close.pct_change(fill_method='backfill')",
        "def volatility(self):\n    return self.close.shift(freq='-1D')",
    ]:
        errors = ai_factor._validate_factor_code(GeneratedFactor.model_validate(draft(source)))
        assert errors, source
        assert any("未来" in error for error in errors)


def test_validator_preserves_past_only_parameterized_windows():
    for source in [
        "def volatility(self, window=20):\n    return self.close.shift(window + 1)",
        "def volatility(self):\n    offset = 2\n    return self.close.pct_change(periods=offset, fill_method=None)",
        "def volatility(self, centered=False):\n    return self.close.rolling(20, center=centered).mean()",
    ]:
        assert ai_factor._validate_factor_code(GeneratedFactor.model_validate(draft(source))) == []
