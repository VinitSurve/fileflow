"""Batch rename planner and two-phase cycle/swap-safe executor."""

from __future__ import annotations

import contextlib
import datetime as dt
import fnmatch
import os
import stat
import string
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from fileflow.common.exceptions import SafetyError, UsageError
from fileflow.common.fsutil import sha256_file
from fileflow.common.paths import ensure_not_protected, require_directory
from fileflow.config import DEFAULT_IGNORE_NAMES
from fileflow.executor import (
    ExecutedItemResult,
    ExecutionReport,
    perform_low_level_move,
)
from fileflow.journal import Journal, JournalItem

DateSource = Literal["mtime", "ctime"]
ALLOWED_RENAME_TOKENS = frozenset({"n", "name", "ext", "date", "year", "month", "day"})
EXT_NORMALIZATION_MAP = {
    ".jpeg": ".jpg",
    ".jpe": ".jpg",
    ".jfif": ".jpg",
    ".htm": ".html",
    ".tif": ".tiff",
}


@dataclass
class PlannedRename:
    """Single file rename item in a batch rename plan."""

    seq: int
    source: Path
    destination: Path
    old_name: str
    new_name: str
    size: int
    mtime_ns: int
    is_symlink: bool = False
    unchanged: bool = False
    collision: bool = False
    collision_note: str | None = None


@dataclass
class RenamePlan:
    """Preview/execution plan for 'fileflow rename'."""

    root: Path
    mode: str  # "preview" or "apply"
    pattern: str | None
    prefix: str
    suffix: str
    start: int
    width: int
    normalize_ext: bool
    glob_filter: str | None
    date_source: DateSource
    items: list[PlannedRename] = field(default_factory=list)
    collisions: list[str] = field(default_factory=list)

    @property
    def has_collisions(self) -> bool:
        return bool(self.collisions)

    @property
    def active_items(self) -> list[PlannedRename]:
        return [i for i in self.items if not i.unchanged]


def validate_new_filename(name: str) -> str:
    """Ensure a generated filename is legal on POSIX/APFS."""
    if not name or name in (".", ".."):
        raise UsageError(f"Generated filename is invalid: {name!r}")
    if "/" in name or "\\" in name or "\x00" in name:
        raise UsageError(
            f"Generated filename must not contain path separators or NUL bytes: {name!r}"
        )
    if len(name.encode("utf-8")) > 255:
        raise UsageError(
            f"Generated filename exceeds 255 UTF-8 bytes ({len(name.encode('utf-8'))} B): {name!r}"
        )
    return name


def _validate_pattern(pattern: str) -> None:
    if not pattern:
        raise UsageError("Rename --pattern cannot be empty.")
    formatter = string.Formatter()
    try:
        for _, field_name, format_spec, conversion in formatter.parse(pattern):
            if field_name is None:
                continue
            if conversion or format_spec:
                raise UsageError(f"Unsupported format specifier in rename pattern: {pattern!r}")
            if field_name not in ALLOWED_RENAME_TOKENS:
                allowed = ", ".join(f"{{{t}}}" for t in sorted(ALLOWED_RENAME_TOKENS))
                raise UsageError(
                    f"Unknown token {{{field_name}}} in rename pattern {pattern!r}.",
                    hint=f"Allowed tokens: {allowed}",
                )
    except ValueError as exc:
        raise UsageError(f"Malformed braces in rename pattern {pattern!r}: {exc}") from exc


def _normalize_ext_str(ext: str, normalize: bool) -> str:
    if not ext:
        return ""
    if not normalize:
        return ext
    lower = ext.lower()
    return EXT_NORMALIZATION_MAP.get(lower, lower)


def format_renamed_filename(
    path: Path,
    st: os.stat_result,
    *,
    number: int,
    width: int,
    pattern: str | None,
    prefix: str,
    suffix: str,
    normalize_ext: bool,
    date_source: DateSource,
) -> str:
    """Compute the new filename for ``path`` according to the rename parameters."""
    stem = path.stem
    ext = _normalize_ext_str(path.suffix, normalize_ext)
    ts = st.st_mtime if date_source == "mtime" else getattr(st, "st_birthtime", st.st_ctime)
    dt_obj = dt.datetime.fromtimestamp(ts, tz=dt.UTC)
    num_str = f"{number:0{max(1, width)}d}"

    if pattern is not None:
        # Handle both "{name}_{n}{ext}" and "{name}_{n}.{ext}" cleanly
        norm_pattern = pattern.replace(".{ext}", "{ext}") if ext.startswith(".") else pattern
        rendered = norm_pattern.format(
            n=num_str,
            name=stem,
            ext=ext,
            date=dt_obj.strftime("%Y-%m-%d"),
            year=f"{dt_obj.year:04d}",
            month=f"{dt_obj.month:02d}",
            day=f"{dt_obj.day:02d}",
        )
        if prefix or suffix:
            rendered_path = Path(rendered)
            r_stem = rendered_path.stem
            r_ext = rendered_path.suffix
            rendered = f"{prefix}{r_stem}{suffix}{r_ext}"
    else:
        rendered = f"{prefix}{stem}{suffix}{ext}"

    return validate_new_filename(rendered)


def build_rename_plan(
    root: Path,
    *,
    pattern: str | None = None,
    prefix: str = "",
    suffix: str = "",
    start: int = 1,
    width: int = 3,
    normalize_ext: bool = False,
    glob_filter: str | None = None,
    date_source: DateSource = "mtime",
    include_hidden: bool = False,
    ignore_names: Sequence[str] = DEFAULT_IGNORE_NAMES,
    mode: str = "preview",
) -> RenamePlan:
    """Build a deterministic batch rename plan with full collision detection."""
    require_directory(root, what="Target folder")
    ensure_not_protected(root, "rename")

    if pattern is None and not prefix and not suffix and not normalize_ext:
        raise UsageError(
            "Specify at least one rename transformation "
            "(--pattern, --prefix, --suffix, or --normalize-ext).",
            hint="Example: fileflow rename ./photos --pattern '{date}_{n}{ext}' --preview",
        )
    if pattern is not None:
        _validate_pattern(pattern)
    if start < 0:
        raise UsageError("--start must be >= 0.")
    if width < 1 or width > 16:
        raise UsageError("--width must be between 1 and 16.")

    ignore_set = set(ignore_names)
    allow_hidden = include_hidden or (glob_filter is not None and glob_filter.startswith("."))

    existing_by_cf: dict[str, str] = {}
    candidates: list[tuple[Path, os.stat_result]] = []

    try:
        entries = sorted(os.scandir(root), key=lambda e: e.name)
    except OSError as exc:
        raise UsageError(f"Cannot read directory {root}: {exc}") from exc

    for entry in entries:
        existing_by_cf[entry.name.casefold()] = entry.name
        if not allow_hidden and (entry.name.startswith(".") or entry.name in ignore_set):
            continue
        if glob_filter and not fnmatch.fnmatchcase(entry.name.lower(), glob_filter.lower()):
            continue
        try:
            st = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        if stat.S_ISREG(st.st_mode):
            candidates.append((root / entry.name, st))

    plan = RenamePlan(
        root=root,
        mode=mode,
        pattern=pattern,
        prefix=prefix,
        suffix=suffix,
        start=start,
        width=width,
        normalize_ext=normalize_ext,
        glob_filter=glob_filter,
        date_source=date_source,
    )

    raw_items: list[PlannedRename] = []
    for idx, (src_path, st) in enumerate(candidates):
        num = start + idx
        new_name = format_renamed_filename(
            src_path,
            st,
            number=num,
            width=width,
            pattern=pattern,
            prefix=prefix,
            suffix=suffix,
            normalize_ext=normalize_ext,
            date_source=date_source,
        )
        dst_path = root / new_name
        unchanged = new_name == src_path.name
        raw_items.append(
            PlannedRename(
                seq=idx + 1,
                source=src_path,
                destination=dst_path,
                old_name=src_path.name,
                new_name=new_name,
                size=st.st_size,
                mtime_ns=st.st_mtime_ns,
                is_symlink=False,
                unchanged=unchanged,
            )
        )

    # Set of casefolded source names that are actively changing in this batch
    moving_source_cfs = {item.old_name.casefold() for item in raw_items if not item.unchanged}
    claimed_target_cfs: dict[str, PlannedRename] = {}

    for item in raw_items:
        if item.unchanged:
            claimed_target_cfs[item.new_name.casefold()] = item

    for item in raw_items:
        if item.unchanged:
            continue
        target_cf = item.new_name.casefold()
        if target_cf in claimed_target_cfs:
            prev = claimed_target_cfs[target_cf]
            msg = (
                f"Collision: '{item.old_name}' and '{prev.old_name}' both rename to "
                f"'{item.new_name}' (case-insensitive)"
            )
            item.collision = True
            item.collision_note = msg
            prev.collision = True
            if prev.collision_note is None:
                prev.collision_note = msg
            plan.collisions.append(msg)
            continue

        if target_cf in existing_by_cf and target_cf not in moving_source_cfs:
            existing_name = existing_by_cf[target_cf]
            msg = (
                f"Collision: '{item.old_name}' -> '{item.new_name}' collides with existing "
                f"file '{existing_name}'"
            )
            item.collision = True
            item.collision_note = msg
            plan.collisions.append(msg)
            continue

        claimed_target_cfs[target_cf] = item

    plan.items = raw_items
    return plan


def execute_rename_plan(
    plan: RenamePlan,
    journal: Journal,
    *,
    hash_limit_for_undo: int = 50_000_000,
    before_move_hook: Callable[[int], None] | None = None,
) -> ExecutionReport:
    """Execute a :class:`RenamePlan` using two-phase temp staging to handle swaps and cycles."""
    ensure_not_protected(plan.root, "rename")

    if plan.has_collisions:
        detail = "; ".join(plan.collisions[:3])
        raise SafetyError(
            f"Refusing to rename files due to {len(plan.collisions)} collision(s): {detail}",
            hint="Adjust --pattern, --prefix, --suffix, or --width so every target name is unique.",
        )

    active = plan.active_items
    if not active:
        return ExecutionReport(
            operation_id="",
            operation_type="rename",
            root=str(plan.root),
            status="noop",
            done_count=0,
            failed_count=0,
            skipped_count=len(plan.items),
            total_bytes=0,
            results=[],
            errors=[],
        )

    # Re-validate sources and check for unexpected new files on disk before touching anything
    moving_source_cfs = {i.old_name.casefold() for i in active}
    for item in active:
        if not os.path.lexists(item.source):
            raise SafetyError(f"Source file no longer exists: {item.source}")
        st = item.source.lstat()
        if st.st_size != item.size or st.st_mtime_ns != item.mtime_ns:
            raise SafetyError(f"Source file changed since plan was built: {item.source}")
        if not os.access(item.source, os.R_OK):
            raise SafetyError(f"Permission denied reading source file: {item.source}")
        if os.path.lexists(item.destination) and item.new_name.casefold() not in moving_source_cfs:
            raise SafetyError(f"Destination file already exists: {item.destination}")

    journal_items = [
        JournalItem(
            seq=item.seq,
            kind="rename",
            source=str(item.source),
            destination=str(item.destination),
            status="planned",
            size=item.size,
            mtime_ns=item.mtime_ns,
            is_symlink=item.is_symlink,
        )
        for item in active
    ]
    op_id = journal.begin_operation(
        "rename",
        plan.root,
        journal_items,
        meta={
            "pattern": plan.pattern,
            "prefix": plan.prefix,
            "suffix": plan.suffix,
            "start": plan.start,
            "width": plan.width,
            "normalize_ext": plan.normalize_ext,
        },
    )

    staged: list[tuple[PlannedRename, Path]] = []
    results: list[ExecutedItemResult] = []
    errors: list[dict[str, str]] = []
    done_count = 0
    failed_count = 0
    total_bytes = 0

    try:
        # Phase 1: move all active sources to unique temporary names in the same directory
        try:
            for item in active:
                tmp_path = plan.root / f".fileflow_tmp_{op_id}_{item.seq}_{item.old_name}"
                counter = 1
                while os.path.lexists(tmp_path):
                    tmp_path = (
                        plan.root / f".fileflow_tmp_{op_id}_{item.seq}_{counter}_{item.old_name}"
                    )
                    counter += 1
                perform_low_level_move(item.source, tmp_path, is_symlink=item.is_symlink)
                staged.append((item, tmp_path))
        except BaseException as exc:
            # Roll back any already-staged temp files to their original names
            for prev_item, prev_tmp in reversed(staged):
                with contextlib.suppress(OSError):
                    if os.path.lexists(prev_tmp) and not os.path.lexists(prev_item.source):
                        perform_low_level_move(
                            prev_tmp, prev_item.source, is_symlink=prev_item.is_symlink
                        )
            if isinstance(exc, KeyboardInterrupt):
                journal.finish_operation(op_id, "interrupted")
                raise
            err_msg = f"{type(exc).__name__}: {exc}"
            for item in active:
                journal.record_item_failed(op_id, item.seq, err_msg)
                failed_count += 1
                errors.append({"source": str(item.source), "error": err_msg})
                results.append(
                    ExecutedItemResult(
                        seq=item.seq,
                        source=str(item.source),
                        destination=str(item.destination),
                        status="failed",
                        size=item.size,
                        error=err_msg,
                    )
                )
            journal.finish_operation(op_id, "failed")
            return ExecutionReport(
                operation_id=op_id,
                operation_type="rename",
                root=str(plan.root),
                status="failed",
                done_count=0,
                failed_count=failed_count,
                skipped_count=0,
                total_bytes=0,
                results=results,
                errors=errors,
            )

        # Phase 2: move each staged temp file to its final destination and update journal
        completed_seqs: set[int] = set()
        try:
            for item, tmp_path in staged:
                if before_move_hook is not None:
                    before_move_hook(item.seq)
                perform_low_level_move(tmp_path, item.destination, is_symlink=item.is_symlink)
                post_stat = item.destination.lstat()
                content_hash: str | None = None
                if not item.is_symlink and post_stat.st_size <= hash_limit_for_undo:
                    with contextlib.suppress(OSError):
                        content_hash = sha256_file(item.destination)
                journal.record_item_done(
                    op_id,
                    item.seq,
                    destination=str(item.destination),
                    size=post_stat.st_size,
                    mtime_ns=post_stat.st_mtime_ns,
                    sha256=content_hash,
                    created_dirs=[],
                )
                completed_seqs.add(item.seq)
                done_count += 1
                total_bytes += post_stat.st_size
                results.append(
                    ExecutedItemResult(
                        seq=item.seq,
                        source=str(item.source),
                        destination=str(item.destination),
                        status="done",
                        size=post_stat.st_size,
                    )
                )
        except BaseException:
            # Restore any remaining staged temp files that haven't been moved to destination yet
            for rem_item, rem_tmp in staged:
                if rem_item.seq not in completed_seqs and os.path.lexists(rem_tmp):
                    if not os.path.lexists(rem_item.source):
                        with contextlib.suppress(OSError):
                            perform_low_level_move(
                                rem_tmp, rem_item.source, is_symlink=rem_item.is_symlink
                            )
                    elif not os.path.lexists(rem_item.destination):
                        # Original slot was taken by an earlier swap item; complete this move
                        with contextlib.suppress(OSError):
                            perform_low_level_move(
                                rem_tmp, rem_item.destination, is_symlink=rem_item.is_symlink
                            )
                            st2 = rem_item.destination.lstat()
                            h2 = (
                                sha256_file(rem_item.destination)
                                if st2.st_size <= hash_limit_for_undo
                                else None
                            )
                            journal.record_item_done(
                                op_id,
                                rem_item.seq,
                                destination=str(rem_item.destination),
                                size=st2.st_size,
                                mtime_ns=st2.st_mtime_ns,
                                sha256=h2,
                                created_dirs=[],
                            )
            raise
    except KeyboardInterrupt:
        journal.finish_operation(op_id, "interrupted")
        raise

    final_status = "applied" if failed_count == 0 else ("partial" if done_count > 0 else "failed")
    journal.finish_operation(op_id, final_status)
    return ExecutionReport(
        operation_id=op_id,
        operation_type="rename",
        root=str(plan.root),
        status=final_status,
        done_count=done_count,
        failed_count=failed_count,
        skipped_count=len(plan.items) - len(active),
        total_bytes=total_bytes,
        results=results,
        errors=errors,
    )
