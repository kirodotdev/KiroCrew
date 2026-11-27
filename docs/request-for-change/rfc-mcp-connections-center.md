---
title: MCP Connections Center — credentials, last error and fix hints, and a curated list on the MCP Servers tab
status: accepted
author: RohanK6
created: 2026-11-19
last-audited: 2026-11-24
audited-at: 3d36b459fd
doc-pr: null
implementation-prs: []
tracking-issues: [38054]
supersedes: []
superseded-by: []
---

# RFC: MCP Connections Center

- Status: accepted. Maintainer bolichen97 (Bolin Chen) accepted it in full on
  2026-11-25 in [the sign-off on this PR](https://github.com/kirodotdev/KiroCrew/pull/39645#pullrequestreview-6063857798), stage 2b
  included; the implementation PRs still go through the usual security review.
  Nothing built; this document changes no code. It is the
  request-for-change that the automated triage note on
  [#38054](https://github.com/kirodotdev/KiroCrew/issues/38054) points to; that
  note also says the issue is not on the current plan, and no maintainer has
  asked for these stages before this proposal.
- Author: RohanK6
- Claims about today's code were checked at `3d36b459fd` (main).

## 1. Summary

Stage 1, a per-row **Test** button on Customize → MCP Servers, shipped in
[#38055](https://github.com/kirodotdev/KiroCrew/pull/38055). This RFC proposes the
three stages after it, each its own pull request:

| Stage | What a person gets | Moves a boundary? |
|---|---|---|
| 2 | Enter a server's key on its row, store it in the vault, test it | Yes: a browser write of a secret, and a probe that resolves one |
| 3 | The last error of each server, with a short fix hint | No |
| 4 | A curated, ordered "popular servers" list in Add Server | No |

Stage 2 is the reason this is an RFC: it changes where a secret is written
from and who may resolve it.

## 2. Motivation: what main does today

Every claim below was checked at `3d36b459fd`.

- **Keys are named, not entered.** `McpBrowserModal` lists a discovered
  server's required env names after install
  (`website/src/components/McpBrowserModal.tsx`, `requiredEnv`). It has no
  field to type a value, so the person edits a config file by hand.
- **The vault and its write route exist.** `SecretVault`
  (`src/kiro_crew/secrets/vault.py`) is an encrypted per-entry store.
  `POST /api/secrets` writes it, and its handler `api_secrets_set` is
  owner-only through `_owner_only`
  (`src/kiro_crew/dashboard/handlers/secrets.py`).
- **A server env can point at the vault.** A `secret://NAME` value is resolved
  in memory at spawn time, and the file keeps only the reference
  (`src/kiro_crew/mcp_gateway/secret_uri.py`, module docstring). Only the
  gateway resolves it, and only for a launch the owner approved: a stubbed
  server's backend runs outside the session sandbox, and its command, args and
  env come from files the agent can write. gatewayd forwards declared env and
  spawns a backend only when the stored approval admits that exact launch
  (`_declared_env_launch_approved` and `_launch_approved_from_snapshot` in
  `src/kiro_crew/mcp_gateway/daemon/launch.py`, which call
  `launch_approved` in `src/kiro_crew/mcp_gateway/launch_approval.py`), and
  resolves `secret://` values after that check (`resolve_secret_uris` in
  `src/kiro_crew/mcp_gateway/gatewayd.py`). The rewriter wraps an entry only
  when the same approval admits it (`_admit_launch` in
  `src/kiro_crew/mcp_gateway/rewriter_spec.py`). The approval is a hash of the
  command, args and declared env text (`launch_fingerprint` and
  `env_fingerprint`), so a changed env entry, such as a new `secret://`
  reference, stops matching the stored approval (`changed_needs_reapproval`).
  The owner approves again in MCP Management: `POST /api/mcp-gateway/servers/launch`
  previews the launch (`api_mcp_gateway_server_launches`) and
  `POST /api/mcp-gateway/servers/stub` approves it
  (`api_mcp_gateway_set_stub`, which calls `approve`). A server whose env holds a reference and that the
  gateway does not route is withheld from sessions
  (`_unrouted_secret_reference` in
  `src/kiro_crew/agent_materialization/mcp_sources.py`).
- **Two routes write a server's entry, and they differ.**
  `PUT /api/mcp/custom/{name}` (`api_mcp_custom_update` in
  `src/kiro_crew/dashboard/handlers/mcp_custom.py`, called by the browser
  through `website/src/api/client/mcp.ts`) is owner-only through
  `require_owner_dashboard_request`. It keeps the server's enabled state and
  carries keys already on the entry, such as `disabledTools` and
  `autoApprove`, through the save. `PUT /api/mcp/servers/{name}`
  (`api_mcp_server_detail` in `src/kiro_crew/dashboard/handlers/mcp.py`) is
  the App Kit register route. It answers 400 `command_required` for a body
  with no `command`, so it cannot save a url server. It rebuilds the entry from
  `command`, `args` and `env` only, which drops `disabledTools` and
  `autoApprove`, and it turns the server on.
- **A url server has no env.** `_validate_spec` in `mcp_custom.py` refuses
  `command`, `args` and `env` (`_STDIO_KEYS`) on a url spec, so a url server's
  key is a `headers` value. A stored header value can only be passed back
  unchanged on an edit (`_merge_stored_headers`), and nothing resolves a
  `secret://` reference in a header.
- **The dashboard cannot test such a server.** `probe_server` does not resolve a
  `secret://` value. A server whose env holds one reports `outdated` with the
  reason "not probed: env … holds a secret:// reference", or `routed` when the
  running gateway serves it (`probe_server` and `secret_reference_refusal` in
  `src/kiro_crew/mcp_discovery_probe.py`).
  So after a person stores a key the stage 1 Test button cannot check it.
- **The last error is saved but not shown.** A row's `error` is the latest
  probe's (the `mcp-probe-error` notice in `website/src/pages/overview/McpTab.tsx`).
  The quarantine store already keeps more: for an `error` or `timeout` verdict,
  `record_verdicts` in `src/kiro_crew/mcp_quarantine.py` saves `last_status`,
  `last_error` and `last_failed_at` to `mcp-quarantine.json`, and an `ok`
  verdict clears the record. `_state_from` reads back `lastStatus` and
  `lastError` but not `last_failed_at`, and `_annotate_quarantine` in `src/kiro_crew/dashboard/handlers/mcp_probe.py`
  copies only `probeFailures` and `probeFailing` onto a row. No row offers a fix
  hint.
- **The popular list is eight bundled rows, in file order.** The Add Server modal shows the
  `mcp_servers` rows of `src/kiro_crew/data/discovery_featured.json` when the
  search is empty (the `featured: true` query in `McpBrowserModal.tsx`, and
  `_featured_results` in `src/kiro_crew/dashboard/handlers/mcp_discover.py`). The rows carry
  `id`, `name`, `provider`, `title`, `description`, `repo_url` and `methods`.
  `featured_entries` in `src/kiro_crew/discovery_featured.py` returns them in
  file order and keeps only string fields (plus the `methods` list), so a
  non-string key such as an integer is dropped.

## 3. Goals

1. A person can add a key for a command (stdio) server on its row, without
   editing a file.
2. The key lands in the vault and the server's env holds only a `secret://`
   reference.
3. The Test button can check a server whose env holds a reference, for a
   launch the owner has approved.
4. A failing row shows its last error and one short, actionable hint.
5. Add Server opens on a short, ordered list of servers people use most.
6. Saving a key changes only that env key: the server's enabled state, its
   `command` and `args`, and its `disabledTools` and `autoApprove` stay as they
   were. The one thing that does not carry over is the launch approval, which
   covers the env text: the owner approves the launch again, and stage 2a tells
   them so and never approves for them.

## 4. Non-goals

- A new secret store. Stage 2 uses `SecretVault` and `POST /api/secrets`.
- Showing a stored value back in the browser.
- Keys for url servers. Their key is a `headers` value, and a `secret://`
  reference in a header has no resolver. That needs its own design and boundary
  review; stage 2a offers no form on a url row.
- A remote catalog or ratings. Stage 4 edits the bundled file only.
- OAuth sign-in. Connections already owns it
  (`docs/system-specs/modules/connections.md`).

## 5. Design

### 5.1 Stage 2: enter a key on the row

**Write path.** The row's form sends the value to the existing
`POST /api/secrets`, under a name the server derives from the server and key
names. The form then reads the server's spec with `GET /api/mcp/custom/{name}`,
sets that one env key to `secret://<that name>`, and saves the spec with the
existing `PUT /api/mcp/custom/{name}` (`api_mcp_custom_update`), the route the
browser's server editor already uses. That route keeps the enabled state and
carries `disabledTools`, `autoApprove` and the other keys already on the entry,
so saving a key neither turns a server on nor widens its tools. The form is
offered only on a command server: the route refuses `env` on a url spec. The App
Kit register route `PUT /api/mcp/servers/{name}` is not used: it would drop the
carried keys and turn the server on. No new route writes a secret, and no raw
value reaches `mcp.json`.

**Who picks the vault name, and the rollback.** The browser picks the name, by a
fixed rule from the server and key names, and sends it in the `POST /api/secrets`
body, because `api_secrets_set` takes the name from the request. That route
upserts, so the form first lists names with `GET /api/secrets`. A name that is
new is created, and if the spec save then fails the form deletes that entry with
`DELETE /api/secrets/{name}?if_revision=<revision>`, using the revision the write
returned, so a value saved over it since is not deleted. A name that exists is a
key rotation only when this server's env already holds `secret://<that name>` for
that key: the new value is written, the reference is unchanged, and a failed
spec save needs no rollback. Any other existing name stops the form with a
message and writes nothing, so a secret that belongs to something else is never
overwritten.

**Routing and approval.** A reference resolves only when the gateway routes the
server and the owner has approved its launch. An unrouted server whose env holds
one is withheld from sessions, so the form is offered only on a row the gateway
routes; on any other row it says the key cannot be stored there yet, and nothing
is written. Saving a key adds or changes the declared env text, so the approved
launch stops matching: the gateway does not start the server and resolves
nothing for it until the owner approves the launch again. Stage 2a does not do
that for them. After the save the row says the launch needs approval and points
to MCP Management, where the existing preview and approve routes
(`POST /api/mcp-gateway/servers/launch`, `POST /api/mcp-gateway/servers/stub`)
show the launch and record the approval. A form that approved on its own would
let one click on a key field approve whatever command and args the spec holds at
that moment. Rotating the value under the same name changes no env text, so it
needs no new approval.

**Who may write.** Owner only for a browser caller, as both routes already
are (`api_secrets_set`'s `_owner_only`; `api_mcp_custom_update`'s
`require_owner_dashboard_request`). No App Kit or internal caller gains a new
way to set a server's env.

**Request safety.** Both routes keep the request checks they have today; this
stage adds none and removes none. The form never echoes the value back, and the
field is cleared after a successful save. Q4 asks a maintainer whether that is
enough for a browser form that writes a secret.

**Redaction.** The value never reaches a log line, an error message, the probe
cache or a probe row. Errors name the env key only, as
`secret_reference_refusal` already does.

**Testing a referenced server (the boundary move).** The gateway resolves a
reference only for a launch the owner approved, because the command, args and env
behind a server come from files the agent can write. Stage 2b keeps that gate. The
owner's Test of one server (`POST /api/mcp/probe/{name}`) resolves that server's
references in memory for that one spawn only when the stored approval admits the
exact launch the probe is about to run, by the check gatewayd runs
(`launch_approved`, which asks `LaunchApprovals.admits_launch`). The probe
resolves the launch once, as `api_mcp_gateway_set_stub` does when it approves
(`resolve_launches` in `src/kiro_crew/mcp_gateway/launch_resolve.py`, whose
`ResolvedLaunch` carries the command, args, declared env and
`approval_identities`), and spawns that same command, args and env, so a file
edited between the check and the spawn cannot change what runs. A launch that is
not approved (never approved, changed since, or a server that is never pooled)
keeps today's refusal, and the probe reads nothing from the vault. The `managed`
path of `LaunchApprovals.admit`, which admits a launch the code derived itself,
is not used. The background probe and the full probe keep refusing. Open question
Q1 asked whether a maintainer accepts this, and the sign-off does, stage 2b
included. A failed
probe keeps a tail of the child's stderr and saves it as `last_error`, so a
server that echoes its env could carry the value there. Phase 2b therefore ships
a leak test: a fake server echoes a planted synthetic credential
(`kiro_crew.testing.synthetic_credentials`) to stderr and fails, and the test
asserts the value is absent from the response, the probe cache and
`mcp-quarantine.json`.

### 5.2 Stage 3: last error and fix hints

Stage 3 reuses the record the quarantine store already writes; it adds no second
copy. A record exists from a server's first `error` or `timeout` verdict, so a
server that has failed fewer times than the quarantine threshold has one too.
Stage 3 extends the read shape `_state_from` returns with `lastFailedAt`, and
`_annotate_quarantine` copies the record's `lastError` and `lastFailedAt` onto
the row. A row with a record says when and how it last failed. A row whose latest
verdict is `ok` has no record and shows no last error; Q2 asks whether to keep it
past an `ok`. A small, fixed table maps
known error shapes to one hint: a missing command, a timeout, an HTTP 401 or 403,
a missing required env name. Hints are catalog strings in all locales. An unknown
error shows no hint. The store's format and its write path are unchanged.

### 5.3 Stage 4: a curated popular list

Stage 4 edits the rows of `discovery_featured.json` only: which servers are
listed and in what order. It adds no key. The file is already rendered in file
order, so order is the row order, and each row's existing one-line
`description` is the reason to use the server. No code change and no new data
source.

## 6. Migration plan

| Phase | PR | Exit criteria |
|---|---|---|
| 3 | Last error and hints | `_state_from` returns `lastFailedAt`; a row with a quarantine record carries `lastError` and `lastFailedAt`, and a row without one carries neither; each mapped shape shows its hint and an unmapped error shows none; `mcp-quarantine.json`'s format is unchanged; a test pins each mapping |
| 4 | Curated featured list | `discovery_featured.json` holds the chosen rows in the chosen order and adds no key; the existing featured test passes |
| 2a | Key form, no resolve | Saving a key creates a vault entry and sets `secret://` in env through `PUT /api/mcp/custom/{name}`; a test asserts the enabled state (on and off), `command`, `args`, `disabledTools` and `autoApprove` are unchanged after the save; a url row and an unrouted row offer no form; after a save on a routed, approved server its launch reports `changed_needs_reapproval`, the row says approval is needed, and no stage 2a code path calls `approve`; the existing preview and stub routes approve the new launch; a rotation under the same name needs no new approval; a failed spec save on a new name leaves no vault entry, and on a name this server already references leaves the entry and the reference unchanged; an existing name this server does not reference stops the form with nothing written; the value never appears in a response, log or `mcp.json` (asserted with a planted synthetic credential) |
| 2b | Test resolves a reference | Accepted (Q1). The single-server Test resolves references for that spawn only, and only when the stored approval admits the launch it is about to run; a test with an approved launch resolves; a test that edits `command`, `args` or `env` after the approval gets today's refusal and a vault whose read counter stays at zero; the full and background probes still refuse; a fake server that echoes a planted synthetic credential to stderr leaves it in no response, probe cache or `mcp-quarantine.json` |

Stages 3 and 4 do not depend on this RFC's open questions and can land first.

## 7. Backward compatibility

- Stage 2a adds a form; existing env values, plain or referenced, are untouched.
  It saves through `PUT /api/mcp/custom/{name}`, which keeps the enabled state
  and carries `disabledTools`, `autoApprove` and every other stored key, so the
  only change on the entry is the one env key the person entered. The launch
  approval is the exception, because it covers the env text: on a routed server
  the owner approves the launch again, as for any other env edit today.
- Stage 3 adds optional row fields read from the existing store; older clients ignore them.
- Stage 4 changes only which rows the bundled file lists and their order.
- Nothing is removed or renamed.

## 8. Security considerations

- Stage 2a writes a secret only through the owner-only `POST /api/secrets`,
  and sets the reference only through the owner-only
  `PUT /api/mcp/custom/{name}`. That route keeps the enabled state and the
  carried `disabledTools` and `autoApprove`, so saving a key never turns a
  server on or widens its tools.
- Stage 2b lets one owner-started probe hold a resolved value in memory. The
  value goes to the child's env only, as at gateway spawn; it is never written,
  logged or returned. The probe's child runs outside the session sandbox as the
  user, as gatewayd's backends do, so the same gate decides whether a vault value
  may go there: the stored launch approval (`launch_approved`). A launch the agent
  edited after the owner approved it does not match, so no vault value reaches
  it. This is the one boundary move; Q1 accepted it.
- Stage 2a adds no approval writer. A key save leaves the launch unapproved until
  the owner approves it through the existing routes.
- No change to `sandbox.py`, to the `.vault` leaf (HIDDEN, bind-masked in every
  sandbox mode), or to any governance ceiling.

## 9. Alternatives considered

1. **A new MCP credential route.** Rejected: it would be a second secret
   writer beside `POST /api/secrets`.
2. **Write the plain value into `mcp.json`.** Rejected: it puts a secret in a
   file the vault exists to keep it out of.
3. **Let every probe resolve references.** Rejected: background probes run
   without a person asking, so they should not touch secrets.
4. **Let the Test resolve references without the launch approval check.**
   Rejected: the command, args and env come from files the agent can write, so an
   edit after the owner's approval would put a vault value into a launch nobody
   approved. The probe reuses the check gatewayd runs instead.
5. **Let the key form approve the launch.** Rejected: it would be a second
   approval writer, and one click on a key field would approve whatever command
   and args the spec holds at that moment.

## 10. Open questions

- **Q1.** May the owner's single-server Test resolve `secret://` references for
  that one spawn, when the stored launch approval admits the launch it is about to
  run (the check gatewayd runs), and keep refusing every other launch? Answered
  yes: the maintainer sign-off accepts stage 2b as written.
- **Q2.** Should a row show its last error after a later `ok`? Today an `ok`
  clears the quarantine record, so this RFC shows the last error only while a
  record exists. Keeping it past an `ok` would change the store's clearing rule.
- **Q3.** Who curates the stage 4 rows and their order: maintainers in the
  JSON file, or a generated count? This RFC proposes the JSON file.
- **Q4.** Are the existing request checks on `POST /api/secrets` and
  `PUT /api/mcp/custom/{name}` enough for a browser form that writes a secret,
  or does stage 2a need an extra confirmation step?
