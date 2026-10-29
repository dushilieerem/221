# FEAT-033：AI 策略转写（聚宽 → SVQuant）

- 状态：MVP 已实现（2026-10-29）
- 所属能力：AI 研究助手 / 策略研究
- 关联：[FEAT-019 AI 向量化策略生成](../FEAT-019-AI向量化策略生成API/README.md)、[FEAT-022 经典策略迁移](../FEAT-022-经典策略迁移/README.md)、[FEAT-009 聚宽兼容因子库](../FEAT-009-聚宽兼容因子库/README.md)
- 产品需求包：工作区 `需求开发\AI策略转写\`（规格、原型、评测集）

## 1. 目标

用户粘贴一段聚宽策略代码，ResearchEngine 将其转写为可审阅的 SVQuant 策略草稿：

- 能直接映射的调用自动转换；
- 数据/时序/标的差异逐条进入报告；
- 无法等价时明确返回阻断项和降级选项，不生成「看起来能跑」的假代码；
- 不执行用户代码、不连接聚宽、不自动回测、不自动保存。

实现位置与既有 AI 因子/策略生成一致：**服务在 ResearchEngine，SVQuant 只负责入口、鉴权、配额、转发与展示**。

## 2. 流程

```text
POST /api/ai/strategy-transcriptions
  → 静态解析（AST：API / 字段 / 回调 / 导入）
  → 能力预检（分钟、财务、估值、动态成分、订单、标的、安全）
  → 阻断项未确认？ → needs_input，不调用模型
  → LLM（受控 Prompt + JSON Schema）
  → JSON / AST / 编译 / 安全 / 未来数据校验
  → ready（等价/降级）或 rejected
```

## 3. 本期范围

**支持**

- 单文件 Python 策略，≤200,000 字符；模型输入 ≤60,000 字符。
- 日频 A 股；向量化（`generate_signals`）与事件（`initialize(context)`）两种目标。
- 当前 15 个研究字段；目标金额/数量/清仓、日/周/月调度、状态、持仓数量。
- 差异报告：`where / why / effect / options / evidence`，含行号与置信度。
- `report_only`：只出报告，不返回代码。
- `accepted_degradations`：用户确认后放行分钟→日频、移除财务条件等。

**明确不做**

- 不运行聚宽代码、不引入 `jqdata`/`jqfactor`、不连接聚宽账号。
- 不支持分钟/Tick、财务/ROE/Piotroski、期货/期权、ETF T+0、两融、做空。
- 不自动执行、不自动回测、不保存策略、不下单。
- 不保证与聚宽回测逐值一致。

## 4. 代码位置

| 文件 | 作用 |
|---|---|
| `app/schemas/strategy_transcription.py` | 请求、报告、响应 Pydantic 模型 |
| `app/api/ai_strategy_transcription.py` | 能力预检、Prompt、LLM 调用、AST/编译校验 |
| `app/api/routes/ai.py` | `POST /api/ai/strategy-transcriptions` |
| `tests/test_ai_strategy_transcription.py` | 13 条契约/边界/安全/幂等测试 |

## 5. 配置

优先读取：

```text
AI_TRANSCRIPTION_API_KEY / AI_TRANSCRIPTION_BASE_URL / AI_TRANSCRIPTION_MODEL_ALIAS
```

未配置时回退到 `AI_STRATEGY_*`、`AI_FACTOR_*`、`ARK_API_KEY` 与 `config.yaml` 的 `ai.*`。
未配置 Key 时，预检可直接返回 `needs_input`/`report_only`；需要生成代码时返回 `503`。

## 6. 验证

```bash
pytest tests/test_ai_strategy_transcription.py -q
```

覆盖：

- 事件策略降级报告（静态成分、复权、目标百分比）；
- 分钟策略未确认 → `needs_input` 且不调用模型；
- 接受 `daily_approx` 后放行并标记 `accepted`；
- ROE 财务策略阻断；PE 策略只降级；
- 向量化策略等价/降级；
- 不安全生成代码拒绝；
- 注释注入只记 `info`；发送前遮罩疑似密钥；
- 幂等键复用；
- `report_only`；
- `from jqdata import *` 在模型调用前阻断；
- 请求缺字段返回 422。

## 7. 已知限制

- 能力目录是模块内的静态规则，尚未覆盖全部聚宽 API；未收录调用按普通代码处理，后续应抽成版本化目录。
- 事件 AST 白名单偏保守：`while`、`try`、`from ... import` 等会被拒绝。
- 向量化只支持自由目标权重策略，不支持结构化 `FactorPortfolioStrategy` 生成。
- 幂等记录仅在当前进程内，未接鉴权和持久化。
- 转写档案存在 SVQuant 本机（按登录用户隔离，最多 30 条），RE 不保存历史；换设备/清缓存后档案不可恢复。

## 8. SVQuant 交互

- 页面：`frontend/src/pages/StrategyConvertPage.tsx`，入口「策略 → 代码转换」。
- 布局：左侧「档案」记录每次转写，右侧「对话」展示用户代码气泡、助手结论/差异/代码气泡。
- 输入框上方 4 个快捷示例（每日定投/RSI 轮动/动量 Top10/分钟+ROE）；支持上传 `.py`、回测配置、仅报告。
- 每次成功响应（`ready` / `needs_input` / `report_only` / `rejected`）都会写入本机档案；点击档案可回看当时的代码与报告。
- 「对话」只是交互壳：一次发送对应一次完整 `POST /api/ai/strategy-transcriptions`，不把历史对话拼进模型上下文；接受降级后会带 `accepted_degradations` 重新请求，产生新的 `transcription_id`。
- RE 侧无需新增历史接口，仍保持无状态。
