"""CLI scaffolding: app factory, global flags, uniform error handling, ``config`` commands."""

from __future__ import annotations

import contextlib
import os
import sys
import traceback
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Annotated, Any

import typer

from fileflow.common.config import ConfigSchema, ConfigStore
from fileflow.common.exceptions import ExitCode, ToolkitError, UsageError
from fileflow.common.logging import get_logger
from fileflow.common.output import Output, dumps_json, version_line
from fileflow.common.paths import ToolPaths
from fileflow.common.safety import confirm, redact_mapping
from fileflow.common.terminal import RuntimeOptions, set_options

#: Flags accepted anywhere on the command line; they are moved in front of the
#: sub-command so ``tool report --json`` and ``tool --json report`` are equivalent.
GLOBAL_FLAGS = ("--json", "--quiet", "--verbose", "--no-color")

GLOBAL_EPILOG = (
    "Global options (--json, --quiet, --verbose, --no-color) may be placed anywhere "
    "on the command line."
)


@dataclass
class ToolContext:
    """Per-invocation state stored on the Click context (``ctx.obj``)."""

    tool: str
    out: Output
    options: RuntimeOptions
    paths: ToolPaths


def get_ctx(ctx: typer.Context) -> ToolContext:
    """Fetch the :class:`ToolContext` from any (sub-)command context."""
    obj = ctx.find_root().obj
    if not isinstance(obj, ToolContext):  # pragma: no cover - programming error
        raise RuntimeError("ToolContext missing: app was not created with create_app()")
    return obj


def exit_with(code: ExitCode | int) -> None:
    """Terminate the command with ``code`` (raises ``typer.Exit``)."""
    raise typer.Exit(int(code))


def hoist_global_flags(argv: Sequence[str]) -> list[str]:
    """Move global flags in front of everything else (stopping at ``--``)."""
    head: list[str] = []
    tail: list[str] = []
    passthrough = False
    for arg in argv:
        if passthrough:
            tail.append(arg)
        elif arg == "--":
            passthrough = True
            tail.append(arg)
        elif arg in GLOBAL_FLAGS:
            if arg not in head:
                head.append(arg)
        else:
            tail.append(arg)
    return head + tail


def _version_callback(ctx: typer.Context, value: bool) -> None:
    if value:
        sys.stdout.write(version_line(ctx.find_root().info_name or "devtoolkit") + "\n")
        raise typer.Exit(0)


def create_app(
    tool: str,
    help_text: str,
    *,
    default: Callable[[typer.Context], None] | None = None,
) -> typer.Typer:
    """Create the Typer app for ``tool`` with the shared global options.

    ``default`` runs when the tool is invoked with no sub-command (otherwise
    help is shown).
    """
    app = typer.Typer(
        name=tool,
        help=help_text,
        epilog=GLOBAL_EPILOG,
        add_completion=False,
        no_args_is_help=False,
        pretty_exceptions_enable=False,
        rich_markup_mode=None,
        context_settings={"help_option_names": ["-h", "--help"]},
    )

    @app.callback(invoke_without_command=True)
    def _root(
        ctx: typer.Context,
        version: Annotated[
            bool,
            typer.Option(
                "--version",
                callback=_version_callback,
                is_eager=True,
                help="Show the version and exit.",
            ),
        ] = False,
        json_output: Annotated[
            bool, typer.Option("--json", help="Machine-readable JSON on stdout.")
        ] = False,
        quiet: Annotated[
            bool,
            typer.Option("--quiet", help="Suppress decorative output; keep results and errors."),
        ] = False,
        verbose: Annotated[
            bool, typer.Option("--verbose", help="Show diagnostic details on stderr.")
        ] = False,
        no_color: Annotated[
            bool, typer.Option("--no-color", help="Disable colour (NO_COLOR is also respected).")
        ] = False,
    ) -> None:
        if quiet and verbose:
            raise UsageError("--quiet and --verbose cannot be combined.")
        options = RuntimeOptions(json=json_output, quiet=quiet, verbose=verbose, no_color=no_color)
        set_options(options)
        ctx.obj = ToolContext(tool, Output(tool, options), options, ToolPaths.for_tool(tool))
        if verbose:
            get_logger(tool, verbose=True)
        if ctx.invoked_subcommand is None:
            if default is not None:
                default(ctx)
            else:
                sys.stdout.write(ctx.get_help() + "\n")

    return app


def _render_error(
    tool: str, message: str, hint: str | None, code: int, *, title: str = "Error"
) -> None:
    options = _current_options()
    if options.json:
        payload: dict[str, Any] = {"error": message, "exit_code": code}
        if hint:
            payload["hint"] = hint
        sys.stderr.write(dumps_json(payload) + "\n")
        return
    Output(tool, options).error(message, hint, title=title)


def _current_options() -> RuntimeOptions:
    from fileflow.common.terminal import get_options

    return get_options()


def run_app(app: typer.Typer, *, tool: str, argv: Sequence[str] | None = None) -> int:
    """Run ``app`` and translate every outcome into a toolkit exit code."""
    set_options(RuntimeOptions())
    args = hoist_global_flags(sys.argv[1:] if argv is None else argv)
    try:
        result = app(args=args, prog_name=tool, standalone_mode=False)
        return int(result) if isinstance(result, int) and not isinstance(result, bool) else 0
    except typer.Exit as exc:
        return int(exc.exit_code)
    except (typer.Abort, KeyboardInterrupt):
        _render_error(
            tool, "Interrupted. Nothing further was changed.", None, ExitCode.INTERRUPTED,
            title="Cancelled",
        )  # fmt: skip
        return int(ExitCode.INTERRUPTED)
    except ToolkitError as exc:
        _render_error(tool, exc.message, exc.hint, exc.exit_code, title=_title_for(exc))
        return int(exc.exit_code)
    except BrokenPipeError:
        with contextlib.suppress(OSError, ValueError):
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0
    except Exception as exc:
        if _is_parser_error(exc):
            # Typer/Click parsing errors. Duck-typed so we work with both the
            # real ``click`` package and Typer's vendored copy.
            context = getattr(exc, "ctx", None)
            command_path = getattr(context, "command_path", tool)
            message = exc.format_message() if hasattr(exc, "format_message") else str(exc)
            _render_error(
                tool, message, f"Try '{command_path} --help' for usage.",
                ExitCode.USAGE, title="Usage error",
            )  # fmt: skip
            return int(ExitCode.USAGE)
        logger = get_logger(tool)
        logger.error("Unhandled error: %s", exc, exc_info=True)
        if _current_options().verbose:
            traceback.print_exc()
        _render_error(
            tool,
            f"Unexpected error: {type(exc).__name__}: {exc}",
            "Re-run with --verbose for details. This is a bug; please report it.",
            ExitCode.ERROR,
            title="Internal error",
        )
        return int(ExitCode.ERROR)


def _is_parser_error(exc: BaseException) -> bool:
    names = {cls.__name__ for cls in type(exc).__mro__}
    return bool(names & {"UsageError", "ClickException", "TyperException", "BadParameter"})


def _title_for(exc: ToolkitError) -> str:
    return {
        "SafetyError": "Refused",
        "UsageError": "Usage error",
        "ConfigError": "Configuration error",
        "DataError": "Data error",
        "NotFoundError": "Not found",
        "PermissionDeniedError": "Permission denied",
    }.get(type(exc).__name__, "Error")


def add_config_commands(app: typer.Typer, tool: str, schema: ConfigSchema) -> None:
    """Attach ``<tool> config [show|get|set|path|reset]`` to ``app``."""
    config_app = typer.Typer(
        help=f"View and change {tool} configuration.",
        add_completion=False,
        no_args_is_help=False,
        rich_markup_mode=None,
        context_settings={"help_option_names": ["-h", "--help"]},
    )

    def _store(ctx: typer.Context) -> tuple[ToolContext, ConfigStore]:
        tctx = get_ctx(ctx)
        return tctx, ConfigStore(tctx.paths, schema)

    def _show(ctx: typer.Context) -> None:
        tctx, store = _store(ctx)
        values = redact_mapping(store.load())
        data = {
            "path": str(store.path),
            "exists": store.path.exists(),
            "values": values,
            "defaults": redact_mapping(schema.defaults()),
        }

        def render(out: Output) -> None:
            out.heading(f"{tool} configuration")
            out.kv(
                [
                    (
                        "File",
                        str(store.path)
                        + ("" if store.path.exists() else "  (not created; using defaults)"),
                    )
                ]
            )
            out.table(
                ["Key", "Value", "Default", "Description"],
                [
                    (k, _fmt(values[k]), _fmt(schema.defaults()[k]), schema.fields[k].description)
                    for k in sorted(schema.fields)
                ],
            )
            out.next_steps([f"{tool} config set KEY VALUE", f"{tool} config reset"])

        tctx.out.emit(data, render)

    @config_app.callback(invoke_without_command=True)
    def _config_root(ctx: typer.Context) -> None:
        if ctx.invoked_subcommand is None:
            _show(ctx)

    @config_app.command("show")
    def config_show(ctx: typer.Context) -> None:
        """Show all configuration values."""
        _show(ctx)

    @config_app.command("path")
    def config_path(ctx: typer.Context) -> None:
        """Print the configuration file location."""
        tctx, store = _store(ctx)
        tctx.out.emit({"path": str(store.path)}, lambda o: o.text(str(store.path)))

    @config_app.command("get")
    def config_get(
        ctx: typer.Context, key: Annotated[str, typer.Argument(help="Configuration key.")]
    ) -> None:
        """Print one configuration value."""
        tctx, store = _store(ctx)
        value = redact_mapping({key: store.get(key)})[key]
        tctx.out.emit({key: value}, lambda o: o.text(_fmt(value)))

    @config_app.command("set")
    def config_set(
        ctx: typer.Context,
        key: Annotated[str, typer.Argument(help="Configuration key.")],
        value: Annotated[str, typer.Argument(help="New value (lists are comma-separated).")],
    ) -> None:
        """Validate and store one configuration value."""
        tctx, store = _store(ctx)
        stored = store.set(key, value)
        shown = redact_mapping({key: stored})[key]
        tctx.out.emit(
            {key: shown, "path": str(store.path)},
            lambda o: o.success(f"{key} = {_fmt(shown)}"),
        )

    @config_app.command("reset")
    def config_reset(
        ctx: typer.Context,
        yes: Annotated[
            bool, typer.Option("--yes", "-y", help="Do not ask for confirmation.")
        ] = False,
    ) -> None:
        """Restore defaults. The old file is kept as a timestamped backup."""
        tctx, store = _store(ctx)
        if not store.path.exists():
            tctx.out.emit(
                {"reset": False, "reason": "no configuration file"},
                lambda o: o.info("Already using defaults; nothing to reset."),
            )
            return
        if not confirm(tctx.out, f"Reset {tool} configuration to defaults?", assume_yes=yes):
            raise typer.Exit(int(ExitCode.REFUSED))
        backup = store.reset()
        tctx.out.emit(
            {"reset": True, "backup": str(backup)},
            lambda o: o.success(f"Configuration reset. Previous file kept at {backup}"),
        )

    app.add_typer(config_app, name="config")


def _fmt(value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(str(v) for v in value) or "(empty)"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)
