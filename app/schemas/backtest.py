"""API 请求/响应模型（pydantic v2）。

定义 UI <-> app 之间的数据契约。所有 pandas/numpy 结构在
:mod:`app.services.factor.serializers` 里转成纯 Python 后再塞进这些模型。
"""

from __future__ import annotations

import math
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# ---------------- 请求 ----------------

class UniverseSnapshotInput(BaseModel):
    """由 WebApp 鉴权、解析后传入的静态证券池快照。"""

    model_config = ConfigDict(extra="forbid")
    asset_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    content_hash: str = Field(min_length=1)
    symbols: list[str] = Field(min_length=1)
    membership_mode: Literal["static"] = "static"

    @model_validator(mode="after")
    def validate_symbols(self):
        if not all(symbol.strip() for symbol in self.symbols):
            raise ValueError("证券池快照含空证券代码")
        if len(self.symbols) != len(set(self.symbols)):
            raise ValueError("证券池快照的证券代码不可重复")
        return self


class BenchmarkSnapshotInput(BaseModel):
    """由 WebApp 鉴权、解析后传入的固定权重组合基准快照。"""

    model_config = ConfigDict(extra="forbid")
    asset_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    content_hash: str = Field(min_length=1)
    weights: dict[str, float] = Field(min_length=1)
    pricing: Literal["close_to_close"] = "close_to_close"

    @model_validator(mode="after")
    def validate_weights(self):
        if not all(symbol.strip() for symbol in self.weights):
            raise ValueError("组合基准快照含空证券代码")
        if not all(math.isfinite(weight) and weight >= 0 for weight in self.weights.values()):
            raise ValueError("组合基准快照的权重必须为有限非负数")
        if not math.isclose(sum(self.weights.values()), 1.0, abs_tol=1e-8):
            raise ValueError("组合基准快照的权重之和必须为 1")
        return self


class ResolvedAssetMeta(BaseModel):
    id: str
    version: str
    content_hash: str
    constituent_count: int
    membership_mode: Optional[Literal["static"]] = None
    pricing: Optional[Literal["close_to_close"]] = None

class FactorBacktestRequest(BaseModel):
    """``POST /api/backtest/factor`` 请求。"""

    model_config = ConfigDict(extra="forbid")

    #: 用户自定义因子的函数源码；未提供时从 SQLite 因子库加载
    factor_code: Optional[str] = Field(None, description="自定义因子 Python 函数源码")
    #: SQLite 因子库名；自定义因子可传一个库以复用其中的方法
    library_name: Optional[str | list[str]] = Field(None, description="SQLite 因子库名")
    #: 要跑的因子标识（优先使用 SQLite 的 sv_id，也兼容名称和源码函数名）
    factor_name: str = Field("alpha101", description="因子名")
    #: 组合构建方式：long_only=纯多头 | long_short=多空组合
    portfolio_type: str = Field("long_only", description="long_only | long_short")
    #: 股票池
    universe: str = Field("CSI300", description="CSI300 | CSI500 | CSI_ALL")
    universe_snapshot: Optional[UniverseSnapshotInput] = None
    #: 自定义回测区间
    start: Optional[str] = None
    end: Optional[str] = None
    #: 是否过滤停牌股
    filter_suspended: bool = False
    #: 是否过滤 ST 股（含 *ST）
    filter_st: bool = False
    #: 是否过滤涨停股
    filter_limit_up: bool = False
    #: 是否过滤跌停股
    filter_limit_down: bool = False
    #: 调仓间隔（交易日）
    rebalance_days: int = Field(1, ge=1, description="调仓间隔，单位为交易日")
    #: 因子分层数
    groups: int = Field(5, ge=2, le=10, description="因子分层数，2-10 组")
    #: 因子去极值方式：0 不处理，1 MAD，2 标准差，3 四分位
    winsorize: int = Field(
        0, ge=0, le=3, description="因子去极值方式：0/1/2/3"
    )
    #: 因子标准化方式：0 不处理，1 原始值，2 rank，3 极差
    standardize: int = Field(
        0, ge=0, le=3, description="因子标准化方式：0/1/2/3"
    )
    #: 对比基准
    benchmark: Literal["CSI300", "CSI500"] = "CSI300"
    benchmark_snapshot: Optional[BenchmarkSnapshotInput] = None
    #: 手续费率（默认 0）
    commission_rate: float = 0.0
    #: 印花税率（默认 0，仅卖出）
    stamp_tax: float = 0.0
    #: 滑点率（默认 0）
    slippage_rate: float = 0.0
    #: 成交时点。信号恒在 T 日收盘产生；same_close = T 日收盘价成交（旧口径，
    #: 最乐观），next_close = T+1 日收盘价成交，next_open = T+1 日开盘价成交。
    execution_timing: Literal["same_close", "next_close", "next_open"] = Field(
        "next_close", description="same_close | next_close | next_open"
    )
    #: core 只计算回测和汇总指标；full 额外计算分层、FF3、行业曲线
    analysis_level: Literal["core", "full"] = Field(
        "core", description="core | full"
    )
    #: 因子 IC 的多持有期（交易日）。默认 [1]；显式传入多个时，响应在
    #: ``analysis.ic_horizons`` 额外返回每个持有期的 IC/RankIC 明细，
    #: 用于 IC 衰减。顶层 ``ic_mean``/``rank_ic_mean``/``ic_series`` 仍是
    #: 1 日口径。
    ic_horizons: list[int] = Field(
        default_factory=lambda: [1],
        description="IC 持有期列表（交易日，1-20，自动去重升序）",
    )

    @field_validator("ic_horizons", mode="before")
    @classmethod
    def validate_ic_horizons(cls, value: object) -> list[int]:
        """去重升序并限制在 1-20 个交易日，拒绝布尔值冒充整数。"""
        if value is None:
            return [1]
        if not isinstance(value, (list, tuple)):
            raise ValueError("ic_horizons 必须是整数列表")
        horizons: list[int] = []
        for item in value:
            if isinstance(item, bool) or not isinstance(item, int):
                raise ValueError("ic_horizons 必须是整数")
            if item < 1 or item > 20:
                raise ValueError("ic_horizons 的取值必须在 1-20 之间")
            horizons.append(item)
        if not horizons:
            raise ValueError("ic_horizons 不能为空")
        return sorted(set(horizons))

    @model_validator(mode="after")
    def validate_factor_source(self):
        """SQLite 内置因子或自定义因子必须有明确来源。"""
        libraries = self.library_names
        if self.factor_code is None and not libraries:
            raise ValueError("未提供 factor_code 时必须提供 library_name")
        if self.factor_code is None and len(libraries) != 1:
            raise ValueError("直接执行内置因子时 library_name 必须是单个库名")
        if len(libraries) != len(set(libraries)):
            raise ValueError("library_name 不可包含重复库名")
        return self

    @property
    def library_names(self) -> list[str]:
        """统一将单个库名和多库列表转换为列表。"""
        if self.library_name is None:
            return []
        return [self.library_name] if isinstance(self.library_name, str) else self.library_name

    @model_validator(mode="after")
    def validate_asset_refs(self):
        if self.universe_snapshot is not None and "universe" in self.model_fields_set:
            raise ValueError("universe 与 universe_snapshot 至多提供一个")
        if self.benchmark_snapshot is not None and "benchmark" in self.model_fields_set:
            raise ValueError("benchmark 与 benchmark_snapshot 至多提供一个")
        return self


class FactorRef(BaseModel):
    """策略回测引用的内置因子：只允许库名+因子名，不接受源码。"""

    model_config = ConfigDict(extra="forbid")

    library: str = Field(min_length=1, max_length=100)
    factor: str = Field(min_length=1, max_length=100)
    direction: Literal["positive", "negative"] = "positive"


class StrategyBacktestRequest(BaseModel):
    """``POST /api/backtest/strategy`` 请求。"""

    model_config = ConfigDict(extra="forbid")

    #: 回测引擎模式；省略时保持既有向量化策略行为。
    engine_mode: Literal["vectorized", "event"] = "vectorized"
    #: 策略方言：native = SVQuant 类契约；joinquant = 模块级聚宽源码，由兼容层执行。
    strategy_dialect: Literal["native", "joinquant"] = "native"
    #: 策略 Python 源码（必须实现 generate_signals 方法）
    strategy_code: Optional[str] = Field(None, description="策略 Python 源码")
    #: 已保存策略的索引 ID；与 ``strategy_code`` 二选一
    strategy_id: Optional[str] = Field(None, description="app/assets/strategies/index.yaml 中的策略 ID")
    #: 已保存策略版本；省略时使用索引声明的 current_version
    strategy_version: Optional[str] = Field(None, description="已保存策略的版本号")
    #: 内置因子引用（1–3 个）。提供时用内置因子合成 @signal 分数，走
    #: FactorPortfolioStrategy 的 selector/rebalance；与 strategy_code/
    #: strategy_id 互斥。只接受库名+因子名，不接受源码。
    factor_refs: Optional[list[FactorRef]] = Field(
        None, min_length=1, max_length=3, description="内置因子引用列表"
    )
    #: 仅 factor_refs 模式生效：等权 TopN 的持仓数（默认 30）。
    top_n: Optional[int] = Field(None, ge=1, le=1000)
    #: 仅 factor_refs 模式生效：调仓间隔交易日（默认 20）。
    rebalance_days: Optional[int] = Field(None, ge=1, le=250)
    universe: str = "CSI500"
    universe_snapshot: Optional[UniverseSnapshotInput] = None
    start: Optional[str] = None
    end: Optional[str] = None
    #: 是否过滤停牌股；事件模式由撮合层常开拒单覆盖，参数本身不额外生效
    filter_suspended: bool = False
    #: 是否过滤 ST 股（含 *ST）；事件模式只拒绝买入
    filter_st: bool = False
    #: 是否过滤涨停股；事件模式由撮合层常开拒单覆盖，参数本身不额外生效
    filter_limit_up: bool = False
    #: 是否过滤跌停股；事件模式由撮合层常开拒单覆盖，参数本身不额外生效
    filter_limit_down: bool = False
    #: 对比基准
    benchmark: Literal["CSI300", "CSI500"] = "CSI300"
    benchmark_snapshot: Optional[BenchmarkSnapshotInput] = None
    #: 佣金率（按成交额计提）；向量化与事件模式均生效
    commission_rate: float = Field(0.0, ge=0)
    #: 印花税率（仅卖出时按成交额计提）；向量化与事件模式均生效
    stamp_tax: float = Field(0.001, ge=0)
    #: 滑点率；向量化模式按换手计提，事件模式调整成交价（买入向上、卖出向下）
    slippage_rate: float = Field(0.0, ge=0)
    #: 每笔最低手续费；仅事件模式生效，显式设 0 即取消最低收费
    min_commission: float = Field(5.0, ge=0)
    #: 成交时点。same_close = T 日收盘价成交（旧口径，事件模式下策略能看到
    #: 当日收盘），next_close = T+1 日收盘价成交，next_open = T+1 日开盘价成交。
    execution_timing: Literal["same_close", "next_close", "next_open"] = Field(
        "next_close", description="same_close | next_close | next_open"
    )
    #: 成交/估值价格来源。adjusted = 复权面板（现状）；unadjusted = 未复权价格
    #: 成交与估值，信号历史仍用复权，持仓按复权比例每日缩放。
    execution_price_source: Literal["adjusted", "unadjusted"] = Field(
        "adjusted", description="adjusted | unadjusted"
    )
    #: 按时点调度：策略可用 schedule.daily(cb, time="14:50") 注册盘中回调；
    #: 成交价取下单时点的盘中价，15:00 之后的订单挂到次日开盘。
    timed_execution: bool = False
    #: 额外并入行情面板的 ETF 代码（如货币 ETF 511880 做现金管理）。
    #: ETF 不在股票池数据里，引擎会从视图的 free/etf_daily 单独加载。
    extra_symbols: list[str] = Field(default_factory=list)
    initial_capital: float = Field(1_000_000.0, gt=0)
    #: 返回持仓权重（前端画持仓图用）
    include_positions: bool = False
    include_rebalance_logs: bool = False
    #: 返回统一执行时间线，供 Web 同时展示向量化调仓与事件策略成交过程。
    include_execution_timeline: bool = False

    @model_validator(mode="after")
    def validate_strategy_source(self):
        if self.factor_refs is not None:
            if self.strategy_code or self.strategy_id:
                raise ValueError(
                    "factor_refs 与 strategy_code/strategy_id 互斥"
                )
            if self.strategy_version is not None:
                raise ValueError("strategy_version 只能与 strategy_id 一起使用")
            keys = [(ref.library, ref.factor) for ref in self.factor_refs]
            if len(keys) != len(set(keys)):
                raise ValueError("factor_refs 中不能出现重复的库+因子")
            if self.engine_mode != "vectorized":
                raise ValueError("factor_refs 仅支持 vectorized 引擎")
            if self.strategy_dialect != "native":
                raise ValueError("factor_refs 仅支持 native 策略方言")
        else:
            if bool(self.strategy_code) == bool(self.strategy_id):
                raise ValueError("strategy_code 与 strategy_id 必须且只能提供一个")
            if self.strategy_version is not None and self.strategy_id is None:
                raise ValueError("strategy_version 只能与 strategy_id 一起使用")
            if self.top_n is not None or self.rebalance_days is not None:
                raise ValueError("top_n/rebalance_days 只能与 factor_refs 一起使用")
        if self.universe_snapshot is not None and "universe" in self.model_fields_set:
            raise ValueError("universe 与 universe_snapshot 至多提供一个")
        if self.benchmark_snapshot is not None and "benchmark" in self.model_fields_set:
            raise ValueError("benchmark 与 benchmark_snapshot 至多提供一个")
        if self.engine_mode == "event" and (self.universe_snapshot is not None or self.benchmark_snapshot is not None):
            raise ValueError("事件驱动策略暂不支持 universe_snapshot 或 benchmark_snapshot")
        if self.engine_mode != "event" and self.execution_price_source != "adjusted":
            raise ValueError("execution_price_source='unadjusted' 仅事件模式支持")
        if self.engine_mode != "event" and self.timed_execution:
            raise ValueError("timed_execution 仅事件模式支持")
        if self.engine_mode != "event" and self.strategy_dialect != "native":
            raise ValueError("strategy_dialect='joinquant' 仅事件模式支持")
        return self


# ---------------- 响应 ----------------


class EquityPoint(BaseModel):
    date: str
    value: float
    drawdown: Optional[float] = None


class DrawdownInfo(BaseModel):
    start: str
    end: Optional[str] = None
    peak: Optional[float] = None
    trough: Optional[float] = None
    recovery: Optional[str] = None
    depth: float
    days: int


class AnnualReturn(BaseModel):
    year: int
    return_value: float


class Holding(BaseModel):
    symbol: str
    name: str
    weight: float


class PositionSnapshot(BaseModel):
    date: str
    holdings: list[Holding]


class RebalanceTrade(BaseModel):
    symbol: str
    name: str
    side: Literal["buy", "sell"]
    previous_weight: float
    target_weight: float
    weight_change: float


class RebalanceLog(BaseModel):
    date: str
    trades: list[RebalanceTrade]


class DateRange(BaseModel):
    start: Optional[str] = None
    end: Optional[str] = None


class LayeredPoint(BaseModel):
    """分层净值点。"""
    date: str
    layer1: Optional[float] = None
    layer2: Optional[float] = None
    layer3: Optional[float] = None
    layer4: Optional[float] = None
    layer5: Optional[float] = None
    layer6: Optional[float] = None
    layer7: Optional[float] = None
    layer8: Optional[float] = None
    layer9: Optional[float] = None
    layer10: Optional[float] = None


class QuantileMetric(BaseModel):
    """单个因子分位或基准的绝对及相对绩效指标。

    分组行的基础指标是分组自身的绝对绩效，``annual_excess_return``
    等字段是相对基准的绩效；``quantile == "benchmark"`` 时只填充
    基准自身的绝对绩效指标。
    """

    quantile: str
    annual_return: Optional[float] = None
    volatility: Optional[float] = None
    sharpe: Optional[float] = None
    #: 该分组的**日均单边换手**（``Σ|Δw| / 2``，逐日计算后取均值、不年化），
    #: 与主组合 ``metrics.turnover`` 同口径。注意分组是等权诊断桶而非可交易
    #: 策略，其换手全部来自排名进出，量级通常显著高于主组合。
    turnover: Optional[float] = None
    max_drawdown: Optional[float] = None
    annual_excess_return: Optional[float] = None
    excess_volatility: Optional[float] = None
    information_ratio: Optional[float] = None
    benchmark_win_rate: Optional[float] = None


class ValuePoint(BaseModel):
    """按日输出的单值序列点。"""
    date: str
    value: float


class ICPoint(ValuePoint):
    """日频截面 IC 及其有效样本数。"""
    sample_size: int


class HorizonIC(BaseModel):
    """单个持有期的 IC/RankIC 汇总与日度序列。"""
    horizon: int
    ic_mean: Optional[float] = None
    ic_std: Optional[float] = None
    rank_ic_mean: Optional[float] = None
    ic_ir: Optional[float] = None
    valid_days: int = 0
    ic_series: list[ICPoint] = []
    rank_ic_series: list[ICPoint] = []
    unavailable_reason: Optional[str] = None


class RollingIRPoint(ValuePoint):
    """滚动 IC 信息比率。"""
    window: int
    sample_size: int


class IndustryEquityCurve(BaseModel):
    """单个行业的等权收益净值曲线。"""
    industry_code: str
    industry_name: str
    points: list[ValuePoint] = []


class IndustryGroupPoint(BaseModel):
    """单日行业内因子分组净值。"""
    date: str
    groups: "dict[str, float]" = {}


class IndustryGroupEquityCurve(BaseModel):
    """单个行业内各因子分组的净值曲线。"""
    industry_code: str
    industry_name: str
    points: list[IndustryGroupPoint] = []


class FactorAnalysis(BaseModel):
    """因子分析结果（因子回测特有）。"""
    ic_mean: Optional[float] = None
    ic_std: Optional[float] = None
    rank_ic_mean: Optional[float] = None
    layered_returns: "dict[str, Optional[float]]" = {}
    cumulative_series: list[LayeredPoint] = []
    quantile_metrics: list[QuantileMetric] = []
    definition: "dict[str, object]" = {}
    ic_series: list[ICPoint] = []
    #: 日度 RankIC（Spearman）序列；``core`` 与 ``full`` 都返回。
    rank_ic_series: list[ICPoint] = []
    #: 多持有期 IC 明细，键为持有交易日；默认请求 [1] 时为空。
    ic_horizons: "dict[str, HorizonIC]" = {}
    cumulative_ic_series: list[ValuePoint] = []
    rolling_ir_series: list[RollingIRPoint] = []
    industry_equity_curves: list[IndustryEquityCurve] = []
    industry_group_equity_curves: list[IndustryGroupEquityCurve] = []


class FactorBacktestMeta(BaseModel):
    """因子回测元信息。"""
    factor: str
    universe: str
    portfolio_type: str
    rebalance_days: int
    groups: int
    benchmark: str
    filter_limit_up: bool = False
    filter_limit_down: bool = False
    filter_suspended: bool = False
    filter_st: bool = False
    filter_status: dict[str, str] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    #: 本次实际生效的交易成本参数回显。成本只作用于主组合（净值/收益/指标），
    #: 分层曲线与多空价差保持毛收益，见 cost_model。
    commission_rate: float = 0.0
    stamp_tax: float = 0.0
    slippage_rate: float = 0.0
    cost_model: dict[str, str] = Field(default_factory=dict)
    #: 成交时点口径回显（信号时点、成交价、成交日）。
    execution_timing: Literal["same_close", "next_close", "next_open"] = "next_close"
    execution_model: dict[str, str] = Field(default_factory=dict)
    execution_version: str = "factor-execution-v8"
    metric_version: str = "factor-scorecard-v5"
    data_release_id: Optional[str] = None
    spec_hash: Optional[str] = None
    factor_source_hash: Optional[str] = None
    actual_end: Optional[str] = None
    factor_price_adjustment: Optional[str] = None
    factor_price_source: Optional[str] = None
    factor_price_fields: list[str] = Field(default_factory=list)
    factor_price_precision: Optional[str] = None
    return_price_adjustment: Optional[str] = None
    return_price_source: Optional[str] = None
    return_price_precision: Optional[str] = None
    timings_seconds: "dict[str, float]" = {}
    universe_asset: Optional[ResolvedAssetMeta] = Field(None, exclude_if=lambda value: value is None)
    benchmark_asset: Optional[ResolvedAssetMeta] = Field(None, exclude_if=lambda value: value is None)


class StrategyBacktestMeta(BaseModel):
    """策略回测元信息。"""
    strategy: str
    universe: str
    benchmark: str
    engine_mode: Literal["vectorized", "event"] = "vectorized"
    strategy_id: Optional[str] = None
    strategy_version: Optional[str] = None
    code_hash: Optional[str] = None
    #: 本次回测实际绑定的行情发布；与因子回测同口径，供报告自证数据版本。
    data_release_id: Optional[str] = None
    #: 请求 + 数据发布的规范化指纹（不含 run_id），同一输入可复现同一结果。
    spec_hash: Optional[str] = None
    #: 本次任务 id；同步接口也会返回，便于把报告与运行日志对应起来。
    run_id: Optional[str] = None
    #: factor_refs 模式下的因子引用回显；其它模式不输出该字段。
    factor_refs: list[dict] = Field(
        default_factory=list, exclude_if=lambda value: not value
    )
    #: 本次请求的过滤开关回显；事件模式下前三个由撮合层常开拒单覆盖
    filter_suspended: bool = False
    filter_st: bool = False
    filter_limit_up: bool = False
    filter_limit_down: bool = False
    filter_status: dict[str, str] = Field(default_factory=dict)
    #: 本次实际生效的交易成本参数回显，便于结果自证用了什么费率。
    #: 向量化模式按换手率建模，min_commission 不适用（回显为 null）。
    commission_rate: float = 0.0
    stamp_tax: float = 0.0
    slippage_rate: float = 0.0
    min_commission: Optional[float] = None
    cost_model: dict[str, str] = Field(default_factory=dict)
    #: 成交时点口径回显（信号时点、成交价、成交日）。
    execution_timing: Literal["same_close", "next_close", "next_open"] = "next_close"
    #: 成交/估值价格来源回显。
    execution_price_source: Literal["adjusted", "unadjusted"] = "adjusted"
    execution_model: dict[str, str] = Field(default_factory=dict)
    trading_symbols: Optional[list[str]] = Field(None, exclude_if=lambda value: value is None)
    universe_asset: Optional[ResolvedAssetMeta] = Field(None, exclude_if=lambda value: value is None)
    benchmark_asset: Optional[ResolvedAssetMeta] = Field(None, exclude_if=lambda value: value is None)


class StrategyInfo(BaseModel):
    id: str
    name: str
    strategy_type: str
    execution_mode: Literal["vectorized", "event"]
    current_version: str
    entrypoint: str
    tags: list[str] = []


class EventOrderInfo(BaseModel):
    order_id: str
    symbol: str
    quantity: Optional[int] = None
    target_value: Optional[float] = None
    side: Optional[str] = None
    status: str
    reason_code: Optional[str] = None
    submitted_at: Optional[str] = None


class EventFillInfo(BaseModel):
    order_id: str
    symbol: str
    side: str
    quantity: int
    price: float
    timestamp: str
    commission: float
    stamp_tax: float


class EventPositionSnapshot(BaseModel):
    timestamp: str
    cash: float
    total_value: float
    positions: "dict[str, int]" = {}
    sellable_quantities: "dict[str, int]" = {}


class ExecutionTimelineItem(BaseModel):
    """统一的策略执行时间线条目。"""

    timestamp: str
    type: str
    payload: "dict[str, object]" = {}


class StrategyListResponse(BaseModel):
    strategies: list[StrategyInfo]


class FactorBacktestResponse(BaseModel):
    """``POST /api/backtest/factor`` 响应。

    内部同步调用在因子代码出错时不抛 500，置 ``error`` 字段返回结构化错误；异步 HTTP
    任务会把该错误映射到任务顶层 ``error``。前端根据成功任务的结构化数据自行渲染图表，
    不依赖后端预渲染图片。
    """

    # 核心指标
    metrics: "dict[str, Optional[float]]" = {}
    # 净值曲线（含回撤，前端画收益曲线+回撤面积图）
    equity_curve: Optional[list[EquityPoint]] = None
    # 回撤区间明细（前端画回撤标注）
    drawdowns: list[DrawdownInfo] = []
    # 分年收益（前端画柱状图）
    annual_returns: list[AnnualReturn] = []
    # 因子分析（因子回测特有）
    factor_analysis: Optional[FactorAnalysis] = None
    # 起止日期
    date_range: DateRange = DateRange()
    # 最终净值
    final_equity: Optional[float] = None
    # 基准净值曲线
    benchmark_curve: Optional[list[EquityPoint]] = None
    # 策略相对基准的区间超额收益
    excess_return: Optional[float] = None
    # 元信息
    meta: Optional[FactorBacktestMeta] = None
    # 错误信息
    error: Optional[str] = None


FactorBacktestTaskStatus = Literal[
    "queued",
    "running",
    "succeeded",
    "failed",
    "cancelled",
]


class FactorBacktestTaskSubmissionResponse(BaseModel):
    """因子回测异步提交结果。"""

    run_id: str
    status: FactorBacktestTaskStatus
    poll_url: str
    created_at: float


class FactorBacktestTaskResponse(BaseModel):
    """因子回测任务快照；成功时 ``result`` 保持旧回测响应结构。"""

    run_id: str
    status: FactorBacktestTaskStatus
    created_at: float
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    cancel_requested: bool = False
    error: Optional[str] = None
    result: Optional[FactorBacktestResponse] = None


class StrategyBacktestResponse(BaseModel):
    """``POST /api/backtest/strategy`` 响应。"""

    # 核心指标
    metrics: "dict[str, Optional[float]]" = {}
    # 净值曲线（含回撤）
    equity_curve: Optional[list[EquityPoint]] = None
    # 回撤区间明细
    drawdowns: list[DrawdownInfo] = []
    # 分年收益
    annual_returns: list[AnnualReturn] = []
    # 起止日期
    date_range: DateRange = DateRange()
    # 最终净值
    final_equity: Optional[float] = None
    # 基准净值曲线
    benchmark_curve: Optional[list[EquityPoint]] = None
    # 策略相对基准的区间超额收益
    excess_return: Optional[float] = None
    # 元信息
    meta: Optional[StrategyBacktestMeta] = None
    # 持仓快照（可选）
    positions: Optional[list[PositionSnapshot]] = None
    rebalance_logs: Optional[list[RebalanceLog]] = None
    execution_timeline: Optional[list[ExecutionTimelineItem]] = None
    # 事件驱动模式的订单、成交和审计轨迹；向量化模式均为 null。
    orders: Optional[list[EventOrderInfo]] = None
    fills: Optional[list[EventFillInfo]] = None
    event_positions: Optional[list[EventPositionSnapshot]] = None
    events: Optional[list["dict[str, object]"]] = None
    assumptions: Optional["dict[str, str]"] = None
    # 错误信息
    error: Optional[str] = None
    #: 机器可读的错误分类；目前用于区分“调用了不支持的聚宽 API”。
    error_code: Optional[str] = None
    #: 当 error_code=unsupported_api 时，给出具体 API/原因，便于调用方预检。
    unsupported_api: Optional[str] = None


class FactorInfo(BaseModel):
    name: str
    description: Optional[str] = None
    factor_id: Optional[str] = None
    display_name: Optional[str] = None
    name_en: Optional[str] = None
    category_name: Optional[str] = None


class FactorLibraryInfo(BaseModel):
    """一个已登记内置因子库的目录信息。"""

    name: str
    description: Optional[str] = None
    factor_count: int = 0
    tags: list[str] = Field(default_factory=list)


class FactorLibraryListResponse(BaseModel):
    """``GET /api/factor-libraries`` 返回的全部内置因子库。"""

    libraries: list[FactorLibraryInfo]


class FactorListResponse(BaseModel):
    library_name: str
    factors: list[FactorInfo]


class HealthResponse(BaseModel):
    status: str = "ok"
    version: str = "2.0.0"
    engines: list[str] = ["native"]


class DataReleaseStatusResponse(BaseModel):
    """当前已装载行情 release 的状态；``/api/data/status`` 与重载接口共用。"""

    data_version: str | None = None
    release_id: str | None = None
    data_dir: str | None = None
    #: 数据侧的视图锁（``views/current.lock``）；锁缺失或与已装载目录不一致时省略。
    view_id: str | None = None
    manifest_sha256: str | None = None


class DailyBar(BaseModel):
    """单个标的的完整日线 OHLC 数据点。"""

    date: str
    open: float
    high: float
    low: float
    close: float
    #: 当日成交量大于 0 时为 True；成交量为 0 或缺失时为 False。
    is_show: bool


class DailyBarsResponse(BaseModel):
    """``GET /api/market-data/daily/{symbol}`` 响应。"""

    symbol: str
    name: str
    bars: list[DailyBar]
    date_range: DateRange


class StockInfo(BaseModel):
    """股票代码与名称。"""

    symbol: str
    name: str


class SymbolListResponse(BaseModel):
    """``GET /api/market-data/symbols`` 响应。"""

    symbols: list[StockInfo]
    unmapped_count: int = 0


class IndexInfo(BaseModel):
    """已发布股指的代码与名称。"""

    code: str
    name: str


class IndexListResponse(BaseModel):
    """``GET /api/market-data/indices`` 响应。"""

    indices: list[IndexInfo]


class IndexConstituentsResponse(BaseModel):
    """``GET /api/market-data/indices/{code}/constituents`` 响应。"""

    index: IndexInfo
    constituents: list[StockInfo]
    unmapped_count: int = 0
