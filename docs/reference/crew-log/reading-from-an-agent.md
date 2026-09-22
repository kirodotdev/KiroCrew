# Reading a crew log from an agent

**Local page, not a mirror.** Part of the [crew log reference](README.md), which is
marked as a named exception in [the Reference index](../README.md).

The crew log is fenced from the agent's file tools on purpose: the whole
`crew-log/` root is a leaf in the shared sensitive-path floor, so an agent cannot
reach the files at all — it can forge no line into its own history and alter
nobody's. That does not change. What the tools below add is a READ, through the
gateway, audited; the fence's integrity guarantee is not what they relax.

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
| the literal `self` | `self` | the calling session's own unit, resolved from its strictly-resolved key |

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
| `crew_log_disabled` | Session crew-log emission is off. Set `KIROCREW_CREW_LOG=1` in `~/.kiro/crew/.env` and restart the gateway. |
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
gateway cannot name reads nothing at all, not even its own unit.

One rule: **a valid strict session identity, and then a scope.** A session may read

- **its own unit** — `unit: "self"`, or its unit named outright;
- **the unit of any session it dispatched**, at any depth — the walk up the creators
  ends at the first session naming no creator, on a repeated session, or on one the
  fold marked as being on a cycle, and each of those answers "no creator above this",
  which REFUSES the read rather than admitting it. The
  edge comes from the dispatched session's own `session/opened` entry, which records
  the creator that `session_create` minted it from, so a conductor reaches its
  children and their children. The fence keys on the recorded `parent.slot` rather
  than on `parent.sid`, because a slot outlives its ACP session: a gateway restart
  gives the same tab a new session id and therefore a new unit, and a sid-keyed
  fence would lock a re-attached conductor out of the children it dispatched minutes
  earlier. The transitive walk is the same fold the Sessions page's tree uses, so
  the two cannot disagree about who dispatched whom;
- **any unit at all**, if it is the owner at a dashboard tab whose own caller class
  also allows it. Reading every unit is the case the routes shipped with; the class
  condition is not, and it is there because a dashboard session MIRRORED to a
  channel republishes every turn, so that tab would otherwise be the one caller that
  could read every log and publish it. A mirrored owner tab is held to its own unit
  like any other excluded caller.

`crew_log_list` carries the same scope as a filter rather than as a verdict: a
conductor's listing names its own unit and its dispatch tree, the owner's names
every unit. An unscoped listing would tell any caller which sessions exist on the
host, which is the one thing a per-unit gate cannot refuse after the fact.

A conductor reading the crew logs of the sub-sessions it dispatched is the ordinary
shape of the work, not a special case, and the lineage record already says which
session created which — so the entitlement is derived from a recorded fact rather
than asserted. Issues #11963 and #12025 were closed as not planned for that reason.

It is a dispatch fence rather than an operator fence, and the difference is about
surfaces rather than trust. The wider premise — that sessions on one gateway belong
to one operator, so any may read any other — does not reach a **channel-linked**
session: its conversation is a Slack or Telegram thread, so several allow-listed
people read it and prompt-injectable content enters it. That is exactly the caller
class an unscoped read would admit, so the caller classes
`session_control.authorize_target` refuses are mirrored here, from that module's own
constants.

The refusals, and what each rules out:

- **No internal secret.** These routes are internal-transport only; a browser
  reads its own log through `/api/sessions/{id}/crew-log`, which has its own
  cookie-and-owner gate. Admitting a cookie here would make a second, separately
  audited path out of a door built for one caller.
- **A request naming another component.** The prefix serves
  `kirocrew-crew-log`; the `X-Internal-Caller` header is validated, not trusted.
- **A session the gateway cannot name.** Refused with `forbidden` and a one-line
  reason, ahead of every scope arm: an operator's record of WHICH session read a
  log is only worth having if the identity behind it was resolved strictly rather
  than walked out of a process tree. The lenient resolver walks `/proc` ancestors,
  and a subagent lives under its spawner's tree, so a leniently resolved key would
  file a subagent's read under its parent slot.
- **A unit outside the caller's dispatch tree.** The fence itself.
- **An excluded caller class reading past its own unit** — unattended (`cron:`,
  `taskrunner:`), app-scoped, incognito or temporary, channel-linked or mirrored.
  Each still reads its own unit; what is refused is reading another session's.
- **A target whose log does not record its class.** The class of a session is
  recorded on its own `session/opened` entry, which is what keeps a CLOSED child
  decidable. A log written before that field existed, or whose opening entry
  retention has taken, does not say — and an absent record is refused rather than
  read as "nothing applies", so every such unit is outside a cross-session read
  until the session is opened again under a build that records it.
- **A target in an excluded class by its record.** App-scoped, incognito or
  temporary, or published to a channel, read from the recorded facts.
- **A target in an excluded class by its live slot.** The record states what the
  session was when its log opened; this catches one it acquired afterwards, such as
  a channel link added mid-conversation. Both tests run and either refuses.

Every read is audited under the same SEL action names the browser routes use —
`session_crew_log.list`, `session_crew_log.resolve`, `session_crew_log.read`,
`session_crew_log.projection` — and the call itself lands as a `tool/called` entry
in the calling session's own crew log.

Every REFUSAL is audited under those names too, including the two that reject a
caller before its identity is settled: a request with no internal secret, and one
naming a different component. Those are the records an operator wants most, since
both are the shape of an attempted boundary crossing. A secret-less caller is
recorded as `dashboard` and an unrecognized component as `unknown-internal`, so the
log cannot be seeded with a name the caller chose.

This is not a thin read, so state the content plainly: a crew log carries **full
message bodies**. `message/received` and `message/sent` record the text itself, and
a body too large for one line is split across `message/chunk` entries the citing
entry names, so the whole text is reconstructable from the log. Other entry types
are narrower than that (`context/composed` is char and token counts,
`request/configured` is a sha256, a tool call is a parameter summary), but the
transcript-bearing types are not.

### How this compares to `session_read_message`

`session_read_message` returns a peer session's transcript behind
`session_control.authorize_target`. The caller-class exclusions are the same; the
two doors differ on the target side and on what they are keyed to:

| Caller or target | `session_read_message` | crew log MCP reads |
|---|---|---|
| persistent dashboard session, own or dispatched | allowed | allowed |
| a session in another dispatch tree | allowed | refused |
| unattended (`cron:`, `taskrunner:`) caller | refused | refused past its own unit |
| app-scoped caller or target | refused | refused past its own unit |
| incognito or temporary caller or target | refused | refused past its own unit |
| channel-linked or mirrored caller | refused | refused past its own unit |
| owner's dashboard tab mirrored to a channel | refused | refused past its own unit |
| channel-linked or mirrored target, live | refused | refused |
| channel-linked or mirrored target, closed | not addressable | refused, from the record |
| closed session's history, class recorded | not addressable | readable by its dispatcher |
| closed session's history, no class recorded | not addressable | refused |
| behind `agent.session_control` | yes | no |

One row is the deliberate difference. A **closed** session is readable here, because
reading a finished child's recorded work is what this door is for, and
`authorize_target` answers 404 for it. That does not cost the target's class: the
class is written on the target's own `session/opened` entry, so it is read from the
log rather than from a slot that is gone, and a log that carries no such record is
refused. A session in **another dispatch tree** is refused here and allowed there,
so this door is narrower on the axis that matters most.

What the fence still protects is INTEGRITY, and that is untouched: there is no
write tool, the tool set is ratcheted, and the agent's file tools still cannot
reach the `crew-log/` root. A session cannot alter its own history or anyone
else's. Reading was never the thing the fence protected.

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
