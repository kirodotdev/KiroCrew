"""Subprocess-spawn audit — security-review finding 92e24570.

Every subprocess spawn in ``src/kiro_crew`` must be either

* routed through the sandbox chokepoint (its enclosing function calls
  ``sandboxed_spawn_argv``, ``wrap_argv``, or the regression-pinned async
  adapter around ``sandboxed_spawn_argv``), so the spawned process gets
  OS-level filesystem isolation and a credential-scrubbed environment, or
* explicitly listed in ``BENIGN_SPAWNS`` below as a spawn whose command,
  arguments, and working directory are NOT agent-influenced.

This test is a regression tripwire: adding a NEW unrouted spawn makes it fail
until the author either routes the spawn through the chokepoint or, having
confirmed the command is not agent-influenced, adds its ``file::function`` key
to ``BENIGN_SPAWNS`` with a justification. This is the "lint or unit test
asserting every subprocess spawn is either allow-listed as benign or routed
through that wrapper" the finding asks for.

The agent-influenced sites — the MCP server probe
(``mcp_discovery.probe_server``), the TaskRunner test command
(``task_executor.run_tests``), TaskRunner git operations
(``git_coord._git`` / ``_is_git_repo``), and authenticated source-provider
fetches (``source_providers._run_json``) — are routed through
``sandboxed_spawn_argv`` and MUST stay routed (see
``test_agent_influenced_sites_are_routed``).

The remaining unrouted spawns below are pre-existing and fall into these
groups, none of which is the finding's agent-influenced-spawn vector:

* Operator-invoked CLI / setup / doctor / self-update (fixed argv against our
  own install: git pull, pip, npm, kiro-cli/kirocrew update,
  systemctl/launchctl, node/ollama bootstrap).
* Internal process management (read our own ppid; enumerate/kill our own
  managed/orphaned processes) and system-metrics probes (fixed sysctl/ps/etc).
* Trusted-side gateway/MCP-backend spawns (``mcp_gateway`` — MCP backends sit
  on the trusted side of the sandbox boundary by design) and the Playwright
  proxy the finding explicitly excludes (inherits the already-sandboxed
  kiro-cli parent).
* Operator-configured state sync (``sync/*`` — git/s3/rsync/litestream
  push/pull against an operator-set remote) and app-registry package install
  of an operator-installed package.

FOLLOW-UP HARDENING CANDIDATES (defense-in-depth, NOT this finding, tracked for
a later pass — they are allowlisted here because their repo/remote is
operator-configured rather than agent-selected in the finding's sense):
``apps/builtins/code_reviewer/git.py`` git against a locally-checked-out CR
repo, and ``sync/*`` push/pull. Routing these would also need their real-git
unit tests to tolerate the sandbox wrapper.
"""

from __future__ import annotations

import ast
import functools
from pathlib import Path

_SRC_ROOT = Path(__file__).resolve().parent.parent / "src" / "kiro_crew"

# Attribute names that actually spawn a child process.
_SPAWN_ATTRS = {
    "Popen",
    "run",
    "call",
    "check_output",
    "check_call",
    "create_subprocess_exec",
    "create_subprocess_shell",
}
# Only calls whose receiver is one of these modules count (excludes e.g.
# ``proc.communicate`` or ``pool.run``).
_SPAWN_BASES = {"subprocess", "asyncio"}

# Tokens whose presence anywhere in the enclosing function marks the spawn as
# routed through the sandbox chokepoint. ``_prepare_sandboxed_spawn`` is the
# prerequisite flow's async adapter; the dedicated regression test below pins
# it to ``sandboxed_spawn_argv`` so this indirection cannot weaken the gate.
_ROUTED_TOKENS = (
    "sandboxed_spawn_argv",
    "wrap_argv",
    "_prepare_sandboxed_spawn",
)

# Token marking a routed function as also applying a kernel resource ceiling
# (RLIMIT_NPROC/NOFILE/CPU/AS) to its child via ``preexec_fn`` — the second
# layer of the spawn guarantee (security-review bdf0d7e5). Every
# sandbox-routed function must reference it: the sandbox gives the child
# filesystem + credential isolation, this gives it a fork-bomb / resource
# ceiling. Functions whose ONLY spawns are fixed-argv internal probes (no
# agent-influenced child) are exempted in ``PREEXEC_EXEMPT`` below.
_PREEXEC_TOKENS = ("resource_limit_preexec", "session_host_preexec")

# Routed functions exempt from the resource-limit requirement: the enclosing
# function is sandbox-routed (so it appears routed) but the specific spawn is a
# fixed-argv internal probe against our own process/host, not a child running
# agent-influenced code — a resource ceiling adds nothing. Keyed by
# ``<relpath>::<function>`` with a justification, same discipline as
# ``BENIGN_SPAWNS``.
PREEXEC_EXEMPT: frozenset[str] = frozenset(set())

# Benign spawns: command/args/cwd are fixed or operator-controlled, NOT
# influenced by the agent, a hostile MCP-config entry, or an agent-selected
# repository. Keyed by ``<relpath>::<enclosing function>``. When adding an
# entry, confirm none of the argv, the cwd, or the resolved binary can be
# steered by the LLM/agent before listing it. See the module docstring for the
# category breakdown and follow-up hardening candidates.
BENIGN_SPAWNS: frozenset[str] = frozenset(
    {
        "acp/runtime.py::_get_rss_mb",
        "acp/runtime.py::_get_rss_tree_mb",
        "apps/backend.py::_proc_start_time",
        "apps/backend.py::_resolve_nvm_path",
        "apps/backend.py::stop_app_backend",
        # gh-CLI open-PR enumeration: fixed `gh api` list-argv (no shell=True);
        # owner/repo are validated to ^[A-Za-z0-9._-]+$ by adapters.parse_repo_url
        # and only fill the API path (bounded to api.github.com). NOT sandboxed
        # because gh needs the host's own authenticated credentials.
        "apps/builtins/code_review_sage/sage_lib/pipeline.py::list_open_prs",
        # Issue Radar GitHub access — same rationale as list_open_prs above.
        # ALL gh calls funnel through ONE chokepoint, _gh_run: a fixed `gh api`
        # list-argv (never shell=True). gh supplies the host's OWN authenticated
        # token, so it CANNOT be sandbox-routed (the sandbox would hide
        # ~/.config/gh + the keychain, breaking auth). As defense-in-depth WITHIN
        # this benign classification, _gh_run resolves a trusted canonical `gh`
        # (never a shim on the agent-writable front of PATH) and passes a MINIMAL
        # env (PATH/HOME/XDG + gh's own auth/network vars), so unrelated secrets
        # (AWS/Slack/SSH) never reach the child. The only agent-reachable inputs:
        #   • owner/repo — validated to ^[A-Za-z0-9._-]+$ + a github.com host
        #     allowlist by github_client.parse_github_repo_url at /connect, and
        #     read routes additionally gate on store.is_repo_connected, so only
        #     an already-validated pair ever reaches the argv;
        #   • the issue number — coerced via int() before it reaches the path;
        #   • write bodies (label names / state reasons) — sent as a JSON stdin
        #     body (--input -), never argv; the DELETE label name is URL-encoded
        #     into the path.
        # The jq filters are hardcoded module constants, and `gh api` is bounded
        # to api.github.com, so no binary/cwd/host is agent-selected.
        "apps/builtins/issue_radar/backend/github_client.py::_gh_run",
        # Issue Radar GitLab access — the glab counterpart of _gh_run, and benign
        # for the same reasons, with ONE extra agent-reachable input that gh does
        # not have: the HOST.
        # ALL glab calls funnel through ONE chokepoint, _glab_run: a fixed
        # `glab api` list-argv (never shell=True). glab supplies the host's OWN
        # authenticated session, so it CANNOT be sandbox-routed (the sandbox would
        # hide ~/.config/glab + the keychain, breaking auth). As defense-in-depth
        # WITHIN this benign classification, _glab_run resolves glab through the
        # shared provider policy (refusing a binary owned by another user, a
        # world-writable one, or one inside the agent-writable project tree) and
        # passes a MINIMAL env, so unrelated secrets never reach the child.
        # The agent-reachable inputs:
        #   • the HOST — the one input with no gh analogue, and the reason this
        #     entry is not simply "same as gh". It is re-authorized against the
        #     operator's dashboard.gitlab_hosts allowlist INSIDE _glab_run on
        #     every call (not just at /connect), is REQUIRED rather than
        #     defaulted so a forgotten argument fails loudly instead of silently
        #     targeting gitlab.com, and is pinned into the child's GITLAB_HOST so
        #     a self-managed default in glab's own config cannot redirect a bare
        #     API path to another instance. The ambient GITLAB_TOKEN is withheld
        #     for any non-gitlab.com host, so a gitlab.com credential cannot be
        #     sent to a private server;
        #   • owner/repo (the project namespace) — charset-validated per segment
        #     by gitlab_client.parse_gitlab_repo_url at /connect, then URL-encoded
        #     into GitLab's single :id path parameter; read routes additionally
        #     gate on store.is_repo_connected, which matches on provider+host too;
        #   • the issue / merge-request iid — coerced via int() before the path;
        #   • write bodies (label names / state events) — sent as a JSON stdin
        #     body (--input -), never argv.
        # No binary or cwd is agent-selected.
        "apps/builtins/issue_radar/backend/gitlab_client.py::_glab_run",
        "apps/builtins/workflows/server.py::handle_run",
        # _start_run's worker spawns argv that is ALWAYS pre-wrapped by its
        # callers through sandboxed_spawn_argv (sync wraps each step with
        # per-step modes; provision wraps the pod CLI argv) and the spawn
        # carries resource_limit_preexec() — routing again here would nest
        # sandboxes. The chokepoint is applied at the call sites.
        "apps/builtins/dev_fleet/server.py::worker",
        # Dev Fleet builtin backend: async version routes all git/gh through
        # _run_cmd which calls sandboxed_spawn_argv (the chokepoint). Only
        # _resolve_primary_checkout uses subprocess.run directly (one-shot
        # git rev-parse at startup, no agent input, no sandbox needed).
        "apps/builtins/dev_fleet/server.py::_resolve_primary_checkout",
        "apps/builtins/dev_fleet/server.py::worker",
        "apps/dependencies.py::_run_aim",
        "cli.py::_consolidate_cmd",
        "cli.py::_ensure_node",
        "cli.py::_node_ok",
        "cli.py::main",
        "cli_chat.py::_tui",
        # NOT a subprocess spawn: the AST heuristic matches ``asyncio.run`` (attr
        # ``run`` on base ``asyncio``), here used only to drive the now-async
        # ``deregister_app_crons_from_service`` coroutine from the loop-less CLI
        # disable/uninstall path. No child process is created; the sole input is
        # the operator-typed app name. Same classification as the other
        # ``asyncio.run`` sites below (cli_doctor.py::_doctor, workflows
        # server.py::handle_run).
        "cli_commands.py::_cleanup_app_crons_from_scheduler",
        "cli_doctor.py::_doctor",
        "cli_doctor.py::_doctor_mcp_tools",
        "cli_server.py::_logs_cmd",
        "cli_server.py::_spawn_detached_gateway",
        "cli_server.py::_update",
        "cli_setup.py::_setup_electron",
        "cloud/source.py::_git_tracked_files",
        "cloud/source.py::_tracked_tree_is_dirty",
        "cloud/source.py::_use_git_archive",
        "cloud/ssm.py::_run_install_command",
        "cloud/ssm.py::open_port_forward",
        "dashboard/chat_voice.py::api_voice_voices",
        "dashboard/handlers/core.py::_is_apple_silicon",
        "dashboard/handlers/core.py::_stt_prereq_commands",
        "dashboard/handlers/core.py::_unusable",
        "dashboard/handlers/core.py::api_stt_install",
        "dashboard/handlers/files.py::_run",
        "dashboard/handlers/files.py::api_reveal_path",
        "dashboard/handlers/files.py::api_screenshot",
        "dashboard/handlers/files.py::api_upload",
        "dashboard/handlers/knowledge.py::_run_folder_dialog",
        # Terminal live-cwd probe on hosts without /proc (macOS/BSD): fixed
        # `lsof -a -p <pid> -d cwd -Fn` list-argv (no shell=True) where <pid>
        # is the gateway's own PTY child pid (an int from asyncio.subprocess),
        # never agent input. Read-only introspection of our own process tree;
        # sandboxing would break lsof's access to host process state.
        "dashboard/handlers/terminal.py::_proc_cwd",
        "dashboard/handlers/terminal.py::api_terminal_ws",
        "dashboard/handlers/updates.py::_apply",
        "dashboard/handlers/updates.py::_do_update_check",
        "dashboard/handlers/updates.py::_venv_pip_install",
        "dashboard/handlers/updates.py::api_update_apply",
        "dashboard/handlers_system.py::_collect_system_metrics",
        "dashboard/handlers_system.py::_get_static_system_info",
        "dashboard/port_reclaim.py::_listeners_on_port",
        "env.py::_run",
        "env.py::activate_mise",
        "frontend.py::build_frontend_async",
        "frontend.py::build_frontend_sync",
        "instances/diagnostics.py::_run_ok",
        "instances/diagnostics.py::_run_stdout",
        "instances/ssh_tunnel_manager.py::_ps_lines",
        "instances/ssh_tunnel_manager.py::start",
        "instances/token_mint.py::mint_remote_token",
        "instances/token_mint.py::run_remote_kirocrew",
        "mcp_caller.py::_parent_pid",
        "mcp_core.py::_get_ppid",
        "mcp_discovery.py::sync_to_agent_config",
        "mcp_gateway/backend.py::spawn_backend",
        "mcp_gateway/gatewayd.py::main",
        "mcp_gateway/manager.py::_spawn_once",
        "mcp_gateway/stub.py::main",
        "mcp_playwright_proxy.py::run_proxy",
        "mcp_shared.py::_get_ppid",
        "platform_compat.py::_current_user_sid",
        "platform_compat.py::find_listening_pids",
        "platform_compat.py::find_python_interpreter",
        "platform_compat.py::kill_pid",
        "platform_compat.py::kill_process_tree",
        "platform_compat.py::process_command_line",
        # Same class as process_command_line: a read-only process-attribute query
        # (``ps -o uid=`` / ``/proc/<pid>`` stat) in the platform leaf module,
        # with a fixed argv containing only an int-coerced pid. It cannot route
        # through the sandbox helper because sandbox imports platform_compat.
        "platform_compat.py::process_owner_uid",
        "platform_compat.py::process_matches",
        "platform_compat.py::restrict_to_owner",
        "pod/cli.py::_logs",
        "pod/provision.py::_run",
        "pod/runtime.py::_git_worktrees",
        "pod/runtime.py::_run",
        "pod/runtime.py::derive_port",
        "pod/runtime.py::recent_journal",
        "sandbox.py::_probe_sandbox_exec",
        "sandbox.py::_ssh_supports_accept_new",
        "service/linux.py::_current_group",
        "service/linux.py::_sudo_run",
        "service/linux.py::_systemctl",
        "service/linux.py::_write_unit_via_sudo",
        "service/macos.py::_launchctl",
        "session_pid.py::_our_orphan_pids",
        "session_pid.py::find_orphan_mcp_candidates",
        "session_pid.py::kill_orphan_mcps",
        "slack/gateway.py::_auto_apply_update",
        "slack/gateway.py::_check_missing_deps",
        "slack/gateway.py::_init_services",
        "testing/harness.py::spawn_feature_gateway",
        "transcribe.py::_python3_bin_dir",
        "transcribe.py::_run_whisper_cli",
        "transcribe.py::_transcribe_aws",
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
        "validation.py::_bounded_pattern_search",
        "voice_reply.py::stitch_mp3s",
    }
)


@functools.lru_cache(maxsize=1)
def _collect_spawn_functions() -> dict[str, str]:
    """Map ``<relpath>::<func>`` -> the enclosing function's source, for every
    function containing a subprocess spawn. ``<module>`` marks a module-level
    spawn (no enclosing function).

    Cached: all six audit tests derive from this one rglob+ast.parse scan of
    the whole source tree (~2s), so re-scanning per test multiplies pure
    duplicated wall-clock. The source tree cannot change mid-run and callers
    only read the mapping, so a shared instance is safe.
    """
    out: dict[str, str] = {}
    for path in _SRC_ROOT.rglob("*.py"):
        # ``builtin_skills/**`` are bundled skill helper scripts the AGENT runs
        # in the USER's repo/shell (e.g. git/gh in prepare-pr's scripts), not
        # gateway runtime code paths. The gateway never imports or spawns them;
        # they ship under the package only for packaging. The sandbox spawn
        # chokepoint governs the gateway's own subprocess usage, so these assets
        # are out of scope for this audit.
        if "builtin_skills" in path.relative_to(_SRC_ROOT).parts:
            continue
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, str(path))
        funcs = [
            n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        lines = source.splitlines()
        rel = path.relative_to(_SRC_ROOT).as_posix()
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in _SPAWN_ATTRS
            ):
                continue
            base = node.func.value
            base_name = (
                base.id
                if isinstance(base, ast.Name)
                else base.attr if isinstance(base, ast.Attribute) else ""
            )
            if base_name not in _SPAWN_BASES:
                continue
            enc = "<module>"
            enc_node: ast.AST | None = None
            best = -1
            for f in funcs:
                if f.lineno <= node.lineno <= (f.end_lineno or f.lineno) and f.lineno > best:
                    best = f.lineno
                    enc = f.name
                    enc_node = f
            fsrc = (
                "\n".join(lines[enc_node.lineno - 1 : (enc_node.end_lineno or enc_node.lineno)])
                if enc_node is not None
                else ""
            )
            out[f"{rel}::{enc}"] = fsrc
    return out


def _collect_unrouted_spawns() -> set[str]:
    """Return ``<relpath>::<func>`` for every spawn whose enclosing function
    does NOT reference the sandbox chokepoint."""
    return {
        key
        for key, fsrc in _collect_spawn_functions().items()
        if not any(tok in fsrc for tok in _ROUTED_TOKENS)
    }


def _collect_routed_spawns_without_preexec() -> set[str]:
    """Return ``<relpath>::<func>`` for every sandbox-routed spawn function that
    does NOT also apply the resource-limit ``preexec_fn``."""
    return {
        key
        for key, fsrc in _collect_spawn_functions().items()
        if any(tok in fsrc for tok in _ROUTED_TOKENS)
        and not any(tok in fsrc for tok in _PREEXEC_TOKENS)
    }


# A routed spawn function applies the cgroup v2 DoS ceiling either directly
# (``cgroup_scope_argv``) or via the ``sandboxed_spawn_argv`` chokepoint, which
# wraps every routed argv in the scope internally.
_CGROUP_TOKENS = (
    "cgroup_scope_argv",
    "sandboxed_spawn_argv",
    "_prepare_sandboxed_spawn",
)


def _collect_routed_spawns_without_cgroup() -> set[str]:
    """Return ``<relpath>::<func>`` for every sandbox-routed spawn function that
    does NOT also apply the cgroup v2 scope (pids.max / memory.max)."""
    return {
        key
        for key, fsrc in _collect_spawn_functions().items()
        if any(tok in fsrc for tok in _ROUTED_TOKENS)
        and not any(tok in fsrc for tok in _CGROUP_TOKENS)
    }


def test_every_spawn_is_routed_or_allowlisted():
    """No spawn may be unrouted-and-unlisted (security-review 92e24570 tripwire)."""
    unrouted = _collect_unrouted_spawns()
    unexpected = unrouted - BENIGN_SPAWNS
    assert not unexpected, (
        "New unrouted subprocess spawn(s) found in src/kiro_crew:\n  "
        + "\n  ".join(sorted(unexpected))
        + "\n\nRoute agent-influenced spawns through "
        "kiro_crew.sandbox.sandboxed_spawn_argv (OS sandbox + scrubbed env), "
        "or, if the command/args/cwd are NOT agent-influenced, add the "
        "file::function key to BENIGN_SPAWNS in this test with a justification. "
        "See security-review finding 92e24570."
    )


def test_prerequisite_async_adapter_keeps_sandbox_chokepoint():
    """The off-loop prerequisite adapter must remain a thin sandbox wrapper."""

    path = _SRC_ROOT / "kiro_prerequisite.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, str(path))
    adapter = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_prepare_sandboxed_spawn"
    )
    adapter_source = ast.get_source_segment(source, adapter) or ""
    assert "asyncio.to_thread" in adapter_source
    assert "sandboxed_spawn_argv" in adapter_source


def test_benign_allowlist_has_no_stale_entries():
    """Every BENIGN_SPAWNS entry must still name a real unrouted spawn, so the
    allowlist cannot silently accumulate dead exemptions (e.g. after a spawn is
    later routed through the chokepoint)."""
    unrouted = _collect_unrouted_spawns()
    stale = BENIGN_SPAWNS - unrouted
    assert not stale, (
        "Stale BENIGN_SPAWNS entries (no longer an unrouted spawn — remove "
        "them or they mask future regressions):\n  " + "\n  ".join(sorted(stale))
    )


def test_agent_influenced_sites_are_routed():
    """Agent-influenced spawns must stay routed through the sandbox."""
    unrouted = _collect_unrouted_spawns()
    for key in (
        "mcp_discovery.py::probe_server",
        "task_executor.py::run_tests",
        "git_coord.py::_git",
        "git_coord.py::_is_git_repo",
        "dashboard/handlers/source_providers.py::_run_json",
    ):
        assert key not in unrouted, (
            f"{key} must route its spawn through sandboxed_spawn_argv "
            "(security-review 92e24570) but is no longer sandbox-wrapped."
        )


def test_every_routed_spawn_applies_resource_limits():
    """Every sandbox-routed spawn must ALSO cap the child's resources.

    The sandbox chokepoint gives a child filesystem + credential isolation; a
    ``preexec_fn`` from ``resource_limit_preexec()`` gives it a kernel-enforced
    ceiling (RLIMIT_NPROC/NOFILE/CPU/AS) so a fork bomb or runaway allocation in
    a compromised tool / MCP server cannot exhaust the host. This is the
    regression tripwire for security-review bdf0d7e5: the helper was merged
    once as dead code (defined, zero callers). If you add a new agent-influenced
    spawn, pass ``preexec_fn=resource_limit_preexec()`` — or, if the spawn is a
    fixed-argv internal probe with no agent-influenced child, add its
    ``file::function`` key to ``PREEXEC_EXEMPT`` with a justification.
    """
    missing = _collect_routed_spawns_without_preexec() - PREEXEC_EXEMPT
    assert not missing, (
        "Sandbox-routed spawn(s) missing a resource-limit preexec_fn:\n  "
        + "\n  ".join(sorted(missing))
        + "\n\nPass preexec_fn=kiro_crew.sandbox.resource_limit_preexec() to the "
        "spawn (kernel RLIMIT ceiling — fork bomb / FD / mem / CPU), or add the "
        "file::function key to PREEXEC_EXEMPT with a justification. "
        "See security-review finding bdf0d7e5."
    )


def test_preexec_exempt_has_no_stale_entries():
    """Every PREEXEC_EXEMPT entry must still name a routed spawn function that
    lacks the preexec token, so the exemption list cannot accumulate dead
    entries that would mask a future regression."""
    routed_missing = _collect_routed_spawns_without_preexec()
    stale = PREEXEC_EXEMPT - routed_missing
    assert not stale, (
        "Stale PREEXEC_EXEMPT entries (no longer a routed spawn lacking the "
        "preexec token — remove them):\n  " + "\n  ".join(sorted(stale))
    )


def test_every_routed_spawn_applies_cgroup_scope():
    """Every sandbox-routed spawn must ALSO be placed in a cgroup v2 scope.

    The RLIMIT preexec caps a single process's FDs; the cgroup scope
    (``cgroup_scope_argv`` → pids.max + memory.max) is the actual default-on
    fork-bomb + memory-DoS ceiling the finding's headline threats require
    (security-review bdf0d7e5). A function satisfies this by calling ``cgroup_scope_argv``
    directly or by routing through ``sandboxed_spawn_argv`` (which applies the
    scope internally). The ``PREEXEC_EXEMPT`` fixed-argv internal probes are
    also exempt here — same rationale (no agent-influenced child to bound).
    """
    missing = _collect_routed_spawns_without_cgroup() - PREEXEC_EXEMPT
    assert not missing, (
        "Sandbox-routed spawn(s) missing a cgroup v2 scope:\n  "
        + "\n  ".join(sorted(missing))
        + "\n\nWrap the final argv with kiro_crew.sandbox.cgroup_scope_argv() "
        "(pids.max + memory.max fork-bomb / memory-DoS ceiling), or route the "
        "spawn through sandboxed_spawn_argv which applies it. "
        "See security-review finding bdf0d7e5."
    )
