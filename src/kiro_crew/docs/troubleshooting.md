# Troubleshooting

When something is wrong, start with `kirocrew doctor` rather than guessing: it
checks the whole chain end to end and repairs the parts it can. The rest of this
page covers the failures the doctor reports but cannot fix by itself.

## Quick Diagnostics

```bash
kirocrew doctor
```

Reports the resolved platform edition, the `kiro-cli` binary and login state,
git, the project directory, the agent config and its managed MCP entries,
credentials, gateway status, and the embedding runtime and model file. Where a
check fails it prints a specific fix command.

## Common Issues

### "kiro-cli not found in PATH"

`kiro-cli` is the agent backend and is required: `agent.provider` is fixed to
`acp`, and the gateway spawns `kiro-cli acp --agent <name>` for every session.

```bash
which kiro-cli   # should print a path; empty means it is not on PATH
```

If that prints nothing, install `kiro-cli` per its docs and add its install
location to your `PATH`. Then log in, which is separate from being installed:

```bash
kiro-cli login
```

`kirocrew doctor` reports the binary and the login state on separate lines, so
check both.

### Agent config missing or stale

```bash
kirocrew setup --agent-only
```

This regenerates `~/.kiro/agents/kirocrew.json` while preserving your own
customizations in it.

### MCP tools not working

`kirocrew doctor` auto-appends missing `tools` / `allowedTools` entries for the
managed servers and rewrites the file. It cannot auto-add a missing `mcpServers`
entry, because the command path is install-specific. If tools still fail:

1. Check `~/.kiro/agents/kirocrew.json` for `kirocrew-core`, `kirocrew-cron`,
   and `kirocrew-computer` under `mcpServers`, and for the matching `@`-prefixed
   entries under `tools`
2. Check `~/.kiro/settings/mcp.json` for globally configured servers
3. Re-run `kirocrew setup --agent-only`

The doctor also runs a live handshake probe against each managed server and
prints the child's stderr tail on failure, which is usually where the real cause
(an import error, a bad path) shows up.

### A remote MCP server shows "Not verified"

Remote MCP servers that authenticate with OAuth — Atlassian, for example — can
show **Not verified** under Connections → MCP Servers while working perfectly in
chat. Nothing is wrong with the server. The badge describes what the dashboard
can see, not what the server can do.

The Kiro CLI runs the OAuth flow and keeps the token in its own credential store;
KiroCrew never holds it. The dashboard's status probe therefore connects without a
token, and the server answers `401`. That single answer covers two situations the
dashboard cannot tell apart: a server nobody has authorized, and a server already
authorized through the Kiro CLI. So it reports only what it knows.

To find out which one you have:

- If an agent can call that server's tools in chat, it is authorized and working.
- If tool calls fail, use the server in chat once. The Kiro CLI starts the OAuth
  flow on the `401` and KiroCrew shows the consent link as a banner; approve it
  there and the calls succeed.

A server that is genuinely broken reads **Error** with the reason next to it, not
**Not verified**.

### Dashboard not loading

```bash
kirocrew status                          # is the gateway running?
curl http://localhost:5476/api/status    # is it answering on the expected port?
```

If the port is taken by something else, either stop that process or run Kiro Crew
on another port with `KIROCREW_PORT`.

### Slack not responding

- Verify `~/.kiro/crew/.env` has current `SLACK_APP_TOKEN` and
  `SLACK_BOT_TOKEN` values
- Check that `KIROCREW_OWNER_ID` is your user ID **in the workspace where the
  bot is installed**. Only the owner is authorized, so a user ID copied from a
  different workspace silently matches nobody
- Confirm the Slack app has Socket Mode enabled
- Run `kirocrew gateway -vv` for debug output

### Context window filling up

Kiro Crew auto-compacts at `session.autocompact_pct` context usage (90% by
default). If compaction fires often:

- Reduce always-on skills, which consume context in every session
- Check memory size: large preferences and project files eat into the budget
- Enable `skills.lazy_load` so a large skills set injects only a ranked top-K
  instead of the whole catalog
- Lower `session.timeout_secs` to recycle sessions more often

### Build failures

Backend:

```bash
pip install -e . && python -m pytest 2>&1 | tail -20
```

Frontend:

```bash
cd website && npm install && npm run build 2>&1 | tail -20
```

Node must be `20` or `>= 22`; an older Node fails the Vite build. Python must be
`>= 3.10`.

### Embedding model download failed

The embedding model (about 610 MB) downloads in the background over HTTPS from
the Kiro Crew CDN on gateway startup and is sha256-verified. A failed download
retries with exponential backoff (up to 6 attempts) and again on every gateway
start. If it keeps failing:

- Run `kirocrew doctor`, which probes the resolved model URL and reports
  reachability
- Check outbound HTTPS connectivity. No git or cloud SDK is involved
- Mirrored or airgapped hosts: point `KIROCREW_EMBED_MODEL_URL` (or
  `memory.embed_model_url`) at a mirror hosting the GGUF. The sha256 pin still
  verifies whatever is downloaded
- To run a different model entirely, set `memory.embed_model_path` (see below).
  The default model is then never downloaded at all
- Retry from the dashboard Overview → Memory card, or do nothing: it retries on
  the next gateway start
- Coming from an install that used Ollama for embeddings? The download is
  usually skipped: Kiro Crew finds the identical model in the local Ollama blob
  store and copies it (sha256-verified) instead of re-downloading

### Embeddings not working

- Run `kirocrew doctor`, which checks the bundled embedding runtime and whether
  the model file is present. Embeddings themselves are always on and cannot be
  disabled, so there is no switch to check
- If `KIROCREW_SKIP_MODEL_DOWNLOAD=1` is set, the model never downloads. Unset
  it, or copy the model in from a machine where it is not set
- While the model is absent, memory falls back to keyword search. That is
  expected rather than an error, and semantic search resumes once the model
  lands, with no restart

### Using your own embedding model

Point `memory.embed_model_path` (or `KIROCREW_EMBED_MODEL_PATH`) at an absolute
path to a local GGUF, and set `memory.embedding_dim` to that model's output
width:

```json
{
  "memory": {
    "embed_model_path": "/home/you/models/bge-m3-q8_0.gguf",
    "embedding_dim": 1024
  }
}
```

What changes when a custom model is configured:

- The bundled model is never downloaded or installed, so your model survives a
  default-model version change.
- Stored embeddings are regenerated automatically, because the model change
  alters the vector space. Vector memory clears its stale vectors and re-embeds
  in the background; the Knowledge Library re-embeds items whose signature no
  longer matches on its next watcher sweep. Affected entries stay
  keyword-searchable throughout, and an interrupted re-embed resumes on the next
  sweep.
- The dashboard Memory card reports `custom` as the model source and shows the
  path. It does not offer a retry, since retrying would fetch the bundled model,
  which is not the one in use.

You can also set the path from the dashboard (Memory → Embedding Model), which
validates it, refuses protected locations, probes the model's real width, and
re-embeds stored vectors in the background with no restart. While
`KIROCREW_EMBED_MODEL_PATH` is set, the dashboard refuses to change the model,
because a config write could not take effect.

Common problems:

- **The doctor says the custom model is unusable.** The path is relative,
  missing, a directory, or too small to be model weights, and the exact reason is
  printed. A broken path deliberately does **not** fall back to the bundled
  model: doing so would silently swap your vector space and re-embed your whole
  corpus because of a typo. Embeddings stay unavailable (keyword search still
  works) until the path is fixed.
- **A log line says the model produces N-dim vectors but `embedding_dim` is M,
  and refuses to load.** Set `memory.embedding_dim` to the number in the message.
  The width is checked at load precisely so a mismatch is a loud refusal rather
  than an unexplained loss of semantic search.
- **You swapped models but nothing re-embedded.** The default vector-space
  identity is derived from the file's name and size, so two different models of
  identical byte size look the same. Set `memory.embed_model_id` explicitly to
  distinguish them.

### High memory usage with embeddings

About 700 MB of RSS is expected while the embedding model is loaded. One copy is
shared by vector memory and the Knowledge Library. The model loads lazily on
first use and stays resident afterwards.

### Subagent completion event seems cut off

The completion event injected into the parent session is a bounded copy of the
subagent's transcript: `agent.completion_keep` defaults to `"head"`, keeping the
first `agent.completion_keep_chars` characters (3000 by default). When that cap
drops content, the event carries a short preview plus the full transcript's file
path, and the parent is told to read the rest on demand (the `read` tool with
offset/limit, `grep`, or the `spawn_status` MCP tool) rather than re-running the
subagent.

To change how much is previewed and which end is kept:

```bash
kirocrew config set agent.completion_keep tail        # keep the conclusion
kirocrew config set agent.completion_keep_chars 5000  # 0 disables truncation
```

The full transcript lives at `~/.kiro/crew/subagents/<agent_id>/result.txt` and
is retained for a grace window (1 hour by default) after delivery so
`spawn_status`, `read`, and `grep` can pull the full text before the reaper
prunes it. Raise the window if you routinely read transcripts long after the
subagent finished:

```bash
kirocrew config set agent.subagent_result_ttl_secs 21600   # 6 hours
```

See [Subagents](subagents.md#completion-event-truncation) for the full
reference.

## Log Levels

```bash
kirocrew gateway          # WARNING only (default)
kirocrew gateway -v       # INFO: session lifecycle, context %
kirocrew gateway -vv      # DEBUG: full ACP events, message traces
```

`agent.log_level` sets the persistent default; `--verbose` overrides it for one
run. You can also change the level at runtime from the dashboard Logs page.

Tail a background gateway's output with `kirocrew logs -f`.

## Emergency Recovery

1. Stop the gateway: Ctrl+C, or `kirocrew stop` if it is running detached
2. Check the logs for the actual error: `kirocrew logs -n 200`
3. Reset sessions: delete `~/.kiro/crew/session_map.json`
4. Fix or reset config: `kirocrew config edit`, or delete
   `~/.kiro/crew/config.json` to fall back to defaults
5. Reconfigure from scratch: `kirocrew setup`

None of these touch `memory.db`, so your memory survives all five. To roll back
memory too, restore a snapshot: see
[Backup & Restore](snapshot-and-restore.md).
