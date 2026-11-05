# 功能包

本目录按 `FEAT-*` 保存一次功能交付的需求、技术设计、验证和交付记录。长期结构写回 [`../模块设计/`](../模块设计/)，跨仓库能力不在这里复制。

## 功能包索引

| 功能包 | 主题 |
|---|---|
| [FEAT-002-研究引擎](FEAT-002-研究引擎/README.md) | 研究服务基础版、研究 API、因子/策略回测 |
| [FEAT-004-因子资产缓存](FEAT-004-因子资产缓存/README.md) | 因子资产加载与缓存 |
| [FEAT-005-Qlib因子库](FEAT-005-Qlib因子库/README.md) | Qlib 因子库接入 |
| [FEAT-006-事件驱动策略回测](FEAT-006-事件驱动策略回测/README.md) | 事件驱动回测 |
| [FEAT-007-回测API性能](FEAT-007-回测API性能/README.md) | 回测接口性能 |
| [FEAT-008-因子组合策略](FEAT-008-因子组合策略/README.md) | 因子组合策略 |
| [FEAT-009-聚宽兼容因子库](FEAT-009-聚宽兼容因子库/README.md) | 聚宽兼容因子 |
| [FEAT-011-内置因子API](FEAT-011-内置因子API/README.md) | 内置因子查询 |
| [FEAT-012-因子预处理工具](FEAT-012-因子预处理工具/README.md) | 因子预处理 |
| [FEAT-013-机器学习模型接口](FEAT-013-机器学习模型接口/README.md) | 机器学习模型接口 |
| [FEAT-014-策略调仓日志](FEAT-014-策略调仓日志/README.md) | 调仓日志 |
| [FEAT-015-个股日线API](FEAT-015-个股日线API/README.md) | 个股日线 |
| [FEAT-017-策略资产](FEAT-017-策略资产/README.md) | 策略资产 |
| [FEAT-021-指数成分API](FEAT-021-指数成分API/README.md) | 指数成分 |
| [FEAT-022-经典策略迁移](FEAT-022-经典策略迁移/README.md) | 经典策略迁移 |
| [FEAT-023-WorldQuantAlpha101](FEAT-023-WorldQuantAlpha101/README.md) | Alpha101 |
| [FEAT-026-国泰君安Alpha191](FEAT-026-国泰君安Alpha191/README.md) | Alpha191 |
| [FEAT-028-分层应用架构](FEAT-028-分层应用架构/README.md) | 分层改造 |
| [FEAT-029-因子注册](FEAT-029-因子注册/README.md) | 因子注册 |
| [FEAT-032-日终模拟盘](FEAT-032-日终模拟盘/README.md) | 日终结算（跨仓库能力） |
| [FEAT-034-聚宽对账基线](FEAT-034-聚宽对账基线/README.md) | 与聚宽回测逐层对账 |

## 目录约定

```text
FEAT-XXX-名称/
├── README.md
├── 0X-product-requirements / technical-design / verification ...
└── 06-研究接口文档/（FEAT-002 的接口契约）
```

## 规则

- 目录名使用 `FEAT-XXX-名称`，编号稳定、不复用。
- 功能包完成后保持只读；后续变更新建功能包，不直接改历史验收记录。
- FEAT-002 的 02–05 是历史设计稿，当前基线见 `模块设计/`。
- 跨仓库能力（如 FEAT-030）由 SVQuant 文档维护，这里只保留 ResearchEngine 侧交付记录。
