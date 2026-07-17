"""Subprocess-spawn audit — Talos finding 92e24570 (V2287169889).

Every subprocess spawn in ``src/kiro_crew`` must be either

* routed through the sandbox chokepoint (its enclosing function calls
  ``sandboxed_spawn_argv`` or ``wrap_argv``), so the spawned process gets
  OS-level filesystem isolation and a credential-scrubbed environment, or
* explicitly listed in ``BENIGN_SPAWNS`` below as a spawn whose command,
  arguments, and working directory are NOT agent-influenced.

This test is a regression tripwire: adding a NEW unrouted spawn makes it fail
until the author either routes the spawn through the chokepoint or, having
confirmed the command is not agent-influenced, adds its ``file::function`` key
to ``BENIGN_SPAWNS`` with a justification. This is the "lint or unit test
asserting every subprocess spawn is either allow-listed as benign or routed
through that wrapper" the finding asks for.

The finding named three agent-influenced sites — the MCP server probe
(``mcp_discovery.probe_server``), the TaskRunner test command
(``task_executor.run_tests``), and TaskRunner git operations
(``git_coord._git`` / ``_is_git_repo``) — which are now routed through
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
# routed through the sandbox chokepoint.
_ROUTED_TOKENS = ("sandboxed_spawn_argv", "wrap_argv")

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
        "agent.py::is_aim_package_installed",
        "apps/backend.py::_proc_start_time",
        "apps/backend.py::_resolve_nvm_path",
        "apps/backend.py::stop_app_backend",
        "apps/builtins/code_review_sage/tests/test_review_pool.py::test_worker_exposes_live_pid_for_shielding",
        "apps/builtins/workflows/server.py::handle_run",
        "apps/dependencies.py::_run_aim",
        "cli.py::_consolidate_cmd",
        "cli.py::_ensure_node",
        "cli.py::_node_ok",
        "cli.py::main",
        "cli_chat.py::_tui",
        "cli_doctor.py::_detect_docker_ollama",
        "cli_doctor.py::_doctor",
        "cli_doctor.py::_doctor_mcp_tools",
        "cli_doctor.py::_doctor_ollama_install",
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
        "dashboard/handlers/_shared.py::_aim_list_stdout",
        "dashboard/handlers/agents.py::_run_aim",
        "dashboard/handlers/core.py::_is_apple_silicon",
        "dashboard/handlers/core.py::_stt_prereq_commands",
        "dashboard/handlers/core.py::_unusable",
        "dashboard/handlers/core.py::api_stt_install",
        "dashboard/handlers/files.py::_run",
        "dashboard/handlers/files.py::api_reveal_path",
        "dashboard/handlers/files.py::api_screenshot",
        "dashboard/handlers/files.py::api_upload",
        "dashboard/handlers/knowledge.py::_run_folder_dialog",
        "dashboard/handlers/mcp.py::api_mcp_remove",
        "dashboard/handlers/terminal.py::api_terminal_ws",
        "dashboard/handlers/updates.py::_apply",
        "dashboard/handlers/updates.py::_do_update_check",
        "dashboard/handlers/updates.py::_venv_pip_install",
        "dashboard/handlers/updates.py::api_update_apply",
        "dashboard/handlers_system.py::_collect_system_metrics",
        "dashboard/handlers_system.py::_get_static_system_info",
        "dashboard/port_reclaim.py::_listeners_on_port",
        "embeddings.py::_install_docker_ollama",
        "embeddings.py::_run_docker",
        "embeddings.py::_sudo_docker",
        "embeddings.py::install_ollama",
        "embeddings.py::pull_model",
        "embeddings.py::start_server",
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
        "voice_reply.py::stitch_mp3s",
    }
)


def _collect_unrouted_spawns() -> set[str]:
    """Return ``<relpath>::<func>`` for every spawn whose enclosing function
    does NOT reference the sandbox chokepoint."""
    unrouted: set[str] = set()
    for path in _SRC_ROOT.rglob("*.py"):
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
            routed = False
            if enc_node is not None:
                fsrc = "\n".join(
                    lines[enc_node.lineno - 1 : (enc_node.end_lineno or enc_node.lineno)]
                )
                routed = any(tok in fsrc for tok in _ROUTED_TOKENS)
            if not routed:
                unrouted.add(f"{rel}::{enc}")
    return unrouted


def test_every_spawn_is_routed_or_allowlisted():
    """No spawn may be unrouted-and-unlisted (Talos 92e24570 tripwire)."""
    unrouted = _collect_unrouted_spawns()
    unexpected = unrouted - BENIGN_SPAWNS
    assert not unexpected, (
        "New unrouted subprocess spawn(s) found in src/kiro_crew:\n  "
        + "\n  ".join(sorted(unexpected))
        + "\n\nRoute agent-influenced spawns through "
        "kiro_crew.sandbox.sandboxed_spawn_argv (OS sandbox + scrubbed env), "
        "or, if the command/args/cwd are NOT agent-influenced, add the "
        "file::function key to BENIGN_SPAWNS in this test with a justification. "
        "See Talos finding 92e24570 / V2287169889."
    )


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
    """The three sites the finding names must stay routed through the sandbox."""
    unrouted = _collect_unrouted_spawns()
    for key in (
        "mcp_discovery.py::probe_server",
        "task_executor.py::run_tests",
        "git_coord.py::_git",
        "git_coord.py::_is_git_repo",
    ):
        assert key not in unrouted, (
            f"{key} must route its spawn through sandboxed_spawn_argv "
            "(Talos 92e24570) but is no longer sandbox-wrapped."
        )
