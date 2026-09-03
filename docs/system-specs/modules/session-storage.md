# Session Storage Module

## Overview

`src/kiro_crew/session_storage.py` measures what conversations cost on disk and
reclaims that space when a user asks. `src/kiro_crew/session_digest.py` reads a
single session's content for the detail view. `src/kiro_crew/dashboard/handlers/session_storage.py`
exposes both over HTTP. Reclaiming stages files under `<data home>/trash/sessions/`
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
client can only present a session as one thing with one size. An inventory row
likewise carries a single `bytes`.
`test_session_storage_api.py::TestReport::test_payload_never_splits_the_two_stores`
pins the absence.

### Halves are always reclaimed together

`_unit_paths()` is the single answer to "what files does staging move", including replay sidecars and transcript archive segments. Restore instead consumes one manifest entry at a time and either returns every listed file in that session or leaves the entry staged; emptying calls `_unlisted_files()` before deletion. Those distinct seams keep a move, undo, or deletion from producing a half-session. `TestMoveTakesBothHalves` and `TestRestoreIsAllOrNothing` fail if either half is dropped.

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

`reclaim_block_reason()` blocks an isolated data home whose replay store is outside that home: its local session map cannot establish ownership of the shared store. It compares resolved paths and requires containment rather than checking environment-variable presence or a default path, so an unsafe override that falls back to the shared default cannot appear isolated. The legacy pre-migration home is a default, not isolation. `reclaim_block_reason` defaults to `cached=False` so mutation gates like `_move_to_trash_locked` always evaluate the live co-tenant state; display aggregators (`measure()`) pass `cached=True` to reuse the recent co-tenant scan from `list_units()`.

`cotenant_sids()` handles discoverable pod candidates differently. A pod with its own replay store cannot own files in this store. A candidate that claims sessions but has no own store, or whose map cannot be read or parsed, makes `move_to_trash()` refuse because it can resume a session after the pre-move snapshot. A dev gateway at an arbitrary data home is not discoverable and remains a Known Limitation.

The freshness floor narrows the window but does not establish ownership, so the report exposes `reclaim_blocked_reason` for a client to explain rather than offering an action that `move_to_trash()` must refuse. Tests that inspect this host state isolate `KIROCREW_POD_ROOT` with the homes.

### The index is re-read after the scan, and again per session

Scanning a six-figure store is the slow part of a reclaim, so an index read *before*
it is already stale by the time anything moves. `move_to_trash` therefore re-reads
through a `refresh` callable **after** the scan, and then again before **every**
session the move loop reaches. The two active sets are **unioned**, so a re-read can
only ever add protection.

The loop needs both signals it has, because they catch different resumes.

A resume that **writes** is caught by mtime. Every source file is stat'd there anyway,
to record its size in the manifest, and that same stat carries an mtime. A candidate
qualified by being untouched for `MIN_RECLAIM_AGE_DAYS`, so a source whose mtime is
newer than the instant the reclaim began has been written to since every read that
certified it — which an idle session's file cannot be. This costs no extra syscall:
the detection rides on a stat the loop already performs.

A resume that only **reads** writes nothing, so mtime has nothing to see: the resume
reads the old transcript to rebuild history and records the turn that follows under a
newly mapped sid, leaving every file's mtime days old. That resume is visible in the
index instead — it is mapped — which is why the re-read runs at the same per-session
cadence rather than once before the loop.

Either way the whole session is left in place, whatever already moved for it is rolled
back, and it is named in `TrashBatch.revived` so the caller can report it. A session
the re-read catches has nothing staged yet, so there is nothing to roll back.

The per-session re-read has to be affordable, and that is the caller's half of the
contract: `refresh` may be called once per selected session, and returning the **same
object** as last time says "nothing has moved", which the loop recognises by identity
and does not re-derive sets for. `_build_index` costs a file read plus a full json
parse — about 0.26 ms even against a 100-entry map — so a rebuild per session would be
~56 s at that floor and hours against a realistic map, at the `_MAX_SELECTION` cap.
The dashboard's refresher therefore rebuilds only when `session_map.json`'s
`(st_ino, st_mtime_ns, st_size)` moves; every mapping write lands via `mkstemp` plus
`os.replace`, so the inode changes on every write and an unmoved token cannot hide
one. A caller that returns a fresh index every call is correct, only slower.

A re-read that fails **before** anything moves refuses the operation rather than
proceeding on the stale view. One that fails **during** the loop is logged once and
the last view read is kept: every re-read only widens the live sets, so a lost one
costs the extra protection it would have added, not the protection already read, and
abandoning a batch that has already moved files would be the worse trade.

Naming a session in `revived` is a claim -- "this one was left where it was" -- so it
is only made when the rollback actually completed. Putting a file back is
deliberately non-overwriting, so the very resume that triggered the detection can
have recreated an origin and made the rollback decline. Two things then have to hold
before the call returns: whatever is still staged is **appended to the manifest**, so
restore and empty can both reach it (safe because restore also moves exclusively, and
therefore declines the origin the resume recreated rather than overwriting the live
session with the stale copy); and the call **raises**, naming the batch, instead of
reporting a revival that is not true. Everything that did move is still described by
the manifest, so the partial batch stays restorable.

The anchor is taken **before** the scan, not after the authority checks. Taken later
it would miss a session resumed during the scan or between the index re-read and the
loop, which is most of the elapsed time. Taking it early adds no false positives,
because a candidate has to be untouched for `MIN_RECLAIM_AGE_DAYS` to qualify at all.

Detection, not prevention. For a resume that writes, the window is the gap between one
file's stat and its rename. For a resume that only reads, it is the gap between the
re-read that precedes its session and that session's files moving, plus however long
the map write takes to become visible in the file this reads — bounded by
`SessionMap._FLUSH_DEBOUNCE_SECS`. Either way what lands in that gap lands in the
trash — fully restorable, with destruction still needing a second explicit `empty`.

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

## The inventory

A user reclaiming space acts on named conversations, not on an age threshold, so
the surface is a list of sessions rather than a report of totals. Each row is one
session — the two stores stay collapsed into one item with one size — carrying
`uid`, `title`, `origin`, `bytes`, `mtime`, `active`, `live` and `background`. The
screen is `website/src/pages/system/SessionStorageScreen.tsx`.

### `active` means resumable; `live` means a turn is running

These are two different facts and the module keeps them apart, because a badge that
conflates them tells the user something they can disprove by reading the date next
to it.

`active` is membership in the session map: the product could resume this
conversation. It is what refuses a reclaim, and its meaning is unchanged — no
refusal logic was rewritten to add the inventory. `live` is a separate, additive
signal populated from `DashboardState.running_session_keys()`: a turn is in flight
*right now*. A session idle for three weeks is `active` and not `live`.

Both are still refused. The distinction only decides what the user is told —
`in_use` for `live`, `resumable` for merely `active` — and a reason a user can
verify is the difference between a guard and an unexplained failure.
`TestWhyAReclaimIsRefused` pins both halves:
`test_an_idle_recorded_session_is_refused_as_resumable_not_in_use` and
`test_the_row_reports_running_separately_from_resumable`.

`running_session_keys()` snapshots `self._slots.values()` into a list before
iterating. It is called through `asyncio.to_thread` while the event loop can still
mutate that dict, and iterating it directly raises `RuntimeError: dictionary changed
size during iteration`.

Each check is by sid **or** by stem, because a unit's own sid can be empty. A
transcript-only unit is caught through `active_stems` / `live_stems`, which
`SessionIndex` derives by mapping `stem_to_sid` through the respective sid set:
`active = sid in index.active_sids or any(stem in active_stems for stem in owned)`.

`live` is serialized as a plain boolean, so an install with no running-state signal
at all reports `live: false` for every row — the payload has no third "unknown"
state. `false` therefore means *no running turn was observed*, not *provably idle*,
and it is safe to render only because it never loosens a refusal: `active` alone
still refuses, and `live` only chooses which reason is shown.

### The list is cheap; the detail is not

The split between the two read endpoints is a cost boundary, not a taste.

A row needs a title, which `list_sessions()` takes from the transcript's first
metadata line, cached on mtime. A session that never got a title falls back to a
**bounded re-read** — up to the first 21 lines, looking for the first user message —
also cached on mtime. So a list costs roughly one `readline()` per titled session
and a short bounded scan per untitled one. A detail needs the first message,
the turn count and the image count, which requires reading the **whole** file.
Those are orders of magnitude apart on a large conversation, and a store with
612 sessions cannot serve a full read of every one of them when a screen opens. The
detail endpoint is therefore per-uid and lazy, and must never be called in a loop
over the list.

`session_digest.digest()` streams line by line and never raises on malformed,
truncated or binary input: a store that has been interrupted mid-write is the
normal case, not the exception, and one bad file must not blank the screen.

**Turns are counted, not estimated.** `history.list_sessions()` exposes a
`messages` field that is `st_size / 200`; presenting that as a turn count would be
a silent lie, so the digest counts records.

### `background` is the absence of a transcript, not absence from the map

`background` is `not unit.stems` — the session has no transcript half, which is
what an unregistered subagent run looks like. Keying it on the session map instead
sweeps titled-but-retired conversations into the anonymous group, and those are
exactly the rows worth showing, since being unmapped is also what makes them
reclaimable.

### Transcript-derived text is redacted before it leaves the process

`title` and `first_message` are conversation content, so they pass
`redact_exfiltration_urls()` then `redact_credentials()` — the same order
`_serialize_artifact` uses. `origin` gets the same treatment: a session id is
rendered, `_UNIT_ID_RE` admits the alphanumeric shape of an access-key id, and a
credential pasted into an id would otherwise reach the dashboard verbatim.

The boundary also accepts only strings: `isinstance(row["title"], str)` guards a
non-string title, which otherwise reaches the redactor and raises `TypeError:
expected string or bytes-like object`.

Refusals resolve to the session's already-scrubbed title rather than printing the
raw uid. A uid is the action handle, so omitting an unsafe row would make that
session's space both unreclaimable *and* invisible; the fix is that uids stop being
display text, not that rows disappear.

`dashboard/handlers/session_storage.py` is registered in `_REDACTION_SINKS`
(`security_posture.py`). The registry is a ratchet — adding a redactor call without
registering the module fails
`test_every_redactor_call_site_is_a_registered_sink_or_allowlisted`.
`TestTranscriptContentIsRedacted` covers both surfaces, and
`TestAMalformedTitleCannotCrashTheList` covers the type guard.

## The trash

Staged batches live at `<data home>/trash/sessions/<batch id>/`, with each file
kept under a `cli/` or `crew/` subdirectory. The two halves can share a filename,
and a flat batch directory would let one silently overwrite the other — turning a
reversible move into data loss.

On a default install both stores sit under `~/.kiro`, so staging is a
same-filesystem `os.rename`: instant regardless of size, and instantly reversible.
A data home mounted apart from the kiro-cli store falls back to a copy — correct,
but slow and needing the space twice while it runs.
`StorageReport.trash_same_filesystem` reports which case applies.

**The cross-filesystem copy is atomic and durable, in that order.**
`_copy_across_filesystems()` copies into an `INCOMING_PREFIX` name beside the
destination, carries the source's metadata onto it, `fsync`s it, publishes it with
`replace_with_retry`, `fsync`s the destination's directory, and unlinks the source.
Nothing there is cosmetic: copying straight to the final name (what `shutil.move`
does) spends the whole copy with a partial file under the name the manifest will
record; `copystat` runs BEFORE the file `fsync` because it writes the inode and the
`fsync` is what forces the inode out, so the other order can publish the bytes under
the temp's own `0600` and creation time — and this module ages sessions by their mtime;
and unlinking before the destination `fsync` can return to a durable absence of the
source with the destination's bytes still in the page cache.

**Nothing that can raise runs after the unlink.** The staging loop reads an exception
from the mover as "this file did not move" — it rolls the session back and omits the
file from the manifest — so raising after the file HAS moved produces a half-staged
session nothing records, which is the split the rollback exists to prevent. The source
directory still wants syncing, because copy-then-unlink is TWO operations (unlike the
same-filesystem rename, which is one, so forcing the destination side forces the
removal with it) and a crash between them can leave BOTH names: a live session the user
was told had been reclaimed beside a staged copy the manifest records as trashed. So
the loop collects the drained directories and `_sync_batch()` syncs them once, after
the batch is fully recorded, `best_effort` — a non-durable source entry can only
resurrect a duplicate, never lose a copy. `test_nothing_that_can_raise_runs_after_a_file_has_moved`
and `test_the_drained_source_directories_are_synced_once_per_batch` pin both halves.

Because an exception out of a mover MEANS "nothing of this move survives", the failing
DESTINATION SYNC withdraws the publication before re-raising — safe there because the
source has not been touched yet, so removing the destination provably restores the state
the call started in. The `src.unlink()` that follows is deliberately NOT treated that way,
and the difference is the point. A filesystem can report a failure for an unlink that DID
commit (a network filesystem losing the reply to a completed operation), and the source
name existing afterwards does not prove the ORIGINAL still does: a concurrent append
recreates that name with a new, empty file, at which point the destination holds the only
copy of the history. Nothing observable at that instant separates "the unlink failed" from
"the unlink succeeded and something recreated the name", so the destination is kept and
the exception propagates untouched. The leftover is then a file no manifest entry names,
which `_unlisted_files()` refuses to delete and which blocks emptying that batch until an
operator clears it — a worse report than the truth, but recoverable, which bytes nobody
can get back are not. `test_the_mover_keeps_the_copy_when_the_unlink_fails` and
`test_the_exclusive_mover_keeps_the_copy_when_the_unlink_fails` pin it on each mover, and
`test_a_failed_unlink_keeps_the_published_copy` pins the end-to-end case where the name is
recreated.

Only a COPY can dereference a symlink: `os.rename` moves the link itself and `os.link`
links to the link, so neither mover's fast path can leak through one. Their copy fallbacks
read through `_open_source_no_follow()`, which sets `O_NOFOLLOW` and refuses a link in the
source's final component rather than reading what it points at — in both directions,
because a session file swapped for a link to a credential store would copy the SECRET's
bytes into an agent-readable trash, and a link planted in the trash would copy arbitrary
bytes back out over a restored origin. Staging does not hand it a link today, since every
enumeration scan uses `is_file(follow_symlinks=False)`, so this is hardening rather than a
reachable hole being closed: it makes the copy safe on its own terms instead of by
inheritance from a filter several hundred lines away.
`test_the_copy_helper_refuses_a_symlink_instead_of_reading_its_target` pins it at the copy
and `test_a_planted_source_symlink_never_reaches_the_trash` pins the end-to-end outcome.
The final component is what a swap replaces; a symlinked PARENT directory is still
traversed, which needs an `openat` walk down the chain and is its own change.

The incoming name is naming only: `_unlisted_files()` deliberately does NOT exempt it,
because a name says nothing about who wrote it and this module's threat model already
assumes a batch can be planted in. So a crash mid-copy leaves debris that blocks
emptying that batch until an operator clears it — the same posture main already has for
an interrupted cross-filesystem move, whose partial file is equally unlisted.
`TestCrossFilesystemStaging` fails if the copy publishes under the final name, skips
any of the syncs, or if the scan starts passing over the incoming prefix.

**`fsync_dir()` is quiet only where a directory sync cannot be expressed.** Windows
has no directory descriptor to open and some network mounts reject `fsync` on a
directory, so those return without complaint; every other errno is raised. The
distinction is load-bearing rather than fussy, because each caller's next step is to
destroy the only other copy — a swallowed `EIO` would hand it a false "durable" just
before the unlink. `best_effort=True` downgrades even that to a warning, and is for
callers whose operation is ALREADY COMMITTED (the drained source directories above; the
vault's key and store writes, where a raise would report a stored secret as never
written and drop it from a migration's rollback set). It is a keyword rather than a
bare `except OSError` per call site so the decision is visible and still logged.
`TestFsyncDirDiscriminates` fails if a device error is logged away by default or if a
platform without directory descriptors starts raising.

**Staging does not free space.** The bytes stay on disk until the trash is
emptied. `StorageReport.trash_bytes` and the payload's `trash.still_on_disk` exist
so a client can say so; a client that reports a reclaim as freed space contradicts
its own payload.

### Manifests

Every new batch starts an append-only `manifest.jsonl`: a header line followed by one entry per staged session recording each file's relative staged path, original path, and size. `_append_entry()` flushes each entry, and `_sync_batch()` `fsync`s the manifest and then EVERY staged directory level, deepest first, at each point `_move_to_trash_locked()` can leave a batch on disk. That is what makes a move that RETURNED durable: the moves are renames whose metadata the filesystem commits on its own schedule, so without it a power loss could come back to the sessions' only copies under an empty manifest — and a batch with no readable manifest is omitted from `list_trash()`, out of reach of restore and of empty alike. Every level, not just each file's own parent, because `mkdir(parents=True)` also creates the intermediates and a directory's name lives in its PARENT's entries — a session with archive segments but no transcript never otherwise records `crew` at all, and on the FIRST batch a machine ever stages the same call creates `trash/` and `trash/sessions/`, whose own entries `_fsync_tree()` walks down from the data home. A fresh chain is ALSO synced at the moment it is created, before any file is renamed into it: the same-filesystem move is a rename, which gives up the file's only other name in the same atomic step that creates the new one, so a rename that reaches disk while the `mkdir` that created its parent does not would leave no name resolving to the file at all. That sync costs one call per new directory rather than one per file, so the per-file path stays a bare rename; if it fails the session is rolled back and left in place, exactly as for a file that would not move, rather than abandoning a batch whose manifest has not been forced out. A failing sync RAISES: the batch is the only copy, so reporting a durable batch that is not one is worse than reporting a failure for work that partly happened. The single exception is the "resumed while being staged" path, which is already raising an error that names the session and locates its fragment; there a sync failure is logged so that pointer is not replaced by an `OSError`. None of this makes a crash mid-move consistent; a batch interrupted halfway still holds files its manifest never named, which is what `_unlisted_files()` is for. `_manifest_records()` retains every valid record it can read and ignores a trailing malformed record, while `_unlisted_files()` prevents deletion of files that no retained manifest entry names. `TestTrashAccounting::test_a_truncated_final_line_does_not_lose_the_batch` pins that recovery posture, and `TestStagedBatchDurability` fails if a returned move left the manifest or any staged directory level unsynced.

A partial restore is the exception to append-only growth: `_rewrite_manifest()` writes the replacement through `atomic_write(..., fsync=True)` and then `fsync`s the batch directory, so both the new content and the rename that gave it the manifest's name are durable before the call returns. Without the directory half a power loss could return the pre-restore manifest, whose entries name files restore has already moved back into live storage. A batch with no readable manifest is omitted from `list_trash()`: its files could not be put back, so offering it as restorable would be a false promise.

**A manifest that is a link stops the empty before anything is removed**
(`SKIP_UNREADABLE`), and the check runs BEFORE the manifest is read and before the
platform branch, because both delete paths act on what it says. It uses
`platform_compat.is_link_or_junction()` rather than `is_symlink()`: on Windows a
junction reports False for the latter, and the coarse path is the Windows path.
Without it there, `rmtree` removed the link and left any staged file it could not
delete -- a locked one -- so the batch lost its listing and kept its data.

A manifest cannot be treated as this batch's own: the product writes it with
`atomic_write`, so a link at that name was not written here, and the entries an
approval was computed from were read through it.

The descriptor path checks the same thing AGAIN from its pinned scan, and that is
not duplication. The first check is computed from a path, so it cannot see a link
planted after it; the scan's view can. It is refused rather than deferred to the end
with the real manifest, because the scan records a link as a LINK and the link
removal pass unlinks every link it recorded -- while the manifest is only excluded
from the FILE pass. That is what made this reachable: the listing went early, and a
batch that then failed to finish left files on disk that `list_trash()` omits, so
the user could neither see them nor restore them.
`test_a_symlinked_manifest_is_refused_rather_than_unlinked` and
`test_the_coarse_path_refuses_a_linked_manifest_too` cover the path check on each
branch, and `test_a_manifest_linked_after_the_path_check_is_still_refused` covers
the scan check; removing either check reds only its own tests.

**Each staged directory is removed under a name nothing can predict.** The
approved-inode chain check admits only the directory the approval named, but
`rmdir` addresses a NAME and so did that check, so an actor with write access to
the parent could swap the name in between and have an unapproved directory
removed on another one's approval. `_remove_scanned_dirs()` therefore renames the
name to `.<name>.removing-<random>` in the same parent, re-checks dev/ino there
against the approved map, and removes THAT. A swap that beats the rename moves
the intruder within its own parent instead of deleting it, and is then refused and
renamed back by `_restore_staged_dir()`, so a refusal never leaves a directory
under a name the user cannot recognise. Removal by name is what the coarse batch
path and the manifest already avoid this way.
`test_a_directory_swapped_after_its_identity_check_is_not_removed` covers the
interval, and `test_a_directory_is_removed_under_a_name_nothing_can_predict`
covers the unguessable name; the first fails if the re-check goes, the second if
the rename does.

A REFUSAL leaves the object under the staging name; only a matched identity is renamed
back. Renaming back on a mismatch writes to a name the refusal has just proved is not
ours, and `rename` replaces its destination. POSIX limits what that can destroy -- a
directory rename fails against a file (`ENOTDIR`) and against a non-empty directory
(`ENOTEMPTY`), so the reachable loss is an empty directory rather than data -- but the
rule is not worth having conditionally: the same courtesy applied to the manifest, which
IS a file, and there it destroyed the only copy (hence `os.link` on that path). Restoring
is correct in exactly one case, when the identity matched and only the removal failed, so
that name is the object's own.

**The batch's own directory is re-pinned the same way** (`_remove_pinned_batch()`),
and it is the one where a name-addressed removal did the most damage rather than
the least: the final scan proves the batch empty by descriptor, and by the time it
is removed the manifest has already been moved aside, so a swap in that interval
removed an empty replacement and left the real batch holding data with nothing to
list it -- while the caller reported success. The name is moved to
`.<batch id>.removing-<random>`, checked against `os.fstat(batch_fd)`, and only
that name is removed; a refusal raises, so the existing recovery renames the
manifest back THROUGH the descriptor and the real batch stays listed.
`test_a_batch_swapped_before_its_removal_is_refused_and_keeps_its_manifest` fails
if the identity check goes -- and fails by reporting success, which is the defect
itself.

**The post-condition authenticates the survivor by inode, not by name.** Everything
after it treats whatever answers to the manifest's name as the batch's own manifest:
it is renamed aside, and once the batch is gone the debris is unlinked. So a file
substituted at that name after the first scan would satisfy "nothing left but the
manifest" and then be destroyed -- an unapproved file whose only copy it is, deleted
for matching a name. The survivor's inode must equal the one the first scan recorded
in `present`, and a mismatch reports `incomplete` and leaves the file untouched --
not even moved aside.
`test_a_file_substituted_at_the_manifests_name_is_not_destroyed` fails if the
comparison is reduced to the name, and it fails by reporting success.

**The move-aside checks what LANDED, since it cannot check first.** The
post-condition verifies the manifest's inode and the rename that follows addresses
its name -- two syscalls apart, the same irreducible interval the leaf unlink has,
because POSIX has no rename-by-inode either. What can be checked is the result: if
the debris is not the file that was verified, the rename moved something else, and
the unlink that ends the successful path would destroy it. It is left as debris
instead, with both names and both inodes logged at ERROR. The real manifest was
already replaced by then, and that loss is not this code's to undo -- but it does not
have to add a second one.
`test_a_file_swapped_after_the_post_condition_is_not_deleted_as_debris` fails if the
landed check is removed, and fails by reporting success.

**Putting the manifest back fails rather than replaces.** The manifest is moved
aside under a random debris name so the batch can be removed, and restored if that
fails -- but POSIX `rename` REPLACES its destination silently, which is the very
property the debris name is randomised to be safe against, and the restore direction
needs the opposite guarantee. If anything has written a `manifest.jsonl` into the
batch since ours went aside, renaming over it destroys the only copy of a file this
code has never read. The restore is therefore `os.link`, which fails with `EEXIST`,
and the debris is unlinked only after the batch has its manifest back, so no window
has neither. `os.link` is part of the `_FD_SAFE_DELETE` capability set for this
reason: a platform that cannot do it takes the coarse path rather than reaching a
recovery it cannot perform safely.
`test_manifest_recovery_never_overwrites_a_manifest_that_arrived_since` fails if the
restore goes back to `rename`.

`link` is not universally available, so the restore has a fallback and the fallback has a
precondition. A filesystem without hard links refuses `os.link` outright, and the capability
probe cannot see that -- it tests whether the OS accepts `dir_fd`, not what the MOUNT
supports. Failing there would leave the batch holding data with no manifest, the exact loss
this recovery exists to prevent. A non-`EEXIST` failure therefore falls back to an
EXCLUSIVE CREATE and a copy (`_copy_back_exclusive()`), not to a rename after checking the
name is free: that check and that rename are two syscalls, and a file arriving between them
is replaced -- the same trusted-a-name mistake every other guard here exists to remove.
`O_CREAT | O_EXCL` decides in ONE syscall, so there is no window to lose, and it needs no
hard-link support, so it strands nothing. The cost is a copy rather than a link, paid only
on a filesystem without links and only when a batch has already failed to empty. A copy that
fails part way is REMOVED rather than left: a manifest holding some of its entries lists some
sessions and silently drops the rest, which reads as a smaller batch rather than as damage. `EEXIST` itself needs no fallback -- something holds that name, so the batch is
still listable, and the debris is left for a human.
`test_a_filesystem_without_hard_links_still_gets_its_manifest_back`,
`test_the_fallback_still_refuses_to_overwrite_an_arriving_manifest` and
`test_a_failed_copy_back_leaves_no_half_written_manifest` pin the three cases; dropping
`O_EXCL` reds the second, and dropping the partial-file cleanup reds the third.

**Recorded links are checked before they are unlinked, exactly as files are.**
"Removing a link destroys nothing" describes the link the scan SAW, not whatever
holds that name when the pass runs: a regular file moved onto a recorded link's
name is data, and unlinking it would be the loss the file pass's identity check
exists to prevent. `_scan_batch()` therefore records each link's inode rather than
just its path, and the pass demands `S_ISLNK` plus the recorded dev/ino before
unlinking. This closes the scan-to-unlink interval; the two syscalls between the
check and the unlink remain the same POSIX residual the leaf file has, for the same
reason. `test_a_file_swapped_onto_a_scanned_link_is_not_unlinked` fails if the
check is removed.

**The directory is the batch's identity, not the manifest header.** A header
claiming a different batch id would make a targeted empty delete the batch it named
rather than the one it came from, so a disagreement is treated as corruption and the
batch is withheld. `TestBatchIdentityIsTheDirectory` covers both the tampered case
and the invariant that every listed id resolves to its own directory.

The APPROVAL re-checks the same agreement, because the listing check happens earlier and
the manifest is read again afterwards. A directory swapped into the selected name between
the two brings its OWN manifest, and everything else about that approval is
self-consistent -- identity, files and digest all describe one directory. What they do not
describe is the batch the user SELECTED, and the name is the only link back to that
selection. `_header_names_this_batch()` is the check; note it only ever COMPARES the
header, never resolves in its favour, so the rule above still holds -- a disagreement
withholds the batch.
`test_the_approval_binds_identity_and_size_to_one_directory` renames a second real batch
over the selected name and expects a refusal; it fails if the comparison goes.

That comparison demands EQUALITY, not merely the absence of disagreement. `_write_header()`
always writes `batch_id` beside `schema`, and a summary is returned only for the current
schema, so within what reaches the approval the id is always present and its absence means
the header was tampered with. Treating a missing id as "nothing to disagree with" is the
fail-OPEN reading and it is the one an actor gets to choose: strip the field and a swapped
batch walks through. `list_trash()` keeps the looser form deliberately -- it decides what to
OFFER, not what to delete, so a batch with a stripped header is still listed and simply
cannot be approved, which surfaces as a refusal on a named selection and as an
`unreadable_batch` skip on the sweep rather than as a batch that silently vanishes from the
screen.
`test_a_header_with_no_batch_id_is_refused_rather_than_waved_through` fails if the approval
goes back to the looser form.

The SCAN that produces that map has a check-to-use window of its own, and it is the one
place that could not be allowed to trust a name: every other guard here is downstream of
the map it builds. `scandir` lists a name and the child is opened a moment later, so a rename
in between records the REPLACEMENT's inode -- the approval then blesses the impostor and the
delete, validating faithfully against that map, removes it. `_scan_batch()` compares the
opened descriptor's inode against `entry.inode()` from the listing and refuses on a
mismatch.
`test_a_directory_swapped_between_the_listing_and_the_open_is_refused` covers it, and the
hook placement matters: swapping inside a patched `scandir` happens BEFORE the entries are
materialised, so both sides see the impostor and agree -- the swap has to land in the open.

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

Within a batch the delete removes the files the MANIFEST names, one at a time, and never
discovers a file by walking. That is what lets progress be reported per file rather than
per batch, and it is also the safer form: a walk has to decide per entry whether to
descend, and on Windows a junction is not a symlink — `os.path.islink` reports False for
one — so a walk descends into it and unlinks the files it points at, outside the trash.

The batch itself is opened by walking from the filesystem ROOT, one component at a time
with `O_NOFOLLOW`, because that flag constrains only the last component: opening the batch
by path left the trash root and everything above it — writable by the same user — to be
re-resolved, so an ancestor swapped to a link after validation was followed. The walk is
`pinned_fs.pin_parent`, not a second copy of it: that module exists because two closed PRs
(#2446, #2447) tried to spell the mechanism per call site and neither converged, so a second
spelling is the failure it was created to end. `supports_pinned_tree_walk()` and its
`_dir_flags()` come from the same place; what this module adds is only the three mutating
calls this path makes relative to a descriptor. The path walked is the RESOLVED one, so this
cannot refuse an install whose data home legitimately sits behind a symlinked home
directory.

Each named file is then removed by `(directory fd, name)`, with every directory component
opened `O_NOFOLLOW` from the batch down, and the emptied directories go the same way —
bottom-up by descriptor, including the batch itself through its parent's descriptor. NO
step of that path resolves a path: checking a path and then unlinking it re-resolves the
prefix, so a component swapped to a link in between is followed, and finishing with
`rmtree(batch)` re-resolved the whole prefix, sending the removal outside the trash even
though the walk above it was pinned.

Removing the emptied directories is driven by that approved map rather than by discovery,
deepest first, each through a descriptor chain that admits only the approved inode — the
directory being removed included, since `rmdir` addresses a name and a top-level staged
directory's parent is the batch itself. A recursive sweep that enumerated children and
descended into whatever answered would remove an empty directory swapped in after
verification; nothing here is discovered, so a directory the approval never named is not
opened and not removed. Both walks are ITERATIVE: a recursive one raises `RecursionError` on
a deeply nested tree, which is not `OSError`, so it escaped the callers that turn a failed
read into a refusal. A tree deep enough still fails, but with `EMFILE`, which is handled.
Whether the batch is empty afterwards is then answered by a fresh pinned scan — nothing may
remain but the manifest — instead of by the sweep that was also deciding what to delete.

The manifest and the batch directory then go TOGETHER, or neither goes. `rmdir` cannot run
while the manifest is inside, and unlinking it first left a window a file created after the
scan turned into silent loss: the `rmdir` failed on a non-empty directory and the batch, now
without a manifest, left `list_trash()` with that file inside it. So the manifest is RENAMED
to the trash root under `.<batch-id>.manifest.jsonl.removing-<random>`, the batch is removed,
and the debris unlinked; if the `rmdir` fails the manifest is renamed straight back. The
suffix is random for the same reason the coarse staging name is: `os.rename` replaces an
existing destination silently on POSIX, so a deterministic name is one an actor with write
access to the trash root can plant a file at and have the rename destroy its only copy.

The removal never unlinks a regular file it does not recognise: callers reach it only after
the unlisted-file guard has passed, so a regular file appearing there is unaccounted-for
data and the batch is reported as kept instead. A link recorded by the scan IS removed,
because removing a link destroys nothing, and leaving one would make a batch holding it
impossible to empty for good. Where the platform supports neither `openat` nor
`O_NOFOLLOW` (Windows) the batch is renamed to an unguessable staging name, verified there,
and taken with `rmtree(ignore_errors=True)`, and progress degrades to one report per batch —
a smoother bar is not worth a weaker delete. On that path the bytes freed are measured after
the attempt and the survivors subtracted, because `ignore_errors=True` returns quietly when
a locked file leaves the batch standing, and the batch's absence is checked as a
post-condition so a tree that could not be removed is reported as kept rather than counted
as emptied.

Every manifest name goes through one validator, `_plain_parts`, before it is stat'd or
unlinked. Absoluteness is asked of the PATH in BOTH flavours rather than by comparing
components against separator spellings: `PurePosixPath("//tmp/x").parts` is
`("//", "tmp", "x")`, whose root a check against `"/"` misses, and an absolute path
handed to `os.open` ignores `dir_fd` entirely; on Windows `..\x` is a parent reference
and `C:\x` is absolute, neither of which POSIX parsing sees. A Windows DRIVE is refused
separately from absoluteness, because they are different questions: `C:.ssh/id_rsa` is
drive-relative and not absolute, yet joining it onto the batch replaces the anchor and
resolves against that drive's working directory, so the size read would stat a file
outside the batch and report its existence and size. Refusing any `.drive` also catches
`c:y`, and as collateral refuses a POSIX file named `a:b` — accepted, since this store
names its own files and the cost is a batch kept as incomplete rather than an escape.
An embedded NUL is refused with no such trade, because no file name can hold one: a JSON
manifest CAN carry `\u0000`, and `os` then raises `ValueError` rather than `OSError`, so
it escaped the handler that is supposed to skip a bad name and aborted the delete partway
through — leaving a batch half-removed. The one site that hands a manifest name to a
syscall therefore catches `(OSError, ValueError)`: one unusable entry costs its own file
and nothing more. The rule lives at the name because the version that wrote it inline at
one call site left the sibling size read statting whatever the manifest said.

Restore refuses a staged path that is a **symlink**, because `is_file()` follows
links: one resolving inside the batch passes the `rel` validation, and moving it
would put a link where the session's data belongs — leaving a dangling pointer once
the batch is emptied. Only a link resolving *within* the batch reaches this check;
`_staged_path` already refuses one that escapes.

### All three removal paths are descriptor-bound, and one module owns the mechanism

Emptying is the largest of the three removals but not the only one. Discarding a
fully-restored batch and cleaning up a batch no session was staged into also remove a
whole directory, and both did it with `shutil.rmtree(path)` - which re-resolves every
ancestor, so a directory above the trash swapped to a link after the caller's own by-path
read is followed and the removal lands outside the trash. Both now go through
`pinned_fs.remove_tree_pinned`: parent chain pinned one `openat` per component, batch
opened through it `O_NOFOLLOW`, one scan, and every removal inside addressed by a
descriptor whose inode that scan recorded.

Pinning is the weaker half of that, and the spec says so because the code does.
`Path.resolve()` FOLLOWS an ancestor that is already a link, so a swap landing before the
resolve produces a faithful pinned walk to the wrong tree. What closes that is an IDENTITY:
each caller records the batch's `(st_dev, st_ino)` before ANY read of the batch's contents - at
`target.mkdir(...)` under the mutation lock on the staging path, and at the TOP of
`_restore_locked`, before its first manifest read, on the restore path - and the approval hook
compares the pinned root's `fstat` against it as its first question. An unreadable identity
refuses rather than proceeding.

The restore's capture point is load-bearing and is why `_discard_restored_batch` takes the
identity rather than sampling one. That cleanup is reached only after the manifest has been read
and every listed file moved out, so an identity taken there would already be a substitute's if an
ancestor had been swapped during any of that, and every later check would then agree with itself
about the wrong directory. Anchored at the top instead, the same comparison refuses a swap that
landed at any point inside the operation. A swap already in place BEFORE the restore begins is
still followed - `pin_parent` resolves the parent path too, so an ancestor that is already a link
cannot be told from a real path - but reaching that requires the victim tree to have served as
the restore's own source, which means content was already injected into the live session stores;
the removal of the emptied directory afterwards is not the damage.

The content checks come second and are not what carries this. The approval also re-asks,
from ONE inode-verified read of ONE file, that the manifest's header claims this batch's own
directory name, and that NOTHING but the manifest remains - not "nothing unlisted", nothing
at all. Both callers arrive with the batch already empty of files: the restore path has moved
every listed file back out, and the staging path has a manifest with no entries. So a file at
a listed path is not the listed file; it arrived at that name afterwards and may be the only
copy of whatever it is, and consulting the listing would authorise deleting exactly that on
the strength of a name the manifest happens to mention. The header check establishes that
emptying the batch is *correct*, not that it *is* the batch: an actor who can write into the
tree a swapped link points at can plant a header naming the selected batch, and every content
question then answers yes about the wrong directory. An inode cannot be forged by writing
files. A withheld approval removes nothing and keeps the batch, which stays listed and
restorable; reporting a success that did not happen is what `ignore_errors=True` did.

"Nothing but the manifest" includes LINKS, which these two paths refuse rather than remove even
though the delete path removes scanned ones. A link holds no data of its own, but it is the only
entry a removal can take by NAME: `_unlink_verified` compares the scanned inode and then unlinks,
and POSIX has no atomic "unlink if this inode", so admitting a link admits that two-syscall
window - plant one, let the approval pass it, put a file at that name in between, and the removal
deletes the file. Neither of these removals was asked for, so they refuse; `empty_trash` still
clears such a batch on the user's own say-so, through an approval that does remove links.

Where the platform has no `openat`/`O_NOFOLLOW`, these two paths remove the batch through
`rename-verify-remove` instead: `_stage_batch_by_name` renames it aside inside the trash root,
verifies `(st_dev, st_ino)` on the RENAMED directory against the identity the caller captured,
and only the staged name is removed; a mismatch or an unreadable identity puts the directory
back under the name the user saw, and so does a tree that will not go. Refusing outright was an
earlier answer here and it was wrong: this branch is the whole of Windows, so refusing left a
batch behind after every restore and every rolled-back move, still listing sessions it no longer
held - five pre-existing tests read that as a failure, and so would a user. What the rename buys
is that the approved name no longer exists once it has happened, so nothing can be substituted
at it, and the identity is checked on the directory that was actually moved. What it does not
buy: the removal still resolves the staging path, so an actor who can OBSERVE that name inside
the window can redirect it through an ancestor swapped afterwards. That residual is accepted on
this platform for all three callers - the explicit empty and the two cleanups - through one
owner rather than three spellings.

Because the cleanup can now DECLINE, a fully restored batch can outlive it - and every entry
in its manifest describes a file the restore already moved back OUT, so that batch shows a
user sessions that are not in it and offers to restore them. That stale listing is an ACCEPTED
RESIDUAL, not an oversight: clearing it needs a write to the batch path, and there is nowhere
safe to put one. After the cleanup the descriptor is closed and every refusal is itself
evidence the path may be redirected, so writing then answers a swap by writing through it.
Before the cleanup, the full-restore branch has no write at all today, so adding one hands
`atomic_write` - which REPLACES its destination - a path that a batch swapped to a link points
wherever an actor chose, including another batch's manifest, whose staged sessions would
become unlistable. A stale listing is visible and reversible; that is not. So neither cleanup
path writes to the batch, the full-restore branch of `_restore_locked` does not either, and
`empty_trash` clears the batch on the user's own say-so (verified in
`test_a_full_restore_never_writes_to_the_batch_it_is_leaving`). The partial-restore branch
keeps its pre-existing rewrite: it has to record what is still staged, and it is not reached
through a refusal.

`rename-verify-remove` - move a name to `.<name>.removing-<random>` in the same parent,
re-check the identity there through the parent's descriptor, remove only that name, and
rename BACK only when the identity matched - was spelled inline three times: the interior
directories, the batch directory, and the coarse path. It is now
`pinned_fs.remove_dir_verified`, with the pinned scan and the identity-verified chain-open
beside it, and `session_storage` keeps only the policy: which map authorises the removal,
what a refusal means to the user, and how it is worded. That split is deliberate rather than
tidy-up: per-call-site respelling of this mechanism is what `pinned_fs` was created to end
after #2446 and #2447, and a fourth copy would have repeated it. The descriptor-less platform
has its own owner, `_stage_batch_by_name`, because `pinned_fs` cannot serve it -
`remove_dir_verified` addresses a parent descriptor and that is precisely what this platform
lacks - and all three callers there (the explicit empty and the two cleanups) go through it
rather than respelling the rename. Which mechanism a call site gets is an explicit branch at
the call site rather than a fallback inside the primitive: `pinned_fs` refuses by design rather
than silently substituting a weaker one.

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
restoring the rest would splice two generations of one session together. On the copy
branch the destination is `fsync`ed and its directory `fsync`ed **before** the source
is unlinked, and the source's directory `fsync`ed after: that unlink is the moment the
copy stops being a second copy, buffered bytes on the far side of it are a session a
power loss can erase from both places at once, and a non-durable unlink can instead
leave both names — a session restored into live storage while its staged copy is still
in the batch under its manifest entry.
`test_a_cross_filesystem_restore_syncs_before_it_unlinks_the_source`
fails if either sync moves after the unlink.

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
- `_canonical_origin()` derives the restore destination from `rel`; `_origin_path()` only validates the recorded origin before restore requires it to agree with that derived destination. A manifest cannot choose another in-store session file or an arbitrary host path as the write target.

A record failing either check blocks its whole session, consistent with
all-or-nothing restore. `TestManifestIsUntrusted` covers the absolute, traversing
and out-of-store cases.

## APIs

All four mutations are gated on `_is_restricted_session` and audited through the
SEL. Every non-2xx body carries a machine-readable `code`.

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/system/session-storage` | Totals, age buckets, and the staged batches |
| GET | `/api/system/session-storage/sessions` | One row per session, biggest first; scan plus one `readline()` each |
| GET | `/api/system/session-storage/sessions/{uid}` | Lazy per-row detail: `first_message`, `turns`, `images` |
| POST | `/api/system/session-storage/cleanup` | Stage sessions older than a threshold; `dry_run` reports without moving |
| POST | `/api/system/session-storage/trash` | Stage an explicit selection of `uids`; reports `refused` per uid |
| POST | `/api/system/session-storage/restore` | Return a batch, or named sessions within it |
| POST | `/api/system/session-storage/empty` | Start deleting staged batches for good; needs `batch_ids` or `all: true`. Answers **202** with a job |
| GET | `/api/system/session-storage/empty` | The running or recently-finished empty, or `{"job": null}` |

Rows are sorted biggest-first on the **units**, before the payload is built:
sorting heterogeneous dicts does not type-check.

The collection GETs are not HTTP-cached and are not polling endpoints, but `measure()` and `list_units()` may reuse a short-lived process-local filesystem scan. `select_reclaimable()`, `move_to_trash()`, and the handler's `_classify()` pass an uncached read because a stale scan or co-tenant map must never decide a refusal. `invalidate_scan_cache()` runs after every mutation. `test_a_reclaim_does_not_select_against_a_cached_pass` and `test_the_pre_classification_never_reads_a_cached_cotenant_pass` pin that boundary.

The dashboard handlers offload every store-accessing path with `asyncio.to_thread`; `kiro_crew.session_storage` itself is synchronous, so any other async caller must do the same. `GET .../empty` is the exception that can run on the event loop because it reads only process-local job counters and touches no store.

Error codes: `restricted_session` (403), `unknown` (404, no such uid on the detail
route), `invalid_body`, `invalid_threshold`, `cleanup_refused`, `invalid_batch`,
`invalid_uid`, `invalid_selection`, `selection_too_large`, `nothing_specified`,
`trash_refused`, `restore_refused`, `empty_refused` (400), `empty_in_progress`
(409, an empty is already running — the body carries that job under `job`).

### Emptying is a job, not a response

Emptying tens of thousands of staged sessions is minutes of filesystem work. Holding
the request open for it left a client unable to say anything during the run: the only
irreversible operation in this surface, and no way to tell a running delete from a
stuck one. So the POST resolves what it will destroy, starts the work, and answers
202 with a job; progress is read from the GET.

The job is process-local, single-slot and NOT persisted. A second empty is refused
with 409 rather than queued (the storage mutation lock would serialize it anyway, and
one "working" state for two operations says nothing about either), and a job that
outlived its gateway would claim a delete is running when no thread is.

Its shape is `job_id`, `running`, `total_bytes`, `freed_bytes`, `error`, `skipped`.
`total_bytes` is what the staged manifests say the resolved batches hold — the same
figure the trash listing showed — so it is a denominator, not a remeasurement.
Progress is counted in BYTES, not sessions: the delete walks files and a session is
more than one file.

Two properties are load-bearing:

* **The set is resolved at request time, under the mutation lock, not in the
  worker.** `staged_targets()` takes the same lock staging holds, because
  `list_trash()` does not: an unlocked read can see a batch whose directory and
  manifest header exist while its sessions are still being moved in, and selecting
  that id makes the delete wait for staging and then destroy the finished batch -
  sessions the user never saw included. Under the lock a batch is either fully staged
  or not visible. Explicit ids are resolved through `_batch_dir()` first, the way
  every other caller resolves one: filtering the staged list alone silently dropped an
  id that is not a batch, so the worker got an empty list and the caller was told a
  delete it asked for had succeeded. A bad id is still a 400 `empty_refused`, and so is a well-formed id whose batch is
  no longer staged: `_batch_dir()` does not require the directory to exist, and
  filtering such an id out turned "destroy this" into a zero-byte success. `empty_trash(None)`
  enumerates the trash when it runs, which is now well after the click and can queue
  behind another mutation — so a batch staged in between would be destroyed although
  the user never saw it, and a staged batch is the only copy of those sessions. The
  handler resolves ids up front and hands the worker an explicit list. If that read
  fails for `all: true` the job settles with a reason and deletes NOTHING; for an
  explicit selection it proceeds, because the caller already named the set.
* **A finished job stops being reported after `_JOB_TTL_SECONDS`.** The slot would
  otherwise hold the last outcome for the life of the process and a screen would
  present it as current days later. The cutoff is applied server-side, where the
  clock is, so no client needs a timestamp.

`skipped` carries `SKIP_*` reason codes for every batch that is still there
afterwards, whether it was refused up front or survived its own delete: the delete
checks as a POST-CONDITION that the directory is gone, because
`rmtree(ignore_errors=True)` reports nothing and a tree it could not remove otherwise
left the batch on screen under a job that said it had succeeded.

`skipped` carries codes for batches deliberately KEPT. That is a
refusal a user has to be told: keeping a batch raises nothing, so an outcome read
only from the exception reported "0 bytes freed, success" above a batch still on
screen, with the reason in a log the user cannot read.

**The snapshot approves a directory, not a name.** `staged_targets()` fixes WHICH batches
an empty will destroy, under the mutation lock, so the worker cannot re-enumerate later
and destroy a batch the user never saw. It returns each id together with a `BatchIdentity`
recording the `(st_dev, st_ino)` of the batch directory AND the inode of every directory
inside it, all read while the lock was held, and the delete re-checks both — the batch with
`fstat` on the descriptor it opened, the interior against that same map —
`identity_changed` if either differs. Without it, the lock being released for the async
handoff meant a directory moved into an approved name would be deleted on consent given for
a different one, destroying sessions the user was never shown.

The interior half matters for the same reason and is easy to get wrong: `O_NOFOLLOW` refuses
a LINK, but a real directory RENAMED onto a staged directory's name is not a link, and the
batch's own inode does not change when something is renamed INSIDE it. A map built at
DELETE time cannot authorise the delete either, because it records the impostor along with
everything else — the map has to come from approval time. The batch check is on the
DESCRIPTOR, not a second stat of the path, because the fd is the object every removal
addresses: a swap after that point no longer reaches the data. A batch that cannot be read
at snapshot time is dropped from the set rather than carried unchecked.

The identity and the batch's SIZE come from the same pinned descriptor (`_approve_batch()`).
`list_trash()` reads each manifest by path and takes no lock, so its byte totals describe
whatever answered to that name then; recording the identity separately, by the same name,
could straddle a swap and pair the REPLACEMENT's identity with the original's numbers. The
manifest is therefore re-read through the pinned directory fd (`O_NOFOLLOW`, so a link at
that name is never mistaken for the manifest), and a batch whose summary cannot be read
that way is dropped from the approved set too.
`test_the_approval_binds_identity_and_size_to_one_directory` fails if the total goes back
to the listing's value.

The approval records the FILES and LINKS as well, for a sharper reason than symmetry with
the directories. The delete checks each staged file's identity, but it used to check
against a map its OWN scan built -- self-consistent, and authorising nothing. A listed
file replaced during the handoff had its replacement's inode recorded, matched, and was
unlinked: an unapproved file, whose only copy it may be, destroyed on consent given for a
different one. `BatchIdentity` therefore carries `files` and `links`, and the delete demands
equality of the whole map in both directions, exactly as it does for `dirs` -- a file added,
removed or replaced since the approval is `identity_changed` rather than something to
reconcile, and a concurrent restore that removed staged files lands there too.
`test_a_listed_file_replaced_after_approval_is_not_unlinked` fails if the comparison is
removed, and it fails by unlinking the replacement.

The LISTING is pinned too, by digest (`rels_digest`). The manifest's inode is not enough:
rewritten in place it keeps that inode, and every other identity check still passes because
no file changed -- only which files the delete believes it may unlink. A file already sitting
in the batch, unlisted, is refused at delete time; adding it to the listing after the
approval would have it deleted as though the user had approved it. A digest rather than the
rels themselves keeps the approval constant-size on a batch with six figures of entries, and
a refusal does not need to say which line moved. `test_a_manifest_rewritten_to_list_more
_after_approval_is_refused` fails if the digest comparison goes, and fails by deleting the
newly-listed file. The digest is captured FIRST inside the approval and read
through the pinned descriptor, before the interior scan. Taken after that scan it could
describe a manifest rewritten in between -- the NEW listing recorded against the OLD inode
maps, which authorises exactly the file the digest exists to refuse -- and taken by path it
could describe another directory's manifest entirely. Ordering it first also fails closed: a
rewrite after that point leaves the digest describing the old listing, so the delete refuses.
`test_a_manifest_rewritten_during_the_approval_does_not_authorize_it` fails if the capture
moves after the scan, and fails by deleting the smuggled file.

The approval resolves only the PARENT and re-joins the batch's own name. `Path.resolve()`
follows the final component too, so a batch directory replaced by a symlink resolved to its
TARGET and the pinned walk pinned that target: the approval would record another
directory's identity under this batch's id, and the delete, checking the identity it was
handed, would destroy session data from outside the trash. Re-joining keeps `O_NOFOLLOW` on
the component that matters, which refuses the link. `test_a_batch_replaced_by_a_symlink_is
_not_approved_as_its_target` points the name at a SECOND real batch -- one with a valid
manifest, because a target without one is refused for that reason instead and the test would
pass either way.

An id that a SUPPLIED approval map does not name is refused (`unreadable_batch`), not treated
as unapproved-and-therefore-unchecked. That is what lets the unnamed sweep stay honest without
blocking: `staged_targets()` raises for a NAMED selection it cannot verify, but on the sweep it
returns the batch in the id list WITHOUT an approval -- raising there would let one batch
damaged by a crash mid-append make the whole trash un-emptyable, which is why the delete loop
skips rather than aborts. The batch then comes back as a skip the user can read instead of
vanishing from the job beneath a success message.
`test_the_sweep_reports_an_unverifiable_batch_instead_of_dropping_it` fails if the membership
check goes -- and fails by DELETING the unverified batch, which is worse than the silent
success that prompted the check.

A selected batch that cannot be approved is a REFUSAL, not a silent omission. Dropping it
from the approved set is right -- no identity means no check -- but dropping it quietly
turned "destroy this" into a reported success for a batch still on screen, which is the
bug the missing-id refusal beside it exists to prevent. `staged_targets()` raises for a NAMED
selection, the same asymmetry that rule already draws: raising on the unnamed sweep would
let one batch damaged by a crash mid-append make the whole trash un-emptyable, which is why
the delete path skips rather than aborts. The dashboard already answers a
`SessionStorageError` from this call as a 400 rather than a job.
`test_a_batch_that_cannot_be_approved_is_refused_not_dropped` fails if the raise is
removed.

A snapshot that cannot be taken at all **cancels the delete**, for a named selection exactly
as for "everything staged": the request is answered as an already-settled job carrying the
reason, and no worker is dispatched. The named case used to proceed anyway, on the reasoning
that the caller had said WHICH batches and a missing snapshot only cost the progress bar its
denominator — but the snapshot is what turns those names into approval of the directories
they pointed at, so proceeding deleted whatever answered to the names by the time the worker
ran. The failure is not always benign either: a staged tree deep enough to exhaust
descriptors arrives as an exception, and writing into the trash is how it gets there.

That cancellation is AUDITED where it returns, with outcome `refused` and `snapshot_unreadable`.
Every other outcome of this endpoint reaches the SEL record inside the worker, and the named case
used to reach it too -- by dispatching, which is the loss above. Failing closed is right, but it
moves the request off the audited path, and the one irreversible operation in this surface must
not be able to be ATTEMPTED with no record that it was.
`test_an_explicit_delete_refused_by_a_failed_snapshot_is_still_audited` pins it.

Where the platform has no descriptor to bind to, the coarse path RENAMES the batch to
`.<batch-id>.removing-<random>` first — atomic within the trash root — verifies the identity
of the renamed directory, and removes it under that name. Checking a path and then handing
the same path to `rmtree` re-resolves it, so a swap in between was followed; after the
rename the approved name no longer exists, a swap that happened BEFORE it is caught and the
impostor renamed back rather than destroyed, and the name finally removed is unguessable.
Not airtight — `rmtree` still resolves the staging path — but failing closed instead would
refuse every empty on that platform, which is a worse answer than a window an attacker has
to guess their way into. A tree that will not go is renamed back, so the batch stays listed
and restorable rather than stranded under a name `list_trash()` does not offer.

A selection larger than `_MAX_SELECTION` stages the **oldest** that many sessions
and returns `remaining` rather than refusing. Refusing would dead-end the install
the feature exists for — a store already at six figures cannot get under the cap by
any threshold a client could pick — and oldest-first makes repeating the call
monotonic progress.

### A selection is pre-classified so one live row cannot void the whole request

`move_to_trash()` is all-or-nothing by design: one live or too-fresh session in the
list and the entire call raises, moving nothing. That is the right guarantee for the
module — a selection either happens or it does not — but it makes a bulk screen
useless, because a single row going live while the user reads the list would discard
the other forty-nine.

`_classify()` therefore splits the selection outside the lock and hands over only
the eligible uids, so on a settled store the module's refusal does not fire and the
per-row reasons reach the client. Each rejected uid comes back in `refused` with a
reason: `in_use`, `resumable`, `too_fresh`, or `unknown`.

**A residual window remains, and it is all-or-nothing.** A session that becomes
mapped or goes live *between* the pre-pass and the in-lock `refresh` is caught by
`move_to_trash`, which raises — and that raise takes the **whole** batch: the
handler answers 400 `trash_refused` and nothing moves, with no per-uid breakdown.
That is the conservative direction (never a wrong move), but a client cannot
distinguish it from a malformed request without reading the code, and retrying is
the only recovery.

A session that goes live *later still* — after the first `refresh`, while the move loop
is running — is not all-or-nothing. The loop leaves that one session in place and the
rest of the batch proceeds, and the handler folds each such uid into the same `refused`
list under `in_use`. A resume that writes is caught by the loop's mtime check; one that
only reads is caught by the per-session index re-read, which is why that re-read runs
inside the loop. The mechanism differs from the pre-pass (caught during the move rather
than by the index before it) but the reason a reader needs is identical, so it carries
no new code.

**The guarantee is not weakened.** `move_to_trash()` still re-reads the session map
inside the mutation lock and still unions the active sets, so the pre-pass can only
ever be more conservative than the authority, never less. Doing less than the user
asked *without saying so* is the defect this avoids — hence `refused` rather than a
silent drop.

`uids` has no widening default on this route. An omitted or empty selection is a
400 `nothing_specified`, because the endpoint exists to act on named rows and there
is no meaningful default for which rows those would be.

### A refusal is audited, including a partial one

Any non-empty `refused` emits a `denied` SEL event listing each uid and its reason.
Someone asked to remove specific conversations and was told no; that is a
security-relevant outcome, not a quiet detail of a 200. It fires on **partial**
refusal too — otherwise a request that took nine of ten sessions would leave the
tenth's protection unrecorded.

The resource string is **truncated at 512 characters**, so a large refusal loses its
tail: roughly a dozen uid-and-reason pairs fill it. The event still records that a
denial happened and how it was reasoned; it is not a complete manifest of a bulk
refusal. `TestRefusalsAreAudited` covers the fully-refused and the partial case
(`test_a_partial_refusal_is_audited_alongside_the_success`).

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

All three now also re-establish the answer through the descriptor they remove by, not only
by path. The by-path read is a pre-screen that produces the user-facing count; the pinned
re-read is what the removal is bound to, because a file arriving after the pre-screen - or
an ancestor swapped after it - makes the by-path answer describe a directory the removal is
no longer addressing.

## Constants

`kiro_crew.session_storage` owns the trash layout, manifest schema, age policy, and mutation-lock constants; `kiro_crew.dashboard.handlers.session_storage` owns the request selection bound. Callers import those constants rather than duplicating their literals, so code and tests remain the source of truth when the policy changes.

## Known Limitations

- **A much smaller residual race remains, and it is now bounded for both shapes of
  resume.** The authority check is re-read after the scan and again before every
  session the move loop reaches, and the loop also rejects any source whose mtime is
  newer than the batch's validation instant, so a session resumed *during* the loop is
  left in place and reported rather than staged — whether the resume writes or only
  reads. What is left is detection, not prevention, because the session map's writer
  still does not take the reclaim lock. Two gaps survive that. A resume that writes can
  land between one file's stat and its rename, which is microseconds. A resume that
  only reads becomes visible when its mapping reaches `session_map.json`, and
  `SessionMap` defers that write by `_FLUSH_DEBOUNCE_SECS` while the refresher reads the
  file rather than the live in-process map, so that gap is bounded by the debounce
  rather than by the loop. Both stay restorable — nothing is destroyed without a second
  explicit action. Removing either entirely still requires the session/slot code and
  this module to share one lock, which is a wider change than this surface.
- Reclaiming is offered both by age (`cleanup`) and by explicit selection
  (`trash`). Neither can take a session the map still lists, so targeting one large
  conversation only works if that conversation is unmapped.
- The trash never expires. It grows until a user empties it, which means reclaiming
  space takes two deliberate actions rather than one.
- `measure()` and `list_units()` may reuse a short-lived process-local scan, but the uncached selection and mutation paths still enumerate the stores; a large store remains expensive whenever a current answer is required.
- **Every SID returned by `SessionMap.mapped_sids_by_key()` is unreclaimable.** `_build_index()` makes those SIDs `active_sids`, and `move_to_trash()` refuses active units. `SessionMap.prune()` clears a stale SID only after its metadata file is absent; reclamation cannot clean the mapping first because that would make an otherwise resumable session eligible. A safe retirement flow needs ordering that remains correct if the process stops between changing the map and staging the files.
- The detail route reads whole files, so a client that calls it per row instead of on
  expand converts a cheap screen into a full scan of both stores.
- A session's size counts its identifying `.json` / `.jsonl` pair and its transcript
  files. A kiro-cli sidecar is *reclaimed* with its session but its bytes are not
  attributed to it, so the reported total slightly under-counts a store holding
  sidecars.
- Emptying the trash removes kiro-cli's index entry along with the file, but does
  not notify a running kiro-cli process, which may hold its own view of the session
  list until it restarts.
