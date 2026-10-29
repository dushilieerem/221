# FEAT-002：ResearchEngine 研究服务

> 当前系统基线统一见[标准系统文档](../../归档/系统文档-README.md)。本目录保留 FEAT-002 的提案和交付材料；带有“历史设计稿”标识的旧架构/模块/类/流程文档不代表当前代码结构。

> 2026-08-28 架构修订：用户已确认 ADR-029 的常规 FastAPI 分层。
> 原 QuantCore 运行时边界取消；五个 Engine 的业务逻辑进入 `app.services`，
> Panel、MetricContext、回测规格/结果和实验上下文进入 `app.models`，数据访问进入 `app.database`。
> 配置为 `app.config`，通用工具为 `app.utils`；不增加 domain/application/infrastructure。
> 下文中的旧目录和旧分层仅作历史设计追溯，不再作为新代码约束；既有业务规则和历史验证记录保留。
> 详见根目录 FEAT-030/06-app-layout.md 与 ADR-029。FEAT-029 因子来源加载目标保持不变。


> 已批准实施：[FEAT-030](../../../../SVQuant/docs/功能包/FEAT-030-因子回测流程重构/README.md) 保留
> Manager 与 Experiment 职责，由一个应用层 BacktestEngine 组织完整因子流程；单次、批量、Python 同路。
> 本 Feature 的基础能力状态不变，因子主链已切换，专项验收见 FEAT-030 Verification。

- 状态：已完成（基础版 + 因子分析 API + 策略回测 API MVP 增量）
- 所属 Initiative：INIT-001 投研核心
- 所属能力：因子研究、策略研究、研究 API
- 代码位置：本仓库 `app/`

## 文档索引

- [当前需求和功能范围](../../需求文档/PRD.md)：ResearchEngine 当前系统实现基线
- [当前系统架构](../../架构与技术/系统架构.md)：系统边界和依赖关系
- [当前模块/类/流程设计](../../模块设计/README.md)：代码模块、核心对象和运行时序
- `01-feature-proposal.md`：早期 Feature 提案，保留当时背景和方案
- `02-architecture.md`：历史架构设计稿，当前实现见标准系统架构
- `03-modules.md`：历史模块划分稿，当前实现见标准模块设计
- `04-class-design.md`：历史类设计稿，当前实现见标准类设计
- `05-runtime-flow.md`：历史运行流程稿，当前流程见标准流程设计
- `06-research-api.md`：HTTP API 使用索引；回测接口和任务说明位于 `06-研究接口文档/`，完整路由索引见[标准 API 设计](../../接口设计/API设计.md)
- `07-factor-strategy-guide.md`：因子与策略编写指南入口，含策略部研究手册及 ResearchEngine 因子、向量化策略、事件策略和示例文档
- `08-factor-analysis-api-extension.md`：因子分析序列与行业曲线 API 增量交付材料
- `09-factor-analysis-api-verification.md`：因子分析 API 增量验证记录
- `10-strategy-backtest-api-verification.md`：策略回测 API MVP 验证记录

## Feature 交付材料状态

该 Feature 在新工作流建立前已实现，Feature 层仍未形成完整的 Project Plan 和统一 Verification 包；因子分析和策略回测增量各有独立验证记录。后续 AI、持久化、团队协作等独立业务能力按各自 Feature 维护。

