# 聚宽对账基线

用同一策略在聚宽与 SVQuant 事件引擎各跑一次，分层比对并定位首个分歧。设计见
`docs/功能包/FEAT-034-聚宽对账基线/`。

## 加一个用例

1. 新建 `cases/<case>/`，放入：
   - `case.yaml`：两侧参数、区间、容差、`price_basis`；
   - `jq_strategy.py`：聚宽侧源码，人工粘贴到聚宽平台；
   - `svquant_strategy.py`：SVQuant 事件策略。
2. 在聚宽平台按 `case.yaml` 的参数创建回测并运行。
3. 导出金标，二选一：
   - **页面导出（推荐）**：下载"收益概述 / 交易详情 / 持仓&收益"三个 CSV，
     然后转换到 `cases/<case>/expected/`：
     ```bash
     python -m app.cli.joinquant_parity import-golden \
       --case <case> --from /path/to/聚宽导出目录
     ```
   - **API 导出**：在聚宽研究环境运行 `jq_export.py`，下载到 `expected/`。
4. 校验并比对：

```bash
python -m app.cli.joinquant_parity validate --case <case>
python -m app.cli.joinquant_parity run --case <case>
python -m app.cli.joinquant_parity compare --case <case>
```

## 现有用例

| 用例 | 资金 | 验收 | 用途 |
|---|---|---|---|
| `fixed_basket_hold` | 100 万 | strict | 不复权成交 + 复权比例缩放；验证整手与现金拖累 |
| `fixed_basket_hold_large` | 1 亿 | strict | 大资金回归门禁，整手误差可忽略 |
| `fixed_basket_hold_real` | 1 亿 | strict | 聚宽 `use_real_price=True` 对 SVQuant `unadjusted`；股数、成交价、现金、费用逐笔严格一致（区间避开除息日） |
| `company/growth_multifactor` | 20 万 | diagnostic | 公司策略成长多因子；兼容层原样执行，验证财务/jqfactor/周频调度 |
| `company/dividend_lowvol_growth` | 10 万 | diagnostic | 红利低波+红利成长；验证分红、行业、涨跌停与委托查询 |
| `company/three_positive_smallcap` | 100 万 | diagnostic | 三正小市值；等待 399317 指数成分（P1-1） |

公司策略按 `cases/company/<strategy>/` 分组，`--case` 传相对路径，例如
`--case company/growth_multifactor`；`company/registry.yaml` 记录 13 个策略的数据依赖与状态。

## 验收口径

`case.yaml` 的 `comparison.acceptance` 决定 pytest 如何处理该用例：

- `strict`（默认）：所有严格层必须通过，用作回归门禁；
- `diagnostic`：只要求比对可执行，差异由 `report.md` 呈现，用于正在定位的用例。

## 口径约定

- 聚宽侧显式 `set_option('use_real_price', False)` 与 `set_slippage(PriceRelatedSlippage(0.0))`，
  避免平台默认值（前复权、0.00246 滑点）污染对比。
- SVQuant 侧 `execution_timing=next_open`，与聚宽日频 9:30 撮合对齐。
- 两侧都用"第 2 个交易日建仓"，不依赖各平台历史数据可用范围。
- `price_basis=hfq` 时，价格、股数、现金只作参考；成交事件、持仓集合、收益率仍严格比较。

## 合规

聚宽导出文件只在本机使用，不提交、不分发；`expected/` 已在 `.gitignore` 中。
