# Connections L1: the authorized-grant smoke rung

A visible Connect card is a promise the flow works; each rung of the launch
ladder asserts something the rung below structurally cannot:

| Rung | Where | Needs an account? | Asserts |
|---|---|---|---|
| **L0** | `connections-l0.yml`, nightly, every branch | no | the provider's PUBLIC OAuth metadata still matches the committed `l0_expectations` |
| **L1** | `connections-l1.yml`, scheduled, opt-in box | yes, one human click per provider, once | a grant that exists still works against the live endpoint |
| **L2** | manual, at the flag-flip gate | yes | the UI walk-through a human has to actually see (see the Connections manual test SOP) |

L0 never authenticates, so it cannot prove a *connection*; L2 costs a human
every time. L1 sits between: one consent click, then automated.

## What a green L1 run actually proves

**Kiro Crew holds no token.** kiro-cli owns the OAuth chain and injects the
bearer inside its own process ([mcp-oauth-ownership.md](mcp-oauth-ownership.md)),
so an exchange opened from the harness carries no credential for a managed
provider, and the live endpoint answers with an OAuth challenge -- exactly as it
answers the dashboard's probe. The verdict vocabulary is built around that fact:

| Verdict | Green? | Established |
|---|---|---|
| `PASS` | yes | the exchange ran through `initialize` and `tools/list` non-error, and the registry `smoke_fixture` tool is still advertised. Reachable for an entry that carries its own credential, or an unprotected server |
| `GRANT_HELD` | yes | the grant is still on disk **and** the endpoint is reachable and still answers a well-formed challenge. Does **not** prove a tool call would succeed |
| `NEEDS_RECONSENT` | no | a credential this process presented was refused, or an authorization error came back mid-exchange. A human must re-approve |
| `FAIL` | no | reached and wrong, or unreachable: non-2xx with no challenge, 5xx, timeout, transport error, broken `tools/list`, fixture tool no longer advertised |
| `SKIPPED` | yes | not a configured MCP server here, no grant for it, or the entry's endpoint does not match the registry `mcp_url`. **L1 never initiates consent** |

Each row also carries `depth` (`tools_list` / `challenge` / `none`), so how far
the exchange got is never inferred.

### The sweep never calls a tool

It stops at `tools/list`. A `tools/call` issued from here would reach the
provider outside the governed dispatch path, so a tool an enterprise policy
denies would be invoked anyway and no SEL event would record it — an unaudited
side channel is a worse outcome than a narrower verdict. The registry's
`smoke_fixture` is therefore used for its NAME only (`tools/list` already
answers whether it is still advertised); exercising it belongs to the ACP-side
slice below, where kiro-cli's own governed, audited dispatch owns the call.

### Runbook for a failing lane

| Symptom | What it means, what to do |
|---|---|
| `vacuous`, every provider `SKIPPED` | No grant is seeded, and seeding IS the one-time consent click: on the box, open Connections and click Connect per provider. Lower `--min-exercised` only if you mean "cover fewer providers" |
| `NEEDS_RECONSENT` | The grant is spent (expired refresh token, or revoked upstream). A human re-approves on the card |
| `FAIL` | Usually a provider-side change to its MCP surface: read their changelog before editing our registry |
| "exceeded its total timeout" | The provider outlasted its whole budget, not one request's. Raise `--timeout` only for a known-slow provider; otherwise treat as `FAIL` -- a session cannot get a tool out of it either |

### The decision that shaped this: a challenge is not a failure

The earlier draft graded every tokenless 401 as `NEEDS_RECONSENT` — but that is
what a **healthy** authorized provider returns under runtime custody, so the
lane would sit permanently red, and a lane nobody believes is worse than no
lane. So the challenge became its own verdict and `NEEDS_RECONSENT` narrowed to
an attributable rejection. A tokenless `403` with no `WWW-Authenticate` grades
`FAIL`, not `NEEDS_RECONSENT` — an edge proxy or geo block reads identically,
and a consent verdict would send a human to re-approve a healthy grant.

### A run that exercised nothing is not green

With no seeded grants every provider is `SKIPPED` and the aggregate would be a
cheerful `ok` establishing nothing. `--min-exercised N` (the lane passes `1`)
makes that `vacuous`, nonzero. `GRANT_HELD` counts as exercised; `SKIPPED` not.

## Grant presence is observed, never read

`grant_present` aliases `mcp_grant.grant_presence` rather than copying it —
this slice's dedupe obligation, pinned by identity in the tests; the key
formula and cache-dir resolution are pinned transitively, since it derives
both internally (a drifted copy grades a live connection `SKIPPED` while the
card says connected). Presence is a `stat` of the paired
`{sha256}.token.json` + `{sha256}.registration.json` artifacts and opens
neither, so no token byte can enter the process, report, or a log line; the
single-file `{sha256}.json` SSO form is deliberately not consulted, and a test
fixture makes any *open* of either artifact an outright failure. L1 calls the
synchronous presence helper and emits no SEL read audit, unlike mint's use of
`grant_observed`: that covers a Connect flow acting for a remote caller, whereas
this is an operator's own CLI, `l0_probe`'s class.

## Authenticated enumeration belongs to the on-demand Test action

The scheduled L1 sweep remains deliberately tokenless: it has no interactive
agent session, so a runtime-custody provider still tops out at `GRANT_HELD` and
stops before `tools/list`. That verdict continues to mean only that a grant
artifact exists and the live endpoint still issues a valid challenge.

The Connections card's owner-only `POST /api/connections/test` closes the
listing gap through the runtime instead of widening this harness. It starts a
promptless kiro-cli ACP session under the real `kirocrew` agent and executes two
native commands, with no model turn:

1. `/mcp` reads kiro-cli's MCP-manager status and per-server tool count. A
   `running` row means kiro-cli authenticated the server and completed the
   provider's `tools/list`; loading, failed, disabled, missing, or malformed
   rows are failures rather than empty tool sets.
2. `/tools` reads the final agent-exposed inventory. Only rows whose source is
   `mcp:<provider alias>` count, so a server that advertises tools but is not
   mounted by the agent is honestly reported as exposing none.

The 200 response is always one of three verdicts, each with a stable `code` and
`toolCount`: `usable` (`tools_available`, count > 0), `no_tools`
(`no_tools_exposed`, count 0), or `failed` (a specific runtime/inventory code,
count 0). The path returns no tool descriptions or credential material and,
like L1, never issues `tools/call`; provider dispatch remains exclusively on the
governed, audited agent path.

## Running it by hand

`python3 -m kiro_crew.connections.l1_smoke --report /tmp/l1.json` (under a
pipx/venv install, use that environment's interpreter). `--min-exercised 1`
reproduces the lane's gate; `--concurrency`/`--timeout` are in `--help`.
