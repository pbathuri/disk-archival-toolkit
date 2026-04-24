"""
Filesystem inventory: collect per-file metadata and heuristics for local vs remote retention.

Designed for macOS/Linux; uses stdlib only. Quality of attributes and classification
is prioritized over scan duration.
"""

from __future__ import annotations

import csv
import os
import stat
import sys
import time
from dataclasses import dataclass, asdict, fields
from pathlib import Path
from typing import Callable, Iterator, Optional


# Extension buckets for coarse "type" and scoring hints
CODE_EXT = frozenset({
    ".py", ".js", ".ts", ".tsx", ".jsx", ".rs", ".go", ".java", ".kt", ".swift",
    ".c", ".h", ".cpp", ".hpp", ".cs", ".rb", ".php", ".scala", ".vue", ".svelte",
    ".sql", ".sh", ".zsh", ".bash", ".fish", ".ps1", ".r", ".m", ".mm",
})
DOC_EXT = frozenset({
    ".pdf", ".doc", ".docx", ".odt", ".rtf", ".txt", ".md", ".tex", ".epub",
    ".ppt", ".pptx", ".xls", ".xlsx", ".csv", ".tsv",
})
MEDIA_EXT = frozenset({
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".heic", ".raw",
    ".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v",
    ".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a",
})
ARCHIVE_EXT = frozenset({
    ".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar", ".dmg", ".iso",
})
DEV_ARTIFACT_EXT = frozenset({
    ".o", ".so", ".dylib", ".dll", ".class", ".pyc", ".pyo", ".egg", ".whl",
})
DEV_DIR_NAMES = frozenset({
    "node_modules", ".git", "__pycache__", ".venv", "venv", ".tox", "dist",
    "build", "target", ".gradle", ".idea", ".vscode",
})


def _iso(ts: Optional[float]) -> str:
    if ts is None:
        return ""
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts))
    except (OSError, OverflowError, ValueError):
        return ""


def _days_ago(ts: Optional[float], now: float) -> str:
    if ts is None:
        return ""
    try:
        return f"{max(0, (now - ts) / 86400):.2f}"
    except (TypeError, OverflowError):
        return ""


def classify_path(path: Path, is_dir: bool, suffix_lower: str) -> str:
    parts_lower = {p.lower() for p in path.parts}
    if any(d in parts_lower for d in DEV_DIR_NAMES):
        return "dev_artifact"
    if is_dir:
        return "directory"
    if suffix_lower in CODE_EXT:
        return "code"
    if suffix_lower in DOC_EXT:
        return "document"
    if suffix_lower in MEDIA_EXT:
        return "media"
    if suffix_lower in ARCHIVE_EXT:
        return "archive"
    if suffix_lower in DEV_ARTIFACT_EXT:
        return "dev_artifact"
    name = path.name.lower()
    if name in {".ds_store", "thumbs.db"}:
        return "system"
    return "other"


def nearest_git_root(start: Path, stop_at: Path, cache: dict[str, str]) -> str:
    """Return absolute path of containing git work tree root, or empty string."""
    cache_key = str(start)
    if cache_key in cache:
        return cache[cache_key]

    cur = start if start.is_dir() else start.parent
    chain: list[Path] = []
    try:
        resolved_stop = stop_at.resolve()
    except OSError:
        cache[cache_key] = ""
        return ""

    found = ""
    while True:
        try:
            cur_r = cur.resolve()
        except OSError:
            found = ""
            break
        chain.append(cur)
        if (cur / ".git").exists():
            found = str(cur_r)
            break
        if cur_r == resolved_stop or cur.parent == cur:
            found = ""
            break
        cur = cur.parent

    for p in chain:
        try:
            cache[str(p)] = found
        except OSError:
            pass
    cache[cache_key] = found
    return found


def local_remote_score(
    *,
    size_bytes: int,
    days_since_atime: Optional[float],
    days_since_mtime: Optional[float],
    category: str,
    is_under_dot: bool,
    depth: int,
    in_git_repo: bool,
) -> tuple[int, str]:
    """
    Return (score 0-100, tier).

    Higher score => stronger case to keep on local disk for immediate access.
    Tier: keep_local | warm_remote | cold_archive
    """
    score = 50
    tier = "warm_remote"

    # Recency (atime weighted more for "immediate access" narrative)
    if days_since_atime is not None:
        if days_since_atime < 7:
            score += 20
        elif days_since_atime < 30:
            score += 10
        elif days_since_atime > 365:
            score -= 15
        elif days_since_atime > 180:
            score -= 8

    if days_since_mtime is not None:
        if days_since_mtime < 14:
            score += 8
        elif days_since_mtime > 730:
            score -= 10

    # Size: large files are better remote unless hot
    gb = size_bytes / (1024**3)
    if gb > 50:
        score -= 25
    elif gb > 10:
        score -= 15
    elif gb > 2:
        score -= 8
    elif size_bytes < 512 * 1024:
        score += 5

    # Category
    if category == "code":
        score += 12
    elif category == "document":
        score += 5
    elif category == "media":
        score -= 8
    elif category == "archive":
        score -= 12
    elif category == "dev_artifact":
        score -= 18
    elif category == "system":
        score -= 5

    if in_git_repo:
        score += 10
    if is_under_dot and depth <= 3:
        score += 5

    score = max(0, min(100, int(round(score))))

    if score >= 68:
        tier = "keep_local"
    elif score <= 35:
        tier = "cold_archive"
    else:
        tier = "warm_remote"

    return score, tier


@dataclass
class FileRecord:
    path: str
    name: str
    parent_dir: str
    size_bytes: int
    mtime_epoch: str
    mtime_iso: str
    atime_epoch: str
    atime_iso: str
    ctime_epoch: str
    ctime_iso: str
    birthtime_epoch: str
    birthtime_iso: str
    extension: str
    file_category: str
    is_symlink: str
    is_directory: str
    is_hidden: str
    mode_octal: str
    uid: str
    gid: str
    depth_from_root: str
    scan_root: str
    git_repo_root: str
    days_since_mtime: str
    days_since_atime: str
    days_since_birth: str
    mtime_year_month: str
    local_priority_score: str
    remote_tier_suggestion: str
    device_id: str
    inode: str
    nlink: str
    error: str


def iter_files(
    roots: list[Path],
    on_error: Optional[Callable[[str, str], None]] = None,
) -> Iterator[tuple[Path, Path, Optional[str]]]:
    """
    Yield (scan_root, path, error_message).

    Directories are yielded with error_message None; files and symlinks similarly.
    If a directory cannot be opened, yields that path with error_message set (once).
    """
    for root in roots:
        root = root.expanduser().resolve()
        if not root.exists():
            if on_error:
                on_error(str(root), "root does not exist")
            continue

        stack: list[Path] = [root]
        yield root, root, None
        while stack:
            current = stack.pop()
            try:
                with os.scandir(current) as it:
                    entries = list(it)
            except OSError as e:
                msg = f"scandir: {e}"
                if on_error:
                    on_error(str(current), msg)
                yield root, current, msg
                continue

            for ent in entries:
                p = Path(ent.path)
                try:
                    if ent.is_symlink():
                        yield root, p, None
                        if ent.is_dir(follow_symlinks=False):
                            # Do not descend into symlinked dirs by default (avoid cycles).
                            continue
                    if ent.is_dir(follow_symlinks=False):
                        stack.append(p)
                        yield root, p, None
                    elif ent.is_file(follow_symlinks=False):
                        yield root, p, None
                except OSError as e:
                    msg = f"entry: {e}"
                    if on_error:
                        on_error(str(p), msg)
                    yield root, p, msg


def stat_record(
    scan_root: Path,
    path: Path,
    now: float,
    git_cache: dict[str, str],
    scan_error: Optional[str] = None,
) -> FileRecord:
    rel = path.relative_to(scan_root)
    depth = len(rel.parts) if path != scan_root else 0
    name = path.name
    parent = str(path.parent)
    ext = path.suffix.lower()
    is_hidden = name.startswith(".")

    err_parts: list[str] = []
    if scan_error:
        err_parts.append(scan_error)

    try:
        st = os.lstat(path)
    except OSError as e:
        err_parts.append(f"lstat: {e}")
        return FileRecord(
            path=str(path),
            name=name,
            parent_dir=parent,
            size_bytes=0,
            mtime_epoch="", mtime_iso="",
            atime_epoch="", atime_iso="",
            ctime_epoch="", ctime_iso="",
            birthtime_epoch="", birthtime_iso="",
            extension=ext,
            file_category="unknown",
            is_symlink="",
            is_directory="",
            is_hidden=str(is_hidden),
            mode_octal="",
            uid="", gid="",
            depth_from_root=str(depth),
            scan_root=str(scan_root),
            git_repo_root="",
            days_since_mtime="", days_since_atime="", days_since_birth="",
            mtime_year_month="",
            local_priority_score="",
            remote_tier_suggestion="",
            device_id="",
            inode="",
            nlink="",
            error="; ".join(err_parts),
        )

    is_lnk = stat.S_ISLNK(st.st_mode)
    is_dir = stat.S_ISDIR(st.st_mode)
    is_file = stat.S_ISREG(st.st_mode)
    size = st.st_size if is_file else 0

    mtime = getattr(st, "st_mtime", None)
    atime = getattr(st, "st_atime", None)
    ctime = getattr(st, "st_ctime", None)
    birth = getattr(st, "st_birthtime",
                    None) if sys.platform == "darwin" else None

    category = classify_path(path, is_dir, ext)
    git_root = nearest_git_root(path, scan_root, git_cache)

    d_mtime = float(_days_ago(mtime, now)) if mtime is not None else None
    d_atime = float(_days_ago(atime, now)) if atime is not None else None
    d_birth = float(_days_ago(birth, now)) if birth is not None else None
    ym = ""
    if mtime is not None:
        try:
            ym = time.strftime("%Y-%m", time.localtime(mtime))
        except (OSError, OverflowError, ValueError):
            ym = ""

    score_s = ""
    tier_s = ""
    if is_file and not is_lnk:
        score, tier = local_remote_score(
            size_bytes=size,
            days_since_atime=d_atime,
            days_since_mtime=d_mtime,
            category=category,
            is_under_dot=is_hidden or any(
                p.startswith(".") for p in path.parts),
            depth=depth,
            in_git_repo=bool(git_root),
        )
        score_s = str(score)
        tier_s = tier
    elif is_dir:
        category = "directory"

    return FileRecord(
        path=str(path),
        name=name,
        parent_dir=parent,
        size_bytes=size,
        mtime_epoch=f"{mtime}" if mtime is not None else "",
        mtime_iso=_iso(mtime),
        atime_epoch=f"{atime}" if atime is not None else "",
        atime_iso=_iso(atime),
        ctime_epoch=f"{ctime}" if ctime is not None else "",
        ctime_iso=_iso(ctime),
        birthtime_epoch=f"{birth}" if birth is not None else "",
        birthtime_iso=_iso(birth),
        extension=ext,
        file_category=category,
        is_symlink=str(is_lnk),
        is_directory=str(is_dir),
        is_hidden=str(is_hidden),
        mode_octal=oct(st.st_mode & 0o7777),
        uid=str(st.st_uid),
        gid=str(st.st_gid),
        depth_from_root=str(depth),
        scan_root=str(scan_root),
        git_repo_root=git_root,
        days_since_mtime=_days_ago(mtime, now),
        days_since_atime=_days_ago(atime, now),
        days_since_birth=_days_ago(birth, now),
        mtime_year_month=ym,
        local_priority_score=score_s,
        remote_tier_suggestion=tier_s,
        device_id=str(st.st_dev),
        inode=str(st.st_ino),
        nlink=str(st.st_nlink),
        error="; ".join(err_parts) if err_parts else "",
    )


def write_csv(path_out: Path, records: Iterator[FileRecord]) -> int:
    path_out.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [f.name for f in fields(FileRecord)]
    n = 0
    with path_out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for rec in records:
            w.writerow(asdict(rec))
            n += 1
    return n
