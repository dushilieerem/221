from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import load_runtime_settings, reset_runtime_settings
import app.main as main_module
from app.services.data.manifest import DataManifestError, validate_data_manifest


def _write_manifest(data_dir: Path, *, version: str = "release-1") -> Path:
    data_dir.mkdir()
    asset = data_dir / "sample.parquet"
    asset.write_bytes(b"research-engine-test-data")
    digest = hashlib.sha256(asset.read_bytes()).hexdigest()
    manifest = data_dir / "data_manifest.yaml"
    manifest.write_text(
        "\n".join(
            (
                "schema_version: 2",
                f"data_version: {version}",
                "assets:",
                "- path: sample.parquet",
                f"  sha256: {digest}",
                "",
            )
        ),
        encoding="utf-8",
    )
    return manifest


def test_runtime_settings_resolve_paths_from_config_directory(tmp_path):
    data_dir = tmp_path / "marketdata"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "\n".join(
            (
                "server:",
                "  host: 127.0.0.1",
                "  port: 9530",
                "data:",
                "  dir: ./marketdata",
                "  expected_version: release-1",
                "cache:",
                "  dir: ./.runtime/cache",
                "",
            )
        ),
        encoding="utf-8",
    )
    try:
        settings = load_runtime_settings(config_path)
        assert settings.project_root == tmp_path
        assert settings.data_dir == data_dir
        assert settings.cache_dir == tmp_path / ".runtime/cache"
        assert settings.base_url == "http://127.0.0.1:9530"
    finally:
        reset_runtime_settings()


def test_data_manifest_checks_version_and_declared_files(tmp_path):
    data_dir = tmp_path / "marketdata"
    _write_manifest(data_dir)

    result = validate_data_manifest(
        data_dir,
        expected_data_version="release-1",
        verify_hashes=True,
    )
    assert result.data_dir == data_dir
    assert result.data_version == "release-1"
    assert result.asset_count == 1

    with pytest.raises(DataManifestError, match="数据版本不匹配"):
        validate_data_manifest(data_dir, expected_data_version="release-2")


def test_data_manifest_rejects_changed_asset_when_hash_check_enabled(tmp_path):
    data_dir = tmp_path / "marketdata"
    _write_manifest(data_dir)
    (data_dir / "sample.parquet").write_bytes(b"changed")

    with pytest.raises(DataManifestError, match="SHA-256 不匹配"):
        validate_data_manifest(data_dir, verify_hashes=True)


def test_fastapi_lifespan_loads_config_and_manifest_before_serving(tmp_path, monkeypatch):
    data_dir = tmp_path / "marketdata"
    _write_manifest(data_dir)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"data:\n  dir: {data_dir}\n  expected_version: release-1\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(main_module, "DEFAULT_CONFIG_PATH", config_path)

    try:
        application = main_module.create_app()
        with TestClient(application) as client:
            response = client.get("/api/health")
            assert response.status_code == 200
            assert application.state.data_manifest.data_version == "release-1"
            assert application.state.data_engine.data_dir == data_dir
    finally:
        reset_runtime_settings()
