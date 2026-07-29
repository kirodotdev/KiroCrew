# Backup & Restore

Portable snapshot and restore of KiroCrew state.

## Quick Start

```bash
kirocrew snapshot                                    # default: ~/.kiro/crew/snapshots
kirocrew snapshot ~/my-snapshots --keep 3             # custom dir, keep 3
kirocrew snapshot --list                             # list existing snapshots
kirocrew restore snapshot.tar.gz                     # auto-detects replace vs merge
kirocrew restore snapshot.tar.gz --components memory,crons
kirocrew restore snapshot.tar.gz --dry-run           # preview without writing
kirocrew restore --list-components                   # show available components
```

## Snapshot

Creates a `.tar.gz` archive containing all KiroCrew state:

| Component | Files |
|-----------|-------|
| memory | `memory.db`, `memory_index.db` |
| crons | `crons.json` |
| config | `config.json`, `session_map.json`, `hooks.json`, `project_dir`, `workspace_dir` |
| skills | `skills/` directory |
| workspace | `workspace/`, `plan_memory/` directories |
| notifications | `notifications.jsonl` |
| security | `sel_hmac.key`, `telemetry_salt` |

Excludes `workspace/hygiene_data/` and `workspace/insert_facts*.py` (large, regenerable).

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `OUTPUT_DIR` | `~/.kiro/crew/snapshots` | Where to write the tarball |
| `--keep N` | 7 | Prune to N most recent snapshots |
| `--list` | — | List existing snapshots and exit |

### WAL Checkpoint

Before snapshotting, `PRAGMA wal_checkpoint(TRUNCATE)` is attempted on `memory.db` to flush the write-ahead log. If the gateway holds a lock, a warning is printed but the snapshot proceeds — the `sqlite3.Connection.backup()` API still produces a consistent point-in-time copy including committed WAL data.

## Restore

### Modes

| Mode | Auto-detected when | Behavior |
|------|---------------------|----------|
| `replace` | No existing `memory.db` | Overwrites target with snapshot contents (backs up any existing state first) |
| `merge` | Existing `memory.db` found | Imports new data without overwriting existing |

Auto-detected based on whether `~/.kiro/crew/memory.db` exists. Override with `--mode replace|merge`.

### Merge Behavior

- **Memory**: `INSERT OR IGNORE` — existing keys preserved, new keys added
- **Crons**: Dedup by job name — existing jobs kept, new jobs imported with fresh IDs
- **Notifications**: Dedup by `ts` field — no duplicates
- **Config/Security**: Only restores files that are missing (never overwrites)
- **Workspace/Skills**: Copy files that don't exist at destination (never overwrites)

### Options

| Flag | Description |
|------|-------------|
| `--mode replace\|merge` | Force mode (default: auto-detect) |
| `--components X,Y` | Restore only specific components |
| `--dry-run` | Show what would be restored without writing |
| `--list-components` | Show available component names |

### Integrity Check

After restore, `PRAGMA integrity_check` runs on `memory.db`. If it fails, restore exits non-zero. A warning is also printed if `memory_index.db` is missing (FTS index needs rebuild).

## Security

- **Symlink/hardlink rejection**: Archives containing symlinks or hardlinks are rejected before extraction
- **Path traversal**: Entries with `..` or absolute paths are rejected
- **tarfile filter**: `filter='data'` strips ownership and permissions
- **SEL audit**: Both snapshot and restore emit audit events (`snapshot_created`, `state_restored`)

> **⚠️ Sensitive data**: Snapshots include `sel_hmac.key` (SEL integrity key). Treat snapshot files as sensitive — an attacker with access could forge audit entries. Store snapshots with appropriate file permissions and do not share over insecure channels.

## Cron Setup

The daily snapshot cron runs at 08:00 UTC:

```
kirocrew-daily-snapshot | At 8:00 AM UTC
→ kirocrew snapshot --keep 7
```

## Architecture

The implementation lives in `src/kiro_crew/snapshot.py` (~500 lines), exposed via `kirocrew snapshot` and `kirocrew restore` subcommands in the CLI.
