"""Per-tool configuration/data/cache/log locations and path validation."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from platformdirs import PlatformDirs

from fileflow.common.exceptions import NotFoundError, PermissionDeniedError, UsageError

APP_NAMESPACE = "devtoolkit"
#: When set, every tool stores its files below this directory instead of the
#: platform defaults. Used by tests and for relocatable setups.
HOME_OVERRIDE_ENV = "DEVTOOLKIT_HOME"


@dataclass(frozen=True)
class ToolPaths:
    """Locations used by one tool. Nothing is created until :meth:`ensure` is called.

    macOS defaults (via ``platformdirs``)::

        config/data  ~/Library/Application Support/devtoolkit/<tool>
        cache        ~/Library/Caches/devtoolkit/<tool>
        logs         ~/Library/Logs/devtoolkit/<tool>
    """

    tool: str
    config_dir: Path
    data_dir: Path
    cache_dir: Path
    log_dir: Path

    @classmethod
    def for_tool(cls, tool: str) -> ToolPaths:
        override = os.environ.get(HOME_OVERRIDE_ENV)
        if override:
            root = Path(override).expanduser()
            return cls(
                tool=tool,
                config_dir=root / "config" / tool,
                data_dir=root / "data" / tool,
                cache_dir=root / "cache" / tool,
                log_dir=root / "logs" / tool,
            )
        dirs = PlatformDirs(appname=f"{APP_NAMESPACE}/{tool}", appauthor=False)
        return cls(
            tool=tool,
            config_dir=Path(dirs.user_config_dir),
            data_dir=Path(dirs.user_data_dir),
            cache_dir=Path(dirs.user_cache_dir),
            log_dir=Path(dirs.user_log_dir),
        )

    @property
    def config_file(self) -> Path:
        return self.config_dir / "config.json"

    @property
    def log_file(self) -> Path:
        return self.log_dir / f"{self.tool}.log"

    @staticmethod
    def ensure(directory: Path) -> Path:
        """Create ``directory`` (private to the user) on demand and return it."""
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        return directory


def resolve_user_path(value: str | os.PathLike[str], *, base: Path | None = None) -> Path:
    """Expand ``~``, make absolute and normalise ``..`` without following symlinks."""
    path = Path(os.path.expanduser(os.fspath(value)))
    if not path.is_absolute():
        path = (base or Path.cwd()) / path
    return Path(os.path.abspath(path))


def require_directory(path: Path, *, what: str = "Directory") -> Path:
    """Return ``path`` if it is a readable directory, else raise a friendly error."""
    if not path.exists():
        raise NotFoundError(f"{what} does not exist: {path}", hint="Check the path and try again.")
    if not path.is_dir():
        raise UsageError(f"{what} is not a directory: {path}")
    if not os.access(path, os.R_OK | os.X_OK):
        raise PermissionDeniedError(
            f"Permission denied reading {path}",
            hint="Grant your terminal access in System Settings → Privacy & Security, "
            "or choose a different folder.",
        )
    return path


def require_file(path: Path, *, what: str = "File") -> Path:
    if not path.exists():
        raise NotFoundError(f"{what} does not exist: {path}", hint="Check the path and try again.")
    if not path.is_file():
        raise UsageError(f"{what} is not a regular file: {path}")
    if not os.access(path, os.R_OK):
        raise PermissionDeniedError(f"Permission denied reading {path}")
    return path


def is_within(child: Path, parent: Path) -> bool:
    """True if ``child`` is ``parent`` or lies below it (after resolving symlinks)."""
    try:
        resolved_child = child.resolve()
        resolved_parent = parent.resolve()
    except (OSError, RuntimeError):
        return False
    return resolved_child == resolved_parent or resolved_parent in resolved_child.parents


# Locations that tools must never delete from, move, or reorganise.
_SYSTEM_ROOTS = (
    "/System",
    "/usr",
    "/bin",
    "/sbin",
    "/etc",
    "/private/etc",
    "/dev",
    "/Library",
    "/Applications",
    "/cores",
    "/opt/homebrew",
)
_SYSTEM_EXCEPTIONS = ("/usr/local",)
_EXACT_PROTECTED = ("/", "/Users", "/Volumes", "/private", "/opt", "/var", "/tmp")  # noqa: S108
_HOME_PROTECTED = (".ssh", ".gnupg", ".aws", ".config/gcloud", ".kube")
_LIBRARY_ALLOWED = ("Caches", "Logs")


def protected_reason(path: Path) -> str | None:
    """Explain why ``path`` is off-limits for destructive operations, or ``None``."""
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError):
        return "the path cannot be resolved safely"
    text = str(resolved)
    home = Path.home().resolve()

    if text in _EXACT_PROTECTED:
        return "it is a top-level system location"
    if resolved == home:
        return "it is your home folder itself"
    for exc in _SYSTEM_EXCEPTIONS:
        if text == exc or text.startswith(exc + "/"):
            return None
    for root in _SYSTEM_ROOTS:
        if text == root or text.startswith(root + "/"):
            return f"it is inside the macOS/system location {root}"
    for rel in _HOME_PROTECTED:
        guarded = home / rel
        if resolved == guarded or guarded in resolved.parents:
            return f"it holds credentials or keys (~/{rel})"
    library = home / "Library"
    if resolved == library:
        return "it is your ~/Library folder"
    if library in resolved.parents:
        sub = resolved.relative_to(library).parts[0]
        if sub not in _LIBRARY_ALLOWED:
            return "it is application support data inside ~/Library"
    return None


def ensure_not_protected(path: Path, action: str) -> None:
    """Raise :class:`SafetyError` if ``action`` on ``path`` is never allowed."""
    from fileflow.common.exceptions import SafetyError

    reason = protected_reason(path)
    if reason:
        raise SafetyError(
            f"Refusing to {action} {path}: {reason}.",
            hint="Choose a regular project, Downloads or Documents folder instead.",
        )
