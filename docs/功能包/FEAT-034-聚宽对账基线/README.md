# FEAT-034：聚宽对账基线

## 状态

进行中：工具链已实现并通过合成数据测试；首个真实金标待用合法聚宽账号导出。

## 关联

- [FEAT-006 事件驱动策略回测](../FEAT-006-事件驱动策略回测/README.md)：被对账的事件引擎。
- [FEAT-009 聚宽兼容因子库](../FEAT-009-聚宽兼容因子库/README.md)：聚宽数据合规定位。
- [FEAT-033 AI 策略转写](../FEAT-033-AI策略转写/README.md)：转写策略的数值验证需求。

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

## 首个真实结论（2024-01-02 ~ 2024-06-28，固定 4 只银行股）

| 用例 | 资金 | 累计收益 聚宽 / SVQuant | 差异 | 结论 |
|---|---|---|---|---|
| `fixed_basket_hold` | 100 万 | 20.3917% / 20.4092% | 0.0175% | **strict 通过**，L0–L5 零严格分歧 |
| `fixed_basket_hold_large` | 1 亿 | 20.4276% / 20.4375% | 0.0099% | **strict 通过**，L0–L5 零严格分歧 |
| `fixed_basket_hold_real` | 1 亿 | 17.367642% / 17.367641% | <0.00001% | **strict 通过**；聚宽 `use_real_price=True` 对 SVQuant `unadjusted`，L2/L3 股数、成交价、现金、佣金全部逐笔一致 |

`fixed_basket_hold_real` 的终值两侧都是 `117,367,641.50`，建仓日现金都是 `4,973,166.50`，四笔成交的股数、价格、手续费完全相同。该用例区间截至 2024-06-13，避开 `000001.SZ` 除息日，专门验证撮合与账户口径。

事件引擎已支持 `execution_price_source: unadjusted`：信号仍用后复权，成交、估值、整手、佣金和现金用未复权价格；持仓每日按 `hfq_close / unadjusted_close` 的比例缩放，等价于分红送转再投资。改造前 100 万用例因后复权价格放大整手取整，累计差 0.40%；改造后降到 0.0175%，不需要新增公司行为数据。

L2/L3 的股数与价格仍为参考差异：聚宽 `use_real_price=False` 按前复权价计算股数，SVQuant 按未复权价计算；成交事件、持仓集合、收益率与指标一致。细节见 `03-technical-design.md` 第 5 节。
