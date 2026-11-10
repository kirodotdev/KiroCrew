---
title: The six Google cards share one stored OAuth client
status: accepted
author: kirocrew agent session, directed by the repository owner
created: 2026-11-10
last-audited: 2026-11-10
audited-at: d4fdb01000
doc-pr: null
implementation-prs: [34290]
tracking-issues: [34291]
supersedes: []
superseded-by: []
---

# RFC: The six Google cards share one stored OAuth client

Status: accepted. The repository owner made this decision on 2026-11-10 in the
conductor chat: one Google app serves all six Google cards and is filled once.
It takes effect when this document merges; the code change
([#34290](https://github.com/kirodotdev/KiroCrew/pull/34290)) rebases onto the
base that carries it. Every code reference below was read at `d4fdb01000`.

This document lands on its own because it settles a question the Connections
spec leaves open ("a shared Google client card is a separate, not-yet-decided
change", `docs/system-specs/modules/connections.md`) and changes where a
client is stored, and the First Principles lane reads that decision from the
base branch.

## 1. Problem

The six Google entries in `src/kiro_crew/connections/registry.json` (Gmail,
Google Drive, Google Calendar, Google Sheets, Google Docs, Google Slides) are
ordinary pre-registered providers: one card and one stored client each
(`connections/oauth_clients.py` keys config, vault and environment names by
slug). Google lets one OAuth client carry many redirect URIs, so one user-owned
Google app serves all six. Today the person pastes the same Client ID and
secret six times.

## 2. Decision

- A registry `auth` block may carry `client_group`, a slug-shaped name. The six
  Google entries carry `"client_group": "google"`.
- `oauth_clients.client_record_key(provider)` is the group when set, else the
  slug. Every read and write of the client uses that key: config
  `connections.oauth_clients.google.client_id`, vault
  `CONNECTIONS_GOOGLE_CLIENT_SECRET`, environment
  `KIROCREW_CONNECTIONS_GOOGLE_CLIENT_ID` / `_CLIENT_SECRET`.
- `PUT` and `DELETE /api/connections/oauth-clients/{slug}` on any Google slug
  write or remove that one record, then refresh the runtime projection of
  every provider that shares it (withdraw each in-flight mint, scrub each
  scope copy, rebuild the agent spec once).
- `GET` reports `client_group` on each record, so a page can show the six as
  one Google entry. The route shape is otherwise unchanged.
- Providers without `client_group` (GitHub, Asana, ...) are untouched.

Compatibility: the six Google entries are launch-gated
(`launch_gate_passed: false`), so no released install relies on per-card
Google records. A per-card Google value saved or exported before this change
(`connections.oauth_clients.gmail`, `CONNECTIONS_GMAIL_CLIENT_SECRET`,
`KIROCREW_CONNECTIONS_GMAIL_CLIENT_ID`, ...) is no longer read; the person
enters the Google app once more.

## 3. Not decided here

- How the Settings page lays out the Google entry; that is a UI change of its
  own and reads `client_group` from `GET`.
- Grouping any other provider family.

## 4. Alternatives considered

- **Keep per-card storage and have the page send six PUTs.** Six writes, six
  vault entries and six env pairs that can drift apart; a container still sets
  six env pairs.
- **Fall back to per-card records when the group record is empty.** Lets a new
  group id pair with an old per-card secret, which Google rejects with
  `invalid_client`; launch-gated entries have no users to migrate.
- **A new `/api/connections/oauth-clients/google` route.** A second route for
  the same record; any Google slug already names it.
