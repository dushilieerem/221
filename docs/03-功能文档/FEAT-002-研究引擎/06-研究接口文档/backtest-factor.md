# POST /api/backtest/factor

用于因子选股研究。服务以截面向量化 `rank` 分组构建组合，返回绩效、净值、基准、超额收益和因子分析。

## 异步任务契约

`POST /api/backtest/factor` 只负责校验请求并提交任务，不等待因子计算、行情指纹扫描或缓存读取完成：

```http
POST /api/backtest/factor
```

成功提交返回 `202 Accepted`：

```http
Location: /api/backtest/factor/9f1c...
Retry-After: 1
```

```json
{
  "run_id": "9f1c...",
  "status": "queued",
  "poll_url": "/api/backtest/factor/9f1c...",
  "created_at": 1720000000.0
}
```

WebApp 按 `poll_url` 轮询：

```http
GET /api/backtest/factor/9f1c...
```

```json
{
  "run_id": "9f1c...",
  "status": "running",
  "created_at": 1720000000.0,
  "started_at": 1720000001.0,
  "finished_at": null,
  "cancel_requested": false,
  "error": null,
  "result": null
}
```

终态有三种：

- `succeeded`：`result` 是本页原有完整 `FactorBacktestResponse`，包括 `metrics`、`equity_curve`、`factor_analysis`、`meta` 等字段。
- `failed`：`error` 是因子代码、数据或执行错误；`result` 为 `null`。
- `cancelled`：排队任务已取消；`result` 为 `null`。

任务完成后保留 60 秒；未知或已过期的 `run_id` 返回 `404`。`DELETE /api/backtest/factor/{run_id}`
可取消任务：排队任务实际取消并释放队列槽位，运行中任务只设置 `cancel_requested=true`，不强杀用户 Python 线程；已完成任务 DELETE 返回当前终态。

```jsonc
{
  "factor_code": "def momentum(self, window=5):\n    return self.close.pct_change(window)",
  "factor_name": "momentum",
  "portfolio_type": "long_only",
  "universe": "CSI300",
  "start": "2024-01-01", "end": "2024-12-31",
  "rebalance_days": 1, "groups": 5,
  "benchmark": "CSI300", "analysis_level": "core",
  "filter_suspended": false, "filter_st": false,
  "filter_limit_up": false, "filter_limit_down": false
}
```

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `factor_code` | string | — | 自定义因子函数。未传时必须传单个 `library_name`。 |
| `library_name` | string/string[] | — | 内置库；自定义因子可传多个以复用方法。 |
| `factor_name` | string | `alpha101` | 要执行的函数名。 |
| `portfolio_type` | enum | `long_only` | `long_only` 或 `long_short`。 |
| `universe` | string | `CSI300` | 股票池。 |
| `universe_snapshot` | object/null | `null` | WebApp 已鉴权并解析的静态证券池快照；与 `universe` 至多传一个。 |
| `start` / `end` | string/null | `null` | 回测区间，格式 `YYYY-MM-DD`。 |
| `rebalance_days` | integer | `1` | 正整数交易日。 |
| `groups` | integer | `5` | 2–10 组。 |
| `winsorize` | integer | `0` | 因子去极值方式：`0` 不处理、`1` MAD、`2` 标准差、`3` 四分位。 |
| `standardize` | integer | `0` | 因子标准化方式：`0` 不处理、`1` 原始值、`2` rank、`3` 极差。 |
| `benchmark` | enum | `CSI300` | `CSI300`、`CSI500`、`CSI1000` 或 `SSE50`。 |
| `benchmark_snapshot` | object/null | `null` | WebApp 已鉴权并解析的固定权重组合基准快照；与 `benchmark` 至多传一个。 |
| `analysis_level` | enum | `core` | `core` 或 `full`；后者增加 IC/IR、FF3、行业曲线。 |
| `filter_suspended` | boolean | `false` | 是否把停牌标的排除出研究样本。 |
| `filter_st` | boolean | `false` | 是否把 ST、\*ST 标的排除出研究样本。 |
| `filter_limit_up` | boolean | `false` | 是否把涨停标的排除出研究样本。 |
| `filter_limit_down` | boolean | `false` | 是否把跌停标的排除出研究样本。 |
| `commission_rate` | number | `0.0` | 佣金率，按主组合的 `\|Δ权重\|` 换手计提，作用于净值与指标。 |
| `stamp_tax` | number | `0.0` | 印花税率，只对主组合的卖出（权重下降）部分计提。 |
| `slippage_rate` | number | `0.0` | 滑点率，同样按换手计提。与佣金同项相加，单独设置其中一个结果相同。 |

### 日期语义

`start/end` 是收益确认区间，不是因子计算区间。服务端会先用当前数据版本的完整可用历史计算因子（包括区间起点之前的预热数据），再将因子值、收盘价、远期收益、分层结果、指标和净值裁剪到请求区间。因子不需要额外声明 `warmup_days`；因子实现仍需自行避免使用未来数据。

完整示例和因子写法见 [因子与策略指南](../07-factor-strategy-guide.md)。

### 交易成本

三个成本参数只作用于**主组合**（`equity_curve`、`returns`、`metrics`、`annual_returns`、`excess_return` 均为净口径）：

| 成本 | 计提方式 | 口径 |
|---|---|---|
| `commission_rate` | `\|Δ权重\|` 换手 × 费率 | 占组合净值的比例 |
| `slippage_rate` | `\|Δ权重\|` 换手 × 费率 | 同上 |
| `stamp_tax` | 卖出（权重下降）部分 × 费率 | 同上 |

三点需要注意：

1. **分层曲线（Q1~Qn）与由它派生的多空价差保持毛收益**。它们衡量的是因子的排序能力，属于诊断口径，扣成本会干扰分组单调性的判断。这一分工回显在 `meta.cost_model` 里。
2. **首个持有期的建仓成本不计入**（`weight_change` 首行填 0），与向量化策略引擎一致。
3. 成本量纲是「占组合净值的比例」而非货币金额——因子回测的净值从 1.0 起算，没有真实资金规模。`meta.metrics.commission_cost` 等字段是全区间累加值。

不传成本参数时结果与扣成本前逐位一致，既有回测结论不受影响。`meta` 会回显 `commission_rate` / `stamp_tax` / `slippage_rate` 与 `cost_model`，便于结果自证。

**批量回测**（`POST /api/internal/factor-backtest-batches` 与 `python -m app.cli.factor_backtest_batch`）同样接受 `commission_rate` / `stamp_tax` / `slippage_rate`，口径与单因子完全一致；三者已进入批次身份（`profile_hash`），改动费率会生成新批次而不是复用旧结果。

> **缓存**：成本参数已进入计算身份（`computation_payload`）。此前它们被固定为 0，若沿用旧口径，不同费率的请求会共用缓存键并互相返回错误结果。同时 `execution_version` 升到 `factor-execution-v7`，旧缓存与库里已存结果按新口径重算。

### 样本过滤

四个 `filter_*` 开关互相独立，默认全部关闭——不传时回测结果与不启用过滤完全一致。开启后不合格标的的因子值被置空，**在去极值和标准化之前**退出研究样本，因此不会参与截面参数估计、分层与选股。

#### 退市剔除（恒开启，无开关）

除上述四项可选开关外，回测**始终**按 `listed_status` 剔除退市（不可投资）标的：只有 `在市`、`ST`、`*ST` 三种取值可进入研究样本，`终止上市`、`退市整理`、`暂停上市`、空值以及未来任何未知取值一律剔除。这一项没有对应的请求参数，无法关闭，`filter_status["is_delisted"]` 恒为 `enabled`。

之所以强制：本 release 的 `market_data_hfq.parquet` 在标的终止上市后**不再置空 OHLC**，而是把价格冻结在最后成交价。不剔除时，只用价格的横截面因子（低波动、低 beta、低换手等）会把这批冻结价标的排到最前——实测 2020-06-30 / 2023-06-30 / 2025-06-30 三个时点的「20 日波动率最低 Top30」中分别有 **29 / 30 / 30 只**是不可投资标的（波动率恒为 0），回测会得到零回撤的假曲线。加上本过滤后这三个时点的 Top30 中不可投资标的数均为 0。

剔除是**点时**的，不是整段的：标的只从「进入退市状态的那一天」起被排除，退市之前的正常交易历史照常参与，避免存活偏差。

空值与四项开关的口径**相反**：`listed_status` 的缺失值是标的上市前的空档，与价格数据完全不重叠，这里按**不可投资**处理而不是放行；单元格级缺失不产生告警（数量以百万计且属预期状态）。只有 `listed_status` 字段整个缺失时才降级跳过，记为 `skipped_missing_field` 并附告警。

过滤作用在样本资格层，不删除行情行。状态字段缺失时的行为按「跳过并放行」处理：

| 数据版本情况 | 结果 |
|---|---|
| 字段存在、取值完整 | 正常剔除，`filter_status` 记为 `enabled` |
| 字段存在、部分单元格为空 | 空值单元格按放行，记为 `enabled_partial` 并附告警 |
| 字段整个缺失（如当前 release 无停牌字段） | 跳过该项过滤并放行，记为 `skipped_missing_field` 并附告警 |

对应的状态字段是 `filter_st` → `listed_status`（取值 `ST`、`*ST`），其余三项 → `up_and_down_status`（取值 `停牌`、`涨停`、`跌停`）。`meta.filter_status` 回显四项各自的上述状态，`meta.warnings` 给出「覆盖不足」等告警原文。上表的「放行」口径只适用于这四项可选开关；退市剔除的缺失口径相反，见上一节。

**已知局限**：掩码在因子值算完之后应用（这样滚动窗口不受过滤影响）。因此因子若在 `compute()` 内部自己做横截面运算（`rank`/`zscore`/`normalize`），横截面里仍会包含冻结的退市股，影响量级为 O(1/N)。打分卡自身的去极值/标准化在掩码之后执行，是干净的。需要严格截面口径的因子应自行按 `listed_status` 白名单过滤。

任务成功时，`result` 包含 `metrics`、`equity_curve`、`drawdowns`、`annual_returns`、`factor_analysis`、`date_range`、`final_equity`、`benchmark_curve`、`excess_return`、`meta` 和 `error`。因子代码、数据或回测执行错误不再要求前端解析 `result.error`，而是以任务 `status=failed` 和顶层 `error` 返回。

`factor_analysis.quantile_metrics` 返回各因子分位（`Q1` 为最高分位，`QN` 为最低分位）及基准的绩效。分组的 `annual_return`、`volatility`、`sharpe` 和 `max_drawdown` 是分组自身的绝对绩效；`annual_excess_return`、`excess_volatility`、`information_ratio` 和 `benchmark_win_rate` 是相对基准指标。最后一项 `quantile: "benchmark"` 返回基准的绝对绩效，相对基准字段为 `null`：

```json
[
  {
    "quantile": "Q1",
    "annual_return": 0.12,
    "volatility": 0.18,
    "sharpe": 0.67,
    "max_drawdown": -0.21,
    "annual_excess_return": 0.03,
    "excess_volatility": 0.10,
    "information_ratio": 0.30,
    "benchmark_win_rate": 0.52
  },
  {
    "quantile": "benchmark",
    "annual_return": 0.09,
    "volatility": 0.15,
    "sharpe": 0.60,
    "max_drawdown": -0.18,
    "annual_excess_return": null,
    "excess_volatility": null,
    "information_ratio": null,
    "benchmark_win_rate": null
  }
]
```

其中，分组 `annual_return` 使用包含首个信号到入场日收益的完整收益序列计算几何年化 CAGR，分组 `volatility`/`sharpe` 使用样本标准差 `ddof=1`；基准使用入场日到结束日的收益序列，首个入场日收益保留为 `0`，年化同样使用几何 CAGR，波动率/夏普使用样本标准差 `ddof=1`。分组净值曲线首个输出点统一为 `1.0`，首个信号收益只进入分组绝对指标，不进入展示曲线或相对基准指标。

相对基准指标只使用分组与基准都有数据的共同交易日：`annual_excess_return` 为共同区间分组几何年化收益减去基准几何年化收益，`excess_volatility` 为日超额收益样本标准差（`ddof=1`）年化，`information_ratio` 为年化超额收益除以超额年化波动；`benchmark_win_rate` 为共同日期上分组当日收益大于基准累计收益的占比，等于时不计为胜。基准累计收益从共同区间首日归一化净值计算。`core` 与 `full` 均返回该字段；若分组或基准没有足够有效数据，对应指标为 `null`。

因子回测响应的 `meta` 会明确返回因子价格和收益价格的复权口径、字段、逻辑来源及精度策略。当前默认策略为：因子使用后复权 OHLC，收益使用后复权 close，均来自研究行情 `data_engine`，读取源精度且不额外四舍五入。`unadjusted_price/unadjusted_price.parquet` 只用于行情展示接口，不参与因子计算。

`meta` 同时回显本次请求的过滤开关 `filter_suspended`、`filter_st`、`filter_limit_up`、`filter_limit_down`，以及诊断字典 `filter_status`（键为 `is_suspended`、`is_st`、`is_limit_up`、`is_limit_down`、`is_delisted`，取值为 `disabled`、`skipped_missing_field`、`enabled`、`enabled_partial`）。即使四项开关全关，`filter_status` 也会返回四个 `disabled`；`is_delisted` 恒开启，正常为 `enabled`。

> **缓存**：退市剔除改变了研究样本口径却不在请求参数里，因此执行版本号已从 `factor-execution-v5` 升到 `factor-execution-v6`，旧的磁盘缓存与批次记录不会命中，会按新口径重算一次。

使用自定义资产的请求示例：

```json
{
  "factor_code": "def momentum(self):\n    return self.close.pct_change(20)",
  "factor_name": "momentum",
  "universe_snapshot": {"asset_id": "user-pool-42", "version": "3", "content_hash": "...", "symbols": ["000858", "600519"]},
  "benchmark_snapshot": {"asset_id": "user-benchmark-9", "version": "2", "content_hash": "...", "weights": {"000858": 0.5, "600519": 0.5}, "pricing": "close_to_close"}
}
```

成功时 `meta.universe_asset` 和 `meta.benchmark_asset` 分别返回 `{id, version, content_hash, constituent_count}`；证券池另有 `membership_mode: "static"`，组合基准另有 `pricing: "close_to_close"`。完整的实际成分和权重保存到运行结果元数据，不在 API 响应中展开。

静态证券池的每个代码必须存在于本次回测的 `close` 面板。组合基准要求所有成分在回测区间内有完整收盘价；权重必须为非负有限数且和为 1。违反这些规则时，任务终态为 `failed`，顶层 `error` 包含可读业务错误；混用旧参数与 `*_snapshot` 时请求直接返回 HTTP `422`。ResearchEngine 不保存、列举或按 ID 查询用户资产。

服务端固定使用配置的数据目录和 Native 回测后端；`backtest_period`、`data_dir`、`backtest_engine`、`initial_capital`、`include_equity`、`include_factor_values`、`include_positions`、`rebalance`、`stock_num` 等旧字段会返回 `422`。

提交阶段的错误码为：请求参数错误 `422`、队列已满 `429`（响应含 `Retry-After: 1`）、ResearchEngine 已关闭 `503`。查询未知或过期任务返回 `404`。

## WebApp 联调验收清单

- ResearchEngine 只启动一个 Uvicorn worker、一个实例，保证 POST 和 GET 访问同一内存任务表。
- 前端收到 `202 + run_id` 后立即轮询：1 秒、2 秒，之后间隔最高 3 秒。
- `succeeded` 时保存 `result` 并进入报告页；`failed` 时展示顶层 `error`。
- 用户取消或重新提交时调用 DELETE/POST；运行中取消应允许任务自然结束。
- WebApp 在任务完成后的 60 秒内取走并持久化完整 `result`。
