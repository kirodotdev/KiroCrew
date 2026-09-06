# Chat Status Tags

Keeps the Chats sidebar honest: every dashboard conversation carries a tag
saying where its work stands and how it is doing.

Two tag families, independent of each other:

| Family | Tags | Written by |
|---|---|---|
| SDLC status | `planned` `todo` `implementation` `review` `done` | the agent itself (`self-tag-chat` skill) + the hourly reconciler |
| Health | `stuck` `network` `error` | the health loop, cleared automatically when the condition clears |

## Pieces

- **`logic.py`** — every decision, as pure functions: the error-card
  classifier, stuck detection, health-tag reconciliation, resume-episode
  accounting, and promote-only status ordering. No HTTP, no gateway state.
- **`store.py`** — the loops' transport: in-process access to the gateway's
  own chat tags/slots state. The loops run *inside* the gateway, so this
  reaches the live `DashboardState` directly (via the route registry) — no
  loopback HTTP, no `X-Internal-Secret`, no port. Reads are in-memory scans;
  writes are marshalled onto the gateway's serving loop. (This replaced an
  earlier loopback-HTTP `client.py` that 403'd on every cycle — a loop cannot
  authenticate to the process it runs in.)
- **`hooks.py`** — the two background loops, started on enable:
  - *health* (60 s): seeds the tag vocabulary, then flags running-but-silent
    chats `stuck`, network-killed chats `network`, and everything else that
    ended in a terminal error card `error`.
  - *auto-resume* (60 s): injects `Continue` into `network`-class chats, but
    only after the connection has held for a full minute of consecutive
    probes, and never more than 3 times per failure episode. Auth and
    unclassified errors are left for the human. Silent by design.
- **`skills/self-tag-chat/`** — the agent-facing skill: a tool-driven
  procedure for tagging your own slot at a phase change through the
  `chat_status_tags_api` MCP tool. Deliberately script-free: the gateway
  holds the credential, so the skill never mints or handles a token.
- **manifest cron `sdlc-tag-reconcile`** (hourly, silent) — the LLM backstop
  for the two transitions an idle chat's agent forgets: owns an open PR →
  `review`; all owned PRs merged → `done`. Promotions only. Its full
  instructions ARE the cron's `message` (see "How the reconcile prompt reaches
  the agent" below).

## Design notes

- **Stuck detection uses `last_ts`, not `last_activity_ts`.** `last_ts`
  covers messages of any role, including the user's fresh prompt;
  `last_activity_ts` only tracks tool/assistant messages, so it still points
  at the *prior* turn on a chat resumed after a long idle and would flag a
  healthy chat.
- **The classifier is pinned to the chat runner's actual card strings**
  ("Connection lost", "Session busy", "Backend hiccup", "Session stuck",
  "not logged in", …). The runner rewrites lower-level failures into these
  cards before they reach a slot, so matching raw exception text would match
  nothing. Tests in `tests/` pin the contract.
- **Tag ids are server-assigned.** Every writer resolves names through
  `GET /api/chat/tags` (creation is idempotent by name) — a PUT silently
  drops unknown ids, so writing names where ids belong fails invisibly.
- **Two ways to reach the chat API, by caller.** The `self-tag-chat` skill
  drives the `chat_status_tags_api` MCP tool from inside the agent's normal
  tool-call gate — the gateway holds the credential, so the skill never mints
  or handles a token. The **background loops run INSIDE the gateway
  process**, so they do NOT dial the HTTP surface at all — they read and mutate
  the live `DashboardState` in-process (`store.py`). An in-gateway loop
  authenticating over loopback HTTP to its own port is the wrong architecture
  and is exactly what produced the 403-every-cycle failure: the loop read the
  shared local secret while a different listener generation owned the port it
  dialed, so every health sweep was rejected and no tag ever moved. The
  **hourly reconciler is different again**: it is an LLM agent whose prompt is
  a tool call, so a `kirocrew token` mint from it is refused by the shipped
  `credential-exfil-kirocrew-token` deny rule and would silently no-op every
  run. The reconciler therefore reaches the API only through the credentialed
  `chat_status_tags_api` MCP tool (allowlisted to `GET /api/chat/slots`,
  `GET /api/chat/slots/{key}`, `GET`/`POST /api/chat/tags`, and
  `PUT /api/chat/slots/{key}/tags`). The default reconcile prompt in
  `prompts.py` states plainly that the tool is the only credentialed path and
  that a token mint is refused by policy.

## How the reconcile prompt reaches the agent

The reconcile prompt is delivered to the agent as the reconcile cron's **own
`message`** — a trusted instruction channel that arrives as the agent's own
instructions and needs no tool call. It is NOT delivered by telling the agent to
READ an instructions file.

An earlier design made the manifest cron message a thin bootstrap that told the
agent to `cat` its instructions from
`$KIROCREW_HOME/apps/chat-status-tags/data/reconcile-prompt.md`. Two live pod
runs proved that broken on both counts:

- **Run A** read the operator's edit, correctly judged that content arriving
  through a data-file channel is untrusted, and ignored it wholesale — replying
  that it was treating the file as a prompt-injection attempt. A legitimate
  customization ("also check code reviews") would have been refused the same way.
- **Run B** could not read the file at all: every path to it (reading it,
  checking for it) was approval-gated, so the unattended cron produced nothing.

Routing OPERATOR CONFIGURATION through an untrusted-data channel AND an
approval-gated tool call is the root cause of both. Delivering the prompt as the
cron's `message` fixes both: instructions in the message are trusted, and no tool
call is needed to obtain them.

The plain-text file (`data/reconcile-prompt.md`, managed by `settings.py`)
remains the **persistence layer of record**: the app page reads and writes it,
and clearing it resets to the shipped default. The effective prompt is pushed
into the live cron's `message` whenever it is saved or reset
(`PUT /reconcile-prompt`), and re-synced on startup, on `POST
/reconcile-cron/repair`, and when the reconciler toggle is turned back on —
because `register_app_crons_with_service` rebuilds the cron from the IMMUTABLE
manifest and would otherwise clobber a custom prompt back to the default on every
restart or heal. The manifest cron `message` in `app.json` is exactly
`prompts.DEFAULT_RECONCILE_PROMPT`; a test asserts the two match byte-for-byte so
they cannot drift.

## Portability

The in-process loops exist because a builtin runs inside the gateway; the
*decisions* do not depend on that. To package this as an external app later:
keep `logic.py` byte-identical, replace `store.py`'s in-process access with
the cron `ScriptContext` HTTP transport (the same `/api/chat/*` contract the
routes expose), and register the two loops as zero-token `script` crons
(staged under `$KIROCREW_HOME/crons/`) plus the same manifest agent cron. Only
the transport layer moves — `logic.py` and the loops' orchestration in
`hooks.py` are unchanged. The reconciler's `chat_status_tags_api` MCP tool
travels with the app in either shape — it is the reconciler's credentialed
path in both, since the token-mint deny rule that forces it applies to any
agent, builtin or external.
