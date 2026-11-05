"""把比对结果渲染成 markdown 与差异明细 CSV。"""

from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.services.parity.compare import ComparisonResult, Divergence, LayerResult


def write_report(
    result: ComparisonResult,
    *,
    output_dir: str | Path,
    provenance: dict[str, Any] | None = None,
) -> dict[str, Path]:
    """写出 ``report.md`` 与 ``divergences.csv``，返回产物路径。"""

    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)

    report_path = directory / "report.md"
    report_path.write_text(
        render_markdown(result, provenance=provenance), encoding="utf-8"
    )
    csv_path = directory / "divergences.csv"
    _write_csv(csv_path, result.all_divergences)
    return {"report": report_path, "divergences": csv_path}


def render_markdown(
    result: ComparisonResult, *, provenance: dict[str, Any] | None = None
) -> str:
    lines: list[str] = [f"# 聚宽对账报告：{result.case_id}", ""]
    lines.append(f"- 状态：**{result.status}**")
    present = {layer.layer for layer in result.layers}
    if not {"L1", "L2", "L3"} & present:
        lines.append(
            "- 对账范围：**只有收益曲线**（L0 参数/日历、L4 收益、L5 指标）；"
            "无成交与持仓，选股、成交、持仓三层未比较"
        )
    lines.append(f"- 生成时间：{datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    if provenance:
        for key, value in provenance.items():
            if value is not None:
                lines.append(f"- {key}：`{value}`")
    lines.append("")

    first = result.first_divergence
    lines.append("## 结论")
    if first is None:
        lines.append("严格层全部通过：参数、日历、标的集合、成交事件、持仓集合、收益率与指标一致。")
    else:
        lines.append(
            f"首个严格分歧：**{first.layer} / {first.date or '-'} / "
            f"{first.symbol or '-'} / {first.field}**，"
            f"聚宽={_fmt(first.expected)}，SVQuant={_fmt(first.actual)}"
            + (f"，差值={_fmt(first.diff)}" if first.diff is not None else "")
        )
    if result.warnings:
        lines.append("")
        lines.append("口径提示：")
        for warning in result.warnings:
            lines.append(f"- {warning}")
    lines.append("")

    lines.extend(_summary_table(result))
    lines.append("")

    for layer in result.layers:
        lines.extend(_layer_section(layer))

    return "\n".join(lines).rstrip() + "\n"


def _summary_table(result: ComparisonResult) -> list[str]:
    lines = [
        "## 分层结果",
        "",
        "| 层 | 严格层 | 检查数 | 严格失败 | 参考差异 | 结论 |",
        "|---|---|---|---|---|---|",
    ]
    for layer in result.layers:
        verdict = "通过" if layer.passed else "**失败**"
        lines.append(
            f"| {layer.layer} | {'是' if layer.strict else '否'} | {layer.checks} | "
            f"{layer.strict_failures} | {layer.reference_failures} | {verdict} |"
        )
    return lines


def _layer_section(layer: LayerResult) -> list[str]:
    lines = [f"## {layer.layer}", ""]
    if not layer.divergences and not layer.notes:
        lines.append("通过。")
        lines.append("")
        return lines

    strict = [item for item in layer.divergences if item.strict]
    reference = [item for item in layer.divergences if not item.strict]
    if strict:
        lines.append(f"严格分歧 {len(strict)} 条（前 20）：")
        lines.append("")
        lines.extend(_divergence_table(strict[:20]))
        lines.append("")
    if reference:
        lines.append(
            f"参考差异 {len(reference)} 条（前 {min(10, len(reference))} 条，不计入失败）："
        )
        lines.append("")
        lines.extend(_divergence_table(reference[:10]))
        lines.append("")
    for note in layer.notes:
        lines.append(f"- 注：{note}")
    if layer.notes:
        lines.append("")
    return lines


def _divergence_table(items: list[Divergence]) -> list[str]:
    lines = [
        "| 日期 | 标的 | 字段 | 聚宽 | SVQuant | 差值 | 容差 | 说明 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for item in items:
        lines.append(
            f"| {item.date or '-'} | {item.symbol or '-'} | {item.field} | "
            f"{_fmt(item.expected)} | {_fmt(item.actual)} | {_fmt(item.diff)} | "
            f"{_fmt(item.tolerance)} | {item.note or ''} |"
        )
    return lines


def _write_csv(path: Path, divergences: list[Divergence]) -> None:
    fields = [
        "layer", "date", "symbol", "field", "expected", "actual",
        "diff", "tolerance", "strict", "note",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for item in divergences:
            writer.writerow(item.as_dict())


def _fmt(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        if value != value:  # NaN
            return "-"
        return f"{value:.10g}"
    return str(value)
