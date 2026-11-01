"""聚宽日频回测语义探针（只读诊断，不是策略）。

用途：确认聚宽「日频回测」下，run_daily(time=...) 在 09:30 / 09:31 / 14:50 /
15:10 等时点看到的 price / open / 涨跌停价，以及在这些时点下的单实际按什么价成交。
兼容层必须按这套语义实现，否则 13 个策略的盘中逻辑无法对齐。

用法：
1. 聚宽新建回测：日频，2024-01-02 ~ 2024-01-31，初始资金 1,000,000，基准沪深300。
2. 粘贴本文件全部内容，运行。
3. 下载回测日志（或直接复制日志里以 PARITY_PROBE 开头的行），发给对账负责人。

每条日志都是一行 JSON，字段：
  tag           触发时点
  dt            聚宽认为的当前时间
  last_price    当前价
  day_open      当日开盘价
  high_limit    涨停价
  low_limit     跌停价
  paused/is_st  停牌/ST
  bar_*         同日日线 OHLC（用于对照）
  order_*       下单价、成交量、状态、均价、现金
"""

import json

SYMBOL = "600000.XSHG"
ORDER_DAYS = {3: "09:31", 4: "14:50", 5: "15:10"}


def initialize(context):
    set_benchmark("000300.XSHG")
    set_option("use_real_price", True)
    set_option("avoid_future_data", False)
    set_order_cost(
        OrderCost(
            open_tax=0.0,
            close_tax=0.0,
            open_commission=0.0,
            close_commission=0.0,
            close_today_commission=0.0,
            min_commission=0.0,
        ),
        type="stock",
    )
    set_slippage(FixedSlippage(0))
    g.day = 0
    try:
        g.run_params = dict(context.run_params)
    except Exception:  # noqa: BLE001
        g.run_params = {"frequency": str(getattr(context.run_params, "frequency", None))}
    run_daily(at_0900, time="09:00")
    run_daily(at_open, time="open")
    run_daily(at_0930, time="09:30")
    run_daily(at_0931, time="09:31")
    run_daily(at_1450, time="14:50")
    run_daily(at_1510, time="15:10")


def at_0900(context):
    g.day += 1
    _log(context, "09:00", order=None)


def at_open(context):
    _log(context, "open", order=None)


def at_0930(context):
    _log(context, "09:30", order=None)


def at_0931(context):
    _log(context, "09:31", order=None)


def at_1450(context):
    _log(context, "14:50", order=None)


def at_1510(context):
    _log(context, "15:10", order=None)


def _log(context, tag, order):
    row = {"tag": tag}
    try:
        data = get_current_data()[SYMBOL]
        row.update({
            "dt": str(context.current_dt),
            "previous_date": str(context.previous_date),
            "last_price": _number(getattr(data, "last_price", None)),
            "day_open": _number(getattr(data, "day_open", None)),
            "high_limit": _number(getattr(data, "high_limit", None)),
            "low_limit": _number(getattr(data, "low_limit", None)),
            "paused": bool(getattr(data, "paused", False)),
            "is_st": bool(getattr(data, "is_st", False)),
            "name": str(getattr(data, "name", "")),
        })
    except Exception as exc:  # noqa: BLE001
        row["current_data_error"] = f"{type(exc).__name__}: {exc}"

    try:
        bar = get_price(
            SYMBOL,
            end_date=context.current_dt,
            count=1,
            frequency="1d",
            fields=["open", "high", "low", "close"],
            skip_paused=False,
            fq=None,
        )
        if len(bar):
            row.update({
                "bar_date": str(bar.index[-1]),
                "bar_open": _number(bar["open"].iloc[-1]),
                "bar_high": _number(bar["high"].iloc[-1]),
                "bar_low": _number(bar["low"].iloc[-1]),
                "bar_close": _number(bar["close"].iloc[-1]),
            })
    except Exception as exc:  # noqa: BLE001
        row["bar_error"] = f"{type(exc).__name__}: {exc}"

    try:
        row["run_params"] = g.run_params
    except Exception:  # noqa: BLE001
        pass

    if ORDER_DAYS.get(g.day) == tag:
        _place_order(context, row)

    log.info("PARITY_PROBE " + json.dumps(row, ensure_ascii=False, default=str))


def _place_order(context, row):
    cash_before = float(context.portfolio.cash)
    try:
        order_obj = order(SYMBOL, 100)
        row.update({
            "order_side": "buy",
            "order_filled": int(getattr(order_obj, "filled", 0) or 0),
            "order_price": _number(getattr(order_obj, "price", None)),
            "order_avg_cost": _number(getattr(order_obj, "avg_cost", None)),
            "order_status": str(getattr(order_obj, "status", "")),
            "cash_before": cash_before,
            "cash_after": float(context.portfolio.cash),
        })
    except Exception as exc:  # noqa: BLE001
        row["order_error"] = f"{type(exc).__name__}: {exc}"

    try:
        row["position_amount"] = int(
            context.portfolio.positions[SYMBOL].total_amount
        )
    except Exception:  # noqa: BLE001
        row["position_amount"] = 0


def _number(value):
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None
