"""Pure, deterministic plan builder for 'fileflow organize'."""

from __future__ import annotations

import os
import stat
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from fileflow.categories import is_organized_destination_dir
from fileflow.common.paths import ensure_not_protected, require_directory
from fileflow.config import DEFAULT_IGNORE_NAMES
from fileflow.rules import FileMetadata, Rule, match_file

CollisionPolicy = Literal["rename", "skip"]


@dataclass
class PlannedMove:
    """Single planned file move or skip decision."""

    seq: int
    source: Path
    destination: Path
    rel_source: str
    rel_destination: str
    destination_folder: str
    category: str
    matched_by: str  # "rule" or "default_category"
    rule_id: str | None
    rule_name: str | None
    reason: str
    size: int
    mtime_ns: int
    is_symlink: bool = False
    action: str = "move"  # "move" or "skip"
    collision: bool = False
    collision_note: str | None = None


@dataclass
class OrganizePlan:
    """Complete preview/execution plan for 'fileflow organize'."""

    root: Path
    mode: str  # "preview" or "apply"
    recursive: bool
    include_hidden: bool
    include_symlinks: bool
    on_collision: CollisionPolicy
    items: list[PlannedMove] = field(default_factory=list)
    skipped_hidden: int = 0
    skipped_symlinks: int = 0
    skipped_special: int = 0
    skipped_already_organized: int = 0
    skipped_collisions: int = 0
    collision_renames: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def move_items(self) -> list[PlannedMove]:
        return [item for item in self.items if item.action == "move"]

    @property
    def total_moves(self) -> int:
        return sum(1 for item in self.items if item.action == "move")

    @property
    def total_bytes(self) -> int:
        return sum(item.size for item in self.items if item.action == "move")

    def grouped_by_destination(self) -> dict[str, list[PlannedMove]]:
        grouped: dict[str, list[PlannedMove]] = {}
        for item in self.items:
            grouped.setdefault(item.destination_folder, []).append(item)
        return dict(sorted(grouped.items(), key=lambda kv: kv[0]))


def _static_rule_roots(rules: Sequence[Rule]) -> set[str]:
    roots: set[str] = set()
    for rule in rules:
        if not rule.enabled:
            continue
        first = rule.destination.split("/", 1)[0].strip()
        if first and "{" not in first and "}" not in first:
            roots.add(first)
    return roots


def _collect_candidates(
    root: Path,
    *,
    recursive: bool,
    include_hidden: bool,
    include_symlinks: bool,
    ignore_names: set[str],
    extra_dest_roots: set[str],
    plan: OrganizePlan,
) -> list[tuple[Path, os.stat_result, bool]]:
    candidates: list[tuple[Path, os.stat_result, bool]] = []

    def _scan_dir(current: Path, rel_parts: tuple[str, ...]) -> None:
        try:
            entries = sorted(os.scandir(current), key=lambda e: e.name)
        except PermissionError:
            plan.warnings.append(f"Permission denied scanning directory: {current}")
            return
        except OSError as exc:
            plan.warnings.append(f"Cannot scan directory {current}: {exc}")
            return

        subdirs: list[tuple[Path, tuple[str, ...]]] = []
        for entry in entries:
            name = entry.name
            if not include_hidden and (name.startswith(".") or name in ignore_names):
                plan.skipped_hidden += 1
                continue

            entry_path = current / name
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError as exc:
                plan.warnings.append(f"Cannot stat {entry_path}: {exc}")
                continue

            mode = st.st_mode
            if stat.S_ISLNK(mode):
                if not include_symlinks:
                    plan.skipped_symlinks += 1
                    continue
                candidates.append((entry_path, st, True))
                continue

            if stat.S_ISDIR(mode):
                if recursive:
                    next_parts = (*rel_parts, name)
                    if is_organized_destination_dir(next_parts, extra_dest_roots):
                        plan.skipped_already_organized += 1
                        continue
                    subdirs.append((entry_path, next_parts))
                continue

            if stat.S_ISREG(mode):
                candidates.append((entry_path, st, False))
                continue

            plan.skipped_special += 1

        for subdir_path, subdir_parts in subdirs:
            _scan_dir(subdir_path, subdir_parts)

    _scan_dir(root, ())
    candidates.sort(key=lambda item: str(item[0].relative_to(root)))
    return candidates


def _existing_names_casefold(directory: Path, cache: dict[Path, set[str]]) -> set[str]:
    if directory in cache:
        return cache[directory]
    names: set[str] = set()
    if directory.exists() and directory.is_dir():
        try:
            for entry in os.scandir(directory):
                names.add(entry.name.casefold())
        except OSError:
            pass
    cache[directory] = names
    return names


def resolve_collision_free_destination(
    target: Path,
    *,
    claimed_in_batch: set[tuple[str, str]],
    dir_entries_cache: dict[Path, set[str]],
) -> tuple[Path, bool]:
    """Return ``(final_path, collided)`` checking disk and batch claims case-insensitively."""
    parent = target.parent
    parent_key = str(parent).casefold()
    existing_cf = _existing_names_casefold(parent, dir_entries_cache)

    def _is_taken(candidate: Path) -> bool:
        cand_cf = candidate.name.casefold()
        if (parent_key, cand_cf) in claimed_in_batch:
            return True
        if cand_cf in existing_cf:
            return True
        return os.path.lexists(candidate)

    if not _is_taken(target):
        return target, False

    stem = target.stem
    suffix = target.suffix
    counter = 1
    while True:
        candidate = parent / f"{stem} ({counter}){suffix}"
        if not _is_taken(candidate):
            return candidate, True
        counter += 1


def build_organize_plan(
    root: Path,
    *,
    rules: Sequence[Rule] = (),
    recursive: bool = False,
    include_hidden: bool = False,
    include_symlinks: bool = False,
    on_collision: CollisionPolicy = "rename",
    ignore_names: Sequence[str] = DEFAULT_IGNORE_NAMES,
    mode: str = "preview",
    now: float | None = None,
) -> OrganizePlan:
    """Build a deterministic organization plan without modifying the filesystem."""
    require_directory(root, what="Target folder")
    ensure_not_protected(root, "organize")

    plan = OrganizePlan(
        root=root,
        mode=mode,
        recursive=recursive,
        include_hidden=include_hidden,
        include_symlinks=include_symlinks,
        on_collision=on_collision,
    )

    ignore_set = set(ignore_names)
    extra_roots = _static_rule_roots(rules)
    candidates = _collect_candidates(
        root,
        recursive=recursive,
        include_hidden=include_hidden,
        include_symlinks=include_symlinks,
        ignore_names=ignore_set,
        extra_dest_roots=extra_roots,
        plan=plan,
    )

    claimed_in_batch: set[tuple[str, str]] = set()
    dir_entries_cache: dict[Path, set[str]] = {}
    seq = 0

    for src_path, st, is_symlink in candidates:
        ctime = getattr(st, "st_birthtime", st.st_ctime)
        try:
            rel_parent = src_path.parent.relative_to(root)
            src_folder = "." if str(rel_parent) == "." else rel_parent.as_posix()
        except ValueError:
            src_folder = "."

        from fileflow.categories import classify_path, normalize_extension

        meta = FileMetadata(
            path=src_path,
            name=src_path.name,
            extension=normalize_extension(src_path.suffix),
            size=st.st_size,
            mtime=st.st_mtime,
            ctime=ctime,
            category=classify_path(src_path),
            source_folder=src_folder,
        )
        decision = match_file(meta, rules, now=now)
        dest_dir = Path(os.path.abspath(root / decision.destination_folder))

        # Safety check: destination directory must stay strictly within root
        root_abs = Path(os.path.abspath(root))
        if dest_dir != root_abs and root_abs not in dest_dir.parents:
            plan.warnings.append(
                f"Skipped {src_path.name}: destination {decision.destination_folder!r} escapes root"
            )
            continue

        initial_dest = dest_dir / src_path.name
        if Path(os.path.abspath(initial_dest)) == Path(os.path.abspath(src_path)):
            plan.skipped_already_organized += 1
            continue

        resolved_dest, collided = resolve_collision_free_destination(
            initial_dest,
            claimed_in_batch=claimed_in_batch,
            dir_entries_cache=dir_entries_cache,
        )

        seq += 1
        rel_src = src_path.relative_to(root).as_posix()

        if collided and on_collision == "skip":
            plan.skipped_collisions += 1
            rel_dest = initial_dest.relative_to(root).as_posix()
            plan.items.append(
                PlannedMove(
                    seq=seq,
                    source=src_path,
                    destination=initial_dest,
                    rel_source=rel_src,
                    rel_destination=rel_dest,
                    destination_folder=decision.destination_folder,
                    category=decision.category,
                    matched_by=decision.matched_by,
                    rule_id=decision.rule_id,
                    rule_name=decision.rule_name,
                    reason=decision.reason,
                    size=st.st_size,
                    mtime_ns=st.st_mtime_ns,
                    is_symlink=is_symlink,
                    action="skip",
                    collision=True,
                    collision_note=(
                        f"Collision with '{initial_dest.name}'; skipped (--on-collision=skip)"
                    ),
                )
            )
            continue

        collision_note: str | None = None
        if collided:
            plan.collision_renames += 1
            collision_note = (
                f"Renamed from '{initial_dest.name}' to '{resolved_dest.name}' to avoid collision"
            )

        claimed_in_batch.add((str(dest_dir).casefold(), resolved_dest.name.casefold()))
        rel_dest = resolved_dest.relative_to(root).as_posix()
        plan.items.append(
            PlannedMove(
                seq=seq,
                source=src_path,
                destination=resolved_dest,
                rel_source=rel_src,
                rel_destination=rel_dest,
                destination_folder=decision.destination_folder,
                category=decision.category,
                matched_by=decision.matched_by,
                rule_id=decision.rule_id,
                rule_name=decision.rule_name,
                reason=decision.reason,
                size=st.st_size,
                mtime_ns=st.st_mtime_ns,
                is_symlink=is_symlink,
                action="move",
                collision=collided,
                collision_note=collision_note,
            )
        )

    return plan
