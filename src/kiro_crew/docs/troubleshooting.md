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

## Diagnostics bundle

To report a problem, collect a bundle instead of pasting logs by hand:

```bash
kirocrew doctor --bundle
```

This writes a zip to `~/.kiro/crew/diagnostics/` holding the gateway logs and
crash reports, `versions.txt` and `manifest.json`. Every text file in it passes
through the credential redaction first, and the command prints how many secrets it
removed. It then prints a GitHub new-issue link on the bug-report form; drag the
zip into that issue. The link fills in the version and the release channel from
the build's own release record. When the build cannot prove its channel, the
channel field reads `Not sure` and no `channel:` label is attached, so a human
sets it. The link printed in the terminal leaves out the free-text fields; the
dashboard's **Report problem** button uses the same collector and fills those in
too.

## Common Issues

### The default kiro-cli backend is not on PATH

`agent.provider` is fixed to ACP, while `agent.acp_backend` selects the ACP
harness. With the default blank backend, `kiro-cli` is required and the gateway
spawns `kiro-cli acp --agent <name>`. If you selected another ACP backend,
`kirocrew doctor` reports that backend's executable and setup instead.

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

**macOS desktop app:** if a command resolves in Terminal but not inside the
app, the cause is usually launchd's minimal `PATH`, which a shell rc file
never changes. The fix is `launchctl setenv PATH "$PATH"` plus a full quit and
relaunch — see the
[macOS troubleshooting guide](https://github.com/kirodotdev/KiroCrew/blob/main/docs/guides/macos-troubleshooting.md)
for the recipe and how to persist it across reboots.

### Dashboard asks for sign-in but `kiro-cli` is already authenticated

Typical on a headless host that authenticates `kiro-cli` with an API key rather
than `kiro-cli login`. `kirocrew doctor` prints a signed-in state while the
dashboard's setup gate still asks for a device login, and `/api/models` plus
usage polling answer 503.

The readiness probe forwards `KIRO_API_KEY` to `kiro-cli whoami`, but only from
the **gateway's own** environment. Exporting it in a shell after the gateway is
running does not reach it, and neither launchd nor systemd passes the installing
shell's environment to the service. Put it where the gateway reads it at boot:

```bash
P=~/.kiro/crew/.env
touch "$P" && chmod 600 "$P"
printf '%s\n' "KIRO_API_KEY=$KIRO_API_KEY" >> "$P"
kirocrew restart           # or restart however you run the gateway
```

The `chmod` comes first on purpose: under a standard `022` umask a file created
by the append alone is `0644`, and the gateway only forces `0600` the next time
it reads it — so the key would be readable by other local users until then. The
quoting matters for the same reason if your crew home contains a space. Every
key in `~/.kiro/crew/.env` is loaded into the gateway's environment at startup;
a bare `KIRO_API_KEY=` with no value does not count, because falsy values are
skipped. Do not put the key in the systemd unit or in
`/etc/kirocrew/kirocrew.env` — both are readable by any local user.
`kirocrew service install` warns when it sees a key in your shell that the
service will not inherit.

Releases before 0.3.0 filtered `KIRO_API_KEY` out of the probe entirely, so no
placement works on those; use `kiro-cli login` or upgrade.

### Agent config missing or stale

```bash
kirocrew setup --agent-only
```

This regenerates `~/.kiro/agents/kirocrew.json` while preserving your own
customizations in it.

### Pod commands report Permission denied on the user bus

`kirocrew pod` uses per-user service-manager units. On Linux it must connect to
`$XDG_RUNTIME_DIR/bus`. Pod verb entry and the Pods row in `kirocrew doctor` run
`systemctl --user is-system-running` once to test that connection. Low-level unit
queries do not repeat the probe before each command.

If doctor reports the bus as `sandboxed away`, the socket exists but an outer
sandbox, such as a container or launcher shim, blocks the current process:

```text
Failed to connect to bus: Permission denied
```

Run pod commands from a host shell instead of that sandboxed process:

```bash
kirocrew doctor
kirocrew pod status <worktree>
kirocrew pod up <worktree>
```

If doctor reports `no user session bus`, or reports the address as stale because
the socket it names holds nothing, no per-user systemd instance is running for
this uid, and pods are `systemd --user` units. Start it with the
`loginctl enable-linger <user>` command doctor prints. That command talks to the
**system** bus, so it is not self-service on a host that cannot reach a bus at
all: if it answers `Failed to create bus connection: Permission denied`, run it
from a host shell, or have an administrator run
`sudo loginctl enable-linger <uid>` — the numeric uid resolves where a name
lookup answers `Failed to look up user <user>: No such process`. A Cloud Dev
Desktop reaches the stale case by exporting `DBUS_SESSION_BUS_ADDRESS` from a
login session whose manager has since stopped. To preview a worktree with no
systemd at all, use `./dev-backend.sh` from the root of a Kiro Crew source
checkout; it is a repository script and is not installed with the package.

If doctor reports `session bus: not applicable (no systemd per-user manager on
this host)`, the host's systemd ships without `user@.service` — Enterprise Linux
7 derivatives such as RHEL 7, CentOS 7 and Amazon Linux 2 do. There is no
per-user manager for linger to start, so `loginctl enable-linger` cannot help.
Run a worktree gateway with `./dev-backend.sh` from a source checkout, or have an
administrator hand-install a `user@$(id -u).service` unit as described under
"Hosts without a working `systemd --user`" in the
[remote and mobile guide](https://github.com/kirodotdev/KiroCrew/blob/main/docs/guides/remote-and-mobile.md#hosts-without-a-working-systemd---user).

Probe and unit operations resolve
`systemctl` only from trusted system directories and ignore same-named PATH entries. A
missing trusted executable, missing interpreter, or other failure while executing the
resolved command is reported as an operational error, not as an absent backend.
Destructive Dev Fleet cleanup then refuses to remove the worktree. Other Kiro Crew
features do not depend on the pod service manager.

### MCP tools not working

`kirocrew doctor` auto-appends missing `tools` entries for managed servers and
rewrites the file. It may also repair `allowedTools` for always-on servers, but
it deliberately does not blanket-auto-approve `kirocrew-computer` or other
opt-in servers. It cannot auto-add a missing `mcpServers` entry, because the
command path is install-specific. If tools still fail:

1. Check `~/.kiro/agents/kirocrew.json` for `kirocrew-core` and
   `kirocrew-cron` under `mcpServers`, plus `kirocrew-computer` only when
   Computer Use is enabled and supported; check for matching `@`-prefixed
   entries under `tools`
2. Check `~/.kiro/settings/mcp.json` for globally configured servers
3. Re-run `kirocrew setup --agent-only`

The doctor also runs a live handshake probe against each managed server and
prints the child's stderr tail on failure, which is usually where the real cause
(an import error, a bad path) shows up.

If a session is refused with `kirocrew-core is withheld by <source>:
<restriction>; <remedy> ...`, a declaration of `kirocrew-core` carries a setting
that a per-session copy of the server cannot keep — `disabled: true`, a
`disabledTools` list, a `type` other than stdio, or another key only kiro-cli
reads. `<source>` names where it lives:
the agent spec, the global MCP settings (`~/.kiro/settings/mcp.json`), or the
project's MCP settings. Session-scoped tools such as skill search need that
per-session copy, so the session is refused instead of silently losing them.
Remove the restriction the way the message names, then start a new session.

### MCP tools missing on an enterprise (work) account

If the probe above reports every server healthy but the tools are still absent in
sessions — no `spawn_run`, no `cron_add`, no `learn_add` — and your Kiro account
is a work account signed in through IAM Identity Center, your administrator has
almost certainly allow-listed MCP servers through an MCP registry. In that mode
kiro-cli connects only to servers marked `"type": "registry"`, and it drops the
rest without an error. The local probe cannot see this because it spawns the
servers directly.

```bash
kirocrew config set agent.mcp_registry_mode true
kirocrew restart
```

Your administrator also has to add `kirocrew-core`, `kirocrew-cron` and
`kirocrew-computer` to the registry under those exact names. `kirocrew doctor`
prints an `MCP Governance (enterprise)` section on Identity Center hosts with the
current state. Full walkthrough, including the registry JSON your administrator
needs: `docs/guides/enterprise-mcp-governance.md`.

### A remote MCP server shows "Not verified"

Remote MCP servers that authenticate with OAuth — Atlassian, for example — can
show **Not verified** under Connections → MCP Servers while working perfectly in
chat. Nothing is wrong with the server. The badge describes what the dashboard
can see, not what the server can do.

The Kiro CLI runs the OAuth flow and keeps the token in its own credential store;
Kiro Crew never holds it. The dashboard's status probe therefore connects without a
token, and the server answers `401`. That single answer covers two situations the
dashboard cannot tell apart: a server nobody has authorized, and a server already
authorized through the Kiro CLI. So it reports only what it knows.

To find out which one you have:

- If an agent can call that server's tools in chat, it is authorized and working.
- If tool calls fail, use the server in chat once. The Kiro CLI starts the OAuth
  flow on the `401` and Kiro Crew shows the consent link as a banner; approve it
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

Kiro Crew auto-compacts at `session.autocompact_pct` context usage (70% by
default for a new install — an existing `config.json` keeps whatever value it
already stores, which for installs created before this default changed is
`90.0`; check with `kirocrew config get session.autocompact_pct`). If
compaction fires often:

- Reduce always-on skills, which consume context in every session
- Check memory size: large preferences and project files eat into the budget
- Keep `skills.lazy_load` on (the default) so a large skills set injects only a ranked top-K
  instead of the whole catalog
- Lower `session.timeout_secs` to recycle sessions more often

### Build failures

Building and testing apply to a Kiro Crew source checkout, not to an installed
package. The backend's test tools live in the `dev` extra, so a plain
`pip install -e .` has no `pytest`. Install with `pip install -e ".[dev]"` and run
the change-scoped gate with `python3 scripts/local-gate.py`; the
[install guide](https://github.com/kirodotdev/KiroCrew/blob/main/docs/guides/install.md#b-from-source-development)
and
[CONTRIBUTING.md](https://github.com/kirodotdev/KiroCrew/blob/main/CONTRIBUTING.md#tests)
have the full recipe.

Frontend:

```bash
cd website && npm install && npm run build 2>&1 | tail -20
```

Node must be `>= 22.12`, the floor Vite and Rolldown declare in
`website/package-lock.json`; an older Node fails the Vite build. Python must be
`>= 3.12`.

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
- **Embedding-model dimension mismatch.** Set `memory.embedding_dim` to the output width named in the error. The width is checked at load so a mismatch is a loud refusal rather than an unexplained loss of semantic search.
- **You swapped models but nothing re-embedded.** The vector-space identity
  is `<label>:sha256:<digest>` of the model file's bytes, so a different model
  under the same name and size is detected on its own; `memory.embed_model_id`
  is only the label and cannot pin the old space. Applying the model from the
  dashboard (Memory → Embedding Model) records the new digest together with
  `memory.embed_model_stamp`, and an unchanged file reuses that digest at
  startup without re-reading the weights. A file replaced behind a stale stamp
  is re-hashed off the event loop. Status reports the model as unverified while
  that check runs and recovers automatically after it succeeds; applying the
  model again is not required.
- **Status warns about inherited legacy vectors.** Older model identities used
  the file name and size, so they cannot prove which weights produced the
  vectors. If you changed weights before upgrading, reapply the same file in
  Memory settings to rebuild inherited vectors while keeping memory text.

### High memory usage with embeddings

About 700 MB of RSS is expected while the embedding model is loaded. One copy is
shared by vector memory and the Knowledge Library. The model loads lazily on
first use and stays resident afterwards.

### `~/.kiro/crew/scratch/` is using a lot of disk

Every agent process gets a directory under `~/.kiro/crew/scratch/` for its
temp files and its `$KIROCREW_SCRATCH` work products (clones, build logs,
screenshots). A directory is reclaimed automatically once every process
recorded in its `.owner` file has exited and nothing in it has been touched for
an hour, so short-lived sessions clean up on their own.

One directory does not: the background runtime's tree is shared by every
dashboard session and is handed on from one runtime to its replacement, so it
lives as long as the gateway does and is never pruned while a session might
still need it. It is one of the `runtime-*` directories — usually the oldest
and largest. Other `runtime-*` directories belong to ordinary agent processes
and are reclaimed by the normal rule above. If the shared tree grows large, the
fix is a gateway restart (a fresh tree is
started and the old one is reclaimed by the hourly sweep once its processes are
gone), or deleting large work products inside it that you know are finished.
Do not delete a directory whose `.owner` names a live process.

```bash
du -sh ~/.kiro/crew/scratch/*/ | sort -h | tail
```

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

### "This conversation hit its turn limit and is paused"

A chat-channel conversation that drives too many turns inside one window is
latched, and every later message in it is refused with this text. It stops a
channel that has started answering its own replies. The default is 90 turns per
hour per conversation; dashboard, cron and subagent turns are not counted.

To continue, reset that conversation from the dashboard. Every reset verb
releases the latch, and so does a gateway restart. To change the limit, set
`KIROCREW_CHANNEL_TURN_CEILING` (turns; `0` turns the limit off) or
`KIROCREW_CHANNEL_TURN_WINDOW_SECS` (seconds) in `~/.kiro/crew/.env` and restart
the gateway.

### `kirocrew restart`: "Replacement gateway ... did not become ready within"

The replacement gateway is still running but did not pass its readiness check in
time, so nothing serves the dashboard yet. A slow host
can need longer than the default 60 seconds; set `KIROCREW_RESTART_READY_TIMEOUT`
to a number of seconds (clamped to 15–180) for the shell running
`kirocrew restart`. A replacement that dies early is reported at once and is not
helped by a longer wait: run `kirocrew logs -f` to see its startup.

### "Refusing to start: the Python standard library is shadowed."

Kiro Crew exits with status 2 when a file or directory on the import path has
the name of a standard-library module (an `asyncio/` folder or a `queue.py` in
the directory you launched from, for example). The message names the module,
where it resolved and which `sys.path` entry provided it. Move or rename that
module, or launch from another directory. The `import path:` row of
`kirocrew doctor` reports the same check.

### Voice input stays off after a crash

If the gateway died while loading the speech-to-text model, the next start does
not try again with the same speech runtime: voice input stays refused and the
reason names a marker file, `.load-in-progress.json` in the models directory
(`~/.kiro/crew/models/whisper/`). It clears when the speech runtime changes (a
reinstall). To try once more with the same runtime, remove that file. To stop
the attempts, turn `stt.enabled` off.

### Doctor warns about run directories without a marker

`run dirs: ... carry no .kirocrew-run-dir marker` counts run directories under
the workspace root that an older build left without a marker. The gateway only
reclaims marked directories, so these stay; above 1000 the row becomes a warning
and prints the manual move to do with the gateway stopped. Doctor itself deletes
nothing.

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
