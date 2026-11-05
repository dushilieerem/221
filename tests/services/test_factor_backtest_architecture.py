"""统一因子入口与 ADR-029 services/models 分层约束。"""

from __future__ import annotations

import ast
from pathlib import Path
import subprocess
import sys

import pandas as pd

from app.services.results.manager import ResultManager
from app.schemas.backtest import FactorBacktestResponse
from app.models.factor_backtest import FactorBacktestResult
from app.services.factor.runner import _factor_execution_from_standard_report, _factor_standard_report
from app.models.factor_backtest import FactorBacktestSpec

ROOT = Path(__file__).resolve().parents[2]
FACTOR_ROUTE = ROOT / "app" / "api" / "routes" / "factor.py"


def test_factor_resolution_belongs_to_factor_engine():
    backtest_source = (
        ROOT / "app" / "services" / "backtest" / "engine.py"
    ).read_text(encoding="utf-8")
    factor_source = (
        ROOT / "app" / "services" / "factor" / "engine.py"
    ).read_text(encoding="utf-8")

    assert "def _resolve_factor" not in backtest_source
    assert "factor_engine = FactorEngine(spec)" in backtest_source
    assert "factor_engine.compute()" in backtest_source
    assert "factor_engine.panel" not in backtest_source
    assert "factor_engine.filter_info" not in backtest_source
    assert "def market_field" in factor_source
    assert "def result_diagnostics" in factor_source
    assert "effective_spec" not in backtest_source
    assert "_config_from_spec" not in backtest_source
    assert "ConfigManager" not in backtest_source
    assert "factor_engine._resolve_factor" not in backtest_source
    assert "def _resolve_factor" in factor_source
    assert "self._factor" in factor_source
    assert "factor_resolver" not in backtest_source


def test_factor_route_does_not_import_quantcore_directly():
    source = FACTOR_ROUTE.read_text(encoding="utf-8")
    assert "app.quantcore" not in source
    for engine in ("DataEngine", "FactorEngine", "PortfolioEngine", "MetricEngine"):
        assert engine not in source


def _runtime_imports(node):
    if isinstance(node, ast.If) and isinstance(node.test, ast.Name) and node.test.id == "TYPE_CHECKING":
        for child in node.orelse:
            yield from _runtime_imports(child)
        return
    if isinstance(node, ast.ImportFrom):
        yield node.module or ""
    elif isinstance(node, ast.Import):
        yield from (alias.name for alias in node.names)
    for child in ast.iter_child_nodes(node):
        yield from _runtime_imports(child)


def test_models_and_database_do_not_import_upstream_at_runtime():
    app_root = ROOT / "app"
    for directory, forbidden in (
        ("models", ("app.api", "app.schemas", "app.services", "app.database", "app.config", "fastapi")),
        ("database", ("app.api", "app.schemas", "app.services", "fastapi")),
    ):
        for path in (app_root / directory).rglob("*.py"):
            imports = _runtime_imports(ast.parse(path.read_text(encoding="utf-8")))
            invalid = [name for name in imports if name.startswith(forbidden)]
            assert not invalid, (path, invalid)


def test_models_can_load_without_loading_services_or_http_framework():
    code = """
import importlib
import pkgutil
import sys
import app.models
for module in pkgutil.walk_packages(app.models.__path__, 'app.models.'):
    importlib.import_module(module.name)
assert not any(name == 'app.services' or name.startswith('app.services.') for name in sys.modules)
assert 'fastapi' not in sys.modules
assert 'app.config' not in sys.modules
"""
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=ROOT)
    assert result.returncode == 0, result.stderr


def test_removed_runtime_packages_are_not_reintroduced():
    app_root = ROOT / "app"
    for name in ("quantcore", "core", "repositories", "domain", "application", "infrastructure"):
        assert not list((app_root / name).rglob("*.py"))
    for path in app_root.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        for removed in ("app.quantcore", "app.core", "app.repositories"):
            assert removed not in source, (path, removed)


def test_factor_backtest_executor_is_removed():
    app_root = ROOT / "app"
    assert not (app_root / "services" / "factor" / "executor.py").exists()
    assert (app_root / "services" / "factor" / "experiment.py").exists()
    for path in app_root.rglob("*.py"):
        assert "FactorBacktestExecutor" not in path.read_text(encoding="utf-8"), path


def test_engines_and_internal_models_have_distinct_owners():
    from app.models.data import Panel
    from app.models.metric import MetricContext
    from app.services.backtest.engine import BacktestEngine
    from app.services.data.engine import DataEngine
    from app.services.factor.engine import FactorEngine
    from app.services.metric.engine import MetricEngine
    from app.services.portfolio.engine import PortfolioEngine

    for engine in (BacktestEngine, DataEngine, FactorEngine, MetricEngine, PortfolioEngine):
        assert engine.__module__.startswith("app.services.")
    for model in (Panel, MetricContext, FactorBacktestSpec, FactorBacktestResult):
        assert model.__module__.startswith("app.models.")


def test_default_asset_and_data_paths_survive_package_moves(monkeypatch):
    from app.services.data.engine import default_market_data_dir
    from app.utils.paths import (
        factor_database_path,
        factor_tool_path,
        strategy_library_dir,
    )

    for variable in ("SVQUANT_DATA_DIR", "SVQUANT_FACTOR_DIR", "SVQUANT_STRATEGY_DIR"):
        monkeypatch.delenv(variable, raising=False)
    # 默认行情目录由运行配置决定；测试不依赖本机 config.yaml。
    from types import SimpleNamespace

    monkeypatch.setattr(
        "app.config.get_runtime_settings",
        lambda: SimpleNamespace(data_dir=ROOT.parent / "marketdata"),
    )
    assert default_market_data_dir() == ROOT.parent / "marketdata"
    assert factor_database_path() == ROOT / "app/assets/factors/factor_library.db"
    assert factor_tool_path() == ROOT / "app/assets/factors/tool.py"
    assert strategy_library_dir() == ROOT / "app/assets/strategies/libraries"


def test_standard_report_cache_payload_serves_http_and_restore():
    dates = pd.date_range("2026-01-02", periods=2, freq="B")
    equity = pd.Series([1.0, 1.1], index=dates)
    benchmark = pd.Series([1.0, 1.05], index=dates)
    positions = pd.DataFrame([[0.5, 0.5], [0.6, 0.4]], index=dates, columns=["A", "B"])
    layered = pd.DataFrame([[1.0, 1.0], [1.1, 1.05]], index=dates, columns=["Q1", "Q2"])
    result_manager = ResultManager(
        metrics={"total_return": 0.1},
        factor_values=layered.copy(),
        equity_curve=equity,
        returns=equity.pct_change(),
        positions=positions,
        meta={"execution_version": "factor-execution-v2"},
    )
    result_manager.factor_analysis = {"layered_equity": layered}
    spec = FactorBacktestSpec(
        factor_name="factor",
        factor_source_hash="source",
        library_names=("library",),
        universe="CSI_ALL",
        start="2026-01-02",
        end="2026-01-05",
    )
    execution = FactorBacktestResult(
        spec=spec,
        result_manager=result_manager,
        benchmark_equity=benchmark,
        quantile_metrics=(("Q1", 0.1),),
        predictive_metrics={"ic_mean": 0.2},
        data_release_id="release-1",
        actual_end="2026-01-05",
    )

    payload = _factor_standard_report(execution, FactorBacktestResponse())
    restored = _factor_execution_from_standard_report(spec, payload)

    # 缓存载荷写的是「命中路径真正读到的字段」：HTTP 入口读 http_response，
    # 标准报告恢复路径读 equity_curve / quantile_metrics / predictive_metrics
    # / meta。factor_values 与 positions 是日期×标的的完整宽表，没有消费者，
    # 且逐格编码再 gzip 会在长历史上比回测本身更慢，因此不得再写进去。
    standard = payload["standard"]
    assert "factor_values" not in standard
    assert "positions" not in standard
    assert payload["http_response"] is not None

    assert restored is not None
    pd.testing.assert_series_equal(restored.equity_curve, equity, check_freq=False)
    pd.testing.assert_series_equal(restored.benchmark_equity, benchmark, check_freq=False)
    # 旧缓存文件仍带着这两张表，读取端保持兼容；新缓存不再写入。
    assert restored.positions is None
    assert restored.result_manager.factor_values is None
    pd.testing.assert_frame_equal(
        restored.factor_analysis["layered_equity"],
        layered,
        check_freq=False,
    )
    assert restored.predictive_metrics == {"ic_mean": 0.2}
