"""ResearchEngine 的标准日志配置。"""

from __future__ import annotations

import logging


def configure_logging(level_name: str = "INFO") -> None:
    """配置应用命名空间日志，复用 Uvicorn 已安装的根处理器。"""

    level = getattr(logging, level_name.upper(), logging.INFO)
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(
            level=level,
            format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        )
    logging.getLogger("app").setLevel(level)
