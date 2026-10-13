"""MarketData release 的纯领域数据结构。

磁盘上的业务域宽表在进入 ResearchEngine 后，分别表达为行情、日频基本面和
证券状态对象。每个逻辑字段保持为原生 ``DataFrame[日期 × 证券]``，不以包装
类型干扰因子和策略中的 Pandas 运算。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

import pandas as pd


DataFrame = pd.DataFrame


@dataclass(frozen=True)
class StockDailyData:
    """`market_data/market_data_hfq.parquet` 的研究用后复权行情与流动性。"""

    open: DataFrame | None = None
    high: DataFrame | None = None
    low: DataFrame | None = None
    close: DataFrame | None = None
    volume: DataFrame | None = None
    amount: DataFrame | None = None
    turnover: DataFrame | None = None


@dataclass(frozen=True)
class DailyBasicData:
    """`date_sequence/date_sequence.parquet` 的日频估值和市值字段。"""

    current_mv: DataFrame | None = None
    dividend_yield_ttm: DataFrame | None = None
    pb_mrq: DataFrame | None = None
    pcf_operating_cash_flow_ttm: DataFrame | None = None
    pe_ttm: DataFrame | None = None


#: 可投资的上市状态白名单。``listed_status`` 取其余值时一律视为不可投资：
#: ``终止上市`` / ``退市整理`` / ``暂停上市`` 是退市生命周期状态，缺失值表示
#: 该日无上市状态记录（实测与价格数据完全不重叠），未来新增的未知取值也按
#: 不可投资保守处理。判定口径由本常量唯一定义，因子侧与事件侧共同引用。
INVESTABLE_LISTED_STATUS = frozenset({"在市", "ST", "*ST"})


def is_delisted_status(value: object) -> bool:
    """判断单个 ``listed_status`` 原始文本是否表示不可投资。

    缺失值按不可投资处理。判定前做去空白，与数据源的原始文本口径一致。
    """

    if value is None or (isinstance(value, float) and pd.isna(value)):
        return True
    try:
        if pd.isna(value):
            return True
    except (TypeError, ValueError):  # pragma: no cover - 非标量输入的防御
        return True
    return str(value).strip() not in INVESTABLE_LISTED_STATUS


@dataclass(frozen=True)
class SecurityStatusData:
    """`date_sequence/date_sequence.parquet` 的日频证券状态字段。"""

    listed_status: DataFrame | None = None
    the_sw_industry_code: DataFrame | None = None
    up_and_down_status: DataFrame | None = None


@dataclass(frozen=True)
class MarketDataRelease:
    """一次只读 MarketData release 在研究计算中的领域表达。"""

    data_version: str | None
    stock: StockDailyData
    basic: DailyBasicData
    status: SecurityStatusData
    extra_fields: Mapping[str, DataFrame] = field(default_factory=dict)

    @classmethod
    def from_panels(
        cls,
        panels: Mapping[str, DataFrame],
        *,
        data_version: str | None = None,
    ) -> "MarketDataRelease":
        """从 Provider 的统一字段视图创建 release，不修改输入 DataFrame。"""

        values = dict(panels)
        stock_names = tuple(StockDailyData.__dataclass_fields__)
        basic_names = tuple(DailyBasicData.__dataclass_fields__)
        status_names = tuple(SecurityStatusData.__dataclass_fields__)
        known = set(stock_names) | set(basic_names) | set(status_names)
        return cls(
            data_version=data_version,
            stock=StockDailyData(**{name: values.get(name) for name in stock_names}),
            basic=DailyBasicData(**{name: values.get(name) for name in basic_names}),
            status=SecurityStatusData(**{name: values.get(name) for name in status_names}),
            extra_fields={name: value for name, value in values.items() if name not in known},
        )

    @property
    def fields(self) -> Mapping[str, DataFrame]:
        """按逻辑字段平铺 release，供运行期计算视图读取。"""

        groups = (self.stock, self.basic, self.status)
        values = {
            name: value
            for group in groups
            for name, value in vars(group).items()
            if value is not None
        }
        return {**values, **self.extra_fields}
