"""Filesystem helpers: sizes, streaming hashes, atomic writes, collision-free names."""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import stat
import tempfile
from pathlib import Path

from fileflow.common.exceptions import UsageError

CHUNK = 1024 * 1024

_UNITS = ("B", "KB", "MB", "GB", "TB", "PB")
_SIZE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([a-zA-Z]*)\s*$")
_FACTORS = {
    "": 1, "b": 1,
    "k": 1000, "kb": 1000, "m": 1000**2, "mb": 1000**2, "g": 1000**3, "gb": 1000**3,
    "t": 1000**4, "tb": 1000**4,
    "kib": 1024, "mib": 1024**2, "gib": 1024**3, "tib": 1024**4,
}  # fmt: skip


def human_size(num_bytes: int | float) -> str:
    """Format bytes using decimal units, like macOS Finder (1 GB = 1,000,000,000 B)."""
    size = float(max(num_bytes, 0))
    for unit in _UNITS:
        if size < 1000 or unit == _UNITS[-1]:
            return f"{int(size)} B" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1000
    return f"{size:.1f} PB"  # pragma: no cover


def parse_size(text: str) -> int:
    """Parse ``100MB``, ``1.5G``, ``512KiB``, ``2048`` into bytes."""
    match = _SIZE_RE.match(text)
    if not match or match.group(2).lower() not in _FACTORS:
        raise UsageError(
            f"Invalid size: {text!r}", hint="Use forms like 100MB, 1.5GB, 512KiB or a byte count."
        )
    return int(float(match.group(1)) * _FACTORS[match.group(2).lower()])


def sha256_file(path: Path, *, chunk_size: int = CHUNK, limit: int | None = None) -> str:
    """Hash a file by streaming. ``limit`` hashes only the first ``limit`` bytes."""
    digest = hashlib.sha256()
    remaining = limit
    with path.open("rb") as handle:
        while True:
            size = chunk_size if remaining is None else min(chunk_size, remaining)
            if size <= 0:
                break
            block = handle.read(size)
            if not block:
                break
            digest.update(block)
            if remaining is not None:
                remaining -= len(block)
    return digest.hexdigest()


def atomic_write_bytes(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    """Write ``data`` to ``path`` so readers see either the old or the new file, never half."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def atomic_write_text(path: Path, text: str, *, mode: int = 0o600) -> None:
    atomic_write_bytes(path, text.encode("utf-8"), mode=mode)


def file_kind(path: Path) -> str:
    """Classify without following symlinks: file, dir, symlink, special, missing."""
    try:
        mode = path.lstat().st_mode
    except OSError:
        return "missing"
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISREG(mode):
        return "file"
    if stat.S_ISDIR(mode):
        return "dir"
    return "special"


def unique_destination(path: Path) -> Path:
    """Return ``path`` or ``name (1).ext``, ``name (2).ext`` ... that does not exist."""
    if not os.path.lexists(path):
        return path
    stem, suffix = path.stem, path.suffix
    counter = 1
    while True:
        candidate = path.with_name(f"{stem} ({counter}){suffix}")
        if not os.path.lexists(candidate):
            return candidate
        counter += 1
