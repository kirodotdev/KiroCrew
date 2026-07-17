# KiroCrew Release Automation

Design for the three-channel release pipeline: Nightly → Beta → Stable.

## Channels

| Channel | Cadence | Source | Who uses it |
|---------|---------|--------|-------------|
| **Nightly** | Every night (UTC 06:00 = 11pm PDT) | `main` HEAD | Developers, CI |
| **Beta** | Friday cut (manual trigger) | `release/x.y.z` branch | Insider testers |
| **Stable** | After 2-week bake (manual promote) | Same release branch | All users |

## Versioning

```
Nightly:  0.2.0-nightly.20260708     (date-stamped, no branch)
Beta:     0.2.0-beta.1, 0.2.0-beta.2 (increments on hotfix cherry-picks)
Stable:   0.2.0                       (promoted from last beta)
```

Version source: `src/kiro_crew/__init__.py` (`__version__`).
Nightly appends `-nightly.{date}`. Beta uses the release branch version + `-beta.{n}`.

## Update Feed Structure (S3 + CloudFront)

```
s3://kirocrew-update-feed-{account}/
├── pre-signed/                     ← CI uploads here (unsigned)
│   ├── nightly/
│   │   └── 0.2.0-nightly.20260708/
│   │       ├── KiroCrew-...-arm64.dmg
│   │       ├── KiroCrew-...-arm64.zip
│   │       ├── KiroCrew-...-x64.AppImage
│   │       └── kirocrew-...-py3-none-any.whl
│   ├── beta/
│   │   └── 0.2.0-beta.1/...
│   └── stable/
│       └── 0.2.0/...
├── signed/                         ← CDSigner deposits here (signed + notarized)
│   ├── nightly/
│   │   └── 0.2.0-nightly.20260708/...
│   ├── beta/...
│   └── stable/...
├── feed/                           ← Update feed (Lambda-written on signed/ PUT)
│   ├── nightly/
│   │   ├── latest-mac.json
│   │   └── latest-linux.json
│   ├── beta/
│   │   ├── latest-mac.json
│   │   └── latest-linux.json
│   └── stable/
│       ├── latest-mac.json
│       └── latest-linux.json
└── blocked-versions.json           ← versions to force-update away from
```

**CloudFront** sits in front with `updates.kirocrew.dev` CNAME.
The client's `auto-update.js` already calls:
```
GET https://updates.kirocrew.dev/feed?platform=darwin-arm64&channel=insider&version=0.1.9
```

A CloudFront Function routes `?channel=X&platform=Y` to `/feed/{channel}/latest-{platform}.json`.

## Signing Pipeline (all channels)

Every build, regardless of channel, goes through signing. No unsigned builds
reach users.

```
┌─────────────┐     ┌──────────────┐     ┌─────────────────┐     ┌──────────────┐
│  CI Build   │────>│ S3 pre-signed│────>│    CDSigner     │────>│  S3 signed/  │
│ (GH Actions)│     │  /{channel}/ │     │ (sign+notarize) │     │  /{channel}/ │
└─────────────┘     └──────────────┘     └─────────────────┘     └──────┬───────┘
                                                                         │
                                                                   S3 PUT event
                                                                         │
                                                                         ▼
                                                                  ┌──────────────┐
                                                                  │ Feed Lambda  │
                                                                  │ writes       │
                                                                  │ latest-*.json│
                                                                  └──────┬───────┘
                                                                         │
                                                                         ▼
                                                                  ┌──────────────┐
                                                                  │  CloudFront  │
                                                                  │  (5min TTL)  │
                                                                  └──────┬───────┘
                                                                         │
                                                                         ▼
                                                                  ┌──────────────┐
                                                                  │   Client     │
                                                                  │ auto-update  │
                                                                  └──────────────┘
```

**CI workflow responsibilities (same for all channels):**
1. Build artifacts (wheel + desktop apps)
2. Upload unsigned to `s3://…/pre-signed/{channel}/{version}/`
3. Done. CI exits.

**Signing infrastructure responsibilities (CDK-managed):**
1. CDSigner watches or is called for `pre-signed/` objects
2. Signs macOS .app with Apple Developer ID + notarizes via Apple
3. Deposits signed artifacts into `signed/{channel}/{version}/`
4. S3 PUT event on `signed/` triggers the Feed Lambda

**Feed Lambda responsibilities:**
1. Triggered by S3 PUT on `signed/{channel}/{version}/*.zip`
2. Reads artifact metadata (version from path, file size, S3 URL)
3. Writes `feed/{channel}/latest-mac.json` (Squirrel-compatible):
   ```json
   {
     "version": "0.2.0-nightly.20260708",
     "url": "https://updates.kirocrew.dev/signed/nightly/0.2.0-nightly.20260708/KiroCrew-...-arm64.zip",
     "name": "0.2.0-nightly.20260708",
     "pub_date": "2026-07-08T06:15:00Z"
   }
   ```
4. Similarly writes `latest-linux.json` if AppImage is present
5. Does NOT write if version is in `blocked-versions.json`

## Workflows

### 1. `nightly.yml` — builds every night from main

```yaml
name: Nightly Build
on:
  schedule:
    - cron: "0 6 * * *"  # 06:00 UTC = 11pm PDT
  workflow_dispatch: {}   # manual trigger for testing

jobs:
  build:
    # builds wheel + desktop (macOS arm64, Linux x64)

  publish:
    needs: build
    steps:
      - download all artifacts
      - upload to s3://…/pre-signed/nightly/{version}/
      # CI exits here. Signing + feed update is infrastructure-driven.
```

**Retention:** pre-signed/ nightly artifacts expire after 14 days (S3 lifecycle).
signed/ nightly artifacts expire after 30 days.

### 2. `beta-cut.yml` — Friday release branch cut (manual)

```yaml
name: Beta Cut
on:
  workflow_dispatch:
    inputs:
      version:
        description: "Version to release (e.g. 0.2.0)"
        required: true

jobs:
  cut:
    runs-on: ubuntu-latest
    steps:
      - checkout main
      - create branch: release/{version}
      - bump __version__ to "{version}-beta.1"
      - commit + push branch
      - trigger build workflow on the release branch
      - upload artifacts to s3://…/beta/
      - write latest-mac.json for beta channel
      - create tag: v{version}-beta.1
      - invalidate CloudFront /beta/*
```

### 3. `beta-hotfix.yml` — push fixes to the release branch

Triggered by pushes to `release/*` branches. Builds, increments beta number,
uploads to the beta feed.

### 4. `promote-stable.yml` — promote beta to stable (manual)

```yaml
name: Promote to Stable
on:
  workflow_dispatch:
    inputs:
      tag:
        description: "Beta tag to promote (e.g. v0.2.0-beta.3)"
        required: true

jobs:
  promote:
    runs-on: ubuntu-latest
    steps:
      - download beta artifacts matching the tag
      - rename to stable version (strip -beta.N suffix)
      - upload to s3://…/stable/
      - write latest-mac.json for stable channel
      - create tag: v{version}
      - create GitHub Release with artifacts attached
      - invalidate CloudFront /stable/*
```

### 5. `rollback.yml` — emergency rollback (manual)

```yaml
name: Rollback Channel
on:
  workflow_dispatch:
    inputs:
      channel: { type: choice, options: [nightly, beta, stable] }
      version: { description: "Version to roll back TO" }

jobs:
  rollback:
    steps:
      - overwrite latest-mac.json with the specified version's metadata
      - optionally add current (bad) version to blocked-versions.json
      - invalidate CloudFront /{channel}/*
```

## Rollback Mechanism

Two layers:

1. **Feed rollback** — overwrite `latest-mac.json` to point at a previous
   version's artifacts. Clients on the bad version auto-update "down" to the
   good version (Squirrel doesn't enforce `new > current`; it just applies
   whatever the feed says).

2. **Version blocking** — add the bad version to `blocked-versions.json`.
   The client checks this on startup (separate from the feed check) and
   force-triggers an update if running a blocked version. This catches
   users who dismissed the update prompt.

## Client-Side Channel Selection

In `website/electron/auto-update.js`, the `getFlavor()` function already
maps "beta" → "insider" channel and "stable" → "stable" channel. We extend:

- Add a user-facing **Settings > Update Channel** preference: Nightly / Beta / Stable
- Store in electron-store (persists across updates)
- `getFlavor()` reads this preference instead of the build-time constant
- Default: "stable" for release builds, "beta" for beta-tagged builds

## Infrastructure (CDK additions to KiroCrewPublishCDK)

### Existing (already deployed)
- `KiroCrewGitHubCi` — GitHub OIDC + Bedrock access
- `KiroCrewCdSigner` — S3 bucket (`kirocrew-signing-artifacts-116101834266`), CDSigner IAM roles, CI signing invoker role

### New: `KiroCrewUpdateFeedStack`

```
S3 Bucket: reuse existing kirocrew-signing-artifacts bucket
  - pre-signed/{channel}/{version}/ — CI uploads here
  - signed/{channel}/{version}/     — CDSigner deposits here
  - feed/{channel}/latest-*.json    — Lambda writes here
  - Lifecycle rules:
    - pre-signed/nightly/* expires 14 days
    - signed/nightly/* expires 30 days
    - pre-signed/beta/* expires 90 days
    - signed/beta/* expires 90 days
    - signed/stable/* never expires
  - Versioning: enabled (rollback = restore previous object version)

S3 Event Notification:
  - Prefix: signed/
  - Suffix: .zip
  - Event: s3:ObjectCreated:*
  - Target: Feed Lambda

Lambda: kirocrew-update-feed-writer
  - Runtime: Python 3.12
  - Trigger: S3 PUT on signed/**/*.zip
  - Action: parse channel+version from key, write feed/{channel}/latest-mac.json
  - Also writes latest-linux.json for .AppImage events
  - Checks blocked-versions.json before writing
  - IAM: S3 GetObject on signed/, PutObject on feed/, GetObject on blocked-versions.json

CloudFront Distribution:
  - Origin: S3 bucket (signed/ and feed/ prefixes)
  - Custom domain: updates.kirocrew.dev (ACM cert in us-east-1)
  - CloudFront Function: route ?channel=X&platform=Y queries to /feed/{channel}/latest-{platform}.json
  - Cache: 5 min TTL on feed/*.json, 1 year on signed/ artifacts
  - Origin Access Control (OAC) — no public bucket access

CDSigner Integration:
  - CDSigner watches pre-signed/ (or CI calls CDSigner API after upload)
  - Signs + notarizes macOS artifacts (Developer ID + Apple notarization)
  - Linux AppImages get GPG-signed (optional, lower priority)
  - Deposits results into signed/{channel}/{version}/
```

## Release Calendar (steady state)

```
Mon-Thu: commits land on main
Fri (weekly):
  - Nightly builds every night as usual
  - OPTIONAL: manual "Beta Cut" if enough has accumulated

Fri (biweekly, when ready):
  - Trigger "Beta Cut" workflow
  - Creates release/x.y.z branch
  - Beta testers get the update automatically

Next 2 weeks:
  - Bug reports → cherry-pick fixes to release branch
  - Each push → new beta.N uploaded to beta feed
  - Nightly continues independently from main

After 2 weeks (or when beta is stable):
  - Trigger "Promote to Stable"
  - All stable users get the update

Emergency:
  - Trigger "Rollback" workflow
  - Or cherry-pick fix → push to release branch → auto-publishes new beta
```

## Migration from Current State

1. **Phase 1** — Add nightly workflow + S3 feed bucket (CDK). Wire `auto-update.js` to use the new feed URL once DNS is live.
2. **Phase 2** — Add beta-cut + promote workflows. Test with internal users.
3. **Phase 3** — Add rollback workflow + blocked-versions.json client check. Add Settings > Update Channel UI.

The existing `release.yml` (GitHub Releases on tag push) stays as-is for the pip wheel distribution. Desktop auto-update uses the S3 feed independently.
