---
title: App usage metrics -- installs, updates, uninstalls and use per app and version, on closed labels, behind the existing consent gate
status: draft
author: chenmingwei23 (drafted with an agent session)
created: 2026-11-17
last-audited: 2026-11-18
audited-at: 5f20c7ae3a
doc-pr: 39107
implementation-prs: []
tracking-issues: []
supersedes: []
superseded-by: []
---

# RFC: App usage metrics

Status: draft. Nothing in this document is built. Every code reference was
read on `main` at `5f20c7ae3a` (2026-11-18). Two changes this document builds
on are already on `main`: the exported metric schema manifest and its
append-only CI gate ([#38172](https://github.com/kirodotdev/KiroCrew/pull/38172),
[#38250](https://github.com/kirodotdev/KiroCrew/pull/38250)), and the public app id
and `kirocrew.app.installed`
([#38157](https://github.com/kirodotdev/KiroCrew/pull/38157)). The privacy
line that named store apps
([#38347](https://github.com/kirodotdev/KiroCrew/pull/38347)) was removed by
[#39331](https://github.com/kirodotdev/KiroCrew/pull/39331); section 7 describes
`main` after that change.

The decisions marked **decided** below were made by the repository owner on
2026-11-17. One default, marked **decided by lead, owner may revert**, was set
by the lead and is listed again in [Open questions](#14-open-questions).

Related: [`rfc-crew-log-telemetry-export.md`](rfc-crew-log-telemetry-export.md)
(the consent gate and counting rules this reuses),
[`rfc-crew-api.md`](rfc-crew-api.md) (how numbers without usage data are
marked, and the remote connector id this does not count),
[`rfc-app-contribution-points.md`](rfc-app-contribution-points.md) (what an app
can carry), [`docs/guides/telemetry-otlp-export.md`](../guides/telemetry-otlp-export.md)
(the exported metric table and the deprecation rule), and
[#18855](https://github.com/kirodotdev/KiroCrew/pull/18855) by FlorentLa
(prior work on installs, active installs and version history; this document
borrows its small-count floor and version normalization ideas).

## 1. Problem and goals

Apps built on Kiro Crew now have real users. One internal app is used by about
500 teams. Its author cannot see how many machines have it, which version they
run, or whether anyone opens it. Today Crew sends one app metric,
`kirocrew.app.installed`, counted on first install only, with no version
(`record_app_installed` in `src/kiro_crew/metrics/feature_usage.py`, called
from `count_first_install` in `src/kiro_crew/apps/store_origin.py`). A second one,
`kirocrew.app.task.completed`, is sent only by the built-in Papyrus app and
never reaches the analytics store, because it is on the `kiro_crew` scope.

Goals, in order:

1. For each public app and each of its versions: how many machines install it,
   update to it, and uninstall it.
2. For each public app and version: how many sessions run under it, how long
   its agents run in total, how long people keep its page in the foreground on
   average, and how many machines use it per day and per week.
3. Keep the rules the metrics code already follows: every label comes from a
   closed set or a reviewed exemption, nothing leaves the machine without the
   notice that names it, and recording never changes the result of what it
   measures.
4. Leave a designed, unscheduled path for later waves: what an app carries
   (agents, skills, MCP servers), likes and shares (Kiro Space), and counts an
   app defines itself.

## 2. Non-goals

- **Kiro Space's half.** Downloads, ratings, likes, comments, store page views,
  review, Editor pick and Space's own daily active people are Kiro Space's
  (section 4). This RFC only names the ids both sides share.
- **Per-person, per-team or per-machine views.** An author sees aggregates over
  a minimum count (section 9). No list of who uses an app.
- **Private apps.** An app with no public id is counted as `private`, with no
  name and no version, as today.
- **Remote connectors from the Crew API** (`client_id`, `rfc-crew-api.md`).
  They are not local installs. If they are counted later, it is under their own
  `Feature`, never mixed into `Entity`.
- **Histograms and gauges for app metrics.** Durations are counters with a
  closed bucket attribute (section 6). A per-minute gauge is ruled out by
  volume (section 8).
- **Changing the consent gate.** The new rows go through the same gate as every
  product metric (section 7).

## 3. Prior art (short)

Rephrased from each source; content was rephrased for compliance with licensing
restrictions.

| Platform | What the author sees | What we take |
|---|---|---|
| [Chrome Web Store](https://developer.chrome.com/docs/webstore/metrics) | Daily installs and uninstalls; weekly users split by item version. Users reflect installs, not activity. | "Installed" and "used" are separate numbers. Version split is standard. |
| [Google Play Console](https://support.google.com/googleplay/android-developer/answer/139628) | Device acquisitions, updates, losses; DAU = opened that day; MAU = rolling 28 days; usage time = foreground only. | Active needs an open or a session. Foreground time is its own number. |
| [App Store Connect](https://developer.apple.com/help/app-store-connect-analytics/engagement/app-usage) | Installations, sessions, active devices, deletions, by app version. | Same split. |
| [App Store analytics privacy](https://developer.apple.com/documentation/analytics-reports/privacy) | Values under 5 users or devices are left out. | A minimum count before any number is shown. |
| [Shopify App Events](https://shopify.dev/docs/apps/build/app-events) | Apps send their own events; a per-app request limit; no personal data in attributes. | Custom counts are a later wave, declared, rate limited, no free text. |
| [VS Code extension telemetry](https://code.visualstudio.com/api/extension-guides/telemetry) | Extensions must honour the global telemetry switch. | One switch stops app counts too. |
| [JetBrains Marketplace](https://plugins.jetbrains.com/docs/marketplace/analytics-tab.html) | Downloads only; no usage. | Downloads are not installs. Crew counts installs; Space counts downloads. |
| [GA4 automatic events](https://support.google.com/analytics/answer/9234069) | Engagement time counts foreground only, with a 1 second minimum. | Foreground spans under 1 second are dropped. |

Kiro Space's own draft design for listing activity (KiroSpace #3790, not
public) shows a count only when at least 3 people are in it, and counts only
people using a listing by hand, not agents or schedules.

## 4. Ownership: Kiro Space and Crew, and the ids they share

Kiro Space is becoming the registry behind the Crew App Store
([#38064](https://github.com/kirodotdev/KiroCrew/pull/38064), open at
`982ec66f92`). Space can only see calls made to Space. Crew can only see what
happens on the machine.

| Kiro Space owns | Crew owns (this RFC) |
|---|---|
| Store page views, downloads, ratings, likes, comments, shares | Installs, updates, uninstalls on the machine |
| Daily active **people** (by hand, opted in, 3-person minimum) | Daily and weekly active **machines** (any open or app session) |
| Review and Editor pick | Sessions, agent run time and foreground time under an app |
| The creator page | The metric rows, their labels and the consent line |

The two active numbers are different and are shown under different names:
Space's counts people who used the listing themselves; Crew's counts machines
where the app was opened or ran a session, including sessions an agent or a
cron started.

Shared ids:

| Thing | Kiro Space | Crew (`Entity` / `entity_version`) |
|---|---|---|
| App | listing `<namespace>/<name>` | Built-in: its `app.json` `name`. App Store registry install: its store name. **Kiro Space install: `space:<namespace>/<name>`** (decided by lead, owner may revert). Anything else: `private`. Not a string: `other`. |
| Version | the listing version; Space's publish design requires semver equal to `app.json` `version` | `X.Y.Z` from the install record or `app.json`; anything else `other`; private apps `private` |
| Person | Space's own person key | Not shared. Crew sends a machine id, never a Space person key; Space never receives a Crew machine id. |

The `space:` prefix keeps a Space name from colliding with a built-in or
registry name of the same text, and it is the same string an author already
sees in Space. Today `public_app_ids()` (`src/kiro_crew/apps/store_origin.py`)
treats only `registry` origins as public, so a Space install would be
`private`; PR 8 adds the Space case after #38064 merges.

## 5. Decisions

1. **Retire `kirocrew.app.task.completed`** (decided). Papyrus is its only
   caller (`src/kiro_crew/apps/builtins/papyrus/backend/routes.py`). Stop
   recording it and keep its manifest row with `"status": "deprecated"`
   (section 10).
2. **Wave 1, per app per version** (decided): installs, updates, uninstalls;
   sessions under the app, total run time, average foreground time, daily and
   weekly active machines. Durations are counters with closed bucket
   attributes, no histograms.
3. **Later waves** (decided, not scheduled): agents, skills and MCP server
   counts per app; likes and shares on the Space side; custom app counts with a
   per-app token bucket and a per-machine cap, where overflow is dropped and
   counted (section 12).
4. **Kiro Space installs are labelled `space:<namespace>/<name>`**, the same id
   as the Space listing, not `private` (decided by lead, owner may revert;
   [Open question 1](#14-open-questions)).

## 6. Metric definitions

Every row is a `counter` on scope `kiro.crew` (`BEHAVIORAL_SCOPE`,
`src/kiro_crew/metrics/behavioral_metrics.py`), so the downstream analytics
collector admits it. Every row has the shape `("app", None, "app")` in
`_SHAPES`: `Feature = app`, `Activity` = the last segment of the name, `Entity`
= the public app id. Each row has a different `Activity`, so no two rows have
the same attribute set and the collector never folds two into one event.
Temporality is `delta`, like `kirocrew.app.installed`.

### 6.1 Labels

`Entity` is `app_public_id()` (`feature_usage.py`), extended by PR 8 with
`space:<namespace>/<name>`.

`entity_version` is a new function `app_public_version()` next to it. It
returns the version only for an app with a public id, and only when it matches
`^\d{1,4}\.\d{1,4}\.\d{1,4}$`; a pre-release or build suffix is `other`, because
`SEMVER_RE` (`src/kiro_crew/apps/manifest_names.py`) lets any text follow
`-` or `+`. A private app is always `private`, so its version never leaves the
machine. The key is not `version`, because the collector maps a top-level
`version` onto the Crew build's own version; and not `app_version`, which is
that same native field's name downstream. `public_app_ids()` returns a name to
version map in the same scan, swapped in one assignment, so recording reads
memory only.

The duration bucket key `duration` uses the `le_` / `gt_` spelling of
`DURATION_BUCKETS` in `src/kiro_crew/metrics/work.py`:

`le_10s`, `le_1m`, `le_5m`, `le_15m`, `le_1h`, `le_4h`, `gt_4h`, `other` (8 values).

The boundaries are proposals without usage data behind them. Their basis: the
1 second and 2 second minimums in GA4 and App Store Connect at the bottom,
5-minute steps in the analytics store's own duration charts in the middle, and
the existing `le_1h` / `le_4h` / `gt_4h` from `work.py` at the top. They are
revisited after two weeks of data.

### 6.2 Rows

| Name | Unit | When one is counted | Attributes beyond `Feature` / `Activity` / `Entity` | Combinations per (app, version) |
|---|---|---|---|---|
| `kirocrew.app.installed` (exists, adds a key) | `1` | First successful install (both `count_first_install` calls in `install_from_registry`, `src/kiro_crew/apps/registry_pipeline/install.py`) | `via` {`default`, `other`, `store`}; **new** `entity_version` | 3 |
| `kirocrew.app.updated` (new) | `1` | The same two sites when `was_installed` is true | `via` (3); `entity_version` = the new version | 3 |
| `kirocrew.app.uninstalled` (new) | `1` | `uninstall_app()` succeeds (`src/kiro_crew/apps/manager_uninstall.py`); id and version are read **before** the record is removed. A `retired_builtin` cleanup is not counted: Crew removed it, not the person. | `entity_version` = the removed version; `keep_data` {`false`, `true`} | 2 |
| `kirocrew.app.present` (new) | `1` | Once per installed app per UTC day, from a background task beside the heartbeat's daily stamp (`src/kiro_crew/beacon.py`), 10 minutes after gateway start, over `list_apps()` (`src/kiro_crew/apps/manager_lookup.py`). Never on a request path. | `entity_version`; `status` {`disabled`, `enabled`, `other`}; `backend` {`none`, `other`, `running`, `stopped`} | 12 |
| `kirocrew.app.opened` (new) | `1` | The app's page mounts (`page`, `popout`), or `openCommand` launches (`desktop`) | `entity_version`; `surface` {`desktop`, `other`, `page`, `popout`} | 4 |
| `kirocrew.app.session_started` (new) | `1` | After `record_session_started` (`src/kiro_crew/metrics/sessions.py`) counts `kirocrew.session.started`, when the provider's trusted `owning_app` is set (`_get_or_create_impl` in `src/kiro_crew/session_allocation_parts/get_or_create.py`) | `entity_version`; `kind` = the 9 `session_kind` values in the manifest (`background`, `channel`, `chat`, `cron`, `heartbeat`, `other`, `subagent`, `task_runner`, `workflow_step`) | 9 |
| `kirocrew.app.run_time` (new) | `s` | Each finished turn in a session whose `owning_app` is set; value = the turn's wall seconds, rounded up; a turn with no duration adds nothing (a 0 would read as a measured instant turn, `turn_completed.py`) | `entity_version`; `duration` (8), the bucket of that turn | 8 |
| `kirocrew.app.foreground_time` (new) | `s` | Each span the app page is visible (Page Visibility), sent when it ends (hidden, unmount, `pagehide`); value = whole seconds; spans under 1 s are dropped; the server clamps a span to 24 h | `entity_version`; `surface` {`other`, `page`, `popout`}; `duration` (8) | 24 |
| **Total** | | | | **65** |

Sources of the trusted app name: `owning_app` is set by Crew from the caller's
slot app, never from transcript metadata (the `Trusted:` comment on `provider.owning_app` in `_get_or_create_impl`), and a
sub-agent of an app session carries the same `owning_app`. So
`app.session_started` is never more than `session.started`, and app run time is
never more than all turn time.

`run_time` is fed from `emit_turn_duration` (`src/kiro_crew/metrics/turns.py`),
the one boundary both turn callers share (`_emit_turn_metric` in `dashboard/chat_turn/turn_stats.py`
and `_emit_turn_histogram` in `dashboard/handlers/usage.py`). It gains an `owning_app` argument that
each caller passes from the live provider; PR 5 checks both callers have it in
scope.

`opened` and `foreground_time` come from the browser through one new
owner-only route, `POST /api/telemetry/app-view`, copied from
`dashboard/handlers/telemetry_ui_control.py` (registered at
`dashboard/routes/system.py` as `/api/telemetry/ui-control`): `require_owner_dashboard_request`, a 256-byte
bounded JSON body, `204`. The body is `{"app": <name>, "surface": <value>,
"event": "opened" | "foreground", "seconds": <int>}`. The server resolves the
public id and version itself; it never takes a version from the page. The page
sends a foreground span with `fetch(..., {keepalive: true})` so it survives
unload. The route opens nothing new to the network; it sits behind the same
owner gate as `ui-control`.

### 6.3 Derived numbers (no new rows)

| Number | How |
|---|---|
| Installed machines per version | distinct machines with `present` that UTC day, by `entity_version` |
| Daily active machines | distinct machines with at least one `opened` or `session_started` for the app that UTC day |
| Weekly active machines | the same over a rolling 7 days |
| Total run time | sum of `run_time` values, read in the metrics store, which keeps values |
| Average foreground time | sum of `foreground_time` values / daily (or weekly) active machines with an `opened` |
| Version adoption | share of `present` machines on the newest version |

The analytics store turns one record into one event and drops the value: five
adds in one minute are one event. So in the analytics store `run_time` and
`foreground_time` give the bucket mix and the machines; the seconds are summed
in the metrics store behind the telemetry endpoint. A duration is a bucket, so
neither store gives an exact mean per turn or a p95. That is the cost of
counters with buckets, and the dashboard says so.

### 6.4 Cardinality

Counted from the enums on `main` at `5f20c7ae3a`: `via` 3, `keep_data` 2,
`status` 3, `backend` 4, `surface` 4 (3 for foreground), `session_kind` 9,
`duration` 8.

Per (app, version): 3 + 3 + 2 + 12 + 4 + 9 + 8 + 24 = **65** series.

`Entity` values: 21 built-ins (`src/kiro_crew/apps/builtins/*/app.json`), plus
public store and Space names, plus `private` and `other`. `entity_version`
values per app: the versions it has shipped, or `private` / `other`.

Fleet bound: (public apps x versions in use x 65) + 2 x 65. With 35 public apps
and 20 versions each (an assumption, not a count; the store catalogue is
remote and was not read) that is about 45,600 series.

One machine in one minute: at most 25 apps x (4 + 9 + 8 + 24) = 1,125 series,
and only if every app was opened, ran every session kind, ended a turn in every
bucket and a foreground span in every bucket in that minute. The daily rows
add one per app per day.

## 7. Privacy and consent

Since [#39331](https://github.com/kirodotdev/KiroCrew/pull/39331)
("default-on usage data, no first-run step"), `main` works like this:

- Local recording is on by default (`telemetry.enabled`,
  `src/kiro_crew/config/sections_telemetry.py`).
- While recording is on, the built-in Kiro telemetry destination exports.
  Nothing waits for a notice acknowledgement: `exported()` in
  `src/kiro_crew/metrics/kiro_export.py` keeps the built-in destination
  whenever recording is on. Operator destinations still need a written opt-in
  (`outbound_consented` in `src/kiro_crew/metrics/consent_pins.py`).
- Every opt-out still holds and stops app rows too: `telemetry.enabled: false`,
  `kirocrew telemetry disable`, `KIROCREW_TELEMETRY=0`, `DO_NOT_TRACK`, a
  config or env pin, and a policy `capabilities.telemetry` pin or `otlp_hosts`
  list that refuses the host. An LTS build and a CI runner have no Kiro
  destination at all. The daily heartbeat and install receipts have their own
  switch, `telemetry.beacon_enabled: false` (`src/kiro_crew/beacon.py`); it
  does not carry app rows.
- The privacy chapter says only that, while Record metrics is on, a reviewed
  list of product usage metrics is sent (key `kiroTelemetryBody` in
  `website/src/i18n/locales/en.manual.json`). It no longer lists apps.

The new rows in this RFC go through the same switch as every product metric.
This RFC adds no gate of its own. Whether the new app rows need their own
disclosure is for the owner to decide ([Open question 2](#14-open-questions)).

Label rules, from the review of
[#21288](https://github.com/kirodotdev/KiroCrew/pull/21288) and
[#22180](https://github.com/kirodotdev/KiroCrew/pull/22180):

| Rule | How wave 1 meets it |
|---|---|
| Every label from a closed set | `via`, `keep_data`, `status`, `backend`, `surface`, `kind`, `duration` are closed and listed in the manifest. `Entity` and `entity_version` are open in the manifest (`{}`), with the exemption already granted to `kirocrew.app.installed`'s `Entity`: values come only from apps Crew ships, apps a store published and their published versions, computed server-side; everything else is `private` or `other`. |
| Cardinality checked against `main`'s enums | Section 6.4. |
| Recording never changes the call's outcome | Every recorder call is wrapped and logs at debug only, as `feature_usage.py` already states ("Recording never changes the result of what it measures"). Each PR has one test where the recorder raises and the measured action still completes. |
| Unknown is null, never a made-up zero | A point from an older build has no `entity_version`; readers treat it as unknown, never as a version. A turn with no duration adds no `run_time`. |
| No default egress without consent | The new rows follow `main`'s rule: they leave the machine only while recording is on, and every opt-out above stops them. |

Never an attribute: free text, user input, paths, URLs, session ids, error
text, tool or command names, team names.

**Minimum count.** Any number shown to an app author covers at least 3 distinct
machines; a smaller cell shows "<3". 3 is Kiro Space's threshold for its own
active people, so one creator page has one rule. App Store Connect uses 5; see
[Open question 8](#14-open-questions).

## 8. Data path and volume

Path: the recorder on the machine -> local JSONL, and, when consented, an OTLP
POST to `https://telemetry.us-east-1.kiro.dev/v1/metrics` once per export
interval (`export_interval_seconds`, default 60) -> the metrics store, which
keeps values -> for the `kiro.crew` scope only, the downstream analytics
collector, which keeps a reviewed list of attribute keys.

Figures below come from a read-only measurement of that pipeline on
2026-11-17, 10:00-11:00Z (not a peak hour; the 24-hour peak was about 1.6
times higher).

| Fact | Value |
|---|---|
| Crew identities seen in a 10-minute sample | 17,690 signed-in, 23 open-source machines |
| Requests from Crew per identity | about 1 POST per minute; the endpoint's per-identity limit is about 3,700 times higher |
| Events the collector sends to the analytics store | about 221,000 per minute |
| `kiro_crew` scope points if all were admitted | counters 1.9%, gauges 98.1%; counters alone would add +3.2%, all would add +167% |
| Analytics-store throttling | 429s that exhaust retries on 6 of the last 14 days, mostly near 11:35Z, at most 60,137 events lost in one day (2026-11-13); one throttled device fails a whole batch of 2,000 |
| The analytics vendor's published per-device limits | 1,000 events per second, 500,000 per rolling 24 hours |

The new rows ride in the existing per-minute POST: no new requests. What
matters is analytics events, one per (machine, attribute set, minute) with a
change.

| Row | Fleet events, estimate | Basis |
|---|---|---|
| `installed`, `updated`, `uninstalled` | negligible | only on lifecycle actions |
| `present` | about 307 / minute | 17,700 identities x 25 apps, once a day |
| `opened` | about 246 / minute | 20 opens per identity per day, each in its own minute |
| `foreground_time` | about 246 / minute | one span end per open |
| `session_started` | about 1,770 / minute | 10% of identities start an app session every minute |
| `run_time` | about 1,770 / minute | 10% of identities end an app turn every minute |
| **Total** | **about 4,340 / minute, +2.0%** | below the +3.2% of counters-only that the measurement called low risk |

"25 apps", "20 opens" and "10%" are assumptions chosen on the high side, not
measurements.

Why counters first: an observable gauge exports a point every minute, which is
1,440 events per machine per app per day; at 17,700 identities x 25 apps that
is about 640 million a day, twice what the analytics store receives today. A
daily counter (`present`) answers "how many machines have it" with one event.

Keys the collector does not keep yet: `entity_version` and `duration` for
sure; `keep_data` was not checked. Without them the events still arrive, minus
that key; the metrics store keeps every key. Adding them is one change in the
telemetry team's repository, requested after PR 3 merges.

If app rows ever push the analytics store into throttling, the collector can
drop them by name prefix `kirocrew.app.` without a Crew release.

## 9. Dashboard for app authors

| Question | Answer |
|---|---|
| Who sees an app | The publishers of its Kiro Space listing (the owner or owner group Space records) see that app only. Built-in apps: Crew maintainers. Private apps never appear. |
| What they see | Section 6.3 numbers per day, split by version; Space's downloads, ratings and active people beside them, under their own names. |
| Minimum | Every cell covers at least 3 machines, else "<3". |
| Granularity | UTC days, updated daily. |
| Not shown | Which machines, people or teams. Locale or region. |
| Where, now | A per-app board in the analytics store, owned by Crew maintainers, who share it with an app's publishers. |
| Where, later | Kiro Space's creator page. Space's design rules out reading client telemetry from that page, so a daily job pushes Crew's per-app aggregates (already thresholded) to Space. That job is a later wave and needs Space's agreement. |

## 10. Append-only and deprecation

The manifest (`src/kiro_crew/metrics/schema_manifest.json`, 73 rows on `main`)
only grows. `is_additive` (`src/kiro_crew/metrics/schema_manifest.py`) allows
new rows, keys and values, and active -> deprecated; `kind`, `unit`, `scope`
and `temporality` never change (`FIXED_FIELDS`). CI runs
`scripts/check_metrics_schema_history.py` against the merge base.

Everything in wave 1 is a new row or a new key on `kirocrew.app.installed`.
Summing `installed` over `entity_version` gives the old total.

Retiring `kirocrew.app.task.completed`, by the rule in
`docs/guides/telemetry-otlp-export.md` ("To retire something"):

- Set the row's `"status"` to `"deprecated"` by hand. The name is never reused.
- Remove it from `PRODUCT_METRICS` (`src/kiro_crew/metrics/product_metrics.py`),
  since active rows must equal that set.
- Remove the Papyrus import and call, and the code that exists only for it in
  `feature_usage.py`: `APP_TASK_COMPLETED`, `OFFICIAL_APP_IDS`,
  `APP_TASK_OUTCOME_VALUES`, `app_label`, `record_app_task`. `APP_PRIVATE` stays;
  `app_public_id` uses it.
- Update the export table, `docs/system-specs/modules/metrics.md` and
  `docs/system-specs/modules/papyrus.md`.

Older Crew builds keep sending it for a while; it falls to zero as they
upgrade. Nothing reads it downstream: it is on `kiro_crew`.

Every PR that adds a name changes four places together: `_SHAPES`
(`behavioral_metrics.py`), `PRODUCT_METRICS`, the manifest (regenerated with
`python test/metrics_schema_helpers.py --write`), and the export table in
`docs/guides/telemetry-otlp-export.md`.

## 11. Rollout

Smallest first. PR 1 waits for nothing; PRs 3-7 do not wait for each other;
PR 8 waits for #38064.

| PR | What | Done when |
|---|---|---|
| 1 | Retire `kirocrew.app.task.completed` (section 10) | Row present with `status: deprecated`; both schema gates pass; `git grep record_app_task` finds nothing; Papyrus's route test asserts nothing is recorded |
| 2 | Dropped. It was a per-row notice floor; `main` has had no acknowledgement gate since #39331, and this RFC adds none (section 7, Open question 2) | - |
| 3 | `app_public_version()`; `installed` gains `entity_version`; new `updated`, `uninstalled` | `1.2.3` kept, `1.2.3-beta` is `other`, private is `private`; an update counts one `updated` with the new version; an uninstall counts once with the removed version; a raising recorder leaves install and uninstall results unchanged |
| 4 | `session_started` | A provider with `owning_app` adds one with the right `Entity`, version and `kind`; none without; `session.started` still counted when the app count raises |
| 5 | `run_time` through `emit_turn_duration` | Both turn callers pass `owning_app`; seconds and bucket edges (10 s / 11 s, 4 h / 4 h + 1 s) tested; a turn with no duration adds nothing |
| 6 | `present` daily task | Once per UTC day per installed app across restarts; `status=disabled` for a disabled app; three `backend` states; no record when telemetry is off |
| 7 | `POST /api/telemetry/app-view`, `opened`, `foreground_time` | Non-owner refused; body over 256 bytes refused; unknown name is `private`; one mount sends one `opened`, a re-render none; a hidden page sends one span; a span under 1 s sends nothing (one `vitest run` file) |
| 8 | Space installs labelled `space:<namespace>/<name>` | An install from Kiro Space records the listing ref; `public_app_ids()` returns the prefixed id; a registry app of the same name is not affected |

Outside this repository: the collector key list gains `entity_version`,
`duration` and `keep_data` (one change, after PR 3); the analytics-store board
(after PR 7).

Tests in every PR run only the touched files, with a fixed `-n 2` at most.

## 12. Later waves (designed, not scheduled)

| Wave | What | Notes |
|---|---|---|
| L1 | What an app carries: `present` gains `agents`, `skills`, `mcp_servers`, each bucketed `0`, `1`, `2`, `3-5`, `6-10`, `11+` | Read the declared counts from the manifest (`agents`, `skills`, `mcpServers`), not registration results, so one version has one combination. The brief's "bots" is counted as `agents`: in Crew an app agent is not a Bot (`rfc-crew-api.md`), so the key `bots` is kept for the Crew API's Bot templates. |
| L2 | Store origin and the app's declared metric list move into `member-records/` | Today the install record is writable from the app's sandbox (`store_origin.py` says so). Same direction as #34177 and #32983. Needed before L4. |
| L3 | Likes and shares | Kiro Space's. Crew adds `via=share` to `installed` when an install came from a Space share link, one new value. |
| L4 | Custom counts: one row `kirocrew.app.custom`, shape `("app_custom", "name", "app")`; names and `kind`s declared in `app.json` `metrics` (at most 20 names, 10 kinds each); value 1-1,000 | Only counters. Private, sideloaded and dev-mode apps record only to a local-only `kirocrew.app.custom.local`. |
| L4 limits | Per-app token bucket 20/s, burst 200; per machine 100/s, burst 1,000; at most 10 new (name, kind) series per app per minute and 40 per machine | Shape copied from `AppRateLimiter` (`src/kiro_crew/notifications/rate_limit.py`, a per-app bucket whose stated purpose is stopping a buggy loop) and the in-memory window of `src/kiro_crew/dashboard/create_rate_limit.py` (its module docstring: a restart buys the caller one window, so no durable state). The numbers are proposals with no usage data behind them; their one basis is that 40 series per minute is 57,600 events per day, under a tenth of the vendor's 500,000 per device per day. |
| L4 overflow | Drop the count, never raise, never change the app's result; count `kirocrew.app.custom.dropped` with `reason` {`invalid`, `other`, `rate`, `series`, `undeclared`} | The HTTP route answers `202 {"accepted": n, "dropped": m}`, not the Crew API's 429: a metric must not fail the app's action, and SDKs retry 429 by themselves, which only lengthens the overflow. |
| L5 | `POST /api/apps/self/metrics`, the Python client and the browser SDK helper | The app name comes only from the app token, never the body. |
| L6 | Report Crew's daily per-app aggregates to Kiro Space; report Space listing activity on `opened`, at most once per app per UTC day, off by default behind its own switch | Waits for KiroSpace #3790 and its open question on who calls it. |

## 13. Alternatives considered

- **A per-minute observable gauge for "running".** Rejected on volume (section 8).
- **Counting backend starts as "running".** Counts restarts, not machines; 4 of
  21 built-ins have no backend and would read zero.
- **Histograms for durations.** The export allowlist takes counters and gauges
  only (`kirocrew.turn.completed.duration` is held for that reason,
  `product_metrics.py`), and the rule on `main` is that a duration is a counter
  with a closed bucket, never a new histogram (`Metric` in
  `src/kiro_crew/metrics/crew_log_export.py`; `src/kiro_crew/metrics/work.py`).
- **Counting `opened` on the server when it serves the app's UI files.** The
  browser caches the entry module, so the server would miss opens.
- **Using the crew log's `session/opened`.** It is written on the first turn, and
  an app's background agent session does not always take that path.
- **Labelling Space installs `private`.** Every Space listing needs sign-in, so
  a "public listing only" rule would label all of them private and their
  authors would see nothing. Kept as the revert path for Open question 1.
- **A notice acknowledgement gate for the new rows.** Not proposed. Since
  #39331, `main` sends product metrics while recording is on and waits for no
  acknowledgement; this RFC keeps that rule.

## 14. Open questions

Each has a recommended answer. None blocks PR 1.

| # | Question | Recommended answer |
|---|---|---|
| 1 | Should Kiro Space installs send `space:<namespace>/<name>` or `private`? | **Decided by lead, owner may revert:** `space:<namespace>/<name>`, the listing's own id. Revert options: `private` for all, or only listings that passed review and that every verified internal user can see. External and LTS builds send nothing either way. |
| 2 | Do the new app rows need their own disclosure? | For the owner to decide. Today they would follow `main`'s rule since #39331: sent while recording is on, no acknowledgement, every opt-out stops them (section 7). This RFC proposes no new gate. |
| 3 | Does a session reopened after compaction count as a new app session? | Yes, the same as `kirocrew.session.started`, so the two counts match. |
| 4 | Is `X.Y.Z` only, with pre-releases as `other`, enough? | Yes. Store releases are `X.Y.Z`; a free suffix would make the label an open string. |
| 5 | The analytics store keeps machine-minutes, not totals. Enough? | Yes for active machines and retention. Totals (run time, foreground seconds) are read from the metrics store. |
| 6 | Several data homes on one machine (a pod, an internal gateway) each count `present`. | Accept. Each data home is its own install with its own machine id. |
| 7 | Do sub-agent turns of an app session count toward its run time? | Yes. They carry the app's `owning_app`, and their time is the app's work. |
| 8 | Minimum count: 3 or 5? | 3, the same as Kiro Space, so one creator page has one rule. |
| 9 | Who owns the author board, and when does it move to Space? | Crew maintainers own the analytics-store board now; move to Space's creator page in L6, with Space's agreement. |
| 10 | Do the new keys at the collector need their own privacy review? | Ask in the same request that adds the keys, so it is reviewed once. Owner decides. |
| 11 | Who tunes the later-wave starting values? | Code constants, not settings. Revisit two weeks after L4 ships, from `.dropped` and the measured volume. |
| 12 | Should the store page list the counts an app declares (L4)? | Yes. That belongs to the store, and does not block any PR here. |

Not checked for this draft: how many apps the store catalogue lists today;
whether `keep_data` is already on the collector's key list;
whether the analytics store can limit a board to one app's publishers; how
#38064 records a Space listing's namespace and name in the install record.
