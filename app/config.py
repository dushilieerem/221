"""ResearchEngine 的配置读取与运行时设置。

正式服务只从项目根目录的 ``config.yaml`` 读取运行配置。配置示例由
``config.example.yaml`` 提供，正式文件由部署人员手工复制和填写；本模块不
读取 ``SVQUANT_*`` 环境变量，也不支持运行时通过命令行替换配置文件。

共享的 AI Base URL、API Key 和模型名可在本地 ``config.yaml`` 中配置；
按功能区分的 AI 环境变量仍可覆盖对应配置。真实密钥不应放入版本库。
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any, Mapping

import yaml


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"


class ConfigManager:
    """统一配置管理器。"""

    def __init__(self, config: dict | None = None, config_path: str | Path | None = None) -> None:
        self.config: dict = dict(config) if config else {}
        if config_path is not None:
            self.load(config_path)

    def load(self, path: str | Path) -> None:
        """从文件加载配置（YAML 或 JSON，按扩展名判断）。"""

        config_path = Path(path)
        if not config_path.exists():
            raise FileNotFoundError(f"配置文件不存在: {config_path}")
        suffix = config_path.suffix.lower()
        text = config_path.read_text(encoding="utf-8")
        if suffix in (".yaml", ".yml"):
            data = yaml.safe_load(text) or {}
        elif suffix == ".json":
            data = json.loads(text)
        else:
            raise ValueError(
                f"不支持的配置格式 {suffix!r}，仅支持 yaml/yml/json"
            )
        if not isinstance(data, dict):
            raise ValueError(f"配置根必须是字典，得到 {type(data).__name__}")
        self.config.update(data)

    def get(self, key: str, default: Any = None) -> Any:
        """获取配置项，支持点号嵌套。"""

        node: Any = self.config
        for part in key.split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                return default
        return node

    def set(self, key: str, value: Any) -> None:
        """设置配置项，支持点号嵌套（自动创建中间字典）。"""

        parts = key.split(".")
        node = self.config
        for part in parts[:-1]:
            if part not in node or not isinstance(node[part], dict):
                node[part] = {}
            node = node[part]
        node[parts[-1]] = value

    def to_dict(self) -> dict:
        return dict(self.config)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<ConfigManager keys={list(self.config)}>"


def _section(manager: ConfigManager, name: str) -> Mapping[str, Any]:
    value = manager.get(name, {})
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError(f"配置项 {name!r} 必须是对象")
    return value


def _path(value: Any, *, base: Path, default: Path) -> Path:
    if value is None:
        return default.expanduser().resolve()
    if not isinstance(value, (str, Path)) or not str(value).strip():
        raise ValueError(f"路径配置必须是非空字符串，得到 {value!r}")
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = base / candidate
    return candidate.resolve()


def _optional_string(value: Any, name: str) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError(f"配置项 {name!r} 必须是字符串或 null")
    return value


def _bool(value: Any, name: str, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "yes", "1", "on"}:
            return True
        if normalized in {"false", "no", "0", "off"}:
            return False
    raise ValueError(f"配置项 {name!r} 必须是布尔值")


def _int(value: Any, name: str, default: int, *, minimum: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        raise ValueError(f"配置项 {name!r} 必须是整数")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"配置项 {name!r} 必须是整数") from exc
    if parsed < minimum:
        raise ValueError(f"配置项 {name!r} 必须大于等于 {minimum}")
    return parsed


@dataclass(frozen=True)
class RuntimeSettings:
    """应用启动后使用的不可变运行设置。"""

    project_root: Path
    config_path: Path
    data_dir: Path
    data_manifest: Path
    expected_data_version: str | None
    expected_manifest_sha256: str | None
    verify_data_hashes: bool
    max_workers: int
    max_queue_size: int
    log_level: str
    host: str
    port: int
    cache_enabled: bool
    cache_dir: Path
    cache_ttl_days: int
    cache_max_bytes: int
    factor_database: Path
    factor_tool: Path
    factor_dir: Path
    factor_index: Path
    strategy_dir: Path
    strategy_index: Path
    factor_backtest_database: Path
    output_path: Path
    ai_base_url: str
    ai_api_key: str | None = field(repr=False)
    ai_model: str

    @property
    def base_url(self) -> str:
        """返回本机客户端使用的 API 地址。"""

        client_host = "127.0.0.1" if self.host in {"0.0.0.0", "::"} else self.host
        return f"http://{client_host}:{self.port}"


def _from_manager(manager: ConfigManager, config_path: Path) -> RuntimeSettings:
    """把原始 YAML 映射为已校验的运行设置。"""

    base_dir = config_path.parent
    application = _section(manager, "application")
    runtime = _section(manager, "runtime")
    server = _section(manager, "server")
    data = _section(manager, "data")
    assets = _section(manager, "assets")
    cache = _section(manager, "cache")
    storage = _section(manager, "storage")
    ai = _section(manager, "ai")

    # data.dir 是独立仓库的正式配置；保留旧的 data_dir 键作为一次性迁移兼容，
    # 但不再读取任何环境变量。
    data_dir = _path(
        data.get("dir", manager.get("data_dir")),
        base=base_dir,
        default=base_dir.parent / "marketdata",
    )
    manifest_value = data.get(
        "manifest",
        manager.get("data_manifest", "data_manifest.yaml"),
    )
    data_manifest = _path(
        manifest_value,
        base=data_dir,
        default=data_dir / "data_manifest.yaml",
    )

    factor_database = _path(
        assets.get("factor_database"),
        base=base_dir,
        default=base_dir / "app/assets/factors/factor_library.db",
    )
    factor_tool = _path(
        assets.get("factor_tool"),
        base=base_dir,
        default=base_dir / "app/assets/factors/tool.py",
    )
    factor_dir = _path(
        assets.get("factor_dir"),
        base=base_dir,
        default=base_dir / "app/assets/factors/libraries",
    )
    factor_index = _path(
        assets.get("factor_index"),
        base=base_dir,
        default=base_dir / "app/assets/factors/index.yaml",
    )
    strategy_dir = _path(
        assets.get("strategy_dir"),
        base=base_dir,
        default=base_dir / "app/assets/strategies/libraries",
    )
    strategy_index = _path(
        assets.get("strategy_index"),
        base=base_dir,
        default=base_dir / "app/assets/strategies/index.yaml",
    )
    factor_backtest_database = _path(
        storage.get(
            "factor_backtest_database",
            assets.get("factor_backtest_database"),
        ),
        base=base_dir,
        default=base_dir / "app/assets/factors/factor_backtest.db",
    )
    output_path = _path(
        storage.get("output_path", manager.get("output_path")),
        base=base_dir,
        default=base_dir / "output",
    )
    ai_base_url = _optional_string(ai.get("base_url"), "ai.base_url")
    ai_api_key = _optional_string(ai.get("api_key"), "ai.api_key")
    ai_model = _optional_string(ai.get("model"), "ai.model")

    max_workers = _int(
        runtime.get("max_workers", application.get("max_workers")),
        "runtime.max_workers",
        2,
        minimum=1,
    )
    max_queue_size = _int(
        runtime.get("max_queue_size", application.get("max_queue_size")),
        "runtime.max_queue_size",
        8,
        minimum=0,
    )
    cache_ttl_days = _int(
        cache.get("ttl_days"), "cache.ttl_days", 30, minimum=0
    )
    cache_max_bytes = _int(
        cache.get("max_bytes"),
        "cache.max_bytes",
        10 * 1024**3,
        minimum=0,
    )
    port = _int(server.get("port"), "server.port", 9527, minimum=1)
    if port > 65535:
        raise ValueError("配置项 'server.port' 必须小于等于 65535")

    host = server.get("host", "0.0.0.0")
    if not isinstance(host, str) or not host.strip():
        raise ValueError("配置项 'server.host' 必须是非空字符串")

    log_level = str(runtime.get("log_level", "INFO")).upper()
    if log_level not in {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}:
        raise ValueError(
            "配置项 'runtime.log_level' 必须是 CRITICAL/ERROR/WARNING/INFO/DEBUG"
        )

    return RuntimeSettings(
        project_root=base_dir.resolve(),
        config_path=config_path,
        data_dir=data_dir,
        data_manifest=data_manifest,
        expected_data_version=_optional_string(
            data.get("expected_version", data.get("version")),
            "data.expected_version",
        ),
        expected_manifest_sha256=_optional_string(
            data.get("expected_manifest_sha256"),
            "data.expected_manifest_sha256",
        ),
        verify_data_hashes=_bool(
            data.get("verify_hashes"), "data.verify_hashes", False
        ),
        max_workers=max_workers,
        max_queue_size=max_queue_size,
        log_level=log_level,
        host=host.strip(),
        port=port,
        cache_enabled=_bool(cache.get("enabled"), "cache.enabled", True),
        cache_dir=_path(
            cache.get("dir"),
            base=base_dir,
            default=base_dir / ".runtime/backtest-cache",
        ),
        cache_ttl_days=cache_ttl_days,
        cache_max_bytes=cache_max_bytes,
        factor_database=factor_database,
        factor_tool=factor_tool,
        factor_dir=factor_dir,
        factor_index=factor_index,
        strategy_dir=strategy_dir,
        strategy_index=strategy_index,
        factor_backtest_database=factor_backtest_database,
        output_path=output_path,
        ai_base_url=(
            ai_base_url or "https://ark.cn-beijing.volces.com/api/coding/v3"
        ).rstrip("/"),
        ai_api_key=ai_api_key,
        ai_model=ai_model or "ark-code-latest",
    )


def default_runtime_settings() -> RuntimeSettings:
    """返回未加载正式配置时使用的开发默认值。

    该值只用于导入模块和单元测试；正式服务的 lifespan 会强制加载
    ``config.yaml``，因此不会静默启动。
    """

    return _from_manager(ConfigManager(), DEFAULT_CONFIG_PATH)


def load_runtime_settings(path: str | Path = DEFAULT_CONFIG_PATH) -> RuntimeSettings:
    """加载并校验正式配置文件。"""

    config_path = Path(path).expanduser().resolve()
    manager = ConfigManager(config_path=config_path)
    settings = _from_manager(manager, config_path)
    configure_runtime_settings(settings)
    return settings


_runtime_settings: RuntimeSettings | None = None


def configure_runtime_settings(settings: RuntimeSettings) -> RuntimeSettings:
    """设置当前进程的运行配置，供应用启动装配和测试注入使用。"""

    global _runtime_settings
    _runtime_settings = settings
    return settings


def get_runtime_settings(*, required: bool = False) -> RuntimeSettings:
    """取得当前运行配置；未启动时返回不含外部副作用的开发默认值。"""

    if _runtime_settings is not None:
        return _runtime_settings
    if required:
        raise RuntimeError("ResearchEngine 尚未加载 config.yaml")
    return default_runtime_settings()


def reset_runtime_settings() -> None:
    """清除进程级配置；仅供测试和嵌入式应用重新装配时使用。"""

    global _runtime_settings
    _runtime_settings = None


__all__ = [
    "ConfigManager",
    "DEFAULT_CONFIG_PATH",
    "PROJECT_ROOT",
    "RuntimeSettings",
    "configure_runtime_settings",
    "default_runtime_settings",
    "get_runtime_settings",
    "load_runtime_settings",
    "reset_runtime_settings",
]
