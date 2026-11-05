# ResearchEngine API 设计

## 1. API 约定

### Base URL

```text
http://<host>:<server.port>/api
```

默认端口为 `9527`。路由通过 `/api` 前缀挂载；不使用 `/api/v1`。

### 请求与返回

- 请求和响应以 JSON 为主；查询参数中的日期使用 ISO `YYYY-MM-DD`。
- 请求/响应模型见 `app/schemas/backtest.py`，FastAPI 默认 OpenAPI 页面为 `/docs`。
- 响应直接采用对应 Pydantic Schema，不使用统一 `{code,message,data}` 包装。
- 标准 HTTP 错误通常由 FastAPI 返回 `{"detail": ...}`；单次结果的业务错误也可能位于结果/任务字段内。
- 当前 API 没有身份认证、授权或限流中间件；网络访问限制由部署边界负责。

---

## 2. 常见 HTTP 状态

| HTTP | 含义 |
|---|---|
| `200` | 查询成功或同步策略回测返回 |
| `202` | 因子任务已接收，后续轮询 |
| `404` | 资产、任务或批次不存在/已不可查询 |
| `422` | 请求 Schema 校验失败 |
| `429` | 回测任务队列容量已满；按 `Retry-After` 处理 |
| `503` | 任务服务已关闭、行情目录或 Provider 暂不可用 |
| `500` | 未处理的服务端错误 |

实际字段和错误条件以各接口 schema 与路由实现为准。

---

## 3. API 列表

### 健康、资产和行情

| 方法 | 路径 | 功能 | 详细契约 |
|---|---|---|---|
| GET | `/api/health` | 健康状态和版本 | 本页 / OpenAPI |
| GET | `/api/data/status` | 当前行情 release 的状态 | [行情发布与重载](../功能包/FEAT-002-研究引擎/06-研究接口文档/data-release.md) |
| POST | `/api/data/reload` | 手动重载行情 release | [行情发布与重载](../功能包/FEAT-002-研究引擎/06-研究接口文档/data-release.md) |
| GET | `/api/factor-libraries` | 因子库列表 | [因子目录 API](../功能包/FEAT-002-研究引擎/06-研究接口文档/factor-libraries.md) |
| GET | `/api/factors/{library_name}` | 指定库的因子列表 | [因子目录 API](../功能包/FEAT-002-研究引擎/06-研究接口文档/factor-library-factors.md) |
| GET | `/api/strategies` | 策略目录 | [策略目录 API](../功能包/FEAT-002-研究引擎/06-研究接口文档/strategy-list.md) |
| GET | `/api/market-data/symbols` | 股票/标的列表 | [标的 API](../功能包/FEAT-002-研究引擎/06-研究接口文档/market-data-symbols.md) |
| GET | `/api/market-data/daily/{symbol}` | 单标的日线，可选 `start`、`end` | [日线 API](../功能包/FEAT-002-研究引擎/06-研究接口文档/market-data-daily.md) |
| GET | `/api/market-data/indices` | 指数目录 | [指数 API](../功能包/FEAT-002-研究引擎/06-研究接口文档/market-data-indices.md) |
| GET | `/api/market-data/indices/{code}/constituents` | 指数成分 | [指数成分 API](../功能包/FEAT-002-研究引擎/06-研究接口文档/market-data-index-constituents.md) |

### 因子和策略回测

| 方法 | 路径 | 功能 | 详细契约 |
|---|---|---|---|
| POST | `/api/backtest/factor` | 提交异步因子回测，返回 `202` 和任务 ID | [因子回测 API](../功能包/FEAT-002-研究引擎/06-研究接口文档/backtest-factor.md) |
| GET | `/api/backtest/factor/{run_id}` | 查询因子任务状态和结果 | [因子回测 API](../功能包/FEAT-002-研究引擎/06-研究接口文档/backtest-factor.md) |
| DELETE | `/api/backtest/factor/{run_id}` | 请求取消因子任务 | [因子回测 API](../功能包/FEAT-002-研究引擎/06-研究接口文档/backtest-factor.md) |
| POST | `/api/backtest/strategy` | 执行策略回测并同步返回结果 | [策略回测 API](../功能包/FEAT-002-研究引擎/06-研究接口文档/backtest-strategy.md) |

### 模拟盘结算（内部）

| 方法 | 路径 | 功能 | 详细契约 |
|---|---|---|---|
| GET | `/api/internal/paper-trading/status?date=YYYY-MM-DD` | 判断 D 日数据是否就绪、返回 signal_date/latest_ready_trading_day | 本页 |
| GET | `/api/internal/paper-trading/trading-days?start=&end=` | 返回区间内已发布的交易日 | 本页 |
| POST | `/api/internal/paper-trading/settle` | 单日同步结算：D-1 信号 + D 日 VWAP 成交 + 未复权估值 | 本页 |

- 请求字段：`settlement_date`、`engine_mode`、`strategy_code`、
  `expected_code_hash`、`expected_data_release_id`、`universe`、`benchmark`、
  `account.cash/positions`、`strategy_state`、`strategy_state_code_hash`、
  `expected_state_hash`、`execution`。
- 响应字段：`status/settlement_date/signal_date/next_trading_day/
  data_release_id/data_release_signature/code_hash/state_hash/orders/fills/
  next_account/strategy_state/fees/warnings/assumptions`。
- 错误：409 `DATA_NOT_READY`/`NOT_A_TRADING_DAY`/`CODE_HASH_MISMATCH`/
  `STRATEGY_STATE_VERSION_MISMATCH`/`STRATEGY_STATE_HASH_MISMATCH`/
  `DATA_RELEASE_MISMATCH`；422 请求或组合非法；500 内部错误。
- 该接口只依赖内网信任边界，未实现服务间认证；不得暴露公网。

### AI 辅助 API

| 方法 | 路径 | 功能 | 详细契约 |
|---|---|---|---|
| POST | `/api/ai/post-summaries` | 社区帖子结构化摘要（临时兜底） | 本页；见 [已移交功能说明](../归档/已移交-SVQuant/README.md) |

AI 因子生成、策略生成、回测解读与策略转写已于 2026-11-05 移交 SVQuant，RE 只保留帖子摘要兜底，待 SVQuant 移除 fallback 后删除。`ai.*` 配置仍用于该接口。

---

## 4. 异步任务约定

因子回测的提交端点返回 `202 Accepted`，并提供任务 ID/轮询地址。客户端应：

1. 保存响应中的 `run_id`。
2. 按 `poll_url` 查询状态；若响应有 `Retry-After`，遵循建议的重试间隔。
3. 在任务成功、失败或取消后停止轮询。
4. 不将进程内单次任务 ID 当作跨服务重启的持久历史 ID。

单次策略端点虽然由后台 Manager Worker 执行，但 HTTP 请求会等待并同步返回最终响应。

---

## 5. API 详细定义

请求字段、默认值、枚举、响应结构、完整示例和专项错误应维护在接口详细契约文件中，避免本索引重复定义。主要 DTO 位于 [backtest.py](../../app/schemas/backtest.py)，路由注册位于 [app/api/routes](../../app/api/routes)。

## 6. 权限、幂等与限流

- 权限要求：当前没有 API 身份认证或权限中间件；请通过受控网络提供服务。
- 幂等性：因子请求按任务键执行去重或复用；普通 GET 查询天然只读；`POST /api/data/reload` 只重新装载当前已发布 release，不修改行情文件，重复调用等价。详细语义见对应接口文档。
- 限流：未实现通用请求速率限制。回测任务由有界 Manager 控制并发/队列，队列满时返回 `429`。
- 安全边界：允许提交代码的回测端点只面向受信任调用方；当前没有执行沙箱。
