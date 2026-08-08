# Session Storage Module

## Overview

`src/kiro_crew/session_storage.py` measures what conversations cost on disk and
reclaims that space when a user asks. `src/kiro_crew/dashboard/handlers/session_storage.py`
exposes it over HTTP. Reclaiming stages files under `<data home>/trash/sessions/`
rather than unlinking them; emptying that trash is the only irreversible step and
the only one that returns space to the filesystem.

Nothing here runs on a timer. Every measurement is pulled by a request and every
move is initiated by a user action.

## One session, two stores

A conversation's bytes live in two places, owned by two programs:

| Store | Path | Read by |
|---|---|---|
| Transcript | `<data home>/sessions/<stem>.jsonl` + `sessions/archive/<stem>__<stamp>.jsonl` | Dashboard history, search, memory consolidation |
| Replay log | `<kiro home>/sessions/cli/<sid>.json` + `<sid>.jsonl` | kiro-cli, to resume a session |

That split is an implementation detail. `StorageReport` carries no per-store
breakdown and the HTTP payload has no field from which one could be derived, so a
client can only present a session as one thing with one size.
`test_session_storage_api.py::TestReport::test_payload_never_splits_the_two_stores`
pins the absence.

### Halves are always reclaimed together

`_unit_paths()` is the single answer to "what files is this session made of", and
move, restore and delete all resolve through it. Reclaiming one half produces a
session that is broken rather than gone: without its replay log a session still
lists but cannot resume, and without its transcript a resumable session has no
history or search. `TestMoveTakesBothHalves` and `TestRestoreIsAllOrNothing` fail
if either half is dropped.

Restore is all-or-nothing per session for the same reason. A file whose original
path is occupied again blocks its whole session from being restored — the occupant
is newer, and undoing a deletion must not cause one.

**Inside a per-file loop, every error path is a session-level failure.** A file
that cannot be sized, moved, or read from the manifest is a file the operation
cannot account for, and continuing past it commits a manifest that omits it — the
split this module exists to prevent, arrived at through an error path rather than a
concurrency one. So those loops `break` and roll back rather than skip. Per-*session*
and per-*batch* loops do skip, because that granularity is the unit of work: an
unknown session id or an unreadable batch affects only itself.

A move that fails part-way is rolled back, and so is a restore. If any of a
session's files cannot be staged, the ones already moved are returned and the
session is skipped; if any file cannot be put back, the ones already restored are
re-staged. A half-moved session is the broken state above *and* invisible: the
manifest would list the staged half while the rest stayed in place, so emptying the
trash would destroy one half of a session nobody knew was split. A half-restored
session is worse still — it is also **wedged**, because the manifest still names
files that are no longer in the batch, so every retry fails its own staged-file
check. `TestPartialMoveRollsBack` and
`TestRestoreIsAllOrNothing::test_a_failed_restore_stays_retryable` fail when either
rollback is removed.

A session's kiro-cli files are **enumerated, not assumed**. A session is identified
by its `.json` / `.jsonl` pair, but reclaiming takes every file whose stem matches
the session id, so a lock file or a sidecar a future kiro-cli version adds follows
its session instead of being orphaned. Kiro Crew is a co-owner of that directory's
layout here, and this is what keeps an unrecognised file from becoming a partial
removal.

Not every session has both halves, and that is normal: a subagent run leaves only
a replay log, and a session whose mapping was pruned leaves only a transcript. The
rule is that whatever halves exist move together.

### Pairing

A session key and its transcript filename differ, because `history` sanitizes the
key: `dashboard:chat-1` is stored as `dashboard_chat-1.jsonl`. Pairing therefore
goes through `history.transcript_stems()`, never a second copy of that rule. A
duplicated rule would drift the moment `history` changed, and the failure is
**silent and destructive** — a missed pairing reclaims one half of a session.

`transcript_stems` returns **every** name a key's transcript could occupy, not just
the canonical one: a Slack thread predating the canonical `slack:<ts>` session key
still logs under its bare `thread_ts` filename, and `ConversationLog._path` falls
back to it. Knowing only the canonical stem would leave such a transcript looking
like it belongs to no session — and therefore reclaimable while the session is
still resumable.

`SessionIndex` consequently maps **stem → session id**, not the reverse: one
session legitimately owns several stems. It is supplied by the caller rather than
read inside the module, so the exclusion set is explicit at the call site and a
test can pin it. The handler builds it from `SessionMap.mapped_sids_by_key()`.
`test_session_storage_api.py::TestIndexConstruction` pins the resolved stems as
literals rather than by calling the resolver, so a resolver that stopped returning
the legacy stem fails instead of agreeing with itself.

## An instance that cannot see who is live must not reclaim

The exclusion set comes from **this** instance's session map, but the kiro-cli
replay store can be shared. When `KIROCREW_HOME` is overridden while `KIRO_HOME` is
not — a dev gateway or a pod — this instance has its own map and the machine-wide
store, so every session belonging to the default instance is missing from the map it
consults: a resumable conversation reads as retired and could be staged and then
emptied out from under a gateway this process cannot see.

`reclaim_block_reason()` detects that configuration and `move_to_trash` refuses
outright. Two things about how it decides:

- It compares **resolved paths**, never whether the environment variables are set.
  Both overrides are validated and silently fall back to the default when they name
  an unsafe target, so `KIRO_HOME=/etc` beside an isolated `KIROCREW_HOME` leaves
  the process on the shared store while a presence test reports it isolated.
- It decides by **containment**, not by comparing the store against its default
  location. Once the data home is isolated, the store must be that data home or live
  inside it. A default-location test would pass the arrangement where sharing is
  least visible: two isolated instances pointed at one *custom* `KIRO_HOME` see
  neither the default store nor each other's maps.
- The **legacy pre-migration home counts as a default**, not as isolation. An
  install that has not yet migrated legitimately resolves to `~/.kirocrew`, and
  treating that as an isolated instance refused every such install.
- The refusal is **symmetric**. A default instance is also blocked when a
  discoverable co-tenant shares its store: a pod isolates `KIROCREW_HOME` but
  deliberately not `KIRO_HOME`, so each pod home under the pod root reads the
  machine-wide replay store while keeping its own session map — and from the default
  side, the pod's sessions read as retired. `_replay_store_cotenants()` enumerates
  the pod root (host-side state at a known location) and the message names the
  eviction command, because a refusal a user cannot act on is not better than the
  hazard. A dev gateway pointed at some other `KIROCREW_HOME` is **not**
  discoverable and remains a Known Limitation.

Because that check reads real host state, tests must isolate `KIROCREW_POD_ROOT`
alongside the homes, or their result depends on whether the machine happens to have
pods.

The freshness floor narrows the window but does not close it, since a session idle
for a day is still resumable. Isolating both homes, or neither, is safe. The reason
is surfaced in the report as `reclaim_blocked_reason` so a client can explain rather
than offer an action that can only be refused.

### The index is re-read after the scan, inside the lock

Scanning a six-figure store is the slow part of a reclaim, so an index read *before*
it is already stale by the time anything moves. `move_to_trash` therefore re-reads
through a `refresh` callable **after** the scan and immediately before the move loop,
making the authority check the freshest view available. The two active sets are
**unioned**, so a re-read can only ever add protection, and a re-read that fails
refuses the operation rather than proceeding on the stale view.

The residual window is now the move loop itself. A session mapped during it is still
staged — but staged means *in the trash*, fully restorable, and destroying it needs a
second explicit `empty`. Closing the window completely needs the session/slot writer
and this module to share one lock; that is recorded in Known Limitations rather than
implied away.

## What is reclaimable

A session is excluded when its ID is still in the session map: that is a session
the product can resume, and moving its files from under a live slot breaks it with
no error the user would connect to the action.

**The session map is not a complete registry of live sessions**, which is why
mapping alone is not the guard. A subagent run creates a kiro-cli session that was
never mapped — on the measured install, a third of sampled sessions were
subagent-created — so a threshold of `0` would otherwise reclaim a conversation
running right now and break its resume. `MIN_RECLAIM_AGE_DAYS` is therefore a hard
floor no caller can lower: freshness is the one signal that does not depend on
which subsystem owns a session, because a live session is being appended to. The
floor is enforced in `move_to_trash` as well as in the selection helper, since the
move is the chokepoint a caller could otherwise bypass by passing IDs directly.
Sub-floor sessions are also left out of the reported reclaimable figure, so it
never promises bytes no threshold can move.

Age is the newest mtime across every file a session owns. A transcript is appended
to while a session runs, so keying on an older metadata file or a long-since
rotated segment would make a live session look stale.

`select_reclaimable()` is separate from `move_to_trash()` so a caller can show the
exact count and size before anything moves. The selection is re-derived at the
moment of the move rather than accepted from the client, because the numbers on a
screen may be minutes old.

### Mutations are serialized across processes

`move_to_trash`, `restore` and `empty_trash` each hold an exclusive file lock at
`<data home>/trash/session-storage.lock`. Two interleaved reclaims can select the
same session and land one half in each batch, after which neither batch can restore
it and emptying either destroys half a session. The lock is a *file* lock rather
than a thread lock because instances share the kiro-cli store — a pod and the live
gateway both read `~/.kiro/sessions/cli` — so in-process exclusion would exclude
nothing. `platform_compat.file_lock` fails closed, so a lock that cannot be taken
raises instead of running the section unserialized.

## The trash

Staged batches live at `<data home>/trash/sessions/<batch id>/`, with each file
kept under a `cli/` or `crew/` subdirectory. The two halves can share a filename,
and a flat batch directory would let one silently overwrite the other — turning a
reversible move into data loss.

On a default install both stores sit under `~/.kiro`, so staging is a
same-filesystem `os.rename`: instant regardless of size, and instantly reversible.
A data home mounted apart from the kiro-cli store falls back to `shutil.move`,
which copies — correct, but slow and needing the space twice while it runs.
`StorageReport.trash_same_filesystem` reports which case applies.

**Staging does not free space.** The bytes stay on disk until the trash is
emptied. `StorageReport.trash_bytes` and the payload's `trash.still_on_disk` exist
so a client can say so; a client that reports a reclaim as freed space contradicts
its own payload.

### Manifests

Every batch carries an append-only `manifest.jsonl`: a header line, then one line
per session recording each file's staged path, original absolute path, and size.
Restore reads origins from the manifest instead of reconstructing them, so a layout
change in either store cannot send a restored file to the wrong place.

Append-only is load-bearing twice over. A batch can span six figures of sessions,
so rewriting a whole-document manifest per session would cost quadratic bytes; and
an interruption leaves every completed line intact, so a partial batch stays
restorable. A trailing partial line is skipped rather than failing the batch —
`TestTrashAccounting::test_a_truncated_final_line_does_not_lose_the_batch`.

A batch with no readable manifest is omitted from `list_trash()`: its files could
not be put back, so offering it as restorable would be a false promise.

**The directory is the batch's identity, not the manifest header.** A header
claiming a different batch id would make a targeted empty delete the batch it named
rather than the one it came from, so a disagreement is treated as corruption and the
batch is withheld. `TestBatchIdentityIsTheDirectory` covers both the tampered case
and the invariant that every listed id resolves to its own directory.

**A move that cannot be recorded is rolled back.** If appending a session's manifest
entry fails — a full disk is the realistic case — its files are already out of live
storage while nothing names them, which is strictly worse than never having moved:
unresumable *and* unrestorable. The partial line is rewound and the files are put
back. `TestManifestPersistenceFailure` fails when the rollback is removed.

## Path safety

A caller-supplied session or batch ID is joined onto a directory, so `_UNIT_ID_RE`
rejects anything that could address a file elsewhere — separators, parent
references, leading dots, and over-long names.

A **link** planted under the trash root defeats that check by having a legal
name, so `_batch_dir()` — the one place every caller resolves a batch id through —
also refuses a path that is a link or that resolves outside the resolved trash
root. The two checks are not redundant: containment catches a link pointing
*outside*, and the link check catches one pointing at **another batch inside** the
trash, where emptying the alias would destroy a real batch the caller never named.

The link test goes through `platform_compat.is_link_or_junction()`, not
`is_symlink()`, which reports False for an NTFS **junction**: on Windows a junction
named as a valid batch id would read as a real directory and the delete would
resolve through it. `list_trash()` uses the same resolver for the same reason.

Containment is anchored to the **data home**, not to the trash root alone. The root
sits under a directory the agent can write, so it could itself be replaced with a
link; resolving relative to it would then accept batches under whatever it points at.
Both `_batch_dir()` and `list_trash()` therefore require the resolved root to live
beneath the resolved data home. This is a data-loss guard, not hardening: with the
anchor removed, a self-consistent batch directory outside the home is enumerated as a
batch and `empty` deletes it outright.

The root must also **not be a link itself**, and that is a separate rule rather than a
duplicate. The anchor catches a linked *ancestor* (a link at `<data home>/trash`
escapes once resolved, while the root stays a real directory). The link test catches a
linked *root* pointing somewhere **inside** the data home — the live sessions or
archive tree — which satisfies both the anchor and per-batch containment. Measured
with the link test removed: the live archive segment is enumerated as a batch and
`empty` deletes it.

`list_trash()` skips links rather than raising, so a planted link cannot wedge
"empty everything"; naming that id explicitly is still refused.
`empty_trash()` additionally re-resolves each target and confirms it is inside the
trash root, so a tampered manifest cannot direct the delete outside it.

Restore refuses a staged path that is a **symlink**, because `is_file()` follows
links: one resolving inside the batch passes the `rel` validation, and moving it
would put a link where the session's data belongs — leaving a dangling pointer once
the batch is emptied. Only a link resolving *within* the batch reaches this check;
`_staged_path` already refuses one that escapes.

### The origin is derived, not trusted

A manifest record's `origin` is not information restore needs: the staged path
already encodes the store *and* the filename, and the filename **is** the session's
identity (`<sid>.jsonl` for a replay log, `<stem>.jsonl` for a transcript). So
`_canonical_origin()` derives the destination from `rel`, and the recorded origin is
only checked for **agreement**.

This matters because "inside a session store" is not a sufficient test: a tampered
in-store origin names a *different session's* file, so both the containment check and
the traversal check pass while the restore corrupts a session the user never touched.
Deriving removes the choice; the agreement check then turns a disagreeing manifest
into a refusal rather than a silently ignored field.

### Restore never replaces an occupied origin

The preflight rejects an origin that already exists, but the session can be recreated
in the interval before the move, and `os.rename` replaces the destination silently —
so undoing a deletion would destroy the newer data it exists to protect.
`_move_file_exclusive()` creates the destination exclusively (`os.link`, falling back
to `O_CREAT | O_EXCL` across filesystems), making the check and the write one atomic
step. A lost race rolls the session back and **retains** its manifest entry, because
restoring the rest would splice two generations of one session together.

`_rollback()` uses the same exclusive move. A rollback runs *after* something already
failed, so the origin may have been recreated in the meantime, and a plain rename
would turn a handled failure into data loss. An occupied origin leaves the file
staged, where it stays recoverable.

### The leftover scan fails closed

`_unlisted_files()` exists to BLOCK deletions, so an empty result must mean "nothing
is unaccounted for" and never "the walk gave up early". It therefore walks with error
reporting and **raises** when any directory or stat fails, instead of returning a
short list — `rglob` skips unreadable directories silently, which would convert a
transient error into permission to delete a file that is a session's only copy.

Each caller maps that raise onto the outcome it already has for finding leftovers:
discarding a restored batch keeps it, and `empty_trash` **skips** that batch rather
than aborting, so one unreadable batch cannot make the whole trash un-emptyable.
Both directions delete nothing, which is the safe one.

### The manifest is untrusted input

It lives under the data home, which the agent can write, so restore validates both
ends of every record rather than acting on it:

- `_staged_path()` refuses an absolute or traversing `rel`. This matters because
  `Path("/a/b") / "/etc/passwd"` is `/etc/passwd` — joining an absolute string
  discards the base entirely, so an unchecked `rel` would let restore pick up any
  file on the host.
- `_origin_path()` accepts an origin only inside the session or archive stores.
  Restore *writes* to that path, so an unconstrained origin is an arbitrary-write
  primitive: a tampered manifest could otherwise relocate a credential file.

A record failing either check blocks its whole session, consistent with
all-or-nothing restore. `TestManifestIsUntrusted` covers the absolute, traversing
and out-of-store cases.

## APIs

All three mutations are gated on `_is_restricted_session` and audited through the
SEL. Every non-2xx body carries a machine-readable `code`.

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/system/session-storage` | Totals, age buckets, and the staged batches |
| POST | `/api/system/session-storage/cleanup` | Stage sessions older than a threshold; `dry_run` reports without moving |
| POST | `/api/system/session-storage/restore` | Return a batch, or named sessions within it |
| POST | `/api/system/session-storage/empty` | Delete staged batches for good; needs `batch_ids` or `all: true` |

The GET is uncached: it walks both stores, so it is meant to be fetched when a
screen opens or after an action, not on a poll. Every operation is offloaded with
`asyncio.to_thread` because a store reaching six figures of files is far too much
filesystem work for the event loop.

Error codes: `restricted_session` (403), `invalid_body`, `invalid_threshold`,
`cleanup_refused`, `invalid_batch`, `nothing_specified`, `restore_refused`,
`empty_refused` (400).

A selection larger than `_MAX_SELECTION` stages the **oldest** that many sessions
and returns `remaining` rather than refusing. Refusing would dead-end the install
the feature exists for — a store already at six figures cannot get under the cap by
any threshold a client could pick — and oldest-first makes repeating the call
monotonic progress.

### Omitted is not the same as malformed, and destroying takes explicit intent

`uids` widens its operation when **omitted** — an absent `uids` restores the whole
batch — so a present-but-malformed value must never collapse into that.
`_optional_str_list()` returns a distinct sentinel for that case, a non-object or
unparseable body yields `None` rather than `{}`, and the handlers answer 400.
Filtering was the trap: a bare string filters to nothing, which is
indistinguishable from absent.

**Emptying has no widening default at all.** It requires either `batch_ids` or
`all: true`. This is the only irreversible endpoint, and an "omitted means
everything" default put that outcome at the end of *every* path that produced an
empty body — a malformed payload, a wrong-typed field, a forgotten argument. Three
separate ways of reaching it were found before the default itself was removed;
guarding each entrance was losing to removing the destination.

### Nothing deletes a staged file the manifest does not list

A process exit between moving a file into a batch and appending its manifest line
leaves a staged file nothing points at — and it is the only copy of that session's
data. `_unlisted_files()` compares what is on disk against what the manifest names,
and **all three** removal paths consult it: a fully-restored batch, the
"nothing staged" cleanup path after a failed rollback, and `empty_trash`.

Emptying is the one worth spelling out. An interrupted batch can list *zero*
sessions while holding real files, so the trash shows it as empty — meaning a
user's "empty this batch" is consent for nothing while destroying something. Such a
batch is skipped and logged rather than deleted, so `freed_bytes` under-reports
instead of the trash over-deleting. `TestEmptyTrash` and
`test_restore_never_deletes_a_file_the_manifest_omits` cover both directions.

## Constants

| Constant | Value | Location |
|---|---|---|
| `TRASH_DIR_NAME` / `TRASH_SESSIONS_LEAF` | `trash` / `sessions` | `session_storage.py` |
| `STAGE_CLI_LEAF` / `STAGE_CREW_LEAF` | `cli` / `crew` | `session_storage.py` |
| `MANIFEST_NAME` | `manifest.jsonl` | `session_storage.py` |
| `MANIFEST_SCHEMA` | `1` | `session_storage.py` |
| `BUCKET_DAYS` | `(7, 30, 90)` | `session_storage.py` |
| `MIN_RECLAIM_AGE_DAYS` | `1.0` | `session_storage.py` |
| `MUTATION_LOCK_NAME` | `session-storage.lock` | `session_storage.py` |
| `ARCHIVE_SEGMENT_DELIMITER` | `__` | `history.py` |
| `_MAX_SELECTION` | `200000` | `dashboard/handlers/session_storage.py` |

## Known Limitations

- **A residual race with session-map writes remains.** The authority check is
  re-read after the scan and inside the reclaim lock, so the window is the move loop
  rather than the whole scan, but the session map's writer does not take that lock. A
  session mapped during the loop would still be staged. It stays restorable — nothing
  is destroyed without a second explicit action — but closing this fully requires the
  session/slot code and this module to share one lock, which is a wider change than
  this surface.
- Reclaiming is offered by age only. Selecting an individual session to reclaim is
  not exposed, so a single large conversation cannot be targeted.
- The trash never expires. It grows until a user empties it, which means reclaiming
  space takes two deliberate actions rather than one.
- `measure()` walks both stores on every call with no caching, so a store with
  hundreds of thousands of files makes the endpoint slow to answer.
- A session reclaimed while its mapping still exists is refused rather than having
  its mapping cleaned up first, so a stale map entry blocks reclaiming until
  `SessionMap.prune()` removes it.
- A session's size counts its identifying `.json` / `.jsonl` pair and its transcript
  files. A kiro-cli sidecar is *reclaimed* with its session but its bytes are not
  attributed to it, so the reported total slightly under-counts a store holding
  sidecars.
- Emptying the trash removes kiro-cli's index entry along with the file, but does
  not notify a running kiro-cli process, which may hold its own view of the session
  list until it restarts.
