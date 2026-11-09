"""Command line interface for fileflow."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated

import typer

from fileflow.common.cli import (
    add_config_commands,
    create_app,
    exit_with,
    get_ctx,
    run_app,
)
from fileflow.common.exceptions import ExitCode, NotFoundError, UsageError
from fileflow.common.fsutil import parse_size
from fileflow.common.paths import resolve_user_path
from fileflow.common.safety import require_confirmation
from fileflow.config import SCHEMA, load_config
from fileflow.duplicates import (
    build_duplicates_move_plan,
    execute_duplicates_move,
    find_duplicates,
)
from fileflow.executor import execute_organize_plan
from fileflow.journal import Journal
from fileflow.planner import CollisionPolicy, build_organize_plan
from fileflow.rename import DateSource, build_rename_plan, execute_rename_plan
from fileflow.render import (
    render_duplicates_report,
    render_execution_report,
    render_history_detail,
    render_history_list,
    render_organize_preview,
    render_rename_preview,
    render_rule_explain,
    render_rules_list,
    render_scan_report,
    render_undo_report,
    render_watch_report,
)
from fileflow.rules import FileMetadata, Rule, RuleStore, match_file
from fileflow.scanner import scan_directory
from fileflow.undo import undo_operation
from fileflow.watch import run_watch

TOOL = "fileflow"

app = create_app(
    TOOL, "Organize files with explainable rules - previewed first, reversible with undo."
)
add_config_commands(app, TOOL, SCHEMA)


def _resolve_collision_policy(value: str | None, default_value: str) -> CollisionPolicy:
    chosen = (value or default_value).strip().lower()
    if chosen not in ("rename", "skip"):
        raise UsageError(
            f"Invalid --on-collision policy: {value!r}",
            hint="Allowed values: 'rename' or 'skip'.",
        )
    return "rename" if chosen == "rename" else "skip"


def _resolve_date_source(value: str) -> DateSource:
    chosen = value.strip().lower()
    if chosen not in ("mtime", "ctime"):
        raise UsageError(
            f"Invalid --date-source: {value!r}",
            hint="Allowed values: 'mtime' or 'ctime'.",
        )
    return "mtime" if chosen == "mtime" else "ctime"


@app.command("scan")
def scan_cmd(
    ctx: typer.Context,
    path: Annotated[
        str,
        typer.Argument(help="Directory to analyze (read-only)."),
    ] = ".",
    large_threshold: Annotated[
        str | None,
        typer.Option(
            "--large-threshold",
            help="Size threshold for large files (e.g. '50MB', '1GB').",
        ),
    ] = None,
    depth: Annotated[
        int,
        typer.Option("--depth", help="Maximum folder tree depth to display."),
    ] = 2,
    include_hidden: Annotated[
        bool,
        typer.Option("--include-hidden", help="Include dotfiles and ignored system files."),
    ] = False,
) -> None:
    """Read-only folder analysis: extensions, categories, large files, duplicates, and tree."""
    tctx = get_ctx(ctx)
    cfg = load_config(tctx.paths)
    threshold_str = large_threshold or str(cfg["large_file_threshold"])
    threshold_bytes = parse_size(threshold_str)
    if depth < 0:
        raise UsageError("--depth must be >= 0.")
    target = resolve_user_path(path)
    report = scan_directory(
        target,
        large_threshold=threshold_bytes,
        max_depth=depth,
        include_hidden=include_hidden,
        ignore_names=cfg["ignore_names"],
    )
    tctx.out.emit(report, lambda o: render_scan_report(o, report))


@app.command("organize")
def organize_cmd(
    ctx: typer.Context,
    path: Annotated[
        str,
        typer.Argument(help="Directory to organize."),
    ] = ".",
    preview: Annotated[
        bool,
        typer.Option(
            "--preview", help="Preview the organization plan without moving files (default)."
        ),
    ] = False,
    apply: Annotated[
        bool,
        typer.Option("--apply", help="Execute the organization plan and record it in the journal."),
    ] = False,
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Confirm applying changes without interactive prompt."),
    ] = False,
    recursive: Annotated[
        bool,
        typer.Option(
            "--recursive", "-r", help="Scan subdirectories (skipping organized destinations)."
        ),
    ] = False,
    include_hidden: Annotated[
        bool,
        typer.Option("--include-hidden", help="Include hidden files (starting with '.')."),
    ] = False,
    include_symlinks: Annotated[
        bool,
        typer.Option(
            "--include-symlinks", help="Move symlinks as symlinks instead of skipping them."
        ),
    ] = False,
    on_collision: Annotated[
        str | None,
        typer.Option("--on-collision", help="Collision strategy: 'rename' (default) or 'skip'."),
    ] = None,
) -> None:
    """Organize files into rule-based or default category folders (preview by default)."""
    if preview and apply:
        raise UsageError("--preview and --apply cannot be combined.")
    tctx = get_ctx(ctx)
    cfg = load_config(tctx.paths)
    policy = _resolve_collision_policy(on_collision, str(cfg["default_collision"]))
    target = resolve_user_path(path)
    rules = RuleStore(tctx.paths).load()
    mode = "apply" if apply else "preview"

    plan = build_organize_plan(
        target,
        rules=rules,
        recursive=recursive,
        include_hidden=include_hidden,
        include_symlinks=include_symlinks,
        on_collision=policy,
        ignore_names=cfg["ignore_names"],
        mode=mode,
    )

    if not apply:
        tctx.out.emit(plan, lambda o: render_organize_preview(o, plan))
        return

    require_confirmation(
        tctx.out,
        f"Organize {plan.total_moves} file(s) in {target}?",
        assume_yes=yes,
    )
    journal = Journal(tctx.paths)
    exec_report = execute_organize_plan(
        plan,
        journal,
        op_type="organize",
        hash_limit_for_undo=int(cfg["hash_limit_for_undo"]),
    )
    tctx.out.emit(exec_report, lambda o: render_execution_report(o, exec_report))
    if exec_report.failed_count > 0:
        exit_with(ExitCode.WARNING)


# --------------------------------------------------------------------- rules
rules_app = typer.Typer(
    help="Manage deterministic file organization rules and explain rule matching.",
    add_completion=False,
    no_args_is_help=False,
    rich_markup_mode=None,
    context_settings={"help_option_names": ["-h", "--help"]},
)


def _emit_rules_list(ctx: typer.Context) -> None:
    tctx = get_ctx(ctx)
    store = RuleStore(tctx.paths)
    rules = store.load()
    payload = {
        "path": str(store.path),
        "count": len(rules),
        "rules": [r.to_dict() for r in rules],
    }
    tctx.out.emit(payload, lambda o: render_rules_list(o, rules, store.path))


@rules_app.callback(invoke_without_command=True)
def _rules_root(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        _emit_rules_list(ctx)


@rules_app.command("list")
def rules_list_cmd(ctx: typer.Context) -> None:
    """List all configured organization rules in priority order."""
    _emit_rules_list(ctx)


@rules_app.command("add")
def rules_add_cmd(
    ctx: typer.Context,
    name: Annotated[str, typer.Option("--name", "-n", help="Human-friendly rule name.")],
    destination: Annotated[
        str,
        typer.Option(
            "--destination",
            "-d",
            help="Relative destination folder ({year}, {month}, {day}, {ext}, {category}).",
        ),
    ],
    rule_id: Annotated[
        str | None,
        typer.Option("--id", help="Short stable rule ID (auto-generated from --name if omitted)."),
    ] = None,
    priority: Annotated[
        int,
        typer.Option("--priority", "-p", help="Evaluation priority (lower number runs first)."),
    ] = 100,
    enabled: Annotated[
        bool,
        typer.Option("--enabled/--disabled", help="Whether the rule is active."),
    ] = True,
    ext: Annotated[
        list[str] | None,
        typer.Option(
            "--ext", "-e", help="Match file extension (repeatable, e.g. --ext .pdf --ext .docx)."
        ),
    ] = None,
    extensions: Annotated[
        str | None,
        typer.Option("--extensions", help="Comma-separated list of extensions (e.g. 'pdf,docx')."),
    ] = None,
    name_glob: Annotated[
        str | None,
        typer.Option("--name-glob", help="Case-insensitive glob on filename (e.g. '*invoice*')."),
    ] = None,
    name_regex: Annotated[
        str | None,
        typer.Option(
            "--name-regex", help="Case-insensitive regex on filename (validated against ReDoS)."
        ),
    ] = None,
    min_size: Annotated[
        str | None,
        typer.Option("--min-size", help="Minimum file size (e.g. '10MB' or byte count)."),
    ] = None,
    max_size: Annotated[
        str | None,
        typer.Option("--max-size", help="Maximum file size (e.g. '500MB' or byte count)."),
    ] = None,
    modified_before: Annotated[
        str | None,
        typer.Option(
            "--modified-before",
            help="Modified before ISO date (YYYY-MM-DD) or relative age ('30d').",
        ),
    ] = None,
    modified_after: Annotated[
        str | None,
        typer.Option(
            "--modified-after", help="Modified after ISO date (YYYY-MM-DD) or relative age ('7d')."
        ),
    ] = None,
    created_before: Annotated[
        str | None,
        typer.Option(
            "--created-before", help="Created before ISO date (YYYY-MM-DD) or relative age ('30d')."
        ),
    ] = None,
    created_after: Annotated[
        str | None,
        typer.Option(
            "--created-after", help="Created after ISO date (YYYY-MM-DD) or relative age ('7d')."
        ),
    ] = None,
    category: Annotated[
        str | None,
        typer.Option(
            "--category", help="Match default category (e.g. 'Documents', 'Images', 'Code')."
        ),
    ] = None,
    source_folder: Annotated[
        str | None,
        typer.Option("--source-folder", help="Glob on parent folder relative to scan root."),
    ] = None,
) -> None:
    """Add a new file organization rule."""
    tctx = get_ctx(ctx)
    store = RuleStore(tctx.paths)

    ext_list: list[str] = []
    if ext:
        for item in ext:
            ext_list.extend(p.strip() for p in item.split(",") if p.strip())
    if extensions:
        ext_list.extend(p.strip() for p in extensions.split(",") if p.strip())

    chosen_id = rule_id.strip() if rule_id else store.next_id(name)
    rule = Rule(
        id=chosen_id,
        name=name,
        destination=destination,
        enabled=enabled,
        priority=priority,
        extensions=ext_list,
        name_glob=name_glob,
        name_regex=name_regex,
        min_size=parse_size(min_size) if min_size else None,
        max_size=parse_size(max_size) if max_size else None,
        modified_before=modified_before,
        modified_after=modified_after,
        created_before=created_before,
        created_after=created_after,
        category=category,
        source_folder=source_folder,
    )
    added = store.add(rule)
    tctx.out.emit(
        {"added": True, "rule": added.to_dict(), "path": str(store.path)},
        lambda o: o.success(
            f"Added rule '{added.name}' (id={added.id}, priority={added.priority}) "
            f"-> {added.destination}"
        ),
    )


@rules_app.command("remove")
def rules_remove_cmd(
    ctx: typer.Context,
    rule_id: Annotated[str, typer.Argument(help="ID of the rule to remove.")],
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Confirm removal without interactive prompt."),
    ] = False,
) -> None:
    """Remove a rule by ID."""
    tctx = get_ctx(ctx)
    store = RuleStore(tctx.paths)
    existing = store.load()
    if not any(r.id == rule_id for r in existing):
        raise NotFoundError(
            f"Rule not found: {rule_id!r}",
            hint="Run 'fileflow rules list' to see configured rule IDs.",
        )
    require_confirmation(tctx.out, f"Remove rule {rule_id!r}?", assume_yes=yes)
    removed = store.remove(rule_id)
    tctx.out.emit(
        {"removed": True, "rule": removed.to_dict()},
        lambda o: o.success(f"Removed rule '{removed.name}' ({removed.id})."),
    )


@rules_app.command("explain")
def rules_explain_cmd(
    ctx: typer.Context,
    file: Annotated[str, typer.Argument(help="File path to evaluate against rules.")],
    root: Annotated[
        str | None,
        typer.Option("--root", help="Optional scan root directory for source_folder matching."),
    ] = None,
) -> None:
    """Explain which rule or default category matches a file and why."""
    tctx = get_ctx(ctx)
    file_path = resolve_user_path(file)
    if not file_path.exists() and not file_path.is_symlink():
        raise NotFoundError(f"File does not exist: {file_path}")
    if file_path.is_dir() and not file_path.is_symlink():
        raise UsageError(f"Path is a directory, expected a file: {file_path}")

    scan_root = resolve_user_path(root) if root else file_path.parent
    meta = FileMetadata.from_path(file_path, scan_root=scan_root)
    rules = RuleStore(tctx.paths).load()
    decision = match_file(meta, rules, include_evaluations=True)
    payload = {
        "file": str(meta.path),
        "name": meta.name,
        "extension": meta.extension,
        "size": meta.size,
        "default_category": meta.category,
        "source_folder": meta.source_folder,
        "matched_by": decision.matched_by,
        "rule_id": decision.rule_id,
        "rule_name": decision.rule_name,
        "destination_folder": decision.destination_folder,
        "reason": decision.reason,
        "evaluations": decision.evaluations,
    }
    tctx.out.emit(payload, lambda o: render_rule_explain(o, meta, decision))


app.add_typer(rules_app, name="rules")


# -------------------------------------------------------------------- rename
@app.command("rename")
def rename_cmd(
    ctx: typer.Context,
    path: Annotated[
        str,
        typer.Argument(help="Directory containing files to rename."),
    ] = ".",
    preview: Annotated[
        bool,
        typer.Option("--preview", help="Preview batch rename without changing files (default)."),
    ] = False,
    apply: Annotated[
        bool,
        typer.Option("--apply", help="Execute the batch rename and record it in the journal."),
    ] = False,
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Confirm applying renames without interactive prompt."),
    ] = False,
    pattern: Annotated[
        str | None,
        typer.Option(
            "--pattern",
            help="Filename pattern with tokens {n}, {name}, {ext}, {date}, {year}, {month}, {day}.",
        ),
    ] = None,
    prefix: Annotated[
        str,
        typer.Option("--prefix", help="Prefix to prepend to filenames."),
    ] = "",
    suffix: Annotated[
        str,
        typer.Option("--suffix", help="Suffix to append before the file extension."),
    ] = "",
    start: Annotated[
        int,
        typer.Option("--start", help="Starting number for {n} sequential token."),
    ] = 1,
    width: Annotated[
        int,
        typer.Option("--width", help="Zero-padding width for {n} sequential token."),
    ] = 3,
    normalize_ext: Annotated[
        bool,
        typer.Option("--normalize-ext", help="Normalize extensions (lowercase, .jpeg -> .jpg)."),
    ] = False,
    glob_filter: Annotated[
        str | None,
        typer.Option("--glob", help="Only rename files matching this filename glob."),
    ] = None,
    date_source: Annotated[
        str,
        typer.Option(
            "--date-source", help="Timestamp source for {date} tokens: 'mtime' or 'ctime'."
        ),
    ] = "mtime",
) -> None:
    """Batch rename files safely with collision detection, two-phase swap support, and undo."""
    if preview and apply:
        raise UsageError("--preview and --apply cannot be combined.")
    tctx = get_ctx(ctx)
    cfg = load_config(tctx.paths)
    ds = _resolve_date_source(date_source)
    target = resolve_user_path(path)
    mode = "apply" if apply else "preview"

    plan = build_rename_plan(
        target,
        pattern=pattern,
        prefix=prefix,
        suffix=suffix,
        start=start,
        width=width,
        normalize_ext=normalize_ext,
        glob_filter=glob_filter,
        date_source=ds,
        ignore_names=cfg["ignore_names"],
        mode=mode,
    )

    if not apply:
        tctx.out.emit(plan, lambda o: render_rename_preview(o, plan))
        if plan.has_collisions:
            exit_with(ExitCode.WARNING)
        return

    if plan.has_collisions:
        # execute_rename_plan raises SafetyError (exit 3) with full details
        execute_rename_plan(plan, Journal(tctx.paths))

    require_confirmation(
        tctx.out,
        f"Rename {len(plan.active_items)} file(s) in {target}?",
        assume_yes=yes,
    )
    exec_report = execute_rename_plan(
        plan,
        Journal(tctx.paths),
        hash_limit_for_undo=int(cfg["hash_limit_for_undo"]),
    )
    tctx.out.emit(exec_report, lambda o: render_execution_report(o, exec_report))
    if exec_report.failed_count > 0:
        exit_with(ExitCode.WARNING)


# ---------------------------------------------------------------- duplicates
@app.command("duplicates")
def duplicates_cmd(
    ctx: typer.Context,
    path: Annotated[
        str,
        typer.Argument(help="Directory to scan for duplicate files."),
    ] = ".",
    move_to: Annotated[
        str | None,
        typer.Option(
            "--move-to",
            help="Directory to move duplicate extras into (keeps suggested original).",
        ),
    ] = None,
    preview: Annotated[
        bool,
        typer.Option(
            "--preview", help="Preview moving duplicate extras without changing files (default)."
        ),
    ] = False,
    apply: Annotated[
        bool,
        typer.Option("--apply", help="Execute moving duplicate extras into --move-to directory."),
    ] = False,
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Confirm moving duplicates without interactive prompt."),
    ] = False,
    recursive: Annotated[
        bool,
        typer.Option("--recursive/--no-recursive", help="Scan subdirectories recursively."),
    ] = True,
    include_hidden: Annotated[
        bool,
        typer.Option("--include-hidden", help="Include hidden files."),
    ] = False,
) -> None:
    """Find content-identical duplicate files (size -> partial hash -> SHA-256); never deletes."""
    if preview and apply:
        raise UsageError("--preview and --apply cannot be combined.")
    if apply and not move_to:
        raise UsageError(
            "--apply requires --move-to DIR (fileflow never deletes duplicate files).",
            hint="Specify --move-to <review-dir> to move duplicate extras safely.",
        )

    tctx = get_ctx(ctx)
    cfg = load_config(tctx.paths)
    target = resolve_user_path(path)
    move_to_path = resolve_user_path(move_to) if move_to else None

    report = find_duplicates(
        target,
        recursive=recursive,
        include_hidden=include_hidden,
        ignore_names=cfg["ignore_names"],
        exclude_dir=move_to_path,
    )

    if move_to_path is None:
        tctx.out.emit(report, lambda o: render_duplicates_report(o, report))
        return

    mode = "apply" if apply else "preview"
    report.move_to = str(move_to_path)
    report.mode = mode
    move_plan = build_duplicates_move_plan(target, report, move_to_path, mode=mode)
    report.planned_moves = move_plan.items

    if not apply:
        tctx.out.emit(report, lambda o: render_duplicates_report(o, report))
        return

    require_confirmation(
        tctx.out,
        f"Move {move_plan.total_moves} duplicate extra file(s) to {move_to_path}?",
        assume_yes=yes,
    )
    exec_report = execute_duplicates_move(
        move_plan,
        Journal(tctx.paths),
        move_to=move_to_path,
        hash_limit_for_undo=int(cfg["hash_limit_for_undo"]),
    )
    report.execution = exec_report
    tctx.out.emit(report, lambda o: render_duplicates_report(o, report))
    if exec_report.failed_count > 0:
        exit_with(ExitCode.WARNING)


# ------------------------------------------------------------------- history
history_app = typer.Typer(
    help="Inspect journaled file operations and per-file move history.",
    add_completion=False,
    no_args_is_help=False,
    rich_markup_mode=None,
    context_settings={"help_option_names": ["-h", "--help"]},
)


def _emit_history_list(ctx: typer.Context, limit: int) -> None:
    if limit < 1:
        raise UsageError("--limit must be >= 1.")
    tctx = get_ctx(ctx)
    ops = Journal(tctx.paths).list_operations(limit=limit)
    payload = {"count": len(ops), "operations": ops}
    tctx.out.emit(payload, lambda o: render_history_list(o, ops))


@history_app.callback(invoke_without_command=True)
def _history_root(
    ctx: typer.Context,
    limit: Annotated[
        int,
        typer.Option("--limit", "-n", help="Maximum number of recent operations to list."),
    ] = 20,
) -> None:
    if ctx.invoked_subcommand is None:
        _emit_history_list(ctx, limit)


@history_app.command("list")
def history_list_cmd(
    ctx: typer.Context,
    limit: Annotated[
        int,
        typer.Option("--limit", "-n", help="Maximum number of recent operations to list."),
    ] = 20,
) -> None:
    """List recent journaled operations."""
    _emit_history_list(ctx, limit)


@history_app.command("show")
def history_show_cmd(
    ctx: typer.Context,
    operation_id: Annotated[
        str,
        typer.Argument(help="Operation ID to inspect (or 'last' for most recent)."),
    ],
) -> None:
    """Show full per-file details for a single operation."""
    tctx = get_ctx(ctx)
    record = Journal(tctx.paths).get_operation(operation_id)
    tctx.out.emit(record, lambda o: render_history_detail(o, record))


app.add_typer(history_app, name="history")


# ---------------------------------------------------------------------- undo
@app.command("undo")
def undo_cmd(
    ctx: typer.Context,
    operation_id: Annotated[
        str,
        typer.Argument(help="Operation ID to reverse (or 'last' for most recent)."),
    ],
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Validate and preview reversal without moving files."),
    ] = False,
    partial: Annotated[
        bool,
        typer.Option(
            "--partial",
            help="Reverse safe items and skip modified/occupied ones instead of refusing.",
        ),
    ] = False,
    strict: Annotated[
        bool,
        typer.Option(
            "--strict", help="Require every item to be safe before reversing any (default)."
        ),
    ] = False,
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Accept undo without prompting."),
    ] = False,
) -> None:
    """Safely reverse a journaled operation after verifying file identity and free source paths."""
    _ = yes  # accepted for CLI consistency across destructive/reversible commands
    tctx = get_ctx(ctx)
    cfg = load_config(tctx.paths)
    report = undo_operation(
        Journal(tctx.paths),
        operation_id,
        dry_run=dry_run,
        partial=partial,
        strict=strict,
        hash_limit_for_undo=int(cfg["hash_limit_for_undo"]),
    )
    tctx.out.emit(report, lambda o: render_undo_report(o, report))
    if not dry_run and report.skipped_unsafe_count > 0:
        exit_with(ExitCode.WARNING)


# --------------------------------------------------------------------- watch
@app.command("watch")
def watch_cmd(
    ctx: typer.Context,
    path: Annotated[
        str,
        typer.Argument(help="Directory to watch in the foreground."),
    ] = ".",
    interval: Annotated[
        float | None,
        typer.Option("--interval", help="Polling interval in seconds (default from config: 5s)."),
    ] = None,
    preview: Annotated[
        bool,
        typer.Option("--preview", help="Report planned moves without changing files (default)."),
    ] = False,
    apply: Annotated[
        bool,
        typer.Option("--apply", help="Apply organization moves for stable files."),
    ] = False,
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Confirm watching with --apply non-interactively."),
    ] = False,
    once: Annotated[
        bool,
        typer.Option("--once", help="Run a single stability poll cycle and exit."),
    ] = False,
    max_cycles: Annotated[
        int | None,
        typer.Option("--max-cycles", help="Stop after N poll cycles."),
    ] = None,
    recursive: Annotated[
        bool,
        typer.Option(
            "--recursive", "-r", help="Watch subdirectories (excluding organized destinations)."
        ),
    ] = False,
    include_hidden: Annotated[
        bool,
        typer.Option("--include-hidden", help="Include hidden files."),
    ] = False,
    include_symlinks: Annotated[
        bool,
        typer.Option("--include-symlinks", help="Include symlinks."),
    ] = False,
    on_collision: Annotated[
        str | None,
        typer.Option("--on-collision", help="Collision strategy: 'rename' or 'skip'."),
    ] = None,
) -> None:
    """Watch a folder in the foreground and organize stable files (never daemonizes)."""
    if preview and apply:
        raise UsageError("--preview and --apply cannot be combined.")
    if max_cycles is not None and max_cycles < 1:
        raise UsageError("--max-cycles must be >= 1.")

    tctx = get_ctx(ctx)
    cfg = load_config(tctx.paths)
    poll_interval = interval if interval is not None else float(cfg["watch_interval"])
    if poll_interval <= 0:
        raise UsageError("--interval must be > 0.")

    policy = _resolve_collision_policy(on_collision, str(cfg["default_collision"]))
    target = resolve_user_path(path)
    rules = RuleStore(tctx.paths).load()

    if apply:
        require_confirmation(
            tctx.out,
            f"Watch and automatically organize stable files in {target}?",
            assume_yes=yes,
        )

    report = run_watch(
        target,
        journal=Journal(tctx.paths),
        rules=rules,
        interval=poll_interval,
        apply=apply,
        once=once,
        max_cycles=max_cycles,
        recursive=recursive,
        include_hidden=include_hidden,
        include_symlinks=include_symlinks,
        on_collision=policy,
        ignore_names=cfg["ignore_names"],
        hash_limit_for_undo=int(cfg["hash_limit_for_undo"]),
    )
    tctx.out.emit(report, lambda o: render_watch_report(o, report))


def main(argv: Sequence[str] | None = None) -> int:
    """Console-script entry point; returns the process exit code."""
    return run_app(app, tool=TOOL, argv=argv)
