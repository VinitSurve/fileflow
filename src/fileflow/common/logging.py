"""Lazy, redacting, per-tool log files under the platform log directory."""

from __future__ import annotations

import contextlib
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from typing import Any

from fileflow.common.paths import ToolPaths
from fileflow.common.safety import redact_text

_MAX_BYTES = 512 * 1024
_BACKUPS = 3


class RedactingFilter(logging.Filter):
    """Mask secrets in every record before any handler sees it."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            message = str(record.msg)
        record.msg = redact_text(message)
        record.args = None
        if record.exc_info and not record.exc_text:
            # Format the traceback now so it is redacted too (it can echo secrets).
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact_text(record.exc_text)
        return True


class LazyRotatingFileHandler(RotatingFileHandler):
    """Creates the log directory and file on the first record, with private permissions."""

    def _open(self) -> Any:
        os.makedirs(os.path.dirname(self.baseFilename), mode=0o700, exist_ok=True)
        stream = super()._open()
        with contextlib.suppress(OSError):
            os.chmod(self.baseFilename, 0o600)
        return stream

    def handleError(self, record: logging.LogRecord) -> None:
        # A read-only home or full disk must not crash or spam the user.
        return None


def get_logger(tool: str, *, verbose: bool = False) -> logging.Logger:
    """Return the logger for ``tool``; handlers are attached once per process."""
    logger = logging.getLogger(f"devtoolkit.{tool}")
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    if not getattr(logger, "_devtoolkit_configured", False):
        paths = ToolPaths.for_tool(tool)
        handler = LazyRotatingFileHandler(
            paths.log_file, maxBytes=_MAX_BYTES, backupCount=_BACKUPS, encoding="utf-8", delay=True
        )
        handler.setLevel(logging.INFO)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        handler.addFilter(RedactingFilter())
        logger.addHandler(handler)
        logger._devtoolkit_configured = True  # type: ignore[attr-defined]
    if verbose and not getattr(logger, "_devtoolkit_verbose", False):
        stream = logging.StreamHandler(sys.stderr)
        stream.setLevel(logging.DEBUG)
        stream.setFormatter(logging.Formatter("[log] %(levelname)s %(message)s"))
        stream.addFilter(RedactingFilter())
        logger.addHandler(stream)
        logger._devtoolkit_verbose = True  # type: ignore[attr-defined]
    return logger
