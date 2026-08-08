# CLI Module

## Overview

The CLI module (`kiro_crew/cli.py`) provides the `kirocrew` command using stdlib `argparse`.

## Source Checkout Launcher

The POSIX wrapper at `bin/kirocrew` resolves symlinks to find the real checkout,
sets `KIROCREW_PROJECT_DIR` to that checkout unless the caller already supplied
one, and delegates every argument to `.venv/bin/kirocrew`. The virtualenv entry
point comes from the editable install created by the setup scripts, so it makes
`src/kiro_crew` importable without adding the source tree to `PYTHONPATH`. Any
caller-provided `PYTHONPATH` is inherited unchanged.

If `.venv/bin/kirocrew` is unavailable, the wrapper exits with source-install
guidance instead of falling through to a different Python environment.

## Standalone Wheel Installer Trust Contract

`cli.sh` installs channel or pinned-version wheels only from an authenticated
manifest. This distribution trust boundary is independent of the runtime CLI
and of macOS signing/notarization.

- Schema: `kirocrew-cli-artifact-manifest-v1`.
- Algorithm: `RSASSA_PKCS1_V1_5_SHA_256`.
- Key identity: `sha256:` plus the lowercase SHA-256 digest of the public
  SubjectPublicKeyInfo DER bytes.
- Signed fields: `algorithm`, `channel`, `key_id`, `pub_date`,
  `python_requires`, `schema`, `sha256`, `version`, and `wheel_url`.
- Signature field: base64 RSA signature over sorted, compact UTF-8 JSON of all
  signed fields; `signature` itself is excluded.
- Channel source: `feed/<channel>/latest-cli.json`. Pinned-version source:
  `cli/<channel>/<version>/cli-manifest.json`; pinned installs do not resolve
  through the mutable channel feed.

The installer embeds the public key and expected key id. Before any network
request, it requires OpenSSL, rejects an unconfigured pin, materializes the key,
and verifies its DER fingerprint. It then applies bounded input/object sizes,
duplicate-key and exact-field-set rejection, printable-ASCII/string checks,
canonical URL and digest validation, requested channel/version matching, and
pinned-key signature verification. Artifact fields are not consumed and wheel
bytes are not fetched until the signature succeeds. The downloaded wheel must
then match the authenticated SHA-256 digest before `pipx install` runs.

Any unavailable trust root, malformed or unsigned legacy feed, unknown field,
wrong schema/algorithm/key id, signature failure, metadata mismatch, network
failure, or wheel digest mismatch terminates installation. There is no unsigned,
`SHA256SUMS`, or trust-on-first-use fallback. Until the operational public key is
pinned, the repository's explicit `UNCONFIGURED` state therefore makes stock
`cli.sh` non-installing by design. Provisioning and rollout are specified in
`packaging/signing/README.md`.

## Project Directory Detection

At startup, `main()` auto-detects the project root and sets `KIROCREW_PROJECT_DIR`:

1. If `KIROCREW_PROJECT_DIR` env var is already set, use it
2. Walk up from CWD looking for a directory with both `skills/` and `src/kiro_crew/` (`_PROJECT_MARKERS`). The project-level `agents/` dir was removed when agent config was consolidated into `src/kiro_crew/config/` (commit bbbc1f6e), so the marker no longer references it — a stale `agents/` requirement left detection (and the dashboard changelog) silently broken.
3. Read saved path from `~/.kiro/crew/project_dir` (written by `kirocrew setup`); the saved path is re-validated against the same markers

This allows `kirocrew` to find project-level agent config and skills from any directory.

## Commands

| Command | Description |
|---------|-------------|
| `kirocrew chat -m "msg"` | Send a single message, print streaming response |
| `kirocrew chat` | Interactive chat mode (readline, exit with Ctrl+D) |
| `kirocrew chat --model X` | Override model for this session |
| `kirocrew gateway` | Start the KiroCrew server (dashboard + Slack) |
| `kirocrew gateway --slack-only` | Start without dashboard or SSH tunnel instructions |
| `kirocrew gateway --no-crons` | Start without cron scheduler (use when another instance handles crons) |
| `kirocrew setup` | Install agent config, save project dir, configure credentials |
| `kirocrew setup --agent-only` | Only install agent config (skip credentials) |
| `kirocrew doctor` | Verify kiro-cli is installed and config is valid |
| `kirocrew cron add/list/remove` | Manage cron jobs |
| `kirocrew spawn run/list` | Manage background subagents |
| `kirocrew app install/list/enable/disable/uninstall` | Manage App Kit apps. Uninstall preserves `apps/<name>/data/` by default. |
| `kirocrew app uninstall NAME --purge-data` | Explicitly uninstall an app and permanently delete its app data. |
| `kirocrew app dev <name> [--off]` | Toggle an installed app into/out of dev mode (no-store UI serving + live reload on file change). See [App Dev Mode](#app-dev-mode). |
| `kirocrew learn add/list/remove` | Manage learned corrections |
| `kirocrew run TASK.md` | Run an autonomous task from a spec file |
| `kirocrew token` | Print a dashboard access URL with auth token |
| `kirocrew logout` | Revoke all active dashboard access sessions (does not revoke refresh chains) |
| `kirocrew manifest` | Generate Slack manifest with user alias auto-populated |
| `kirocrew update` | Update to latest version (git pull + rebuild) |
| `kirocrew status` | Show runtime stats from running gateway |
| `kirocrew stop` | Stop a running gateway (service-aware: stops the systemd/launchd service if active, otherwise terminates the gateway found by a cross-platform port lookup — lsof on POSIX, netstat on Windows). Pass `--port N` to bypass the service short-circuit and target a specific gateway. |
| `kirocrew restart` | Restart a running gateway (service-aware: restarts the systemd/launchd service if active, otherwise terminates the foreground gateway and respawns it detached). Pass `--port N` to bypass the service short-circuit and target a specific gateway. |
| `kirocrew service install` | Install gateway as a system-level systemd service (Linux, requires sudo for `tee` + `systemctl` only) or launchd LaunchAgent (macOS, no sudo). Auto-restarts on crash, auto-starts on boot. |
| `kirocrew service uninstall` | Stop and remove the systemd unit / launchd plist. |
| `kirocrew service status` | Show service status (`systemctl status` or `launchctl list`). No sudo required. |
| `kirocrew logs` | Tail gateway logs from the systemd journal, launchd stdout file, or `~/.kiro/crew/gateway.log`. |
| `kirocrew logs -f` | Follow logs live (long-running tail). |
| `kirocrew cloud launch/list/status/connect/stop/start/destroy/iam-policy/doctor` | Provision, connect to, and manage a KiroCrew EC2 instance in the user's AWS account. |
| `kirocrew security events` | Show recent SEL audit events (`-n N` for count) |
| `kirocrew security verify` | Verify SEL HMAC chain integrity |
| `kirocrew snapshot` | Create a .tar.gz snapshot of all KiroCrew state |
| `kirocrew snapshot --keep N` | Auto-prune to N most recent snapshots (default 7) |
| `kirocrew snapshot --list` | List existing snapshots |
| `kirocrew restore <file>` | Restore from a snapshot (auto-detects replace vs merge) |
| `kirocrew restore <file> --mode replace\|merge` | Force restore mode |
| `kirocrew restore <file> --components X,Y` | Selective component restore |
| `kirocrew restore <file> --dry-run` | Preview restore without writing |
| `kirocrew restore --list-components` | Show available component names |
| `kirocrew config get [key]` | Print full config or a dot-path value |
| `kirocrew config set <key> <val>` | Set a config value (auto type detection) |
| `kirocrew config set --file <path>` | Replace config from a JSON file |
| `kirocrew config edit` | Open config in `$EDITOR` |
| `kirocrew memory list/search/stats/audit` | Inspect vector memory (entries, semantic search, counts, suspicious-content scan) |
| `kirocrew memory export/import/migrate` | Export memory to JSON, import it back, or migrate legacy markdown memory into the vector store |
| `kirocrew policy show/validate/explain/profile` | Inspect the effective enterprise security policy, load-check it and all profiles, explain one tool/scope decision for a surface, or print a profile |
| `kirocrew pod up/down/ls/status/token/url/logs/exec/install/provision` | Isolated worktree test gateways (**Linux `systemd --user` only** — every systemd-touching verb refuses with a one-line message on macOS/Windows). See `src/kiro_crew/pod/README.md`. |
| `kirocrew knowledge dedup [--apply]` | Collapse cross-source duplicate knowledge documents (dry-run unless `--apply`) |
| `kirocrew cron preview <script>` | Run a script cron locally with real MCP tools; notifications are captured and printed instead of delivered |
| `kirocrew workspace create/update --dir <name>` | `--dir` is a directory NAME that must resolve to a **strict descendant of the data home** (`~` is expanded first); anything landing outside — and the home **root itself**, in any spelling — is refused with a SEL `denied` audit event. Containment, not an absolute-path ban: an absolute path *under* the home resolves where the relative form would and is accepted. The strict-descendant test is what closes the root case for tilde paths, since the per-call-site root-equality checks compare un-expanded `config_dir() / ws_dir`. Deliberately stricter than the dashboard's `POST /api/workspaces`, which accepts an absolute `dir` anywhere, screened by `is_sensitive_path`. |
| `kirocrew computer doctor [--json]` | Report computer-use availability: platform support, the keystone primary-enable state, and the **advisory** macOS Accessibility / Screen Recording probe with a `responsible_hint`. See [Computer Use Commands](#computer-use-commands). |
| `kirocrew computer apps` | List on-screen applications the accessibility layer can address (human-facing twin of the `computer_list_apps` MCP tool). Gated by the same chokepoint as `call` — refused while the feature is off or the session is unattended. |
| `kirocrew computer call <tool> [k=v ...]` | Run ONE computer-use tool through the same gated chokepoint the agent uses, and print its reply (debug / reproduction) |
| `kirocrew computer call --calls '[…]'` | Run a JSON array of tool calls in a SINGLE process, so `element_index` values from an earlier `computer_get_state` are still resolvable |
| `kirocrew mcp-cron` | MCP server for cron tools (spawned by kiro-cli) |
| `kirocrew mcp-core` | MCP server for spawn, learn, task tools (spawned by kiro-cli) |
| `kirocrew mcp-computer` | MCP server for computer-use tools (spawned by kiro-cli; `argparse.SUPPRESS`-hidden). A **thin shim** — it forwards to the gateway over loopback and does no accessibility work itself. |
| `kirocrew --version` | Print version |

## Token Command Output Streams

`kirocrew token` has a **machine-readable stdout contract**: stdout carries only
the dashboard URL(s), and every failure reason (invalid TTL, gateway not running,
gateway unreachable, empty token) goes to **stderr**.

The contract exists because stdout is parsed, not just read by a human. The
remote-mint path (`kiro_crew.instances.token_mint.mint_remote_token`) runs
`kirocrew token` on a remote host over SSH and regex-extracts the JWT from its
stdout. Error prose on stdout would both break the Unix convention and hide the
reason from a caller that captures stderr.

**Legacy remote handling.** Older remotes predate this split and still print
their failure reasons to stdout, which made a stderr-only error message degrade
to a bare `<no stderr>`. `mint_remote_token` therefore also carries a bounded,
redacted **stdout tail** in `TokenMintError` — appended only when stdout was
non-empty, so a current remote keeps the single-stream message shape. Because
stdout is the one stream that legitimately carries a token, the tail is
token-scrubbed (URL-borne and bare forms) before the generic credential and
exfiltration redactors run.

## Setup Command

`kirocrew setup` performs:

1. Saves `KIROCREW_PROJECT_DIR` to `~/.kiro/crew/project_dir`
2. Installs agent config to `~/.kiro/agents/kirocrew.json`
3. Prompts for Slack credentials (unless `--agent-only`)
4. Offers to set up custom domain `kirocrew.localhost` (macOS/Linux)

The saved project dir enables running `kirocrew` from any directory.

### First-run Kiro CLI prerequisite onboarding

KiroCrew exposes the same two-step readiness contract on every supported
platform: an executable candidate must answer `kiro-cli --version`, then
`kiro-cli whoami` must confirm authentication. Candidate discovery includes
supported fixed locations in addition to inherited `PATH`; unusable candidates
are reported for repair. Setup probes the same first executable candidate ACP
will launch, so a stale earlier candidate cannot produce a false-ready result
from a different later installation.

- Missing CLI: the setup page offers an explicit install action on macOS,
  Linux, and Windows. macOS/Linux download the fixed
  `https://cli.kiro.dev/install`; Windows downloads the fixed
  `https://cli.kiro.dev/install.ps1`. Every redirect and the final response
  must remain on the exact `cli.kiro.dev:443` endpoint and expected path, with
  no userinfo, query, or fragment. Redirect destinations are resolved and
  validated before any request is sent, and the chain is limited to three
  redirects. Responses are size-bounded and must match a release-pinned
  SHA-256 digest plus the platform-specific official installer marker. A
  changed upstream script therefore fails closed until a KiroCrew release
  updates the pin; the manual official guide remains available. The exact
  validated bytes stay in memory and run through the fixed system interpreter's
  standard input. The installer receives a system-only `PATH` plus explicit
  HTTP(S) proxy variables, never user-writable executable directories or
  ambient application credentials. The official installer additionally
  verifies its downloaded package manifest and artifact checksum.
- Unusable CLI candidates: the same page identifies that Kiro CLI needs repair
  instead of treating a spawn failure as a signed-out session. If the upstream
  POSIX installer would require an interactive `/dev/tty` replacement prompt
  (an existing macOS app bundle or Linux `~/.local/bin/kiro-cli`), automatic
  repair is disabled and the user is directed to the official guide.
  A candidate that already runs is directly usable for sign-in regardless of
  install source; the post-installer attestation file is now write-only
  bookkeeping and does not gate credential access.
- Installed but signed out: the setup page names the commands the USER runs and
  runs nothing itself — `kiro-cli login` for a personal account (Builder ID,
  Google, or GitHub), or `kiro-cli login --use-device-flow --license pro` for
  organization SSO, which prompts for the organization's start URL and region.
  Both are backend code constants rendered verbatim in a `<code>`, never catalog
  values, because a translated command cannot be typed. Both tiers are named
  because the browser portal the bare command opens presents a free Builder ID
  as a peer of organization SSO; Kiro Crew does not detect which tier applies,
  so the gate describes the choice and the user makes it. Sign-in completion is
  observed only through the read-only `kiro-cli whoami` probe.
- Browser dashboard: the authenticated SPA gate operates on the **gateway
  host**, not the browser host. This covers native Windows source installs,
  Linux gateways, and browsers connected to another machine.
- Desktop shell: the shell starts or reuses the gateway first, then displays the
  same gateway-served setup gate as a browser. Remote gateways are therefore
  checked on the remote host rather than the desktop host.
- Offline test harness: the explicit `gateway --test-mode` bundle injects a
  ready prerequisite state so deterministic fake-ACP smoke and Playwright
  suites do not depend on a developer machine's Kiro installation, identity, or
  Linux sandbox capabilities. Ordinary gateway invocations always use the real
  probe/install/login service.

The setup client cannot supply a command, URL, argument, or output path. The web
API exposes only fixed install/login mutations to the configured owner (or the
signed `local-app` / `local-startup` identities before an owner exists).
Authenticated non-owner dashboard users receive only a redacted readiness bit:
they enter the dashboard once ready but cannot see host state/output or operate
setup. App tokens remain denied. Electron has no separate installer/login IPC
or subprocess implementation. Filesystem discovery runs off the event loop.
Version probes use a minimal noninteractive environment with no proxy
credentials or desktop-session IPC. They use the strict OS sandbox and
additionally hide the configured data home, `~/.kiro/crew`, `~/.kirocrew`, and
every known Kiro identity store. Any candidate that runs `--version` is eligible
for `whoami` and device login — trust is "it runs, and it has a valid login",
not install source, owner, or fixed path (KiroCrew is not the authority on where
Kiro CLI is installed, and its self-updater rewrites its own bytes as the user).
Auth calls execute the user's installed binary IN PLACE, never a private copy of
its bytes — a multi-call Kiro CLI resolves its sibling subcommand executable
relative to its own path, so a copy strands it (see security.md).
Sign-in itself is delegated to Kiro CLI: `login --use-device-flow` runs in the
standard sandbox against the user's real home, with only the Kiro Crew data
homes hidden, and the CLI writes its own credential store exactly as it does
from a terminal. KiroCrew stages nothing and publishes nothing, so no staged
state has to be reconciled after a failure, timeout, or cancellation. The
credential-minimal temporary home populated only with Kiro identity JSON and
SQLite files survives as an opt-in read-only mode — one that also hides
unrelated AWS, SSH, GitHub, and Kubernetes state — and its temporary directory
is removed on every exit path. Any allowlisted live identity artifact that is a
symlink, non-regular, oversized, unreadable, or disappears while being captured
aborts that mode before the command runs. Every
probe emits a critical `invoked` SEL event before spawn
and a best-effort terminal event without argv, candidate paths, output, or
environment values. Installer and login timeouts cover process exit and
output-pipe draining. On POSIX, a private supervisor remains the process-group
leader after the real command exits and keeps the group safely addressable until
all descendants close or are terminated. Windows cleanup opens an exact
primary-process handle before yielding after spawn and completes an initial
descendant snapshot even when the launcher exits immediately. It then retains
exact child handles and continues discovery from every live child, so late
helpers remain supervised and identifier reuse cannot target an unrelated
process. Numeric parent edges are accepted only when exact-handle creation and
exit times prove that the child was created during the parent's lifetime, and
the check is repeated across both tree snapshots. The primary root and each
retained child root receive one final snapshot after becoming inactive, so a
child spawned immediately before its parent exits is not lost between polls.
Failure to anchor or validate the primary process, create a Toolhelp snapshot,
or complete any process enumeration fails the operation closed. Ordinary
pipe/task errors follow the same terminate, reap, cancel, and cleanup path
before another action can start.

An auto-created `config.json` alone does not mark first-run setup complete; a
successful authenticated probe writes the setup marker, while existing
session/history state preserves established-install migration behavior. Fresh
installs receive the full-screen flow. Established dashboards remain navigable
and fully usable when signed out — no controls are paused. Readiness is probed
once at gateway boot and thereafter only on explicit user action, so no path runs
a subprocess probe on the message hot path; the authoritative logout signal is
the ACP attempt's `AcpAuthRequired`. The SPA refreshes ready status every 30 seconds,
retains cached readiness across transient refetch errors, and invalidates
prerequisite state after access-cookie refresh.
POSIX group membership ignores zombie records, which cannot retain pipes or
perform work, so an unreaping PID 1 cannot hold the supervisor forever.
The supervisor source is captured eagerly at import for replacement resistance;
if it is missing or unreadable, gateway import still succeeds and each affected
POSIX setup operation fails cleanly before spawning a command.
Sandbox launcher/profile preparation and cleanup are worker-thread operations
and do not stall the asyncio gateway loop.

Setup and ACP launch share the side-effect-free `kiro_cli` resolver on every OS.
Status requests never publish a discovered path by mutating `KIROCREW_KIRO_BIN`.
Both setup discovery and ACP launch enumerate the same candidates — inherited
`PATH`, the interpreter Scripts directory, package-manager dirs (incl. the
Windows Program Files `Kiro-Cli` tree and winget/scoop/user installs on `PATH`),
and an operator override — and accept a runnable candidate wherever it lives,
since trust is "the CLI runs". ACP launch runs the resolved candidate in place on
every platform — never a copy of its bytes. Setup discovery and ACP resolution therefore agree on
Windows, so a winget/scoop install is never sent to a redundant reinstall.

When a previously completed setup is no longer ready, the dashboard remains
fully navigable and fully usable — nothing is paused and no sign-in chrome is
shown. A signed-out CLI is reported by the turn itself as an actionable
`kiro-cli login` error card (see `modules/learn-cron-dashboard.md` § "The
dashboard does not guide the user to sign in"). Only the endpoints that act
BEFORE a turn still return 503: the poll-driven `kiro-cli` spawn sites
(`/api/models`, `/api/sessions/usage`) and the destructive reruns (regenerate,
edit-resend, rewind), which rewrite persisted history up front.

### Custom Domain

After credentials, `kirocrew setup` offers to add `127.0.0.1 kirocrew.localhost` to the system hosts file so the dashboard is accessible at `http://kirocrew.localhost:5476`:

- **macOS/Linux**: Uses `sudo tee -a /etc/hosts` for safe append

Skipped if `kirocrew.localhost` is already present or user declines.

## Cloud Command

`kirocrew cloud` is a human installer/control-plane surface for running
KiroCrew on the user's own AWS EC2 instance. Provisioning and teardown are not
LLM-facing tools. AWS credentials are resolved by the AWS CLI; KiroCrew stores
only profile, region, and the most recent instance tag in `cloud.json`.

`kirocrew cloud launch` runs a six-step wizard: check AWS reachability, explain
permissions, choose whether to keep an existing deployment or create a new one,
choose an instance size when creating a new stack, deploy or resume the
CloudFormation stack, sign in the remote `kiro-cli`, and open the dashboard
through SSM port forwarding. Launch is resume-safe by default: if `cloud.json`
contains a `last_tag` whose stack still exists in the same saved profile/region,
rerunning interactive `launch` offers to keep/resume that stack or create a new
installation. If `cloud.json` is missing or stale, launch discovers existing
`kirocrew-*` CloudFormation stacks with `cloudformation:ListStacks` and offers a
choice to resume one or create a new installation. `kirocrew cloud launch --new`
is the explicit escape hatch for creating a separate new stack. `--yes` keeps a
single or saved existing stack; if multiple unsaved stacks exist it fails closed
instead of choosing one arbitrarily. For a new launch, the generated tag is
written to `cloud.json` before the long CloudFormation deploy starts, so an
interrupted provisioning run can be found on the next launch attempt.

Launch and connect require the local AWS Session Manager plugin for
`AWS-StartPortForwardingSession`. If `session-manager-plugin` is missing,
`cloud launch` prompts to install AWS's official package for the current local
platform (macOS `.pkg`, Debian/Ubuntu `.deb`, or RPM Linux `.rpm`) before the
wizard reaches sign-in/dashboard tunneling. `--yes` accepts this installer
prompt. `cloud connect` performs the same check and installer prompt before
opening the dashboard tunnel. If installation is declined or fails, the command
exits non-zero and tells the user to retry after fixing the local prerequisite.

The instance-size picker supports arrow keys in an interactive terminal
(`↑`/`↓`, `j`/`k`, digit shortcuts, Enter to select) and falls back to the
numbered prompt for non-TTY input. Ctrl-C must interrupt prompts and long AWS
subprocesses; unhandled cloud-command interrupts return exit code 130.

Remote Kiro sign-in prefers the device-code flow over SSM. The launcher starts
`kiro-cli login --use-device-flow` as a background process on the instance,
captures the URL/code from its log, and leaves that same process alive while the
wizard polls for completion. It must not kill that process after scraping the
prompt or start a second hidden device-code flow. If device-code startup does
not produce an actionable URL, launch falls back to the Google/GitHub callback
flow automatically: it starts `kiro-cli login` on the instance with FIFO-backed
stdin, captures the printed loopback callback port, opens an
`AWS-StartPortForwardingSession` from the same local port to the remote port,
sends the Enter continuation back to the remote CLI, then opens or prints the
local browser URL. The temporary callback tunnel is closed after the sign-in
poll completes. In headless local terminals, browser auto-open is skipped and
the URL is printed for manual opening.

`kirocrew cloud connect` mints a dashboard token over SSM, opens an
`AWS-StartPortForwardingSession`, waits for the local tunnel port to accept TCP
connections, and opens or prints the local dashboard URL. If the tunnel port
does not become reachable, the command reports failure, does not present the
dashboard URL as usable, and does not keep a dead tunnel process open. If final
dashboard opening fails during `cloud launch`, the instance remains running but
launch returns non-zero and tells the user to rerun `kirocrew cloud connect`
after fixing the local SSM tunnel issue.

## Config Command

`kirocrew config` manages `~/.kiro/crew/config.json`:

- **get** — prints full effective config (with defaults resolved) or a single dot-path value
- **set key value** — sets a value with auto type detection (bool/int/float/JSON/string). Rejects unknown leaf keys.
- **set --file path** — replaces entire config from a JSON file. File read routed through `hooks.safe_read_file()` (blocks sensitive paths).
- **edit** — opens config in `$EDITOR` (supports args like `code --wait` via `shlex.split`). Creates default config if missing.

All write paths emit SEL audit events (`config_get`, `config_set`, `config_set_file`, `config_edit`).

### Gateway Auto-Create

`kirocrew gateway` creates `~/.kiro/crew/config.json` with defaults if the file doesn't exist. Does nothing if it already exists.

## Verbosity

| Flag | Level | What you see |
|------|-------|-------------|
| (none) | WARNING | Errors only |
| `-v` | INFO | Session lifecycle, context %, compaction |
| `-vv` | DEBUG | ACP events, message updates, full traces |

## Interactive Mode

- Prompt: `you> `
- Exit: `exit`, `quit`, `/exit`, `/quit`, `:q`, Ctrl+D
- Streaming output printed as chunks arrive

### Context Tracking

After each message, checks `provider.context_usage_pct()`:
- `>= autocompact_pct` (default 90%): compact → shutdown → restart provider, reset counter
- `>= 75%`: warning printed to stderr

CLI compaction is blocking (single-user, acceptable).

## Entry Point

`console_scripts` in `setup.cfg` maps `kirocrew` → `kiro_crew._bootstrap:main`.

### Gateway asyncio child watcher

`_install_child_watcher()` runs once on the **`gateway` command path only** (not
`chat`, `doctor`, or any other subcommand) and must be called before
`asyncio.run`, on the main thread. It replaces CPython's default
thread-per-child `ThreadedChildWatcher` — whose `os.waitpid` reaper threads can
starve the event loop when many `kiro-cli`/MCP children die at once — with a
single-descriptor alternative:

| Runtime | Installed watcher |
|---------|-------------------|
| Linux, `os.pidfd_open` probe succeeds (kernel ≥ 5.3) | `PidfdChildWatcher` |
| Linux, probe raises `OSError`/`AttributeError` | `SafeChildWatcher` (SIGCHLD) |
| macOS / other non-Linux Unix | `SafeChildWatcher` (SIGCHLD) |
| **Python ≥ 3.14** (child-watcher API removed) | **none — no-op** |
| `SafeChildWatcher` unavailable (e.g. Windows) | none — default retained |

**Python 3.14+ is a deliberate no-op.** CPython 3.14 removed
`set_child_watcher`, `PidfdChildWatcher`, `SafeChildWatcher`, and
`ThreadedChildWatcher`; the Unix event loop reaps children itself with a single
non-thread reaper, so the loop-starvation wedge this installer exists to prevent
cannot occur. The function short-circuits on `hasattr(asyncio,
"set_child_watcher")` — probed by capability, not `sys.version_info`, so a
runtime that still ships the API keeps the mitigation. Without that guard the
Linux pidfd branch raised `AttributeError` and `kirocrew gateway` died before
binding its port, while every other subcommand kept working.

### Live-target bootstrap

On the `gateway` command path only, immediately after `_JAILED_COMMANDS`
attestation and before the `--seed` handler:

```python
if args.command == "gateway":
    from kiro_crew.service.live_target import maybe_reexec
    maybe_reexec(sys.argv[1:])
```

`maybe_reexec` reads the live-target pointer (`config_dir() / "live_target.json"`)
and, when it names a different checkout, `os.execve`s into that checkout's own
`kirocrew` binary. This runs before anything is written to `$KIROCREW_HOME`,
before the gateway lock is acquired, and before any socket is bound — so exec'ing
away leaves nothing half-done. It is **fail-safe**: an absent, unreadable,
malformed, or stale pointer (missing binary, same image already running, or
`KIROCREW_LIVE_EXECED` marker already in env) causes the function to return, and
the currently-installed build boots normally. A bad pointer can never leave the
host with no gateway.

Gateway only — a plain CLI invocation (`kirocrew doctor`, `kirocrew chat`, etc.)
keeps running the install the user typed, not a worktree someone made live.

## Environment Variables

| Variable | Purpose |
|----------|---------|
| `KIROCREW_HOME` | Override config/data directory (default `~/.kiro/crew`) |
| `KIROCREW_PORT` | Override dashboard port (default `5476`, validated as int at CLI startup) |
| `KIROCREW_PROJECT_DIR` | Override agent config/skills directory |
| `KIROCREW_WORKSPACE` | Override workspace root directory |

For local dev:
- **macOS/Linux**: `bin/kirocrew` (POSIX shell wrapper); `source setup.sh` adds `bin/` to PATH

The wrapper sets `KIROCREW_PROJECT_DIR` and routes to the right runtime based on install type:

- **One-liner install** (`install.sh` clones the repo into `~/.kirocrew-app/`): if a sibling `.venv/bin/kirocrew` exists, the wrapper execs it directly.
- **pip editable install** (`pip install -e .`): the console_scripts entry point resolves directly.

## Setup Scripts (First-Time Bootstrap)

`setup.sh` (macOS/Linux) auto-installs all dependencies from scratch using public tooling only.

> **Note:** Windows is not supported.

**Install order:**
1. Node.js (via `ensure-node.sh`)
2. Optional tools (git-lfs, ffmpeg for voice)
3. kiro-cli (`npm i -g`)
4. kiro-cli login (guided authentication)
5. Frontend build (`npm install && npm run build`)
6. Backend build (`pip install -e .`)
7. PATH setup + shell profile persistence
8. `kirocrew setup --agent-only` (install kiro-cli agent config)
9. Optional Slack credential configuration

Each step checks if the tool is already installed and skips if present.

## Doctor Checks

1. `kiro-cli` binary in PATH
2. Project directory and git repo
3. Agent config installed
4. Config values (provider, model, approval mode, dashboard port)
5. **MCP tools**: `@kirocrew-cron` and `@kirocrew-core` in `tools`, `allowedTools`, and `mcpServers` — auto-fixes missing entries
6. **Global mcp.json**: kirocrew MCP servers present with valid binary paths — auto-fixes stale paths
7. **Python environment**: checks Python 3.9+ availability and dependency installation
8. **Vector memory (in-process embeddings)**: vendored llama-cpp-python runtime importable, embedding model file present (downloads in background on gateway start; when absent, a light HTTPS-reachability probe of the resolved model URL runs); embeddings are always-on (`embeddings:  ✅ always-on`). On platforms with no vendored native libs (`_platform_libs_dirname()` returns None, e.g. darwin/x86_64 — Intel Macs or a Rosetta interpreter), the runtime line reports `⏹ unsupported platform … — memory uses keyword search` and is NOT counted as an issue (designed degradation per `embeddings.py`); only a load failure on a supported platform flags `embedding runtime`. When that failure is an INCOMPLETE shipped payload, doctor additionally names the absent files (`Missing native libs for <platform>: …`, from `embeddings.verify_vendored_libs()`) and says it is a packaging defect rather than an unsupported platform — the two are indistinguishable in ctypes' own `Shared library with base name 'llama' not found`, which reads as an architecture problem and misdirects diagnosis. When `LLAMA_CPP_LIB_PATH` is set, doctor reports THAT directory as the thing to check instead (mirroring the loader's exemption): the libs load from there, so blaming the bundled tree would send the operator to reinstall a package they are deliberately not loading from. A `faiss:` line reports whether the optional FAISS accelerator is importable — never an issue on any platform (episodic recall falls back to the stdlib cosine scan); when absent it suggests `pip install faiss-cpu`
9. **Speech-to-Text (optional)**: whisper + ffmpeg presence when STT is enabled. On Windows these are reported as non-fatal `⚠️` notes (neither is a Kiro Crew dependency there, and STT ships enabled-by-default) so a healthy first install exits 0 and the guide's `kirocrew doctor && kirocrew gateway` chain proceeds; on macOS/Linux a missing binary still flags an issue. Fix hints are OS-aware (`brew` / `winget` / Linux)
10. Slack credentials (optional)
11. kiro-cli connectivity
12. Gateway running status

## Update Command

`kirocrew update` pulls the latest source and rebuilds:

1. `git pull` from `KIROCREW_PROJECT_DIR`
2. Rebuilds the dashboard via `build_frontend_sync()` (npm; non-fatal on failure)
3. Reinstalls backend via `pip install -e .`

## Client Port Resolution

`kirocrew token` / `status` / `logout` / `stop` / `restart` must find the port
the gateway is actually bound to. `cli_server.resolve_client_port()` resolves it
in this order, first hit wins:

1. An explicit `--port N` flag (`0` counts — the check is `is not None`).
2. `KIROCREW_PORT`, when it parses as an int.
3. A port **explicitly written** in `dashboard.url`. A portless URL
   (`http://my.host`) is *not* a port choice: `parse_dashboard_url()`
   substitutes `5476` for the server's benefit, so the client re-splits the URL
   and only accepts the port when it was actually named.
4. The sole **gateway-owned run-marker**. A running gateway records
   `<data-home>/run/gateway-<port>.bin` (see
   `kiro_crew.instances.run_marker`, written for the SSH token-mint), so its
   filename already advertises the port. A client with nothing configured reads
   the marker names — never the file contents — and uses that port. Two guards
   keep it from being a guess:
   - **Ownership, not reachability** — `clear_marker()` only runs on graceful
     shutdown, so a crash leaves a stale marker behind and an unrelated process
     may since have bound that port. Because `_token` / `_logout` send
     `X-Local-Secret` to whatever answers, a bare "is something listening" probe
     would walk the local secret into that process. A command-line check is not
     enough either — argv is attacker-chosen, so a listener started as
     `/tmp/kirocrew gateway` would pass it. `_gateway_owns_port()` therefore
     requires three things, none sufficient alone:
     1. the pid recorded in `run/gateway-<port>.pid` (written `0600` inside the
        `0700` `run/` dir, which is on the `is_sensitive_path` floor, so neither
        another local user nor an agent file tool can write it);
     2. that pid must be among the pids listening on the port
        (`platform_compat.find_listening_pids`), which is what makes a stale
        recorded pid harmless;
     3. that pid must be owned by the calling uid
        (`platform_compat.process_owner_uid`) and look like a KiroCrew process.
        The uid check is what closes pid *recycling* into a foreign user's
        process; argv is retained only as defense in depth.

     It **fails closed** at every step: no sidecar, an unparseable pid, a pid
     that does not hold the port, an unresolvable uid, a missing `lsof` /
     `netstat`, or a throwing lookup all deny, and discovery is skipped. A
     same-user attacker is out of scope by construction — they can already read
     `.local_secret` under their own uid.

     **On non-POSIX platforms the step denies outright.** `process_owner_uid`
     cannot report an owner on Windows, and a `KIROCREW_HOME` writable by another
     user would let them replace both the marker and the sidecar with a forged
     listener — the file-permission argument that carries requirement 1 stops
     holding there. So discovery is skipped rather than approximated: Windows
     users keep `--port` / `KIROCREW_PORT`, exactly where they were before this
     fallback existed, so nothing regresses.
   - **Ambiguity** — with several gateways up there is no basis to pick one, so
     the step refuses, prints the candidate ports and the `--port` /
     `KIROCREW_PORT` hint to stderr, and falls through.
5. `_DEFAULT_PORT` (`5476`).

Step 4 is what makes a single gateway started on a non-default port
(`kirocrew gateway --port 6776`) reachable from a bare `kirocrew token` with
zero configuration; before it existed, the client hit a dead 5476 while the
marker naming the live gateway sat unread. Config-load, URL-parse (including a
non-string `dashboard.url`, which raises `TypeError` rather than `ValueError`),
and discovery failures all degrade to the next step — a client command never
dies on a bad config or an unreadable data home.

Because `restart` resolves a port and then polls it for readiness, it passes the
resolved port to the detached replacement (`_spawn_detached_gateway(port)`). The
child re-resolves independently, so without that the replacement could bind 5476
while the parent waited on the discovered port.

The marker is written for **every** dashboard-serving gateway, including a
source-tree `python -m kiro_crew` launch with no console script beside
`sys.executable`: in that case the `.bin` file is written empty, which is inert
for the token mint (its shell clause requires a non-empty executable path) but
still advertises the port for discovery. The pid always goes to the separate
`.pid` sidecar — never into the marker, whose contents mint `cat`s and execs. A
`--slack-only` gateway serves no dashboard, so it writes no marker — there is no
client port to discover.

Writing a marker also **prunes** markers naming other ports. A gateway is a
singleton per data home (`gateway.lock`), so any other port's marker is residue
from a run that crashed before `clear_marker()` could fire. Unpruned, they
accumulate one per port ever used and each costs every client command an extra
listener lookup, making discovery slower the longer a dev box churns ports. The
live gateway is the only writer and knows which port is current, so it is the
right place to reap them; pruning is best-effort, and the ownership check still
rejects anything it misses.

CLI→gateway requests are built against the literal `127.0.0.1`, never the name
`localhost`. On a dual-stack host `localhost` can resolve to `::1` first, and the
listener verification is address-agnostic (`lsof -ti TCP:<port>` cannot tell an
IPv6 squatter from the real IPv4 gateway), so a name-based URL could deliver
`X-Local-Secret` to a socket other than the one that was verified. The URL
*printed* for the browser still uses `resolve_dashboard_host()` (`localhost`) —
that must not change, because the SPA's per-origin `localStorage` is keyed on it.

## Stop Command

`kirocrew stop [--port PORT]` stops a running gateway:

1. If a systemd/launchd service is active **and** the caller did not pass
   `--port` explicitly (see Service Management), stop it via the service
   manager and return — without this branch, SIGTERM-by-port would be
   racing the manager's auto-restart.
2. Otherwise (no service active, or `--port` was passed explicitly to
   target a non-default dev gateway): `platform_compat.find_listening_pids(port)`
   to find PIDs — `lsof -ti TCP:{port} -sTCP:LISTEN` on POSIX, `netstat -ano`
   parsing on Windows (there is no `lsof` there; this previously made
   `kirocrew stop` a no-op on Windows). Both binaries are resolved through
   `platform_compat.trusted_system_bin()` — the fixed system directories, never
   `PATH`, which on a gateway can lead with same-uid-writable dirs — and a name
   that does not resolve there counts as absent rather than falling back.
   `listening_pid_tool_available()` performs the same pinned resolution, so it
   distinguishes "no listener" from "lookup tool missing" without disagreeing
   with the lookup it describes. A host that installs the tool outside those
   directories (NixOS, a Homebrew or conda prefix) therefore reads as not having
   it; `trusted_system_bin()` logs a warning once per name when the tool is on
   `PATH` but not resolvable under the pin, and `tool_outside_trusted_dirs()`
   lets `stop` name where the tool actually is rather than tell an operator who
   already has it to install it. That case carries SEL
   `reason=<tool>_outside_trusted_dirs`, distinct from `<tool>_not_found`, so
   the two are separable in the audit log.
3. `platform_compat.process_command_line(pid)` to verify it's a KiroCrew process —
   `/proc/<pid>/cmdline` (Linux), `ps -o command=` (macOS), `Win32_Process.CommandLine`
   via WMI (Windows). The Windows venv `kirocrew.exe` re-execs `python.exe`, so the
   match is on the command line (`-m kiro_crew gateway` / `\Scripts\kirocrew.exe gateway`),
   not the image name.
4. Terminate each verified PID: `os.kill(SIGTERM)` on POSIX; `taskkill /T /F`
   (via `platform_compat.kill_process_tree`) on Windows so the gateway's detached
   children are reaped too. Liveness is probed with `platform_compat.pid_exists`
   (a raw `os.kill(pid, 0)` would *terminate* the process on Windows).
5. Waits up to 1s for exit.
6. SEL audit event logged.

## Restart Command

`kirocrew restart [--port PORT]` restarts a running gateway. Mirrors
`stop`'s service-aware structure:

1. If a systemd/launchd service is active **and** the caller did not
   pass `--port` explicitly, ask the platform to restart it. On Linux:
   `sudo systemctl restart kirocrew.service` (single
   atomic operation, smaller down-window than stop+start, and the
   supervisor stays in charge of the lifecycle the whole time). On
   macOS: `launchctl unload <plist>` + `launchctl load <plist>` (no
   `-w`, so persistent enable state is unchanged). The deprecated
   `launchctl restart` is avoided because under `KeepAlive` it behaves
   like `stop` (SIGTERM + immediate respawn) and never re-reads the plist.
2. Otherwise (foreground gateway, no service, or `--port` passed
   explicitly to target a non-default dev gateway):
   - `platform_compat.find_listening_pids(port)` (lsof on POSIX, netstat
     on Windows) to detect a running gateway. If found — OR if the lookup
     tool is absent (`not listening_pid_tool_available()`, so a missing
     tool is not mistaken for a dead gateway) — run the existing `_stop`
     kill-by-port path. If not (e.g. the user runs `restart` after a
     crash), skip the stop step rather than erroring — the user expects to
     end up with a running gateway either way. The `_stop` call is wrapped
     in a `try / except SystemExit` so a TOCTOU race (gateway exits between
     the listener check and `_stop`'s own lookup → `_stop` calls
     `sys.exit(1)`) does not abort the restart before the spawn.
   - Spawn a detached `kirocrew gateway` via `subprocess.Popen`, stdin set
     to `subprocess.DEVNULL`, and stdout + stderr redirected to
     `~/.kiro/crew/gateway.log` (the same file the `kirocrew logs` command
     tails for foreground gateways). Detach is per-platform: POSIX uses
     `start_new_session=True`; Windows uses `creationflags=DETACHED_PROCESS
     | CREATE_NEW_PROCESS_GROUP` (there is no setsid) — both via
     `platform_compat`. The shell returns immediately and the user can
     follow logs via `kirocrew logs -f`.
3. SEL audit event logged with `via=service` or `via=fork pid=<n>` so
   the audit trail distinguishes the two paths.

## Service Management

`kirocrew service {install,uninstall,status}` registers the gateway
with the OS service manager so it survives SSH disconnects, restarts
on crash, and starts on boot. Implemented in `src/kiro_crew/service/`.

- **Linux** (`current_platform() == SYSTEMD`):
  - Unit file: `/etc/systemd/system/kirocrew.service` (root-owned).
  - Install: `sudo install` writes the unit, then `sudo systemctl
    daemon-reload && sudo systemctl enable --now kirocrew.service`.
    Privilege is resolved per call: already-root (euid 0) skips `sudo`
    entirely — required on minimal container / `root`-login images that
    ship no `sudo` binary — and a non-root caller with no `sudo` fails
    with a clear `ServiceInstallError` rather than an uncaught
    `FileNotFoundError`.
  - The gateway runs as `User=$USER Group=$(id -gn)` — kirocrew
    code never runs under sudo. Only `install` and `systemctl` invocations
    are elevated.
  - **Environment**: values are captured from the installer's environment
    into the unit's `Environment=` lines at install time
    (`service_environment()` in `service/common.py`) — this is how
    `KIROCREW_PORT=5477 kirocrew service install` binds a non-default port.
    The unit also reads `EnvironmentFile=-/etc/kirocrew/kirocrew.env`, an
    operator-editable file the installer seeds create-if-absent (a reinstall
    never clobbers edits). systemd applies the file AFTER — and overriding —
    the baked `Environment=` lines, so editing it and running `sudo systemctl
    restart kirocrew` changes a value (e.g. the port) without reinstalling.
    Uninstall removes the file and its `/etc/kirocrew` directory.
  - Boot survival via `WantedBy=multi-user.target` (no linger needed —
    that's a user-service concept; this is system-level).
  - Crash-loop safety: `StartLimitBurst=3 StartLimitIntervalSec=300`.
  - Logs are read from the journal: `sudo journalctl -u kirocrew -f`,
    or unprivileged if the user is in `systemd-journal` / `adm`.
- **macOS** (`current_platform() == LAUNCHD`):
  - Plist: `~/Library/LaunchAgents/dev.kirocrew.gateway.plist`
  - Install: `launchctl load -w <plist>`. `RunAtLoad=true` and
    `KeepAlive` ensure auto-start and crash recovery.
  - Stdout and stderr are written to
    `~/Library/Logs/KiroCrew/gateway.{log,err}`.
- **Other platforms**: install/uninstall return exit code 2 with a
  message pointing to manual setup.

`kirocrew stop` is service-aware: if the service is active it calls
the platform's stop instead of SIGTERM, so the manager does not
immediately restart the gateway under us.

## Logs Command

`kirocrew logs [-n LINES] [-f]` tails the gateway log from whichever
source is most appropriate:

1. systemd journal if the system service is installed on Linux. Tries
   unprivileged `journalctl` first; falls back to `sudo journalctl`
   only if the unprivileged probe returns no rows.
2. launchd stdout file if a plist exists on macOS
3. `~/.kiro/crew/gateway.log` for foreground gateways

Uses `os.execvp` so signals (Ctrl+C) propagate naturally to the
underlying `journalctl`/`tail` process.

## Dashboard Self-Update

On gateway startup and every 12 hours, a background task runs `git fetch`
and compares the remote `__version__` with the local version. Only triggers
when the remote version is strictly higher (commits without a version bump
are ignored).

- Topbar shows `📦 v0.1.3` badge — click to check and view changelog
- If newer version found: badge turns into "📦 Update Available"
- Clicking opens a dismissible changelog modal with rendered markdown
- "Update Now" button: `git pull` → rebuild → `os.execv()` restart
- Health indicator shows "Updating…" during the process
- SSE auto-reconnects when the new process starts

## Status Command

`kirocrew status` queries the running gateway's `/api/status` endpoint
and prints uptime, sessions, messages, tool calls, subagents, crons, lessons.

## App Dev Mode

`kirocrew app dev <name> [--off]` toggles an installed App Kit app into (or,
with `--off`, out of) **dev mode**, which speeds the app-UI edit loop by serving
UI files uncached and live-reloading the dashboard on file change. The command
writes the flag out-of-process; the running gateway's watcher picks it up within
one poll interval, so no gateway restart is needed. Full App Kit developer docs
live in `docs/app-kit/api-reference.md`; the durable contract surfaces this
feature introduces are:

- **Persisted schema — `installed.json` `dev: bool`** (default `false`): a
  per-app flag in each app's `~/.kiro/crew/apps/<name>/installed.json`. Tolerant
  on read (absent ⇒ `false`), reversible, no migration. This field is the sole
  authoritative source of truth for an app's dev-mode state. Builtin apps cannot
  enter dev mode.
- **Endpoint — `POST /api/apps/{name}/dev`**, body `{"enabled": <bool>}`,
  returns `{"name": <name>, "dev": <bool>}`. Behind standard gateway auth; emits
  an `app_dev_mode` SEL audit event. `400` for a non-boolean body, a builtin
  app, or an unsafe app name; `404` when the app is not installed. Equivalent to
  the CLI toggle for in-dashboard control.
- **WebSocket event — `app_reload`**, payload `{"app": <name>, "ts": <float>}`,
  broadcast when a dev-mode app's `ui/` tree changes; the dashboard reloads that
  app so edits appear immediately.
- **Serving behavior:** while an app is in dev mode the gateway serves its UI
  with `Cache-Control: no-store`; otherwise the standard revalidation header
  applies.

An internal, unstable sentinel cache under `~/.kiro/crew/apps/` mirrors the set of
dev-mode apps so the zero-dev-apps steady state costs one `stat()` per second.
It is a derived cache reconciled from `installed.json` at watcher init (under a
cross-process lock, atomic with concurrent toggles), **not** part of the App Kit
contract — its path and format are internal and may change without notice.

## Computer Use Commands

`kirocrew computer {doctor [--json] | apps | call}` — hand-rolled dispatch
mirroring `browser/cli.py` (see [computer-use.md](computer-use.md)).

**`doctor`** reports, in order: whether the platform is supported (macOS today;
Windows and Linux report a typed refusal), whether the keystone primary enable at
`~/.kiro/crew/computer_use.json` is on, and the macOS TCC probe
(`AXIsProcessTrusted()` + `CGPreflightScreenCaptureAccess()`). The probe is
**advisory and never a gate**: macOS attributes a grant to the *responsible
parent* of the process tree, so both rows can read `missing` while a
full-fidelity capture succeeds — observed live. `doctor` therefore prints a
`responsible_hint` naming the process a user should actually grant (the packaged
app, or the terminal that launched a dev gateway) and says outright that "not
detected" does not always mean unavailable. It never calls
`CGRequestScreenCaptureAccess`, which would pop a system dialog from a background
process.

`--json` is the machine form the **gateway shells out to** for the Settings
permission rows. That indirection is deliberate: a short-lived subprocess keeps
native ctypes out of the gateway, so a native fault cannot take down the gateway
and with it cron, Slack and the dashboard WebSocket.

**`apps`** lists on-screen applications resolved from
`CGWindowListCopyWindowInfo` (layer-0 windows only, never `pgrep` — a `pgrep -n`
lookup returns short-lived helper pids whose accessibility tree is empty). It runs
`computer_list_apps` through the SAME gated dispatcher as `call`, so it is refused
while the feature is disabled, in an unattended session, or under a policy that bans
computer use — the agent can run this command with bash, so an ungated version was
an unauthorized read of every window title.

**`call`** runs one tool — `call computer_get_state app=Finder` — or a whole
sequence in ONE process: `call --calls '[{"tool":"computer_get_state","args":
{"app":"Finder"}},{"tool":"computer_click","args":{"app":"Finder",
"element_index":12}}]'`. The batch form exists because `element_index` values only
resolve against the per-process snapshot cache that produced them, so two separate
invocations cannot share them. `key=value` arguments are JSON-decoded when they can
be (`element_index=3` → int, `screenshot=false` → bool) and kept as text otherwise
(`app=Finder`). `--json` emits `[{tool, text}, …]`; the exit code is non-zero if any
reply carries the `Error: ` prefix, and a batch runs to completion rather than
aborting at the first refusal.

`call` goes through `computer_use.tools.dispatch_tool`, the **same** chokepoint an
agent call traverses, so the primary enable, the target policy and the secure-field
floors all apply — it is a reproduction tool, not a bypass. Its session key is the
attended `cli_chat` surface, which is what the SEL audit records. There is no
separate diagnostics opt-in and no identity proof: the unattended-surface refusal
that made one necessary was removed along with the rest of the computer-use
governance model.

All three are **human-facing**. `apps` has an MCP twin (`computer_list_apps`) per
the MCP-first rule; `doctor` is a permission diagnostic rather than a capability,
so the rule does not bind it; and `call` adds no capability at all — it is a
harness over the ten existing MCP tools, and deliberately has **no** MCP twin,
because a tool that runs other tools would let a model launder one per-call gate
decision into many. There is deliberately **no** `kirocrew computer state <app>` —
that would be a second, CLI-shaped spelling of an LLM-facing capability and would
have to be an MCP tool instead (it is: `computer_get_state`).

## Gateway Test Harness

Four composable flags let an integration test or eval harness boot a gateway
deterministically, with no model and no developer-machine state:

```bash
kirocrew gateway --test-mode          # bundle: ephemeral port + json-ready + reads approval
kirocrew gateway --port auto          # OS-assigned port, avoiding a collision with a real gateway
kirocrew gateway --json-ready         # print KIROCREW_READY:{port,token,pid,home} once listening
kirocrew gateway --approval reads     # auto-approve read-only tools
kirocrew gateway --approval yolo      # auto-approve ALL tools
```

`--json-ready` is what makes the harness race-free: the caller waits for the
`KIROCREW_READY` line instead of polling a port, and reads the token from it rather
than minting one.

**`--approval yolo` refuses to start unless `KIROCREW_HOME` is explicitly set to a
non-default path.** The flag disables every per-call approval, so pointing it at the
real data home would let a test drive an operator's live sessions and credentials.
The guard is a startup refusal rather than a warning because a warning in CI output
is not read.
