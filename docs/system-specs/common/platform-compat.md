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
| Parent PID | `get_ppid(pid)` | `/proc` read / libproc |
| Session process identity | `get_process_start_id(pid)`; Windows uses query-only creation FILETIME, Linux start ticks, macOS libproc microseconds with a `sysctl KERN_PROC_PID` fallback for a zombie (libproc refuses one; the kernel's zombie list still carries the same `p_start` instant) | caller-supplied PID or a bare PID without its creation identity |
| macOS zombie state | `darwin_pid_is_zombie(pid)` (`True` / `False` / `None` unreadable; a pid the kernel does not list reads `True`); `darwin_kinfo_proc(pid)` for the record with its start id; `darwin_pgroup_members(pgid)` lists a process group with each member's zombie flag | `pid_exists` as an exit oracle (a zombie is alive to it); `pgroup_exists` as an empty-group oracle (a retained zombie leader keeps it true); `proc_pidinfo` on a zombie |
| Linux execution-boundary equality | `process_namespaces_match(pid, reference_pid)`; compares user and mount namespace inodes with incarnation checks; `None` on unreadable or unsupported platforms | absent current ancestry as proof that a process is unconfined |
| macOS inherited sandbox state | `process_is_sandboxed(pid)`; read-only Seatbelt query with an incarnation check; `None` on errors or other platforms | treating an unavailable query as unsandboxed |
| macOS sandbox file-read permission | `process_can_read_under_sandbox(pid, trusted_absolute_path)`; queries Seatbelt without opening the file, checks incarnation before and after, and returns `None` on unknown | treating all sandboxed processes as either private or Global; a query error as a grant |
| Loopback TCP caller PID | `get_tcp_peer_pid(sockname[:2], peername[:2])`; unique ESTABLISHED reverse IPv4/IPv6 tuple, failure is unknown. Linux maps the kernel socket inode to process FDs; macOS uses trusted system lsof; Windows uses the owner-PID table. Offload this probe; prefer Unix peer credentials where available. | HTTP headers, a listener's PID, or matching only a port |
| Match process cmdline | `process_matches(pid, needles)` | `/proc/<pid>/cmdline` / `ps` |
| Read Linux process name | `linux_process_name(pid)`; exact kernel `comm`, `None` off Linux or on an unreadable/empty value. Fixture tests may pass `proc_root=`. | reading `/proc/<pid>/comm` outside the compatibility layer |
| Compare process cgroups | `process_cgroups_match(pid, reference_pid)`; compares stable unified cgroup v2 membership and returns `None` off Linux, on cgroup v1, on unreadable/changing membership, or when identity is otherwise inconclusive. Fixture tests may pass `proc_root=`. | comparing one `/proc/<pid>/cgroup` read without stability rechecks |
| Process start time (PID-reuse guard) | `process_start_time(pid)` | `/proc/<pid>/stat` / `ps -o lstart=` (both answer `None` on Windows, so the guard silently never confirms) |
| Is this pid a PROCESS rather than a thread | `is_thread_group_leader(pid)` for one pid; `live_thread_group_leaders()` once for a whole sweep | `pid_exists(pid)` alone (Linux numbers threads from the pid space and POSIX permits signalling a tid, so a pid recycled as a THREAD of an unrelated process reads as alive forever). Both answer `None`, never `False`, when unknowable — treat `None` as "retain", never as licence to act |
| Signals | `platform_compat.SIGKILL` / `SIGTERM` | `signal.SIGKILL` (undefined on Windows) |
| Spawn isolation | `start_new_session=IS_POSIX` + `creationflags=CREATE_NEW_PROCESS_GROUP` | bare `start_new_session=True` |
| Wait on a subprocess PIPE with a deadline | a daemon reader thread feeding a `queue.Queue`, consumed with a bounded `get` (`testing/harness.py`'s `_StdoutPump`) | `selectors.DefaultSelector()` on the pipe (select()-based on Windows, which accepts SOCKETS only, so registering a pipe RAISES there) |
| Re-enter an edition's stable gateway launcher | `reexec_launcher(launcher, args)` after `gateway_restart.resolve_restart_launcher()` validates it; keeps the dispatch pathname, original arguments and UTF-8 environment | resolving the symlink basename away, passing Python `-m` flags to a launcher, or evaluating a shell command |
| Re-exec the current Python module | `reexec_python_module(module, args)` | `os.execv(sys.executable, [sys.executable, ...])` (breaks when the Windows interpreter path contains spaces) |
| Launch a Kiro Crew-owned Python child | `isolated_python_argv(*args, executable=...)`; it adds `-s` for the bundle and parents whose user site is already unavailable, unless the option prefix carries `-s` or stronger `-I` | a raw `[sys.executable, ...]`, or an ad hoc `PYTHONNOUSERSITE` env that another spawn path can omit |
| Replace the current process with another program (a supervised service body) | spawn a child, record its pid + `process_start_time`, and `wait()` on it under `IS_WINDOWS` (see `pod.windows.supervise_gateway`) | `os.execve` (on Windows this SPAWNS and terminates the caller, so the pid changes and the service manager sees the unit exit while the real program keeps running orphaned) |
| Open an exact Windows process object for later tree discovery/termination | `open_process_termination_handle(pid, expected_token)` validates the opened handle's creation identity before returning it (caller closes with `close_process_handle`); combine with `descendant_termination_handles` so the anchored root and each retained child receive a final post-exit snapshot | opening by PID and checking the token beforehand (PID reuse can occur between those operations) |
| Race-free Job object assignment | `creationflags \|= CREATE_SUSPENDED`, then `apply_job_limits`, then `resume_process_main_thread` | assigning a job to an already-running child (descendants it already spawned escape) |
| Fork-bomb / memory ceiling on a spawned tree | `sandbox.apply_windows_resource_ceiling(pid)` after the spawn, alongside `cgroup_scope_argv` | `cgroup_scope_argv` alone (a no-op on Windows, so no ceiling at all) |
| File mode | `chmod_safe(path, mode)` / `fchmod_safe(fd, mode)` | `os.chmod` / `os.fchmod` (no `os.fchmod` on Windows) |
| Owner-only secret (fail-loud) | `restrict_to_owner(path)` | `os.chmod(path, 0o600)` under `if IS_POSIX` (silent no-op leaves secrets world-readable) |
| Owner-only secret directory (fail-loud, inheritable) | `restrict_dir_to_owner(path)`; `make_owner_only_dir(path)` to also create it (its tighten step is best-effort) | `restrict_to_owner(path)` on a directory (its Windows grants carry no `(OI)(CI)`, so files created inside land on the default DACL, not owner-only; its `0o600` also drops the execute bit a directory needs) |
| Is a path on a NETWORK volume | `path_volume_is_remote(path)` (Windows: the volume ROOT's drive type through `windows_acl.volume_is_remote`, so a UNC path and a mapped drive both read remote, at no SMB round trip; `None` off Windows, where the caller has a mount table — `taskq.store.detect_network_filesystem` is the caller) | reading a failed query, `DRIVE_UNKNOWN` or a non-Windows host as "local" (`windows_acl.volume_is_local` collapses unknown onto `False` on purpose; that is the trust answer, not this one) |
| Confirm a Linux readonly filesystem | `is_readonly_filesystem(path)`; false for other platforms or probe failure. Used to reject a private-runtime diagnostic marker planted in the writable host home. | `os.statvfs` in a cross-platform consumer, or readonly file mode alone |
| Directory link | `symlink_or_junction(target, link)` | `os.symlink` (`WinError 1314` without elevation) |
| Detect/remove a dir link | `is_link_or_junction(path)` / `unlink_link_or_junction(path)` | `path.is_symlink()` (misses a Windows junction) |
| Compare a resolved path against an unresolved one | `strip_extended_length_prefix(path)` on BOTH sides before comparing | comparing the two spellings as `Path.resolve` returns them (on Windows `ntpath.realpath` keeps the extended-length prefix when its prefix-strip re-check races a concurrent swap of the same file, so a prefixed child against an unprefixed parent reads as a path escape; the fold is LEXICAL and must not re-resolve, which would bless the redirect the caller is testing for) |
| Hold a directory in place while a child writes into it by path | `pin_directory(path)` (then `os.close`) | `os.open(dir, O_RDONLY)` (EACCES on Windows, and even where it opens it follows a link planted at the name) |
| Process RSS (live) / peak RSS / CPU | `proc_rss_bytes()` / `proc_peak_rss_bytes()` / `proc_cpu_seconds()` | `resource.getrusage` (`ru_maxrss` is a high-water mark, never a live reading, and its unit is KiB on Linux but bytes on macOS) |
| Available host memory | `host_available_mib()` (0 = unknown, never 0 = no memory) | `/proc/meminfo` directly (Linux-only, so the bound built on it silently vanishes on macOS and Windows) |
| FD soft limit | `raise_nofile_soft_limit(n)` | `resource.setrlimit` |
| Port to PID | `find_listening_pids(port)` / `listening_pid_tool_available()`; `find_port_listeners(port)` when ownership must be scoped to the local address actually probed | `lsof` directly |
| Spawn a system tool (`ps`, `lsof`, `netstat`, `taskkill`) | `trusted_system_bin(name)`, treating `None` as "unavailable" | a bare argv name (resolved through a `PATH` that can lead with same-uid-writable dirs) |
| Spawn the AWS CLI (`aws`) | `trusted_aws_bin()` — `trusted_system_bin` plus a `/usr/local/bin` fallback (the installers' default `--bin-dir`), accepted only when `_is_root_owned_path` resolves the path COMPONENT BY COMPONENT and finds every directory it walks through — including the directories on a symlinked component's target side — plus the final target, root-owned, not group/world-writable, and (via `os.access(..., effective_ids=True)`, the only form that reads a POSIX ACL) not writable by the non-root account through an ACL entry `st_mode` cannot express. Running AS root DECLINES: there `os.access` answers True for everything, so the ACL arm has no signal, and the entry it would catch grants a NON-root user write — the one case root must not execute. Only the `/usr/local/bin` fallback is lost under root; `trusted_system_bin` does not route through this. The fallback also refuses a `#!` SCRIPT (`_is_native_program`): a shebang names its interpreter in the file's CONTENT, which the path walk never validated, and `sudo pip install awscli` against a pyenv Python produces exactly that — AWS CLI v2 ships a native executable, so the case this exists for is unaffected. Both conditions live in ONE predicate, `_local_aws_bin_is_trusted`, because the resolver and `aws_bin_declined_on_ownership` both ask and must never contradict each other about one file. Debian policy has `/usr/local` subdirectories `root:staff` mode `2775`, so the fallback DECLINES by default on stock Debian/Ubuntu: intended, because a `staff` member can replace the binary. A diagnostic must then report the decline with `aws_bin_declined_on_ownership()` rather than as absence | adding `/usr/local/bin` to `_TRUSTED_SYSTEM_BIN_DIRS` (Intel macOS Homebrew owns that directory as the console user, so membership alone would let a same-uid process supply `ps`, `lsof` and every other pinned tool); or validating the path with `realpath` (collapses a chain, so a hop through writable space vanishes) or a lexical `dirname` walk (`os.stat` follows symlinks and `dirname` does not, so a symlinked component's target ancestors are never seen) |
| Read a Windows system tool's ANSWER (`schtasks /Query`, `tasklist`, `sc query`) | the tool's **exit code**, or a fact the program under test recorded itself | parsing its stdout (column headers AND status words are translated by the UI language, so a match on `"Running"` reports every instance down on a non-English host — the fail-OPEN direction) |
| strftime no-pad | `strftime(dt, "%-I")` | bare `dt.strftime("%-I")` (`ValueError` on Windows) |

## Internal Python child user-site isolation

Every Kiro Crew-owned Python service, helper, and runtime dependency installer that
can run under the bundled interpreter routes argv through `isolated_python_argv`.
The helper adds `-s` for the bundle and for parents whose user site is already
unavailable, removing the interpreter's user-site directory without dropping
`PYTHONPATH`, cwd import behavior, or other required environment settings. A caller
that already carries `-I` stays unchanged; `-I` is stronger because it also ignores
Python environment variables and the cwd. A non-bundled parent that currently allows
the user site preserves that policy because Kiro Crew itself may be installed there.

A module-style child that imports `kiro_crew` from the parent's own user site
keeps the parent's user-site policy. Isolation is forced only when the parent's
own launch rewrite injects the child's import path, never because the child
inherited `PYTHONPATH`. A caller MUST therefore pass `force_isolation=True`
only for that launcher-injected path, such as the path-based dependency shim,
or for a script or entry path that does not import `kiro_crew` from the user
site. The explicit override keeps `-s` on non-bundled, user-site-enabled
interpreters only where removing the user site cannot remove the child target.

This contract does not apply to user-authored cron scripts, project test commands, or
other workloads whose documented environment may include `--user` packages. The
explicit inventory in `test/test_internal_python_isolation.py` names the internal
boundaries. It includes resident runtime workers such as
`piper_runtime.PiperRuntime._start`; their module launches follow the same unforced
parent-policy decision. Adding one requires routing it through the helper and adding it
to that inventory, so a later spawn cannot silently return to the user-site-dependent
behavior.
## Confined decision-log append

`platform_log_append.append_line` owns the decision log's filesystem transaction.
The configured home is resolved once as the trusted anchor; the immediate log
subdirectory and daily file are never resolved through links. POSIX pins the
anchor with `pinned_fs.pin_parent`, creates the directory relative to that pin,
and opens the directory and leaf with no-follow flags. Windows opens the resolved
anchor and the log directory with `FILE_LIST_DIRECTORY` access and read-only
sharing (attribute-only access takes no part in Windows sharing, so it would pin
nothing), rejecting reparse attributes on both, so a data-write or delete open of
either directory is a sharing violation while the append runs. The leaf uses
`CreateFileW` read/write, open-or-create, without following reparse points, and
its `GetFinalPathNameByHandleW` path must equal the pinned directory plus the
leaf name; a swap that landed before the pins is refused rather than written
through. Every native handle or descriptor is closed on failure as well as success.

Fresh POSIX directories/files request 0700/0600; existing modes are untouched.
The leaf is created with `O_CREAT | O_EXCL`. If another creator won or the file
already exists, one non-creating open uses the same pinned directory and retains
`O_NOFOLLOW`, `O_APPEND` and `O_NONBLOCK`. This avoids Darwin's concurrent
nonexclusive-create `ENOENT` without re-resolving the parent or following a
swapped link. A leaf that disappears between that `EEXIST` and the open is one
more lost interleaving: the existing bounded create-directory retry runs the whole
pinned sequence again, so the record is still written, through the same exclusive
no-follow create, under the same resolved anchor. Each failed attempt closes its
descriptors. Exhaustion raises `FileNotFoundError` naming the full path.
Other open errors propagate. Windows keeps its native open-or-create path.
Windows uses inherited ACLs, not a claim that POSIX mode bits enforce privacy.
The open file must be regular and have exactly one hard link. The existing
cross-platform file lock spans EOF validation, short-write/EINTR retries and
rollback. File offsets are explicitly reset inside the lock, including on Windows.
Lock acquisition and retries share a finite deadline that starts once the log is
open, so the create-and-pin ahead of it never spends the budget it cannot be
cancelled by. No additional worker is spawned; a stalled filesystem syscall
itself is not cancellable by that deadline.

On write failure, rollback removes only the bytes counted for that append when
the file has exactly the expected size. Existing bytes or unrelated growth are
never truncated. A pre-existing torn tail, including one left by failed rollback
or process death, is terminated with a newline before the next record is written,
so one torn row costs one unparseable line and never joins the record after it.
This is best-effort observation, not a
durable journal: no fsync or crash-atomic publication is promised. POSIX locks
serialize cooperating writers, not arbitrary same-user mutations or hostile
filesystem mounts. Descriptor pinning prevents redirected opens, not POSIX rename
of an already-open inode outside its original directory.

The retention sweep reuses the same pin: `pinned_log_dir` yields the no-follow directory descriptor on POSIX (names are listed with `scandir(fd)` and removed with `unlink(name, dir_fd=...)`) and holds the directory handle on Windows, so a swapped directory link cannot redirect a deletion. The decision caller catches append failures and warns without failing its turn.
`test/test_platform_log_append.py` exercises ordinary appends on every platform,
link refusal, short writes, rollback, concurrent writers, deadlines and cleanup.
Native Windows cases also require rename and directory-write-handle exclusion,
refusal of a redirected leaf handle, and release after failed CRT handle
conversion; Linux simulations do not verify these.

## Embedding threading and cancellation

Embedding cancellation uses `threading.Event` and monotonic deadlines on all
supported platforms. It cancels queued work, not a running native inference.
Executor admission follows the underlying future's completion, never the
cancelled asyncio waiter's lifetime. Cache stripes and dispatch locks never
cover native inference; store alignment holds only Python locks and performs
no model load or inference.

## Exact-handle descendant continuity on Windows

An open Windows process handle pins its process object and prevents PID reuse even
through exit; reuse is possible only after exit and the last handle closes
([Windows process-object lifetime](https://devblogs.microsoft.com/oldnewthing/20110107-00/?p=11803)).
Exact-handle tree discovery retains root/descendant handles through its scans;
retained PIDs cannot hide a replacement while their handles remain open.
Host-effect tests must attempt authoritative teardown in `finally`; OS refusal or
incomplete identity proof must fail loudly and preserve isolated HOME/service
evidence, never certify zero residue or invoke an unsafe duplicate cleanup authority.
Service-free, newly owned precondition cleanup is a separate case.

`windows.stop` requires a boot-contained Job descriptor or a generation-bound
kernel-zero receipt. The CLI reserves the run before scheduling; the supervisor
claims it under a separate short lock, assigns the initial child while suspended,
and atomically publishes its exact identities before resume. A legacy PID record,
marker, HOME or task without that protocol refuses reclamation.

Before `/End`, stop opens the existing Job and pins its publisher and available
initial process by exact identity. Access/query failures never mean death. After
retiring the publisher it requires a successful Job zero-count query and persists
a receipt. A publisher may also publish that receipt as its final action after
draining, with no further child creation or resume. Receipt consumers still retire
the publisher before cleanup. The receipt survives task-deletion, sidecar-deletion
or HOME-cleanup failure. Authoritative teardown must delete the handoff marker,
PID record and result sidecar successfully before consuming the receipt after the
full seven-sweep HOME cleanup. A retry uses the same generation's receipt even if
the Job has disappeared; supervisor-side diagnostic cleanup remains best-effort.
This covers unobserved restart branches without reconstructing dead intermediaries;
marker/PID record removal and polling history do not authorize reclamation.

`descendant_termination_handles` checks every first-snapshot edge against exact
handle creation/exit times, then rechecks identity and lifetime bounds after a
second snapshot. If an observed intermediary exits and disappears from Toolhelp,
its pinned handle preserves the first edge only when the second identity read
confirms the same PID/creation time and a published exit time. A surviving child's
PPID must still agree; its creation time must precede that intermediary's exit.
Changed parent links and positively disproven identities/lifetimes are excluded.
Unknown is not an exclusion: an unopenable candidate must be absent from a fresh,
successful full process snapshot, or discovery raises `OSError`. Absence alone
is insufficient when that same fresh snapshot contains an entry referencing the
observed, now-vanished unopened parent: discovery refuses even if the child and
its descendants first appeared after the initial snapshot. This guard reports
only a total and at most three child/parent PID pairs, takes no extra snapshot,
and grants no identity or termination authority. A vanished unopened child with
no fresh descendant reference remains admissible.
A false `pid_exists` result is not sufficient because query denial can produce it too.
Unreadable opened/retained identities, missing live objects, and a surviving
child whose vanished unpinned parent has no lifetime proof also raise, so callers
must preserve HOME/task state rather than certify a partial tree as drained.
For an unopenable candidate still present in the fresh snapshot, the refusal
includes failure-only diagnostics for at most three candidates and eight PIDs
per first/fresh ancestry chain. The opener captures the immediate native error
(or Python exception type only); the report includes the root identity at scan
start and current identity/lifetime observations from already-pinned relevant
handles. A separate query-only handle may observe the candidate, but is always
closed and is explicitly unvalidated: no observation changes the refusal or
provides kill authority. Diagnostic failures leave the original refusal intact.
No command lines, environment, file contents, or unrelated process inventory
are emitted, and successful discovery does not collect or log this report.
All newly opened handles are closed on failure, including failures partway through
opening candidates; root and retained handles remain caller-owned. Positively
rejected newly opened handles are closed before returning the proven subset.

This covers an **already observed, handle-pinned** chain. An intermediary that died
before it was ever observed/pinned remains unverifiable; a single numeric snapshot
is not enough to recover that chain. The deterministic and self-owned native
regressions are in `test/test_platform_compat.py`, `TestProcessDescendants`.

## Pod lifetime Job primitives

`pod._windows_job.PodJob` owns pod-specific named Windows Job handles. Creation
uses a unique global name and an owner-only protected DACL, so the scheduler and
CLI can run in different Windows sessions; opening an
existing job never creates one. Assignment borrows the caller's original process
handle and requires a never-resumed `CREATE_SUSPENDED` child. Breakaway and
kill-on-close flags are refused. Membership and accounting errors raise rather
than reporting absence; termination succeeds only after a bounded kernel zero
count. Closing a handle does not terminate members. The shared resource-ceiling
helper and its configuration are unchanged.

The Task Scheduler backend uses these primitives with `pod._windows_run` durable
run identities and publisher retirement. Job emptiness alone is not reclamation
authority. Before attempting `/Run`, a failed start may cancel only its exact
unclaimed reservation under the supervisor's claim lock. The durable `cancelled`
state refuses admission and survives task/wrapper/result cleanup errors; the next
`start` retries that cleanup before reserving a new generation. The claim lock
covers cancellation through receipt deletion. Claimed, malformed or changed-run
records and unexpected HOME/PID/handoff evidence refuse rollback. A failed or
raised `/Run` never grants cancellation, even if no publisher has claimed yet.
If cancellation itself cannot be persisted, the ordinary reservation remains
unresolved rather than being inferred safe on a later invocation.
Native tests exercise owner-only access, descendant containment,
nested resource jobs and breakaway refusal; injected failures run on all hosts.

## Verifying a change

`rename_noreplace` uses the Linux libc wrapper when available. On older glibc
without that symbol, it uses the same kernel operation through `syscall` with
an architecture-specific number. Unknown architectures remain unsupported,
and an occupied destination still refuses atomically. The fallback does not
replace the operation with a check-then-rename sequence.

CI holds all three platforms at the UNIT layer: the `backend-test` shards cover
Linux, `backend-test-windows` covers Windows, and `backend-test-macos` covers
macOS. All three run the whole suite, so a POSIX call that only works on Linux
goes red on the macOS shards rather than shipping — but the macOS shards are
NIGHTLY (`platform-tests.yml`, called by `nightly.yml`), not per-pull-request: a
`macos-15` runner took 176-213 minutes to arrive on the PR path, which is ~64% of a
pull request's CI wall clock, and the queue sat on the required check. So a
POSIX-but-not-Linux regression is caught within a day and before any nightly bytes
are published, rather than before merge. In front of a pull request there is
`macos-on-demand.yml` (the same full suite, called against the PR head, advisory;
runs on a darwin-sensitive path, on the `ci:macos` label, or on a 1-in-20 SHA sample) and the static side of
this table. A shard passing is still not
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
