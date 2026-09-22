---
name: deploy-web
description: Publish a Kiro Crew artifact (HTML / markdown / widget) to a public HTTPS URL on the user's own AWS account (private S3 + CloudFront + OAC). Use when the user says "publish this publicly", "deploy this artifact to the web", "make this a public URL", "deploy-web", or asks to set up / recall / destroy a deploy-web site. Kiro Crew never stores credentials — only an AWS profile name.
---

# deploy-web — publish artifacts to your own AWS

deploy-web is a **core Artifact Deploy feature**, not an installable built-in app. The
deploy/recall/destroy mechanics run as deterministic Python in `src/kiro_crew/deploy/`
(which shells to the `aws` CLI with `--profile`). This legacy-named skill is the
**chat-native preview front door**. You never read, store, or manage credentials, and you
never perform an IAM write — the `/deploy` console generates policy text the user applies.

> **UI entry point:** publishing is initiated from an **artifact's page** (Publish →
> "Publish to public web (your AWS)"), backed by `POST /api/deploy/deploy`. The core
> **Artifact Deploy console** at `/deploy` owns profile setup, health checks, pending
> confirmations, and Manage Sites (Recall/Destroy); it has no static-artifact publish form.

## Hard rules (never violate)
- **Never** run `aws configure`, `aws sso login`, `ada`, or any credential-establishing
  command on the user's behalf. Tell them to run it themselves.
- **Never** create/attach/modify IAM roles or policies. Generate the JSON; the user applies it.
- **Never** auto-approve deploy / recall / destroy. Each is per-invocation confirmed.
- Publishing makes content **world-readable**. Always state this before deploying.

## Backend endpoints (dashboard/core contract)

Agents use the MCP `deploy_artifact` tool for previews; they do not self-confirm by
posting these routes. Profile management, pending confirmation, Recall, and Destroy belong
in the `/deploy` console.

- `GET/PUT /api/deploy/config` — legacy default-profile view; GET also returns
  `cloudDeploymentEnabled`
- `GET/POST /api/deploy/profiles`; `PUT/DELETE /api/deploy/profiles/{name}` —
  multi-profile registry control plane
- `GET /api/deploy/iam-policy[?tier=static|fullstack]` — generated policy JSON
- `GET /api/deploy/pricing?profile=<name>` — live/fallback estimate rates
- `POST /api/deploy/verify` `{profile}` — read-only reachability, not full verification
- `POST /api/deploy/deploy` `{site_id, artifact_slug|local_dir, profile?, ttl_hours?,
  confirm?, override_scan?}` — direct dashboard two-step flow
- `POST /api/deploy/recall` / `destroy` — two-step withdrawal flows
- `GET /api/deploy/list` — live site list
- `POST /api/deploy/teardown/{slug}` — webapp tombstone/reaper handoff
- `GET /api/deploy/pending`; `POST /api/deploy/pending/{id}/confirm|dismiss` —
  human handling of MCP-generated previews

## Guided installation (one-time, ~10–15 min — runs once, reused forever)

### Step 1 — AWS access (user-run; console-managed)
Send the user to `/deploy`. They run `aws configure sso` or configure a named profile
on the gateway host themselves, then register its name and region in the Profiles control
plane. The console may write only the allowlisted `region` and `credential_process` AWS
config keys; it never writes credential values. Click **Verify access**, which calls
`POST /api/deploy/verify` with the registered profile.
- If not reachable: tell them exactly what to run (`aws sso login --profile <name>` for
  expired SSO; install AWS CLI v2 if missing) and re-verify. Do **not** run it for them.
- On success, report the resolved account + that access is **reachable** (not "verified").

### Step 2 — Permissions (console generates; user applies)
The console calls `GET /api/deploy/iam-policy`, shows the JSON, and tells the user to
apply it themselves to a dedicated role/identity. For fullstack it also emits the required
`kirocrew-deploy-app-boundary` policy. You do **not** apply IAM. A second read-only Verify
check still means "access reachable, not fully verified — first deploy is the real test."

### Step 3 — Done
Confirm the core registry contains the profile name + region only (plus display-only
account/verification metadata). Offer to preview the first static artifact.

## Deploy flow (MCP preview, human execution)

1. For `widget`, `html`, or `markdown`, call `deploy_artifact` with `site_id` and
   `artifact_slug`. For a built static directory, use `local_dir`; a `webapp` artifact's
   text is only a summary and is rejected as `artifact_slug`.
2. The tool calls the preview path **without** `confirm` or `override_scan`. It never
   creates infrastructure. A clean preview or overridable non-credential scan finding is
   stored under **Pending confirmations** on `/deploy`.
3. State that the URL will be world-readable with no authentication, then direct the human
   to review and confirm there. Credential findings are a hard block; only the dashboard
   can explicitly override non-credential findings. Never self-confirm.
4. If confirmation returns `AccessDenied`, surface `missing_statement`; the user updates
   their generated policy and retries. Deploys are idempotent.
5. A new distribution returns `status: "InProgress"` and may need up to ~15 minutes to
   become reachable. Watch **Deployments** on `/deploy`; re-deploys reuse the site and
   normally go live in seconds.

For an app with a backend, this skill is not the fullstack path: the operator must use the
`artifact-deploy` skill's `scripts/deploy-app.sh`, which places static and API resources
behind the same shared distribution.

## Recall vs Destroy
Run both from the `/deploy` console; the dashboard performs the preview + explicit
confirmation and binds the confirmed call to the previewed resource ids.

- **Recall** = fast unpublish (empties objects + invalidates; URL → 404; infra stays;
  reversible). Caveat: edge caches may serve briefly; already-downloaded content can't be recalled.
- **Destroy** = full teardown (disable → wait → delete distribution, OAC, bucket). Irreversible.
