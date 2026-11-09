"""Shared pytest fixtures for fileflow tests."""

from __future__ import annotations

import contextlib
import importlib
import io
import json
import os
import subprocess
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest


@dataclass
class Result:
    exit_code: int
    stdout: str
    stderr: str

    def json(self) -> Any:
        return json.loads(self.stdout)

    @property
    def output(self) -> str:
        return self.stdout + self.stderr


@pytest.fixture
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("DEVTOOLKIT_HOME", str(tmp_path / "dtk"))
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("TERM", "dumb")
    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.delenv("DEVTOOLKIT_FORCE_INTERACTIVE", raising=False)
    yield home


@pytest.fixture
def run(isolated_env: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[..., Result]:
    def _run(
        tool: str,
        *args: str,
        input: str = "",
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
        interactive: bool = False,
    ) -> Result:
        module = importlib.import_module("fileflow.cli")
        for key, value in (env or {}).items():
            monkeypatch.setenv(key, value)
        if interactive:
            monkeypatch.setenv("DEVTOOLKIT_FORCE_INTERACTIVE", "1")
        out, err = io.StringIO(), io.StringIO()
        old_cwd = Path.cwd()
        if cwd is not None:
            os.chdir(cwd)
        monkeypatch.setattr(sys, "stdin", io.StringIO(input))
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = module.main(list(args))
        finally:
            os.chdir(old_cwd)
        return Result(int(code), out.getvalue(), err.getvalue())

    return _run


@pytest.fixture
def run_proc(isolated_env: Path, tmp_path: Path) -> Callable[..., Result]:
    def _run(
        tool: str,
        *args: str,
        input: str = "",
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
        timeout: float = 60,
    ) -> Result:
        full_env = dict(os.environ)
        full_env.update(
            HOME=str(isolated_env),
            DEVTOOLKIT_HOME=str(tmp_path / "dtk"),
            NO_COLOR="1",
            TERM="dumb",
            PYTHONDONTWRITEBYTECODE="1",
        )
        full_env.update(env or {})
        proc = subprocess.run(
            [sys.executable, "-m", "fileflow", *args],
            input=input,
            capture_output=True,
            text=True,
            cwd=cwd,
            env=full_env,
            timeout=timeout,
            check=False,
        )
        return Result(proc.returncode, proc.stdout, proc.stderr)

    return _run


def pytest_collection_modifyitems(items: Sequence[pytest.Item]) -> None:
    if sys.platform == "darwin":
        return
    skip = pytest.mark.skip(reason="requires macOS")
    for item in items:
        if "macos" in item.keywords:
            item.add_marker(skip)
