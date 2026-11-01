"""聚宽对账基线（parity harness）。"""

from __future__ import annotations

from app.services.parity.compare import compare_runs
from app.services.parity.loader import (
    ParityError,
    golden_available,
    golden_dir,
    load_case,
    load_golden,
)
from app.services.parity.normalizer import CanonicalRun, from_response, load_response
from app.services.parity.report import write_report
from app.services.parity.runner import build_data_engine, run_case

__all__ = [
    "CanonicalRun",
    "ParityError",
    "compare_runs",
    "from_response",
    "golden_available",
    "golden_dir",
    "load_case",
    "load_golden",
    "load_response",
    "run_case",
    "write_report",
    "build_data_engine",
]
