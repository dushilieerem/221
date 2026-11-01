# -*- coding: utf-8 -*-
"""用户代码容器隔离（P0-11 / M07）。

使用 bubblewrap：独立 mount/pid/net/user 命名空间，ro-bind 系统目录，
tmpfs /tmp，CPU/内存 rlimit。用于在真正执行业务回测前做一次隔离的
导入/语法冒烟；完整回测仍在进程内执行（数据/引擎耦合），默认开关关闭。
"""
from __future__ import annotations

import os
import shutil
import subprocess
from typing import Any

DEFAULT_TIMEOUT_SEC = 5
DEFAULT_MEMORY_MB = 256
DEFAULT_CPU_SEC = 2

_WRAPPER = """
import resource, sys
cpu = {cpu}
mem = {mem} * 1024 * 1024
resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
resource.setrlimit(resource.RLIMIT_FSIZE, (1 * 1024 * 1024, 1 * 1024 * 1024))
code = sys.stdin.read()
exec(compile(code, "<sandbox>", "exec"), {{"__name__": "user_code"}})
"""


def available() -> bool:
    return shutil.which("bwrap") is not None


def _command(timeout_cpu: int, memory_mb: int) -> list[str]:
    return [
        "bwrap",
        "--die-with-parent",
        "--unshare-all",
        "--ro-bind", "/usr", "/usr",
        "--ro-bind", "/lib", "/lib",
        "--ro-bind", "/lib64", "/lib64",
        "--ro-bind", "/bin", "/bin",
        "--ro-bind", "/sbin", "/sbin",
        "--proc", "/proc",
        "--dev", "/dev",
        "--tmpfs", "/tmp",
        "--chdir", "/tmp",
        "/usr/bin/python3",
        "-I",
        "-c",
        _WRAPPER.format(cpu=int(timeout_cpu), mem=int(memory_mb)),
    ]


def run_python(
    code: str,
    *,
    timeout_sec: float = DEFAULT_TIMEOUT_SEC,
    memory_mb: int = DEFAULT_MEMORY_MB,
    cpu_sec: int = DEFAULT_CPU_SEC,
) -> dict[str, Any]:
    """在沙箱中执行 Python 代码；返回 {ok, stdout, stderr, reason}。"""
    if not available():
        return {"ok": False, "reason": "bwrap_unavailable", "stdout": "", "stderr": ""}
    try:
        proc = subprocess.run(
            _command(cpu_sec, memory_mb),
            input=(code or "").encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=max(0.5, float(timeout_sec)),
            env={"PATH": "/usr/bin:/bin", "HOME": "/tmp"},
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "reason": "timeout", "stdout": "", "stderr": ""}
    except OSError as exc:
        return {"ok": False, "reason": f"spawn_error: {exc}", "stdout": "", "stderr": ""}
    return {
        "ok": proc.returncode == 0,
        "reason": None if proc.returncode == 0 else f"exit_{proc.returncode}",
        "stdout": proc.stdout.decode("utf-8", "replace")[-4000:],
        "stderr": proc.stderr.decode("utf-8", "replace")[-4000:],
    }


def validate_user_code(code: str) -> dict[str, Any]:
    """隔离冒烟：只做语法/导入期检查，不调用引擎。"""
    if (os.environ.get("SANDBOX_VALIDATE_USER_CODE", "0") or "0").strip().lower() not in {"1", "true", "yes"}:
        return {"ok": True, "skipped": True, "reason": "disabled"}
    return run_python(code)


__all__ = ["available", "run_python", "validate_user_code"]
