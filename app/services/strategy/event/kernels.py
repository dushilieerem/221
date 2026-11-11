"""事件回测的编译内核（numba）。

只覆盖引擎/数据层的纯数值循环；聚宽策略源码本身仍是解释执行，不在此处。

开关：``RE_NUMBA_KERNELS=0`` 强制走纯 numpy 路径；未安装 numba 时自动回退。
所有内核不使用 ``fastmath``，保证与 numpy/pandas 相同的双精度结果。
"""

from __future__ import annotations

import os

import numpy as np

try:  # pragma: no cover - 取决于运行环境
    from numba import njit

    _HAS_NUMBA = True
except Exception:  # noqa: BLE001 - numba 是可选依赖
    _HAS_NUMBA = False

    def njit(*args, **kwargs):  # type: ignore[misc]
        if args and callable(args[0]) and len(args) == 1 and not kwargs:
            return args[0]

        def decorate(function):
            return function

        return decorate


def kernels_enabled() -> bool:
    """是否使用编译内核。

    实测（2026-11-11，58 日红利策略）numba 版本与纯 numpy 的整跑耗时在
    噪声范围内相同，但首次调用多 20 秒 JIT；因此默认关闭，只有显式设置
    ``RE_NUMBA_KERNELS=1`` 才启用，便于后续单点实验。
    """

    return _HAS_NUMBA and os.environ.get("RE_NUMBA_KERNELS") == "1"


@njit(cache=True)
def limit_price_values(
    previous_close: np.ndarray,
    ratios: np.ndarray,
    st_mask: np.ndarray,
    upper: bool,
) -> np.ndarray:
    """按昨收与板块/ST 比例生成涨跌停价，保留两位小数。

    ``previous_close`` 是已 shift 的 (T, N) 昨收（首行 NaN），``ratios`` 是
    (N,) 板块比例，``st_mask`` 是同日 (T, N) 的 ST 标记（ST 用 5%）。
    """

    rows = previous_close.shape[0]
    columns = previous_close.shape[1]
    out = np.empty((rows, columns), dtype=np.float64)
    for column in range(columns):
        base_ratio = ratios[column]
        for row in range(rows):
            price = previous_close[row, column]
            if not np.isfinite(price) or price <= 0:
                out[row, column] = np.nan
                continue
            ratio = 0.05 if st_mask[row, column] else base_ratio
            factor = 1.0 + ratio if upper else 1.0 - ratio
            out[row, column] = _round_two(price * factor)
    return out


@njit(inline="always")
def _round_two(value: float) -> float:
    # 与 pandas DataFrame.round(2) / numpy.round 相同的银行家舍入。
    return np.round(value, 2)


__all__ = ["kernels_enabled", "limit_price_values", "njit"]
