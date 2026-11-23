#!/usr/bin/env python3
"""
Move redundant files to a local archive root (e.g. external hard drive).

Uses the same folder layout as upload_from_inventory.py (without Google Drive):

  {archive_root} / {mtime_year_month} / {file_category} / {project_slug} / {filename}

Browsing the archive is ordered by modification month, then category, then project.

Sources:
  - upload_manifest.csv from audit_upload_plan.py (--manifest), or
  - file_inventory.csv from scan.py (--inventory), filtered by --tiers.

Default is dry-run: pass --execute to move files.

Safety:
  - Refuses to move a file that already lives under --dest-root.
  - Refuses if --dest-root lies inside a source path (misconfiguration).
  - Name collisions in the destination get a numeric suffix before the extension.
  - Copy uses file data only (no rich metadata) so exFAT/FAT32 volumes do not hit EINVAL from xattrs.
  - Filenames are sanitized for portable disks (exFAT): odd Unicode spaces, ?, *, :, etc.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import re
import shutil
import sys
import unicodedata
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent

# exFAT / cross-platform: narrow NBSP, BOM, other exotic spaces, control chars, Windows-forbidden.
_EXOTIC_SPACE = str.maketrans(
    {c: " " for c in "\u202f\u00a0\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007"
     "\u2008\u2009\u200a\u200b\u200c\u200d\ufeff\u2060\u180e\n\r\t\v\f"}
)
# macOS+exFAT also rejects * ? " < > | \ / and colon in some APIs; = & [ ] are risky
_FORBID_FILENAME = re.compile(r"[\x00-\x1f\x7f\"*/:<>?\\|]")


def safe_portable_filename(name: str, max_basename: int = 180) -> str:
    """Name safe for exFAT/FAT32 and precomposed UTF-8 on macOS."""
    s = unicodedata.normalize("NFC", (name or "").strip())
    s = s.translate(_EXOTIC_SPACE)
    s = s.replace(":", " -")
    s = s.replace("?", " ")
    s = s.replace("*", " ")
    s = s.replace("[", "(").replace("]", ")")
    s = s.replace("&", " and ")
    s = s.replace("=", "-")
    s = _FORBID_FILENAME.sub("_", s)
    s = re.sub(r"\.+$", "", s)
    s = s.rstrip(" .")
    s = re.sub(r"\s+", " ", s, flags=re.UNICODE).strip() or "unnamed"
    # Basename + extension length cap (exFAT: 255 UTF-16 units; cap chars conservatively)
    p = Path(s)
    ext = p.suffix
    stem = p.stem
    if len(s) > max_basename and max_basename > len(ext) + 1:
        take = max_basename - len(ext)
        if take > 0:
            s = (stem[:take] + ext)[:max_basename]
    if s in (".", "..", "") or not s.strip():
        s = f"file_{hashlib.sha256(name.encode('utf-8', errors='replace')).hexdigest()[:16]}"
    return s


def project_slug(git_repo_root: str) -> str:
    if not git_repo_root.strip():
        return "_no_project"
    n = safe_portable_filename(Path(git_repo_root).name, max_basename=80)
    return n or "_no_project"


def _safe_ym_cat(seg: str, default: str) -> str:
    t = safe_portable_filename((seg or "").strip() or default, max_basename=64)
    t = t.replace(" ", "_")
    return t or default


def copy_file_then_unlink_source(src: Path, dst: Path) -> None:
    """Write file data then delete source. Avoids shutil.copy2 / move xattr + metadata paths that
    often return Errno 22 (EINVAL) when the destination is exFAT and the source is APFS."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst, follow_symlinks=True)
    try:
        shutil.copystat(src, dst, follow_symlinks=True)
    except OSError:
        pass
    src.unlink()


def unique_dest_path(dest_dir: Path, filename: str) -> Path:
    """Pick dest_dir / filename, or dest_dir / name (n).ext if taken."""
    base = safe_portable_filename(filename, max_basename=200)
    candidate = dest_dir / base
    if not candidate.exists():
        return candidate
    stem = Path(base).stem
    suf = Path(base).suffix
    for n in range(2, 10_000):
        candidate = dest_dir / f"{stem} ({n}){suf}"
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"could not uniquify {dest_dir / base}")


def path_conflict(dest_root: Path, source: Path) -> str | None:
    """Return error message if this move would be unsafe, else None."""
    try:
        s = source.resolve()
        d = dest_root.resolve()
    except OSError as e:
        return f"resolve failed: {e}"
    try:
        s.relative_to(d)
        return "source is already under archive root"
    except ValueError:
        pass
    try:
        d.relative_to(s)
        return "archive root is inside source path"
    except ValueError:
        pass
    return None


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Move files from inventory or manifest to a local archive folder.",
    )
    p.add_argument(
        "--dest-root",
        type=Path,
        required=True,
        help="Top of archive on the external disk (e.g. /Volumes/MyDrive/disk_cleanup_archive). "
        "Created if missing when using --execute.",
    )
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
        "--min-size-mb",
        type=float,
        default=0.0,
        help="Skip files smaller than this many MB (inventory mode only).",
    )
    p.add_argument(
        "--max-files",
        type=int,
        default=0,
        help="Stop after this many moves (0 = no limit). For testing.",
    )
    p.add_argument(
        "--execute",
        action="store_true",
        help="Actually move files. Without this flag, only print the plan.",
    )
    p.add_argument(
        "--log-csv",
        type=Path,
        default=None,
        help="Append one row per attempted move (local_path, dest_path or empty, status).",
    )
    p.add_argument(
        "--skip-moved",
        type=Path,
        default=None,
        metavar="LOG_CSV",
        help="Skip paths already recorded as 'ok' in this log CSV (resume interrupted runs).",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    tiers = {t.strip() for t in args.tiers.split(",") if t.strip()}
    min_bytes = int(args.min_size_mb * 1024 * 1024)

    dest_root = args.dest_root.expanduser()
    if args.execute:
        dest_root.mkdir(parents=True, exist_ok=True)
    try:
        dest_canon = dest_root.resolve()
    except OSError:
        dest_canon = dest_root

    src = args.manifest if args.manifest is not None else args.inventory
    if not src.is_file():
        print(f"Input not found: {src}", file=sys.stderr)
        return 1

    already_done: set[str] = set()
    if args.skip_moved and args.skip_moved.is_file():
        with args.skip_moved.open(newline="", encoding="utf-8", errors="replace") as slf:
            for row in csv.DictReader(slf):
                if (row.get("status") or "").strip() == "ok":
                    p_val = (row.get("local_path") or "").strip()
                    if p_val:
                        already_done.add(p_val)
        print(
            f"Skipping {len(already_done)} path(s) already moved per {args.skip_moved}",
            file=sys.stderr,
        )

    seen_inodes: set[tuple[str, str]] = set()

    n_plan = 0
    n_moved = 0
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
                dev = (row.get("device_id") or "").strip()
                ino = (row.get("inode") or "").strip()
                if dev and ino:
                    inode_key = (dev, ino)
                    if inode_key in seen_inodes:
                        log_rows.append(
                            (path_s, "", "skip:hardlink_duplicate"))
                        continue
                    seen_inodes.add(inode_key)

            if path_s in already_done:
                log_rows.append((path_s, "", "skip:already_moved"))
                continue

            bad = path_conflict(dest_canon, local)
            if bad:
                log_rows.append((path_s, "", f"skip:unsafe:{bad}"))
                continue

            ym = _safe_ym_cat((row.get("mtime_year_month")
                              or "").strip(), "unknown_ym")
            cat = _safe_ym_cat(
                (row.get("file_category") or "").strip(), "other")
            proj = project_slug(row.get("git_repo_root") or "")
            dest_dir = dest_root / ym / cat / proj

            n_plan += 1
            final_path = unique_dest_path(dest_dir, local.name)

            if not args.execute:
                print(f"[dry-run] {path_s} -> {final_path}")
                log_rows.append((path_s, str(final_path), "dry_run"))
            else:
                try:
                    copy_file_then_unlink_source(local, final_path)
                    print(f"OK {path_s} -> {final_path}")
                    log_rows.append((path_s, str(final_path), "ok"))
                    n_moved += 1
                except OSError as e:
                    print(f"FAIL {path_s}: {e}", file=sys.stderr)
                    log_rows.append((path_s, "", f"error:{e}"))

            if args.max_files and n_plan >= args.max_files:
                break

    if not args.execute:
        print(
            f"\nDry-run: {n_plan} file(s) would move to {dest_root}. Pass --execute to run.",
            file=sys.stderr,
        )
    else:
        print(
            f"\nMoved {n_moved} / planned {n_plan} -> {dest_root}", file=sys.stderr)

    if args.log_csv:
        args.log_csv.parent.mkdir(parents=True, exist_ok=True)
        write_header = not args.log_csv.is_file()
        with args.log_csv.open("a", newline="", encoding="utf-8") as lf:
            w = csv.writer(lf)
            if write_header:
                w.writerow(["local_path", "dest_path", "status"])
            w.writerows(log_rows)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
