# Artifact Deploy

Deploy a webapp artifact from your library into **your own AWS account** and get
a global public HTTPS link — Vercel-like, with a default TTL, automatic cleanup,
and a promote-to-persistent path. KiroCrew orchestrates; your account pays only
for what the site actually serves.

## Quick Start

1. Enable the **Artifact Deploy** app (App Store → Artifact Deploy), open it
   from the sidebar, and register an AWS profile (a named profile from
   `~/.aws/config`). Click **Verify** to confirm access.
2. Ask the agent to build something — apps saved with `kind="webapp"` appear in
   the Artifacts gallery with a live local preview.
3. Click **Deploy** on the artifact card. A deploy session opens, the agent
   proposes the plan (resources, region, cost estimate), and you confirm in the
   console.
4. You get a CloudFront URL. The card flips to **Live** with the link, TTL
   countdown, architecture summary, and a **Tear down** button.

## What gets deployed

| Tier | Resources in your account | Example |
|------|---------------------------|---------|
| Static | S3 (per-site prefix) + shared CloudFront distribution | landing page, three.js demo |
| Fullstack | + Lambda Function URL behind `/api/*` | API-backed demo |
| Stateful | + DynamoDB table | app with persistence |

The first deploy in an account creates a shared **base stack**
(`kirocrew-deploy-base`: bucket + CloudFront, ~5–15 min while CloudFront
propagates globally). Every later deploy reuses it and completes in seconds.

## TTL and the reaper

| Mode | Behavior |
|------|----------|
| Finite TTL (default 72h) | Requires the **reaper stack** (`install-reaper.sh`) — an in-account Lambda that removes expired deployments. Without it, finite-TTL deploys are refused (409). |
| Persistent (`ttl_hours=0`) | No reaper required. Tear down manually from the card or console. |

The reaper only ever touches resources that carry the `kirocrew:site` +
`kirocrew:managed` tags and match the managed naming scheme — it cannot delete
anything else in your account.

## The artifact card

- **Live preview** — the card renders your app inside a browser-framed preview.
  It prefers the **local copy** (served through a token-gated gateway channel,
  sandboxed, works even before deploying); deployed sites can also render the
  remote CloudFront page when the gateway confirms the site is framable.
- **States** — Not deployed (Deploy button + profile picker), Deploying,
  Live (URL, TTL countdown, architecture rows, cost pills, Tear down),
  Expired (tombstone + **Redeploy**).
- **Cost pills** — what-if traffic scenarios (e.g. `1,000 views · $0.05`).
  These are **estimates, not a bill**; you pay only for actual usage.

## The Artifact Deploy console

Sidebar → Artifact Deploy. One console for everything deployed:

| Section | What it does |
|---------|--------------|
| Profiles | Register/create AWS profiles, set the default, **Verify** access (an STS read — KiroCrew never stores credentials) |
| Stats | Profiles, active deployments, ready-to-deploy artifacts, estimated cost (labelled *not a bill*) |
| Fleet | Every active deployment: URL, TTL, profile, health, tear down / persist |
| Setup | IAM policy generator + reaper install guidance |

## Security model

- **KiroCrew never writes IAM.** The deploy policy and permissions boundary are
  generated for you but *you* apply them in your own account. Any error that
  needs an operator action fails loudly with the exact command to run.
- Deploy actions are tag-gated: mutations only apply to resources tagged
  `kirocrew:managed=true`.
- The local preview channel is deny-by-default: token-gated, sandboxed to an
  opaque origin, path-traversal/symlink hardened, and every response is scanned
  so credential-bearing files are refused rather than served.

## Troubleshooting

| Symptom | Cause / fix |
|---------|-------------|
| Finite-TTL deploy returns 409 | Reaper stack missing — run `install-reaper.sh` for that profile/region, or use `ttl_hours=0`. |
| Blank remote preview on the card | The deployed site's headers pre-date the current base stack. Any next deploy updates the stack in place; until then the card shows the status fallback with a plain link. |
| Card stuck on "Not deployed" after a script deploy | Script-path deploys don't auto-update the artifact yet — ask the agent to back-fill the deploy metadata (the audited API path does this automatically). |
| Old `meshclaw-deploy-*` stacks in the account | Pre-rename leftovers — see `src/kiro_crew/deploy/skills/artifact-deploy/MIGRATION.md`. |

Design doc: Pippin project `9iRV03PF7Ptb`.
