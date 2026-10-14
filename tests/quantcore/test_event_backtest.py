"""日频 EventEngine 的核心时序与 A 股规则测试。"""

import pandas as pd
import pytest

from app.models.event import EventBacktestConfig
from app.services.strategy.event.engine import EventBacktestEngine


def _panels():
    dates = pd.DatetimeIndex(["2024-01-02", "2024-01-03", "2024-01-04"])
    return {
        "open": pd.DataFrame({"A": [10.0, 11.0, 12.0]}, index=dates),
        "high": pd.DataFrame({"A": [10.0, 11.0, 12.0]}, index=dates),
        "low": pd.DataFrame({"A": [10.0, 11.0, 12.0]}, index=dates),
        "close": pd.DataFrame({"A": [10.5, 11.5, 12.5]}, index=dates),
    }


def _config(**overrides):
    """事件回测配置。

    成交时点参数 ``execution_timing`` 的默认值是 ``next_close``；本文件里的
    既有用例都按"当日开盘价成交"构造（购买数量也是照开盘价配的），因此这里
    显式固定为 ``next_open`` 保持原意。成交时点本身由本文件末尾的
    ``test_execution_timing_*`` 用例覆盖。
    """

    overrides.setdefault("execution_timing", "next_open")
    return EventBacktestConfig(initial_capital=10_000, **overrides)


def test_before_open_only_sees_previous_close_and_fills_at_current_open():
    seen = []

    class Strategy:
        def initialize(self, context):
            def rebalance(ctx):
                seen.append(ctx.data.history(["A"], ["close"], 2)["close"].index.tolist())
                if ctx.current_dt.date().isoformat() == "2024-01-03":
                    ctx.orders.target_quantity("A", 100)

            context.schedule.daily(rebalance)

    result = EventBacktestEngine(_panels(), _config()).run(Strategy())

    assert seen[0] == []
    assert seen[1] == [pd.Timestamp("2024-01-02")]
    assert result.fills[0].timestamp == pd.Timestamp("2024-01-03").to_pydatetime()
    assert result.fills[0].price == 11.0


def test_t_plus_one_and_lot_size_rejections_are_audited():
    class Strategy:
        def initialize(self, context):
            def trade(ctx):
                if ctx.current_dt.date().isoformat() == "2024-01-02":
                    ctx.orders.target_quantity("A", 50)
                if ctx.current_dt.date().isoformat() == "2024-01-03":
                    ctx.orders.target_quantity("A", 100)
                    ctx.orders.close("A")

            context.schedule.daily(trade)

    result = EventBacktestEngine(_panels(), _config()).run(Strategy())
    rejected = {order.reason_code for order in result.orders if order.status == "rejected"}

    assert "LOT_SIZE" in rejected
    assert "T_PLUS_ONE" in rejected
    assert len(result.fills) == 1


def test_limit_up_rejects_buy():
    panels = _panels()
    panels["up_and_down_status"] = pd.DataFrame(
        {"A": ["非涨跌停", "涨停", "非涨跌停"]}, index=panels["close"].index
    )

    class Strategy:
        def initialize(self, context):
            def trade(ctx):
                if ctx.current_dt.date().isoformat() == "2024-01-03":
                    ctx.orders.target_value("A", 1_100)

            context.schedule.daily(trade)

    result = EventBacktestEngine(panels, _config()).run(Strategy())

    assert result.orders[0].reason_code == "LIMIT_UP"


def test_suspended_rejects_buy():
    panels = _panels()
    panels["up_and_down_status"] = pd.DataFrame(
        {"A": ["非涨跌停", "停牌", "非涨跌停"]}, index=panels["close"].index
    )

    class Strategy:
        def initialize(self, context):
            def trade(ctx):
                if ctx.current_dt.date().isoformat() == "2024-01-03":
                    ctx.orders.target_value("A", 1_100)

            context.schedule.daily(trade)

    result = EventBacktestEngine(panels, _config()).run(Strategy())

    assert result.orders[0].reason_code == "SUSPENDED"


def test_st_buy_rejected_when_filter_st_enabled():
    panels = _panels()
    panels["listed_status"] = pd.DataFrame(
        {"A": ["在市", "ST", "在市"]}, index=panels["close"].index
    )

    class Strategy:
        def initialize(self, context):
            def trade(ctx):
                if ctx.current_dt.date().isoformat() == "2024-01-03":
                    ctx.orders.target_value("A", 1_100)

            context.schedule.daily(trade)

    result = EventBacktestEngine(
        panels, _config(filter_st=True)
    ).run(Strategy())

    assert result.orders[0].reason_code == "ST"


def test_st_filter_is_off_by_default():
    panels = _panels()
    panels["listed_status"] = pd.DataFrame(
        {"A": ["在市", "ST", "在市"]}, index=panels["close"].index
    )

    class Strategy:
        def initialize(self, context):
            def trade(ctx):
                if ctx.current_dt.date().isoformat() == "2024-01-03":
                    ctx.orders.target_value("A", 1_100)

            context.schedule.daily(trade)

    result = EventBacktestEngine(
        panels, _config()
    ).run(Strategy())

    assert result.orders[0].reason_code is None


def test_st_filter_still_allows_selling_existing_holding():
    panels = _panels()
    panels["listed_status"] = pd.DataFrame(
        {"A": ["在市", "在市", "ST"]}, index=panels["close"].index
    )

    class Strategy:
        def initialize(self, context):
            def trade(ctx):
                day = ctx.current_dt.date().isoformat()
                if day == "2024-01-02":
                    ctx.orders.target_value("A", 1_000)
                if day == "2024-01-04":
                    ctx.orders.close("A")

            context.schedule.daily(trade)

    result = EventBacktestEngine(
        panels, _config(filter_st=True)
    ).run(Strategy())

    # 转 ST 只拦买入；已持仓仍须可以卖出，否则会被套死。
    assert "ST" not in {order.reason_code for order in result.orders}


def test_normal_status_does_not_reject_sell_as_limit_down():
    panels = _panels()
    panels["up_and_down_status"] = pd.DataFrame(
        {"A": ["非涨跌停", "非涨跌停", "非涨跌停"]}, index=panels["close"].index
    )

    class Strategy:
        def initialize(self, context):
            def trade(ctx):
                if ctx.current_dt.date().isoformat() == "2024-01-02":
                    ctx.orders.target_quantity("A", 100)
                if ctx.current_dt.date().isoformat() == "2024-01-04":
                    ctx.orders.close("A")

            context.schedule.daily(trade)

    result = EventBacktestEngine(
        panels, _config()
    ).run(Strategy())

    assert len(result.fills) == 2
    assert result.fills[-1].side == "sell"


def test_weekly_and_monthly_callbacks_follow_trading_calendar():
    dates = pd.DatetimeIndex(["2024-01-31", "2024-02-01", "2024-02-05"])
    panels = {
        field: pd.DataFrame({"A": [10.0, 10.0, 10.0]}, index=dates)
        for field in ("open", "high", "low", "close")
    }
    calls = []

    class Strategy:
        def initialize(self, context):
            context.schedule.weekly(lambda ctx: calls.append(("weekly", ctx.current_dt.date())), weekday=0)
            context.schedule.monthly(lambda ctx: calls.append(("monthly", ctx.current_dt.date())), trading_day=1)

    EventBacktestEngine(panels).run(Strategy())

    assert calls == [("monthly", pd.Timestamp("2024-01-31").date()), ("monthly", pd.Timestamp("2024-02-01").date()), ("weekly", pd.Timestamp("2024-02-05").date())]


def test_donchian_style_signal_uses_previous_close_and_target_value():
    dates = pd.date_range("2024-01-02", periods=5, freq="B")
    close = pd.DataFrame({"A": [10.0, 11.0, 12.0, 13.0, 14.0]}, index=dates)
    panels = {field: close.copy() for field in ("open", "high", "low", "close")}

    class DonchianStyleStrategy:
        def initialize(self, context):
            def rebalance(ctx):
                history = ctx.data.history(["A"], ["close", "high"], 3)
                if len(history["close"]) == 3 and history["close"].iloc[-1, 0] > history["high"].iloc[:-1, 0].max():
                    ctx.orders.target_value("A", 2_000)

            context.schedule.daily(rebalance)

    result = EventBacktestEngine(panels, _config()).run(DonchianStyleStrategy())

    assert result.fills[0].timestamp == dates[3].to_pydatetime()
    assert result.fills[0].quantity == 100


def test_portfolio_average_cost_tracks_open_fills_and_clears_after_exit():
    seen = []

    class Strategy:
        def initialize(self, context):
            def trade(ctx):
                if ctx.current_dt.date().isoformat() == "2024-01-02":
                    ctx.orders.target_quantity("A", 100)
                elif ctx.current_dt.date().isoformat() == "2024-01-03":
                    seen.append(ctx.portfolio.average_cost("A"))
                elif ctx.current_dt.date().isoformat() == "2024-01-04":
                    ctx.orders.close("A")

            context.schedule.daily(trade)

    result = EventBacktestEngine(_panels(), _config()).run(Strategy())

    assert seen == [10.0]
    assert result.position_snapshots[-1].positions["A"] == 0


# ---------------- 退市剔除与强制平仓 ----------------

def _buy_once_then_hold(*, buy_day: str = "2024-01-02", close_day: str | None = None):
    """构造一个只在 buy_day 买入、之后不再下单的最小策略。"""

    class Strategy:
        def initialize(self, context):
            def trade(ctx):
                day = ctx.current_dt.date().isoformat()
                if day == buy_day:
                    ctx.orders.target_value("A", 1_000)
                elif close_day is not None and day == close_day:
                    ctx.orders.close("A")

            context.schedule.daily(trade)

    return Strategy()


def _listed(statuses):
    dates = pd.DatetimeIndex(["2024-01-02", "2024-01-03", "2024-01-04"])
    return pd.DataFrame({"A": statuses}, index=dates)


def test_delisted_buy_is_rejected_without_any_switch():
    panels = _panels()
    panels["listed_status"] = _listed(["在市", "终止上市", "终止上市"])

    class Strategy:
        def initialize(self, context):
            def trade(ctx):
                if ctx.current_dt.date().isoformat() == "2024-01-03":
                    ctx.orders.target_quantity("A", 100)

            context.schedule.daily(trade)

    # 没有传任何过滤开关：退市剔除恒开启。
    result = EventBacktestEngine(panels, _config()).run(Strategy())

    assert result.orders[0].status == "rejected"
    assert result.orders[0].reason_code == "DELISTED"


def test_engine_force_liquidates_delisted_holding_at_transition_close():
    panels = _panels()
    panels["listed_status"] = _listed(["在市", "终止上市", "终止上市"])

    result = EventBacktestEngine(
        panels, _config()
    ).run(_buy_once_then_hold())

    exits = [order for order in result.orders if order.reason_code == "DELISTED"]
    assert len(exits) == 1
    assert exits[0].status == "filled"
    assert exits[0].side == "sell"

    # 强平价是转换日（2024-01-03）的收盘价 11.5，不是当日开盘价 11.0。
    fill = next(fill for fill in result.fills if fill.order_id == exits[0].order_id)
    assert fill.price == 11.5
    assert fill.quantity == 100
    assert fill.stamp_tax > 0
    assert fill.commission > 0

    # 转换日收盘后持仓已归零，当日权益不再被冻结价格吊住。
    transition_snapshot = result.position_snapshots[1]
    assert transition_snapshot.positions["A"] == 0
    assert transition_snapshot.total_value == transition_snapshot.cash

    assert any(event["type"] == "FORCED_LIQUIDATION" for event in result.events)
    assert result.assumptions["delisted_handling"].startswith("point_in_time_exclude")


def test_force_liquidation_survives_missing_open_price():
    """转换日 open 为 NaN 时仍须强平。

    ``_price`` 读 open，退市样本里约一半的转换日 open 缺失；若强平走常规
    撮合路径会被 ``NO_PRICE`` 拒单，仓位就会一直挂在冻结价上。
    """

    panels = _panels()
    panels["open"] = pd.DataFrame({"A": [10.0, None, None]}, index=panels["close"].index)
    panels["listed_status"] = _listed(["在市", "终止上市", "终止上市"])

    result = EventBacktestEngine(
        panels, _config()
    ).run(_buy_once_then_hold())

    exits = [order for order in result.orders if order.reason_code == "DELISTED"]
    assert len(exits) == 1
    assert exits[0].status == "filled"
    assert result.position_snapshots[1].positions["A"] == 0


def test_force_liquidation_ignores_suspension_and_limit_down_status():
    panels = _panels()
    panels["listed_status"] = _listed(["在市", "退市整理", "终止上市"])
    panels["up_and_down_status"] = pd.DataFrame(
        {"A": ["非涨跌停", "跌停", "停牌"]}, index=panels["close"].index
    )

    result = EventBacktestEngine(
        panels, _config()
    ).run(_buy_once_then_hold())

    exits = [order for order in result.orders if order.reason_code == "DELISTED"]
    assert len(exits) == 1
    assert exits[0].status == "filled"
    assert result.position_snapshots[1].positions["A"] == 0


def test_force_liquidation_leaves_no_ghost_sellable_quantity():
    panels = _panels()
    panels["listed_status"] = _listed(["在市", "终止上市", "终止上市"])

    result = EventBacktestEngine(
        panels, _config()
    ).run(_buy_once_then_hold())

    # 强平当日及之后都不应残留可卖数量或反手的持仓。
    for snapshot in result.position_snapshots[1:]:
        assert snapshot.positions.get("A", 0) == 0
        assert snapshot.sellable_quantities.get("A", 0) == 0


def test_backtest_ending_before_transition_does_not_liquidate():
    panels = _panels()
    # 状态面板比行情多一天：转换日落在回测区间之外。
    panels["listed_status"] = pd.DataFrame(
        {"A": ["在市", "在市", "在市", "终止上市"]},
        index=pd.DatetimeIndex(
            ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"]
        ),
    )

    result = EventBacktestEngine(
        panels, _config()
    ).run(_buy_once_then_hold())

    # 区间结束在转换日之前：持仓保留，不做任何强平。
    assert not [order for order in result.orders if order.reason_code == "DELISTED"]
    assert result.position_snapshots[-1].positions["A"] == 100


def test_no_force_liquidation_when_listed_status_field_is_absent():
    """数据版本缺少 listed_status 时按降级处理：不拦截也不强平。"""

    result = EventBacktestEngine(
        _panels(), _config()
    ).run(_buy_once_then_hold())

    assert not [order for order in result.orders if order.reason_code == "DELISTED"]
    assert result.position_snapshots[-1].positions["A"] == 100


def test_symbol_already_delisted_at_first_date_is_never_bought():
    panels = _panels()
    panels["listed_status"] = _listed(["暂停上市", "暂停上市", "暂停上市"])

    result = EventBacktestEngine(
        panels, _config()
    ).run(_buy_once_then_hold())

    assert result.fills == []
    assert all(order.status == "rejected" for order in result.orders)


def test_strategy_sell_on_transition_day_is_not_liquidated_twice():
    """卖出不额外拦截：策略在转换日自己按开盘价出逃，强平不应重复卖。"""

    panels = _panels()
    panels["listed_status"] = _listed(["在市", "终止上市", "终止上市"])

    result = EventBacktestEngine(
        panels, _config()
    ).run(_buy_once_then_hold(close_day="2024-01-03"))

    exits = [order for order in result.orders if order.reason_code == "DELISTED"]
    # 策略自己成交了，引擎不应再强平一次。
    assert exits == []
    assert not any(event["type"] == "FORCED_LIQUIDATION" for event in result.events)

    transition_fills = [
        fill
        for fill in result.fills
        if fill.timestamp.date().isoformat() == "2024-01-03"
    ]
    assert len(transition_fills) == 1
    assert transition_fills[0].side == "sell"
    assert transition_fills[0].price == 11.0  # 开盘价，不是强平价 11.5
    assert result.position_snapshots[1].positions["A"] == 0


# ---------------- 交易成本参数 ----------------

def test_slippage_worsens_fill_price_on_both_sides():
    """滑点建模为更差的成交价：买入向上、卖出向下，并体现在成交记录里。"""

    panels = _panels()  # open 10/11/12，close 10.5/11.5/12.5

    result = EventBacktestEngine(
        panels,
        _config(slippage_rate=0.01),
    ).run(_buy_once_then_hold(close_day="2024-01-04"))

    buy, sell = result.fills[0], result.fills[1]
    assert buy.side == "buy"
    assert buy.price == pytest.approx(10.0 * 1.01)  # 开盘 10.0 上浮 1%
    assert sell.side == "sell"
    assert sell.price == pytest.approx(12.0 * 0.99)  # 开盘 12.0 下浮 1%
    # 成交额、佣金、印花税都按调整后的成交价计算。
    assert buy.commission == pytest.approx(max(buy.price * 100 * 0.0003, 5.0))
    assert sell.stamp_tax == pytest.approx(sell.price * 100 * 0.0005)


def test_slippage_defaults_to_zero_and_leaves_fills_unchanged():
    panels = _panels()
    result = EventBacktestEngine(
        panels, _config()
    ).run(_buy_once_then_hold(close_day="2024-01-04"))

    assert result.fills[0].price == 10.0
    assert result.fills[1].price == 12.0


def test_min_commission_can_be_disabled_but_defaults_to_five_yuan():
    panels = _panels()

    def commission_for(**config):
        result = EventBacktestEngine(
            panels, _config(**config)
        ).run(_buy_once_then_hold())
        return result.fills[0].commission

    # 默认每笔至少 5 元：成交额约 1000 元 × 0.03% = 0.3 元，被抬到 5 元。
    assert commission_for() == pytest.approx(5.0)
    # 显式设 0 即取消最低收费，只按费率计。
    assert commission_for(min_commission=0.0) == pytest.approx(0.3, abs=1e-9)
    # 费率与最低收费都为 0 时才是真正的零成本。
    assert commission_for(min_commission=0.0, commission_rate=0.0) == 0.0


# ---------------- 成交时点 execution_timing ----------------

def _buy_on_first_day():
    class Strategy:
        def initialize(self, context):
            def trade(ctx):
                if ctx.current_dt.date().isoformat() == "2024-01-02":
                    ctx.orders.target_quantity("A", 100)

            context.schedule.daily(trade)

    return Strategy()


def test_execution_timing_picks_open_or_close_of_the_fill_day():
    """信号恒在前一收盘产生，成交发生在次日：
    next_close 取当日收盘价，next_open 取当日开盘价。"""

    def run(timing: str):
        result = EventBacktestEngine(
            _panels(), _config(execution_timing=timing)
        ).run(_buy_on_first_day())
        return result.fills[0].price, result.assumptions["execution"]

    # _panels(): 2024-01-02 的 open=10.0、close=10.5
    close_price, close_assumption = run("next_close")
    open_price, open_assumption = run("next_open")

    assert close_price == 10.5
    assert open_price == 10.0
    assert close_assumption == "next_close"
    assert open_assumption == "next_open"


def test_execution_timing_defaults_to_next_close():
    """不传时用 next_close，且与显式传入逐位一致。"""

    default = EventBacktestEngine(
        _panels(), EventBacktestConfig(initial_capital=10_000)
    ).run(_buy_on_first_day())
    explicit = EventBacktestEngine(
        _panels(), _config(execution_timing="next_close")
    ).run(_buy_on_first_day())

    assert default.fills[0].price == 10.5
    assert default.fills[0].price == explicit.fills[0].price


def test_force_liquidation_uses_close_regardless_of_execution_timing():
    """退市强平恒按转换日收盘价处置，不受成交时点参数影响。"""

    panels = _panels()
    panels["listed_status"] = _listed(["在市", "终止上市", "终止上市"])

    class Strategy:
        """按股数买入，避免 target_value 的整手取整随成交价变化。"""

        def initialize(self, context):
            def trade(ctx):
                if ctx.current_dt.date().isoformat() == "2024-01-02":
                    ctx.orders.target_quantity("A", 100)

            context.schedule.daily(trade)

    for timing in ("next_close", "next_open"):
        result = EventBacktestEngine(
            panels, _config(execution_timing=timing)
        ).run(Strategy())

        exits = [order for order in result.orders if order.reason_code == "DELISTED"]
        assert len(exits) == 1, timing
        fill = next(fill for fill in result.fills if fill.order_id == exits[0].order_id)
        assert fill.price == 11.5, timing  # 转换日 close，不是 open 11.0


def test_same_close_lets_the_strategy_see_the_fill_day_close():
    """same_close 是「当日收盘出信号、当日收盘成交」：回调能看到当日收盘。"""

    def visibility(timing: str):
        """回测并返回 2024-01-03 回调当日可见的收盘序列。"""

        seen = []

        class Strategy:
            def initialize(self, context):
                def trade(ctx):
                    if ctx.current_dt.date().isoformat() == "2024-01-03":
                        history = ctx.data.history(["A"], ["close"], 5)["close"]
                        seen.append(history.index.tolist())

                context.schedule.daily(trade)

        result = EventBacktestEngine(
            _panels(), _config(execution_timing=timing)
        ).run(Strategy())
        return seen[0], result

    same_dates, same = visibility("same_close")
    previous_dates, later = visibility("next_close")

    # same_close：回调看得到当日（01-03）；其余口径只到前一收盘（01-02）。
    assert pd.Timestamp("2024-01-03") in same_dates
    assert same_dates[-1] == pd.Timestamp("2024-01-03")
    assert previous_dates[-1] == pd.Timestamp("2024-01-02")

    assert same.assumptions["data_visibility"] == "same_close"
    assert later.assumptions["data_visibility"] == "previous_close"
    assert same.assumptions["execution"] == "same_close"
