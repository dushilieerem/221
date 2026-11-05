"""在进程内运行 SVQuant 侧对账用例。

走 ``app.services.strategy.backtest.run_strategy_backtest``（HTTP 路由调用的
同一服务函数），不要求 FastAPI 服务在线。这是开发期对账工具，因此与
``factor_backtest_batch`` 的"只调 HTTP"约定不同。
"""

from __future__ import annotations

from pathlib import Path

from app.config import DEFAULT_CONFIG_PATH, load_runtime_settings
from app.schemas.backtest import StrategyBacktestRequest
from app.services.data.engine import DataEngine
from app.services.data.manifest import validate_data_manifest
from app.services.parity.loader import ParityError
from app.services.parity.schema import CaseSpec


def build_request(case: CaseSpec) -> StrategyBacktestRequest:
    """按用例声明构造事件回测请求。"""

    strategy_path = case.strategy_path("svquant", "svquant_strategy.py")
    if not strategy_path.is_file():
        raise ParityError(f"SVQuant 策略源码不存在: {strategy_path}")
    config = case.svquant
    start = case.jq.get("start")
    end = case.jq.get("end")
    if not start or not end:
        raise ParityError("case.yaml 必须提供 jq.start 与 jq.end 作为对账区间")

    try:
        return StrategyBacktestRequest(
            engine_mode="event",
            strategy_code=strategy_path.read_text(encoding="utf-8"),
            universe=str(config.get("universe", "CSI300")),
            benchmark=str(config.get("benchmark", "CSI300")),
            start=str(start),
            end=str(end),
            initial_capital=float(config.get("initial_capital", 1_000_000.0)),
            commission_rate=float(config.get("commission_rate", 0.0)),
            stamp_tax=float(config.get("stamp_tax", 0.0)),
            slippage_rate=float(config.get("slippage_rate", 0.0)),
            min_commission=float(config.get("min_commission", 0.0)),
            execution_timing=str(config.get("execution_timing", "next_open")),
            execution_price_source=str(config.get("execution_price_source", "adjusted")),
            strategy_dialect=str(config.get("strategy_dialect", "native")),
            timed_execution=bool(config.get("timed_execution", False)),
            extra_symbols=list(config.get("extra_symbols", []) or []),
        )
    except ValueError as exc:
        raise ParityError(f"回测请求参数非法: {exc}") from exc


def run_case(case: CaseSpec, data_engine: DataEngine):
    """运行 SVQuant 侧，返回 ``StrategyBacktestResponse``。"""

    from app.services.strategy.backtest import run_strategy_backtest

    request = build_request(case)
    response = run_strategy_backtest(request, data_engine=data_engine)
    if response.error:
        raise ParityError(f"SVQuant 回测失败: {response.error}")
    return response


def build_data_engine(
    *,
    data_dir: str | Path | None = None,
    config_path: str | Path = DEFAULT_CONFIG_PATH,
) -> tuple[DataEngine, str | None]:
    """构造只读 DataEngine。

    显式传入 ``data_dir`` 时跳过 manifest 校验（测试与临时目录用）；否则按
    ``config.yaml`` 的 release 校验流程装载。
    """

    directory = Path(data_dir) if data_dir is not None else None
    if directory is None:
        settings = load_runtime_settings(config_path)
        manifest = validate_data_manifest(
            settings.data_dir,
            manifest_path=settings.data_manifest,
            expected_data_version=settings.expected_data_version,
            expected_manifest_sha256=settings.expected_manifest_sha256,
            verify_hashes=settings.verify_data_hashes,
        )
        directory = Path(manifest.data_dir)

    engine = DataEngine()
    engine.load_parquet(directory)
    return engine, getattr(engine, "release_id", None)
