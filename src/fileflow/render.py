"""Human-readable rendering for fileflow commands using the shared Output API."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from rich.tree import Tree

from fileflow.common.fsutil import human_size
from fileflow.common.output import Output
from fileflow.duplicates import DuplicatesReport
from fileflow.executor import ExecutionReport
from fileflow.journal import OperationRecord, OperationSummary
from fileflow.planner import OrganizePlan
from fileflow.rename import RenamePlan
from fileflow.rules import FileMetadata, MatchDecision, Rule
from fileflow.scanner import FolderTreeNode, ScanReport
from fileflow.undo import UndoReport
from fileflow.watch import WatchReport


def render_scan_report(out: Output, report: ScanReport) -> None:
    """Render 'fileflow scan' output."""
    out.banner()
    out.summary(
        f"Scan Summary — {report.root}",
        [
            ("Total files", str(report.total_files)),
            ("Total size", report.total_size_human),
            ("Directories", str(report.total_dirs)),
            ("Symlinks", str(report.total_symlinks)),
            ("Top-level unorganized", str(report.unorganized_count)),
            ("Large files (>= " + report.large_threshold_human + ")", str(len(report.large_files))),
            ("Duplicate candidate groups", str(len(report.duplicate_candidates))),
        ],
    )

    if report.by_category:
        out.table(
            ["Category", "Files", "Total Size"],
            [(c.category, c.count, c.total_size_human) for c in report.by_category],
            title="By Category",
            right_align=("Files", "Total Size"),
        )

    if report.by_extension:
        out.table(
            ["Extension", "Files", "Total Size"],
            [(e.extension, e.count, e.total_size_human) for e in report.by_extension[:20]],
            title="By Extension",
            right_align=("Files", "Total Size"),
        )

    if report.large_files:
        out.table(
            ["File", "Category", "Size"],
            [(f.rel_path, f.category, f.size_human) for f in report.large_files[:20]],
            title=f"Large Files (>= {report.large_threshold_human})",
            right_align=("Size",),
        )

    if report.duplicate_candidates:
        out.table(
            ["Count", "File Size", "Wasted", "Verified", "Sample Paths"],
            [
                (
                    d.count,
                    d.size_human,
                    d.wasted_human,
                    "SHA-256" if d.verified_by_hash else "size only",
                    ", ".join(d.paths[:3]),
                )
                for d in report.duplicate_candidates[:15]
            ],
            title="Duplicate Candidates",
            right_align=("Count", "File Size", "Wasted"),
        )

    if report.unorganized_files:
        out.table(
            ["Top-Level File", "Suggested Category", "Size"],
            [(f.name, f.category, f.size_human) for f in report.unorganized_files[:25]],
            title=f"Unorganized Top-Level Files ({report.unorganized_count})",
            right_align=("Size",),
        )

    if report.folder_tree is not None:
        tree_label = (
            f"{report.folder_tree.name}/ "
            f"({report.folder_tree.file_count} files, {report.folder_tree.total_size_human})"
        )
        tree = out.tree(tree_label)
        _populate_folder_tree(tree, report.folder_tree)
        out.render(tree)

    for w in report.warnings:
        out.warn(w)

    out.next_steps(
        [
            f"fileflow organize {report.root} --preview",
            f"fileflow duplicates {report.root}",
        ]
    )


def _populate_folder_tree(parent_tree: Tree, node: FolderTreeNode) -> None:
    for child in node.children:
        branch = parent_tree.add(
            f"{child.name}/ ({child.file_count} files, {child.total_size_human})"
        )
        _populate_folder_tree(branch, child)


def render_organize_preview(out: Output, plan: OrganizePlan) -> None:
    """Render 'fileflow organize --preview' plan grouped by destination folder."""
    out.banner()
    out.summary(
        f"Organize Preview — {plan.root}",
        [
            ("Mode", "preview (no files changed)"),
            ("Planned moves", str(plan.total_moves)),
            ("Total size", human_size(plan.total_bytes)),
            ("Collision renames", str(plan.collision_renames)),
            ("Skipped collisions", str(plan.skipped_collisions)),
            ("Already organized", str(plan.skipped_already_organized)),
        ],
    )

    if not plan.items:
        out.info("Nothing to organize — folder is already clean.")
        return

    grouped = plan.grouped_by_destination()
    for dest_folder, items in grouped.items():
        rows: list[tuple[Any, ...]] = []
        for item in items:
            note = item.collision_note or ("move" if item.action == "move" else "skip")
            rows.append(
                (
                    item.rel_source,
                    item.rel_destination,
                    human_size(item.size),
                    item.reason,
                    note,
                )
            )
        out.table(
            ["Source", "Destination", "Size", "Matched Rule / Category", "Note"],
            rows,
            title=f"Destination: {dest_folder}/ ({len(items)} file(s))",
            right_align=("Size",),
        )

    for w in plan.warnings:
        out.warn(w)

    out.next_steps([f"fileflow organize {plan.root} --apply"])


def render_execution_report(out: Output, report: ExecutionReport) -> None:
    """Render execution results of organize/rename/duplicates/watch --apply."""
    out.banner()
    if report.status == "noop":
        out.info("No files needed to be moved.")
        return

    out.summary(
        f"Operation {report.operation_id} ({report.operation_type})",
        [
            ("Status", report.status),
            ("Moved / renamed", str(report.done_count)),
            ("Skipped", str(report.skipped_count)),
            ("Failed", str(report.failed_count)),
            ("Total size", human_size(report.total_bytes)),
        ],
    )

    if report.results:
        out.table(
            ["#", "Source", "Destination", "Status", "Note / Error"],
            [
                (
                    r.seq,
                    Path(r.source).name,
                    r.destination,
                    r.status,
                    r.error or r.collision_note or r.reason,
                )
                for r in report.results
            ],
            title="Processed Files",
            right_align=("#",),
        )

    if report.errors:
        for err in report.errors:
            out.warn(f"Failed {err['source']}: {err['error']}")

    if report.operation_id:
        out.next_steps(
            [
                f"fileflow history show {report.operation_id}",
                f"fileflow undo {report.operation_id}",
            ]
        )


def render_rules_list(out: Output, rules: Sequence[Rule], rules_path: Path) -> None:
    """Render 'fileflow rules list'."""
    out.heading(f"FileFlow Rules ({rules_path})")
    if not rules:
        out.info("No custom rules configured. Default category mappings are active.")
        out.next_steps(
            [
                "fileflow rules add --name 'PDF Invoices' --ext .pdf "
                "--name-glob '*invoice*' --destination 'Documents/Invoices'"
            ]
        )
        return

    rows: list[tuple[Any, ...]] = []
    for r in rules:
        conds: list[str] = []
        if r.extensions:
            conds.append(f"ext={','.join(r.extensions)}")
        if r.name_glob:
            conds.append(f"glob={r.name_glob}")
        if r.name_regex:
            conds.append(f"regex={r.name_regex}")
        if r.min_size is not None:
            conds.append(f"min={human_size(r.min_size)}")
        if r.max_size is not None:
            conds.append(f"max={human_size(r.max_size)}")
        if r.category:
            conds.append(f"cat={r.category}")
        if r.source_folder:
            conds.append(f"src={r.source_folder}")
        if r.modified_before:
            conds.append(f"mod<{r.modified_before}")
        if r.modified_after:
            conds.append(f"mod>={r.modified_after}")
        if r.created_before:
            conds.append(f"crt<{r.created_before}")
        if r.created_after:
            conds.append(f"crt>={r.created_after}")
        rows.append(
            (
                r.priority,
                r.id,
                r.name,
                "yes" if r.enabled else "no",
                "; ".join(conds) or "(all files)",
                r.destination,
            )
        )

    out.table(
        ["Priority", "ID", "Name", "Enabled", "Conditions", "Destination"],
        rows,
        right_align=("Priority",),
    )


def render_rule_explain(out: Output, meta: FileMetadata, decision: MatchDecision) -> None:
    """Render 'fileflow rules explain FILE'."""
    out.heading(f"Rule Explanation — {meta.path}")
    out.kv(
        [
            ("File", str(meta.path)),
            ("Extension", meta.extension or "(none)"),
            ("Size", f"{human_size(meta.size)} ({meta.size} B)"),
            ("Default Category", meta.category),
            ("Matched By", decision.matched_by),
            (
                "Winning Rule",
                f"{decision.rule_name} ({decision.rule_id})"
                if decision.rule_id
                else "(none — default category)",
            ),
            ("Destination Folder", decision.destination_folder),
            ("Reason", decision.reason),
        ]
    )

    if decision.evaluations:
        out.table(
            ["Priority", "Rule ID", "Name", "Matched", "Details"],
            [
                (
                    ev.priority,
                    ev.rule_id,
                    ev.rule_name,
                    "YES" if ev.matched else "no",
                    "; ".join(ev.reasons if ev.matched else ev.failed_conditions),
                )
                for ev in decision.evaluations
            ],
            title="Rule Evaluation Trace",
            right_align=("Priority",),
        )


def render_rename_preview(out: Output, plan: RenamePlan) -> None:
    """Render 'fileflow rename --preview'."""
    out.banner()
    out.summary(
        f"Batch Rename Preview — {plan.root}",
        [
            ("Mode", "preview (no files changed)"),
            ("Files matched", str(len(plan.items))),
            ("Files to rename", str(len(plan.active_items))),
            ("Collisions detected", str(len(plan.collisions))),
        ],
    )

    if not plan.items:
        out.info("No matching files found to rename.")
        return

    out.table(
        ["#", "Current Name", "New Name", "Status"],
        [
            (
                i.seq,
                i.old_name,
                i.new_name,
                i.collision_note if i.collision else ("unchanged" if i.unchanged else "ready"),
            )
            for i in plan.items
        ],
        title="Planned Renames",
        right_align=("#",),
    )

    for col in plan.collisions:
        out.warn(col)

    if not plan.has_collisions and plan.active_items:
        out.next_steps([f"fileflow rename {plan.root} ... --apply"])


def render_duplicates_report(out: Output, report: DuplicatesReport) -> None:
    """Render 'fileflow duplicates' report and optional '--move-to' preview/execution."""
    out.banner()
    out.summary(
        f"Duplicates Report — {report.root}",
        [
            ("Files scanned", str(report.total_scanned)),
            ("Duplicate groups", str(report.duplicate_groups_count)),
            ("Extra duplicate files", str(report.duplicate_files_count)),
            ("Reclaimable space", report.wasted_human),
            ("Hard-link groups (not duplicates in storage)", str(len(report.hardlinks))),
        ],
    )

    if report.hardlinks:
        out.table(
            ["Inode", "Size", "Note", "Paths"],
            [(h.inode, h.size_human, h.note, ", ".join(h.paths)) for h in report.hardlinks],
            title="Hard Links (Not Duplicates in Storage)",
            right_align=("Inode", "Size"),
        )

    if not report.groups:
        out.info("No content-identical duplicate files found.")
        return

    for idx, group in enumerate(report.groups, start=1):
        group_title = (
            f"Group {idx} — SHA-256 {group.sha256[:16]}… "
            f"({group.count} files, {group.wasted_human} wasted)"
        )
        out.table(
            ["Role", "File", "Size", "Reason"],
            [
                (
                    "KEEP (original)" if f.is_keeper else "EXTRA (duplicate)",
                    f.rel_path,
                    group.size_human,
                    f.reason,
                )
                for f in group.files
            ],
            title=group_title,
            right_align=("Size",),
        )

    if report.move_to and report.mode == "preview" and report.planned_moves:
        out.table(
            ["#", "Duplicate Source", "Review Destination", "Note"],
            [
                (
                    m.seq,
                    m.rel_source,
                    m.rel_destination,
                    m.collision_note or "ready to move",
                )
                for m in report.planned_moves
            ],
            title=f"Preview: Move Extras to {report.move_to}",
            right_align=("#",),
        )
        out.next_steps([f"fileflow duplicates {report.root} --move-to {report.move_to} --apply"])
    elif report.execution is not None:
        render_execution_report(out, report.execution)


def render_history_list(out: Output, operations: Sequence[OperationSummary]) -> None:
    """Render 'fileflow history'."""
    out.heading("Operation History")
    if not operations:
        out.info("No operations recorded in journal yet.")
        return

    out.table(
        [
            "Operation ID",
            "Time (UTC)",
            "Type",
            "Status",
            "Done",
            "Skipped",
            "Failed",
            "Undone",
            "Root",
        ],
        [
            (
                op.id,
                op.created_at,
                op.type,
                op.status,
                op.done_count,
                op.skipped_count,
                op.failed_count,
                op.undone_count,
                op.root,
            )
            for op in operations
        ],
        right_align=("Done", "Skipped", "Failed", "Undone"),
    )
    out.next_steps(
        [
            f"fileflow history show {operations[0].id}",
            f"fileflow undo {operations[0].id}",
        ]
    )


def render_history_detail(out: Output, record: OperationRecord) -> None:
    """Render 'fileflow history show OPERATION_ID'."""
    out.heading(f"Operation {record.id}")
    out.kv(
        [
            ("Operation ID", record.id),
            ("Type", record.type),
            ("Root", record.root),
            ("Created At", record.created_at),
            ("Status", record.status),
            ("Done", str(record.done_count)),
            ("Failed", str(record.failed_count)),
            ("Skipped", str(record.skipped_count)),
            ("Undone", str(record.undone_count)),
        ]
    )
    if record.items:
        out.table(
            ["#", "Kind", "Source", "Destination", "Status", "Size", "Error / Note"],
            [
                (
                    i.seq,
                    i.kind,
                    i.source,
                    i.destination,
                    i.status,
                    human_size(i.size),
                    i.error or "",
                )
                for i in record.items
            ],
            title="Operation Items",
            right_align=("#", "Size"),
        )


def render_undo_report(out: Output, report: UndoReport) -> None:
    """Render 'fileflow undo' result."""
    out.banner()
    title = (
        f"Undo Dry Run — {report.operation_id}"
        if report.dry_run
        else f"Undo Operation — {report.operation_id}"
    )
    out.summary(
        title,
        [
            ("Operation ID", report.operation_id),
            ("Status", report.status),
            ("Restored files", str(report.restored_count)),
            ("Skipped unsafe", str(report.skipped_unsafe_count)),
            ("Removed empty dirs", str(len(report.removed_dirs))),
        ],
    )
    if report.items:
        out.table(
            ["#", "Current Location", "Restored Original Path", "Status", "Reason"],
            [(i.seq, i.destination, i.source, i.status, i.reason) for i in report.items],
            title="Undo Items",
            right_align=("#",),
        )


def render_watch_report(out: Output, report: WatchReport) -> None:
    """Render 'fileflow watch' summary."""
    out.banner()
    out.summary(
        f"Watch Summary — {report.root}",
        [
            ("Mode", report.mode),
            ("Cycles completed", str(report.cycles_run)),
            ("Files planned", str(report.total_planned)),
            ("Files moved", str(report.total_moved)),
            ("Skipped unstable", str(report.total_skipped_unstable)),
            ("Skipped partial downloads", str(report.total_skipped_partial)),
        ],
    )
    if report.operation_ids:
        out.kv([("Operations recorded", ", ".join(report.operation_ids))])
