---
title: Local preview origins accept a bounded loopback port range
status: draft
author: unstablebrainiac
created: 2026-11-20
last-audited: 2026-11-20
audited-at: 9539609a2c
doc-pr: 40061
implementation-prs: []
tracking-issues: [40036]
supersedes: []
superseded-by: []
---
# RFC: Local preview origins accept a bounded loopback port range

**Status:** draft. Nothing is on `main`. Acceptance requested from a maintainer;
the status flips to `accepted` when one records it here. This document lands first
because the change widens what one entry of `dashboard.browser_local_origins`
lets the agent's `browser` tool open, which CONTRIBUTING.md counts as moving a
trust boundary.
**Measured at:** `9539609a2c` (main, 2026-11-20). Every symbol cited below was
read at that commit.

## Summary

Settings -> Browser -> Local preview origins (`dashboard.browser_local_origins`)
gains one more entry shape: `host:LOW-HIGH`, for example `127.0.0.1:8760-8799`.
The host stays one of the three loopback spellings, LOW must be below HIGH, and
one entry spans at most 100 ports. Everything else about the list stays as it
is: at most 32 entries, the gateway's own port refused even when listed, the
list read fresh on every check, and any other shape dropped.

## Motivation

### Current state

An entry is exactly `host:port`. The rule lives in three places that must agree:

- `coerce_browser_local_origins` and `BROWSER_LOCAL_ORIGIN_PATTERN` in
  `src/kiro_crew/config/sections_dashboard_hosts.py` (lines 39 and 45). The
  loader keeps an entry only when its host is `localhost`, `127.0.0.1` or
  `[::1]` and its port is a plain number in 1-65535. The cap is
  `BROWSER_LOCAL_ORIGINS_MAX = 32` (line 32).
- The settings write: `_EDITABLE_CONFIG["dashboard.browser_local_origins"]` in
  `dashboard/handlers/core_config_schema.py` (line 664) is a `str_list` whose
  `item_pattern` is that pattern, applied in `dashboard/handlers/core_config_patch.py`
  (line 509).
- The match: `target_is_allowed_local` in `browser_harness/url_gate.py`
  (line 108) admits a URL only when `f"{host}:{port}"` is in the list (line 140).
  The shim (`mcp_tools/browser.py`, `_allowed_local_origins`) applies it to the
  URL asked for, and the gateway (`dashboard/handlers/browser_ops.py`,
  `committed_url_allowed`) applies it again to the URL a navigation committed on.
- The frontend mirror: `website/src/utils/localPreviewOrigins.ts`
  (`ORIGIN_RE`, `normalizeLocalOrigin`, `previewAllowsUrl`), used by the Settings
  chip editor (`pages/settings/BrowserLocalOrigins.tsx`) and the Preview tab
  (`components/WebPreviewPanel.tsx`).

The dashboard's CSP is not part of the rule: `_LOOPBACK_FRAME_SRC` in
`dashboard/server.py` (line 655) already admits `http(s)://127.0.0.1:*` and
`http(s)://localhost:*` for framing.

### Problem

A person who runs several preview servers at once, for example one per Bot, each
on the next free port of a known block, has to add every port as its own chip.
Until a port is listed, the `browser` tool refuses it and the Preview tab will
not frame it, so an unattended agent stops on the refusal until someone opens
Settings. A block wider than 32 ports cannot be listed at all.

## Goals

- One entry can name a contiguous block of loopback ports.
- The block is small and bounded, so no entry opens a broad slice of the
  machine's ports.
- Every reader of the list (loader, settings write, both navigate checks,
  Preview tab, Settings editor) applies the same rule, so no surface accepts what
  another refuses.

## Non-goals

- `*.localhost` host names. That is [#37893](https://github.com/kirodotdev/KiroCrew/issues/37893),
  which a maintainer parked on 2026-11-16 as a change to the shipped rule. This
  RFC keeps the three host spellings exactly as they are.
- A wildcard port (`localhost:*`) or a bare host (`localhost`).
- Raising the 32-entry cap.
- Any change to the public-URL gate (`target_is_public`) or to which browser runs.

## Design

### Entry shape

```
host:port         one port, as today
host:LOW-HIGH     LOW through HIGH, both ends included
```

- `host` is `localhost`, `127.0.0.1` or `[::1]`, compared byte for byte with the
  URL's host, as today. A range for `127.0.0.1` does not admit `localhost`.
- Each port is a plain ASCII number in 1-65535, rebuilt canonically
  (`08760` -> `8760`).
- `LOW < HIGH`. A one-port range (`8760-8760`) is refused, because that is the
  existing `host:port` form and two spellings of one entry would make duplicates
  hard to see. Open question 2 can change this to "stored as `host:port`".
- `HIGH - LOW + 1 <= 100`. The cap is a new constant,
  `BROWSER_LOCAL_ORIGIN_RANGE_MAX = 100`, next to `BROWSER_LOCAL_ORIGINS_MAX`.
- A reversed, over-wide, half-written (`8760-`) or non-loopback range is dropped
  by the loader and refused by the settings write, like any other bad entry.

### Why 100

The use case is a block of preview servers, one per Bot or per worktree. Forty is
a large crew; 100 leaves room without letting one entry approach the size of the
ephemeral range or a whole service band. The 32-entry cap stays, so the most the
list can ever open is 3,200 loopback ports. A maintainer may prefer a smaller
cap, and the constant makes that a one-line change (see Open questions).

### One rule, every reader

| Reader | Change |
|---|---|
| Loader, `coerce_browser_local_origins` | Parses both shapes through one helper that returns `(host, low, high)` or nothing. |
| Settings write, `core_config_patch.py` | `BROWSER_LOCAL_ORIGIN_PATTERN` admits an optional `-port`. A pattern cannot order the two ends or bound their distance, so the `str_list` spec gains an optional `item_check` callable: the entry is refused unless the loader would keep it exactly as written. Every write path runs the same check: `item_check` runs inside `_str_list_problem`, which both the settings PATCH and change-card writes (`change_cards_settings.check_setting_value`) call. |
| Navigate gate, `url_gate.target_is_allowed_local` | After the exact-match test, a URL is admitted when a range entry for the same host covers its port. The gate parses the entry with the same helper, so a range that skipped the loader (wider than 100, reversed) admits nothing. The gateway port refusal runs after the match, so a range that covers the gateway port still cannot open it. |
| Refusal text, `mcp_tools/browser.py` `_URL_GATE_TEXT` | Names the range form and the cap. |
| Frontend, `localPreviewOrigins.ts` | `normalizeLocalOrigin` and `previewAllowsUrl` mirror the same rule and cap. |
| Settings editor, `BrowserLocalOrigins.tsx` | A range is one chip; the invalid hint names the range form, in every shipped language. |
| Preview tab, `WebPreviewPanel.tsx` | The one-click origin buttons skip range entries, which name no single URL. The frame check still honours them. |
| Field help and docs | The `browser_local_origins` field help, `config-baseline.json`, `src/kiro_crew/docs/configuration.md` and the browser and config system specs describe the range form. |

## Migration plan

One implementation PR, after acceptance. Exit criteria, each a test:

- `127.0.0.1:8760-8799` is kept by the loader and saved by the settings write;
  `127.0.0.1:8760` and `127.0.0.1:8799` are admitted by the navigate gate and the
  Preview tab; `8759` and `8800` are refused.
- Reversed, 101-port and non-loopback ranges are dropped by the loader, refused
  by the settings write, by a change-card write and by the Settings editor, and
  admit nothing at the navigate gate even when handed to it directly.
- A one-port range (`8760-8760`) follows the answer to open question 2: refused
  everywhere the line above names while that answer is "refuse", or stored as
  `host:port` if a maintainer answers "accept".
- `127.0.0.1:8760-8799` does not admit `localhost:8770` or `[::1]:8770`, at the
  navigate gate or in the Preview tab's `previewAllowsUrl`.
- A range that covers the gateway's port does not admit the gateway's port.
- A range that overlaps the pod band is refused at every write path, and a
  navigate to a port an instance tunnel currently holds is refused.

An implementation exists on a local branch and has not been pushed; it waits for
this document's acceptance.

## Backward compatibility

Compatible. Every entry valid today keeps its meaning. A config written with a
range and read by an older version loses only that entry, because the older
loader drops any shape it does not know (fails closed). No migration.

## Security considerations

- **What widens.** One entry can now open up to 100 loopback ports to the agent's
  `browser` tool, which may be the person's logged-in Chrome. The person still
  writes every entry; the agent cannot, because `config.json` is read-only inside
  the sandbox and the refusal text tells the agent not to edit the setting.
- **What does not.** The host stays loopback-only and spelled exactly. The
  gateway's own port is refused even inside a range. Both navigate checks (asked
  URL and committed URL) apply the range rule, and the gate re-checks the bound,
  so a hand-written over-wide entry admits nothing. DNS rebinding stays the
  accepted residual it is today.
- **Residual: a range admits any listener in it.** A range opens whatever binds a
  port inside it later, not only the dev server the person meant. That includes
  another Kiro Crew gateway on the same host: pod gateways start above 7810 and
  Instances tunnels count up from 7778, and loopback cookies are shared across
  ports, so a logged-in browser could reach one with an owner session. Only this
  gateway's own port is refused by today's gate. The person takes on the rest of
  that when they list a range, which is why ranges are loopback only and capped
  at 100 ports. The example `8760-8799` is clear of both bands.
- **The implementation refuses the known Kiro Crew port bands.** A range that
  overlaps the pod band (`base_port + 1` through `base_port + 199`, 7811-8009 by
  default, from `pod/config.py` and `pod/runtime_ports.py`, honouring a
  configured `KIROCREW_POD_BASE_PORT`) is refused at every write path. The
  Instances tunnel allocator (`instances/port_allocator.py`) counts up from 7778
  with no upper bound, so no static band can be refused without blocking every
  port above it; the navigate gate instead refuses a port an instance tunnel
  currently holds.
- **Why not `*.localhost` or a wildcard.** A wildcard port opens every local
  service, databases and admin consoles included, which is what the exact-port
  rule exists to prevent. `*.localhost` changes which host names count as
  loopback, a separate question already parked in #37893.

## Alternatives considered

- **Raise the 32-entry cap.** Still one chip per port, and the person still has to
  add each port before the agent reaches it.
- **Wildcard port, `localhost:*`.** Rejected above.
- **A per-Bot preview port the gateway assigns and lists by itself.** Removes the
  person from the decision of what the agent's browser may open; a larger design.
- **Expand ranges into single entries in the loader.** One 40-port entry would use
  40 of the 32 slots, and the Settings view would no longer show what the person
  wrote.

## Related

- [#40036](https://github.com/kirodotdev/KiroCrew/issues/40036): the feature request this answers.
- [#37893](https://github.com/kirodotdev/KiroCrew/issues/37893): `*.localhost` hosts, parked; the precedent for treating this list's shape as a design decision.
- [#40038](https://github.com/kirodotdev/KiroCrew/issues/40038): a related browser trust question, letting the dashboard page check use Kiro's own sealed Chromium.

## Open questions

1. Is 100 the right cap, or should it be smaller (for example 50)?
2. Should a one-port range (`8760-8760`) be accepted and stored as `host:port`
   instead of refused?
