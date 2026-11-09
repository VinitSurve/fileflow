"""Consistent human and machine output for every tool.

Rules every command follows:

* ``--json``  -> stdout carries exactly one JSON document and nothing else.
  Human decoration is suppressed; warnings/errors still go to stderr.
* ``--quiet`` -> decorative output (banners, headings, success/info lines,
  progress) is suppressed; primary data and errors are still printed.
* ``--verbose`` -> :meth:`Output.debug` lines are shown on stderr.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import enum
import json
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import PurePath
from typing import Any

from rich import box
from rich.align import Align
from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table
from rich.text import Text
from rich.tree import Tree

from fileflow import TOOLKIT_NAME, __version__
from fileflow.common.terminal import (
    LOGO_LINES,
    TAGLINES,
    RuntimeOptions,
    get_options,
    make_console,
)

SYMBOLS = {
    "ok": ("✓", "success"),
    "warn": ("⚠", "warning"),
    "error": ("✗", "error"),
    "info": ("•", "info"),
    "skip": ("–", "muted"),
}


def to_jsonable(value: Any) -> Any:
    """Convert dataclasses, paths, enums, datetimes, sets and bytes into JSON types."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: to_jsonable(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, Mapping):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return sorted((to_jsonable(v) for v in value), key=str)
    if isinstance(value, PurePath):
        return str(value)
    if isinstance(value, enum.Enum):
        return to_jsonable(value.value)
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def dumps_json(data: Any) -> str:
    """Stable, readable JSON used for all machine output."""
    return json.dumps(to_jsonable(data), indent=2, ensure_ascii=False, default=str)


class NullProgress:
    """Progress object that does nothing (quiet, JSON or non-interactive runs)."""

    def advance(self, amount: int = 1) -> None:
        return None

    def update(
        self,
        description: str | None = None,
        *,
        total: int | None = None,
        completed: int | None = None,
    ) -> None:
        return None


class _RichProgress(NullProgress):
    def __init__(self, progress: Progress, task_id: Any) -> None:
        self._progress = progress
        self._task = task_id

    def advance(self, amount: int = 1) -> None:
        self._progress.advance(self._task, amount)

    def update(
        self,
        description: str | None = None,
        *,
        total: int | None = None,
        completed: int | None = None,
    ) -> None:
        kwargs: dict[str, Any] = {}
        if description is not None:
            kwargs["description"] = description
        if total is not None:
            kwargs["total"] = total
        if completed is not None:
            kwargs["completed"] = completed
        self._progress.update(self._task, **kwargs)


class Output:
    """All rendering goes through this object; commands never call ``print``."""

    def __init__(self, tool: str, options: RuntimeOptions | None = None) -> None:
        self.tool = tool
        self.options = options or get_options()
        self.console = make_console(options=self.options)
        self.err = make_console(stderr=True, options=self.options)

    # ------------------------------------------------------------------ flags
    @property
    def json_mode(self) -> bool:
        return self.options.json

    @property
    def quiet(self) -> bool:
        return self.options.quiet

    @property
    def verbose(self) -> bool:
        return self.options.verbose

    @property
    def _decorate(self) -> bool:
        return not (self.options.json or self.options.quiet)

    # ------------------------------------------------------------ decorative
    def banner(self, subtitle: str | None = None) -> None:
        """Welcome panel with the tool name and tagline."""
        if not self._decorate:
            return
        title = LOGO_LINES.get(self.tool, self.tool.upper())
        tagline = subtitle or TAGLINES.get(self.tool, "")
        body = Group(
            Align.center(Text(" ".join(title), style="heading")),
            Align.center(Text(tagline, style="muted")),
        )
        self.console.print(
            Panel(body, box=box.ROUNDED, border_style="accent", padding=(0, 4), expand=False)
        )

    def heading(self, text: str) -> None:
        if self._decorate:
            self.console.print(Text(text, style="heading"))

    def info(self, message: str) -> None:
        if self._decorate:
            self.console.print(Text(message, style="info"))

    def success(self, message: str) -> None:
        if self._decorate:
            self.status("ok", message)

    def muted(self, message: str) -> None:
        if self._decorate:
            self.console.print(Text(message, style="muted"))

    def next_steps(self, steps: Sequence[str]) -> None:
        """Helpful follow-up commands, shown after a result."""
        if not self._decorate or not steps:
            return
        self.console.print()
        self.console.print(Text("Next steps", style="heading"))
        for step in steps:
            self.console.print(Text("  → ", style="accent") + Text(step, style="muted"))

    def status(self, kind: str, message: str, *, to_stderr: bool = False) -> None:
        """``✓``/``⚠``/``✗``/``•``/``–`` line. Honors json/quiet only via callers."""
        if self.options.json and not to_stderr:
            return
        symbol, style = SYMBOLS.get(kind, SYMBOLS["info"])
        target = self.err if to_stderr else self.console
        target.print(Text(f"{symbol} ", style=style) + Text(message))

    def warn(self, message: str, hint: str | None = None) -> None:
        if self.options.quiet:
            return
        self.err.print(Text("⚠ ", style="warning") + Text(message))
        if hint:
            self.err.print(Text(f"  {hint}", style="muted"))

    def debug(self, message: str) -> None:
        if self.options.verbose:
            self.err.print(Text(f"[debug] {message}", style="muted"))

    def error(self, message: str, hint: str | None = None, *, title: str = "Error") -> None:
        """Error panel on stderr. Always shown."""
        body = Text(message)
        if hint:
            body.append("\n")
            body.append(f"Hint: {hint}", style="muted")
        self.err.print(Panel(body, title=title, border_style="red", box=box.ROUNDED, expand=False))

    # ----------------------------------------------------------------- data
    def text(self, message: str = "", style: str | None = None) -> None:
        """Plain primary output line (suppressed only in JSON mode)."""
        if self.options.json:
            return
        self.console.print(Text(message, style=style or ""))

    def render(self, renderable: RenderableType) -> None:
        if self.options.json:
            return
        self.console.print(renderable)

    def kv(
        self,
        rows: Sequence[tuple[str, str]],
        *,
        title: str | None = None,
    ) -> None:
        """Aligned key/value block."""
        if self.options.json:
            return
        grid = Table.grid(padding=(0, 2))
        grid.add_column(style="key", no_wrap=True)
        grid.add_column(overflow="fold")
        for key, value in rows:
            grid.add_row(key, Text(str(value)))
        if title:
            self.console.print(Text(title, style="heading"))
        self.console.print(grid)

    def table(
        self,
        columns: Sequence[str],
        rows: Sequence[Sequence[Any]],
        *,
        title: str | None = None,
        caption: str | None = None,
        right_align: Sequence[str] = (),
    ) -> None:
        if self.options.json:
            return
        table = Table(
            title=title,
            caption=caption,
            box=box.SIMPLE_HEAD,
            header_style="heading",
            title_style="heading",
            caption_style="muted",
            title_justify="left",
            caption_justify="left",
        )
        for col in columns:
            table.add_column(
                col,
                justify="right" if col in right_align else "left",
                overflow="fold",
            )
        for row in rows:
            table.add_row(*[c if isinstance(c, Text) else Text(str(c)) for c in row])
        self.console.print(table)

    def tree(self, label: str) -> Tree:
        """Create a tree; call :meth:`render` to print it."""
        return Tree(Text(label, style="heading"))

    def panel(
        self, body: RenderableType | str, *, title: str | None = None, style: str = "accent"
    ) -> None:
        if self.options.json:
            return
        renderable = Text(body) if isinstance(body, str) else body
        self.console.print(Panel(renderable, title=title, border_style=style, box=box.ROUNDED))

    def summary(
        self, title: str, rows: Sequence[tuple[str, str]], *, style: str = "accent"
    ) -> None:
        """Summary panel with key/value lines."""
        if not self._decorate:
            return
        grid = Table.grid(padding=(0, 2))
        grid.add_column(style="key", no_wrap=True)
        grid.add_column(overflow="fold")
        for key, value in rows:
            grid.add_row(key, Text(str(value)))
        self.console.print(
            Panel(grid, title=title, border_style=style, box=box.ROUNDED, expand=False)
        )

    # ----------------------------------------------------------------- JSON
    def emit_json(self, data: Any) -> None:
        """Write one JSON document to stdout (the only stdout content in JSON mode)."""
        sys.stdout.write(dumps_json(data) + "\n")
        sys.stdout.flush()

    def emit(self, data: Any, render: Callable[[Output], None]) -> None:
        """JSON mode -> print ``data``; otherwise call ``render(self)``."""
        if self.options.json:
            self.emit_json(data)
        else:
            render(self)

    # ------------------------------------------------------------- progress
    @contextmanager
    def progress(self, description: str, *, total: int | None = None) -> Iterator[NullProgress]:
        """Progress bar/spinner on stderr; a no-op unless stderr is a terminal."""
        if not self._decorate or not self.err.is_terminal:
            yield NullProgress()
            return
        columns: list[Any] = [SpinnerColumn(style="accent"), TextColumn("{task.description}")]
        if total is not None:
            columns += [BarColumn(), MofNCompleteColumn()]
        columns.append(TimeElapsedColumn())
        with Progress(*columns, console=self.err, transient=True) as progress:
            task = progress.add_task(description, total=total)
            yield _RichProgress(progress, task)


def version_line(tool: str) -> str:
    return f"{tool} {__version__} ({TOOLKIT_NAME})"
