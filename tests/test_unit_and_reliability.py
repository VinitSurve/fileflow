"""Unit, reliability, security, and transactional tests for fileflow."""

from __future__ import annotations

import errno
import json
import os
from pathlib import Path

import pytest

from fileflow.categories import (
    category_matches,
    classify_extension,
    classify_path,
    is_organized_destination_dir,
)
from fileflow.common.exceptions import DataError, UsageError
from fileflow.common.paths import ToolPaths
from fileflow.duplicates import (
    build_duplicates_move_plan,
    execute_duplicates_move,
    find_duplicates,
)
from fileflow.executor import execute_organize_plan, perform_low_level_move
from fileflow.journal import Journal
from fileflow.planner import build_organize_plan
from fileflow.rename import (
    PlannedRename,
    RenamePlan,
    build_rename_plan,
    execute_rename_plan,
    validate_new_filename,
)
from fileflow.rules import (
    FileMetadata,
    Rule,
    RuleStore,
    match_file,
    validate_destination_template,
    validate_safe_regex,
)
from fileflow.undo import undo_operation
from fileflow.watch import is_partial_download, run_watch


def test_categories_classification_and_matching() -> None:
    assert classify_extension(".pdf") == "Documents/PDFs"
    assert classify_extension("DOCX") == "Documents/Word"
    assert classify_extension(".xlsx") == "Documents/Spreadsheets"
    assert classify_extension(".pptx") == "Documents/Presentations"
    assert classify_extension(".md") == "Documents/Text"
    assert classify_extension(".png") == "Images/PNG"
    assert classify_extension(".jpeg") == "Images/JPEG"
    assert classify_extension(".gif") == "Images/GIF"
    assert classify_extension(".webp") == "Images/Other"
    assert classify_extension(".mp4") == "Videos"
    assert classify_extension(".mp3") == "Audio"
    assert classify_extension(".zip") == "Archives"
    assert classify_extension(".py") == "Code"
    assert classify_extension(".unknown_xyz") == "Other"
    assert classify_extension("") == "Other"
    assert classify_path(Path("archive.tar.gz")) == "Archives"

    assert category_matches("Documents/PDFs", "Documents")
    assert category_matches("Documents/PDFs", "PDFs")
    assert category_matches("Documents/PDFs", "documents/pdfs")
    assert not category_matches("Documents/PDFs", "Images")
    assert is_organized_destination_dir(("Documents", "PDFs"))
    assert is_organized_destination_dir(("CustomDest",), {"CustomDest"})
    assert not is_organized_destination_dir(("unorganized_sub",))


def test_rule_validation_rejects_traversal_and_absolute_paths() -> None:
    with pytest.raises(UsageError, match="relative path"):
        validate_destination_template("/etc/passwd")
    with pytest.raises(UsageError, match="relative path"):
        validate_destination_template("~/Documents/Out")
    with pytest.raises(UsageError, match="path traversal"):
        validate_destination_template("../outside")
    with pytest.raises(UsageError, match="path traversal"):
        validate_destination_template("Documents/../../outside")
    with pytest.raises(UsageError, match="Unknown placeholder"):
        validate_destination_template("Documents/{secret_env}")
    assert validate_destination_template("Documents/{year}/{month}") == "Documents/{year}/{month}"


def test_rule_regex_redos_guard() -> None:
    # Valid regexes pass
    compiled = validate_safe_regex(r"^invoice_[0-9]{4}\.pdf$")
    assert compiled.search("INVOICE_2026.PDF")

    # Nested quantifiers are rejected
    for dangerous in ("(a+)+", "(.*)*", "([a-z]+)+", "((ab)+)*", "([0-9]+){2,5}"):
        with pytest.raises(UsageError, match="nested quantifiers"):
            validate_safe_regex(dangerous)

    # Length limit
    with pytest.raises(UsageError, match="maximum length"):
        validate_safe_regex("a" * 300)

    # Invalid syntax
    with pytest.raises(UsageError, match="Invalid regular expression"):
        validate_safe_regex("[unclosed")


def test_rules_matching_and_priority_order(isolated_env: Path, tmp_path: Path) -> None:
    workspace = tmp_path / "work"
    workspace.mkdir()
    pdf_file = workspace / "invoice_2026.pdf"
    pdf_file.write_bytes(b"%PDF-1.4 content" * 100)

    r_low = Rule(
        id="general-pdf",
        name="All PDFs",
        priority=50,
        extensions=[".pdf"],
        destination="Documents/AllPDFs",
    ).validate()
    r_high = Rule(
        id="invoice-pdf",
        name="Invoices",
        priority=10,
        extensions=["pdf"],
        name_glob="*invoice*",
        min_size=100,
        max_size=1_000_000,
        category="Documents",
        source_folder=".",
        destination="Documents/Invoices/{ext}",
    ).validate()

    meta = FileMetadata.from_path(pdf_file, scan_root=workspace)
    decision = match_file(meta, [r_low, r_high], include_evaluations=True)
    assert decision.matched_by == "rule"
    assert decision.rule_id == "invoice-pdf"
    assert decision.destination_folder == "Documents/Invoices/pdf"
    assert len(decision.evaluations) == 2


def test_example_rules_file_loads_cleanly(isolated_env: Path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    example_path = repo_root / "examples" / "fileflow_rules.json"
    assert example_path.exists()
    raw = json.loads(example_path.read_text(encoding="utf-8"))
    rules = [Rule.from_dict(item) for item in raw["rules"]]
    assert len(rules) >= 4
    paths = ToolPaths.for_tool("fileflow")
    store = RuleStore(paths)
    store.save(rules)
    loaded = store.load()
    assert [r.id for r in loaded] == [r.id for r in sorted(rules, key=lambda x: (x.priority, x.id))]


def test_cross_device_exdev_move_and_verification(
    isolated_env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "exdev_test"
    workspace.mkdir()
    src_file = workspace / "report.pdf"
    payload = b"important binary payload for cross-device move\n" * 50
    src_file.write_bytes(payload)
    orig_stat = src_file.stat()

    link_target = workspace / "target.txt"
    link_target.write_text("symlink target", encoding="utf-8")
    sym_file = workspace / "shortcut.txt"
    sym_file.symlink_to("target.txt")

    plan = build_organize_plan(workspace, include_symlinks=True, mode="apply")
    assert plan.total_moves == 3

    def _raise_exdev(src: os.PathLike[str] | str, dst: os.PathLike[str] | str) -> None:
        raise OSError(errno.EXDEV, "Cross-device link")

    monkeypatch.setattr(os, "rename", _raise_exdev)

    journal = Journal(ToolPaths.for_tool("fileflow"))
    exec_report = execute_organize_plan(plan, journal)
    assert exec_report.status == "applied"
    assert exec_report.done_count == 3

    moved_pdf = workspace / "Documents" / "PDFs" / "report.pdf"
    assert moved_pdf.exists()
    assert not src_file.exists()
    assert moved_pdf.read_bytes() == payload
    # Metadata (mtime) preserved via copy2
    assert abs(moved_pdf.stat().st_mtime - orig_stat.st_mtime) < 1e-3

    moved_sym = workspace / "Documents" / "Text" / "shortcut.txt"
    assert moved_sym.is_symlink()
    assert os.readlink(moved_sym) == "target.txt"

    # Undo also works across EXDEV
    undo_rep = undo_operation(journal, exec_report.operation_id)
    assert undo_rep.status == "undone"
    assert src_file.exists() and src_file.read_bytes() == payload
    assert sym_file.is_symlink() and os.readlink(sym_file) == "target.txt"


def test_cross_device_exdev_verification_failure_keeps_source(
    isolated_env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "source.bin"
    dst = tmp_path / "dest.bin"
    src.write_bytes(b"authentic content")

    def _raise_exdev(s: object, d: object) -> None:
        raise OSError(errno.EXDEV, "Cross-device link")

    def _corrupt_copy2(s: object, d: object, *, follow_symlinks: bool = True) -> object:
        Path(str(d)).write_bytes(b"corrupted content!!")
        return d

    monkeypatch.setattr(os, "rename", _raise_exdev)
    monkeypatch.setattr("shutil.copy2", _corrupt_copy2)

    with pytest.raises(OSError, match=r"mismatch|verification failed"):
        perform_low_level_move(src, dst, is_symlink=False)

    # Source must remain intact and corrupted destination copy must be cleaned up
    assert src.exists()
    assert src.read_bytes() == b"authentic content"
    assert not dst.exists()


def test_interrupted_mid_batch_leaves_accurate_journal_and_undo_works(
    isolated_env: Path, tmp_path: Path
) -> None:
    workspace = tmp_path / "interrupt_ws"
    workspace.mkdir()
    f1 = workspace / "a_first.pdf"
    f2 = workspace / "b_second.png"
    f3 = workspace / "c_third.zip"
    f1.write_bytes(b"pdf-1")
    f2.write_bytes(b"png-2")
    f3.write_bytes(b"zip-3")

    plan = build_organize_plan(workspace, mode="apply")
    assert plan.total_moves == 3

    journal = Journal(ToolPaths.for_tool("fileflow"))

    def _interrupt_on_second(seq: int) -> None:
        if seq == 2:
            raise KeyboardInterrupt("Simulated user Ctrl-C after first file")

    with pytest.raises(KeyboardInterrupt):
        execute_organize_plan(plan, journal, before_move_hook=_interrupt_on_second)

    # Verify journal state
    ops = journal.list_operations()
    assert len(ops) == 1
    op_summary = ops[0]
    assert op_summary.status == "interrupted"
    assert op_summary.done_count == 1

    record = journal.get_operation(op_summary.id)
    statuses = {Path(i.source).name: i.status for i in record.items}
    assert statuses["a_first.pdf"] == "done"
    assert statuses["b_second.png"] == "planned"
    assert statuses["c_third.zip"] == "planned"

    # First file was moved; second and third are still in root
    assert (workspace / "Documents" / "PDFs" / "a_first.pdf").exists()
    assert not f1.exists()
    assert f2.exists()
    assert f3.exists()

    # Undo reverses the interrupted operation cleanly
    undo_rep = undo_operation(journal, op_summary.id)
    assert undo_rep.status == "undone"
    assert undo_rep.restored_count == 1
    assert f1.exists() and f1.read_bytes() == b"pdf-1"
    assert not (workspace / "Documents").exists()


def test_rename_swap_and_cycle_and_undo(isolated_env: Path, tmp_path: Path) -> None:
    workspace = tmp_path / "swap_ws"
    workspace.mkdir()
    fa = workspace / "a.txt"
    fb = workspace / "b.txt"
    fa.write_text("content-of-A", encoding="utf-8")
    fb.write_text("content-of-B", encoding="utf-8")
    st_a = fa.stat()
    st_b = fb.stat()

    # Construct an explicit 2-way swap plan: a.txt <-> b.txt
    swap_plan = RenamePlan(
        root=workspace,
        mode="apply",
        pattern=None,
        prefix="",
        suffix="",
        start=1,
        width=1,
        normalize_ext=False,
        glob_filter=None,
        date_source="mtime",
        items=[
            PlannedRename(
                seq=1,
                source=fa,
                destination=fb,
                old_name="a.txt",
                new_name="b.txt",
                size=st_a.st_size,
                mtime_ns=st_a.st_mtime_ns,
            ),
            PlannedRename(
                seq=2,
                source=fb,
                destination=fa,
                old_name="b.txt",
                new_name="a.txt",
                size=st_b.st_size,
                mtime_ns=st_b.st_mtime_ns,
            ),
        ],
    )

    journal = Journal(ToolPaths.for_tool("fileflow"))
    rep = execute_rename_plan(swap_plan, journal)
    assert rep.status == "applied"
    assert rep.done_count == 2
    assert fa.read_text(encoding="utf-8") == "content-of-B"
    assert fb.read_text(encoding="utf-8") == "content-of-A"

    # Undo reverses the swap without falsely complaining that a.txt/b.txt are occupied
    undo_rep = undo_operation(journal, rep.operation_id)
    assert undo_rep.status == "undone"
    assert undo_rep.restored_count == 2
    assert fa.read_text(encoding="utf-8") == "content-of-A"
    assert fb.read_text(encoding="utf-8") == "content-of-B"


def test_rename_shift_chain_and_validation(isolated_env: Path, tmp_path: Path) -> None:
    workspace = tmp_path / "chain_ws"
    workspace.mkdir()
    (workspace / "001.txt").write_text("first", encoding="utf-8")
    (workspace / "002.txt").write_text("second", encoding="utf-8")

    # Renaming 001.txt -> 002.txt and 002.txt -> 003.txt (chain where 002.txt is in the batch)
    plan = build_rename_plan(
        workspace,
        pattern="{n}{ext}",
        start=2,
        width=3,
        mode="apply",
    )
    assert not plan.has_collisions
    journal = Journal(ToolPaths.for_tool("fileflow"))
    rep = execute_rename_plan(plan, journal)
    assert rep.status == "applied"
    assert not (workspace / "001.txt").exists()
    assert (workspace / "002.txt").read_text(encoding="utf-8") == "first"
    assert (workspace / "003.txt").read_text(encoding="utf-8") == "second"

    # Undo the chain
    undo_operation(journal, rep.operation_id)
    assert (workspace / "001.txt").read_text(encoding="utf-8") == "first"
    assert (workspace / "002.txt").read_text(encoding="utf-8") == "second"

    # Filename validation
    with pytest.raises(UsageError):
        validate_new_filename("..")
    with pytest.raises(UsageError):
        validate_new_filename("bad/name.txt")
    with pytest.raises(UsageError):
        validate_new_filename("a" * 260 + ".txt")


def test_corrupt_journal_raises_data_error(isolated_env: Path) -> None:
    paths = ToolPaths.for_tool("fileflow")
    ToolPaths.ensure(paths.data_dir)
    (paths.data_dir / "journal.db").write_bytes(b"not a valid sqlite database header")
    journal = Journal(paths)
    with pytest.raises(DataError, match="corrupt"):
        journal.list_operations()


def test_watch_skips_partial_downloads_and_unstable_files(
    isolated_env: Path, tmp_path: Path
) -> None:
    workspace = tmp_path / "watch_ws"
    workspace.mkdir()
    stable = workspace / "notes.txt"
    stable.write_text("stable content", encoding="utf-8")
    partial = workspace / "video.mp4.crdownload"
    partial.write_bytes(b"downloading...")
    growing = workspace / "growing.pdf"
    growing.write_bytes(b"chunk1")

    assert is_partial_download(partial)
    assert is_partial_download(Path("archive.zip.part"))
    assert not is_partial_download(stable)

    def _mutate_growing(cycle: int) -> None:
        if cycle == 1:
            # Modify growing.pdf between snapshot 1 and snapshot 2 so it is detected as unstable
            growing.write_bytes(b"chunk1 + chunk2")

    journal = Journal(ToolPaths.for_tool("fileflow"))
    rep = run_watch(
        workspace,
        journal=journal,
        interval=0.01,
        apply=True,
        once=True,
        between_snapshots_hook=_mutate_growing,
        sleep_fn=lambda _: None,
    )
    assert rep.cycles_run == 1
    assert rep.total_moved == 1
    assert rep.total_skipped_partial == 1
    assert rep.total_skipped_unstable == 1
    assert (workspace / "Documents" / "Text" / "notes.txt").exists()
    assert partial.exists()
    assert growing.exists()


def test_duplicates_hardlinks_and_move_to(isolated_env: Path, tmp_path: Path) -> None:
    workspace = tmp_path / "dups_ws"
    workspace.mkdir()
    orig = workspace / "a_orig.txt"
    dup = workspace / "b_copy.txt"
    hardlink = workspace / "c_hardlink.txt"

    payload = b"duplicate content payload\n" * 20
    orig.write_bytes(payload)
    dup.write_bytes(payload)
    # Ensure orig has older mtime than dup
    os.utime(orig, (1_700_000_000, 1_700_000_000))
    os.utime(dup, (1_700_000_100, 1_700_000_100))
    os.link(orig, hardlink)

    rep = find_duplicates(workspace)
    assert len(rep.hardlinks) == 1
    assert "not duplicates in storage" in rep.hardlinks[0].note
    assert rep.duplicate_groups_count == 1
    assert rep.duplicate_files_count == 1
    assert rep.groups[0].keeper == str(orig)

    review_dir = tmp_path / "review_dups"
    plan = build_duplicates_move_plan(workspace, rep, review_dir, mode="apply")
    assert plan.total_moves == 1
    journal = Journal(ToolPaths.for_tool("fileflow"))
    exec_rep = execute_duplicates_move(plan, journal, move_to=review_dir)
    assert exec_rep.status == "applied"
    assert orig.exists()
    assert not dup.exists()
    assert (review_dir / "b_copy.txt").exists()

    # Undo restores the duplicate back to workspace
    undo_operation(journal, exec_rep.operation_id)
    assert dup.exists() and dup.read_bytes() == payload
