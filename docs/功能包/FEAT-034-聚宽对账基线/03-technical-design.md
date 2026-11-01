# 聚宽对账基线：技术设计

## 1. 模块边界

```text
app/services/parity/          纯数据变换与比对，不导入回测引擎
  schema.py                   列别名、符号映射、容差、用例声明
  loader.py                   case.yaml 与聚宽 CSV 的读取/校验
  normalizer.py               StrategyBacktestResponse -> 统一帧
  compare.py                  L0–L5 比对
  report.py                   markdown / CSV
  runner.py                   构造请求、进程内运行 SVQuant、装配 DataEngine
app/cli/joinquant_parity.py   CLI
tests/parity/                 用例、金标、导出脚本、测试
```

- `app.services.parity` 只依赖 pandas、pydantic schema 与指标引擎；`runner.py` 是唯一导入 `run_strategy_backtest` 的模块。
- `app.main`、`app.api` 不导入 `app.services.parity`，对账工具不进入生产路径。
- 不新增 HTTP 路由。

## 2. 金标来源

支持两种聚宽导出，二者最终都转换成 `expected/` 下的统一契约：

1. **页面导出**（`import-golden`）：`result_*.csv`（收益概述）、`transaction.csv`（交易详情）、`position.csv`（持仓&收益）。
   - 编码为 GBK；持仓文件股票行比表头多一列"总资产"（位于仓位占比之前），现金行只有 16 列，转换器按列数区分。
   - 代码格式为 `名称(000001.XSHE)`；数量带"股"后缀；收益概述的"策略收益"是累计百分比。
   - 权益优先取持仓行的总资产，缺失时用累计收益还原；现金取 Cash 行的"市值/价值"。
2. **API 导出**（`jq_export.py`）：`get_backtest()` 的 `get_results/get_positions/get_orders/get_balances`。

两者都写出 `daily.csv`、`balances.csv`、`positions.csv`、`orders.csv`、`meta.json`。金标目录整体不入库。

## 3. 用例与金标契约

```text
tests/parity/cases/<case>/
  case.yaml            入库：两侧参数、容差、price_basis
  jq_strategy.py       聚宽侧源码（人工粘贴到平台）
  svquant_strategy.py  SVQuant 事件策略
  expected/            不入库：聚宽导出
    meta.json          backtest_id、exported_at、params
    daily.csv          time, returns[, benchmark_returns]
    balances.csv       time, cash, total_value（可选）
    positions.csv      time, security, amount[, closeable_amount]
    orders.csv         time, security, action, filled, price, commission[, status]
```

规范化规则：

- 符号 `600519.XSHG -> 600519.SH`、`000001.XSHE -> 000001.SZ`、`430047.XBSE -> 430047.BJ`。
- 日期只保留 `YYYY-MM-DD`；成交方向 `open/close -> buy/sell`。
- `daily.csv` 缺失权益时按 `initial_cash × cumprod(1+returns)` 重建，首日收益置 0。
- 列名通过别名表匹配；缺列直接报错并打印实际列，避免静默错位。

## 4. 分层比对

| 层 | 内容 | 严格性 |
|---|---|---|
| L0 | 初始资金、佣金、最低佣金、印花税、滑点、基准映射、交易日历 | 恒严格 |
| L1 | 交易标的集合、首次持仓日期 | 恒严格 |
| L2 | 成交事件 (date, symbol, side)、数量、价格、费用 | 事件集合严格；数值仅在 `price_basis=real` 时严格 |
| L3 | 每日持仓集合、数量、现金 | 集合严格；数值仅在 `price_basis=real` 时严格 |
| L4 | 日收益、累计收益 | 恒严格 |
| L5 | 总收益、年化、最大回撤、波动、Sharpe | 恒严格；绝对差与相对差**同时**超限才判失败（`metric_abs`/`sharpe_abs` + `metric_rel`），避免把 0.0003 的回撤差异误报 |

设计要点：

1. **口径差异不掩盖真实分歧**。复权基准不同会让价格/股数/现金不可比，但"哪天成交、持有哪只、每天收益多少"仍然可比，因此这些始终严格。
   用例可用 `comparison.acceptance: strict|diagnostic` 声明验收方式：`strict` 要求严格层全过，`diagnostic` 只要求比对可执行。
2. **首个分歧**按 L0→L5、日期、标的排序，报告只把第一条严格分歧放在结论区。
3. **L5 不直接使用聚宽 `get_risk()`**，避免指标定义差异；`get_risk()` 只作为后续参考存档。
4. 容差集中在 `Tolerances`，用例可覆盖；未知容差项报错。

## 5. 复权与成交价口径（重要）

**现状（已实现）**：事件引擎新增 `execution_price_source`：

- `adjusted`（默认）：成交与估值都读复权面板，保持历史行为；
- `unadjusted`：策略信号、`context.data.history` 仍读后复权；成交价、估值价、整手、佣金、现金全部用未复权价格。

**为什么不能只换成交价**：两个价格不在同一量纲。若按未复权价买入、却按后复权价估值，`000001.SZ` 的后复权/未复权比例超过 100 倍，持仓市值会瞬间放大百倍。成交、估值、现金必须使用同一口径。

**方案：未复权记账 + 每日按复权比例缩放股数**

```text
factor_t = hfq_close_t / unadjusted_close_t
ratio_t  = factor_t / factor_{t-1}
N_t      = floor(N_{t-1} × ratio_t)
```

除权日开盘前缩放持仓与可卖数量，取整余数按当日未复权开盘价折算现金（cash in lieu）；平均成本按 `1/ratio` 调整。这样持仓市值 `N_t × unadjusted_close_t` 的涨幅恰好等于后复权收益，等价于“分红送转全部再投资”，**不需要外部公司行为表**。

验证 `000001.SZ` 2024-06-14 除息：复权比例 1.0713（反推每股分红约 0.72 元），31,400 股 → 33,639 股，持仓价值 339,120 → 342,454，**+0.98%**，与后复权收益一致。

**语义边界**

- 分红按再投资处理，不是现金入账；与聚宽 `use_real_price=False`（复权价）完全一致。
- 若与聚宽 `use_real_price=True` 对账，差异是“分红再投资 vs 现金留存”，约为分红率 × 后续涨幅，通常小于 0.1%。
- 缩放产生的小数股向下取整、余数入现金，单次误差小于 1 股 × 价格。
- 复权比例来自两张价格面板，跳变日可反推分红金额，可作为数据质量校验；不需要新增数据表。

**改造后的对账结果（2024-01-02 ~ 2024-06-28）**

| 用例 | 累计收益 聚宽 / SVQuant | 差异 | 建仓日现金占比 |
|---|---|---|---|
| 100 万 | 20.3917% / 20.4092% | 0.0175%（改造前 0.40%） | 5.11% / 5.08% |
| 1 亿 | 20.4276% / 20.4375% | 0.0099% | 4.94% / 4.94% |

上述两个用例的 L2/L3 股数与价格仍列为参考差异，因为聚宽 `use_real_price=False` 按前复权价计算股数，而 SVQuant 按未复权价计算；成交事件、持仓集合、收益率与指标一致。

**真实价严格对账**：新增 `fixed_basket_hold_real`（1 亿，2024-01-02 ~ 2024-06-13，避开 `000001.SZ` 除息日），聚宽 `use_real_price=True` 对 SVQuant `unadjusted`，`price_basis=real`：

- 终值两侧均为 `117,367,641.50`，累计收益 17.367642% vs 17.367641%；
- 建仓日现金均为 `4,973,166.50`；
- 四笔成交的股数、成交价、佣金逐笔一致（如 `000001.SZ` 2,584,300 股 @9.19，佣金 7,124.92）；
- L2 13 项、L3 526 项、L4 107 项、L5 5 项全部零严格分歧。

这验证了真实价模式下的整手、T+1、开盘撮合、最低佣金、现金记账与未复权估值；除息日之后的分红处理仍由前两个复权用例覆盖总收益。

## 6. 成交口径对齐

- 聚宽日频 9:30 运行，只能看到上一交易日数据，撮合价偏开盘口径 → SVQuant 使用 `execution_timing=next_open`。
- 两侧都按"第 2 个交易日"建仓：策略内用调用次数计数，不依赖各平台历史数据的可用性。
- 聚宽侧显式设置 `use_real_price=False`（与 hfq 同属复权序列，收益率可比）和 `PriceRelatedSlippage(0.0)`（关闭默认 0.00246）。
- 预算 95%，给佣金与整手取整留余量；标的选低价、流动性好的大市值股票，降低整手现金拖累。

## 7. CLI 与产物

```bash
python -m app.cli.joinquant_parity list
python -m app.cli.joinquant_parity import-golden --case fixed_basket_hold --from DIR
python -m app.cli.joinquant_parity validate --case fixed_basket_hold
python -m app.cli.joinquant_parity run --case fixed_basket_hold [--data-dir DIR] [--out DIR]
python -m app.cli.joinquant_parity compare --case fixed_basket_hold [--svquant-result PATH]
```

- 退出码：0 通过；1 严格分歧；2 输入/配置/金标错误。
- 默认输出 `output/joinquant-parity/<case>/<UTC 时间戳>/`：
  `report.md`、`divergences.csv`、`svquant_response.json`、`svquant/{daily,positions,fills}.csv`。
- `runner.build_data_engine`：显式 `--data-dir` 时直接 `load_parquet`；否则按 `config.yaml` 校验 manifest 与 release。

## 8. 测试策略

- `tests/parity/test_schema.py`：符号映射、列别名、容差校验、金标加载。
- `tests/parity/test_compare.py`：合成数据覆盖通过、L0–L4 分歧、hfq 参考差异、首个分歧与报告渲染。
- `tests/parity/test_cli.py`：`list/validate` 退出码，不启动服务。
- `tests/parity/test_golden.py`：端到端；`expected/` 或行情不可用时 `pytest.skip`，不阻塞常规测试。

## 9. 风险与缓解

| 风险 | 缓解 |
|---|---|
| 聚宽导出字段变化 | 别名表 + 必需列校验 + 错误打印实际列 |
| 聚宽 `get_orders().commission` 是否含印花税不明 | L2 比较费用合计；卖出用例单独确认字段 |
| hfq 与真实价导致持仓不可比 | `price_basis` 分层严格性；只严格比收益与事件集合 |
| 整手现金拖累 | 95% 预算、低价股、收益容差 1e-3、报告注明 |
| 分红送转处理差异 | `unadjusted` 模式按复权比例缩放股数（再投资口径）；与聚宽现金分红模型的差异作为已知边界记录 |
| 复权比例依赖两张价格面板对齐 | 停牌或单侧缺价时可能漏记公司行为；比例按 `ffill` 延续，跳变日可再用公司行为表/公告交叉校验 |
| 交易日历不一致 | L0 显式列出缺失日期，默认判失败 |
| 聚宽日频撮合价假设不成立 | 首次运行后按实测回填 `case.yaml`，并把成交价来源写入报告 |
| 金标缺失导致 CI 静默通过 | skip 带原因；比较器单测始终运行 |
