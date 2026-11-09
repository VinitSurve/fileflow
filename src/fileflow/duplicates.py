"""Content-hash duplicate finder and safe journaled '--move-to' review mover."""

from __future__ import annotations

import os
import stat
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from fileflow.common.fsutil import human_size, sha256_file
from fileflow.common.paths import ensure_not_protected, require_directory
from fileflow.config import DEFAULT_IGNORE_NAMES
from fileflow.executor import ExecutionReport, execute_organize_plan
from fileflow.journal import Journal
from fileflow.planner import (
    OrganizePlan,
    PlannedMove,
    resolve_collision_free_destination,
)

PARTIAL_HASH_BYTES = 64 * 1024


@dataclass
class DuplicateFileItem:
    """Single file within a duplicate group."""

    path: str
    rel_path: str
    size: int
    mtime: float
    mtime_ns: int
    is_keeper: bool
    reason: str


@dataclass
class DuplicateGroup:
    """Group of byte-identical files (verified by full SHA-256)."""

    sha256: str
    size: int
    size_human: str
    count: int
    wasted_bytes: int
    wasted_human: str
    keeper: str
    keeper_reason: str
    files: list[DuplicateFileItem] = field(default_factory=list)


@dataclass
class HardlinkGroup:
    """Files sharing the same inode on disk (not duplicates in storage)."""

    inode: int
    device: int
    size: int
    size_human: str
    note: str
    paths: list[str] = field(default_factory=list)


@dataclass
class DuplicatesReport:
    """Result of 'fileflow duplicates'."""

    root: str
    total_scanned: int
    duplicate_groups_count: int
    duplicate_files_count: int
    wasted_bytes: int
    wasted_human: str
    groups: list[DuplicateGroup] = field(default_factory=list)
    hardlinks: list[HardlinkGroup] = field(default_factory=list)
    move_to: str | None = None
    mode: str = "report"  # "report", "preview", "apply"
    planned_moves: list[PlannedMove] = field(default_factory=list)
    execution: ExecutionReport | None = None
    warnings: list[str] = field(default_factory=list)


@dataclass
class _CandidateFile:
    path: Path
    rel_path: str
    size: int
    mtime: float
    mtime_ns: int
    dev: int
    ino: int


def find_duplicates(
    root: Path,
    *,
    recursive: bool = True,
    include_hidden: bool = False,
    ignore_names: Sequence[str] = DEFAULT_IGNORE_NAMES,
    exclude_dir: Path | None = None,
) -> DuplicatesReport:
    """Scan ``root`` and group content-identical files by size -> partial hash -> full SHA-256."""
    require_directory(root, what="Target folder")
    ignore_set = set(ignore_names)
    exclude_abs = Path(os.path.abspath(exclude_dir)) if exclude_dir is not None else None
    warnings: list[str] = []
    all_files: list[_CandidateFile] = []

    def _walk(current: Path) -> None:
        try:
            entries = sorted(os.scandir(current), key=lambda e: e.name)
        except OSError as exc:
            warnings.append(f"Cannot scan {current}: {exc}")
            return

        subdirs: list[Path] = []
        for entry in entries:
            name = entry.name
            if not include_hidden and (name.startswith(".") or name in ignore_set):
                continue
            entry_path = current / name
            entry_abs = Path(os.path.abspath(entry_path))
            if exclude_abs is not None and (
                entry_abs == exclude_abs or exclude_abs in entry_abs.parents
            ):
                continue
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISLNK(st.st_mode):
                continue
            if stat.S_ISDIR(st.st_mode):
                if recursive:
                    subdirs.append(entry_path)
                continue
            if stat.S_ISREG(st.st_mode):
                all_files.append(
                    _CandidateFile(
                        path=entry_path,
                        rel_path=entry_path.relative_to(root).as_posix(),
                        size=st.st_size,
                        mtime=st.st_mtime,
                        mtime_ns=st.st_mtime_ns,
                        dev=st.st_dev,
                        ino=st.st_ino,
                    )
                )

        for sd in subdirs:
            _walk(sd)

    _walk(root)

    # Identify hardlink groups sharing the same (dev, ino)
    by_inode: dict[tuple[int, int], list[_CandidateFile]] = {}
    for cand in all_files:
        by_inode.setdefault((cand.dev, cand.ino), []).append(cand)

    hardlink_groups: list[HardlinkGroup] = []
    unique_inodes: list[_CandidateFile] = []
    for (dev, ino), inode_files in sorted(by_inode.items(), key=lambda kv: kv[1][0].rel_path):
        inode_files.sort(key=lambda f: (f.mtime_ns, len(f.rel_path), f.rel_path))
        unique_inodes.append(inode_files[0])
        if len(inode_files) > 1:
            sz = inode_files[0].size
            hardlink_groups.append(
                HardlinkGroup(
                    inode=ino,
                    device=dev,
                    size=sz,
                    size_human=human_size(sz),
                    note="not duplicates in storage (hard links to the same inode)",
                    paths=[str(f.path) for f in inode_files],
                )
            )

    # Stage 1: Group by file size (skip 0-byte empty files from duplicate waste)
    by_size: dict[int, list[_CandidateFile]] = {}
    for cand in unique_inodes:
        if cand.size > 0:
            by_size.setdefault(cand.size, []).append(cand)

    groups: list[DuplicateGroup] = []
    for size, same_size_files in sorted(
        by_size.items(), key=lambda kv: (-kv[0], kv[1][0].rel_path)
    ):
        if len(same_size_files) < 2:
            continue

        # Stage 2: Group by partial hash (first 64 KiB)
        by_partial: dict[str, list[_CandidateFile]] = {}
        for cand in same_size_files:
            try:
                phash = sha256_file(cand.path, limit=PARTIAL_HASH_BYTES)
            except OSError as exc:
                warnings.append(f"Cannot read {cand.path}: {exc}")
                continue
            by_partial.setdefault(phash, []).append(cand)

        # Stage 3: Group by full SHA-256
        for phash, partial_group in by_partial.items():
            if len(partial_group) < 2:
                continue
            by_full: dict[str, list[_CandidateFile]] = {}
            for cand in partial_group:
                if cand.size <= PARTIAL_HASH_BYTES:
                    fhash = phash
                else:
                    try:
                        fhash = sha256_file(cand.path)
                    except OSError as exc:
                        warnings.append(f"Cannot read {cand.path}: {exc}")
                        continue
                by_full.setdefault(fhash, []).append(cand)

            for fhash, dup_list in sorted(by_full.items(), key=lambda kv: kv[1][0].rel_path):
                if len(dup_list) < 2:
                    continue
                # Deterministic keeper selection: oldest mtime_ns, shortest rel_path, then lexical
                dup_list.sort(key=lambda f: (f.mtime_ns, len(f.rel_path), f.rel_path))
                keeper_cand = dup_list[0]
                if keeper_cand.mtime_ns < dup_list[1].mtime_ns:
                    keeper_reason = "Suggested original (oldest modification time)"
                elif len(keeper_cand.rel_path) < len(dup_list[1].rel_path):
                    keeper_reason = "Suggested original (oldest mtime and shortest path)"
                else:
                    keeper_reason = "Suggested original (oldest mtime, first alphabetically)"

                items: list[DuplicateFileItem] = []
                for idx, item_cand in enumerate(dup_list):
                    is_k = idx == 0
                    items.append(
                        DuplicateFileItem(
                            path=str(item_cand.path),
                            rel_path=item_cand.rel_path,
                            size=item_cand.size,
                            mtime=item_cand.mtime,
                            mtime_ns=item_cand.mtime_ns,
                            is_keeper=is_k,
                            reason=keeper_reason
                            if is_k
                            else f"Duplicate of {keeper_cand.rel_path}",
                        )
                    )

                wasted = size * (len(dup_list) - 1)
                groups.append(
                    DuplicateGroup(
                        sha256=fhash,
                        size=size,
                        size_human=human_size(size),
                        count=len(dup_list),
                        wasted_bytes=wasted,
                        wasted_human=human_size(wasted),
                        keeper=str(keeper_cand.path),
                        keeper_reason=keeper_reason,
                        files=items,
                    )
                )

    total_dup_files = sum(g.count - 1 for g in groups)
    total_wasted = sum(g.wasted_bytes for g in groups)

    return DuplicatesReport(
        root=str(root),
        total_scanned=len(all_files),
        duplicate_groups_count=len(groups),
        duplicate_files_count=total_dup_files,
        wasted_bytes=total_wasted,
        wasted_human=human_size(total_wasted),
        groups=groups,
        hardlinks=hardlink_groups,
        warnings=warnings,
    )


def build_duplicates_move_plan(
    root: Path,
    report: DuplicatesReport,
    move_to: Path,
    *,
    mode: str = "preview",
) -> OrganizePlan:
    """Build a journal-ready move plan for all non-keeper duplicates into ``move_to``."""
    ensure_not_protected(root, "move duplicates from")
    ensure_not_protected(move_to, "move duplicates to")

    plan = OrganizePlan(
        root=root,
        mode=mode,
        recursive=True,
        include_hidden=False,
        include_symlinks=False,
        on_collision="rename",
    )

    claimed_in_batch: set[tuple[str, str]] = set()
    dir_entries_cache: dict[Path, set[str]] = {}
    seq = 0

    for group in report.groups:
        for file_item in group.files:
            if file_item.is_keeper:
                continue
            src_path = Path(file_item.path)
            initial_dest = move_to / src_path.name
            resolved_dest, collided = resolve_collision_free_destination(
                initial_dest,
                claimed_in_batch=claimed_in_batch,
                dir_entries_cache=dir_entries_cache,
            )
            claimed_in_batch.add((str(move_to).casefold(), resolved_dest.name.casefold()))
            seq += 1
            collision_note = (
                f"Renamed to '{resolved_dest.name}' to avoid collision in review folder"
                if collided
                else None
            )
            if collided:
                plan.collision_renames += 1
            plan.items.append(
                PlannedMove(
                    seq=seq,
                    source=src_path,
                    destination=resolved_dest,
                    rel_source=file_item.rel_path,
                    rel_destination=str(resolved_dest),
                    destination_folder=str(move_to),
                    category="Duplicates",
                    matched_by="duplicate_hash",
                    rule_id=None,
                    rule_name=None,
                    reason=f"{file_item.reason} (SHA-256 {group.sha256[:12]}…)",
                    size=file_item.size,
                    mtime_ns=file_item.mtime_ns,
                    is_symlink=False,
                    action="move",
                    collision=collided,
                    collision_note=collision_note,
                )
            )

    return plan


def execute_duplicates_move(
    plan: OrganizePlan,
    journal: Journal,
    *,
    move_to: Path,
    hash_limit_for_undo: int = 50_000_000,
) -> ExecutionReport:
    """Execute a duplicates '--move-to' plan through the journaled executor."""
    return execute_organize_plan(
        plan,
        journal,
        op_type="duplicates",
        hash_limit_for_undo=hash_limit_for_undo,
        meta={"move_to": str(move_to)},
    )
