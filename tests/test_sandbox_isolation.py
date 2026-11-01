# -*- coding: utf-8 -*-
"""M07：用户代码容器隔离冒烟。"""
from __future__ import annotations

import pytest

from app.services.security import sandbox


pytestmark = pytest.mark.skipif(not sandbox.available(), reason="bwrap 不可用")


def test_runs_simple_code() -> None:
    out = sandbox.run_python("print(1 + 2)")
    assert out["ok"] is True
    assert out["stdout"].strip() == "3"


def test_network_is_blocked() -> None:
    code = (
        "import socket\n"
        "s = socket.socket()\n"
        "try:\n"
        "    s.connect(('127.0.0.1', 80))\n"
        "    print('connected')\n"
        "except OSError as exc:\n"
        "    print('blocked')\n"
    )
    out = sandbox.run_python(code)
    assert out["ok"] is True
    assert "blocked" in out["stdout"]


def test_outside_files_are_not_mounted() -> None:
    code = (
        "try:\n"
        "    open('/etc/passwd').read()\n"
        "    print('read')\n"
        "except OSError:\n"
        "    print('denied')\n"
    )
    out = sandbox.run_python(code)
    assert out["ok"] is True
    assert "denied" in out["stdout"]


def test_timeout() -> None:
    out = sandbox.run_python("while True:\n    pass\n", timeout_sec=1, cpu_sec=1)
    assert out["ok"] is False
    assert out["reason"] in {"timeout", "exit_-9", "exit_137"} or "killed" in (out["stderr"] or "")
