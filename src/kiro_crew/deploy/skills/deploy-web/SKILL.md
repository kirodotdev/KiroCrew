---
name: deploy-web
description: Publish a Kiro Crew artifact (HTML / markdown / widget) to a public HTTPS URL on the user's own AWS account (private S3 + CloudFront + OAC). Use when the user says "publish this publicly", "deploy this artifact to the web", "make this a public URL", "deploy-web", or asks to set up / recall / destroy a deploy-web site. Kiro Crew never stores credentials — only an AWS profile name.
---

# deploy-web — publish artifacts to your own AWS

deploy-web is a **core Artifact Deploy feature**, not an installable built-in app. The
deploy/recall/destroy mechanics run as **deterministic Python** in `src/kiro_crew/deploy/`
(it shells to the `aws` CLI with `--profile`). This skill is the **chat-native front
door** + the **AI-guided one-time setup**. You never read, store, or manage credentials,
and you never perform an IAM write — the `/deploy` console generates policy text the user
applies themselves (design Option A).

> **UI entry point (Route B, design §1.1):** publishing is initiated from an **artifact's
> page** (Publish → "Publish to public web (your AWS)"), backed by `POST /api/deploy/deploy`.
> The core **Artifact Deploy console** at `/deploy` owns profile setup, health checks,
> pending confirmations, and Manage Sites (Recall/Destroy); it has no static-artifact
> publish form. The chat-native path (this skill) still drives the same deploy endpoint,
> through the preview-only `deploy_artifact` tool.

## Hard rules (never violate)
- **Never** run `aws configure`, `aws sso login`, `ada`, or any credential-establishing
  command on the user's behalf. Tell them to run it themselves.
- **Never** create/attach/modify IAM roles or policies. Generate the JSON; the user applies it.
- **Never** auto-approve deploy / recall / destroy. Each is per-invocation confirmed.
- Publishing makes content **world-readable**. Always state this before deploying.

## Backend endpoints (dashboard/core contract; do not reinvent the aws flow)

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
apply it themselves (AWS console or their own `aws iam` command) on a dedicated
role/identity. For fullstack it also emits the required `kirocrew-deploy-app-boundary`
policy. Offer only: **"I'll apply it myself"** (the only apply path) / **"Explain it"**.
You do **not** apply IAM. A second read-only Verify check still means "access reachable,
not fully verified — first deploy is the real test."

### Step 3 — Done
Confirm config is saved (profile name + region only). Offer to publish the first artifact.

## Deploy flow (MCP preview, human execution)

1. For `widget`, `html`, or `markdown`, call `deploy_artifact` with `site_id` and
   `artifact_slug`. For a built static directory, use `local_dir`; a `webapp` artifact's
   text is only a summary and is rejected as `artifact_slug`.
2. The tool calls the preview path **without** `confirm` or `override_scan` → you get a
   preview that states the **public** nature + a pre-publish scan summary. It never
   creates infrastructure. A clean preview or overridable non-credential scan finding is
   stored under **Pending confirmations** on `/deploy`.
3. Show the preview to the user, state that the URL will be world-readable with no
   authentication, then direct the human to review and confirm there. If the scan
   blocked, show the flagged findings: credential findings are a hard block, and only the
   dashboard can explicitly override non-credential findings after the user says publish
   anyway. Never self-confirm.
4. On `AccessDenied` (502 with `missing_statement`), tell the user the exact IAM statement
   to add to the policy, then they re-run (deploys are idempotent).
5. After a successful **first** deploy (`status: "InProgress"`, `reused: false`), tell the
   user the site is **provisioning** and can take **up to ~15 minutes** to go live while
   CloudFront finishes its first global deployment — until then the URL returns a DNS / "site
   can't be reached" error (this is expected, not a failure). They can watch the live status
   flip from **In Progress → Deployed** in the **Deployments** card on `/deploy` (or via
   `GET /api/deploy/list`). Re-deploys to an existing site go live in seconds.

For an app with a backend, this skill is not the fullstack path: the operator must use the
`artifact-deploy` skill's `scripts/deploy-app.sh`, which places static and API resources
behind the same shared distribution.

## Recall vs Destroy
Run both from the `/deploy` console; the dashboard performs the preview + explicit
confirmation and binds the confirmed call to the previewed resource ids.

- **Recall** = fast unpublish (empties objects + invalidates; URL → 404; infra stays;
  reversible). Caveat: edge caches may serve briefly; already-downloaded content can't be recalled.
- **Destroy** = full teardown (disable → wait → delete distribution, OAC, bucket). Irreversible.
