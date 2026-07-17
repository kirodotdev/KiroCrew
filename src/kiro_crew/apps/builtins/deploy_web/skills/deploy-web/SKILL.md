---
name: deploy-web
description: Publish a KiroCrew artifact (HTML / markdown / widget) to a public HTTPS URL on the user's own AWS account (private S3 + CloudFront + OAC). Use when the user says "publish this publicly", "deploy this artifact to the web", "make this a public URL", "deploy-web", or asks to set up / recall / destroy a deploy-web site. KiroCrew never stores credentials — only an AWS profile name.
---

# deploy-web — publish artifacts to your own AWS

deploy-web is a built-in app. The deploy/recall/destroy mechanics run as **deterministic
Python** in the app backend (it shells to the `aws` CLI with `--profile`). This skill is
the **chat-native front door** + the **AI-guided one-time setup**. You never read, store,
or manage credentials, and you never perform an IAM write — you generate policy text the
user applies themselves (design Option A).

> **UI entry point (Route B, design §1.1):** in the dashboard, publishing is initiated from
> an **artifact's page** (Publish → "Publish to public web (your AWS)"), which calls the
> `POST /deploy` endpoint below. The **Web Deploy app page is a management console** — guided
> setup, health check, and Manage Sites (Recall/Destroy) — it has no publish form. The
> chat-native path (this skill) is unchanged and still drives the same backend endpoints.

## Hard rules (never violate)
- **Never** run `aws configure`, `aws sso login`, `ada`, or any credential-establishing
  command on the user's behalf. Tell them to run it themselves.
- **Never** create/attach/modify IAM roles or policies. Generate the JSON; the user applies it.
- **Never** auto-approve deploy / recall / destroy. Each is per-invocation confirmed.
- Publishing makes content **world-readable**. Always state this before deploying.

## Backend endpoints (call via the gateway; do not reinvent the aws flow)
- `GET  /api/apps/deploy-web/config` → `{profile, region}`
- `PUT  /api/apps/deploy-web/config` `{profile, region}` → saves the **profile name only**
- `GET  /api/apps/deploy-web/iam-policy[?custom_domain=1]` → least-privilege policy JSON
- `POST /api/apps/deploy-web/verify` → read-only reachability (NOT full verification)
- `POST /api/apps/deploy-web/deploy` `{site_id, artifact_slug|local_dir, confirm, override_scan}`
- `POST /api/apps/deploy-web/recall` `{site_id, confirm}`
- `POST /api/apps/deploy-web/destroy` `{site_id, confirm}`
- `GET  /api/apps/deploy-web/sites` → live site list (status from the distribution)

## Guided installation (one-time, ~10–15 min — runs once, reused forever)

### Step 1 — AWS access (user-run; you only check)
Ask the user to confirm an AWS profile exists (`aws configure sso` / a named profile).
Then `PUT /config` with the profile name + region and `POST /verify`.
- If not reachable: tell them exactly what to run (`aws sso login --profile <name>` for
  expired SSO; install the AWS CLI if missing) and re-verify. Do **not** run it for them.
- On success, report the resolved account + that access is **reachable** (not "verified").

### Step 2 — Permissions (you generate; the user applies)
`GET /iam-policy`, show the JSON, and tell the user to apply it themselves (console or
their own `aws iam` command) on a dedicated role/identity. Offer only:
**"I'll apply it myself"** (the only apply path) / **"Explain it"**. You do **not** apply IAM.
Then `POST /verify` again for a read-only reachability re-check, clearly labelled
"access reachable, not fully verified — first deploy is the real test."

### Step 3 — Done
Confirm config is saved (profile name + region only). Offer to publish the first artifact.

## Deploy / recall / destroy flow (always confirm)
1. Call the endpoint **without** `confirm` first → you get a preview. For deploy it states
   the **public** nature + a pre-publish scan summary; for destroy it echoes the exact
   bucket + distribution that will be deleted.
2. Show the preview to the user and get an explicit yes.
3. Re-call **with** `confirm: true`. If deploy returns a 409 scan block, show the flagged
   findings and only pass `override_scan: true` after the user explicitly says publish anyway.
4. On `AccessDenied` (502 with `missing_statement`), tell the user the exact IAM statement
   to add to the policy, then they re-run (deploys are idempotent).
5. After a successful **first** deploy (`status: "InProgress"`, `reused: false`), tell the
   user the site is **provisioning** and can take **up to ~15 minutes** to go live while
   CloudFront finishes its first global deployment — until then the URL returns a DNS / "site
   can't be reached" error (this is expected, not a failure). They can watch the live status
   flip from **In Progress → Deployed** on the **Web Deploy** app's *My Sites* console
   (or via `GET /api/apps/deploy-web/sites`). Re-deploys to an existing site go live in seconds.

## Recall vs Destroy
- **Recall** = fast unpublish (empties objects + invalidates; URL → 404; infra stays;
  reversible). Caveat: edge caches may serve briefly; already-downloaded content can't be recalled.
- **Destroy** = full teardown (disable → wait → delete distribution, OAC, bucket). Irreversible.
