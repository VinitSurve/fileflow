"""Safe, verified transaction reversal ('fileflow undo') with zero-overwrite guarantees."""

from __future__ import annotations

import contextlib
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path

from fileflow.common.exceptions import SafetyError, UsageError
from fileflow.common.fsutil import sha256_file
from fileflow.executor import _ensure_parent_dirs, perform_low_level_move
from fileflow.journal import Journal, JournalItem


@dataclass
class UndoItemCheck:
    """Validation and execution status of a single item during undo."""

    seq: int
    source: str  # original location to restore to
    destination: str  # current location to move back from
    safe: bool
    status: str  # "restored", "would_restore", "skipped_unsafe", "failed"
    reason: str = ""
    size: int = 0


@dataclass
class UndoReport:
    """Result of 'fileflow undo'."""

    operation_id: str
    operation_type: str
    dry_run: bool
    partial_mode: bool
    status: str  # "undone", "partially_undone", "dry_run"
    restored_count: int
    skipped_unsafe_count: int
    removed_dirs: list[str] = field(default_factory=list)
    items: list[UndoItemCheck] = field(default_factory=list)


def _check_destination_identity(item: JournalItem, *, hash_limit_for_undo: int) -> tuple[bool, str]:
    """Verify that the file at ``item.destination`` still matches the recorded identity."""
    dest = Path(item.destination)
    if not os.path.lexists(dest):
        return False, f"Destination file no longer exists: {dest}"

    actual_is_symlink = dest.is_symlink()
    if actual_is_symlink != item.is_symlink:
        return False, f"Destination file type changed (symlink mismatch): {dest}"

    try:
        st = dest.lstat()
    except OSError as exc:
        return False, f"Cannot stat destination file {dest}: {exc}"

    if not item.is_symlink:
        if not stat.S_ISREG(st.st_mode):
            return False, f"Destination is no longer a regular file: {dest}"
        if st.st_size != item.size:
            return (
                False,
                f"Destination file modified (size {item.size} B -> {st.st_size} B): {dest}",
            )
        if st.st_mtime_ns != item.mtime_ns:
            return False, f"Destination file was modified (mtime changed): {dest}"
        if item.sha256 is not None and st.st_size <= max(hash_limit_for_undo, st.st_size):
            try:
                current_hash = sha256_file(dest)
            except OSError as exc:
                return False, f"Cannot read destination file to verify hash ({dest}): {exc}"
            if current_hash != item.sha256:
                return False, f"Destination file content was modified (SHA-256 mismatch): {dest}"

    return True, "verified"


def _parent_can_be_recreated(path: Path) -> tuple[bool, str]:
    cur = path.parent
    while not os.path.lexists(cur):
        if cur.parent == cur:
            break
        cur = cur.parent
    if os.path.lexists(cur) and (cur.is_symlink() or not cur.is_dir()):
        return False, f"Original parent path is blocked by a non-directory: {cur}"
    return True, ""


def undo_operation(
    journal: Journal,
    operation_id: str,
    *,
    dry_run: bool = False,
    partial: bool = False,
    strict: bool = False,
    hash_limit_for_undo: int = 50_000_000,
) -> UndoReport:
    """Validate and safely reverse a journaled operation."""
    if partial and strict:
        raise UsageError("--partial and --strict cannot be combined.")

    record = journal.get_operation(operation_id)
    if record.status == "undone":
        raise SafetyError(
            f"Operation {record.id} has already been undone.",
            hint="Run 'fileflow history' to inspect past operations.",
        )

    done_items = [i for i in reversed(record.items) if i.status == "done"]
    if not done_items:
        raise SafetyError(
            f"Operation {record.id} has no completed items to undo (status: {record.status}).",
            hint="Only operations with applied moves can be undone.",
        )

    # Step 1: check destination identity for all done items
    dest_checks: dict[int, tuple[bool, str]] = {}
    valid_dest_paths_cf: set[str] = set()
    for item in done_items:
        ok, msg = _check_destination_identity(item, hash_limit_for_undo=hash_limit_for_undo)
        dest_checks[item.seq] = (ok, msg)
        if ok:
            valid_dest_paths_cf.add(os.path.abspath(item.destination).casefold())

    # Step 2: check original source location is free (allowing intra-batch swap destinations
    # that are themselves valid and being undone in this batch)
    checks: list[tuple[JournalItem, UndoItemCheck]] = []
    for item in done_items:
        ok, msg = dest_checks[item.seq]
        src = Path(item.source)
        dest = Path(item.destination)
        if ok:
            parent_ok, parent_msg = _parent_can_be_recreated(src)
            if not parent_ok:
                ok = False
                msg = parent_msg
            elif os.path.lexists(src):
                src_abs_cf = os.path.abspath(src).casefold()
                same_inode = False
                with contextlib.suppress(OSError):
                    same_inode = os.path.samestat(src.lstat(), dest.lstat())
                if not same_inode and src_abs_cf not in valid_dest_paths_cf:
                    ok = False
                    msg = f"Original path is occupied: {src}"

        status_label = ("would_restore" if dry_run else "restored") if ok else "skipped_unsafe"
        checks.append(
            (
                item,
                UndoItemCheck(
                    seq=item.seq,
                    source=item.source,
                    destination=item.destination,
                    safe=ok,
                    status=status_label,
                    reason=msg,
                    size=item.size,
                ),
            )
        )

    # If in partial mode an item was marked unsafe, its destination will NOT be vacated,
    # so any other item whose original source is at that unsafe item's destination is also unsafe.
    changed = True
    while changed:
        changed = False
        safe_dest_cfs = {
            os.path.abspath(it.destination).casefold() for it, chk in checks if chk.safe
        }
        for it, chk in checks:
            if not chk.safe:
                continue
            src = Path(it.source)
            dest = Path(it.destination)
            if os.path.lexists(src):
                same_inode = False
                with contextlib.suppress(OSError):
                    same_inode = os.path.samestat(src.lstat(), dest.lstat())
                src_cf = os.path.abspath(src).casefold()
                if not same_inode and src_cf not in safe_dest_cfs:
                    chk.safe = False
                    chk.status = "skipped_unsafe"
                    chk.reason = f"Original path is occupied: {src}"
                    changed = True

    safe_pairs = [(it, chk) for it, chk in checks if chk.safe]
    unsafe_pairs = [(it, chk) for it, chk in checks if not chk.safe]

    if dry_run:
        return UndoReport(
            operation_id=record.id,
            operation_type=record.type,
            dry_run=True,
            partial_mode=partial,
            status="dry_run",
            restored_count=len(safe_pairs),
            skipped_unsafe_count=len(unsafe_pairs),
            removed_dirs=[],
            items=[chk for _, chk in checks],
        )

    if unsafe_pairs and (not partial or strict):
        reasons = "; ".join(f"{chk.destination} ({chk.reason})" for _, chk in unsafe_pairs[:3])
        raise SafetyError(
            f"Refusing to undo operation {record.id}: "
            f"{len(unsafe_pairs)} item(s) failed safety validation — {reasons}",
            hint=(
                "Pass --partial to reverse only the safe items and leave modified "
                "or occupied files untouched."
            ),
        )

    if not safe_pairs:
        reasons = "; ".join(f"{chk.destination} ({chk.reason})" for _, chk in unsafe_pairs[:3])
        raise SafetyError(
            f"Cannot undo operation {record.id}: no items are safe to reverse — {reasons}",
        )

    # Determine if two-phase staging is needed (when any safe source overlaps a safe destination)
    safe_dest_cfs = {os.path.abspath(it.destination).casefold() for it, _ in safe_pairs}
    needs_two_phase = any(
        os.path.abspath(it.source).casefold() in safe_dest_cfs for it, _ in safe_pairs
    )

    all_created_dirs: list[str] = []
    if needs_two_phase:
        staged: list[tuple[JournalItem, UndoItemCheck, Path]] = []
        for it, chk in safe_pairs:
            dest_path = Path(it.destination)
            tmp_path = dest_path.parent / f".fileflow_undo_{record.id}_{it.seq}.tmp"
            counter = 1
            while os.path.lexists(tmp_path):
                tmp_path = dest_path.parent / f".fileflow_undo_{record.id}_{it.seq}_{counter}.tmp"
                counter += 1
            perform_low_level_move(dest_path, tmp_path, is_symlink=it.is_symlink)
            staged.append((it, chk, tmp_path))

        for it, chk, tmp_path in staged:
            src_path = Path(it.source)
            _ensure_parent_dirs(src_path.parent)
            perform_low_level_move(tmp_path, src_path, is_symlink=it.is_symlink)
            journal.record_item_undone(record.id, it.seq)
            chk.status = "restored"
            all_created_dirs.extend(it.created_dirs)
    else:
        for it, chk in safe_pairs:
            dest_path = Path(it.destination)
            src_path = Path(it.source)
            _ensure_parent_dirs(src_path.parent)
            perform_low_level_move(dest_path, src_path, is_symlink=it.is_symlink)
            journal.record_item_undone(record.id, it.seq)
            chk.status = "restored"
            all_created_dirs.extend(it.created_dirs)

    # Remove empty destination directories created by this operation (deepest first)
    unique_dirs = sorted(
        set(all_created_dirs),
        key=lambda p: len(Path(p).parts),
        reverse=True,
    )
    removed_dirs: list[str] = []
    for d_str in unique_dirs:
        d_path = Path(d_str)
        if d_path.exists() and not d_path.is_symlink() and d_path.is_dir():
            try:
                d_path.rmdir()
                removed_dirs.append(d_str)
            except OSError:
                # Directory is not empty or cannot be removed; leave it intact
                pass

    final_status = "undone" if not unsafe_pairs else "partially_undone"
    journal.finish_operation(record.id, final_status)

    return UndoReport(
        operation_id=record.id,
        operation_type=record.type,
        dry_run=False,
        partial_mode=partial,
        status=final_status,
        restored_count=len(safe_pairs),
        skipped_unsafe_count=len(unsafe_pairs),
        removed_dirs=removed_dirs,
        items=[chk for _, chk in checks],
    )
