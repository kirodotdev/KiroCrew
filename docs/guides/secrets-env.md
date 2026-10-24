# Passing secrets to MCP servers

MCP servers often need API keys, database passwords, or other secrets at
runtime. Their specs can live in `~/.kiro/crew/mcp.json` (Kiro Crew scope) or
`~/.kiro/settings/mcp.json` (Kiro global scope), and either file may be shared or
copied with an agent configuration. Plain `env` values in those files are not
protected, so use vault references instead.

> **Use the encrypted vault.** It is the supported route: a secret is stored
> encrypted on disk and resolved into the bound MCP server's environment alone,
> never the agent's. Service-level environment files and root-owned wrappers do
> not provide that same per-server boundary; the section below explains why.

---

## The encrypted vault

A service-level environment value enters the gateway before any MCP server is
selected. The encrypted vault avoids that gap: secrets are stored encrypted on
disk under `.vault` in the data home, and a `secret://NAME` reference in an MCP
server's env is resolved to the real value only at spawn time, injected into that
server's environment alone — never the agent's.

Store a secret through the dashboard **Settings → Secrets** tab, then reference
it from `~/.kiro/crew/mcp.json` (or from the global
`~/.kiro/settings/mcp.json`):

```jsonc
{
  "mcpServers": {
    "my-server": {
      "command": "my-mcp-server",
      "env": { "MY_MCP_SECRET": "secret://MY_MCP_SECRET" }
    }
  }
}
```

At spawn the gateway resolves `secret://MY_MCP_SECRET` from the vault.  If the
named secret does not exist, the server fails to start rather than launching
with a missing credential.

### Route the server through the gateway

The gateway is the only place a reference is resolved, so **route the server
through the gateway**: in **Developer → MCP Management**, turn routing on for
the server (this adds it to `mcp_gateway.stub_servers`). For the servers the
default agent mounts (from either `mcp.json` above), an unrouted server with a
`secret://` value is not started at all, and a warning names the env key —
rather than starting it with the literal `secret://` text as its credential.
Routing takes effect at the next gateway start, as any routing change does,
and the server stays off until then: it counts as routed only once the running
gateway serves it. Switching the server on in MCP Management, or **Enable
all**, does not start it before that either. In the MCP servers table under
**Customize → Connections**, its row reads Outdated and states why: the
dashboard does not probe it, and the line names the env key that holds the
`secret://` value. On a platform where the MCP
gateway does not run, no reference can be resolved: put the credential in the
server's `env` directly instead.
An agent file you maintain by hand under `~/.kiro/agents/` is launched as
written, so route any server it declares with a `secret://` value.

Such a server is kept in `~/.kiro/agents/kirocrew.json` with
`"disabled": true` and `"x-kirocrew-withheld": true`. If you roll back to a
release without this check, that release's rebuild switches a server declared
in either `mcp.json` back on by itself. A server declared only in
`kirocrew.json` stays off: the older release reads its `disabled` as your own
switch-off and lists no row for it in MCP Management. To clear it, delete both
keys from its entry in that file. Either way the older release can again start
the server with the literal `secret://` text as its credential.

### Managed secrets in Settings

`GET /api/secrets` returns every stored name plus a `managed` catalog. A managed
entry contains `name` and a stable machine-readable `kind`; per-host entries
also include the normalized non-secret `host` so the dashboard can identify the
row. Secret values remain write-only and configured state is derived from membership in the
response's existing `names` list. If managed integration configuration cannot
be read, stored names are still returned and `managed_error` is `true`; the
dashboard surfaces that non-fatal warning instead of misrepresenting the empty
catalog as “no managed integrations enabled.”
`WAKATIME_API_KEY` is advertised only when `config.wakatime.enabled` is true,
matching the client-construction gate. With exactly one raw configured Jira
entry whose normalized host is non-empty, the global `JIRA_API_TOKEN` slot is
advertised.
With multiple entries, only each normalized host's exact `JIRA_TOKEN_<HEX>` slot
is advertised; duplicate normalized hosts produce one display row but still
count as multiple runtime entries, so they never accidentally enable the global
fallback. For a single host, an already-stored per-host token remains visible
because the runtime gives it precedence.

Gerrit follows the same rule from `dashboard.gerrit_hosts`, as kind
`gerrit_http_credential` with `host` set on every row: one listed host is
offered the global `GERRIT_HTTP_CREDENTIAL` slot (or its already-stored
`GERRIT_HTTP_CREDENTIAL_<HEX>`, which the provider prefers); two or more hosts
are each offered their own `GERRIT_HTTP_CREDENTIAL_<HEX>` row. The dashboard
labels each row by host, so the hex-suffixed name is never typed by hand.

A stored Connections OAuth client secret,
`KIROCREW_CONNECTIONS_<SLUG>_CLIENT_SECRET`, appears in the catalog as kind
`connections_client_secret`, with `host` set to the provider slug. It is listed
only once stored. The Secrets routes do not edit it: a write or delete of that
name returns `409` with code `managed_by_connections`; change it under
**Settings → OAuth Apps**.

When a stored integration name is one the runtime will not read, the response
also carries an `unused` list of `{name, reason}` entries. The reasons are
`wakatime_disabled` (`WAKATIME_API_KEY` stored while WakaTime is off),
`jira_multi_host` (`JIRA_API_TOKEN` stored with more than one Jira host),
`jira_host_precedence` (`JIRA_API_TOKEN` stored beside the single host's own
`JIRA_TOKEN_<HEX>`, which wins), and their Gerrit counterparts
`gerrit_multi_host` and `gerrit_host_precedence` for `GERRIT_HTTP_CREDENTIAL`.

The catalog is deliberately based on **actual vault consumers**, not the broader
set of credential names accepted from `.env`. Most channel credentials still
read literal environment/config values, so storing a same-named vault entry does
not make those channels consume it. An unrecognized vault name remains listed
and fully manageable; it is never hidden or reclassified merely because it looks
credential-like.

### Migrating existing plaintext secrets

If you already keep a credential as a plaintext `KEY=VALUE` line in the data
home's `.env`, the importer moves it into the vault. It migrates the credential
keys whose every reader resolves a `secret://` reference: the Jira tokens (the
global `JIRA_API_TOKEN` and per-host `JIRA_TOKEN_<hex>`) and the channel secrets
(`SLACK_APP_TOKEN`, `SLACK_BOT_TOKEN`, `DISCORD_BOT_TOKEN`, `TELEGRAM_BOT_TOKEN`,
`WEBEX_BOT_TOKEN`, `MICROSOFT_APP_PASSWORD`, `WECOM_SECRET`, `WEIXIN_TOKEN`,
`FEISHU_APP_SECRET`). Identifiers such as app ids and the owner id stay in
`.env`, and so do `KIRO_API_KEY` and `AZURE_DEVOPS_EXT_PAT`, which a child
process reads from its own environment:

```bash
# Dry run (default): report what WOULD migrate, change nothing.
kirocrew secrets import

# Apply: store the tokens in the vault and rewrite each .env line
# to a secret:// reference.
kirocrew secrets import --apply
```

The importer reads only the data-home `.env` (there is no `--file` option, so a
caller cannot point it at an attacker-controlled file). A key whose value is
overridden in the process environment is skipped, and the `.env` rewrite aborts
if the file changes under a concurrent writer.

Every other credential key and unrecognized operator setting is left untouched.
On `--apply` each migrated line becomes `KEY=secret://KEY`, so the resolver
picks the value up from the vault.

The importer **does not delete** the `.env` file — it rewrites the migrated
lines in place, so the plaintext value for those keys is replaced by the
`secret://` reference.  A line that is already a `secret://` reference is left
alone, so re-running `--apply` is a no-op.  If you keep a separate backup copy
of the file, delete that plaintext copy once you have verified the migration.

### Channel tokens and the vault

A channel token saved from the dashboard (Settings > Channels) is stored in the
vault under its key name, and its `.env` line is written as
`KEY=secret://KEY`. A token already in `.env` as plaintext keeps working as it
is: every channel reader accepts either form, and a hand-edited
`KEY=secret://NAME` line resolves the vault secret `NAME`. A reference that
cannot be resolved (no such vault entry, or a vault that cannot be read) leaves
the channel unconfigured and logs the key once; the log never carries the
token. Clearing a channel token from the dashboard also removes the vault entry
that its `secret://KEY` line pointed at.

A release that predates this behaviour reads a `KEY=secret://KEY` line as the
token itself, so after a downgrade the channels saved this way fail to
authenticate. To go back, re-enter each channel token under Settings > Channels
on the older release (vault values cannot be read back), or put the plaintext
token on its `.env` line by hand before downgrading.

### Jira uses the vault automatically

The Jira integration reads its API token from the vault first, then falls back
to the legacy `.env` / environment value.  Per host it looks up the vault
secret `JIRA_TOKEN_<HEX>` (the hex-encoded host name); for a single configured
host it also accepts the global `JIRA_API_TOKEN` vault secret.  If neither vault
entry exists it uses the same `.env` value it always has, so existing setups
keep working without change — run `kirocrew secrets import --apply` to move the
Jira token into the vault when you are ready.

### Remote (URL) servers: headers are not resolved

A `secret://` reference is resolved only in a **stdio** server's `env`. A remote
server (`"url": ...`) authenticates with `headers`, and the session runtime reads
those as written, so a reference such as
`"headers": { "x-api-key": "secret://MY_TOKEN" }` would reach the server as the
literal text `secret://MY_TOKEN`.

Kiro Crew does not send it silently:

- The MCP server probe (the dashboard's MCP server status) reports the server as
  an error naming the header, and does not contact the server.
- At session start the launch log has one warning naming the server and header,
  and the session's MCP problem summary lists it.

On the kiro-cli backend, use an environment reference in the header and set the
variable in `~/.kiro/crew/.env`. kiro-cli expands `${VAR}` and `${env:VAR}` in a
remote server's headers at session time (see the `headers` example in
[kiro-cli MCP configuration](../reference/kiro-cli/mcp/configuration.md)):

```jsonc
{
  "mcpServers": {
    "my-remote": {
      "url": "https://mcp.example.com/mcp",
      "headers": { "x-api-key": "${env:MY_TOKEN}" }
    }
  }
}
```

The value then lives in `.env` rather than the encrypted vault, with the
limits described under [What NOT to do](#what-not-to-do).

Other backends (Claude, Codex, OpenCode, Goose, DeepSeek) receive a remote
server's headers from Kiro Crew as written, and Kiro Crew does not rely on them
expanding `${env:}`; the KAS backend does not receive declared headers. On those
backends there is no documented way to supply a header value from the vault or
from `.env` yet.

---

## Why service-level environment variables are not an MCP-only fallback

The built-in Linux service reads `/etc/kirocrew/kirocrew.env`, but every value in
that file enters the gateway process first. An unknown credential key then
reaches the agent unless the source build adds it to `_AGENT_DENIED_ENV_KEYS`.
Adding it to that scrub list is not a portable MCP delivery mechanism either: a
server launched as a direct descendant of the agent loses the variable with its
parent, while the optional pooled MCP topology has a different trusted-side
environment path. The result depends on topology instead of the server spec.

A root-owned per-server wrapper does not solve this. The built-in service runs
the entire gateway as the invoking `User=` / `Group=`; it does not start as root
and drop privileges only for agent subprocesses. A wrapper launched by the
gateway cannot read a root-owned mode-`0600` file, while making that file readable
by the service user also makes it readable by the agent running as the same user.
Running the gateway as root would instead give the agent and every gateway child
root privileges.

Use `secret://` for per-server injection. It behaves the same in supported MCP
topologies and resolves the value only on the trusted side of the server spawn.

---

## Non-secret environment for the agent: `agent.env`

`agent.env` in `config.json` sets environment variables on every agent process
Kiro Crew starts for a chat session, background session or subagent, on every
backend, so shell tool calls and the MCP servers the agent starts inherit them.
The gateway itself and MCP servers the gateway hosts do not get them. Use it for configuration, never for credentials:

```bash
kirocrew config set agent.env '{"HTTPS_PROXY": "http://proxy.example:3128", "NO_PROXY": "localhost,127.0.0.1", "NODE_EXTRA_CA_CERTS": "/etc/ssl/certs/corp.pem", "AWS_PROFILE": "dev"}'
```

- A value here wins over the same variable in the gateway's own environment;
  the variables Kiro Crew sets for the session itself still win over it, except
  defaults Kiro Crew only fills in when a name is unset (such as
  `agent.child_env_defaults`), which this map overrides.
- A name this map refuses is rejected when you save it: `kirocrew config set`
  (and any dashboard write) fails with the key and the reason, and nothing is
  written. A refused name that a hand edit puts in `config.json` is ignored with
  a warning naming the key.
- Values are stored in plaintext, so a credential-shaped name is refused: one
  with a name segment that is, or ends in, `KEY`, `TOKEN`, `SECRET`, `PASSWORD`,
  `PASSWD` or `CREDENTIAL` (`API_KEY`, `GITHUB_TOKEN`, `APIKEY`). A name that only
  contains such a word, such as `GOPRIVATE` or `TOKENIZERS_PARALLELISM`, is
  accepted. Put credentials in the vault and reference them with `secret://`.
- Names Kiro Crew or the sandbox owns (`PATH`, `HOME`, `TMPDIR`, `KIROCREW_*`,
  `KIRO_*`, `OPENCODE_*`, and similar) are refused. So are the names known to
  make a shell, the dynamic loader, a language runtime or a tool the agent runs
  execute code the command did not name: `LD_*`, `DYLD_*`, `PYTHON*`,
  `NODE_OPTIONS`, `BASH*`, `SHELLOPTS`, `PS4`, `ZDOTDIR`, `IFS`, `SSH_*`, `GIT_*`
  (except `GIT_TERMINAL_PROMPT` and `GIT_SSL_CAINFO`), and any name ending in
  `PATH`, `LIB`, `OPT`, `OPTS`, `OPTIONS`, `FLAGS`, `COMMAND`, `SHELL`,
  `EXECUTABLE`, `EDITOR`, `PAGER`, `ASKPASS` or `BROWSER`. That includes the
  location of a config file a tool may read a program from: `KUBECONFIG`,
  `AWS_CONFIG_FILE`, and any name ending in `CONFIG`, `CONFIG_DIR`,
  `CONFIG_FILE` or `CONFIG_HOME` (`GH_CONFIG_DIR`, `OPENCODE_CONFIG_DIR`). This
  is a denylist: it narrows what a write to `config.json` can attach to later
  commands, and it cannot list every tool's variables. Choosing a toolchain
  (`JAVA_HOME`, `CC`) is accepted. Also refused: the variables that move a
  coding agent's sign-in directory (`CLAUDE_CONFIG_DIR`, `CODEX_HOME`,
  `XDG_CONFIG_HOME`, `XDG_DATA_HOME`, and similar) and any name the agent
  environment scrub removes. Set a name such as `GIT_SSH_COMMAND` in the
  gateway's own environment instead.
- A change applies to agent processes started after it: warm pooled processes
  and the shared background process are replaced. A session that is already
  running, and subagents sharing its process, keep the environment that process
  started with.

---

## What NOT to do

- **Do not** put secrets as plain string values inside
  `~/.kiro/crew/mcp.json` or `~/.kiro/settings/mcp.json` — either file is easy
  to copy or share with an agent configuration.
- **Do not** add custom keys to `~/.kiro/crew/.env` expecting them to be
  agent-isolated — the gateway loads them and propagates them to all child
  processes including the agent. A warning is logged, but the key still reaches
  the process tree. Use the vault instead.
- **Do not** use `.env` just to give the agent non-secret settings such as a
  proxy, a CA bundle or `AWS_PROFILE`. Set them in `agent.env` instead (see
  above): they then reach only the agent and what it runs, not the gateway.
- **Do not** store MCP secrets in user-readable paths — a file at
  `~/.kiro/crew/mcp-secrets.env` or `~/.kiro/.env` is accessible to the agent
  via filesystem reads. A root-owned service environment file does not fix the
  runtime boundary: its values enter the gateway before server selection. Use
  the vault.
