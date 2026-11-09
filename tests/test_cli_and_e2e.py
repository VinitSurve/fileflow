"""CLI functional, subprocess E2E roundtrip, and negative/security tests for fileflow."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fileflow.common.exceptions import ExitCode


def test_help_and_version(run: Callable[..., Any]) -> None:
    res = run("fileflow", "--version")
    assert res.exit_code == 0
    assert res.stdout.startswith("fileflow ")

    res = run("fileflow", "--help")
    assert res.exit_code == 0
    for cmd in (
        "scan",
        "organize",
        "rules",
        "rename",
        "duplicates",
        "history",
        "undo",
        "watch",
        "config",
    ):
        assert cmd in res.stdout


def test_scan_command_human_and_json(run: Callable[..., Any], tmp_path: Path) -> None:
    folder = tmp_path / "scan_target"
    folder.mkdir()
    (folder / "doc1.pdf").write_bytes(b"a" * 500)
    (folder / "doc2_dup.pdf").write_bytes(b"a" * 500)
    (folder / "big.zip").write_bytes(b"z" * 4096)
    sub = folder / "nested"
    sub.mkdir()
    (sub / "script.py").write_text("print('hi')\n", encoding="utf-8")

    # Human output
    res = run("fileflow", "scan", str(folder), "--large-threshold", "2KB")
    assert res.exit_code == 0
    assert "Documents/PDFs" in res.stdout
    assert "big.zip" in res.stdout

    # JSON output
    res_json = run("fileflow", "scan", str(folder), "--large-threshold", "2KB", "--json")
    assert res_json.exit_code == 0
    data = res_json.json()
    assert data["total_files"] == 4
    assert data["unorganized_count"] == 3
    assert len(data["large_files"]) == 1
    assert data["large_files"][0]["name"] == "big.zip"
    assert len(data["duplicate_candidates"]) == 1
    assert data["duplicate_candidates"][0]["verified_by_hash"] is True


def test_organize_preview_is_default_and_non_interactive_apply_requires_yes(
    run: Callable[..., Any], tmp_path: Path
) -> None:
    folder = tmp_path / "downloads"
    folder.mkdir()
    f = folder / "invoice.pdf"
    f.write_bytes(b"pdf-bytes")

    # Default is preview — file does not move
    res = run("fileflow", "organize", str(folder))
    assert res.exit_code == 0
    assert f.exists()
    assert not (folder / "Documents").exists()

    # --apply without --yes in non-interactive mode is refused (ExitCode.REFUSED = 3)
    res_refused = run("fileflow", "organize", str(folder), "--apply")
    assert res_refused.exit_code == int(ExitCode.REFUSED)
    assert f.exists()

    # --apply --yes executes and records in journal
    res_apply = run("fileflow", "organize", str(folder), "--apply", "--yes", "--json")
    assert res_apply.exit_code == 0
    payload = res_apply.json()
    assert payload["status"] == "applied"
    assert payload["done_count"] == 1
    assert not f.exists()
    assert (folder / "Documents" / "PDFs" / "invoice.pdf").exists()

    # Idempotency: second organize plans 0 moves
    res_second = run("fileflow", "organize", str(folder), "--preview", "--json")
    assert res_second.exit_code == 0
    assert res_second.json()["items"] == []


def test_e2e_subprocess_organize_history_undo_roundtrip(
    run_proc: Callable[..., Any], tmp_path: Path
) -> None:
    folder = tmp_path / "e2e_folder"
    folder.mkdir()
    files = {
        "manual.pdf": b"%PDF-1.7 manual bytes",
        "photo.png": b"\x89PNG\r\n\x1a\nphoto",
        "Café résumé — 2026 (final).txt": "Unicode text content ☕\n".encode(),
    }
    for name, content in files.items():
        (folder / name).write_bytes(content)

    # 1. Organize via real subprocess
    apply_res = run_proc("fileflow", "organize", str(folder), "--apply", "--yes", "--json")
    assert apply_res.exit_code == 0, apply_res.stderr
    apply_data = apply_res.json()
    op_id = apply_data["operation_id"]
    assert apply_data["done_count"] == 3
    assert (folder / "Documents" / "PDFs" / "manual.pdf").exists()
    assert (folder / "Images" / "PNG" / "photo.png").exists()
    assert (folder / "Documents" / "Text" / "Café résumé — 2026 (final).txt").exists()

    # 2. Inspect history list and history show via real subprocess
    hist_res = run_proc("fileflow", "history", "list", "--json")
    assert hist_res.exit_code == 0
    hist_data = hist_res.json()
    assert hist_data["count"] == 1
    assert hist_data["operations"][0]["id"] == op_id
    assert hist_data["operations"][0]["status"] == "applied"

    show_res = run_proc("fileflow", "history", "show", op_id, "--json")
    assert show_res.exit_code == 0
    assert len(show_res.json()["items"]) == 3

    # 3. Undo dry-run first
    dry_res = run_proc("fileflow", "undo", op_id, "--dry-run", "--json")
    assert dry_res.exit_code == 0
    assert dry_res.json()["status"] == "dry_run"
    assert (folder / "Documents" / "PDFs" / "manual.pdf").exists()

    # 4. Real undo via subprocess restores every file and removes empty created dirs
    undo_res = run_proc("fileflow", "undo", op_id, "--json")
    assert undo_res.exit_code == 0, undo_res.stderr
    undo_data = undo_res.json()
    assert undo_data["status"] == "undone"
    assert undo_data["restored_count"] == 3

    for name, content in files.items():
        restored = folder / name
        assert restored.exists()
        assert restored.read_bytes() == content
    assert not (folder / "Documents").exists()
    assert not (folder / "Images").exists()

    # 5. Double undo is refused (ExitCode.REFUSED = 3)
    double_res = run_proc("fileflow", "undo", op_id)
    assert double_res.exit_code == int(ExitCode.REFUSED)


def test_pre_move_collision_detection_and_case_insensitive_collisions(
    run: Callable[..., Any], tmp_path: Path
) -> None:
    folder = tmp_path / "collisions"
    folder.mkdir()
    dest_dir = folder / "Documents" / "PDFs"
    dest_dir.mkdir(parents=True)
    # Existing file at destination
    (dest_dir / "report.pdf").write_bytes(b"existing-report")

    # Top-level file with different case ("Report.pdf" vs existing "report.pdf")
    (folder / "Report.pdf").write_bytes(b"new-report-top")
    # Subdirectories with colliding case-insensitive names in recursive mode
    sub1 = folder / "incoming_a"
    sub2 = folder / "incoming_b"
    sub1.mkdir()
    sub2.mkdir()
    (sub1 / "slides.pptx").write_bytes(b"slides-1")
    (sub2 / "SLIDES.PPTX").write_bytes(b"slides-2")

    # Test --on-collision skip first in preview
    skip_res = run(
        "fileflow", "organize", str(folder), "--recursive", "--on-collision", "skip", "--json"
    )
    assert skip_res.exit_code == 0
    skip_data = skip_res.json()
    assert skip_data["skipped_collisions"] == 2

    # Test default --on-collision rename in --apply
    apply_res = run(
        "fileflow", "organize", str(folder), "--recursive", "--apply", "--yes", "--json"
    )
    assert apply_res.exit_code == 0
    assert (dest_dir / "report.pdf").read_bytes() == b"existing-report"
    assert (dest_dir / "Report (1).pdf").read_bytes() == b"new-report-top"
    pres_dir = folder / "Documents" / "Presentations"
    assert (pres_dir / "slides.pptx").read_bytes() == b"slides-1"
    assert (pres_dir / "SLIDES (1).PPTX").read_bytes() == b"slides-2"


def test_undo_refuses_modified_destination_and_occupied_source_and_supports_partial(
    run: Callable[..., Any], tmp_path: Path
) -> None:
    folder = tmp_path / "undo_safety"
    folder.mkdir()
    f1 = folder / "one.pdf"
    f2 = folder / "two.png"
    f3 = folder / "three.zip"
    f1.write_bytes(b"original-1")
    f2.write_bytes(b"original-2")
    f3.write_bytes(b"original-3")

    res = run("fileflow", "organize", str(folder), "--apply", "--yes", "--json")
    assert res.exit_code == 0
    op_id = res.json()["operation_id"]

    moved_f1 = folder / "Documents" / "PDFs" / "one.pdf"
    moved_f2 = folder / "Images" / "PNG" / "two.png"
    moved_f3 = folder / "Archives" / "three.zip"

    # Modify destination file moved_f1
    moved_f1.write_bytes(b"modified-after-move!!")
    # Occupy original source location f2
    f2.write_bytes(b"unrelated-new-file-at-source")

    # Default undo (all-or-nothing) refuses without changing anything
    refused = run("fileflow", "undo", op_id)
    assert refused.exit_code == int(ExitCode.REFUSED)
    # Verify moved_f3 was NOT moved back during the refused all-or-nothing check
    assert moved_f3.exists()
    assert not f3.exists()

    # --partial reverses safe item (three.zip) and skips unsafe items with exit code 1
    partial_res = run("fileflow", "undo", op_id, "--partial", "--json")
    assert partial_res.exit_code == int(ExitCode.WARNING)
    pdata = partial_res.json()
    assert pdata["status"] == "partially_undone"
    assert pdata["restored_count"] == 1
    assert pdata["skipped_unsafe_count"] == 2
    assert f3.exists() and f3.read_bytes() == b"original-3"
    # Modified destination and occupied source were never overwritten
    assert moved_f1.read_bytes() == b"modified-after-move!!"
    assert f2.read_bytes() == b"unrelated-new-file-at-source"
    assert moved_f2.read_bytes() == b"original-2"


def test_partial_failure_on_readonly_file_and_dir(run: Callable[..., Any], tmp_path: Path) -> None:
    folder = tmp_path / "partial_fail"
    folder.mkdir()
    good_file = folder / "good.txt"
    good_file.write_text("good content", encoding="utf-8")
    unreadable_file = folder / "unreadable.pdf"
    unreadable_file.write_bytes(b"secret pdf")
    unreadable_file.chmod(0o000)

    # Also create a read-only destination parent for Images/PNG
    ro_images = folder / "Images"
    ro_images.mkdir()
    ro_images.chmod(0o555)
    img_file = folder / "pic.png"
    img_file.write_bytes(b"png data")

    try:
        res = run("fileflow", "organize", str(folder), "--apply", "--yes", "--json")
        # Partial failure returns ExitCode.WARNING (1)
        assert res.exit_code == int(ExitCode.WARNING)
        data = res.json()
        assert data["status"] == "partial"
        assert data["done_count"] == 1
        assert data["failed_count"] == 2
        assert (folder / "Documents" / "Text" / "good.txt").exists()
        assert unreadable_file.exists()
        assert img_file.exists()

        # Undo still works for the partial operation, restoring good.txt
        undo_res = run("fileflow", "undo", data["operation_id"], "--json")
        assert undo_res.exit_code == 0
        assert good_file.exists()
    finally:
        unreadable_file.chmod(0o644)
        ro_images.chmod(0o755)


def test_symlink_safety_never_followed_or_dereferenced(
    run: Callable[..., Any], tmp_path: Path
) -> None:
    outside = tmp_path / "outside_secret"
    outside.mkdir()
    secret_file = outside / "credentials.txt"
    secret_file.write_text("DO_NOT_TOUCH", encoding="utf-8")

    folder = tmp_path / "sym_folder"
    folder.mkdir()
    (folder / "normal.txt").write_text("normal", encoding="utf-8")
    file_sym = folder / "link_to_secret.txt"
    file_sym.symlink_to(secret_file)
    broken_sym = folder / "broken.pdf"
    broken_sym.symlink_to(outside / "nonexistent.pdf")
    dir_sym = folder / "linked_dir"
    dir_sym.symlink_to(outside)
    loop_sym = folder / "loop"
    loop_sym.symlink_to(folder)

    # 1. Default organize skips symlinks and never descends into symlinked dirs
    res1 = run("fileflow", "organize", str(folder), "--recursive", "--apply", "--yes", "--json")
    assert res1.exit_code == 0
    assert res1.json()["done_count"] == 1
    assert file_sym.is_symlink()
    assert broken_sym.is_symlink()
    assert secret_file.exists() and secret_file.read_text(encoding="utf-8") == "DO_NOT_TOUCH"

    # 2. --include-symlinks moves symlinks as symlinks without dereferencing
    res2 = run(
        "fileflow",
        "organize",
        str(folder),
        "--include-symlinks",
        "--apply",
        "--yes",
        "--json",
    )
    assert res2.exit_code == 0
    moved_sym = folder / "Documents" / "Text" / "link_to_secret.txt"
    moved_broken = folder / "Documents" / "PDFs" / "broken.pdf"
    assert moved_sym.is_symlink()
    assert os.readlink(moved_sym) == str(secret_file)
    assert moved_broken.is_symlink()
    assert secret_file.exists() and secret_file.read_text(encoding="utf-8") == "DO_NOT_TOUCH"


def test_rules_cli_commands_and_traversal_rejection(
    run: Callable[..., Any], tmp_path: Path
) -> None:
    # Rejects '..' traversal and absolute destinations via CLI
    bad_trav = run(
        "fileflow",
        "rules",
        "add",
        "--name",
        "Escape",
        "--ext",
        ".txt",
        "--destination",
        "../escape",
    )
    assert bad_trav.exit_code == int(ExitCode.USAGE)

    bad_abs = run(
        "fileflow",
        "rules",
        "add",
        "--name",
        "Abs",
        "--ext",
        ".txt",
        "--destination",
        "/tmp/escape",
    )
    assert bad_abs.exit_code == int(ExitCode.USAGE)

    bad_regex = run(
        "fileflow",
        "rules",
        "add",
        "--name",
        "ReDoS",
        "--name-regex",
        "(a+)+",
        "--destination",
        "Safe/Dest",
    )
    assert bad_regex.exit_code == int(ExitCode.USAGE)

    # Add valid rule
    add_res = run(
        "fileflow",
        "rules",
        "add",
        "--id",
        "tax-pdfs",
        "--name",
        "Tax PDFs",
        "--priority",
        "5",
        "--ext",
        ".pdf",
        "--name-glob",
        "*tax*",
        "--destination",
        "Documents/Taxes/{year}",
        "--json",
    )
    assert add_res.exit_code == 0
    assert add_res.json()["rule"]["id"] == "tax-pdfs"

    # List rules
    list_res = run("fileflow", "rules", "list", "--json")
    assert list_res.exit_code == 0
    assert list_res.json()["count"] == 1

    # Explain matching file
    sample = tmp_path / "2026_tax_return.pdf"
    sample.write_bytes(b"%PDF tax")
    exp_res = run("fileflow", "rules", "explain", str(sample), "--json")
    assert exp_res.exit_code == 0
    exp_data = exp_res.json()
    assert exp_data["matched_by"] == "rule"
    assert exp_data["rule_id"] == "tax-pdfs"
    assert exp_data["destination_folder"].startswith("Documents/Taxes/")

    # Remove rule requires --yes in non-interactive mode
    assert run("fileflow", "rules", "remove", "tax-pdfs").exit_code == int(ExitCode.REFUSED)
    rem_res = run("fileflow", "rules", "remove", "tax-pdfs", "--yes", "--json")
    assert rem_res.exit_code == 0
    assert rem_res.json()["removed"] is True


def test_rename_cli_and_collision_refusal(run: Callable[..., Any], tmp_path: Path) -> None:
    folder = tmp_path / "photos"
    folder.mkdir()
    (folder / "IMG_1.JPEG").write_bytes(b"img1")
    (folder / "IMG_2.JPEG").write_bytes(b"img2")

    # Preview rename
    prev = run(
        "fileflow",
        "rename",
        str(folder),
        "--pattern",
        "vacation_{n}{ext}",
        "--start",
        "10",
        "--width",
        "3",
        "--normalize-ext",
        "--json",
    )
    assert prev.exit_code == 0
    assert (folder / "IMG_1.JPEG").exists()

    # Apply rename
    app_res = run(
        "fileflow",
        "rename",
        str(folder),
        "--pattern",
        "vacation_{n}{ext}",
        "--start",
        "10",
        "--width",
        "3",
        "--normalize-ext",
        "--apply",
        "--yes",
        "--json",
    )
    assert app_res.exit_code == 0
    op_id = app_res.json()["operation_id"]
    assert (folder / "vacation_010.jpg").read_bytes() == b"img1"
    assert (folder / "vacation_011.jpg").read_bytes() == b"img2"

    # Undo rename
    undo_res = run("fileflow", "undo", op_id)
    assert undo_res.exit_code == 0
    assert (folder / "IMG_1.JPEG").read_bytes() == b"img1"
    assert (folder / "IMG_2.JPEG").read_bytes() == b"img2"

    # Collision when pattern lacks {n} or {name} so both files map to the same name
    col_res = run(
        "fileflow",
        "rename",
        str(folder),
        "--pattern",
        "same_name{ext}",
        "--apply",
        "--yes",
    )
    assert col_res.exit_code == int(ExitCode.REFUSED)
    assert (folder / "IMG_1.JPEG").exists()
    assert (folder / "IMG_2.JPEG").exists()


def test_protected_paths_refused(run: Callable[..., Any], isolated_env: Path) -> None:
    for protected in ("/", "/System", "/Library", str(isolated_env), str(isolated_env / ".ssh")):
        p = Path(protected)
        if not p.exists():
            p.mkdir(parents=True, exist_ok=True)
        res = run("fileflow", "organize", str(p), "--preview")
        assert res.exit_code == int(ExitCode.REFUSED), f"Expected REFUSED for {p}"


def test_duplicates_and_watch_and_config_cli(run: Callable[..., Any], tmp_path: Path) -> None:
    folder = tmp_path / "dup_cli"
    folder.mkdir()
    (folder / "keep.txt").write_text("identical data", encoding="utf-8")
    (folder / "extra.txt").write_text("identical data", encoding="utf-8")
    os.utime(folder / "keep.txt", (1_700_000_000, 1_700_000_000))
    os.utime(folder / "extra.txt", (1_700_000_500, 1_700_000_500))

    review = tmp_path / "dup_review"
    res_dup = run(
        "fileflow",
        "duplicates",
        str(folder),
        "--move-to",
        str(review),
        "--apply",
        "--yes",
        "--json",
    )
    assert res_dup.exit_code == 0
    assert (folder / "keep.txt").exists()
    assert not (folder / "extra.txt").exists()
    assert (review / "extra.txt").exists()

    # Watch --once
    watch_dir = tmp_path / "watch_cli"
    watch_dir.mkdir()
    (watch_dir / "song.mp3").write_bytes(b"mp3-bytes")
    (watch_dir / "incomplete.crdownload").write_bytes(b"partial")

    res_watch = run(
        "fileflow",
        "watch",
        str(watch_dir),
        "--once",
        "--interval",
        "0.01",
        "--apply",
        "--yes",
        "--json",
    )
    assert res_watch.exit_code == 0
    wdata = res_watch.json()
    assert wdata["total_moved"] == 1
    assert wdata["total_skipped_partial"] == 1
    assert (watch_dir / "Audio" / "song.mp3").exists()
    assert (watch_dir / "incomplete.crdownload").exists()

    # Config commands
    assert run("fileflow", "config", "set", "default_collision", "skip").exit_code == 0
    assert run("fileflow", "config", "get", "default_collision").stdout.strip() == "skip"
    assert run("fileflow", "config", "reset", "--yes").exit_code == 0
