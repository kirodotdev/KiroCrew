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
| memory | `memory.db`, `memory_index.db`, `workspace/memory/`, `workspace/knowledge/` |
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

## What leaves the host, and what redaction is for

The local archive and the off-host copy are the same bytes unless you ask otherwise.

What protects the uploaded bundle is the destination, not a rewrite: the bucket is created
private and every upload re-asserts the whole set before sending a byte — all four
public-access blocks, default encryption, ACLs disabled via `BucketOwnerEnforced`,
versioning, no bucket policy at all, and the object write pinned to the expected owner
account. Any of those missing or unreadable refuses the upload. The audience for the
uploaded copy is therefore the same as the audience for your local disk: you.

On top of that you can opt IN to rewriting the copy that leaves, by writing
`{"redact_uploads": true}` to `redaction.json` inside your backup directory. Then both
mandatory outbound redactors run over the throwaway copy, and `config.json`'s token plus
anything credential-shaped in a note or a memory row is replaced with an inert tag.

It is off by default because it is not free. Replacing a credential is a variable-length
edit, so any file whose structure depends on byte offsets — an archive, a PDF, most binary
container formats — comes out the other side invalid. Paying that to re-protect a copy only
you can read is the wrong default. Turn it on when the bucket's audience is genuinely wider
than you believe, or when you want the off-host copy to be inert on principle.

One pass runs only here. The shared redactors also run over live model output and tool
results, where rewriting something that merely resembles a key corrupts what you are
reading, so they recognise specific vendor formats and specific field names. That leaves
real shapes uncovered — a bot token whose format they have no pattern for, or your own
`api_key = "…"` with an opaque value. This copy is a throwaway on its way off the host and
your complete archive stays local, so the egress pass can afford to be broader: it also
replaces any long quoted value assigned to a credential-ish field name, and any bearer
token shaped as three dot-separated segments. That is the same trade the over-reach note
below describes, made deliberately in the one place where the cost is one note's text
rather than a corrupted answer.

Two consequences worth knowing before you turn it on:

- **Restoring a redacted off-host copy gives you working memory and inert credentials.** The
  shape is complete and the databases are valid; the fields that authenticate are not.
  Re-enter them after restoring. The restore prints what was redacted, so you are told
  rather than left to discover it.
- **Redaction is pattern-based, so it can over-reach.** A note holding something that
  merely looks like a key can lose that text in the off-host copy. The local archive is
  unaffected, and the per-path replacement counts are printed at upload and again at
  restore so you can judge whether a count looks wrong.

Databases are redacted value by value through SQL rather than over their bytes. That is
not a stylistic choice: the redactors substitute a tag, so they change length, and
rewriting a SQLite file's bytes produces a file SQLite cannot open — which the restore
path would then correctly refuse as corrupt.

Search indexes and files whose only purpose is to be secret are left out of the
outbound copy entirely rather than blanked, because an inert key present in the bundle
is indistinguishable from a rotated one. Restore already reports an absent index and
what to rebuild. Those files are matched by their **exact position** in the bundle, not
by name: your workspace may hold a `telemetry_salt` or a `memory_index.db` of your own,
and leaving out a file that merely shares a name with one of the product's would be
losing your data, not protecting it.

Values are redacted on what they hold rather than on the column's declared type, so a
credential stored as binary is rewritten too. Bytes make the round trip through a
byte-preserving codec and are only written back when something actually matched, so
embeddings and other genuine blobs come out identical.

A database's **schema** is checked as well as its rows. A key can be written into a column
default, a view's body or a trigger, and none of those are values any row scan reaches.
Schema text also cannot be rewritten the way a value can — changing it means rebuilding the
object — so a database in that state is one this pass cannot clean, and the upload is
refused either way. Nothing is deleted to make an upload possible: `memory.db` and the
Knowledge Library are what the backup exists to carry, so sending the bundle without one
of them would report success and restore nothing.

Rows are scanned to a **fixpoint**, not once. An update fires the database's own triggers,
and a trigger can copy the pre-update value into a table the scan has already cleaned, so a
single pass can leave a credential behind in a place it already visited. Each pass reports
its own replacements and the scan stops when a pass changes nothing. A database that keeps
moving is one that cannot be shown clean, so the upload is refused and the database is
named rather than removed.

The **manifest** is redacted too, after it is stamped — it is the one file guaranteed to be
in the upload, and the stamp itself writes paths and error text into it. It is checked to
still parse afterwards, because a manifest that does not is a bundle that cannot be
restored.

Whether a file is text is decided by **decoding** it, not by its name — a workspace holds
whatever you put there, and a suffix list would classify your `.py`, `.csv` or
extension-less notes as opaque. A file that genuinely does not decode (an image, an
archive) cannot be shown free of credentials, so the **upload is refused** and those
files are named. They are not removed: a restore that reports success while quietly
lacking your own files is worse than an upload that stops and tells you. Narrow the
selection with `--components`, or turn redaction off for that run. A `.db` that is not
a database the product ships is treated the same way, and so is a database the product
DOES ship: whichever it is, the file is kept and the upload refuses, naming it and why.
Your local snapshot is complete and unaffected in every one of these cases.

Turning it on is a file, not a setting in `config.json`, and that placement is the point.
`config.json` is readable and writable by the agent, so a switch living there could be
flipped by the agent itself. The fence matters in BOTH directions now: an agent that could
turn this on could corrupt your off-host copy, and one that could turn it off could publish
a credential into the bucket. The backup directory is already fenced for the same reason its
destination record is — neither the agent's file tools nor any shell form can read or write
it. Only you can.

Four cases, and none of them is a silent guess:

- **No file** — off. The default, and it needs no file.
- **`{"redact_uploads": true}`** — on.
- **`{"redact_uploads": false}`** — off, written down explicitly, which is allowed.
- **Anything else** — a file that parses to neither, an unreadable one, or `"true"` as a
  string. The upload refuses and names the file. You wrote it on purpose, so guessing off
  would ignore a request to scrub and guessing on would rewrite files you may not have
  meant to touch. Your local snapshot is already written and is unaffected.

If redaction is on and cannot be completed, the upload is refused; it never falls back to
sending the unredacted bundle.

`--purpose share` currently refuses whatever you select, and that is deliberate rather
than unfinished. Whether a component is safe to share is a question about its
**content**, not its shape: a workspace file, a skill, a cron's `env` map, a
notification body or a lesson you pasted a token into can each carry a credential, and
staging cannot tell. Marking components share-safe one at a time was tried during
review and guessed wrong twice, so nothing claims it until the redaction work behind
it exists. The purpose, the per-component declaration and the refusal are all live, so
the first certified component only has to change its own declaration.

For now, use `--purpose backup` — restoring onto a host you control is what this
feature is for. The bundle's manifest records the purpose and each component's
declaration, so a reader of a bundle can tell which they are holding.

A component added without a policy declaration is refused at staging rather than
defaulting to permissive, so a new component cannot inherit a permissive value by
omission.

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

In `replace` mode the state being overwritten is moved into a
`pre-restore-<timestamp>/` folder inside the data home first, and the path is
printed, so a wrong-snapshot restore is recoverable.

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
state is touched** — `memory.db`, `memory_index.db`, and
`workspace/knowledge/knowledge.db`. A snapshot whose database is unreadable or
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
