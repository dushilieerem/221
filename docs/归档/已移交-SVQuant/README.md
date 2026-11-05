# 已移交 SVQuant 的功能（RE 侧已删除）

2026-11-05 起，以下功能由 SVQuant 提供；RE 只保留历史文档，不再包含代码与路由：

- **FEAT-027 因子批量回测**：路由 `/api/internal/factor-backtest-batches*`、`FactorBacktestBatch*` 服务、批量 CLI、`factor_backtest.db` 结果库与相关测试已删除。SVQuant 确认不再调用。
- **FEAT-016 AI 因子生成**、**FEAT-019 AI 向量化策略生成**、**FEAT-020 AI 回测解读**、**FEAT-033 AI 策略转写**：`/api/ai/*` 对应路由与服务已删除；SVQuant 已有本地实现与自己的 LLM 网关。
- **FEAT-002 下的 4 篇 AI 接口文档**随功能归档到 `研究接口文档/`。

例外：RE 仍保留 `POST /api/ai/post-summaries`（社区帖子摘要兜底）和 `ai.*` 配置，待 SVQuant 移除 RE fallback 并通知后再删除。

相关 ADR（012、015）一并归档，仅作历史决策记录。
