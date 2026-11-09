"""Safe, journaled file move executor with metadata preservation and EXDEV fallback."""

from __future__ import annotations

import contextlib
import errno
import os
import shutil
import stat
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fileflow.common.fsutil import sha256_file
from fileflow.common.paths import ensure_not_protected
from fileflow.journal import Journal, JournalItem
from fileflow.planner import CollisionPolicy, OrganizePlan


@dataclass
class ExecutedItemResult:
    """Result of executing a single planned item."""

    seq: int
    source: str
    destination: str
    status: str  # "done", "failed", "skipped"
    size: int
    reason: str = ""
    error: str | None = None
    collision_note: str | None = None


@dataclass
class ExecutionReport:
    """Result of applying an organize, rename, duplicates, or watch plan."""

    operation_id: str
    operation_type: str
    root: str
    status: str  # "applied", "partial", "failed", "interrupted", "noop"
    done_count: int
    failed_count: int
    skipped_count: int
    total_bytes: int
    results: list[ExecutedItemResult] = field(default_factory=list)
    errors: list[dict[str, str]] = field(default_factory=list)


@dataclass
class MoveOutcome:
    """Post-move metadata captured for the operation journal."""

    destination: Path
    size: int
    mtime_ns: int
    sha256: str | None
    created_dirs: list[str]


def _ensure_parent_dirs(parent: Path) -> list[str]:
    """Create missing ancestor directories and return the exact list of directories created."""
    missing: list[Path] = []
    cur = Path(os.path.abspath(parent))
    while not cur.exists():
        missing.append(cur)
        if cur.parent == cur:
            break
        cur = cur.parent
    created: list[str] = []
    for directory in reversed(missing):
        directory.mkdir()
        created.append(str(directory))
    return created


def _cleanup_created_dirs(created_dirs: Sequence[str]) -> None:
    """Remove newly created directories in reverse order if they are still empty."""
    for d in reversed(created_dirs):
        with contextlib.suppress(OSError):
            Path(d).rmdir()


def _casefold_collision_in_dir(destination: Path, *, allow_source: Path | None = None) -> bool:
    """Check if ``destination.parent`` has a case-insensitive match for ``destination.name``."""
    parent = destination.parent
    if not parent.exists() or not parent.is_dir():
        return False
    target_cf = destination.name.casefold()
    try:
        for entry in os.scandir(parent):
            if entry.name.casefold() == target_cf:
                if allow_source is not None:
                    try:
                        if os.path.samestat(
                            entry.stat(follow_symlinks=False), allow_source.lstat()
                        ):
                            return False
                    except OSError:
                        pass
                return True
    except OSError:
        pass
    return False


def _unique_casefold_destination(destination: Path) -> Path:
    """Find ``name (1).ext``, ``name (2).ext`` that does not exist even case-insensitively."""
    if not os.path.lexists(destination) and not _casefold_collision_in_dir(destination):
        return destination
    stem = destination.stem
    suffix = destination.suffix
    parent = destination.parent
    counter = 1
    while True:
        candidate = parent / f"{stem} ({counter}){suffix}"
        if not os.path.lexists(candidate) and not _casefold_collision_in_dir(candidate):
            return candidate
        counter += 1


def perform_low_level_move(
    source: Path,
    destination: Path,
    *,
    is_symlink: bool,
) -> None:
    """Move ``source`` to ``destination`` via ``os.rename`` with verified ``EXDEV`` fallback."""
    try:
        os.rename(source, destination)  # noqa: PTH104
        return
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise

    # Cross-filesystem move fallback
    if is_symlink:
        link_target = source.readlink()
        destination.symlink_to(link_target)
        try:
            if not destination.is_symlink() or destination.readlink() != link_target:
                raise OSError("Cross-device symlink verification failed")
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(destination)
            raise
        os.unlink(source)
        return

    src_stat = source.lstat()
    src_size = src_stat.st_size
    src_hash = sha256_file(source)
    try:
        shutil.copy2(source, destination, follow_symlinks=False)
        with contextlib.suppress(OSError):
            fd = os.open(destination, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        dst_stat = destination.lstat()
        if dst_stat.st_size != src_size:
            raise OSError(
                f"Cross-device copy size mismatch (expected {src_size}, got {dst_stat.st_size})"
            )
        dst_hash = sha256_file(destination)
        if dst_hash != src_hash:
            raise OSError("Cross-device copy SHA-256 verification failed")
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(destination)
        raise

    os.unlink(source)


def move_item(
    source: Path,
    destination: Path,
    *,
    expected_size: int,
    expected_mtime_ns: int,
    expected_is_symlink: bool = False,
    on_collision: CollisionPolicy | str = "rename",
    hash_limit_for_undo: int = 50_000_000,
    allow_same_source: bool = False,
    before_move_hook: Callable[[int], None] | None = None,
    seq: int = 1,
) -> MoveOutcome | None:
    """Validate, create parent dirs, and move one item safely without ever overwriting.

    Returns ``None`` if ``on_collision == 'skip'`` and a collision occurred at execution time.
    """
    ensure_not_protected(source, "move")
    ensure_not_protected(destination, "move to")

    if not os.path.lexists(source):
        raise FileNotFoundError(f"Source file no longer exists: {source}")

    st = source.lstat()
    actual_is_symlink = stat.S_ISLNK(st.st_mode)
    if actual_is_symlink != expected_is_symlink:
        raise OSError(f"Source file type changed since planning: {source}")

    if not actual_is_symlink:
        if not stat.S_ISREG(st.st_mode):
            raise OSError(f"Source is not a regular file: {source}")
        if not os.access(source, os.R_OK):
            raise PermissionError(f"Permission denied reading source file: {source}")
        if st.st_size != expected_size or st.st_mtime_ns != expected_mtime_ns:
            raise OSError(f"Source file was modified since plan was created: {source}")

    allow_src = source if allow_same_source else None
    has_dest_collision = (
        os.path.lexists(destination)
        and (allow_src is None or not os.path.samestat(destination.lstat(), st))
    ) or _casefold_collision_in_dir(destination, allow_source=allow_src)

    final_dest = destination
    if has_dest_collision:
        if on_collision == "skip":
            return None
        if on_collision == "rename":
            final_dest = _unique_casefold_destination(destination)
        else:
            raise FileExistsError(f"Destination already exists: {destination}")

    if before_move_hook is not None:
        before_move_hook(seq)

    created_dirs = _ensure_parent_dirs(final_dest.parent)
    try:
        perform_low_level_move(source, final_dest, is_symlink=actual_is_symlink)
    except BaseException:
        _cleanup_created_dirs(created_dirs)
        raise

    post_stat = final_dest.lstat()
    content_hash: str | None = None
    if not actual_is_symlink and post_stat.st_size <= hash_limit_for_undo:
        with contextlib.suppress(OSError):
            content_hash = sha256_file(final_dest)

    return MoveOutcome(
        destination=final_dest,
        size=post_stat.st_size,
        mtime_ns=post_stat.st_mtime_ns,
        sha256=content_hash,
        created_dirs=created_dirs,
    )


def execute_organize_plan(
    plan: OrganizePlan,
    journal: Journal,
    *,
    op_type: str = "organize",
    hash_limit_for_undo: int = 50_000_000,
    meta: dict[str, Any] | None = None,
    before_move_hook: Callable[[int], None] | None = None,
) -> ExecutionReport:
    """Execute an :class:`OrganizePlan` with write-ahead journaling and partial-failure recovery."""
    ensure_not_protected(plan.root, op_type)

    if not plan.items:
        return ExecutionReport(
            operation_id="",
            operation_type=op_type,
            root=str(plan.root),
            status="noop",
            done_count=0,
            failed_count=0,
            skipped_count=0,
            total_bytes=0,
            results=[],
            errors=[],
        )

    journal_items: list[JournalItem] = [
        JournalItem(
            seq=item.seq,
            kind="move",
            source=str(item.source),
            destination=str(item.destination),
            status="planned" if item.action == "move" else "skipped",
            error=item.collision_note if item.action == "skip" else None,
            size=item.size,
            mtime_ns=item.mtime_ns,
            is_symlink=item.is_symlink,
        )
        for item in plan.items
    ]

    op_meta = {
        "recursive": plan.recursive,
        "include_hidden": plan.include_hidden,
        "include_symlinks": plan.include_symlinks,
        "on_collision": plan.on_collision,
        **(meta or {}),
    }
    op_id = journal.begin_operation(op_type, plan.root, journal_items, meta=op_meta)

    results: list[ExecutedItemResult] = []
    errors: list[dict[str, str]] = []
    done_count = 0
    failed_count = 0
    skipped_count = 0
    total_bytes = 0

    try:
        for item in plan.items:
            if item.action == "skip":
                skipped_count += 1
                results.append(
                    ExecutedItemResult(
                        seq=item.seq,
                        source=str(item.source),
                        destination=str(item.destination),
                        status="skipped",
                        size=item.size,
                        reason=item.reason,
                        collision_note=item.collision_note,
                    )
                )
                continue

            try:
                outcome = move_item(
                    item.source,
                    item.destination,
                    expected_size=item.size,
                    expected_mtime_ns=item.mtime_ns,
                    expected_is_symlink=item.is_symlink,
                    on_collision=plan.on_collision,
                    hash_limit_for_undo=hash_limit_for_undo,
                    before_move_hook=before_move_hook,
                    seq=item.seq,
                )
                if outcome is None:
                    skipped_count += 1
                    reason_msg = "Destination appeared before move; skipped"
                    journal.record_item_skipped(op_id, item.seq, reason_msg)
                    results.append(
                        ExecutedItemResult(
                            seq=item.seq,
                            source=str(item.source),
                            destination=str(item.destination),
                            status="skipped",
                            size=item.size,
                            reason=item.reason,
                            collision_note=reason_msg,
                        )
                    )
                    continue

                journal.record_item_done(
                    op_id,
                    item.seq,
                    destination=str(outcome.destination),
                    size=outcome.size,
                    mtime_ns=outcome.mtime_ns,
                    sha256=outcome.sha256,
                    created_dirs=outcome.created_dirs,
                )
                done_count += 1
                total_bytes += outcome.size
                results.append(
                    ExecutedItemResult(
                        seq=item.seq,
                        source=str(item.source),
                        destination=str(outcome.destination),
                        status="done",
                        size=outcome.size,
                        reason=item.reason,
                        collision_note=item.collision_note,
                    )
                )
            except (OSError, Exception) as exc:
                if isinstance(exc, KeyboardInterrupt):
                    raise
                err_msg = f"{type(exc).__name__}: {exc}"
                failed_count += 1
                journal.record_item_failed(op_id, item.seq, err_msg)
                errors.append({"source": str(item.source), "error": err_msg})
                results.append(
                    ExecutedItemResult(
                        seq=item.seq,
                        source=str(item.source),
                        destination=str(item.destination),
                        status="failed",
                        size=item.size,
                        reason=item.reason,
                        error=err_msg,
                    )
                )
    except KeyboardInterrupt:
        journal.finish_operation(op_id, "interrupted")
        raise

    if failed_count == 0:
        final_status = "applied"
    elif done_count > 0:
        final_status = "partial"
    else:
        final_status = "failed"

    journal.finish_operation(op_id, final_status)
    return ExecutionReport(
        operation_id=op_id,
        operation_type=op_type,
        root=str(plan.root),
        status=final_status,
        done_count=done_count,
        failed_count=failed_count,
        skipped_count=skipped_count,
        total_bytes=total_bytes,
        results=results,
        errors=errors,
    )
