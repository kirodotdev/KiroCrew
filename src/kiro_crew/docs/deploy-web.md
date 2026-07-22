# Artifact Deploy (deploy-web)

Publish a KiroCrew artifact (HTML / Markdown / widget) to a public HTTPS URL on
**your own AWS account** — private S3 + CloudFront + Origin Access Control (OAC).
KiroCrew stores only your AWS **profile name**; it never sees, stores, or manages
your credentials.

Artifact Deploy is a built-in app, **opt-in / disabled by default**. Enable it from
Settings → Apps (or `kirocrew app enable deploy-web`).

- The **Artifact Deploy page** is a management console: AWS setup, health check, and
  site management (Recall / Destroy).
- **Publishing** happens from an artifact's own page: **Publish → "Publish to
  public web (your AWS)"**.

---

## 1. One-time AWS setup

You need an AWS account you control (Amazon-internal iam-identity or an external AWS
account both work). Setup is once; every later publish is ~30 seconds.

### 1.1 Where the profile must live (important)

Artifact Deploy shells out to the `aws` CLI **from the gateway process**. The AWS
profile must therefore be configured on the **machine running the KiroCrew
gateway** (your dev desktop / host), **not** on your laptop if the gateway runs
elsewhere. Running `aws configure sso` on a different machine does not help.

### 1.2 AWS CLI v2 is required for SSO

SSO `sso-session` profiles can only be parsed by **AWS CLI v2**. AWS CLI v1
(often the system `/usr/bin/aws`, Python 2.7) predates the `sso-session` format
and fails verification with:

```
... is configured to use SSO but is missing required configuration:
sso_start_url, sso_region
```

Fix:

1. Install AWS CLI v2 (e.g. to `~/.local/bin`).
2. Make sure the **gateway's** `PATH` resolves v2 **before** any v1. Add the v2
   directory to the front of your shell rc (`~/.zshrc` / `~/.bashrc`) so every
   gateway restart inherits it, then restart the gateway cleanly.
3. Verify the right binary: `aws --version` should print `aws-cli/2.x`.

> Tip: if a long-running gateway still resolves v1, symlink v2 into a directory
> that already precedes `/usr/bin` in the gateway's `PATH` (e.g.
> `ln -sf ~/.local/bin/aws ~/.toolbox/bin/aws`) — `execvp` re-resolves per call,
> so the fix takes effect without a restart.

### 1.3 Authenticate

Pick one (run in the gateway host's terminal — KiroCrew never sees your keys):

```bash
aws configure sso                  # recommended: short-lived, auto-refreshing
aws configure --profile myweb      # or a long-lived named profile
```

For `aws configure sso`, the account is bound at the **account selection** step;
the profile stores a profile name only, not an account number.

**Amazon-internal:** use `ada credentials update` / `ada profile add` as usual.
A `credential_process` entry in `~/.aws/config` lets the CLI auto-refresh tokens.

### 1.4 Configure Artifact Deploy

On the Artifact Deploy page:

1. Enter the **profile name** and **region**, click **Save**.
2. Click **Verify access** — a **read-only reachability** check
   (`sts:GetCallerIdentity` + `s3`/`cloudfront` list calls). It confirms the
   profile resolves and the services are reachable. It is **not** full
   verification — create/write permissions can't be checked without writing.
3. Click **Get IAM policy**, then apply the least-privilege policy **yourself**
   to a dedicated role/identity (console or your own `aws iam` command).
   **KiroCrew never edits your IAM.** The first real deploy is the true test: on
   `AccessDenied` it reports the exact missing IAM statement to add, then you
   re-run (deploys are idempotent).

---

## 2. Publishing, and the ~15-minute first-deploy wait

Publish from an artifact's page (**Publish → "Publish to public web (your AWS)"**).
Before upload, content is scanned for secrets and internal-data signals and
publishing is **blocked-and-warned** on any finding until you explicitly choose
"publish anyway".

A **first** deploy of a new site provisions a fresh CloudFront distribution. The
distribution must finish its first global deployment (`In Progress → Deployed`)
before the URL works — **typically up to ~15 minutes**. Until then the link
returns a DNS / "site can't be reached" error. **This is expected, not a
failure.** Watch the live status on the Artifact Deploy page's **Static Sites** console;
re-deploys to an existing site go live in seconds.

---

## 3. Security model

Artifact Deploy is designed to keep KiroCrew **out of credential and account
management** entirely, and to serve content from a bucket that is never itself
public.

### 3.1 Credentials never touch KiroCrew
- KiroCrew stores **only the profile name** (in
  `~/.kirocrew/apps/deploy-web/data/config.json`).
- All AWS calls run through the **`aws` CLI subprocess** with `--profile`
  (never boto3), so credential resolution stays in your OS credential store.
- KiroCrew **never** writes IAM and **never** creates/manages accounts, users,
  or roles. You apply the generated least-privilege policy yourself (Option A).

### 3.2 The origin bucket is private
- The S3 bucket is created with **Block Public Access ON**,
  `BucketOwnerEnforced` ownership, and **SSE-AES256** encryption.
- It has **no public bucket policy**. Only CloudFront can read it, via an **OAC**
  bucket policy whose `AWS:SourceArn` condition pins the **specific distribution**
  — no other principal (including other CloudFront distributions) can read it.
- The bucket name is random/opaque (`kirocrew-web-<random hex>`) and is hidden
  from the public URL by CloudFront + OAC.

### 3.3 The published URL is public-by-link
- The site is served at a random CloudFront domain
  (`https://<random>.cloudfront.net/`). **Anyone with the link can view it** —
  treat published content as world-readable. v1 has no auth/signed-URL gate.
- Access is "public by obscurity" only (random bucket name + random domain).
  Do not publish anything you wouldn't put on the open internet.

### 3.4 Pre-publish content scan (block-and-warn)
- Before upload, content runs through KiroCrew's secret/credential regexes plus
  internal-data heuristics (employee aliases, internal `*.example.com` hosts,
  cloud account IDs / ARNs).
- On a match, publishing is **blocked** and the findings are shown; you must
  explicitly choose "publish anyway" to override. Detection is best-effort, not
  a guarantee.

### 3.5 Sensitive-path guard
- When publishing from a local directory, the path (and its contents,
  recursively) is validated against `is_sensitive_path()` before any read or
  upload, so a credential directory (`~/.aws`, `~/.ssh`, `~/.gnupg`, …) can never
  be pushed to a public URL.

### 3.6 Confirm-gate + audit on every mutating action
- **Deploy / Recall / Destroy** are each a two-call **confirm-gate**: the first
  call returns a preview (resources, public nature, scan summary) and you must
  re-call with `confirm` to proceed. They are never auto-approved.
- Each confirmed action emits a **SEL audit event** (action, site_id, outcome).

### 3.7 Taking content down
- **Recall** = fast unpublish: empties the bucket objects and invalidates the
  cache so the URL returns 404 in seconds-to-minutes. Infra stays; reversible by
  re-deploying. Caveat: edge caches may serve briefly until invalidation
  completes, and already-downloaded content cannot be recalled.
- **Destroy** = full teardown: disable → wait → delete the distribution, OAC, and
  bucket. Irreversible.

---

## 4. Cost

Small static sites are effectively **~$0/month** under the S3 + CloudFront free
tier. The UI always shows cost as **"~estimated"**; no billing permissions are
requested.

---

## 5. Troubleshooting

| Symptom | Cause / fix |
|---------|-------------|
| URL shows "site can't be reached" / DNS error right after publishing | New distribution still `In Progress`; wait up to ~15 min for first deploy. Check **Static Sites** status. |
| Verify fails with `missing ... sso_start_url, sso_region` | Gateway resolved AWS CLI **v1**. Install v2 and put it ahead of `/usr/bin` in the gateway's PATH (§1.2). |
| Verify shows `s3_reachable: false` with a correct policy | Ensure the policy includes `s3:ListAllMyBuckets` in the `DiscoveryAndIdentity` statement. |
| First deploy returns `AccessDenied` | The error names the exact missing IAM statement — add it to your policy and re-run (deploys are idempotent). |
| Profile saved but nothing works | The profile must exist on the **gateway host**, not your laptop (§1.1). |
