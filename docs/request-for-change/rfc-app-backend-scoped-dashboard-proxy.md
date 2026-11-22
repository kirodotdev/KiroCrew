---
title: App backends lose the gateway secret; a scoped dashboard-proxy grant replaces it
status: draft
kind: decision
author: kirocrew-worker (drafted for maintainer decision)
created: 2026-11-22
last-audited: 2026-11-22
audited-at: a4a99c183b
doc-pr:
implementation-prs: [40783]
tracking-issues: []
supersedes: []
superseded-by: []
---

# RFC: App backends lose the gateway secret; a scoped dashboard-proxy grant replaces it

Status: draft. This document puts a decision on the base branch because the change it covers REMOVES a capability that the current spec describes. It asks maintainers to accept the removal, or to reject it and say what should ship instead. A maintainer accepts it by setting `status: accepted` when merging. The author cannot accept it.

- Implementation: [#40783](https://github.com/kirodotdev/KiroCrew/pull/40783) (draft).
- Related:
  - `docs/system-specs/modules/dashboard-token-auth.md`, section "App backends mint through the local secret";
  - `AGENTS.md`, the rule that a tightened credential rule sweeps the clients outside this repo;
  - [#38182](https://github.com/kirodotdev/KiroCrew/pull/38182) (the mask);
  - [#40690](https://github.com/kirodotdev/KiroCrew/pull/40690) (its revert).

## Problem

On main, a running app backend can read `.local_secret` and every `run/gateway-<port>.secret`. That value is the gateway's local-owner secret. With it, any installed app's backend can call `GET /api/token/local` and receive an unscoped owner token. That token can mint mobile links and webhook tokens and read the secret store.

#38182 hid the files from the backend sandbox. #40690 reverted that because Crew Tunnel (registry `Crew Tunnels`, package `PdebieKiroCrewTunnelDist`) reads the file to mint the token it proxies the dashboard with.

## The decision this asks for

A sandboxed running app backend reads none of the gateway secret files. An app that proxies the dashboard gets a scoped credential instead, under an owner grant `dashboard_proxy`:
- it is derived per app, per install and per gateway boot;
- the gateway accepts it only from that app's own spawned backend;
- the token it mints is refused on credential routes and control-plane writes, and on any untagged route;
- disabling the app ends it.

The design and its security read are in #40783.

## The removals, stated plainly

### Removal 1: an ungranted app backend can no longer read the gateway secret

Any app that reads `.local_secret` or `run/gateway-<port>.secret` today reads an empty file, and its `/api/token/local` call answers `403 invalid_secret`. To keep minting, the owner grants it `dashboard_proxy`. That needs a registry install, and the backend restarts.

### Removal 2: Crew Tunnel stops working on macOS until an API path exists

On a sandbox backend that cannot bind per-app content over a masked file (macOS Seatbelt), the grant is refused (`platform-unsupported`) and every mask is kept. Crew Tunnel on macOS therefore gets `403` at start, which is what #38182 did and what #40690 reverted.

The alternative was to leave macOS unmasked for granted apps. TALOS refused it: a granted app there would read the REAL secret and mint an unscoped owner token, so granting would make an app stronger than an ungranted one.

macOS comes back when Crew Tunnel moves to a dedicated mint route that delivers the scoped credential without a file (future work, not in #40783).

### Unchanged

- No-sandbox mode, where no boundary exists, behaves as main.
- Windows has no mask, so the finding stays open there and needs the owner's written risk acceptance before release.

## Questions for the maintainer

1. Accept Removal 1 and Removal 2 as stated, so #40783 can land after its gates?
2. Is the macOS break acceptable until the dedicated mint route exists? If not, the alternative is to hold #40783 until that route and a Crew Tunnel release ship.
