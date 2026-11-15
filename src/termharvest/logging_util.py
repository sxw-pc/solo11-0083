"""Small logging helper; logs contain metadata, never screen content or secrets."""

from __future__ import annotations

import logging
from pathlib import Path

_FORMAT = "%(asctime)sZ %(levelname)s %(name)s %(message)s"

class _UtcFormatter(logging.Formatter):
    converter = staticmethod(lambda *_: None)  # replaced per record below

    def formatTime(self, record, datefmt=None):  # noqa: N802 - logging API
        from datetime import datetime, timezone

        stamp = datetime.fromtimestamp(record.created, timezone.utc)
        return stamp.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]

def build_logger(name: str = "termharvest", *, level: str = "INFO", log_file: str | Path | None = None) -> logging.Logger:
    """Build a logger without leaking screen text into diagnostics."""
    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    stream = logging.StreamHandler()
    stream.setFormatter(_UtcFormatter(_FORMAT))
    logger.addHandler(stream)

    if log_file is not None:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(path, encoding="utf-8")
        file_handler.setFormatter(_UtcFormatter(_FORMAT))
        logger.addHandler(file_handler)
    return logger

def safe_context(**values: object) -> dict[str, object]:
    """Drop None values from an event context for stable JSON output."""
    return {key: value for key, value in values.items() if value is not None}
