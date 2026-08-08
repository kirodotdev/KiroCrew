# Installing & Testing Kiro Crew on Windows

Kiro Crew runs **natively on Windows** as a Python **source install**.
The cross-platform process / signal / file-lock / metrics behavior is routed
through `kiro_crew.platform_compat`, so macOS + Linux behavior is unchanged and
the same code path also runs on Windows.

## Desktop installer (preview, CI-built)

CI's Windows lane (`build-windows.yml`) also builds a Windows desktop app: an
NSIS `KiroCrew Setup <version>.exe` with the backend bundled (no separate Python
install needed). It has its own workflow rather than being a leg of
`build-desktop.yml` because Authenticode signing has to happen *during* the
build — the installer compresses its own already-signed executable — so that job
needs AWS credentials the shared build workflow deliberately does not hold.
Current status:

- **CI artifact only** — produced on nightly/release runs and the manual
  `workflow_dispatch` probe; not yet published to the download CDN (that is
  the upcoming `publish-windows.yml` lane).
- **Signing wired but not yet active** — the AWS Signer path is in place and
  skips cleanly until the signing profiles are provisioned, so today's
  installers are still unsigned and SmartScreen shows an "unrecognized app"
  interstitial (More info > Run anyway).
- **No auto-update yet** — win32 remains outside `SUPPORTED_PLATFORMS` in
  `auto-update.js`. The NSIS target removes the *packaging* blocker
  (electron-updater's win32 path is `NsisUpdater`, and it has no
  Squirrel.Windows support at all), but two prerequisites remain: a published
  `latest.yml` feed alongside `latest-mac.yml`/`latest-linux.yml`, and active
  signing — `NsisUpdater` verifies Authenticode fail-closed, so an unsigned
  installer makes every update fail rather than merely warn. Tracked in
  [issue #598](https://github.com/kirodotdev/KiroCrew/issues/598); until then,
  installs update by running a newer Setup.exe.
- **Assisted installer, per user by default** — `nsis.oneClick` is false and
  `perMachine` is false, so the installer offers an install-mode page whose
  default is a per-user install into a directory named from the product name,
  with no UAC prompt. Choosing "for all users" on that page opts into an
  elevated install under Program Files instead. The per-user default is what
  keeps a nightly install (`KiroCrew Nightly`) side by side with a stable one
  rather than replacing it; nightly additionally pins its own `nsis.guid` so
  the two channels do not share an uninstall registry key. Either mode leaves
  the Kiro Crew home alone (`deleteAppDataOnUninstall` stays false, and
  `~/.kiro/crew` is outside the install directory).

The source install below remains the fully supported path.

## Prerequisites

| Tool | Why | Get it |
|------|-----|--------|
| **Git for Windows** | clone the repo | https://git-scm.com/download/win |
| **kiro-cli** | the agent backend (ACP); the first dashboard launch can install it | Kiro Crew setup page or kiro-cli's native Windows release |
| **Python 3.10-3.13** | the venv runtime. `python_requires` is `>=3.10` and 3.13 is in the supported range, but **3.12 is the tested Windows runtime** (it is what the Windows CI shard runs, and numpy 1.x ships no 3.13 Windows wheel) | https://python.org (install user-scoped), or `winget install Python.Python.3.12` |
| **Node.js** (optional) | builds the full React dashboard; without it the gateway serves the prebuilt bundle | `winget install OpenJS.NodeJS.LTS` |

No admin is required — everything installs user-scoped under `%USERPROFILE%`.

Avoid the Microsoft Store `python` alias stub: Kiro Crew's interpreter finder
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

`kirocrew` lands in `.venv\Scripts\`. If a launched (non-shell)
gateway can't find the built-in `kirocrew-cron` / `kirocrew-core` MCP servers,
that dir is appended to the MCP spawn `PATH` automatically
(`env.augmented_path`), and the managed-server invocation falls back to
`python -m kiro_crew <sub>` when the `kirocrew.exe` wrapper isn't resolvable.

## The unsandboxed-exec opt-in

Windows has no OS sandbox backend, and `wrap_argv` fails closed rather than running
an agent subprocess unconfined without consent. So chat tool calls, and the Papyrus
compile and git paths, need an explicit opt-in in `%USERPROFILE%\.kiro\crew\config.json`:

```json
{ "agent": { "sandbox_allow_unsandboxed_exec": true } }
```

**`kirocrew setup` now offers this for you.** Because Windows has no OS-level
sandbox backend, the wizard detects that and asks once — stating that agent
subprocesses will be able to read your home directory, including `.aws` and
`.ssh`, with no OS confinement. It defaults to **no** and writes the key only if
you answer yes, so the choice stays yours; the JSON above remains the manual
equivalent if you skipped the prompt or run setup non-interactively. Answering no
(or pressing Enter) leaves the fail-closed posture in place, and the wizard tells
you how to opt in later.

Setting it means agent subprocesses run with your own user privileges, which is the
same posture as running the tool yourself in a shell. Config is read live, so no
gateway restart is needed. Without it, the affected paths answer a clear 422 naming
the remedy rather than failing obscurely.

## Per-feature status on Windows

| Feature | Status on Windows |
|---------|-------------------|
| Core gateway / chat / dashboard | works — a source install with a built `website/dist` is linked into `src/kiro_crew/static/dist` at gateway start via a **directory junction** (`platform_compat.symlink_or_junction`), which needs no privilege; a symlink there would need `SeCreateSymbolicLinkPrivilege` and would leave a non-elevated install serving the "not built" page |
| LLM cron jobs (the `message` kind) | works |
| Script cron jobs | need the `agent.sandbox_allow_unsandboxed_exec` opt-in above — they run through `wrap_argv`, which fail-closes where no OS sandbox backend exists. Without it the job fails with a message naming that setting (it no longer raises an uncaught error) |
| Command cron jobs (`sh -c "…"`) | not supported on Windows — the stored command is vetted under POSIX-sh semantics, and Windows ships no shell whose language matches: cmd.exe is not POSIX at all, and Git-for-Windows's `sh.exe` is bash and performs brace expansion that hides `cat ~/.a{w,w}s/credentials` from the vet. The job fails-closed with an explanation. Use a **script cron** or an LLM `message` cron on this platform |
| Script hooks (Settings → Hooks) | need the `agent.sandbox_allow_unsandboxed_exec` opt-in above (like script crons — the hook command routes through `wrap_argv`, which fail-closes where no OS sandbox backend exists; without it the hook returns that message as its `error`). With the opt-in they run in **cmd.exe** language: a hook `command` runs as `%ComSpec% /c "<command>"`, so read the context env vars as `%KIROCREW_HOOK_EVENT%` / `%KIROCREW_HOOK_CONTEXT%` (not `$VAR`), and group arguments with double quotes only (cmd.exe gives `'…'` no meaning). The line reaches cmd.exe verbatim, so a quoted interpreter path with a space works. A hook authored on macOS/Linux is not portable and must be rewritten |
| Pull-request source drawer provider fetch/check/resolve | not yet — provider CLIs require the POSIX OS-level sandbox and fail closed with a clear unsupported response |
| Browser automation (Playwright MCP) | works (installed via `npm`/`npx @playwright/mcp`) |
| Vector memory / embeddings | via a **remote embedding endpoint or Docker**; local Ollama auto-install is not yet supported |
| STT (whisper / optional cloud transcription) | works |
| Voice reply (Piper TTS) | not yet — upstream rhasspy/piper ships no Windows binary; Polly (optional) works if the `aws` CLI is present **and** the `agent.sandbox_allow_unsandboxed_exec` opt-in above is set — the `aws polly` spawn routes through `wrap_argv`, which fail-closes where no OS sandbox backend exists. Without it synthesis returns no audio and the log names that setting |
| SSH tunnel (`kirocrew cloud` remote dashboard) | not yet — needs the OpenSSH client on `PATH` and a signal-handling audit |
| MCP server tool listing (dashboard MCP page, `kirocrew doctor`) | **built-in servers work, no opt-in** — with no sandbox backend the probe falls back to reading `kirocrew-core` / `-cron` / `-computer`'s own tool declaration instead of spawning them, and logs a WARNING noting that `ok` means "declared" rather than "handshake succeeded" (it does not verify the server can start; set `agent.sandbox_allow_unsandboxed_exec` to probe for real). A **third-party** server has no declaration to read, so its listing needs that opt-in — its binary is named by config and spawning it is what the sandbox exists to confine. The third-party server itself is unaffected: kiro-cli launches it from the agent config without this probe, so its tools still work in chat |
| MCP gateway (opt-in, OFF by default) | works — a named-pipe transport replaces the AF_UNIX socket, and the peer check uses `GetNamedPipeClientProcessId` + a SID comparison in place of `SO_PEERCRED`. Still opt-in: set `mcp_gateway.enabled` to turn it on |
| Papyrus (LaTeX editor, opt-in builtin) | works, **but compiling and git need the `agent.sandbox_allow_unsandboxed_exec` opt-in above** — like chat, its spawns route through `wrap_argv`, which fail-closes where no OS sandbox backend exists. Without it, compile and clone/commit/push/pull answer a clear 422 (`compiler_sandbox_unavailable` / `git_sandbox_unavailable`) naming the remedy rather than a bare "internal error". The managed Tectonic compiler is Windows-pinned (`x86_64-pc-windows-msvc`); Windows-on-ARM has no upstream asset and keeps the manual install path |

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

## File locking on Windows

`platform_compat.file_lock` / `acquire_lock` provide a genuine *blocking*
acquire on Windows, not a best-effort one. The catch is that `msvcrt.locking`'s
own "blocking" codes (`LK_LOCK` / `LK_RLCK`) are **not** the equivalent of
POSIX `fcntl.flock(LOCK_EX)`: they retry ~10 times at 1-second intervals and
then raise `EDEADLOCK`, so a naive wrapper would silently give up after ~10s
and run its read-modify-write with no exclusion — losing writes (this was the
root cause of the concurrent-memory-append data loss). The shim instead spins
on the non-blocking code (`LK_NBLCK`), with two behaviors by context. On the
asyncio **event-loop thread** the acquire is single-shot — a spin-sleep there
would freeze chat/heartbeat, so it takes the lock if free and otherwise fails
immediately. **Off the loop** (cron, home migration, app backends) it polls up
to a generous `_WIN_LOCK_TIMEOUT_SECS` ceiling — long enough to wait out a
legitimately long holder such as a data-home migration, rather than racing it,
yet bounded so a truly stuck/permission-denied fd still fails. Either way, if
the lock cannot be taken the acquire **fails closed**: it raises rather than
entering the critical section unserialized, since proceeding lock-less is the
exact fail-open that loses writes. Non-blocking `try_acquire_lock` already used
`LK_NBLCK` and is unchanged.

## Win32 struct layouts live at module scope

Every `ctypes.Structure` subclass the Win32 helpers need is declared **once at
module scope** — `_ProcessEntry32`, `_ProcessMemoryCounters`, `_MemoryStatusEx`,
`_SidAndAttributes` and `_TokenUser` in `platform_compat`, plus
`_SecurityAttributes` in `mcp_gateway/transport.py` and `_VMStatistics64` in
`subagent.py`. Declaring one inside the function that uses it is a **memory
leak**, not a style question: `ctypes.POINTER(T)` memoises `T -> POINTER(T)` in a
module-level dict inside ctypes that is never evicted, so a locally-declared
Structure pins a brand-new pair of type objects on every call. The affected
helpers are all polled — the dashboard's system-metrics endpoint, the RSS-recycle
watchdog, the process-tree walk behind `kill_process_tree`, and the MCP pipe's
per-connection peer check — so the gateway grew unboundedly on Windows alone
(measured at ~8 KiB per `proc_rss_bytes` call, ~15 MiB per 2,000 calls, never
reclaimed). POSIX is unaffected because those branches read `/proc`, `sysctl` or
`resource` instead of calling Win32.

Taking `ctypes.POINTER()` is what pins the type, so a struct that is only ever
instantiated (never pointed at) does not leak — but the distinction is too subtle
to rely on, and `test_platform_compat.py::TestWin32StructsAreModuleScoped`
enforces the blanket rule by parsing each helper's source. That check runs on the
POSIX fleet too, where the Windows branches never execute.

## The RSS-recycle ceiling measures real trees on Windows

`session.watchdog_rss_max_mb` (opt-in, `0`/disabled by default) recycles a
non-busy session whose process tree exceeds the ceiling. Its measurement is
`/proc`-based, so `get_session_rss_mb` measured every tree as 0 MiB on Windows:
the ceiling an operator had configured could never be reached and no session was
ever recycled — a silent no-op rather than a visible failure. It now delegates
there to `platform_compat.proc_rss_tree_mb_for_pid`.

That helper, **not** a Toolhelp parent->child walk, is the only safe route.
`th32ParentProcessID` is never cleared when a parent exits and Windows recycles
PIDs aggressively, so a raw walk can attach an unrelated subtree to a recycled
PID — which for this watchdog means recycling a *healthy* session. The helper
validates every parent->child edge against exact creation/exit times across two
snapshots, and treats an unreadable tree as `None` → 0 MiB so the ceiling never
fires on a guess. The cost is one enumeration per candidate instead of the single
shared `/proc` scan the POSIX sweep does per tick; `_build_child_map` therefore
deliberately has no Windows branch. macOS still has no ctypes-only per-pid RSS
path and keeps returning 0.

## Directory links on Windows

`os.symlink` needs `SeCreateSymbolicLinkPrivilege`, which an ordinary
(non-elevated, non-Developer-Mode) Windows account does NOT hold, so it raises
`OSError [WinError 1314]`. Every feature that links a *directory* into place
therefore routes through `platform_compat.symlink_or_junction`, which falls
back to a directory **junction** — a reparse point that needs no privilege and
is followed transparently by reads and by `resolve()`/`realpath` (so
containment/escape checks still hold). Affected paths: app skill registration
(`apps/bridges.py`), boot-time skill reconcile, and the dev-mode frontend dist
link (`frontend.ensure_dev_dist_symlink`). Because a junction is not reported by
`os.path.islink`/`Path.is_symlink`, code that must *detect or remove* such a
link uses `platform_compat.is_link_or_junction` / `unlink_link_or_junction` —
notably the md-notebook `.trash` guard, whose refusal would otherwise be
POSIX-only and let a Windows junction redirect a trashed note out of the vault.
A *file* symlink has no junction equivalent, so the few tests that plant one
stay Windows-skipped in `test/windows-expected-failures.txt`.

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
- **`kirocrew stop` reports "No Kiro Crew gateway currently running" on a
  non-English Windows** — `find_listening_pids` matches the `netstat` state
  against the wildcard foreign address and the literal English `LISTENING`;
  some localized Windows editions emit translated state names. Workaround:
  `netstat -ano | findstr :5476` to find the PID and `taskkill /F /PID <pid>`.
- **Web terminal / interactive SSO login panels** — unavailable on Windows
  (they need `pty`/`fork`/`termios`); they return a clear "not supported on
  Windows" response instead of crashing.

## Related

- [README](../../README.md) — quick-start Platforms note
- [AGENTS.md](../../AGENTS.md) — the cross-platform shim table
- `src/kiro_crew/platform_compat.py` — the cross-platform shim
