# -*- coding: utf-8 -*-
"""用户代码静态护栏（P0-11 过渡层）。

不是容器沙箱；在 exec 前拒绝明显的文件/网络/系统调用与逃逸语法。
真正的 CPU/内存/网络隔离由后续容器执行器承担。
"""
from __future__ import annotations

import ast

FORBIDDEN_IMPORTS = {
    "os", "sys", "subprocess", "socket", "shutil", "pathlib", "glob",
    "ctypes", "builtins", "importlib", "pickle", "marshal", "shelve",
    "multiprocessing", "threading", "asyncio", "http", "urllib",
    "requests", "httpx", "ftplib", "smtplib", "telnetlib", "pty",
    "resource", "signal", "tempfile", "sqlite3", "psycopg", "psycopg2",
}
FORBIDDEN_NAMES = {
    "exec", "eval", "compile", "open", "__import__", "input", "breakpoint",
    "globals", "locals", "vars", "memoryview", "__builtins__",
}
FORBIDDEN_ATTRS = {
    "__subclasses__", "__globals__", "__builtins__", "__code__",
    "__closure__", "__mro__", "__bases__", "__dict__", "__getattribute__",
}
ALLOWED_IMPORT_PREFIXES = (
    "app.services.strategy", "app.models.strategy", "numpy", "pandas",
    "scipy", "math", "statistics", "datetime", "typing", "dataclasses",
    "time", "collections", "itertools", "functools",
)


class UnsafeUserCode(ValueError):
    pass


def assert_safe_user_code(code: str, *, kind: str = "user") -> None:
    try:
        tree = ast.parse(code or "")
    except SyntaxError as exc:
        raise UnsafeUserCode(f"{kind} 代码语法错误: {exc.msg}") from exc
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in FORBIDDEN_IMPORTS or not alias.name.startswith(ALLOWED_IMPORT_PREFIXES):
                    raise UnsafeUserCode(f"{kind} 代码不允许导入 {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            root = module.split(".")[0]
            if root in FORBIDDEN_IMPORTS or not module.startswith(ALLOWED_IMPORT_PREFIXES):
                raise UnsafeUserCode(f"{kind} 代码不允许从 {module} 导入")
        elif isinstance(node, ast.Name):
            if node.id in FORBIDDEN_NAMES:
                raise UnsafeUserCode(f"{kind} 代码不允许使用 {node.id}")
        elif isinstance(node, ast.Attribute):
            if node.attr in FORBIDDEN_ATTRS:
                raise UnsafeUserCode(f"{kind} 代码不允许访问 {node.attr}")
        elif isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name) and fn.id in FORBIDDEN_NAMES:
                raise UnsafeUserCode(f"{kind} 代码不允许调用 {fn.id}")


__all__ = ["UnsafeUserCode", "assert_safe_user_code"]
