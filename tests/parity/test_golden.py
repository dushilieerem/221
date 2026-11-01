"""端到端对账用例：缺金标或缺行情时跳过，不阻塞常规测试套件。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.services.parity.compare import compare_runs
from app.services.parity.loader import golden_available, load_case, load_golden
from app.services.parity.normalizer import from_response
from app.services.parity.runner import build_data_engine, run_case

REPO = Path(__file__).resolve().parents[2]
CASES_DIR = Path(__file__).parent / "cases"
CONFIG_PATH = REPO / "config.yaml"


def _case_files() -> list[Path]:
    return sorted(CASES_DIR.glob("*/case.yaml"))


@pytest.mark.parametrize(
    "case_file", _case_files(), ids=lambda path: path.parent.name
)
def test_golden_case(case_file: Path):
    case = load_case(case_file.parent)
    if not golden_available(case):
        pytest.skip(f"{case.case_id}: 聚宽金标未导出（expected/ 不存在）")

    data_dir = os.environ.get("RESEARCHENGINE_DATA_DIR")
    if not data_dir and not CONFIG_PATH.is_file():
        pytest.skip(f"{case.case_id}: 未提供 RESEARCHENGINE_DATA_DIR 或 config.yaml")

    engine, _ = build_data_engine(data_dir=data_dir, config_path=CONFIG_PATH)
    response = run_case(case, engine)
    canonical = from_response(response.model_dump(mode="json"))
    result = compare_runs(load_golden(case), canonical, case)

    acceptance = str(case.comparison.get("acceptance", "strict")).lower()
    if acceptance == "diagnostic":
        # 诊断用例只要求比对可执行并产出分层结果；具体差异由 CLI 报告呈现。
        assert result.layers
        assert any(layer.checks for layer in result.layers)
        return

    assert result.status == "pass", (
        result.first_divergence.as_dict() if result.first_divergence else "unknown"
    )


@pytest.mark.parametrize(
    "case_file", _case_files(), ids=lambda path: path.parent.name
)
def test_svquant_strategy_source_is_loadable(case_file: Path):
    """不依赖行情：确认策略源码可编译且只有一个带 initialize 的类。"""

    case = load_case(case_file.parent)
    source = case.strategy_path("svquant", "svquant_strategy.py").read_text(encoding="utf-8")
    namespace = {"__name__": "user_strategy"}
    exec(compile(source, "<strategy>", "exec"), namespace)
    classes = [
        value
        for value in namespace.values()
        if isinstance(value, type)
        and value.__module__ == "user_strategy"
        and hasattr(value, "initialize")
    ]
    assert len(classes) == 1
