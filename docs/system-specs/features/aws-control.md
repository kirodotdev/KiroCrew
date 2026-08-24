# AWS Control — account portal and S3-backed cloud drive

Status: draft (delivered as ONE PR shipping the approved two-page design end
to end; Tasks and Sites remain ghost cards)

## 1. Problem

Kiro Crew already touches the user's AWS account in several disconnected places:
deploy-web publishes static sites to S3 + CloudFront, artifact-deploy ships app
stacks, voice features bill Polly/Transcribe behind per-service consent, and
agents run read-only AWS CLI calls. What is missing:

- No single place to see which accounts Kiro Crew can use, whether each one's
  credentials still work, what Kiro Crew has created in them, and what that
  costs this month.
- No durable cloud home for the gateway's own data. Artifacts, sessions,
  memory and workspace files live on one machine; there is no backup and no
  way to hand a file to someone who does not run Kiro Crew.
- Every new AWS capability re-invents account selection, consent,
  confirmation and cost display.

AWS Control is one builtin app that answers all three with a surface a
non-technical user can operate: an account portal, plus an S3-backed drive
that holds files, cloud copies of artifacts, and backups, with sharing.

S3 is the substrate because it is cheap enough to hold everything: ~27 GB of
cold sessions is roughly $0.62/month in Standard storage (about $0.11/month
in Glacier Instant Retrieval); uploads are free and downloads are $0.09/GB.

## 2. Product shape

The surface speaks outcomes, not services: Drive, Library, Backup, Sites,
Tasks, Bill, Access. AWS service names (S3, Lambda, IAM, CloudFront, ARN)
never appear at the top layer — they appear only inside the per-section
"under the hood" drawer (§2.4).

Visual direction is fixed by the approved two-page mockup (artifact
`aws-control-home-mockups` v2), which uses the dashboard's real design
tokens.

### 2.1 Page 1 — Accounts

A thin list-plus-summary page:

- One row/card per account: name, health light, account-id tail, a one-line
  summary (storage · sites · tasks · backup state), month-to-date cost.
- A cross-account totals strip: total spend, total storage (with cost
  estimate), sites, tasks.
- A degraded account shows exactly one repair action inline (Reconnect).
- `+ Connect account` starts the onboarding ceremony (§2.5).

### 2.2 Page 2 — Account Console

Opening an account shows one console:

- Header: account name, id, connection state, `Open AWS Console ↗`
  (federation URL), `Ask crew`.
- Overview stats: month-to-date cost + projection, storage used + cost
  estimate, sites, tasks; a Guard statement line (§3, invariant G1).
- **Library** — cloud copies of artifacts, aligned to artifact kinds
  (widget / markdown / html / json / webapp filter chips), each with version,
  timestamp, shared marker. Syncs with the local artifact library.
  ("Library" is the working name; final naming is an open question, §9.)
- **Drive** — general file area: folder/file tiles, upload/download, share.
  Deliberately no quota bar: S3 is not a subscription, so the display is
  "space used + estimated monthly cost", never "space remaining".
- **Backup** — session archives (cold storage) and nightly memory/workspace
  backup; shows last run, size, cost share, and a Restore entry.
- **Access** — who can see what, remaining validity, Revoke/Change.
  Posture line: "Nothing is public unless you say so."
- **Apps** (ghost placeholder cards) — Tasks (scheduled crawlers whose
  output lands in Drive) and Sites (publish from Library). Later phases
  plug into the same console pattern.
- A persistent Ask-crew input bar at the bottom.

### 2.3 Conversation is the operation

The UI is read-only by design; every mutation goes through the crew ("share
this folder with Wang, read-only, 7 days") or through an explicit dashboard
confirmation card. Consent cards speak plain language ("Kiro wants to create
a storage space for the drive, estimated under $1/month — Allow / Not now").
The approval flow is itself the UX, not a hurdle bolted onto it.

`Ask crew` opens a dedicated agent session seeded with AWS Control context —
the same launch pattern Issue Radar uses for Investigate (create a chat slot
in an app folder, seed the first prompt, persist the slot mapping so a second
click resumes instead of duplicating). It is not an embedded mini-chat.

### 2.4 Two-layer disclosure

Each console section carries a `</>` drawer showing what is really
underneath: the actual bucket name, the equivalent CLI command, the raw
policy. The top layer stays jargon-free; the drawer builds trust and serves
engineer users. Progressive disclosure is the premium feel, not hidden power.

### 2.5 One-button health and onboarding

Each account has one health light. A degraded light converges to exactly two
actions: **Reconnect** (one click where the profile type allows it) or
**Let Kiro look** (hands diagnosis to the crew). The user never needs to
learn what a credential is.

Reconnect degrades by profile type: SSO profiles can be re-authenticated
gateway-side (`aws sso login`, surfacing the device-flow URL/code in the UI);
`credential_process`-backed profiles get terminal guidance instead. The
gateway-side feasibility of each type is validated in P0 before the UI
promises it.

Connecting an account runs a short ceremony: the crew lays out the drive,
installs a spend alert, applies the guardrail policy, and ends with "your
cloud is ready" — zero-to-first-share in about three minutes.

### 2.6 Bill

Phone-bill mental model: this month, projected, versus last month, and one
slider — "tell me when it goes over $X". The crew flags anomalous growth
proactively. Costs come from the Cost Explorer API (~$0.01/query, ~24 h
latency) cached daily per account; the budget guard is computed locally.
No AWS Budgets resources are created — that would be another account-level
resource requiring permissions that a non-technical user never needs to
know exists.

## 3. Security invariants

These carry over from the deploy subsystem and the AWS consent feature, and
any PR touching them must update this section in the same commit.

- **G1 — Guard (human confirmation for billable resources).** Any operation
  that creates a new billable resource (create storage, deploy a task,
  publish a site) requires an explicit human confirmation in the dashboard.
  Pure reads are consent-gated (G3) but confirmation-free. Implemented on
  the deploy two-phase pattern: a preview call echoes the resources and
  costs, a pending record is stored, and only an owner-authenticated
  dashboard confirm executes. Agent/MCP callers get `confirm` stripped
  server-side — an agent can request, never execute.
- **G2 — Names-only credentials.** The account registry stores profile
  names, regions and display metadata only. Credential material is never
  read, stored, or written; profile writes go through the existing allowlist
  (`region`, `credential_process`) in `deploy/profiles.py`. Credential
  resolution happens inside the external `aws` CLI process.
- **G3 — Per-service usage consent.** Every paid AWS service the app touches
  (`s3`, `ce`, later `lambda`/`events`/`logs`) is gated by the existing
  aws-usage-consent mechanism: a grant per (service, profile, region)
  recording the confirmed account id, stored in the keystone-fenced
  `aws_service_consent.json` leaf, re-verified against a live
  `sts:GetCallerIdentity` probe on every authorization, revoked on account
  drift. Fails closed.
- **G4 — Single CLI chokepoint, gateway-side.** All AWS calls go through
  `deploy/engine.run_aws` (AWS CLI subprocess, `--profile`, fixed argv, OS
  sandbox) on the gateway. Agents interact only with the app API; no boto3,
  no SDK, no agent-side credential access.
- **G5 — Private by default.** The drive bucket is created private: Block
  Public Access on, SSE enabled, ACLs disabled, versioning on. Nothing under
  it is reachable externally except through an explicit share (§4.5), and
  every share has an expiry or an explicit revocation path.
- **G6 — Audited mutations.** Every mutating endpoint is owner-gated,
  refused for restricted sessions, and SEL-audited, matching the deploy
  handler discipline.

## 4. Architecture

### 4.1 App shell

`aws-control` is a builtin app (added to `BUILTIN_NAMES`) using the
in-process route pattern (`backend.routes:register_routes` dispatched through
`RouteRegistry`), because it needs gateway-side access to the profile
registry, consent store, and engine — the same reasons Issue Radar is
in-process. Frontend lives at `website/src/apps/aws-control/` with a
`builtinRegistry.ts` entry and nav coming from the manifest's `ui.pages`.

### 4.2 Account center

`deploy/profiles.py` stays the registry engine (single flock-guarded
`profiles.json`, unchanged format, deploy-web keeps working). A new
`aws_control` backend module aggregates registry entries **by account id**:
an account owns one or more profiles ("keys"), a default region, health
state, and cached cost/storage summaries. Profiles demote to a detail-page
concept; the account is the top-level object. Health = the existing
read-only identity probe; per-account state caches under the app data dir.

### 4.3 Storage engine — one bucket, three prefixes

Per account, one private bucket `kirocrew-drive-<12hex>` (opaque name),
discovered stateless-by-tag (`kirocrew:managed=true` +
`kirocrew:drive=<drive-id>`) exactly like deploy-web's `find_site_by_tag`.
Bootstrap reuses `create_private_bucket`/`_harden_bucket` plus **versioning
enabled** — a deliberate delta from deploy-web (which keeps versioning off
for teardown safety); teardown therefore needs a version-aware purge.
Artifact versions map naturally onto S3 object versions.

Three prefixes, one engine, three views: `artifacts/` (Library), `drive/`
(Drive), `backup/` (Backup). New primitives built on `run_aws`:
`put_object`, `get_object`, `list_prefix`, `delete_object`, `presign`
(bounded expiry), and a daily storage-usage read. deploy-web's `sync_dir`
is reused for directory pushes.

### 4.4 Costs

A new `ce get-cost-and-usage` wrapper (month granularity, grouped by
service), consent-gated as service `ce`, cached 24 h per account. The
existing `pricing.py` (unit prices) stays for what-if estimates like "this
drive costs about $0.62/month". Budget thresholds and anomaly detection are
computed locally from the cache.

### 4.5 Sharing — three compilation targets

A share is metadata compiled to one of three mechanisms, in escalating
scope: presigned URL (time-boxed, anyone with the link) → bucket policy
grant (a specific AWS account) → public CDN via the existing deploy-web
path. The P4 access model (owner/group/other × read/write) compiles onto
these three targets; the product never promises real POSIX ACLs. Creating
or widening a share is access-granting and is human-confirmed (G1 applies);
revocation is one click and never gated.

### 4.6 Consent extension

`GATED_SERVICES` grows from Polly/Transcribe to `s3` and `ce` (P0), later
`lambda`/`events`/`logs` (P3). The mechanism is unchanged; per-service
`(profile, region)` resolution extends `_effective_target`, and the app
mounts the existing `AwsConsentGate` component per account. IAM guardrails
remain user-applied (Option A: the app renders policy JSON to paste, and
extends the existing boundary-policy document for drive/tasks scopes; the
gateway itself never writes IAM).

### 4.7 Library sync

Cloud copies of artifacts record publication state on the artifact record
(provider, version map, content hash), following the existing artifact
publication metadata shape and its no-force-push conflict discipline.
Whether this registers through the `PublishProvider` seam or a parallel
record is a P1 implementation decision.

### 4.8 Backup

Session archive plugs into the session-storage inventory as a third action
("Archive to cloud") next to trash/restore, honoring the "both halves move
together" invariant (transcript + CLI replay log). Memory/workspace nightly
backup reuses the snapshot component map (`memory`, `config`, `skills`,
`workspace`, …) pushed to `backup/` — optionally on a Glacier IR lifecycle
rule for cold pricing.

## 5. API surface (P1 shape)

Routes under `/api/apps/aws-control/`:

- `GET  /accounts` — aggregated account list with health + summaries
- `GET  /accounts/{id}` — console payload (stats, sections)
- `POST /accounts/{id}/reconnect` — SSO re-auth (or guidance payload)
- `GET  /accounts/{id}/costs` — cached CE summary
- `GET  /drive/{account}/list?prefix=` — object listing
- `POST /drive/{account}/put|get|delete` — object I/O (G1/G3/G6 as applicable)
- `POST /drive/{account}/bootstrap` — bucket creation (two-phase, G1)
- `POST /share` / `DELETE /share/{id}` — share create (two-phase) / revoke
- `POST /library/{account}/push|pull` — artifact sync
- `GET  /pending` / `POST /pending/{id}/confirm|dismiss` — the Guard queue

Agent access goes through the internal-secret path with `confirm` stripped
(G1); read endpoints require an existing consent grant (G3).

## 6. Delivery — one PR

The whole two-page design ships in a single PR (Closes #5496), in this
internal build order:

1. Foundations: app shell + nav entry; account aggregation over the existing
   registry; Accounts page with health lights and reconnect guidance;
   consent extension (`s3`, `ce`).
2. Storage engine: object put/get/list/delete + presign primitives on
   `run_aws`; bucket bootstrap (private + SSE + versioning, tag-discovered)
   behind the two-phase confirmation flow.
3. Console: overview stats; Bill (CE wrapper + daily cache); Library
   (artifact push/pull + shared marker); Drive (browse/upload/download/
   delete); share via presigned link with a local share ledger; Access
   section listing live shares with time remaining.
4. Backup: memory/workspace snapshot push to `backup/` (manual now + nightly
   cron), session archive (both halves together), listing + restore.

Shipped share semantics (honest subset of §4.5): presigned links only, expiry
capped at 7 days (the SigV4 ceiling). The Access section shows each live
share with its countdown; a presigned link cannot be revoked before expiry,
so the UI says "expires in N days" and never offers a fake Revoke. Recipient-
account grants (bucket policy) and the owner/group/other model compile onto
the same ledger later.

Out of this PR (future work, sections stay visible as ghost cards where the
mockup shows them): Tasks (scheduled crawlers), Sites consolidation,
recipient-account and public share tiers, spend-threshold notifications.

## 7. Non-goals

- Not a general AWS console replacement; the app manages only what Kiro Crew
  created (tagged resources) plus account-level health and cost readouts.
- No boto3/SDK dependency; no credential storage or agent-side credential
  access, ever.
- No real POSIX ACLs; the access model is metadata compiled to §4.5 targets.
- No AWS Budgets or other hidden account-level resources for the bill guard.
- No quota/storage-cap mental model in the UI.

## 8. Failure modes

- Consent absent or account drifted → all engine calls refuse (G3 fails
  closed) and the console shows the consent card, not an error wall.
- Identity probe fails → health light degrades; every section header shows
  the single Reconnect action; cached summaries render greyed with their
  age labelled.
- CE query fails → last cached bill with age label; never blocks the page.
- Bucket tag lookup ambiguous (two tagged buckets) → refuse and surface,
  matching deploy-web's ambiguity discipline.
- Presign/share on a missing object → 404 mapped to a plain-language card;
  share records are reconciled against bucket state on console load.

## 9. Open questions

1. Final section naming: Library vs Vault vs Archive ("Artifactory" is
   excluded — trademark and internal-service collision).
2. `credential_process` resolution feasibility on the gateway host (P0
   test decides how far Reconnect can be automated per profile type).
3. Library sync vehicle: `PublishProvider` seam vs parallel publication
   record (P1 decision).
