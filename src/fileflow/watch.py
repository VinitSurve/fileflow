"""Foreground polling watcher ('fileflow watch') with stability checks and loop prevention."""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from fileflow.common.paths import ensure_not_protected, require_directory
from fileflow.config import DEFAULT_IGNORE_NAMES
from fileflow.executor import ExecutionReport, execute_organize_plan
from fileflow.journal import Journal
from fileflow.planner import (
    CollisionPolicy,
    OrganizePlan,
    PlannedMove,
    build_organize_plan,
)
from fileflow.rules import Rule

PARTIAL_DOWNLOAD_SUFFIXES: tuple[str, ...] = (
    ".crdownload",
    ".part",
    ".download",
    ".tmp",
    ".partial",
    ".opdownload",
)


def is_partial_download(path: Path) -> bool:
    """True if ``path`` looks like an in-progress download or temporary file."""
    name_lower = path.name.lower()
    if name_lower.endswith("~") or name_lower.startswith(".fileflow_"):
        return True
    return any(name_lower.endswith(suffix) for suffix in PARTIAL_DOWNLOAD_SUFFIXES)


@dataclass
class WatchCycleResult:
    """Summary of one polling cycle in 'fileflow watch'."""

    cycle: int
    planned_count: int
    moved_count: int
    skipped_unstable: int
    skipped_partial_downloads: int
    operation_id: str | None = None
    items: list[PlannedMove] = field(default_factory=list)


@dataclass
class WatchReport:
    """Summary of a 'fileflow watch' session."""

    root: str
    mode: str  # "preview" or "apply"
    interval: float
    cycles_run: int
    total_planned: int
    total_moved: int
    total_skipped_unstable: int
    total_skipped_partial: int
    operation_ids: list[str] = field(default_factory=list)
    cycles: list[WatchCycleResult] = field(default_factory=list)


def _snapshot_candidate_states(
    plan: OrganizePlan,
) -> dict[str, tuple[int, int]]:
    return {
        str(item.source): (item.size, item.mtime_ns) for item in plan.items if item.action == "move"
    }


def run_watch(
    root: Path,
    *,
    journal: Journal,
    rules: Sequence[Rule] = (),
    interval: float = 5.0,
    apply: bool = False,
    once: bool = False,
    max_cycles: int | None = None,
    recursive: bool = False,
    include_hidden: bool = False,
    include_symlinks: bool = False,
    on_collision: CollisionPolicy = "rename",
    ignore_names: Sequence[str] = DEFAULT_IGNORE_NAMES,
    hash_limit_for_undo: int = 50_000_000,
    sleep_fn: Callable[[float], None] = time.sleep,
    between_snapshots_hook: Callable[[int], None] | None = None,
) -> WatchReport:
    """Run the foreground polling watcher until ``max_cycles`` or ``KeyboardInterrupt``."""
    require_directory(root, what="Watched folder")
    ensure_not_protected(root, "watch")

    target_cycles = 1 if once else max_cycles
    mode = "apply" if apply else "preview"
    report = WatchReport(
        root=str(root),
        mode=mode,
        interval=interval,
        cycles_run=0,
        total_planned=0,
        total_moved=0,
        total_skipped_unstable=0,
        total_skipped_partial=0,
    )

    processed: set[tuple[str, int, int]] = set()
    moved_destinations: set[str] = set()
    prev_snapshot: dict[str, tuple[int, int]] | None = None

    cycle = 0
    try:
        while target_cycles is None or cycle < target_cycles:
            cycle += 1
            if prev_snapshot is None:
                # Take first stability snapshot
                first_plan = build_organize_plan(
                    root,
                    rules=rules,
                    recursive=recursive,
                    include_hidden=include_hidden,
                    include_symlinks=include_symlinks,
                    on_collision=on_collision,
                    ignore_names=ignore_names,
                    mode=mode,
                )
                prev_snapshot = _snapshot_candidate_states(first_plan)
                if between_snapshots_hook is not None:
                    between_snapshots_hook(cycle)
                else:
                    sleep_fn(min(interval, 0.05))

            raw_plan = build_organize_plan(
                root,
                rules=rules,
                recursive=recursive,
                include_hidden=include_hidden,
                include_symlinks=include_symlinks,
                on_collision=on_collision,
                ignore_names=ignore_names,
                mode=mode,
            )
            curr_snapshot = _snapshot_candidate_states(raw_plan)

            stable_items: list[PlannedMove] = []
            skipped_unstable = 0
            skipped_partial = 0

            for item in raw_plan.items:
                if item.action != "move":
                    continue
                src_str = str(item.source)
                if src_str in moved_destinations:
                    continue
                if is_partial_download(item.source):
                    skipped_partial += 1
                    continue
                sig = (src_str, item.size, item.mtime_ns)
                if sig in processed:
                    continue
                prev_state = prev_snapshot.get(src_str)
                if prev_state is None or prev_state != (item.size, item.mtime_ns):
                    skipped_unstable += 1
                    continue
                stable_items.append(item)

            prev_snapshot = curr_snapshot

            op_id: str | None = None
            moved_in_cycle = 0

            if stable_items:
                # Renumber sequence cleanly for the cycle's filtered plan
                for idx, it in enumerate(stable_items, start=1):
                    it.seq = idx
                filtered_plan = OrganizePlan(
                    root=root,
                    mode=mode,
                    recursive=recursive,
                    include_hidden=include_hidden,
                    include_symlinks=include_symlinks,
                    on_collision=on_collision,
                    items=stable_items,
                )

                if apply:
                    exec_report: ExecutionReport = execute_organize_plan(
                        filtered_plan,
                        journal,
                        op_type="watch",
                        hash_limit_for_undo=hash_limit_for_undo,
                        meta={"cycle": cycle, "interval": interval},
                    )
                    op_id = exec_report.operation_id or None
                    if op_id:
                        report.operation_ids.append(op_id)
                    moved_in_cycle = exec_report.done_count
                    for res in exec_report.results:
                        if res.status == "done":
                            moved_destinations.add(res.destination)
                else:
                    for it in stable_items:
                        processed.add((str(it.source), it.size, it.mtime_ns))

            report.cycles_run = cycle
            report.total_planned += len(stable_items)
            report.total_moved += moved_in_cycle
            report.total_skipped_unstable += skipped_unstable
            report.total_skipped_partial += skipped_partial
            report.cycles.append(
                WatchCycleResult(
                    cycle=cycle,
                    planned_count=len(stable_items),
                    moved_count=moved_in_cycle,
                    skipped_unstable=skipped_unstable,
                    skipped_partial_downloads=skipped_partial,
                    operation_id=op_id,
                    items=stable_items,
                )
            )

            if target_cycles is not None and cycle >= target_cycles:
                break
            sleep_fn(interval)
    except KeyboardInterrupt:
        # Foreground watch stops cleanly on Ctrl-C with summary (exit 0)
        pass

    return report
