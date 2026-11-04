"""行情 release 监视与自动重载测试。"""

from __future__ import annotations

from dataclasses import replace

import pandas as pd
from fastapi.testclient import TestClient

import app.config as config_module
from app.config import (
    configure_runtime_settings,
    default_runtime_settings,
    reset_runtime_settings,
)
from app.main import create_app
from app.services.data.engine import DataEngine
from app.services.data.release_watcher import DataReleaseWatcher, reload_data_release


def _write_close(data_dir, values):
    data_dir.mkdir(parents=True, exist_ok=True)
    dates = pd.date_range("2024-01-02", periods=len(values), freq="D")
    pd.DataFrame({"datetime": dates, "A": values, "B": values}).to_parquet(
        data_dir / "close.parquet",
        index=False,
    )


def _write_manifest(data_dir, version):
    (data_dir / "data_manifest.yaml").write_text(
        "\n".join(
            [
                "schema_version: 2",
                f"data_version: {version}",
                "assets:",
                "- path: close.parquet",
                f'  sha256: "{"0" * 64}"',
            ]
        ),
        encoding="utf-8",
    )


def _settings_for(data_dir):
    return replace(
        default_runtime_settings(),
        data_dir=data_dir,
        data_manifest=data_dir / "data_manifest.yaml",
    )


def test_current_release_signature_tracks_disk_changes(tmp_path):
    _write_close(tmp_path, [10.0, 11.0])
    engine = DataEngine()
    engine.load_parquet(tmp_path)
    provider = engine.provider

    assert provider.current_release_signature() == provider.release_id

    _write_close(tmp_path, [10.0, 11.0, 12.0])

    assert provider.current_release_signature() != provider.release_id


def test_watcher_reloads_after_signature_stabilizes(tmp_path):
    _write_close(tmp_path, [10.0, 11.0])
    _write_manifest(tmp_path, "v1")
    engine = DataEngine()
    engine.load_parquet(tmp_path)
    old_release = engine.release_id
    assert engine.get_task_panels(fields=["close"])["close"].iloc[-1, 0] == 11.0

    watcher = DataReleaseWatcher(engine, _settings_for(tmp_path))
    _write_close(tmp_path, [10.0, 11.0, 12.0])
    _write_manifest(tmp_path, "v2")

    # 首次发现变化：数据可能仍在写入，只记录不切换。
    assert watcher.check_once() is False
    assert engine.release_id == old_release

    # 连续两次签名一致 → 确认写入停止，原子切换。
    assert watcher.check_once() is True
    assert engine.release_id != old_release
    assert engine.get_task_panels(fields=["close"])["close"].iloc[-1, 0] == 12.0


def test_watcher_waits_while_files_keep_changing(tmp_path):
    _write_close(tmp_path, [10.0, 11.0])
    _write_manifest(tmp_path, "v1")
    engine = DataEngine()
    engine.load_parquet(tmp_path)
    old_release = engine.release_id

    watcher = DataReleaseWatcher(engine, _settings_for(tmp_path))
    _write_close(tmp_path, [10.0, 11.0, 12.0])
    assert watcher.check_once() is False
    _write_close(tmp_path, [10.0, 11.0, 12.0, 13.0])
    assert watcher.check_once() is False
    assert engine.release_id == old_release

    assert watcher.check_once() is True
    assert engine.release_id != old_release


def test_watcher_keeps_release_when_manifest_invalid(tmp_path):
    _write_close(tmp_path, [10.0, 11.0])
    _write_manifest(tmp_path, "v1")
    engine = DataEngine()
    engine.load_parquet(tmp_path)
    old_release = engine.release_id

    watcher = DataReleaseWatcher(engine, _settings_for(tmp_path))
    _write_close(tmp_path, [10.0, 11.0, 12.0])
    (tmp_path / "data_manifest.yaml").unlink()

    assert watcher.check_once() is False
    assert watcher.check_once() is False
    assert engine.release_id == old_release


def test_reload_data_release_clears_display_layer_caches(tmp_path, monkeypatch):
    from app.services.factor import market_data as market_data_module

    stale_engines = {tmp_path: object()}
    stale_names = {tmp_path: {"000001": "甲"}}
    monkeypatch.setattr(market_data_module, "_DATA_ENGINES", stale_engines)
    monkeypatch.setattr(market_data_module, "_DISPLAY_DATA_ENGINES", {})
    monkeypatch.setattr(market_data_module, "_STOCK_NAMES", stale_names)

    _write_close(tmp_path, [10.0, 11.0])
    _write_manifest(tmp_path, "v2")
    engine = DataEngine()
    engine.load_parquet(tmp_path)

    manifest = reload_data_release(engine, _settings_for(tmp_path))

    assert manifest.data_version == "v2"
    assert stale_engines == {}
    assert stale_names == {}


def test_factor_cache_key_is_bound_to_release_identity(tmp_path):
    from app.database.results.cache_impl import FileBacktestCache
    from app.models.factor_backtest import FactorBacktestSpec
    from app.services.factor.runner import _factor_cache_key

    cache = FileBacktestCache(tmp_path / "cache")
    spec = FactorBacktestSpec(
        factor_name="factor",
        factor_source_hash="source",
        library_names=("library",),
        universe="CSI_ALL",
        start="2024-01-02",
        end="2024-01-05",
    )

    class _Context:
        def __init__(self, release_id, data_dir):
            self.release_id = release_id
            self.data_dir = data_dir

    same_release = _factor_cache_key(
        spec, cache, data_context=_Context("release-a", tmp_path / "one")
    )
    same_release_other_dir = _factor_cache_key(
        spec, cache, data_context=_Context("release-a", tmp_path / "two")
    )
    other_release = _factor_cache_key(
        spec, cache, data_context=_Context("release-b", tmp_path / "one")
    )

    assert same_release == same_release_other_dir
    assert same_release != other_release


def test_data_reload_route_switches_release(tmp_path):
    _write_close(tmp_path, [10.0, 11.0])
    _write_manifest(tmp_path, "v1")
    engine = DataEngine()
    engine.load_parquet(tmp_path)
    old_release = engine.release_id

    previous_engine = DataEngine._instance
    previous_settings = config_module._runtime_settings
    try:
        application = create_app(testing=True)
        application.state.data_engine = engine
        DataEngine.configure_instance(engine)
        configure_runtime_settings(_settings_for(tmp_path))

        with TestClient(application) as client:
            status = client.get("/api/data/status")
            assert status.status_code == 200
            assert status.json()["release_id"] == old_release

            _write_close(tmp_path, [10.0, 11.0, 12.0])
            _write_manifest(tmp_path, "v2")

            reloaded = client.post("/api/data/reload")
            assert reloaded.status_code == 200
            assert reloaded.json()["data_version"] == "v2"
            assert reloaded.json()["release_id"] != old_release
    finally:
        DataEngine._instance = previous_engine
        if previous_settings is None:
            reset_runtime_settings()
        else:
            configure_runtime_settings(previous_settings)


def test_watcher_reloads_when_symlink_points_to_new_release(tmp_path):
    """配置路径是符号链接时，视图轮换（重指链接）也应触发重载。"""

    first = tmp_path / "rv-1"
    second = tmp_path / "rv-2"
    _write_close(first, [10.0, 11.0])
    _write_manifest(first, "v1")
    _write_close(second, [10.0, 11.0, 12.0])
    _write_manifest(second, "v2")

    current = tmp_path / "current"
    current.symlink_to(first, target_is_directory=True)

    engine = DataEngine()
    # 生产环境里清单校验会把目录解析成绝对路径再交给 provider，
    # 这里同样传入解析后的路径，模拟“进程持有旧发布路径”。
    engine.load_parquet(first)
    assert engine.get_task_panels(fields=["close"])["close"].iloc[-1, 0] == 11.0

    settings = replace(
        default_runtime_settings(),
        data_dir=current,
        data_manifest=current / "data_manifest.yaml",
    )
    watcher = DataReleaseWatcher(engine, settings)

    # 指向新视图：即便旧目录还在，也应按配置路径切换。
    current.unlink()
    current.symlink_to(second, target_is_directory=True)

    assert watcher.check_once() is True
    assert engine.get_task_panels(fields=["close"])["close"].iloc[-1, 0] == 12.0
