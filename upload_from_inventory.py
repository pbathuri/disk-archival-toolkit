#!/usr/bin/env python3
"""
Upload files to Google Drive with a stable folder layout.

For archiving to an external disk (same layout, no cloud quota), use move_from_inventory.py.

Layout (under optional parent or My Drive root):
  {drive_root} / {mtime_year_month} / {file_category} / {project_slug} / {file}

Sources:
  - Full inventory CSV from scan.py (--inventory), filtered by --tiers, or
  - Precise manifest from audit_upload_plan.py (--manifest upload_manifest.csv).

Default is dry-run: pass --execute to upload.

First run opens a browser for Google consent; token.json is stored in this project.
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

from drive_client import build_service, ensure_folder_path, safe_drive_name, upload_local_file

PROJECT_DIR = Path(__file__).resolve().parent


def project_slug(git_repo_root: str) -> str:
    if not git_repo_root.strip():
        return "_no_project"
    return safe_drive_name(Path(git_repo_root).name, max_len=80)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Upload files to Google Drive from inventory or audit manifest.")
    p.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help="upload_manifest.csv from audit_upload_plan.py (takes precedence over --inventory).",
    )
    p.add_argument(
        "--inventory",
        type=Path,
        default=Path.home() / "file_inventory.csv",
        help="Path to file_inventory.csv from scan.py (ignored if --manifest is set).",
    )
    p.add_argument(
        "--tiers",
        default="cold_archive,warm_remote",
        help="Comma-separated remote_tier_suggestion values (inventory mode only).",
    )
    p.add_argument(
        "--drive-root",
        default="disk_cleanup_archive",
        help="Top-level folder name on Drive (created if missing).",
    )
    p.add_argument(
        "--parent-folder-id",
        default="",
        help="Optional Drive folder ID to create --drive-root inside (else My Drive root).",
    )
    p.add_argument(
        "--min-size-mb",
        type=float,
        default=0.0,
        help="Skip files smaller than this many MB (inventory mode only).",
    )
    p.add_argument(
        "--max-files",
        type=int,
        default=0,
        help="Stop after this many uploads (0 = no limit). For testing.",
    )
    p.add_argument(
        "--execute",
        action="store_true",
        help="Actually upload. Without this flag, only print the plan.",
    )
    p.add_argument(
        "--log-csv",
        type=Path,
        default=None,
        help="Append one row per attempted upload (local_path, drive_file_id or empty, status).",
    )
    p.add_argument(
        "--skip-uploaded",
        type=Path,
        default=None,
        metavar="LOG_CSV",
        help="Skip paths already recorded as 'ok' in this log CSV (for resuming interrupted sessions).",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    tiers = {t.strip() for t in args.tiers.split(",") if t.strip()}
    min_bytes = int(args.min_size_mb * 1024 * 1024)

    src = args.manifest if args.manifest is not None else args.inventory
    if not src.is_file():
        print(f"Input not found: {src}", file=sys.stderr)
        return 1

    # Build skip-set from a previous log CSV so interrupted sessions can resume.
    already_uploaded: set[str] = set()
    if args.skip_uploaded and args.skip_uploaded.is_file():
        with args.skip_uploaded.open(newline="", encoding="utf-8", errors="replace") as slf:
            for row in csv.DictReader(slf):
                if (row.get("status") or "").strip() == "ok":
                    p_val = (row.get("local_path") or "").strip()
                    if p_val:
                        already_uploaded.add(p_val)
        print(
            f"Skipping {len(already_uploaded)} already-uploaded path(s) from {args.skip_uploaded}", file=sys.stderr)

    service = None
    if args.execute:
        service = build_service(PROJECT_DIR)

    root_parent = args.parent_folder_id.strip() or None
    folder_cache: dict[tuple[str, ...], str] = {}
    # Tracks (device_id, inode) pairs seen in inventory mode to avoid uploading hard-linked duplicates.
    seen_inodes: set[tuple[str, str]] = set()

    def parent_for_row(row: dict) -> str:
        ym = (row.get("mtime_year_month") or "").strip() or "unknown_ym"
        cat = (row.get("file_category") or "other").strip() or "other"
        proj = project_slug(row.get("git_repo_root") or "")
        key = (args.drive_root, ym, cat, proj)
        if key not in folder_cache:
            if not args.execute:
                folder_cache[key] = f"<would_create:{'/'.join(key)}>"
            else:
                assert service is not None
                top = ensure_folder_path(
                    service, [args.drive_root], root_parent)
                rest_id = ensure_folder_path(service, [ym, cat, proj], top)
                folder_cache[key] = rest_id
        return folder_cache[key]

    n_plan = 0
    n_uploaded = 0
    log_rows: list[tuple[str, str, str]] = []

    with src.open(newline="", encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if args.manifest is not None:
                path_s = (row.get("path") or "").strip()
                if not path_s:
                    continue
                local = Path(path_s)
                if not local.is_file():
                    log_rows.append((path_s, "", "skip:not_found"))
                    continue
            else:
                if row.get("is_directory", "").strip().lower() in ("true", "1"):
                    continue
                if row.get("is_symlink", "").strip().lower() in ("true", "1"):
                    continue
                if (row.get("error") or "").strip():
                    continue
                tier = (row.get("remote_tier_suggestion") or "").strip()
                if tier not in tiers:
                    continue
                try:
                    size = int(row.get("size_bytes") or 0)
                except ValueError:
                    continue
                if size < min_bytes:
                    continue
                path_s = (row.get("path") or "").strip()
                if not path_s:
                    continue
                local = Path(path_s)
                if not local.is_file():
                    log_rows.append((path_s, "", "skip:not_found"))
                    continue
                # Deduplicate hard links by (device_id, inode).
                dev = (row.get("device_id") or "").strip()
                ino = (row.get("inode") or "").strip()
                if dev and ino:
                    inode_key = (dev, ino)
                    if inode_key in seen_inodes:
                        log_rows.append(
                            (path_s, "", "skip:hardlink_duplicate"))
                        continue
                    seen_inodes.add(inode_key)

            # Skip paths already successfully uploaded in a previous session.
            if path_s in already_uploaded:
                log_rows.append((path_s, "", "skip:already_uploaded"))
                continue

            n_plan += 1
            parent_id = parent_for_row(row)
            desc = f"source_path:{path_s}"

            if not args.execute:
                rel = "/".join(
                    (
                        args.drive_root,
                        row.get("mtime_year_month") or "unknown_ym",
                        (row.get("file_category") or "other"),
                        project_slug(row.get("git_repo_root") or ""),
                    )
                )
                print(f"[dry-run] {path_s} -> …/{rel}/{local.name}")
                log_rows.append((path_s, "", "dry_run"))
            else:
                try:
                    fid = upload_local_file(
                        service, local, parent_id, description=desc)
                    print(f"OK {path_s} -> id:{fid}")
                    log_rows.append((path_s, fid, "ok"))
                    n_uploaded += 1
                except Exception as e:
                    print(f"FAIL {path_s}: {e}", file=sys.stderr)
                    log_rows.append((path_s, "", f"error:{e}"))

            if args.max_files and n_plan >= args.max_files:
                break

    if not args.execute:
        print(
            f"\nDry-run: {n_plan} file(s) would upload. Pass --execute to run.", file=sys.stderr)
    else:
        print(f"\nUploaded {n_uploaded} / planned {n_plan}", file=sys.stderr)

    if args.log_csv:
        args.log_csv.parent.mkdir(parents=True, exist_ok=True)
        write_header = not args.log_csv.is_file()
        with args.log_csv.open("a", newline="", encoding="utf-8") as lf:
            w = csv.writer(lf)
            if write_header:
                w.writerow(["local_path", "drive_file_id", "status"])
            w.writerows(log_rows)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
