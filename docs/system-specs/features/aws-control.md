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
profile registration; drive bootstrap, upload, delete, folder create/delete,
and share; share-ledger removal; library push; backup run, nightly toggle, and
staged restore.

Drive bootstrap is the only API-level preview-plus-confirm flow. Upload, profile
registration, library push, share creation, and backup mutations have no
separate confirmation request; the dashboard separately confirms object and
folder deletion. Every mutation is owner-gated, restricted-session refused, and
SEL-audited. Account-targeted AWS operations additionally enforce live identity
and service consent, and egress paths enforce publish governance.
