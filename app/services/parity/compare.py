"""聚宽与 SVQuant 结果的分层比对。

层级：L0 参数/日历、L1 目标组合、L2 成交、L3 持仓、L4 净值/收益、L5 指标。

关键口径：聚宽金标可能使用前复权价、SVQuant 使用后复权价，绝对价格、股数、
现金不可直接比较。``case.comparison.price_basis`` 决定哪些数值差异只作参考；
但"成交事件集合""每日持仓集合""收益率"始终严格比较。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from app.services.parity.loader import GoldenRun
from app.services.parity.normalizer import CanonicalRun
from app.services.parity.schema import CaseSpec, Tolerances

LAYER_ORDER = ("L0", "L1", "L2", "L3", "L4", "L5")

#: 聚宽指数代码与本仓库基准名的对应关系。
BENCHMARK_ALIASES = {
    "000300.XSHG": "CSI300",
    "000905.XSHG": "CSI500",
    "000001.XSHG": "CSI_ALL",
}

_PRICE_BASIS_REAL = "real"


@dataclass
class Divergence:
    layer: str
    field: str
    expected: Any
    actual: Any
    date: str | None = None
    symbol: str | None = None
    diff: float | None = None
    tolerance: float | None = None
    strict: bool = True
    note: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "layer": self.layer,
            "date": self.date,
            "symbol": self.symbol,
            "field": self.field,
            "expected": self.expected,
            "actual": self.actual,
            "diff": self.diff,
            "tolerance": self.tolerance,
            "strict": self.strict,
            "note": self.note,
        }


@dataclass
class LayerResult:
    layer: str
    strict: bool
    checks: int = 0
    divergences: list[Divergence] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def strict_failures(self) -> int:
        return sum(1 for item in self.divergences if item.strict)

    @property
    def reference_failures(self) -> int:
        return sum(1 for item in self.divergences if not item.strict)

    @property
    def passed(self) -> bool:
        return self.strict_failures == 0


@dataclass
class ComparisonResult:
    case_id: str
    layers: list[LayerResult]
    warnings: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        return "pass" if all(layer.passed for layer in self.layers) else "diverged"

    @property
    def first_divergence(self) -> Divergence | None:
        strict = [
            item
            for layer in self.layers
            for item in layer.divergences
            if item.strict
        ]
        if not strict:
            return None
        strict.sort(key=lambda item: (LAYER_ORDER.index(item.layer), item.date or "", item.symbol or "", item.field))
        return strict[0]

    @property
    def all_divergences(self) -> list[Divergence]:
        return [item for layer in self.layers for item in layer.divergences]


def compare_runs(jq: GoldenRun, svq: CanonicalRun, case: CaseSpec) -> ComparisonResult:
    """执行 L0–L5 比对。"""

    tolerances = case.tolerances
    layers = [
        _compare_parameters(jq, svq, case, tolerances),
        _compare_targets(jq, svq, case),
        _compare_fills(jq, svq, case, tolerances),
        _compare_positions(jq, svq, case, tolerances),
        _compare_equity(jq, svq, case, tolerances),
        _compare_metrics(jq, svq, case, tolerances),
    ]
    warnings: list[str] = []
    if case.price_basis != _PRICE_BASIS_REAL:
        warnings.append(
            f"price_basis={case.price_basis}：成交价、股数、现金只作参考，"
            "严格层为参数/日历、标的集合、成交事件、持仓集合、收益率与指标"
        )
    if not svq.meta.get("metrics"):
        warnings.append("SVQuant 响应缺少 metrics；L5 由权益曲线独立重算")
    return ComparisonResult(case_id=case.case_id, layers=layers, warnings=warnings)


# ---------------------------------------------------------------- L0


def _compare_parameters(
    jq: GoldenRun, svq: CanonicalRun, case: CaseSpec, tolerances: Tolerances
) -> LayerResult:
    layer = LayerResult(layer="L0", strict="L0" in case.strict_layers)

    pairs = _declared_parameter_pairs(case)
    for name, jq_value, svq_value, tolerance in pairs:
        layer.checks += 1
        if isinstance(jq_value, (int, float)) and isinstance(svq_value, (int, float)):
            diff = abs(float(jq_value) - float(svq_value))
            if diff > tolerance:
                layer.divergences.append(Divergence(
                    layer="L0", field=name, expected=jq_value, actual=svq_value,
                    diff=diff, tolerance=tolerance,
                ))
        elif str(jq_value) != str(svq_value):
            layer.divergences.append(Divergence(
                layer="L0", field=name, expected=jq_value, actual=svq_value,
            ))

    # 金标 meta 里的初始资金必须与声明一致（防止导出错回测）。
    declared_cash = case.jq.get("initial_cash")
    meta_cash = jq.meta.get("initial_cash")
    if meta_cash is None:
        meta_cash = (jq.meta.get("params") or {}).get("initial_cash")
    if meta_cash is not None and declared_cash is not None:
        layer.checks += 1
        if abs(float(meta_cash) - float(declared_cash)) > 0.01:
            layer.divergences.append(Divergence(
                layer="L0", field="meta.initial_cash",
                expected=declared_cash, actual=meta_cash,
                diff=abs(float(meta_cash) - float(declared_cash)), tolerance=0.01,
            ))

    # 交易日历：两侧必须一致，否则后续层级无法按日期对齐。
    jq_dates = _dates(jq.daily["date"])
    svq_dates = _dates(svq.daily["date"])
    layer.checks += 1
    missing_in_svq = sorted(jq_dates - svq_dates)
    missing_in_jq = sorted(svq_dates - jq_dates)
    if missing_in_svq:
        layer.divergences.append(Divergence(
            layer="L0", field="trading_dates.missing_in_svquant",
            expected=len(jq_dates), actual=len(svq_dates),
            note=f"{len(missing_in_svq)} 个日期缺失，示例: {missing_in_svq[:8]}",
        ))
    if missing_in_jq:
        layer.divergences.append(Divergence(
            layer="L0", field="trading_dates.missing_in_jq",
            expected=len(jq_dates), actual=len(svq_dates),
            note=f"{len(missing_in_jq)} 个日期缺失，示例: {missing_in_jq[:8]}",
        ))
    return layer


def _declared_parameter_pairs(case: CaseSpec) -> list[tuple[str, Any, Any, float]]:
    jq = case.jq
    svq = case.svquant
    options = jq.get("options") or {}
    pairs: list[tuple[str, Any, Any, float]] = []

    def add(name: str, jq_value: Any, svq_value: Any, tolerance: float = 1e-12) -> None:
        if jq_value is not None and svq_value is not None:
            pairs.append((name, jq_value, svq_value, tolerance))

    add("initial_cash", jq.get("initial_cash"), svq.get("initial_capital"), 0.01)
    add("commission", options.get("commission"), svq.get("commission_rate"))
    add("min_commission", options.get("min_commission"), svq.get("min_commission"), 0.01)
    add("stamp_tax", options.get("stamp_tax"), svq.get("stamp_tax"))
    add("slippage", options.get("slippage_rate"), svq.get("slippage_rate"))

    jq_benchmark = jq.get("benchmark")
    svq_benchmark = svq.get("benchmark")
    if jq_benchmark is not None and svq_benchmark is not None:
        mapped = BENCHMARK_ALIASES.get(str(jq_benchmark).upper(), str(jq_benchmark))
        # 数据缺失时用例可显式声明等价基准，避免把参考口径差当成 L0 分歧。
        equivalent = (case.comparison or {}).get("benchmark_equivalent")
        if equivalent is not None:
            mapped = str(equivalent)
        add("benchmark", mapped, svq_benchmark)
    return pairs


def _dates(series: pd.Series) -> set[str]:
    if series is None or len(series) == 0:
        return set()
    return set(pd.to_datetime(series).dt.strftime("%Y-%m-%d"))


# ---------------------------------------------------------------- L1


def _compare_targets(jq: GoldenRun, svq: CanonicalRun, case: CaseSpec) -> LayerResult:
    layer = LayerResult(layer="L1", strict="L1" in case.strict_layers)
    jq_symbols = _traded_symbols(jq.positions, jq.fills)
    svq_symbols = _traded_symbols(svq.positions, svq.fills)
    layer.checks += 1
    for symbol in sorted(jq_symbols - svq_symbols):
        layer.divergences.append(Divergence(
            layer="L1", field="symbol_set", symbol=symbol,
            expected="present", actual="absent",
        ))
    for symbol in sorted(svq_symbols - jq_symbols):
        layer.divergences.append(Divergence(
            layer="L1", field="symbol_set", symbol=symbol,
            expected="absent", actual="present",
        ))

    # 建仓日：两侧同一标的的首次持仓日期应一致。
    jq_first = _first_holding_dates(jq.positions)
    svq_first = _first_holding_dates(svq.positions)
    for symbol in sorted(jq_first.keys() & svq_first.keys()):
        layer.checks += 1
        if jq_first[symbol] != svq_first[symbol]:
            layer.divergences.append(Divergence(
                layer="L1", field="first_holding_date", symbol=symbol,
                expected=jq_first[symbol], actual=svq_first[symbol],
            ))
    return layer


def _traded_symbols(positions: pd.DataFrame, fills: pd.DataFrame) -> set[str]:
    symbols: set[str] = set()
    if positions is not None and not positions.empty:
        symbols |= set(positions["symbol"].dropna().astype(str))
    if fills is not None and not fills.empty:
        symbols |= set(fills["symbol"].dropna().astype(str))
    return symbols


def _first_holding_dates(positions: pd.DataFrame) -> dict[str, str]:
    if positions is None or positions.empty:
        return {}
    frame = positions.copy()
    frame["date_text"] = pd.to_datetime(frame["date"]).dt.strftime("%Y-%m-%d")
    grouped = frame.groupby("symbol")["date_text"].min()
    return {str(symbol): str(value) for symbol, value in grouped.items()}


# ---------------------------------------------------------------- L2


def _compare_fills(
    jq: GoldenRun, svq: CanonicalRun, case: CaseSpec, tolerances: Tolerances
) -> LayerResult:
    layer = LayerResult(layer="L2", strict="L2" in case.strict_layers)
    numbers_strict = case.price_basis == _PRICE_BASIS_REAL

    jq_agg = _aggregate_fills(jq.fills)
    svq_agg = _aggregate_fills(svq.fills)
    keys = ["date", "symbol", "side"]
    layer.checks += 1

    if jq_agg.empty and svq_agg.empty:
        return layer

    merged = jq_agg.merge(
        svq_agg, on=keys, how="outer", suffixes=("_jq", "_svq"), indicator=True
    )
    for _, row in merged.iterrows():
        date = _date_text(row["date"])
        symbol = str(row["symbol"])
        side = str(row["side"])
        if row["_merge"] != "both":
            present_side = "jq" if row["_merge"] == "left_only" else "svquant"
            layer.divergences.append(Divergence(
                layer="L2", field="fill_event", date=date, symbol=symbol,
                expected="present" if present_side == "jq" else "absent",
                actual="absent" if present_side == "jq" else "present",
                note=f"side={side}",
            ))
            continue

        _compare_number(
            layer, "fill.quantity", date, symbol, row["quantity_jq"], row["quantity_svq"],
            tolerance=tolerances.quantity, strict=numbers_strict,
        )
        _compare_number(
            layer, "fill.price", date, symbol, row["vwap_jq"], row["vwap_svq"],
            tolerance=tolerances.price_abs, strict=numbers_strict,
        )
        _compare_number(
            layer, "fill.fees", date, symbol, row["fees_jq"], row["fees_svq"],
            tolerance=tolerances.cash_abs, strict=numbers_strict,
        )
    return layer


def _aggregate_fills(frame: pd.DataFrame) -> pd.DataFrame:
    columns = ["date", "symbol", "side", "quantity", "vwap", "fees"]
    if frame is None or frame.empty:
        return pd.DataFrame(columns=columns)
    work = frame.copy()
    work["notional"] = work["quantity"].astype(float) * pd.to_numeric(work["price"], errors="coerce")
    grouped = work.groupby(["date", "symbol", "side"], as_index=False).agg(
        quantity=("quantity", "sum"),
        notional=("notional", "sum"),
        commission=("commission", "sum"),
        stamp_tax=("stamp_tax", "sum"),
    )
    quantity = grouped["quantity"].astype(float).replace(0.0, float("nan"))
    grouped["vwap"] = grouped["notional"] / quantity
    grouped["fees"] = grouped["commission"] + grouped["stamp_tax"]
    return grouped[columns]


def _compare_number(
    layer: LayerResult,
    field_name: str,
    date: str | None,
    symbol: str | None,
    expected: Any,
    actual: Any,
    *,
    tolerance: float,
    strict: bool,
) -> None:
    layer.checks += 1
    if pd.isna(expected) and pd.isna(actual):
        return
    if pd.isna(expected) or pd.isna(actual):
        layer.divergences.append(Divergence(
            layer=layer.layer, field=field_name, date=date, symbol=symbol,
            expected=None if pd.isna(expected) else float(expected),
            actual=None if pd.isna(actual) else float(actual),
            strict=strict, note="一侧为空",
        ))
        return
    diff = abs(float(expected) - float(actual))
    if diff > tolerance:
        layer.divergences.append(Divergence(
            layer=layer.layer, field=field_name, date=date, symbol=symbol,
            expected=float(expected), actual=float(actual),
            diff=diff, tolerance=tolerance, strict=strict,
            note=None if strict else "价格基准不同，仅参考",
        ))


# ---------------------------------------------------------------- L3


def _compare_positions(
    jq: GoldenRun, svq: CanonicalRun, case: CaseSpec, tolerances: Tolerances
) -> LayerResult:
    layer = LayerResult(layer="L3", strict="L3" in case.strict_layers)
    numbers_strict = case.price_basis == _PRICE_BASIS_REAL

    jq_pos = _positions_by_date(jq.positions)
    svq_pos = _positions_by_date(svq.positions)
    common_dates = sorted(set(jq_pos) & set(svq_pos))

    # 持仓集合始终严格：即使复权基准不同，持有与清仓的日期应当一致。
    mask_limit = 50
    for date in common_dates:
        jq_symbols = set(jq_pos[date].keys())
        svq_symbols = set(svq_pos[date].keys())
        for symbol in sorted(jq_symbols ^ svq_symbols):
            if layer.strict_failures >= mask_limit:
                layer.notes.append(f"持仓集合差异超过 {mask_limit} 条，已截断")
                break
            layer.checks += 1
            layer.divergences.append(Divergence(
                layer="L3", field="holding", date=date, symbol=symbol,
                expected="held" if symbol in jq_symbols else "absent",
                actual="held" if symbol in svq_symbols else "absent",
            ))

    if not numbers_strict:
        layer.notes.append("price_basis != real：持仓数量与现金差异只作参考")

    for date in common_dates:
        for symbol in sorted(set(jq_pos[date]) | set(svq_pos[date])):
            _compare_number(
                layer, "position.quantity", date, symbol,
                jq_pos[date].get(symbol, 0), svq_pos[date].get(symbol, 0),
                tolerance=tolerances.quantity, strict=numbers_strict,
            )

    jq_cash = _cash_by_date(jq.daily)
    svq_cash = _cash_by_date(svq.daily)
    for date in sorted(set(jq_cash) & set(svq_cash)):
        _compare_number(
            layer, "cash", date, None, jq_cash[date], svq_cash[date],
            tolerance=tolerances.cash_abs, strict=numbers_strict,
        )
    return layer


def _positions_by_date(frame: pd.DataFrame) -> dict[str, dict[str, int]]:
    result: dict[str, dict[str, int]] = {}
    if frame is None or frame.empty:
        return result
    work = frame.copy()
    work["date_text"] = pd.to_datetime(work["date"]).dt.strftime("%Y-%m-%d")
    grouped = work.groupby(["date_text", "symbol"])["quantity"].sum()
    for (date, symbol), quantity in grouped.items():
        if int(quantity) == 0:
            continue
        result.setdefault(str(date), {})[str(symbol)] = int(quantity)
    return result


def _cash_by_date(daily: pd.DataFrame) -> dict[str, float]:
    if daily is None or daily.empty or "cash" not in daily:
        return {}
    frame = daily.dropna(subset=["cash"]).copy()
    frame["date_text"] = pd.to_datetime(frame["date"]).dt.strftime("%Y-%m-%d")
    return {
        str(row.date_text): float(row.cash)
        for row in frame.itertuples(index=False)
    }


# ---------------------------------------------------------------- L4


def _compare_equity(
    jq: GoldenRun, svq: CanonicalRun, case: CaseSpec, tolerances: Tolerances
) -> LayerResult:
    layer = LayerResult(layer="L4", strict="L4" in case.strict_layers)
    jq_series = _equity_series(jq.daily)
    svq_series = _equity_series(svq.daily)
    if jq_series.empty or svq_series.empty:
        layer.notes.append("一侧权益曲线为空，跳过 L4")
        return layer

    common = jq_series.index.intersection(svq_series.index)
    layer.checks += 1
    if len(common) < 2:
        layer.divergences.append(Divergence(
            layer="L4", field="equity.common_dates", expected=len(jq_series), actual=len(common),
        ))
        return layer

    jq_returns = jq_series.loc[common].pct_change().dropna()
    svq_returns = svq_series.loc[common].pct_change().dropna()
    aligned = pd.concat({"jq": jq_returns, "svq": svq_returns}, axis=1).dropna()
    for date, row in aligned.iterrows():
        _compare_number(
            layer, "daily_return", date.strftime("%Y-%m-%d"), None,
            row["jq"], row["svq"], tolerance=tolerances.return_abs, strict=True,
        )

    jq_cumulative = float(jq_series.loc[common[-1]] / jq_series.loc[common[0]] - 1.0)
    svq_cumulative = float(svq_series.loc[common[-1]] / svq_series.loc[common[0]] - 1.0)
    _compare_number(
        layer, "cumulative_return", common[-1].strftime("%Y-%m-%d"), None,
        jq_cumulative, svq_cumulative, tolerance=tolerances.return_abs, strict=True,
    )

    # 诊断：账户收益差异里有多少来自现金拖累。
    jq_invested = _invested_series(jq.daily)
    svq_invested = _invested_series(svq.daily)
    if not jq_invested.empty and not svq_invested.empty:
        jq_returns_invested = jq_invested.pct_change().dropna()
        svq_returns_invested = svq_invested.pct_change().dropna()
        aligned_invested = pd.concat(
            {"jq": jq_returns_invested, "svq": svq_returns_invested}, axis=1
        ).dropna()
        if not aligned_invested.empty:
            diff = (aligned_invested["jq"] - aligned_invested["svq"]).abs()
            layer.notes.append(
                "剔除现金后日收益最大差 "
                f"{float(diff.max()):.6f}（均差 {float(diff.mean()):.6f}）；"
                f"末日现金占比 聚宽 {_cash_fraction(jq.daily):.2%} / "
                f"SVQuant {_cash_fraction(svq.daily):.2%}"
            )
    return layer


def _invested_series(daily: pd.DataFrame) -> pd.Series:
    """持仓市值序列（权益 - 现金），用于剥离现金拖累。"""

    if daily is None or daily.empty or "cash" not in daily:
        return pd.Series(dtype=float)
    frame = daily.dropna(subset=["equity", "cash"]).copy()
    if frame.empty:
        return pd.Series(dtype=float)
    frame["date"] = pd.to_datetime(frame["date"])
    frame = frame.set_index("date").sort_index()
    invested = frame["equity"].astype(float) - frame["cash"].astype(float)
    return invested[invested > 0]


def _cash_fraction(daily: pd.DataFrame) -> float:
    """最后一个交易日的现金占总权益比例。"""

    frame = daily.dropna(subset=["equity", "cash"])
    if frame.empty:
        return float("nan")
    last = frame.sort_values("date").iloc[-1]
    equity = float(last["equity"])
    return float(last["cash"]) / equity if equity else float("nan")


def _equity_series(daily: pd.DataFrame) -> pd.Series:
    if daily is None or daily.empty:
        return pd.Series(dtype=float)
    frame = daily.dropna(subset=["equity"]).copy()
    frame["date"] = pd.to_datetime(frame["date"])
    return frame.set_index("date")["equity"].astype(float).sort_index()


# ---------------------------------------------------------------- L5


def _compare_metrics(
    jq: GoldenRun, svq: CanonicalRun, case: CaseSpec, tolerances: Tolerances
) -> LayerResult:
    layer = LayerResult(layer="L5", strict="L5" in case.strict_layers)
    try:
        jq_metrics = _compute_metrics(_equity_series(jq.daily))
        svq_metrics = _compute_metrics(_equity_series(svq.daily))
    except Exception as exc:  # noqa: BLE001 - 指标失败不应吞掉其他层
        layer.notes.append(f"指标重算失败，跳过 L5: {type(exc).__name__}: {exc}")
        return layer

    for name in ("total_return", "annual_return", "max_drawdown", "volatility", "sharpe"):
        expected = jq_metrics.get(name)
        actual = svq_metrics.get(name)
        layer.checks += 1
        if expected is None or actual is None:
            layer.notes.append(f"{name}: 一侧无值，跳过")
            continue
        diff = abs(float(expected) - float(actual))
        scale = max(abs(float(expected)), abs(float(actual)), 1e-12)
        abs_tolerance = (
            tolerances.sharpe_abs if name == "sharpe" else tolerances.metric_abs
        )
        # 比率型指标同时要求：绝对差超过下限，且相对差超过 metric_rel。
        # 只用一个阈值会把 0.0003 的回撤差异或 0.005 的 Sharpe 差异误判为分歧。
        if diff > abs_tolerance and diff / scale > tolerances.metric_rel:
            layer.divergences.append(Divergence(
                layer="L5", field=name, expected=float(expected), actual=float(actual),
                diff=diff, tolerance=abs_tolerance,
            ))
    return layer


def _compute_metrics(equity: pd.Series) -> dict[str, float | None]:
    if equity is None or len(equity) < 2:
        return {}
    from app.models.metric import MetricContext
    from app.services.metric.engine import MetricEngine

    returns = equity.pct_change().dropna()
    result = MetricEngine().calculate_all(MetricContext(
        equity_curve=equity,
        returns=returns,
        periods_per_year=252,
    ))
    result = dict(result)
    result["total_return"] = float(equity.iloc[-1] / equity.iloc[0] - 1.0)
    normalized: dict[str, float | None] = {}
    for name in ("total_return", "annual_return", "max_drawdown", "volatility", "sharpe"):
        value = result.get(name)
        normalized[name] = None if value is None or pd.isna(value) else float(value)
    return normalized


def _date_text(value: Any) -> str | None:
    if value is None or pd.isna(value):
        return None
    return pd.Timestamp(value).strftime("%Y-%m-%d")
