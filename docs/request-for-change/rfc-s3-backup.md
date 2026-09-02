---
title: Off-host backup — a bundle a dead machine cannot take with it
status: draft
author: mingweic
created: 2026-08-11
last-audited: 2026-08-11
audited-at: f4d3327a7
doc-pr: 2744
implementation-prs: [2764]
tracking-issues: []
supersedes: []
superseded-by: []
---

# RFC: Off-host backup — a bundle a dead machine cannot take with it

## TL;DR

* Nothing in the product survives losing the host. Two of the three
  state-packaging mechanisms write to local disk; the third needs the source host
  still answering on a socket.
* The snapshot bundle omits the conversations. No component stages transcripts,
  `uploads/`, or `artifacts/` — so a restore today returns settings and memory but
  not the work.
* Two PRs. **M1: memory is backed up off-host and comes back** — the policy seam, a
  self-contained `memory` component, S3 up and down. **M2: sessions are backed up**,
  riding M1's seam and destination.
* The destination is **created once by a human**, not named per run. A backup job that
  must decide whether an arbitrary bucket is safe to write memory into is being asked
  a question that does not close (D5); creating the bucket ourselves and refusing
  every other one answers it once, visibly.
* Memory first because it is 22 MB against 385 MB, carries no `config.json` and so
  no secret question, and proves upload plus restore before the heavy part.
* Scheduling is not a milestone: a `command`-kind cron already runs a shell command
  with no model in the loop, so M1's command is schedulable on arrival.
* 385 MB is irreplaceable on a real install, so a full bundle per run beats
  incremental transfer.
* The seam ships in M1 even though `memory` needs no redaction, because today "what
  is in a bundle" and "is it safe to hand to someone" are both answered by which
  filenames happen to be on an allowlist.
* One decision still open, and it blocks neither milestone: the bundle needs an
  explicit secret policy per purpose — a backup keeps credentials so recovery is
  turnkey, a shared export must not carry them (O1). Retention (O5) is now closed by
  versioning plus a lifecycle rule.

Motivating incident: an operator's cloud desktop went unreachable — no login, no
SSH, no reboot — taking every conversation and every learned memory with it.

## Current state

| Mechanism | Code | Destination |
|---|---|---|
| CLI snapshot / restore | `snapshot.py` (`VALID_COMPONENTS`:29, `CORE_FILES`:142) | `<data home>/snapshots/` — inside what it protects |
| Dashboard export / import | `portability.py` (`EXPORT_EXCLUDE`:41, `EXCLUDE_DIRS`:59) | browser download, unscheduled |
| Session transfer | `dashboard/session_transfer.py`, `handlers_instances.py:417` | another **live** instance over an SSH tunnel |

Session transfer needs both hosts up simultaneously — the one condition a host loss
breaks — and carries no memory or config (`instances.md` §14). No code path in the
repository writes crew state to a remote store.

## Three problems

**P1 — the bundle omits the conversations.** No component stages the transcript
store `session_storage.py:7-10` documents (`<data home>/sessions/*.jsonl` +
`sessions/archive/`, resolver `:277`; `<kiro home>/sessions/cli/<sid>.*`, resolver
`config/paths.py` (`kiro_sessions_dir`)). `uploads/` is excluded outright on the dashboard path
(`portability.py:62`) and absent from `CORE_FILES`; `artifacts/` is in neither.
Measured on one install: 385 MB irreplaceable (322 MB transcripts, 28 MB uploads,
22 MB memory DBs, < 2 MB config + artifacts) against a ~30 MB bundle today. Small
enough that full-bundle-per-run beats incremental.

**P2 — one component ships a reference to state no component ships.**
`CORE_FILES["config"]`:144 carries `session_map.json`, the join to kiro-cli
sessions living outside the crew home (`session_transfer.py:16`), while neither
side of what it points at is staged; a `session_map` entry is load-bearing for
storage reclamation (`state.py:3207`). The dashboard path takes the opposite
position (`portability.py:54`). This RFC asserts no runtime consequence — M2
lands a fix or a test pinning current behavior. It is evidence that "what belongs
in a bundle" was never settled between the two implementations.

**P3 — both destinations are the machine being backed up**, and neither is
scheduled.

## Already in place

`deploy/` supplies the AWS half: `engine.run_aws`:99 (sandboxed `aws` CLI
chokepoint), `create_private_bucket`:268 and `_harden_bucket`:215 (idempotent Block
Public Access + `BucketOwnerEnforced` + SSE-AES256 + tags),
`map_access_denied`:123, and `profiles.py` — a profile-*name* registry already
consumed outside deploy (`apps/routes.py:310`), documented at `profiles.py:1` as
names only, never keys. `boto3` is optional (`setup.cfg:110`, `voice` extra) and
neither deploy nor cloud uses it. Follow that: shell out, add no hard dependency.

## Goals

1. One command produces one artifact from which a fresh host recovers memory,
   lessons, settings, crons, workspace, and the sessions a human recognizes.
2. That artifact lands off-host, in storage the user already owns.
3. It runs on a schedule, with no model in the loop.
4. Recovery runs from a CLI on a bare host — a dying host often cannot serve a
   dashboard.
5. No new credential surface: reuse the profile-name registry and the subprocess
   chokepoint.

## Non-goals

Continuous replication (RPO is one interval, honestly stated). Sharing state
between users — that is the portability export. Backing up the whole 30 GB
kiro-cli replay store, most of which is orphaned logs (see O2). Replacing a hosted
runtime; this is local-host disaster recovery, an orthogonal axis.

An earlier revision of this document also ruled out bucket versioning and lifecycle
rules, on the grounds that teardown cannot empty a versioned bucket. That reasoning
was inherited from `deploy`, which has a teardown path. **Backup has none** — there
is no destroy verb and nothing reaps a backup bucket. The constraint therefore does
not apply here, and versioning is load-bearing rather than optional: without it, a
bundle overwritten by a truncated or corrupt run is unrecoverable. D5 adopts it, and
that is what closes O5.

## Design

**D1 — every component declares a purpose policy, and the bundle declares its
purpose.** A bundle is stamped `purpose: backup` or `purpose: share`, and each
component declares what class of data it carries and which policy applies to it
under each purpose. The redaction hook sits at staging, once, and every component
passes through it whether or not it has anything to redact.

This seam exists from the first phase even though the first phase does not need it.
The reason is P1 and the secret finding below: today "what is in a bundle" and "is
it safe to hand to someone" are both answered by which filenames happen to be on an
allowlist, so the two purposes cannot diverge without one. `memory` declares no
redaction — its payload is the operator's own recall, and filtering it would
silently drop the thing being protected. `config` and `sessions` attach their
policies to the same hook when they arrive.

**D2 — a self-contained `memory` component.** `CORE_FILES["memory"]`:143 stages
`memory.db` + `memory_index.db`, which is where lessons actually live — 121
`lesson.*` rows in `semantic_memory` alongside 486 `project.*`, 23 `user.*` and 5
`pref.*` on the measured install, despite `learn.py:6` naming a
`<config_dir>/lessons.jsonl` that does not exist there. But the markdown half —
`workspace/memory/preferences.md`, `projects.md`, `history/` — reaches a bundle only
through the `workspace` component, which drags the entire workspace tree. Restoring
memory should not require restoring 62 MB of unrelated working files, so `memory`
becomes self-contained: the two databases plus `workspace/memory/`, and
`workspace/knowledge/` (the knowledge base) declared explicitly rather than
inherited.

**D3 — a `sessions` component.** Extend `VALID_COMPONENTS` with transcripts +
`sessions/archive/` + `uploads/` + `artifacts/`. The two-store split is an
implementation detail and must not surface: a component backs up a session
completely or does not claim to have backed it up. Its tree walk goes through the
existing `_data_filter`:49 (traversal, symlink and hardlink rejection, `0o600`
pinning) rather than reimplementing those properties.

**D4 — session fidelity is a tier.** Default `sessions` = crew transcripts +
uploads + artifacts, which buys readable history. Opt-in (O2) adds
`<kiro home>/sessions/cli/<sid>.*` for *reachable* sids only — the per-sid
reasoning `session_storage.cotenant_sids` already applies, explicitly not the whole
directory — which buys real resume instead of a lossy prefix. Measured: 1.17 GB
selected versus 30 GB unselected.

**D5 — the destination is provisioned once, by a human, and the backup path takes
no destination from its caller.**

```
kirocrew backup setup                                  # once
kirocrew snapshot --components memory --to-s3           # every run
```

`setup` **creates** the bucket via `engine.run_aws`, `create_private_bucket` and
`harden_bucket`, applies versioning and a noncurrent-expiration lifecycle rule, then
**reads the controls back from the API** and records `{bucket, region, account}` only
if AWS confirms them. Re-running repairs a bucket weakened out of band. The backup
path writes only to what setup recorded; no flag names a bucket.

A pre-existing bucket of that name is accepted only if it demonstrably carries **no
bucket policy**, and an unreadable policy fails closed. Hardening sets public-access,
ownership and encryption controls, none of which revoke a policy, so adopting a bucket
that already grants read elsewhere would publish the memory it is about to receive.
"Is there a policy" is decidable in one call; "is this policy safe" is the question
that does not close. That is what keeps `--bucket` from reopening the hole below, while
still admitting a bucket an earlier host created — which is how several machines share
one destination.

The lifecycle rule bounds the history of a *replaced* bundle, not the number of
bundles: keys are timestamped per run, so nothing becomes noncurrent in normal use.
See O5 for why expiring current bundles is refused rather than merely unimplemented.

The rejected alternative is worth recording, because it is the obvious one. Accepting
`--to s3://bucket/prefix` forces the write path to answer "is this bucket safe to put
memory in" on every run, unattended. That question does not close: a tag records who
*intended* a bucket for backup but not who can read it, so the policy must be parsed;
Block Public Access does not neutralise a CloudFront origin grant; an unreadable
policy has to fail closed; and a foreign account's IAM principal is structurally
identical to one of ours. Each is a real hole, and the shape of the design is what
generates them — a trust decision placed on an automated path has to be perfect
forever, whereas the same decision made once, with a human present, is merely
correct. So the destination is created rather than adopted, and every other bucket is
refused by construction.

One check survives on the write path, and **S3 enforces it, not this code**: every
upload carries `--expected-bucket-owner`, so a bucket that is no longer ours — deleted
and re-created by a third party under the same name — fails the write. Keys are
namespaced `backups/<hostid>/`, so several hosts share one bucket without
interleaving.

**D6 — `kirocrew restore s3://…/<object>`** downloads, then hands off to the
existing `restore_main` merge/replace. Bootstrap needs only the CLI, a profile, and
network. A downloaded object is untrusted input even from one's own bucket and gets
the same validation the import path already applies.

**D7 — schedule as a `command`/`script` cron**, which consumes no tokens. Backup is
deterministic; nothing for a model to decide. Failure notifies, success is silent
apart from the timestamp.

**D8 — manifest carries purpose, per-component counts and sizes** (plus the sid list
when replay logs ride along), so restore reports "replay logs absent for 12
sessions" instead of succeeding silently with less than the operator assumes.

## Milestones

Two PRs. Memory first because it is 22 MB against 385 MB, carries no `config.json`
and therefore no secret question, and proves upload plus restore before the heavy
part. Scheduling needs no phase of its own: a `command`-kind cron already runs a
shell command with no model in the loop, so once M1 ships a command, the schedule is
a registration, not code.

**M1 — memory is backed up off-host and comes back.** The policy seam, a
self-contained `memory` component, the S3 destination, and restore from it.

*Exit criteria*
* `kirocrew backup setup` creates a bucket that reports itself private, encrypted
  and versioned when read back, and `kirocrew snapshot --components memory --to-s3`
  produces an object listable with `aws s3 ls`.
* On a host with only the CLI and a profile, `kirocrew restore s3://…/<object>`
  reproduces every semantic key, episodic row, lesson, and the markdown memory
  files, asserted by test and by count — without requiring the `workspace`
  component.
* The created bucket reports all four Block Public Access flags, SSE and versioning
  from the API response, not assumed from the create call; a second run is idempotent
  and weakens nothing. Nothing is recorded as a destination unless that read-back
  passes.
* The backup path accepts no bucket from its caller, and refuses with an actionable
  message when `setup` has never run — while still leaving the local bundle intact,
  so an unconfigured destination costs no backup.
* Every object write carries `--expected-bucket-owner`, asserted by a mutation that
  removes it.
* The manifest carries `purpose` and each component's declared policy, and a
  component whose declaration is missing is refused at staging rather than
  defaulting to permissive.
* A truncated or tampered object is refused before any write into a data home.
* A denied AWS call yields an actionable IAM hint; no credential material appears in
  any object key, log line, or manifest.
* Mutations that fail a test: dropping `workspace/memory/` from staging; dropping the
  policy check; dropping the ownership assertion from a write; recording a
  destination that failed read-back; dropping versioning from the definition of
  private.

**M2 — sessions are backed up.** The `sessions` component riding M1's seam and
destination. Also resolves P2.

*Exit criteria*
* A restore reproduces session count and per-session message counts, asserted by
  test; uploads referenced by a restored transcript resolve; artifacts round-trip.
* A test that rotates before snapshotting proves `sessions/archive/` is included.
* A transcript copied while the gateway is appending to it either restores without a
  torn final record or the reader's tolerance for one is asserted by test —
  `memory.db` gets the SQLite backup API and a WAL checkpoint, an appended `.jsonl`
  gets neither.
* The P2 asymmetry is fixed or pinned by a test named in the spec.
* The upload's `run_aws` timeout is sized for the largest bundle this tier can
  produce on a slow link, not left at a default that turns a 1.5 GB transfer into a
  subprocess kill.
* A mutation dropping the transcript tree from staging fails a test.

Two pieces of work are deliberately **not** milestones, because neither is needed
for a host loss to be survivable and each is blocked on a decision: attaching
`config` to the seam (O1) and replay-log fidelity (O2). Both are follow-up issues.

## Backward compatibility

A new restore reads old bundles, reporting missing components rather than inferring
them. An old restore meeting a new bundle **refuses with a version message** rather
than extracting the subset it understands — silently dropping the conversations is
the failure this document exists to prevent. A snapshot that does not ask for an
off-host copy behaves exactly as today.

## Security considerations

* **The recorded destination is a trust anchor and must be agent-unwritable.**
  Because the backup path takes no bucket from its caller, the recorded file *is* the
  decision: an agent able to author it could redirect every future backup to a bucket
  it controls, and `--expected-bucket-owner` would then verify the attacker's
  ownership rather than refusing. Format validation does not help — a well-formed
  record naming a hostile bucket passes it. The file therefore belongs behind the same
  sensitive-path floor as the pointer deciding which checkout the gateway executes,
  which is the existing precedent for "a pointer whose writer controls where
  privileged work goes".
* **`sel_hmac.key` stays out.** `NEVER_SNAPSHOT_FILES`:46 excludes it so audit-log
  HMACs stay bound to the host that wrote them. A backup must not become the
  mechanism that clones a trust root.
* **A bundle's secret policy must be explicit per purpose, not implied by which
  filenames are on an allowlist.** Today both questions — what is in a bundle, and
  whether it is safe to hand to another person — are answered by the same
  filename-matching gate: `is_sensitive_path` (`portability.py:156`) matches
  credential *files*, and the config dataclass's own `sensitive=True` field metadata
  has no reader in either exporter. Whether the current gate delivers what
  `portability.py:7` claims is under review through the channel in `SECURITY.md`,
  not in this document. The requirement this RFC takes on is structural: the seam
  exists (D1), a `purpose: share` bundle's freedom from credential material is
  asserted by test rather than argued from the allowlist, and a component that fails
  to declare a policy is refused at staging rather than defaulting to permissive.
* **Backup and export want opposite things here.** A backup restoring onto a
  replacement host *wants* the credentials; an export shared with another person must
  not carry them. One bundle format serving both purposes cannot satisfy both, which
  is why the policy is a declared mode (O1, O4). The `.env` file stays out either way
  unless O1 says otherwise, in which case the minimum bar is a separate config key,
  off by default, an explicit per-invocation flag, SSE-KMS with a customer-managed
  key rather than SSE-S3, and refusal to upload when the bucket resolves as publicly
  accessible.


* **Transcripts are the most sensitive payload in the product** — everything the
  agent was ever shown, including content redacted at display time. Private bucket,
  ownership enforced, encrypted at rest, TLS in transit, no presigned URLs by
  default, never readable by CloudFront or any public policy.
* **Landing a backup in a publicly-served bucket would publish every transcript.**
  An earlier revision guarded this by inspecting the target bucket — by tag, then by
  policy — and that guard is what proved unclosable (D5). It is replaced by
  construction rather than by a better check: the only writable destination is one
  `backup setup` created and recorded, so a `deploy` site bucket is not refused after
  inspection, it is never a candidate. The backup bucket also never carries deploy's
  managed tag, so deploy's teardown and reaper cannot claim it.
* **Least privilege.** `PutObject`/`GetObject`/`ListBucket` on one prefix, plus
  bucket creation on first run. `deploy/iam.py` is not reusable as-is — it carries
  CloudFront permissions a backup has no business holding.
* **Restore writes into a data home** and is therefore owner-authority only.

## Alternatives considered

* **Extend the dashboard ZIP instead of the CLI tarball** — rejected as the primary
  path: goal 4 requires recovery without a dashboard. The duplication is real and
  worth collapsing, raised as O4 rather than assumed.
* **Session transfer to a second always-on instance** — needs a permanently
  available second host and carries neither memory nor config, so it cannot meet
  goal 1 even when both hosts are up.
* **External tooling (rsync, borg, a cloud-drive client)** — reuses no existing
  credential surface, has nowhere to report status, and in practice is not set up
  before it is needed. Nothing stops an operator also doing this.
* **Incremental or deduplicating transfer** — premature at under half a gigabyte;
  adds a consistency problem and a reassembly step to save bandwidth nobody lacks.

## Open questions

* **O1 — what is the bundle's secret policy, per purpose?** Blocks neither
  milestone — memory and sessions carry no `config.json`, which is why they ship
  first — and gates the follow-up that attaches `config` to the seam. The question is
  whether a backup keeps credentials (turnkey recovery, bucket becomes a credential
  store) while an export strips them (safe to share, restore names what to
  re-authenticate), and whether that is one bundle with a mode flag or two. The
  operator's risk decision, not the implementer's.
* **O2 — are replay logs in the default set?** Gates the fidelity follow-up. ~1.17 GB against
  385 MB, buying real resume over readable history.
* **O3 — reuse the deploy profile registry, or a `backup.profile` field?** Reuse
  means one AWS identity to manage; a separate field allows backing up under an
  identity that does not also publish public sites.
* **O4 — consolidate the two bundle formats?** They already disagree about what
  belongs in a bundle (P2 is one instance). Leaving both answers every future
  component question twice.
* **O5 — retention, partly answered.** Versioning plus a noncurrent-version
  expiration rule (both applied by `backup setup`) bound the history of a *replaced*
  bundle, which is what makes a corrupt or truncated re-upload recoverable. They do
  **not** bound how many bundles accumulate: each run writes a new timestamped key, so
  every bundle is a current version and none ever becomes noncurrent. An earlier
  revision of this document claimed O5 closed on that basis; it was wrong.
  What remains open is deliberately narrow, because automatic expiry of *current*
  bundles is refused rather than unimplemented: a lifecycle rule can express "delete
  older than N days" but never "keep the newest N", so on a host that stopped backing
  up — the dead machine this document exists for — such a rule would delete the last
  surviving copy of its memory exactly when it is needed. Unbounded growth is the
  cheaper failure (a few gigabytes a year at M1 sizes). The open question is therefore
  whether operators want an opt-in, count-based remote prune, which needs a lister and
  a deleter rather than a lifecycle rule.
