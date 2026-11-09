"""Configuration schema for fileflow (stored in this tool's own config directory)."""

from __future__ import annotations

from typing import Any

from fileflow.common.config import ConfigSchema, ConfigStore, Field
from fileflow.common.exceptions import UsageError
from fileflow.common.fsutil import parse_size
from fileflow.common.paths import ToolPaths

DEFAULT_IGNORE_NAMES = [".DS_Store", ".localized", "Thumbs.db", "desktop.ini"]


def _validate_size_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return "must be a size string such as '50MB' or '1GB'"
    try:
        size = parse_size(value)
    except UsageError as exc:
        return exc.message
    if size <= 0:
        return "size threshold must be greater than 0"
    return None


SCHEMA = ConfigSchema(
    {
        "large_file_threshold": Field(
            "str",
            "50MB",
            "Files at or above this size are flagged as large during scan.",
            validator=_validate_size_str,
        ),
        "journal_retention_days": Field(
            "int",
            90,
            "Days to retain operation history in journal.db.",
            minimum=1,
            maximum=3650,
        ),
        "hash_limit_for_undo": Field(
            "int",
            50_000_000,
            "Maximum file size in bytes to compute SHA-256 content hashes for undo verification.",
            minimum=0,
            maximum=10_000_000_000,
        ),
        "watch_interval": Field(
            "float",
            5.0,
            "Default polling interval in seconds for 'fileflow watch'.",
            minimum=0.1,
            maximum=3600.0,
        ),
        "default_collision": Field(
            "str",
            "rename",
            "Default collision strategy when organizing files (rename or skip).",
            choices=("rename", "skip"),
        ),
        "ignore_names": Field(
            "list",
            list(DEFAULT_IGNORE_NAMES),
            "Filenames ignored by default unless --include-hidden is passed.",
        ),
    }
)


def load_config(paths: ToolPaths) -> dict[str, Any]:
    """Load validated configuration values for fileflow."""
    return ConfigStore(paths, SCHEMA).load()
