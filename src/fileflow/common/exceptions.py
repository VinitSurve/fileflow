"""Exception hierarchy and the toolkit-wide exit-code contract."""

from __future__ import annotations

from enum import IntEnum
from typing import Any


class ExitCode(IntEnum):
    """Exit codes shared by every utility.

    * ``OK``          - success / healthy.
    * ``WARNING``     - completed, but warnings were found (used by diagnostics).
    * ``ERROR``       - the operation failed, or errors were found.
    * ``REFUSED``     - the user (or a safety rule) declined a risky operation.
    * ``USAGE``       - invalid command line usage (sysexits ``EX_USAGE``).
    * ``INTERRUPTED`` - cancelled with Ctrl-C (128 + SIGINT).
    """

    OK = 0
    WARNING = 1
    ERROR = 2
    REFUSED = 3
    USAGE = 64
    INTERRUPTED = 130


class ToolkitError(Exception):
    """Base class for expected, user-presentable errors.

    ``message`` says what went wrong, ``hint`` says what to do about it.
    """

    exit_code: ExitCode = ExitCode.ERROR

    def __init__(
        self,
        message: str,
        *,
        hint: str | None = None,
        exit_code: ExitCode | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.details = details or {}
        if exit_code is not None:
            self.exit_code = exit_code


class UsageError(ToolkitError):
    """The command was invoked incorrectly."""

    exit_code = ExitCode.USAGE


class NotFoundError(ToolkitError):
    """A file, directory, record or tool that was asked for does not exist."""


class PermissionDeniedError(ToolkitError):
    """The OS refused access to a path."""


class ConfigError(ToolkitError):
    """Configuration is missing, malformed or invalid."""


class DataError(ToolkitError):
    """Persistent data (database, journal, import file) is corrupt or invalid."""


class SafetyError(ToolkitError):
    """A safety rule blocked the operation, or required confirmation was not given."""

    exit_code = ExitCode.REFUSED


class ExternalToolError(ToolkitError):
    """A required external program failed or is unavailable."""
