# 因子实现速查（ResearchEngine）

> 适用对象：需要把因子公式交给 ResearchEngine 计算的研究员与开发同事
> 研究流程、假设设计和结果判断见[策略部因子与策略编写手册](00-策略部因子与策略编写手册.md)。
> 关联：[策略编写指南入口](README.md)

## 1. 因子库与因子

因子库是前端管理的一组因子函数；研究员创建因子时只需填写函数，不需要编写 `FactorLibrary` 类或导入语句。每个因子输出“交易日 × 股票”的宽表。

新建因子的模板：

```python
def 因子名(self):
    """因子描述"""
    return
```

名称使用小写字母和下划线，例如 `momentum_20d`。函数可带默认参数，例如 `def momentum(self, window=20):`。

## 2. 示例

```python
def momentum_20d(self):
    """过去 20 个交易日收益率。"""
    return self.close.pct_change(20)

def volatility_20d(self):
    """过去 20 个交易日收益率波动率。"""
    return self.close.pct_change().rolling(20).std()

def bollinger_position(self, window=20, n_std=2.0):
    """收盘价在布林带中的相对位置。"""
    mid = self.close.rolling(window).mean()
    std = self.close.rolling(window).std()
    return (self.close - (mid - n_std * std)) / (2 * n_std * std)

def ep_ratio(self):
    """市盈率倒数。"""
    return 1.0 / self.pe_ttm
```

复杂逻辑允许使用多行中间变量，但最后必须返回宽表。

## 3. 当前因子面板字段

| 分类 | 当前常用字段 |
|------|------|
| 价格 | `open`、`high`、`low`、`close` |
| 成交 | `volume`、`amount`、`turnover` |
| 市值 | `current_mv` |
| 估值 | `dividend_yield_ttm`、`pe_ttm`、`pb_mrq`、`pcf_operating_cash_flow_ttm` |
| 状态/行业 | `listed_status`、`the_sw_industry_code`、`up_and_down_status` |

实际可用字段取决于当前行情数据版本，可先通过 `self.fields` 检查；其他已装载字段可用 `self["字段名"]` 访问。指数日线可用 `self.csi300.close`、`self.csi500.close`、`self.csi1000.close` 与 `self.sse50.close` 访问。

> **退市股已经替你剔除了，不要再自己过滤一遍。** 回测框架恒定按 `listed_status` 白名单（`在市`/`ST`/`*ST`）点时剔除退市标的——退市后行情会被前值填充成冻结价，不剔除会让低波动类因子把退市股排到最前（实测低波动 Top30 曾 100% 是冻结的退市股）。
>
> 两点需要注意：
> 1. 剔除发生在**因子值算完之后**（这样滚动窗口不受影响）。如果你在 `compute()` 内部自己做 `rank`/`zscore`/`normalize`，横截面里仍会包含冻结的退市股，量级为 O(1/N)；需要严格口径时自行按 `listed_status` 过滤。
> 2. 白名单认的是**上市状态**，识别不了「长期停牌但仍在市」（其价格同样被冻结）。需要更严口径时叠加停牌过滤。

使用 `self.fields` 判断字段是否存在。缺失字段不能用最新静态值替代：应改用可用字段，或停止该因子的回测。

与指数比较时按日期对齐：

```python
def relative_strength_vs_csi300(self, window=20):
    return self.close.pct_change(window).sub(
        self.csi300.close.pct_change(window), axis=0
    )
```

## 4. 编写规则

- 使用 pandas 向量化表达式，不逐只股票、逐日循环。
- 窗口期形成前产生的 `NaN` 可以保留，系统会处理。
- 不得使用未来数据，例如 `self.close.shift(-1)`。
- 参数应表达业务含义，例如 `window`、`n_std`，不要使用 `a`、`b`。
- 写清楚 docstring，它会作为因子说明展示。

## 5. 因子回测

通过 `POST /api/backtest/factor` 提交因子函数体和因子名。接口采用异步任务契约：提交成功返回 HTTP `202 Accepted`，响应包含 `run_id` 和 `poll_url`；客户端应继续通过 `GET poll_url` 轮询任务状态。

只有轮询结果的 `status` 为 `succeeded` 才表示回测成功，此时从顶层 `result` 读取回测结果。`status` 为 `failed` 或 `cancelled` 时，应从顶层 `error` 读取失败原因；不要按同步接口直接读取 `result["error"]`。

