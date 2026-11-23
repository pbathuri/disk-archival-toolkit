<div align="center">

# Disk Archival Toolkit

**Memory-safe filesystem audit and budget-constrained archival manifest generator. Built for researchers managing multi-terabyte working sets across local, warm-remote, and cold-archive tiers.**

<br/>

<img src="https://img.shields.io/badge/Python-3.11+-0D1117?style=for-the-badge&labelColor=161B22&color=58A6FF" />
<img src="https://img.shields.io/badge/Domain-HPC%20%C2%B7%20storage-0D1117?style=for-the-badge&labelColor=161B22&color=FFA657" />
<img src="https://img.shields.io/badge/Streaming-multi--million%20rows-0D1117?style=for-the-badge&labelColor=161B22&color=8B949E" />

</div>

---

## Why this exists

Working with HPC + ML datasets fills disks faster than you can tier them. macOS Finder, `du`, and even GUI archivers fall over at multi-million-file scale (loading the whole inventory into memory). This toolkit:

1. **Scans** filesystems into a flat CSV inventory without loading paths into memory.
2. **Audits** that CSV against safety exclusions and tier heuristics.
3. **Selects** a maximum-bytes subset within a configurable size budget (e.g. "fit 3 TB into the cold tier").
4. **Moves** the selected files to an external disk or **uploads** them to Google Drive.

All four steps stream — peak memory stays under ~50 MB even on 2.6 GB inventory CSVs.

---

## Stack

| Module | Purpose |
|--------|---------|
| `scan.py` | CLI: walks roots, emits per-file CSV with stat + local/remote heuristics |
| `inventory_core.py` | `FileRecord`, `iter_files`, tier classification, stat helpers |
| `audit_upload_plan.py` | Streams the inventory, applies exclusions, produces `upload_manifest.csv` within a budget |
| `move_from_inventory.py` | Executes the manifest against an external disk (rsync-shaped, idempotent, logged) |
| `drive_client.py` | Google Drive v3 client (resumable upload, OAuth refresh) |

---

## Quick start

```bash
# 1. Inventory the filesystem
python3 scan.py --roots ~ -o ~/Desktop/file_inventory.csv

# 2. Audit + select a 3 TB archival manifest
python3 audit_upload_plan.py \
    --inventory ~/Desktop/file_inventory.csv \
    --budget-tb 3 \
    --output upload_manifest.csv

# 3. Execute against an external disk
python3 move_from_inventory.py \
    --manifest upload_manifest.csv \
    --dest /Volumes/ColdArchive/2026 \
    --log move_log.csv
```

Add `--aggressive` to the audit step to include large, stale media/archives that would normally score `keep_local`.

---

## Tier heuristics

`inventory_core.py` classifies every file into one of three tiers:

- `keep_local` - small, recently accessed, or under a project root
- `warm_remote` - moderate size or older but still touched in the last year
- `cold_archive` - large, untouched for >1 year, no project breadcrumbs

The heuristic is intentionally simple (size threshold + access age + path hints) so it stays auditable. Override with `--policy` if you want stricter rules.

---

## Safety exclusions

Hard-blocked paths that are never archived, even if they match the budget:

```
/.Trash/, /Library/Caches/, /Library/Containers/, /node_modules/,
/.git/, /.ssh/, /.gnupg/, /Library/Keychains/, /Library/Mail/,
/Library/Messages/, /Parallels/, *.vmwarevm/, /.vagrant/,
/.Spotlight-V100/, .DS_Store, .localized
```

These are system-managed or contain credentials. Even with `--aggressive`, they stay put.

---

## Why I built this

While running L1-cache experiments for [`finance-cache-hpc`](https://github.com/pbathuri/finance-cache-hpc), my Mac filled up with intermediate dumps, profiler outputs, and dataset replicas. Buying a bigger drive doesn't solve the problem; choosing **what** to archive does. This is the script I wished existed.

The same primitives are also useful for HPC users on shared scratch filesystems where quota matters and the cost of accidentally archiving a `node_modules` is real.

---

## License

MIT

---

<div align="center">
<sub>Part of <a href="https://github.com/pbathuri">@pbathuri</a>'s <a href="https://github.com/pbathuri/Map_Projects_MAC">project portfolio</a> - HPC tooling.</sub>
</div>

## `darc` — folder-level tiering to an external drive

`scan.py` / `audit_upload_plan.py` work file by file. `darc.py` is the day-to-day layer on top: it moves
whole cold folders from `~` to an external drive (default `/Volumes/Optometry`, override with `DARC_DRIVE`),
mirroring paths under `Mac_Archive/` and appending every action to `Mac_Archive/MANIFEST.txt`.

```bash
darc status            # Mac + drive free space, recent moves   (--brief: silent unless < 50 GB free)
darc plan --days 21    # cold, large folders ranked by size; skips paths in keep_local.txt
darc move PATH         # verified copy (per-file size check) → delete local
darc move PATH --link  # …and leave a symlink so scripts still find the data when the drive is plugged in
darc move PATH --tar   # code trees: tar keeps symlinks/exec bits that exFAT drops
darc restore PATH      # bring it back (drive copy kept)
darc find TEXT         # search the manifest
```

Nothing local is deleted unless every file matches on the drive (or the tar entry count matches).
Install: `ln -s "$PWD/darc.py" ~/.local/bin/darc`; copy `keep_local.example.txt` to `keep_local.txt`.
