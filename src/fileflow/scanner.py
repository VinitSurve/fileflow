"""Read-only folder analysis for 'fileflow scan'."""

from __future__ import annotations

import os
import stat
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from fileflow.categories import classify_path, normalize_extension
from fileflow.common.fsutil import human_size, sha256_file
from fileflow.common.paths import require_directory
from fileflow.config import DEFAULT_IGNORE_NAMES

HASH_VERIFY_MAX_SIZE = 20 * 1000 * 1000  # 20 MB per file for automatic scan hash verification
HASH_VERIFY_MAX_GROUP_FILES = 16


@dataclass
class ExtensionStat:
    """Count and total size for one file extension."""

    extension: str
    count: int
    total_size: int
    total_size_human: str


@dataclass
class CategoryStat:
    """Count and total size for one category."""

    category: str
    count: int
    total_size: int
    total_size_human: str


@dataclass
class ScanFileEntry:
    """Metadata for a notable file (e.g. large or top-level unorganized file)."""

    path: str
    rel_path: str
    name: str
    extension: str
    category: str
    size: int
    size_human: str
    mtime: float


@dataclass
class ScanDuplicateCandidate:
    """Group of duplicate candidates detected during scan."""

    size: int
    size_human: str
    count: int
    wasted_bytes: int
    wasted_human: str
    verified_by_hash: bool
    sha256: str | None
    paths: list[str] = field(default_factory=list)


@dataclass
class FolderTreeNode:
    """Node in the existing folder structure tree."""

    name: str
    rel_path: str
    file_count: int
    dir_count: int
    total_size: int
    total_size_human: str
    children: list[FolderTreeNode] = field(default_factory=list)


@dataclass
class ScanReport:
    """Read-only directory scan report."""

    root: str
    total_files: int
    total_dirs: int
    total_symlinks: int
    total_size: int
    total_size_human: str
    large_threshold: int
    large_threshold_human: str
    unorganized_count: int
    by_extension: list[ExtensionStat] = field(default_factory=list)
    by_category: list[CategoryStat] = field(default_factory=list)
    large_files: list[ScanFileEntry] = field(default_factory=list)
    duplicate_candidates: list[ScanDuplicateCandidate] = field(default_factory=list)
    unorganized_files: list[ScanFileEntry] = field(default_factory=list)
    folder_tree: FolderTreeNode | None = None
    warnings: list[str] = field(default_factory=list)


def scan_directory(
    root: Path,
    *,
    large_threshold: int = 50_000_000,
    max_depth: int = 2,
    include_hidden: bool = False,
    ignore_names: Sequence[str] = DEFAULT_IGNORE_NAMES,
) -> ScanReport:
    """Perform a read-only analysis of ``root`` without modifying any files."""
    require_directory(root, what="Target folder")
    ignore_set = set(ignore_names)

    total_files = 0
    total_dirs = 0
    total_symlinks = 0
    total_size = 0
    warnings: list[str] = []

    ext_Map: dict[str, list[int]] = {}  # ext -> [count, bytes]
    cat_map: dict[str, list[int]] = {}  # cat -> [count, bytes]
    large_files: list[ScanFileEntry] = []
    unorganized_files: list[ScanFileEntry] = []
    by_size: dict[int, list[tuple[Path, int, int]]] = {}  # size -> [(path, dev, ino)]

    def _walk(current: Path, depth: int) -> FolderTreeNode:
        nonlocal total_files, total_dirs, total_symlinks, total_size
        rel_dir = "." if current == root else current.relative_to(root).as_posix()
        node_files = 0
        node_dirs = 0
        node_size = 0
        child_nodes: list[FolderTreeNode] = []

        try:
            entries = sorted(os.scandir(current), key=lambda e: e.name)
        except OSError as exc:
            warnings.append(f"Cannot read directory {current}: {exc}")
            return FolderTreeNode(
                name=current.name or str(current),
                rel_path=rel_dir,
                file_count=0,
                dir_count=0,
                total_size=0,
                total_size_human="0 B",
            )

        subdirs: list[Path] = []
        for entry in entries:
            name = entry.name
            if not include_hidden and (name.startswith(".") or name in ignore_set):
                continue
            entry_path = current / name
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError as exc:
                warnings.append(f"Cannot stat {entry_path}: {exc}")
                continue

            mode = st.st_mode
            if stat.S_ISLNK(mode):
                total_symlinks += 1
                continue
            if stat.S_ISDIR(mode):
                total_dirs += 1
                node_dirs += 1
                subdirs.append(entry_path)
                continue
            if not stat.S_ISREG(mode):
                continue

            f_size = st.st_size
            total_files += 1
            total_size += f_size
            node_files += 1
            node_size += f_size

            ext = normalize_extension(entry_path.suffix) or "(no ext)"
            cat = classify_path(entry_path)

            ext_bucket = ext_Map.setdefault(ext, [0, 0])
            ext_bucket[0] += 1
            ext_bucket[1] += f_size

            cat_bucket = cat_map.setdefault(cat, [0, 0])
            cat_bucket[0] += 1
            cat_bucket[1] += f_size

            rel_file = entry_path.relative_to(root).as_posix()
            file_entry = ScanFileEntry(
                path=str(entry_path),
                rel_path=rel_file,
                name=name,
                extension=ext,
                category=cat,
                size=f_size,
                size_human=human_size(f_size),
                mtime=st.st_mtime,
            )

            if depth == 0:
                unorganized_files.append(file_entry)
            if f_size >= large_threshold:
                large_files.append(file_entry)
            if f_size > 0:
                by_size.setdefault(f_size, []).append((entry_path, st.st_dev, st.st_ino))

        for subdir in subdirs:
            sub_node = _walk(subdir, depth + 1)
            node_size += sub_node.total_size
            if depth < max_depth:
                child_nodes.append(sub_node)

        return FolderTreeNode(
            name=current.name or str(current),
            rel_path=rel_dir,
            file_count=node_files,
            dir_count=node_dirs,
            total_size=node_size,
            total_size_human=human_size(node_size),
            children=child_nodes,
        )

    tree_root = _walk(root, 0)

    by_extension = [
        ExtensionStat(
            extension=ext,
            count=vals[0],
            total_size=vals[1],
            total_size_human=human_size(vals[1]),
        )
        for ext, vals in sorted(ext_Map.items(), key=lambda kv: (-kv[1][0], -kv[1][1], kv[0]))
    ]

    by_category = [
        CategoryStat(
            category=cat,
            count=vals[0],
            total_size=vals[1],
            total_size_human=human_size(vals[1]),
        )
        for cat, vals in sorted(cat_map.items(), key=lambda kv: (-kv[1][0], -kv[1][1], kv[0]))
    ]

    large_files.sort(key=lambda f: (-f.size, f.rel_path))
    unorganized_files.sort(key=lambda f: f.name)

    duplicate_candidates: list[ScanDuplicateCandidate] = []
    for f_size, candidates in sorted(by_size.items(), key=lambda kv: -kv[0]):
        # Deduplicate hardlinks sharing the same (dev, ino)
        seen_inodes: set[tuple[int, int]] = set()
        unique_paths: list[Path] = []
        for p, dev, ino in candidates:
            if (dev, ino) in seen_inodes:
                continue
            seen_inodes.add((dev, ino))
            unique_paths.append(p)

        if len(unique_paths) < 2:
            continue

        if f_size <= HASH_VERIFY_MAX_SIZE and len(unique_paths) <= HASH_VERIFY_MAX_GROUP_FILES:
            by_hash: dict[str, list[Path]] = {}
            for p in unique_paths:
                try:
                    digest = sha256_file(p)
                except OSError:
                    continue
                by_hash.setdefault(digest, []).append(p)
            for digest, h_paths in sorted(by_hash.items(), key=lambda kv: str(kv[1][0])):
                if len(h_paths) >= 2:
                    wasted = f_size * (len(h_paths) - 1)
                    duplicate_candidates.append(
                        ScanDuplicateCandidate(
                            size=f_size,
                            size_human=human_size(f_size),
                            count=len(h_paths),
                            wasted_bytes=wasted,
                            wasted_human=human_size(wasted),
                            verified_by_hash=True,
                            sha256=digest,
                            paths=[p.relative_to(root).as_posix() for p in h_paths],
                        )
                    )
        else:
            wasted = f_size * (len(unique_paths) - 1)
            duplicate_candidates.append(
                ScanDuplicateCandidate(
                    size=f_size,
                    size_human=human_size(f_size),
                    count=len(unique_paths),
                    wasted_bytes=wasted,
                    wasted_human=human_size(wasted),
                    verified_by_hash=False,
                    sha256=None,
                    paths=[p.relative_to(root).as_posix() for p in unique_paths],
                )
            )

    return ScanReport(
        root=str(root),
        total_files=total_files,
        total_dirs=total_dirs,
        total_symlinks=total_symlinks,
        total_size=total_size,
        total_size_human=human_size(total_size),
        large_threshold=large_threshold,
        large_threshold_human=human_size(large_threshold),
        unorganized_count=len(unorganized_files),
        by_extension=by_extension,
        by_category=by_category,
        large_files=large_files,
        duplicate_candidates=duplicate_candidates,
        unorganized_files=unorganized_files,
        folder_tree=tree_root,
        warnings=warnings,
    )
