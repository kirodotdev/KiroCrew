# Artifact Deploy

Artifact Deploy publishes an artifact from your library to a public HTTPS URL in **your own AWS
account**: a private S3 bucket behind CloudFront with Origin Access Control, with an optional
time-to-live and automatic cleanup. Kiro Crew stores only your AWS **profile name**, never your
credentials, and it never edits your IAM. Your account pays only for what the site actually
serves, which for a small static page is effectively nothing.

Deploy is part of Kiro Crew itself, so there is nothing to install or enable. The console lives at
`/deploy` in the dashboard (the **Artifact Deploy** button on the Artifacts page opens it); it
holds AWS profile setup, the IAM policy generator, and the list of everything you have deployed.
Publishing happens from the thing you want to publish: an artifact's **Publish** panel, or the
**Deploy** button on an app card.

---

## 1. One-time AWS setup

You need an AWS account you control. Setup happens once; later publishes take seconds.

### 1.1 The profile must live on the gateway host

Artifact Deploy shells out to the `aws` CLI **from the gateway process**, so the AWS profile has
to be configured on the machine running the Kiro Crew gateway, not on your laptop if the gateway
runs somewhere else. Running `aws configure sso` on a different machine has no effect.

Deploy needs a POSIX shell. On Windows every deploy endpoint returns an error asking you to run
the gateway under WSL.

### 1.2 AWS CLI v2 is required for SSO

Only AWS CLI v2 understands `sso-session` profiles. A v1 binary (often the system `/usr/bin/aws`)
fails verification with:

```
... is configured to use SSO but is missing required configuration:
sso_start_url, sso_region
```

To fix it:

1. Install AWS CLI v2 (for example into `~/.local/bin`).
2. Make sure the **gateway's** `PATH` resolves v2 before any v1. Put the v2 directory at the front
   of your shell rc (`~/.zshrc` / `~/.bashrc`) so every gateway restart inherits it, then restart
   the gateway.
3. Confirm with `aws --version`, which should print `aws-cli/2.x`.

If a long-running gateway still resolves v1, symlinking v2 into a directory that already precedes
`/usr/bin` on the gateway's `PATH` takes effect without a restart, because each call re-resolves
the binary.

### 1.3 Authenticate

Run one of these in a terminal on the gateway host. Kiro Crew never sees the keys:

```bash
aws configure sso                  # recommended: short-lived, auto-refreshing
aws configure --profile myweb      # or a long-lived named profile
```

With `aws configure sso` the account is chosen at the account-selection step; the saved profile
holds a profile name, not an account number.

### 1.4 Register the profile in the console

On the console at `/deploy`:

1. Register the **profile name** and **region**. Registered profiles are kept in
   `~/.kiro/crew/deploy/profiles.json`, and one of them is the default. Optionally supply a
   12-digit account plus a role name and Kiro Crew writes a `credential_process` entry that assumes
   that role; only `region` and `credential_process` are ever written to your AWS config, never
   credential material.
2. Click **Verify**. This is a read-only reachability check (`sts:GetCallerIdentity` plus harmless
   `s3` and `cloudfront` list calls). It confirms the profile resolves and the services answer. It
   is **not** full verification: create and write permissions cannot be checked without writing.
3. Click **Get IAM policy** and apply the generated least-privilege policy **yourself** to a
   dedicated role or identity. **Kiro Crew never writes IAM.** For the `fullstack` tier the console
   also emits a permissions-boundary policy that must exist as `kirocrew-deploy-app-boundary`
   *before* the first deploy, because role creation is conditioned on it.

The first real deploy is the true permission test. On `AccessDenied` the error names the exact
policy statement to add, and deploys are idempotent, so you fix the policy and re-run.

---

## 2. Publishing

**An artifact** (HTML, Markdown, widget): open it and choose **Publish** then **Publish to public
web (your AWS)**. You pick a TTL, get a preview (size, scan result, resolved profile and region),
and confirm. This creates a dedicated CloudFront distribution for the site and returns a
`https://<random>.cloudfront.net/` URL.

**An app artifact** (`kind="webapp"`): click **Deploy** on the card. That opens a fresh chat
session that runs the `artifact-deploy` skill on the app, so the agent can conform the app to the
deploy layout, ship it, and debug failures in place. The script path the skill uses puts many apps
behind one shared per-account base stack (`kirocrew-deploy-base`: one private bucket plus one
global distribution), each served under its own `/<slug>/` prefix.

### The first deploy is slow, once

Whichever path creates a CloudFront distribution, that distribution takes **roughly 5 to 15
minutes** to propagate globally. Until it reaches `Deployed`, the link returns a DNS or "site can't
be reached" error. This is expected, not a failure. Watch the status in the console. Later deploys
reuse the existing distribution and are just an S3 upload plus a cache invalidation, so they
complete in seconds.

### What gets created

| Tier | Resources in your account | Example |
|------|---------------------------|---------|
| Static | S3 (private, per-site prefix) + CloudFront + OAC | landing page, three.js demo |
| API | adds an API Gateway HTTP API to a Lambda at `/<slug>/api/*` | API-backed demo |
| Stateful | adds a DynamoDB table | app that persists data |

The Lambda is invoked only by API Gateway rather than exposed through a world-accessible Function
URL, so accounts running automated guardrails against public Function URLs do not flag it.

---

## 3. TTL, the reaper, and taking a site down

| Mode | Behavior |
|------|----------|
| Finite TTL (API default 72h, maximum 8760h) | Requires the **reaper**: an in-account, EventBridge-scheduled Lambda that deletes expired deployments. Without it, finite-TTL deploys are refused with a 409. |
| Persistent (`ttl_hours=0`) | No reaper required. Take the site down yourself from the console. |

The reaper is installed once per account by an operator, with
`scripts/install-reaper.sh --profile <P> --region <R>` from
`~/.kiro/crew/skills/artifact-deploy/`. It is an operator step by design: the stack creates an IAM
role, and Kiro Crew never writes IAM. When a finite-TTL deploy is refused, the 409 body carries
that exact command with your profile and region already filled in.

The reaper only touches resources that carry both the `kirocrew:site=<id>` and
`kirocrew:managed=true` tags and match the managed naming scheme. It verifies the tag against the
slug it is about to delete before deleting anything, so it cannot remove something else in your
account.

Three ways to take content down:

- **Recall** is a fast unpublish: it empties the site's objects and invalidates the cache, so the
  URL returns 404 within seconds to minutes. The infrastructure stays, so re-deploying restores
  it. Edge caches may serve briefly until the invalidation completes, and content someone already
  downloaded cannot be recalled.
- **Destroy** is full teardown: disable, wait, then delete the distribution and bucket. It is
  irreversible, and the CloudFront disable step alone takes 5 to 15 minutes.
- **Cancel / Tear down** on an app card marks the deployment expired and lets the in-account
  reaper remove the infrastructure on its next sweep. It refuses with a 409 if the reaper is not
  installed, because tombstoning the card would otherwise leave live infrastructure with no
  cleanup path.

Recall and Destroy are each a two-call confirm gate: the first call previews the live resources,
and the confirmed call is bound to those exact resource ids, so a site recreated in between is
refused rather than emptied under a stale dialog.

---

## 4. The app card

- **Live preview.** The card renders your app in a browser-framed preview, preferring the **local
  copy** so it works before you deploy at all and does not depend on the remote site, the CDN, or
  the network. The local channel is served only to a directly connected local browser, and only on
  macOS and Linux. A deployed site is iframed instead when its headers permit framing from
  localhost; otherwise the card shows a status panel with a plain link.
- **States.** Not deployed (Deploy button plus profile picker), Deploying, Live (URL, TTL
  countdown, architecture rows, cost, Cancel / Tear down), Expired (tombstone plus **Redeploy**).
- **Cost.** What-if traffic scenarios, for example `1,000 views - $0.05`, built from live AWS
  Pricing API rates for the profile's region (cached for a day, with a fallback price table so an
  estimate never blocks on a pricing lookup). These are **estimates, not a bill**; you pay only
  for actual usage.

---

## 5. Cost

Small static sites sit inside the S3 and CloudFront free tier, so they are effectively
**~$0/month**. Idle cost is close to zero because everything in the stack scales to zero. Every
cost surface is labelled as an estimate, and no billing permissions are requested.

---

## 6. Security model

Artifact Deploy is built to keep Kiro Crew out of credential and account management entirely, and
to serve content from a bucket that is never itself public.

### 6.1 Credentials never touch Kiro Crew
- Only the **profile name** is stored (in `~/.kiro/crew/deploy/profiles.json`).
- Every AWS call runs through the **`aws` CLI as a subprocess** with `--profile` rather than an
  in-process SDK, so credential resolution stays in your OS credential store.
- Kiro Crew **never writes IAM** and never creates or manages accounts, users, or roles. You apply
  the generated least-privilege policy yourself.

### 6.2 The origin bucket is private
- The bucket is created with Block Public Access on, `BucketOwnerEnforced` ownership, and
  SSE-AES256 encryption.
- It has **no public bucket policy**. Only CloudFront can read it, through an OAC bucket policy
  whose `AWS:SourceArn` condition pins the **specific distribution**, so no other principal
  (including another CloudFront distribution) can read it.
- Per-site bucket names are random and opaque (`kirocrew-web-<hex>`) and are hidden from the
  public URL by CloudFront.

### 6.3 The published URL is public by link
- Content is served from a random CloudFront domain over HTTPS only. **Anyone with the link can
  view it**: treat published content as world-readable. There is no auth or signed-URL gate.
- Obscurity is not access control. Do not publish anything you would not put on the open internet.

### 6.4 Mutations are tag-gated
The generated IAM policy conditions every mutating and deleting action on
`aws:ResourceTag/kirocrew:managed=true`, and Kiro Crew tags each resource at creation. An unrelated
production bucket, distribution, or API in the same account carries no such tag, so it cannot be
modified or deleted through this policy even with its id in hand. Resource names are additionally
scoped to the `kirocrew-deploy-*` prefix, and the audit-log bucket is covered by an explicit Deny.

### 6.5 Pre-publish content scan
- Before upload, content runs through Kiro Crew's credential patterns plus data-leak heuristics
  (private-key headers, vendor API keys, internal hostnames, cloud account ids and ARNs).
- **Credential-severity findings can never be overridden.** The deploy is refused outright.
- Other findings block the deploy and are shown to you; you may then explicitly choose "publish
  anyway" or "Deploy anyway". Detection is best-effort, not a guarantee.

### 6.6 Local-directory guards
When publishing from a local directory, the path must resolve inside your configured workspace
roots, and it plus its contents are checked against the sensitive-path rules before any read, so a
credential directory (`~/.aws`, `~/.ssh`, `~/.gnupg`) can never be pushed to a public URL. The
tree is snapshotted first and the snapshot is scanned, and any symlink or hardlink in it is
rejected, so the directory cannot be swapped between the check and the upload.

### 6.7 Confirm gate and audit on every mutating action
Deploy, Recall, Destroy, and Tear down each require an explicit confirmation and are never
auto-approved. Every confirmed action emits an audit event recording the action, the site, and the
outcome.

---

## 7. Troubleshooting

| Symptom | Cause / fix |
|---------|-------------|
| Finite-TTL deploy returns 409 | The reaper stack is missing. Run `install-reaper.sh` for that profile and region (the 409 body has the exact command), or publish with `ttl_hours=0` for a persistent site. |
| URL shows "site can't be reached" / a DNS error right after publishing | A new distribution is still propagating. Wait up to ~15 minutes for the first deploy and watch the status in the console. |
| Verify fails with `missing ... sso_start_url, sso_region` | The gateway resolved AWS CLI v1. Install v2 and put it ahead of `/usr/bin` on the gateway's `PATH` (see 1.2). |
| Verify shows `s3_reachable: false` with a correct-looking policy | The policy needs `s3:ListAllMyBuckets` in its `DiscoveryAndIdentity` statement. |
| First deploy returns `AccessDenied` | The error names the exact missing IAM statement. Add it to your policy and re-run; deploys are idempotent. |
| Profile saved but nothing works | The profile has to exist on the **gateway host**, not on your laptop (see 1.1). |
| Blank or missing remote preview on an app card | The deployed site's headers do not allow framing from localhost. The card falls back to a status panel with a plain link; re-deploying a base-stack site applies the current template. |
| App card still says "Not deployed" after a deploy | Only the agent-driven deploy path back-fills the card's metadata. After a raw script deploy from a terminal, ask the agent to record the public URL and lifecycle status on the artifact. |
| Two base stacks, or duplicate buckets and distributions, in one account | A pre-rename install left a parallel set of stacks. Follow `~/.kiro/crew/skills/artifact-deploy/MIGRATION.md` to move live sites over and remove the old set. |
