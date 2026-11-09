"""Terminal capabilities, theme and runtime options shared by every tool."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass

from rich.console import Console
from rich.theme import Theme

# Design language: cyan headings, blue/purple info, green success,
# yellow warnings, red errors, muted secondary text.
THEME = Theme(
    {
        "heading": "bold cyan",
        "info": "blue",
        "accent": "medium_purple1",
        "success": "green",
        "warning": "yellow",
        "error": "bold red",
        "muted": "grey62",
        "key": "cyan",
        "path": "default",
    }
)

#: Environment variable that forces prompts to behave as if a TTY is attached.
#: Intended for automated tests only; documented in docs/architecture.md.
FORCE_INTERACTIVE_ENV = "DEVTOOLKIT_FORCE_INTERACTIVE"


@dataclass
class RuntimeOptions:
    """Global flags available on every command."""

    json: bool = False
    quiet: bool = False
    verbose: bool = False
    no_color: bool = False


_options = RuntimeOptions()


def get_options() -> RuntimeOptions:
    """Return the options of the currently running command."""
    return _options


def set_options(options: RuntimeOptions) -> None:
    """Install the options of the command that is starting."""
    global _options  # noqa: PLW0603 - process-wide CLI state by design
    _options = options


def is_interactive() -> bool:
    """True when both stdin and stdout are terminals (or interactivity is forced)."""
    if os.environ.get(FORCE_INTERACTIVE_ENV) == "1":
        return True
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


def color_disabled(options: RuntimeOptions | None = None) -> bool:
    """Colour is off for ``--no-color``, ``NO_COLOR`` or ``TERM=dumb``."""
    opts = options or _options
    if opts.no_color or "NO_COLOR" in os.environ:
        return True
    return os.environ.get("TERM") == "dumb"


def make_console(*, stderr: bool = False, options: RuntimeOptions | None = None) -> Console:
    """Create a Rich console that honours the toolkit's colour rules.

    Rich already strips colour when the stream is not a TTY.
    """
    return Console(
        theme=THEME,
        stderr=stderr,
        no_color=color_disabled(options),
        highlight=False,
        soft_wrap=False,
    )


LOGO_LINES = {
    "envdoctor": "ENVDOCTOR",
    "disklens": "DISKLENS",
    "snipvault": "SNIPVAULT",
    "fileflow": "FILEFLOW",
    "cmdsense": "CMDSENSE",
}

TAGLINES = {
    "envdoctor": "Development Environment Diagnostics",
    "disklens": "Disk Usage & Storage Intelligence",
    "snipvault": "Local Developer Snippet Library",
    "fileflow": "Safe, Rule-Based File Organization",
    "cmdsense": "Terminal Command Intelligence",
}
