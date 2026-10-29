# 示例与常见问题

> 关联：[因子与策略编写指南入口](README.md)

## 1. 创建并回测因子

1. 在前端创建因子库，例如“动量因子”。
2. 新建 `momentum_5d` 因子。
3. 填写：

```python
def momentum_5d(self):
    """过去 5 个交易日的收益率。"""
    return self.close.pct_change(5)
```

4. 调用因子回测 API：

```python
import requests
import time
from urllib.parse import urljoin

base_url = "http://<research-api>"
response = requests.post(f"{base_url}/api/backtest/factor", json={
    "factor_code": """def momentum_5d(self):
    return self.close.pct_change(5)
""",
    "factor_name": "momentum_5d",
    "universe": "CSI500",
    "portfolio_type": "long_only",
    "rebalance_days": 20,
    "groups": 5,
    "benchmark": "CSI300",
})
if response.status_code != 202:
    raise RuntimeError(f"提交因子回测失败：{response.status_code} {response.text}")

task = response.json()
poll_url = urljoin(f"{base_url}/", task["poll_url"])
while True:
    poll_response = requests.get(poll_url)
    poll_response.raise_for_status()
    task = poll_response.json()
    if task["status"] in {"succeeded", "failed", "cancelled"}:
        break
    time.sleep(1)

if task["status"] != "succeeded":
    raise RuntimeError(task.get("error") or f"任务结束状态：{task['status']}")

result = task["result"]
```

## 2. 常见问题

### 因子值需要手动处理 NaN 吗？

通常不需要。滚动窗口前的 NaN 会由系统在后续分析中处理；不要用未来值填补。

### 一个因子库可以有多个因子吗？

可以。每个因子是同一库文件中的一个方法。

### 因子回测返回 HTTP 202 是否代表成功？

不一定。HTTP `202` 只表示任务已提交；必须继续轮询 `poll_url`，直到 `status` 为
`succeeded` 才表示成功。失败或取消时，原因在任务响应顶层的 `error` 字段中。

### 如何通过 API 调用事件策略？

事件策略可以调用同一个 `/api/backtest/strategy` 端点，但必须显式设置
`engine_mode: "event"`。例如：

```python
response = requests.post("http://<research-api>/api/backtest/strategy", json={
    "engine_mode": "event",
    "strategy_id": "donchian_a_share_event",
    "start": "2026-07-01",
    "end": "2026-08-14",
    "include_execution_timeline": True,
})
result = response.json()
if result["error"]:
    raise RuntimeError(result["error"])
```

事件策略必须实现 `initialize(context)`；向量化策略必须实现
`generate_signals()`，两种契约不能混用。

### 我能直接粘贴聚宽策略吗？

不能。SVQuant 不提供 `jqdata`、全局 `g` 或 `run_daily` 兼容层；应按[事件策略规范](03-event-strategy-writing.md)迁移到 `context` 接口。

