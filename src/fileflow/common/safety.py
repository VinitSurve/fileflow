"""Secret redaction, confirmation prompts and safe subprocess execution."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fileflow.common.exceptions import SafetyError
from fileflow.common.output import Output
from fileflow.common.terminal import is_interactive

REDACTED = "***REDACTED***"

_SENSITIVE_WORDS = (
    r"API[_-]?KEY|SECRET|TOKEN|PASSWORD|PASSWD|PASSPHRASE|CREDENTIALS?|PRIVATE[_-]?KEY|"
    r"ACCESS[_-]?KEY|AUTH(?![A-Za-z])|AUTHORIZATION|SESSION[_-]?ID|COOKIE|DSN|CONNECTION[_-]?STRING"
)
_SENSITIVE_KEY_RE = re.compile(_SENSITIVE_WORDS, re.IGNORECASE)
_ASSIGNMENT_RE = re.compile(
    rf"(?P<key>[A-Za-z0-9_.\-]*(?:{_SENSITIVE_WORDS})[A-Za-z0-9_.\-]*)"
    r"(?P<sep>\s*[=:]\s*)"
    r"(?P<val>\"[^\"]*\"|'[^']*'|[^\s,;]+)",
    re.IGNORECASE,
)
_BEARER_RE = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=\-]{8,}")
_TOKEN_RE = re.compile(
    r"\b(?:ghp_|gho_|ghs_|ghu_|github_pat_|sk-|sk_live_|sk_test_|xox[abprs]-|AKIA|ASIA|AIza|glpat-|npm_)"
    r"[A-Za-z0-9_\-]{8,}"
)
_URL_CRED_RE = re.compile(
    r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.\-]*://)(?P<user>[^/\s:@]+):(?P<pw>[^/\s@]+)@"
)
_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)", re.DOTALL
)


def is_sensitive_key(name: str) -> bool:
    """True if an environment-variable / config key name suggests a secret."""
    return bool(_SENSITIVE_KEY_RE.search(name))


def redact_text(text: str) -> str:
    """Mask secrets in free text (logs, terminal output, command lines)."""
    text = _PRIVATE_KEY_RE.sub(REDACTED, text)
    text = _URL_CRED_RE.sub(lambda m: f"{m['scheme']}{m['user']}:{REDACTED}@", text)
    text = _BEARER_RE.sub(lambda m: f"{m.group(1)} {REDACTED}", text)
    text = _TOKEN_RE.sub(REDACTED, text)

    def _assign(match: re.Match[str]) -> str:
        value = match["val"]
        if value.upper().startswith(("BEARER", "BASIC")) and len(value) <= 6:
            return match.group(0)
        return f"{match['key']}{match['sep']}{REDACTED}"

    return _ASSIGNMENT_RE.sub(_assign, text)


def redact_mapping(data: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy with values of sensitive-looking keys masked (recursively)."""
    result: dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(value, Mapping):
            result[key] = redact_mapping(value)
        elif is_sensitive_key(key):
            result[key] = REDACTED
        elif isinstance(value, str):
            result[key] = redact_text(value)
        else:
            result[key] = value
    return result


def confirm(out: Output, message: str, *, assume_yes: bool = False, default: bool = False) -> bool:
    """Ask a yes/no question.

    * ``assume_yes`` (``--yes``) answers yes without prompting.
    * Without a terminal and without ``--yes`` the operation is refused rather
      than guessed, so scripts can never trigger a destructive action by accident.
    """
    if assume_yes:
        return True
    if not is_interactive():
        raise SafetyError(
            f"Confirmation required: {message}",
            hint="Re-run with --yes to approve this operation non-interactively.",
        )
    from rich.prompt import Confirm

    return Confirm.ask(message, console=out.err, default=default)


def require_confirmation(out: Output, message: str, *, assume_yes: bool = False) -> None:
    """Like :func:`confirm` but raises :class:`SafetyError` (exit code 3) on "no"."""
    if not confirm(out, message, assume_yes=assume_yes):
        raise SafetyError("Cancelled. Nothing was changed.")


@dataclass
class CommandResult:
    """Outcome of :func:`run_command`; never raises for ordinary failures."""

    argv: list[str]
    returncode: int | None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    not_found: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out and not self.not_found

    @property
    def output(self) -> str:
        return (self.stdout or self.stderr).strip()


def which(name: str) -> str | None:
    """Locate an executable on PATH."""
    return shutil.which(name)


def run_command(
    argv: Sequence[str],
    *,
    cwd: Path | None = None,
    timeout: float = 10.0,
    env: Mapping[str, str] | None = None,
    input_text: str | None = None,
    max_output: int = 1_000_000,
) -> CommandResult:
    """Run a program with an argument list (never through a shell).

    stdin is closed unless ``input_text`` is given, output is size-capped and a
    missing program, timeout or permission problem is reported in the result.
    """
    if isinstance(argv, str) or not argv or not all(isinstance(a, str) for a in argv):
        raise TypeError(
            "argv must be a non-empty sequence of strings; shell strings are not allowed"
        )
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    command = list(argv)
    try:
        proc = subprocess.run(
            command,
            cwd=cwd,
            env=full_env,
            input=input_text,
            stdin=None if input_text is not None else subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            check=False,
            shell=False,
        )
    except FileNotFoundError:
        return CommandResult(
            command, None, not_found=True, error=f"{command[0]}: command not found"
        )
    except PermissionError as exc:
        return CommandResult(command, None, error=f"permission denied: {exc}")
    except subprocess.TimeoutExpired as exc:
        stdout = (
            exc.stdout.decode("utf-8", "replace")
            if isinstance(exc.stdout, bytes)
            else (exc.stdout or "")
        )
        stderr = (
            exc.stderr.decode("utf-8", "replace")
            if isinstance(exc.stderr, bytes)
            else (exc.stderr or "")
        )
        return CommandResult(
            command, None, stdout[:max_output], stderr[:max_output], timed_out=True,
            error=f"timed out after {timeout:g}s",
        )  # fmt: skip
    except OSError as exc:
        return CommandResult(command, None, error=str(exc))
    return CommandResult(
        command, proc.returncode, proc.stdout[:max_output], proc.stderr[:max_output]
    )
