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
