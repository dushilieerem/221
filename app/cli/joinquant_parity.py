"""聚宽对账基线 CLI。

    python -m app.cli.joinquant_parity list
    python -m app.cli.joinquant_parity validate --case fixed_basket_hold
    python -m app.cli.joinquant_parity run --case fixed_basket_hold
    python -m app.cli.joinquant_parity compare --case fixed_basket_hold

退出码：0 通过；1 存在严格分歧；2 输入/配置/金标错误。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from app.services.parity.compare import compare_runs
from app.services.parity.jq_ui_export import import_returns, import_ui_export
from app.services.parity.loader import (
    GoldenRun,
    ParityError,
    golden_available,
    golden_dir,
    load_case,
    load_golden,
)
from app.services.parity.normalizer import from_response, load_response
from app.services.parity.report import write_report
from app.services.parity.runner import build_data_engine, run_case
from app.services.parity.schema import CaseSpec

DEFAULT_CASES_DIR = Path(__file__).resolve().parents[2] / "tests" / "parity" / "cases"
DEFAULT_OUTPUT_DIR = Path("output") / "joinquant-parity"


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except ParityError as exc:
        _error(str(exc))
        return 2
    except FileNotFoundError as exc:
        _error(f"文件不存在: {exc}")
        return 2
    except Exception as exc:  # noqa: BLE001 - CLI 统一转成退出码
        _error(f"{type(exc).__name__}: {exc}")
        return 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="聚宽回测对账基线")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--cases-dir", default=str(DEFAULT_CASES_DIR), help="用例目录")
    sub = parser.add_subparsers(dest="command", required=True)

    listing = sub.add_parser("list", parents=[common], help="列出用例")
    listing.set_defaults(handler=_cmd_list)

    validate = sub.add_parser("validate", parents=[common], help="校验用例与金标 schema")
    validate.add_argument("--case", required=True)
    validate.add_argument("--json", action="store_true")
    validate.set_defaults(handler=_cmd_validate)

    imports = sub.add_parser(
        "import-golden", parents=[common], help="导入聚宽页面导出（收益概述/交易详情/持仓&收益）"
    )
    imports.add_argument("--case", required=True)
    imports.add_argument("--from", dest="source", required=True, help="聚宽导出文件所在目录")
    imports.add_argument("--backtest-id", default=None, help="可选：回测 ID，写入 provenance")
    imports.add_argument("--out", default=None, help="输出目录；省略时写用例 expected/")
    imports.add_argument("--json", action="store_true")
    imports.set_defaults(handler=_cmd_import_golden)

    returns_import = sub.add_parser(
        "import-returns",
        parents=[common],
        help="只导入收益曲线（result_1.csv），用于收益层对账",
    )
    returns_import.add_argument("--case", required=True)
    returns_import.add_argument("--from", dest="source", required=True, help="result_1.csv 路径")
    returns_import.add_argument("--initial-cash", type=float, default=None)
    returns_import.add_argument("--out", default=None, help="输出目录；省略时写用例 expected/")
    returns_import.add_argument("--json", action="store_true")
    returns_import.set_defaults(handler=_cmd_import_returns)

    run = sub.add_parser("run", parents=[common], help="运行 SVQuant 侧并落盘响应")
    run.add_argument("--case", required=True)
    run.add_argument("--data-dir", default=None, help="行情目录；省略时读取 config.yaml")
    run.add_argument("--config", default=None, help="config.yaml 路径")
    run.add_argument("--out", default=None, help="输出目录；省略时写 output/joinquant-parity")
    run.add_argument("--json", action="store_true")
    run.set_defaults(handler=_cmd_run)

    compare = sub.add_parser("compare", parents=[common], help="比对并生成报告")
    compare.add_argument("--case", required=True)
    compare.add_argument("--data-dir", default=None, help="行情目录；省略时读取 config.yaml")
    compare.add_argument("--config", default=None, help="config.yaml 路径")
    compare.add_argument("--out", default=None, help="输出目录；省略时写 output/joinquant-parity")
    compare.add_argument("--svquant-result", default=None, help="复用已落盘的响应 JSON")
    compare.add_argument("--json", action="store_true")
    compare.set_defaults(handler=_cmd_compare)
    return parser


# ---------------------------------------------------------------- 命令


def _cmd_list(args: argparse.Namespace) -> int:
    cases_dir = Path(args.cases_dir)
    rows: list[dict[str, Any]] = []
    if cases_dir.is_dir():
        # 公司策略按分组存放（company/<strategy>/），递归发现；用例名取相对于
        # cases 目录的路径，可直接作为 --case 参数。
        for case_file in sorted(cases_dir.rglob("case.yaml")):
            path = case_file.parent
            relative = path.relative_to(cases_dir).as_posix()
            try:
                case = load_case(path)
            except ParityError as exc:
                rows.append({"case": relative, "error": str(exc)})
                continue
            rows.append({
                "case": relative,
                "description": case.description,
                "golden": golden_available(case),
            })
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    return 0


def _cmd_validate(args: argparse.Namespace) -> int:
    case = _load_case(args)
    report: dict[str, Any] = {
        "case": case.case_id,
        "case_file": str(Path(case.path) / "case.yaml"),
        "golden_dir": str(golden_dir(case)),
        "golden_available": golden_available(case),
    }
    if golden_available(case):
        golden = load_golden(case)
        report["golden"] = {
            "daily_rows": int(len(golden.daily)),
            "position_rows": int(len(golden.positions)),
            "fill_rows": int(len(golden.fills)),
            "backtest_id": golden.meta.get("backtest_id"),
            "exported_at": golden.meta.get("exported_at"),
        }
    else:
        report["note"] = "金标未导出；validate 只校验了 case.yaml"
    _emit(report, as_json=args.json)
    return 0


def _cmd_import_returns(args: argparse.Namespace) -> int:
    case = _load_case(args)
    summary = import_returns(
        args.source,
        case,
        initial_cash=args.initial_cash,
        output_dir=args.out,
    )
    _emit(summary, as_json=args.json)
    return 0


def _cmd_import_golden(args: argparse.Namespace) -> int:
    case = _load_case(args)
    summary = import_ui_export(
        args.source,
        case,
        backtest_id=args.backtest_id,
        output_dir=args.out,
    )
    _emit(summary, as_json=args.json)
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    case = _load_case(args)
    engine, release_id = build_data_engine(
        data_dir=args.data_dir,
        config_path=args.config or _default_config_path(),
    )
    response = run_case(case, engine)
    payload = response.model_dump(mode="json")
    output_dir = _output_dir(args, case)
    output_dir.mkdir(parents=True, exist_ok=True)
    response_path = output_dir / "svquant_response.json"
    response_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    canonical = from_response(payload)
    bundle_dir = output_dir / "svquant"
    bundle_dir.mkdir(exist_ok=True)
    _write_frame(canonical.daily, bundle_dir / "daily.csv")
    _write_frame(canonical.positions, bundle_dir / "positions.csv")
    _write_frame(canonical.fills, bundle_dir / "fills.csv")

    summary = {
        "case": case.case_id,
        "status": "ok",
        "response": str(response_path),
        "data_release_id": release_id,
        "trading_days": int(len(canonical.daily)),
        "fills": int(len(canonical.fills)),
    }
    _emit(summary, as_json=args.json)
    return 0


def _cmd_compare(args: argparse.Namespace) -> int:
    case = _load_case(args)
    if not golden_available(case):
        raise ParityError(f"金标未导出: {golden_dir(case)}")
    golden = load_golden(case)

    release_id = None
    if args.svquant_result:
        payload = load_response(args.svquant_result)
        response_path = Path(args.svquant_result)
    else:
        engine, release_id = build_data_engine(
            data_dir=args.data_dir,
            config_path=args.config or _default_config_path(),
        )
        response = run_case(case, engine)
        payload = response.model_dump(mode="json")
        output_dir = _output_dir(args, case)
        output_dir.mkdir(parents=True, exist_ok=True)
        response_path = output_dir / "svquant_response.json"
        response_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    canonical = from_response(payload)
    result = compare_runs(golden, canonical, case)
    output_dir = _output_dir(args, case)
    provenance = _provenance(case, golden, release_id=release_id, response_path=response_path)
    paths = write_report(result, output_dir=output_dir, provenance=provenance)

    summary = {
        "case": result.case_id,
        "status": result.status,
        "report": str(paths["report"]),
        "divergences": str(paths["divergences"]),
        "first_divergence": (
            result.first_divergence.as_dict() if result.first_divergence else None
        ),
        "layers": [
            {
                "layer": layer.layer,
                "strict": layer.strict,
                "checks": layer.checks,
                "strict_failures": layer.strict_failures,
                "reference_failures": layer.reference_failures,
            }
            for layer in result.layers
        ],
    }
    _emit(summary, as_json=args.json)
    return 0 if result.status == "pass" else 1


# ---------------------------------------------------------------- helpers


def _load_case(args: argparse.Namespace) -> CaseSpec:
    return load_case(Path(args.cases_dir) / args.case)


def _default_config_path() -> Path:
    return Path(__file__).resolve().parents[2] / "config.yaml"


def _output_dir(args: argparse.Namespace, case: CaseSpec) -> Path:
    if args.out:
        return Path(args.out)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return DEFAULT_OUTPUT_DIR / case.case_id / stamp


def _provenance(
    case: CaseSpec,
    golden: GoldenRun,
    *,
    release_id: str | None,
    response_path: Path,
) -> dict[str, Any]:
    case_file = Path(case.path) / "case.yaml"
    strategy = case.strategy_path("svquant", "svquant_strategy.py")
    return {
        "jq.backtest_id": golden.meta.get("backtest_id") or case.jq.get("backtest_id"),
        "jq.exported_at": golden.meta.get("exported_at"),
        "data_release_id": release_id,
        "case.yaml.sha256": _sha256(case_file),
        "svquant_strategy.sha256": _sha256(strategy),
        "svquant_response": str(response_path),
    }


def _sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_frame(frame: pd.DataFrame, path: Path) -> None:
    output = frame.copy()
    if "date" in output.columns:
        output["date"] = pd.to_datetime(output["date"]).dt.strftime("%Y-%m-%d")
    output.to_csv(path, index=False)


def _emit(payload: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    print(json.dumps(payload, ensure_ascii=False))


def _error(message: str) -> None:
    print(json.dumps({"error": message}, ensure_ascii=False), file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
