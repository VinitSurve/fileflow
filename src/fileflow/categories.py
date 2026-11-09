"""Default file categories, extension mappings, and destination folders."""

from __future__ import annotations

from pathlib import Path

DEFAULT_CATEGORY_FOLDERS: tuple[str, ...] = (
    "Documents/PDFs",
    "Documents/Word",
    "Documents/Spreadsheets",
    "Documents/Presentations",
    "Documents/Text",
    "Images/PNG",
    "Images/JPEG",
    "Images/GIF",
    "Images/Other",
    "Videos",
    "Audio",
    "Archives",
    "Code",
    "Other",
)

DEFAULT_TOP_LEVEL_DIRS: frozenset[str] = frozenset(
    folder.split("/", 1)[0] for folder in DEFAULT_CATEGORY_FOLDERS
)

EXTENSION_TO_CATEGORY: dict[str, str] = {
    # Documents/PDFs
    ".pdf": "Documents/PDFs",
    # Documents/Word
    ".doc": "Documents/Word",
    ".docx": "Documents/Word",
    ".odt": "Documents/Word",
    ".rtf": "Documents/Word",
    ".pages": "Documents/Word",
    # Documents/Spreadsheets
    ".xls": "Documents/Spreadsheets",
    ".xlsx": "Documents/Spreadsheets",
    ".csv": "Documents/Spreadsheets",
    ".tsv": "Documents/Spreadsheets",
    ".ods": "Documents/Spreadsheets",
    ".numbers": "Documents/Spreadsheets",
    # Documents/Presentations
    ".ppt": "Documents/Presentations",
    ".pptx": "Documents/Presentations",
    ".odp": "Documents/Presentations",
    ".key": "Documents/Presentations",
    # Documents/Text
    ".txt": "Documents/Text",
    ".md": "Documents/Text",
    ".markdown": "Documents/Text",
    ".rst": "Documents/Text",
    ".tex": "Documents/Text",
    ".log": "Documents/Text",
    ".org": "Documents/Text",
    ".epub": "Documents/Text",
    ".mobi": "Documents/Text",
    # Images/PNG
    ".png": "Images/PNG",
    # Images/JPEG
    ".jpg": "Images/JPEG",
    ".jpeg": "Images/JPEG",
    ".jpe": "Images/JPEG",
    ".jfif": "Images/JPEG",
    # Images/GIF
    ".gif": "Images/GIF",
    # Images/Other
    ".svg": "Images/Other",
    ".webp": "Images/Other",
    ".bmp": "Images/Other",
    ".tiff": "Images/Other",
    ".tif": "Images/Other",
    ".heic": "Images/Other",
    ".heif": "Images/Other",
    ".ico": "Images/Other",
    ".raw": "Images/Other",
    ".cr2": "Images/Other",
    ".nef": "Images/Other",
    ".psd": "Images/Other",
    ".ai": "Images/Other",
    ".avif": "Images/Other",
    # Videos
    ".mp4": "Videos",
    ".mov": "Videos",
    ".mkv": "Videos",
    ".avi": "Videos",
    ".webm": "Videos",
    ".m4v": "Videos",
    ".flv": "Videos",
    ".wmv": "Videos",
    ".mpeg": "Videos",
    ".mpg": "Videos",
    # Audio
    ".mp3": "Audio",
    ".wav": "Audio",
    ".flac": "Audio",
    ".aac": "Audio",
    ".ogg": "Audio",
    ".m4a": "Audio",
    ".wma": "Audio",
    ".opus": "Audio",
    ".aiff": "Audio",
    # Archives
    ".zip": "Archives",
    ".tar": "Archives",
    ".gz": "Archives",
    ".tgz": "Archives",
    ".bz2": "Archives",
    ".tbz2": "Archives",
    ".xz": "Archives",
    ".txz": "Archives",
    ".7z": "Archives",
    ".rar": "Archives",
    ".zst": "Archives",
    ".dmg": "Archives",
    ".iso": "Archives",
    ".pkg": "Archives",
    # Code
    ".py": "Code",
    ".pyi": "Code",
    ".js": "Code",
    ".mjs": "Code",
    ".cjs": "Code",
    ".ts": "Code",
    ". jsx": "Code",
    ".jsx": "Code",
    ".tsx": "Code",
    ".go": "Code",
    ".rs": "Code",
    ".c": "Code",
    ".cpp": "Code",
    ".cc": "Code",
    ".h": "Code",
    ".hpp": "Code",
    ".java": "Code",
    ".kt": "Code",
    ".swift": "Code",
    ".rb": "Code",
    ".php": "Code",
    ".cs": "Code",
    ".sh": "Code",
    ".zsh": "Code",
    ".bash": "Code",
    ".fish": "Code",
    ".sql": "Code",
    ".html": "Code",
    ".htm": "Code",
    ".css": "Code",
    ".scss": "Code",
    ".less": "Code",
    ".json": "Code",
    ".yaml": "Code",
    ".yml": "Code",
    ".toml": "Code",
    ".xml": "Code",
    ".ini": "Code",
    ".cfg": "Code",
    ".lua": "Code",
    ".r": "Code",
    ".pl": "Code",
    ".scala": "Code",
    ".clj": "Code",
    ".ex": "Code",
    ".exs": "Code",
}


def normalize_extension(ext: str) -> str:
    """Normalize an extension string to lowercase with a leading dot (or empty string)."""
    cleaned = ext.strip().lower()
    if not cleaned:
        return ""
    return cleaned if cleaned.startswith(".") else f".{cleaned}"


def classify_extension(ext: str) -> str:
    """Return the default category folder for a file extension."""
    norm = normalize_extension(ext)
    if not norm:
        return "Other"
    return EXTENSION_TO_CATEGORY.get(norm, "Other")


def classify_path(path: Path) -> str:
    """Classify a file path into its default category folder."""
    return classify_extension(path.suffix)


def category_matches(file_category: str, target_category: str) -> bool:
    """Check whether ``file_category`` matches ``target_category`` (full, group, or leaf)."""
    fc = file_category.strip().lower()
    tc = target_category.strip().lower()
    if not tc:
        return True
    if fc == tc:
        return True
    parts = [p for p in fc.split("/") if p]
    return tc in parts


def is_organized_destination_dir(
    rel_parts: tuple[str, ...], extra_roots: set[str] | None = None
) -> bool:
    """True if a relative path lies inside an organized destination folder."""
    if not rel_parts:
        return False
    first = rel_parts[0]
    first_cf = first.casefold()
    for top in DEFAULT_TOP_LEVEL_DIRS:
        if first_cf == top.casefold():
            return True
    if extra_roots:
        for root_name in extra_roots:
            if first_cf == root_name.casefold():
                return True
    return False
