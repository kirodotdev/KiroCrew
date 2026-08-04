# Backup & Restore

`kirocrew snapshot` packs everything Kiro Crew has learned about you into a single
portable `.tar.gz`, and `kirocrew restore` unpacks it, on this machine or a
different one. Use it before an upgrade you are unsure about, to move your setup
to a new laptop, or to merge the memory from two machines you have been using in
parallel. Snapshots are **not** automatic: nothing takes one for you, so if you
want a routine backup, schedule the command yourself.

## Quick Start

```bash
kirocrew snapshot                                     # write to ~/.kiro/crew/snapshots
kirocrew snapshot ~/my-snapshots --keep 3             # custom dir, prune to 3
kirocrew snapshot --list                              # list existing snapshots
kirocrew restore snapshot.tar.gz                      # auto-detects replace vs merge
kirocrew restore snapshot.tar.gz --components memory,crons
kirocrew restore snapshot.tar.gz --dry-run            # preview, write nothing
kirocrew restore --list-components                    # show component names
```

Stop the gateway before restoring. `kirocrew restore` refuses to run while a
gateway is listening, because a live gateway holds the memory database open and
would write over what was just restored. Pass `--force` only if you know the
gateway on that port is not this instance.

## What a snapshot contains

| Component | Files |
|-----------|-------|
| memory | `memory.db`, `memory_index.db` |
| crons | `crons.json` |
| config | `config.json`, `session_map.json`, `hooks.json`, `project_dir`, `workspace_dir` |
| skills | `skills/` directory |
| workspace | `workspace/`, `plan_memory/` directories |
| notifications | `notifications.jsonl` |
| security | `telemetry_salt` |

`workspace/hygiene_data/` and `workspace/insert_facts*.py` are excluded: they are
large and regenerable.

The security event log's HMAC key (`sel_hmac.key`) is deliberately **excluded**
from every snapshot, and is regenerated on the restoring host. That keeps each
machine's audit-log signatures bound to the machine that wrote them, so a copied
snapshot cannot be used to forge audit entries elsewhere.

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `OUTPUT_DIR` | `~/.kiro/crew/snapshots` (or `snapshot_dir` in config) | Where to write the tarball |
| `--keep N` | 7 | Prune the output dir to the N most recent snapshots |
| `--list` | | List existing snapshots and exit |

The tarball is written to a temporary name and renamed into place only once it is
complete, so an interrupted run cannot leave a half-written archive that looks
like a valid backup. It is then locked down to owner-only permissions before the
command reports success.

Before copying the memory database, Kiro Crew flushes its write-ahead log. If the
gateway holds the lock it prints a warning and proceeds anyway: the SQLite backup
API still produces a consistent point-in-time copy that includes committed data.

## Restore

### Replace vs merge

| Mode | Chosen when | Behavior |
|------|-------------|----------|
| `replace` | No existing `memory.db` | Overwrite the target with the snapshot, backing up any existing state first |
| `merge` | An existing `memory.db` is found | Import new data without overwriting what is already there |

The mode is auto-detected from whether `~/.kiro/crew/memory.db` exists, so a
restore onto a fresh machine replaces and a restore onto a machine you are
already using merges. Override with `--mode replace` or `--mode merge`.

In `replace` mode the state being overwritten is moved into a
`pre-restore-<timestamp>/` folder inside the data home first, and the path is
printed, so a wrong-snapshot restore is recoverable.

### What merge does per component

- **Memory**: existing entries win, new keys are added
- **Crons**: deduplicated by job name. Existing jobs are kept; new jobs are
  imported with fresh IDs
- **Notifications**: deduplicated by timestamp
- **Config and security**: only files that are missing are restored, never
  overwritten
- **Workspace and skills**: only files that do not exist at the destination are
  copied

So a merge never destroys anything on the receiving machine. If you want the
snapshot to win, use `--mode replace`.

### Options

| Flag | Description |
|------|-------------|
| `--mode replace\|merge` | Force the mode instead of auto-detecting |
| `--components X,Y` | Restore only these components |
| `--dry-run` | List what would be restored and write nothing |
| `--list-components` | Show the component names and what each covers |
| `--force` | Restore even though a gateway is listening |

After a restore, run `kirocrew restart` so the gateway picks up the new state.

### Integrity check

A restore runs an integrity check on the memory database and exits non-zero if it
fails, so a corrupted archive is a loud failure rather than a quietly broken
memory. If the full-text index (`memory_index.db`) is missing you get a warning:
search keeps working, but the index needs to rebuild first.

## Security

Snapshots are handled as untrusted input on the way in and as sensitive data on
the way out.

- Archives containing symlinks or hardlinks are rejected before extraction, so a
  crafted archive cannot be used to write outside the data home
- Entries with `..` or absolute paths are rejected
- Extraction strips ownership and permissions from the archive
- Both snapshot and restore emit security audit events, including a rejected
  restore and the reason it was rejected
- The tarball itself is created owner-only. It still contains your config,
  memory, and workspace, so treat it as private: store it with restrictive
  permissions and do not send it over a channel you would not send your notes
  over

## Scheduling your own backups

There is no built-in backup schedule. To get one, add a cron job that runs the
command, for example by asking the agent to schedule `kirocrew snapshot --keep 7`
daily. Verify it afterwards with `kirocrew snapshot --list`: an unverified backup
job is the same as no backup.
