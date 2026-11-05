"""把聚宽回测页面的导出文件转换成对账金标格式。

聚宽页面导出（收益概述 / 交易详情 / 持仓&收益）与 ``get_backtest()`` 的列名、
结构与编码都不同，且持仓文件存在列错位（股票行比表头多一列"总资产"）。
本模块只做本地文件转换，不联网、不依赖聚宽 SDK。

输入目录需包含：
* ``result_*.csv``   收益概述：时间, 基准收益, 策略收益（累计 %）, ...
* ``transaction.csv`` 交易详情：日期, 委托时间, 标的（名称(代码)）, 交易类型, 成交数量, 成交价, 手续费, 状态
* ``position.csv``    持仓&收益：日期, 品种, 标的, 数量, 可用数量, 市值/价值, ..., [总资产], 仓位占比
"""

from __future__ import annotations

import csv
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from app.services.parity.loader import ParityError, golden_dir
from app.services.parity.schema import JQ_EXCHANGE_SUFFIX, CaseSpec

_SYMBOL_PATTERN = re.compile(r"\((\d{6})\.([A-Z]+)\)")
_DIGITS = re.compile(r"-?\d+(?:\.\d+)?")


def import_ui_export(
    source_dir: str | Path,
    case: CaseSpec,
    *,
    backtest_id: str | None = None,
    output_dir: str | Path | None = None,
) -> dict[str, Any]:
    """转换并写出 ``expected/`` 金标。"""

    source = Path(source_dir)
    if not source.is_dir():
        raise ParityError(f"聚宽导出目录不存在: {source}")

    results = _find(source, ("result_*.csv", "result_*.xlsx"))
    transactions = _find(source, ("transaction.csv", "transaction.xlsx"))
    positions_file = _find(source, ("position*.csv", "position*.xlsx"))
    if positions_file is None:
        raise ParityError(f"{source}: 缺少 position.csv / position.xlsx")

    equity_rows = _read_equity(results) if results is not None else []
    cumulative = equity_rows[-1]["cumulative_return"] if equity_rows else None
    position_rows, cash_rows, total_assets = _read_positions(positions_file)
    if not position_rows and not cash_rows:
        raise ParityError(f"{positions_file}: 未解析到持仓或现金行")

    order_rows = _read_transactions(transactions) if transactions is not None else []

    target = Path(output_dir) if output_dir is not None else golden_dir(case)
    target.mkdir(parents=True, exist_ok=True)

    initial_cash = _initial_cash(total_assets, equity_rows, case)
    daily, balances = _build_daily(
        equity_rows, cash_rows, total_assets, initial_cash=initial_cash
    )
    _write_csv(target / "daily.csv", daily, ["time", "returns"])
    _write_csv(target / "balances.csv", balances, ["time", "cash", "total_value"])
    _write_csv(
        target / "positions.csv",
        position_rows,
        ["time", "security", "amount", "closeable_amount"],
    )
    _write_csv(
        target / "orders.csv",
        order_rows,
        ["time", "security", "action", "filled", "price", "commission", "status"],
    )

    meta = {
        "schema_version": 1,
        "backtest_id": backtest_id or case.jq.get("backtest_id"),
        "exported_at": datetime.fromtimestamp(
            positions_file.stat().st_mtime, tz=timezone.utc
        ).isoformat(timespec="seconds"),
        "initial_cash": initial_cash,
        "source": {
            "directory": str(source),
            "result": results.name if results else None,
            "transaction": transactions.name if transactions else None,
            "position": positions_file.name,
        },
        "cumulative_return_reported": cumulative,
    }
    (target / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    warnings: list[str] = []
    if cumulative is not None and initial_cash and balances:
        reported = float(initial_cash) * (1.0 + cumulative / 100.0)
        actual = float(balances[-1]["total_value"])
        if abs(reported - actual) / max(actual, 1.0) > 0.001:
            warnings.append(
                f"收益概述累计收益 {cumulative}% 与持仓总资产 {actual:.2f} 不一致"
            )
    if not order_rows and transactions is None:
        warnings.append("未找到 transaction.csv，成交层将为空")

    return {
        "case": case.case_id,
        "output": str(target),
        "trading_days": len(daily),
        "position_rows": len(position_rows),
        "order_rows": len(order_rows),
        "initial_cash": initial_cash,
        "cumulative_return_reported": cumulative,
        "warnings": warnings,
    }


# ---------------------------------------------------------------- 读取


def _find(directory: Path, patterns: tuple[str, ...]) -> Path | None:
    """同时支持 CSV 与 Excel 导出；多个版本时取修改时间最新的一个。"""

    matches: list[Path] = []
    for pattern in patterns:
        matches.extend(directory.glob(pattern))
    if not matches:
        return None
    return max(matches, key=lambda item: item.stat().st_mtime)


def _read_rows(path: Path) -> list[list[str]]:
    if path.suffix.lower() in {".xlsx", ".xls"}:
        frame = pd.read_excel(path, header=None)
        rows: list[list[str]] = []
        for values in frame.itertuples(index=False):
            row: list[str] = []
            for value in values:
                if pd.isna(value):
                    row.append("")
                elif isinstance(value, float) and value.is_integer():
                    row.append(str(int(value)))
                else:
                    row.append(str(value))
            rows.append(row)
        return [row for row in rows if any(cell.strip() for cell in row)]

    for encoding in ("gbk", "utf-8-sig", "utf-8", "latin-1"):
        try:
            with path.open("r", encoding=encoding, newline="") as handle:
                return [row for row in csv.reader(handle) if row]
        except UnicodeDecodeError:
            continue
    raise ParityError(f"{path}: 无法识别文件编码")


def _read_equity(path: Path) -> list[dict[str, Any]]:
    rows = _read_rows(path)
    if not rows:
        return []
    header = rows[0]
    try:
        date_index = header.index("时间")
        return_index = header.index("策略收益")
    except ValueError as exc:
        raise ParityError(
            f"{path.name}: 收益概述缺少 时间/策略收益 列；实际列 {header}"
        ) from exc
    profit_index = header.index("当日盈利") if "当日盈利" in header else None
    loss_index = header.index("当日亏损") if "当日亏损" in header else None
    result: list[dict[str, Any]] = []
    for row in rows[1:]:
        if len(row) <= max(date_index, return_index):
            continue
        date = _date(row[date_index])
        value = _number(row[return_index])
        if date is None or value is None:
            continue
        pnl = 0.0
        if profit_index is not None and len(row) > profit_index:
            pnl += _number(row[profit_index]) or 0.0
        if loss_index is not None and len(row) > loss_index:
            pnl += _number(row[loss_index]) or 0.0
        result.append({"date": date, "cumulative_return": value, "pnl": pnl})
    return result


def _read_positions(
    path: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, float]]:
    rows = _read_rows(path)
    if not rows:
        return [], [], {}
    positions: list[dict[str, Any]] = []
    cash: list[dict[str, Any]] = []
    total_assets: dict[str, float] = {}
    for row in rows[1:]:
        if len(row) < 8:
            continue
        date = _date(row[0])
        if date is None:
            continue
        instrument = (row[2] or "").strip()
        if instrument.lower() == "cash":
            cash.append({"time": date, "cash": _number(row[7]) or 0.0})
            total_assets.setdefault(date, _number(row[7]) or 0.0)
            continue

        symbol = _symbol(row[2])
        quantity = _quantity(row[4])
        if symbol is None or quantity is None:
            continue
        market_value = _number(row[7]) or 0.0
        # 股票行比表头多一列"总资产"，位于仓位占比之前。
        assets = _number(row[15]) if len(row) >= 17 else None
        if assets is not None:
            total_assets[date] = assets
        elif date not in total_assets:
            total_assets[date] = market_value
        positions.append({
            "time": date,
            "security": symbol,
            "amount": quantity,
            "closeable_amount": _quantity(row[5]) or 0,
        })

    return positions, cash, total_assets


def _read_transactions(path: Path) -> list[dict[str, Any]]:
    rows = _read_rows(path)
    if not rows:
        return []
    result: list[dict[str, Any]] = []
    for row in rows[1:]:
        if len(row) < 14:
            continue
        symbol = _symbol(row[3])
        quantity = _quantity(row[6])
        if symbol is None or quantity is None or quantity == 0:
            continue
        side = (row[4] or "").strip()
        if side not in {"买", "卖"}:
            continue
        result.append({
            "time": _date(row[0]) or row[0],
            "security": symbol,
            "action": "buy" if side == "买" else "sell",
            "filled": quantity,
            "price": _number(row[7]),
            "commission": _number(row[12]) or 0.0,
            "status": (row[13] or "").strip(),
        })
    return result


# ---------------------------------------------------------------- 组装


def _build_daily(
    equity_rows: list[dict[str, Any]],
    cash_rows: list[dict[str, Any]],
    total_assets: dict[str, float],
    *,
    initial_cash: float | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """以持仓总资产为准；缺失时回退到收益概述的累计收益。"""

    cash_by_date = {row["time"]: row["cash"] for row in cash_rows}
    dates = sorted(set(total_assets) | set(cash_by_date) | {row["date"] for row in equity_rows})
    if not dates:
        return [], []

    initial = initial_cash
    equity_values: list[float | None] = []
    for date in dates:
        if date in total_assets:
            equity_values.append(float(total_assets[date]))
        else:
            equity_values.append(None)

    # 收益概述兜底：用累计收益还原缺失日期的权益。
    cumulative_by_date = {row["date"]: row["cumulative_return"] for row in equity_rows}
    if initial is not None:
        for index, date in enumerate(dates):
            if equity_values[index] is None and date in cumulative_by_date:
                equity_values[index] = initial * (1.0 + float(cumulative_by_date[date]) / 100.0)

    # 仍缺失时用前值填充，避免金标中断。
    filled: list[float] = []
    last = initial or 0.0
    for value in equity_values:
        last = float(value) if value is not None else last
        filled.append(last)

    daily: list[dict[str, Any]] = []
    balances: list[dict[str, Any]] = []
    previous = None
    for date, value in zip(dates, filled):
        returns = 0.0 if previous in (None, 0) else value / previous - 1.0
        daily.append({"time": date, "returns": returns})
        balances.append({
            "time": date,
            "cash": cash_by_date.get(date, float("nan")),
            "total_value": value,
        })
        previous = value
    return daily, balances


def _initial_cash(
    total_assets: dict[str, float],
    equity_rows: list[dict[str, Any]],
    case: CaseSpec,
) -> float | None:
    """首日总资产减去当日盈亏，还原建仓前本金。

    公司策略的导出从建仓当天开始，首日总资产已包含当日盈亏；直接拿它当
    初始资金会让首日收益被吞掉（例如 200,000 建仓、首日盈利 1,311.95，
    首日总资产是 201,311.95）。
    """

    if total_assets:
        first_date = min(total_assets)
        pnl = 0.0
        for row in equity_rows:
            if row.get("date") == first_date:
                pnl = float(row.get("pnl") or 0.0)
                break
        return float(total_assets[first_date]) - pnl
    value = case.jq.get("initial_cash")
    return float(value) if value is not None else None


# ---------------------------------------------------------------- 值解析


def _date(value: str) -> str | None:
    text = str(value).strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    try:
        return pd.Timestamp(text).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return None


def _number(value: Any) -> float | None:
    if value is None:
        return None
    match = _DIGITS.search(str(value).replace(",", ""))
    if match is None:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def _quantity(value: Any) -> int | None:
    number = _number(value)
    return None if number is None else int(round(number))


def _symbol(value: Any) -> str | None:
    text = str(value or "").strip()
    match = _SYMBOL_PATTERN.search(text)
    if match:
        code, suffix = match.groups()
        return f"{code}.{JQ_EXCHANGE_SUFFIX.get(suffix, suffix)}"
    if re.fullmatch(r"\d{6}\.(SH|SZ|BJ|XSHG|XSHE|XBSE)", text.upper()):
        code, suffix = text.upper().split(".")
        return f"{code}.{JQ_EXCHANGE_SUFFIX.get(suffix, suffix)}"
    return None


def _write_csv(path: Path, rows: list[dict[str, Any]], columns: list[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def import_returns(
    source: str | Path,
    case: CaseSpec,
    *,
    initial_cash: float | None = None,
    output_dir: str | Path | None = None,
) -> dict[str, Any]:
    """只导入收益曲线（``result_1.csv``），不要求成交与持仓。

    用于策略组尚未提供三件套时的收益层对账：写出 ``daily.csv`` 与
    ``meta.json``，``positions.csv`` / ``orders.csv`` 不生成。
    """

    path = Path(source)
    if not path.is_file():
        raise ParityError(f"收益曲线文件不存在: {path}")
    equity_rows = _read_equity(path)
    if not equity_rows:
        raise ParityError(f"{path}: 未解析到时间/策略收益行")

    equity_rows.sort(key=lambda row: row["date"])
    rows: list[dict[str, Any]] = []
    previous = None
    for row in equity_rows:
        cumulative = 1.0 + float(row["cumulative_return"]) / 100.0
        returns = 0.0 if previous in (None, 0) else cumulative / previous - 1.0
        rows.append({"time": row["date"], "returns": returns})
        previous = cumulative

    initial = initial_cash if initial_cash is not None else case.jq.get("initial_cash")
    if initial is None:
        initial = _infer_initial_cash(equity_rows)
    if initial is None:
        raise ParityError("缺少初始资金：请在 case.yaml 的 jq.initial_cash 声明")

    target = Path(output_dir) if output_dir is not None else golden_dir(case)
    target.mkdir(parents=True, exist_ok=True)
    _write_csv(target / "daily.csv", rows, ["time", "returns"])

    cumulative = equity_rows[-1]["cumulative_return"]
    meta = {
        "schema_version": 1,
        "backtest_id": case.jq.get("backtest_id"),
        "exported_at": datetime.fromtimestamp(
            path.stat().st_mtime, tz=timezone.utc
        ).isoformat(timespec="seconds"),
        "initial_cash": float(initial),
        "mode": "returns_only",
        "source": {"directory": str(path.parent), "result": path.name},
        "cumulative_return_reported": cumulative,
    }
    (target / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    return {
        "case": case.case_id,
        "output": str(target),
        "mode": "returns_only",
        "trading_days": len(rows),
        "first_date": rows[0]["time"],
        "last_date": rows[-1]["time"],
        "initial_cash": float(initial),
        "cumulative_return_reported": cumulative,
        "warnings": ["只有收益曲线：L1–L3 不参与比对"],
    }


def _infer_initial_cash(equity_rows: list[dict[str, Any]]) -> float | None:
    """用前几日的当日盈亏 / 当日收益率反推初始资金。"""

    implied: list[float] = []
    previous = None
    for row in equity_rows[:20]:
        cumulative = 1.0 + float(row["cumulative_return"]) / 100.0
        if previous not in (None, 0):
            daily = cumulative / previous - 1.0
            pnl = float(row.get("pnl") or 0.0)
            if abs(daily) > 1e-4 and pnl:
                implied.append(abs(pnl / daily))
        previous = cumulative
        if len(implied) >= 5:
            break
    if not implied:
        return None
    implied.sort()
    return float(implied[len(implied) // 2])
