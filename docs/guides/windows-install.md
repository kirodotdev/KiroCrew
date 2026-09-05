# Installing & Testing Kiro Crew on Windows

Kiro Crew runs **natively on Windows** as a Python **source install**.
The cross-platform process / signal / file-lock / metrics behavior is routed
through `kiro_crew.platform_compat`, so macOS + Linux behavior is unchanged and
the same code path also runs on Windows.

## Desktop installer

CI's Windows lane (`build-windows.yml`) builds a Windows desktop app: an NSIS
`KiroCrew Setup <version>.exe` with the backend bundled (no separate Python
install needed). It has its own workflow rather than being a leg of
`build-desktop.yml` because Authenticode signing has to happen *during* the
build — the installer compresses its own already-signed executable — so that job
needs AWS credentials the shared build workflow deliberately does not hold.
Current status:

- **Published on every channel — nightly, insider and stable.** The `latest/`
  alias is the human download:
  `https://download.crew.kiro.dev/desktop/<channel>/latest/KiroCrew-Setup.exe`.
  A stable release republishes the already-signed installer it verified at
  insider time rather than rebuilding it. If a Windows build fails, that release
  simply ships without an installer instead of holding up the other platforms.
- **Authenticode-signed** — signing runs during the build through AWS Signer and
  the publish lane refuses to publish bytes whose certificate table is empty,
  whose signer is not the pinned publisher, or which carry no RFC3161
  countersignature (`scripts/verify_windows_installer.py`). Signing removes the
  unknown-publisher prompt but not SmartScreen's first-download interstitial:
  reputation accrues per file hash and per certificate over download volume, and
  a nightly produces a new hash daily.
- **Auto-update is live on the channels that publish** — win32 is in
  `SUPPORTED_PLATFORMS` and driven by `NsisUpdater`, which reads `latest.yml`
  from the same per-channel feed directory the other platforms use. It verifies
  the downloaded installer's Authenticode signature **fail-closed** against
  `win.signtoolOptions.publisherName`, so a mis-signed publish would break every
  client's update at once rather than degrade quietly — which is why the publish
  lane verifies the signature before the bytes become immutable.
- **Assisted installer, per user by default** — `nsis.oneClick` is false and
  `perMachine` is false, so the installer offers an install-mode page whose
  default is a per-user install into a directory named from the product name,
  with no UAC prompt. Choosing "for all users" on that page opts into an
  elevated install under Program Files instead. The restored native flow does
  not expose the former custom destination, desktop-shortcut, or start-with-
  Windows controls. The per-user default is what
  keeps a nightly install (`KiroCrew Nightly`) side by side with a stable one
  rather than replacing it; nightly additionally pins its own `nsis.guid` so
  the two channels do not share an uninstall registry key, and its own
  `win.appId` so they do not share a shortcut **AppUserModelID**. That last one
  is Windows-only on purpose: the shared `appId` is required on macOS, where
  Squirrel.Mac validates an update against the host's designated requirement,
  but on Windows it reaches `${APP_ID}`, which the NSIS uninstaller passes to
  `WinShell::UninstAppUserModelId`. Shared, uninstalling one channel
  deregisters the identity the *other* channel's desktop and Start Menu
  shortcuts still carry, and the shell then reports that app as relocated or
  missing even though its `.exe` is untouched. Either mode leaves
  the Kiro Crew home alone (`deleteAppDataOnUninstall` stays false, and
  `~/.kiro/crew` is outside the install directory).
- **Auto-updates stay visible without becoming interactive** — after the app
  stops its local gateway and closes, the update path skips Welcome, install
  scope, and Finish, but leaves the native NSIS extraction page on screen with
  real progress. At 100% it reopens Kiro Crew and closes automatically. A
  persistent message on that progress page warns that the handoff can take
  several minutes; a Windows notification tells the user to reopen Kiro Crew
  from the Start menu if the relaunch does not happen. New clients call
  `quitAndInstall(false, true)` so NSIS is visible;
  the installer also converts a legacy `/S --updated` launch back to this same
  visible path, which covers the first upgrade from clients that predate the
  change.
- **Guided Kiro Crew artwork** — the welcome and finish pages use the existing
  Kiro Crew logo and ghost family in the native NSIS sidebar, and intermediate
  pages retain a compact branded header. Buttons, progress, install-mode copy,
  keyboard behavior, and localization remain the standard Windows experience.
  Native page boundaries use a short Win32 alpha-blended cross-fade and honor
  Windows' client-area animation setting. The fade contains no timer-driven
  bitmap swap or UI-thread sleep, so extraction keeps the native progress path;
  CI performs a real silent first install, records its duration, and fails if
  it exceeds 2 minutes. The auto-update path remains visible.
- **Single-pass payload publication** — the differential-aware updater still
  verifies and fully extracts its 7z payload into a staging directory before it
  changes the installation. On the normal same-volume per-user Windows layout,
  the installer then renames Electron's large `resources` and `locales`
  directories into place instead of asking Defender and the filesystem to
  process thousands of Python files in a second copy pass. Per-machine installs
  retain electron-builder's original `CopyFiles` path so the payload inherits
  the Program Files ACL. A cross-volume temporary directory, occupied
  destination, or failed rename also falls back to that copy path and its
  bounded retry prompts. The build-time patch is
  pinned to the installed app-builder-lib version and fails closed if its NSIS
  template changes, so an upgrade cannot silently remove that fallback.
- **Uninstall removes the app and its caches, and keeps your data.** Removed:
  the install directory, the Start Menu shortcut, the uninstall registry key,
  and any “start with Windows” Run entry left by an earlier custom installer,
  and — via the `customUnInstall` macro in `website/electron/build/installer.nsh`
  — this channel's electron-updater cache under
  `%LOCALAPPDATA%\<package-name>-updater`, which holds a full installer payload
  (~200MB) that nothing else would ever reclaim. Two things scope that removal.
  It is guarded on `isUpdated`, because an auto-update runs the same uninstaller
  and the cache is what the next update diffs against to avoid re-downloading the
  whole installer. And the path is **per channel**: stable resolves
  `kirocrew-desktop-updater`, nightly `kirocrew-desktop-nightly-updater`
  (`build-desktop.sh` overrides `extraMetadata.name`), so uninstalling one
  channel cannot touch the other's pending download or window state. An install
  predating that split leaves a shared `kirocrew-electron-mac-updater` behind,
  which is deliberately NOT removed for the same reason — it may still belong to
  the other channel. **Your settings survive that rename**: the first launch after
  it carries over your update channel, remote hosts, hotkey and window position,
  so an Insider install is not quietly moved to Stable. A preference you have
  already changed is never overwritten.
  **Deliberately kept:** `~/.kiro/crew` — sessions, memory,
  the database and config. Delete it by hand to remove Kiro Crew's data too.
  Also kept, because it belongs to a different product:
  `%LOCALAPPDATA%\Kiro-Cli`.
- **Integrated Windows chrome** — the desktop shell uses the
  dashboard's 42px header as its titlebar. File/Edit/View/Connection/Window/Help
  open the existing native Electron menus from the left of that row, the command
  palette remains centered on the window, and native minimize/maximize/close
  controls remain on the right.
- **Precompiled Windows gateway startup** — packaging traces the real
  `kiro_crew.cli_server` import after pruning and ships checked-hash bytecode for
  that import closure beside its sources. Windows consumes those caches directly,
  avoiding the thousand-file cache-population burst that otherwise overlaps
  Defender's post-install scanning. macOS and Linux still redirect bytecode out
  of the signed/read-only app tree. The loading screen retains its extended
  Windows handoff window as a slow-machine fallback; a child exit or spawn error
  still fails immediately and includes the launch-log cause. CI starts the
  just-installed bundled interpreter against an isolated data home and requires
  `/api/ready` within 30 seconds, so both the packaged caches and the full gateway
  handoff are covered rather than only a synthetic import benchmark.

The source install below remains the fully supported path.

## Prerequisites

| Tool | Why | Get it |
|------|-----|--------|
| **Git for Windows** | clone the repo | https://git-scm.com/download/win |
| **kiro-cli** | the agent backend (ACP); the first dashboard launch can install it | Kiro Crew setup page or kiro-cli's native Windows release |
| **Python 3.12-3.13** | the venv runtime. `python_requires` is `>=3.12` and 3.13 is in the supported range, but **3.12 is the tested Windows runtime** (it is what the Windows CI shard runs, and numpy 1.x ships no 3.13 Windows wheel) | https://python.org (install user-scoped), or `winget install Python.Python.3.12` |
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
.\make.ps1 build
```

If that reports "running scripts is disabled on this system", Windows' default
execution policy (`Restricted`) is blocking it. Either allow your own scripts
once, or bypass the policy for a single run without changing any setting:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned   # persistent, no admin needed
powershell -ExecutionPolicy Bypass -File .\make.ps1 build   # or: one run only
```

`make.ps1` is the Windows counterpart of the Makefile — same target names, same
artifacts (`build`, `frontend`, `backend`, `test`, `wheel`, `backend-bin`,
`desktop`, `clean`). It exists as a separate driver because `make` is not part of
a Windows install and the Makefile's recipes are POSIX-shaped throughout;
`test/test_build_target_parity.py` fails the build if the two target sets
diverge. Unlike `ensure-python.sh` / `ensure-node.sh`, `make.ps1` itself installs
no toolchain (their install paths are `curl … | sh`): it searches — the `py`
launcher first, then `PATH`, skipping the Microsoft Store alias stub — and prints
the `winget` command if nothing usable is found. The two desktop targets are the
exception, because they hand off to `packaging/build-desktop.sh`, which does
provision what it needs: a pinned `uv` and a python-build-standalone interpreter
to embed in the app.

The equivalent by hand, if you would rather not use the driver:

```powershell
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

The per-user Kiro CLI install under `%LOCALAPPDATA%\Kiro-Cli` is discovered
independently of the gateway's inherited `PATH`. Installing it while the desktop
gateway is already running is therefore picked up by the setup page's next
automatic check; neither a gateway restart nor a Windows reboot is required.

`kirocrew` lands in `.venv\Scripts\`. If a launched (non-shell)
gateway can't find the built-in `kirocrew-cron` / `kirocrew-core` MCP servers,
that dir is appended to the MCP spawn `PATH` automatically
(`env.augmented_path`), and the managed-server invocation falls back to
`python -m kiro_crew <sub>` when the `kirocrew.exe` wrapper isn't resolvable.
In the desktop bundle the relocatable `bin\kirocrew.cmd` shim is preferred
over `Scripts\kirocrew.exe` (whose embedded interpreter path names the build
machine) and is unwrapped to `<root>\python.exe -P -s -m kiro_crew <sub>` when
spawned.

## Kiro sandbox delegation and the unsandboxed-exec opt-in

Windows has no Kiro Crew OS sandbox backend. The official Kiro CLI does have its
own sandbox, so Kiro Crew delegates the default chat backend, model list, account
identity and usage reads to it automatically. A fresh desktop install therefore
does **not** need a config edit before the first chat.

This is deliberately not a Windows-wide bypass. Scripts, hooks, third-party ACP
backends and other commands without a proven internal sandbox still fail closed.
To run those paths without an OS sandbox, explicitly opt in at
`%USERPROFILE%\.kiro\crew\config.json`:

```json
{ "agent": { "sandbox_allow_unsandboxed_exec": true } }
```

**`kirocrew setup` offers this for non-Kiro subprocesses.** Because Windows has no OS-level
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

**The model picker and the credit pill follow chat's Kiro delegation.**
`/api/models`, the credit pill's `whoami` identity fetch and its `/usage` scrape
spawn the same `kiro-cli` binary chat does, at the same `agent.sandbox` tier — so
on this platform they succeed and fail together with chat, rather than one working
while the other 503s. Concretely:

- **Default install** (`agent.sandbox` unset → `"auto"`): chat and these fixed
  Kiro reads delegate to the CLI's built-in sandbox. No broad opt-in is needed.
  The parent strips gateway credentials, secret/session variables and inherited
  Python runtime variables before every delegated spawn.
- **`agent.sandbox` explicitly `"off"`** (isolation deferred to kiro-cli's own
  internal sandbox): all of them run, and none of them need the opt-in. Note that
  an explicit `"off"` now logs a one-time `SECURITY` warning where no OS-level
  isolation ends up active.

## Per-feature status on Windows

| Feature | Status on Windows |
|---------|-------------------|
| Core gateway / chat / dashboard | works without `sandbox_allow_unsandboxed_exec` — the official Kiro backend delegates to Kiro CLI's built-in sandbox, while the parent scrubs sensitive environment variables. A source install with a built `website/dist` is linked into `src/kiro_crew/static/dist` at gateway start via a **directory junction** (`platform_compat.symlink_or_junction`), which needs no privilege; a symlink there would need `SeCreateSymbolicLinkPrivilege` and would leave a non-elevated install serving the "not built" page |
| Project skills (`<project>/.kiro/skills`) | not yet — Python on Windows does not expose handle-relative directory traversal that can reject every reparse point before resolving it. Catalog, consent and loading fail closed before canonicalizing the project path, preventing a raced junction to a UNC share from initiating SMB authentication. Global and installed skills continue to work. |
| Theme-pack install, detail, assets, overlays, topbars, and removal | works — opened pack files are contained with `GetFinalPathNameByHandleW`; descriptor resolution fails closed instead of trusting a pathname-only check |
| LLM cron jobs (the `message` kind) | works |
| Script cron jobs | need the `agent.sandbox_allow_unsandboxed_exec` opt-in above — they run through `wrap_argv`, which fail-closes where no OS sandbox backend exists. Without it the job fails with a message naming that setting (it no longer raises an uncaught error) |
| Command cron jobs (`sh -c "…"`) | not supported on Windows — the stored command is vetted under POSIX-sh semantics, and Windows ships no shell whose language matches: cmd.exe is not POSIX at all, and Git-for-Windows's `sh.exe` is bash and performs brace expansion that hides `cat ~/.a{w,w}s/credentials` from the vet. The job fails-closed with an explanation. Use a **script cron** or an LLM `message` cron on this platform |
| Script hooks (Settings → Hooks) | need the `agent.sandbox_allow_unsandboxed_exec` opt-in above (like script crons — the hook command routes through `wrap_argv`, which fail-closes where no OS sandbox backend exists; without it the hook returns that message as its `error`). With the opt-in they run in **cmd.exe** language: a hook `command` runs as `%ComSpec% /c "<command>"`, so read the context env vars as `%KIROCREW_HOOK_EVENT%` / `%KIROCREW_HOOK_CONTEXT%` (not `$VAR`), and group arguments with double quotes only (cmd.exe gives `'…'` no meaning). The line reaches cmd.exe verbatim, so a quoted interpreter path with a space works. A hook authored on macOS/Linux is not portable and must be rewritten |
| Pull-request source drawer provider fetch/check/resolve | not yet — and for a different reason than it used to be. The provider-CLI **trust** check now works here (see Issue Radar below), but the drawer does not share Issue Radar's spawn: it keeps its own async, sandbox-routed one (`source_providers._run_json`), which refuses on Windows because no OS sandbox backend exists. So the blocker is the sandbox, not the binary check |
| Issue Radar | works — its `gh` spawn is not sandbox-routed, so the trust check is the only gate, and that is answered by reading the binary's Windows ACL (`kiro_crew.windows_acl`) in place of the POSIX `st_uid` + write-bit walk, which reports nothing on this platform. Refused when any principal outside `{you, SYSTEM, Administrators, TrustedInstaller}` can replace the binary or a parent directory, when the security descriptor is unreadable, or when the gateway token is **elevated** (an elevated gateway spawns elevated children, which makes the walk vacuous). GitHub only on this platform unless `glab` is installed. **If a `gh` you trust is refused**, the override variables (`KIROCREW_ISSUE_RADAR_GH`, `KIROCREW_GH_BIN`) re-enter the same check rather than bypassing it, so the recourse is to install `gh` somewhere only you and the system can write — a per-user `%LOCALAPPDATA%` install is accepted — or to file an issue quoting the refusal, which names the offending principal or the ACE type it could not evaluate |
| Spec Builder | works, except **Duplicate** — crash-safe copy publication pins a staging directory and uses the platform's atomic no-replace rename (`renameat2(RENAME_NOREPLACE)` on Linux, `renameatx_np(RENAME_EXCL)` on macOS). Windows provides neither that native contract nor CPython's directory-descriptor operations, so the backend reports the capability as unavailable and the dashboard omits Duplicate instead of falling back to a check-then-rename race or a junction-prone path write. Approval, per-task runs, labels, archive/restore, chat, and whole-plan execution work normally |
| Code Review Sage | not yet — the provider-CLI trust check now passes, but its review worker hands the session `python3 sage_lib/…` commands and `python3` is not an interpreter on Windows (the name resolves to the Microsoft Store app-execution alias, or to nothing). It refuses with that reason rather than starting a review that produces no result |
| Browser automation (`playwright-cli`) | works (`npm install -g @playwright/cli@latest`, needs Node.js 20 or newer) |
| Vector memory / embeddings | works — embeddings run **in-process** through the vendored llama-cpp-python (`_vendor/llama_cpp_libs/win_amd64`), which loads the Qwen3-Embedding-0.6B GGUF from `~/.kiro/crew/models`. No remote endpoint, no Docker and no Ollama server is involved on any platform |
| STT (whisper / optional cloud transcription) | works |
| Voice reply (Piper TTS) | not yet — upstream rhasspy/piper ships no Windows binary; Polly (optional) works if the `aws` CLI is present **and** the `agent.sandbox_allow_unsandboxed_exec` opt-in above is set — the `aws polly` spawn routes through `wrap_argv`, which fail-closes where no OS sandbox backend exists. Without it synthesis returns no audio and the log names that setting |
| SSH tunnel (`kirocrew cloud` remote dashboard) | not yet — needs the OpenSSH client on `PATH` and a signal-handling audit |
| MCP server tool listing (dashboard MCP page, `kirocrew doctor`) | **built-in servers work, no opt-in** — `kirocrew-core` / `-cron` / `-computer` are probed for real: their command line is derived entirely inside the package (never user-config text), so the first-party carve-out spawns the handshake probe unconfined (env-scrubbed, SEL-audited as `unconfined`) even with no sandbox backend. When that probe cannot run (a transient sandbox failure, a governance sandbox floor, or a customized command for the server), the listing falls back to reading the package's own tool declaration and logs a WARNING noting that `ok` then means "declared" rather than "handshake succeeded". A **third-party** server has no declaration to read and never gets the carve-out, so its listing needs the `agent.sandbox_allow_unsandboxed_exec` opt-in — its binary is named by config and spawning it is what the sandbox exists to confine. The third-party server itself is unaffected: kiro-cli launches it from the agent config without this probe, so its tools still work in chat |
| MCP gateway (opt-in, OFF by default) | works — a named-pipe transport replaces the AF_UNIX socket, and the peer check uses `GetNamedPipeClientProcessId` + a SID comparison in place of `SO_PEERCRED`. Still opt-in: set `mcp_gateway.enabled` to turn it on |
| Papyrus (LaTeX editor, opt-in builtin) | works, **but compiling and git need the `agent.sandbox_allow_unsandboxed_exec` opt-in above** — unlike official Kiro, these processes have no proven internal sandbox, so `wrap_argv` keeps the no-backend fail-closed policy. Without it, compile and clone/commit/push/pull answer a clear 422 (`compiler_sandbox_unavailable` / `git_sandbox_unavailable`) naming the remedy rather than a bare "internal error". The managed Tectonic compiler is Windows-pinned (`x86_64-pc-windows-msvc`); Windows-on-ARM has no upstream asset and keeps the manual install path |
| Computer use — **reading** (`computer_list_apps`, `computer_get_state`) | works, still behind the operator's one keystone opt-in (Settings → Computer Use). Reads the UI Automation tree of a window and can attach a `PrintWindow` screenshot. Two Windows-specific limits: a **non-elevated gateway cannot see an elevated window** (UIPI, and the secure desktop is unreachable to any application — a security property, not a gap), and a window drawn on a swapchain surface **cannot be captured**, so WindowsTerminal returns a tree with no screenshot rather than a blank image. Walking is also markedly slower than macOS — a large Chromium window costs hundreds of milliseconds at the node budget — so raise `max_tree_nodes` deliberately |
| Computer use — **input** (click, drag, type, key, set value, scroll, action) | works, behind the same keystone opt-in. **Element-addressed actions touch neither your cursor nor your focus** — they go through UI Automation control patterns, so the provider performs them inside the target application; prefer them, and they are what `click_method: "auto"` resolves to. The exceptions are forced by the platform: Windows has no per-process input delivery (no `CGEventPostToPid` analogue), so `type_text` / `press_key` TAKE your keyboard focus (the result says so), and a coordinate click needs `click_method: "global"` named explicitly because it moves your real cursor — `auto` refuses to resolve onto it. Every pointer gesture is confined to the authorized window first, comparing top-level handles rather than pids (one broker process fronts many packaged apps), and a drag confines every point of its path since the release is where a drop lands |

The not-yet items are tracked as Windows feature-parity follow-ups.

## Secret-at-rest posture on Windows

Files under `%USERPROFILE%\.kiro\crew` that hold auth material — the token
signing key, refresh-token state, per-app secrets, snapshot tarballs, and the
cron internal-secret temp file — are locked down to the current user via an
owner-only NTFS DACL (inheritance stripped, `S-1-3-4:F` = Owner Rights full
control). This is applied through `platform_compat.restrict_to_owner`, which
routes to `os.chmod(..., 0o600)` on POSIX and, on Windows, builds the descriptor
in-process through `advapi32` (`SetNamedSecurityInfoW` with
`PROTECTED_DACL_SECURITY_INFORMATION`, the equivalent of
`icacls /inheritance:r /grant:r "*S-1-3-4:F"`). It is a direct API call rather
than a subprocess -- measured 0.24 ms against 313 ms for the equivalent `icacls`
invocation -- so it is safe to call on the gateway's event loop. Failure is
fail-loud (raises `OSError`) so the
security-warning handlers in each caller fire — a naive `if IS_POSIX: os.chmod`
guard would silently no-op on Windows, leaving secrets group/world-readable
under NTFS.

`restrict_to_owner` is file-shaped by design: its grants are deliberately
non-inheritable (inheritance flags mean nothing on a file). A **directory**
holding secrets must instead go through `platform_compat.restrict_dir_to_owner`
— the directory twin — whose grants carry `(OI)(CI)` so files and
subdirectories created inside it inherit the owner-only DACL, and which uses
`0o700` on POSIX (a directory needs the execute bit to be traversable, which
the file helper's `0o600` drops). `make_owner_only_dir` wraps creation
(with parents) plus a best-effort tighten for the common case. Calling the
file helper on a directory tightens only the directory node itself and leaves
every file later created inside on the creating token's default DACL — the
runtime logs a warning when it detects that misuse. Inheritance governs only
what is created from then on: a file that already existed inside keeps its own
DACL and — because Windows grants *Bypass Traverse Checking* to Everyone by
default — stays reachable through the tightened parent, so repairing an
existing install needs a per-file pass, not a parent tighten.

### The memory store: why a per-file pass, not just the directory

`memory.db` (semantic/episodic memories and their embeddings) is the first
caller to need that repair pass, and it names every memory-bearing file rather
than only the `.db`. It runs under `journal_mode=WAL`, so SQLite keeps
`memory.db-wal` and `memory.db-shm` beside it, and a *committed* row lives in
the `-wal` until a checkpoint moves it — locking the `.db` alone would leave
committed memories readable under whatever DACL a pre-lockdown sidecar carries.
The pass covers the `.db`, its `-wal`/`-shm` sidecars, and `memory.faiss` /
`memory.ids.json` (the embedding index and its id map).

`VectorMemoryStore.init()` calls `make_owner_only_dir` on the parent first, so
everything SQLite and FAISS create from then on inherits owner-only access on
both platforms. The per-file pass is for what already exists — a restored
backup, a home migration, a manual edit, or simply an install predating this
lockdown. It therefore runs on **every** init rather than only when init created
the files: gating on creation would leave every pre-existing install permanently
readable, which is most of them.

It runs **twice**, once before `sqlite3.connect` and once after. The first call
is what stops the schema migrations running against a file another local user
can still write; the second covers whatever SQLite has just created.

The Windows cost is up to 11 `icacls` spawns per init — one for the directory
plus one per file on each of the two passes, and a file that does not exist
still spawns (icacls exits non-zero and the caller warns). That is more than it
sounds and still cheap in context: once per workspace per process, beside the
`sqlite3.connect`, the migrations and the FAISS index load already in that
function — and `context.get_memory_for` caches the store and is reached from a
worker thread, not the gateway event loop.

It is fail-soft (warn, keep going), which is the contract `restrict_to_owner`
documents for its callers: memory being unavailable is a supported degraded
state, so a read-only filesystem must not take init down.

> **Scope note.** With the default `db_path`, "the directory" *is* the data home
> (`config_dir()`), so a memory init tightens the whole home to owner-only. That
> direction is right — the home also holds the security policy, sessions and
> lessons, all private on the same boundary — but it is wider than memory and it
> is the only place in the tree that does it today. `memory.py`'s FTS index
> (`memory_index.db`) and its sidecars carry the same secrets and are **not** yet
> covered by the per-file pass.

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

## `os.kill(pid, 0)` is a process killer here, not a liveness probe

On POSIX, `os.kill(pid, 0)` is the idiomatic "does this pid exist?" test: signal 0
runs the permission and existence check without delivering anything. On Windows
CPython maps `os.kill(pid, sig)` onto `TerminateProcess(handle, sig)` for every
signal except `CTRL_C_EVENT` and `CTRL_BREAK_EVENT`, so the same expression
**terminates the process it is asking about** and then reports it alive. This is
not a portability wart that degrades to "unavailable" — it is a silent process
killer, and because pids are recycled the damage lands on whatever happens to own
that number now.

Route liveness through `platform_compat.pid_exists`, or `pid_liveness` when the
caller has to tell "gone" apart from "alive but not signallable by us". Both
preserve the POSIX semantics callers depend on — notably that EPERM means the
process EXISTS and must never be conflated with `ProcessLookupError` — and on
Windows they ask `OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)` instead of
signalling anything.

`test/test_windows_kill_probe_audit.py` enforces this as a tripwire rather than a
convention: it walks the AST of every module under `src/kiro_crew` and fails on a
raw signal-0 probe until the author either routes it through the shim or records
the site in `GATED_PROBES` with a justification for why it can never execute on
Windows. A second test rejects allowlist entries whose code has since moved or
gone, so an exemption cannot outlive what it covered.

The scope is deliberately only the signal-0 *probe* form. The tree contains many
raw POSIX call sites — `fcntl`, `resource`, `os.killpg`, `pty`, `termios` — and
nearly all are legitimately POSIX-gated implementation detail, so auditing them
here would bury the signal in noise; those are governed by the shim table in
`AGENTS.md` and by review. What makes signal-0 worth its own gate is that getting
it wrong is destructive rather than merely unavailable, and that the added-line CI
check cannot see a probe which arrives by a file move or a rebase. This test reads
the whole tree on every run.

## The agent tree gets a Job object ceiling, not a cgroup scope

On Linux, `sandbox.cgroup_scope_argv` bounds an agent subprocess and every
descendant it spawns as one cgroup: `TasksMax` is the fork-bomb ceiling and
`MemoryMax` the RSS-balloon ceiling. There is no systemd on Windows, so that
wrapper returns argv unchanged and logs a one-time loud
`SECURITY: cgroup v2 scope enforcement unavailable (not Linux)` — which meant the
agent and every MCP server it spawned ran with **no fork-bomb and no memory
ceiling at all**, a warning at boot rather than an enforced limit.

A **Job object** is the native equivalent: limits apply to every process in the
job, and a member's descendants join automatically.
`platform_compat.apply_job_limits` sets `ActiveProcessLimit` against the same
budget as `TasksMax` and `JobMemoryLimit` against `MemoryMax`, and
`sandbox.apply_windows_resource_ceiling` reads the **same** `resource_limits`
config as the cgroup path, so one operator setting governs both platforms. The
memory limits are equivalent; the process limits are not one-for-one, because
`TasksMax` counts every thread while `ActiveProcessLimit` counts processes — the
same number is therefore a looser bound here, though it still bounds a fork
bomb. The memory default is derived from `GlobalMemoryStatusEx` rather than the
POSIX `os.sysconf` probe, so it scales with the host on Windows too instead of
collapsing to a flat cap that a small machine could exceed. Enforcement is by denial, matching the cgroup
tier in practice: past the process limit the member's `CreateProcess` fails with
`ERROR_NOT_ENOUGH_QUOTA` (1816), and an allocation past the memory limit fails.

Two details are load-bearing rather than incidental:

- **The child is created suspended.** A Job object cannot be an argv prefix, so
  unlike the cgroup wrapper it has to be attached to a live pid — and job
  membership covers a member's *future* descendants only. Attaching to an
  already-running child would leave a window in which it could spawn descendants
  that escape the ceiling. Both ACP spawn sites and the source-monitor provider
  CLI transport therefore pass
  `creationflags |= CREATE_SUSPENDED`, apply the job, then call
  `platform_compat.resume_process_main_thread`. A process created suspended has
  executed no instructions, so it provably has no descendants: the window is
  closed by construction rather than merely made small. kernel32 has no
  `ResumeProcess`, so the resume enumerates the Toolhelp thread snapshot and
  resumes every thread owned by that pid.
- **`KILL_ON_JOB_CLOSE` is deliberately not set.** It would terminate the agent
  tree as soon as the last job handle closed, turning a resource ceiling into a
  process-lifecycle change (a gateway exit would kill running agents). Leaving it
  off also means the handle need not be held: a job stays alive while processes
  are assigned to it, so the limits persist after `CloseHandle` and there is no
  handle registry or teardown to get wrong.

Failure modes are asymmetric on purpose. The **ceiling** fails soft — any Win32
error logs a SECURITY warning and returns `False`, because a missing ceiling must
not break the gateway. The **resume** is fatal when the pid still exists: a
process that is alive but frozen would masquerade as a running agent and hang the
session on the ACP handshake with no diagnosis, so it is killed and the spawn
fails loudly. If the pid is already gone there is nothing frozen, and the
handshake reports the real error instead.

`CREATE_SUSPENDED` is 0 on POSIX and both helpers return `False` there, so the
POSIX path is a plain unsuspended spawn with the cgroup scope doing the work.

## Win32 struct layouts live at module scope

Every `ctypes.Structure` subclass the Win32 helpers need is declared **once at
module scope** — `_ProcessEntry32`, `_ProcessMemoryCounters`, `_MemoryStatusEx`,
`_SidAndAttributes`, `_TokenUser`, and the Job object layouts `_IoCounters`,
`_JobObjectBasicLimitInformation`, `_JobObjectExtendedLimitInformation` and
`_ThreadEntry32` in `platform_compat`, plus
`_SecurityAttributes` in `mcp_gateway/transport.py` and `_VMStatistics64` in
`subagent.py`. Declaring one inside the function that uses it is a **memory
leak**, not a style question: `ctypes.POINTER(T)` memoises `T -> POINTER(T)` in a
module-level dict inside ctypes that is never evicted, so a locally-declared
Structure pins a brand-new pair of type objects on every call. The affected
helpers are all polled — the dashboard's system-metrics endpoint, the RSS-recycle
watchdog, the process-tree walk behind `kill_process_tree`, the MCP pipe's
per-connection peer check, and the per-spawn Job object ceiling — so the gateway
grew unboundedly on Windows alone (measured at ~8 KiB per `proc_rss_bytes` call,
~15 MiB per 2,000 calls, never reclaimed). POSIX is unaffected because those
branches read `/proc`, `sysctl` or `resource` instead of calling Win32.

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

- **Desktop gateway recovery refuses to force-stop the port** - the Electron
  launcher uses `netstat -ano` to identify the listener, PowerShell
  (`Get-CimInstance`) with a WMIC fallback to read its command line, and
  `taskkill /F /PID` only after confirming a Kiro Crew executable or
  `python -m kiro_crew` process. Localized listener-state text is ignored.
  SSH forwards and unrelated processes are never terminated, and a failed or
  timed-out `netstat` probe is treated as unknown rather than as a free port.
- **`ModuleNotFoundError: No module named 'fcntl'`** — you installed a
  branch/commit that predates the Windows port. `fcntl` is a Unix-only Python
  stdlib module; it cannot be pip-installed on Windows. Update to a build that
  routes locking through `platform_compat`.
- **`ZoneInfoNotFoundError` / "No time zone found"** — install `tzdata`
  (`pip install tzdata`); Windows has no system IANA tz database.
- **"Python was not found" (Microsoft Store)** — a bare `python`/`python3` was
  resolving the Store alias stub; install a real CPython and ensure it precedes
  the stub on `PATH`.
- **`kirocrew stop` reports "No Kiro Crew gateway currently running"** — a
  localized Windows edition is *not* the cause: `find_listening_pids` identifies
  a listening row by its wildcard foreign address (`0.0.0.0:0` / `[::]:0`), which
  no edition translates, and treats the English `LISTENING` literal only as a
  defensive second signal. If it still finds nothing while the dashboard answers,
  locate the PID by hand with `netstat -ano | findstr :5476` and stop it with
  `taskkill /F /PID <pid>`.
- **Web terminal / interactive SSO login panels** — unavailable on Windows
  (they need `pty`/`fork`/`termios`); they return a clear "not supported on
  Windows" response instead of crashing.

## Related

- [README](../../README.md) — quick-start Platforms note
- [install](install.md) — the build-target table shared with macOS and Linux
- [AGENTS.md](../../AGENTS.md) — the cross-platform shim table
- `src/kiro_crew/platform_compat.py` — the cross-platform shim
- `make.ps1` — the Windows build driver
