"""ResearchEngine 项目内资产和运行时目录。"""

from __future__ import annotations

from pathlib import Path

from app.config import PROJECT_ROOT, get_runtime_settings


def _repo_root() -> Path:
    """返回独立 ResearchEngine 仓库根目录。"""

    return PROJECT_ROOT


def _app_root() -> Path:
    return PROJECT_ROOT / "app"


def factor_library_dir() -> Path:
    """返回配置指定的旧版文件因子库目录。"""

    return get_runtime_settings().factor_dir


def factor_index_path() -> Path:
    """返回因子目录索引。"""

    return get_runtime_settings().factor_index


def factor_database_path() -> Path:
    """返回 SQLite 因子库路径。"""

    return get_runtime_settings().factor_database


def factor_tool_path() -> Path:
    """返回 SQLite 因子源码依赖的公共 ``tool.py`` 路径。"""

    return get_runtime_settings().factor_tool


def backtest_cache_dir() -> Path:
    """返回配置指定的可再生回测缓存目录。"""

    return get_runtime_settings().cache_dir


def strategy_library_dir() -> Path:
    """返回项目级策略库目录。"""

    return get_runtime_settings().strategy_dir


def strategy_index_path() -> Path:
    """返回策略目录索引。"""

    return get_runtime_settings().strategy_index


__all__ = [
    "_app_root",
    "_repo_root",
    "backtest_cache_dir",
    "factor_database_path",
    "factor_index_path",
    "factor_library_dir",
    "factor_tool_path",
    "strategy_index_path",
    "strategy_library_dir",
]
