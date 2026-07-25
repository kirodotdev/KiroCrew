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
- **No auto-update yet** — the app does not consume the Squirrel feed on
  win32 until the publish + updater lane lands; installs update by running a
  newer Setup.exe.

The source install below remains the fully supported path.

## Prerequisites

| Tool | Why | Get it |
|------|-----|--------|
| **Git for Windows** | clone the repo | https://git-scm.com/download/win |
| **kiro-cli** | the agent backend (ACP) — must be on `PATH` | kiro-cli's native Windows release |
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

`kirocrew` / `kirocrew-browse` land in `.venv\Scripts\`. If a launched (non-shell)
gateway can't find the built-in `kirocrew-cron` / `kirocrew-core` MCP servers,
that dir is appended to the MCP spawn `PATH` automatically
(`env.augmented_path`), and the managed-server invocation falls back to
`python -m kiro_crew <sub>` when the `kirocrew.exe` wrapper isn't resolvable.

## Per-feature status on Windows

| Feature | Status on Windows |
|---------|-------------------|
| Core gateway / chat / cron / dashboard | works |
| Pull-request source drawer provider fetch/check/resolve | not yet — provider CLIs require the POSIX OS-level sandbox and fail closed with a clear unsupported response |
| Browser automation (Playwright MCP) | works (installed via `npm`/`npx @playwright/mcp`) |
| Vector memory / embeddings | via a **remote embedding endpoint or Docker**; local Ollama auto-install is not yet supported |
| STT (whisper / optional cloud transcription) | works |
| Voice reply (Piper TTS) | not yet — upstream rhasspy/piper ships no Windows binary; Polly (optional) works if the `aws` CLI is present |
| SSH tunnel (`kirocrew cloud` remote dashboard) | not yet — needs the OpenSSH client on `PATH` and a signal-handling audit |
| MCP gateway (opt-in, OFF by default) | not yet — the AF_UNIX socket + `SO_PEERCRED` peer check are POSIX-only |

The not-yet items are tracked as Windows feature-parity follow-ups.

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
- **`kirocrew stop` reports "No KiroCrew gateway currently running" on a
  non-English Windows** — `find_listening_pids` matches the `netstat` state
  against the wildcard foreign address and the literal English `LISTENING`;
  some localized Windows editions emit translated state names. Workaround:
  `netstat -ano | findstr :5476` to find the PID and `taskkill /F /PID <pid>`.
- **Web terminal / interactive SSO login panels** — unavailable on Windows
  (they need `pty`/`fork`/`termios`); they return a clear "not supported on
  Windows" response instead of crashing.

## Related

- [README](../README.md) — quick-start Platforms note
- [AGENTS.md](../AGENTS.md) — "Platform Support" + the `platform_compat` shim table
- `src/kiro_crew/platform_compat.py` — the cross-platform shim
