# FEAT-034：聚宽对账基线

## 状态

> **2026-11-02 暂停**：公司策略源码目录里每个策略都有根目录版与「最新」目录版，此前四个用例的代码与金标来源未做版本配对（3 个用例是最新代码配旧导出，成长是旧代码配旧导出）。现有四份公司策略报告**不再作为对齐结论**，只保留为引擎可运行性的证据。等策略组按最新代码导出完整三件套后，统一重建用例再重跑。见 [04-13策略对齐路线](04-13策略对齐路线.md) 第 14 节。

进行中：对账工具链与聚宽兼容层已实现；固定篮子用例 strict 通过。13 个策略的路线与数据缺口见 [04-13策略对齐路线](04-13策略对齐路线.md)。

数据职责已交接给 `svquant-data`：P0 完成后 RE 通过 `views/current` 的 `ifind` 与 `free/compat` 读取同一发布视图；交接工作包与验收见 `svquant-data/docs/HANDOFF-ResearchEngine.md`。

## 关联

- [FEAT-006 事件驱动策略回测](../FEAT-006-事件驱动策略回测/README.md)：被对账的事件引擎。
- [FEAT-009 聚宽兼容因子库](../FEAT-009-聚宽兼容因子库/README.md)：聚宽数据合规定位。
- [FEAT-033 AI 策略转写](../FEAT-033-AI策略转写/README.md)：转写策略的数值验证需求。

## 性能与迁移

回测性能诊断、引擎目标形态与策略组接入方式见 [07-回测性能与策略迁移方案](07-回测性能与策略迁移方案.md)。

## 因子口径文档

13 个策略的 `get_factor_values` / `get_fundamentals` 口径参考：

- [聚宽基本面及量价因子清单](聚宽基本面及量价因子清单.md)：230 个因子。
- [聚宽技术分析因子_三平台公式对照](聚宽技术分析因子_三平台公式对照.md)：98 个 TA 指标。
- [聚宽风格因子清单](聚宽风格因子清单.md)：风险模型 10 个风格因子与描述因子（含 `earnings_growth`）。

## 目标

回答"我们的回测与聚宽差在哪、差多少"。用同一策略在聚宽与 SVQuant 事件引擎各跑一次，按参数、日历、标的、成交、持仓、净值、指标逐层比对，输出首个分歧，作为后续修正回测口径的验收基线。

## 非目标

- 不替换 `EventBacktestEngine`，不修改撮合、估值与指标实现。
- 不新增 HTTP 路由，不改变现有 API 契约。
- 不抓取、转存或分发聚宽数据；金标只在本地使用且不入库。
- 首批只覆盖 A 股日频、固定篮子持有；分钟、ETF、期货后续另立用例。

## MVP 范围

| 能力 | 说明 |
|---|---|
| 用例声明 | `tests/parity/cases/<case>/case.yaml` 声明两侧参数、区间、容差与口径 |
| 金标导入 | `expected/` 放聚宽导出的 daily/positions/orders/balances；目录不入库 |
| SVQuant 运行 | 进程内调用 `run_strategy_backtest`，不要求服务在线 |
| 分层比对 | L0 参数/日历、L1 标的、L2 成交、L3 持仓、L4 净值、L5 指标 |
| 报告 | markdown + 差异 CSV，给出首个严格分歧与口径提示 |
| 自动回归 | pytest 发现用例；金标或行情缺失时 skip |

## 使用

```bash
# 1a. 页面导出：把收益概述/交易详情/持仓&收益 CSV 放到同一目录后导入
python -m app.cli.joinquant_parity import-golden \
  --case fixed_basket_hold --from /path/to/聚宽导出目录
# 1b. 或者在聚宽研究环境运行 tests/parity/jq_export.py，下载到 expected/
# 2. 校验金标
python -m app.cli.joinquant_parity validate --case fixed_basket_hold
# 3. 运行本仓库侧并落盘
python -m app.cli.joinquant_parity run --case fixed_basket_hold
# 4. 比对，报告写到 output/joinquant-parity/<case>/<时间戳>/
python -m app.cli.joinquant_parity compare --case fixed_basket_hold
```

退出码：`0` 严格层通过；`1` 存在严格分歧；`2` 输入/配置/金标错误。

## 验收

1. 合成数据单测覆盖符号映射、列校验、L0–L5 与首个分歧定位。
2. `fixed_basket_hold` 在两侧各跑一次后能生成报告，并明确指出首个严格分歧（即使结果不为零）。
3. 缺金标或缺行情时 `pytest tests/parity` 只 skip 端到端用例，其余用例照常运行。
4. 对账工具不进入生产依赖：`app.main`、`app.api` 不导入 `app.services.parity`。

## 公司策略用例

公司策略源码快照在 `tests/parity/cases/company/<strategy>/jq_strategy.py`，通过聚宽兼容层原样执行；金标仍在本地 `expected/`（gitignore）。

| 用例 | 区间 | 数据依赖 | 状态 |
|---|---|---|---|
| `growth_multifactor` | 2019-01-02 ~ 2026-08-12 | 财务 PIT、jqfactor、全市场 | 已出报告（2014–2015 财务补齐后重跑，`output/joinquant-parity/growth_multifactor/20261102T024904Z/report.md`）：L0 通过，1846 个交易日；首个严格分歧是 002591.SZ 建仓日（聚宽 2019-02-18 / SVQuant 2019-01-02）；持仓重合 63/144（补齐前 59/144），终值 127.6 万 vs 聚宽 200.1 万。注意：用例代码是根目录旧版，与最新版 `成长多因子策略-1.py` 不同 |
| `dividend_lowvol_growth` | 2017-01-12 ~ 2026-07-21 | 财务 PIT、分红事件、行业、涨跌停 | 已出报告（2310 个交易日，`output/joinquant-parity/dividend_lowvol_growth/20261101T112457Z/report.md`）：L0 通过（基准 000015 无行情，用例声明等价 CSI300）；首个严格分歧在 L1 选股集合，聚宽持有 000002.SZ 而 SVQuant 未持有；全区间运行 35 分钟 |
| `pb_break` | 2020-01-02 ~ 2026-07-23 | 财务 PIT、分红、自定义 `calc_factors`、ETF 511880 | 已出报告（1588 个交易日，`output/joinquant-parity/pb_break/20261101T131744Z/report.md`）：L0 通过；首个严格分歧在 L1 的 `first_holding_date`（000623.SZ 聚宽 2020-01-02 / SVQuant 2021-01-04）。归母口径修正后持有标的与聚宽重合 38/120（修正前 29/66），全区间终值 2.19M vs 聚宽 6.82M；ETF 511880 暂无行情，现金管理登记为已知偏差 |
| `three_positive_smallcap` | 2019-01-02 ~ 2026-08-05 | 399317 成分、财务 PIT、市值分层 | 已出报告（1841 个交易日、2526 笔成交，`output/joinquant-parity/three_positive_smallcap/20261101T131808Z/report.md`）：L0 通过；首个严格分歧在 L1 的 `first_holding_date`（002205.SZ 聚宽 2019-01-02 / SVQuant 2020-01-13）；持有标的重合 259/407，全区间终值 24.9M vs 聚宽 7.98M |

其余 9 个策略的阻塞数据缺口与解锁顺序见 [04-13策略对齐路线](04-13策略对齐路线.md)；2026-11-01 的根因排查结论见该文档第 13 节。

**数据侧待授权/待补**（由 `svquant-data` 登记）：

- 2014–2015 财务历史：ModelScope 起点是 2016-03-31，`earnings_growth` 需要 5 年年报；补齐需 iFinD 财务报表历史导出，当前 `blocked_by: data-source`。
- 指数覆盖：`399317/399101/399303` 已由免费源交付（2016-11-01 起，规则重建、`confidence=medium`）；`000015` 只有 2026-09-30 起的官方锚点段，历史 unresolved。
- 历史涨跌停价、退市/停复牌：P1-3。

## 价值类 v3 用例（策略组 2026-11-03 最新版）

源码 `tests/parity/cases/value_v3/<case>/jq_strategy.py`，金标为聚宽导出（2428 个交易日，2016-09-30 ~ 2026-09-30）。持仓重合按「每月最后一个持仓快照」统计，收益是 L5 的 `total_return`。

| 用例 | 资金 | 持仓重合（按金标 / 按我方） | 累计收益（聚宽 / SVQuant） | 报告 |
|---|---|---|---|---|
| `dividend_v3_10w` | 10 万 | 79.4% / 90.2%（单季口径与送股换算修复前 59.5% / 56.9%） | 397.5% / 313.4% | `output/joinquant-parity/dividend_v3_10w/20261105T035353Z/report.md` |
| `dividend_v3_100w` | 100 万 | 79.1% / 90.0%（修复前 59.2% / 56.9%） | 568.6% / 447.2% | `output/joinquant-parity/dividend_v3_100w/20261105T035535Z/report.md` |
| `pb_v3` | 10 万 | 70.5% / 73.4%（ROA 修正前 56.3% / 60.1%） | 518.4% / 402.5% | `output/joinquant-parity/pb_v3/20261105T041609Z/report.md` |
| `fcf_v3` | 10 万 | 76.8% / 84.1%（行业剔除修复前 57.7% / 65.8%） | 573.0% / 343.9% | `output/joinquant-parity/fcf_v3/20261104T075737Z/report.md` |

四个用例的 L0 全部通过，首个严格分歧都在 L1 的选股集合/建仓日；L5 五项指标全部不满足容差。

**已定位的口径问题**

- `indicator.roa` 在聚宽是「单季净利润（含少数）/ 平均总资产 × 100」，RE 原先直接用源表累计 ROA。2026-11-03 改为单季口径后，与聚宽导出的秩相关 0.89 → **0.996**，两位小数一致率 93.7%；但破净 v3 持仓重合只从 56.3% 升到 57.9%，说明它不是收益差的主因。
- `calc_factors` 自定义 FF8：聚宽 jqfactor 的依赖项是**单季度**口径，源表 `pershare_index` / 利润表流量字段是年内累计。2026-11-04 改为按财报重算单季度 ROE（单季净利/平均全部权益）、ROA（单季净利/平均总资产）、毛利率（单季营业收入−营业成本），`total_profit`/`financial_expense` 按年内差分。用聚宽导出逐值核对：8 项布尔一致率 74%–99% → **88%–99%**，依赖原值中位偏差 0%–2.3%。破净 v3 持仓重合 62.2% → **66.2%**（按金标口径），收益 220.4% → 278.6%。
- `indicator.gross_profit_margin` 秩相关 0.94，但两位小数只有 33% 一致，极值处差异可达数倍，尚未确认是单季/累计口径还是营业成本定义不同。
- 破净 v3 的 ETF 现金管理（511880）：2026-11-04 已接入事件引擎（`extra_symbols` + 视图 `free/etf_daily`，聚宽代码归一、卖出免印花税）；2026-11-05 已在 `research-rv-20261104-1` 全区间重跑，重合 70.5%（按金标）、收益 402.5%。
- 红利 v3 的两处口径修复（2026-11-05）：聚宽 `indicator` 字段是单季度口径（此前按累计值过滤 B 池），以及送股派息换算把送股部分计入现金股息（`bonus_amount_rmb` 需按除权前股本还原）；同时把聚宽方言预热从 400 天扩到 1200 天，覆盖 780 日波动率窗口。全区间重跑后 10 万/100 万重合 79.4%/79.1%，收益 313.4%/4472.4%；剩余差异的主因是 2016–2024 分钟线缺失导致的补单窗口降级（数据侧 P0）。
- 行业分类：数据侧已交付 `derived/industry_membership_history`（PIT，2016-01-19 起，与聚宽月度快照不一致率 0.14–0.18%），兼容层已优先使用。**对价值类 v3 的选股无影响**：2019 全年破净/红利在 PIT 与 latest_only 下持仓逐字节相同（行业约束未触发）。同时修复了 `get_industry` 返回 `801780.SI` 导致自由现金流 v3 银行/地产剔除失效的问题——修复后自由现金流 v3 重合率 57.7% → **76.8%**（按金标），收益 217.1% → 343.9%。

## 首个真实结论（2024-01-02 ~ 2024-06-28，固定 4 只银行股）

| 用例 | 资金 | 累计收益 聚宽 / SVQuant | 差异 | 结论 |
|---|---|---|---|---|
| `fixed_basket_hold` | 100 万 | 20.3917% / 20.4092% | 0.0175% | **strict 通过**，L0–L5 零严格分歧 |
| `fixed_basket_hold_large` | 1 亿 | 20.4276% / 20.4375% | 0.0099% | **strict 通过**，L0–L5 零严格分歧 |
| `fixed_basket_hold_real` | 1 亿 | 17.367642% / 17.367641% | <0.00001% | **strict 通过**；聚宽 `use_real_price=True` 对 SVQuant `unadjusted`，L2/L3 股数、成交价、现金、佣金全部逐笔一致 |

`fixed_basket_hold_real` 的终值两侧都是 `117,367,641.50`，建仓日现金都是 `4,973,166.50`，四笔成交的股数、价格、手续费完全相同。该用例区间截至 2024-06-13，避开 `000001.SZ` 除息日，专门验证撮合与账户口径。

事件引擎已支持 `execution_price_source: unadjusted`：信号仍用后复权，成交、估值、整手、佣金和现金用未复权价格；持仓每日按 `hfq_close / unadjusted_close` 的比例缩放，等价于分红送转再投资。改造前 100 万用例因后复权价格放大整手取整，累计差 0.40%；改造后降到 0.0175%，不需要新增公司行为数据。

L2/L3 的股数与价格仍为参考差异：聚宽 `use_real_price=False` 按前复权价计算股数，SVQuant 按未复权价计算；成交事件、持仓集合、收益率与指标一致。细节见 `03-technical-design.md` 第 5 节。
