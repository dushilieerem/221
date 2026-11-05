# -*- coding: utf-8 -*-
"""破净组合 v3（收益优化版），聚宽Python3，股票/分钟回测。

在 v2 基础上做的三处收益优化 + 两处口径修复，详见同目录《改动说明.md》：

一、收益优化
1. 踏空修复：选股日买入时点由"仅 09:30"扩展为 09:30 / 10:05 / 14:45 三个补单窗口；
   且当"可买目标为空"时保留原持仓（不误清仓）。修复 v2 在 2024-10-08 这类大涨日
   "涨停买不进却照样卖、80% 资金躲进货币基金踏空"的问题。
2. 质量加权选股：把 v2 的"五维各取 Top1 机械合并"改为"五因子 rank-pct 等权合成总分，
   取总分最高的前 8 只"。选出的是"综合质量最优"的股票，而不是某个单维度的极端值，
   同时避免 ROA/EBIT/毛利率三个盈利质量维度重复选到同一只。
3. 行业中性：全组合每申万一级行业最多 2 只（v2 仅红利候选有此限制），降低金融/地产/
   周期扎堆造成的同涨同跌。

二、口径修复
4. 股息率口径：由"最近 5 个互不重叠 365 天窗口"改为"5 个完整自然年均有正派息"，
   且股息率改为连续信号（近 1 年派息/市值），不再硬性要求 ≥5%。
5. 财务/因子观察日统一 previous_date，并保留 avoid_future_data=True。

保留 v2：0<PB<1 破净池、调整利润占比>80%、净利同比>0、正 EBIT、跨日待卖、
T+1/停牌/涨跌停保护、09:31/14:57 撤单、月末 14:55 释放 ETF / 14:56 归集现金、
昨日涨停开板卖出、单股仓位检查线。未添加普通个股止损、指数择时或账户级回撤止损。
"""
from jqdata import *
from jqfactor import get_factor_values, Factor, calc_factors
import builtins as bi
import datetime as dt
import numpy as np
import pandas as pd


def rank_pct(series):
    """rank-pct（0~1，稳健、不受极端值影响），缺失/无穷/非正按要求处理后的 NaN 记 0。"""
    s = pd.to_numeric(series, errors='coerce')
    s = s.replace([np.inf, -np.inf], np.nan)
    return s.rank(pct=True).fillna(0.0)


def ff8_components(data):
    """保留 v2 的 8 项结构，修正负基数改善方向与两期流动资产平均。"""
    def last(name):
        return pd.to_numeric(data[name].iloc[-1], errors='coerce')
    def ratio(a, b):
        return a / b.where(b > 0)
    avg_assets = (last('total_current_assets') + last('total_current_assets_1')) / 2.0
    avg_assets_old = (last('total_current_assets_4') + last('total_current_assets_5')) / 2.0
    r2 = ratio(last('total_profit') + last('financial_expense'), avg_assets)
    r2_old = ratio(last('total_profit_4') + last('financial_expense_4'), avg_assets_old)
    lever = ratio(last('total_non_current_liability'), last('total_non_current_assets'))
    lever_old = ratio(last('total_non_current_liability_4'), last('total_non_current_assets_4'))
    liquid = ratio(last('total_current_assets'), last('total_current_liability'))
    liquid_old = ratio(last('total_current_assets_4'), last('total_current_liability_4'))
    turnover = ratio(last('operating_revenue'), avg_assets)
    turnover_old = ratio(last('operating_revenue_4'), avg_assets_old)
    return pd.DataFrame({
        'DELTA_ROE': last('roe') - last('roe_4'),
        'DELTA_ROA1': last('roa') - last('roa_4'),
        'ROA2': r2,
        'DELTA_ROA2': r2 - r2_old,
        'DELTA_LEVER': lever_old - lever,
        'DELTA_LIQUID': liquid - liquid_old,
        'DELTA_MARGIN': last('gross_profit_margin') - last('gross_profit_margin_4'),
        'DELTA_CATURN': turnover - turnover_old,
    }).replace([np.inf, -np.inf], np.nan)


class FF8Score(Factor):
    name = 'PojingFF8'
    max_window = 1
    dependencies = [
        'roe', 'roe_4', 'roa', 'roa_4',
        'total_profit', 'total_profit_4', 'financial_expense', 'financial_expense_4',
        'total_current_assets', 'total_current_assets_1',
        'total_current_assets_4', 'total_current_assets_5',
        'total_non_current_liability', 'total_non_current_liability_4',
        'total_non_current_assets', 'total_non_current_assets_4',
        'total_current_liability', 'total_current_liability_4',
        'gross_profit_margin', 'gross_profit_margin_4',
        'operating_revenue', 'operating_revenue_4',
    ]

    def calc(self, data):
        metrics = ff8_components(data)
        score = metrics.gt(0).sum(axis=1).astype(float)
        return score.where(metrics.notna().all(axis=1))


class PojingStrategy:
    def __init__(self):
        self.max_positions = 8          # v3: 5 → 8
        self.max_weight = 0.15          # v3: 0.20 → 0.15
        self.target_weight = 0.148      # v3: 相对 15% 留余量
        self.max_per_industry = 2       # v3: 全组合申万一级行业上限
        self.cash_reserve = 0.01
        self.min_listed_days = 200
        self.min_market_cap = 0         # v3: 流动性门槛（0 表示关闭，单位亿元）
        self.etf = '511880.XSHG'
        self.day = None
        self.candidates = []
        self.selection_day = None
        self.selection_failed_day = None
        self.yesterday_hl = set()
        self.hold_data_ok = False
        self.pending_sells = {}
        self.tracked_orders = {}
        self.bought_today = set()
        self.sold_today = set()
        self.exit_intent_today = set()
        self.orders_ok = False
        self.cash_release_day = None
        self.cash_blocked_today = False

    def setup(self, context):
        set_benchmark('000300.XSHG')
        set_option('use_real_price', True)
        set_option('avoid_future_data', True)
        set_order_cost(OrderCost(open_tax=0, close_tax=0.001,
            open_commission=0.00012, close_commission=0.00012, min_commission=5), type='stock')
        set_order_cost(OrderCost(open_tax=0, close_tax=0,
            open_commission=0.00012, close_commission=0.00012, min_commission=5), type='fund', ref=self.etf)
        set_slippage(FixedSlippage(0.001))
        log.info('破净组合v3（收益优化版）：质量加权Top8、行业上限2、补单窗口、ETF现金管理')

    def ensure_day(self, context):
        day = str(context.current_dt.date())
        if self.day == day:
            return
        self.day = day
        self.tracked_orders = {}
        self.bought_today = set()
        self.sold_today = set()
        self.exit_intent_today = set()
        self.orders_ok = False
        self.hold_data_ok = False
        self.yesterday_hl = set()
        self.cash_blocked_today = False

    def holdings(self, context, stocks_only=False):
        return {c: p for c, p in context.portfolio.positions.items()
                if p.total_amount > 0 and (not stocks_only or c != self.etf)}

    # ---------- 委托与待卖（沿用 v2） ----------
    def sync_orders(self, context):
        self.ensure_day(context)
        try:
            orders = get_orders()
            opens = get_open_orders()
            if orders is None or opens is None:
                raise ValueError('委托查询返回None')
            for oid, meta in self.tracked_orders.items():
                o = orders.get(oid)
                if o is not None and o.filled > 0:
                    if meta['buy']:
                        self.bought_today.add(meta['code'])
                    else:
                        self.sold_today.add(meta['code'])
            positions = self.holdings(context)
            open_codes = {o.security for o in opens.values()}
            for code, task in list(self.pending_sells.items()):
                if code in open_codes:
                    continue
                p = positions.get(code)
                if p is None:
                    del self.pending_sells[code]
                elif task['weight'] > 0:
                    price = get_current_data()[code].last_price
                    if np.isfinite(price) and price > 0:
                        if p.total_amount * price <= context.portfolio.portfolio_value * task['weight']:
                            del self.pending_sells[code]
            self.orders_ok = True
            return opens
        except Exception as e:
            self.orders_ok = False
            log.error('委托状态异常，暂停新增委托：%s', e)
            return None

    def track(self, o, code, buy):
        if o is None:
            log.warn('委托未受理 %s %s', code, '买' if buy else '卖')
            return
        self.tracked_orders[o.order_id] = {'code': code, 'buy': buy}
        if o.filled > 0:
            (self.bought_today if buy else self.sold_today).add(code)
        log.info('委托 %s %s 数量=%s 已成交=%s，未成交部分不视为完成',
                 code, '买' if buy else '卖', o.amount, o.filled)

    def cancel_buys(self, context, code=None):
        opens = self.sync_orders(context)
        if opens is None:
            self.cash_blocked_today = True
            return
        for o in list(opens.values()):
            if o.is_buy and (code is None or o.security == code):
                try:
                    cancel_order(o)
                except Exception as e:
                    self.cash_blocked_today = True
                    log.error('撤买单失败 %s：%s', o.security, e)
        self.sync_orders(context)

    def queue_sell(self, context, code, weight, reason):
        previous = self.pending_sells.get(code)
        if previous is None or weight < previous['weight']:
            self.pending_sells[code] = {'weight': float(weight), 'reason': reason}
        self.exit_intent_today.add(code)
        self.cancel_buys(context, code)

    def process_sells(self, context, only_code=None):
        opens = self.sync_orders(context)
        if opens is None:
            return
        open_codes = {o.security for o in opens.values()}
        current = get_current_data()
        for code, task in list(self.pending_sells.items()):
            if only_code is not None and code != only_code:
                continue
            p = self.holdings(context).get(code)
            if p is None or code in open_codes:
                continue
            d = current[code]
            if d.paused or p.closeable_amount <= 0:
                log.info('待卖 %s（%s）：停牌/T+1无可卖数量，跨日保留', code, task['reason'])
                continue
            if not np.isfinite(d.last_price) or d.last_price <= 0:
                continue
            if d.last_price <= d.low_limit:
                log.info('待卖 %s：跌停，等待后续检查', code)
                continue
            desired = 0
            if task['weight'] > 0:
                desired = bi.int(context.portfolio.portfolio_value * task['weight'] / d.last_price / 100) * 100
            amount = bi.min(bi.int(p.closeable_amount), bi.max(0, bi.int(p.total_amount) - desired))
            if amount <= 0:
                continue
            try:
                self.track(order(code, -amount), code, False)
            except Exception as e:
                log.error('卖单异常 %s：%s；保留任务', code, e)
        self.sync_orders(context)

    # ---------- 盘前 / 可交易判断 ----------
    def before_open(self, context):
        self.ensure_day(context)
        self.sync_orders(context)
        codes = list(self.holdings(context, True))
        if not codes:
            self.hold_data_ok = True
            return
        try:
            df = get_price(codes, end_date=context.previous_date, count=1,
                           frequency='daily', fields=['close', 'high_limit'],
                           panel=False, skip_paused=False, fq=None)
            if df is None or df.empty or df['code'].nunique() < len(codes):
                raise ValueError('昨日持仓行情缺失')
            self.yesterday_hl = set(df.loc[df['close'] == df['high_limit'], 'code'])
            self.hold_data_ok = True
        except Exception as e:
            self.hold_data_ok = False
            log.error('昨日涨停状态未知，暂停月度换仓：%s', e)

    def tradable(self, code, buy=False):
        d = get_current_data()[code]
        if d.paused or not np.isfinite(d.last_price) or d.last_price <= 0:
            return False
        if buy and (d.is_st or '退' in d.name or d.last_price >= d.high_limit):
            return False
        return d.last_price > d.low_limit

    # ---------- 基本面 / 因子 / 股息 ----------
    def fundamentals(self, codes, asof):
        chunks = []
        for start in bi.range(0, len(codes), 1000):
            batch = codes[start:start + 1000]
            q = query(valuation.code, valuation.pb_ratio, valuation.market_cap,
                      indicator.adjusted_profit_to_profit, indicator.inc_net_profit_year_on_year,
                      indicator.roa, indicator.gross_profit_margin).filter(
                valuation.code.in_(batch), valuation.pb_ratio > 0, valuation.pb_ratio < 1,
                indicator.adjusted_profit_to_profit > 80,
                indicator.inc_net_profit_year_on_year > 0)
            if self.min_market_cap and self.min_market_cap > 0:
                q = q.filter(valuation.market_cap >= self.min_market_cap)
            df = get_fundamentals(q, date=asof)
            if df is None:
                raise ValueError('基本面查询失败')
            if not df.empty:
                chunks.append(df)
        if not chunks:
            return pd.DataFrame()
        df = pd.concat(chunks, ignore_index=True).drop_duplicates('code').set_index('code')
        return df.replace([np.inf, -np.inf], np.nan)

    def query_dividends(self, codes, start, end):
        q = query(finance.STK_XR_XD.code, finance.STK_XR_XD.a_registration_date,
                  finance.STK_XR_XD.bonus_amount_rmb).filter(
            finance.STK_XR_XD.code.in_(codes),
            finance.STK_XR_XD.a_registration_date > start,
            finance.STK_XR_XD.a_registration_date <= end).limit(4000)
        frame = finance.run_query(q)
        if frame is None:
            raise ValueError('分红查询失败')
        if len(frame) < 4000:
            return frame
        days = (end - start).days
        if days > 1:
            mid = start + dt.timedelta(days=days // 2)
            return pd.concat([self.query_dividends(codes, start, mid),
                              self.query_dividends(codes, mid, end)], ignore_index=True)
        if len(codes) > 1:
            half = len(codes) // 2
            return pd.concat([self.query_dividends(codes[:half], start, end),
                              self.query_dividends(codes[half:], start, end)], ignore_index=True)
        raise ValueError('单股单日分红记录触达查询上限，拒绝使用不完整结果')

    def dividend_yields(self, codes, base, asof):
        """v3：股息率 = 近1年派息总额/市值；仅"5 个完整自然年均有正派息"的股票计分，否则 0。"""
        asof_ts = pd.Timestamp(asof)
        oldest = asof_ts - pd.Timedelta(days=365 * 6)
        pieces = []
        for start in bi.range(0, len(codes), 200):
            frame = self.query_dividends(codes[start:start + 200], oldest, asof_ts)
            if frame is not None and not frame.empty:
                pieces.append(frame)
        if not pieces:
            return pd.Series(0.0, index=codes)
        records = pd.concat(pieces, ignore_index=True)
        d = records.copy()
        d['a_registration_date'] = pd.to_datetime(d['a_registration_date'], errors='coerce')
        d['bonus_amount_rmb'] = pd.to_numeric(d['bonus_amount_rmb'], errors='coerce')
        d = d.dropna(subset=['a_registration_date', 'bonus_amount_rmb'])
        d = d[(d['bonus_amount_rmb'] > 0) & (d['a_registration_date'] <= asof_ts)]
        if d.empty:
            return pd.Series(0.0, index=codes)
        # 5 个完整自然年均有正派息（asof.year-5 ~ asof.year-1）
        years = list(range(asof_ts.year - 5, asof_ts.year))
        d['cy'] = d['a_registration_date'].dt.year
        sub = d[d['cy'].isin(years)]
        if sub.empty:
            return pd.Series(0.0, index=codes)
        by_year = sub.groupby(['code', 'cy'])['bonus_amount_rmb'].sum().unstack(fill_value=0)
        by_year = by_year.reindex(columns=years, fill_value=0)
        stable = by_year.index[(by_year > 0).all(axis=1)]
        # 近 1 年派息总额 / 市值
        recent = d[(d['code'].isin(stable)) & (d['a_registration_date'] > asof_ts - pd.Timedelta(days=365))]
        recent_sum = recent.groupby('code')['bonus_amount_rmb'].sum()
        cap = pd.to_numeric(base['market_cap'].reindex(recent_sum.index), errors='coerce')
        yield_ = (recent_sum / 10000.0) / cap.where(cap > 0)
        out = pd.Series(0.0, index=codes)
        out.loc[yield_.index] = yield_.fillna(0.0).clip(lower=0.0)
        return out

    def apply_industry_cap(self, ranked_codes, asof, cap):
        if not ranked_codes:
            return []
        info = get_industry(security=list(ranked_codes), date=asof)
        result, seen = [], {}
        for c in ranked_codes:
            ind = info.get(c, {}).get('sw_l1', {}).get('industry_code')
            if ind is None:
                result.append(c)
                continue
            if seen.get(ind, 0) < cap:
                result.append(c)
                seen[ind] = seen.get(ind, 0) + 1
        return result

    def select(self, context):
        self.ensure_day(context)
        self.selection_day = None
        self.candidates = []
        try:
            asof = context.previous_date
            info = get_all_securities(types=['stock'], date=asof)
            if info is None or info.empty:
                raise ValueError('股票池为空')
            current = get_current_data()
            codes = []
            for code, row in info.iterrows():
                if code.startswith(('68', '4', '8', '92')):
                    continue
                if (pd.Timestamp(asof) - pd.Timestamp(row['start_date'])).days < self.min_listed_days:
                    continue
                d = current[code]
                if not d.paused and not d.is_st and '退' not in d.name:
                    codes.append(code)
            base = self.fundamentals(codes, asof)
            if base.empty:
                log.warn('破净池为空，本月保留原持仓，不视为清仓信号')
                self.selection_failed_day = self.day
                return
            codes = base.index.tolist()

            # ---- v3：五因子 rank-pct 等权合成总分 ----
            roa_s = rank_pct(base['roa'].reindex(codes))
            gross_s = rank_pct(base['gross_profit_margin'].reindex(codes))

            raw = get_factor_values(securities=codes, factors=['EBIT'], end_date=asof, count=1)
            if raw is None or 'EBIT' not in raw or raw['EBIT'].empty:
                raise ValueError('EBIT行情缺失')
            ebit_raw = pd.to_numeric(raw['EBIT'].iloc[-1], errors='coerce').reindex(codes)
            ebit_s = rank_pct(ebit_raw.where(ebit_raw > 0))

            ff = calc_factors(codes, [FF8Score()], start_date=asof, end_date=asof)
            if ff is None or 'PojingFF8' not in ff or ff['PojingFF8'].empty:
                raise ValueError('FF评分缺失')
            ff_raw = pd.to_numeric(ff['PojingFF8'].iloc[-1], errors='coerce').reindex(codes)
            ff_s = rank_pct(ff_raw)

            div_raw = self.dividend_yields(codes, base, asof)
            div_s = rank_pct(div_raw.where(div_raw > 0))

            scores = pd.DataFrame({'roa': roa_s, 'ebit': ebit_s, 'gross': gross_s,
                                   'ff': ff_s, 'div': div_s}).fillna(0.0)
            total = scores.mean(axis=1).sort_values(ascending=False)

            ranked = self.apply_industry_cap(total.index.tolist(), asof, self.max_per_industry)
            self.candidates = ranked[:self.max_positions]
            self.selection_day = self.day
            self.selection_failed_day = None
            log.info('质量加权选股：总分Top=%s（行业上限%d，最大%d只）',
                     self.candidates, self.max_per_industry, self.max_positions)
        except Exception as e:
            self.candidates = []
            self.selection_day = None
            self.selection_failed_day = self.day
            log.error('月度选股失败，保留持仓、不沿用旧名单：%s', e)

    # ---------- 调仓 ----------
    def enforce_limits(self, context):
        held = self.holdings(context, True)
        nav = context.portfolio.portfolio_value
        current = get_current_data()
        if len(held) > self.max_positions:
            ordered = sorted(held, key=lambda c: (c in self.candidates, c))
            for code in ordered[:len(held) - self.max_positions]:
                self.queue_sell(context, code, 0, '纠正既有股票数量超限')
        for code, p in held.items():
            price = current[code].last_price
            if np.isfinite(price) and price > 0 and nav > 0:
                if price * p.total_amount > nav * self.max_weight:
                    self.queue_sell(context, code, self.target_weight, '单股超过15%，减至14.8%附近')

    def trade_at_0930(self, context):
        self.ensure_day(context)
        self.enforce_limits(context)
        self.process_sells(context)
        if self.selection_day != self.day or not self.candidates or not self.hold_data_ok:
            return
        target = [c for c in self.candidates if self.tradable(c, True)]
        if not target:
            log.warn('当月目标均不可买，保留原持仓，不误清仓（待补单窗口）')
            return
        for code in self.holdings(context, True):
            if code not in self.candidates and code not in self.yesterday_hl:
                self.queue_sell(context, code, 0, '月度换出')
        if self.etf in self.holdings(context):
            self.queue_sell(context, self.etf, 0, '月度释放ETF资金')
        self.process_sells(context)
        self.buy_stocks(context, target)

    def buy_stocks(self, context, target):
        allowed = {'09:30', '10:05', '14:45'}   # v3：三个补单窗口
        if (context.current_dt.strftime('%H:%M') not in allowed
                or self.selection_day != self.day or not self.hold_data_ok
                or self.cash_blocked_today):
            return
        for code in target:
            opens = self.sync_orders(context)
            if opens is None:
                return
            held = self.holdings(context, True)
            if self.pending_sells or bi.any(not o.is_buy for o in opens.values()):
                log.info('仍有待卖/卖单未完成，本次不新增股票，下一次窗口再评估')
                return
            if code in self.bought_today or code in self.sold_today or code in self.exit_intent_today:
                continue
            if bi.any(o.security == code for o in opens.values()):
                continue
            reserved = {o.security for o in opens.values()
                        if o.is_buy and o.security != self.etf and o.security not in held}
            if code not in held and len(held) + len(reserved) >= self.max_positions:
                continue
            if not self.tradable(code, True):
                continue
            d = get_current_data()[code]
            nav = float(context.portfolio.portfolio_value)
            old_value = held[code].total_amount * d.last_price if code in held else 0.0
            desired = nav * bi.min(self.target_weight, 0.99 / len(target))
            headroom = bi.max(0.0, desired - old_value)
            available = bi.max(0.0, context.portfolio.available_cash - nav * self.cash_reserve)
            addition = bi.min(headroom, available * 0.99)
            if addition < d.last_price * 100 * 1.002 + 5:
                continue
            shares = bi.int(addition / (d.last_price + 0.001) / 100) * 100
            if shares <= 0:
                continue
            try:
                self.track(order(code, shares), code, True)
            except Exception as e:
                log.error('买入失败 %s：%s', code, e)

    def buy_back(self, context):
        """v3：10:05 / 14:45 补单窗口，重试买入未买齐的目标（涨停开板后可补）。"""
        self.ensure_day(context)
        if self.selection_day != self.day or not self.candidates or not self.hold_data_ok:
            return
        self.cancel_buys(context)
        self.enforce_limits(context)
        target = [c for c in self.candidates if self.tradable(c, True)]
        if not target:
            return
        self.process_sells(context)
        self.buy_stocks(context, target)

    def afternoon(self, context):
        self.ensure_day(context)
        self.cancel_buys(context)
        self.enforce_limits(context)
        for code in list(self.yesterday_hl):
            if code not in self.holdings(context, True):
                continue
            if not self.tradable(code, False):
                continue
            d = get_current_data()[code]
            if d.last_price < d.high_limit:
                self.queue_sell(context, code, 0, '昨日涨停今日检查时开板')
        self.process_sells(context)

    def release_cash(self, context):
        self.ensure_day(context)
        self.cash_release_day = self.day
        self.cancel_buys(context)
        if self.etf in self.holdings(context):
            self.queue_sell(context, self.etf, 0, '月末提前释放下月股票资金')
        self.process_sells(context, only_code=self.etf)

    def manage_cash(self, context):
        self.ensure_day(context)
        if (self.cash_release_day == self.day or self.selection_failed_day == self.day
                or self.cash_blocked_today):
            return
        opens = self.sync_orders(context)
        if opens is None or opens or self.pending_sells:
            return
        if not self.tradable(self.etf, True):
            return
        if self.etf in self.sold_today or self.etf in self.bought_today:
            return
        nav = context.portfolio.portfolio_value
        budget = bi.max(0.0, context.portfolio.available_cash - nav * self.cash_reserve) * 0.99
        price = get_current_data()[self.etf].last_price
        shares = bi.int(budget / (price + 0.001) / 100) * 100
        if shares <= 0 or budget < shares * (price + 0.001) + 5:
            return
        try:
            self.track(order(self.etf, shares), self.etf, True)
        except Exception as e:
            log.error('ETF现金归集失败：%s', e)


def initialize(context):
    g.pojing = PojingStrategy()
    g.pojing.setup(context)
    run_daily(before_market_open, time='09:00')
    run_monthly(check_stocks, 1, time='09:05')
    run_daily(my_trade, time='09:30')
    run_daily(cancel_buy_remainders, time='09:31')
    run_daily(buy_back_1005, time='10:05')     # v3：补单窗口
    run_daily(check_limit_up, time='14:00')
    run_daily(buy_back_1445, time='14:45')     # v3：补单窗口
    run_daily(check_limit_up, time='14:50')
    run_monthly(release_etf_cash, -1, time='14:55')
    run_daily(manage_cash, time='14:56')
    run_daily(cancel_buy_remainders, time='14:57')
    run_daily(end_of_day, time='15:10')


def before_market_open(context):
    g.pojing.before_open(context)

def check_stocks(context):
    g.pojing.select(context)

def my_trade(context):
    g.pojing.trade_at_0930(context)

def buy_back_1005(context):
    g.pojing.buy_back(context)

def buy_back_1445(context):
    g.pojing.buy_back(context)

def cancel_buy_remainders(context):
    g.pojing.cancel_buys(context)

def check_limit_up(context):
    g.pojing.afternoon(context)

def release_etf_cash(context):
    g.pojing.release_cash(context)

def manage_cash(context):
    g.pojing.manage_cash(context)

def end_of_day(context):
    g.pojing.cancel_buys(context)
    g.pojing.sync_orders(context)
    log.info('收盘：股票数=%d 已买=%s 已卖=%s 待卖=%s 选股日=%s',
             len(g.pojing.holdings(context, True)), sorted(g.pojing.bought_today),
             sorted(g.pojing.sold_today), g.pojing.pending_sells, g.pojing.selection_day)
