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
kirocrew backup setup                                 # once: provision the S3 destination
kirocrew snapshot --components memory --to-s3         # off-host copy
kirocrew snapshot --list                              # list existing snapshots
kirocrew restore snapshot.tar.gz                      # auto-detects replace vs merge
kirocrew restore s3://my-backups/host1/snapshot.tar.gz # fetch, then restore
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

## Off-host copies (S3)

A snapshot written only to `~/.kiro/crew/snapshots` does not survive losing the machine,
which is the whole point of backing up. Sending it to S3 is two steps, and the split
between them is deliberate.

**Once, with you present:**

```bash
kirocrew backup setup
```

`setup` has to be authorized out of band. It refuses until a file exists at
`<data home>/backup/setup-authorized`, and that file must **name the destination it
authorizes**:

```json
{"account": "123456789012", "region": "us-west-2"}
```

Setup refuses unless the account and region it resolved match what you wrote, and deletes
the file once used, so one authorization is good for exactly one setup. You may also pin
`"bucket"`. There is deliberately no `--yes` or `--force`.

Naming the destination is load-bearing, not paperwork. A blank permission slip would only
prove that *somebody* approved *something*: with several AWS profiles registered, a token
you created intending one account could be spent with `--aws-profile other`, and every
later backup would go somewhere you never approved. The refusal prints the values this
invocation resolved so you can paste them — check them first, because that is exactly the
moment a wrong profile becomes visible.

The reason it is a file and not a terminal prompt is worth stating, because the obvious
design does not work. An earlier revision required a TTY and described that as a
human-presence check. It is not: a pseudo-terminal is something any process can allocate,
so `printf 'yes\n' | script -qec 'kirocrew backup setup …' /dev/null` satisfies both the
terminal test and the prompt.

**What the authorization is, and what it is not.** It is a record that an operator chose a
destination: a deliberate, out-of-band act naming the account and region, spent once. It is
**not** a control an agent cannot defeat, and this document will not claim otherwise. The
keystone floor over `<data home>/backup/` is two policy layers — `is_sensitive_path` makes
the agent's file tools refuse that path, and a deny pattern refuses shell commands naming
it — and neither inspects the *body* of a program. An agent that writes a script elsewhere
and runs it by path never names the keystone directory on a command line and never touches
it with a file tool. There is no separate uid and no ACL the agent process lacks, so
nothing below the policy layer objects. Making the stronger claim true needs a control this
program does not own: a PreToolUse denial of agent-issued `backup setup`, or an OS-level
keystone.

Read the gate for what it is worth. It stops the ordinary accident — a scheduled job or an
agent turn quietly provisioning a destination nobody chose — because that path runs
`backup setup` directly and hits the refusal. It does not stop a determined agent, and if
your threat model includes one, do not enable this feature and rely on the authorization to
contain it.

You still get a confirmation prompt naming the AWS account, region and bucket when a
terminal is present, because "wrong profile" is the mistake most worth catching. That
prompt is a convenience, not a security boundary.

The reason any gate is needed at all: everything else stops the backup path from being
*pointed* somewhere new — it takes no bucket from its caller, and the recorded destination
sits behind the same keystone floor. But `setup` writes that record through this program's
own code, so with no gate at all, an ordinary agent turn could pick a registered profile
belonging to another account, create a bucket there, and every later `--to-s3` would ship
your memory to it. The gate makes that require deliberate circumvention rather than being
the default outcome of asking an agent to set up backups.

What holds regardless of the gate: `snapshot --to-s3` takes no bucket from its caller and
writes only to what setup recorded; every write carries `--expected-bucket-owner`, so S3
itself refuses a bucket owned by another account; and setup verifies the bucket is private
before recording anything.

Once setup has run, `kirocrew snapshot --to-s3` needs no authorization and is safe to
schedule.

This creates a bucket in **your own** AWS account — private (Block Public Access on all
four flags), encrypted at rest (AES256), ownership enforced, **versioned**, with a
lifecycle rule expiring superseded versions after 30 days — then reads those controls
back from the API and refuses to record anything if AWS does not confirm them. The
bucket name, region and owning account are written to
`<data home>/backup/destination.json`.

Be clear about what that lifecycle rule bounds. Every run writes a new timestamped key,
so bundles are current versions and practically never become noncurrent: the rule bounds
the history of a *replaced* bundle (a corrupt re-upload, a same-second re-run), which is
what versioning is there to make recoverable. **It does not bound how many bundles you
accumulate** — that grows by one per run, and nothing deletes them. That is a deliberate
choice, not an oversight: a lifecycle rule can only say "delete older than N days", never
"keep the newest N", so on a machine that stopped backing up — the dead host this whole
feature exists for — an expiry rule would delete the last surviving copy of its memory
exactly when you need it. A few gigabytes a year is the better trade. Add your own
expiry rule if you want remote pruning; `kirocrew backup list` shows what is there.

If a bucket of that name already exists, `setup` refuses it unless **both** hold: it
carries this feature's own `kirocrew:backup` tag, and it has no bucket policy at all.
Either check failing — including a tag or policy that cannot be read — is a refusal, and
nothing is written or replaced first.

Both conditions guard a different harm. The tag answers "did Kiro Crew create this?",
because `setup` **replaces** the bucket's lifecycle configuration: pointed at an unrelated
bucket, it would discard that bucket's own rules and start expiring its old versions —
destroying someone's data in order to set up a backup. The policy check answers "who can
read this?", because the hardening step sets public-access, ownership and encryption
controls and none of those revoke a bucket policy, so a bucket already granting read
elsewhere would publish the memory it is about to receive.

Note what the tag is *not* doing: it is not evidence that a bucket is safe to put secrets
in — anyone who owns a bucket can tag it. It is a necessary condition for reuse, never a
sufficient one. A destination is created, not adopted; the escape hatch is to pick a name
that does not exist yet.

Re-running `setup` on the destination it already recorded is the repair path and stays
available regardless, so a bucket weakened out of band can always be fixed.

It is idempotent: run it again and it re-applies every control to the same bucket, which
is how you repair one that was weakened out of band. Default name is
`kirocrew-backup-<accountid>-<region>`; override with `--bucket`. The name carries no
username on purpose: bucket names live in one global namespace that anyone can probe for
existence, the account ID already makes the name unique, and adding an identity there
would only advertise whose bucket it is. Host identity appears in the *key* prefix
instead, where it is not publicly listable and where it helps you recognise which
machine a backup came from.

**Then, as often as you like:**

```bash
kirocrew snapshot --components memory --to-s3
```

That writes only to the destination `setup` recorded. There is no flag for naming a
bucket, and that absence is the design: a backup job must not be in the business of
deciding whether some arbitrary bucket is safe to write your memory into. Deciding that
automatically is not a job code can finish — a tag says who *intended* a bucket for
backup, a bucket policy has to be parsed to learn who can read it, and Block Public
Access does not neutralise a CloudFront origin grant. Creating the bucket ourselves and
refusing every other one answers the question once, visibly, with you there.

The one check that remains on every write is enforced by S3 rather than by Kiro Crew:
each upload carries `--expected-bucket-owner`, so a bucket that is no longer yours —
deleted and re-created by someone else under the same name — fails the write instead of
receiving your data.

Exposure is also re-checked immediately before each upload, because setup's verification
is only a point-in-time fact: a bucket policy can be added afterwards, and Block Public
Access does not stop a grant to one specific named account. If the bucket has acquired a
policy — **or if the policy cannot be read at all** — the upload is refused and your local
snapshot is untouched.

Refusing on an unreadable answer is deliberate. Warning and proceeding sounds kinder, but
a profile simply lacking `s3:GetBucketPolicy` makes the answer permanently unknown, so
every run would warn, you would learn to ignore the line, and the check would be
decorative exactly when it is load-bearing. What a refusal costs is the off-host copy for
that run, not the backup: the local bundle is already written, and the message names the
permission to grant.

Adding your own lifecycle rule is safe: `setup` reads the bucket's existing configuration
and replaces only the rule it owns, leaving yours in place. If it cannot read the existing
configuration it refuses rather than overwriting rules it cannot see.

Bundles are namespaced per machine (`backups/<hostname>/`), so several hosts can share
one bucket and a restore can name which machine's backup it wants:

```bash
kirocrew backup list          # bundles by host
kirocrew backup status        # what is configured, and whether it still checks out
kirocrew restore s3://my-backups/backups/laptop/kirocrew-snapshot-….tar.gz
```

The upload runs **after** the local bundle is written and locked down, and is retried on
a transient failure — so a flaky network costs you a retry, not the backup. A permission
failure is not retried: it will not fix itself.

AWS credentials are never stored or read by Kiro Crew. The `aws` CLI resolves them from a
profile *name*, taken from the same registry `deploy-web` uses, or from `--aws-profile
NAME`.

Scheduling is a cron, not a flag. The command is deterministic, so it belongs on a
schedule that runs it directly rather than dispatching an agent turn. A `command`-kind
Kiro Crew cron does exactly that and costs no tokens; note that `kirocrew cron add` does
not expose it, so create it from the dashboard's cron surface. A plain system crontab
entry works too:

```
0 3 * * *  kirocrew snapshot --components memory --to-s3
```

On a host with no OS-level sandbox backend (no Linux user namespaces, no macOS
`sandbox-exec`), the `aws` subprocess fails closed with a `SandboxUnavailableError` — the
same constraint `deploy-web` has. Set `agent.sandbox_allow_unsandboxed_exec=true` to
allow it, understanding that the subprocess then runs without credential isolation.

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

There is no built-in backup schedule. To get one, add a cron job that runs the
command, for example by asking the agent to schedule `kirocrew snapshot --keep 7`
daily. Verify it afterwards with `kirocrew snapshot --list`: an unverified backup
job is the same as no backup.
