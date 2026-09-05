# Running the agent on an Anthropic-compatible LLM endpoint

Kiro Crew's model requests normally follow your kiro-cli account. If you have
your own endpoint that speaks the Anthropic API — DeepSeek, a self-hosted
gateway, or another provider's Anthropic-compatible surface — you can point the
agent at it by selecting the `claude` harness. The harness is
`claude-agent-acp`, a public npm package that delegates the model turn to the
Claude Code agent SDK, which honors the standard `ANTHROPIC_*` environment
variables.

With this backend selected, model requests go to **your** endpoint with **your**
credentials. The kiro-cli sign-in remains in use for everything else the Kiro
account owns (dashboard auth, messaging channels, kiro-cli-specific features),
but no model request reaches the Kiro account.

The contract this guide describes is
[claude-code-provider.md](../system-specs/features/claude-code-provider.md);
the dashboard's backend probe reports which prerequisite is missing on a given
machine.

---

## 1. Select the harness

In `~/.kiro/crew/config.json`, under `agent`:

```json
{
  "agent": {
    "acp_backend": "claude"
  }
}
```

`acp_backend` selects the harness *inside* the ACP provider — `agent.provider`
stays `"acp"`. An unrecognized value degrades to the kiro harness
(the empty string) at config load, so a typo cannot brick the gateway.

New chat sessions spawn `claude-agent-acp` as their subprocess. Background
workers (title generation, suggestions, memory consolidation) follow the same
selection automatically: they run on the provider-backed path for any non-kiro
harness, so no second setting is needed.

## 2. Install the harness prerequisites

Both are npm- or CLI-installable; the dashboard's backend status reports which
one is absent and the command that installs it.

```bash
npm install -g @agentclientprotocol/claude-agent-acp
```

The harness also needs a `claude` CLI on `PATH` — `claude-agent-acp`'s SDK
delegates the turn to it and does not search `PATH` itself. If the binary
resolves for you in a terminal but not for a GUI-launched app (macOS apps get
a minimal `PATH`), pin it explicitly (see step 3).

## 3. Point the environment at your endpoint

The gateway passes its inherited environment through to the harness child
(`AcpClient._spawn` merges it into the child environment, and the spawn scrub
list does not cover `ANTHROPIC_*`). Set these before the gateway starts:

```bash
export ANTHROPIC_BASE_URL="https://api.deepseek.com/anthropic"   # your endpoint
export ANTHROPIC_AUTH_TOKEN="sk-..."                             # your endpoint's key
export ANTHROPIC_MODEL="deepseek-v4-pro"                         # main model
export CLAUDE_CODE_SUBAGENT_MODEL="deepseek-v4-flash"            # subagent model
```

How the gateway is launched decides where they live:

- **macOS desktop app** — GUI launches do not read your shell profile. Use
  `launchctl setenv` so the app (and the gateway it spawns) inherits them,
  then relaunch the app. Pin the binaries as well, since the GUI `PATH` does
  not include Homebrew or npm global bin:

  ```bash
  launchctl setenv ANTHROPIC_BASE_URL "https://api.deepseek.com/anthropic"
  launchctl setenv ANTHROPIC_AUTH_TOKEN "sk-..."
  launchctl setenv ANTHROPIC_MODEL "deepseek-v4-pro"
  launchctl setenv CLAUDE_CODE_SUBAGENT_MODEL "deepseek-v4-flash"
  launchctl setenv CLAUDE_CODE_EXECUTABLE "$(command -v claude)"
  launchctl setenv CLAUDE_AGENT_ACP_BIN "$(command -v claude-agent-acp)"
  ```

- **systemd service** — use an `EnvironmentFile=` unit as described in
  [secrets-env.md](secrets-env.md), or edit the service environment directly.

- **gateway started from a terminal** — the exported variables above are
  enough; the gateway and every harness child inherit them.

The variables reach the harness child on every spawn path, so a gateway
restart is all that is needed after a change.

## 4. Models

The endpoint's own catalog decides what runs. A model advertised by the
endpoint runs even when it is absent from Kiro Crew's shipped model registry:
the harness keeps the verbatim id and resolves its capabilities from the SDK's
unfiltered list. `ANTHROPIC_MODEL` (and your `~/.claude/settings.json` model)
is what the SDK resolves when Kiro Crew does not pin a model.

- Leave the chat slot's model on **auto** to use `ANTHROPIC_MODEL`.
- Pin per-slot from the dashboard model chip; the pin rides on
  `session/set_config_option` and wins over the environment.
- `CLAUDE_CODE_SUBAGENT_MODEL` applies to subagents spawned by the harness
  itself.

## 5. What stays on the Kiro account

- Dashboard authentication and messaging channels (Slack, Discord, …) still
  use the kiro-cli sign-in.
- Anything explicitly pinned to the kiro harness (per-agent `acp_backend`, or
  a session that predates the switch and resumes on kiro-cli) still sends its
  model requests to the Kiro account.
- Kiro-side features the harness does not implement (kiro-cli's own tool
  search, kiro-specific MCP routing) are not available on `claude` sessions.
- A `claude` session starts with **no Crew MCP tools**: the harness reads none
  of the agent spec's `mcpServers`, so the session's MCP array is empty by
  default. Tool calls pre-approved in your own `~/.claude` settings (including
  a `.claude/settings.json` inside a cloned project) never reach Crew's
  approval path, so Crew's deny rules and audit log do not see them. Both
  boundaries are disclosed on the dashboard's Agent Backend panel and in
  [claude-code-provider.md](../system-specs/features/claude-code-provider.md).
