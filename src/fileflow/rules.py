"""Deterministic, explainable rule engine and rule store for fileflow."""

from __future__ import annotations

import datetime as dt
import fnmatch
import json
import re
import string
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fileflow.categories import (
    category_matches,
    classify_path,
    normalize_extension,
)
from fileflow.common.exceptions import DataError, NotFoundError, UsageError
from fileflow.common.fsutil import atomic_write_text, parse_size
from fileflow.common.paths import ToolPaths

MAX_REGEX_LENGTH = 256
ALLOWED_PLACEHOLDERS = frozenset({"year", "month", "day", "ext", "category"})
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}$")
_REL_TIME_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhdwySMHDWY])\s*$")
_REL_SECONDS = {
    "s": 1.0,
    "m": 60.0,
    "h": 3600.0,
    "d": 86400.0,
    "w": 7 * 86400.0,
    "y": 365 * 86400.0,
}


def validate_safe_regex(pattern: str) -> re.Pattern[str]:
    """Validate and compile a filename regex with ReDoS safeguards."""
    if not pattern:
        raise UsageError("Regular expression pattern cannot be empty.")
    if len(pattern) > MAX_REGEX_LENGTH:
        raise UsageError(
            f"Regular expression exceeds maximum length of {MAX_REGEX_LENGTH} characters.",
            hint="Use a simpler pattern or a filename glob (--name-glob).",
        )
    if _has_nested_quantifiers(pattern):
        raise UsageError(
            f"Unsafe regular expression rejected (nested quantifiers): {pattern!r}",
            hint="Remove nested repetitions like (a+)+ or (.*)* to avoid backtracking.",
        )
    try:
        return re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        raise UsageError(
            f"Invalid regular expression {pattern!r}: {exc}",
            hint="Check regex syntax or use --name-glob instead.",
        ) from exc


def _has_nested_quantifiers(pattern: str) -> bool:
    """Detect nested quantifiers like (a+)+, (a*)*, ([a-z]+){2,} via a group stack."""
    stack: list[bool] = []
    in_char_class = False
    escaped = False
    i = 0
    n = len(pattern)
    while i < n:
        ch = pattern[i]
        if escaped:
            escaped = False
            i += 1
            continue
        if ch == "\\":
            escaped = True
            i += 1
            continue
        if in_char_class:
            if ch == "]":
                in_char_class = False
            i += 1
            continue
        if ch == "[":
            in_char_class = True
            i += 1
            continue
        if ch == "(":
            stack.append(False)
            i += 1
            continue
        if ch == ")":
            had_inner_quant = stack.pop() if stack else False
            next_ch = pattern[i + 1] if i + 1 < n else ""
            if had_inner_quant and next_ch in ("+", "*", "{", "?"):
                return True
            if stack and (had_inner_quant or next_ch in ("+", "*", "{")):
                stack[-1] = True
            i += 1
            continue
        if stack and (ch in ("+", "*") or (ch == "{" and _looks_like_quantifier_brace(pattern, i))):
            stack[-1] = True
        i += 1
    return False


def _looks_like_quantifier_brace(pattern: str, pos: int) -> bool:
    close = pattern.find("}", pos + 1)
    if close == -1:
        return False
    inner = pattern[pos + 1 : close]
    if not inner:
        return False
    parts = inner.split(",")
    if len(parts) > 2:
        return False
    return all(p.strip().isdigit() or p.strip() == "" for p in parts) and any(
        p.strip().isdigit() for p in parts
    )


def validate_destination_template(destination: str) -> str:
    """Validate that a rule destination is a safe relative path with valid placeholders."""
    cleaned = destination.strip()
    if not cleaned:
        raise UsageError("Rule destination cannot be empty.")
    if "\x00" in cleaned:
        raise UsageError("Rule destination must not contain NUL bytes.")
    if (
        cleaned.startswith(("/", "\\", "~"))
        or Path(cleaned).is_absolute()
        or (len(cleaned) >= 2 and cleaned[1] == ":")
    ):
        raise UsageError(
            f"Rule destination must be a relative path within the target folder: {destination!r}",
            hint="Use a relative path like 'Documents/Invoices' instead of an absolute path.",
        )
    raw_parts = re.split(r"[/\\]+", cleaned)
    norm_parts: list[str] = []
    for part in raw_parts:
        if part in ("", "."):
            continue
        if part == ".." or ".." in part:
            raise UsageError(
                f"Rule destination must not contain '..' path traversal: {destination!r}",
                hint="All organized files must stay inside the target directory.",
            )
        norm_parts.append(part)
    if not norm_parts:
        raise UsageError(f"Rule destination resolves to an empty path: {destination!r}")

    formatter = string.Formatter()
    try:
        for _, field_name, format_spec, conversion in formatter.parse(cleaned):
            if field_name is None:
                continue
            if conversion or format_spec:
                raise UsageError(
                    f"Unsupported placeholder formatting in destination: {destination!r}"
                )
            if field_name not in ALLOWED_PLACEHOLDERS:
                allowed = ", ".join(f"{{{p}}}" for p in sorted(ALLOWED_PLACEHOLDERS))
                raise UsageError(
                    f"Unknown placeholder {{{field_name}}} in destination {destination!r}.",
                    hint=f"Allowed placeholders: {allowed}",
                )
    except ValueError as exc:
        raise UsageError(
            f"Malformed placeholder braces in destination {destination!r}: {exc}"
        ) from exc

    return "/".join(norm_parts)


def parse_date_cutoff(spec: str, *, now: float | None = None) -> float:
    """Parse an ISO date/datetime or relative offset (e.g. '30d', '12h') into a UNIX timestamp."""
    text = spec.strip()
    if not text:
        raise UsageError("Date condition cannot be empty.")
    match = _REL_TIME_RE.match(text)
    if match:
        amount = float(match.group(1))
        unit = match.group(2).lower()
        ref = time.time() if now is None else now
        return ref - amount * _REL_SECONDS[unit]
    try:
        if "T" in text or " " in text:
            parsed_dt = dt.datetime.fromisoformat(text)
            if parsed_dt.tzinfo is None:
                parsed_dt = parsed_dt.replace(tzinfo=dt.UTC)
            return parsed_dt.timestamp()
        parsed_date = dt.date.fromisoformat(text)
        parsed_dt = dt.datetime(parsed_date.year, parsed_date.month, parsed_date.day, tzinfo=dt.UTC)
        return parsed_dt.timestamp()
    except ValueError as exc:
        raise UsageError(
            f"Invalid date or relative time: {spec!r}",
            hint="Use ISO format (YYYY-MM-DD) or relative offsets like '30d', '2w', '12h'.",
        ) from exc


def _coerce_optional_size(value: int | str | None, field_name: str) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise UsageError(f"Invalid {field_name}: {value!r}")
    if isinstance(value, int):
        if value < 0:
            raise UsageError(f"{field_name} must be >= 0 (got {value})")
        return value
    if isinstance(value, str):
        val = parse_size(value)
        if val < 0:
            raise UsageError(f"{field_name} must be >= 0")
        return val
    raise UsageError(f"Invalid {field_name}: {value!r}")


@dataclass
class Rule:
    """A single file organization rule."""

    id: str
    name: str
    destination: str
    enabled: bool = True
    priority: int = 100
    extensions: list[str] = field(default_factory=list)
    name_glob: str | None = None
    name_regex: str | None = None
    min_size: int | None = None
    max_size: int | None = None
    modified_before: str | None = None
    modified_after: str | None = None
    created_before: str | None = None
    created_after: str | None = None
    category: str | None = None
    source_folder: str | None = None

    def validate(self) -> Rule:
        """Validate and normalize fields in-place; returns ``self``."""
        self.id = self.id.strip()
        if not self.id or not _ID_RE.match(self.id):
            raise UsageError(
                f"Invalid rule id {self.id!r}.",
                hint="Use 1-64 alphanumeric characters, dashes, underscores or dots.",
            )
        self.name = self.name.strip()
        if not self.name:
            raise UsageError("Rule name cannot be empty.")
        if isinstance(self.priority, bool) or not isinstance(self.priority, int):
            raise UsageError(f"Rule priority must be an integer (got {self.priority!r}).")
        self.destination = validate_destination_template(self.destination)
        self.extensions = [
            normalize_extension(ext) for ext in self.extensions if ext and ext.strip()
        ]
        if self.name_glob is not None:
            self.name_glob = self.name_glob.strip() or None
        if self.name_regex is not None:
            self.name_regex = self.name_regex.strip() or None
            if self.name_regex is not None:
                validate_safe_regex(self.name_regex)
        self.min_size = _coerce_optional_size(self.min_size, "min_size")
        self.max_size = _coerce_optional_size(self.max_size, "max_size")
        if (
            self.min_size is not None
            and self.max_size is not None
            and self.min_size > self.max_size
        ):
            raise UsageError(
                f"min_size ({self.min_size}) cannot be greater than max_size ({self.max_size})."
            )
        for attr in ("modified_before", "modified_after", "created_before", "created_after"):
            val = getattr(self, attr)
            if val is not None:
                cleaned = val.strip()
                if cleaned:
                    parse_date_cutoff(cleaned)
                    setattr(self, attr, cleaned)
                else:
                    setattr(self, attr, None)
        if self.category is not None:
            self.category = self.category.strip() or None
        if self.source_folder is not None:
            self.source_folder = self.source_folder.strip() or None
        return self

    def has_any_condition(self) -> bool:
        """True if at least one matching condition is configured."""
        return bool(
            self.extensions
            or self.name_glob
            or self.name_regex
            or self.min_size is not None
            or self.max_size is not None
            or self.modified_before
            or self.modified_after
            or self.created_before
            or self.created_after
            or self.category
            or self.source_folder
        )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Rule:
        """Construct and validate a :class:`Rule` from a JSON mapping."""
        if not isinstance(data, Mapping):
            raise DataError("Each rule entry must be a JSON object.")
        conditions = data.get("conditions")
        src: dict[str, Any] = dict(data)
        if isinstance(conditions, Mapping):
            for k, v in conditions.items():
                src.setdefault(k, v)
        raw_exts = src.get("extensions") or []
        if isinstance(raw_exts, str):
            raw_exts = [part.strip() for part in raw_exts.split(",") if part.strip()]
        elif not isinstance(raw_exts, list) or not all(isinstance(x, str) for x in raw_exts):
            raise DataError("Rule 'extensions' must be a list of strings.")
        try:
            rule = cls(
                id=str(src.get("id", "")),
                name=str(src.get("name", "")),
                destination=str(src.get("destination", "")),
                enabled=bool(src.get("enabled", True)),
                priority=int(src.get("priority", 100)),
                extensions=list(raw_exts),
                name_glob=_opt_str(src.get("name_glob")),
                name_regex=_opt_str(src.get("name_regex")),
                min_size=_coerce_optional_size(src.get("min_size"), "min_size"),
                max_size=_coerce_optional_size(src.get("max_size"), "max_size"),
                modified_before=_opt_str(src.get("modified_before")),
                modified_after=_opt_str(src.get("modified_after")),
                created_before=_opt_str(src.get("created_before")),
                created_after=_opt_str(src.get("created_after")),
                category=_opt_str(src.get("category")),
                source_folder=_opt_str(src.get("source_folder")),
            )
            return rule.validate()
        except (UsageError, ValueError, TypeError) as exc:
            msg = exc.message if isinstance(exc, UsageError) else str(exc)
            raise DataError(
                f"Invalid rule definition ({src.get('id', 'unknown')}): {msg}",
                hint="Fix the rules file or remove the broken rule.",
            ) from exc

    def to_dict(self) -> dict[str, Any]:
        """Serialize rule to a clean dictionary."""
        return {
            "id": self.id,
            "name": self.name,
            "enabled": self.enabled,
            "priority": self.priority,
            "destination": self.destination,
            "extensions": self.extensions,
            "name_glob": self.name_glob,
            "name_regex": self.name_regex,
            "min_size": self.min_size,
            "max_size": self.max_size,
            "modified_before": self.modified_before,
            "modified_after": self.modified_after,
            "created_before": self.created_before,
            "created_after": self.created_after,
            "category": self.category,
            "source_folder": self.source_folder,
        }


def _opt_str(val: Any) -> str | None:
    if val is None:
        return None
    if not isinstance(val, str):
        raise ValueError(f"Expected string, got {type(val).__name__}")
    s = val.strip()
    return s if s else None


@dataclass
class FileMetadata:
    """Attributes of a file used for deterministic rule evaluation."""

    path: Path
    name: str
    extension: str
    size: int
    mtime: float
    ctime: float
    category: str
    source_folder: str

    @classmethod
    def from_path(cls, path: Path, *, scan_root: Path | None = None) -> FileMetadata:
        st = path.lstat()
        ctime = getattr(st, "st_birthtime", st.st_ctime)
        ext = normalize_extension(path.suffix)
        cat = classify_path(path)
        if scan_root is not None:
            try:
                rel_parent = path.parent.relative_to(scan_root)
                src_folder = "." if str(rel_parent) == "." else rel_parent.as_posix()
            except ValueError:
                src_folder = "."
        else:
            src_folder = "."
        return cls(
            path=path,
            name=path.name,
            extension=ext,
            size=st.st_size,
            mtime=st.st_mtime,
            ctime=ctime,
            category=cat,
            source_folder=src_folder,
        )


@dataclass
class RuleEvaluation:
    """Detailed evaluation trace of a single rule against a file."""

    rule_id: str
    rule_name: str
    priority: int
    enabled: bool
    matched: bool
    reasons: list[str]
    failed_conditions: list[str]


@dataclass
class MatchDecision:
    """Result of matching a file against user rules and default categories."""

    matched_by: str  # "rule" or "default_category"
    rule_id: str | None
    rule_name: str | None
    category: str
    destination_folder: str
    reason: str
    evaluations: list[RuleEvaluation] = field(default_factory=list)


def evaluate_rule(rule: Rule, meta: FileMetadata, *, now: float | None = None) -> RuleEvaluation:
    """Evaluate all conditions of ``rule`` against ``meta``."""
    if not rule.enabled:
        return RuleEvaluation(
            rule_id=rule.id,
            rule_name=rule.name,
            priority=rule.priority,
            enabled=False,
            matched=False,
            reasons=[],
            failed_conditions=["rule is disabled"],
        )

    reasons: list[str] = []
    failed: list[str] = []

    if rule.extensions:
        norm_exts = [normalize_extension(e) for e in rule.extensions]
        if meta.extension in norm_exts:
            reasons.append(f"extension {meta.extension!r} in {norm_exts}")
        else:
            failed.append(f"extension {meta.extension or '(none)'!r} not in {norm_exts}")

    if rule.name_glob:
        if fnmatch.fnmatchcase(meta.name.lower(), rule.name_glob.lower()):
            reasons.append(f"name matches glob {rule.name_glob!r}")
        else:
            failed.append(f"name {meta.name!r} does not match glob {rule.name_glob!r}")

    if rule.name_regex:
        compiled = validate_safe_regex(rule.name_regex)
        # Bound subject length as an extra match-time safeguard
        subject = meta.name[:512]
        if compiled.search(subject):
            reasons.append(f"name matches regex {rule.name_regex!r}")
        else:
            failed.append(f"name {meta.name!r} does not match regex {rule.name_regex!r}")

    if rule.min_size is not None:
        if meta.size >= rule.min_size:
            reasons.append(f"size {meta.size} >= min_size {rule.min_size}")
        else:
            failed.append(f"size {meta.size} < min_size {rule.min_size}")

    if rule.max_size is not None:
        if meta.size <= rule.max_size:
            reasons.append(f"size {meta.size} <= max_size {rule.max_size}")
        else:
            failed.append(f"size {meta.size} > max_size {rule.max_size}")

    if rule.modified_before:
        cutoff = parse_date_cutoff(rule.modified_before, now=now)
        if meta.mtime < cutoff:
            reasons.append(f"modified before {rule.modified_before}")
        else:
            failed.append(f"not modified before {rule.modified_before}")

    if rule.modified_after:
        cutoff = parse_date_cutoff(rule.modified_after, now=now)
        if meta.mtime >= cutoff:
            reasons.append(f"modified after {rule.modified_after}")
        else:
            failed.append(f"not modified after {rule.modified_after}")

    if rule.created_before:
        cutoff = parse_date_cutoff(rule.created_before, now=now)
        if meta.ctime < cutoff:
            reasons.append(f"created before {rule.created_before}")
        else:
            failed.append(f"not created before {rule.created_before}")

    if rule.created_after:
        cutoff = parse_date_cutoff(rule.created_after, now=now)
        if meta.ctime >= cutoff:
            reasons.append(f"created after {rule.created_after}")
        else:
            failed.append(f"not created after {rule.created_after}")

    if rule.category:
        if category_matches(meta.category, rule.category):
            reasons.append(f"category {meta.category!r} matches {rule.category!r}")
        else:
            failed.append(f"category {meta.category!r} does not match {rule.category!r}")

    if rule.source_folder:
        pattern = rule.source_folder.strip().rstrip("/") or "."
        actual = meta.source_folder.strip().rstrip("/") or "."
        if fnmatch.fnmatchcase(actual.lower(), pattern.lower()):
            reasons.append(f"source_folder {actual!r} matches {pattern!r}")
        else:
            failed.append(f"source_folder {actual!r} does not match {pattern!r}")

    if not reasons and not failed:
        reasons.append("unconditional rule")

    matched = len(failed) == 0
    return RuleEvaluation(
        rule_id=rule.id,
        rule_name=rule.name,
        priority=rule.priority,
        enabled=True,
        matched=matched,
        reasons=reasons,
        failed_conditions=failed,
    )


def expand_destination(template: str, meta: FileMetadata) -> str:
    """Expand ``{year}``, ``{month}``, ``{day}``, ``{ext}``, ``{category}`` safely."""
    validated = validate_destination_template(template)
    mtime_dt = dt.datetime.fromtimestamp(meta.mtime, tz=dt.UTC)
    ext_clean = meta.extension.lstrip(".") or "no_ext"
    expanded = validated.format(
        year=f"{mtime_dt.year:04d}",
        month=f"{mtime_dt.month:02d}",
        day=f"{mtime_dt.day:02d}",
        ext=ext_clean,
        category=meta.category,
    )
    return validate_destination_template(expanded)


def match_file(
    meta: FileMetadata,
    rules: Sequence[Rule],
    *,
    now: float | None = None,
    include_evaluations: bool = False,
) -> MatchDecision:
    """Match a file deterministically against sorted rules, falling back to default category."""
    sorted_rules = sorted(rules, key=lambda r: (r.priority, r.id))
    evaluations: list[RuleEvaluation] = []
    winner: tuple[Rule, RuleEvaluation] | None = None

    for rule in sorted_rules:
        ev = evaluate_rule(rule, meta, now=now)
        if include_evaluations:
            evaluations.append(ev)
        if ev.matched and winner is None:
            winner = (rule, ev)
            if not include_evaluations:
                break

    if winner is not None:
        rule, ev = winner
        dest_folder = expand_destination(rule.destination, meta)
        reason_detail = ", ".join(ev.reasons)
        return MatchDecision(
            matched_by="rule",
            rule_id=rule.id,
            rule_name=rule.name,
            category=meta.category,
            destination_folder=dest_folder,
            reason=f"Rule '{rule.name}' ({rule.id}): {reason_detail}",
            evaluations=evaluations,
        )

    ext_label = meta.extension or "(no extension)"
    return MatchDecision(
        matched_by="default_category",
        rule_id=None,
        rule_name=None,
        category=meta.category,
        destination_folder=meta.category,
        reason=f"Default category '{meta.category}' for extension {ext_label}",
        evaluations=evaluations,
    )


class RuleStore:
    """Manages loading, validating, and saving rules in the tool's own directory."""

    def __init__(self, paths: ToolPaths) -> None:
        self.paths = paths

    @property
    def path(self) -> Path:
        cfg_rules = self.paths.config_dir / "rules.json"
        data_rules = self.paths.data_dir / "rules.json"
        if not cfg_rules.exists() and data_rules.exists():
            return data_rules
        return cfg_rules

    def load(self) -> list[Rule]:
        """Load and validate all rules, sorted by ``(priority, id)``."""
        target = self.path
        if not target.exists():
            return []
        try:
            text = target.read_text(encoding="utf-8")
            raw = json.loads(text)
        except (OSError, UnicodeDecodeError) as exc:
            raise DataError(
                f"Cannot read rules file {target}: {exc}",
                hint="Check file permissions or fix the rules JSON file.",
            ) from exc
        except json.JSONDecodeError as exc:
            raise DataError(
                f"Rules file is not valid JSON: {target} ({exc.msg}, line {exc.lineno})",
                hint="Fix the JSON syntax in rules.json.",
            ) from exc

        if isinstance(raw, Mapping):
            raw_list = raw.get("rules", [])
        elif isinstance(raw, list):
            raw_list = raw
        else:
            raise DataError(
                f"Rules file must contain a JSON list or an object with 'rules': {target}"
            )

        if not isinstance(raw_list, list):
            raise DataError(f"'rules' in {target} must be a JSON list.")

        rules: list[Rule] = []
        seen_ids: set[str] = set()
        for item in raw_list:
            rule = Rule.from_dict(item)
            if rule.id in seen_ids:
                raise DataError(f"Duplicate rule id {rule.id!r} in {target}.")
            seen_ids.add(rule.id)
            rules.append(rule)

        rules.sort(key=lambda r: (r.priority, r.id))
        return rules

    def save(self, rules: Sequence[Rule]) -> None:
        """Validate and atomically write rules to disk."""
        validated = [r.validate() for r in rules]
        validated.sort(key=lambda r: (r.priority, r.id))
        payload = {"version": 1, "rules": [r.to_dict() for r in validated]}
        atomic_write_text(self.path, json.dumps(payload, indent=2) + "\n")

    def add(self, rule: Rule) -> Rule:
        """Add a validated rule and persist."""
        rule.validate()
        existing = self.load()
        if any(r.id == rule.id for r in existing):
            raise UsageError(
                f"A rule with id {rule.id!r} already exists.",
                hint=f"Use another --id or run 'fileflow rules remove {rule.id} --yes' first.",
            )
        existing.append(rule)
        self.save(existing)
        return rule

    def remove(self, rule_id: str) -> Rule:
        """Remove a rule by ID and persist; raises NotFoundError if missing."""
        existing = self.load()
        kept: list[Rule] = []
        removed: Rule | None = None
        for r in existing:
            if r.id == rule_id:
                removed = r
            else:
                kept.append(r)
        if removed is None:
            raise NotFoundError(
                f"Rule not found: {rule_id!r}",
                hint="Run 'fileflow rules list' to see configured rule IDs.",
            )
        self.save(kept)
        return removed

    def next_id(self, name: str) -> str:
        """Generate a short, stable rule ID derived from ``name``."""
        existing_ids = {r.id for r in self.load()}
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "rule"
        slug = slug[:24].rstrip("-")
        if slug not in existing_ids:
            return slug
        counter = 2
        while f"{slug}-{counter}" in existing_ids:
            counter += 1
        return f"{slug}-{counter}"
