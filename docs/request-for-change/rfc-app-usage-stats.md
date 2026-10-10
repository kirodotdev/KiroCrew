---
title: "App usage stats for builders: installs, active installs and version history per published app"
status: draft
author: FlorentLa
created: 2026-10-10
last-audited: 2026-10-10
audited-at: d32c084921
doc-pr: 18855
implementation-prs: []
tracking-issues: [18761]
supersedes: []
superseded-by: []
---

# RFC: App usage stats for builders: installs, active installs and version history per published app

- Status: draft. Nothing is built. Acceptance requested from a maintainer; the status flips to `accepted` when one records it in [Acceptance](#acceptance). Five [decisions](#decisions-needed) gate the phases that send or render anything.
- Author: FlorentLa
- Partially addresses [#18761](https://github.com/kirodotdev/KiroCrew/issues/18761): it covers installs, active installs and the version distribution over time. The issue's opt-in finer-grained usage (tools, pages, crons) is a non-goal here and needs its own design.
- Related: [#17520](https://github.com/kirodotdev/KiroCrew/issues/17520) and its client PR [#17958](https://github.com/kirodotdev/KiroCrew/pull/17958) (install-count ranking document), [#9490](https://github.com/kirodotdev/KiroCrew/issues/9490) (Discover sort), [#12883](https://github.com/kirodotdev/KiroCrew/issues/12883) (Discover popularity), [#1440](https://github.com/kirodotdev/KiroCrew/issues/1440) (closed: the server-side `AppInstallations` rollup), `docs/system-specs/modules/metrics.md` (beacon, consent ladder, install receipt, retention).

## Summary

A builder who publishes a Kiro Crew app sees GitHub stars on an official listing and nothing else: no install count, no idea how many installs are still enabled, no idea which versions are in the field. This RFC proposes:

1. A **weekly presence ping** per enabled app, carrying a token that rotates every week and the app's own normalized version. It goes through the existing consent ladder and is never joined to install receipts or the heartbeat.
2. **Three fields added to the published per-app document** that [#17958](https://github.com/kirodotdev/KiroCrew/pull/17958) proposes for the official catalog (`active`, `versions`, `series`), with one small-count floor applied to every count in it. External registries publish the same schema at an https URL their index declares.
3. A **Usage section** on the app's detail page, rendered only when the listing's own registry publishes stats for it.

Official apps report to the existing telemetry endpoint. An external registry receives data only from hosts whose owner confirmed that registry's exact collector URL. Local, dev and legacy installs with unknown provenance never report.

## Motivation

### What exists today

Verified at `d32c084921`:

- **Install receipt.** `src/kiro_crew/apps/install_receipt.py` sends one GET at the end of a successful `install_from_registry` (the two `install_receipt.dispatch_async` calls in `src/kiro_crew/apps/registry_pipeline/install.py`), with `k=fresh` or `k=update`. Provenance is decided by `_official_entry` in `install.py` (an entry with no `_registry` tag); `dispatch` returns early for anything else, and `should_send` refuses it again as defence in depth (`"unofficial"`). Its `v` field is Kiro Crew's release (`app_version=__version__` in `_send_configured`, clamped by `beacon.release`), not the app's version.
- **No population signal.** Nothing reports that an app is still installed or enabled, and nothing reports an uninstall. The heartbeat (`beacon.py`) carries no app data.
- **No consumer in this repository.** The rollup ([#1440](https://github.com/kirodotdev/KiroCrew/issues/1440)) was closed as not planned here because it needs an owner with access to the telemetry account. The ranking client ([#17958](https://github.com/kirodotdev/KiroCrew/pull/17958)) is open and not merged; it reads `cumulativeInstalls` and `windowedInstalls` per official slug, with no small-count floor.
- **Raw logs are permanent.** metrics.md "Retention: S3/Athena is permanent" and the delivered field set: every stored row keeps its date, time, URI and edge-derived `c-country`, and no client IP or User-Agent.
- **External registries are git repositories, not services.** `ExternalRegistryConfig` (`src/kiro_crew/config/integration_sections.py`) is a repo URL and branch; `_fetch_external_registry_index` (`src/kiro_crew/apps/registry_pipeline/indexes.py`) shallow-clones into a temporary root, reads `app-registry.json` or scans `apps/*/app.json`, returns the entry list and deletes the clone.
- **The local record.** `InstalledApp` in `src/kiro_crew/apps/manager.py` carries `version`, `enabled`, `dev`, `origin`, `source`, `sourceUrl` and `sourceRegistry`. Records written before provenance was recorded have these fields empty.
- **The only listing signal.** `AppDetailPage.tsx` shows GitHub stars (`stargazersCount`), baked into catalog rows by `official_catalog.py`.

### What a builder cannot answer

- Did anyone install my app, and how many still have it enabled?
- Did users move to the release I shipped last week, or is an old version still in the field that I have to keep supporting?
- Is a breaking change safe yet?

Install receipts cannot answer these: a receipt counts an event, not a population, and does not carry the app version.

## Goals

- For every app on a registry that publishes stats: total installs, active installs (installed and enabled, reported in the last week), active installs per app version, and a weekly history of those numbers.
- The official catalog and any external registry whose owner runs a collector use one schema and one client.
- No identifier that links one installation's apps to each other, to the heartbeat, or to its own install receipts, across more than one week.
- An app with no published stats renders exactly as today: no zero, no empty panel.

## Non-goals

- Finer-grained usage (tool calls, page views, cron runs, errors). It needs a per-app opt-in by the installing user and a manifest declaration by the author.
- Building the official collector and producer. As with [#1440](https://github.com/kirodotdev/KiroCrew/issues/1440), that is telemetry-account work outside this repository; this RFC pins the contract and [decision 0](#0-owner-of-the-official-producer) asks who owns it.
- Shipping a collector for external registries. The contract is specified so a registry owner can run one; none ships in this repository.
- A Discover "my apps" filter. The rule for "mine" is sketched under [Follow-ups](#follow-ups); it is not needed to show stats on a listing.
- Ranking or reordering Discover ([#17520](https://github.com/kirodotdev/KiroCrew/issues/17520), [#9490](https://github.com/kirodotdev/KiroCrew/issues/9490)). Presence numbers never feed ranking.

## Design

### 1. The presence ping

Once per ISO week, for each eligible app (§2), the gateway sends one GET:

```text
/b/1/presence/<app-slug>?p=<token>&av=<app-version>
```

| Field | Contract |
|---|---|
| `<app-slug>` | The registry slug, validated by `_valid_slug`. |
| `p` | First 32 hex characters of `HMAC-SHA256(key=receipt_secret, msg=b"app-presence:" + slug + b":" + iso_week)`, e.g. `iso_week = "2026-W41"`. Domain-separated from the receipt's `b"app-install:"` token, so presence and receipt rows cannot be joined, and a fresh value each week, so a token's series is one week long. |
| `av` | The app's installed `version` through one shared normalizer (§5): `x.y.z`, `x.y.z-pre` for any prerelease, or `unknown`. |

Rules:

- **Enabled only.** A disabled app sends nothing. Active therefore means installed, enabled and reported this week; there is no disabled count.
- **One request per app, never batched.** One request naming several apps would link them.
- **Send time.** Each (app, week) gets a target drawn uniformly at random within the first 7 hours of the gateway's cumulative uptime in that ISO week, counted across restarts. The ping is sent when the week's uptime counter reaches the target, except that no presence send is made in the 30 minutes after a heartbeat send: a target reached in that window waits until it closes. A gateway that runs at least 8 hours in a week, in sessions longer than 30 minutes, always reports for it, so one running two hours a day reports every week by Thursday. A week in which the gateway runs less than its target sends nothing for that app.
- **Same consent ladder.** Every send goes through `beacon.telemetry_permitted`: `KIROCREW_TELEMETRY_DISABLED`, the `capabilities.telemetry` ceiling, the beacon toggle, CI and non-default-home suppression, and the first-run acknowledgement. `--test-mode` gateways do not run the scheduler. [Decision 1](#1-consent-for-the-presence-ping) settles the disclosure.
- **Uninstall is absence.** No uninstall ping exists; an app that sends nothing in a week is not active that week.
- **Best effort.** A failed send is dropped and never retried into another week.

The install receipt also gains `av`, so an install event records which app version it installed. `v` keeps its meaning.

`receipt_secret`'s comment states that it keys receipt tokens "and nothing else". The domain-separated presence derivation needs that comment, and the metrics.md row for `t`, updated to name both uses.

### 2. Eligibility and destination

Eligibility is decided from the local record at send time, and fails closed:

| Install record | Destination |
|---|---|
| `origin == "registry"`, `sourceRegistry == ""`, `dev` false, and `sourceUrl` equal to the official catalog's repository for that slug | The build-pinned default beacon endpoint. A `telemetry.beacon_endpoint` override in `config.json` does not redirect presence: that file has gateway-side writers (the dashboard config API, channel handlers, migrations), so a value in it is not an owner assertion. |
| `origin == "registry"`, `sourceRegistry` naming a configured external registry with a confirmed collector grant (below), `dev` false | That registry's confirmed collector URL. Install receipts for that registry go there too. |
| Anything else: `origin` `local` or `external`, `dev: true`, empty `sourceUrl` (legacy records), a `sourceUrl` that is a local path, an external registry with no grant | Nothing is sent. |

An empty-provenance record stays silent until an update rewrites its provenance; otherwise a legacy install from a private registry would be classified as official and its slug sent to the vendor endpoint. A store update that copies from a builder's dev checkout records the checkout path as its source and is therefore excluded.

**External collector grants.** The collector URL cannot come from `config.json` or from the index alone: the index is untrusted, and `config.json`, although read-only inside the sandbox, has gateway-side writers that act on requests (the dashboard config API, channel handlers, migrations), which is why `ExternalRegistryConfig`'s field docs let only a build-pinned row claim a trust tier. An index may *propose* `statsCollector` and `statsUrl`; the owner confirms them once per registry through an owner-only route. The grant is stored next to the `owner_trusted` record in `registry_trust.json` (agent-unwritable: read-only in every sandbox mode, `_CREW_READONLY_LEAVES` in `sandbox.py`) and records the exact collector URL; a changed proposal needs a new confirmation. A new governance scope, `capabilities.app_stats_external`, lets a managed fleet pin external stats egress off independently of `capabilities.telemetry`.

The confirmation dialog states what the collector receives: this host's IP address, the time, the registry's own app names, their versions, and a weekly per-app identifier. The official path's no-IP, no-User-Agent log projection does not bind a third-party collector, and the unlinkability argument in §1 covers the official path only: an external collector can link every app from its registry on one host by IP. Phase 2 sets a fixed User-Agent on both paths instead of urllib's default, which carries the Python minor version.

### 3. The stats schema

One document schema, used by the official catalog and by external registries. For the official catalog it is the document [#17958](https://github.com/kirodotdev/KiroCrew/pull/17958) proposes, extended; this RFC adds fields and a floor and does not introduce a second document.

```json
{
  "schemaVersion": 2,
  "generatedAt": "2026-10-12T00:00:00Z",
  "floor": 5,
  "apps": {
    "customer-360": {
      "cumulativeInstalls": 41,
      "windowedInstalls": null,
      "active": 23,
      "versions": { "0.3.11": 15, "0.3.10": null, "other": null },
      "versionsBelowFloor": ["0.3.9"],
      "series": [
        { "week": "2026-W41", "active": 23, "newInstalls": null, "versions": { "0.3.11": 15, "0.3.10": null, "other": null } }
      ]
    }
  }
}
```

- `cumulativeInstalls`, `windowedInstalls`: as #17958 defines them, from `k=fresh` receipts. Updates never add to them. They start at rollout: installs that predate receipts are not in them, so early on `cumulativeInstalls` can be below `active`, and the Usage section says "since <date>".
- `active`: distinct `p` tokens for that slug in the last complete ISO week.
- `versions`: `active` by `av`. `versionsBelowFloor` lists version names whose count is below the floor, without counts, so a builder still sees that an old version is in the field.
- `series`: one entry per ISO week, bounded by the producer's retention.
- **Floor.** Every count below `floor` is published as `null`. If exactly one count in a group (the version buckets of one app, or `active` against its buckets) is hidden, the producer hides the next-smallest too, so the hidden one cannot be recovered by subtraction. That second count may be at or above the floor, so `null` means "hidden", never "below the floor". The floor applies to `cumulativeInstalls` and `windowedInstalls` as well, which is a change to #17958's contract; without it the floor here would hide nothing. The client enforces a minimum floor of 5 whatever a document declares.

The weekly series remains open to differencing across weeks (23 to 22 shows one host stopped). Publishing weekly rather than daily, with the floor, is the mitigation this RFC proposes; [decision 4](#4-floor-window-and-retention) asks whether that is enough.

**Retention.** The producer's retention bounds the published series only. Each presence row is stored in the official raw logs permanently, with its timestamp and country, like every other row. Decision 4 asks whether presence rows get their own log prefix with an expiry.

**Where the client reads it.**

- Official: the URL #17958 fetches, through `official_catalog.fetch_document` (already on main: https only, no redirects, byte cap), with the caller's own cache and last-good fallback as #17958 implements.
- External: the https `statsUrl` the registry's grant records, fetched through the same seam. A static document hosted by the collector keeps stats out of the registry repo and out of its release branch. Reading an `app-stats.json` committed to the registry repo is not proposed: `_fetch_external_registry_index` discards its clone, and a daily bot commit would collide with registries that publish by mirroring a branch.

**Parsing.** The document is untrusted display data. Integers only (no `bool`, no `NaN` or `Infinity` via `parse_constant`), size capped before parsing, series length bounded, version keys matching `^\d+\.\d+\.\d+(-pre)?$|^unknown$|^other$`, slugs matching `KEBAB_RE`, and slugs absent from that registry's index ignored. A malformed document shows no stats. Stats never feed trust, install, ranking or ordering, and external stats are labelled "reported by <registry>".

### 4. Who runs what for an external registry

A registry owner who wants stats runs, outside Kiro Crew:

- an https collector that accepts unauthenticated GETs on the two routes, reachable from every member's gateway (an endpoint behind interactive SSO does not work: the sender carries no credentials);
- row storage and a weekly producer that writes the schema in §3;
- static https hosting for the produced document (the `statsUrl`).

Nothing in this RFC requires the registry's git repository to change beyond the two proposed index fields. Counts are advisory: the routes are unauthenticated, so anyone can send pings for any slug.

### 5. Version normalization

One function, in `src/kiro_crew/apps/version.py` beside `parse_version`, used by the ping, by the parser, and by the UI's update comparison: the leading numeric release padded to `x.y.z`; `-pre` appended when any prerelease or PEP 440 suffix was present (`0.4.0-rc.1` and `0.4.0rc3` both give `0.4.0-pre`, so testers are not counted as a release that has not shipped); `unknown` when no numeric release parses. Build metadata is dropped. `beacon.release` is not reused: it maps prereleases onto the release and unparseable values onto `unknown` for a different purpose (Kiro Crew's own version).

### 6. Where a builder sees it

The app detail page (`website/src/pages/AppDetailPage.tsx`) gains a Usage section: active installs, installs since rollout, a version breakdown with the below-floor version names, and a weekly history chart. It reads stats only for the listing's own registry (`registryEntry._registry`, official when absent), never for the install's source, so a dev install of an app still shows its listing's stats and a slug listed in two registries never borrows the other's numbers. A `null` count reads "hidden (small count)", never a number or a bound; an app whose counts are all `null` shows one line, "Too few installs to show", not a grid. Active is labelled "Active (reported, last week)": hosts with telemetry off, governed hosts, and members of an external registry who did not confirm its collector are not counted.

### Tenet 8

The send gate, the consent ladder and the external egress grant sit under the app line, beside `telemetry_permitted` and registry trust, and are core. The Usage section extends the built-in App Store pages and moves with them if those become an app.

## Migration plan

Each phase is one PR and can be abandoned without undoing the previous one.

### Phase 1: contract (docs only)

Extend `docs/system-specs/modules/metrics.md` with the presence route (§1), eligibility (§2), the schema additions and floor (§3) and the normalizer (§5), in a section marked proposed and not yet sent. The text that describes what is sent today stays unchanged until the send exists.

Exit criteria:
- No code changes; `scripts/docs_lint.py` passes.
- No user-facing or shipped-behaviour text changes: README.md, the existing metrics.md sections and the privacy copy still describe today's sends.

### Phase 2: official presence ping and `av` (blocked on decisions 0 and 1)

Exit criteria:
- One GET per eligible enabled official app per ISO week, each through `beacon.telemetry_permitted`; tests pin every suppression the receipt tests pin, plus `--test-mode`.
- No request names more than one slug. No presence send lands in the 30 minutes after a heartbeat send. A gateway up two hours a day reports every week in a simulated month, and one up less than its week's target sends nothing that week.
- A record with empty `sourceUrl`, `origin` other than `registry`, `dev: true`, a local-path `sourceUrl`, or a `sourceRegistry` naming an external registry sends nothing to the vendor endpoint.
- `p` for one app differs between consecutive weeks and from that app's receipt `t`.
- `av` is produced only by the shared normalizer.
- The disclosure copy in every locale names the presence ping, and the decision-1 gate holds the first presence send until it has been shown.
- The same PR updates every text the new send contradicts: README.md's telemetry section ("Exactly these five fields are sent, at most once per day", "Apps from user-configured registries ... emit nothing"), metrics.md "Default-ON with six suppressions" (and moves the Phase 1 section out of "proposed"), the governance.py comment calling the beacon and receipt "the repo's only default-on egress family", the `_SECRET_FILE` comment, the `security_posture.py` egress allowlist justification, and `privacyDisclosure.installReceiptBody` / `installReceiptFields` in all locales.
- `app_receipt_secret` is pinned as absent from snapshot and export selection by `TestSnapshotAndPortabilityRegistration`.

### Phase 3: Usage section (blocked on decisions 0 and 2, and on #17958)

Ships the parser with its first consumer. It reads the document #17958's `catalog_ranking.py` fetches, so it waits for #17958 to merge. If #17958 is closed instead, this phase first lands that document's schema (`schemaVersion`, `cumulativeInstalls`, `windowedInstalls`) as its own, and the floor applies from the start.

Exit criteria:
- With no stats for the listing's registry, the detail page renders as before.
- The parser rejects a wrong `schemaVersion`, a non-integer, boolean or negative count, a non-finite number, an oversized document, an over-long series, a bad version key, and a slug absent from the index, and shows no stats for that document.
- A `null` count is never rendered as a number or a bound ("fewer than N"), since complementary suppression can hide a count at or above the floor; the client applies a floor of at least 5 to a document declaring less.
- Stats come only from the listing's registry.

### Phase 4: external registries (blocked on decision 3)

Exit criteria:
- A collector URL set in `config.json` or proposed by an index sends nothing until the owner confirms it; the grant records the exact URL and a changed proposal is not honoured until reconfirmed.
- `capabilities.app_stats_external` pinned off stops all external presence and receipt sends.
- Install receipts for a granted registry go to its collector and never to the vendor endpoint.
- Stats are read from the grant's `statsUrl` and labelled "reported by <registry>".

The official collector and producer extend the `AppInstallations` rollup in the telemetry account, outside this repository. Phase 1 can merge before they exist, and Phase 3 once #17958 has; with no document, nothing renders.

## Backward compatibility

- The receipt route keeps its fields; `av` is additive.
- `/b/1/presence/` is a new route; an endpoint that does not know it returns an error the sender ignores.
- The ranking document gains fields and a `schemaVersion` bump; a client that reads only `cumulativeInstalls` and `windowedInstalls` keeps working, and sees `null` where a count is now below the floor.
- An install with telemetry off, a governed host, a legacy record, or an external registry without a grant sends exactly what it sends today.

## Security considerations

- **Linkability (official path).** Presence tokens are domain-separated from receipt tokens and rotate weekly, so a collector cannot join presence to install events or follow one app on one host for more than a week. Within a week, tokens from one host could still be grouped by co-occurring send times; independent per-app targets spread over 7 hours of uptime and the heartbeat exclusion window are the mitigation, and the stored `time` field is what remains. Raw rows are permanent and carry country.
- **Linkability (external path).** None beyond what an IP address gives: a third-party collector sees the host IP and can link every app from its registry on that host. The confirmation dialog says so.
- **Private app names.** A slug is sent only to the vendor endpoint for a record that matches the official catalog's repository, or to the confirmed collector of the registry that lists it.
- **Agent-driven egress.** Nothing the agent can write, directly or through a gateway-side config writer, turns on or redirects a send: presence ignores a `beacon_endpoint` override in `config.json`, and external grants live in `registry_trust.json`, which only the owner-only route writes. The control rests on write protection alone: the confirmed collector URL is not a secret.
- **Untrusted stats.** Parsed into bounded integers (§3), display only, labelled by source.
- **Spoofing.** Unauthenticated pings can inflate or deflate any app's counts; stats are advisory and never feed ranking.
- **Small populations.** Floor with complementary suppression, weekly granularity, and a client-side minimum floor. Cross-week differencing remains (decision 4).
- **Governance.** `capabilities.telemetry` pins the official presence ping off with the heartbeat and receipts; `capabilities.app_stats_external` pins external sends off.

## Alternatives considered

- **A daily ping reusing the receipt token.** Builds a permanent day-by-day presence bitmap per token; all tokens from one host share the same absent days as the heartbeat id, so they can be joined into an installed-app profile. Rejected for the rotating weekly token.
- **Derive active installs from update receipts.** An install that never updates sends nothing, and enabled state is invisible.
- **List installed apps in the heartbeat.** Links all of a host's apps and sends private names to the vendor endpoint.
- **A second stats document beside #17958's.** Two documents from one rollup would show different totals, and an unfloored ranking document defeats the floor.
- **Commit `app-stats.json` to the registry repo.** Needs a bot with push rights, collides with registries that publish by mirroring a branch, and the index fetch discards its clone.
- **A vendor-hosted collector for external registries**, keyed by `HMAC(registryStatsKey, slug)` with the key in the private index, so no plaintext private names leave the host. Removes per-team infrastructure; puts external registries' traffic on the vendor endpoint. Left open as [decision 3](#3-external-registries) option C.
- **Forge statistics (GitHub traffic, stars).** Counts clones and interest, not installs, enabled state or versions.

## Follow-ups

- A Discover "my apps" filter. "Mine" cannot come from the registry trust tier, which is a cloning posture, not authorship. A workable rule: a listing is mine when a local or dev install of the same name points at the listing's repository, or the listing's repository is in a "my repositories" list in Settings.
- Finer-grained, opt-in usage from #18761.

## Decisions needed

### 0. Owner of the official producer

The official half needs the `AppInstallations` rollup extended with presence (active, versions, weekly series) and the floor. #1440 was closed because that work has no owner. Phases 2 and 3 are blocked on a named owner; without one, Phase 2 would send data nothing reads.

### 1. Consent for the presence ping

The ping is a new recurring default-on send: one request per enabled official app per host per week, against one heartbeat per host per day today (Tenets 1 and 6).

- A. Same toggle; the disclosure gains a line; already-acknowledged users see a notice after the fact.
- **B (recommended).** Same toggle; the acknowledgement is versioned, and the first presence send waits until the updated disclosure has been shown. The heartbeat and receipt are not re-gated.
- C. A separate toggle, default off. Most conservative; most installs would never report, so active counts would be a small self-selected sample.

### 2. Who sees the stats

- **A (recommended).** Public aggregates under the floor, on every listing that has them.
- B. Builder-only, which needs each producer to authenticate builders (for example, proof of push access to the app's repository) and brings back a server with an identity model per registry.

### 3. External registries

- **A (recommended).** Off by default; the owner confirms one collector URL per registry (§2).
- B. External registries never report; stats are official-only.
- C. A vendor-hosted collector for external registries, keyed per registry so private names are never sent in plain text.

### 4. Floor, window and retention

Recommended for the official producer: floor 5 with complementary suppression, active window one ISO week, weekly series kept 104 weeks. Open: whether presence rows get their own log prefix with an expiry instead of the permanent raw logs, and whether weekly granularity is enough against cross-week differencing for small apps.

## Acceptance

Proposed 2026-10-10 by FlorentLa. Not yet accepted.
