# FEAT-032：日终模拟盘（ResearchEngine 侧）

- 状态：已实现（2026-10-22 首期）
- 跨仓库契约：[SVQuant FEAT-032](../../../../SVQuant/docs/03-模块详细设计/FEAT-032-日终模拟盘/README.md)
- 接口：[API 设计](../../系统文档/API设计/API设计.md)

## 职责

ResearchEngine 只做单日确定性结算，不持有账户，不持久化：

1. 用截至 D-1 的行情生成事件/向量化策略信号；
2. 用 D 日 `amount / volume` 的 VWAP 全量成交；
3. 复用事件引擎的现金、整手、T+1、涨跌停、停牌、费用规则；
4. 用未复权 D 日 close 估值并返回下一账户状态；
5. 返回 `orders/fills/next_account/strategy_state/state_hash/warnings`。

SVQuant 负责账户生命周期、账本、幂等、日终调度、重试和用户界面。

## 内部接口

- `GET /api/internal/paper-trading/status?date=YYYY-MM-DD`
- `GET /api/internal/paper-trading/trading-days?start=&end=`
- `POST /api/internal/paper-trading/settle`

## 价格与状态口径

| 用途 | 口径 |
|---|---|
| 信号 | 研究行情（后复权 OHLC），最大可见 D-1 |
| 成交 | `vwap = amount / volume`；无效值 → `NO_VWAP`，不回退 |
| 估值 | `unadjusted_price/unadjusted_price.parquet` 的 D 日 close；缺失回退最近有效值并告警 |
| 交易日 | 优先 `calendar/calendar_all_a.parquet`，超出覆盖范围按工作日乐观判断并由行情面板确认 |
| 策略状态 | 带类型标签 JSON（date/datetime/tuple/NaN/numpy 标量）；initialize 后合并 |

## 首期限制

- 不支持滑点、部分成交、多空、融券、保证金、退市强平和公司行为；
- 结算日不做退市强平，持仓只能由策略主动卖出；
- 内部接口无服务间认证，必须部署在内网/受控入口之后；
- 每次结算会加载 `history_days`（默认 900）交易日面板，并发由路由信号量限制为 2。

## 验证

```bash
cd /home/dev01/project/ResearchEngine
conda activate researchengine
python -m pytest tests/quantcore/test_event_backtest.py \
  tests/quantcore/test_paper_state.py tests/api/test_paper_trading.py \
  tests/api/test_v2_contract.py -q
```
