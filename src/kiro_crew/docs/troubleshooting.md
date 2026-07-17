# Troubleshooting

## Quick Diagnostics

```bash
kirocrew doctor
```

This checks everything: agent backend, project directory, agent config, MCP
tools, credentials, gateway status, and vector memory.

## Common Issues

### "agent backend not found"

KiroCrew needs the `kiro-cli` agent backend on your PATH. Install `kiro-cli`
and make sure it resolves on your PATH:

```bash
which kiro-cli   # should print a path; if empty, kiro-cli is not on PATH
```

`kiro-cli` (the ACP backend) is required — the agent provider is fixed to
`acp`. If `which kiro-cli` prints nothing, install `kiro-cli` and add its
install location to your PATH.

### Agent config missing or stale

```bash
kirocrew setup --agent-only
```

### MCP tools not working

`kirocrew doctor` auto-fixes missing MCP entries. If tools still fail:

1. Check `~/.kiro/settings/mcp.json` for the server config
2. Check `~/.kiro/agents/kirocrew.json` for `@kirocrew-cron` and `@kirocrew-core` in tools
3. Re-run `kirocrew setup --agent-only`

### Dashboard not loading

- Check if gateway is running: `kirocrew status`
- Check the port: `curl http://localhost:5476/api/status`
- Check for port conflicts: `lsof -i :5476`

### Slack not responding

- Verify credentials: check `~/.kirocrew/.env` has valid tokens
- Check owner ID: `KIROCREW_OWNER_ID` must match your Slack user ID
- Check gateway logs: `kirocrew gateway -vv` for debug output
- Confirm the Slack app has Socket Mode enabled and the app/bot tokens are current

### Context window filling up

KiroCrew auto-compacts at 90% context usage. If you see frequent compaction:
- Reduce always-on skills (they consume context every session)
- Check memory size — large preferences/projects files eat into the budget
- Use shorter session timeouts to recycle sessions more often

### Build failures

Backend (Python):

```bash
pip install -e . && python -m pytest 2>&1 | tail -20
```

Frontend (dashboard):

```bash
cd website && npm install && npm run build 2>&1 | tail -20
```

Common Python lint issues:
- Unused imports (flake8 F401) — remove them
- Missing type annotations (mypy) — add them
- Variable naming (flake8 N806) — use lowercase in functions

### Vector memory issues

- "Ollama not found": click "Enable Vector Memory" on the dashboard to install,
  or install Ollama from https://ollama.com
- "Model not loaded": pull the embedding model with
  `ollama pull qwen3-embedding:0.6b`
- Docker fallback: if your platform's glibc < 2.27, set
  `memory.embedding_runtime` to `docker` and Ollama runs in a container

## Log Levels

```bash
kirocrew gateway          # WARNING only (default)
kirocrew gateway -v       # INFO — session lifecycle, context %
kirocrew gateway -vv      # DEBUG — full ACP events, message traces
```

Or change at runtime via the dashboard Logs page.

## Emergency Recovery

If something goes wrong:
1. Stop the gateway: Ctrl+C (or `systemctl stop kirocrew`)
2. Check logs in `~/.kirocrew/` for error details
3. Reset sessions: delete `~/.kirocrew/session_map.json`
4. Reset config: `kirocrew config edit` or delete `~/.kirocrew/config.json`
5. Full reset: `kirocrew setup` to reconfigure from scratch

## Community-Reported Issues

### SSL certificate errors on first run

If `aiohttp` caches an empty SSL context before the CA bundle is set up,
HTTPS requests will fail. Fix: ensure `setup.sh` completes fully before
starting the gateway. The v1.1.0 release runs SSL CA bundle setup before
aiohttp import.

### Tool approval buttons not working

In v1.0, a hooks commit broke interactive approval in normal mode. Fixed in
v1.1.0. Update with `kirocrew update`.

### Subagent replies truncated in Slack

Slack has a 3900-character message limit. In v1.1.0+, long subagent replies
are automatically split into multiple messages instead of truncating.

### Subagent completion event seems cut off

The completion event injected into the parent session is a bounded copy of
the subagent's streamed transcript (`agent.completion_keep` = `"head"` by
default keeps the **first 3000 characters**). When that cap drops content,
KiroCrew no longer injects a bare truncated blob: the event carries a
**short preview + the full transcript's file path**, and the parent is told
to read the rest on demand (the `read` tool with offset/limit, `grep`, or the
`spawn_status` MCP tool) rather than re-running the subagent.

To change how much is previewed / which end is kept:

```bash
kirocrew config set agent.completion_keep tail   # keep the conclusion
# or: both (head + middle marker + tail); head is the default
# optional: change the size cap (default 3000 chars; 0 disables truncation)
kirocrew config set agent.completion_keep_chars 5000
```

The full transcript lives at `~/.kirocrew/subagents/<agent_id>/result.txt`.
After delivery it is **retained for a grace window** (default 1 hour,
`agent.subagent_result_ttl_secs`) so `spawn_status` / `read` / `grep` can pull
the full text, then the reaper prunes it. Raise the TTL if you routinely read
subagent transcripts long after they finish:

```bash
kirocrew config set agent.subagent_result_ttl_secs 21600   # 6 hours
```

See [Subagents — Completion Event Truncation](subagents.md#completion-event-truncation)
for the full reference.

### WebSocket errors on startup

If Slack events arrive before the WebSocket is ready, you may see connection
errors in the logs. This is a race condition fixed in v1.1.0 — the gateway
now queues early events until the WebSocket is established.

### kirocrew.json customizations lost after restart

User customizations in `kirocrew.json` were being overwritten on gateway
restart. Fixed in v1.1.0 — the gateway now preserves user edits across
restarts.

### npm ci fails during build

If `npm ci` fails in `build-frontend.sh`, the v1.1.0 release switched to
`npm install` which is more tolerant of lockfile mismatches.
