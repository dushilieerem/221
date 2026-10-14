"""因子组合的向量化收益计算。

本模块只处理已经构造好的持仓/分组产物，不负责数据读取或因子计算。
核心路径只做一次个股收益矩阵 ``close.pct_change()``，主组合和分组
收益都从这份矩阵聚合，避免按分组重复扫描完整行情。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class GrossReturnBundle:
    """一次向量化计算得到的组合收益中间结果。"""

    asset_returns: pd.DataFrame
    held_weights: pd.DataFrame
    portfolio_returns: pd.Series
    weight_change: pd.DataFrame
    turnover: pd.Series


def calculate_gross_returns(
    close: pd.DataFrame,
    positions: pd.DataFrame,
) -> GrossReturnBundle:
    """计算不含交易成本的组合收益及换手明细。"""

    prices = close.reindex(index=positions.index, columns=positions.columns)
    asset_returns = prices.pct_change(fill_method=None)
    held = positions.ffill().fillna(0.0)
    portfolio_returns = held.mul(asset_returns).sum(axis=1)
    weight_change = held.diff().fillna(0.0)
    turnover = weight_change.abs().sum(axis=1)
    return GrossReturnBundle(
        asset_returns=asset_returns,
        held_weights=held,
        portfolio_returns=portfolio_returns,
        weight_change=weight_change,
        turnover=turnover,
    )


@dataclass(frozen=True)
class _HeldGroups:
    """每个收益日实际持有的分组（0 基）。"""

    #: 组号，未持有为 NaN
    held: "np.ndarray"
    #: 有效性掩码
    member: "np.ndarray"

    @property
    def codes(self) -> "np.ndarray":
        """有效日的组号，无效日填 -1（供 bincount 聚合用）。"""

        return np.where(self.member, self.held, -1).astype(np.intp)


def _held_group_codes(
    index: pd.Index,
    columns: pd.Index,
    group_assignments: pd.DataFrame,
    rebalance_mask: pd.Series,
    n_groups: int,
    lag: int,
) -> _HeldGroups:
    """把每个收益日映射到「上一调仓日产生的分组」。

    收益与换手都要用到这个映射，抽出来共用以免两处口径漂移。``lag`` 与主组合
    的持仓位移一致：1 表示信号日收盘成交、次日生效；2 表示再晚一天生效。
    """

    if lag < 1:
        raise ValueError(f"lag 至少为 1，得到 {lag}")
    assignments = group_assignments.reindex(index=index, columns=columns)
    assignment_values = assignments.to_numpy(dtype=float, na_value=np.nan)
    n_dates = assignment_values.shape[0]

    rebalance_values = rebalance_mask.reindex(index).to_numpy(dtype=bool)
    signal_rows = np.where(rebalance_values, np.arange(n_dates), -1)
    previous_signal = np.full(n_dates, -1, dtype=np.intp)
    if n_dates > 1:
        previous_signal[1:] = np.maximum.accumulate(signal_rows[:-1])

    # lag 把「最近一次调仓」整体前推：收益日 t 改用 t-lag 之前的最近调仓
    # 所产生的分组，等价于持仓再晚 lag-1 天生效。
    if lag > 1:
        shifted = np.full(n_dates, -1, dtype=np.intp)
        if n_dates > lag - 1:
            shifted[lag - 1:] = previous_signal[: n_dates - (lag - 1)]
        previous_signal = shifted

    held = np.full_like(assignment_values, np.nan, dtype=float)
    has_previous_signal = previous_signal >= 0
    held[has_previous_signal] = (
        assignment_values[previous_signal[has_previous_signal]] - 1.0
    )
    member = np.isfinite(held) & (held >= 0) & (held < n_groups)
    return _HeldGroups(held=held, member=member)


def calculate_group_turnover(
    index: pd.Index,
    columns: pd.Index,
    group_assignments: pd.DataFrame,
    rebalance_mask: pd.Series,
    n_groups: int,
    *,
    lag: int = 1,
) -> pd.DataFrame:
    """各分组逐日的**单边**换手，口径与主组合 ``metrics.turnover`` 一致。

    ``Σ|Δw| / 2``，逐日计算、不分位年化；调用方取均值即得日均单边换手。与
    ``calculate_group_returns`` 用同一个「持有分组」映射，所以生效日、``lag``
    与首日处理完全一致（都丢掉首个信号日，建仓当日不计换手）。

    注意分层语义：各分组是**等权诊断桶**，不是可交易策略。成员不变时换手为
    0，换手全部来自排名的进出，量级通常显著高于主组合，不宜直接当作可实现
    的交易成本。
    """

    if n_groups < 2:
        raise ValueError(f"n_groups 至少为 {n_groups}")
    labels = [f"Q{number}" for number in range(1, n_groups + 1)]
    if group_assignments.empty:
        return pd.DataFrame(index=index, columns=labels, dtype=float).iloc[1:]

    held_groups = _held_group_codes(
        index, columns, group_assignments, rebalance_mask, n_groups, lag
    )
    codes = held_groups.codes
    member = held_groups.member
    n_dates, n_symbols = codes.shape

    date_codes = np.broadcast_to(
        np.arange(n_dates, dtype=np.intp)[:, None], (n_dates, n_symbols)
    )
    counts = np.bincount(
        date_codes[member] * n_groups + codes[member],
        minlength=n_dates * n_groups,
    ).reshape(n_dates, n_groups)

    def weights(
        day_codes: "np.ndarray", day_member: "np.ndarray", day_counts: "np.ndarray"
    ) -> "np.ndarray":
        """等权持有权重：属于某组则为 1/该组成员数，否则 0。"""

        safe = np.where(day_member, day_codes, 0)
        # 按 (行, 组) 逐格取值：必须显式给出行索引。只写 day_counts[safe]
        # 或 np.take(axis=0) 都会把二维索引当成「每行取整行」，返回三维数组。
        rows = np.arange(day_codes.shape[0], dtype=np.intp)[:, None]
        denom = day_counts[rows, safe]
        return np.where(day_member, 1.0 / np.where(denom > 0, denom, 1), 0.0)

    current = weights(codes, member, counts)
    previous_codes = np.vstack(
        [np.full((1, n_symbols), -1, dtype=np.intp), codes[:-1]]
    )
    previous_member = np.vstack([np.zeros((1, n_symbols), dtype=bool), member[:-1]])
    previous_counts = np.vstack([np.zeros((1, n_groups)), counts[:-1]])
    previous = weights(previous_codes, previous_member, previous_counts)

    stayed = member & previous_member & (codes == previous_codes)
    entered = member & ~stayed
    left = previous_member & ~stayed

    def aggregate(
        mask: "np.ndarray", day_codes: "np.ndarray", values: "np.ndarray"
    ) -> "np.ndarray":
        """把 ``values`` 按 (日, 组) 归集；``values`` 的口径由调用方给定。"""

        keys = date_codes[mask] * n_groups + day_codes[mask]
        return np.bincount(
            keys, weights=values[mask], minlength=n_dates * n_groups
        ).reshape(n_dates, n_groups)

    # 换手按「组」而不是「标的自身所属组」算：标的离开某组时贡献的是它在
    # 该组的**全部旧权重**（归 0），进入某组时贡献全部新权重。
    # 留在原组的按 |新权重 − 旧权重| 计（成员数变化时两者不同）。
    total = (
        aggregate(stayed, codes, np.abs(current - previous))
        + aggregate(entered, codes, current)
        + aggregate(left, previous_codes, previous)
    ) / 2.0
    return pd.DataFrame(total, index=index, columns=labels).iloc[1:]


def calculate_group_returns(
    asset_returns: pd.DataFrame,
    group_assignments: pd.DataFrame,
    rebalance_mask: pd.Series,
    n_groups: int,
    *,
    lag: int = 1,
) -> pd.DataFrame:
    """一次性计算所有分组的等权毛收益。

    ``group_assignments[t]`` 是 t 日收盘形成的分组；收益在下一交易日
    承担，因此先用 NumPy 的索引传播得到每个交易日的上一调仓分组，
    再用 ``bincount`` 按 ``(date, group)`` 聚合收益和成员数。整个过程
    不创建每个分组的 ``T×N`` 权重表，也不包含 Python 日期/标的循环。

    ``lag`` 控制分组的生效日，与主组合的持仓位移口径一致：``1``（默认）
    表示信号日收盘成交、次日生效；``2`` 表示再晚一天生效（T+1 收盘成交）。
    """

    if n_groups < 2:
        raise ValueError(f"n_groups 至少为 2，得到 {n_groups}")
    if asset_returns.empty or group_assignments.empty:
        return pd.DataFrame(
            index=asset_returns.index,
            columns=[f"Q{number}" for number in range(1, n_groups + 1)],
            dtype=float,
        ).iloc[1:]

    returns = asset_returns.to_numpy(dtype=float, na_value=np.nan)
    n_dates, n_symbols = returns.shape

    # 持有分组映射与 calculate_group_turnover 共用，避免两处口径漂移。
    held_groups = _held_group_codes(
        asset_returns.index,
        asset_returns.columns,
        group_assignments,
        rebalance_mask,
        n_groups,
        lag,
    )
    member = held_groups.member
    group_codes = np.where(member, held_groups.held, 0).astype(np.intp, copy=False)
    date_codes = np.broadcast_to(
        np.arange(n_dates, dtype=np.intp)[:, None],
        (n_dates, n_symbols),
    )

    count_keys = date_codes[member] * n_groups + group_codes[member]
    counts = np.bincount(
        count_keys,
        minlength=n_dates * n_groups,
    ).reshape(n_dates, n_groups)

    valid_returns = member & ~np.isnan(returns)
    return_keys = date_codes[valid_returns] * n_groups + group_codes[valid_returns]
    sums = np.bincount(
        return_keys,
        weights=returns[valid_returns],
        minlength=n_dates * n_groups,
    ).reshape(n_dates, n_groups)
    group_values = np.divide(
        sums,
        counts,
        out=np.zeros_like(sums, dtype=float),
        where=counts > 0,
    )
    result = pd.DataFrame(
        group_values,
        index=asset_returns.index,
        columns=[f"Q{number}" for number in range(1, n_groups + 1)],
    )
    # 与原组合收益口径一致：首日是信号日，没有持有收益。
    return result.iloc[1:]


__all__ = [
    "GrossReturnBundle",
    "calculate_gross_returns",
    "calculate_group_returns",
    "calculate_group_turnover",
]
