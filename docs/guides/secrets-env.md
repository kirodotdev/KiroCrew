# Passing secrets to MCP servers

MCP servers often need API keys, database passwords, or other secrets at
runtime.  Kiro Crew deliberately keeps secrets **out** of
`~/.kiro/mcp.json` (which is versioned and may be shared across machines).

> **Security note:** Both routes below are **interim workarounds** pending
> the encrypted vault (planned — see
> [issue tracker](https://github.com/kirodotdev/KiroCrew/issues/2351)).
> They deliver the secret to the MCP server subprocess, but a
> prompt-injected agent running in the same process tree can observe
> environment variables that the sandbox does not explicitly scrub.  Use
> these only when you accept that risk; the vault will close this gap by
> resolving secrets at spawn time without exposing them to the agent.

---

## Route 1: systemd service unit `EnvironmentFile=`

If you run Kiro Crew as a systemd service (see
[remote-and-mobile.md](remote-and-mobile.md)), point the unit at a
protected secrets file:

```ini
# /etc/systemd/system/kirocrew.service.d/secrets.conf
[Service]
EnvironmentFile=/etc/kirocrew/secrets.env
```

Create the secrets file with owner-only access:

```bash
sudo install -m 600 /dev/null /etc/kirocrew/secrets.env
# Use an editor or redirect from a non-history source to avoid
# leaving the token in shell history:
sudo sh -c 'read -rp "Secret: " val && printf "MY_MCP_SECRET=%s\n" "$val" >> /etc/kirocrew/secrets.env'
```

Then reload and restart:

```bash
sudo systemctl daemon-reload
sudo systemctl restart kirocrew
```

The variables are visible to the gateway process and its MCP server
children.  The file itself (`/etc/kirocrew/secrets.env`) is owned by root
with mode `0600`, so the agent cannot read it via filesystem access.

**Required:** after adding a secret, you **must** also add its key name to
`_AGENT_DENIED_ENV_KEYS` in `src/kiro_crew/sandbox.py` to prevent the
agent subprocess from inheriting it.  Without this step, the variable
propagates through `AcpClient._spawn()` and a prompt-injected agent can
read it from its own environment.

> The encrypted vault (PR 1+) will eliminate this manual step — secrets
> resolved via `secret://` are injected only into the bound MCP server's
> env, never the agent's.

---

## Route 2: per-server shell wrapper with a root-owned secrets file

Source a dedicated secrets file in the server's `command` array using a
shell wrapper.  The file **must** be owned by root with mode `0600` so
the agent (running as your user) cannot read it:

```jsonc
// ~/.kiro/mcp.json
{
  "mcpServers": {
    "my-server": {
      "command": "sh",
      "args": [
        "-c",
        "set -a; . /etc/kirocrew/mcp-secrets.env; set +a; exec my-mcp-server --stdio"
      ]
    }
  }
}
```

**How it works:**

| Fragment | Purpose |
|---|---|
| `set -a` | Auto-export every variable assigned after this point. |
| `. /etc/kirocrew/mcp-secrets.env` | Source secrets from a root-owned file. |
| `set +a` | Stop auto-exporting (keeps the child env minimal). |
| `exec …` | Replace the shell with the actual server process. |

Create the secrets file with root ownership:

```bash
sudo install -m 600 /dev/null /etc/kirocrew/mcp-secrets.env
sudo sh -c 'read -rp "Secret: " val && printf "MY_MCP_SECRET=%s\n" "$val" >> /etc/kirocrew/mcp-secrets.env'
```

The gateway process (running as root under systemd) can read the file.
Agent subprocesses are spawned as a non-root user (`User=` in the service
unit), so they cannot read the root-owned file.

> **Important:** the systemd unit **must** set `User=<your-user>` for the
> agent subprocess isolation to hold.  Running the entire gateway as root
> without dropping privileges would give the agent root access too.

> **Agent exposure caveat:** the MCP server receives the variable via its
> process environment; the agent shares that process tree and can observe
> the variable unless it is scrubbed by the sandbox.  This route protects
> the **file** from agent reads but not the **runtime value** from agent
> environment inspection.

---

## What NOT to do

- **Do not** put secrets as plain string values inside `mcp.json` — the
  file has no access controls beyond POSIX permissions and is easy to
  accidentally commit or share.
- **Do not** add custom keys to `~/.kiro/crew/.env` expecting them to be
  agent-isolated — the gateway loads them and propagates them to all child
  processes including the agent.  A warning is logged, but the key still
  reaches the process tree.  Use the vault once available.
- **Do not** store MCP secrets in user-readable paths — a file at
  `~/.kiro/crew/mcp-secrets.env` or `~/.kiro/.env` is accessible to the
  agent via filesystem reads.  Use root-owned paths (`/etc/kirocrew/`)
  or wait for the encrypted vault.

---

## The encrypted vault (recommended)

The interim routes above deliver a secret to an MCP server's process
environment, where an agent sharing that process tree can observe it.  The
encrypted vault closes that gap: secrets are stored encrypted on disk under
`.vault` in the data home, and a `secret://NAME` reference in an MCP server's
env is resolved to the real value only at spawn time, injected into that
server's environment alone — never the agent's.

Store a secret through the dashboard **Settings → Secrets** tab, then reference
it from `mcp.json`:

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
