# Installing & Testing KiroCrew on Windows

KiroCrew runs **natively on Windows** as a Python **source install**.
The cross-platform process / signal / file-lock / metrics behavior is routed
through `kiro_crew.platform_compat`, so macOS + Linux behavior is unchanged and
the same code path also runs on Windows.

## Desktop installer (preview, CI-built)

CI's desktop lane (`build-desktop.yml`) also builds a Windows desktop app:
`KiroCrew Setup.exe` plus the Squirrel.Windows `RELEASES`/`.nupkg` pair, with
the backend bundled (no separate Python install needed). Current status:

- **CI artifact only** — produced on nightly/release runs and the manual
  `workflow_dispatch` probe; not yet published to the download CDN (that is
  the upcoming `publish-windows.yml` lane).
- **Unsigned** — SmartScreen shows an "unrecognized app" interstitial
  (More info > Run anyway). Authenticode signing is a tracked follow-up.
- **No auto-update yet** — the macOS/Linux client moved to
  electron-updater (`latest-mac.yml` / `latest-linux.yml` feeds), but its
  win32 path drives NSIS installers, not Squirrel.Windows, so win32 stays
  excluded from the updater (`SUPPORTED_PLATFORMS` in `auto-update.js`)
  until the NSIS migration lands (issue #598); installs update by running
  a newer Setup.exe.

The source install below remains the fully supported path.

## Prerequisites

| Tool | Why | Get it |
|------|-----|--------|
| **Git for Windows** | clone the repo | https://git-scm.com/download/win |
| **kiro-cli** | the agent backend (ACP); the first dashboard launch can install it | Kiro Crew setup page or kiro-cli's native Windows release |
| **Python 3.10–3.12** | the venv runtime (3.12 preferred; numpy 1.x has no 3.13 wheel) | https://python.org — install user-scoped, or `winget install Python.Python.3.12` |
| **Node.js** (optional) | builds the full React dashboard; without it the gateway serves the prebuilt bundle | `winget install OpenJS.NodeJS.LTS` |

No admin is required — everything installs user-scoped under `%USERPROFILE%`.

Avoid the Microsoft Store `python` alias stub: KiroCrew's interpreter finder
(`platform_compat.find_python_interpreter`) rejects it, but a Store-only `python`
on `PATH` can still confuse other tooling. Prefer a real CPython install.

## Install (native)

From a clone, in PowerShell:

```powershell
git clone https://github.com/kirodotdev/KiroCrew.git
cd kirocrew

# Build the frontend first (optional but recommended) so the dashboard is bundled:
#   cd website; npm install; npm run build; cd ..
#   Copy-Item -Recurse website\dist src\kiro_crew\static\dist

py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
# tzdata: Windows has no system IANA tz database, so zoneinfo.ZoneInfo() needs it.
# (setup.cfg already declares tzdata under a platform_system == "Windows" marker,
#  so a plain `pip install -e .` pulls it in on Windows.)
pip install -e ".[voice]"
```

Then:

```powershell
kirocrew setup
kirocrew gateway
```

Open the dashboard URL printed by the gateway. On first launch, Kiro Crew checks
the **Windows gateway host** for a runnable and authenticated Kiro CLI. If it is
missing, choose **Install Kiro CLI** to download and run the fixed official
PowerShell installer; if it is signed out, choose **Sign in to Kiro** and
complete the device-code flow in the browser. The dashboard opens automatically
after `kiro-cli whoami` succeeds. This setup runs on the gateway machine, which
may be different from the computer running the browser.

`kirocrew` / `kirocrew-browse` land in `.venv\Scripts\`. If a launched (non-shell)
gateway can't find the built-in `kirocrew-cron` / `kirocrew-core` MCP servers,
that dir is appended to the MCP spawn `PATH` automatically
(`env.augmented_path`), and the managed-server invocation falls back to
`python -m kiro_crew <sub>` when the `kirocrew.exe` wrapper isn't resolvable.

## Per-feature status on Windows

| Feature | Status on Windows |
|---------|-------------------|
| Core gateway / chat / cron / dashboard | works |
| MCP tool probing / `Discover & Sync` inventory | first-party (`kirocrew-cron`, `kirocrew-core`) works; third-party servers cannot be probed (they need the POSIX OS-level sandbox and fail closed). The tools themselves still run — kiro-cli spawns them, and registration never gated on probe status |
| Pull-request source drawer provider fetch/check/resolve | not yet — provider CLIs require the POSIX OS-level sandbox and fail closed with a clear unsupported response |
| Browser automation (Playwright MCP) | works (installed via `npm`/`npx @playwright/mcp`) |
| Vector memory / embeddings | works natively — the vendored llama-cpp-python (`_vendor/llama_cpp_libs/win_amd64`) loads the Qwen3 GGUF in-process; no Ollama, remote endpoint, or Docker needed |
| STT (whisper / optional cloud transcription) | works |
| Voice reply (Piper TTS) | not yet — upstream rhasspy/piper ships no Windows binary; Polly (optional) works if the `aws` CLI is present |
| SSH tunnel (`kirocrew cloud` remote dashboard) | not yet — needs the OpenSSH client on `PATH` and a signal-handling audit |
| MCP gateway (opt-in, OFF by default) | not yet — the AF_UNIX socket + `SO_PEERCRED` peer check are POSIX-only |
| App backends / App Store install | not yet — app spawn needs the POSIX OS-level sandbox; stale-orphan reaping additionally needs `ps` and fails safe (orphans leak, nothing mis-killed) |

The not-yet items are tracked as Windows feature-parity follow-ups.

## The OS-level sandbox has no Windows backend

`sandbox.detect_backend()` supports exactly two backends — Linux user namespaces
and macOS `sandbox-exec` — so on Windows it always reports `none`. `wrap_argv()`
**fail-closes** (raises `RuntimeError`) whenever no backend is available and the
requested mode is anything other than `"off"`, unless the operator sets
`agent.sandbox_allow_unsandboxed_exec=true`.

Windows works today because the shipped default is `agent.sandbox: "off"`, which
delegates isolation to kiro-cli's own internal agent sandbox. Consequences to
keep in mind:

- **Leave `agent.sandbox` at `"off"` on Windows.** Setting it to `"auto"` makes
  every kiro-cli spawn fail closed, including chat itself.
- Callers that **hardcode** a non-`off` mode still fail closed here regardless of
  config. That is why the pull-request source-drawer providers are listed as
  not-yet above. One-shot `kiro-cli` queries (`--list-models`, `/usage`) follow
  the configured tier instead and therefore work — see the security spec's
  "One-shot kiro-cli spawns follow the configured tier".
- The app-level controls are unaffected: denied-command patterns, sensitive-path
  blocking, credential redaction, governance, and the SEL audit log all run in
  the KiroCrew process and apply identically on Windows.

### Audit: which spawn sites this affects

Every site below wraps an **untrusted or agent-influenced** target, so the
hardcoded tier is a deliberate security control and is *kept* — these features
fail closed on Windows by design rather than running unconfined:

| Spawn site | Target | Tier |
|---|---|---|
| `mcp_discovery.probe_server` (third-party servers) | any binary named in MCP config | `standard` |
| `apps/registry.py`, `apps/routes.py` | `git clone`/`pull` of app repos | `strict` / derived |
| `apps/backend.py` | app venv, `pip install`, `npm install` | `standard` |
| `dashboard/handlers/themes.py` | `git clone` of a theme URL | `standard` |
| `dashboard/handlers/memory.py` | `ensurepip`, `pip install faiss-cpu` | `standard` |
| `cron_script.py` | user/agent-authored cron scripts | `standard` / `cc` |
| `hooks.py` | user/agent-authored hook command (`cmd /c`) | `auto` |
| `cloud/aws.py`, `deploy/engine.py` | `aws` CLI | `standard` |
| `dashboard/handlers/source_providers.py` | `gh` / `glab` CLI | `standard` |
| `dashboard/handlers/worktree.py`, `git_coord.py`, `file_explorer` | `git` | `strict` / `standard` |
| `task_executor.py` | project test command | `standard` |
| `voice_reply.py` | Piper / Polly binary | `standard` |

Trusted first-party targets instead take the same carve-out `kiro_prerequisite`
has always applied to `kiro-cli` on Windows:

| Spawn site | Target | Behavior on Windows |
|---|---|---|
| `kiro_prerequisite._run_process` | `kiro-cli` | wrap skipped (`not IS_WINDOWS`) |
| `api_models`, `_fetch_usage_bg` | `kiro-cli` one-shot | configured `agent.sandbox` tier |
| `mcp_discovery.probe_server` (managed) | `kirocrew-cron` / `kirocrew-core` | `mode="off"` |

A native Windows confinement backend (AppContainer / restricted token / job
object) is not implemented.

## Process signalling: `os.kill(pid, 0)` is destructive on Windows

CPython maps `os.kill(pid, sig)` onto `TerminateProcess(handle, sig)` for most
signals — but **signal 0 is `CTRL_C_EVENT` on Windows**, so `os.kill(pid, 0)`
takes the other branch and calls `GenerateConsoleCtrlEvent(CTRL_C_EVENT, pid)`,
delivering a real Ctrl+C to the console process group identified by `pid`.
Either way the idiomatic POSIX liveness probe is **not** a probe here: it
signals instead of asking, tells you nothing reliable about whether the pid
exists, and fails outright when `pid` is not a console group id. The signal
lands on whatever currently owns that pid, so a stale or recycled pid can take
an unrelated process group down.

Always route liveness through the shim: `platform_compat.pid_exists(pid)`
(`OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)` on Windows), or
`pid_liveness(pid)` when you must distinguish EPERM/unsignalable from dead. Both
preserve the POSIX semantics callers depend on, including "EPERM means the
process exists but is not signallable by us — alive, not gone".

`test/test_windows_kill_probe_audit.py` is an AST tripwire that fails on any new
raw signal-0 probe under `src/kiro_crew`. Sites that genuinely cannot execute on
Windows go in its `GATED_PROBES` allowlist with a justification, and a second
test rejects stale allowlist entries so an exemption cannot outlive the code it
covered.

Related gap found by that audit: **app-backend stale-orphan reaping never runs on
Windows.** Its PID-reuse guard calls `apps/backend.py:_proc_start_time`, which
reads `/proc/<pid>/stat` on Linux and otherwise shells `ps` — absent on Windows —
so it returns `None` and the reap loop always declines to act. This **fails safe**
(an orphaned app backend leaks rather than the wrong process being signalled).
Giving `_proc_start_time` a Windows implementation is the fix; it must land
together with a shim-routed liveness probe, because that `None` was the only
thing keeping the raw probe below it unreachable.

## Secret-at-rest posture on Windows

Files under `%USERPROFILE%\.kiro\crew` that hold auth material — the token
signing key, refresh-token state, per-app secrets, snapshot tarballs, and the
cron internal-secret temp file — are locked down to the current user via an
owner-only NTFS DACL (inheritance stripped, `S-1-3-4:F` = Owner Rights full
control). This is applied through `platform_compat.restrict_to_owner`, which
routes to `os.chmod(..., 0o600)` on POSIX and `icacls /inheritance:r /grant:r
"*S-1-3-4:F"` on Windows. Failure is fail-loud (raises `OSError`) so the
security-warning handlers in each caller fire — a naive `if IS_POSIX: os.chmod`
guard would silently no-op on Windows, leaving secrets group/world-readable
under NTFS.

## Troubleshooting

- **`ModuleNotFoundError: No module named 'fcntl'`** — you installed a
  branch/commit that predates the Windows port. `fcntl` is a Unix-only Python
  stdlib module; it cannot be pip-installed on Windows. Update to a build that
  routes locking through `platform_compat`.
- **`ZoneInfoNotFoundError` / "No time zone found"** — install `tzdata`
  (`pip install tzdata`); Windows has no system IANA tz database.
- **"Python was not found" (Microsoft Store)** — a bare `python`/`python3` was
  resolving the Store alias stub; install a real CPython and ensure it precedes
  the stub on `PATH`.
- **`kirocrew stop` reports "No KiroCrew gateway currently running"** — fixed for
  localized Windows. `find_listening_pids` no longer matches the literal English
  `LISTENING` state (which some localized editions translate, e.g. `ABHÖREN` on
  German Windows); it identifies a listener by its **wildcard foreign address**
  (`0.0.0.0:0` / `[::]:0`), which is locale-independent, and keeps the English
  literal only as a defensive second signal. If stop still finds nothing:
  `netstat -ano | findstr :5476` to find the PID, then `taskkill /F /PID <pid>`.
- **Model picker shows only "Auto"** — fixed. `GET /api/models` shells `kiro-cli
  chat --list-models` and used to wrap it at `wrap_argv`'s hardcoded `"auto"`
  tier, which demands an OS-level sandbox backend; Windows has none, so the wrap
  fail-closed, the handler returned its degraded 503, and the dashboard fell back
  to an auto-only list. It now requests the configured `agent.sandbox` tier
  (`sandbox.agent_sandbox_mode()`), the same one the agent session itself uses.
  If the picker is still auto-only, check that `agent.sandbox` in
  `%USERPROFILE%\.kiro\crew\config.json` is `"off"` (the shipped default) —
  setting it to `"auto"` on Windows re-enables the fail-closed path, because
  KiroCrew's OS sandbox has no Windows backend. The same applies to the credits
  usage pill.
- **Web terminal / interactive SSO login panels** — unavailable on Windows
  (they need `pty`/`fork`/`termios`); they return a clear "not supported on
  Windows" response instead of crashing.

## Related

- [README](../README.md) — quick-start Platforms note
- [AGENTS.md](../AGENTS.md) — "Platform Support" + the `platform_compat` shim table
- `src/kiro_crew/platform_compat.py` — the cross-platform shim
