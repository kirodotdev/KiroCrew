# Windows Changes

This file records the changes made so KiroCrew runs natively on Windows,
including the Electron desktop app, and explains why each was needed. Every
change is scoped to Windows or preserves the existing macOS and Linux behavior.

## Environment used

* Windows with CPython 3.13, Node 24, npm 11
* `kiro-cli` already installed and logged in
* A repo local virtual environment at `.venv`
* The React dashboard built with `npm run build` and staged into
  `src/kiro_crew/static/dist`

## Runtime enablement (user config, not code)

* Set `agent.sandbox_allow_unsandboxed_exec` to true in
  `~/.kirocrew/config.json`. On Windows there is no OS level sandbox backend
  (Linux user namespaces and macOS `sandbox-exec` only), so `sandbox.wrap_argv`
  fails closed and refuses to spawn `kiro-cli`. This is the documented opt in,
  so chat, model listing, and cron turns work. Without it the dashboard loads
  but every agent turn errors with "Sandbox backend unavailable".

## Backend changes (`src/kiro_crew`)

* `conpty.py` (new). A Windows pseudo console backend for the dashboard web
  terminal. It wraps `pywinpty`, the maintained ConPTY binding, and exposes the
  same small surface the terminal handler needs (`read` returning bytes,
  `write` taking bytes, `resize`, `isalive`, `terminate`, `pid`). Why. The web
  terminal used POSIX `pty` and `fork`, which do not exist on Windows, so the
  panel returned "not supported on Windows". A hand rolled ctypes ConPTY was
  attempted first but the child never attached to the pseudo console (it printed
  to the parent console and exited), so the maintained binding was used instead.
  This module is imported only inside the Windows branch of the terminal
  handler, so macOS and Linux never load it.

* `platform_compat.py`. Added `system_memory` and `system_cpu_percent`. The
  first reads total and available physical memory with `GlobalMemoryStatusEx`.
  The second computes a system wide CPU percentage from a `GetSystemTimes`
  delta. Both return `None` when the host is not Windows, so POSIX callers fall
  through to their existing code. Why. The dashboard header read CPU from
  `/proc/stat` or `ps` and memory from `/proc/meminfo`, none of which exist on
  Windows, so CPU showed 0 and memory showed blank.

* `dashboard/handlers_system.py`. Added Windows branches for live memory, static
  total memory, and CPU that call the two new `platform_compat` helpers. The
  macOS and Linux branches are unchanged. A last known Windows CPU value is kept
  so the first sample (which has no delta yet) does not flash to zero.

* `dashboard/handlers/terminal.py`. Wired the ConPTY backend into the terminal
  websocket. On Windows it spawns PowerShell through `conpty.WindowsPty` and
  routes read, write, and resize through it. Session teardown reaps the whole
  process tree with `taskkill /T` through `platform_compat` so a background
  child started in the terminal cannot outlive the closed tab. Added backend
  agnostic helpers `_sess_alive` and `_sess_pid` that return the same values the
  old direct process attribute access returned on POSIX. Every Windows path is
  behind a `winpty` or `IS_WINDOWS` check, so POSIX keeps the original `pty`
  path.

* `apps/registry.py`. The app install script timeout now reaps through
  `platform_compat.kill_process_tree_async` instead of a raw `os.killpg`. Why.
  `os.killpg` does not exist on Windows and raised `AttributeError`, which the
  `except OSError` guard did not catch, so the process tree leaked and the
  fallback kill never ran. On POSIX the shim performs the same group kill under
  the hood, matching how the rest of the codebase already reaps trees.

* `apps/backend.py`. `_pid_alive` now delegates to `platform_compat.pid_exists`.
  Why. A raw `os.kill(pid, 0)` does not probe liveness on Windows (signal 0 is
  the console `CTRL_C_EVENT` there), so it misreported liveness in the app
  backend orphan reaper. The shim uses `OpenProcess` on Windows and the
  identical `os.kill(pid, 0)` with EPERM treated as alive on POSIX, so POSIX
  behavior is unchanged.

* `cron_script.py`. Cancelling or timing out a script or command cron now reaps
  the tree with `platform_compat.kill_process_tree` on Windows. Why. The cancel
  and escalation paths called `os.getpgid` and `os.killpg`, which do not exist
  on Windows and raised `AttributeError`. `_resolve_safe_pgid` now returns
  `None` on Windows and the POSIX group kill logic is left exactly as it was.

* `mcp_discovery.py`. Failed MCP probe cleanup now reaps the probe tree with
  `taskkill /T` on Windows. Why. The existing reap ran only on POSIX (an
  `os.killpg` on the probe group), so on Windows the launcher grandchildren
  (`npx` and `node` shims that start the real MCP server) leaked one tree per
  failed probe per discovery cycle.

* `dashboard/port_reclaim.py`. Stale gateway port reclaim now works on Windows.
  The listener lookup routes through `platform_compat.find_listening_pids`,
  which parses `netstat -ano` on Windows and uses `lsof` on POSIX, and returns
  a None result (fall back to wait and retry) when the lookup tool is absent.
  The gateway identity check already resolved the full command line cross
  platform through WMI, so it needed no change. The terminator now uses
  `platform_compat.kill_process_tree` (which is `taskkill /T` on Windows and
  reaps the gateway kiro-cli and MCP children too) while the POSIX branch keeps
  the original `os.kill` behavior with the portable `platform_compat.SIGTERM`
  and `platform_compat.SIGKILL` constants (identical to `signal.SIGTERM` and
  `signal.SIGKILL` on POSIX). Why. It previously identified the holder only with
  `lsof`, which is absent on Windows, so it returned an unavailable result and
  never reclaimed the port. Now an unclean previous gateway that still holds the
  dashboard port is terminated so the next bind rebinds cleanly. The POSIX path
  is unchanged.

## Frontend and Electron changes (`website/electron`)

* `find-bin.js`. Added Windows candidates that look for `kirocrew.exe` under
  `Scripts` (the repo `.venv`, the one line installer venv, and bundled
  layouts), guarded by an injectable `isWindows` parameter, and a Windows aware
  fallback of `kirocrew.exe`. Why. Node `spawn` does no `PATHEXT` resolution for
  a bare name on Windows, so the app could not find the backend and fell back to
  a name it could not launch. POSIX keeps its original candidate list and bare
  `kirocrew` fallback.

* `main.js`. All of the following are guarded by `IS_WIN` so macOS and Linux are
  untouched.
  * Native Windows title bar with real minimize, maximize, and close buttons.
    An earlier Window Controls Overlay approach was dropped because the caption
    buttons floated over the dashboard header content.
  * Auto hide of the application menu bar so it does not add a permanent second
    strip. Tap Alt to reveal it. All keyboard shortcuts still work.
  * The window and taskbar icon is set from `icon.png` so an unpackaged run does
    not show the default Electron icon.
  * An `AppUserModelId` of `com.amazon.kiro.crew` so the taskbar groups and pins
    the app as KiroCrew rather than the generic Electron host.
  * `KIROCREW_PROJECT_DIR` resolves to the repo root (where `agents` and
    `skills` live) on Windows, because the source layout places them two levels
    up from the electron folder. macOS and Linux keep the original one level up
    path.
  * The window title drops the `[:5476]` port suffix for the primary local
    window on Windows. macOS and Linux keep the original suffix.
  * The injected drag region is skipped on Windows since the native title bar
    already moves the window.

* `test/find-bin.test.js`. Made the fallback test platform deterministic by
  passing an explicit `isWindows` value and added Windows candidate and fallback
  cases. All 21 tests pass.

## Backend tests

* `test/test_handlers_system_cpu_pct.py`. Made the `ps` fallback test platform
  deterministic by pinning the platform to Linux, and added Windows CPU tests
  that stub `platform_compat.system_cpu_percent`. These run the same on every
  host.

* `test/test_port_reclaim.py`. Updated the three terminate tests to patch both
  delivery primitives (`os.kill` for POSIX and `platform_compat.kill_process_tree`
  for Windows) and to assert against the portable `platform_compat` signal
  constants, so they validate both platforms. Added two tests for the Windows
  netstat listener lookup. All 17 pass.

* `test/test_terminal_handler.py`. Made the terminal tests cross platform. The
  two unit tests and one integration test that asserted the old "not supported
  on Windows" refusal now exercise the ConPTY path instead (forcing IS_WINDOWS
  and mocking `WindowsPty`, so the exercised path is identical on Windows and
  POSIX and needs no pywinpty on the POSIX CI). The spawn and reconnect
  integration tests read liveness and pid through the backend agnostic
  `_sess_alive` and `_sess_pid` helpers so they pass for both the pty and ConPTY
  backends. The genuinely POSIX only tests (foreground title detection via
  `os.tcgetpgrp`, POSIX fd and proc teardown, and Ctrl+C via PTY SIGINT) are
  skipped on Windows and still run in full on macOS and Linux. The expanduser
  test now sets `USERPROFILE` as well as `HOME` so it passes on both. Result on
  Windows is 80 passed, 13 skipped, 0 failed.

## Packaging

* `setup.cfg`. Added `pywinpty` under a `platform_system == "Windows"` marker,
  next to `tzdata`. It is never installed or imported on macOS or Linux, which
  keep the stdlib `pty` and `fork` terminal path.

## Desktop launcher

* Created a Desktop shortcut `KiroCrew.lnk` that starts the Electron app, which
  in turn starts its own gateway. It targets the Electron executable with the
  app folder as its argument and uses a generated `website/electron/KiroCrew.ico`
  icon.

## Cross platform safety

* Backend Windows code is guarded by `platform_compat.IS_WINDOWS` or
  `sys.platform == "win32"`, or returns `None` off Windows so POSIX callers use
  their existing code.
* Electron Windows code is guarded by `IS_WIN` or the `isWindows` parameter.
* The one shared change in `apps/registry.py` routes through the same
  `platform_compat` tree kill the rest of the codebase already uses, so the
  POSIX result is the same group kill as before.
* Verification done on Windows. `flake8` clean on all changed Python files, the
  changed modules import cleanly, targeted metric and platform tests pass (86
  passed), the Electron `find-bin` suite passes (21 of 21), and the live
  dashboard endpoints returned real data (`/api/models` and `/api/system`).

## Known limitations on Windows

These remain unsupported or degrade gracefully and were not changed.

* Pull request source drawer provider fetch and resolve. The provider CLIs rely
  on the POSIX OS level sandbox and fail closed with a clear unsupported
  response.
* Voice reply through Piper. Upstream ships no Windows binary. Cloud voice works
  when the `aws` CLI is present.
* The SSH tunnel for the remote dashboard. It needs an OpenSSH client on the
  path and a signal handling audit.
* The optional MCP gateway, which is off by default. It uses an `AF_UNIX` socket
  and a peer credential check that are POSIX only.
