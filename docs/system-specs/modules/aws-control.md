# AWS Control

AWS Control is the builtin account portal and S3-backed drive. It registers
selected local AWS profiles, groups their live identity probes by AWS account,
and exposes Drive, Library, Backup, cost, share, and IAM-policy views. The
builtin is declared by `kiro_crew.apps.builtins` and mounted by
`aws_control.backend.routes.register_routes`; the dashboard surface is
`website/src/apps/aws-control/`.

## Access boundary

Every AWS Control route passes `routes._guarded`. It refuses a disabled app and
any caller that is not the dashboard owner, and records either denial in SEL.
`test_aws_control_app.py::TestRouteRegistration.test_every_route_refuses_non_owner_when_enabled`
pins the owner boundary.

Every mutating route additionally passes `routes._mutating`. It refuses
restricted sessions and emits an SEL outcome for success, refusal, or an
`AWSError`; this is load-bearing because a restricted or non-owner session must
not turn an ordinary dashboard request into an AWS mutation. The mutating route
registrations in `routes.register_routes` cover profile registration, drive
operations, shares, library publication, and backup operations.

Account-targeted operations resolve the registered profile and then re-probe
its live identity in `routes._account_target`. A profile that has been repointed
to another account is refused instead of being used for the account named in
the URL. `test_aws_control_storage.py::TestFindDrive.test_a_bucket_owned_by_another_account_is_refused`
pins the related storage ownership check.

## Credentials and paid-service consent

The profile registry in `deploy.profiles` stores profile metadata, not keys or
tokens. It discovers names through the AWS CLI and writes only its allowlisted
configuration keys through `aws configure`; `credential_process` is a stored
command, not credential material. This separation is load-bearing because the
gateway passes profile names to the CLI provider chain rather than persisting
AWS secrets itself.

`aws_consent.GATED_SERVICES` includes S3 and Cost Explorer. A grant is scoped
to service, profile, region, and the account returned by the identity probe.
`aws_consent.authorize` consults a short-cached live identity probe before it
allows a gated call and withdraws a mismatched grant; unreadable, absent,
changed, or unresolved grants refuse the operation.
`test_aws_control_app.py::TestConsentExtension` pins the AWS Control service
registrations, and
`test_aws_control_app.py::TestDriveGuards.test_consent_refusal_answers_409_before_any_aws_call`
pins refusal before the drive handler calls AWS.

The confirmation surface and the operations resolve the same key, through one
policy. `accounts.resolve_default_account_profile` is the strict form: the
registry default picks the ACCOUNT, and `accounts._pick_profile` picks the key
within it — healthy first, the default preferred only among the healthy ones,
which is exactly what `_resolve_target` gives a request-driven operation. The
nightly backup loop (`hooks.py::_run_once`) calls it and skips the wake on None,
because a caller about to spend money must read "no working key" as "not now".
`accounts.resolve_consent_target` is the DISPLAY form the consent handler
(`dashboard/handlers/aws_consent.py::_effective_target`) calls for S3 and Cost
Explorer: same resolution, but with no working key it names the registry default
anyway so the card reports the credential error instead of rendering nothing.
That fallback reads the agent-writable registry directly, so it is `_safe_field`
scrubbed on the resolver's side of the return — the profile charset admits the
shape of an access key id, and the handler emits `profile` and `region` in its
JSON. If the resolver itself raises (it spawns the AWS CLI), the handler degrades
to the empty profile and `DEFAULT_REGION`, both module constants, rather than to
an unscrubbed read.
Resolving the default directly is a dead end: the card bound to an unhealthy
default has no account to confirm, `Confirm and enable` requires one, and the
account's healthy sibling key keeps serving every operation — a working account
with no way to authorize it. The same read in the unattended loop is worse than a
dead end, because the grant names the healthy key while the loop resolves the
broken one, so the backup skips on every wake with only a log line.
`test_aws_control_app.py::TestConsentTargetTracksTheOperation` pins the shared
resolution as an equality against `resolve_account_profile`, the key the nightly
loop gates on, the fallback scrub, and each degraded state.
The surface stays one grant per service, so the account the default names is
still the account a confirmation applies to; a grant recorded for one account
and used under another fails closed in `aws_consent.is_granted`, and the gate's
own row names the account it would bill so the mismatch is visible.

Registration is reversible from the same surface. `POST /profiles/unregister`
drops the named profiles from the registry and nothing else: it never reaches
`deploy.profiles.create_aws_profile` (the module's one `aws configure` writer)
or the AWS CLI, so the operator's AWS CLI configuration and every AWS resource
the account holds, the drive bucket included, are untouched. Names are checked
against the shared profile pattern but not against the machine's profile list,
so an entry whose profile was already deleted from the CLI configuration is
removable. Because grants are keyed by service, `aws_consent.revoke_for_profile`
sweeps the gated services under one consent lock and withdraws every grant
naming a removed profile, so a later re-registration under the same name starts
unconsented; the sweep runs BEFORE the registry write, so a request that fails
between the two leaves a registered-but-unconsented profile (the operator
retries) rather than an unregistered profile still holding an authorization
(`consent_unwritable` is the 500 that reports the former). The share,
library, and backup ledgers are account-keyed and are left alone: they describe
the bucket, which still exists and still bills, and must render unchanged when
the key is registered again. `test_aws_control_app.py::TestProfileUnregister`
pins the registry-only boundary, the default re-pick, and the grant sweep.

AWS Control reaches AWS through deploy-engine helpers: account inspection uses
`deploy.engine.run_aws`, while storage uses `deploy.engine._checked`. The engine
constructs fixed AWS CLI argument vectors with a profile name and runs the CLI
through the standard subprocess sandbox. The app does not import an AWS SDK.

## Drive and destructive operations

`storage.find_drive` discovers a drive by its managed tags, validates the bucket
name, and verifies bucket ownership against the requested account. Ambiguous or
unverifiable discovery refuses. The result is deliberately not cached: a bucket
identity is an authorization decision, not a display value.

`storage.create_drive` creates a bucket only after the bootstrap handler's
preview-plus-confirm flow. `routes._handle_drive_bootstrap` rechecks the
account target and S3 consent after confirmation and serializes creation so
concurrent confirmations cannot create competing drives.
`test_aws_control_app.py::TestDriveGuards.test_bootstrap_without_confirm_previews_and_creates_nothing`,
`TestDriveGuards.test_concurrent_bootstrap_confirms_create_exactly_one_drive`,
and `TestDriveGuards.test_consent_withdrawn_mid_create_refuses_and_creates_nothing`
pin those guarantees.

A created drive is ownership-checked before it becomes discoverable. The storage
layer enables versioning and then calls `deploy.engine._harden_bucket`, which
sets S3 Block Public Access, bucket-owner-enforced ownership controls, default
SSE, and the discovery tags. The order is load-bearing: a partially configured
bucket is left untagged rather than becoming a usable drive without versioning.
`test_aws_control_storage.py::TestCreateDrive.test_versioning_is_enabled_before_hardening_tags_land`
pins the sequence.

Drive objects live beneath the `artifacts/`, `drive/`, and `backup/` prefixes.
`storage.validate_key` rejects paths that could escape a section. Folder deletion
uses a validated, slash-anchored prefix, so it cannot target an empty section,
the bucket root, or a sibling with a common name prefix.
`test_aws_control_routes.py::TestFolderDelete.test_delete_rejects_an_empty_path`
and `test_aws_control_storage.py::TestDeletePrefix.test_deletes_every_object_and_returns_the_count`
pin that guard.

At the API layer, object and folder deletion do not require a `confirm`
parameter. The dashboard shows a confirmation strip before either deletion, and
`routes._handle_drive_delete` and `routes._handle_drive_folder_delete` then
execute after the owner, restricted-session, S3-consent, and key-scope guards.
On the versioned drive, `storage.delete_key` writes an S3 delete marker rather
than purging historical versions. This is the current recovery property, and it
is also why a delete on this path reaches no billing-zero. `storage.list_object_versions`
and `storage.delete_object_versions` are the version-aware pair that does erase
bytes; the Drive's own object and folder deletes deliberately do not use it, so
the operator keeps the S3-layer recovery. Backup retention is its one caller.

`routes._handle_drive_move` moves one object as a server-side copy followed by
a delete, both through `storage` (`storage.copy_object`, then
`storage.delete_key`), with the source deleted only after the copy succeeded —
a failed copy leaves the drive unchanged. Two invariants are load-bearing: a
move never silently overwrites (an existing destination refuses with 409,
pinned by
`test_aws_control_routes.py::TestDriveMove.test_move_existing_destination_answers_409_and_deletes_nothing`),
and the section is restricted to `drive` (the `library` and `backup` sections
are managed surfaces whose objects carry ledger state a move would orphan;
pinned by
`test_aws_control_routes.py::TestDriveMove.test_move_rejects_a_non_drive_section`).
Both keys pass `storage.validate_key` before any AWS call, the source must
exist (404), and the copy-before-delete order is pinned by
`test_aws_control_routes.py::TestDriveMove.test_move_copies_before_deleting`
and `TestDriveMove.test_move_copy_failure_issues_no_delete`. The copy carries
both `--expected-bucket-owner` and `--expected-source-bucket-owner`
(`test_aws_control_storage.py::TestCopyObject.test_copy_object_is_owner_pinned_on_both_ends`).
The dashboard reaches this route two ways with one mutation behind both: a
pointer drag of a file row or tile onto a folder, and a "Move to folder…" item
in the file's own overflow menu that opens a picker of the folders the current
listing can see (the top level, the parent, and the sub-folders on screen) —
the keyboard and touch path, since a drag is a convention only a pointer user
discovers. Moves are serialized: while one copy runs the source row is dimmed
and marked busy, other rows are not draggable and their "Move to folder…" item
is disabled, so the busy marker and any refusal always belong to the one move
in flight. A refused move (409 conflict, live share) is reported in the picker,
which stays open for another choice. The picker can be dismissed at any point,
including mid-move: the copy keeps running, the row stays busy until it lands,
and a refusal that arrives after dismissal is reported on the pane's own error
strip.

`routes._handle_drive_preview` is the gateway-proxied read behind the dashboard's
text preview: the browser cannot fetch a presigned URL itself (the bucket has no
CORS configuration), so the gateway reads a bounded head of the object through
`storage.get_object_head_bytes` and returns it decoded. The CLI only writes to a
path, so the bytes stage through a fresh per-call directory under
`storage.STAGING_DIR_LEAF` (`aws-control-staging`), a top-level leaf of the data
home that every agent sandbox bind-masks and the shared file-tool gate refuses —
a same-UID agent cannot swap the destination for a link between the gateway's
create and the CLI's open. Top-level on purpose: a mask covers the leaf, not its
ancestors, so a leaf under `apps/aws-control/` would leave agent-writable
directories that a rename could swap out from under a transfer; at the top level
the only ancestors are the data home and `$HOME`, the residual every other
fenced leaf already stands on. That same mask would hide the directory from the
sandboxed CLI, so the per-call directory is granted to that one fixed-argv spawn
through `engine.run_aws(extra_visible_dirs=...)`. The mask is a Linux/macOS
mechanism; on Windows, which has no sandbox, the destination is pinned by
identity instead — the whole path, not just the file. The staging root and then
the per-call directory are each opened and held through
`platform_compat.pin_directory` (a handle without `FILE_SHARE_DELETE`, which
refuses a link or reparse point at the name and, while held, lets neither that
directory nor anything above it be renamed or deleted); inside the pinned
directory the gateway creates the file itself with `O_EXCL` (a pre-planted name
— a hard link to a sensitive file included — fails the create), holds that
handle across the CLI call, re-checks device, inode and link count against it
once the CLI returns, and reads the bytes back through the handle rather than by
reopening the path; the directory is removed before the call returns. A
0-byte object makes the byte range unsatisfiable (S3 answers 416
`InvalidRange`), which reads as the empty preview it is, not as a failure. The head
is fetched with an 8 KB look-ahead past the 256 KB window, redacted whole, and
only then cut to the window at a whitespace boundary, so a secret straddling
the window's end is masked rather than shipped as an unrecognised prefix. The
response carries `truncated` (the object continues past the window) and
`redacted` (the egress redactor masked at least one value), and the dashboard
shows a one-line notice for each — the redaction notice at body weight, since
it changes the meaning of every byte below it — so a masked value is never read
as the file's own bytes. The window is a byte budget: the redacted text is cut
to `_PREVIEW_MAX_BYTES` of UTF-8, not characters, so a multibyte file cannot
carry the look-ahead past it. The dialog header names the file and, for a
nested key, its folder muted beside it, so two same-named search hits stay
distinguishable once open. `routes._handle_drive_download` returns the stored `contentType`
from the same HEAD that gates the presign, and the dashboard routes a `.pdf`
key whose stored type is not `application/pdf` (an object uploaded before
content types were set, served as octet-stream) to the "cannot be previewed"
fallback instead of an empty sandboxed frame.

`routes._handle_drive_search` matches FILE NAMES only (the full section-relative
key, case-insensitively), never contents, and the dashboard's search box says
so ("Search by file name or path…" — the match runs over the whole
relative key, folder segments included, but only files are returned). While a search is active the folder view, its
crumbs, the folder-scoped write controls (Upload, New folder) and the grid/list
toggle are all withdrawn together: results span the whole section and always
render as a table, so a write would land in a folder the reader cannot see and
the toggle would visibly do nothing. The section's drop zone goes inert rather
than absent — it still swallows a dropped OS file (the page's only
`dragover`/`drop` `preventDefault`, without which the browser navigates the
tab to the file) but uploads nothing. A refinement keeps the previous query's
rows on screen (`keepPreviousData`) and marks them busy with a "Searching…"
line while the new walk runs, so stale rows are never read as the new answer.
Each hit carries the same overflow menu as a file row (Download, Rename, Share)
plus "Open containing folder", with Delete alone below a separator; rename and
delete run in place against the hit's own path (the delete confirmation names
the full relative key, since same-named files from different folders sit side
by side in results) and re-run the search. "Open containing folder" clears the
search, navigates to the hit's directory, and marks the hit's row (or grid
card) for a few seconds counted from the moment the row appears — the listing
is a CLI round-trip, so a clock started at the click could expire before there
is anything to mark — scrolling it into view and moving keyboard focus onto it
as it mounts (the menu trigger the reader activated unmounted with the search
view, and the ring alone is a cue assistive technology never announces), so the
reader does not re-find by eye the file they just searched for; a hit that sits
past the first listing page is paged in automatically until its row mounts (or
the folder runs out), and navigating to any other folder retires the marker.

Rename (rows, grid cards, and search hits) is a same-directory call to the move
endpoint, so every move guarantee applies and only the refusal wording is
rename's own. Its completion is scoped to the row that started it: the in-place
editor may have moved to another row while the request was in flight, and the
name being typed there survives — a success closes the editor only if it still
belongs to the renamed row, and a failure lands inline only there; when the
editor has moved on, the failure is reported in the page-level notice naming
the file (`rename_failed_named`), never dropped. Delete follows the same rule:
its inline notice lives in the confirmation strip, and a rejection arriving
after that strip is gone (`delete_failed_named`) goes to the page-level notice.
A query change closes every in-place editor with the view it belonged to, in
flight or not — the request finishes regardless, and its outcome takes the
page-level route above.

On Linux the staging root is created empty before every namespace spawn
(`sandbox._CREW_PRECREATE_HIDDEN_DIR_LEAVES`, materialised by
`sandbox._materialize_maskable_dirs`): the launcher's mask loop only binds over
a directory that exists, so a root created lazily on first preview would be
visible to every sandbox already running. The materialisation is fail-closed:
a non-directory squatting the name, a creation failure other than `EEXIST`, or
a data home that cannot be resolved at all refuses the spawn
(`SandboxCeilingUnsealable`) rather than launching the agent with the directory
unmasked.

## Publishing and sharing

`routes._publish_gate` applies the shared fail-closed publish-governance decision
before a library push, a download presign, or a share presign. This guard is
load-bearing because each operation makes bytes reachable outside the local
machine.

The share implementation is a presigned URL and a local metadata ledger only.
`storage.presign` clamps the requested lifetime to the S3 signing limit, while
`shares.record_share` stores metadata and expiry but never the URL. A presigned
URL cannot be revoked by this app before it expires; `shares.forget_share` only
removes its ledger record. Backup objects are not shareable.
`test_aws_control_app.py::TestDriveGuards.test_share_of_backup_section_is_refused_outright`
and `test_aws_control_routes.py::TestSharesListForget.test_forget_removes_a_known_share`
pin those boundaries.

The share ledger is CURRENT STATE, not an audit log: `shares._prune` drops every
expired entry on both the read and the write path, and `record_share` keeps only
the newest `_MAX_SHARES`. The audit trail of minted URLs is the SEL event
`routes._audit` writes per grant, so nothing is lost by this file forgetting.
The state it holds is a GRANT — a URL was minted for this key and has not
expired — and NOT a claim that the object is still there.

That distinction decides how a deleted object is handled. `GET /shares` reads the
account's drive (`storage.list_object_keys`) and `shares.mark_missing_objects`
sets `objectMissing` on every row whose `section/key` the drive does not hold; no
row is removed and nothing is written. Dropping the row would be wrong on both
counts: a presigned URL signs bucket, key and expiry but no version, so
recreating the key makes an unexpired URL resolve again — the grant is dormant,
not dead — and dropping the record is exactly the `forget` the app documents as
the user's decision. The mark is not persisted for the same reason: it is a fact
about the bucket at render time, which recreating the key would make stale in the
under-reporting direction. This is a deliberate divergence from
`library.reconcile`, which does prune, because that ledger claims a cloud copy
exists and the bucket can settle that claim.

The listing is best-effort and its outcome is reported, never implied: `checked`
says whether the rows were compared against the drive, because an absent
`objectMissing` otherwise reads as "the object is there" on a render where the
drive was never read. WHY the check did not run is logged rather than sent — the
reason is a backend-authored English sentence and the console is rendered in
12 production locales plus the development pseudolocale, so it shows a
translated "not checked" line gated on `checked`,
the same resolution the Library's `remoteError` reaches.
`storage.list_object_keys` raises rather than degrading to an empty set, and
per-row `storage.object_exists` is deliberately not used — it answers `rc == 0`,
so a throttle or a timeout is indistinguishable from a 404, which is correct when
refusing to mint and would mark live shares dead here. The ledger is read BEFORE
the listing is taken, which is what makes an `observed_at` cutoff unnecessary: no
row in hand can postdate the listing.
`test_aws_control_routes.py::TestSharesListForget` pins the mark, the read order,
the unmarked degradation, the logged reason, and the empty-ledger case that takes
no listing at all.

Existing rows stranded before this shipped are corrected on the next render;
there is no migration over `shares.json`.

AWS Control does not create bucket-policy account grants or public CDN shares.
The IAM-policy endpoint renders `deploy.iam.policy_json` for the operator to
apply; it does not write IAM policy.

## Library, costs, and backup

`library.push_artifact` copies a selected artifact through the Drive storage
layer after the route's S3-consent and publish-governance checks. It refuses
credential-bearing artifact content; `test_aws_control_app.py::TestLibraryScan.test_credential_bearing_artifact_is_refused`
pins that egress boundary.

`library.library_remove` deletes the whole `artifacts/<slug>/` prefix and then
forgets the slug's ledger record. The order is load-bearing rather than
transactional: a local file and a remote bucket cannot be committed as one, and
objects-then-record leaves at worst a record the bucket does not back, which
`library.reconcile` repairs. The reverse order would leave objects that no
surface lists. Removal writes delete markers on the versioned bucket, so it
empties the listing rather than reaching billing-zero; the version-aware purge
`storage.delete_object_versions` provides is deliberately not used here or by the
Drive's own deletes, which keep the S3-layer recovery those surfaces promise.

`library.reconcile` is the direction that makes the ledger's "display state, not
truth" claim hold: it drops records the bucket does not back and never invents a
record for a cloud copy it finds, because version and push time live in that
copy's sidecar. It prunes only what the bucket has had a chance to disprove — a
record stamped at or after the listing it is judged against is left alone, since
that listing predates the record — so a push completing mid-render does not lose
its record. `routes._handle_library_list` reconciles before joining local
artifacts, and reports whether the bucket was actually read — a failed, absent,
or unconsented read leaves the rows rendering as an unverified ledger claim
rather than as an authoritative empty. It reads the prefix through
`storage.list_library_folders`, which is unredacted and completely paginated
because a reconcile reasons about absence; the paged, redacted display listing
cannot answer that question. `library._update_ledger` is the ledger's only
writer, so push, removal, and reconcile cannot drop each other's records.

`routes._library_lock` serializes the three Library operations on one drive —
push, removal, and the reconcile read. Each is a network round trip followed by a
ledger write, and interleaving two of them corrupts state neither half can
detect: a push completing between the reconcile's listing and its prune, or a
push racing a removal of the same slug past the delete sweep. The ledger's file
lock cannot serve this — it covers a sub-second read plus rename by design.

The two mutations wait on that lock unbounded — they are user-initiated actions
that may legitimately queue — but the render path waits only
`_LIBRARY_RECONCILE_LOCK_WAIT_SECS` and then reports `reconciled: false`. A push
holds the lock across an upload allowed up to 600s, and a page render must not
hang for that; skipping loses nothing durable because the reconcile is
self-correcting, so the next render performs it. Errors on this path already
degrade rather than failing, and slowness degrades the same way.

Because that lock makes a caller WAIT, all three operations re-run their
authorization inside it via `routes._reauthorize_in_lock` — app enabled, then live
identity still resolving to the requested account, then S3 consent, then the drive
bucket re-resolved and compared, plus publish governance for the push. This is the
same re-check `_handle_drive_upload` runs after its spool and for the same reason:
the wait sits between the checks that authorized the call and the call itself. The
bucket is included because tag discovery can return a different bucket while the
identity is unchanged, and this module keeps no bucket-name cache precisely
because that identity must not be stale. The reconcile read is included because a
listing is still a call into a paid service; on the read path a failed re-check
degrades to "not reconciled" rather than an error, so the local half still
renders, and so does a ledger that cannot be written — the rows are renderable,
they are merely unverified. The degraded identity denial is SEL-audited even
though the route does not fail on it: a permission decision reaches SEL whether or
not it becomes an error response.

`library_remove` CONFIRMS the prefix is gone before touching the ledger.
`delete_prefix` deliberately degrades on an unreadable listing page — it stops the
walk and reports the count so far, so it can under-delete — and forgetting a record
on that would drop a copy still in the bucket while reporting the removal as done.
A slug still present raises instead, leaving the record intact.

The lock is per-process, so a second gateway sharing the data home is still a
racer. `library._recorded_at_or_after` is that cross-process guard, one rule
asked by both operations: reconcile will not prune, and removal will not forget,
a record written at or after the remote observation each is acting on. Both
cutoffs are read BEFORE their observation begins, never after — a cutoff that
postdates its own observation protects nothing, because a record written in the
gap compares as older than it. Reading early only widens the set of records left
alone, and a record left behind whose objects were really removed is merely
stale, which the next render repairs.

Soundness also depends on `pushedAt` being stamped when the record is WRITTEN —
inside the ledger lock, after the uploads have succeeded — not when the push
began; a pre-upload stamp would read older than a listing that ran during a slow
upload. The metadata sidecar keeps its own pre-upload stamp, which is what remote
metadata should say about when the push started.

Cloud copies with no local artifact row are reported to the caller as
`remoteOnly` rather than being hidden: `list_pushable` walks the local store, so
a copy pushed from another machine has no row to carry it and would otherwise be
unreachable from the console that must be able to remove it. The dashboard keeps
that promise through the Library folder's listing rather than through this field:
the folder renders one card per listed prefix and offers removal on all of them,
so a copy with no local row is reachable by construction rather than by a second,
separately-derived set.

`costs.fetch_month_costs` calls Cost Explorer for the requested linked account
and groups results by service. `routes._handle_costs` serves a fresh local cache
without a new consent check; a stale cache is returned with its stale state when
Cost Explorer consent is absent or a refresh fails. This keeps the Bill view
available without misrepresenting a cached value as fresh.

`backup.run_snapshot_backup` uploads a generated snapshot archive, and
`backup.run_sessions_backup` archives session material only when descriptor-based
traversal pinning is available. `backup._authorize_upload` requires the app to
remain enabled, the S3 grant to still name the target account, and shutdown not
to be in progress before upload. `backup.restore_download` stages an archive
locally; it does not restore it into live gateway state.
`test_aws_control_app.py::TestRound22Hardening.test_restore_refuses_a_symlinked_destination`
pins the staged restore safety boundary.

Both runs build their archive and then decide whether to send it.
`backup._tree_fingerprint` digests one row per archive member -- path, kind, permission
mode, size and a content hash -- in sorted path order, read from the packed payload.
The comparison is over that entry set rather than the archive's bytes because a
`tar.gz` embeds per-entry
mtimes and a gzip stamp, so two runs over an identical tree produce different bytes and
an archive-level comparison reports "changed" every night. Reading it from the payload
rather than by a second walk of the source also means it cannot disagree with what would
actually be sent, and that a redaction switch changes the fingerprint.

Two normalizations are part of the digest's definition, each measured against the real
engine rather than assumed. `_VOLATILE_MANIFEST_FIELDS` drops `created_at` from
`MANIFEST.json`, the one field `snapshot.py` rewrites on a rebuild of an unchanged tree;
the member's other fields stay, because `purpose`, `staging` and `version` are not
derivable from the file set. The snapshot bundle's root directory carries a timestamp, so
snapshots pass `volatile_root=True` while the sessions archive passes `False` -- its
`crew` and `cli` roots are meaningful. `test_aws_control_backup_unchanged.py::TestRealBundleAssumption`
builds real bundles and pins that assumption, so a second volatile field turns CI red
instead of the skip quietly never firing again.

`backup._unchanged_baseline` decides the skip, and returns the matched run record rather
than a boolean so a skip can carry the baseline's own key, fingerprint and version
forward. It skips only when the previous archive is PROVEN still in the drive at its
recorded key, its recorded length and its recorded version; every other branch uploads,
including a moved tree, a missing object, an unanswerable `head-object`, an unreadable
archive, and a version id that identifies no single version. `backup._is_provable_version_id`
is that last rule: the empty string names nothing, and `"null"` is the id S3 gives every
object written to a key while the bucket's versioning is SUSPENDED, where an overwrite
replaces that version rather than adding one. Both the recorded id and the stored one run
through it, so the skip never fires on an unversioned or suspended drive and those
accounts keep uploading in full.

The `head-object` that proves the baseline is a request on the operator's account, so it
passes `backup._authorize_upload` under its own operation, `SEL_OP_BASELINE_PROBE`,
distinct from the gate that guards bytes leaving. It is taken after the local checks, so a
run that could not skip anyway spends no round trip discovering it.

A run record persists `tree` (the fingerprint, which is what tomorrow compares against)
and `uploaded`. A skip writes `uploaded: false`, keeps the matched run's key, fingerprint
and version so the baseline survives, and takes a fresh `at` so `due_for_nightly` does not
rebuild on the next wake. It writes that record only when `runs[kind]` is still the very
record the baseline was read from, identified by its `(process, sequence)` pair, under the
state lock. `sequence` is bumped on every run-record write and `process` carries the pid,
so no two records an install writes share the pair; `_run_is_newer` already requires that
same pairing, because a bare sequence counts one process's own writes and two processes can
both sit at the same number. Neither `at` nor `key` can stand in for it and neither is
compared: `datetime.now` resolves to the platform's clock tick, so two writes on a coarse
clock share one `at` value, while a skip carries the matched run's own `key` and so cannot
be told from a second skip by key. Windows CI produced both collisions at once and accepted
a second skip against a baseline the first had already replaced. A record predating these
fields carries neither, so the skip is refused and a full copy uploads -- the direction
every other proof here fails toward, and the reason there is no fallback to `at` alone.
`test_aws_control_backup_unchanged.py::TestRecordSkipCompareAndSet` pins each term with a
case no other term can see, including the coarse-clock collision reproduced by hand; the
two sequence type guards cover each other on a record with no sequence, so they are pinned
as a pair. If the slot is absent or moved while the archive is
being built, the skip is refused, the refusal leaves the newer record in place, and the
run logs the reason before uploading the archive. `hooks._run_once` reads `uploaded` and
reports and audits a skip as `unchanged` rather than as a push; a record without the field
reads as a push. Neither the label publish nor the retention sweep runs on a successful
skip. Labels follow a push, so a local rename reaches the drive on the next real upload
rather than on a skip. An unchanged night cannot consume a keep slot or prune the archive
the next skip depends on.

Both push keys carry a timestamp, so nothing is overwritten and the drive would
otherwise only grow. `backup._prune_remote_archives` runs as the LAST step of a
successful push, and by default it retires nothing: retention is OFF unless this
account's `backup.json` holds a usable count under `RETENTION_KEEP_STATE_KEY`. That
key is the whole switch. Absent, or holding anything that is not an integer, means
keep every archive, so a fresh install and an upgraded one both behave exactly as
before and no first run after an upgrade deletes anything. There is deliberately no
count that applies without being configured: a default would perform permanent
deletes on a value the operator never wrote, and while shipping off costs storage
they can see in a listing and reclaim with one command, shipping on destroys archives
somebody was deliberately keeping and leaves nothing to restore from. It is also the
house shape rather than a new policy — `nightly` ships off, reads fail-closed, and is
never inferred from an adjacent grant.

The shape is the one this repository already recorded, not one chosen here.
`docs/request-for-change/rfc-s3-backup.md` O5 ("retention, partly answered") observes
that a lifecycle rule can express "delete older than N days" but never "keep the newest
N", so on a host that stopped backing up such a rule "would delete the last surviving
copy of its memory exactly when it is needed"; it concludes that unbounded growth is the
cheaper failure and frames the remaining question as "whether operators want an opt-in,
count-based remote prune, which needs a lister and a deleter rather than a lifecycle
rule". Opt-in, count-based, a lister and a deleter is what this module implements, and
the by-name protection of the newest archive is that document's own reason. The RFC is
`status: draft`, so it is cited for SCOPE rather than as an approval; the question it
leaves open is whether operators want the prune, which is exactly what shipping off
leaves open.

`backup._retention_keep_for_sweep` reads it fail-closed and returns the count with
the reason when there is none, because the two roads to off need different handling:
nothing configured is the ordinary state of an unconfigured install and audits as a
successful sweep with nothing to do, while a state file this process could not read
keeps everything too but audits as failed, since an operator who DID configure a
count is silently not getting it. "Could not read" includes bytes that are not valid
UTF-8, not only a refusal from the OS: `read_text` raises `UnicodeDecodeError`, which is
a `ValueError`, so the reader catches it explicitly rather than letting it escape past
the sweep -- which runs outside its own best-effort handler, so an escaping decode error
would report a backup already off-host as failed and skip the audited unreadable branch
entirely. The read-modify-write treats the same bytes as unreadable too, and abandons the
mutation rather than publishing over them: repair-on-write is justified for a document
that DECODED and then failed to parse, and bytes that are not UTF-8 never reached the
parser. Overwriting them would replace every account's toggles, retention count and run
history -- including the `upload_versions` records the ownership test reads -- on the
strength of a document nobody read. The abandoning error stays an `OSError` subclass, so
`set_nightly`'s existing handler needs no change. Neither reader catches the wider
`ValueError`,
so a surprising one stays loud. Neither is ever derived from `nightly` in either
direction: authorizing unattended uploads is not authorizing permanent deletes, and
turning the nightly off does not withdraw a configured count. The two ends of the
range are NOT treated alike, because the two directions are not alike. Zero and
negatives clamp UP to `RETENTION_KEEP_MIN` rather than reading as off, because a typo
must not silently stop doing what the operator asked for, and that direction deletes
FEWER archives than the stored number named. There is no ceiling. A count larger than
the number of archives that exist keeps all of them, which is not a harm worth refusing
an operator over, and both alternatives were worse: clamping down would delete archives
the stored number named, and reading it as off would ignore what they configured. The
write path validates the floor only, for the same reason. Once on, turning it on is the whole
consent and the sweep prunes to the count without asking again.

A key that is PRESENT and does not resolve to a usable count -- a string, a float, a
`bool` -- keeps everything and reports the same reason as a
state file that could not be read, which audits as a failure. Only an ABSENT key is the
ordinary off state that audits as a success. Somebody wrote the unusable value, so
reporting it as "not enabled" would audit a clean sweep and leave them believing a
count they set is in force.

`POST /backup/{account}/retention` is the shipped way in, beside the nightly toggle
and guarded the same way. It takes the COUNT rather than a flag, because a count is on
and `null` is off, and `null` is what turns retention back off -- a switch that can
only be turned on would leave an operator hand-editing state to stop it. The value
authorizes permanent deletion, so the route validates and never coerces: a `bool` is
refused rather than read as `keep=1`, and an out-of-range count is refused rather than
clamped, so nobody configures `0` and is later told they configured `1`. A large
hand-edited count is honoured as written, by the route and by the sweep alike: there is
no upper bound for either to police. A state
write that fails returns `state_persist_failed` and does not echo the value, because
reporting a setting the next read contradicts is worse than an error. The status read
reports `retentionKeep` as the EFFECTIVE count the sweep would use, or `null` when
retention is off, `retentionUnclaimed` as the last sweep's per-kind count and bytes
of archives no sweep can ever retire, and `retentionUnrecorded` as its per-kind count and
bytes of objects under this install's folder that it holds no record of -- a number that
claims neither ownership nor reclaimability. No console control ships for any of them: the
count is set and read over HTTP
only, and the status field exists so an operator who wrote one can confirm what was
stored instead of trusting the write. A renderer is a separate surface and is not part
of this module.

The count is read again at the moment of deletion, not carried over from before the
listing. That final check and the delete are held under TWO locks: an in-process one so
another thread's write cannot interleave, and the state file's own sidecar lock so
another PROCESS's write cannot either. Both are required, and a thread lock alone would
have been the weaker half of the pair: this module treats a second install sharing one
bucket and one state file as a designed-for case, so a count cleared over there has to
be ordered against this delete too. The cost is that a state write in any process waits
for the purge, which is one batched delete rather than the whole sweep. `list_object_versions` is a network round trip that follows a build which may
have taken minutes, so an owner who switches retention off inside that window has chosen
to keep the versions the sweep already selected. A count that GREW protects keys the
candidate set was built to delete, so the set is stale and the sweep refuses; a count
that shrank authorizes every key in the set and more, so the set stays valid; unreadable
refuses. This is the same rule the consent gate beside it follows: a check is good for
the call that follows it, not for a later one. The re-read is deliberately not a lock
held across the deletion, because `_run_lock` also serializes the status read, which
would then stall for the length of a purge.

One archive is never retired, and that guarantee is separate from the count rather
than a consequence of it: the key this run just uploaded is removed from the
candidates by name, whatever the count says, including `1`. The floor at
`RETENTION_KEEP_MIN` also spares the first entry of the age order, but that is a
different claim — the run's own key is only first while nothing else carries a later
timestamp, and a co-writer with a skewed clock is enough to move it, which is exactly
the case the by-name guard exists for. If the listing does not show that key the
sweep deletes nothing at all, because a view missing the newest object cannot be
trusted about which objects are old. That refusal is the cloud form of the guard
`snapshot`'s local `--keep` already carries.

The sweep is scoped per kind and per install prefix, so one kind's cadence cannot
evict the other's last copy. Scoping alone is not ownership, though: the drive is
shared by design, so an object can sit under this install's prefix without this
install having written it. Candidates are therefore intersected with
`backup.uploaded_keys` — what `_record_run` wrote down — and anything outside that
record is neither retired nor allowed to occupy a `keep` slot, since a co-writer's
upload filling a slot would push one of ours over the edge. That record proves the
KEY, though, and a key can carry versions this install never wrote, so ownership is
settled at the VERSION: `storage.put_file` returns the `VersionId` S3 assigns and
`_record_run` stores it per key, and the sweep erases only a version id present in
that record and present in the listing. A key absent from the version record is not
retired at all, which is the fail-closed direction — an unversioned bucket and a
response naming no version both land there, and erasing bytes nothing proves are
ours is the worse outcome. A co-writer's version riding on one of our keys survives
while ours is erased, so the key still reaches billing-zero for the bytes we paid
for. A version the record names and the listing lacks is skipped as well: ours is
already gone, so whatever remains under that key belongs to someone else. Delete
markers carry no version in the record and are left alone; they carry no bytes
either, so leaving them costs nothing billable. Ownership also decides which keys
COUNT, not just which bytes may be erased. A key is a restorable copy of this
install's while the version the record names is the current one, which is what
`backup._current_version_is_ours` answers. A key failing that holds no `keep` slot
and is not retired either -- its current version is a delete marker, whose
noncurrent bytes belong to the manual delete path, or it is a co-writer's, in which
case our bytes are on the drive as a noncurrent version. Those bytes are reachable:
`storage.get_file` takes an optional version, and when the current object fails the
body fingerprint `backup._recover_recorded_version` makes one further read of the
version the record names, accepting it only on that same fingerprint re-taken over
the bytes that arrive. Retention still declines such a key,
which is now the CONSERVATIVE reading rather than a forced one: it retains more,
never less, and counting a recoverable-but-noncurrent copy toward `keep` is a
decision about what may be DELETED and is not taken here.
Erasing them would also be this sweep deciding a key someone else is
actively writing is finished with. Because the rule is about the CURRENT version
rather than the newest by timestamp, a live key also carries our version as its
newest, so `_newest_first` orders by the age of the archive we wrote with no second
rule to drift from. Two harms fall out of it. A co-writer overwriting an older
recorded key would otherwise inflate that key's apparent recency, push it into the
`keep` newest and displace a genuinely newer archive of ours into the candidate set
to be erased. And an overwrite of the key this run JUST uploaded would leave the
sweep trading older backups against an archive that is no longer restorable, so that
case aborts under its own reason, distinct from a listing that omits the upload
entirely -- an auditor needs to tell those apart. One consequence
is worth stating
plainly rather than leaving a reader to derive it: an archive uploaded before the
version record existed has no id in it, so it is kept PERMANENTLY, not drained
gradually. Retention therefore bounds forward growth and reclaims nothing already
on the drive. Adopting those archives would mean proving ownership another way --
downloading each version and matching it against the body fingerprint `uploads`
already stores -- and that migration, like a one-shot reclaim, is a separate
change. The restore path draws
the same line for a cheaper operation, and a permanent delete is held to at least
that standard. It deletes object VERSIONS, not objects: on this versioned bucket a
plain delete leaves a marker and the bytes keep billing, so a count-based
retention built that way would empty the listing and save nothing. The keep count
is read fail-closed — an unreadable or corrupt `backup.json` yields no number
rather than the default, and the sweep then deletes nothing, because a default
silently replacing a configured 50 would erase the 47 archives the owner asked to
keep. `_authorize_upload` runs under its own `SEL_OP_RETENTION` operation TWICE:
once before the listing and again immediately before the delete, since a listing
round trip separates the two and consent withdrawn in that window must not reach
an irreversible call. The sweep is best-effort throughout — a cleanup that fails
logs one line and leaves the successful backup reported as done.

Every terminal outcome of the sweep files one `SEL_OP_RETENTION` event, because
this is the app's only permanent version-erasing path and a purge with no entry
reads exactly like no purge: `successful` when it completed, with or without
anything to delete, and `failed` for an AWS error, for the refusal to act on a
listing missing the newest key, for an unreadable keep count, and for an
authorization failure that never reached the gate's own record — an expired
credential raises before `_refuse_upload`, so without that the commonest failure
on this path filed nothing at all. A refusal BY the gate is not filed twice: it is
already recorded as `denied` where the decision is made, and the exception type is
what separates the two. The event carries the counts (`keep`, `live`, `retired`,
`versions`) that say which outcome happened, and a delete that fails partway
through its batches reports the versions it had already erased rather than zero,
because a half-finished purge recorded as nothing is the one shape an auditor must
not be handed. `Quiet` is set on every `delete-objects` call, so a 200 response
names only the entries it could not remove and the rest of that batch is counted
too — otherwise a first batch that half succeeded would still report zero. The audit is best-effort
like the sweep, so a SEL write that fails cannot reach the backup. The failure LOG
line follows the same rule as the audit entry: when versions were already erased it
names that count instead of saying nothing was deleted, because a line claiming
nothing happened beside an audit entry carrying a nonzero count contradicts the
record and points a reader at a purge that did not happen. Only the version count is
named there -- batches are filled to the API's limit without regard to key
boundaries, so a number of retired KEYS is not something that failure measured.

Every sweep also reports what it CANNOT own: the count and total bytes of keys this
install wrote but recorded no version for. Those hold no `keep` slot and no sweep
can delete them, so their bytes are billed permanently, and the report is what makes
that cost visible in the audit trail instead of only on an invoice. The total is
over every version under such a key, because every version is billed. It is
reported even when the sweep aborts on an untrustworthy listing, which is the case
most likely to carry a large one.

`test_aws_control_backup_retention.py` pins the keep-newest rule, the default-off
switch including an unconfigured install deleting nothing, every unusable stored
value reading as off, the refusal to infer the count from `nightly` in either
direction, a garbled count beside intact upload records still deleting nothing, the
failure log naming the erased count in one direction and saying nothing was deleted in
the other, the two off reasons staying distinguishable with their different audit
outcomes, and `test_aws_control_routes.py` pins the enablement route: registration,
the write, `null` clearing it, a `bool` and a string and an out-of-range count all
refused, a missing field refused rather than read as off, and a failed state write
reporting the failure rather than the value. The retention suite also pins the writer
end to end -- setting a count turns the next sweep on, clearing it turns the next sweep
off, clearing removes the key rather than storing a sentinel, and the nightly grant is
untouched either way. It further pins the per-kind and
per-install scoping, the ownership record down to the version id, the refusal to
retire a key with no recorded version, the refusal to count a foreign current
version, the overwritten-upload abort, the live gate at the delete, the
fail-closed keep count, the version-pinned deletes, the client-side paging that
bounds one listing response, the refusal to answer a folder whose history is too
large to hold rather than returning the part that fits, the unclaimed-archive count
and bytes reaching the audit event AND the status read, the unrecorded-object count
beside it, the refusal to persist either pair -- or to prune a version record -- from a
listing the sweep would not act on, the survival of a version record past its key
leaving the panel history, the audit events including the
partial-purge count, and the best-effort contract.

What the sweep measures is the keys this install still REMEMBERS whose recorded version
id is missing: an archive pushed before the version record existed, an unversioned
bucket, or a put response that named no version. `_current_version_is_ours` is false for
all of them, so they hold no `keep` slot and no sweep will ever delete them, and the
status read serves the last sweep's pair per kind as `retentionUnclaimed` so that floor
appears beside the count that will not collect it rather than only in audit events.
Letting the sweep adopt an archive it has no record for would weaken the one property
the ownership test exists for, so that choice is tracked separately in issue 12274
rather than settled here; the disclosure does not reclaim anything.

A version record's lifetime is the ARCHIVE's, not the panel listing's. Trimming it to
the keys `uploads` still holds under `MAX_REMEMBERED_UPLOADS` would let a number chosen
for a 20-per-kind panel decide what retention is able to retire: with retention shipping
off, an install pushing nightly passes that bound on its 201st push, and a `keep` count
enabled afterwards would reach nothing behind that point -- no recorded version, so the
ownership test refuses the archive and its bytes are billed for as long as the bucket
keeps it. That bound would MINT the floor rather than only inherit legacy data. So
`upload_versions` is bounded separately: a record is dropped when a listing the
sweep TRUSTED proves its object is gone (`_prune_recorded_versions`), and
`MAX_RECORDED_VERSIONS` (5000) is a backstop under which a pathological document stays
bounded rather than a horizon. It is the one path left that can still drop a version
record without a listing having proved anything, so its overflow is COUNTED before
anything is dropped and reported with that count at both trim sites -- the persisted map
and the recovery map, named apart in the message. Silently it would read exactly like a
population that never held those records, and with retention off the sweep returns before
any listing, so nothing else would ever measure them. The retained value needs no length
bound of its own: it is a version id S3 issues under S3's own limit and a key this app
mints, and truncating either would be worse than unbounded, since a shortened version id
is not the version and would fail the ownership test it exists to pass. The sweep's ownership set is `retention_owned_keys` --
`uploads` union the recorded versions -- because a recorded version id is strictly
stronger evidence than an `uploads` entry, and reading only the weaker one would discard
the record that makes an older archive retireable. Nothing is retired on weaker proof: every
admitted key still has to pass `_current_version_is_ours`, so the version erased is the
one a restore would fetch. `classify_key` and the restore path deliberately keep reading
`uploads` alone, because their question is whether this install vouches for these BYTES,
which the fingerprint answers and a version id does not.

Three bounds make the prune's absence a proof rather than a guess: it runs only past the
gate that accepted the listing, and `list_object_versions` walks the whole token chain
and RAISES rather than returning a partial answer, so partial data arrives as an
exception and never as a short list; only records under `<kind subpath>/<install id>/`
are eligible, since the listing is evidence about that folder alone; and only records
that existed BEFORE the listing began are eligible, so a push landing while the listing
is in flight -- a manual run racing the nightly loop -- cannot have its record pruned.
The prune deletes STATE and never an object, and its two failure directions are not
symmetric: a record wrongly kept costs document space, while a record wrongly dropped
returns its archive to the unreclaimable floor. Neither can erase data.

The prune's deletion is also FINAL, which takes one more release than the bound above.
`_unpersisted_versions` holds a version whose state write failed, and `_merge_pending`
copies that map whole into the next successful update -- so a held entry the state
already carries would be written back on every later update, re-adding precisely the
records the prune deleted. It is therefore released on its own contract as well as with
the fingerprint it arrived with: `_release_persisted_versions` drops a held record once
the document just written carries the same id for the same key. Equality with the
persisted id is the release condition, never the key's presence, because a different id
under one key is exactly the record the state does not have. The two maps are bounded
differently, which is why one release cannot cover both: a held version outlives its
`uploads` counterpart, and after that eviction the paired release can never reach it.

`retentionUnclaimed` still does NOT cover every unretirable archive, and that is the
contract rather than an oversight: it counts keys in `retention_owned_keys` whose
recorded version is missing, so an object with neither an `uploads` entry nor a version
record is filtered out before the measurement and reads 0 there however many bytes it
holds. It is read against the `keep` count to say what retention will collect out of the
set it can SEE, and absorbing an object nothing has a record of would make it a figure
that answers neither question. Those objects are counted separately and claim nothing:
`retentionUnrecorded` (`{kind: {objects, bytes, at}}`) reaches the same status read and
the same audit event, holding what the listing showed under this kind's install folder
that the install has no record of. Two unlike things land in it and this code cannot
separate them -- archives of this install's own for which its state holds no record,
and another writer's objects under a prefix that is co-writable by design
-- so the leaf is `objects` rather than `archives`, because the install id in a key is a
string any co-writer can type. A key the listing shows only as a delete marker is not
one of them: it holds nothing and is billed nothing, so counting it would put a phantom
in the floor that no later listing can remove, and the same reading
`_current_version_is_ours` already applies to a marker is applied here. A key that also
carries a real version still counts, on that version's row, because those bytes are
billed whatever sits on top of them. The prune's own set is deliberately WIDER: a marker
adds its key there, because that set decides whether a RECORD survives and a record
wrongly dropped is unrecoverable proof where one wrongly kept costs document space.
Nothing acts on the count: those keys are skipped before
ownership is tested, hold no `keep` slot, and are never deleted. Whether any of them
could be PROVEN this install's and reclaimed is a separate design owing its own argument
about proof, tracked in issue 12274 rather than settled here; a count erases nothing, so
it needs no such proof.

`retentionUnclaimed` is written only from a listing the sweep accepted as showing the
archive it just uploaded, and `retentionUnrecorded` and the version-record prune share
that gate for the same reason. Before it the sweep has already declined to trust the
listing about age, so it cannot be trusted about how many keys it omitted either, and
an undercount published as the floor would read as no floor at all. The audit event
still carries the number on that path, where its `failed` result says how much to trust
it. One write covers every later path because deletion draws only from `live` and an
unclaimed key is absent from `live` by construction. The value is stamped rather than
live: refreshing it would need the bucket listing this payload keeps opt-in, and
without the stamp a reader cannot tell a measurement taken before a manual delete from
one taken after. A measured zero is stored like any other count, so an absent kind
means only that no sweep has measured it.

`run_sessions_backup` always archives the crew half (the display transcript under
the data home). It archives the kiro-cli half -- Layer B, the byte-exact
unredacted model context window -- only when the operator has granted it for that
account; the default is withhold, and an unreadable or non-boolean stored value
also withholds. The permission is read once, before the archive is opened, and
the resulting run record carries `layer_b` so whoever inspects the run can tell
which layers the archive holds rather than inferring it from an absent key. That
value is taken from the number of kiro-cli files actually added, not from the
permission: a granted run whose kiro-cli directory is absent or empty adds none,
and a record is written once, so reading the permission there would state a
fidelity the object does not hold with nothing afterwards to correct it.
Nothing reads the field programmatically -- `restore_download` does not consult
it -- so it is a record for a human or an incident review, and the two archives
it distinguishes are otherwise identical by name.

The grant is stored PER ACCOUNT as `sessionsIncludeLayerB` in the app's state
document, `backup.json`, which sits inside the `apps/aws-control/data` directory
registered in `security._CREW_SECRET_LEAVES` -- the read+write keystone floor,
beside the `nightly` bit. It is deliberately NOT a `config.json` key.
`config.json` is writable by any auto-approved agent shell, so a permission
honoured from there is one a prompt-injected agent can grant itself, and an
unredacted archive already in a bucket cannot be recalled; an authorization whose
subject can write it is not an authorization. The sole writer is the owner-gated
`POST /api/apps/aws-control/backup/{account}/layer-b`, which opens the state file
directly rather than through the agent file gate. The grant is per account
because the risk it prices is the destination bucket, so granting it for one
account must not grant it for another.

A revocation landing while an archive is being built refuses the upload:
`run_sessions_backup` re-reads the permission immediately before the PUT and
raises rather than shipping bytes under a permission the operator has withdrawn.
On the permitted path that re-read, the live authorization checks, and the PUT all
run inside one acquisition of the state file's sidecar lock, taken before
`_authorize_upload` through `_upload_lock`. Taking it after authorizing put a
blocking wait between the consent check and the PUT: a concurrent account's backup
can hold this lock across its own upload, and consent withdrawn during that wait
was never re-read, because the Layer B re-read does not cover consent. This is the
same rule `routes._reauthorize_in_lock` already states for `routes._library_lock`
-- a lock that makes a caller wait must re-run the authorization inside it, because
the wait sits between the checks that authorized the call and the call itself.

ONE shape takes no lock: an owner-initiated WITHHELD run. Which shape may skip it
is decided by what is RE-READ inside the block, not by the Layer B decision alone.
The Layer B re-read is `layer_b and not sessions_layer_b_enabled(account)`, which
short-circuits on its first operand when the half is withheld, so it contributes
no read there. But the crew display half rides on EVERY run, withheld or not, and
for a scheduled caller that half is authorized by the unattended grant, which
`_authorize_upload` re-reads inside this block and `set_nightly_sessions` writes
under this same sidecar lock. So a scheduled withheld run still holds it: unlocked,
a revocation committing between that read and the PUT is not ordered against the
PUT, and the transcript ships after the grant was withdrawn, which no later action
recovers.

An owner-initiated withheld run has neither read. Both scheduled-only re-reads are
skipped -- an owner who clicked the button is present and authorized the run by
clicking -- the Layer B re-read short-circuits, and what remains
(`is_app_enabled`, `aws_consent`, STS) is not stored in this module's state file,
so an exclusive hold would order nothing. Taking none serves the
`_authorize_upload` rule above directly rather than by holding something -- with no
blocking acquisition in the block, the authorization and the PUT are adjacent.
Taking one would cost what an exclusive hold costs: the lock file is
`_state_path()`'s sidecar, `backup.json` in the app data directory, one path for
every account rather than one per account, so every state writer of every account
-- `_record_run`, `set_sessions_layer_b`, `set_retention_keep`, and the nightly
loop -- waits out one account's upload up to `_STATE_LOCK_TIMEOUT_SECS`. That is
the cross-account stall this module already removed from the status read, one layer
down. The predicate is written so that only this one proven shape skips the lock
and any other caller holds it, because the exposure it prevents has no recovery.

`_upload_lock` takes ONLY the sidecar file lock, deliberately not `_run_lock` --
the same shape `_delete_under_the_retention_gate` composes, and for the same
reason. `_run_lock` also serializes `last_runs`, which the dashboard's backup
status read goes through (`routes.py`), so holding it across a PUT allowed up to
`_PUSH_TIMEOUT_SECS` stalled every account's status surface for one account's
upload. The revocation guarantee does not need `_run_lock`: the setter
(`set_sessions_layer_b` -> `_locked_state_update` -> `_state_lock`) takes this
same sidecar file lock exclusively, so an exclusive hold across the upload already
orders a revocation wholly before or wholly after it, across processes as well as
threads. Dropping `_run_lock` therefore keeps the guarantee.

Dropping it there was necessary and not sufficient. The stall arrives through the
contending WRITER, not through the upload: `_state_lock` took `_run_lock` before
parking on the sidecar file lock, so a writer meeting an in-flight upload -- a
mid-upload revocation, or any second account's `_record_run` finishing -- held
`_run_lock` for the upload's whole duration and `last_runs` queued behind the
writer. So the module has ONE acquisition order, stated in a comment above
`_state_lock` and pointed at from `_run_lock`'s own definition:

    _RETENTION_GATE -> state sidecar FILE lock -> _run_lock -> leaf locks

Nothing may hold `_run_lock` while waiting for the file lock. `_state_lock` takes
the file lock first, and `_record_run` and `_record_skip` no longer wrap it in
`_run_lock` at all -- `_record_run_locked` takes that lock only for the
`_run_sequence` bump, which cannot park. The bump has to stay under it: the
callers' outer hold was the only thing serialising it, and `(process, sequence)`
is the identity the compare-and-set inside `mutate` reads, so a shared sequence
would let a stale baseline pass a check it must fail.
The sidecar lock is taken with a ceiling derived from that hold rather than
`file_lock`'s 300s default, which is sized for a sub-second read plus a rename.
A shorter ceiling would refuse a contender that is only waiting, and that refusal
arrives as the `OSError` `_record_run` absorbs by keeping a completed upload's
record in memory alone -- so a short-lived process that exits first loses the
record and leaves the nightly loop due. `frontend._STAGING_LOCK_TIMEOUT` derives
its ceiling the same way, for a lock spanning a frontend build.
Nothing is uploaded and no run record is written, so the archive-and-record
agreement above is preserved -- rebuilding without Layer B instead would be the
torn state the read-once rule exists to prevent. Only the withdrawn direction
refuses; a grant arriving mid-build leaves an archive without Layer B, which the
next run picks up.

`test_aws_control_backup.py::TestSessionsArchiveLayerBGate` pins both directions,
that a permitted upload holds the setter's lock, that a scheduled withheld one holds
it too, and that an owner-initiated withheld one does not, that
no `config.json` key can grant it, that a grant does not cross accounts, and that
the store stays inside the fenced directory.

This decision is separate from the file export's
`dashboard.export_include_layer_b`: a downloaded file can be handed to another
person, while this archive lands in a bucket the operator owns, so the two are
different risk decisions and enabling one must not enable the other.

### Run identity and failed state writes

A run's `at` is the observed UTC wall time, not a unique identifier or a
monotonic clock. `process` (a random process token plus PID) and `sequence`
distinguish and order this process's completed run records even when wall time
ties or moves backwards. Recording and state updates share an in-process lock;
the existing sidecar lock still serializes disk writes across processes.
A newly recorded run unconditionally replaces its kind's prior state under the
sidecar lock, including prior-process or legacy records with equal or later wall
times. Only best-effort overlay/recovery comparisons use local sequence or the
other-process/legacy wall-time fallback; that fallback does not establish global
newest when a process's state was unobservable.

A successful upload stays successful if its state read or write fails. Memory
holds one last run per account/kind and at most `MAX_REMEMBERED_UPLOADS` pending
key/fingerprint pairs per account, not full run history. `last_runs` and the
restore proof read that memory. The next successful state update considers the
pending runs and merges their fingerprints under the sidecar lock. Its final
run slot can supersede a pending run rather than storing that run as history;
only after the write succeeds are the exact processed pending records cleared.
Failed recovery acknowledges nothing. Same-time acknowledgement of a different
run cannot clear the current one. The existing durable fingerprint bound still
applies, and a restart before recovery loses process-only metadata.
No API ordering envelope or UI state is added.

### Several installs, one drive

Drive discovery is by tag, so every install pointed at the account finds the same
bucket and writes to it. This is supported rather than refused, and the archive
keys carry the distinction: `backup/snapshots/<install>/...` and
`backup/sessions/<install>/...`, where `<install>` is a random 32-hex id held at
the top level of the app's own `backup.json` and minted on first use by
`backup.install_identity`. It is deliberately NOT `beacon.install_id()`, which is
the telemetry egress identity and is only materialised under telemetry consent;
minting it from the backup path would create a telemetry identity on a host that
opted out. An unwritable state file degrades to a process-local id rather than
refusing the backup, because refusing would stop a backup that works today.

The nested prefix is what lets one delimited listing answer two questions at
once: `storage.list_section` on `snapshots/` returns every install id present as a
folder and every pre-namespace archive as a file, so attribution and "another
install writes here" arrive together. A second call reads this install's own
prefix. Enumerating the OTHER installs' prefixes is opt-in
(`list_remote_backups(include_others=True)`, capped at
`backup.MAX_OTHER_INSTALLS`) because it costs a list call and a label read per
install; it exists because a replacement machine owns no archives, so a view
limited to its own prefix would show it nothing on the one occasion the bucket
holds the only surviving copy.

An install also publishes a human-readable label beside its own archives at
`<kind>/<install>/_label.json`, written from the upload path and under BOTH kind
prefixes on every backup, so a rename followed by a backup of one kind cannot
leave the other prefix serving the old name. Each S3 write carries its OWN
`_authorize_upload` immediately before it, with no other network call in between:
whichever of the two writes ran second would otherwise be running on a decision
taken before the first one's transfer, and since the archive upload can last
minutes, ordering the pair differently only moves which write is exposed. The gate
therefore lives inside `_publish_label` rather than at its call site, so the
authorization cannot be separated from the write it guards. The label is **display only**. The id
in the key is the identity S3 itself recorded and no writer can restate;
`backup.classify_key` and every decision that follows read that and nothing else.
A label read from the bucket is foreign-authored text, so it is control-stripped,
run through the same egress redactors `storage.list_section` applies to object
names, and length-bounded by `backup.sanitize_label`; the transfer is
range-bounded through `storage.get_object_head_bytes`, since the object's size is
another install's choice. The dashboard renders a foreign label in quotation marks
beside the owning id so the self-asserted part is visibly self-asserted.
`test_aws_control_backup.py::TestLabelIsDisplayOnly.test_a_published_label_cannot_make_a_foreign_archive_restorable`
pins that the gate never reads it.

`backup.restore_download` refuses every archive it cannot PROVE is this install's
own -- a co-tenant's, one under this install's own prefix with no matching entry in
the upload record, and one predating install ids -- unless the caller passes
`foreign_ok`. `routes._handle_backup_restore` answers `409 foreign_install_archive`
carrying both the refused origin and the owning id, because the three cases need
different words. The rule is uniform on purpose: an earlier revision refused only
the foreign case and left the other two to a confirmation dialog, which put a
safety property in one client, so any caller that did not open the dashboard
restored a planted archive with no override. `ORIGIN_SELF` is the only origin that
needs none, and it is the one the local upload record can vouch for.

An `ORIGIN_SELF` key whose downloaded bytes fail the recorded body fingerprint is
the one case where this install's archive can still be ON the drive: a co-writer
overwrote the key, so our bytes are the noncurrent version. `storage.get_file` takes
an optional version, and `backup._recover_recorded_version` uses it for exactly one
further read, of the `VersionId` `uploaded_versions` records for that key. It accepts
that read on the same evidence the current-version read uses and nothing more -- the
same body fingerprint, re-taken over the bytes that arrive. A match settles
provenance: those ARE the bytes this install uploaded. Whether they still open as a
`tar.gz` is not asked, because the current-version read does not ask it either and the
upload side pushes payloads it cannot read, so an own archive can legitimately be
malformed; refusing one only on the recovery path would hand the operator their own
file when nobody overwrote the key and a refusal when somebody did. An absent,
deleted or mismatched version returns the same `ORIGIN_UNVERIFIED` refusal, and so
does a staged copy that cannot be hashed -- reading the bytes back is part of
fetching them, so it sits inside the same guard as the transfer rather than escaping
a helper whose every non-matching outcome is that refusal. The recovery can only
find bytes that already pass; it can never widen what a restore accepts.

The extra read is also the one AWS call in a restore the caller did not ask for, so
`backup._authorize_recovery_read` authorizes it again immediately before it is made,
asking the same four questions `backup._authorize_upload` asks of the paid upload and
in the same order: the live caller identity must still name the requested account,
the app must still be enabled, S3 consent must still hold for this profile and
region, and the recorded grant must name THIS account. The network round-trip runs
FIRST and the cheap local decisions LAST, so no window sits between a check and the
read it guards. The first read can take minutes, and the route's pre-flight cannot
speak for a decision made after it ran -- a withdrawn grant, a disabled app, or a
profile repointed at another account. The stored grant is read ONCE and its profile,
region and account all checked against that one snapshot: grant reads are unlocked
while writes take the consent lock, so checking profile and region against one read
and the account against a second would let a re-grant landing between them satisfy
each half from a different record, turning a refusal into an allow. The parity between
the two gates is in the four questions they ask, not in the mechanism underneath them.
They differ in what a refusal does -- the upload raises, because a refused upload is a
failed run, while this returns a reason, so the recovery does not run and the caller
keeps exactly the refusal it already had -- and in how the grant is reached: the upload
gate still asks `is_granted` and then reads the grant again for its account, which is
a pre-existing race recorded in #12705 for its own review rather than changed here.

That further read is authorized against `s3:GetObjectVersion` rather than
`s3:GetObject` -- S3 treats a version-pinned `GetObject` as a distinct action -- and
`storage.get_file` reports whichever of the two matches the request it made.
`engine._checked` uses that name twice: as the failure label on every error, and, on an
`AccessDenied`, as the permission its remediation hint tells the operator to add.
Neither GetObject action appears in `_ACTION_STATEMENT_HINTS`, so the statement Sid
inside that hint falls back to the generic policy pointer; the action name in it is
still whatever the call passed. Naming the unversioned action on a pinned read would
send a denied operator to add a permission they already hold. The recommended drive
tier grants neither on `backup/*`: that prefix is write-only there on purpose, so any
restore already needs rights beyond it.

The recovery runs only where the mismatch would REFUSE. Under `foreign_ok` the caller
has already said it will take whatever is current at the key without proof, so there
is no refusal to rescue, and reaching past the current object would hand that caller
different bytes than it accepted, labelled `ORIGIN_SELF` instead of
`ORIGIN_UNVERIFIED`. Gating on the override keeps its meaning intact.

Retention is deliberately not taught about this: `_current_version_is_ours` stays a
question about the CURRENT version, so a recoverable-but-noncurrent key still holds
no `keep` slot. That errs toward retaining more, and counting it is a decision about
what may be DELETED.

The override is mandatory rather than optional: restoring onto a replacement
machine means nothing in the bucket is provably this install's, which is what
disaster recovery is, so a hard wall would block the one case the backup exists
for. `POST /install/label` renames this install and reaches no AWS service.

A shared drive is not shared state. Each backup is an opaque point-in-time
archive, and a restore only downloads it: the operator applies it themselves with
the gateway stopped. The panel copy says exactly that, because "share memory
between my laptop and my cloud desktop" is the request that leads people here.

The nightly toggle records whether an account is eligible for a scheduled
snapshot. `aws_control.hooks._run_once` resolves an account and drive, checks
S3 consent, runs only due backups, and SEL-audits invocation, success, and
failure. It skips unavailable accounts or absent drives rather than creating
resources itself. When `hooks._note_shared_drive` sees another install's prefix it
logs and SEL-audits the observation and then PROCEEDS. It does not take ownership
of the schedule: with the keys namespaced there is nothing left to collide, and a
single-owner schedule would leave one machine silently un-backed-up, which is
discovered at restore time and is worse than the state it replaced.

### A failed unattended attempt is recorded, so the loop can back off

The half-hourly wake is a due-CHECK interval and never a retry interval. Only
completed runs used to be recorded, so a deterministic fault — an unreadable file,
a disconnected mount, a payload database that cannot be shown free of credentials —
left the state file unable to distinguish
"never ran" from "keeps breaking": `due_for_nightly` took its never-ran branch on
every wake, re-staged the whole data home into a fresh temporary directory, and
repeated the same traceback roughly every half hour for as long as the fault
lasted.

`hooks._failed_attempt` closes that. It is the single place a failed unattended
attempt is both SEL-audited and recorded, reached from the shared setup's handler
and from each kind's own push, so the backoff cannot depend on which way the run
broke.

It closes that for a fault that leaves the state file writable, which is not every
fault. A full disk fails the failure-write as well, so nothing is recorded and every
wake is due exactly as before. That residual is deliberate rather than overlooked:
`record_nightly_failure` never raises, because a state file it cannot write must not
turn a logged backup failure into an unhandled one, and a count that did not persist
leaves the loop retrying as it does today. So a full disk is still a repeated-traceback
case; what this closes is the larger class where the disk is fine and the backup is not.

Cancellation does not reach it: a cancelled attempt is teardown, and
counting it would let a clean shutdown push the next night out.

The record lives under `backup.NIGHTLY_FAILURE_STATE_KEY`, per account then per
kind, as `{"at": iso8601, "since": iso8601, "consecutive": int, "error": str}`. It is a SEPARATE key
from `runs`, because `runs` is read by `uploaded_versions`, `_unchanged_baseline`
and the retention sweep as proof an archive exists — a failed attempt filed there
would hand each of them a baseline to compare against and a version to retire for
an upload that never happened.

`backup.NIGHTLY_RETRY_BACKOFF_SECS` maps consecutive failures to a wait, indexed by
N-1 with the last entry as the ceiling: 0, 1 h, 2 h, 4 h, 8 h, then 12 h. The first
entry is zero, so a single failure is retried on the next wake exactly as before —
one failure is not yet a pattern. A module-level assertion pins the ceiling below
`NIGHTLY_WINDOW_SECS`, which is what keeps this a backoff rather than a second way
for a backup the owner enabled to go quiet.

`_backoff_withholds` answers DUE for every unusable reading — a corrupt count, a
corrupt or absent stamp, a stamp in the future from a backwards clock step. That is
the rule `_a_day_since_last_run` already states for an unparseable success stamp,
and a failure record is a new place for the same silence to appear.

Any success clears the count, atomically inside `_record_run_locked`'s mutate
rather than as a second write beside it, so no wake can land between the run record
and the clear. Only the SCHEDULED path records a failure, while a success from
anywhere clears one: an owner pressing the button is present and has just
demonstrated the fault is gone, which is the line `_unattended_sessions_redaction_gap`
already draws. The status read serves the record as `nightlyFailures` so an operator
can see the count and the day it started; no console renderer ships with it.

The clear alone is not enough, because the two writers serialize under the sidecar lock
but each mutate re-reads fresh state. An unconditional failure write can therefore land
AFTER a concurrent manual success cleared the count and record a failure against a kind
that just succeeded. So `nightly_run_witness` reads the run slot's `(process, sequence)`
identity BEFORE an attempt starts, `record_nightly_failure` requires it, and the write is
refused when the slot moved during the attempt. Absent compares equal to absent, which is
what keeps a nightly that has never succeeded recording its count normally; only an actual
move refuses. That identity is the one `_record_run_locked`'s `expected` parameter
already established for this compare-and-set, because neither `at` nor `key` can stand
in for it: the clock resolves to a platform tick and a skip copies the matched run's key.

What the raced write costs was measured, not assumed, and the obvious claim is wrong: it
restarts the count at 1, `nightly_retry_delay_secs` answers 0 there, and the fresh run
record already holds the account not-due for the window, so it withholds no attempt. What
it produces is a false `nightlyFailures` row for an account that just backed up, plus a
one-step skew on the next genuine failure. The row is why the guard ships -- making that
state readable is half of what this change is for -- and a review lane that priced the
guard against the withheld-attempt claim was right to reject that claim.

A run record reaches the document by TWO paths, so the clear sits on both. The second is
`_merge_pending`, which carries a run whose own state write raised and was held in memory;
before it also cleared, a stale count outlived the success that should have ended it, and
after a restart withheld one nightly for up to the ceiling on an account that had already
backed up. It is gated on `_run_is_newer` for the same reason the run write is.

`run_witness` is a required keyword with no default, so a call site added later cannot
opt out of the protocol silently -- which is the shape of the bug it closes. Each hooks
call site reads it at the top of its own attempt, never in the handler, because by then
the window it has to witness has closed. A refusal returns `None` and is logged as the
good case it is: skipping a real failure costs the extra attempts the loop already makes,
while writing a false one misreports a healthy account, so this ambiguity resolves toward
attempting the backup like every other one here.

The row carries TWO stamps. `at` is the latest attempt and is what the backoff measures
from; `since` is when the current run of failures began, carried forward while the streak
continues and cleared with the row. Both are needed because the issue asks for the second
by name -- an operator has to see that the nightly has been failing since a particular day
-- and one overwritten stamp cannot say both. A stored `since` is carried only when it
PARSES as a timestamp, not merely when it is a non-empty string: it is published in an
operator-facing row and each write carries the previous one forward, so an unparseable
value would otherwise be rendered as the day the failures began for the whole life of the
streak. Anything unusable restarts the streak, so corruption can only ever under-report the
outage. The backoff never reads `since`, so a corrupt value there cannot affect scheduling
in either direction.

### The nightly sessions archive is a second, separate grant

`backup.nightly_sessions_enabled` authorizes the scheduled SESSIONS archive and
is never `nightly`. The two answer different questions -- one about memory and
workspace, one about every conversation the agent was ever shown -- so an
operator who enabled nightly snapshots has said nothing about transcripts. The
key is absent by default and an absent key reads False, so no install begins
uploading transcripts by being upgraded. Both read fail-closed: an unreadable
state file answers False, because a corrupt file must never be the reason an
unattended upload starts.

Due-ness is keyed per kind (`backup.due_for_sessions_nightly` against
`KIND_SESSIONS`), so a snapshot that ran an hour ago does not make the
transcripts look backed up, and a wake proceeds when EITHER kind is due. Each
kind is pushed inside its own `hooks._push_nightly` call with its own
try/except and its own audit subject (`backup/snapshots`, `backup/sessions`), so
one kind failing costs the other nothing and an incident review can tell which
bytes left the host. A platform without descriptor-pinned traversal is never due
for the archive: `run_sessions_backup` refuses there, so scheduling it would
record a failed run every wake for a payload that platform cannot produce.

The grant is settable on every registered account while the loop runs for the one
`resolve_default_account_profile` names, so "granted" and "will run" are separate
answers and `backup.scheduled_sessions_blocked_code` carries the second. Its
`scheduled_account` argument is the part only a per-account surface can answer:
the status route compares its target against `accounts.default_account_id` -- the
account half of the loop's own resolution, so the two cannot disagree about which
account is scheduled -- and reports `other_account` when they differ. The console
renders that beside the switch, which keeps reading back exactly as the owner set
it. Without it a grant recorded on a second account is authorized and unreachable
at once, and the operator learns which at the host loss the feature exists to
survive. The loop never sees that code: it reads the scheduling answer for the
account it just resolved, where the condition is false by construction, which is
why `scheduled_sessions_blocked_reason` covers only the other two conditions.
A host that cannot produce the archive outranks the account, because naming the
account would send the operator to a page carrying the same notice.

The scheduled path calls `backup.run_sessions_backup` unchanged, with the same
arguments the owner-triggered job passes and `CALLER_SCHEDULED`. So the
archive's CONTENTS, its redaction posture and its size behaviour are not
decided here and are not changed by scheduling: both session halves ship as they
are, byte-exact and unredacted, exactly as the owner-triggered archive already
ships them, and the push shares the one `_PUSH_TIMEOUT_SECS` budget. Whether
that posture is right for the archive at all is a question about the archive,
tracked on its own; the scheduler inherits whatever that path decides, because it
is the same function. What scheduling adds is one consent bit that is strictly
narrower than the owner-triggered route's gate, never wider.

## Dashboard surface

The app opens on an Overview pane, not on a listing: a strip of metric cards
(accounts, keys healthy of total, drive used, month-to-date spend, live share
links, backup schedule), each restating a fact one of the other panes owns and
carrying a one-line reading under the number, then an Accounts card and a Cloud
drive card side by side, then a Paid services card. The Overview adds no
mutation of its own beyond the two paid-service gates. Its account rows are the
same `AccountRow` component the Accounts pane renders (a `variant` prop decides
density), so the remove flow, the Reconnect disclosure and the hand-off gating
exist once. A row carries two controls, the select surface and its overflow
menu; Reconnect (offered on a degraded resolved row) and Remove are items in
that menu, and the health word on the row is the cue that the menu holds
something to do. The bare `/aws-control` path and an unknown pane segment both land on
Overview; every named pane path is unchanged. The month-to-date figure shares the
Usage pane's cost cache entry and, like it, settles to a dash with a visible
reason (consent missing, or the read failed) rather than a tooltip. A read that
fails (drive, bill, share links, backup schedule) renders an `AwsErrorNotice`
with a retry under the metric strip, and its card holds a dash. The one
rejection that is not a failure is the `aws_consent_required` 409, the reader's
own pending decision, which routes to the setup action or the consent gate; a
stale connection's 409 (`account_unavailable`, `account_mismatch`) is told apart
by its code and renders as the failed read it is. Neither the Cloud drive card
nor the Usage pane's storage meter repeats the byte total its metric card
already prints; each owns the split, drawn once as `StorageBar`.

Paid-service consent renders in two shapes from one component. `AwsConsentGate`'s
default mode is the full card the settings panels use; its `compact` mode is one
row per service in every state (receipt, ask, error) with no container of its
own, so the Overview and Usage panes lay those rows in a single `divide-y` list
inside their Paid services card. The Usage pane's month-to-date, storage and
object figures are metric cards; the storage split (one bar, one legend, one
tile per section) is the shared `StorageBar` from `shared.tsx`, drawn once and
placed by both the Usage pane's `StorageMeter` and the Overview's Cloud drive
card, so the two readings cannot drift. Health is encoded the same way on every
row (account, key, backup, share): a dot plus a `Badge` word, never colour alone,
and the word is never hidden at any width.

Every list on the app's panes (accounts, keys, backups, share links, library,
files) sits inside a `Card` with a `PanelSectionHeader`, empty states render
through the shared `EmptyState` (a filtered-to-nothing state through
`FilteredEmpty`, which offers the clear action in place), loading states mirror
the row box they replace, and the three file dialogs keep their hand-rolled
overlays because `DriveSectionView` restores focus to a remembered opener that
the Radix dialog would fight. The App Store card and detail page carry hero
art declared in `app.json` (`heroImage`, `heroImageDark`, `heroImageDetail`,
`heroImageDetailDark`), authored in the same palette and restraint as the other
builtins' art.

## HTTP surface

`routes.register_routes` exposes owner-gated reads for accounts, available
profiles, reconnect guidance, drive status/list/download/preview/search, costs,
library, backup status, share metadata, and rendered IAM policy. Its mutations
are profile registration and unregistration; drive bootstrap, upload, delete,
move, folder create/delete, and share; share-ledger removal; library push and
library removal; backup run, the snapshot and sessions nightly toggles,
retention-count updates, and staged restore; and renaming this install (local,
display only -- see "Several installs, one drive").

Drive bootstrap is the only API-level preview-plus-confirm flow. Upload, move,
profile registration, library push, library removal, share creation, and backup
mutations have no separate confirmation request; the dashboard separately
confirms object deletion, folder deletion, library removal, and account
removal. Account removal lives in an overflow menu beside each account row on
the Accounts pane, outside the row's select button so opening it cannot select
the account; the menu item reveals the same inline Cancel-plus-danger strip the
Files and Library folders use, naming the account (or, for the unresolved
pseudo-row, the keys it will forget) and stating that nothing in AWS or in the
AWS CLI configuration changes, and it posts every key the row holds; the menu
item itself carries that reassurance as a muted second line, since the strip
sits behind a click a cautious reader would otherwise refuse. Library removal
is offered on the Library folder's own listing — one overflow menu per listed
cloud copy, in both the grid and the list view, the same `⋮` shape the Files
folder's cards and rows use — and never on the "Add from Artifacts" picker. That
placement is the correctness boundary, not a layout preference: a picker row is a
LOCAL artifact joined to the `account -> slug` ledger, and because
`ArtifactStore.delete` does not prune that ledger and a new artifact starts at
version 1, a reused slug lends a never-pushed artifact another one's push record,
so a removal offered there empties a different artifact's copy under the wrong
name. No predicate available on such a row separates the two, and naming the
bucket folder in the confirm narrows the blast radius without fixing it — the
reader is still asked to vouch for an identity this machine cannot establish. A
folder row comes from the bucket listing, so removing it empties the object that
was LISTED rather than one inferred from the ledger. That fixes the target, not
the name: the card's label still comes from the slug-keyed join, and the folder
named in the confirm is built from that same shared slug, so under slug reuse the
reader can be shown one artifact's name over another's bytes with nothing on
screen able to separate them. Establishing whose copy it is needs the pushed
`meta.json` sidecar and is tracked by #6987; the same slug-targeted removal is
offered from the picker on the current release, so this placement neither
introduces that gap nor closes it. Removal is therefore not gated on local state
at all, which is
what makes a copy pushed from another machine (`remoteOnly` above) removable
rather than stranded. It is gated the way folder deletion is: the menu item
reveals an inline Cancel-plus-danger strip that names both the item and the
`artifacts/<slug>/` prefix it will empty, and that strip stays open until the
request resolves — it is the only place the outcome can render, so neither its
Cancel nor an early close may discard an in-flight answer. Every mutation is
owner-gated, restricted-session
refused, and SEL-audited. Account-targeted AWS operations additionally enforce
live identity and service consent, and egress paths enforce publish governance.
Library removal is deliberately outside that egress set: it sends no bytes out,
so a profile that denies publishing can still empty a bucket it is paying for.

## Error surfaces

Every failure the dashboard shows for this app renders through one wrapper,
`shared.AwsErrorNotice`, over the dashboard's shared `ErrorNotice` — never an
ad-hoc red paragraph, and never nothing. The wrapper exists because of a
mismatch the shared notice cannot bridge on its own: `ErrorNotice` recovers an
error's context from the error journal by matching the message it renders, and
this app renders a LOCALISED sentence keyed off the backend `code`, not the
backend prose, so that lookup can never match here. The client closes the gap
at the transport instead. `api.request` journals every non-ok response
(`utils/errorReport.recordError`: status, machine-readable `code`, path-only
endpoint, raw body) and hands the resulting entry to the thrown
`AwsControlError` as `report`; a transport-level rejection is journaled by its
own message and rethrown unchanged. `api.errorReportOf` is the one reader of
both paths, and `AwsErrorNotice` passes what it returns to the notice as the
structured report — so the "ask the agent" hand-off carries the endpoint, the
status, the code and the body, while the reader sees the sentence.
`api.test.ts::request error contract` pins the journal entry's shape, the
query-string strip, and that an error built outside the client (as tests do)
degrades to the sentence alone rather than throwing.

The hand-off keeps `ErrorNotice`'s opt-in default and is stated at every call
site in this app. The safety argument for leaving it off is an unsaved draft the
navigation would destroy, so the three notices rendered beside unsaved input —
the folder-create failure under the folder-name field, the share failure under
the share note, and the register failure under the Add-accounts checkboxes —
leave it off; the refusal is journaled regardless. Two panes go further and
gate every notice they render on their one draft being absent, because all of
those notices share the screen with it: the Files pane on the folder-name
disclosure being closed, and the accounts pane on no profile being ticked in
the Add-accounts form (`AddAccounts` reports that through `onDraftChange`; the
row and connections-card Reconnect notices and the orphaned-consent rescue all
take the pane's `handOff`). A
client-side name check (a rejected folder or file name) leaves it off too: no
request was made, so there is no report and nothing for the agent to read.
Every other notice opts in. `AwsConsentGate` is shared with settings panels
that DO hold drafts, so it takes the decision as an `askAgent` prop, off by
default, which this app sets. Every READ notice this app renders itself offers
a Try-again button under the notice (`onRetry`), because a transient read is
the one failure the reader can clear alone; a mutation's retry is the control
that fired it, which is still on screen. `AwsConsentGate`'s own status-read
failure carries that button itself, because with the grant and withdraw
controls gone the notice is the whole card. The page-level accounts failure words
a 403 as a permission answer (sign in as the owner) rather than as a transient
read, because a retry cannot clear it. A confirm strip that already holds
Cancel and Delete renders its inline notice on its own line (`basis-full`), so
the hand-off never becomes a third action in that row.

Two classes of failure previously rendered nothing and now render a notice:
every read whose query had no error branch (the Files listing, drive status
outside the consent 409, the permissions drawer, backup status, the share
ledger, the local profile scan) and every mutation whose error was never read
(drive create-confirm, share creation, nightly toggle, restore, share removal).
A failed read must not fall through to the surface's empty state — a failed
listing is not an empty folder, a failed profile scan is not "nothing left to
add" — and the tests for each state assert the empty state's absence alongside
the notice. Two states are deliberately NOT errors: a 403 whose code is
`app_disabled` renders the disabled-app copy (any other 403 — a non-owner
caller's `dashboard_owner_required` — is an error to diagnose and goes to the
notice), and a costs 409 `aws_consent_required` is answered by the Cost Explorer
ask, not a banner beside it. A reason the backend reports inside a 200 (the
backup archive's `remoteError`) travels as the notice's message under the
localised lead, so the hand-off carries the text AWS returned.
`DrivePage.test.tsx::error surfaces reach the agent`,
`AwsControlPage.test.tsx::edge states`, and `ConsoleView.test.tsx` pin these.

## The crew container runtime

`crew/runtime/` is the source of a Linux container image, not code the owner's
gateway runs. It is what a remote crew runs AS: a supervisor that orders and
watches the task's processes, a front process that receives a turn, and Kiro
Crew's own backend executing it. The design of record is
[rfc-remote-instance-on-fargate](../../request-for-change/rfc-remote-instance-on-fargate.md).

The tree is a docker build context and deliberately not a Python package:
`crew/runtime/` has no `__init__.py`, its modules import each other as top-level
`container.*` because that is what they are inside the image, and
`test_spawn_audit.py::test_container_image_assets_are_not_imported` pins that the
gateway never imports it. Three repo-level audits exempt it by shape for that
reason: the spawn audit (`_is_container_image_asset`), the MCP secret-caller scan,
and the hardcoded-agents-dir ratchet.

Its tests live in `container_tests/`, run only by the `backend-test-crew-container`
lane in `ci.yml` (see [ci-and-reviews](../../ci/ci-and-reviews.md)), and are
excluded from `setup.cfg`'s `package_data` and from `MANIFEST.in`, so a user's
wheel and DMG carry the image's build context and not its test suite.
`test_crew_runtime_payload.py` pins both directions.

The image's two review-sensitive fetches are pinned to what was reviewed: the
base image by manifest-list digest on the explicit `-bookworm` tag (a bare tag
can move -- `3.12-slim` had already drifted to trixie when the pin was added;
the digest names the multi-arch index, from which the builder's platform
selects the per-arch manifest) and the kiro-cli tarball by a per-arch sha256
recorded in the Dockerfile. The host publishes checksums beside the tarballs,
but a checksum served by the same host it protects verifies transport, not
review, so the reviewed values live in the repo. A moved tag or a re-published
tarball fails the build instead of silently changing the image. The remaining
fetches are bounded but not content-addressed:
`container/requirements.txt` pins direct versions yet carries no `--hash`
entries (pip's hash-checking mode is all-or-nothing per invocation and the same
install includes the locally built `vendor/*.whl`, whose hash cannot be
recorded ahead of the build -- transitive resolution can therefore still
drift), and apt packages are unpinned, since bookworm point releases drop
superseded versions and a pin would break on every security update.

### Three processes, one task

| Process | Owns | Exposure |
|---|---|---|
| Supervisor | process order, the crew bundle install, the container's own config, teardown | no listener; it is the task's init process |
| Front | receiving a turn, stripping any route prefix, forwarding over loopback | the only listener, port 8080 |
| Kiro Crew backend | sessions, conversations, transcripts, MCP, subagents, skills, memory | loopback only (`127.0.0.1`, not configurable) |

Nothing serves a user interface. `config_dir` equals `data_home`: the gateway's
`config_dir()` and `data_home()` resolve to the same directory, so
`session_map.json` and `open_slots.json` sit at the home root, and the supervisor
refuses to start when the two disagree, because a backend writing to one path
while the deployment points at another loses the record of which conversations
existed.

The startup order is a correctness requirement rather than a preference:

1. Gate the environment (path layout, model credential, sandbox) and install the
   crew bundle. Nothing has started.
2. Write the container's own configuration. It must land after the bundle, because
   a bundle may ship config and the container's posture has to win on the keys it
   sets, and before the backend, which reads the file at boot.
3. Start the backend. `wait_until_ready` returns only when the port answers **and**
   the boot secret file exists; process-alive is not ready.
4. Start the front process.

There is no backup sidecar and no restore phase. Durability across task
replacement is a capability the container does not have; the front still fetches a
slot's transcript from S3 on demand, so `SMC_BACKUP_BUCKET`/`SMC_BACKUP_PREFIX`
name where it reads. When durability returns it must reinstate the ordering rule
that made it correct -- a restore must finish before the backend starts, because
the backend's periodic flush persists its in-memory slot table and a flush landing
before the restore completes writes an empty set over the record of which
conversations existed.

### The front layer

Two customer routes and nothing else. Everything is classified once, on the path
AFTER any configured route prefix is stripped, because classifying the prefixed
path let a control route read as a customer route in an earlier build:

| Route | Auth | What it does |
|---|---|---|
| `POST /v1/chat/completions` | none of its own | one turn, forwarded to the backend |
| `GET /health` | none of its own | liveness; `{"status": "ok"}` and no internals |
| everything else | `SMC_CONTROL_SECRET` in `X-SMC-Control-Secret` | 404: no control operation is served yet |

Both outcomes of the control-authorization decision are recorded before the response is
sent -- the grant as well as the deny, because a trail holding only grants records exactly
the events nobody needed to investigate, and the deny is what answers who tried. **A
decision that cannot be recorded is not acted on**: the request gets a 503
`control_audit_unavailable` rather than the 403 or the 404 it would otherwise get, because
serving the grant would be the unaudited grant the record exists to prevent, and serving
the deny would leave a denial nobody can later account for. 503 rather than 403 says the
failure is the container's, not the caller's.

The record mirrors `kiro_crew.sel` -- the `SecurityEvent` field names, its `event_type`
vocabulary (`tool_approval` / `tool_denial`), and the audit-or-deny discipline
`log_tool_invocation(critical=True)` exists for -- and is mirrored rather than called
because of the SINK, not importability. The wheel is installed in this image, so
`kiro_crew.sel` would import; what does not survive the move is where SEL keeps its log.
`sel._default_dir()` resolves under `config_dir()` with the HMAC key in `trust/` beside
it, which here means the persistent volume, and the supervisor and the model worker run as
the same user -- so the audited party could delete both and write a self-consistent
replacement, and there is no trust root in the container to anchor a chain. Nothing
harvests that file either: the container's only writer to the owner's bucket is the
transcript store. So the record goes to the process log stream, which leaves the task as
it is written. It carries no `prev_hash` or `entry_hash`, because claiming SEL's integrity
fields for an unchained line would assert a property the record does not have. Durability
of that stream is the task definition's log driver, which belongs to the deploy track
rather than to this image.

No header value is ever recorded -- not the control secret, not any other header -- so
what the record says is only whether the header was presented.

**The sink is the container's own, and `emit` refuses when a record would not be
delivered.** This is a different shape of defect from the rest of this document: the guard
above is present, its logic is right, and it does refuse when it should -- but under
uvicorn's own logging configuration a module logger in this process resolves to an
effective level of WARNING with no reachable handler, and `logging.lastResort` sits at
WARNING too, so an INFO audit call writes zero bytes. A dropped record is not an error, so
nothing raised, the audit-or-deny path never fired, and every control decision was acted
on with no record -- in production only, since a test session has a root handler. A guard
that is live only where it is not needed cannot be told from no guard.

Two halves, both load-bearing. `configure_sink()` attaches an INFO-capable handler that
this module owns, called from `build_app` rather than from the process entrypoint so that
an app which can serve a request has one -- a lazy "configure on first emit" would let
whichever request arrives first decide whether the audit works. And `emit` checks that a
record would actually reach a handler before writing it, which is what stops a later
logging change from switching the audit off silently. The check is not
`logger.hasHandlers()`: that ignores the logger's effective level and every handler's own
level, so it answers yes for a logger whose only reachable handler is at WARNING, which is
exactly the arrangement that drops an INFO record.

`/health` is registered twice, prefixed and bare, so liveness can be probed
without knowing the crew's prefix. The route prefix is optional and empty by
default; nothing in this repository sets `SMC_ROUTE_PREFIX`, and a wrong value
fails closed because a path that does not strip to one of the two customer routes
is control and is refused.

The control gate sits IN FRONT of the 404 rather than behind it. Adding a control
route is then adding a handler rather than also remembering to authorise it, and
an unauthorised caller cannot learn which control paths exist by reading which
ones 404. `_control_authorized` fails closed when no control secret is configured
and compares in constant time, on bytes rather than `str`, because
`hmac.compare_digest` raises `TypeError` on a non-ASCII `str` and a caller could
otherwise turn a 403 into an unhandled 500.

**How an endpoint gets added.** Decide first whether it is a customer route or a
control route, because that decision is the security boundary and not a detail of
registration. A customer route is added to the allowlist that `gateway` checks and
inherits no authentication of its own, so it must be safe for anyone who can reach
the port; a control route needs only a handler, since the gate already refuses
without the secret. Either way the path is matched on the STRIPPED path, and the
turn path stays the only route that accepts a caller-supplied conversation id.

**Authorisation, and what this process cannot do.** Reaching port 8080 is an
authorised call in the owner's own account, decided before the request arrives; the
task is not published to the internet and has no external DNS name. That decision's
result is not passed through to the front process, so it has no caller identity to
bind a forwarded conversation id to, and a binding written against an absent
identity would fail open. What it does instead is refuse to serve customer turns at
all unless the deployment has declared `SMC_SINGLE_PRINCIPAL`, which is the RFC's
one-owner invariant made explicit and enforced at startup rather than assumed. The
refusal is at startup for the reason `require_api_key` refuses at startup: a
container that answers its port while mixing two callers' conversations looks
healthy and is not.

### The loopback hop

The front forwards to the gateway's own `POST /v1/chat/completions` with
`{model, messages, id, stream}`, returning one JSON completion or an SSE stream.
`model` is set from the DEPLOYED crew name and never copied from the payload, and
`id` is the slot id, which is what continues a conversation.

Facts the front must respect, each established by reading the gateway's source and
each wrong once in a way that produced no error:

- **Authenticate with `X-Internal-Secret`, read from disk on EVERY attempt.** The
  boot secret is `os.urandom(16)` per boot and is never persisted, so a cached copy
  works until the backend restarts and then 403s on every request in a way that
  looks like a client fault. On a 403 the front re-reads the secret once and retries
  once, then relays the failure.
- **Do not forward the client's `Origin` or any `X-Forwarded-*` header.** Loopback
  with no Origin is trusted by the gateway's CSRF check and a forwarded foreign
  Origin trips it. Outbound headers are built from scratch rather than copied and
  stripped, so a header the caller sends cannot reach the backend by accident.
- **A busy slot answers 409.** Requests for one slot id are serialised in the front
  process rather than surfacing 409 to the caller.
- **The stream is OpenAI-format, not ACP.** Completion chunks, keepalives, a
  terminal sentinel and an error object, with no event names at all. The ACP
  `sessionUpdate` vocabulary lives on the owner control stream, which this process
  does not serve, so the projection is keyed on frame shape and is fail-closed: a
  kind added later is dropped rather than relayed.
- **Readiness proves less than it looks like.** A backend with no model credential
  answers its port and then returns `503 kiro_prerequisite_required` on every turn,
  so a present `KIRO_API_KEY` is not a working one and only a real turn establishes
  that it is.

A restored transcript is bounded by SIZE as well as by shape. The bytes come from the
bucket and are held in memory for the length of a turn, so without a ceiling one stored
object decides how much memory the task uses. `ContentLength` is checked first and then
not trusted -- a header is a claim by the source -- and the read is streamed in chunks
against the same 64 MiB ceiling, one chunk past it so an object of exactly that size can
be told from a larger one. The refusal is a `TranscriptUnavailable`, so an oversized
object fails that turn rather than letting the backend serve one with the conversation
missing.

### Launching the backend

Verified names only, because the plausible ones are read by nothing:
`KIROCREW_BIND=127.0.0.1` (the published image sets `0.0.0.0`, and a deployment
that does not override it puts the backend on the network while every local test
still passes); `KIROCREW_HOME` equal to `SMC_DATA_HOME`, which is what makes the
boot secret path resolve; `KIROCREW_TELEMETRY_DISABLED=1` to silence the beacon;
`--no-crons`, because arming the scheduler fires any overdue job immediately; and
`--approval yolo`, which the gateway refuses unless `KIROCREW_HOME` is explicitly
non-default. Nothing in the container is there to click Approve, so an interactive
prompt would be an indefinite stall rather than a question, and the cost is stated
plainly: every tool the crew carries is auto-approved for whatever a caller's
message causes it to do. The boot update check cannot be disabled by config; it is
recorded as an outbound request the deployment makes rather than suppressed.

**The container serves no messaging channel, and disables them positively.** Before
the backend starts, the supervisor writes `<config dir>/config.json` with every
channel section's `enabled` false, merged over anything already there so a shipped
file cannot outvote it, and the launch environment carries no channel credential.
Both halves are required and neither covers the other's case: `imessage` and
`whatsapp` carry no credential in the gateway's channel registry, so they start on
their config flag alone, and `slack` has no `enabled` key at all, so it starts on
its tokens alone. Both name lists are ratcheted against that registry in both
directions by `test_crew_container_config_isolation.py` -- a channel the gateway
can start that the container does not disable fails, and so does a name the gateway
does not have, because that reads as coverage while doing nothing.

**The backend environment drops what the worker must not hold.** The task role
(`AWS_CONTAINER_CREDENTIALS_RELATIVE_URI` and its peers) and the front's
`SMC_CONTROL_SECRET` are removed: the backend spawns the model subprocess with this
environment, that subprocess auto-approves every tool, and a turn could otherwise
read the task role from its own environment and act as it. `KIRO_API_KEY` cannot be
removed, because kiro-cli re-injects it into the worker and it is the whole
model-auth mechanism.

**A reinstall replaces the bundle's own files and prunes nothing else.**
`install_bundle` runs at every boot and the data home may be a persistent volume, so
the skills install has two jobs that pull against each other: a skill a later bundle
DROPPED must not stay active, and a skill the running crew created must not be
deleted. Replacing the whole tree satisfies the first and violates the second, so the
two sets are separated. The bundle-managed set is derived from the tree in the image
layer, which needs no trust and cannot be forged by the crew; the marker records that
set so the NEXT install can prune exactly what this one installed. Pruning is per
FILE, not per directory, because a directory can hold both the bundle's file and the
crew's. When the marker is absent, hand-edited or from an older build, the previous
set is unknown and NOTHING is pruned: a stale skill an operator can delete is a
smaller harm than the crew's work disappearing silently, and the log says which of the
two happened. A symlink at a bundle-managed path is unlinked before that file is
written -- a pointer is not work, and its target is never opened -- while a link at the
skills ROOT is still refused outright, since that one redirects the whole install.

**The install marker is authenticated, because it lives where the crew can write.**
The record the prune reads sits in the data home, so its contents are an INPUT and not
state this code owns; constraining what the list may contain does not change that. It
carries an HMAC tag keyed on `SMC_CONTROL_SECRET` -- the one value the supervisor holds
that the worker provably does not, since `build_backend_env` removes it from the
environment the backend is launched with and the worker inherits that environment.
Relocating the marker instead is not available: the image runs the supervisor and the
worker as the same user, so no directory on the persistent volume is writable by one and
not the other, and a location in the read-only image layer cannot record what a previous
task installed, which is the marker's whole purpose. Every untrusted case -- no tag, a
tag that does not verify, no control secret to verify with -- prunes nothing and is
logged as itself, because "the tag is wrong" and "there is no marker" are different
things to be told.

**Every directory the container creates on that volume refuses rather than crashes.**
`mkdir(exist_ok=True)` raises `FileExistsError` when a regular file holds the path, and a
previous task can leave one where a skill directory belongs. Uncaught, that crashes the
supervisor, ECS restarts the task, and the next boot hits the same file on the same
volume -- a boot loop that spends the owner's money and never says what is wrong. A boot
loop is worse than a refusal for the reason a crash is worse than a refusal, so each
directory goes through one helper that names the path it could not create.

**The prune's bound is in the code, not in the record.** The marker lives in the data
home, which the crew can write, so what it says is an INPUT: an absolute entry, a
non-normalised one, one carrying `..`, or a clean-looking one whose parent is a symlink
would each turn "delete the bundle's own file" into "delete a file of the entry's
choosing". Pruning from a recorded list is narrower than replacing the tree and would
be strictly worse if the list decided where the deleting happens.

So the bound is enforced twice. Any entry that is not a plain relative path is refused
by name, and one bad entry discards the WHOLE record rather than only itself: a
malformed entry means the record is corrupt or hostile, which says nothing good about
the entries beside it, so the install falls back to pruning nothing and logs an error.
It does not fail the boot, because refusing to start over a corrupt bookkeeping file
would be a worse outcome than the residue. Then each deletion walks descriptors from
the skills directory with `O_NOFOLLOW | O_DIRECTORY` and unlinks relative to the last
one, so a component swapped for a symlink is refused by the kernel as it is traversed
and the directory verified is the directory deleted from -- there is no window in which
the path is re-resolved by name. A resolved-containment check runs first as well, not
because the walk needs it but because it turns "this entry leaves the tree" into its
own named refusal instead of a bare `ELOOP`. The leaf's own type is read by `lstat` on
that descriptor, so a link at the recorded path is left alone rather than followed.

**A transcript entry that is not a file is refused, not opened.** Boot restores no
transcripts, so what is on disk was put there by an earlier turn or by the restore,
and the restore's bytes and keys come from the backup bucket -- names and files from
outside the container. Before the backend is handed a path it will open and append to,
the entry's shape is checked: a directory, a FIFO, a socket, a symlink or a
hard-linked file is refused by name with `TranscriptUnavailable`, which fails that one
turn. A crash mid-turn on whatever `open()` raises would be worse, because a refusal
is auditable and a crash is not.

Shape is decided on the DESCRIPTOR, never on the name. `Path.exists()` follows a
symlink, answers true for a directory, and is a separate resolution from the open that
follows it, so a check by name plus an open by name is the check-to-use gap every
finding in this area lives in. The probe opens with the link refused and `O_NONBLOCK`
(a FIFO open would otherwise wait for a writer and hang the turn rather than answer),
then `fstat`s that descriptor, so the entry validated is the entry that was opened.
`st_nlink > 1` is refused as well as a non-regular type, because a hard link is a
regular file and the backend appends to this path. This mirrors
`hooks.safe_read_file_bytes_nolink`, which the container cannot import, the way
`supervisor/bundle.py` mirrors the agents-dir resolver. The write side refuses a
symlinked sessions directory for the same reason `mkdir(exist_ok=True)` cannot be
trusted, and still installs by `os.link` so an existing target is never clobbered.

A publish COLLISION goes through the same check. `os.link` failing means the entry the
turn will use is not the one just written and has had none of these checks applied to
it, so the winner is validated by the same helper before it is accepted. Keeping it is
still right when it is a transcript -- whoever wrote it has the newer history -- and a
shape that fails the check fails that turn rather than being handed to the backend.

**The container writes the sandbox settings rather than inheriting them.** The
gateway reads `agent.sandbox` and two unsandboxed-fallback flags from the same
`config.json`, so a file supplied to the task could turn the sandbox off while the
supervisor's refusal reported nothing wrong. All three are forced -- `sandbox` to
`auto`, both flags to false -- and forcing rather than defaulting matters because
`sandbox_allow_unsandboxed_exec` resolves an undeclared value through a platform
default, so silence is not a constant. The rule is by PREFIX rather than by that list
of three: `test_crew_container_config_isolation.py` requires every `AgentConfig`
field whose name begins with `sandbox` to appear in `FORCED_AGENT_SETTINGS`, with the
value checked as well as the key, so a sandbox knob added to the gateway reds CI
until the container decides what to write for it.

**The config is published atomically.** The file's failure is in the future: nothing
reads it during the write, and a truncated write is read at the NEXT start, on a
container that boots on a config it cannot parse and with the run that produced it
already gone. So the write is a sibling temp in the destination's own directory
opened `O_EXCL | O_NOFOLLOW`, fsynced, then one `os.replace`, with the directory
fsynced after so the rename itself survives a power loss. A symlink at the
destination is refused rather than replaced: `rename` would unlink the link rather
than follow it, so nothing would be written through it, but a link there means
something else chose the path and consuming it silently hides that.

**A link planted where the boot secret goes stops the task.** The gateway writes
`run/gateway-<port>.secret` itself, with an ordinary link-following open, and
truncates it on every start; the model worker can write inside the data home and
what it writes is driven by prompt content. Nothing in the container can make the
gateway's writer link-safe, so the supervisor refuses to start when the run
directory or the secret path is a symlink, or when the secret path is not a regular
file. The checks are `lstat`-based, because `exists()` follows the link they are
looking for. The container's own writes into the data home already refuse a link at
the destination (`bundle._write_nofollow`); this is the same guard for a file
another process writes.

### Sandboxed-only, and why there is no opt-in

kiro-cli runs the model subprocess inside an unprivileged user namespace, and
without one `wrap_argv` fails closed. This container runs SANDBOXED-ONLY: there is
deliberately no config key or environment variable that opts into unsandboxed
execution, and the supervisor refuses to start on a host that cannot provide the
sandbox rather than running the worker exposed.

The refusal is positive. The probe returns an available verdict, a denied verdict,
or an `undetermined: <why>` verdict, and only the first proceeds: undetermined
refuses and names what could not be determined, and so does any verdict the guard
does not recognise. Reading "could not determine" as "probably fine" fails open as
new hosts appear, which is the same defect as reading the environment through a
denylist.

Why no opt-in, and why the task boundary is not a substitute for one: the model
subprocess auto-approves every tool and its environment carries `KIRO_API_KEY`, so
an unsandboxed worker would run an auto-approved shell, driven by untrusted prompt
content, with a live credential readable in its own environment. The ECS task
boundary (one owner, one data home, no public endpoint, reached only by an
authorised call in the owner's own account) does not close that path, because the
attacker there is the caller's own prompt content, already inside the boundary.
Offering an unsandboxed posture safely requires brokering the model credential out
of the worker's environment, which is tracked separately; until then the worker is
sandboxed or the container does not start. Unprivileged user namespaces are not
available on Fargate today
([aws/containers-roadmap#2102](https://github.com/aws/containers-roadmap/issues/2102)),
so the supported target is a host that permits them.

### Shutdown

A kiro-cli worker spawns with `start_new_session`, so it `setsid`s into its own
process group and ESCAPES a `killpg` on the backend. Only the backend's own SIGTERM
handling reaps it, which makes the drain window load-bearing rather than a
courtesy: too short a drain SIGKILLs the backend before it finishes reaping and
orphans a worker that goes on to finish its turn. Teardown order is front, then
backend -- stop new turns arriving, then let the backend drain and flush -- and
anything still alive afterwards is an escaped worker, which the teardown sweeps by
process group over bounded rounds, because a killed process's children reparent to
PID 1 and surface in the next round.

The exit code distinguishes the two reasons, because it is the only thing the
platform reads: a stop signal is the one success case, and any other reason,
including one this code cannot account for, exits non-zero. Reporting both as 0
told ECS that a crash loop was a clean shutdown.
