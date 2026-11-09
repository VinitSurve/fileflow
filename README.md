# FileFlow (`fileflow`)

> Intelligent, Rule-Based File Organization with Write-Ahead SQLite Journaling & Lossless Undo

[![CI](https://github.com/VinitSurve/fileflow/actions/workflows/ci.yml/badge.svg)](https://github.com/VinitSurve/fileflow/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python: 3.11+](https://img.shields.io/badge/Python-3.11%2B-brightgreen.svg)](https://www.python.org/)

`fileflow` is a local-first, safe command-line utility for organizing messy directories (like `~/Downloads` or desktop folders). It operates with complete transparency: nothing changes on disk without an explainable pre-execution preview, explicit confirmation, write-ahead SQLite journaling, and verified lossless undo.

---

## Features

- **Preview-First by Default:** Commands default to preview/dry-run mode. Applying file relocations requires `--apply` and explicit interactive confirmation (or `--yes` in scripts).
- **SQLite Write-Ahead Operation Journal (`journal.db`):** Every planned file move is committed to a write-ahead journal before any filesystem mutation occurs. Item state (`done`, `failed`, `skipped`, `undone`) is updated transactionally.
- **Verified Lossless Undo (`fileflow undo`):** Reverses operations by matching transaction batches. Validates that destination files still match original size, modification timestamp, and SHA-256 hash before restoring. Refuses to overwrite files if the original path has since been occupied.
- **APFS Case-Insensitive Collision Protection:** Detects intra-batch collisions, swap cycles, and case collisions (`Report.pdf` vs `report.pdf`) on macOS APFS filesystems. Automatically renames destination files with unique counters (`file (1).ext`) or skips duplicates according to `--on-collision`.
- **Customizable Rule Engine:** Filter by extension, glob patterns, modification date, size bounds, or custom metadata into structured directory hierarchies.
- **Directory Watching (`fileflow watch`):** Monitors target directories and automatically routes newly completed downloads, filtering partial downloads (`.crdownload`, `.part`, `.tmp`).
- **Protected Path Guards:** Refuses to analyze or modify protected directories including `/`, `/System`, `/Library`, `/usr`, `/Applications`, `~/.ssh`, `~/.aws`, or `$HOME` itself.

---

## Installation

### Using pipx (Recommended for standalone CLI use)

```bash
pipx install git+https://github.com/VinitSurve/fileflow.git
```

Or install from a local checkout:

```bash
git clone https://github.com/VinitSurve/fileflow.git
cd fileflow
pipx install .
```

### Using pip (in an active virtual environment)

```bash
pip install .
```

---

## Quick Start

```bash
# Scan and inspect category breakdown in Downloads
fileflow scan ~/Downloads

# Preview organization without moving any files (dry run)
fileflow organize ~/Downloads --preview

# Apply organization with confirmation
fileflow organize ~/Downloads --apply --yes

# Inspect transaction history
fileflow history

# Undo the most recent operation
fileflow undo --yes

# List active custom rules
fileflow rules list
```

---

## Command Reference

| Command | Description |
|---|---|
| `fileflow scan [PATH]` | Analyze directory contents, categories, large files, and duplicates (read-only). |
| `fileflow organize [PATH]` | Organize files according to rules (`--preview`, `--apply`, `--recursive`, `--on-collision`). |
| `fileflow rename [PATH]` | Batch rename files with patterns (`--prefix`, `--suffix`, `--replace`, `--date-prefix`). |
| `fileflow duplicates [PATH]` | Find duplicates and optionally isolate them (`--move-to DIR`). |
| `fileflow undo [OP_ID]` | Revert a recorded operation batch safely with hash and identity verification. |
| `fileflow history` | Display recent organization operations from `journal.db`. |
| `fileflow rules` | Manage custom organization rules (`list`, `add`, `remove`, `enable`, `disable`). |
| `fileflow watch [PATH]` | Monitor a directory and automatically apply rules to newly stabilized files. |
| `fileflow config` | View and edit `fileflow` configuration. |

### Global Flags

- `--json`: Output machine-readable JSON on `stdout`.
- `--quiet`: Suppress banners, headings, and decoration.
- `--verbose`: Print diagnostic debugging details to `stderr`.
- `--no-color`: Disable ANSI color escapes (respects `NO_COLOR=1`).
- `--help`, `-h`: Show usage assistance.
- `--version`: Display `fileflow` version.

---

## Default Category Routing

`fileflow` routes unorganized files into standard categorized hierarchies:
- **Documents:** `Documents/PDFs`, `Documents/Word`, `Documents/Spreadsheets`, `Documents/Presentations`, `Documents/Text`
- **Images:** `Images/PNG`, `Images/JPEG`, `Images/GIF`, `Images/SVG`
- **Media & Archives:** `Videos/`, `Audio/`, `Archives/`
- **Development:** `Code/Python`, `Code/JavaScript`, `Code/Rust`, `Data/JSON`, `Data/CSV`

---

## Configuration & Storage

`fileflow` stores its configuration, custom rules, and write-ahead journal in macOS platform directories:
- **Journal Database:** `~/Library/Application Support/devtoolkit/fileflow/journal.db`
- **Custom Rules:** `~/Library/Application Support/devtoolkit/fileflow/rules.json`
- **Config:** `~/Library/Application Support/devtoolkit/fileflow/config.json`
- **Logs:** `~/Library/Logs/devtoolkit/fileflow/fileflow.log`

Override with `DEVTOOLKIT_HOME=/custom/path`.

Example rule management:
```bash
# Add a rule for PDF tax invoices
fileflow rules add \
  --id invoices-pdf \
  --name "Tax Invoices" \
  --priority 10 \
  --ext .pdf \
  --name-glob "*invoice*" \
  --destination "Documents/Invoices/{year}"
```

---

## Safety Guarantees

1. **Pre-Write Journaling:** Every file movement is persisted to SQLite before any filesystem mutation occurs.
2. **Identity Verification Before Undo:** `fileflow undo` validates that the destination file has not been modified or replaced since the original move before moving it back.
3. **Collision Avoidance:** Computes unique non-conflicting destinations (`file (1).ext`) without overwriting existing files, accounting for case-insensitive APFS filesystems.
4. **Symlink Safety:** Never traverses symlinked directory trees during recursive scanning.

---

## Development Setup

```bash
git clone https://github.com/VinitSurve/fileflow.git
cd fileflow
make dev
make test
make check
```

---

## Running Tests

```bash
# Run pytest suite
pytest -v

# Run fast unit tests
pytest -m "not slow" -q
```

---

## Known Limitations

- **Cross-Device Moves:** Moving files across different physical filesystems requires a copy-and-verify-then-unlink procedure; within the same APFS volume, moves are instantaneous atomic directory link updates.
- **Locked Files:** Files locked by running processes or lacking write permissions are skipped and recorded in the operation journal as `skipped`.

---

## License

This project is licensed under the [MIT License](LICENSE).
