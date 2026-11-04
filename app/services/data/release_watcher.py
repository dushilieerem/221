"""行情 release 发布监视与自动重载。

数据侧每日原地更新行情目录；本模块在应用进程中按固定间隔重算数据目录的
release 签名（文件大小/修改时间的哈希，不读数据内容）。检测到签名变化且
连续两次检查一致（说明写入已经停止）后，重新校验清单并原子替换
:class:`DataEngine` 的数据来源，使新请求无需重启进程即可使用新 release。
已经持有旧 provider 的在跑任务继续按旧快照完成，不受切换影响。
"""

from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path

from app.config import RuntimeSettings
from app.services.data.engine import DataEngine
from app.services.data.manifest import (
    DataManifest,
    DataManifestError,
    validate_data_manifest,
)


logger = logging.getLogger(__name__)

#: 发布检查间隔（秒）。签名计算只 stat 文件元数据，成本可忽略。
DEFAULT_WATCH_INTERVAL_SECONDS = 60

#: 串行化重载，避免监视器与手动重载接口同时替换 provider。
_RELOAD_LOCK = threading.Lock()


def reload_data_release(
    engine: DataEngine,
    settings: RuntimeSettings,
) -> DataManifest:
    """重新校验行情清单并原子切换 DataEngine 的当前 release。

    与启动期装载共用同一套清单校验；``data.verify_hashes`` 为真时会按清单
    逐文件校验 SHA-256，成本较高。校验或装载失败时抛出异常，当前 release
    保持不变。
    """

    with _RELOAD_LOCK:
        manifest = validate_data_manifest(
            settings.data_dir,
            manifest_path=settings.data_manifest,
            expected_data_version=settings.expected_data_version,
            expected_manifest_sha256=settings.expected_manifest_sha256,
            verify_hashes=settings.verify_data_hashes,
        )
        engine.load_parquet(manifest.data_dir)
        # 展示层（行情图表、证券列表）按数据目录缓存了独立引擎，必须随
        # 主 release 一起失效，否则仍返回旧数据。
        from app.services.factor.market_data import reset_market_data_caches

        reset_market_data_caches()
        return manifest


class DataReleaseWatcher:
    """周期性检测行情数据发布切换，并触发原子重载。

    只依赖磁盘上的 release 签名，不假设数据侧的更新时间：签名连续两次
    检查保持一致且不同于当前已装载 release 时才切换，因此写入进行中
    （签名持续变化）不会触发重载。
    """

    def __init__(
        self,
        engine: DataEngine,
        settings: RuntimeSettings,
        *,
        interval_seconds: int = DEFAULT_WATCH_INTERVAL_SECONDS,
    ) -> None:
        self._engine = engine
        self._settings = settings
        self._interval_seconds = max(1, int(interval_seconds))
        self._pending_signature: str | None = None

    async def run(self) -> None:
        """后台循环；由 lifespan 启动，取消即停止。"""

        while True:
            await asyncio.sleep(self._interval_seconds)
            try:
                await asyncio.to_thread(self.check_once)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - 监视失败不得拖垮服务
                logger.exception("行情 release 检查失败")

    def check_once(self) -> bool:
        """执行一次发布检查；完成切换时返回 ``True``。"""

        # 配置里的 data.dir 可能是符号链接（如 views/current/ifind）。视图轮换
        # 后链接指向新目录，但 provider 持有的是启动时解析出的旧绝对路径，
        # 旧目录被清理后签名永远不变——必须先比较解析后的路径。
        if self._linked_release_changed():
            return self._reload()
        provider = getattr(self._engine, "provider", None)
        signature_of = getattr(provider, "current_release_signature", None)
        if provider is None or not callable(signature_of):
            return False
        signature = signature_of()
        if signature == getattr(provider, "release_id", None):
            self._pending_signature = None
            return False
        if signature != self._pending_signature:
            # 首次发现变化：数据可能仍在写入，记录签名并等待下一轮确认。
            self._pending_signature = signature
            logger.info("检测到行情数据变化，等待写入完成确认")
            return False
        try:
            manifest = reload_data_release(self._engine, self._settings)
        except (DataManifestError, OSError, ValueError, RuntimeError) as exc:
            logger.warning("行情数据已变化但重载未成功，保持当前 release: %s", exc)
            self._pending_signature = None
            return False
        logger.info(
            "已切换到新行情 release: version=%s release_id=%s",
            manifest.data_version,
            self._engine.release_id,
        )
        self._pending_signature = None
        return True

    def _linked_release_changed(self) -> bool:
        """配置路径（符号链接）解析后与已装载目录不一致即为视图轮换。"""

        configured = getattr(self._settings, "data_dir", None)
        active = getattr(self._engine, "data_dir", None)
        if configured is None or active is None:
            return False
        try:
            return Path(configured).resolve() != Path(active).resolve()
        except OSError:  # 路径暂时不可用（写入中/权限），下一轮再判断
            return False

    def _reload(self) -> bool:
        try:
            manifest = reload_data_release(self._engine, self._settings)
        except (DataManifestError, OSError, ValueError, RuntimeError) as exc:
            logger.warning("配置路径已切换到新视图但重载未成功，保持当前 release: %s", exc)
            return False
        logger.info(
            "已按配置路径切换行情 release: version=%s release_id=%s",
            manifest.data_version,
            self._engine.release_id,
        )
        self._pending_signature = None
        return True


__all__ = [
    "DEFAULT_WATCH_INTERVAL_SECONDS",
    "DataReleaseWatcher",
    "reload_data_release",
]
