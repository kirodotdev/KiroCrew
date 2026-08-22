# Connections L1: the authorized-grant smoke rung

A visible Connect card is a promise the flow works; each rung of the launch
ladder asserts something the rung below structurally cannot:

| Rung | Where | Needs an account? | Asserts |
|---|---|---|---|
| **L0** | `connections-l0.yml`, nightly, every branch | no | the provider's PUBLIC OAuth metadata still matches the committed `l0_expectations` |
| **L1** | `connections-l1.yml`, scheduled, opt-in box | yes, one human click per provider, once | a grant that exists still works against the live endpoint |
| **L2** | manual, at the flag-flip gate | yes | the UI walk-through a human has to actually see (see the Connections manual test SOP) |

L0 never authenticates, so it cannot prove a *connection*; L2 costs a human
every time. L1 sits between: one consent click seeds a provider, then automated.

## What a green L1 run actually proves

**Kiro Crew holds no token.** kiro-cli owns the OAuth chain and injects the
bearer inside its own process ([mcp-oauth-ownership.md](mcp-oauth-ownership.md)),
so an exchange opened from the harness presents only the headers the server
entry itself carries — for a managed provider, none — and the live endpoint
answers with an OAuth challenge, exactly as it answers the dashboard's probe
(`mcp_discovery._needs_authorization`, imported rather than re-derived). The
verdict vocabulary is built around that fact:

| Verdict | Green? | Established |
|---|---|---|
| `PASS` | yes | the exchange ran through `initialize` and `tools/list` non-error, and the registry `smoke_fixture` tool is still advertised. Reachable for an entry that carries its own credential, or an unprotected server |
| `GRANT_HELD` | yes | the grant is still on disk **and** the endpoint is reachable and still answers a well-formed challenge. Does **not** prove a tool call would succeed |
| `NEEDS_RECONSENT` | no | a credential this process presented was refused, or an authorization error came back mid-exchange. A human must re-approve |
| `FAIL` | no | reached and wrong, or unreachable: non-2xx with no challenge, 5xx, timeout, transport error, broken `tools/list`, fixture tool no longer advertised |
| `SKIPPED` | yes | not a configured MCP server here, or no grant for it. **L1 never initiates consent** |

Each row also carries `depth` (`tools_list` / `challenge` / `none`), so how far
the exchange got is never inferred.

### The sweep never calls a tool

It stops at `tools/list`. A `tools/call` issued from here would reach the
provider outside the governed dispatch path, so a tool an enterprise policy
denies would be invoked anyway and no SEL event would record it — an unaudited
side channel is a worse outcome than a narrower verdict. So the registry's
`smoke_fixture` is used for its NAME only: `tools/list` already answers whether
the pinned tool is still advertised, which is the regression this rung can
honestly detect. Actually exercising the tool belongs with the ACP-side slice
below, where kiro-cli's own governed, audited dispatch owns the call.

### Runbook for a failing lane

| Symptom | What it means, what to do |
|---|---|
| `vacuous`, every provider `SKIPPED` | No grant is seeded, and seeding IS the one-time consent click: on the box, open Connections and click Connect per provider. Lower `--min-exercised` only if you mean "cover fewer providers" |
| `NEEDS_RECONSENT` | The grant is spent (expired refresh token, or revoked upstream). A human re-approves on the card |
| `FAIL` | Usually a provider-side change to its MCP surface: read their changelog before editing our registry |
| "exceeded its total timeout" | The provider outlasted its whole budget, not one request's. Raise `--timeout` only for a known-slow provider; otherwise treat as `FAIL` -- a session cannot get a tool out of it either |

### The decision that shaped this: a challenge is not a failure

The earlier draft graded every tokenless 401 as `NEEDS_RECONSENT` — but under
runtime token custody that is what a **healthy** authorized provider returns
every time, so the lane would sit permanently red, and a lane nobody believes
is worse than no lane. So the challenge became its own verdict and
`NEEDS_RECONSENT` narrowed to an attributable rejection. Consequence worth
naming: a tokenless `403` with no `WWW-Authenticate` grades `FAIL`, not
`NEEDS_RECONSENT` — an edge proxy or geo block reads identically, and a consent
verdict would send a human to re-approve a grant that was never at fault.

### A run that exercised nothing is not green

With no seeded grants every provider is `SKIPPED` and the aggregate would be a
cheerful `ok` establishing nothing. `--min-exercised N` (the lane passes `1`)
makes that `vacuous`, nonzero, naming the consent step. `GRANT_HELD` counts as
exercised; `SKIPPED` does not.

## Grant presence is observed, never read

`grant_key`, `grant_present` and `kiro_oauth_cache_dir` are imported from
`connections/mint.py` rather than copied — this slice's dedupe obligation,
pinned by identity in the tests (the copy that drifts grades a live connection
`SKIPPED` while the card says connected). Presence is a `stat` of the paired
`{sha256}.token.json` + `{sha256}.registration.json` artifacts and opens
neither, so no token byte can enter the process, report, or a log line; the
single-file `{sha256}.json` SSO form is deliberately not consulted, and a test
fixture makes any *open* of either artifact an outright failure. No SEL read
audit, unlike mint's `_grant_observed`: that covers a Connect flow acting for a
remote caller, whereas this is an operator's own CLI, `l0_probe`'s class.

## Known gap: proving a tool actually works

Two things keep this rung short of "a tool call succeeded", and they point the
same way. *Who holds the bearer*: kiro-cli does, so an exchange opened here
presents no credential for a managed provider. *Who owns dispatch*: the governed
path does, so a call issued here would skip enterprise tool policy and its audit
record. Both are answered by having the runtime make the call, not by reading its
token store or bypassing its gate — the boundaries this module exists to respect.
ACP exposes no tool-invocation surface outside a model turn today, so that lands
with the ACP-side observation slice; until then `GRANT_HELD` is the ceiling for
runtime-custody providers and `PASS` means "reachable, authenticated, and the
pinned tool is still advertised".

## Running it by hand

`python3 -m kiro_crew.connections.l1_smoke --report /tmp/l1.json` (under a
pipx/venv install, use that environment's interpreter). `--min-exercised 1`
reproduces the lane's gate; `--concurrency` and `--timeout` are in `--help`.
