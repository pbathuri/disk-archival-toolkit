#!/usr/bin/env python3
"""
CLI: filesystem inventory to CSV (metadata + local vs remote heuristics).

Example:
  python3 scan.py --roots ~ -o ~/Desktop/file_inventory.csv
  python3 scan.py --roots / --exclude-prefix /System/Volumes/Data/.MobileBackups \\
      --errors ~/inventory_errors.csv -o ~/full_inventory.csv

On macOS, scanning protected paths may require Full Disk Access for Python/Terminal.
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from dataclasses import asdict, fields
from pathlib import Path
from typing import Optional

from inventory_core import FileRecord, iter_files, stat_record


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Inventory files to CSV (metadata + local/remote heuristics).")
    p.add_argument(
        "--roots",
        nargs="+",
        default=[str(Path.home())],
        help="One or more root paths to scan (default: home directory).",
    )
    p.add_argument(
        "--output",
        "-o",
        type=Path,
        default=Path.home() / "file_inventory.csv",
        help="Output CSV path.",
    )
    p.add_argument(
        "--errors",
        type=Path,
        default=None,
        help="Optional CSV path for scan errors (path, message).",
    )
    p.add_argument(
        "--exclude-prefix",
        action="append",
        default=[],
        help="Skip paths starting with this prefix (repeatable).",
    )
    p.add_argument(
        "--files-only",
        action="store_true",
        help="Only emit rows for regular files (skip directory rows).",
    )
    return p.parse_args(argv)


def should_skip(path: Path, exclude_prefixes: list[str]) -> bool:
    s = str(path)
    for pref in exclude_prefixes:
        if not pref:
            continue
        pref_n = pref.rstrip("/")
        if s == pref_n or s.startswith(pref_n + "/"):
            return True
    return False


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    roots = [Path(r) for r in args.roots]
    exclude = list(args.exclude_prefix)
    error_rows: list[tuple[str, str]] = []

    def on_error(p: str, msg: str) -> None:
        error_rows.append((p, msg))

    out = args.output
    out.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [f.name for f in fields(FileRecord)]

    now = time.time()
    git_cache: dict[str, str] = {}
    n = 0

    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for scan_root, path, scan_err in iter_files(roots, on_error=on_error):
            if should_skip(path, exclude):
                continue
            rec = stat_record(scan_root, path, now,
                              git_cache, scan_error=scan_err)
            if args.files_only and rec.is_directory == "True":
                continue
            w.writerow(asdict(rec))
            n += 1
            if n % 50000 == 0:
                print(f"… {n} rows", file=sys.stderr)

    print(f"Wrote {n} rows to {out}", file=sys.stderr)

    if args.errors and error_rows:
        args.errors.parent.mkdir(parents=True, exist_ok=True)
        with args.errors.open("w", newline="", encoding="utf-8") as ef:
            ew = csv.writer(ef)
            ew.writerow(["path", "message"])
            ew.writerows(error_rows)
        print(f"Wrote {len(error_rows)} errors to {args.errors}",
              file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
