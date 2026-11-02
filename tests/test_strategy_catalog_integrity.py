# -*- coding: utf-8 -*-
"""FEAT-022 验收：经典策略目录完整性。

校验 index.yaml 与实际文件、entrypoint、执行模式一致；不运行回测。
"""
from __future__ import annotations

import ast
from pathlib import Path

import yaml

ASSETS = Path(__file__).resolve().parents[1] / "app" / "assets" / "strategies"
INDEX = ASSETS / "index.yaml"
LIBRARIES = ASSETS / "libraries"
VALID_MODES = {"event", "vectorized"}


def _catalog() -> dict:
    raw = yaml.safe_load(INDEX.read_text(encoding="utf-8")) or {}
    return raw


def test_catalog_structure_and_files() -> None:
    raw = _catalog()
    assert raw.get("version") == 2
    strategies = raw.get("strategies") or {}
    assert len(strategies) >= 80

    seen: set[tuple[str, str]] = set()
    for key, meta in strategies.items():
        assert meta.get("name"), f"{key} 缺少 name"
        assert meta.get("strategy_type"), f"{key} 缺少 strategy_type"
        assert meta.get("execution_mode") in VALID_MODES, f"{key} execution_mode 非法"
        assert meta.get("tags"), f"{key} 缺少 tags"
        versions = meta.get("versions") or {}
        assert meta.get("current_version") in versions, f"{key} current_version 不在 versions 中"

        for ver, info in versions.items():
            filename = info.get("file")
            entrypoint = info.get("entrypoint")
            assert filename, f"{key}/{ver} 缺少 file"
            assert entrypoint, f"{key}/{ver} 缺少 entrypoint"
            path = LIBRARIES / filename
            assert path.exists(), f"{key}/{ver} 文件不存在: {filename}"
            tree = ast.parse(path.read_text(encoding="utf-8"))
            classes = {n.name for n in tree.body if isinstance(n, ast.ClassDef)}
            assert entrypoint in classes, f"{key}/{ver} 入口类不存在: {entrypoint}"
            marker = (filename, entrypoint)
            assert marker not in seen, f"重复登记: {marker}"
            seen.add(marker)


def test_expected_classic_strategies_present() -> None:
    strategies = _catalog().get("strategies") or {}
    text = " ".join(
        f"{key} {meta.get('name', '')} {' '.join(meta.get('tags') or [])}"
        for key, meta in strategies.items()
    ).lower()
    expected = [
        "turtle",
        "dual_moving_average",
        "rsi",
        "macd",
        "bollinger",
        "aroon",
        "piotroski",
        "fama",
        "kama",
        "stc",
        "rmi",
        "frama",
        "supertrend",
        "heikin",
        "ulcer",
    ]
    missing = [name for name in expected if name not in text]
    assert not missing, f"缺少预期策略: {missing}"
