import numpy as np
import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from unittest.mock import MagicMock, patch
from app.services.novelty.comparison import compare_panels
from app.services.novelty.source import validate_source
from app.schemas.factor_novelty import NoveltyRequest
from app.api.routes.novelty import router
from app.api.dependencies import get_experiment_manager


def panel(rows):
    return pd.DataFrame(
        rows, index=pd.date_range("2024-01-01", periods=len(rows)), columns=list("abcd")
    )


def body():
    return {
        "candidate": {
            "factor_name": "f",
            "factor_code": "def f(self): return self.close",
            "start": "2024-01-01",
            "end": "2024-06-30",
        },
        "references": [
            {"reference_id": "r", "factor_name": "old", "library_name": "lib"}
        ],
        "expected_data_release_id": "release",
        "expected_candidate_source_hash": "hash",
        "expected_candidate_spec_hash": "spec",
    }


def test_sign_and_scale_cannot_escape():
    a = panel([[1, 2, 3, 4], [4, 1, 3, 2]])
    for b in [a, a * 100, -a]:
        r = compare_panels(a, b, 3)
        assert r["mean_abs_spearman"] == pytest.approx(1)
        assert r["valid_days"] == 2 and r["coverage"] == 1


def test_absolute_value_before_time_average():
    a = panel([[1, 2, 3, 4], [1, 2, 3, 4]])
    b = panel([[1, 2, 3, 4], [4, 3, 2, 1]])
    r = compare_panels(a, b, 3)
    assert r["mean_abs_spearman"] == pytest.approx(1)
    assert r["series"][1]["spearman"] == pytest.approx(-1)


def test_coverage_denominator_is_candidate_not_intersection():
    a = panel([[1, 2, 3, 4], [1, 2, 3, 4]])
    b = panel([[1, 2, 3, np.nan], [1, 2, 3, np.nan]])
    r = compare_panels(a, b, 3)
    assert r["coverage"] == 0.75 and r["min_pairs"] == 3


def test_missing_and_constant_days_are_not_zero_correlation():
    a = panel([[1, 2, 3, 4], [1, 2, 3, 4]])
    b = panel([[1, 1, 1, 1], [np.nan, np.nan, 2, 3]])
    r = compare_panels(a, b, 3)
    assert (
        r["status"] == "failed"
        and r["mean_abs_spearman"] is None
        and r["valid_days"] == 0
    )


def test_alignment_is_by_stock_and_day_not_position():
    a = panel([[1, 2, 3, 4], [4, 1, 3, 2]])
    b = a.loc[a.index[::-1], list("dcba")]
    assert compare_panels(a, b, 3)["mean_abs_spearman"] == pytest.approx(1)


def test_duplicate_panel_axes_rejected():
    a = panel([[1, 2, 3, 4]])
    b = a.copy()
    b.columns = ["a", "a", "c", "d"]
    with pytest.raises(ValueError):
        compare_panels(a, b, 3)


@pytest.mark.parametrize(
    "source",
    [
        "import os\ndef f(self): return self.close",
        "@print\ndef f(self): return self.close",
        "def f(self): return self.close.__class__",
        'def f(self): return np.load("x")',
        'def f(self): return self.close.to_csv("x")',
        "def f(self):\n self.close=1\n return self.close",
        "def f(self): return self.close.shift(-1)",
        "def f(self): return self.close.rolling(3,center=True).mean()",
        "def f(self): return self.close.apply(eval)",
    ],
)
def test_custom_input_rejects_io_mutation_dynamic_calls_and_future_lags(source):
    with pytest.raises((ValueError, SyntaxError)):
        validate_source(source, "f")


def test_supports_generated_panel_math_and_existing_return_validation():
    src = """def f(self):
    x=self.close / self.close.shift(1)
    if hasattr(x,"notna") and hasattr(x,"replace"):
        if not x.replace([float("inf"),-float("inf")],float("nan")).notna().to_numpy().any():
            raise ValueError("invalid")
    return x
"""
    assert validate_source(src, "f") == src


def test_reference_warmup_is_effective_above_default():
    b = body()
    b["reference_warmup_sessions"] = 504
    assert NoveltyRequest.model_validate(b).reference_warmup_sessions == 504
    b["reference_warmup_sessions"] = 251
    with pytest.raises(ValueError):
        NoveltyRequest.model_validate(b)


def test_candidate_spec_hash_is_required_and_strict():
    b = body()
    b.pop("expected_candidate_spec_hash")
    with pytest.raises(ValueError):
        NoveltyRequest.model_validate(b)


def test_manifest_duplicates_empty_and_reversed_dates_rejected():
    for mutate in [
        lambda x: x.update(references=[]),
        lambda x: x["references"].extend(x["references"]),
        lambda x: x["candidate"].update(start="2025-01-01"),
    ]:
        b = body()
        mutate(b)
        with pytest.raises(ValueError):
            NoveltyRequest.model_validate(b)


def test_contract_rejects_user_extra_fields_before_scheduling():
    app = FastAPI()
    app.include_router(router, prefix="/api")
    manager = MagicMock()
    app.dependency_overrides[get_experiment_manager] = lambda: manager
    r = TestClient(app).post("/api/factor/novelty", json={**body(), "approved": True})
    assert r.status_code == 422
    manager.submit.assert_not_called()


def test_novelty_cannot_read_or_cancel_other_experiment_types():
    app = FastAPI()
    app.include_router(router, prefix="/api")
    manager = MagicMock()
    manager.get.return_value.metadata = {"type": "factor"}
    app.dependency_overrides[get_experiment_manager] = lambda: manager
    client = TestClient(app)
    assert client.get("/api/factor/novelty/backtest").status_code == 404
    assert client.delete("/api/factor/novelty/backtest").status_code == 404
    manager.get.return_value.cancel.assert_not_called()


def test_declared_reference_history_is_bounded_and_explicit():
    b = body()
    b["references"][0]["max_window"] = 504
    assert NoveltyRequest.model_validate(b).references[0].max_window == 504
    b["references"][0]["max_window"] = True
    with pytest.raises(ValueError):
        NoveltyRequest.model_validate(b)


def test_coverage_contract_includes_total_days_and_quantiles():
    a = panel([[1, 2, 3, 4], [4, 3, 2, 1]])
    b = panel([[1, 2, 3, np.nan], [4, 3, 2, 1]])
    r = compare_panels(a, b, 3)
    assert r["trading_days_total"] == 2 and r["pairs_total"] == 7
    assert r["coverage_pooled"] == 0.875 and r["coverage_by_day_quantiles"]["0"] == 0.75


def test_task_with_all_failed_references_keeps_evidence_but_is_failed():
    from app.api.routes.novelty import task

    manager = MagicMock()
    handle = manager.get.return_value
    handle.metadata = {
        "type": "factor_novelty",
        "progress": {"processed_references": 1, "total_references": 1},
    }
    handle.snapshot.return_value = {
        "run_id": "x",
        "status": "succeeded",
        "cancel_requested": False,
        "result": {"comparisons": [{"status": "failed", "error": "missing"}]},
    }
    result = task(manager, "x")
    assert (
        result["status"] == "failed"
        and result["result"]["comparisons"][0]["error"] == "missing"
    )


def test_failed_reference_diagnosis_names_all_nan_constant_and_insufficient():
    a = panel([[1, 2, 3, 4], [4, 3, 2, 1]])
    all_nan = compare_panels(a, panel([[np.nan] * 4, [np.nan] * 4]), 3)
    assert all_nan["diagnosis"] == "reference_all_nan"
    constant = compare_panels(a, panel([[5, 5, 5, 5], [5, 5, 5, 5]]), 3)
    assert constant["diagnosis"] == "reference_constant"
    sparse = compare_panels(
        a, panel([[1, np.nan, np.nan, np.nan], [2, np.nan, np.nan, np.nan]]), 3
    )
    assert sparse["diagnosis"] == "reference_finite_values_insufficient"


def test_experiment_rejects_candidate_spec_hash_before_compute():
    from types import SimpleNamespace
    from unittest.mock import patch
    from app.services.novelty.experiment import NoveltyExperiment

    req = SimpleNamespace(
        candidate=SimpleNamespace(),
        expected_data_release_id="release",
        expected_candidate_source_hash="source",
        expected_candidate_spec_hash="expected-spec",
        references=[],
        reference_warmup_sessions=252,
        min_pairs=30,
    )
    spec = SimpleNamespace(
        factor_source_hash="source", identity=lambda data_release_id: "actual-spec"
    )
    engine = SimpleNamespace(release_id="release")
    with (
        patch(
            "app.services.novelty.experiment.DataEngine.get_instance",
            return_value=engine,
        ),
        patch("app.services.novelty.experiment._request_spec", return_value=spec),
    ):
        with pytest.raises(ValueError, match="回测规格"):
            NoveltyExperiment(req, {"cancelled": False}).run()


def test_experiment_positive_path_computes_meta_and_request_hash(monkeypatch):
    from types import SimpleNamespace
    from dataclasses import dataclass
    from unittest.mock import patch
    from app.services.novelty import experiment as exp
    from app.services.novelty.experiment import NoveltyExperiment, digest
    from app.schemas.factor_novelty import NoveltyRequest
    import pandas as pd

    index = pd.date_range("2022-01-01", periods=800, freq="D")
    candidate_dates = pd.date_range("2024-01-01", periods=10, freq="D")
    values = pd.DataFrame(
        {"a": range(10), "b": range(10, 20), "c": range(20, 30)}, index=candidate_dates
    )

    class FakeEngine:
        release_id = "release"
        provider = SimpleNamespace(current_release_signature=lambda: "release")

        def get_task_panels(self, start, end, symbols=None):
            return {"close": pd.DataFrame(1.0, index=index, columns=["a", "b", "c"])}

        def get_index(self, *a, **k):
            return None

    class FakeFactorEngine:
        def __init__(self, spec=None):
            self.eligibility_mask = None

        def compute(self):
            return values

        def compute_from_spec(self, spec, panel):
            return values + 1

        def preprocess(self, raw, eligibility_mask=None, **kwargs):
            return raw

        def result_diagnostics(self):
            return {}

    b = body()
    b["candidate"].update(start="2024-01-01", end="2024-01-10")
    b["expected_candidate_spec_hash"] = "spec"
    b["reference_warmup_sessions"] = 252
    b["min_pairs"] = 3
    req = NoveltyRequest.model_validate(b)

    @dataclass
    class Spec:
        factor_source_hash: str = "hash"
        start: str = "2024-01-01"
        end: str = "2024-01-10"
        factor_name: str = "f"
        factor_code: str | None = None
        library_names: list | None = None
        winsorize: int = 0
        standardize: int = 0

        def identity(self, data_release_id):
            return "spec"

    spec = Spec()
    with (
        patch.object(exp.DataEngine, "get_instance", return_value=FakeEngine()),
        patch.object(exp, "_request_spec", return_value=spec),
        patch.object(exp, "FactorEngine", FakeFactorEngine),
    ):
        out = NoveltyExperiment(req, {"cancelled": False}).run()
    assert out["assessment_status"] == "complete"
    assert out["comparisons"][0]["status"] == "succeeded"
    assert out["meta"]["request_hash"] == digest(req.model_dump(mode="json"))
    assert out["meta"]["reference_warmup_sessions_requested"] == 252
