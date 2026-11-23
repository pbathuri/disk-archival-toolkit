#!/usr/bin/env python3
"""darc — tiered storage for this Mac and the Optometry drive.

Folder-level (not file-level) archival: keep what you're working on local, move cold
data/archives/media to the external drive with verified copies and an audit trail.

  darc status [--brief]          free space on Mac + drive, archive size, last moves
  darc plan [--days 21] [--min-gb 1]   rank cold, large folders/files as move candidates
  darc move PATH... [--link] [--tar] [--keep]   verified copy -> drive, then delete local
  darc restore PATH              bring an archived path back to the Mac
  darc find TEXT                 search the manifest for something you archived

Rules baked in (from the 2026-11-22 migration):
  * the drive is exFAT: no symlinks/Unix perms on it. Code trees go as --tar; data goes as-is.
  * --link leaves a symlink on the Mac (APFS) pointing at the drive, so scripts keep working
    whenever the drive is plugged in.
  * nothing local is deleted unless every file's size matches on the drive (or tar entry count).
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import shutil
import subprocess
import sys
from pathlib import Path

HOME = Path.home()
DRIVE = Path(os.environ.get("DARC_DRIVE", "/Volumes/Optometry"))
ARCHIVE = DRIVE / "Mac_Archive"
MANIFEST = ARCHIVE / "MANIFEST.txt"
KEEP_FILE = HOME / "Tools/disk_archival/keep_local.txt"   # one path prefix per line, never proposed
SKIP_NAMES = {".DS_Store"}
# never propose these: system/app state, credentials, live tool state
NEVER = [HOME / p for p in ("Library", ".ssh", ".gnupg", ".claude", ".cartograph", ".config",
                             ".local", ".git-ai", "Applications")]


def gb(n: float) -> str:
    return f"{n / 1e9:.1f}G"


def free_bytes(p: Path) -> int:
    return shutil.disk_usage(p).free


def drive_ok() -> bool:
    return DRIVE.is_dir() and os.path.ismount(DRIVE)


def rel(p: Path) -> Path:
    p = p.expanduser().absolute()
    try:
        return p.relative_to(HOME)
    except ValueError:
        sys.exit(f"darc: only paths under {HOME} are supported: {p}")


def inventory(p: Path) -> dict[str, int]:
    if p.is_file():
        return {"": p.stat().st_size}
    out = {}
    for d, _, fs in os.walk(p):
        for f in fs:
            if f in SKIP_NAMES or f.startswith("._"):
                continue
            fp = os.path.join(d, f)
            if not os.path.islink(fp):
                out[os.path.relpath(fp, p)] = os.path.getsize(fp)
    return out


def du(p: Path) -> int:
    try:
        return int(subprocess.run(["du", "-sk", str(p)], capture_output=True, text=True).stdout.split()[0]) * 1024
    except Exception:
        return 0


def newest_mtime(p: Path) -> float:
    if p.is_file():
        return p.stat().st_mtime
    r = subprocess.run(f'find "{p}" -type f -not -path "*/.git/*" -not -path "*/node_modules/*" '
                       f'-print0 2>/dev/null | xargs -0 stat -f %m 2>/dev/null | sort -n | tail -1',
                       shell=True, capture_output=True, text=True).stdout.strip()
    return float(r or 0)


def log(status: str, size: float, action: str, r: Path) -> None:
    line = f"{dt.datetime.now():%F %T}\t{status}\t{size / 1e9:.2f}GB\t{action}\t~/{r}"
    print(line)
    with MANIFEST.open("a") as f:
        f.write(line + "\n")


# ---------------------------------------------------------------- commands
def cmd_status(a) -> None:
    mac = free_bytes(HOME)
    if a.brief:  # used by the Claude Code SessionStart hook: silent unless something needs attention
        msgs = []
        if mac < 50e9:
            msgs.append(f"Mac free space is low ({gb(mac)}). Run `darc plan` to find cold folders for the Optometry drive.")
        if msgs:
            print("[darc] " + " ".join(msgs))
        return
    print(f"Mac free:    {gb(mac)}")
    if not drive_ok():
        print(f"Drive:       not mounted ({DRIVE})")
        return
    print(f"Drive free:  {gb(free_bytes(DRIVE))}   archive: {gb(du(ARCHIVE))} at {ARCHIVE}")
    links = [l for l in MANIFEST.read_text().splitlines() if "symlink" in l] if MANIFEST.exists() else []
    if links:
        print(f"Symlinked:   {len(links)} paths on the Mac point at the drive (need it plugged in)")
    if MANIFEST.exists():
        print("Last moves:")
        for l in MANIFEST.read_text().splitlines()[-8:]:
            print("  " + l)


def cmd_plan(a) -> None:
    keep = [Path(l.strip()).expanduser() for l in KEEP_FILE.read_text().splitlines()
            if l.strip() and not l.startswith("#")] if KEEP_FILE.exists() else []
    cutoff = dt.datetime.now().timestamp() - a.days * 86400
    cands = []
    roots = [HOME / "Desktop", HOME / "Documents", HOME / "Downloads", HOME / "Code", HOME / "Tools", HOME]
    seen = set()
    for root in roots:
        for c in sorted(root.iterdir()) if root.is_dir() else []:
            if c.is_symlink() or c in seen or any(str(c).startswith(str(n)) for n in NEVER + keep):
                continue
            if root == HOME and c.name.startswith(".") and c.name not in (".ollama",):
                continue
            if root == HOME and c in roots:
                continue
            seen.add(c)
            size = du(c)
            if size < a.min_gb * 1e9:
                continue
            m = newest_mtime(c)
            if m < cutoff:
                cands.append((size, m, c))
            elif c.is_dir():  # active folder: look one level down for cold, big children
                for cc in c.iterdir():
                    if cc.is_symlink():
                        continue
                    s2 = du(cc)
                    if s2 >= a.min_gb * 1e9 and newest_mtime(cc) < cutoff:
                        cands.append((s2, newest_mtime(cc), cc))
    cands.sort(reverse=True)
    print(f"Cold (> {a.days} days untouched) and >= {a.min_gb} GB — largest first:\n")
    for size, m, c in cands[: a.top]:
        kind = "tar" if (c.is_dir() and (c / ".git").exists() or any((c / x).exists() for x in ("package.json", "pyproject.toml"))) else "copy"
        print(f"  {gb(size):>7}  {dt.date.fromtimestamp(m) if m else '?':<10}  [{kind}]  {c}")
    print(f"\nTotal: {gb(sum(s for s, _, _ in cands[: a.top]))}.  Move with: darc move <path> [--link|--tar]")
    print(f"Never-propose list: {KEEP_FILE}")


def cmd_move(a) -> None:
    if not drive_ok():
        sys.exit(f"darc: drive not mounted at {DRIVE}")
    ARCHIVE.mkdir(exist_ok=True)
    for raw in a.paths:
        src = Path(raw).expanduser().absolute()
        r = rel(src)
        if src.is_symlink() or not src.exists():
            print(f"skip {src}: missing or already a symlink"); continue
        if a.tar:
            dst = ARCHIVE / r.parent / (r.name + ".tar")
            dst.parent.mkdir(parents=True, exist_ok=True)
            n_src = sum(1 for _ in src.rglob("*")) + 1
            rc = subprocess.run(["tar", "-cf", str(dst), "-C", str(src.parent), src.name]).returncode
            n_tar = int(subprocess.run(f'tar -tf "{dst}" | wc -l', shell=True, capture_output=True, text=True).stdout)
            ok = rc == 0 and n_tar >= n_src
            size = dst.stat().st_size if dst.exists() else 0
            if not ok:
                log(f"TAR CHECK FAILED {n_tar}/{n_src} rc={rc}", size, "kept", r); continue
            if not a.keep:
                shutil.rmtree(src)
            log(f"OK tar {n_tar}/{n_src}", size, "tarred" + ("" if a.keep else "+deleted"), r)
            continue
        dst = ARCHIVE / r
        dst.parent.mkdir(parents=True, exist_ok=True)
        s, d = (str(src) + "/", str(dst) + "/") if src.is_dir() else (str(src), str(dst))
        rc = subprocess.run(["rsync", "-rt", s, d]).returncode
        A, B = inventory(src), inventory(dst)
        bad = [k for k, v in A.items() if B.get(k) != v]
        size = sum(A.values())
        if bad or rc:
            log(f"MISMATCH {len(bad)} rc={rc}", size, "kept", r); continue
        if a.keep:
            log("OK", size, "copied", r); continue
        shutil.rmtree(src) if src.is_dir() else src.unlink()
        if a.link:
            os.symlink(dst, src)
        log("OK", size, "moved+symlink" if a.link else "moved", r)


def cmd_restore(a) -> None:
    if not drive_ok():
        sys.exit(f"darc: drive not mounted at {DRIVE}")
    local = Path(a.path).expanduser().absolute()
    r = rel(local)
    src, tar = ARCHIVE / r, ARCHIVE / r.parent / (r.name + ".tar")
    if local.is_symlink():
        local.unlink()
    if local.exists():
        sys.exit(f"darc: {local} already exists locally")
    if tar.exists():
        local.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["tar", "-xf", str(tar), "-C", str(local.parent)], check=True)
    elif src.exists():
        s, d = (str(src) + "/", str(local) + "/") if src.is_dir() else (str(src), str(local))
        subprocess.run(["rsync", "-rt", s, d], check=True)
    else:
        sys.exit(f"darc: nothing archived for ~/{r}")
    log("OK", du(local), "restored (drive copy kept)", r)


def cmd_find(a) -> None:
    if not MANIFEST.exists():
        sys.exit("darc: no manifest (drive not mounted?)")
    for l in MANIFEST.read_text().splitlines():
        if a.text.lower() in l.lower():
            print(l)


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="darc", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = p.add_subparsers(dest="cmd", required=True)
    s = sp.add_parser("status"); s.add_argument("--brief", action="store_true"); s.set_defaults(f=cmd_status)
    s = sp.add_parser("plan"); s.add_argument("--days", type=int, default=21); s.add_argument("--min-gb", type=float, default=1.0)
    s.add_argument("--top", type=int, default=25); s.set_defaults(f=cmd_plan)
    s = sp.add_parser("move"); s.add_argument("paths", nargs="+"); s.add_argument("--link", action="store_true", help="leave a symlink on the Mac")
    s.add_argument("--tar", action="store_true", help="pack as .tar (for code trees: keeps symlinks/perms)")
    s.add_argument("--keep", action="store_true", help="copy only; don't delete local"); s.set_defaults(f=cmd_move)
    s = sp.add_parser("restore"); s.add_argument("path"); s.set_defaults(f=cmd_restore)
    s = sp.add_parser("find"); s.add_argument("text"); s.set_defaults(f=cmd_find)
    a = p.parse_args(argv)
    a.f(a)


if __name__ == "__main__":
    main()
