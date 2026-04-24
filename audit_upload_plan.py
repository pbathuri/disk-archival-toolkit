#!/usr/bin/env python3
"""
Audit file_inventory.csv and produce a precise archive manifest within a size budget.

- Streams the CSV (memory-safe for multi-million rows).
- Applies safety exclusions (Trash, .git, .ssh, node_modules, etc.).
- Default eligibility: remote tiers cold_archive + warm_remote (regular files only).
- Optional --aggressive: add large, stale media/archives scored keep_local.
- If total eligible size exceeds --budget-tb, selects a maximum-bytes subset (sort by size, greedy fill).

Outputs:
  - Console summary (counts, bytes, tier/category breakdown).
  - upload_manifest.csv (paths to archive — use move_from_inventory.py for an external disk
    or upload_from_inventory.py for Google Drive).
"""

from __future__ import annotations

import argparse
import csv
import os
import sqlite3
import sys
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Optional

# Default: 3 TB in decimal bytes (many providers quote TB as 10^12).
DEFAULT_BUDGET_BYTES = 3_000_000_000_000

EXCLUDE_PATH_SUBSTRINGS = (
    "/.Trash/",
    "/Library/Caches/",
    "/Library/Containers/",   # macOS app sandbox data — system-managed, essential
    "/node_modules/",
    "/.git/",
    "/.ssh/",
    "/.gnupg/",
    "/Library/Keychains/",
    "/Library/Mail/",
    "/Library/Messages/",
    "/Parallels/",  # VM disks
    ".vmwarevm/",
    ".vagrant/",
    "/.Spotlight-V100/",      # Spotlight index — system-managed
)

EXCLUDE_NAMES = frozenset({
    ".DS_Store",
    "token.json",
    "client_secret",
})


def human_bytes(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if abs(n) < 1024.0 or unit == "PiB":
            return f"{n:.2f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024.0
    return f"{n:.2f} PiB"


def parse_float(s: str) -> Optional[float]:
    s = (s or "").strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def should_exclude_path(path: str, name: str) -> Optional[str]:
    pl = path
    low = pl.lower()
    for sub in EXCLUDE_PATH_SUBSTRINGS:
        if sub.lower() in low:
            return f"path_contains:{sub}"
    if name in EXCLUDE_NAMES:
        return f"name:{name}"
    for frag in EXCLUDE_NAMES:
        if frag.lower() in name.lower():
            return f"name_pattern:{frag}"
    return None


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Audit inventory CSV for archive planning (local disk or Google Drive).")
    ap.add_argument(
        "--inventory",
        type=Path,
        default=Path(__file__).resolve().parent / "file_inventory.csv",
        help="Path to file_inventory.csv",
    )
    ap.add_argument(
        "--budget-tb",
        type=float,
        default=3.0,
        help="Maximum total upload size in terabytes (decimal TB, 1 TB = 1e12 bytes).",
    )
    ap.add_argument(
        "--budget-bytes",
        type=int,
        default=0,
        help="Override budget in bytes (0 = derive from --budget-tb).",
    )
    ap.add_argument(
        "--min-size-mb",
        type=float,
        default=0.25,
        help="Skip files smaller than this (reduces noise; default 256 KiB).",
    )
    ap.add_argument(
        "--aggressive",
        action="store_true",
        help="Also include keep_local media/archive over --aggressive-min-mb stale for --aggressive-min-days.",
    )
    ap.add_argument("--aggressive-min-mb", type=float, default=200.0)
    ap.add_argument("--aggressive-min-days-atime", type=float, default=60.0)
    ap.add_argument(
        "--manifest-out",
        type=Path,
        default=Path(__file__).resolve().parent / "upload_manifest.csv",
    )
    ap.add_argument(
        "--skip-manifest",
        action="store_true",
        help="Only print summary (no manifest file).",
    )
    args = ap.parse_args()

    budget = int(args.budget_bytes) if args.budget_bytes > 0 else int(
        args.budget_tb * 1_000_000_000_000)
    min_bytes = int(args.min_size_mb * 1024 * 1024)
    min_aggr_bytes = int(args.aggressive_min_mb * 1024 * 1024)

    if not args.inventory.is_file():
        print(f"Missing inventory: {args.inventory}", file=sys.stderr)
        return 1

    # Pass 1: streaming stats + populate SQLite for packing
    tmp = tempfile.NamedTemporaryFile(
        prefix="inv_audit_", suffix=".sqlite", delete=False)
    tmp.close()
    db_path = tmp.name
    try:
        conn = sqlite3.connect(db_path)
        conn.execute(
            """CREATE TABLE eligible (
                path TEXT PRIMARY KEY,
                size_bytes INTEGER NOT NULL,
                tier TEXT,
                category TEXT,
                mtime_ym TEXT,
                git_repo_root TEXT,
                reason TEXT
            )"""
        )
        conn.execute("CREATE INDEX idx_sz ON eligible(size_bytes)")

        stats_rows = defaultdict(int)
        stats_bytes = defaultdict(int)
        skipped = defaultdict(int)
        scanned = 0

        batch: list[tuple] = []
        BATCH = 8000

        def flush() -> None:
            nonlocal batch
            if not batch:
                return
            conn.executemany(
                "INSERT OR REPLACE INTO eligible(path, size_bytes, tier, category, mtime_ym, git_repo_root, reason) VALUES (?,?,?,?,?,?,?)",
                batch,
            )
            conn.commit()
            batch = []

        with args.inventory.open(newline="", encoding="utf-8", errors="replace") as f:
            reader = csv.DictReader(f)
            for row in reader:
                scanned += 1
                if scanned % 500_000 == 0:
                    print(f"… scanned {scanned:,} rows", file=sys.stderr)

                if (row.get("is_directory") or "").strip().lower() in ("true", "1"):
                    skipped["dir"] += 1
                    continue
                if (row.get("is_symlink") or "").strip().lower() in ("true", "1"):
                    skipped["symlink"] += 1
                    continue
                if (row.get("error") or "").strip():
                    skipped["inventory_error"] += 1
                    continue

                path = (row.get("path") or "").strip()
                name = (row.get("name") or "").strip()
                if not path:
                    skipped["empty_path"] += 1
                    continue

                ex = should_exclude_path(path, name)
                if ex:
                    skipped[f"exclude:{ex}"] += 1
                    continue

                try:
                    size = int(row.get("size_bytes") or 0)
                except ValueError:
                    skipped["bad_size"] += 1
                    continue
                if size < min_bytes:
                    skipped["under_min_size"] += 1
                    continue

                tier = (row.get("remote_tier_suggestion") or "").strip()
                cat = (row.get("file_category") or "").strip()
                ym = (row.get("mtime_year_month") or "").strip()
                git_root = (row.get("git_repo_root") or "").strip()
                d_atime = parse_float(row.get("days_since_atime") or "")

                reason = ""
                include = False
                if tier in ("cold_archive", "warm_remote"):
                    include = True
                    reason = f"tier:{tier}"
                elif args.aggressive and tier == "keep_local":
                    if cat in ("media", "archive") and size >= min_aggr_bytes:
                        if d_atime is not None and d_atime >= args.aggressive_min_days_atime:
                            include = True
                            reason = "aggressive:stale_large_media_or_archive"

                if not include:
                    skipped[f"tier_not_selected:{tier or 'empty'}"] += 1
                    continue

                stats_rows["eligible_raw"] += 1
                stats_bytes["eligible_raw"] += size
                stats_rows[f"tier:{tier}"] += 1
                stats_bytes[f"tier:{tier}"] += size
                stats_rows[f"cat:{cat}"] += 1
                stats_bytes[f"cat:{cat}"] += size

                batch.append((path, size, tier, cat, ym, git_root, reason))
                if len(batch) >= BATCH:
                    flush()

        flush()

        total_eligible = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(size_bytes),0) FROM eligible").fetchone()
        assert total_eligible is not None
        n_elig, bytes_elig = int(total_eligible[0]), int(total_eligible[1])

        n_planned = 0
        running = 0
        manifest_header = [
            "path",
            "size_bytes",
            "remote_tier_suggestion",
            "file_category",
            "mtime_year_month",
            "git_repo_root",
            "plan_reason",
        ]

        mf = None
        w = None
        if not args.skip_manifest:
            args.manifest_out.parent.mkdir(parents=True, exist_ok=True)
            mf = args.manifest_out.open("w", newline="", encoding="utf-8")
            w = csv.writer(mf)
            w.writerow(manifest_header)

        sel_sql = "SELECT path, size_bytes, tier, category, mtime_ym, git_repo_root, reason FROM eligible"

        def emit_row(tup: tuple) -> None:
            nonlocal n_planned, running
            path, sz, tier, cat, ym, git_root, reason = (
                tup[0],
                int(tup[1]),
                tup[2],
                tup[3],
                tup[4],
                tup[5],
                tup[6],
            )
            if w is not None:
                w.writerow([path, sz, tier, cat, ym, git_root, reason])
            n_planned += 1
            running += sz

        if bytes_elig <= budget:
            for tup in conn.execute(f"{sel_sql} ORDER BY size_bytes DESC"):
                emit_row(tup)
        else:
            # Two-pass fill: large files first, then smallest remaining files to soak slack (not NP-optimal, much better than large-only).
            selected: set[str] = set()
            for tup in conn.execute(f"{sel_sql} ORDER BY size_bytes DESC"):
                sz = int(tup[1])
                if running + sz <= budget:
                    emit_row(tup)
                    selected.add(tup[0])
            for tup in conn.execute(f"{sel_sql} ORDER BY size_bytes ASC"):
                if tup[0] in selected:
                    continue
                sz = int(tup[1])
                if running + sz <= budget:
                    emit_row(tup)
                    selected.add(tup[0])

        if mf is not None:
            mf.close()
        conn.close()

        # Report
        print("=== Inventory audit (upload plan) ===\n")
        print(f"Inventory file:     {args.inventory}")
        print(f"Rows scanned:       {scanned:,}")
        print(
            f"Budget cap:         {human_bytes(budget)} ({args.budget_tb} TB decimal)")
        print(f"Min file size:      {args.min_size_mb} MiB")
        print(f"Aggressive mode:    {args.aggressive}")
        print()

        print("--- Eligibility (before budget trim) ---")
        print(f"Eligible files:     {n_elig:,}")
        print(
            f"Eligible bytes:     {human_bytes(bytes_elig)} ({bytes_elig:,} B)")
        print()

        if bytes_elig > budget:
            left = bytes_elig - running
            print(
                "--- Budget trim (two-pass: large files first, then small files to fill slack) ---")
            print(f"Planned files:      {n_planned:,}")
            print(
                f"Planned bytes:      {human_bytes(running)} ({running:,} B)")
            print(
                f"Left out (budget):  {human_bytes(left)} ({left:,} B)  ← raise --budget-tb or run another audit after upload")
            print()
        else:
            print("--- Within budget: all eligible rows are planned ---")
            print(f"Planned files:      {n_planned:,}")
            print(f"Planned bytes:      {human_bytes(running)}")
            print()

        print("--- Top tier breakdown (eligible raw) ---")
        for k in sorted(stats_rows.keys()):
            if k.startswith("tier:"):
                print(
                    f"  {k}: {stats_rows[k]:,} files, {human_bytes(stats_bytes[k])}")
        print()

        print("--- Top category breakdown (eligible raw) ---")
        cats = sorted((k for k in stats_rows if k.startswith(
            "cat:")), key=lambda x: -stats_bytes[x])[:15]
        for k in cats:
            print(
                f"  {k}: {stats_rows[k]:,} files, {human_bytes(stats_bytes[k])}")
        print()

        print("--- Skip reasons (sample counts) ---")
        for k, v in sorted(skipped.items(), key=lambda kv: -kv[1])[:25]:
            print(f"  {k}: {v:,}")
        if len(skipped) > 25:
            print(f"  … plus {len(skipped) - 25} more skip buckets")
        print()

        headroom = budget - running
        print(
            f"--- Budget utilization vs your {args.budget_tb:.1f} TB cap ---")
        print(
            f"Plan uses ~{100.0 * running / budget:.2f}% of budget ({human_bytes(headroom)} headroom remains).")
        print()

        if not args.skip_manifest and n_planned:
            print(f"Wrote manifest: {args.manifest_out} ({n_planned:,} rows)")
            print(
                "Review the manifest, then archive with either:\n"
                f"  External disk: .venv/bin/python move_from_inventory.py --dest-root /Volumes/YOUR_DISK/disk_cleanup_archive "
                f"--manifest {args.manifest_out} --execute\n"
                f"  Google Drive:  .venv/bin/python upload_from_inventory.py --manifest {args.manifest_out} --execute"
            )
        elif args.skip_manifest:
            print("Manifest skipped (--skip-manifest).")
        elif not n_planned:
            print("No eligible files; manifest not written.")

    finally:
        try:
            os.unlink(db_path)
        except OSError:
            pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
