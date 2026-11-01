"""在聚宽研究环境运行，导出对账金标。

用法：
1. 把本文件粘贴到聚宽"研究环境"的 Notebook 中；
2. 修改 BACKTEST_ID 与 OUT_DIR；
3. 运行后，从投资研究文件树下载 OUT_DIR 下的文件；
4. 放到 tests/parity/cases/<case>/expected/（该目录不入库）。

只依赖聚宽平台 API 与 pandas，不向聚宽上传任何本地数据。
"""

import json
import os

import pandas as pd
from jqdata import *  # noqa: F401,F403 - 聚宽研究环境提供 get_backtest

BACKTEST_ID = "<在此填写回测 ID>"
OUT_DIR = "<在此填写研究环境中可写的目录，例如 /home/jquser/parity_fixed_basket_hold>"


def _frame(value):
    """把 get_backtest 返回的 list/dict/对象的集合转成 DataFrame。"""

    if isinstance(value, pd.DataFrame):
        return value
    if isinstance(value, dict):
        value = list(value.values())
    rows = []
    for item in value or []:
        if isinstance(item, dict):
            rows.append(item)
        elif hasattr(item, "__dict__"):
            rows.append(vars(item))
        else:
            rows.append({"value": item})
    return pd.DataFrame(rows)


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    bt = get_backtest(BACKTEST_ID)

    results = _frame(bt.get_results())
    results.to_csv(os.path.join(OUT_DIR, "daily.csv"), index=False)

    positions = _frame(bt.get_positions())
    positions.to_csv(os.path.join(OUT_DIR, "positions.csv"), index=False)

    orders = _frame(bt.get_orders())
    orders.to_csv(os.path.join(OUT_DIR, "orders.csv"), index=False)

    try:
        balances = _frame(bt.get_balances())
        balances.to_csv(os.path.join(OUT_DIR, "balances.csv"), index=False)
    except Exception as exc:  # noqa: BLE001 - 余额表缺失时可回退到收益推算
        print("balances 导出失败，将回退到按收益推算权益：", exc)

    params = {}
    try:
        params = bt.get_params()
        if not isinstance(params, dict):
            params = vars(params)
    except Exception as exc:  # noqa: BLE001
        print("get_params 失败：", exc)

    meta = {
        "schema_version": 1,
        "backtest_id": BACKTEST_ID,
        "exported_at": pd.Timestamp.now().isoformat(),
        "initial_cash": params.get("initial_cash"),
        "start": params.get("start_date"),
        "end": params.get("end_date"),
        "frequency": params.get("frequency"),
        "benchmark": params.get("benchmark"),
        "params": params,
    }
    with open(os.path.join(OUT_DIR, "meta.json"), "w", encoding="utf-8") as handle:
        json.dump(meta, handle, ensure_ascii=False, indent=2, default=str)

    print("已导出到", OUT_DIR)
    print("daily 列:", list(results.columns))
    print("positions 列:", list(positions.columns))
    print("orders 列:", list(orders.columns))


if __name__ == "__main__":
    main()
