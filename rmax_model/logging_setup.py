import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import PathsConfig

_CONFIGURED = False


def get_logger(name: str, paths: PathsConfig | None = None) -> logging.Logger:
    global _CONFIGURED
    logger = logging.getLogger(name)
    if paths is not None and not _CONFIGURED:
        paths.resolved_log_path.parent.mkdir(parents=True, exist_ok=True)
        root = logging.getLogger("rmax_model")
        root.setLevel(logging.INFO)
        file_handler = RotatingFileHandler(
            paths.resolved_log_path, maxBytes=5_000_000, backupCount=3
        )
        stream_handler = logging.StreamHandler()
        fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        file_handler.setFormatter(fmt)
        stream_handler.setFormatter(fmt)
        root.addHandler(file_handler)
        root.addHandler(stream_handler)
        _CONFIGURED = True
    return logger
