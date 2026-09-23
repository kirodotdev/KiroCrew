---
name: artifact-deploy
description: One-click deploy a user's pre-built app/artifact into their OWN AWS account and get a global public HTTPS link (Vercel-like), with a default TTL and promote-to-persistent. Use when the user says "deploy this", "ship this demo", "give me a public link", "share this externally", or "deploy to AWS".
triggers: deploy, ship, publish demo, public link, deploy to aws, share externally, one-click deploy, vercel
---

# Kiro Crew One-Click Deploy (MVP)

> **Shipped by Kiro Crew core** (`src/kiro_crew/deploy/`), not by an installable
> app. Gateway startup copies this skill into `~/.kiro/crew/skills/artifact-deploy/`.
> The `/deploy` console owns AWS profile setup, verification, pending confirmations,
> and the fleet/cost view; this skill prepares and previews deployments.

## AWS config -- resolve the profile from the core registry, don't ask

The Artifact Deploy core module owns the AWS configuration as a **multi-profile
registry** at `~/.kiro/crew/deploy/profiles.json`
(`{"profiles": [{"name", "region", ...}], "default": "<name>"}`). Resolve the
deploy profile in this order, **before asking the user anything**:

1. **User picked one**: if the deploy request names a profile (the artifact
   card's dropdown injects `Use the AWS profile "<name>".`), use that entry's
   `name`/`region` from the registry. If the name is not in the registry, stop
   and send the user to the `/deploy` console to register it -- never deploy
   with an unregistered profile name.
2. **Registry default**: otherwise use the entry named by `default`.
3. **Legacy fallback**: if the core registry doesn't exist, read the old
   `~/.kiro/crew/apps/deploy-web/data/profiles.json`, then its sibling
   `config.json` (`{"profile", "region"}`).
4. **Unconfigured** (no registry, no legacy profile): do NOT walk the user
   through manual profile setup in chat -- link them to the `/deploy` console
   (Artifacts -> Artifact Deploy), which has the Profiles control plane (register /
   create + Verify access + IAM policy generator). Resume once a profile exists.

After a successful deploy, back-fill `webapp_metadata.deploy_target.profile`
with the profile NAME actually used, so the fleet table and the artifact's
control card show which identity owns the deployment.

This replaces the old "pick the AWS profile" step: profiles are managed once in
the console, then every deploy (static publish or fullstack webapp) reuses them.
Optionally confirm reachability via the core verify endpoint
(`POST /api/deploy/verify` with `{"profile": "<name>"}` -- an STS
read, no credential access).

Deploy a **pre-built** static site (and later, an app with a backend) into the
**user's own AWS account**, served globally over HTTPS via CloudFront.

## Where this fits — app artifacts come first

An **app-style artifact** (`kind="webapp"`) is generated **first**, as the primary
object: every generated app is saved as an artifact up front (initially *not
deployed*). **Deploy is an optional downstream action on that existing
artifact**, offered two ways — a **skill** (in-conversation) and a **Deploy
button** on the artifact card. This skill *is* that deploy action; it does NOT
create the artifact. Deploying **fills in** the artifact's `webapp_metadata`
(deploy target, architecture, cost, TTL, teardown) and flips the card from its
not-deployed state (Deploy button) to the deployed control card.

**Producer — register at generation time.** When you generate a deployable app,
create the artifact immediately (before any deploy) via
`artifact_save(name, content=<one-line human summary>, kind="webapp", webapp_metadata=…)`.
For a not-yet-deployed app: fill `architecture` (intended tiers) + `cost`
(projected from the model), set `lifecycle.status="draft"`, and leave
`deploy_target.public_url` empty — the card then shows the Deploy button.
Filling `cost.estimates` is REQUIRED (empty estimates render a blank cost area
on the card; `artifact_save` warns when you skip it). For the unit prices, GET
`/api/deploy/pricing?profile=<name>` on the gateway — it returns live AWS
Pricing API rates for the profile's region (`source: "live"`) or the fallback
table when the API is unreachable; multiply into per-bucket what-if totals
(e.g. 1,000 / 100,000 / 1,000,000 views). (The
MCP `artifact_save` tool accepts `kind="webapp"` + `webapp_metadata`.) Deploy — via this
skill or the card's Deploy button — fills in the rest.

## Model — two static deployment paths

- **Operator-script app path — cheap AND instant:** one shared base stack per
  account (`kirocrew-deploy-base`): a private S3 bucket + a global CloudFront
  distribution. Created once (~5-15 min for the first CloudFront propagation —
  the only slow step on this path). Each deploy is just an S3 upload under a
  `/<slug>/` prefix + a CloudFront invalidation → **seconds**. No per-deploy
  stack, no per-deploy cold-create. Backend scripts attach `/<slug>/api/*` to
  that same distribution.
- **Artifact publish / MCP preview path:** `POST /api/deploy/deploy` uses the core
  Python engine and creates per-site `kirocrew-web-*` S3 + CloudFront + OAC
  infrastructure. It does not wrap `scripts/deploy.sh` and must not be mixed with
  `deploy-backend.sh`, which targets the shared stack.
- Cost lives in the **user's** account and is scale-to-zero (S3 storage +
  CloudFront requests). Idle ≈ $0. AWS earns the consumption; Kiro Crew pays
  nothing.

## Invocation — how this skill is summoned (and where isolation comes from)

Every deploy runs in its **own isolated, agent-debuggable context** — deploys are
long (CloudFront cold-create), fail in ways that need iterative fixing (IAM,
boto3 `Decimal`, framework quirks), and are context-heavy. There are two entry
points; the isolation source differs:

- **The "Deploy" button (preferred — solves discoverability).** Most users won't
  know this skill exists. After a user has an app, a **Deploy** button (on the
  app / app-artifact card / dashboard) **opens a fresh session pre-loaded with
  this skill** and a seed prompt. That new session **IS** the isolated context —
  run the whole adapt→deploy→debug flow **inline** in it. **Do NOT spawn a
  subagent here** (you are already in a dedicated session; debug errors directly
  in-session).
- **In-conversation ("deploy my app" mid-session).** The user asks inside an
  existing, busy session. Here you **MUST run the deploy via a `spawn_run`
  subagent** — the subagent is the isolation boundary, so the long/noisy deploy
  and its debugging don't pollute or blow the current session's context.

Rule of thumb: **one isolated context per deploy — a fresh session (button) OR a
subagent (in-conversation). Never run a deploy inline in a busy existing
session.**

> The button is a *launcher*, not an executor: it does not run the deploy itself
> (a button can't debug a failed CloudFormation/IAM step) — it summons a
> skill-loaded session where the agent can. That is why deploy is a skill, not a
> button-triggered script.

## The deploy contract (target shape)

The deploy scripts consume ONE fixed layout. The skill's real job is to get the
user's app **into** this shape (see Adapter playbook), then ship it:

```
<app>/public/   static SPA — index.html at root  → S3 + CloudFront
<app>/api/      an HTTP backend                   → API Gateway → Lambda at /<slug>/api/*
                (index.py handler, OR any HTTP server wrapped in a Lambda shim —
                 see playbook; pick language via --runtime)
state           DynamoDB single table (--table), read via os.environ["TABLE_NAME"]
```

Static-only apps need just `public/`. No auth in MVP (content is public).

## Adapter playbook — wrap, don't rewrite

A user's app almost never arrives in the contract shape. Conform it — but
**prefer wrapping the app's existing HTTP server in a Lambda adapter over
hand-rewriting its routes** (hand-rewriting is usually a language port + a
data-model rewrite = fragile). Wrapping keeps the user's code and adds a thin
shim:

- **ASGI (FastAPI / Django / Starlette)** → **Mangum** adapter, `--runtime python*`.
- **Express / Node / Next.js standalone** → **serverless-http** or **AWS Lambda
  Web Adapter**, `--runtime nodejs*`.
- **Static SPA** → point the build output at `public/`.
- Don't force Python — `deploy-backend.sh --runtime` already supports the app's
  own language.

This broadens "our format" from *one Python handler* to **static assets + any
HTTP server + DynamoDB**, so the adapter mostly *places dirs + adds a shim*,
not *rewrites business logic*.

### Tiers — what adapts, and where to fail loud

- **Tier 1 — mechanical (reliable):** static SPA + a simple JSON API. Place dirs,
  align the handler entry, done.
- **Tier 2 — wrap (agent-doable, must test):** a framework backend in a supported
  runtime + stateless or KV-mappable state → wrap in a shim + DynamoDB. Deploy,
  then exercise the real endpoints (incl. the base64 body path) before handoff.
- **Tier 3 — needs redesign (fail loud, don't fake it):** SSR needing a live
  server beyond a single Lambda, websockets, background workers/cron, or a
  **relational schema with joins/transactions**. **Detect these and tell the user
  they're unsupported / need a redesign — never silently ship a broken deploy.**

### Database strategy (a real boundary, not laziness)

- Stateless or KV-mappable → **DynamoDB** (`--table`, `TABLE_NAME`).
- **SQL / relational → out of tier.** The "$0-idle, cents-per-demo" cost story
  depends on scale-to-zero DynamoDB. SQL means RDS/Aurora → floor cost + VPC +
  not scale-to-zero, which breaks the cheap/ephemeral promise. So map state to
  DynamoDB, or tell the user this app doesn't fit the cheap-ephemeral tier (offer
  a redesign). Do **not** silently mangle a relational schema into KV.

## Prerequisites

- **POSIX platform (Linux / macOS)** — deploy scripts require `bash`.
  Windows is not supported; use WSL (Windows Subsystem for Linux) to run the
  Kiro Crew gateway if your host OS is Windows. The backend returns HTTP 400
  with a clear message on unsupported platforms.
- AWS access configured **once in the Artifact Deploy console** (profiles registered
  in `~/.kiro/crew/deploy/profiles.json`, verified at `/deploy`).
  Prefer a **least-privilege deploy profile**, not admin (see Security).
- The app is conformed to the **deploy contract** (above) — producing that
  layout is the skill's job (Greenfield: generate in-contract; Brownfield: run
  the Adapter playbook). The deploy *scripts* consume a built `public/` (+
  optional `api/`); they don't build or adapt for you, so the agent produces
  that layout first. Static apps need an `index.html` at the static root.

## Commands (operator-terminal reference)

These are run from this skill's directory by **human operators in a terminal**,
not by agents. They implement the separate shared-stack app path; the core
`POST /api/deploy/deploy` endpoint does not wrap them. All accept `--profile`
and `--region` (default `us-west-2`).

- **Deploy (static)**: `scripts/deploy.sh <app_dir> [--slug NAME] [--ttl HOURS] [--profile P] [--region R]`
- **Deploy (fullstack)**: `scripts/deploy-app.sh <app_dir> --slug NAME [--table] [--runtime R] [--handler H] [--wait] [--profile P] [--region R]`
- **Deploy (backend only)**: `scripts/deploy-backend.sh <handler_dir> --slug NAME [--table] [--runtime R] [--handler H] [--wait] [--profile P] [--region R]`
- **Install reaper**: `scripts/install-reaper.sh [--rate 'rate(1 hour)'] [--alarm-email EMAIL] [--profile P] [--region R]`
- **Teardown**: `scripts/teardown.sh <slug> [--profile P] [--region R]`
- **Lifecycle**: `scripts/list.sh`, `scripts/cost.sh [slug]`, `scripts/persist.sh <slug>`, `scripts/detach_backend.py --slug NAME`

## Backend handler contract

Copy `templates/handler-example.py` as your app's `api/index.py` starting point.
Behind an API Gateway HTTP API (payload v2):
- Route on the path after `/api/`: `event["rawPath"].split("/api/",1)[1]`.
- **Decode the body via `isBase64Encoded`** — API Gateway base64-encodes the
  request body when Content-Type isn't a known text type (or is absent, e.g.
  `curl -d` without `-H application/json`). Calling `json.loads(event["body"])`
  directly will 500 for those callers. `base64.b64decode` first when
  `event.get("isBase64Encoded")`. (Browsers sending `application/json` are
  unaffected — a browser-only test won't catch this.)
- Return `{"statusCode", "headers", "body": json.dumps(...)}`.
- Stateful apps: table name in `os.environ["TABLE_NAME"]` (deploy with `--table`).

## Agent workflow — preview through the audited MCP tool

First honor **Invocation** (above): summoned mid-session → run the steps below
inside a `spawn_run` subagent; launched in a fresh Deploy-button session → run
them inline.

When the user asks to deploy / ship / share a demo:
0. **Conform the app to the contract** (Adapter playbook) — greenfield: generate
   in-contract; brownfield: wrap the existing server in a Lambda shim. Fail loud
   on Tier-3 apps rather than shipping something broken.
1. Confirm the resulting app dir has `public/` (an `index.html` at its static
   root) and, if there's a backend, `api/`.
2. Resolve the AWS profile from the **core registry** (see "AWS config" above):
   user-picked > registry default > legacy config; if unconfigured, send the
   user to the Artifact Deploy page (`/deploy`) for the one-time setup. Then
   **confirm which account** (verify endpoint returns the account id) -- this
   provisions REAL resources that cost money. Never guess a prod account; if
   unsure, ask.
3. **Scan the app for internal tokens** first (see Security) — this content is
   going to the public internet. Credential findings are a hard stop.
4. Choose one coherent deployment path:
   - **Static artifact or static-only app:** call the MCP `deploy_artifact` tool
     once. Use `artifact_slug` only for `widget`, `html`, or `markdown`; a
     `webapp` artifact is a summary, so use `local_dir` pointing at its built
     `public/` directory.
   - **App with `api/`:** prepare the complete layout, then give the human
     operator the exact `scripts/deploy-app.sh <app_dir> --slug <slug> ...`
     command. That script deploys the static and backend pieces behind the same
     shared distribution. Do not preview only `public/` and then claim that
     `deploy-backend.sh` will attach to that per-site distribution.
5. `deploy_artifact` is **preview-only**. It never accepts `confirm` or
   `override_scan`; it stores a pending entry and returns instructions for the
   human to confirm on the Artifact Deploy page. Relay that the link is public
   with no authentication. A human may override only non-credential findings in
   the dashboard; credential findings cannot be overridden. The API path behind
   the tool provides: schema validation, fail-closed scan gate, confirm gate,
   SEL audit trail, and `_deny_restricted` session guard. **Do NOT bypass it by
   calling deploy scripts directly.**
6. Return the public URL + the TTL.
   **Important ordering**: after the human confirms and the deploy succeeds,
   **immediately back-fill the artifact's `webapp_metadata`** (`public_url`,
   `lifecycle.status`, `deploy_target`) before performing endpoint verification
   (HTTP GET on the deployed URL). The endpoint check can timeout (~30s+) or be
   killed by a session budget wall — if the metadata write happens after it, a
   timeout leaves the artifact in a stale "draft" state even though the deploy
   succeeded. Metadata first, verify second.
7. Offer tear down / promote-to-persistent.

### MCP `deploy_artifact` tool (preview-only in MCP-tool-capable sessions)

Agents with MCP tool access use the `deploy_artifact` tool to get a deploy
preview (scan results, size, and TTL). The tool is **preview-only** — it never
confirms or executes the deploy. Human confirmation happens exclusively in the
dashboard UI (Artifact Deploy page), where the user clicks the confirm button.

This design prevents an LLM caller from self-confirming a public deployment.

Parameters:

- `site_id` (required): deploy slot name
- `artifact_slug`: slug of a static `widget`, `html`, or `markdown` artifact;
  `kind="webapp"` is rejected because its content is only an app summary
- `local_dir`: validated path to a static directory (fullstack `public/` root)
- `profile`: AWS profile override (default: registry default)
- `ttl_hours`: hours until auto-cleanup (default: 72)

Exactly one of `artifact_slug` or `local_dir` is required. The tool returns
a preview summary; to execute the deploy, the user must confirm via the
Artifact Deploy page in the dashboard.

### Operator-terminal commands (NOT for agent use)

The following scripts are for **human operators running in a terminal**. They
implement the shared-stack app path independently of the core API. Agents do NOT
call them directly; unlike the API, the scripts do not provide the dashboard's
audit, preview, or human-confirmation boundary.

- `scripts/teardown.sh <slug>` — destructive; agent-blocked by design
- `scripts/install-reaper.sh` — one-time account setup; operator-only
- `scripts/deploy.sh` / `scripts/deploy-app.sh` / `scripts/deploy-backend.sh` —
  the raw deploy scripts; operators may use for debugging or manual deploys
- `scripts/list.sh` / `scripts/cost.sh` / `scripts/persist.sh` /
  `scripts/detach_backend.py` — lifecycle management utilities

## Security

- **Credentials hard rule** — the agent NEVER executes credential writes and
  NEVER reads credentials files. "Configure a profile" means: *generate* the
  commands (`aws configure --profile X` / `ada profile add ...`) for the USER to
  run in their terminal, then verify with
  `aws sts get-caller-identity --profile X` (a read). After a successful deploy,
  fill `webapp_metadata.deploy_target.profile` with the profile NAME (display
  only — never a credential value).
- **Least privilege** — never run with admin credentials if a scoped deploy profile
  exists. Use the generated policy for the selected tier: static needs the managed
  S3/CloudFront/CloudFormation surface; fullstack additionally needs its scoped
  Lambda, API Gateway, DynamoDB, and IAM role lifecycle permissions.
- **Internal-data leak** — before deploying, scan the app dir for internal
  tokens (internal hostnames, corp identifiers) and
  refuse on a hit. This is public.
- **Private by default** — the bucket stays **private** (CloudFront OAC only);
  CloudFront adds security headers (nosniff / HSTS / `frame-ancestors 'self'` +
  loopback so the dashboard can live-preview the site) and enforces
  TLS 1.2+ via `redirect-to-https`.
- **No auth** — MVP serves static content publicly with no auth. If the app
  expects a protected backend, warn the user.

## Status / Roadmap

- **M1 static** — DONE, live-validated (S3 + CloudFront OAC + security headers).
- **M2 backend** — DONE via **API Gateway HTTP API → Lambda** behind CloudFront
  `/<slug>/api/*` (`app-apigw.yaml` + `deploy-backend.sh`). Live-validated:
  `/<slug>/` and `/<slug>/api/` both 200 on one public link.
  Chosen over a raw Lambda Function URL because a Function URL requires a
  `Principal:"*"` resource policy, making the Lambda itself world-accessible. With
  API Gateway the Lambda's resource policy is scoped to `apigateway.amazonaws.com`
  only — the function is never directly reachable from the internet, and all public
  traffic enters through the managed API Gateway front door. This is also
  compatible with managed corporate accounts whose guardrails flag world-accessible
  Function URLs. `app-lambda.yaml` (Function URL + CloudFront OAC) is kept as the
  lighter variant for unrestricted accounts.
- **M3 lifecycle** — DONE: `list.sh` / `persist.sh` / `detach_backend.py`, plus the
  **in-account scheduled reaper** (`install-reaper.sh` → EventBridge-timed Lambda;
  `templates/reaper.yaml` + `scripts/reaper_lambda/`) that deletes expired
  non-persistent deploys (S3 prefix + CloudFront behavior + backend stack) via a
  role scoped to `kirocrew-deploy-app-*`. Runs in-account with no local creds —
  the reliable mechanism. Local `reaper.sh` is a dev-only fallback.
- **M4 cost** — DONE: `cost.sh` live usage estimate (per-slug S3 exact + shared
  CloudFront account-wide; honest "estimate not bill" labeling).
- **Core/card integration** — DONE: the deploy engine lives in
  `src/kiro_crew/deploy/`; the `deploy-web-aws` provider is emitted by core; and
  the app card has a not-deployed state, profile picker, and Deploy button that
  launches a skill-loaded session. After a confirmed agent-driven deploy, the
  agent still back-fills the artifact's URL, profile, lifecycle, TTL, and teardown
  metadata before endpoint verification.
