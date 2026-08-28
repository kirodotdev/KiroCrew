# Experimental ACP adapters

Kiro Crew normally drives `kiro-cli`. Other agents can serve your turns
instead, so an existing vendor subscription does the work:

| Adapter | `agent.acp_backend` | Sign-in | Status |
|---|---|---|---|
| Kiro CLI | `""` (default) | `kiro-cli login` | Supported |
| OpenAI Codex | `codex` | `codex login` | Described, not selectable |
| Claude Code | `claude` | the Claude CLI | Described, not selectable |
| goose | `goose` | `goose configure` | Described, not selectable |
| OpenCode | `opencode` | `opencode auth login` | Described, not selectable |
| pi | `pi` (registry alias `pi-acp`) | `pi` | Described, not selectable |

Selectable means the value may be persisted. The initial preview is limited to
Kiro CLI and KAS; registry discovery and the other built-in descriptors do not
widen that reviewed set. Claude remains withheld because its routing seed can
merge into an existing project settings file while session cleanup currently
removes the whole file. Codex remains withheld because `mode=read-only` blocks
writes but does not permission-route passive reads, while the standard sandbox
leaves credential homes readable. The withheld goose path uses `approve` rather than its default
`auto` (auto-approve);
after `session/new` Crew pins `approve` (ask before every tool call) and
refuses if that pin fails. Goose's harness-owned Auto mode has no dashboard
surface and its mode-change endpoint refuses it: Auto suppresses permission RPCs,
so it cannot yet inherit Kiro Crew's governance clamp or expiring safety override. pi
sends `session/request_permission` for privileged tools. OpenCode is seeded
with project `permission: "ask"` in the session
`work_dir` (never `~/.config/opencode`); an explicit `allow` is left alone and
the session refuses unless the ungated-tools opt-out is on. All three start
without that opt-out when ROUTED. Kiro Crew does not implement `fs/*` or
`terminal/*`. Pi may accept Crew MCP on `session/new` without forwarding it to
the pi agent; those tools can stay inert. Doctor reports that as a capability
note, not an install failure. Do not treat spawn or Crew tools as verified on
Pi until the adapter forwards MCP.

Mid-turn steer (`_session/steer`) is kiro-cli and KAS only. On a spec adapter
the dashboard and Slack queue the message as a follow-up (Slack does not wait
on the session semaphore), and `spawn_steer` degrades to follow-up with a
reason that names the harness.
Crew treats goose native resume as unavailable. 1.47's `session/load` RPC
can succeed, but transcript restore is unmeasured, so `spawn_continue`
fail-closes instead of starting a blank child. Regular chat replays
Crew's transcript the same way a provider switch does.

Crew starts goose ACP with its built-in developer extension selected. Goose
1.47 otherwise replaces configured extensions when Crew supplies its MCP servers
on `session/new`, leaving the session without filesystem or terminal tools. Those
developer calls still pass through Goose's `approve` mode and Crew's permission
gate.

**Sign-in is not part of selectability.** Kiro Crew assumes the adapter is already
authenticated and never handles a credential, so a host that has not signed in
gets a readable error telling you which command to run, not a backend quietly
withheld.

> **The preview is intentionally narrow.** Each described adapter resolves its
> own binary — `codex-acp` and `claude-agent-acp` through their npm entry scripts,
> goose through `goose acp`, OpenCode through `opencode acp`, and pi through the
> `pi-acp` binary (not `pi acp`). Only Kiro CLI and KAS are
> selectable in the initial preview. Treat the remaining rows as integration
> evidence, not as a promise that a turn completes.

Each one clears the governability bar differently, which is why it is established
per adapter rather than assumed:

- **goose** asks per privileged tool only after Crew pins session mode
  `approve`. Its default `auto` auto-approves tools, so Crew neither advertises
  nor accepts that mode from the approval picker. **pi** asks via
  `session/request_permission`.
  **OpenCode** is seeded with `permission: "ask"` in the session work_dir
  so those permission frames actually fire (its own default is permissive).
  File I/O stays in the adapter because Kiro Crew does not advertise `fs/*`.
  That is enough to start without the opt-out.
  Crew MCP is delivered to OpenCode and pi when ROUTED; on pi the adapter may
  leave those servers inert until it forwards MCP. Goose establishes its
  `approve` route only after the session exists, so Crew withholds memory, cron,
  spawn, artifacts, and its other MCP tools from goose rather than creating an
  ungated interval.
- **Codex** applies ACP v1 session config `mode=read-only` after `session/new` /
  `session/load` on direct integration paths. That is defense in depth for
  writes, not a security-gate route for reads, so it does not qualify the backend
  for operator selection. Crew MCP stays withheld from Codex sessions too.
- **Claude** is seeded with a permission mode in
  `<work_dir>/.claude/settings.local.json`, the path its own settings resolver
  reads.

## What tools an adapter session gets

kiro-cli receives Kiro Crew's own MCP servers through its agent spec, which it
reads off disk. An adapter reads no Kiro Crew configuration at all, so those
servers are handed over on the session request instead — which is what gives an
adapter session memory, cron, subagents, artifacts and lessons rather than leaving
it a bare vendor agent.

Three deliberate limits on that:

- **Only Kiro Crew's own servers are sent, never the ones you configured.** A user
  MCP server's environment routinely holds tokens and API keys. kiro-cli reads
  those from a file on your machine; an adapter can only be told over the wire, so
  sending them would push your secrets through a third-party binary. Your own MCP
  servers therefore stay available to kiro-cli sessions and are absent from adapter
  sessions.
- **A server you have not enabled is not sent.** Computer use stays out unless it
  is supported and switched on, and dashboard control — which rearranges your
  session layout — is only ever granted to an agent you assigned it to.
- **An adapter Kiro Crew cannot govern gets none of them.** If tool calls cannot be
  shown to reach the security gate, the session still runs on the adapter's own
  tools, but Kiro Crew withholds its own. Running your agent's tools ungoverned is
  a much larger step than running the adapter's, and the log says which happened.

**An adapter outside the selectable set is refused by default.** The ACP registry
lists dozens, and nothing in the ACP handshake reveals whether a given one routes
its tool calls or simply executes locally and reports afterwards — those two look
identical on the wire until it is too late. So an unverified adapter resolves as
*indeterminate* and refuses to start rather than running with the deny rules,
sensitive-path block and governance ceiling silently inert. A registry adapter
that is not one of the hand-described backends (for example `example-acp`) is
that case. Registry metadata remains visible for discovery but never expands the
initial selection set. `pi` is hand-described and routed; do not treat the
registry spelling `pi-acp` as a second unverified path.

You can override that with `agent.acp_backend_allow_ungated_tools`, which is the
single named opt-out. It is off by default and should stay off: with it on, the
adapter can run commands Kiro Crew never checked, and every session logs which
controls are not being enforced.

**Kiro Crew stores no credential for any backend, and does not participate in
sign-in at all.** Each vendor CLI owns its own login and token file. Kiro Crew
checks only whether a token file exists, and treats a missing one as *unknown*
rather than "not signed in": an adapter has more than one way to reach a
credential, and a turn has succeeded on a host with no token file present. So the
doctor reports what it found and names the login command as a hint, without
failing an install that works.

Experimental means what it says: fewer features work, and the list below is
specific about which.

## Turning one on

```bash
kirocrew config set agent.acp_backend codex
kirocrew doctor                               # check the adapter's own rows
```

There is no API-key setting, for any backend, on purpose — these adapters exist to
reuse a subscription you already have.

Existing sessions keep the adapter they started on. New sessions use the new one.
Switching adapters resets global, role, crew-agent, and live-session model
selections because each harness owns a different model namespace. The dashboard
confirmation names that reset before it saves an experimental adapter.

`kirocrew doctor` grows an **ACP Backend** section reporting the adapter, sign-in,
the tool gate, and every capability that differs from kiro-cli. Read it before
your first turn — it is faster than discovering the same facts one failed message
at a time.

## OpenAI Codex

**Install the adapter.** Codex needs the standalone `codex-acp` adapter. The
`codex` CLI on its own is not enough: it treats `acp` as a prompt rather than
starting an ACP server, so Kiro Crew will not use it and will tell you so. Point
`CODEX_ACP_BIN` at the adapter if it is not on your `PATH`.

**Sign in** with `codex login`, which completes a ChatGPT-subscription OAuth flow
in a browser and writes `$CODEX_HOME/auth.json` (default `~/.codex/auth.json`). On
a headless host, either forward the callback port the login flow prints, or copy an
existing `auth.json` onto the machine.

**Kiro Crew applies session config before the first prompt.** After `session/new`
or `session/load`, it writes ACP v1 session config `mode=read-only` and refuses
the session if the adapter does not advertise that value or the write fails.
`read-only` still permits passive reads without a permission prompt. Commands
and writes must ask.

`$CODEX_HOME/config.toml` is not consulted for this decision. An earlier probe of
`approval_policy` resolved ROUTED for a session that did not emit permission
frames.

## Claude Code

Install `@agentclientprotocol/claude-agent-acp` yourself (`npm install -g`) and
sign in with the Claude CLI. Kiro Crew resolves an explicit override, then a
project or `_vendor` `node_modules` copy if one exists, then PATH. The published
wheel does not bundle the adapter.

Tool approval is handled for you: Kiro Crew writes `permissions.defaultMode:
"default"` into the session's `.claude/settings.local.json` when nothing is
configured there, then reads it back to confirm. If you have deliberately set
`auto` or `bypassPermissions`, Kiro Crew leaves your setting alone and refuses the
session instead — your configuration is not overwritten behind your back.

## OpenCode

Install the OpenCode binary yourself (see https://opencode.ai) and sign in with
`opencode auth login`. There is no npm adapter; `install_command` is empty on
purpose.

Tool approval is handled for you: Kiro Crew writes `permission: "ask"` into the
session work_dir (`opencode.json`, or an existing `.opencode/opencode.json`)
when nothing is configured there, then reads it back to confirm. If you have
deliberately set `allow` (or `--auto`), Kiro Crew leaves your setting alone and
refuses the session instead. It never writes `~/.config/opencode`.

## How features differ

| Feature | Claude Code | OpenAI Codex |
|---|---|---|
| Shared subagent runtime | no | no |
| Reasoning effort | works differently (`effort`) | yes (`reasoning_effort`) |
| MCP Tool Search | no | no |
| Agent profiles | prompts and skills work; restricted tools refused | prompts and skills work; restricted tools refused |
| Slash commands | sent as text | sent as text |
| Per-turn usage | partial | partial |
| Cost and limits | session cost and plan limit | no |
| Session resume | yes | yes |
| Model picker | the adapter's own models, no Auto | the adapter's own models, no Auto |

Four consequences worth expecting rather than discovering:

**A custom agent that withholds shell access will be refused.** Neither adapter
can enforce a narrowed tool set on the wire, so running such an agent there would
silently grant it full shell. Use that agent on the default kiro-cli backend.

**Usage numbers are thinner.** Message counts come from Kiro Crew's own records
rather than kiro transcripts, so they count completed turns rather than every
transcript entry, and tool-call counts read zero.

**Your Claude plan limit shows up in the context popover, and only changes when
Claude says it has.** On the Claude Code backend, the popover under the context
ring gains a *Plan limit* section: how close the account is to its rolling window,
when that window resets, and which window it is. The adapter reports it only when
the state changes, so the reading persists between turns rather than refreshing on
each one — and after a page reload it stays hidden until the next turn brings it
back. A per-turn input/output token breakdown is still not available on either
adapter; what you get is the context meter, a cumulative session cost, and this
quota reading.

**The model picker has no Auto entry, and is empty until a chat starts.** Auto is
a kiro model id, not something every agent understands: an adapter rejects it, so
offering it would leave one unusable entry on the menu. The picker instead lists
exactly the models the adapter itself reported when it opened a session — which is
why it stays empty until the first chat on that backend, and fills in once one is
running. Switching back to kiro restores Auto.

## Going back

```bash
kirocrew config set agent.acp_backend ""
```

Nothing needs migrating. Sessions recorded under another backend stay attributed
to it and are not rewritten.

## If something looks wrong

Start with `kirocrew doctor`. Its ACP Backend section distinguishes the five things
that actually differ: the adapter was not found, no sign-in token was located,
tool calls would bypass the gate, a capability is unavailable, or it has not been
verified for that adapter. Note the second one is a *reading*, not a verdict — a
missing token file
does not mean the adapter cannot authenticate, so it is reported without failing
the check.

There is one setting that turns the tool-gate refusal off,
`agent.acp_backend_allow_ungated_tools`. It is off by default and should stay off:
with it on, an adapter can run commands Kiro Crew never checked. Doctor reports it
as a problem whenever it is enabled, including when nothing is currently being
bypassed, because it disarms the refusal for future sessions too.
