# Reading a crew log from an agent

**Local page, not a mirror.** Part of the [crew log reference](README.md), which is
marked as a named exception in [the Reference index](../README.md).

The crew log is fenced from the agent's file tools on purpose: the whole
`crew-log/` root is a leaf in the shared sensitive-path floor, so a prompt-injected
agent can neither read another unit's history nor forge a line into its own. That
does not change.

What it also did was make the log unverifiable by anyone but the operator with a
shell. `kirocrew-crew-log` is the sanctioned door: an MCP server of three read-only
proxies over the gateway's own routes, so an agent can check a crew log without
being able to touch it.

## The three tools

| Tool | Answers |
|---|---|
| `crew_log_list` | One row per session log on this host: unit, slot, agent, model, first and last entry time, last seq, whether the session is open. `with_type_counts=True` adds the per-type histogram, per unit and across the listing. |
| `crew_log_read` | A range of entries as `{seq, ts, type, data}`, each citation resolved to its verdict and span, with `next_from` when more follows. |
| `crew_log_projection` | One fold and the seq it was folded through: `status`, `usage`, `timeline`, `tools`, `approvals`. |

There is no write tool, and none may be added: `test/test_mcp_crew_log.py` ratchets
the set to exactly these three names, so adding one fails a test rather than
shipping. The gateway remains the log's only writer.

### Naming the unit

Every tool takes `unit` in one of three forms, and the rule separating them is
fixed so one argument cannot mean two different units.

| Form | Example | Resolved how |
|---|---|---|
| a raw unit id | `s-7f3a` | passed through untouched |
| a session key | `chat-1533-1789617503`, `dashboard:chat-1533-1789617503` | through `crew_log/resolve.py::unit_for_session_key` — anything carrying a namespace (a colon) or shaped `chat-<n>-…` |
| the literal `self` | `self` | the calling session's own unit, via the strict identity gate |

`self` is the form to use when verifying that something the session just did was
recorded. A slot's unit is only valid at the moment it is asked: a reset, a
compaction, an agent or model switch, and a provider swap each tear the ACP session
down, and the successor gets a new id.

### Caps

`limit` is at most 200 entries. One result is at most 64 KiB, `full=True`
included — past that the rows
are cut at a **row** boundary and `next_from` is rewritten, so the answer stays
parseable and paging on `next_from` still reads the whole log, with `returned`
rewritten to the rows actually carried. A single row larger than the whole budget
has its own `data` trimmed rather than escaping the cap. Each row's `data` is
trimmed to a few hundred characters; to see one row whole, ask for it alone with
`from_seq=<seq>, limit=1, full=True`.

### Refusals

Every failure is a typed code, never a traceback.

| Code | Means |
|---|---|
| `crew_log_disabled` | The crew log is off. Set `KIROCREW_CREW_LOG=1` in `~/.kiro/crew/.env` and restart the gateway. |
| `unknown_unit` | No session log for that unit. |
| `unresolvable_key` | The key names no live ACP session, so no unit is receiving its work right now. |
| `unknown_projection` | Not one of the five folds. |
| `bad_range` | The requested seq range is not readable as one. |
| `forbidden` | The caller may not read that unit. See below. |
| `unavailable` | Nothing answered — the gateway is not reachable. |

The tools are advertised whether or not the flag is on. An agent must learn the
flag state from `crew_log_disabled`, which says how to switch it on; a missing tool
would instead read as "Kiro Crew cannot do this at all".

## Who may read what

Authorization lives in the endpoints, not in the MCP layer. Every request the
server makes carries the calling session's STRICTLY resolved key, so a caller the
gateway cannot name reads nothing at all, not even its own unit. That matters
because the lenient resolver walks `/proc` ancestors: a subagent lives under its
spawner's process tree, and the spawner is commonly the owner's own tab, so a
leniently-resolved key would present an owner identity for a subagent's call.

Given a named caller, two rules:

- **The caller's OWN unit** (`unit="self"`, or a unit that resolves from the
  caller's own key) needs only a strict session identity — the same gate
  `session_ledger_read` uses. A subagent, a cron, an incognito session: each may
  read itself.
- **Any other unit, and the listing**, needs the owner at a dashboard tab. A
  headless caller (cron, task runner, a subagent with no parent identity), a
  channel-bound session (`slack:`, `discord:`, …), an app-owned session, a key
  naming no live slot, and an incognito or temporary session are each refused with
  `forbidden` and a one-line reason.

Every read is audited under the same SEL action names the browser routes use —
`session_crew_log.list`, `session_crew_log.resolve`, `session_crew_log.read`,
`session_crew_log.projection` — and the call itself lands as a `tool/called` entry
in the calling session's own crew log.

Every REFUSAL is audited under those names too, including the two that reject a
caller before its identity is settled: a request with no internal secret, and one
naming a different component. Those are the records an operator wants most, since
both are the shape of an attempted boundary crossing, and a refused session class
is ordinary by comparison. A secret-less caller is recorded as `dashboard` and an
unrecognized component as `unknown-internal`, so the log cannot be seeded with a
name the caller chose.

This grants no read that is new in kind, and it is not a thinner read. State the
content plainly: a crew log carries **full message bodies**. `message/received`
and `message/sent` record the text itself, and a body too large for one line is
split across `message/chunk` entries the citing entry names, so the whole text is
reconstructable from the log. Other entry types are narrower than that
(`context/composed` is char and token counts, `request/configured` is a sha256, a
tool call is a parameter summary), but the transcript-bearing types are not.

What makes this safe to grant is the gate, not the payload. `session_read_message`
in `kirocrew-dashboard` already returns a peer session's full transcript to an
agent the owner granted it; these reads answer the same class of content behind a
**stricter** caller test — a strict session identity for `self`, and the owner's
own non-restricted, non-app dashboard session for anything wider. Read the gate as
the whole of the justification.

## Granting it to an agent

The server is **assignable only** — never injected into every agent, because
kiro-cli reads `tools/list` once per session and an always-on tool spends context
in every request of every session. The default agent's spec carries neither the
entry nor the reference, so a default session pays nothing.

An agent that should read crew logs gets both halves in its own spec — kiro-cli
loads a server only when something references it:

```json
{
  "tools": ["@kirocrew-crew-log"],
  "mcpServers": {
    "kirocrew-crew-log": {
      "command": "kirocrew",
      "args": ["mcp-crew-log"]
    }
  }
}
```

The spec carries no `autoApprove` key, and none may be added: kiro-cli approves an
autoApproved MCP tool locally and emits no permission request, so the always-on
deny floor, the sensitive-path check and the governance ceiling are never reached
for it.

## The routes underneath

| Route | Serves |
|---|---|
| `GET /api/crew-log/sessions` | the listing |
| `GET /api/crew-log/resolve?key=` | a session or slot key → its unit |
| `GET /api/crew-log/units/{unit}/page?from=&to=` | one range of entries |
| `GET /api/crew-log/units/{unit}/projection/{name}` | one fold |

All four are on the **strict** internal transport, and the handler refuses a
cookie. Those are two separate facts, because strict membership alone is not the
gate: a request from loopback carrying no `X-Internal-Secret` falls through to
ordinary cookie auth and reaches the handler. What strict decides is the
NON-loopback caller — hard-denied, where a mixed path would get the cookie
fall-through. So the handler refuses any caller arriving without the secret,
naming the browser's own route, and that refusal is what keeps the owner test
above from becoming a second way in from an authenticated page.

The browser's own `GET /api/sessions/{id}/crew-log` pair is unchanged and stays
cookie-only. The split is the authorization model, not the data: both doors call
the same page reader (`crew_log/read.py::read_page`) and the same fold, so they
cannot answer differently.
