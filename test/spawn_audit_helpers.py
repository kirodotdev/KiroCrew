"""The spawn audit's benign-spawn rows for spawns outside ``apps/builtins/``.

``test_spawn_audit.py`` checks the unrouted spawns in ``src/kiro_crew`` against
``BENIGN_SPAWNS``, the union of this table and ``BENIGN_APP_SPAWNS`` in
``spawn_audit_app_spawns.py``. A row is keyed by ``<relpath>::<enclosing function>``, and a
comment above a row or a run of rows justifies them; the rule for adding one is the
comment above ``BENIGN_SPAWNS`` in ``test_spawn_audit.py``.
"""

from __future__ import annotations

BENIGN_PACKAGE_SPAWNS: frozenset[str] = frozenset(
    {
        # boxd lifecycle: resolved operator CLI, fixed machine verbs, validated
        # generated names, saved snapshot/CPU and retention duration; no shell or
        # agent cwd, stdin DEVNULL. Needs the host login.
        "boxd_session/cli.py::_run",
        # asyncio.run drives the terminal bridge in-process, not a child spawn.
        "sprites_session/terminal.py::main",
        # Coder lifecycle: resolved operator CLI, fixed verbs, saved template/preset
        # and validated generated names; cwd the data home, stdin DEVNULL, no shell.
        "coder/client.py::_run",
        # Embedding child: this interpreter, -P, a fixed module; requests travel
        # on its pipes as data, never as argv. No agent input reaches the spawn.
        "embeddings_child.py::spawn",
        # Gateway transport: argv from the resolved CLI, saved deployment and
        # validated workspace; the shell text is a quoted GUEST command and payloads
        # ride stdin. Host credential isolation would prevent coder login.
        "coder/remote_worker.py::run_collect",
        # The fixed coder ssh attach argv plus a numeric port; stdin is framed bytes.
        "remote_session/client.py::dial",
        # asyncio.run drives the CLI loop in-process; it is not a child spawn.
        "remote_session/client.py::main",
        "remote_session/worker.py::main",
        # Guest-script rule, per execution function: these stdlib-only files run
        # inside the remote workspace, never on the gateway, so the guest is the
        # isolation boundary and the gateway sandbox is unavailable.
        "remote_session/worker.py::run",
        "remote_session/workspace_tools.py::run_command",
        "remote_session/workspace_tools.py::_git_status",
        # Gateway credential reads: the callers supply fixed CLI verbs and a
        # host from HOSTS, not guest argv. Git helper discovery uses trusted git,
        # its install cwd, a discovery ceiling and a scrubbed GIT_* environment;
        # the operator's global helpers remain available. No local shell parses
        # guest input. Host logins must remain visible to these credential CLIs.
        "remote_session/git_gateway.py::_run_quiet",
        # Gateway readiness uses fixed auth-status argv for a HOSTS entry, with
        # all three streams discarded. It reads no credential or guest cwd.
        "remote_session/git_gateway.py::login_status",
        "acp/runtime_process_tree.py::_get_rss_mb",
        # The spawn primitive for three fixed-argv kiro-cli one-shots
        # (`chat --list-models`, `whoami`, the `/usage` scrape). Every caller has
        # already wrapped the argv with sandbox.wrap_argv and cgroup_scope_argv
        # before handing it over; this function only prefixes the immutable
        # process-group supervisor and spawns through create_subprocess_limited
        # in a new session. Nothing agent-influenced reaches the argv here.
        "kiro_prerequisite_process.py::spawn_supervised_oneshot",
        # The Windows off-loop one-shot for provider CLI reads. Its only caller,
        # dashboard/source_providers/runner.py::_run_provider, has already
        # OS-isolated and env-scrubbed the argv (through its own routed sandbox
        # spawn-prep) before handing it over; this function only adds the
        # resource-limit shim via popen_limited and drains the bounded spawn on a
        # worker thread so CreateProcess leaves the gateway loop. Same pre-wrapped
        # rationale as spawn_supervised_oneshot above.
        "dashboard/source_providers/runner.py::_oneshot_drain_limited",
        # Four pre-existing spawns in ``acp/client.py`` and
        # ``acp/runtime_process_tree.py`` that the scan could not see until receivers
        # were derived from each file's imports (``acp/client.py`` binds the module
        # as ``subprocess_mod``). None is
        # agent-influenced and each is a fixed argv with a bounded timeout and no
        # shell: ``mise which <tool>`` where the tool is a module-level binary-name
        # constant; ``pgrep -P <pid>``, ``ps -o lstart= -p <pid>`` and
        # ``ps -o comm= -p <pid>``, all against a pid this core is already managing,
        # with ``ps`` resolved through ``platform_compat.trusted_system_bin`` rather
        # than PATH. They are listed rather than routed for the same reason the rest
        # of this group is: they are how process management and toolchain discovery
        # observe the machine, and the sandbox they would route through is a thing
        # they run underneath.
        "acp/client.py::_mise_which",
        "acp/runtime_process_tree.py::_direct_children",
        "acp/runtime_process_tree.py::_get_start_time",
        "acp/runtime_process_tree.py::_read_basename",
        # The opencode routing read-back. ONE fixed argv -- the resolved harness
        # binary plus the two literal words in ``_OPENCODE_CONFIG_READBACK_ARGS``
        # (``debug config``) -- with no shell, a 30s timeout, and a cwd that is the
        # session work dir rather than anything the agent names in a turn. The only
        # variable input is the child's ``OPENCODE_CONFIG_CONTENT``, which this core
        # composes from its own permission-setting table (see
        # ``acp/harness/opencode.py::_opencode_routing_config``); the agent supplies
        # nothing to it.
        # Stdout is read and nothing else: the JSON document is parsed for one key,
        # the harness's resolved ``permission``, which decides whether the session
        # may start at all.
        #
        # It is SANDBOX-WRAPPED before it is spawned: the caller runs the floor
        # check first, then hands this function an argv already through
        # ``wrap_argv_async`` with the same ``extra_hidden_dirs`` credential mask the
        # session spawn gets. That matters because the child is the harness's own
        # binary resolving config out of the work dir, which can load a project's
        # plugins -- unwrapped, it would read the credential homes the mask exists to
        # deny it. It is listed here rather than routed through
        # ``sandboxed_spawn_argv`` because that wrapper is for agent-INFLUENCED argv,
        # and nothing about this one comes from a turn.
        # Called from a worker thread, never the event loop --
        # ``test_the_routing_read_back_runs_off_the_event_loop`` in
        # ``test/test_acp_opencode_backend.py`` pins that.
        "acp/harness/opencode.py::_verify_opencode_routing",
        # The opencode version gate's probe. ONE fixed argv -- the resolved
        # harness binary plus the literal words in ``_OPENCODE_VERSION_ARGS``
        # (``--version``) -- no shell, a 15s timeout, and a cwd that is the session
        # work dir rather than anything the agent names in a turn; the agent
        # supplies nothing to it, and stdout is read for the major version alone.
        # SANDBOX-WRAPPED before it is spawned, exactly like the routing read-back
        # above: the caller (``_opencode_version_refusal``) hands it an argv already
        # through ``wrap_argv_async`` with the same ``extra_hidden_dirs`` credential
        # mask, and reclaims the launcher artifact on every path. Listed rather than
        # routed for the same reason the read-back is: ``sandboxed_spawn_argv`` is
        # for agent-INFLUENCED argv, and nothing about this one comes from a turn.
        # Called from a worker thread, never the event loop --
        # ``test_the_version_child_is_wrapped_like_the_read_back`` in
        # ``test/test_acp_opencode_backend.py`` pins that.
        "acp/harness/opencode.py::_opencode_reported_major",
        # The pi gate read-back, the same shape as the opencode one above. ONE fixed
        # argv -- Kiro Crew's own gate launcher (a file this core wrote into the
        # sandbox run directory, execing the resolved ``pi`` binary) plus the three
        # literal words in ``_PI_RPC_ARGS`` (``--mode rpc --no-themes``, what the
        # adapter itself passes) -- no shell, a 30s timeout, and a cwd that is the
        # session work dir. The only input written to the child is
        # ``_PI_READBACK_REQUEST``, a constant ``get_commands`` frame on stdin; the
        # agent supplies nothing to it. Stdout is read for one response, whose
        # command list decides whether the session may start at all.
        #
        # SANDBOX-WRAPPED before it is spawned, by the same caller pattern: the
        # floor check runs first and the argv arrives through ``wrap_argv_async``
        # with the same credential mask the session spawn gets, because the child
        # is the agent itself loading extensions out of the operator's own
        # directories. Called from a worker thread, never the event loop --
        # ``test/test_acp_pi_backend_spawn.py`` pins that.
        "acp/harness/pi.py::_verify_pi_gate",
        # pi's Node-engine probe, the same shape as the opencode version probe
        # above. ONE fixed argv -- a ``node`` executable found on the session's
        # PATH, beside the resolved ``pi`` or in pi's npm prefix, plus the literal
        # ``--version`` -- no shell, a 10s timeout, stdin closed, an environment of
        # only ``PATH`` and ``HOME``, and a cwd that is the session work dir; the
        # agent supplies nothing to it in a turn, and stdout is read for a version
        # alone. SANDBOX-WRAPPED for the life of the child by the caller's wrap
        # (``acp/harness/pi.py::_pi_probe_wrap``: ``wrap_argv`` with the session's
        # credential mask, run after the floor check, launcher reclaimed on exit),
        # because a version-manager shim reads config from that work dir. Called
        # from a worker thread, never the event loop --
        # ``test/test_acp_pi_node_guard.py`` pins the wrap, the order and the thread.
        "acp/harness/pi_node.py::_node_version",
        # The DeepSeek Harness gate read-back, the same shape as the pi one above:
        # the argv is the SESSION'S own argv already wrapped by ``wrap_argv_async``
        # before it reaches this function, so the sandbox and credential mask are
        # applied by the caller rather than here. Nothing in it is agent-influenced
        # -- the harness binary comes from its ``ACP_BACKEND_LAUNCH`` row, the sealed
        # plugin and its patch from the owner-only gate-artifact directory, and the
        # marker path from the probe's OWN private scratch window (allocated by the
        # adapter, passed as ``extra_private_dirs``, removed in its ``finally``) -- the
        # one argument the child writes, and it lands nowhere the child could plant
        # something a later session loads. stdin is a pipe that carries nothing and
        # is closed once the plugin publishes its marker (EOF is the profile's own
        # shutdown). The env adds only the operator's configured key NAMES under
        # canary values, never the key. Called from a worker thread, never the
        # event loop.
        "acp/harness/deepseek.py::_verify_deepseek_gate",
        # The subprocess-pool child interpreter: ONE fixed argv, ``sys.executable -I -S
        # -c <leaf source>``, where the source is the text of a module-relative
        # constant script (the sensitive-path resolver's
        # ``security/_child_realpath.py``; tests pass their own stub) captured when
        # the executor module loads, so a later edit to the file reaches no respawn.
        # No agent value reaches the command, the args or the cwd -- the
        # path to resolve travels over stdin as a length-prefixed frame, never as an
        # argument, and no shell is involved. It is listed rather than routed because
        # this child exists to ``lstat``/``readlink`` the very paths the
        # sensitive-path gate is checking, sensitive ones included: under the agent
        # sandbox it would be denied exactly those reads, and a resolver answering
        # "cannot resolve" where the true answer is a credential symlink's target
        # would weaken the gate rather than harden it. ``-I`` drops ``PYTHONPATH``
        # and the working directory ``-c`` would add from ``sys.path`` so nothing can
        # be planted in front of the child's imports; the cwd is inherited
        # deliberately, so a path resolves in the child to what it resolves to in the
        # parent.
        "subprocess_pool/executor.py::_spawn",
        # The PDF extractor child: ONE fixed argv, ``sys.executable -P -m
        # kiro_crew.pdf_extract_child --max-chars=N --max-pages=M`` with both
        # numbers module constants of the two callers (file-grep, knowledge
        # ingest). The untrusted input -- the document -- travels on stdin, never
        # on argv, cwd or env, and the env is ``scrub_env()``. It is spawned
        # through ``popen_limited`` under ``RLIMIT_PROFILE_EXTRACTOR``, whose
        # fixed ``RLIMIT_AS`` is the containment this spawn exists to add:
        # ``pdfplumber`` commits a page's whole character list before any caller
        # can measure it, so the memory bound has to sit one process down.
        "pdf_extract.py::extract_pdf_segments",
        # The shadow-venv update engine's one spawn seam. Nothing it runs is
        # agent-influenced, and none of it can route through sandboxed_spawn_argv,
        # because the engine's whole job is to build the NEXT gateway install
        # outside the agent sandbox. _spawn_build_child runs, in its own session
        # (inside the gateway with the trusted-PATH scrubbed environment, under
        # `kirocrew update` with the operator's own shell environment; interpreter
        # children run -I either way): the openssl binary
        # resolved via trusted_system_bin (never PATH), which reads the pinned key
        # on stdin and the key and signature over anonymous pipe FDs (no file is
        # staged by name; the Windows fallback opens each one
        # O_CREAT|O_EXCL|O_NOFOLLOW), `sys.executable -I -m venv <tree>`, the shadow
        # interpreter's `-I -m pip` refresh, install and `pip check`, and the `-I`
        # import probe. The tree name is composed from the SIGNED manifest's
        # validated version string and the wheel path from the same workdir. The
        # update flow is reachable only from the CLI on the operator's terminal,
        # the gateway's approve endpoint behind the OQ7 host-local step-up, and the
        # gateway's own update coordinator (auto_update or a policy floor, against
        # the check's own verdict) — the agent's own bash path is closed by the
        # self-update denied rule.
        "platform/wheel_engine_build_child.py::_spawn_build_child",
        # The userns probe child: ONE fixed argv, `sys.executable -I -S -c <shim>`,
        # no shell, no cwd, stdin/stdout are the two handshake pipes. Nothing is
        # agent-influenced -- the shim is a module-level string constant and takes
        # no arguments. It CANNOT route through sandboxed_spawn_argv: this probe is
        # what decides whether that sandbox exists at all, so routing it would be
        # circular (wrap_argv consults the verdict this child is producing). The
        # child does two unshare() calls against its own fresh process and exits;
        # it executes nothing else (the shim dlopens the already-loaded libc
        # rather than letting ctypes.util.find_library exec ldconfig/gcc).
        "sandbox_probe.py::_probe_unshare_via_spawn",
        # The speech-runtime preflight child: `sys.executable -I -S -c <module-level
        # string constant> <extension path>`, no shell, a bounded timeout, cwd
        # pinned to the interpreter prefix, `-I` keeping the working directory
        # off `sys.path` so nothing the gateway was started from can shadow the
        # import, `-S` keeping `site` -- and with it every `.pth` in the venv's
        # site-packages -- from running at all in a child that is unsandboxed on
        # purpose, and an environment reduced to a fixed allow-list (no credentials,
        # no `PYTHON*`, no `KIROCREW_*`). The one argument is the path of the
        # installed `_pywhispercpp` extension as the interpreter's OWN finder
        # resolved it (`binary_identity()`, i.e. `importlib.util.find_spec`), never
        # anything an agent supplied; it is passed so the `-I` child, which cannot
        # see a user-site install by name, loads the same file the parent would.
        # The child prints the build's compile-time feature string. It is a
        # subprocess for exactly one reason: that load can SIGILL on a CPU the build
        # was not compiled for, which kills the process it runs in, so it must not
        # be this one (kirodotdev/KiroCrew#13179); the child zeroes its own
        # RLIMIT_CORE first so that death writes no core file. The interpreter is
        # the one running the gateway, like the userns probe above.
        "stt/preflight.py::_run_probe_child",
        # _get_rss_tree_mb is deliberately NOT listed: its own spawn moved into
        # _ps_process_table below, so an entry for it would be stale and would
        # mask a future regression that put a spawn back inline.
        #
        # Whole-machine process-table snapshot behind _get_rss_tree_mb's macOS
        # branch, extracted so N pids share ONE walk. Same trust profile as
        # _get_rss_mb above: one fixed argv (`ps -Ao pid=,ppid=,rss=`) with a 2s
        # timeout, no shell, no cwd, and no arguments at all — nothing here is
        # agent-influenced, and the binary is resolved through
        # platform_compat.trusted_system_bin (a vetted absolute path), not PATH.
        "acp/runtime_process_tree.py::_ps_process_table",
        # (_bootstrap.py::_self_heal removed — the console-entry self-heal now
        # delegates its install to dep_sync.sync_or_reinstall, so the spawn lives
        # at that key below and an entry here would be stale.)
        # Diagnostics support-bundle version probe: fixed argv
        # ``[<kiro-cli>, "--version"]`` with a 5s timeout, no shell, no cwd, and
        # no agent-influenced args — it only stamps the collected kiro-cli
        # version into versions.txt. The binary is the absolute path
        # ``kiro_cli.pin_kiro_cli`` returns from the known install directories
        # with the inherited PATH excluded (no pin, no spawn), so nothing an
        # agent can write to names the executable; a resource ceiling / sandbox
        # adds nothing to a `--version` call.
        "diagnostics.py::_kiro_cli_version",
        # The spec-permissions and MCP-deferral version gates: fixed argv
        # ``[<kiro-cli>, "--version"]``, 5s timeout, no shell, no cwd, cached per
        # binary path and mtime. Every probed path is pin_kiro_cli's own install:
        # the pinned binary or its chat sibling. Nothing a PATH entry names
        # reaches this argv; no turn text enters argv.
        # The result gates spec permissions and Crew MCP deferral, and a sandbox
        # adds nothing to this bounded ``--version`` probe.
        "kiro_cli.py::kiro_cli_version_at",
        # Tailnet origin derivation + forwarded-peer whois (RFC:
        # rfc-tailnet-dashboard-access): one fixed argv — ``["<tailscale>",
        # "status", "--json"]`` or ``["<tailscale>", "whois", "--json",
        # <validated tailnet address>]`` — with a 3s timeout,
        # no shell and no cwd. The binary is resolved from a vetted absolute
        # allowlist (``_CLI_CANDIDATE_PATHS``) and NOT from ``PATH`` — a ``PATH``
        # lookup made the executable itself agent-selectable even though the
        # arguments never were, since ``~/.local/bin`` is both on ``PATH`` and
        # agent-writable. The child also gets ``sandbox.scrub_env()`` rather than
        # the inherited environment. Deliberately NOT routed through
        # ``sandboxed_spawn_argv``: this is a read-only query of the local daemon
        # on the dashboard's startup path, and the module's load-bearing property
        # is that *nothing raises* so the gateway still boots on a host with no
        # Tailscale. Routing it would make dashboard startup depend on sandbox
        # availability, which is exactly the failure that property rules out.
        "dashboard/tailnet.py::_run_json_detail",
        # Tailnet publish/withdraw (same RFC): three fixed argv shapes —
        # ``serve status --json``, ``serve --bg --https=443 http://127.0.0.1:<port>``
        # and ``serve --https 443 off``. The only interpolated value is the
        # dashboard's own port, read from config and rendered as an int. Same
        # hardening as the read path and for the same reasons: the binary comes
        # from ``_cli_path``'s vetted absolute allowlist and never from ``PATH``,
        # and the child gets ``sandbox.scrub_env()`` instead of the gateway's
        # environment — both shared with ``tailnet.py`` by import rather than
        # copied, so the two cannot drift apart.
        #
        # Deliberately NOT routed through ``sandboxed_spawn_argv``: the whole
        # purpose of the call is to mutate the LOCAL Tailscale daemon's serve
        # configuration through its unix socket, which is precisely the ambient
        # authority a sandbox exists to remove. A routed call would either fail
        # or need the socket handed back in, which is the sandbox in name only.
        # This is an operator-initiated action, gated on the
        # ``capabilities.tailnet_origin`` ceiling at the enforcement call, and
        # never reached from a tool dispatch path.
        "dashboard/tailnet_serve.py::_run",
        # Shared Crew's daemon query fallback when no LocalAPI socket answers:
        # ``["<tailscale>", "whois", "--json", <addr>]`` or
        # ``["<tailscale>", "status", "--json", "--peers=false"]``. The address is
        # the connecting socket's own peer, never request text; the binary comes
        # from ``cli_candidates``, which keeps only the vetted absolute paths
        # ``tailnet.py`` trusts; the child gets ``sandbox.scrub_env()``. Not
        # routed for the same reason as ``tailnet.py::_run_json_detail``: it is
        # a read-only query of the local daemon on the auth path, and it must
        # never depend on sandbox availability.
        "dashboard/tailnet_localapi.py::_run_cli",
        # PID-reuse guard for app-backend reap and source-stable Browser
        # descendant checks. Only the macOS/other-POSIX arm spawns, and it uses
        # the fixed ``ps -o lstart= -p <pid>`` argv: no shell, the binary comes
        # from ``trusted_system_bin`` rather than PATH, and the sole interpolated
        # value is a pid the gateway itself recorded, rendered through ``str()``.
        # Nothing here is agent-influenced.
        "platform_compat_identity.py::_process_lstart",
        "apps/backend.py::_resolve_nvm_path",
        # py-spy attach for `kirocrew perf sample --pid`: fixed list-argv (no
        # shell=True), binary resolved via shutil.which rather than from input,
        # and every value is either a range-validated int (pid/seconds/rate) or a
        # path passed as a flag VALUE. NOT sandboxed because py-spy's whole job is
        # reading another process's memory (ptrace / task_for_pid) — a sandbox that
        # scrubbed that capability would break the feature it is guarding. Gated
        # behind KIROCREW_DEBUG and reachable only from the CLI.
        "cli_perf.py::_sample_out_of_process",
        # The SINGLE shared gh spawn chokepoint (github_runner.run_gh), serving
        # Issue Radar (`_gh_run`), Code Review Sage (`run_gh_json`,
        # `current_login`, `pipeline.list_open_prs`), and any future gh caller.
        # A fixed `gh api`-style LIST argv (never shell=True): owner/repo
        # segments are charset-validated (^[A-Za-z0-9._-]+$) plus a github.com
        # host allowlist by parse_github_repo_url / adapters.parse_repo_ref
        # before they ever reach the argv; issue numbers are int()-coerced;
        # write bodies travel as JSON stdin (--input -), never argv; jq filters
        # are hardcoded module constants. NOT sandbox-routed because gh needs
        # the host's OWN authenticated session (~/.config/gh + the keychain),
        # which the sandbox would hide, breaking auth. As defense-in-depth
        # WITHIN this benign classification, run_gh refuses a non-absolute
        # argv[0] (binding callers to the validated resolve_gh path, never a
        # shim on the agent-writable front of PATH), passes a MINIMAL env
        # (safe-key base + gh's own auth/network/TLS vars — no AWS/Slack/SSH
        # secrets), and emits an SEL audit event on success/failure/timeout.
        "github_runner.py::run_gh",
        # Fixed `git -C <package root> rev-parse HEAD` / `git diff --quiet HEAD -- <root>`
        # argv (shell=False). The only path is the kiro_crew package directory this
        # process imported, derived from `__file__`; no agent-influenced input reaches
        # it, and it runs once per process (lru_cache) to name the code revision the
        # MCP gateway daemon and its owner compare.
        "code_fingerprint.py::_git_fingerprint",
        # Fixed `git log -1 --format=%ct` argv (shell=False), run against the same
        # `code_fingerprint._PACKAGE_ROOT` the entry above uses, and derived from
        # `__file__` rather than from any request. No agent-influenced input reaches
        # the command, the args or the cwd: `debug_gateway` takes no parameters at
        # all. It answers HEAD's commit time so the route can say whether the running
        # gateway predates the caller's fix, and it is bounded by a 5s timeout with
        # every failure answering None.
        "dashboard/handlers/debug.py::_head_commit_time",
        # dep_sync stands in for `pip install -e .` on a checkout whose console
        # script is locked, and it spawns the same shapes that step did:
        # `<target python> -c <fixed metadata/version probe>` and `<target python>
        # -m pip install <requirements the merged revision declares>`. It spawns no
        # git at all -- it runs after the merge, so it reads the declarations
        # straight from the working tree. The interpreter is the target repo's own
        # venv python (handed down, never resolved from PATH here); the repo comes
        # from the operator-configured checkout, and the requirement specs are read
        # from that checkout's own declarations -- the same ones `pip install -e .`
        # would have read, so this adds no surface the step it replaces did not
        # already have. Routing here would also NEST sandboxes: dep_sync runs as a
        # sync step, which runtime.py::worker already wrapped through
        # sandboxed_spawn_argv, and a filesystem-scoped wrapper around pip would
        # block the venv writes that are the point of the step.
        #
        # sync_or_reinstall spawns the EDITABLE REINSTALL for the same callers, and
        # is allowlisted on the same grounds: it is the identical argv those callers
        # spelled out inline before, with the target read from sys.executable and
        # the repo from the operator-configured checkout. Sandboxing it now would
        # refuse a step every platform has always run unsandboxed.
        #
        # The three interpreter probes (interpreter_version,
        # installed_package_origin, installed_console_script_target) share one
        # fixed-argv helper, _probe_interpreter, which is where their single
        # spawn now lives: `<target python> -I -X utf8 -c <fixed probe>` with a
        # neutral cwd, so the answer describes the venv instead of the caller.
        # The doctor's venv deps check (cli_doctor._venv_deps_ok) and the STT
        # scripts-dir probe (transcribe._python3_bin_dir) route through the
        # same helper for the same reason: their argv is equally fixed, and an
        # unisolated `python -c` would let a decoy on the caller's
        # PYTHONPATH/CWD answer for the interpreter under test.
        "dep_sync.py::_probe_interpreter",
        # The stale-asset watchdog's relaunch probe: `<python> -X utf8 -c "import
        # kiro_crew.cli, kiro_crew.cli_server"` from `/`, a fixed literal. The interpreter is the one
        # the service manager's own loaded command names, run under that unit's
        # environment, which the supervisor's relaunch would execute anyway; it
        # is unisolated on purpose, because the relaunch is.
        "dep_sync.py::_probe_relaunch_import",
        "dep_sync.py::sync",
        "dep_sync.py::sync_or_reinstall",
        # _git_blob_text is the pre-mutation interpreter-floor gate's one read:
        # `<git> show <ref>:<fixed filename>` (pyproject.toml / setup.cfg) in the
        # operator-configured checkout, so an update can refuse a revision the
        # venv cannot import BEFORE the pull/reset moves the tree to it. Nothing
        # in the argv is agent-influenced: the ref is either the literal `@{u}`
        # or the OID the auto-apply already resolved and is about to reset to,
        # the filenames are fixed, and the git binary is whatever the calling
        # update path already uses for its own pull/reset (the trusted pinned
        # binary on the unattended path). Same read shape and grounds as
        # npm_preflight._extract above.
        "dep_sync.py::_git_blob_text",
        # (apps/dependencies.py::_run_aim removed — App Kit capability deps now
        # resolve through the CapabilityManager seam, so the resolver spawns no
        # subprocess at all and needs no allowlist entry.)
        # Browser Mode setup/install path, run only from the dashboard settings
        # save (off the event loop) or the `kirocrew browse setup` CLI. Fixed
        # argv of trusted node-toolchain tools resolved via find_node_tool
        # (npm/npx/node) plus the ``playwright install <engine>`` subcommand,
        # ``browser_cli`` spawns the Playwright CLI toolchain on fixed argv that
        # the AGENT never contributes to, which is what makes them benign here:
        #   * ``install_runner.py::_run`` runs the three install steps
        #     (``npm install -g @playwright/cli@latest``,
        #     ``playwright-cli install-browser``, ``playwright-cli install
        #     --skills agents --global``). Every token is a constant in our code
        #     and the only trigger is the operator pressing Install in Settings.
        #     Sandboxing it would be wrong, not merely unnecessary: the install
        #     needs the real npm registry and writes the global prefix.
        #   * ``view.py::_spawn`` runs ``playwright-cli show --port <n>`` where
        #     the port comes from our own bind probe, and the host is the
        #     hardcoded loopback constant.
        # ``view.py::stop`` spawns nothing: it reaps its own child's process
        # group rather than issuing a global ``show --kill``, which would take
        # an operator's independent session down with ours.
        #   * ``launcher.py::_run_cli`` runs ``playwright-cli --json list`` and ``-s=panel-<8hex>
        #     goto|open <url>`` (and ``close`` at shutdown) for the Browser
        #     panel's address bar. The ONE free argv element is a URL the
        #     dashboard OWNER typed: the route is owner-only, refuses
        #     internal-secret (agent) callers, and ``validate_url`` admits only
        #     http(s) with a host and no credentials before it reaches argv.
        #     The session name is hex derived from the slot key. Not sandboxed
        #     for the same reason ``show`` is not: the CLI launches the
        #     operator's real browser daemon, which needs the real cache and
        #     network, and the human at the dashboard is the approval.
        # The agent's OWN browser commands are not spawned by us at all -- it
        # runs them as ordinary shell tool calls through the standard approval
        # path.
        "browser_cli/install_runner.py::_run",
        # The KAS engine installer (`kirocrew kas install`, operator terminal, outside
        # the sandbox). `_open_tar` runs the host's `zstd -d -c` on an archive whose
        # name the manifest validated as a bare object name and whose bytes were
        # sha256-pinned before extraction; `_probe_node` runs the just-extracted Node
        # with `--version` from the gateway-owned staging directory. No agent input
        # reaches either argv. `_run_tool` runs `lipo -create`/`lipo -archs` for
        # `make_universal_node`, called only by packaging/build-desktop.sh at build
        # time: the paths are the installed Node and the other arch's sha256-pinned
        # Node, both under the build's own staging root.
        "acp/kas_bundle.py::_open_tar",
        "acp/kas_bundle.py::_probe_node",
        "acp/kas_bundle.py::_run_tool",
        "browser_cli/launcher.py::_run_cli",
        "browser_cli/view.py::_spawn",
        # ``chromium_compat.py::_run_tool`` runs ``nm -D <chrome>`` and ``ldconfig -p``
        # once at gateway start to decide whether the playwright-cli config needs an
        # older cached Chromium. The tool comes from ``trusted_system_bin`` (fixed
        # system dirs, never PATH), the flags are fixed, and the one free element is
        # a ``chrome`` path under the gateway's own Playwright cache. No agent input
        # reaches the argv; it only reads symbol tables, like the system probes above.
        "browser_cli/chromium_compat.py::_run_tool",
        # The offline run of an authored dashboard page (``dashboard_verify``). The argv
        # is fixed: a system Python and a browser path, each from a fixed list of system
        # locations and each past the strict provider-executable check (root-owned, not a
        # link, not writable by this account, so nothing the agent can write is ever
        # run), a launcher stub that moves the DevTools pipe onto descriptors 3 and 4,
        # and flags that are constants of ours plus a throwaway profile directory we
        # created. The page is DATA sent over the pipe; it reaches no flag, path or
        # environment variable.
        # Not routed through the sandbox chokepoint, on purpose: Chromium's own sandbox
        # is the containment and it must create namespaces, which the chokepoint's mount
        # namespace would deny; the process runs on a scrubbed environment, with every
        # request but the page refused, no listening port, and its process group killed
        # at the end.
        "dashboard_verify.py::_spawn",
        "cli_knowledge.py::_consolidate_cmd",
        "cli.py::_ensure_node",
        "cli.py::_node_ok",
        "cli.py::main",
        "cli_chat.py::_run_chat",
        # NOT a subprocess spawn: the AST heuristic matches ``asyncio.run`` (attr
        # ``run`` on base ``asyncio``), here used only to drive the now-async
        # ``deregister_app_crons_from_service`` coroutine from the loop-less CLI
        # disable/uninstall path. No child process is created; the sole input is
        # the operator-typed app name. Same classification as the other
        # ``asyncio.run`` sites below (cli_doctor.py::_doctor, workflows
        # server.py::handle_run).
        "cli_commands_app.py::_cleanup_app_crons_from_scheduler",
        # NOT a subprocess spawn: the AST heuristic matches ``asyncio.run`` (attr
        # ``run`` on base ``asyncio``) driving the async
        # ``register_app_crons_with_service`` coroutine from the loop-less CLI
        # enable path — the exact enable-direction mirror of
        # ``_cleanup_app_crons_from_scheduler`` above. No child process is
        # created; the sole input is the operator-typed app name.
        "cli_commands_app.py::_register_app_crons_to_scheduler",
        # NOT a subprocess spawn: the AST heuristic matches ``asyncio.run`` (attr
        # ``run`` on base ``asyncio``) driving ``KasLoginService``'s device-code
        # sign-in and sign-out coroutines from the loop-less ``kirocrew kas
        # login`` / ``kas logout`` verbs. No child process is created: the
        # inputs are operator-typed flags (provider, start URL, region) that go
        # into HTTPS requests to the issuer and into Crew's own vault, never
        # into an argv. Same classification as ``cli_doctor.py::_doctor``.
        "cli_kas_login.py::_login",
        "cli_kas_login.py::_logout",
        "cli_doctor.py::_doctor",
        # NOT a subprocess spawn: the AST heuristic matches ``asyncio.run`` (attr
        # ``run`` on base ``asyncio``) driving the async Discord
        # privileged-intent probe from the loop-less doctor path. No child
        # process is created: the probe is one HTTPS GET to Discord's own
        # ``/oauth2/applications/@me`` with a bot token read from the operator's
        # own credential store, and every failure is folded into its result. Same
        # classification as ``cli_doctor.py::_doctor`` above.
        "cli_doctor_parts/host_probes.py::_discord_intent_grants",
        "cli_doctor_parts/mcp_sections.py::_doctor_mcp_tools",
        # ``node -v`` with a fixed argv and a 5 s timeout, read-only, to judge the
        # installed Node against ``MIN_NODE_VERSION``. Split out of ``_doctor``
        # unchanged; same classification as ``cli.py::_node_ok``.
        "cli_doctor_parts/host_probes.py::_report_node",
        # The AST heuristic matches ``asyncio.run`` (attr ``run`` on base
        # ``asyncio``) driving one async capability-manager read from the
        # loop-less doctor path so the Credentials section can report whether this
        # host mounts a credential-vending MCP server. Unlike the sibling
        # ``asyncio.run`` entries above this one is not purely a false positive:
        # on a composed edition the awaited ``list_mcp()`` does reach that
        # edition's own package manager as a child process. It is benign for the
        # reasons the allowlist asks for — the argv is the manager's own fixed
        # subcommand with no agent-influenced component, the result is read-only
        # and never carries a credential value, and the public default spawns
        # nothing at all (``available()`` is False, so the await is never issued).
        # Note this section IS reachable from a tool call, so the waiver rests on
        # those three legs rather than on who invokes doctor.
        "cli_doctor_parts/host_probes.py::_credential_vendor_line",
        # ``aws configure list-profiles`` / ``aws configure get credential_process``
        # for the Credentials section. Fixed argv — the subcommand is a literal and
        # NOTHING is interpolated, so no component is agent-influenced; the binary
        # is resolved through ``platform_compat.trusted_system_bin("aws")`` rather
        # than ``PATH`` — which can lead with an agent-writable worktree venv
        # ``bin`` while doctor runs as the operator — and a miss means no spawn at
        # all.
        # Read-only and 10s-capped. These two exist SO THAT doctor does not parse
        # ``~/.aws/config`` itself: that file is inside a directory the sensitive-
        # path floor fences from the agent, and ``kirocrew doctor`` is reachable
        # from a tool call, so reading it here would vend through a diagnostic what
        # the floor refuses directly. ``aws configure`` is the sanctioned path the
        # deny-remediation text itself names, and the profile set it returns is the
        # same information an allowed command already gives the agent — a
        # credential VALUE is never requested or printed.
        "cli_doctor_parts/host_probes.py::_aws_profile_names",
        "cli_doctor_parts/host_probes.py::_aws_auto_refreshes",
        # Read-only diagnostic for the Source Checkout section: ``git -C <repo>
        # rev-parse/rev-list`` with a hardcoded argv whose only variable is the
        # install's own source directory (derived from the package's module
        # path, never agent-supplied). The binary itself is pinned via
        # ``platform_compat.trusted_system_bin("git")`` with a Windows-only
        # fallback to the fixed Git for Windows install roots under Program
        # Files (literal paths, never ``%ProgramFiles%`` — the environment is
        # exactly what the pin declines to trust); a miss on both means no
        # spawn at all. Operator-invoked doctor, 10s-capped, queries only — no
        # fetch, no mutation. Same classification as the other fixed-argv
        # doctor probes (``_detect_userspace_oom_killer``, ``_detect_linger``).
        "cli_doctor_parts/host_probes.py::_git_line",
        # Read-only system-metrics probe for the Memory section on macOS, where
        # the ``platform_compat`` shim has no ctypes-only per-pid RSS path:
        # ``ps -o rss= -p <pid>`` with a hardcoded argv whose only variable is
        # the gateway pid read from our own lock file (never agent-supplied).
        # The binary is pinned via ``platform_compat.trusted_system_bin("ps")``;
        # a miss means no spawn at all. Operator-invoked doctor, 2s-capped, no
        # shell. Same classification as the ``ps``-based probe in
        # ``acp/runtime_process_tree.py::_get_rss_mb``.
        "cli_doctor_parts/host_probes.py::_gateway_rss_bytes",
        # ``<kiro-cli> acp --help`` readiness probe for the KAS backend: fixed
        # argv (subcommand and flag are module constants), 15s-capped, no shell,
        # no agent-influenced arguments, and no credential involved — it reads
        # help text to confirm this kiro-cli can select the KAS engine at all.
        # Crew mints no KAS token anywhere; the relay resolves tokens
        # from kiro-cli's own store (see ``acp/kas_transport.py``), so the former
        # ``chat _ get-kas-token`` spawn is gone rather than moved.
        "kiro_cli.py::kas_relay_help",
        # ``<kiro-cli> chat --help`` engine probe for the ``/usage`` scrape: fixed
        # argv, 15s-capped, no shell, no agent-influenced arguments, no credential.
        # It reads which ``--agent-engine`` values this kiro-cli accepts, so the
        # scrape pins V2 only where V2 exists. Cached per binary path and mtime,
        # and only ever called on ``pin_kiro_cli``'s own install, never a PATH hit.
        "kiro_cli.py::chat_agent_engines_at",
        # ``<kiro-cli> whoami`` sign-in probe for the declaration-driven auth row:
        # fixed argv (the binary name is a module constant and the subcommand is a
        # literal), 10s-capped, no shell, no agent-influenced argument. It reads only
        # the EXIT CODE -- stdout is captured so it cannot reach a terminal and is
        # never parsed, so no identity or token value enters the process.
        #
        # This is the one harness whose credential state the doctor probes at all, and
        # deliberately so: kiro-cli signs in to the HOST identity store, so its state
        # is the host's own. Every other harness keeps its entitlement in a file it
        # owns, and reading that file is what the credential floor exists to forbid --
        # a probe there would be the one reader the floor cannot fence. Those rows
        # print their declared remedy unprobed instead (``_doctor_agent_auth``).
        "cli_doctor_parts/host_probes.py::_kiro_cli_signed_in",
        # ``systemctl is-active <unit>`` probes for the memory-pressure
        # preparedness check: argv is hardcoded (systemd-oomd/earlyoom unit
        # names), no agent influence, 5s-capped, read-only query.
        "cli_doctor_parts/host_probes.py::_detect_userspace_oom_killer",
        "cli_server.py::_logs_cmd",
        "cli_server_restart.py::_spawn_detached_gateway",
        "cli_server_update.py::_update",
        # The agent-only config refresh extracted from _update: a fixed argv
        # (`<this interpreter> -m kiro_crew setup --agent-only`) built from
        # sys.executable plus literals, cwd from the detected install layout —
        # no shell, no PATH lookup, nothing agent-influenced. stdin=DEVNULL
        # and TimeoutExpired handling are pinned by
        # test_update_agent_refresh.py.
        "cli_server_update.py::_refresh_agent_config",
        # (_divergence_verdict removed — its counting now delegates to the
        # git_divergence module, allowlisted below, and spawns nothing itself.)
        "cli_server_update.py::_update_wheel",
        "cli_setup.py::_setup_electron",
        # Cursor Motion overlay renderer: `<this interpreter> -m
        # kiro_crew.computer_use.overlay_proc`, a fixed argv built from
        # sys.executable plus a module constant — no shell, no PATH lookup, and
        # nothing agent-supplied (pinned structurally by
        # test_computer_use_unsupported.py::test_overlay_spawn_is_a_fixed_module_launch).
        # The only agent-influenced values in the subsystem are numeric screen
        # coordinates, and they travel as JSON on the child's stdin, never as argv.
        # It exists as a subprocess because AppKit requires a MAIN-THREAD run loop
        # and the gateway's main thread is the asyncio loop; drawing in-process is
        # impossible, and a segfaulting AppKit in the gateway would take the chat
        # sessions, cron scheduler and Slack socket down with it. NOT sandbox-routed:
        # the child's entire purpose is to draw on the user's real WindowServer
        # session, which a sandbox that rewrites the process identity would deny.
        # The child is purely cosmetic — it reads no window, captures no pixels, and
        # imports none of the AX/capture modules (asserted in
        # test_computer_use_overlay.py::test_the_renderer_never_reaches_into_the_ax_or_capture_surface).
        "computer_use/overlay.py::_spawn",
        # ``computer_launch_app``: opening an application IS creating a process, so
        # this verb cannot exist without a spawn. Listed here rather than routed
        # through ``sandboxed_spawn_argv`` for the same reason as the overlay above —
        # the child's whole purpose is to appear on the operator's real desktop, which
        # a sandbox that rewrites the process identity would deny — and the argv is
        # bounded by verification rather than by the sandbox:
        #
        #  * the argv is exactly ``[executable]`` (Windows) or
        #    ``[/usr/bin/open, -a, <bundle>]`` (macOS), with NO agent-supplied element:
        #    no document, no flag, no URL. Pinned structurally by
        #    test_computer_use_unsupported.py::
        #    test_a_launch_spawn_interpolates_nothing_into_its_argv;
        #  * the target is a NAME resolved against an OS catalog, never a path the
        #    caller supplied — the MCP schema has one ``app`` field and no others
        #    (pinned by test_computer_use_launch.py::
        #    test_the_schema_accepts_no_path_or_argument_field);
        #  * on Windows the resolved executable must sit under an install root this
        #    user cannot write AND be named after the catalog key that found it, which
        #    is what neutralises the measured fact that ``HKCU``'s ``App Paths`` and
        #    ``%LOCALAPPDATA%\Microsoft\WindowsApps`` (on PATH) are both agent-writable.
        #
        # The honest residual, stated rather than discovered: this DOES let the agent
        # start an installed application the operator did not ask for, and the built-in
        # denylist is the only thing narrowing which. That is the same posture the rest
        # of computer use takes once the operator enables it (see
        # computer-use.md § Known limitations) — not a new plane.
        "computer_use/launch_windows.py::spawn_detached",
        "computer_use/launch_macos.py::spawn_detached",
        # NOT subprocess spawns: the AST heuristic matches ``asyncio.run``
        # (attribute ``run`` on base ``asyncio``). Both sites drive an in-process
        # coroutine from the loop-less CLI entry point; the network I/O is
        # aiohttp in the same process and no child is created. Same
        # classification as the other ``asyncio.run`` sites in this list.
        "connections/l0_probe.py::_run_record",
        "connections/l0_probe.py::main",
        # Same classification as l0_probe: a CLI entry point driving an
        # in-process aiohttp sweep with asyncio.run.
        "connections/l1_smoke.py::main",
        "cloud/source.py::_git_tracked_files",
        # `check-attr filter` over the same tracked list: reads attributes, runs
        # no driver, fixed argv, the hardened env. Same classification.
        "cloud/source.py::_tracked_attr_values",
        "cloud/source.py::_tracked_tree_is_dirty",
        "cloud/source.py::_use_git_archive",
        # Release-tag probe before a packaged install's cloud launch: `<trusted
        # git> ls-remote --exit-code --tags -- <repo> refs/tags/<ref>`, a fixed
        # argv with no shell. The binary comes from
        # `platform_compat.trusted_git_bin` (never PATH); `ref` is built by
        # `release_channel.release_refs` from this build's own `__version__`
        # through a `\d+\.\d+\.\d+` regex, and `repo` is the template's public
        # URL or a caller argument `ec2.deploy` charset-validates -- neither is
        # agent-reachable. Env drops every inherited `GIT_*` and pins global /
        # system config off; stdin is DEVNULL; the exit code is the only thing
        # read. Same classification as the `cloud/source.py` git probes above.
        "cloud/ec2.py::release_tag_exists",
        # Windows tunnel teardown: `taskkill /T /F /PID <pid>`, a fixed argv whose
        # only variable is the pid of a child THIS process created (the Popen handed
        # to kill_port_forward) -- never agent-supplied, no shell, no PATH shim
        # (taskkill is a System32 binary). It exists because Windows has no
        # os.killpg: without a tree kill the session-manager-plugin child survives
        # and keeps the forwarded local port bound, which is the exact leak
        # kill_port_forward exists to prevent. NOT sandbox-routed for the same
        # reason as its sibling `open_port_forward` below -- a sandbox that rewrites
        # the process identity could not signal our own already-running child.
        "cloud/ssm.py::_kill_tree_windows",
        "cloud/ssm.py::_run_install_command",
        "cloud/ssm.py::open_port_forward",
        # Local identity probe: `<resolved kiro-cli> whoami --format json`, a
        # fixed argv with no shell and no agent-reachable input — the binary
        # comes from `kiro_cli.resolve_kiro_cli` (the same resolution every
        # other kiro-cli spawn uses), stdin is DEVNULL and the output is only
        # parsed, never executed. Same classification as the whoami probes
        # in `cloud/ssm.py` above.
        "cloud/login_target.py::discover_local_identity",
        "dashboard/chat_voice.py::api_voice_voices",
        # Computer-use permission probe: `<our own kirocrew binary> computer
        # doctor --json`, a fixed argv (module constants) with no shell and no
        # agent-reachable input — the handler passes nothing from the request
        # body. The binary is resolved by `agent._kirocrew_mcp_invocation`, i.e.
        # the SAME install as the running gateway (or `sys.executable -m
        # kiro_crew`), never a PATH shim the agent could plant. It exists as a
        # subprocess precisely to keep the native ctypes probe OUT of the
        # gateway: a missing ctypes argtypes is a SIGSEGV, not an exception, and
        # in-process it would take the chat sessions, cron scheduler and Slack
        # socket down with it. NOT sandbox-routed because the whole point of the
        # probe is to read the HOST's own macOS TCC grants, which a sandbox that
        # rewrites the process identity would answer wrongly.
        "dashboard/handlers/computer_use.py::_probe_permissions",
        "dashboard/file_api/editor.py::_run",
        # The file panel diff: fixed list-argv `git diff --no-index -- a b`
        # (no shell) with the same textconv/attributes/fsmonitor hardening as
        # `_run`, over two files the gateway itself wrote into a fresh private
        # temp dir. No argument, path or cwd comes from the request.
        "dashboard/file_api/editor.py::_diff_redacted_bodies",
        "dashboard/file_api/desktop.py::api_screenshot",
        "dashboard/file_api/desktop.py::api_upload",
        "dashboard/handlers/knowledge_sources.py::_run_folder_dialog",
        # Terminal live-cwd probe on hosts without /proc (macOS/BSD): fixed
        # `lsof -a -p <pid> -d cwd -Fn` list-argv (no shell=True) where <pid>
        # is the gateway's own PTY child pid (an int from asyncio.subprocess),
        # never agent input. Read-only introspection of our own process tree;
        # sandboxing would break lsof's access to host process state.
        "dashboard/handlers/terminal.py::_proc_cwd",
        "dashboard/handlers/terminal.py::api_terminal_ws",
        "dashboard/handlers/updates_apply.py::_apply",
        # The update check's git side: fixed `git fetch` / `rev-parse` / `show` /
        # `diff` list-argv (no shell=True) run in KIROCREW_PROJECT_DIR, an operator
        # environment value, never agent input. Read-only version comparison —
        # nothing here writes to the tree.
        "dashboard/handlers/updates_check.py::_check_git_checkout",
        # (_venv_pip_install removed — it now delegates the install to
        # dep_sync.sync_or_reinstall and spawns nothing itself.)
        "dashboard/handlers/updates_apply.py::api_update_apply",
        # Fixed argv `git config --global --path --get core.excludesFile`: reads the
        # operator's own config file for an ignore-list path, runs no program.
        "dashboard/handlers/updates_apply.py::_user_excludes_file",
        "dashboard/handlers_system.py::_collect_system_metrics",
        # Split out of _collect_system_metrics above so the whole-machine process
        # walk can be cached on its own (much longer) TTL instead of the live
        # graph's. Identical trust profile to its former enclosing function,
        # which is still listed: one fixed argv (`ps -eo pid,command`) with a 5s
        # timeout, no shell, no cwd, no agent-influenced arguments.
        "dashboard/handlers_system.py::_scan_mcp_processes",
        "dashboard/handlers_system.py::_get_static_system_info",
        "dashboard/port_reclaim.py::_listeners_on_port",
        # Bundle-freshness startup probe: fixed `git rev-parse HEAD:website`
        # list-argv (no shell=True) with git PINNED via trusted_git_bin (fixed
        # system dirs, never PATH; absent trusted git → skip, no spawn) under
        # code_fingerprint.hardened_git_env (no planted config). `-C` is the
        # package's OWN install directory, never a request- or agent-supplied
        # path. Runs once at dashboard startup, only in a source checkout where
        # the baked _build_info WEBSITE_TREE is absent, and its output is
        # compared (read-only) against the dist's website/ tree stamp to log a
        # staleness warning. No agent input reaches command, args, or cwd.
        "dashboard/stale_bundle_guard.py::_backend_website_tree",
        "env.py::_run",
        "env.py::activate_mise",
        # Node bootstrap: runs the bundled ``ensure-node.sh`` (a fixed `bash
        # <script>` argv, script path derived from KIROCREW_PROJECT_DIR / the
        # module's own location, never agent input) when no node resolves. Same
        # class as cli.py::_ensure_node, which invokes the identical script.
        "env.py::ensure_node",
        # Fixed argv (`npm run build`) in the operator's own checkout. The npm
        # binary and project path arrive from the caller: the Dev Fleet sync
        # resolves npm via its trusted-bin allowlist and the path from the
        # operator-registered worktree, never from agent input.
        "frontend.py::_npm_build_and_stage_locked",
        "frontend.py::build_frontend_async",
        "frontend.py::build_frontend_sync",
        # _write_build_source_fingerprint stamps the built bundle's source
        # identity beside static/dist, with two read-only spawns:
        # `<git> -C <root> status --porcelain -- website` and
        # `<git> -C <root> rev-parse HEAD:website`. Fixed list-argv, shell-free,
        # no agent-supplied component: <root> is the operator's own registered
        # checkout, the subcommands and pathspec are literals, and neither call
        # writes anything (only the resulting tree id is written to a file, by
        # Python, not by git). The git binary is the sync's _trusted_bin absolute
        # path when Dev Fleet calls build_and_stage (git= is threaded through);
        # the standalone callers fall back to a PATH `git`, the same resolution
        # the sibling frontend.py::_npm_build_and_stage_locked already uses for
        # npm. Same trust class as npm_preflight.py::_install_already_proven and
        # git_divergence.py::count_divergence.
        "frontend.py::_write_build_source_fingerprint",
        # The shared ahead/behind divergence count: a read-only ``git rev-list
        # --count --left-right HEAD...<upstream>`` fixed list-argv (no shell)
        # run against the install's own checkout. Callers pass the repo path
        # (KIROCREW_PROJECT_DIR, an operator environment value) and the
        # upstream spelling — a literal ``@{u}`` or ``origin/<branch>`` where
        # <branch> is git's own ``rev-parse --abbrev-ref HEAD`` output sitting
        # after the ``origin/`` prefix so it cannot become an option. Nothing
        # agent-supplied, nothing written; same trust profile as the update
        # surfaces it serves (``_check_git_checkout`` / ``api_update_apply`` /
        # ``_update`` above). The papyrus status caller does NOT spawn through
        # these: it reuses only the argv/parse primitives and keeps its own
        # sandbox-routed runner.
        "git_divergence.py::count_divergence",
        "git_divergence.py::count_divergence_sync",
        # The shared isolated update fetch: a `git fetch <url> <branch>` into a
        # throwaway bare git dir this function builds and owns, so the checkout's
        # own config can name no hook that fires on the fetch's ref write. git is
        # the trusted-bin absolute path the caller resolved (threaded as `git=`);
        # the env is `git_command_env()` (trusted PATH, built-in transports only,
        # every fixed-key exec vector pinned); the URL is the governance-validated
        # `resolve_remote_url`, the branch git's own `branch.<b>.merge`. The
        # spawn carries start_new_session and the shared kill-and-reap on timeout
        # or cancellation. Same trust class as git_divergence.py::count_divergence;
        # it replaced the inline fetch spawns in _check_git_checkout and
        # _auto_apply_update.
        "platform/update_governance.py::fetch_and_pin_upstream",
        "instances/diagnostics.py::_run_ok",
        "instances/diagnostics.py::_run_stdout",
        "instances/ssh_tunnel_manager_child.py::start",
        "instances/token_mint.py::mint_remote_token",
        "instances/token_mint.py::run_remote_kirocrew",
        # NOT a subprocess spawn: the AST heuristic matches ``asyncio.run`` in the
        # desktop tunnel keeper's CLI entry point. The only child it creates is the
        # forward ``_SshTunnel.start`` spawns, listed just above.
        "instances/tunnel_keeper.py::run",
        # The iMessage bridge child (`<cli_path> rpc [--db-path <p>]`). Fixed
        # list-argv, no shell: both paths come from the operator's own
        # `config.json` `imessage` section, which the settings API writes only
        # from a direct-local request and which rejects a line break or NUL, so
        # neither value can be agent-supplied or split into extra arguments.
        # Sandboxing it would defeat the point: the child exists to reach
        # Messages.app through the operator's own Full Disk Access and
        # Automation grants, which a scrubbed-env sandbox strips.
        "imessage/rpc.py::start",
        "mcp_gateway/backend.py::spawn_backend",
        # NOT a subprocess spawn: the AST heuristic matches ``asyncio.run`` (attr
        # ``run`` on base ``asyncio``), used here only to drive the one-shot
        # ``_ping_async`` coroutine from a synchronous CLI path -- ``kirocrew
        # doctor`` and ``kirocrew stop`` have no event loop of their own. The
        # coroutine opens a local endpoint (``AF_UNIX`` socket or named pipe) and
        # exchanges two frames with a daemon that is already running; no child
        # process is created and there is no argv to sandbox. Same classification
        # as the other ``asyncio.run`` sites in this list.
        "mcp_gateway/daemon_control.py::_ping_detailed",
        "mcp_gateway/gatewayd.py::main",
        "mcp_gateway/manager.py::_spawn_once",
        "mcp_gateway/stub.py::main",
        # The Windows arm of the stub's degrade path. Same spawn as
        # ``stub.py::main`` and the same argv: the target command and args the
        # rewriter resolved out of the operator's own ``~/.kiro/agents/*.json``,
        # plus the env from the 0600 sidecar it wrote beside them -- an MCP
        # backend on the trusted side of the sandbox boundary, like every other
        # ``mcp_gateway`` entry above. Nothing on this path is agent-supplied;
        # the stub receives it all as its own argv from the overlay.
        #
        # It is a spawn only because Windows has no in-place exec: the POSIX
        # sibling of this call is ``os.execvpe``, which is not a spawn at all and
        # so needed no entry. Sandboxing it would be strictly wrong rather than
        # merely pointless -- the child must inherit THIS process's stdio fds to
        # keep serving kiro-cli's existing pipe, which is the whole fix, and a
        # scrubbed env would drop the credentials the sidecar exists to restore.
        "mcp_gateway/stub_fallback.py::_fallback_spawn_child",
        # The update seam's one read-only git chokepoint: `git config` (the
        # `updates.source` pin's remote, the repo-driver probe, and which remote a
        # branch tracks) and `git ls-remote --get-url`. Fixed list-argv (no
        # shell=True), no agent input: the branch lands mid-key
        # (`branch.<x>.remote`) so it cannot lead with a dash, and the remote
        # name — which is read out of git config and COULD — is passed after
        # `--`. Must NOT be sandboxed: it reads the real checkout's git metadata.
        # It does carry `git_neutralizer_env()`, so repo config cannot make these
        # reads exec a program (`core.fsmonitor` and friends are pinned).
        "platform/update_governance.py::_git_probe",
        # Same trusted (or caller's) git, fixed argv `check-attr -z --stdin filter
        # diff` fed the tracked path list on stdin: reads attributes, runs no
        # driver, the probe's env. Same classification as `_git_probe`.
        "platform/update_governance.py::_bound_driver_names",
        # `git config --system|--global --includes -z --get-all safe.directory`,
        # run so `git_command_env` can forward those values after it stops git
        # reading the home's config files. Fixed list-argv (no shell=True), no
        # agent input, cwd is the filesystem root. Must NOT be sandboxed: it
        # reads the operator's real config. Reading config runs no program.
        "platform/update_governance.py::_read_protected_safe_directories",
        # Read-only `git rev-parse --show-toplevel` deciding whether the install
        # root IS a working tree. Fixed list-argv (no shell=True); the only
        # variable is the path, which comes from KIROCREW_PROJECT_DIR — an
        # operator environment value, never agent input — and is absolutized,
        # NUL-rejected and dash-rejected before being passed to `-C`, so it cannot
        # be read as an option. The environment is stripped of the GIT_DIR family
        # so no inherited value can redirect the answer to another repository.
        # Must NOT be sandboxed: the answer is about the real checkout's own
        # metadata.
        "platform/update_capability.py::_git_toplevel",
        # Signature verification for both signed documents: the feed manifest
        # gating the forced-update floor, and the feature-video manifest. Both
        # verifiers share this one helper, which is where the spawn lives.
        # One fixed argv (`openssl dgst -sha256 -verify …`) whose binary comes
        # from platform_compat.trusted_system_bin (a vetted absolute path,
        # never PATH — asserted by test_feed_trust's PATH-shim test), no
        # shell, a 10s timeout, and no cwd. The untrusted document bytes travel
        # as FILE CONTENT inside a private TemporaryDirectory; the three path
        # arguments are that directory's own absolute literals, so nothing
        # agent- or network-influenced ever reaches argv. Must NOT be
        # sandboxed: the spawn's whole purpose is to REJECT tampered input,
        # and every failure (openssl missing included) already fails safe to
        # "no floor".
        "platform/feed_trust.py::_verify_signature",
        # File-manager launchers for the dashboard's reveal action. The
        # command is an absolute literal resolved in this module (never a bare
        # argv name, so an agent-writable PATH entry cannot supply it), the
        # spawn gets a PATH pinned to the trusted system directories, and the
        # only caller-supplied element is the path being revealed — which is
        # passed as a later argv element, never as the command.
        "platform_compat_trust.py::open_with_default_app",
        # Read-only process-table snapshot through the trusted absolute ``ps``
        # binary. It uses fixed argv, no shell or cwd, and accepts no agent input.
        # It cannot route through sandbox because sandbox imports this module.
        "platform_compat_descendants.py::_posix_process_snapshot",
        "platform_compat_ports.py::probe_port_listeners",
        # Read-only kernel socket attribution: trusted absolute lsof binary,
        # fixed flags and a validated numeric port, no shell/user command.
        "platform_compat_ports.py::_macos_tcp_peer_pid",
        "platform_compat_runtime.py::find_python_interpreter",
        "platform_compat_kill.py::kill_pid",
        "platform_compat_kill.py::kill_process_tree",
        "platform_compat_trust.py::reveal_in_file_manager",
        "platform_compat_identity.py::process_command_line",
        # Same class as process_command_line: a read-only process-attribute query
        # (``ps -o uid=`` / ``/proc/<pid>`` stat) in the platform leaf module,
        # with a fixed argv containing only an int-coerced pid. It cannot route
        # through the sandbox helper because sandbox imports platform_compat.
        "platform_compat_identity.py::process_owner_uid",
        "platform_compat_identity.py::process_matches",
        # Same class as process_matches: a read-only process-attribute query
        # (macOS ``ps -ww -o command= -p <pid>``; Linux reads /proc without
        # spawning) whose only interpolated value is an int-guarded pid — the
        # expected argv is compared IN PYTHON, never passed to the child. It is
        # the strict identity check consulted before reclaiming a recorded
        # forwarder pid, and cannot route through the sandbox helper because
        # sandbox imports platform_compat.
        "platform_compat_identity.py::process_argv_matches_exact",
        # OS keep-awake helper for the prevent-sleep feature (power.py). FIXED
        # argv — `caffeinate -i -w <pid>` on macOS, `systemd-inhibit
        # --what=idle:sleep --mode=block … /bin/sh -c 'while kill -0 <pid> …'`
        # on Linux — whose binaries are resolved from fixed absolute system
        # paths (never PATH), and whose ONLY variable is os.getpid() (an int,
        # never agent input). No shell PATH lookup, no cwd, nothing
        # agent-influenced. It is an OS power utility, not an agent/LLM
        # subprocess, so the AcpClient sandbox chokepoint does not apply and a
        # resource ceiling adds nothing to a fixed caffeinate/systemd-inhibit.
        "power.py::_spawn_posix_inhibitor",
        "pod/cli.py::_logs",
        # launchd twin of pod/runtime.py::_run below: the single chokepoint for
        # `launchctl <verb> gui/<uid>/dev.kirocrew.pod.<name>`. Argv is a fixed
        # verb set plus a label built from a validate_name-checked pod name —
        # not agent-influenced. Same disposition as the systemctl wrapper.
        "pod/launchd.py::launchctl",
        # Windows interpreter discovery for the worktree venv, the same class as
        # platform_compat_runtime.py::find_python_interpreter above. Fixed argv: the `py`
        # launcher plus `-<version>` and a literal `-I -X utf8 -c` probe that
        # prints sys.executable. The only variable is the version string, built
        # by _python_candidates from this module's own MIN_PYTHON/_NAMED_MINORS
        # constants and sys.version_info -- never agent input -- and it is a later
        # argv element, never the command. No shell, no cwd, bounded timeout,
        # stderr discarded, and the result is used only after it is confirmed to
        # name a real file.
        "pod/provision.py::_find_python_via_launcher",
        # Interpreter probe for the worktree venv: runs a candidate interpreter
        # with a literal `-I -m venv --without-pip <tempdir>`, then that temp
        # venv's own python with a literal `-I -c` snippet built from the
        # MIN_PYTHON constant. Candidates are this process's own interpreter and
        # names resolved from fixed install dirs / PATH -- the same interpreters
        # `_run` then builds the venv with, unsandboxed, today. No shell, no
        # cwd, no worktree code runs, bounded timeout, stdin closed.
        "pod/provision.py::_probe_python",
        # `uv python find --no-python-downloads '>=<MIN_PYTHON>'`: a fixed argv
        # from resolve_uv() (the wheel's own locator, then an absolute PATH hit)
        # and a module constant. Read-only lookup, never a download, bounded
        # timeout, stdin closed, stderr discarded.
        "pod/provision.py::_uv_found_python",
        "pod/provision.py::_run",
        "pod/runtime.py::_git_worktrees",
        "pod/runtime.py::_run",
        "pod/runtime.py::recent_journal",
        # Task Scheduler twin of launchctl above: the single chokepoint for
        # `schtasks.exe /<verb> /TN KiroCrewPod-<name> ...`. Argv is a fixed verb
        # set plus a task name built from a validate_name-checked pod name and a
        # script path under the pod dir. `schtasks()` routes through it, and the
        # gate's own create-and-delete probe of a throwaway task calls it
        # directly. Not agent-influenced.
        "pod/windows.py::_schtasks_raw",
        # The pod's own gateway. On Linux and macOS the service manager execs
        # it; Windows has no exec, so the task's wrapper spawns
        # `<python> -m kiro_crew gateway ...` itself and supervises it. Argv is
        # the interpreter plus a fixed module and flags from the pod's config,
        # created CREATE_SUSPENDED so the Job object ceiling is applied before
        # the first instruction. It IS the product, not an agent subprocess; the
        # agent-facing spawns inside it go through the normal chokepoints.
        "pod/windows.py::supervise_gateway",
        "sandbox_probe.py::_probe_sandbox_exec",
        "sandbox_argv.py::_ssh_supports_accept_new",
        # The aggregate slice-ceiling apply: `systemctl --user set-property
        # --runtime kirocrew-agents.slice MemoryMax=<N>M MemorySwapMax=0
        # TasksMax=<N>`. Argv is a fixed verb plus module-constant unit name;
        # the only variable tokens are integers derived from config/sysconf
        # (type-checked, junk falls back to defaults) — not agent-influenced.
        # Runs once at gateway startup, not from an agent turn. Sandboxing it
        # would be circular: it constructs the cgroup boundary agent spawns
        # are confined by.
        "sandbox_slice.py::ensure_agents_slice_limits",
        # Shared Crew per-principal slices: `systemctl --user set-property
        # --runtime <slice> CPUQuota=<N>% MemoryMax=<N>M MemorySwapMax=0
        # TasksMax=<N>`, `start|stop <slice>` and `is-active <unit>`. The binary
        # is resolved from trusted system directories, the unit name is minted
        # by shared_crew.containment (sha256 hex of a principal id) and checked
        # against a unit-name pattern, and every number comes from the bounded
        # dashboard.shared_crew config -- nothing is agent-influenced. Like the
        # aggregate ceiling above, it builds the boundary agents are confined by.
        "shared_crew/containment.py::_run",
        # The agent-slice MemoryHigh reconciler. Fixed argv: `systemctl --user
        # set-property --runtime kirocrew-agents.slice MemoryHigh=<value>`, where
        # the binary is resolved with shutil.which (never a caller-supplied PATH)
        # and <value> is derived from host RAM, never from agent input.
        # Sandboxing it would also be circular: it CONFIGURES the cgroup
        # containment that agent spawns are wrapped in.
        "sandbox_slice.py::_ensure_agent_slice_memory_high",
        # The chokepoint wrapper itself. It spawns whatever argv it is handed, so
        # it cannot route on its own behalf — its CALLERS are the ones this audit
        # holds to sandboxed_spawn_argv / wrap_argv, and they still appear here
        # individually because _SPAWN_NAMES collects bare-name calls to it.
        "sandbox_rlimit.py::create_subprocess_limited",
        # The synchronous siblings of the above, same reasoning: they wrap an argv
        # they are handed and cannot route on its behalf.
        "sandbox_rlimit.py::run_limited",
        "sandbox_rlimit.py::popen_limited",
        # The AppArmor profile installer. All three spawn FIXED, operator-facing
        # tooling with no agent-influenced input: `apparmor_parser --version`,
        # `apparmor_parser -Q --skip-cache <temp profile this module generated>`.
        # (The aa-exec enforcement check is NOT here: it must run under sudo, so
        # it goes through the caller's privileged runner rather than spawning.)
        # The binaries are resolved with shutil.which (never a caller-supplied PATH),
        # the only variable argument is a tempfile path this module just wrote,
        # and the whole flow runs from `kirocrew service install` on a TTY, not
        # from an agent turn. Sandboxing them would also be circular: their
        # purpose is to make the sandbox constructible in the first place.
        "service/apparmor.py::parser_version",
        "service/apparmor.py::validate",
        "service/linux_unit.py::_current_group",
        # Read-only diagnostic: `loginctl show-user <user> -p Linger`, with
        # `Linger=` parsed from the Key=value output (no `--value`), a
        # fixed argv whose only variable is the service account name taken from
        # $USER/$LOGNAME (never agent-supplied). Same class as
        # service/linux_unit.py::_current_group — an identity/state query the install
        # makes to warn the operator when runtimes would die at logout. No shell,
        # no agent-influenced argument, nothing written.
        "service/linux_unit.py::_linger_enabled",
        "service/linux_systemctl.py::_sudo_run",
        "service/linux_systemctl.py::_systemctl",
        "service/linux_install.py::_write_unit_via_sudo",
        "service/macos.py::_launchctl",
        "session_pid.py::kill_orphan_mcps",
        "session_pid_orphan_scan.py::_our_orphan_pids",
        "session_pid_orphan_scan.py::find_orphan_mcp_candidates",
        # The abandoned-agent-scope reaper's two systemctl calls are gateway
        # maintenance against this install's own units, not agent work. The binary
        # comes only from platform_compat.trusted_system_bin("systemctl")'s fixed
        # system directories; a miss returns without spawning, so PATH and agent
        # input cannot choose it. Argv is fixed `--user show` / `--user stop`
        # apart from a unit name enumerated from this install's own per-instance
        # `kirocrew-agents-<token>.slice` cgroup tree. The unit is one separate
        # list-argv element, shell is never enabled, and no cwd is passed.
        "session_scope_reap.py::_scope_active_enter_us",
        "session_scope_reap.py::_systemctl_stop",
        # The stale-asset watchdog's re-entry check reads the gateway's own unit:
        # `systemctl [--user] show -p <fixed properties> kirocrew.service`, a fixed
        # argv with no agent input, bounded by a timeout. The binary comes only from
        # platform_compat.trusted_system_bin("systemctl"); a miss is inconclusive
        # and spawns nothing, so neither PATH nor an agent can choose it.
        "gateway_restart.py::_systemd_show",
        "slack/gateway_runtime/update_apply.py::_auto_apply_update",
        # The `git status --porcelain` and `git diff --diff-filter=A` spawns,
        # moved out of _auto_apply_update into its nested pre-check so every
        # refusal also runs before the memory copy. Same trusted git binary
        # (trusted_git_bin, never PATH), same git_command_env, fixed argv apart
        # from the rev-parse'd target OID, cwd the configured checkout.
        "slack/gateway_runtime/update_apply.py::_refused_before_reset",
        # Pluggable update provider: CommandProvider runs operator-configured
        # shell commands from security_policy.json or config.json (sensitive
        # home dirs the agent cannot write). The check command probes for a
        # newer version; the apply command performs the update. Both are
        # operator-authored, not agent-influenced. NOT sandbox-routed because
        # the command must reach the host's package manager / registry.
        "platform/update_provider.py::check",
        "platform/update_provider.py::apply",
        "slack/gateway_runtime/install_repair.py::_check_missing_deps",
        # The kiro-cli version probe. Fixed argv ("kiro-cli --version"), no
        # agent-influenced input; sandboxing the probe would be circular for the
        # same reason as the other boot-time self-checks above.
        "slack/gateway_runtime/install_repair.py::_warn_if_kiro_cli_outdated",
        # Only spawn_feature_gateway calls this helper, for initial boot and
        # restart. It fixes sys.executable -m kiro_crew gateway --test-mode;
        # no executable/argv parameter exists. Home/env come from the isolated
        # test supervisor, never HTTP/MCP requests. Dynamic projected MCP
        # commands in workflow_memory_scenario instead use sandboxed_spawn_argv.
        "testing/harness.py::_launch_gateway",
        # before_spawn still seeds via sys.executable -c with a literal seed
        # program and the test's fixture name as data, never interpolated code.
        # harness_environment pins the owned temporary home and checkout src;
        # seed keeps its fixture-containment/main-home/nonempty guards and the
        # call has timeout=30. No HTTP/MCP-selected command reaches this site.
        "testing/harness.py::spawn_feature_gateway",
        # Apple on-device speech (macOS only). None of these takes an agent-authored
        # command: the argv is a fixed toolchain path or the helper Kiro Crew itself
        # compiled. `_build_helper` runs swiftc over a file that ships inside the
        # package, writing to the data home's `run/` dir (sensitive-path fenced,
        # 0700). The three spawns that EXECUTE the compiled helper
        # (`transcribe`, `inventory`, `StreamingSession.start`) now route through
        # `sandbox.sandboxed_spawn_argv(mode="strict")` via `_sandboxed`, so they are
        # wrapped rather than merely declared; `strict` was verified to leave batch,
        # inventory and streaming all working. `_swiftc` and `_sdk_path` spawn only `/usr/bin/xcrun` with a
        # fixed flag and no agent input; both pass `env=_build_env()`, which strips
        # `DEVELOPER_DIR`/`SDKROOT`/`TOOLCHAINS`/`SWIFT_EXEC` and pins PATH, and both
        # trust-check the returned path via `_is_trusted_toolchain` before it is used
        # — so a redirected toolchain is refused rather than compiled with.
        "apple_speech/__init__.py::_build_helper",
        "apple_speech/__init__.py::_sdk_path",
        "apple_speech/__init__.py::_swiftc",
        "apple_speech/__init__.py::_to_native_audio",
        "apple_speech/__init__.py::inventory",
        "apple_speech/__init__.py::start",
        "apple_speech/__init__.py::transcribe",
        # (`transcribe.py::_python3_bin_dir` is absent: its scripts-dir probe
        # routes through `dep_sync.py::_probe_interpreter`, so an entry here
        # would be stale.)
        # Every runtime audio conversion now converges here so the authenticated
        # bundled image stays bound until spawn. The executable is either that
        # digest-verified image or a fixed-directory system candidate; the three
        # current callers pass fixed ffmpeg flags and positional audio/temp paths,
        # never a shell, custom cwd, or agent-controlled environment. A hostile path
        # can only name a bad input, not a second command. `_SPAWN_NAMES` propagates
        # this audit through the generic helper, so each caller remains independently
        # reviewed and a future caller fails the gate until it is classified.
        "transcribe_ffmpeg.py::_create_ffmpeg_subprocess",
        # The macOS authenticity oracle for the bundled ffmpeg: spawns the
        # absolute /usr/bin/codesign (never PATH) with constant flags and a
        # requirement string built from a module-level team-ID constant. The
        # only variable argument is the path of the process-private snapshot
        # (`/proc/self/fd/N`-style descriptor or the 0o500 snapshot dir this
        # module itself just wrote and digest-verified) — no agent influence
        # over command, args, cwd, or env. It cannot route through
        # sandboxed_spawn_argv: codesign must read the system trust store and
        # evaluate the Apple certificate chain, which the OS sandbox denies.
        "transcribe_ffmpeg.py::_macos_developer_id_authentic",
        # NOT a subprocess spawn: ``asyncio.run`` drives a coroutine; no process is spawned.
        "speech_providers.py::_run",
        # `_pcm_via_ffmpeg` decodes a container the stdlib cannot read (a Slack
        # voice memo's ogg/Opus, an uploaded m4a) down to the 16 kHz mono PCM the
        # recogniser takes. Fixed argv, ffmpeg only; the sole variable part is a
        # positional audio path that `_is_sensitive_audio_path` has already
        # cleared, so a hostile value can only name a bad file, not a command.
        # (`audio_exceeds_secs`, the meetings import route's duration probe, is
        # the same ffmpeg on the same class of path and routes through
        # `_create_ffmpeg_subprocess` above — `_SPAWN_NAMES` propagates the audit
        # to each caller, so it is classified here like the other three: a null
        # decode with fixed flags, `-t` bounded at the duration cap, no output
        # file at all (`-f null -`), and the only variable argv element a
        # positional path that `_vet_audio_file` validated and the route then
        # snapshot-copied via `pinned_fs` into its own 0700 directory.)
        "transcribe_local.py::_pcm_via_ffmpeg",
        "transcribe.py::audio_exceeds_secs",
        # The two spawns that split an over-cap recording for the meetings import
        # route, both ffmpeg-only through `_create_ffmpeg_subprocess` above, so
        # `_SPAWN_NAMES` propagates the same audit and both are classified here.
        # `_detect_silence_ends` is a null decode (`-f null -`) with the fixed
        # `silencedetect` filter and no output file; `_extract_segment` decodes one
        # `-ss`/`-t` span to a 16 kHz mono WAV in the route's own 0700 snapshot dir.
        # In both the only variable argv element is the positional input, which the
        # route hands in as the process-private pinned descriptor path
        # (`/dev/fd/N`) of the snapshot it already validated and copied — a hostile
        # value can only name a bad input, never a second command, and the output
        # path (`_extract_segment`) is a gateway-chosen name under the snapshot dir,
        # not agent input.
        "transcribe.py::_detect_silence_ends",
        "transcribe.py::_extract_segment",
        "transcribe_aws.py::_transcribe_aws",
        # The build probe executes the same authenticated image with the single
        # fixed `-version` argument; it accepts no external input at all. Both
        # streams are CAPTURED rather than discarded, so a refusal can name itself
        # in the build log, and are decoded with errors="replace" because a loader
        # complaint arrives in the host's console encoding.
        "transcribe_ffmpeg.py::_packaged_ffmpeg_version_probe",
        # JSON-Schema ``pattern`` validation for MCP app→gateway tool-call args
        # (validate_mcp_tool_arguments). The spawn's command surface is FULLY
        # fixed and NOT agent-selectable: binary is our own ``sys.executable``,
        # argv is the constant ``-I -c <_PATTERN_CHILD_SRC>`` (``-I`` = isolated
        # mode: no env, no user site, no PYTHON* vars), cwd is inherited (never
        # set from input). The only agent/server-influenced values — the regex
        # ``pattern`` (from the server's declared inputSchema) and the ``value``
        # (from the app) — are passed as a JSON **stdin** body, never as argv,
        # and the child does nothing but ``re.search(p, v)`` then exits with a
        # status code. It cannot exec a shell, import beyond re/json/sys, or run
        # agent code. The subprocess exists SOLELY so a catastrophic-backtrack
        # (ReDoS) pattern can be hard-KILLED on wall-clock timeout (an in-process
        # thread cannot be stopped — it holds the GIL for the whole match); that
        # ``subprocess.run(timeout=...)`` kill is the DoS bound, plus the pattern
        # and value are size-capped before the spawn. Fixed argv + isolated
        # interpreter + stdin-only data + killed on timeout ⇒ benign, not routed.
        "validation_parts/json_schema_args.py::_bounded_pattern_search",
        "voice_reply.py::stitch_mp3s",
        # Enumerating the host speech engine's voices. The whole argv is fixed
        # by this module: the binary comes from ``trusted_system_bin`` (a system
        # directory, resolved WITHOUT consulting PATH, so a shim in an
        # agent-writable directory cannot be reached), and the arguments are
        # module constants — ``-v ?`` for ``say``, ``--voices`` for
        # ``espeak-ng``, or a constant base64 ``-EncodedCommand`` for Windows
        # PowerShell. Nothing from the agent, the model, or user config enters
        # the command, no stdin is written, and the output is only parsed.
        # It stands on those properties ALONE, and deliberately not by analogy to
        # the synthesis path: that path always goes through
        # ``sandboxed_spawn_argv_async``, claiming the first-party carve-out only
        # on the SAPI branch, so citing it here would teach the next reader an
        # invariant the tree does not have. What makes this probe benign is that
        # it parses no attacker-supplied text — synthesis parses a reply the model
        # wrote, which is exactly why synthesis is confined and this is not.
        # Fixed argv + trusted-directory binary + read-only output ⇒ benign, not
        # routed.
        "voice_reply.py::list_system_voices",
    }
)
