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

## HTTP surface

`routes.register_routes` exposes owner-gated reads for accounts, available
profiles, reconnect guidance, drive status/list/download, costs, library,
backup status, share metadata, and rendered IAM policy. Its mutations are
profile registration; drive bootstrap, upload, delete, move, folder
create/delete, and share; share-ledger removal; library push and library
removal; backup run, nightly toggle, and staged restore.

Drive bootstrap is the only API-level preview-plus-confirm flow. Upload, move,
profile registration, library push, library removal, share creation, and backup
mutations have no separate confirmation request; the dashboard separately
confirms object deletion, folder deletion, and library removal. Library removal
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
