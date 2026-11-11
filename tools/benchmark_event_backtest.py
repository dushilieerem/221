"""事件回测性能基准（离线脚本，不进服务）。

用法::

    python tools/benchmark_event_backtest.py \
        --case value_v3/dividend_v3_10w --start 2022-01-04 --end 2022-03-31 \
        --repeat 2 --hash

输出每次运行的 wall/CPU 秒数、交易日数、成交笔数、final_equity 与净值曲线
SHA-256，用于性能对比和优化前后的逐位等价性门禁。
"""

from __future__ import annotations

import argparse
import cProfile
import hashlib
import json
import os
import pstats
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="事件回测性能基准")
    parser.add_argument("--case", required=True, help="相对 cases 目录的用例，如 value_v3/dividend_v3_10w")
    parser.add_argument("--cases-dir", default=str(ROOT / "tests" / "parity" / "cases"))
    parser.add_argument("--start", default=None, help="覆盖用例起点（YYYY-MM-DD）")
    parser.add_argument("--end", default=None, help="覆盖用例终点（YYYY-MM-DD）")
    parser.add_argument("--repeat", type=int, default=1, help="同一进程内重复运行次数")
    parser.add_argument("--profile", action="store_true", help="对最后一次运行做 cProfile")
    parser.add_argument("--no-numba", action="store_true", help="关闭编译内核（RE_NUMBA_KERNELS=0）")
    parser.add_argument("--data-dir", default=None, help="固定行情目录（跳过 config.yaml 的 current 视图）")
    parser.add_argument("--json", action="store_true", help="只输出 JSON 结果")
    return parser


def equity_digest(equity) -> str:
    return hashlib.sha256(
        "\n".join(f"{point.date}:{point.value:.6f}" for point in equity or []).encode()
    ).hexdigest()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.no_numba:
        os.environ["RE_NUMBA_KERNELS"] = "0"

    from app.services.parity.loader import load_case
    from app.services.parity.runner import build_data_engine, build_request
    from app.services.strategy.backtest import run_strategy_backtest

    case = load_case(Path(args.cases_dir) / args.case)
    started = time.perf_counter()
    engine, release = build_data_engine(data_dir=args.data_dir)
    startup = time.perf_counter() - started
    request = build_request(case)
    if args.start or args.end:
        request = request.model_copy(
            update={
                "start": args.start or request.start,
                "end": args.end or request.end,
            }
        )

    results = []
    for index in range(max(1, args.repeat)):
        profiler = None
        if args.profile and index == args.repeat - 1:
            profiler = cProfile.Profile()
            profiler.enable()
        wall_start = time.perf_counter()
        cpu_start = time.process_time()
        response = run_strategy_backtest(request, data_engine=engine)
        wall = time.perf_counter() - wall_start
        cpu = time.process_time() - cpu_start
        if profiler is not None:
            profiler.disable()
            stats = pstats.Stats(profiler)
            stats.sort_stats("cumulative").print_stats(30)
        payload = {
            "run": index + 1,
            "wall_seconds": round(wall, 1),
            "cpu_seconds": round(cpu, 1),
            "error": response.error,
            "days": len(response.equity_curve or []),
            "fills": len(response.fills or []),
            "final_equity": response.final_equity,
            "equity_sha256": equity_digest(response.equity_curve),
            "release": release,
            "startup_seconds": round(startup, 1),
        }
        results.append(payload)
        if not args.json:
            print(json.dumps(payload, ensure_ascii=False))
    if args.json:
        print(json.dumps(results, ensure_ascii=False))
    return 0 if all(item["error"] is None for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
