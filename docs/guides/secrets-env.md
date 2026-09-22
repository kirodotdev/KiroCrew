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

The catalog is deliberately based on **actual vault consumers**, not the broader
set of credential names accepted from `.env`. Most channel credentials still
read literal environment/config values, so storing a same-named vault entry does
not make those channels consume it. An unrecognized vault name remains listed
and fully manageable; it is never hidden or reclassified merely because it looks
credential-like.

### Migrating existing plaintext secrets

If you already keep the Jira API token as a plaintext `KEY=VALUE` line in the
data home's `.env`, the importer moves it into the vault. It migrates ONLY the
Jira credential keys the vault-aware Jira consumer reads — the global
`JIRA_API_TOKEN` and per-host `JIRA_TOKEN_<hex>` tokens; other credential keys
are left untouched because their consumers still read the literal `.env` value:

```bash
# Dry run (default): report what WOULD migrate, change nothing.
kirocrew secrets import

# Apply: store the Jira token(s) in the vault and rewrite each .env line
# to a secret:// reference.
kirocrew secrets import --apply
```

The importer reads only the data-home `.env` (there is no `--file` option, so a
caller cannot point it at an attacker-controlled file). A key whose value is
overridden in the process environment is skipped, and the `.env` rewrite aborts
if the file changes under a concurrent writer.

Only the Jira credential keys are migrated (`JIRA_API_TOKEN` and per-host
`JIRA_TOKEN_<HEX>`); every other credential key and unrecognized operator
setting is left untouched.  On `--apply` each migrated line becomes
`KEY=secret://KEY`, so the resolver picks the value up from the vault.

The importer **does not delete** the `.env` file — it rewrites the migrated
lines in place, so the plaintext value for those keys is replaced by the
`secret://` reference.  A line that is already a `secret://` reference is left
alone, so re-running `--apply` is a no-op.  If you keep a separate backup copy
of the file, delete that plaintext copy once you have verified the migration.

### Jira uses the vault automatically

The Jira integration reads its API token from the vault first, then falls back
to the legacy `.env` / environment value.  Per host it looks up the vault
secret `JIRA_TOKEN_<HEX>` (the hex-encoded host name); for a single configured
host it also accepts the global `JIRA_API_TOKEN` vault secret.  If neither vault
entry exists it uses the same `.env` value it always has, so existing setups
keep working without change — run `kirocrew secrets import --apply` to move the
Jira token into the vault when you are ready.

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

## What NOT to do

- **Do not** put secrets as plain string values inside
  `~/.kiro/crew/mcp.json` or `~/.kiro/settings/mcp.json` — either file is easy
  to copy or share with an agent configuration.
- **Do not** add custom keys to `~/.kiro/crew/.env` expecting them to be
  agent-isolated — the gateway loads them and propagates them to all child
  processes including the agent. A warning is logged, but the key still reaches
  the process tree. Use the vault instead.
- **Do not** store MCP secrets in user-readable paths — a file at
  `~/.kiro/crew/mcp-secrets.env` or `~/.kiro/.env` is accessible to the agent
  via filesystem reads. A root-owned service environment file does not fix the
  runtime boundary: its values enter the gateway before server selection. Use
  the vault.
