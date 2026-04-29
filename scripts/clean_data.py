#!/usr/bin/env python3
"""Clean pipeline data artifacts.

Wipes contents of data directories produced by pipeline runs. Sessions are
never cleaned automatically — they take time to warm up and survive reboots.
The platform database (platform.db) is also excluded by default.

Usage:
    # Interactive: show sizes, then prompt
    python scripts/clean_data.py

    # Wipe everything (no prompt)
    python scripts/clean_data.py --all

    # Wipe specific groups
    python scripts/clean_data.py --discovery --resumes --covers

    # Wipe all debug/capture artifacts (both discovery and application)
    python scripts/clean_data.py --debug

    # Wipe pipeline logs only
    python scripts/clean_data.py --logs

    # Also reset dedup stores (forces re-evaluation of all listings)
    python scripts/clean_data.py --all --dedup

    # Preview without deleting
    python scripts/clean_data.py --all --dry-run
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DATA = _REPO_ROOT / "data"

# Directories wiped by their named flag (and by --all).
# "debug" covers both discovery and application captures.
TARGETS: dict[str, list[Path]] = {
    "discovery": [_DATA / "discovery_runs"],
    "resumes":   [_DATA / "tailored_resumes"],
    "covers":    [_DATA / "tailored_cover_letters"],
    "debug":     [
        _DATA / "debug_captures",       # discovery browser failures
        _DATA / "application_debug",    # application failure screenshots/HTML
    ],
    "logs":      [_DATA / "logs"],
    "app_state": [_DATA / "application_state"],  # LinkedIn daily-cap counters
}

# Files wiped when --dedup is passed.
DEDUP_FILES: list[Path] = [
    _DATA / "dedup_seen.json",      # discovery dedup (file-based)
    _DATA / "application_dedup.db", # application dedup (SQLite)
]

# Intentionally excluded from all wipes:
#   data/sessions/   — browser sessions (slow to rebuild)
#   data/platform.db — main SQLite schema + job records


def _count_items(path: Path) -> tuple[int, int]:
    """Return (file_count, total_bytes) under path."""
    if not path.exists():
        return 0, 0
    files = [f for f in path.rglob("*") if f.is_file()]
    return len(files), sum(f.stat().st_size for f in files)


def _fmt_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024  # type: ignore[assignment]
    return f"{n:.1f} TB"


def _wipe_dir(path: Path, *, dry_run: bool) -> int:
    if not path.exists():
        return 0
    files, size = _count_items(path)
    if files == 0:
        return 0
    if dry_run:
        print(f"  [dry-run] would delete {files} files ({_fmt_size(size)}) from {path.relative_to(_DATA)}/")
    else:
        shutil.rmtree(path)
        path.mkdir(parents=True, exist_ok=True)
        print(f"  cleaned {path.relative_to(_DATA)}/ — {files} files ({_fmt_size(size)})")
    return files


def _wipe_file(path: Path, *, dry_run: bool) -> int:
    if not path.exists():
        return 0
    size = path.stat().st_size
    if dry_run:
        print(f"  [dry-run] would delete {path.name} ({_fmt_size(size)})")
    else:
        path.unlink()
        print(f"  cleaned {path.name} ({_fmt_size(size)})")
    return 1


def _summarise() -> None:
    """Print a size summary of all cleanable targets."""
    print("Data directories:\n")
    for key, paths in TARGETS.items():
        total_files, total_bytes = 0, 0
        labels = []
        for p in paths:
            f, b = _count_items(p)
            total_files += f
            total_bytes += b
            labels.append(p.relative_to(_DATA))
        label = ", ".join(str(l) for l in labels)
        status = f"{total_files} files, {_fmt_size(total_bytes)}" if total_files else "empty"
        print(f"  {key:12s}  {label:45s}  {status}")

    print()
    print("Dedup files (wiped with --dedup):\n")
    for p in DEDUP_FILES:
        status = _fmt_size(p.stat().st_size) if p.exists() else "not present"
        print(f"  {'':12s}  {p.name:45s}  {status}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Clean pipeline data artifacts.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--all",       action="store_true", help="Wipe all directories (no prompt)")
    parser.add_argument("--discovery", action="store_true", help="Wipe data/discovery_runs/")
    parser.add_argument("--resumes",   action="store_true", help="Wipe data/tailored_resumes/")
    parser.add_argument("--covers",    action="store_true", help="Wipe data/tailored_cover_letters/")
    parser.add_argument("--debug",     action="store_true",
                        help="Wipe data/debug_captures/ and data/application_debug/")
    parser.add_argument("--logs",      action="store_true", help="Wipe data/logs/")
    parser.add_argument("--app-state", action="store_true",
                        help="Wipe data/application_state/ (daily cap counters)")
    parser.add_argument("--dedup",     action="store_true",
                        help="Also wipe dedup_seen.json and application_dedup.db")
    parser.add_argument("--dry-run",   action="store_true", help="Show what would be deleted")
    args = parser.parse_args()

    any_flag = args.all or args.discovery or args.resumes or args.covers or args.debug or args.logs or args.app_state

    if not any_flag:
        _summarise()
        print()
        resp = input("Clean all? [y/N/list e.g. 'debug,logs']: ").strip().lower()
        if not resp or resp == "n":
            print("Cancelled.")
            return
        if resp in ("y", "yes", "all"):
            selected = list(TARGETS.keys())
        else:
            selected = [t.strip() for t in resp.split(",") if t.strip() in TARGETS]
            if not selected:
                print(f"No valid targets in: {resp!r}. Valid: {', '.join(TARGETS)}")
                return
    elif args.all:
        selected = list(TARGETS.keys())
    else:
        selected = []
        if args.discovery: selected.append("discovery")
        if args.resumes:   selected.append("resumes")
        if args.covers:    selected.append("covers")
        if args.debug:     selected.append("debug")
        if args.logs:      selected.append("logs")
        if args.app_state: selected.append("app_state")

    total = 0
    print()
    for key in selected:
        for path in TARGETS[key]:
            total += _wipe_dir(path, dry_run=args.dry_run)

    if args.dedup:
        for f in DEDUP_FILES:
            total += _wipe_file(f, dry_run=args.dry_run)

    if total == 0:
        print("  Nothing to clean.")
    elif not args.dry_run:
        print(f"\n  Done. Removed {total} files total.")


if __name__ == "__main__":
    main()
