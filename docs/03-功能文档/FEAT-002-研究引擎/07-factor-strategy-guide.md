# 因子与策略指南

> 文档版本：v2.2
> 最后更新：2026-09-20
> 面向读者：策略部研究员与策略开发同事；ResearchEngine 接口细节作为实现参考。

策略部建议从[因子与策略编写手册](07-因子与策略指南/00-策略部因子与策略编写手册.md)开始：先明确研究假设、数据时点和评价口径，再选择因子或策略实现。其余文档描述当前 ResearchEngine 的代码契约与接口。

| 文档 | 适用场景 |
|------|----------|
| [策略部因子与策略编写手册](07-因子与策略指南/00-策略部因子与策略编写手册.md) | 研究假设、因子评价、稳健性、交付记录；建议先读 |
| [因子实现速查](07-因子与策略指南/01-factor-writing.md) | ResearchEngine 因子函数约定、字段与回测提交方式 |
| [02-向量化策略编写规范](07-因子与策略指南/02-vectorized-strategy-writing.md) | 因子选股、Top-N、目标权重、`/api/backtest/strategy` |
| [03-事件策略编写规范](07-因子与策略指南/03-event-strategy-writing.md) | 突破、止损、持仓状态、订单与 A 股日频规则 |
| [04-示例与常见问题](07-因子与策略指南/04-examples-and-faq.md) | 从创建因子到调用 API 的完整示例、常见错误 |
| 前端因子库映射 | 由 WebApp 团队依据现有接口契约维护 |

ResearchEngine 当前的版本化策略资产位于
`ResearchEngine/app/assets/strategies/libraries/`，索引位于
`ResearchEngine/app/assets/strategies/index.yaml`。策略列表通过
`GET /api/strategies` 获取；回测通过 `POST /api/backtest/strategy`，并按
`engine_mode` 选择向量化或事件策略。

策略选择原则：只需生成日期 × 标的目标权重时写向量化策略；需要订单、持仓状态、T+1、停牌或涨跌停时写事件策略。两种接口不能混用。
