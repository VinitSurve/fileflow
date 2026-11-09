"""Schema-validated, per-tool JSON configuration with safe reset."""

from __future__ import annotations

import json
import shutil
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from fileflow.common.exceptions import ConfigError
from fileflow.common.fsutil import atomic_write_text
from fileflow.common.paths import ToolPaths

FieldType = Literal["bool", "int", "float", "str", "list"]

_TRUE = {"1", "true", "yes", "on", "y"}
_FALSE = {"0", "false", "no", "off", "n"}


@dataclass(frozen=True)
class Field:
    """One configuration key: type, default, constraints and documentation."""

    type: FieldType
    default: Any
    description: str
    choices: tuple[Any, ...] | None = None
    minimum: float | None = None
    maximum: float | None = None
    validator: Callable[[Any], str | None] | None = None  # returns an error message or None

    def coerce(self, key: str, raw: Any) -> Any:
        """Validate/convert a JSON value or a user-typed string."""
        value = self._convert(key, raw)
        if self.choices is not None and value not in self.choices:
            allowed = ", ".join(str(c) for c in self.choices)
            raise ConfigError(f"Invalid value for {key!r}: {value!r}", hint=f"Allowed: {allowed}")
        if self.type in ("int", "float"):
            if self.minimum is not None and value < self.minimum:
                raise ConfigError(f"{key!r} must be >= {self.minimum:g} (got {value})")
            if self.maximum is not None and value > self.maximum:
                raise ConfigError(f"{key!r} must be <= {self.maximum:g} (got {value})")
        if self.validator is not None:
            problem = self.validator(value)
            if problem:
                raise ConfigError(f"Invalid value for {key!r}: {problem}")
        return value

    def _convert(self, key: str, raw: Any) -> Any:
        bad = ConfigError(f"Invalid value for {key!r}: {raw!r} (expected {self.type})")
        if self.type == "bool":
            if isinstance(raw, bool):
                return raw
            if isinstance(raw, str) and raw.strip().lower() in _TRUE | _FALSE:
                return raw.strip().lower() in _TRUE
            raise bad
        if self.type == "int":
            if isinstance(raw, bool):
                raise bad
            try:
                return int(raw)
            except (TypeError, ValueError):
                raise bad from None
        if self.type == "float":
            if isinstance(raw, bool):
                raise bad
            try:
                return float(raw)
            except (TypeError, ValueError):
                raise bad from None
        if self.type == "str":
            if isinstance(raw, str):
                return raw
            raise bad
        if isinstance(raw, list) and all(isinstance(i, str) for i in raw):
            return list(raw)
        if isinstance(raw, str):
            return [part.strip() for part in raw.split(",") if part.strip()]
        raise bad


class ConfigSchema:
    """A flat mapping of key -> :class:`Field`."""

    def __init__(self, fields: Mapping[str, Field]) -> None:
        self.fields = dict(fields)

    def defaults(self) -> dict[str, Any]:
        return {k: (list(f.default) if isinstance(f.default, list) else f.default)
                for k, f in self.fields.items()}  # fmt: skip

    def validate(self, data: Mapping[str, Any]) -> dict[str, Any]:
        """Return defaults overlaid with validated ``data``; unknown keys are errors."""
        unknown = sorted(set(data) - set(self.fields))
        if unknown:
            raise ConfigError(
                f"Unknown configuration key(s): {', '.join(unknown)}",
                hint=f"Valid keys: {', '.join(sorted(self.fields))}",
            )
        merged = self.defaults()
        for key, raw in data.items():
            merged[key] = self.fields[key].coerce(key, raw)
        return merged


class ConfigStore:
    """Reads and writes ``config.json`` for one tool. Never creates files on read."""

    def __init__(self, paths: ToolPaths, schema: ConfigSchema) -> None:
        self.paths = paths
        self.schema = schema

    @property
    def path(self) -> Path:
        return self.paths.config_file

    def load(self) -> dict[str, Any]:
        """Defaults overlaid with the validated user file (missing file -> defaults)."""
        if not self.path.exists():
            return self.schema.defaults()
        try:
            text = self.path.read_text(encoding="utf-8")
            data = json.loads(text)
        except (OSError, UnicodeDecodeError) as exc:
            raise ConfigError(
                f"Cannot read configuration file {self.path}: {exc}",
                hint=f"Fix the file or run '{self.paths.tool} config reset'.",
            ) from exc
        except json.JSONDecodeError as exc:
            raise ConfigError(
                f"Configuration file is not valid JSON: {self.path} ({exc.msg}, line {exc.lineno})",
                hint=f"Fix the file or run '{self.paths.tool} config reset' (a backup is kept).",
            ) from exc
        if not isinstance(data, dict):
            raise ConfigError(
                f"Configuration file must contain a JSON object: {self.path}",
                hint=f"Run '{self.paths.tool} config reset' to restore defaults.",
            )
        return self.schema.validate(data)

    def get(self, key: str) -> Any:
        if key not in self.schema.fields:
            raise ConfigError(
                f"Unknown configuration key: {key}",
                hint=f"Valid keys: {', '.join(sorted(self.schema.fields))}",
            )
        return self.load()[key]

    def save(self, values: Mapping[str, Any]) -> None:
        validated = self.schema.validate(values)
        atomic_write_text(self.path, json.dumps(validated, indent=2, sort_keys=True) + "\n")

    def set(self, key: str, raw: Any) -> Any:
        """Validate and persist one value; returns the stored value."""
        if key not in self.schema.fields:
            raise ConfigError(
                f"Unknown configuration key: {key}",
                hint=f"Valid keys: {', '.join(sorted(self.schema.fields))}",
            )
        value = self.schema.fields[key].coerce(key, raw)
        current = self.load()
        current[key] = value
        self.save(current)
        return value

    def reset(self) -> Path | None:
        """Restore defaults by moving the current file aside. Returns the backup path."""
        if not self.path.exists():
            return None
        backup = self.path.with_name(f"config.json.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        counter = 0
        while backup.exists():
            counter += 1
            backup = self.path.with_name(f"{backup.name.split('.bak-')[0]}.bak-{counter}")
        shutil.move(str(self.path), str(backup))
        return backup
