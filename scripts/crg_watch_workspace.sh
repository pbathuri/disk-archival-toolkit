#!/bin/sh
# Per-repo file watcher (upstream README also documents crg-daemon when available).
# Run from anywhere: keeps the graph fresh for this workspace while you edit.
cd "$(dirname "$0")/.." && exec .venv/bin/code-review-graph watch
