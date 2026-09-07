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
than purging historical versions. This is the current recovery property; the
app does not implement a version purge.

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
thirteen locales, so it shows a translated "not checked" line gated on `checked`,
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
empties the listing rather than reaching billing-zero; a version purge remains
outstanding, as it does for the Drive's own deletes.

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

The nightly toggle records whether an account is eligible for a scheduled
snapshot. `aws_control.hooks._run_once` resolves an account and drive, checks
S3 consent, runs only due backups, and SEL-audits invocation, success, and
failure. It skips unavailable accounts or absent drives rather than creating
resources itself.

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
library removal; backup run, nightly toggle, and staged restore.

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
