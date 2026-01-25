#!/usr/bin/env python3
"""Prune translated counterparts for deleted source files.

This script is a small, dependency-free helper used by CI to keep
`rose/translations` aligned with the default branch when source files are
deleted.

It computes deleted files from a `git diff base..head` and removes the
corresponding translated files (e.g. `zh/...`) if they exist.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Iterable


LANGUAGE_CODE_PATTERN = re.compile(r"^[A-Za-z]{2}(?:[-_][A-Za-z]{2})?$")
ALLOWED_EXTENSIONS = {
    ".md",
    ".markdown",
    ".mkd",
    ".html",
    ".jinja",
    ".jinja2",
    ".j2",
    ".tpl",
    ".yml",
    ".yaml",
}

EXCLUDED_PREFIXES = (
    ".github/",
    "translation-workflow/scripts/",
    "i18n/",
    "images/",
    "scripts/",
    "locale/",
)
EXCLUDED_EXACT = {
    ".github",
    "translation-workflow/scripts",
    "i18n",
    "images",
    "scripts",
    "locale",
    "readme.md",
    "variables.yml",
}


def _repo_relative_str(path: str, repo_root: Path) -> str:
    value = str(path).replace("\\", "/").lstrip("/")
    value = str(Path(value)).replace("\\", "/")
    resolved = (repo_root / value).resolve()
    try:
        return resolved.relative_to(repo_root.resolve()).as_posix()
    except Exception:
        return value


def _parse_languages(raw: str) -> list[str]:
    raw = (raw or "").strip()
    if not raw:
        return []
    if raw.startswith("["):
        parsed = json.loads(raw)
        if not isinstance(parsed, list):
            raise ValueError("ROSE_LANGUAGES must be a JSON array string or whitespace list")
        langs = [str(item).strip() for item in parsed]
    else:
        langs = re.split(r"[,\s]+", raw)
        langs = [item.strip() for item in langs]
    normalized: list[str] = []
    for lang in langs:
        if not lang:
            continue
        candidate = lang.lower()
        if not LANGUAGE_CODE_PATTERN.match(candidate):
            continue
        normalized.append(candidate)
    return sorted(set(normalized))


def _is_allowed_file(rel_path: str) -> bool:
    return Path(rel_path).suffix.lower() in ALLOWED_EXTENSIONS


def _should_skip_path(rel_path: str, languages: list[str]) -> bool:
    normalized = str(rel_path).replace("\\", "/").lstrip("/")
    lower = normalized.lower()
    if lower in EXCLUDED_EXACT:
        return True
    for prefix in EXCLUDED_PREFIXES:
        if lower.startswith(prefix):
            return True
    if not _is_allowed_file(lower):
        return True
    parts = Path(lower).parts
    for lang in languages:
        if parts and parts[0] == lang:
            return True
    return False


def _derive_target_path(rel_path: str, language: str) -> str:
    path = Path(str(rel_path).replace("\\", "/").lstrip("/"))
    parts = list(path.parts)

    if parts and parts[0] == "locale":
        rest = parts[1:]
        if not rest:
            return str(Path("locale", f"{language}.yml"))
        filename = Path(rest[-1])
        new_name = f"{language}{filename.suffix or '.yml'}"
        return str(Path("locale", *rest[:-1], new_name))

    if ".translations" in parts:
        idx = parts.index(".translations")
        before = parts[: idx + 1]
        filename = parts[-1] if parts else ""
        suffix = Path(filename).suffix
        new_name = f"{language}{suffix or '.json'}"
        new_parts = before + parts[idx + 1 : -1] + [new_name]
        return str(Path(*new_parts))

    if parts and parts[0] == language:
        return str(path)
    if parts and parts[0] == "en":
        parts = parts[1:]
    return str(Path(language, *parts))


def _collect_deleted_files(repo_root: Path, base: str, head: str, paths: Iterable[str]) -> list[str]:
    cmd = [
        "git",
        "-C",
        str(repo_root),
        "diff",
        "--name-only",
        "--diff-filter=D",
        "-z",
        f"{base}..{head}",
        "--",
    ]
    cmd.extend(str(path) for path in paths)
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    if not result.stdout:
        return []
    return [item for item in result.stdout.split("\0") if item]


def prune_deleted_translations(
    repo_root: Path,
    deleted_files: Iterable[str],
    languages: list[str],
    *,
    dry_run: bool,
) -> tuple[list[str], dict[str, list[str]]]:
    pruned_sources: list[str] = []
    pruned_by_lang: dict[str, list[str]] = {}
    seen: set[str] = set()

    for rel_path in deleted_files:
        if not rel_path:
            continue
        normalized = _repo_relative_str(rel_path, repo_root)
        if normalized in seen:
            continue
        seen.add(normalized)
        if _should_skip_path(normalized, languages):
            continue
        pruned_sources.append(normalized)
        for lang in languages:
            target_rel = _repo_relative_str(_derive_target_path(normalized, lang), repo_root)
            target_abs = repo_root / target_rel
            if target_abs.is_file() or target_abs.is_symlink():
                if not dry_run:
                    target_abs.unlink()
                pruned_by_lang.setdefault(lang, []).append(target_rel)

    pruned_sources.sort()
    for lang, paths in pruned_by_lang.items():
        pruned_by_lang[lang] = sorted(set(paths))
    return pruned_sources, pruned_by_lang


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", default=".", help="Repository root (where git runs)")
    parser.add_argument("--base", required=True, help="Base git ref/sha")
    parser.add_argument("--head", required=True, help="Head git ref/sha")
    parser.add_argument("--paths", nargs="*", default=["."], help="Paths to inspect for deletions")
    parser.add_argument("--languages", nargs="*", default=None, help="Languages (default: ROSE_LANGUAGES)")
    parser.add_argument("--dry-run", action="store_true", help="Print what would be pruned without deleting")
    args = parser.parse_args()

    repo_root = Path(args.repo_root).resolve()
    base = (args.base or "").strip()
    head = (args.head or "").strip()
    if not base or base == "0000000000000000000000000000000000000000":
        print("[rose][prune] base ref missing/zero; skipping")
        return 0
    if not head or head == "0000000000000000000000000000000000000000":
        print("[rose][prune] head ref missing/zero; skipping")
        return 0

    if args.languages is not None and args.languages:
        languages = [lang.strip().lower() for lang in args.languages if lang.strip()]
    else:
        languages = _parse_languages(os.environ.get("ROSE_LANGUAGES", ""))
    if not languages:
        print("[rose][prune] no languages configured; skipping")
        return 0

    deleted_files = _collect_deleted_files(repo_root, base, head, args.paths)
    pruned_sources, pruned_by_lang = prune_deleted_translations(
        repo_root, deleted_files, languages, dry_run=args.dry_run
    )
    total = sum(len(paths) for paths in pruned_by_lang.values())
    mode = "DRY RUN" if args.dry_run else "PRUNED"
    print(
        f"[rose][prune] {mode}: removed {total} translated file(s) for {len(pruned_sources)} deleted source file(s)"
    )
    for lang, paths in sorted(pruned_by_lang.items()):
        if not paths:
            continue
        print(f"[rose][prune] {lang}: {len(paths)} file(s)")
        for path in paths[:20]:
            print(f"  - {path}")
        if len(paths) > 20:
            print("  - ...")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

