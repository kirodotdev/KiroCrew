# Running the agent on an Anthropic-compatible LLM endpoint

Kiro Crew's model requests normally follow your kiro-cli account. If you have
your own endpoint that speaks the Anthropic API — DeepSeek, a self-hosted
gateway, or another provider's Anthropic-compatible surface — you can point the
agent at it by selecting the `claude` harness. The harness is
`claude-agent-acp`, a public npm package that delegates the model turn to the
Claude Code agent SDK, which honors the standard `ANTHROPIC_*` environment
variables.

With this backend selected, your chat and worker model turns go to **your**
endpoint with **your** credentials. The dashboard keeps its own token
authentication, messaging channels keep their own bot or channel credentials,
and the kiro-cli sign-in remains for kiro-cli-specific features. Some
agent-internal and background paths still run on the kiro-cli sign-in —
crew-member DM threads follow `agent.member_acp_backend` (default `"kas"`)
rather than `acp_backend`, and other internal pools may too; anything pinned
to the kiro harness always does. Set `agent.member_acp_backend` as well if
members should use your endpoint.

The contract this guide describes is
[claude-code-provider.md](../system-specs/modules/claude-code-provider.md);
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
one is absent and, when the adapter is the missing half, the command that
installs it.

```bash
npm install -g @agentclientprotocol/claude-agent-acp
```

The harness also needs a `claude` CLI on `PATH` — `claude-agent-acp`'s SDK
delegates the turn to it and does not search `PATH` itself. If the binary
resolves for you in a terminal but not for a GUI-launched app (macOS apps get
a minimal `PATH`), pin it explicitly (see step 3).

## 3. Point the environment at your endpoint

The gateway forwards inherited `ANTHROPIC_*` and `CLAUDE_CODE_*` variables to
the harness child; see the contract in
[claude-code-provider.md](../system-specs/modules/claude-code-provider.md). Set
the non-secret routing variables before the gateway starts:

```bash
export ANTHROPIC_BASE_URL="https://api.deepseek.com/anthropic"   # your endpoint
export ANTHROPIC_MODEL="deepseek-v4-pro"                         # main model
export CLAUDE_CODE_SUBAGENT_MODEL="deepseek-v4-flash"            # subagent model
```

**Recommended — use Claude Code's `apiKeyHelper`.** In
`~/.claude/settings.json`, point Claude Code at a script that prints the
endpoint key after reading it from a real secret store such as the OS keychain,
`pass`, or a cloud secrets manager:

```json
{
  "apiKeyHelper": "/absolute/path/to/print-endpoint-key"
}
```

Claude Code sends the helper's stdout in the `X-Api-Key` and
`Authorization: Bearer` headers on model requests. It re-invokes the helper on
the interval set by `CLAUDE_CODE_API_KEY_HELPER_TTL_MS`, which defaults to five
minutes:

```bash
export CLAUDE_CODE_API_KEY_HELPER_TTL_MS="300000"
```

The helper does not run in your shell: Crew's subprocess sandbox wraps the
harness child and every descendant, the helper included (the `claude` harness
carries no internal sandbox of its own), and that sandbox hides the home
directories the common secret stores read from. In the default (`standard`)
sandbox a helper that shells out to `pass` or to a cloud-CLI secrets manager
(`gcloud secrets`, `az keyvault`) reads from a masked home and comes back
empty, so the model request fails auth and keeps failing every
`CLAUDE_CODE_API_KEY_HELPER_TTL_MS`. Pick a source that survives the sandbox:
the macOS keychain, or any store that keeps its material outside your home
directory (or that you hand the helper explicitly). The exact directories the
sandbox masks — and the stricter set under `strict`/`cc` mode — are listed
beside `_STANDARD_DIRS` in `sandbox.py`; see
[claude-code-provider.md](../system-specs/modules/claude-code-provider.md).

Do **not** export `ANTHROPIC_AUTH_TOKEN` alongside this setup; an environment
token takes precedence, so the helper is not consulted. The helper keeps the
raw token out of the ambient environment inherited by every agent-run child,
but it does not make the credential unreachable to code running as the same
user: user-level processes can read or execute the helper and may be able to
access the same secret store.

**Fallback — put the token in the gateway environment.** If a helper is not
available, export the key with the routing variables:

```bash
export ANTHROPIC_AUTH_TOKEN="sk-..."   # your endpoint's key
```

This works, but the harness child and every agent-run child process inherit the
raw token on every spawn path. A prompt-injected command can read it.

How the gateway is launched decides where the variables live:

- **macOS desktop app** — GUI launches do not read your shell profile. Use
  `launchctl setenv` so the app (and the gateway it spawns) inherits them,
  then relaunch the app. Pinning the binaries is needed only when they live
  somewhere unusual: the gateway resolves them through its own augmented
  search path, which already covers Homebrew, the npm global bin, and
  version-manager shims (mise, nvm, fnm, volta). Set `ANTHROPIC_AUTH_TOKEN` only for
  the fallback path; omit it when using `apiKeyHelper`:

  ```bash
  launchctl setenv ANTHROPIC_BASE_URL "https://api.deepseek.com/anthropic"
  launchctl setenv ANTHROPIC_MODEL "deepseek-v4-pro"
  launchctl setenv CLAUDE_CODE_SUBAGENT_MODEL "deepseek-v4-flash"
  launchctl setenv CLAUDE_CODE_API_KEY_HELPER_TTL_MS "300000"
  launchctl setenv ANTHROPIC_AUTH_TOKEN "sk-..." # fallback only
  launchctl setenv CLAUDE_CODE_EXECUTABLE "$(command -v claude)"
  launchctl setenv CLAUDE_AGENT_ACP_BIN "$(command -v claude-agent-acp)"
  ```

  Be aware that `launchctl setenv` makes the raw token readable via
  `launchctl getenv ANTHROPIC_AUTH_TOKEN` by every process in your login
  session; see [secrets-env.md](secrets-env.md) for less exposed ways to
  deliver secrets.

  The vault flow in that guide cannot supply this endpoint token: it injects a
  secret only into a bound MCP server's environment, not the agent or harness
  environment.

- **systemd service** — use an `EnvironmentFile=` unit as described in
  [secrets-env.md](secrets-env.md), or edit the service environment directly.
  Store `ANTHROPIC_AUTH_TOKEN` there only for the fallback path.

- **gateway started from a terminal** — the exported routing variables and
  helper TTL above are enough for the recommended path. Export
  `ANTHROPIC_AUTH_TOKEN` only for the fallback path.

The variables reach the harness child on every spawn path, so a gateway
restart is all that is needed after a change.

## 4. Models

The endpoint's own catalog decides what runs. A model advertised by the
endpoint runs even when it is absent from Kiro Crew's shipped model registry:
the harness keeps the verbatim id and resolves its capabilities from the SDK's
unfiltered list. `ANTHROPIC_MODEL` (and your `~/.claude/settings.json` model)
is what the SDK resolves when Kiro Crew does not pin a model.

- A chat slot on **auto** uses `ANTHROPIC_MODEL` only until the endpoint's
  advertised models have been cached (the first session); after that, Crew
  seeds them as the settings file's `availableModels` and auto resolves to
  the head of that list.
- Pin per-slot from the dashboard model chip; the pin rides on
  `session/set_config_option` and wins over the environment.
- `CLAUDE_CODE_SUBAGENT_MODEL` applies to subagents spawned by the harness
  itself.

## 5. What stays on the Kiro account

- The kiro-cli sign-in remains for kiro-cli-specific features; dashboard
  authentication uses its own tokens, and messaging channels (Slack, Discord,
  …) use their own bot or channel credentials.
- Anything explicitly pinned to the kiro harness (per-agent `acp_backend`, or
  a session that predates the switch and resumes on kiro-cli) still sends its
  model requests to the Kiro account.
- Kiro-side features the harness does not implement (kiro-cli's own tool
  search, kiro-specific MCP routing) are not available on `claude` sessions.
- A `claude` session **does receive Crew's MCP servers**: Crew fills the
  session's `mcpServers` array per spawn from the materialized agent spec, so
  Crew's control plane is mounted whenever Crew authors the session's
  `.claude/settings.local.json`; a governed registry-mode install may
  withhold spec-referenced servers. See "MCP tools on a Claude session" in
  [claude-code-provider.md](../system-specs/modules/claude-code-provider.md)
  for the exact rules. A project that already carries its own
  `.claude/settings.local.json` is the exception: Crew did not author the file
  that governs tool use there, so that session gets no seed and no
  `mcpServers` array at all. Tool calls pre-approved in your own `~/.claude`
  settings (including a `.claude/settings.json` inside a cloned project) never
  reach Crew's approval path, so Crew's deny rules and audit log do not see
  them.
