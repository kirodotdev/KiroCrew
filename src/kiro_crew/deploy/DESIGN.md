# deploy-web — KiroCrew App Design Doc (v1)

**Status:** ✅ Implemented (v1) — built-in app at `kiro_crew/apps/builtins/deploy_web/`
(engine/render/scan/iam/handlers) + UI `KiroCrewWebsite/src/apps/deploy-web/DeployWebPage.tsx`,
disabled/opt-in by default. 54 backend tests pass; flake8/isort/mypy clean; tsc + vite build clean.
🔄 **In progress (2026-06-14): Route B refactor** — publishing unified behind a removable
publish-provider registry on the artifact page; app page slimming to a pure management
console (see §1.1–§1.3). Follow-ups: IAM policy not yet run through Access Analyzer (§9.5);
chat-native skill ships but its auto-registration via discovery `skills` propagation is pending.
**Source research:** `/local/home/zejiangg/.kirocrew/workspace/research/5d1b29a9/FINDINGS.md`

---

## 1. Goal

Extend KiroCrew's scope — including **public usage** — by collaborating with AWS,
**without KiroCrew ever managing AWS accounts or credentials**, to **safely deploy
artifacts as a publicly-available web page** served from the **user's own AWS account**.

### Audience
**Anyone with their own AWS account — Amazon-internal *and* external alike.** "Bring
your own AWS" is the deliberate bar. The *user-owns-their-own-cloud* model (KiroCrew
never hosts, never holds liability) applies identically to internal and external AWS
users, so AWS S3 + CloudFront is the right fit for the whole segment. No-cloud / casual
users (who'd want GitHub Pages / Vercel / Netlify push-simplicity) are a **different,
out-of-scope audience** — a possible future *separate* app, not a gap here.

### Core idea — set up once, reuse forever
1. **One-time AI-guided setup** (§12): the agent walks the user through AWS *access* —
   credentials reachable (profile) + the scoped IAM policy they apply + region. Done
   **once ever**, reused for everything after. KiroCrew stores only the profile name.
2. **Per publish, reuse that setup:** a new artifact → new site (fresh bucket +
   distribution + URL) reuses the same credentials/IAM/code path — ~30 sec of user time.
   Re-publishing the **same** site is **idempotent** (find by tag → `s3 sync` changed
   files → invalidate): **same bucket, same distribution, same public URL**, so a shared
   link stays stable while content updates.

   > Net value prop: pay the AWS setup cost **once** (with AI smoothing it), then every
   > future publish is **near-zero-effort**.

### Packaging — built-in app, ships with KiroCrew
deploy-web is a **built-in app that ships *with* KiroCrew** (like Research Lab / Team
Manager), **not** a separately-installed App Store download. It is still a self-contained
*app module* (its own page + skill, per §1.1) — registered in the
KiroCrew package and **disabled / opt-in by default** so users without AWS never see AWS
surface. Shipping built-in means zero install friction; the opt-in gate + the §6.1
credential non-goals keep the *credential/account* review surface unchanged (the AWS
*resource* code lives isolated in this module, gated off until enabled).

**Flagship use case:** publish an existing KiroCrew artifact — **HTML, markdown file,
or interactive widget** (these three are the primary target) — to a real **public
URL**. Today `artifact_publish` shares only internally (Amazon Artifactory);
deploy-web makes the artifact publicly live via the user's own S3 + CloudFront.

> **Design note (publish-interface generalization — ADOPTED via the provider registry,
> see §1.1):** the original `artifact_publish` was tightly bound to Artifactory. Route B
> now **generalizes the publish interface** so the destination is pluggable: every
> destination (Artifactory, AWS, future apps) is a **registered provider** the artifact
> page renders generically. The markdown/widget render-to-static step still lives **inside
> deploy-web**, not in core (§1.1) — core stays destination-agnostic and AWS-code-free.

### 1.1 Publish paths & app boundary — DECISION: Route B (unified provider registry)

> **Reversal (2026-06-14):** the original v1 locked **Route A** (strict separation —
> public publishing only inside the deploy-web page, core artifact page untouched). We
> are now **deliberately reversing to Route B**. What changed: the AWS logic is already
> fully isolated inside the deploy-web builtin module, so exposing a publish entry point
> on the core artifact page can be done with **only a thin UI affordance + a fetch to the
> app's existing endpoint** — **no** AWS SDK, IAM, or credential code moves into core.
> The §6.1 credential boundary is therefore untouched, which removes the original reason
> Route A was chosen (review-scope caution).

**The artifact page is the single publish surface; every destination is a registered
provider.** All publishing — the existing internal Artifactory visibility options
(PRIVATE / SHARED / PUBLIC) **and** the new public-web (AWS) destination — flows through
one **publish-provider registry**. The artifact page renders publish actions purely from
that registry; it has no hardcoded knowledge of any specific destination.

| | Artifactory provider (built-in) | deploy-web provider (app) |
|---|---|---|
| Destination | Amazon **Artifactory** | User's **own AWS** (S3 + CloudFront) |
| Audience | **Internal** (Amazon employees) | **Public** internet |
| Origin of registration | Core built-in registration list | App `app.json` manifest (`publishProvider`) |
| Availability | Present in internal builds only | When the app is enabled + configured |
| Renderer | Existing visibility/sharing dialog (unchanged) | Generic confirm-gate + scan-gate dialog |

**Route B (chosen):**
- **One publish surface.** The core artifact page's publish affordance is driven entirely
  by the **provider registry**. Each provider supplies its own renderer (dialog/flow); the
  page just lists the available providers and opens the selected provider's renderer.
- **All destinations are providers, including Artifactory.** The existing
  PRIVATE/SHARED/PUBLIC visibility options become the **Artifactory provider**, a
  built-in registry entry that wraps the **existing visibility/sharing dialog with zero
  behavioral change**. It is no longer special-cased inline.
- **deploy-web is a registered app provider.** Its renderer is a generic confirm-gate +
  scan-gate dialog that calls the app's existing `POST /api/apps/deploy-web/deploy`
  endpoint. Shown only when the app is **enabled + configured**; otherwise the artifact
  page shows a "Set up Web Deploy →" link to the app page.
- **No core AWS code.** Core still contains **zero** AWS/IAM/credential logic — it only
  fetches the registry (`GET /api/publish-providers`) and calls the provider's declared
  endpoint. All AWS code stays confined to the deploy-web module (§6.1 boundary intact).
- **Render-to-static stays in deploy-web.** The artifact `kind` → static render
  (md → HTML, widget → iframe HTML body) remains inside deploy-web, applied to the
  artifact content the app reads — not a core publish change.

**Accepted nuance (the real trade-off):** core (the artifact page) now **conditionally
depends on the provider registry**, and app providers couple to a stable
route/endpoint contract. This is a mild, declared coupling — clean because the UI gates
on availability and degrades to a "set it up" link. It is the boundary Route A
deliberately avoided; we now accept it because the AWS code stays in the module and the
registry keeps core destination-agnostic.

### 1.2 External-fork removability (why Route B is *better* for the public build)

The registry is the **single abstraction for all publish destinations**, which makes the
external-facing build (KiroCrewExternal, the public Python fork) clean:

- Artifactory is Amazon-internal only and does **not** exist outside Amazon.
- Because Artifactory is now just a **built-in registry entry** (one registration in a
  single list), the external fork removes **all** internal publishing by **dropping that
  one entry** — no scattered conditionals across the artifact page.
- After removal, the artifact page still works: the registry simply has fewer providers
  (e.g. only deploy-web, or none → a "set it up" prompt). No dead Artifactory UI, no
  inert internal-only buttons to hide.

> Net: Route B both improves UX (publish from where artifacts live) **and** makes the
> internal/external split a one-line registration difference instead of a fork-wide diff.

### 1.3 Publish-provider contract

A publish provider is described by:

| Field | Meaning |
|---|---|
| `id` | Stable provider id (e.g. `artifactory`, `deploy-web-aws`). |
| `label` | Human-readable action label (e.g. "Share on Artifactory", "Publish to public web (your AWS)"). |
| `icon` | Icon name for the action. |
| `origin` | `builtin` (core registration list) or `app` (declared in an app `app.json` manifest). |
| `availability` / configured-check | When the action is offered. Built-in Artifactory: present in internal builds. App providers: app **enabled + configured** (resolved via the app's own config endpoint). |
| `renderer` | The per-provider dialog/flow. Artifactory → the existing visibility/sharing dialog (unchanged). App providers → a generic confirm-gate + scan-gate dialog that posts to `endpoint`. |
| `endpoint` (app providers) | The app backend route the renderer calls (e.g. `/api/apps/deploy-web/deploy`). |

**Two registration sources, one registry:**
1. **Built-in providers** — registered in a single core list (e.g. a
   `BUILTIN_PUBLISH_PROVIDERS` constant). Artifactory lives here. The external fork
   removes internal publishing by dropping this entry.
2. **App providers** — declared via a `publishProvider` field in the app's `app.json`
   manifest; core aggregates the **enabled + configured** ones through
   `GET /api/publish-providers` (no app imports, no AWS code in core).

The artifact page merges both sources, filters to available providers, and renders one
action per provider. When none are available it degrades to a setup prompt.

---

## 2. Verdict (from research)

**YES — build a `deploy-web` built-in KiroCrew app** (ships with KiroCrew, opt-in/disabled
by default) that publishes static sites to the user's own AWS account via a thin
`aws`-CLI workflow.
The "never manage credentials" constraint is fully satisfiable — it is already
KiroCrew's code-verified posture.

### One-picture flow
> User authenticates **once** (`aws configure sso`) → KiroCrew stores only the
> **profile name** → `deploy-web --artifact <dir>` runs a deterministic 6-step flow
> on the user's credentials → returns a live HTTPS URL. KiroCrew never sees or stores a key.

---

## 3. Architecture — private S3 + CloudFront + OAC

The AWS-recommended **secure** static-hosting pattern:

- **Private S3 bucket** — Block Public Access ON, Object Ownership = *Bucket owner
  enforced*, ACLs disabled, **no** S3 website endpoint.
- **CloudFront** distribution as the only reader, gated by Origin Access Control (OAC).
- Bucket policy `Condition AWS:SourceArn = <distributionArn>` → objects reachable
  **only through** CloudFront, never via direct S3 URLs.
- **HTTPS enforced** — viewer protocol policy = *Redirect HTTP→HTTPS*; OAC
  `SigningBehavior: always` keeps CloudFront→S3 over HTTPS.
- **OAC, not legacy OAI.** The public-bucket S3 website endpoint is **rejected**
  (HTTP-only, insecure).
- Optional custom domain = ACM cert in **us-east-1** + Route 53 alias.

### Why low-risk to build
deploy-web mostly **extends code KiroCrew already ships**:
- `sync/s3.py` already shells to `aws` with `--profile` only (never reads keys) and
  already creates private, encrypted, public-access-blocked buckets.
- Ships as a **built-in app** (own page + skill, registered in the package);
  `hooks.py` is the approval gate.
- The **only genuinely new logic** is the CloudFront OAC + distribution + invalidation block.

---

## 4. Per-deploy flow (6 deterministic steps)

Logical flow (entry points in v1 are the **Web Deploy page** + **chat skill**, §8 — the
line below is illustrative, not a committed standalone CLI):

`deploy-web <artifact> [--domain www.example.com] [--profile P]`

| # | Step | `aws` command | IAM action(s) |
|---|------|---------------|---------------------|
| 0 | Preflight: confirm creds resolve | `sts get-caller-identity` | `sts:GetCallerIdentity` |
| 1 | Create **private** bucket (reuse `_create_bucket`) | `s3api create-bucket` + BPA + encryption + ownership | `s3:CreateBucket, PutBucketPublicAccessBlock, PutEncryptionConfiguration, PutBucketOwnershipControls` |
| 2 | Create OAC | `cloudfront create-origin-access-control` | `cloudfront:CreateOriginAccessControl` |
| 3 | Create distribution (REST origin + OAC, redirect-to-https, default-root=index.html) | `cloudfront create-distribution` | `cloudfront:CreateDistribution` |
| 4 | **Now** write OAC bucket policy pinning `AWS:SourceArn=<distArn>` | `s3api put-bucket-policy` | `s3:PutBucketPolicy` |
| 5 | Upload assets | `s3 sync <dir> s3://bucket` | `s3:PutObject, ListBucket, DeleteObject` |
| 6 | Invalidate edge cache | `cloudfront create-invalidation --paths "/*"` | `cloudfront:CreateInvalidation` |
| 7 | Return URL | (read distribution domain) | `cloudfront:GetDistribution` |

**Ordering gotcha:** step 4 (`put-bucket-policy`) MUST follow step 3
(`create-distribution`) — the policy condition needs the distribution ARN, which
doesn't exist until the distribution is created.

### 4.1 Pre-upload pipeline (before step 5)

Two deterministic steps run in the Python module **before** assets are uploaded:

**a) `render_standalone(artifact)` (Q1)** — artifacts aren't all web-ready:
- **widget** → wrap the inner `<mcwidget>` HTML in a self-contained `<html>` shell with
  a **fixed light/default theme** CSS inlined + `<meta viewport>`. (v1 ships one fixed
  light theme — no picker/parity; widgets that rely on runtime dashboard APIs won't work
  standalone.)
- **markdown** → render md → HTML with a minimal built-in stylesheet.
- **html** → pass through (already a full doc).
- Output is written as **`index.html`** (matches the distribution default-root) so the
  bare URL resolves. Uploaded with `text/html` content-type (s3 sync sets this by
  extension) so browsers render rather than download.

**b) Pre-publish content scan (Q4)** — deploying makes content world-readable, so before
upload the module runs the rendered output through KiroCrew's **existing
`redaction.py`/`security.py` secret regexes** + internal-data heuristics (Amazon aliases,
`*.internal-corp` hosts, AWS account ids/ARNs). On a match: **block-and-warn** — show what
was flagged and where, require an explicit "publish anyway"; **never silently redact**.
Always show a content summary in the approval. Best-effort detection, not a guarantee.

---

## 5. State model — stateless-by-tag (no local cache)

- **AWS account is the source of truth.** KiroCrew holds **no** local record — **no
  SQLite cache in v1** (Q8). `list-sites` queries the Resource Groups Tagging API live
  every time (`tag:GetResources Key=kirocrew:managed`), so there is zero drift.
- **Identity vs naming (Q2):**
  - **Logical site id** = the artifact slug (or user-supplied name) → drives the
    `kirocrew:site=<id>` tag and the UI label. This is what makes re-deploy idempotent.
  - **Physical bucket name** = `kirocrew-web-<random ~12hex>` — opaque, **no account
    id** (S3 names are globally unique per partition; entropy avoids collision; with
    CloudFront+OAC the bucket name never appears in the public URL). On first deploy,
    generate a random name and retry on a 409 name-clash; on re-deploy, **resolve the
    existing bucket by tag** (never re-derive the name).
- Mandatory tags on every resource: `kirocrew:managed=true` + `kirocrew:site=<id>`,
  applied **at creation** (`create-distribution-with-tags`). Tags do double duty:
  (1) stateless discovery/teardown; (2) IAM `aws:ResourceTag` least-privilege confinement.
- **Live status, not stored:** "Deploying / Live" derives from the CloudFront
  distribution `Status` field (`InProgress` / `Deployed`) read live — consistent with
  no-cache. Don't add a status table.
- **Scope (v1):** one **self-contained** artifact per site (the rendered HTML doc,
  uploaded as `index.html`). Multi-asset bundles (external/relative CSS/JS/images) are
  **out of scope** — the standalone render (§ render_standalone, Q1) inlines what it needs.

### Recall vs Destroy — two-tier takedown (Q5)
- **Recall (fast unpublish):** empty the bucket objects + invalidate `/*`. The URL goes
  to 404 in seconds-to-minutes; the bucket + distribution **remain**, so it's
  **reversible** by re-deploying. This is the emergency takedown (NOT disable-distribution,
  which takes 5–15 min). Caveat shown in UI: recall stops *future* serving only — edge
  caches may serve until invalidation completes, and already-downloaded content can't be
  recalled.
- **Destroy (full teardown):** resolve bucket + distributionId by tag →
  CloudFront **disable-before-delete** (get-config → `Enabled=false` →
  update-distribution → **wait** until `Deployed` → delete-distribution) →
  delete-origin-access-control → empty + delete bucket. Slower; removes all resources/cost.
- **Partial-failure → reconcile, not auto-rollback:** because every resource is tagged,
  a re-run completes a partial deploy and Destroy always finds orphans by tag.

---

## 6. Credentials — code-verified safe

- KiroCrew **never persists AWS credentials** — every key reference in source is
  defensive (redaction + symlink-block); no write path exists.
- Profile-name-only invocation; credential resolution delegated to the AWS CLI
  default provider chain (auto-refreshing SSO tokens).
- Onboarding reuses `sync/api.py` availability-check + `install_hint` pattern;
  swap the internal `ada` hint for `aws configure sso` for public users.

### 6.1 Explicit non-goals (review-surface minimization)
To keep this feature **out of credential- and account-management scope** (and avoid
triggering a heavy security review), the design commits to these hard NON-goals — none
of the following is ever done by KiroCrew:

- ❌ Reading, parsing, storing, caching, logging, or transmitting AWS access keys,
  secret keys, session tokens, or `~/.aws/credentials` / `~/.aws/config` contents.
- ❌ Creating, importing, rotating, or refreshing credentials of any kind.
- ❌ Creating or managing **AWS accounts**, IAM **users**, IAM **roles**, or IAM
  **policies** (no IAM write — guided install only *shows* policy text the user applies).
- ❌ Running `aws configure`, `aws sso login`, `ada`, or any credential-establishing
  command on the user's behalf — these remain **user-run, outside KiroCrew**.
- ❌ Assuming roles, minting STS tokens, or brokering cross-account access.

What KiroCrew **does**, and the entire trust surface, is narrow:
- ✅ Store a single opaque string: the **profile name** (no secret material).
- ✅ Pass `--profile <name>` to the **local** AWS CLI and let the OS-resident provider
  chain resolve credentials entirely outside KiroCrew's process boundary.
- ✅ Make scoped, tagged, least-privilege resource calls (S3 + CloudFront) the user
  has already authorized via their own applied IAM policy.

This makes the credential/account boundary identical to KiroCrew's existing,
already-reviewed `sync/s3.py` posture — deploy-web introduces **no new credential
handling** to review, only new (non-credential) resource API calls.

---

## 7. IAM — least-privilege customer-managed policy

Two scoping levers: an **S3 name prefix** (`kirocrew-web-*`) and a **resource tag**
(`aws:ResourceTag/kirocrew:managed=true`) confining CloudFront mutate/delete actions.
Distribution must be tagged **at creation** (`create-distribution-with-tags`).
Attach to a **dedicated assumable role** (short-lived creds), not a standing admin
identity. Full paste-ready JSON in research cycle 006 / FINDINGS.md.

> One un-run check: the IAM policy was not validated through IAM Access Analyzer
> (no creds in the research environment).

---

## 8. Build shape — built-in app (ships with KiroCrew)

**Mechanism:** deploy-web is **not** an MCP server and does **not** define a new
`deploy_web` LLM tool (the App SDK has no "app publishes a tool" primitive;
`permissions.mcpTools` is only an allowlist of *existing* tools). Because it ships
**built-in**, it can include a backend Python module (like `auto_research/handlers.py` /
`code_reviewer/db.py`). The deploy logic is therefore **deterministic Python**, not the
LLM free-handing commands:

- **Backend: a Python builtin module** — runs the §4 6-step deploy flow + recall/destroy
  by shelling to the **`aws` CLI as a subprocess with `--profile`** (the exact
  `sync/s3.py` pattern; reuses `_create_bucket`). **Not boto3** — the CLI keeps
  credential resolution entirely outside KiroCrew's process, preserving the §6.1
  boundary. Deterministic, fast, no per-deploy LLM tokens.
- **`ui.pages`** → a "Web Deploy" page that calls the module's **backend endpoints
  directly** (deploy / recall / destroy / list). No LLM in the deploy mechanics.
- **`skills/deploy-web/SKILL.md`** — thin, only to enable the **chat-native** path
  ("deploy this artifact publicly") by routing to the same backend; documents the
  recipe for the agent. The deterministic flow lives in Python, not the skill.
- **No per-app update cron.** As a **built-in** app, deploy-web is versioned and updated
  with the KiroCrew package itself (`kirocrew update`) — there is no independent upstream
  to poll, so it ships **no** update-check cron (the App-Store self-update pattern doesn't
  apply to built-ins; re-add a *script/command* cron only if it is ever repackaged as an
  externally-installed app).
- **`setup.configSchema`**: `{profile, region}` — **profile name only**, no keys.
- **`dependencies.commands:["aws"]`** with `managedBy:"app"` → check existence + install
  hint, never auto-install or manage.
- **Built-in packaging:** ships inside the KiroCrew package (registered like Research
  Lab / Team Manager builtins), **disabled / opt-in by default**; os `macos`/`linux`.

**Destructive ops:** recall/destroy (`s3 rm`, `delete-bucket`, `delete-distribution`)
run **inside the module's controlled code path** with an explicit UI/approval confirm —
not as LLM-issued shell commands subject to core's denylist. Each is per-invocation
confirmed, never auto-approved, never in heartbeat/cron safe sets (§9.3).

---

## 9. Security

Security is a first-class concern for this app — it creates **public internet
resources** on the user's **own AWS account** and spends real money, so the bar is
higher than for a normal local widget.

### 9.1 Credential safety (never manage keys)
- KiroCrew **never reads, stores, or persists** AWS credentials — verified across the
  source (§6). Every key reference is defensive (redaction + symlink-block); no write
  path exists.
- Only the **profile name** is persisted in app config. Credential resolution is fully
  delegated to the AWS CLI default provider chain (auto-refreshing SSO tokens).
- The guided installer (§12) writes only the profile name; it performs no IAM writes.

### 9.2 Least-privilege IAM
- Scoped customer-managed policy (§7): S3 confined to the `kirocrew-web-*` prefix;
  CloudFront mutate/delete confined by the `kirocrew:managed=true` resource tag.
- Attached to a **dedicated assumable role**, not a standing admin identity.
- The blast radius of a compromised/confused invocation is bounded to deploy-web's own
  tagged resources — it cannot touch the rest of the account.

### 9.3 Tool-approval model
- `deploy_web` / `destroy_web` default to **per-invocation approval** (`TOOL_ALLOW`) —
  never auto-approved, even under broad user auto-approve globs.
- **Hard-excluded from heartbeat/cron safe-tool sets** — a background or scheduled
  session can never silently provision or tear down public infrastructure.
- The approval prompt **states the public nature** of the URL being created
  (a world-readable site), per KiroCrew's security-awareness norm.
- `destroy_web` is treated as **destructive**: it echoes the tag-resolved resources
  (bucket + distribution) it will delete, and confirms.
- Guided install (§12) performs **no IAM writes** — it only generates policy text for
  the user to apply and verifies read-only, so there is no agent-driven permission
  mutation to approve.

### 9.4 Content & exposure awareness
- Deploying makes the artifact **publicly readable by anyone with the URL** — the
  default access posture is public-by-obscurity (§11b). The UI and approval prompt
  must make this explicit so a user never publishes something private by accident.
- Real access control (signed URLs, edge basic-auth, `s3 presign`) is a deliberate v2
  decision, not an implicit default.

### 9.5 Residual risk
- One un-run check: the cycle-006 IAM policy was **not validated through IAM Access
  Analyzer** (no creds in the research environment) — validate before shipping.

---

## 10. Cost

### 10.1 The numbers
A small static site is effectively **$0/month**:
- **CloudFront free tier:** 1 TB data-transfer-out + 10M HTTP/HTTPS requests per month.
- **S3 → CloudFront origin transfer:** free (no origin-fetch charge).
- **Invalidations:** `/*` = 1 path; first 1,000/month free.
- **S3 storage:** ~$0.023/GB-month → a few-MB site is fractions of a cent.
- **Only recurring non-free item:** a custom-domain Route 53 hosted zone ($0.50/mo) —
  and only if the user opts into a custom domain. Default to the free `*.cloudfront.net`
  URL, which has zero DNS cost.

### 10.2 How cost is served to the user (display rule, Q6)
The "My Sites" list shows a per-site cost figure. deploy-web has **no billing
permissions** (`ce:`/`cloudwatch:` are deliberately *not* in the IAM policy, to keep
§6.1 scope minimal), so it cannot read real spend. Therefore:

- Cost is **always an estimate**, computed locally from the site's object bytes ×
  published S3/CloudFront rates, and **always labelled `~estimated`** in the UI
  (e.g. `~$0.00/mo (estimated)`). No "accurate vs estimated" branching; never blank.
- Surface the free-tier caveat, framed "**estimated, assuming free-tier headroom**":
  allowances are per-account/shared, so a spike past 1 TB / 10M req incurs standard
  charges.

> **v1 implementation note:** v1 ships a **flat free-tier estimate** — the UI shows a
> static `~$0.00/mo (estimated)` (a small static site is genuinely ~$0 under the
> CloudFront free tier). Per-byte computation (size × published rates) is a deliberate
> deferral; the `~estimated` label keeps it honest. Wire per-site size→cost later if real
> variance matters.

---

## 11. Early design Q&A (setup / URL / UX)

> Note: these three (11a–11c) are the *early* design questions. The numbered **Q1–Q9**
> referenced elsewhere (§4.1, §5, §10.2, §12, §13) are the later **design-grill**
> decisions — a separate set.

### 11a — Setup time & reusability
- **One-time AWS prep (~10–15 min, once ever):** `aws configure sso` (~3 min) +
  create the IAM role/policy (~5–10 min). KiroCrew stores only the profile name.
- **Per-deploy (~30 sec of user time, then 5–15 min unattended):** pick artifact →
  Deploy → approve. CloudFront propagation 5–15 min (AWS-side; app polls, doesn't block).
- **Reusable:** the framework is reused directly for every deploy. Re-deploying the
  same site is idempotent (`s3 sync` changed files + invalidate, ~1–2 min, no new
  infra/propagation). New sites reuse the same code path + credentials.

### 11b — URL appearance & access control
- **Default (zero config, free):** `https://d111111abcdef8.cloudfront.net/` — random
  CloudFront domain, HTTPS out-of-the-box, no DNS.
- **Optional custom domain (v2, $0.50/mo):** `https://demo.yourdomain.com/` —
  ACM cert (us-east-1) + Route 53 alias.
- **Access-control nuance (IMPORTANT — unresolved):** private-bucket + OAC makes the
  *bucket* private but CloudFront still serves content **publicly** — anyone with the
  URL can read it. It is NOT viewer-side access control. Tiers if real control is wanted:
  - **Public-by-obscurity (v1 default):** unguessable random URL — fine for "share with a few".
  - **CloudFront signed URLs / signed cookies:** time-limited, key-pair-signed links.
  - **Basic auth via CloudFront Function:** edge function checks `Authorization` header.
  - **`s3 presign`:** expiring signed S3 URL for a single file (no CloudFront) — simplest
    private share (research companion use case #3).
  - **Recommendation:** v1 = public-by-obscurity; treat real access control as a
    deliberate v2 feature (worth a grill — "public artifact" vs "access-controlled
    artifact" are different products).

### 11c — App UX (Route B — pure management console)

Under Route B (§1.1) the deploy-web app page is a **pure management console** — it does
**not** contain a publish form. Publishing happens from the **artifact page** via the
provider registry. The app page has three responsibilities:

1. **Guided setup** — AWS profile + region selector with a live credential-validity check
   (green/red, reusing the `sync/api.py` availability pattern); the Getting Started guide;
   **Get IAM policy** (generate scoped JSON for the user to apply themselves).
2. **Health check** — read-only reachability verify (§12 / Q3), labelled "access
   reachable, not fully verified".
3. **Manage sites** — the stateless-by-tag "My Sites" inventory rendered live: live URL
   with Copy/Open, status badge (Live / Deploying+progress / Error), `~estimated` cost,
   **Recall** (fast unpublish) and **Destroy** (full teardown, echoes exact resources,
   confirmed even under broad auto-approve).

**Publish entry points (Route B):**
- **Artifact page** — the primary surface. The registry renders a "Publish to public web
  (your AWS)" action when deploy-web is enabled + configured; otherwise a
  "Set up Web Deploy →" link to the console page. The action runs the confirm-gate +
  scan-gate flow against `POST /api/apps/deploy-web/deploy`.
- **Chat-native path:** saying "deploy this artifact publicly" in chat triggers the same
  deploy-web **skill workflow** in kirocrew-core (no custom tool involved) — same backend
  endpoint, same gates.

The app page's former Deploy button + source picker are **removed** — publishing is no
longer initiated from the app.

---

## 12. Guided Installation (AI-guided onboarding)

The one-time AWS prep (§11a) — the IAM role + policy + SSO config — is the real
friction point. It is also exactly the kind of fiddly, failure-prone setup an **agent**
is good at: KiroCrew already has a live agent in the loop, so "installation" doesn't
have to be a static form — it can be a **conversation that verifies itself**.

### Why agent-guided beats a static wizard
AWS setup fails in many subtle ways (expired SSO token, wrong account, missing
permission, typo'd ARN, no CLI). A static wizard can collect inputs but cannot
**diagnose**. The agent closes the loop by making a real `aws` call after each step,
reading the result, and adapting — that verification-and-diagnosis is the differentiator.

### 3-step conversational flow (each step gated on a real verification call)

**Step 1 — AWS access**
- Check the `aws` CLI is present and a profile resolves: `aws sts get-caller-identity
  --profile P`.
- Diagnose on failure: no CLI → install hint; expired SSO → "run `aws sso login
  --profile P`"; wrong account → report the resolved account and ask.
- ✓ on success: shows resolved account + region.

**Step 2 — Permissions**
- Generate the cycle-006 scoped IAM policy JSON (S3 + CloudFront only, confined to
  `kirocrew-web-*` + `kirocrew:managed` tag) and show it to the user.
- The **user applies it themselves** (console or their own `aws iam` command) and
  attaches it to their assumable role. KiroCrew does **not** create, attach, or modify
  any IAM role/policy — it never performs an IAM write. Options shown are
  **"I'll apply it myself"** (paste-the-JSON, the only apply path) / **"Explain it"**.
- **Verify (read-only reachability only, Q3):** after the user applies it, KiroCrew runs
  a **read-only reachability** check — `sts:GetCallerIdentity` + a harmless
  `cloudfront:ListDistributions` / `s3:ListAllMyBuckets` — labelled "**access
  reachable**," **not** "fully verified." It is *not possible* to verify create/write
  perms without writing (CloudFront has no `--dry-run`), and `iam:SimulatePrincipalPolicy`
  is deliberately **not** used (extra scope). The **first deploy is the real permission
  test**: on `AccessDenied`, the module maps the failing action → the exact missing IAM
  statement, tells the user what to add, and they re-run (idempotent).

**Step 3 — Wire up**
- Write the **profile name + region** into the app's `setup.configSchema`.
- Never writes credential material.
- ✓ Ready → "Want to publish your first artifact now?"

```
🐾 Let's get deploy-web connected to your AWS account. I'll never
   see or store your credentials — only a profile name.

Step 1/3 — AWS access
   ✓ Found profile 'my-sso' → account 1234..., us-west-2.
Step 2/3 — Permissions
   [shows scoped IAM JSON]  [I'll apply it myself | Explain]
   (You apply it — KiroCrew never edits your IAM.)
   Verifying (read-only reachability)... ✓ Access reachable (full check on first deploy).
Step 3/3 — Done
   ✓ Saved profile 'my-sso' + region us-west-2. Ready.
```

### Design constraints
- **Safety boundary intact.** The agent guides, *generates* policy text, and
  *read-only verifies* — it never reads/stores keys and **never performs an IAM write**.
  The only thing it persists is the profile name. The user applies the IAM policy
  themselves; KiroCrew never creates/attaches/modifies roles or policies.
- **No account management.** KiroCrew does not create AWS accounts, IAM users/roles,
  or credentials, and does not rotate or transmit them. All account/IAM mutation is the
  user's own action, outside KiroCrew.

### Reusable App SDK primitive
This is **not** deploy-web-specific. Generalize it into the App SDK as
`setup.guided: true` with a **per-step verification script** the agent runs and
diagnoses. Any app needing external credentials (connectors, the AWS bridge, future
apps) reuses the same guided-onboarding machinery — arguably a bigger win than
deploy-web alone. deploy-web is the first consumer.

---

## 13. v1 cut line & locked decisions

**In v1:**
- Deploy **HTML / md / widget** (via `render_standalone`, §4.1 / Q1) → private S3 +
  CloudFront + OAC → default `*.cloudfront.net` URL.
- Random opaque bucket name + tag-based identity (Q2); single config region (Q7); pure
  stateless-by-tag, **no cache** (Q8).
- **Recall** (fast unpublish) + **Destroy** (full teardown) (Q5), as scoped
  per-invocation-confirmed module operations (§8).
- Pre-publish **block-and-warn** content scan (§4.1 / Q4); per-invocation approval; not
  in heartbeat/cron safe sets (§9.3).
- Guided install: read-only **reachability** check + first-deploy `AccessDenied` mapping
  (Q3); **user applies the IAM policy themselves** (Option A, §12 / Route B §1.1).
- Cost always shown **`~estimated`** (Q6).
- Built as a **Python builtin module** (aws CLI subprocess, not boto3) + UI page +
  thin chat skill (§8) — no per-app update cron (built-in; updated with KiroCrew).
- **Route B publish surface (§1.1):** all publishing flows through a **provider
  registry** on the artifact page. Artifactory (PRIVATE/SHARED/PUBLIC) is a built-in
  provider wrapping the existing dialog unchanged; deploy-web is an app provider
  (enabled + configured gated). The deploy-web app page is a **pure console**
  (setup / health / manage sites) — no publish form. External fork drops internal
  publishing by removing the single Artifactory registration entry (§1.2).

**Deferred to v2+:** custom domains (ACM us-east-1 + Route 53); viewer access control
(signed URLs / edge basic-auth / `s3 presign`); local SQLite cache; Lambda-backed
dynamic sites; scheduled republish; widget theme parity; multi-asset bundles.

**Decisions index (from the design grill):** Q1 render_standalone (§4.1) · Q2 bucket
naming + identity (§5) · Q3 read-only reachability verify (§12) · Q4 pre-publish scan
(§4.1) · Q5 Recall+Destroy (§5) · Q6 estimate-only cost (§10.2) · Q7 single config region (§8) ·
Q8 no cache (§5) · Q9 v1 cut (this section). Build model + Option A IAM (§8/§12).

**Confidence:** High on architecture, credentials, IAM, reuse, and build model
(doc + code-verified). Moderate on the synthesized UX/packaging specifics (sound, not
yet prototyped). One un-run check: cycle-006 IAM policy not validated through IAM Access
Analyzer (§9.5).
