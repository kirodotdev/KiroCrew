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
kirocrew snapshot --components memory                 # just memory, ~20 MB
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

Both commands refuse to run on a platform that cannot open a directory relative to a file descriptor, because every component would then be re-opened by name and an ancestor swapped mid-walk could redirect the copy into a credential store. `--allow-unpinned-staging` accepts that by-name traversal instead; a snapshot taken that way records the weaker staging mode in its `MANIFEST.json`.

## What a snapshot contains

| Component | Files |
|-----------|-------|
| memory | `memory.db`, `memory_index.db`, `workspace/memory/`, `workspace/knowledge/`, `memory_stores/` |
| crons | `crons.json` |
| config | `config.json`, `session_map.json`, `hooks.json`, `project_dir`, `workspace_dir` |
| skills | `skills/` directory |
| workspace | `workspace/`, `plan_memory/` directories |
| notifications | `notifications.jsonl` |
| security | `telemetry_salt` |

`memory` is self-contained: it names the markdown half of memory (preferences,
projects, history) and the knowledge base explicitly, so `--components memory`
restores your recall without also restoring every unrelated working file in
`workspace/`. Selections may overlap — asking for both `memory` and `workspace`
stages the shared paths once.

`workspace/hygiene_data/` and `workspace/insert_facts*.py` are excluded: they are
large and regenerable.

`memory_stores/` holds every **named memory store**: one directory per crew member,
each with its own markdown memory, lessons, FTS index, vector database and
ownership manifest. All of that rides with `memory`. Host-local entries under
the tree are this machine's runtime state rather than memory and never ride, in
either direction: the member signing key (`memory_stores/.member-api-key`,
regenerated on the restoring host like `sel_hmac.key`), the private execution
logs (`memory_stores/.execution-logs/`), and the local rolling backups
(`memory_stores/.member-backups/` and a named store's own `backups/`, which hold
that host's recovery copies and any pending-restore journal). A restore drops
them if a hand-built archive carries them. Retirement markers under
`memory_stores/.archived-members/` also stay on this host and never ride in an
archive. Replace and rollback preserve these markers, so restoring an older
configuration does not reactivate a retired private store.

Archives contain private memory in cleartext. Keep backup files and temporary
storage away from untrusted agents. Owner-only staging and ZIP-extraction
permissions block other OS users, not agents running as the same OS user; the
live-store path fence does not automatically protect an exported copy.

The security event log's HMAC key (`sel_hmac.key`) is deliberately **excluded**
from every snapshot, and is regenerated on the restoring host. That keeps each
machine's audit-log signatures bound to the machine that wrote them, so a copied
snapshot cannot be used to forge audit entries elsewhere.

## Purpose: backup vs share

Every bundle records why it exists, and each component declares whether it is safe to
hand to another person.

| Purpose | Meaning | Today |
|---------|---------|-------|
| `backup` (default) | Restoring onto a host you control | Everything selected rides; the LOCAL archive is unredacted — that is the point |
| `share` | Leaving your control | **Refused for every component** |

`--purpose share` refuses whatever you select, and that is deliberate rather than
unfinished. Whether a component is safe to share is a question about its **content**, not
its shape: a workspace file, a skill, a cron's `env` map, a notification body or a lesson
you pasted a token into can each carry a credential, and staging cannot tell. Nothing
claims share-safety until the redaction work behind it exists. The purpose, the
per-component declaration and the refusal are all live, so the first certified component
only has to change its own declaration. A component added without a policy declaration is
refused at staging rather than defaulting to permissive, so a new component cannot inherit
a permissive value by omission.

Use `--purpose backup` — restoring onto a host you control is what this feature is for.
The bundle's manifest records the purpose and each component's declaration, so a reader of
a bundle can tell which they are holding.

## Off-host copies

A snapshot written only to `~/.kiro/crew/snapshots` does not survive losing the machine,
which is the whole point of backing up. Getting it off the host is not this module's job:
the **AWS Control** app owns the destination and everything that protects it. It creates
one private drive bucket per account, re-asserts the whole posture on every write (public
access blocked, default encryption, ACLs disabled, object ownership enforced, versioning
on), routes every call through a single audited `aws` chokepoint, takes consent through the
existing AWS-usage grant, and runs the nightly schedule. Set it up and read its state from
that app's console; there is no destination flag here any more.

What stays here is the one thing a hardened destination does not do: **rewriting the bytes
that leave**. A private bucket still holds whatever was put into it, and a bundle restored
somewhere else carries every secret the original held.

### The redaction switch

Redacting the outbound copy is opt-in, and the switch lives at
`<data home>/backup/redaction.json`:

```json
{"redact_uploads": true}
```

It is **off by default**, deliberately. Substituting a placeholder changes a value's
length, and any payload whose format depends on byte offsets is invalidated by that. So
turning this on trades exact fidelity for a safer copy, which is a call to make rather than
a default to inherit.

No file means off, and that is the common case. If the file IS there, the value must be
exactly `true` or `false`: a `"true"` string or a `1` makes the upload refuse and name the
file rather than guess which way you meant it, because resolving it either way would decide
on your behalf whether your files get rewritten.

When it is on:

- Only the OUTBOUND copy is rewritten. The local archive is never touched: it sits on the
  machine that already holds these secrets, and redacting it would damage the only copy
  that restores complete.
- Databases are rewritten value-by-value through SQL rather than over their bytes, and
  every outbound database is rebuilt afterwards so no old value survives in page slack.
- A pass that cannot complete **refuses the send**. "Could not redact" never falls through
  to "send it unredacted", so a redaction failure costs you that upload, not your secrets.
- Content that cannot be proven safe is refused rather than shipped: a file whose bytes are
  opaque, a database that fails its integrity check, a text container that declares its own
  extents, or a database whose triggers keep reintroducing the values the scan just removed.

The agent cannot reach this file. It is fenced at the same level as the command deny list
and the computer-use enable, for reading as well as writing -- flipping it off is the
attack, and reading it tells an attacker whether the store is currently being scrubbed.

Two consequences worth knowing before you turn it on:

- **Restoring a redacted off-host copy gives you working memory and inert credentials.**
  The shape is complete and the databases are valid; the fields that authenticate are not.
  Re-enter them after restoring. The restore prints what was redacted, so you are told
  rather than left to discover it.
- **Redaction is pattern-based, so it can over-reach.** A note holding something that
  merely looks like a key can lose that text in the off-host copy. The local archive is
  unaffected, and the per-path replacement counts are printed at upload and again at
  restore so you can judge whether a count looks wrong.

Search indexes and files whose only purpose is to be secret are left out of the outbound
copy entirely rather than blanked, because an inert key present in the bundle is
indistinguishable from a rotated one. Restore reports an absent index and what to rebuild.

### Restoring a bundle that came from off-host

`kirocrew restore` takes a **local path**. An `s3://` argument is refused, with a message
telling you to fetch the bundle through the AWS Control app first and then restore the file
it hands back. That app deliberately lands a fetched archive in its own restore directory
and returns the path rather than hot-swapping it under a running gateway: restoring into
live state is this module's job, and it needs the gateway stopped (below).

## Restore

### Replace vs merge

| Mode | Chosen when | Behavior |
|------|-------------|----------|
| `replace` | No existing `memory.db` | Overwrite the target with the snapshot, backing up any existing state first |
| `merge` | An existing `memory.db` is found | Import new data without overwriting what is already there |

The mode is auto-detected from whether `~/.kiro/crew/memory.db` exists, so a
restore onto a fresh machine replaces and a restore onto a machine you are
already using merges. Override with `--mode replace` or `--mode merge`.

In `replace` mode the state being overwritten is saved first. Named stores go
into `memory_stores/.member-backups/pre-restore-<timestamp>/` inside the data home,
where agents cannot read them. Other components go into `pre-restore-<timestamp>/`
at the data-home root. The saved paths are printed, so a wrong-snapshot restore
is recoverable. If rollback cannot finish, the failure report names both locations.

### What merge does per component

- **Memory**: existing entries win, new keys are added
- **Crons**: deduplicated by job name. Existing jobs are kept; new jobs are
  imported with fresh IDs. If either cron file has a JSON shape the merger cannot use, the cron merge is skipped.
- **Notifications**: deduplicated by timestamp
- **Config and security**: only files that are missing are restored, never
  overwritten
- **Workspace and skills**: only files that do not exist at the destination are
  copied

So a merge never destroys anything on the receiving machine. If you want the
snapshot to win, use `--mode replace`.

#### Known limitation: the knowledge database is not row-merged

`workspace/knowledge/knowledge.db` follows the file rule above rather than the
row-level rule that `memory.db` gets. If the receiving machine already has a
knowledge database, a merge **keeps that one and does not import the snapshot's
rows**. The restore says so on the spot rather than reporting a silent success.

The reason is that combining two knowledge libraries is not a copy: that database
carries a full-text index plus foreign keys spanning its `sources`, `items`,
`mentions` and `source_locations` tables, so a correct merge has to remap keys,
rebuild the derived index, and first decide what makes two documents the same
document across two machines. `memory.db` gets row-level merging because that
merge is written per table for its own schema.

Until the same is written for the knowledge schema, the two ways to move a
knowledge library are:

- `--mode replace`, which takes the snapshot's knowledge database whole; or
- restore onto a machine that has no knowledge database yet, where nothing is
  being merged and the snapshot's copy lands directly.

Named stores merge as whole directories, in both snapshot restore and dashboard
import. An existing `memory_stores/<name>/` is kept whole: no missing files are
added to it, so a V1 database cannot gain a V2 manifest or index. Each kept store
is named in the result. Only stores the receiving machine lacks are installed,
with their manifest, databases and memory files together. Empty directories and
Markdown-only stores are kept too; missing databases do not mean local notes can
be combined with another store's identity. Use replace to take the archive's store.

#### Replace refuses while a named store is open

A named V1 or V2 store that some process still has open would survive the replacement as
an unlinked file, and that process would keep writing memory nothing will ever
read again. `--mode replace` therefore takes each store's lifetime lock for the
whole replace and refuses, before changing anything, when one is already held:
stop the gateway (`kirocrew stop`) and any other process using the store, then
re-run. A store that something tries to open during the replace waits for it to
finish. The dashboard import answers the same refusal with its reason instead of
applying. On POSIX, named V1 Markdown-only dashboard caches hold admission until
their last user releases the object. Snapshot and ZIP-export readers hold admission
while copying named stores, so replacement cannot mix their files across generations.
Creating and publishing new named stores waits behind the same namespace lock as
replace, merge and backup reads. Replace holds that lock before listing stores and
keeps it through rollback, so a successful concurrent create is not silently erased.
Opening a named vector store also takes namespace admission before its lifetime lock
and SQLite initialization. A cold open with no directory waits until replace finishes;
it cannot create an open database that replace then deletes. Directory creation helpers
and pending member-restore activation follow the same order. Global V1 is unchanged.
Keep the gateway stopped for replace because external writers may bypass these locks.
On every platform, named Markdown and lesson-file reads and writes hold the namespace
lock for the full operation, including index updates. A write that starts during replace
waits until replacement or rollback finishes; it does not disappear between backup and
clear. This also protects Windows stores that have no SQLite database. Global V1 is
unchanged. Windows SQLite handles separately prevent deleting an open database.
Async lesson and memory requests perform locked reads and store construction in
worker threads, so waiting for replace does not freeze the gateway event loop.

Because those locks live under `memory_stores/.member-backups/`, replace clears
`memory_stores/` store by store and leaves its host-local entries
(`.member-backups/`, `.execution-logs/`, `.member-api-key`) in place, the way the
default store's `<home>/backups/` is left in place. If replacement fails, rollback
restores the store directories into that same root without removing the locks or
the saved copy. The default, unnamed store does not take this named-store lock.

#### Replace and snapshots taken before named stores were backed up

Replace makes each memory tree match the archive, so a store directory the
archive does not carry is removed (saved under
`memory_stores/.member-backups/pre-restore-<timestamp>/`).
The one exception is a snapshot whose `MANIFEST.json` predates version 4:
those were written before the tree was part of `memory`, so their silence says
nothing about the source. Replacing from one leaves the live named stores exactly
as they are and prints a line saying so, while the rest of `memory` is replaced.
### Options

| Flag | Description |
|------|-------------|
| `--mode replace\|merge` | Force the mode instead of auto-detecting |
| `--components X,Y` | Restore only these components |
| `--dry-run` | List what would be restored and write nothing |
| `--list-components` | Show the component names and what each covers |
| `--force` | Restore even though a gateway is listening |
| `--allow-unpinned-staging` | Permit path-based restore when descriptor-pinned traversal is unavailable. |

After a restore, run `kirocrew restart` so the gateway picks up the new state.

### Integrity check

In `replace` mode every database the snapshot carries is checked **before any live
state is touched** — `memory.db`, `memory_index.db`,
`workspace/knowledge/knowledge.db`, and every named store's
`memory_stores/<name>/memory.db` and `memory_index.db`. A snapshot whose database is unreadable or
fails its integrity check is refused with a non-zero exit and nothing is
replaced, so a corrupt archive cannot leave the data home sitting on it. This
matters most for a bundle fetched from S3, which is untrusted input regardless of
whose bucket held it.

Other `.db` files inside a restored folder are only checked when they open as a
database at all, so a file that was never SQLite — a Windows `Thumbs.db`, say —
does not block a restore.

`merge` mode validates its own source before importing rows and skips a component
whose incoming database is unsound, rather than failing the whole command: a merge
cannot corrupt the receiving database, because it copies rows out of the incoming
one instead of putting it in place.

If the full-text index (`memory_index.db`) is missing you get a warning: search
keeps working, but the index needs to rebuild first.

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

`kirocrew snapshot` runs only when something runs it. OFF-HOST backups are scheduled
by the AWS Control app, which drives a nightly run of its own. For a LOCAL-only
schedule, add a cron job that runs the command -- for example by asking the agent to
schedule `kirocrew snapshot --keep 7` daily. Verify it afterwards with
`kirocrew snapshot --list`: an unverified backup job is the same as no backup.
