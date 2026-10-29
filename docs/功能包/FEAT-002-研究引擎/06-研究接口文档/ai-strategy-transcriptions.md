# AI 策略转写（聚宽 → SVQuant）API

> 状态：MVP 已实现。关联：[FEAT-033](../../FEAT-033-AI策略转写/README.md)。
> 端点：`POST /api/ai/strategy-transcriptions`

## 1. 概述

将用户粘贴的聚宽策略源码转写为可审阅的 SVQuant 策略草稿，并返回逐条差异报告。

服务端先做静态能力预检，再调用模型；**阻断项未确认时不会调用模型**。模型输出必须通过 JSON、AST、编译、安全、未来数据与字段校验。接口不会执行代码、不会自动回测、不会保存策略。

```text
聚宽代码 → 解析/能力预检 → (阻断? needs_input) → LLM → JSON/AST/编译校验
                                                              ↓
                                              ready / rejected / report_only
                                                              ↓ 用户确认
                                              POST /api/backtest/strategy
```

## 2. 请求

```json
{
  "code": "def initialize(context):\n    g.stocks = get_index_stocks('000905.XSHG')\n    run_daily(trade, time='open')\n...",
  "context": {
    "target": {
      "engine_mode": "auto",
      "universe": "CSI500",
      "benchmark": "CSI500",
      "start": "2020-01-01",
      "end": "2026-08-31",
      "initial_capital": 1000000
    },
    "accepted_degradations": [],
    "report_only": false,
    "source_name": "rsi_rotation.py"
  },
  "idempotency_key": "8ecb2eb2-6f7f-4d64-93b2-7e63b34ff2a8"
}
```

| 字段 | 类型 | 必填 | 规则 |
|---|---|:---:|---|
| `code` | string | 是 | 1–200,000 字符；只接受单文件 Python；不做多文件解析。 |
| `context.target.engine_mode` | `auto/vectorized/event` | 否 | 默认 `auto`；有 `initialize/run_daily/order_*` 等标记时判为事件。 |
| `context.target.universe` | string/null | 否 | 仅作为回测/提示上下文；实际股票池由回测请求决定。 |
| `context.target.benchmark` | string/null | 否 | 同上；不改变服务端数据权限。 |
| `context.target.start/end` | string/null | 否 | 仅回测上下文，不参与代码生成校验。 |
| `context.target.initial_capital` | number/null | 否 | 正数。 |
| `context.accepted_degradations` | string[] | 否 | 已确认的降级 id，例如 `daily_approx`；只放行对应阻断项。 |
| `context.report_only` | boolean | 否 | `true` 时只出报告，不调用模型、不返回代码。 |
| `context.source_name` | string/null | 否 | 仅用于展示/审计，不参与解析。 |
| `idempotency_key` | UUID | 是 | 同键同内容返回首次结果；同键不同内容返回 `409`。 |

## 3. 响应

### 3.1 状态与结论

| `status` | 含义 | `strategy` |
|---|---|---|
| `ready` | 代码通过静态校验，可提交回测 | 存在 |
| `needs_input` | 存在未确认的阻断项；先让用户选择降级/仅报告 | `null` |
| `report_only` | 用户只要报告 | `null` |
| `rejected` | 代码解析失败或模型输出未通过校验 | `null` |

| `verdict` | 含义 |
|---|---|
| `equivalent` | 未发现需要披露的差异 |
| `degraded` | 代码可运行，但存在语义差异；`requires_ack=true` 时需用户确认 |
| `blocked` | 存在阻断项，不能等价实现 |

> `status=needs_input` 时 `verdict` 固定为 `blocked`：等待确认不是第四种结论。

### 3.2 成功响应示例（节选）

```json
{
  "transcription_id": "st_1f2e...",
  "status": "ready",
  "verdict": "degraded",
  "target_engine": "event",
  "runnable": true,
  "requires_ack": true,
  "summary": "可以运行，但有 3 处语义差异；不是等价迁移，请逐条确认。",
  "issues": [
    {
      "code": "SEMANTIC-UNIVERSE",
      "severity": "degraded",
      "impact": "high",
      "line": 2,
      "title": "指数成分只能取回测起点快照",
      "why": "SVQuant 的股票池在回测数据起点解析，之后不随指数调样动态切换。",
      "effect": "忽略历史调样，早期样本存在幸存者偏差。",
      "options": [
        {
          "id": "static_universe",
          "label": "接受静态快照（推荐）",
          "risk": "high",
          "note": "最快，报告注明偏差",
          "recommended": true
        }
      ],
      "evidence": "事件策略规范 §3；当前数据只有 CSI300/CSI500 成分快照",
      "confidence": "certain",
      "accepted": null
    }
  ],
  "mappings": [
    {"jq": "get_index_stocks", "svq": "universe=CSI300/CSI500（请求配置）", "status": "degraded", "line": 2}
  ],
  "assumptions": [],
  "checks": [
    {"id": "syntax", "label": "语法编译", "status": "pass"},
    {"id": "contract", "label": "策略契约", "status": "pass"},
    {"id": "security", "label": "安全扫描", "status": "pass"},
    {"id": "future_data", "label": "未来数据", "status": "pass"},
    {"id": "fields", "label": "字段范围", "status": "pass"}
  ],
  "errors": [],
  "strategy": {
    "strategy_name": "RsiReversalEventStrategy",
    "strategy_code": "class RsiReversalEventStrategy:\n...",
    "description": "由聚宽策略转写的受控草稿。",
    "used_fields": ["close"],
    "assumptions": [],
    "warnings": ["仅为研究草稿，需回测验证。"]
  },
  "meta": {
    "model_alias": "ark-code-latest",
    "prompt_version": "v1",
    "capability_catalog_version": "2026-10-29",
    "data_version": "ifind-sync-20260908-055514",
    "latency_ms": 2468
  }
}
```

### 3.3 阻断响应示例

```json
{
  "status": "needs_input",
  "verdict": "blocked",
  "runnable": false,
  "requires_ack": true,
  "summary": "存在 2 个阻断项：请选择降级方案，或仅导出报告。",
  "issues": [
    {
      "code": "API-UNSUPPORTED",
      "severity": "blocker",
      "impact": "high",
      "line": 8,
      "title": "分钟/Tick 行情无法读取",
      "options": [
        {"id": "daily_approx", "label": "生成日频近似（高风险）", "risk": "high", "note": "需你确认", "recommended": true}
      ]
    }
  ],
  "strategy": null
}
```

## 4. 差异报告契约

每条 `issues` 必须包含：

- `line`：聚宽源码行号（0 表示无法定位）；
- `why`：机制解释，不能只写「不支持」；
- `effect`：对收益/回撤/样本/换手的影响；
- `options`：至少 1 个可执行选项，推荐项排第一；
- `evidence`：能力目录/数据版本/文档依据；
- `confidence`：`certain | likely | needs_check`。

严重度：

| `severity` | 含义 |
|---|---|
| `blocker` | 无等价路径；未确认前不生成代码 |
| `degraded` | 代码可运行，但语义不同 |
| `info` | 可忽略/仅展示 |

## 5. 校验与安全

- 只允许纯计算代码；禁止文件、网络、命令执行、动态执行、双下划线、随机/当前时间。
- 禁止负周期 `shift`、`rolling(center=True)`。
- 向量化：单类；`__init__(self, <字段...>)` + `generate_signals(self)`；仅 `import pandas as pd`；返回目标权重。
- 事件：单类；`initialize(self, context)`；白名单 `context` API；只允许 `pandas/math/statistics`；不允许 `from ... import`、`while`、`try`。
- 代码只编译、不执行；回测必须由用户确认后另行调用 `POST /api/backtest/strategy`。

## 6. 配置

```text
AI_TRANSCRIPTION_API_KEY / AI_TRANSCRIPTION_BASE_URL / AI_TRANSCRIPTION_MODEL_ALIAS
```

未配置时回退 `AI_STRATEGY_*`、`AI_FACTOR_*`、`ARK_API_KEY`、`config.yaml` 的 `ai.*`。
无 Key 时，预检仍可返回 `needs_input`/`report_only`；需要生成时返回 `503`。
