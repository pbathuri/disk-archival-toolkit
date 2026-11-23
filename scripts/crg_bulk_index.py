#!/usr/bin/env python3
"""Register and build code-review-graph for every path in Map_Projects_MAC/projects.json."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parent.parent
PROJECTS_JSON = WORKSPACE / "Map_Projects_MAC" / "projects.json"
STATE_PATH = WORKSPACE / ".code-review-graph-bulk-state.json"
LOG_PATH = WORKSPACE / ".code-review-graph-bulk.log"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def log(msg: str, *, also_print: bool = True) -> None:
    line = f"{utc_now()} {msg}"
    if also_print:
        print(line, flush=True)
    with open(LOG_PATH, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(
        state, indent=2, sort_keys=True), encoding="utf-8")


def repo_has_graph(path: Path) -> bool:
    return (path / ".code-review-graph" / "graph.db").is_file()


def graph_status_has_nodes(out: str) -> bool:
    """True if `status` output reports a non-zero node count (indexed content)."""
    for line in out.splitlines():
        if line.strip().startswith("Nodes:"):
            rest = line.split(":", 1)[1].strip().split()[0]
            try:
                return int(rest) > 0
            except ValueError:
                return False
    return False


def graph_looks_built(path: Path, crg: str) -> bool:
    """Avoid full rebuild when graph.db exists and status shows real content."""
    proc = subprocess.run(
        [crg, "status"],
        cwd=str(path),
        capture_output=True,
        text=True,
        timeout=600,
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        return False
    if graph_status_has_nodes(out):
        return True
    if "Last updated: never" in out:
        return False
    return "Last updated:" in out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--crg-bin",
        default=os.environ.get("CRG_BIN", str(
            WORKSPACE / ".venv" / "bin" / "code-review-graph")),
        help="Path to code-review-graph executable",
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="List actions only")
    parser.add_argument("--no-resume", action="store_true",
                        help="Ignore state file and heuristics")
    parser.add_argument("--force", action="store_true",
                        help="Rebuild even if state says success")
    parser.add_argument(
        "--serial",
        action="store_true",
        help="Set CRG_SERIAL_PARSE=1 for all builds (slower, more stable)",
    )
    args = parser.parse_args()

    crg = args.crg_bin
    if not Path(crg).is_file():
        log(f"ERROR: CRG binary not found: {crg}", also_print=True)
        return 2

    if not PROJECTS_JSON.is_file():
        log(f"ERROR: missing {PROJECTS_JSON}", also_print=True)
        return 2

    data = json.loads(PROJECTS_JSON.read_text(encoding="utf-8"))
    projects = data.get("projects") or []
    paths = [Path(p["path"]) for p in projects if p.get("path")]

    state: dict = {} if args.no_resume or args.force else load_state()
    if args.force:
        state = {}

    ok = 0
    skipped = 0
    failed = 0

    for path in paths:
        key = str(path)
        name = path.name

        if not path.is_dir():
            log(f"SKIP missing_dir name={name} path={key}")
            skipped += 1
            continue

        st = state.get(key, {})
        if not args.force and st.get("build_ok") is True:
            log(f"SKIP state name={name} path={key}")
            skipped += 1
            continue

        if not args.force and repo_has_graph(path) and graph_looks_built(path, crg):
            log(f"SKIP existing_graph name={name} path={key}")
            state[key] = {**st, "build_ok": True,
                          "note": "detected_existing_graph"}
            save_state(state)
            skipped += 1
            continue

        if args.dry_run:
            log(f"DRY build+register name={name} path={key}")
            ok += 1
            continue

        # Build first so `.code-review-graph/` exists; `register` accepts .git OR .code-review-graph
        # (folders without git init can still be indexed).
        env = os.environ.copy()
        if args.serial:
            env["CRG_SERIAL_PARSE"] = "1"

        log(f"BUILD name={name} path={key}")
        build = subprocess.run(
            [crg, "build"],
            cwd=str(path),
            capture_output=True,
            text=True,
            timeout=7200,
            env=env,
        )
        if build.returncode != 0 and not args.serial:
            err = (build.stderr or "")[:2000]
            log(f"BUILD_RETRY_SERIAL name={name} first_err={err!r}")
            env2 = dict(env)
            env2["CRG_SERIAL_PARSE"] = "1"
            build = subprocess.run(
                [crg, "build"],
                cwd=str(path),
                capture_output=True,
                text=True,
                timeout=7200,
                env=env2,
            )

        if build.returncode != 0:
            err = (build.stderr or "")[:4000]
            log(f"BUILD_FAIL name={name} path={key} rc={build.returncode} err={err!r}")
            state[key] = {"register_ok": False,
                          "build_ok": False, "error": err}
            save_state(state)
            failed += 1
            continue

        tail = (build.stdout or "")[-500:]
        log(f"BUILD_OK name={name} path={key} tail={tail!r}")

        log(f"REGISTER name={name} path={key}")
        reg = subprocess.run(
            [crg, "register", str(path)],
            cwd=str(WORKSPACE),
            capture_output=True,
            text=True,
            timeout=600,
        )
        if reg.returncode != 0:
            err = (reg.stderr or "")[:4000]
            log(
                f"REGISTER_FAIL name={name} path={key} rc={reg.returncode} err={err!r} "
                "(graph built; multi-repo registry not updated)"
            )
            state[key] = {"register_ok": False, "build_ok": True, "error": err}
            save_state(state)
            failed += 1
            continue

        state[key] = {"register_ok": True, "build_ok": True}
        save_state(state)
        ok += 1

    log(f"SUMMARY ok={ok} skipped={skipped} failed={failed} total_paths={len(paths)}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
