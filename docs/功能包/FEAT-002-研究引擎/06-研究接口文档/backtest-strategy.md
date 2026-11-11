# POST /api/backtest/strategy

用于向量化或事件驱动策略。通过 `engine_mode` 显式选择：`vectorized`（默认）支持 `generate_signals()` 与 `FactorPortfolioStrategy`；`event` 支持实现 `initialize(context)` 的日频事件策略。引用策略目录时，请求模式必须与资产的 `execution_mode` 一致。

Web 端需要展示调仓或成交过程时，使用 `include_execution_timeline=true`，统一消费响应中的 `execution_timeline`。该字段不改变回测计算，只控制是否序列化执行明细：向量化模式将目标权重变化转换为 `rebalance` 条目；事件模式将订单、成交、账户快照和审计事件合并为时间线。`rebalance_logs` 以及事件模式的 `orders`、`fills`、`event_positions`、`events` 保留用于兼容和底层调试。

```jsonc
{
  "engine_mode": "vectorized",
  "strategy_code": "class MyStrategy: ...",
  "universe": "CSI500",
  "start": null, "end": null,
  "benchmark": "CSI300",
  "commission_rate": 0.0, "stamp_tax": 0.001,
  "slippage_rate": 0.0, "min_commission": 5.0,
  "execution_timing": "next_close",
  "initial_capital": 1000000,
  "include_positions": false, "include_rebalance_logs": true,
  "include_execution_timeline": true,
  "filter_suspended": false, "filter_st": false,
  "filter_limit_up": false, "filter_limit_down": false
}
```

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `strategy_code` | string/null | — | 完整 Python 源码；与 `strategy_id` 二选一。 |
| `engine_mode` | enum | `vectorized` | `vectorized` 或 `event`；决定策略契约和回测引擎。 |
| `strategy_id` | string/null | — | 已保存策略 ID；与 `strategy_code` 二选一。 |
| `strategy_version` | string/null | `null` | 已保存策略版本；省略时使用 `index.yaml` 中的 `current_version`。 |
| `factor_refs` | array/null | `null` | 内置因子引用（1–3 个），每项 `{library, factor, direction}`；与 `strategy_code`/`strategy_id` 互斥，仅向量化 + native 方言。只接受库名+因子名，不接受源码。 |
| `top_n` | integer/null | `null` | 仅 `factor_refs` 模式生效：等权 TopN 持仓数，默认 30。 |
| `rebalance_days` | integer/null | `null` | 仅 `factor_refs` 模式生效：调仓间隔交易日，默认 20。 |
| `universe` | string | `CSI500` | 股票池。 |
| `universe_snapshot` | object/null | `null` | WebApp 已鉴权并解析的静态证券池快照；仅向量化模式支持，且与 `universe` 至多传一个。 |
| `start` / `end` | string/null | `null` | 回测区间，格式 `YYYY-MM-DD`。 |
| `benchmark` | enum | `CSI300` | `CSI300` 或 `CSI500`（请求模型目前只接受这两个值）。 |
| `benchmark_snapshot` | object/null | `null` | WebApp 已鉴权并解析的固定权重组合基准快照；仅向量化模式支持，且与 `benchmark` 至多传一个。 |
| `commission_rate` | number | `0.0` | 佣金率。向量化按换手率计提；事件按每笔成交额计提。 |
| `slippage_rate` | number | `0.0` | 滑点率。向量化与佣金同项相加按换手计提；事件模式下**调整成交价**（买入向上、卖出向下）。 |
| `stamp_tax` | number | `0.001` | 卖出印花税率，两种模式都只对卖出部分计提。 |
| `min_commission` | number | `5.0` | 每笔最低手续费，**仅事件模式生效**；显式设 `0` 即取消最低收费。向量化按换手率建模、没有单笔概念，回显为 `null`。 |
| `execution_timing` | enum | `next_close` | `same_close` \| `next_close` \| `next_open`；信号恒在 T 日收盘产生，控制何时、以什么价成交。详见「成交时点语义」。 |
| `execution_price_source` | enum | `adjusted` | 仅事件模式：`adjusted` 用复权价成交与估值；`unadjusted` 用未复权价成交、估值、整手与费用，信号历史仍用复权价，持仓按复权比例每日缩放。详见「成交价来源」。 |
| `timed_execution` | boolean | `false` | 仅事件模式：按时点调度。策略用 `schedule.daily(cb, time="14:50")` 注册盘中回调，成交价取下单时点的盘中价；15:00 之后的订单挂到次日开盘。详见「时点调度」。 |
| `extra_symbols` | string[] | `[]` | 仅事件模式：额外并入行情面板的 ETF 代码（如货币基金 `511880.SH`）。ETF 不在股票池数据里，引擎从视图的 `free/etf_daily` 单独加载，参与撮合与估值但不算股票（`get_all_securities(['stock'])` 会排除）；卖出不收印花税。 |
| `initial_capital` | number | `1000000` | 初始资金。 |
| `include_positions` | boolean | `false` | 返回持仓权重快照。 |
| `include_rebalance_logs` | boolean | `false` | 仅向量化模式返回目标权重变化日志；事件模式请使用 `include_execution_timeline`。 |
| `include_execution_timeline` | boolean | `false` | 返回统一执行时间线；向量化策略包含目标权重变更，事件策略包含订单、成交、账户快照和审计事件。 |
| `filter_suspended` | boolean | `false` | 是否过滤停牌标的。向量化模式按信号剔除；事件模式由撮合层常开拒单覆盖，该参数不额外生效。 |
| `filter_st` | boolean | `false` | 是否过滤 ST、\*ST 标的。向量化模式按信号剔除；事件模式**只拒绝买入**（见下）。 |
| `filter_limit_up` | boolean | `false` | 是否过滤涨停标的。向量化模式按信号剔除；事件模式由撮合层常开拒单覆盖，该参数不额外生效。 |
| `filter_limit_down` | boolean | `false` | 是否过滤跌停标的。向量化模式按信号剔除；事件模式由撮合层常开拒单覆盖，该参数不额外生效。 |

## 成交时点语义

`execution_timing` 控制信号产生后何时成交，默认 `next_close`。

两种模式下的策略回调**都**只看到截至前一交易日收盘的数据，因此信号恒在 T 日收盘产生、成交发生在 T+1 日：

| 取值 | 成交价 | 向量化模式（持仓生效） | 事件模式 |
|---|---|---|---|
| `same_close` | T 日**收盘价**（旧口径） | 次日（`lag=1`），收益收盘价对收盘价 | 当日收盘撮合，**回调能看到当日收盘** |
| `next_close`（默认） | T+1 日**收盘价** | 再次日（`lag=2`） | 当日收盘撮合 |
| `next_open` | T+1 日**开盘价** | 次日（`lag=1`），收益拆两段 | 当日开盘撮合 |

三点需要注意：

1. **事件模式的行为变了**：此前它恒按当日开盘价成交（`assumptions.execution` 为 `same_session_open_after_before_open`）。现在默认 `next_close` 取当日收盘价；需要开盘成交请显式传 `next_open`。
2. **`same_close` 与 `next_close` 在事件模式下成交价相同**（都是当日收盘），区别在**数据可见性**：`same_close` 的回调能看到当日收盘（`assumptions.data_visibility` 为 `same_close`），另两种只到前一收盘。前者是「收盘出信号、收盘成交」的乐观口径，与因子/向量化路径的 `same_close` 对齐。
3. **退市强平不受影响**：无论哪种口径，持仓遇退市都按转换日收盘价强制平仓（见上文「退市剔除」）。

**成交价同时用于定量**：事件模式的目标金额换算成股数时用同一个价格，保证自洽；滑点仍然作用在这个价格之上。

`meta.execution_model` 会回显 `signal`、`fill_price`、`fill_session` 与 `fill_field`；事件模式的 `assumptions.execution` 取值为 `next_close` 或 `next_open`。

## 成交价来源

事件模式的 `execution_price_source` 决定成交、估值、整手和费用使用哪套价格：

| 取值 | 信号/`context.data.history` | 成交价、估值、整手、费用 |
|---|---|---|
| `adjusted`（默认） | 复权面板 | 复权面板 |
| `unadjusted` | 复权面板 | `unadjusted_price` 未复权面板 |

`unadjusted` 适用于需要真实股数、真实现金和真实整手取整的回测。由于未复权价格在除权日会跳空，引擎在除权日开盘前按 `hfq_close / unadjusted_close` 的每日比例缩放持仓股数，取整余数折算现金；这等价于“分红送转全部再投资”，不需要额外的公司行为数据。该模式仅支持 `engine_mode="event"`，响应 `meta.execution_price_source` 与 `assumptions.price_source` 会回显实际口径。

## 时点调度

`timed_execution=true` 时，事件引擎按策略注册的时点顺序执行：

| 时点 | 行为 |
|---|---|
| 09:00 等开盘前 | 只运行回调、收集订单，不成交 |
| 09:30 | 执行挂起订单与 09:30 回调产生的订单，按开盘价成交 |
| 09:31–14:59 | 每个时点先跑回调，再按该时点的盘中价成交 |
| 15:00 及之后 | 只运行回调，订单挂起到次日 09:30，现金冻结 |

盘中成交价优先读 `high_frequency/1min`（按日文件、`(date, "HH:MM", symbol)`）；该日期没有分钟数据时，上午时点回退到日线开盘价、下午时点回退到日线收盘价。`assumptions.execution` 为 `timed`，`assumptions.intraday_price` 为 `1m` 或 `daily_open_close_fallback`。

该语义来自聚宽日频回测实测：09:31 与 14:50 的市价单按当时的 `last_price` 成交，15:10 的订单状态为 `new`、当日不成交。仅支持 `engine_mode="event"`。

## 额外 ETF 标的

事件模式默认只交易股票池里的标的。策略需要持有货币 ETF 做现金管理时（例如破净 v3 的 `511880.XSHG`），用 `extra_symbols` 显式声明：

- 行情来自视图的 `free/etf_daily`（ETF 不并入 `ifind/market_data` 股票池），按 `symbol + time` 与股票面板 join；
- 策略里的聚宽代码（`511880.XSHG`）会被统一转换成 `511880.SH` 再查面板，持仓与订单回报仍按策略侧写法返回，保证策略内的字符串比较成立；
- ETF 参与撮合、估值与涨跌停（按 10%），但 `get_all_securities(['stock'])` 不会返回它们；卖出不收印花税。

区间内 ETF 没有行情时对应列为空，引擎按停牌处理（不能成交）；缺少数据集时直接报错，不会静默跳过。

## 交易成本语义

两种 `engine_mode` 对同一组参数的建模方式不同，`meta.cost_model` 会回显本次实际生效的口径。

**向量化模式**（按换手率，没有单笔成交的概念）：

```
净收益 = 毛收益 − 换手率×(commission_rate + slippage_rate) − 卖出换手率×stamp_tax
```

- 佣金与滑点**同项相加**，因此单独设置其中一个得到的结果相同；
- `min_commission` 不适用（回显 `not_applicable` 与 `null`），因为无法表达「按笔下限」；
- 首个持有期的建仓成本不计入。

**事件模式**（按笔成交，滑点体现在成交价上）：

- 佣金：每笔 `max(成交额 × commission_rate, min_commission)`；
- 印花税：每笔卖出 `成交额 × stamp_tax`；
- 滑点：买入成交价 `参考价 × (1 + slippage_rate)`、卖出 `参考价 × (1 − slippage_rate)`。因此滑点**会体现在 `fills[].price`** 上，成交额、佣金与印花税都按调整后的价格计算；强平同样计滑点；
- 注意：事件模式**每笔至少收 `min_commission`（默认 5 元）**，即使把三个费率都设为 0 也仍有最低收费。需要真正的零成本请显式传 `min_commission: 0`。

滑点与佣金在经济上等价（都是成交额的固定比例），所以单独设 1% 佣金与单独设 1% 滑点在两种模式下都会得到相近的结果。

`meta` 同时回显本次实际生效的成本参数 `commission_rate`、`stamp_tax`、`slippage_rate`、`min_commission`（向量化模式为 `null`）与建模口径字典 `cost_model`，使结果可以自证用了什么费率。

## 过滤语义

四个 `filter_*` 开关互相独立，默认全部关闭——不传时回测结果与不启用过滤完全一致。两种 `engine_mode` 的落地方式不同：

### 向量化模式

不合格标的的**得分被置空**，在 TopN 选股之前退出候选池，因此由次优标的补足目标持仓数，而不是留下现金缺口。过滤只在调仓日改变选股结果，不改变调仓节奏（非调仓日依旧保持「不调仓」语义）。

对不继承 `FactorPortfolioStrategy`、自行实现 `generate_signals()` 的自由策略，服务层改为把被剔除标的的目标权重写为 `0`，使当期真正退出。

### 事件模式

事件模式的撮合层**始终**执行 A 股真实交易约束，与请求参数无关：停牌单被拒（`SUSPENDED`）、买入涨停单被拒（`LIMIT_UP`）、卖出跌停单被拒（`LIMIT_DOWN`）。

因此：

- `filter_suspended`、`filter_limit_up`、`filter_limit_down` 在事件模式下**不额外生效**（撮合层已覆盖，`filter_status` 回显为 `matcher_always_on`）。
- `filter_st` 是事件模式下唯一生效的开关：开启后按 `listed_status` 判定，**只拒绝买入** ST、\*ST 标的，`reason_code` 为 `ST`；已持仓标的转为 ST 后仍允许卖出，避免持仓被套死。
- 当前数据版本缺少 `listed_status` 字段时，该过滤按「跳过并放行」处理，`filter_status` 记为 `skipped_missing_field`，请求本身不报错。

状态字段的取值口径与因子回测一致：`filter_st` → `listed_status`（`ST`、`*ST`），其余三项 → `up_and_down_status`（`停牌`、`涨停`、`跌停`）。

### 退市剔除（恒开启，两种模式都生效，无开关）

除上述四项可选开关外，两种 `engine_mode` 都**始终**按 `listed_status` 剔除退市（不可投资）标的：只有 `在市`、`ST`、`*ST` 可交易，`终止上市`、`退市整理`、`暂停上市`、空值一律剔除。没有对应请求参数，无法关闭，`filter_status["is_delisted"]` 在向量化模式下为 `enabled`、事件模式下为 `matcher_always_on`。

- **向量化模式**：与 `filter_st` 同一套剔除机制——不合格标的的得分在 TopN 之前被置空（自由策略则把权重写 `0`）。剔除是**点时**的：标的只从进入退市状态那天起被排除，历史照常参与，避免存活偏差。
- **事件模式**：
  - 不可投资标的的**买单被拒**，`reason_code` 为 `DELISTED`；卖出不额外拦截——退市整理期本就是留给投资者退出的，策略可以自己按开盘价卖出，卖不掉（停牌/跌停）则由下面这一步兜底；
  - 持仓遇到退市时，引擎在该标的的**转换日收盘后按当日收盘价强制平仓**，记一笔 `reason_code=DELISTED` 的成交并发出 `FORCED_LIQUIDATION` 审计事件。强平是引擎主动行为，不受 T+1、涨跌停、停牌与整手约束，因此仓位不会一直被冻结价格吊着。强平只处置**当日撮合后的残余持仓**，策略自己卖掉的不会被重复卖出；
  - 成交价的取值依据：数据源的 OHLC 在退市后被前值填充，所以**转换日收盘价恒等于最后成交价**——当日无成交时它就是此前最后成交价，当日有成交时就是真实成交价。不能用开盘价，实测约一半的退市样本转换日开盘价为缺失，会直接以 `NO_PRICE` 拒单；
  - 该口径记录在响应的 `assumptions.delisted_handling` 中。

因此，本改动**不影响** `filter_st` 的语义：ST/\*ST 仍属白名单（可投资），是否排除它们依旧由 `filter_st` 单独决定。

## 成功响应示例

以下示例省略了 `equity_curve`、`benchmark_curve` 和 `positions` 的其余数据点；实际响应会返回完整序列。数值仅用于说明结构，不代表真实回测表现。

```json
{
  "metrics": {
    "annual_return": 0.1842,
    "sharpe": 1.3568,
    "sortino": 1.9021,
    "calmar": 1.7455,
    "max_drawdown": -0.1055,
    "volatility": 0.1634,
    "ic": null,
    "rank_ic": null,
    "win_rate": 0.5317,
    "turnover": 0.0842,
    "total_return": 0.2015
  },
  "equity_curve": [
    {"date": "2024-01-02", "value": 1000000.0, "drawdown": 0.0},
    {"date": "2024-01-03", "value": 1012500.0, "drawdown": 0.0},
    {"date": "2024-01-04", "value": 1008000.0, "drawdown": -0.004444}
  ],
  "drawdowns": [
    {
      "start": "2024-01-03",
      "end": "2024-01-04",
      "peak": 1012500.0,
      "trough": 1008000.0,
      "recovery": "2024-01-08",
      "depth": -0.004444,
      "days": 5
    }
  ],
  "annual_returns": [{"year": 2024, "return_value": 0.2015}],
  "date_range": {"start": "2024-01-02", "end": "2024-12-31"},
  "final_equity": 1201500.0,
  "benchmark_curve": [
    {"date": "2024-01-02", "value": 1.0, "drawdown": 0.0},
    {"date": "2024-01-03", "value": 1.006, "drawdown": 0.0}
  ],
  "excess_return": 0.0832,
  "meta": {
    "strategy": "EqualWeightMomentum",
    "universe": "CSI500",
    "benchmark": "CSI300",
    "engine_mode": "vectorized",
    "strategy_id": null,
    "strategy_version": null,
    "code_hash": null
  },
  "positions": [
    {
      "date": "2024-01-02",
      "holdings": [
        {"symbol": "000001.SZ", "name": "平安银行", "weight": 0.1},
        {"symbol": "600000.SH", "name": "浦发银行", "weight": 0.1}
      ]
    }
  ],
  "rebalance_logs": [
    {
      "date": "2024-01-02",
      "trades": [
        {
          "symbol": "000001.SZ",
          "name": "平安银行",
          "side": "buy",
          "previous_weight": 0.0,
          "target_weight": 0.1,
          "weight_change": 0.1
        }
      ]
    }
  ],
  "execution_timeline": [
    {
      "timestamp": "2024-01-02T00:00:00",
      "type": "rebalance",
      "payload": {
        "trades": [
          {
            "symbol": "000001.SZ",
            "name": "平安银行",
            "side": "buy",
            "previous_weight": 0.0,
            "target_weight": 0.1,
            "weight_change": 0.1
          }
        ]
      }
    }
  ],
  "orders": null,
  "fills": null,
  "event_positions": null,
  "events": null,
  "assumptions": null,
  "error": null
}
```

### 事件模式时间线示例

事件模式请求需要显式设置 `engine_mode`，并可按需打开统一时间线：

```jsonc
{
  "engine_mode": "event",
  "strategy_id": "donchian_a_share_event",
  "benchmark": "CSI300",
  "include_execution_timeline": true
}
```

事件模式响应中的 `execution_timeline` 结构如下（仅展示关键条目，实际会按回测日期返回完整序列）：

```json
{
  "meta": {
    "strategy": "DonchianAShareEventStrategy",
    "universe": "CSI500",
    "benchmark": "CSI300",
    "engine_mode": "event",
    "strategy_id": "donchian_a_share_event",
    "strategy_version": "v1",
    "code_hash": "..."
  },
  "execution_timeline": [
    {
      "timestamp": "2024-01-02T00:00:00",
      "type": "audit",
      "payload": {"type": "SESSION_OPEN", "timestamp": "2024-01-02T00:00:00"}
    },
    {
      "timestamp": "2024-01-02T00:00:00",
      "type": "audit",
      "payload": {
        "type": "SCHEDULED_CALLBACK",
        "timestamp": "2024-01-02T00:00:00",
        "callback": "rebalance"
      }
    },
    {
      "timestamp": "2024-01-02T00:00:00",
      "type": "order",
      "payload": {
        "order_id": "EVT-000001",
        "symbol": "000001",
        "quantity": 100,
        "target_value": null,
        "side": "buy",
        "status": "filled",
        "reason_code": null,
        "submitted_at": "2024-01-02T00:00:00"
      }
    },
    {
      "timestamp": "2024-01-02T00:00:00",
      "type": "fill",
      "payload": {
        "order_id": "EVT-000001",
        "symbol": "000001",
        "side": "buy",
        "quantity": 100,
        "price": 10.0,
        "timestamp": "2024-01-02T00:00:00",
        "commission": 5.0,
        "stamp_tax": 0.0
      }
    },
    {
      "timestamp": "2024-01-02T00:00:00",
      "type": "position",
      "payload": {
        "timestamp": "2024-01-02T00:00:00",
        "cash": 998995.0,
        "total_value": 999995.0,
        "positions": {"000001": 100},
        "sellable_quantities": {"000001": 0}
      }
    }
  ]
}
```

`execution_timeline[].type` 的取值和 `payload` 约定如下：

| `type` | 适用模式 | `payload` 内容 |
|---|---|---|
| `rebalance` | 向量化 | `{trades: [...]}`；每笔交易含 `symbol`、`name`、`side`、`previous_weight`、`target_weight`、`weight_change`。 |
| `order` | 事件 | 一个 `EventOrderInfo`：订单编号、标的、数量/目标金额、方向、状态、拒单原因和提交时间。 |
| `fill` | 事件 | 一个 `EventFillInfo`：订单编号、标的、方向、数量、成交价、成交时间和费用。 |
| `position` | 事件 | 一个 `EventPositionSnapshot`：时间、现金、账户总值、持仓数量和可卖数量。 |
| `audit` | 事件 | 事件引擎审计记录，例如 `SESSION_OPEN`、`SCHEDULED_CALLBACK`、`ORDER_REJECTED`、`FILL`、`SESSION_CLOSE`。 |

时间线按 `timestamp` 升序排列；同一时间点按事件引擎阶段排序。事件策略的完整订单/成交/快照/审计原始数组仍分别位于 `orders`、`fills`、`event_positions`、`events`，前端不需要同时消费这些字段。

## 响应字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `metrics` | object | 标准绩效指标。指标无法计算时，对应值为 `null`；自由策略没有因子值和远期收益，因此 `ic`、`rank_ic` 通常为 `null`。 |
| `equity_curve` | object[]/`null` | 策略每日账户权益和回撤序列；`value` 为金额，起点通常等于 `initial_capital`。 |
| `drawdowns` | object[] | 最大的至多 10 段回撤，按回撤深度（最负优先）排序。无回撤时为空数组。 |
| `annual_returns` | object[] | 按自然年计算的区间收益。某年不足两个有效交易日时不返回该年。 |
| `date_range` | object | 实际用于计算的净值序列起止日期；没有有效净值时两个字段均为 `null`。 |
| `final_equity` | number/`null` | 回测结束日的策略账户权益金额。 |
| `benchmark_curve` | object[]/`null` | 基准收盘价按首日归一化后的净值和回撤序列，首个 `value` 为 `1.0`。 |
| `excess_return` | number/`null` | 策略区间收益减去同期基准区间收益，不是逐日超额收益序列。 |
| `meta` | object/`null` | 本次回测实际采用的策略、股票池、基准和 `engine_mode` 信息。执行失败时为 `null`。 |
| `positions` | object[]/`null` | 仅当请求 `include_positions=true` 且存在持仓时返回；为控制体积，最多采样 60 个日期，每日只保留权重最高的 20 个标的。 |
| `rebalance_logs` | object[]/`null` | 仅向量化模式在请求 `include_rebalance_logs=true` 且有调仓记录时返回，按调仓日聚合；事件模式不将订单或成交伪装成目标权重日志。 |
| `execution_timeline` | object[]/`null` | 仅当请求 `include_execution_timeline=true` 时返回，条目为 `{timestamp, type, payload}`，按执行顺序排列；Web 应优先消费此字段。没有可序列化的调仓/执行记录时为 `null`。 |
| `orders` / `fills` / `event_positions` / `events` / `assumptions` | object[]/object/`null` | 仅事件模式返回：订单、成交、账户快照、调度审计轨迹和执行假设。 |
| `error` | string/`null` | 业务执行错误信息；成功时为 `null`。 |
| `error_code` | string/`null` | 机器可读的错误分类。目前只有 `unsupported_api`：聚宽策略调用了兼容层未实现的 API。其他错误为 `null`。 |
| `unsupported_api` | string/`null` | 仅 `error_code=unsupported_api` 时有值，给出具体 API 与原因，便于调用方在提交前做预检。 |

当使用资产快照时，`meta.universe` 为资产 ID，`meta.universe_asset`、`meta.benchmark_asset` 返回解析版本、内容哈希和成分数量。事件驱动模式当前拒绝 `universe_snapshot`、`benchmark_snapshot`，以避免错误宣称已经支持同一资产接入。

### `metrics` 指标

| 字段 | 含义 |
|------|------|
| `annual_return` | 日均策略收益乘以 252 得到的年化收益率。 |
| `total_return` | 整个回测区间的累计收益率，等于末日权益除以首日权益减 1。 |
| `sharpe` / `sortino` | 无风险利率为 0 的年化夏普比率 / 索提诺比率。 |
| `calmar` | 年化收益率除以最大回撤绝对值。 |
| `max_drawdown` | 区间最大回撤，使用负数表示回撤。 |
| `volatility` | 按 252 个交易日年化的收益率波动率。 |
| `win_rate` | 日收益为正的交易日占比。 |
| `turnover` | 相邻日期持仓权重变化计算的平均单边换手率。 |
| `ic` / `rank_ic` | 因子值与远期收益的截面 Pearson / Spearman 相关均值；仅有可用因子数据时才可能有值。 |

### 嵌套对象

| 对象 | 字段 | 说明 |
|------|------|------|
| 净值点（`equity_curve`、`benchmark_curve`） | `date`、`value`、`drawdown` | 日期为 `YYYY-MM-DD`；`drawdown` 是相对历史最高值的回撤，取值小于等于 0。 |
| 回撤段（`drawdowns`） | `start`、`end`、`peak`、`trough`、`recovery`、`depth`、`days` | `start` 是高点日，`end` 是最低点日，`recovery` 为恢复到此前高点的日期；未恢复则为 `null`。`days` 为从高点到恢复日（未恢复时到回测末日）的自然日数。 |
| 年度收益（`annual_returns`） | `year`、`return_value` | 自然年份及该年首末有效净值点计算的收益率。 |
| 元信息（`meta`） | `strategy`、`universe`、`benchmark`、`engine_mode` | 实际执行的策略类名、股票池、基准和回测引擎模式。 |
| 已保存策略元信息（`meta`） | `strategy_id`、`strategy_version`、`code_hash` | 仅通过 `strategy_id` 运行时有值：策略 ID、实际使用版本和源码 SHA-256 哈希；直接提交源码时均为 `null`。 |
| 运行标识（`meta`） | `data_release_id`、`spec_hash`、`run_id` | `data_release_id` 为本次实际绑定的行情发布（与因子回测同口径）；`spec_hash` 是「请求参数 + 数据发布」的 SHA-256，不含运行期 id，同一输入可复现；`run_id` 为本次任务 id（同步接口同样返回），用于把报告与运行日志对应。三者都由引擎真实返回，调用方不应从页面 `current` 推断。 |
| 过滤开关回显（`meta`） | `filter_suspended`、`filter_st`、`filter_limit_up`、`filter_limit_down` | 本次请求的四个过滤开关原样回显。 |
| 过滤诊断（`meta.filter_status`） | `is_suspended`、`is_st`、`is_limit_up`、`is_limit_down` | 四项过滤各自的执行状态：`disabled`（开关关闭或字段缺失且被跳过）、`skipped_missing_field`（字段缺失，放行）、`enabled` / `enabled_partial`（生效，后者表示部分状态值缺失并按放行处理）；事件模式下前三个常为 `matcher_always_on`，表示由撮合层无条件拦截、不受开关影响。 |
| 持仓快照（`positions`） | `date`、`holdings` | `holdings` 是 `{symbol, name, weight}` 数组。`symbol` 为带交易所后缀的代码（如 `000001.SZ`），`name` 为证券名称，`weight` 为目标持仓权重。 |
| 调仓日志（`rebalance_logs`） | `date`、`trades` | `trades` 为 `{symbol, name, side, previous_weight, target_weight, weight_change}` 数组；`symbol` 与 `name` 的口径同持仓快照，`side` 取 `buy` 或 `sell`。 |
| 执行时间线（`execution_timeline`） | `timestamp`、`type`、`payload` | 向量化策略的 `type` 为 `rebalance`，`payload.trades` 与调仓日志字段一致；事件策略的 `type` 为 `order`、`fill`、`position` 或 `audit`，完整明细位于 `payload`。 |

## 业务错误响应示例

策略源码、策略目录、数据或回测执行出错时，接口仍返回 HTTP `200`，由调用方检查 `error` 判断是否成功；此时其他结果字段使用默认空值，`meta` 为 `null`。

```json
{
  "metrics": {},
  "equity_curve": null,
  "drawdowns": [],
  "annual_returns": [],
  "date_range": {"start": null, "end": null},
  "final_equity": null,
  "benchmark_curve": null,
  "excess_return": null,
  "meta": null,
  "positions": null,
  "rebalance_logs": null,
  "execution_timeline": null,
  "orders": null,
  "fills": null,
  "event_positions": null,
  "events": null,
  "assumptions": null,
  "error": "ValueError: 策略代码必须且只能定义一个包含 generate_signals 方法的类",
  "error_code": null,
  "unsupported_api": null
}
```

聚宽策略调用了不支持的 API 时，`error_code` 为 `unsupported_api`，`unsupported_api` 给出具体原因（如 `兼容层不支持 get_call_auction：集合竞价数据未发布`）；调用方据此提示用户改写代码，而不是把所有失败都当成数据或代码语法错误。

```json
{
  "error": "NotImplementedError: 兼容层不支持 get_call_auction：集合竞价数据未发布",
  "error_code": "unsupported_api",
  "unsupported_api": "兼容层不支持 get_call_auction：集合竞价数据未发布"
}
```

请求体不符合参数约束（例如 `strategy_code` 与 `strategy_id` 同时提供、必填二选一缺失，或包含未知字段）时，FastAPI 返回 HTTP `422`，不使用上述业务错误结构。

`strategy_params`、`factor_code`、`factor_name`、`data_dir`、`backtest_engine`、`include_equity` 和未知字段会返回 `422`。策略代码示例见 [因子与策略指南](../../../用户指南/因子与策略指南/README.md)。

### 内置因子引用（`factor_refs`）

```jsonc
{
  "factor_refs": [
    {"library": "主因子库", "factor": "SV01_0005", "direction": "positive"},
    {"library": "主因子库", "factor": "SV01_0010", "direction": "negative"}
  ],
  "top_n": 30,
  "rebalance_days": 20
}
```

- 因子通过当前 SQLite 内置因子库加载（`library` + `factor`，`factor` 可填 `sv_id` 或因子名），不执行调用方源码。
- 每个因子按 `direction` 取正/负号 → 截面 zscore → 等权平均，作为 `FactorPortfolioStrategy` 的 `@signal` 分数；选股与调仓沿用 `top_n` / `rebalance_days`。
- 响应 `meta.factor_refs` 回显引用清单，`meta.strategy` 为 `FactorRefsStrategy`，`meta.code_hash` 为引用清单的稳定哈希。
- `factor_refs` 与 `strategy_code`/`strategy_id` 互斥；`top_n`/`rebalance_days` 只能与 `factor_refs` 一起使用；事件模式和聚宽方言暂不支持。

### 公司行为（分红送转）

- `strategy_dialect="joinquant"` 且 `execution_price_source="unadjusted"` 时，事件引擎按**现金分红**记账：事件日按税前 `interest/10 × 持股数` 预扣 20% 红利税后计入现金、`stockBonus/10` 增加股数，现金不自动再投资，等策略自行下单；`assumptions.corporate_action_adjustment = "cash_dividend_net_20pct_tax"`。
- 其余组合（native 方言，或复权价格来源）保持原有口径：未复权时按 `hfq/unadj` 比例缩放持仓（等价分红再投资），复权时公司行为已包含在价格中。
- 分红事件明细包含 `cash_dividend`（税后入账）、`cash_dividend_gross`（税前）和 `dividend_tax`（预扣税额）；送股不足 1 股向下取整；分红事件数据缺失时回退再投资口径并在 `assumptions` 标注。

### 分红与估值口径

- `finance.STK_XR_XD.a_registration_date` 优先映射数据侧的 `registration_date`（股权登记日），缺失时才回退到 `time`（除权日）；依赖 trailing 365 天分红的策略按登记日计算，避免窗口边界多算/少算一笔。
- `valuation.market_cap` 优先使用数据侧已按 `total_mv → float_mv` 回退的 `market_cap` 字段；旧数据集没有该列时回退到 `total_mv`。

### 股票池口径

- `get_all_securities(types=['stock'])` 已排除 B 股（`200xxx` / `900xxx`），与聚宽 stock 类型一致；主数据的 ALL_A 曾混入 22 只 B 股，不剔除会进入 A/B 池候选。

### 聚宽行情读取（`get_price`）

- 默认 `skip_paused=False`，与聚宽一致：停牌日保留在时间轴上，价格用前收盘价填充（由行情源提供），`volume`/`amount`（`money`）填 0；`skip_paused=True` 时剔除停牌行。否则策略按 `count` 或窗口统计成交额时会把停牌日算丢，导致流动性过滤误杀停牌股。

## 异步提交（202 + 轮询）

聚宽策略回测通常要跑几十分钟到数小时，超过 Web 网关的同步超时。需要等待长任务时改用异步接口；请求体与 `POST /api/backtest/strategy` 完全一致。

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/backtest/strategy/async` | 提交任务，立即返回 `202` |
| GET | `/api/backtest/strategy/async/{run_id}` | 查询状态，终态时带上完整结果 |
| DELETE | `/api/backtest/strategy/async/{run_id}` | 请求取消；排队中的任务直接取消，运行中的只记录取消意图 |

提交响应：

```json
{
  "run_id": "6f0f2a0b6b4f0f8f2f5a1f2c3d4e5f",
  "status": "queued",
  "poll_url": "/api/backtest/strategy/async/6f0f2a0b6b4f0f8f2f5a1f2c3d4e5f"
}
```

同时返回 `Location` 头，值与 `poll_url` 相同。轮询响应的字段固定为 `run_id`、`status`、`created_at`、`started_at`、`finished_at`、`cancel_requested`、`error`、`result`；`status` 取 `queued`/`running`/`succeeded`/`failed`/`cancelled`，`result` 是同步接口的完整响应体（含 `metrics`、`equity_curve`、`orders`、`fills`、`error_code` 等）。

约定：

- 策略任务的结果保留 **24 小时**，与同步接口的 60 秒不同；过期后轮询返回 `404`。
- `run_id` 是进程内任务 ID，服务重启即失效；跨重启的历史归档由调用方自行保存结果。
- 队列满返回 `429`（带 `Retry-After`），Manager 关闭返回 `503`。
- 上游网关不要为 `POST .../async` 设置长超时；按 `poll_url` 轮询，建议 1–5 秒间隔。

## 能力清单（提交前预检）

`GET /api/strategy-capabilities` 返回兼容层支持的方言、引擎模式、频率、价格来源以及 API 支持/不支持清单。响应不依赖数据引擎，服务刚启动时也可用；能读到数据引擎时附带 `release_id`、`data_version`、`data_dir`。

```json
{
  "version": "2026-11-11",
  "dialects": {
    "native": {"engine_modes": ["vectorized", "event"]},
    "joinquant": {
      "engine_modes": ["event"],
      "frequency": ["1d"],
      "timed_execution": true,
      "price_sources": ["unadjusted"],
      "supported_apis": ["g", "log", "run_daily", "get_price", "..."],
      "unsupported_apis": [
        {"api": "get_call_auction", "reason": "集合竞价数据未发布"},
        {"api": "get_price(frequency='1m')", "reason": "分钟数据仅供 timed_execution 内部使用"}
      ]
    }
  },
  "data": {"release_id": "39e86da5...", "data_version": "...", "data_dir": "/data/marketdata"}
}
```

WebApp 应在策略创建页保存或缓存该清单，提交前对源码做粗检；`unsupported_apis` 中的 API 即使漏检也会在回测响应里以 `error_code=unsupported_api` 结构化返回。
