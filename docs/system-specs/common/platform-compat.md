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
| Tail a rotating log | `open_log_file_for_tail(path)` returns a binary read descriptor (caller closes); Windows permits read/write/delete sharing so the writer can rename during a read. Only for log readers, never security pinning. | plain `open` held while a Windows writer rolls over |
| File lock | `file_lock(fd, exclusive=)` / `acquire_lock`+`release_lock` / `try_acquire_lock` | `fcntl.flock` |
| Liveness probe | `pid_exists(pid)` / `pid_liveness(pid)` | `os.kill(pid, 0)` (kills on Windows!) |
| Kill a process | `kill_pid(pid, sig)` | `os.kill(pid, sig)` |
| Kill a tree | `kill_process_tree(pid, sig)` | `os.killpg(os.getpgid(pid), sig)` |
| Kill a tree whose leader may already be REAPED | `capture_spawned_process_group(pid)` at spawn (its group read sits between two matching identity reads), `record_process_group_members(group, descendants)` as children appear and again during teardown, always WHILE THE LEADER IS ALIVE (it re-proves the leader's incarnation both before and after its own scan, and records nothing otherwise) and always off the event loop, then `kill_pgroup(g.pgid, sig)` guarded by `pgroup_matches_incarnation(g)` before EVERY signal | `kill_process_tree(pid, sig)` (its `os.getpgid(pid)` raises `ProcessLookupError` once the leader is reaped, so every surviving descendant is missed), or a bare pgid guarded by `pgroup_exists` alone (a pgid IS a pid: once the group empties the kernel can hand it to an unrelated session leader, and `killpg` would then take down that stranger's whole tree — a group whose leader is ALSO gone reaches that same populated-but-leaderless shape, so only a member witnessed while the group was ours separates the two), a capped witness set (a fixed ceiling drops whichever descendant sorts past it, and the dropped one can be the sole SIGTERM-resistant survivor), or proving an incarnation only ONCE around a read that can race it (a pid reaped and reused mid-capture reads back `pgid == pid` like ours, and a swap during an unbounded scan makes every pid read after it the stranger's) |
| Ask an agent process for its spawn group | the public `LLMProvider.spawned_process_group()` capability resolved from the provider's TYPE (`getattr(type(p), ...)`), whose base returns `None`, re-validating the answer with `isinstance(g, SpawnedProcessGroup)` | a private client attribute, a structural `Protocol` + `isinstance`, a backend check, or an INSTANCE lookup (`getattr(p, ...)`) — a `Mock` stand-in synthesizes any attribute on access, so an instance lookup finds a capability on a double that declares none, an `AsyncMock` answers a synchronous caller with a coroutine it can only leak unawaited, and a returned `Mock`'s `pgid` coerces to 1 via `__index__`, making `killpg` a signal to every process this uid owns |
| Parent PID | `get_ppid(pid)` | `/proc` read / libproc |
| Session process identity | `get_process_start_id(pid)`; Windows uses query-only creation FILETIME, Linux start ticks, macOS libproc microseconds | caller-supplied PID or a bare PID without its creation identity |
| Linux execution-boundary equality | `process_namespaces_match(pid, reference_pid)`; compares user and mount namespace inodes with incarnation checks; `None` on unreadable or unsupported platforms | absent current ancestry as proof that a process is unconfined |
| macOS inherited sandbox state | `process_is_sandboxed(pid)`; read-only Seatbelt query with an incarnation check; `None` on errors or other platforms | treating an unavailable query as unsandboxed |
| macOS sandbox file-read permission | `process_can_read_under_sandbox(pid, trusted_absolute_path)`; queries Seatbelt without opening the file, checks incarnation before and after, and returns `None` on unknown | treating all sandboxed processes as either private or Global; a query error as a grant |
| Loopback TCP caller PID | `get_tcp_peer_pid(sockname[:2], peername[:2])`; unique ESTABLISHED reverse IPv4/IPv6 tuple, failure is unknown. Linux maps the kernel socket inode to process FDs; macOS uses trusted system lsof; Windows uses the owner-PID table. Offload this probe; prefer Unix peer credentials where available. | HTTP headers, a listener's PID, or matching only a port |
| Match process cmdline | `process_matches(pid, needles)` | `/proc/<pid>/cmdline` / `ps` |
| Process start time (PID-reuse guard) | `process_start_time(pid)` | `/proc/<pid>/stat` / `ps -o lstart=` (both answer `None` on Windows, so the guard silently never confirms) |
| Is this pid a PROCESS rather than a thread | `is_thread_group_leader(pid)` for one pid; `live_thread_group_leaders()` once for a whole sweep | `pid_exists(pid)` alone (Linux numbers threads from the pid space and POSIX permits signalling a tid, so a pid recycled as a THREAD of an unrelated process reads as alive forever). Both answer `None`, never `False`, when unknowable — treat `None` as "retain", never as licence to act |
| Signals | `platform_compat.SIGKILL` / `SIGTERM` | `signal.SIGKILL` (undefined on Windows) |
| Spawn isolation | `start_new_session=IS_POSIX` + `creationflags=CREATE_NEW_PROCESS_GROUP` | bare `start_new_session=True` |
| Wait on a subprocess PIPE with a deadline | a daemon reader thread feeding a `queue.Queue`, consumed with a bounded `get` (`testing/harness.py`'s `_StdoutPump`) | `selectors.DefaultSelector()` on the pipe (select()-based on Windows, which accepts SOCKETS only, so registering a pipe RAISES there) |
| Re-exec the current Python module | `reexec_python_module(module, args)` | `os.execv(sys.executable, [sys.executable, ...])` (breaks when the Windows interpreter path contains spaces) |
| Replace the current process with another program (a supervised service body) | spawn a child, record its pid + `process_start_time`, and `wait()` on it under `IS_WINDOWS` (see `pod.windows.supervise_gateway`) | `os.execve` (on Windows this SPAWNS and terminates the caller, so the pid changes and the service manager sees the unit exit while the real program keeps running orphaned) |
| Race-free Job object assignment | `creationflags \|= CREATE_SUSPENDED`, then `apply_job_limits`, then `resume_process_main_thread` | assigning a job to an already-running child (descendants it already spawned escape) |
| Fork-bomb / memory ceiling on a spawned tree | `sandbox.apply_windows_resource_ceiling(pid)` after the spawn, alongside `cgroup_scope_argv` | `cgroup_scope_argv` alone (a no-op on Windows, so no ceiling at all) |
| File mode | `chmod_safe(path, mode)` / `fchmod_safe(fd, mode)` | `os.chmod` / `os.fchmod` (no `os.fchmod` on Windows) |
| Owner-only secret (fail-loud) | `restrict_to_owner(path)` | `os.chmod(path, 0o600)` under `if IS_POSIX` (silent no-op leaves secrets world-readable) |
| Owner-only secret directory (fail-loud, inheritable) | `restrict_dir_to_owner(path)`; `make_owner_only_dir(path)` to also create it (its tighten step is best-effort) | `restrict_to_owner(path)` on a directory (its Windows grants carry no `(OI)(CI)`, so files created inside land on the default DACL, not owner-only; its `0o600` also drops the execute bit a directory needs) |
| Confirm a Linux readonly filesystem | `is_readonly_filesystem(path)`; false for other platforms or probe failure. Used to reject a private-runtime diagnostic marker planted in the writable host home. | `os.statvfs` in a cross-platform consumer, or readonly file mode alone |
| Directory link | `symlink_or_junction(target, link)` | `os.symlink` (`WinError 1314` without elevation) |
| Detect/remove a dir link | `is_link_or_junction(path)` / `unlink_link_or_junction(path)` | `path.is_symlink()` (misses a Windows junction) |
| Hold a directory in place while a child writes into it by path | `pin_directory(path)` (then `os.close`) | `os.open(dir, O_RDONLY)` (EACCES on Windows, and even where it opens it follows a link planted at the name) |
| Process RSS (live) / peak RSS / CPU | `proc_rss_bytes()` / `proc_peak_rss_bytes()` / `proc_cpu_seconds()` | `resource.getrusage` (`ru_maxrss` is a high-water mark, never a live reading, and its unit is KiB on Linux but bytes on macOS) |
| Available host memory | `host_available_mib()` (0 = unknown, never 0 = no memory) | `/proc/meminfo` directly (Linux-only, so the bound built on it silently vanishes on macOS and Windows) |
| FD soft limit | `raise_nofile_soft_limit(n)` | `resource.setrlimit` |
| Port to PID | `find_listening_pids(port)` / `listening_pid_tool_available()`; `find_port_listeners(port)` when ownership must be scoped to the local address actually probed | `lsof` directly |
| Spawn a system tool (`ps`, `lsof`, `netstat`, `taskkill`) | `trusted_system_bin(name)`, treating `None` as "unavailable" | a bare argv name (resolved through a `PATH` that can lead with same-uid-writable dirs) |
| Read a Windows system tool's ANSWER (`schtasks /Query`, `tasklist`, `sc query`) | the tool's **exit code**, or a fact the program under test recorded itself | parsing its stdout (column headers AND status words are translated by the UI language, so a match on `"Running"` reports every instance down on a non-English host — the fail-OPEN direction) |
| strftime no-pad | `strftime(dt, "%-I")` | bare `dt.strftime("%-I")` (`ValueError` on Windows) |

## Verifying a change

CI holds all three platforms at the UNIT layer: the `backend-test` shards cover
Linux, `backend-test-windows` covers Windows, and `backend-test-macos` covers
macOS. All three run the whole suite, so a POSIX call that only works on Linux
goes red on the macOS shards rather than shipping. A shard passing is still not
evidence that a gateway starts: 25 whole files are excluded on Windows by
`test/windows-collect-ignore.txt` and further node ids by
`test/windows-expected-failures.txt` and `test/macos-expected-failures.txt`.
What runs a real gateway on macOS and Windows is `ci.yml`'s `e2e-boot-matrix`
job (`test/e2e/test_gateway_boot_matrix.py`), which boots one per test against
the packaged fake ACP backend and asserts a completed prompt turn. Point a
process or signal change at that job, not only at the shards. See
[../../ci/e2e-gate.md](../../ci/e2e-gate.md).

Still run process, signal, file-lock and metrics changes on macOS **and** Linux
locally where you can. A test that only ever runs on the author's platform is how
a silent no-op ships, and a CI red found after the push costs a round trip.

A test that cannot pass on macOS gets a precise
`skipif(sys.platform == "darwin", reason=...)` naming the capability, or its node
id in `test/macos-expected-failures.txt`, the burn-down list applied by the
rootdir `conftest.py`, same mechanism as `windows-expected-failures.txt`. Never
widen a platform assertion to make a red go away.

Frontend support is Chrome, Firefox, Safari and Edge, using standard Web APIs and
guarding the rest (`typeof Notification !== 'undefined'`).
