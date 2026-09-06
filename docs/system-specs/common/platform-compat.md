# Cross-platform: route POSIX calls through `platform_compat`

Kiro Crew runs on macOS, Linux (x86_64 and ARM), and Windows (native). `fcntl`,
`termios`, `resource` and `pty` do not exist on Windows, and
**`os.kill(pid, 0)` TERMINATES the target there**: it is not a liveness probe.

`kiro_crew.platform_compat` owns one helper per POSIX call the codebase needs. Reach
for the helper, not the stdlib call, even in code you believe only runs on POSIX — the
import alone is enough to break a Windows install, and the failure lands at import time
in a module a Windows user cannot avoid.

This is the contract. The Windows install and runtime story a user follows is
[windows-install.md](../../guides/windows-install.md).

## Why a table rather than a rule

Half of these are not "the POSIX call is missing on Windows". They are cases where the
stdlib call **exists and answers wrongly**: it silently no-ops, it returns a
high-water mark where a live reading was wanted, its unit differs per platform, or it
follows a link planted at the name. A rule of the form "guard it with `IS_POSIX`"
produces exactly those silent failures, which is why the helper is named per call.

## The helper for each call

| Need | Use (`platform_compat`) | NOT |
|------|--------------------------|-----|
| File lock | `file_lock(fd, exclusive=)` / `acquire_lock`+`release_lock` / `try_acquire_lock` | `fcntl.flock` |
| Liveness probe | `pid_exists(pid)` / `pid_liveness(pid)` | `os.kill(pid, 0)` (kills on Windows!) |
| Kill a process | `kill_pid(pid, sig)` | `os.kill(pid, sig)` |
| Kill a tree | `kill_process_tree(pid, sig)` | `os.killpg(os.getpgid(pid), sig)` |
| Parent PID | `get_ppid(pid)` | `/proc` read / libproc |
| Match process cmdline | `process_matches(pid, needles)` | `/proc/<pid>/cmdline` / `ps` |
| Process start time (PID-reuse guard) | `process_start_time(pid)` | `/proc/<pid>/stat` / `ps -o lstart=` (both answer `None` on Windows, so the guard silently never confirms) |
| Signals | `platform_compat.SIGKILL` / `SIGTERM` | `signal.SIGKILL` (undefined on Windows) |
| Spawn isolation | `start_new_session=IS_POSIX` + `creationflags=CREATE_NEW_PROCESS_GROUP` | bare `start_new_session=True` |
| Re-exec the current Python module | `reexec_python_module(module, args)` | `os.execv(sys.executable, [sys.executable, ...])` (breaks when the Windows interpreter path contains spaces) |
| Race-free Job object assignment | `creationflags \|= CREATE_SUSPENDED`, then `apply_job_limits`, then `resume_process_main_thread` | assigning a job to an already-running child (descendants it already spawned escape) |
| Fork-bomb / memory ceiling on a spawned tree | `sandbox.apply_windows_resource_ceiling(pid)` after the spawn, alongside `cgroup_scope_argv` | `cgroup_scope_argv` alone (a no-op on Windows, so no ceiling at all) |
| File mode | `chmod_safe(path, mode)` / `fchmod_safe(fd, mode)` | `os.chmod` / `os.fchmod` (no `os.fchmod` on Windows) |
| Owner-only secret (fail-loud) | `restrict_to_owner(path)` | `os.chmod(path, 0o600)` under `if IS_POSIX` (silent no-op leaves secrets world-readable) |
| Owner-only secret directory (fail-loud, inheritable) | `restrict_dir_to_owner(path)`; `make_owner_only_dir(path)` to also create it (its tighten step is best-effort) | `restrict_to_owner(path)` on a directory (its Windows grants carry no `(OI)(CI)`, so files created inside land on the default DACL, not owner-only; its `0o600` also drops the execute bit a directory needs) |
| Directory link | `symlink_or_junction(target, link)` | `os.symlink` (`WinError 1314` without elevation) |
| Detect/remove a dir link | `is_link_or_junction(path)` / `unlink_link_or_junction(path)` | `path.is_symlink()` (misses a Windows junction) |
| Hold a directory in place while a child writes into it by path | `pin_directory(path)` (then `os.close`) | `os.open(dir, O_RDONLY)` (EACCES on Windows, and even where it opens it follows a link planted at the name) |
| Process RSS (live) / peak RSS / CPU | `proc_rss_bytes()` / `proc_peak_rss_bytes()` / `proc_cpu_seconds()` | `resource.getrusage` (`ru_maxrss` is a high-water mark, never a live reading, and its unit is KiB on Linux but bytes on macOS) |
| Available host memory | `host_available_mib()` (0 = unknown, never 0 = no memory) | `/proc/meminfo` directly (Linux-only, so the bound built on it silently vanishes on macOS and Windows) |
| FD soft limit | `raise_nofile_soft_limit(n)` | `resource.setrlimit` |
| Port to PID | `find_listening_pids(port)` / `listening_pid_tool_available()`; `find_port_listeners(port)` when ownership must be scoped to the local address actually probed | `lsof` directly |
| Spawn a system tool (`ps`, `lsof`, `netstat`, `taskkill`) | `trusted_system_bin(name)`, treating `None` as "unavailable" | a bare argv name (resolved through a `PATH` that can lead with same-uid-writable dirs) |
| strftime no-pad | `strftime(dt, "%-I")` | bare `dt.strftime("%-I")` (`ValueError` on Windows) |

## Verifying a change

Verify process, signal, file-lock and metrics changes on macOS **and** Linux; the
Windows shards in CI cover the third. A test that only ever runs on the author's
platform is how a silent no-op ships.

Frontend support is Chrome, Firefox, Safari and Edge, using standard Web APIs and
guarding the rest (`typeof Notification !== 'undefined'`).
