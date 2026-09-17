"""MarketData release 清单读取与启动期完整性校验。"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import re
from typing import Any, Mapping

import yaml


SUPPORTED_SCHEMA_VERSION = 2
_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")


class DataManifestError(RuntimeError):
    """外部行情 release 清单或文件完整性不符合运行契约。"""


@dataclass(frozen=True)
class DataManifest:
    """已经通过结构校验的行情 release 清单。"""

    data_dir: Path
    path: Path
    schema_version: int
    data_version: str
    asset_count: int


def release_dir(data_dir: str | Path) -> Path:
    """解析数据根目录，并兼容旧的 ``current/`` 发布包装层。"""

    root = Path(data_dir).expanduser().resolve()
    if not root.is_dir():
        raise DataManifestError(f"数据目录不存在: {root}")
    current = root / "current"
    return current if current.is_dir() else root


def _manifest_path(
    configured_path: str | Path | None,
    data_root: Path,
    resolved_data_dir: Path,
) -> Path:
    if configured_path is None:
        return resolved_data_dir / "data_manifest.yaml"

    candidate = Path(configured_path).expanduser()
    if not candidate.is_absolute():
        candidate = data_root / candidate
    candidate = candidate.resolve()
    if candidate.is_file():
        return candidate

    # 如果配置指向包含 current/ 的发布根，允许清单随 current 一起切换。
    fallback = resolved_data_dir / candidate.name
    return fallback if fallback.is_file() else candidate


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_data_manifest(
    data_dir: str | Path,
    *,
    manifest_path: str | Path | None = None,
    expected_schema_version: int = SUPPORTED_SCHEMA_VERSION,
    expected_data_version: str | None = None,
    expected_manifest_sha256: str | None = None,
    verify_hashes: bool = False,
) -> DataManifest:
    """读取并校验外部行情清单。

    默认执行快速的版本和文件声明校验；当 ``verify_hashes`` 为真时，还会按
    清单逐个计算资产 SHA-256。这样部署可以选择严格完整性检查，同时不会
    把 9GB 级行情文件的哈希耗时隐含到每次本地开发启动中。
    """

    data_root = Path(data_dir).expanduser().resolve()
    resolved_data_dir = release_dir(data_root)
    path = _manifest_path(manifest_path, data_root, resolved_data_dir)
    if not path.is_file():
        raise DataManifestError(f"数据清单不存在: {path}")

    if expected_manifest_sha256 is not None:
        if not _SHA256.fullmatch(expected_manifest_sha256):
            raise DataManifestError(
                "配置项 data.expected_manifest_sha256 必须是 64 位十六进制 SHA-256"
            )
        actual_manifest_hash = _sha256(path)
        if actual_manifest_hash.lower() != expected_manifest_sha256.lower():
            raise DataManifestError(
                "数据清单 SHA-256 不匹配: "
                f"expected={expected_manifest_sha256}, actual={actual_manifest_hash}"
            )

    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise DataManifestError(f"无法读取数据清单: {path}: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise DataManifestError("数据清单根节点必须是对象")

    schema_version = payload.get("schema_version")
    if isinstance(schema_version, bool) or not isinstance(schema_version, int):
        raise DataManifestError("数据清单缺少有效的 schema_version")
    if expected_schema_version is not None and schema_version != expected_schema_version:
        raise DataManifestError(
            "数据清单 schema_version 不匹配: "
            f"expected={expected_schema_version}, actual={schema_version}"
        )

    data_version = payload.get("data_version")
    if not isinstance(data_version, str) or not data_version.strip():
        raise DataManifestError("数据清单缺少有效的 data_version")
    data_version = data_version.strip()
    if expected_data_version is not None and data_version != expected_data_version:
        raise DataManifestError(
            "数据版本不匹配: "
            f"expected={expected_data_version}, actual={data_version}"
        )

    assets = payload.get("assets")
    if not isinstance(assets, list) or not assets:
        raise DataManifestError("数据清单 assets 必须是非空数组")

    for index, asset in enumerate(assets):
        if not isinstance(asset, Mapping):
            raise DataManifestError(f"数据清单 assets[{index}] 必须是对象")
        relative_path = asset.get("path")
        if not isinstance(relative_path, str) or not relative_path.strip():
            raise DataManifestError(f"数据清单 assets[{index}] 缺少 path")
        relative = Path(relative_path)
        if relative.is_absolute():
            raise DataManifestError(
                f"数据清单 assets[{index}] 的 path 必须是相对路径: {relative_path}"
            )
        asset_path = (resolved_data_dir / relative).resolve()
        try:
            asset_path.relative_to(resolved_data_dir)
        except ValueError as exc:
            raise DataManifestError(
                f"数据清单 assets[{index}] 的 path 越出数据目录: {relative_path}"
            ) from exc
        if not asset_path.is_file():
            raise DataManifestError(f"数据文件不存在: {asset_path}")

        declared_hash = asset.get("sha256")
        if not isinstance(declared_hash, str) or not _SHA256.fullmatch(declared_hash):
            raise DataManifestError(
                f"数据清单 assets[{index}] 缺少有效的 sha256: {relative_path}"
            )
        if verify_hashes:
            actual_hash = _sha256(asset_path)
            if actual_hash.lower() != declared_hash.lower():
                raise DataManifestError(
                    f"数据文件 SHA-256 不匹配: {asset_path} "
                    f"expected={declared_hash}, actual={actual_hash}"
                )

    return DataManifest(
        data_dir=resolved_data_dir,
        path=path,
        schema_version=schema_version,
        data_version=data_version,
        asset_count=len(assets),
    )


__all__ = [
    "DataManifest",
    "DataManifestError",
    "SUPPORTED_SCHEMA_VERSION",
    "release_dir",
    "validate_data_manifest",
]
