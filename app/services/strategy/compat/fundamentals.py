"""基于 ModelScope / QuantDB 财务 Parquet 的聚宽 get_fundamentals 查询。"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pandas as pd

from app.services.strategy.compat.financial_cache import load_statement, visible
from app.services.strategy.compat.query import Condition, Field, Query, evaluate

DEFAULT_ROOT = Path(
    os.environ.get(
        "MODELSCOPE_ROOT",
        "/home/dev01/project/svquant-data/free/staging/modelscope",
    )
)

#: 聚宽 (table, field) -> (数据集, 列名)；数据集为财务表时按 V1 逐股文件读取。
FINANCIAL_MAP: dict[tuple[str, str], tuple[str, str]] = {
    ("balance", "cash_equivalents"): ("balance", "cash_equivalents"),
    ("balance", "total_assets"): ("balance", "tot_assets"),
    ("balance", "total_liability"): ("balance", "tot_liab"),
    ("balance", "total_owner_equities"): ("balance", "total_equity"),
    # operating_revenue / np_parent_company_owners 是年内累计，源数据是单季度，
    # 由 _derive_income_fields 按年内累加派生（见 FINANCIAL_EXPRESSIONS）。
    ("income", "net_profit"): ("income", "net_profit_incl_min_int_inc"),
    ("cash_flow", "net_operate_cash_flow"): ("cashflow", "net_cash_flows_oper_act"),
    ("cash_flow", "fix_intan_other_asset_acqui_cash"): ("cashflow", "cash_pay_acq_const_fiolta"),
    ("indicator", "eps"): ("pershare_index", "s_fa_eps_basic"),
    ("indicator", "roa"): ("pershare_index", "roa"),
    ("indicator", "roe"): ("pershare_index", "equity_roe"),
    ("indicator", "inc_return"): ("pershare_index", "equity_roe"),
    ("indicator", "gross_profit_margin"): ("pershare_index", "gross_margin"),
    # inc_net_profit_year_on_year 改由利润表按归母口径派生（见 FINANCIAL_EXPRESSIONS）。
    ("indicator", "inc_total_revenue_year_on_year"): ("pershare_index", "inc_revenue_rate"),
    ("indicator", "ocf_to_revenue"): ("pershare_index", "operating_cf_to_revenue"),
}

#: 由利润表原始列派生的聚宽字段；净利润一律按**归母**口径（聚宽 indicator 表口径）。
FINANCIAL_EXPRESSIONS: dict[tuple[str, str], tuple[str, str]] = {
    ("income", "operating_revenue"): ("income", "营业额年内累计"),
    ("income", "np_parent_company_owners"): ("income", "归母净利润年内累计"),
    ("income", "adjusted_profit_to_profit"): (
        "income",
        "扣非归母净利润(累计) / 归母净利润(累计)",
    ),
    # 聚宽把该指标挂在 indicator 下，实际由利润表两列计算。
    ("indicator", "adjusted_profit_to_profit"): (
        "income",
        "扣非归母净利润(累计) / 归母净利润(累计)",
    ),
    ("indicator", "inc_net_profit_year_on_year"): (
        "income",
        "(归母净利润(累计) - 去年同期) / |去年同期|",
    ),
}

#: 表达式字段依赖的原始列；在 pandas 侧计算，避免每次查询都扫 Parquet。
#: ``net_profit_excl_min_int_inc`` 与 ``deducted_net_profit`` 是单季度口径，
#: 累计值必须按年内累加，不能与累计净利润直接相除。
EXPRESSION_COLUMNS: dict[tuple[str, str], tuple[str, ...]] = {
    ("income", "operating_revenue"): ("revenue",),
    ("income", "np_parent_company_owners"): ("net_profit_excl_min_int_inc",),
    ("income", "adjusted_profit_to_profit"): (
        "net_profit_incl_min_int_inc_after",
        "net_profit_excl_min_int_inc",
    ),
    ("indicator", "adjusted_profit_to_profit"): (
        "net_profit_incl_min_int_inc_after",
        "net_profit_excl_min_int_inc",
    ),
    ("indicator", "inc_net_profit_year_on_year"): (
        "net_profit_excl_min_int_inc",
    ),
}


def _to_single_quarter(frame: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    """把年内累计的现金流量字段差分成单季度；年初首期保持原值。"""

    data = frame.copy()
    year = data["m_timetag"].astype(str).str[:4]
    for column in dict.fromkeys(columns):
        if column not in data.columns:
            continue
        values = pd.to_numeric(data[column], errors="coerce")
        previous = values.groupby([data["Symbol"], year]).shift(1)
        data[column] = (values - previous).where(previous.notna(), values)
    return data

def _derive_income_fields(frame: pd.DataFrame) -> pd.DataFrame:
    """在利润表全量可见行上派生聚宽口径字段（索引与入参一致）。

    - 归母净利润累计：``net_profit_excl_min_int_inc`` 是单季度，按年内累加；
    - 扣非比率：累计扣非归母 / 累计归母净利润，聚宽是百分数；
    - 净利润同比：累计归母与去年同报告期累计归母比较。
    """

    data = frame.sort_values(["Symbol", "m_timetag"], kind="stable")
    period = data["m_timetag"].astype(str)
    parent = pd.to_numeric(data["net_profit_excl_min_int_inc"], errors="coerce")
    parent_cum = parent.groupby([data["Symbol"], period.str[:4]]).cumsum()
    lookup = pd.Series(
        parent_cum.to_numpy(dtype=float),
        index=pd.MultiIndex.from_arrays([data["Symbol"], period]),
    )
    prior_period = (period.str[:4].astype(int) - 1).astype(str) + period.str[4:]
    prior = lookup.reindex(
        pd.MultiIndex.from_arrays([data["Symbol"], prior_period])
    ).to_numpy(dtype=float)
    cum = parent_cum.to_numpy(dtype=float)

    out = pd.DataFrame(index=data.index)
    if "revenue" in data.columns:
        out["operating_revenue"] = pd.to_numeric(
            data["revenue"], errors="coerce"
        ).groupby([data["Symbol"], period.str[:4]]).cumsum().to_numpy(dtype=float)
    if "net_profit_excl_min_int_inc" in data.columns:
        out["np_parent_company_owners"] = cum
    if "net_profit_incl_min_int_inc_after" in data.columns:
        deducted = pd.to_numeric(
            data["net_profit_incl_min_int_inc_after"], errors="coerce"
        ).to_numpy(dtype=float)
        ratio = np.full(cum.shape, np.nan)
        np.divide(deducted * 100.0, cum, out=ratio, where=cum != 0)
        out["adjusted_profit_to_profit"] = ratio
    growth = np.full(cum.shape, np.nan)
    np.divide(
        (cum - prior) * 100.0,
        np.abs(prior),
        out=growth,
        where=np.isfinite(prior) & (prior != 0),
    )
    out["inc_net_profit_year_on_year"] = growth
    return out


VALUATION_MAP: dict[str, str] = {
    "code": "symbol",
    "market_cap": "total_mv",
    "circulating_market_cap": "float_mv",
    "pe_ratio": "pe_ttm",
    "pb_ratio": "pb",
    "ps_ratio": "ps_ttm",
}


def _condition_fields(condition: Condition | None) -> list[Field]:
    if condition is None:
        return []
    if condition.op in {"and", "or"}:
        return _condition_fields(condition.left) + _condition_fields(condition.right)
    return [condition.left] if isinstance(condition.left, Field) else []


class FundamentalStore:
    """按公告日 PIT 查询财务与估值。"""

    def __init__(self, root: str | Path = DEFAULT_ROOT) -> None:
        self.root = Path(root)
        self.fin = self.root / "3_financial_data"
        self.valuation = self.root / "5_technical_derived" / "valuation"
        self._valuation_cache: dict[str, pd.DataFrame] = {}

    def query(self, q: Query, *, as_of: str) -> pd.DataFrame:
        as_of_key = as_of.replace("-", "")
        frame = pd.DataFrame()

        needed = list(q.fields)
        needed.extend(order.field for order in q.orders)
        needed.extend(_condition_fields(q.where))
        needed = list(dict.fromkeys(needed))

        financial = [
            field for field in needed
            if field.name != "code"
            and (
                (field.table, field.name) in FINANCIAL_MAP
                or (field.table, field.name) in FINANCIAL_EXPRESSIONS
            )
        ]
        valuation = [field for field in needed if field.table == "valuation"]
        # 任一表的 ``code`` 都映射到证券代码，不要求财务字段映射。
        code_tables = {
            field.table
            for field in needed
            if field.name == "code" and field.table != "valuation"
        }

        if financial:
            frame = self._financial_frame(financial, as_of_key)
        if valuation:
            val = self._valuation_frame(as_of_key)
            frame = val if frame.empty else frame.merge(val, on="symbol", how="inner")

        if frame.empty:
            return pd.DataFrame(columns=["code", *[f.name for f in q.fields]])

        for table in code_tables:
            frame[f"{table}.code"] = frame["symbol"]

        if q.where is not None:
            frame = frame[evaluate(frame, q.where)]

        order_columns = [
            f"{order.field.table}.{order.field.name}" for order in q.orders
        ]
        if order_columns:
            frame = frame.sort_values(
                order_columns,
                ascending=[order.ascending for order in q.orders],
                kind="stable",
            )
        if q.limit_count is not None:
            frame = frame.head(q.limit_count)

        output = pd.DataFrame({"code": frame["symbol"]})
        for field in q.fields:
            if field.name == "code":
                continue
            output[field.name] = frame[f"{field.table}.{field.name}"].values
        return output.reset_index(drop=True)

    def _financial_frame(self, fields: list[Field], as_of: str) -> pd.DataFrame:
        by_statement: dict[str, list[Field]] = {}
        for field in fields:
            statement = (
                FINANCIAL_MAP.get((field.table, field.name))
                or FINANCIAL_EXPRESSIONS.get((field.table, field.name))
            )[0]
            by_statement.setdefault(statement, []).append(field)

        merged: pd.DataFrame | None = None
        for statement, items in by_statement.items():
            raw_columns: set[str] = set()
            for field in items:
                mapped = FINANCIAL_MAP.get((field.table, field.name))
                if mapped is not None:
                    raw_columns.add(mapped[1])
                elif (field.table, field.name) in EXPRESSION_COLUMNS:
                    raw_columns.update(EXPRESSION_COLUMNS[(field.table, field.name)])
                else:
                    raise KeyError(f"未映射字段: {field.table}.{field.name}")

            frame = visible(
                load_statement(self.root, statement, sorted(raw_columns)), as_of
            )
            if frame.empty:
                continue
            needs_derived = any(
                (field.table, field.name) in FINANCIAL_EXPRESSIONS for field in items
            )
            derived = (
                _derive_income_fields(frame)
                if statement == "income" and needs_derived
                else None
            )
            latest = (
                frame.sort_values(
                    ["Symbol", "m_timetag", "m_anntime"],
                    ascending=[True, False, False],
                    kind="stable",
                )
                .drop_duplicates("Symbol", keep="first")
            )
            output = pd.DataFrame({"symbol": latest["Symbol"].to_numpy()})
            for field in items:
                alias = f"{field.table}.{field.name}"
                mapped = FINANCIAL_MAP.get((field.table, field.name))
                if mapped is not None:
                    output[alias] = latest[mapped[1]].to_numpy()
                    continue
                if derived is None or field.name not in derived.columns:
                    raise KeyError(f"未映射字段: {field.table}.{field.name}")
                output[alias] = derived.loc[latest.index, field.name].to_numpy()
            merged = (
                output
                if merged is None
                else merged.merge(output, on="symbol", how="inner")
            )

        if merged is None:
            return pd.DataFrame()
        return merged

    def history(
        self,
        fields: list[Field],
        securities: list[str],
        *,
        watch_date: str | pd.Timestamp,
        count: int,
    ) -> pd.DataFrame:
        """聚宽 ``get_history_fundamentals`` 的最小实现。

        返回 ``code / statDate / <字段>`` 长表，只包含 ``watch_date`` 当日已公告的
        最近 ``count`` 个报告期；合并报表（``report_type=0``）只有一套，无需区分。
        """

        as_of = pd.Timestamp(watch_date).strftime("%Y%m%d")
        by_statement: dict[str, list[Field]] = {}
        for field in fields:
            mapped = FINANCIAL_MAP.get((field.table, field.name))
            if mapped is None:
                raise KeyError(f"get_history_fundamentals 未映射字段: {field.table}.{field.name}")
            by_statement.setdefault(mapped[0], []).append(field)

        merged: pd.DataFrame | None = None
        wanted = set(securities)
        for statement, items in by_statement.items():
            raw_columns = [FINANCIAL_MAP[(f.table, f.name)][1] for f in items]
            frame = visible(load_statement(self.root, statement, raw_columns), as_of)
            if frame.empty:
                continue
            frame = frame.loc[frame["Symbol"].isin(wanted)]
            if frame.empty:
                continue
            frame = frame.sort_values(["Symbol", "m_timetag"], kind="stable")
            if statement == "cashflow":
                # 聚宽 get_history_fundamentals(interval='1q') 返回单季度流量，
                # 源表是年内累计，先按年内差分。
                frame = _to_single_quarter(frame, raw_columns)
            frame = frame.groupby("Symbol", group_keys=False).tail(int(count))
            output = pd.DataFrame({"code": frame["Symbol"].to_numpy()})
            output["statDate"] = [
                f"{str(value)[:4]}-{str(value)[4:6]}-{str(value)[6:8]}"
                for value in frame["m_timetag"]
            ]
            for field in items:
                mapped = FINANCIAL_MAP[(field.table, field.name)]
                output[field.name] = pd.to_numeric(
                    frame[mapped[1]], errors="coerce"
                ).to_numpy(dtype=float)
            merged = output if merged is None else merged.merge(
                output, on=["code", "statDate"], how="inner"
            )
        if merged is None:
            return pd.DataFrame(columns=["code", "statDate"])
        return merged.reset_index(drop=True)

    def _valuation_frame(self, as_of: str) -> pd.DataFrame:
        if not self.valuation.is_dir():
            raise FileNotFoundError(f"估值数据未下载: {self.valuation}")
        cache_key = str(as_of)
        cached = self._valuation_cache.get(cache_key)
        if cached is not None:
            return cached
        target = self.valuation / f"dt={as_of}" / "data.parquet"
        if not target.is_file():
            # 取不晚于 as_of 的最近一个交易日分区，不能直接用最新分区。
            candidates = sorted(
                path
                for path in self.valuation.glob("dt=*/data.parquet")
                if path.parent.name.removeprefix("dt=") <= as_of
            )
            if not candidates:
                raise FileNotFoundError(
                    f"估值分区为空或不早于 {as_of}: {self.valuation}"
                )
            target = candidates[-1]
        columns = list(dict.fromkeys(["symbol", *VALUATION_MAP.values()]))
        frame = pd.read_parquet(target, columns=columns)
        rename = {
            column: f"valuation.{name}"
            for name, column in VALUATION_MAP.items()
            if name != "code" and column in frame.columns
        }
        frame = frame.rename(columns=rename)
        # 聚宽的 valuation.market_cap / circulating_market_cap 单位是亿元，
        # 供应商总市值/流通市值是元；统一换算，避免选股阈值失真。
        for name in ("market_cap", "circulating_market_cap"):
            column = f"valuation.{name}"
            if column in frame.columns:
                frame[column] = pd.to_numeric(
                    frame[column], errors="coerce"
                ) / 1e8
        # 保留 symbol 作为合并键，同时提供 valuation.code 供条件/排序使用。
        frame["valuation.code"] = frame["symbol"]
        self._valuation_cache[cache_key] = frame
        return frame
