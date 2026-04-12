#!/usr/bin/env python3
"""Clean pipeline data artifacts.

Wipes contents of data directories: discovery_runs, tailored_resumes,
tailored_cover_letters, debug_captures. Sessions are never cleaned
automatically (they take effort to rebuild).

Usage:
    # Interactive: pick which directories to clean
    python scripts/clean_data.py

    # Wipe everything (no prompt)
    python scripts/clean_data.py --all

    # Wipe specific directories
    python scripts/clean_data.py --discovery --resumes --covers

    # Wipe debug captures only
    python scripts/clean_data.py --debug

    # Also wipe the file-based dedup store
    python scripts/clean_data.py --all --dedup

    # Preview what would be deleted
    python scripts/clean_data.py --all --dry-run
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DATA = _REPO_ROOT / "data"

TARGETS: dict[str, Path] = {
    "discovery": _DATA / "discovery_runs",
    "resumes": _DATA / "tailored_resumes",
    "covers": _DATA / "tailored_cover_letters",
    "debug": _DATA / "debug_captures",
}

DEDUP_FILE = _DATA / "dedup_seen.json"


def _count_items(path: Path) -> tuple[int, int]:
    """Return (file_count, total_bytes) under path."""
    if not path.exists():
        return 0, 0
    files = list(path.rglob("*"))
    file_count = sum(1 for f in files if f.is_file())
    total_bytes = sum(f.stat().st_size for f in files if f.is_file())
    return file_count, total_bytes


def _fmt_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} {unit}"
        n /= 1024  # type: ignore[assignment]
    return f"{n:.1f} TB"


def _wipe_dir(path: Path, *, dry_run: bool) -> int:
    if not path.exists():
        return 0
    files, size = _count_items(path)
    if files == 0:
        return 0
    if dry_run:
        print(f"  [dry-run] would delete {files} files ({_fmt_size(size)}) from {path.name}/")
    else:
        shutil.rmtree(path)
        path.mkdir(parents=True, exist_ok=True)
        print(f"  cleaned {path.name}/ — {files} files ({_fmt_size(size)})")
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


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Clean pipeline data artifacts.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--all", action="store_true", help="Wipe all directories (no prompt)")
    parser.add_argument("--discovery", action="store_true", help="Wipe data/discovery_runs/")
    parser.add_argument("--resumes", action="store_true", help="Wipe data/tailored_resumes/")
    parser.add_argument("--covers", action="store_true", help="Wipe data/tailored_cover_letters/")
    parser.add_argument("--debug", action="store_true", help="Wipe data/debug_captures/")
    parser.add_argument("--dedup", action="store_true", help="Also wipe dedup_seen.json")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be deleted")
    args = parser.parse_args()

    any_flag = args.all or args.discovery or args.resumes or args.covers or args.debug

    if not any_flag:
        print("Data directories:\n")
        for key, path in TARGETS.items():
            files, size = _count_items(path)
            status = f"{files} files, {_fmt_size(size)}" if files else "empty"
            print(f"  {key:12s}  {path.name:30s}  {status}")

        dedup_status = (
            f"{_fmt_size(DEDUP_FILE.stat().st_size)}" if DEDUP_FILE.exists() else "not present"
        )
        print(f"  {'dedup':12s}  {'dedup_seen.json':30s}  {dedup_status}")

        print()
        resp = input("Clean all? [y/N/list e.g. 'discovery,debug']: ").strip().lower()
        if not resp or resp == "n":
            print("Cancelled.")
            return
        if resp in ("y", "yes", "all"):
            selected = list(TARGETS.keys())
        else:
            selected = [t.strip() for t in resp.split(",") if t.strip() in TARGETS]
            if not selected:
                print(f"No valid targets in: {resp}")
                return
    elif args.all:
        selected = list(TARGETS.keys())
    else:
        selected = []
        if args.discovery:
            selected.append("discovery")
        if args.resumes:
            selected.append("resumes")
        if args.covers:
            selected.append("covers")
        if args.debug:
            selected.append("debug")

    total = 0
    print()
    for key in selected:
        total += _wipe_dir(TARGETS[key], dry_run=args.dry_run)

    if args.dedup:
        total += _wipe_file(DEDUP_FILE, dry_run=args.dry_run)

    if total == 0:
        print("  Nothing to clean.")
    elif not args.dry_run:
        print(f"\n  Done. Removed {total} files total.")


if __name__ == "__main__":
    main()
