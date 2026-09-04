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
  on the trusted side of the sandbox boundary by design) and the Playwright CLI
  toolchain spawns in ``browser_cli`` (fixed argv, operator-triggered install;
  the agent's own browser commands are shell tool calls gated by the approval
  path, not spawns we make).
* Operator-configured state sync (``sync/*`` — git/s3/rsync/litestream
  push/pull against an operator-set remote) and app-registry package install
  of an operator-installed package.

FOLLOW-UP HARDENING CANDIDATES (defense-in-depth, NOT this finding, tracked for
a later pass — they are allowlisted here because their repo/remote is
operator-configured rather than agent-selected in the finding's sense):
``apps/builtins/code_reviewer/git.py`` git against a locally-checked-out CR
repo, and ``sync/*`` push/pull. Routing these would also need their real-git
unit tests to tolerate the sandbox wrapper.

The Design Tweak builtin (``apps/builtins/design_tweak/backend/server.py``)
adds three spawns in the same non-agent-selected categories:
``_lsof_fields`` (fixed-argv ``lsof`` on numeric pids the backend discovered —
a system probe like the sysctl/ps ones above), ``_h_pick_folder`` (fixed-argv
``osascript`` running a hardcoded AppleScript for the macOS folder picker — a
desktop-UI spawn), and ``_start_dev_proc`` (the user's OWN registered project
dev server: cwd is the user-selected project dir and the argv is that project's
package-manager dev script — operator/user-configured, reached only via the
HMAC-signed gateway proxy, not agent-prompt-selected). ``_start_dev_proc`` is
directly analogous to ``code_reviewer/git.py`` and is a follow-up sandbox-routing
candidate; routing a long-lived dev server would need the resource/filesystem
wrapper not to starve it.

OUT OF SCOPE BY SHAPE: a skill's own helper scripts, under ``builtin_skills/**``
or ``apps/builtins/<app>/skills/**`` (see ``_is_bundled_skill_asset``). These are
scripts an agent runs in a shell, gated by the shell approval path, not
subprocesses this package spawns; the gateway neither imports them (pinned by
``test_bundled_skill_assets_are_not_imported``) nor execs them (such an exec
would be a spawn site in a non-exempt file, reviewed there).
"""

from __future__ import annotations

import ast
import functools
from pathlib import Path

_SRC_ROOT = Path(__file__).resolve().parent.parent / "src" / "kiro_crew"


def _is_bundled_skill_asset(path: Path) -> bool:
    """True for a skill's own helper script rather than gateway runtime code.

    Two shapes hold the same kind of thing:

    * ``builtin_skills/**`` -- the top-level bundled skills.
    * ``apps/builtins/<app>/skills/**`` -- skills an app ships with itself
      (dev-fleet's recording and pod-e2e scripts).

    Both are scripts the AGENT runs in the USER's repo/shell, not code the
    gateway imports or spawns; they ship under the package only for packaging,
    and ``test_bundled_skill_assets_are_not_imported`` pins that. The sandbox
    spawn chokepoint governs the gateway's OWN subprocess usage, so these
    assets are out of scope for this audit -- the shell approval path is what
    governs an agent running them, exactly as it governs the agent's own
    browser commands (see ``browser_cli`` in the module docstring).
    """
    parts = path.relative_to(_SRC_ROOT).parts
    if "builtin_skills" in parts:
        return True
    return parts[:2] == ("apps", "builtins") and "skills" in parts[2:]


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

# Spawn helpers called as a BARE NAME rather than ``module.attr`` -- they are
# imported directly, so the receiver check above cannot see them. Without this
# the audit goes blind the moment a call site moves to the wrapper.
_SPAWN_NAMES = {
    "_create_ffmpeg_subprocess",
    "create_subprocess_limited",
    "run_limited",
    "popen_limited",
}

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
# (RLIMIT_NPROC/NOFILE/CPU/AS) to its child — the second layer of the spawn
# guarantee (security-review bdf0d7e5). Every sandbox-routed function must
# reference one: the sandbox gives the child filesystem + credential isolation,
# this gives it a fork-bomb / resource ceiling. Functions whose ONLY spawns are
# fixed-argv internal probes (no agent-influenced child) are exempted in
# ``PREEXEC_EXEMPT`` below.
#
# ``create_subprocess_limited`` (async) and ``run_limited`` / ``popen_limited``
# (sync) are the preferred forms: they deliver the same limits AFTER exec via the
# spawn shim instead of in a fork child of this threaded gateway. The two
# ``*_preexec`` names remain valid for the wrappers' own no-shim fallbacks and
# the terminal's pre-resolved ioctl callback.
#
# Every token is matched as a CALL (trailing paren) rather than a bare name: the
# check scans the enclosing function's raw source, docstrings and comments
# included, so a bare-name match lets prose like "routed through run_limited"
# satisfy the gate while the actual spawn silently reverts to a bare
# ``subprocess.run`` (verified by mutation before the parens were added).
_PREEXEC_TOKENS = (
    "create_subprocess_limited(",
    "run_limited(",
    "popen_limited(",
    "resource_limit_preexec()",
    "session_host_preexec(",
)

# Routed functions exempt from the resource-limit requirement: the enclosing
# function is sandbox-routed (so it appears routed) but the specific spawn is a
# fixed-argv internal probe against our own process/host, not a child running
# agent-influenced code — a resource ceiling adds nothing. Keyed by
# ``<relpath>::<function>`` with a justification, same discipline as
# ``BENIGN_SPAWNS``.
PREEXEC_EXEMPT: frozenset[str] = frozenset(
    {
        # Applies the SAME limits post-exec instead of post-fork. This spawn
        # already prepends the immutable process-group supervisor
        # (`python -I -c <supervisor>`), so it hands the resolved rlimits to that
        # supervisor as `--rlimits=NAME:value,...` (see
        # sandbox.resource_limit_supervisor_argv) and the supervisor calls
        # setrlimit before forking the real child, which inherits the ceiling.
        # The limits are therefore NOT dropped; only the delivery point moved.
        # Why it had to move: `preexec_fn` forces CPython off posix_spawn/vfork
        # onto a plain fork() of the multi-GB, ~118-thread gateway and runs
        # Python in the child before exec. A lock another thread held at fork
        # time is unreleasable there, and a child so wedged deadlocked in a
        # futex, never exec'd, never exited, and pinned every fd it inherited --
        # including gateway.lock and the dashboard listener.
        "kiro_prerequisite.py::_run_process",
        # Spawns NOTHING. It PATCHES `sandboxed_spawn_argv` and raises from the
        # replacement, so the argv is captured at the boundary and no child is ever
        # created -- but the name appears in the function body, which is what this scan
        # matches on. A resource ceiling has nothing to apply to. Deliberately NOT in
        # BENIGN_SPAWNS: that set is for an UNROUTED spawn, and the staleness check
        # correctly rejects this key there.
        "apps/builtins/ops_mission_control/tests/test_ledger_sync_git.py"
        "::test_every_git_invocation_carries_the_identity",
    }
)

# Benign spawns: command/args/cwd are fixed or operator-controlled, NOT
# influenced by the agent, a hostile MCP-config entry, or an agent-selected
# repository. Keyed by ``<relpath>::<enclosing function>``. When adding an
# entry, confirm none of the argv, the cwd, or the resolved binary can be
# steered by the LLM/agent before listing it. See the module docstring for the
# category breakdown and follow-up hardening candidates.
BENIGN_SPAWNS: frozenset[str] = frozenset(
    {
        "acp/runtime.py::_get_rss_mb",
        # The shadow-venv update engine's four spawns. None is agent-influenced
        # and none can route through sandboxed_spawn_argv, because the engine's
        # whole job is to build the NEXT gateway install outside the agent
        # sandbox: (1) _verify_signature runs the openssl binary resolved via
        # trusted_system_bin (never PATH) over files it just wrote into its own
        # mkstemp workdir; (2) _run spawns `sys.executable -m venv <tree>` and
        # `<shadow python> -m pip install <wheel>` where the tree name is
        # composed from the SIGNED manifest's validated version string and the
        # wheel path from the same workdir; (3) build_shadow_venv's best-effort
        # pip self-upgrade in the shadow tree; (4) verify_shadow_venv's `-I`
        # isolated import probe against the shadow interpreter. The update flow
        # is reachable only from the CLI on the operator's terminal or the
        # gateway's approve endpoint behind the OQ7 host-local step-up — the
        # agent's own bash path is closed by the self-update denied rule.
        "platform/wheel_engine.py::_run",
        "platform/wheel_engine.py::_verify_signature",
        "platform/wheel_engine.py::build_shadow_venv",
        "platform/wheel_engine.py::verify_shadow_venv",
        # The userns probe child: ONE fixed argv, `sys.executable -I -S -c <shim>`,
        # no shell, no cwd, stdin/stdout are the two handshake pipes. Nothing is
        # agent-influenced -- the shim is a module-level string constant and takes
        # no arguments. It CANNOT route through sandboxed_spawn_argv: this probe is
        # what decides whether that sandbox exists at all, so routing it would be
        # circular (wrap_argv consults the verdict this child is producing). The
        # child does two unshare() calls against its own fresh process and exits;
        # it executes nothing else (the shim dlopens the already-loaded libc
        # rather than letting ctypes.util.find_library exec ldconfig/gcc).
        "sandbox.py::_probe_unshare_via_spawn",
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
        "acp/runtime.py::_ps_process_table",
        # (_bootstrap.py::_self_heal removed — the console-entry self-heal now
        # delegates its install to dep_sync.sync_or_reinstall, so the spawn lives
        # at that key below and an entry here would be stale.)
        # Ops Mission Control ledger-sync tests: fixed `git` argv (init --bare / ls-files)
        # against a per-test tempdir. Nothing here is agent-influenced — the repo path is
        # `tempfile.mkdtemp()` and every argument is a literal in the test file. These are
        # the TEST harness, not shipped code; the module under test (`ledger_sync._git`)
        # is itself routed through `sandboxed_spawn_argv` and is asserted to be.
        # Sandboxing them would defeat the point: the tests exist to exercise real git
        # against a real bare remote, which is how four fatal sync bugs were found that
        # every mocked-git test passed.
        "apps/builtins/ops_mission_control/tests/test_ledger_sync_git.py::_git",
        "apps/builtins/ops_mission_control/tests/test_ledger_sync_git.py::setUp",
        # Diagnostics support-bundle version probe: fixed argv
        # ``["kiro-cli", "--version"]`` with a 5s timeout, no shell, no cwd, and
        # no agent-influenced args — it only stamps the collected kiro-cli
        # version into versions.txt. The binary name is a module constant; a
        # resource ceiling / sandbox adds nothing to a `--version` call.
        "diagnostics.py::_kiro_cli_version",
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
        # PID-reuse guard for the app-backend reap, moved out of
        # ``apps/backend.py::_proc_start_time`` so Windows gets a real answer
        # instead of a blanket None. Only the macOS/other-POSIX arm spawns, and
        # it is the same fixed ``ps -o lstart= -p <pid>`` argv the backend used
        # before: no shell, the binary comes from ``trusted_system_bin`` rather
        # than PATH, and the sole interpolated value is a pid the gateway itself
        # recorded, rendered through ``str()``. Nothing here is agent-influenced.
        "platform_compat.py::process_start_time",
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
        # TEST-ONLY: spawns `sys.executable -c <literal>` to prove the candidate
        # read-modify-write lock holds across PROCESSES, which is what review
        # workers actually are. A single-process test cannot observe the loss it
        # covers. Fixed argv, no shell=True, no model-derived input -- the only
        # variables are a tmpdir path and a loop index.
        "apps/builtins/code_review_sage/tests/test_learning.py::test_concurrent_processes_both_land",
        # TEST-ONLY: creates a temporary repository with a fixed git argv for the
        # local-review fixture. The path comes from pytest's tmp_path, and no
        # command, argument, or repository is selected by an agent.
        "apps/builtins/code_review_sage/tests/test_local_review.py::_git",
        # auto-improvement: fixed `git`/`gh`/`ruff` argv against the OPERATOR-chosen
        # repository. Same class as code_reviewer/git.py and issue_radar's gh/glab
        # spawns: the repo is selected by the operator through the Connect endpoint,
        # and `clone`/`target_url` are deliberately EXCLUDED from the config PUT
        # allowlist precisely so the agent cannot retarget them. No shell=True, no
        # argv[0] from model output. The agent's own edits happen inside a throwaway
        # worktree of a push-disabled clone, which is where its blast radius is
        # contained; these calls are the harness around it, not the agent's hands.
        "apps/builtins/auto_improvement/backend/clone_setup.py::_disable_push",
        "apps/builtins/auto_improvement/backend/clone_setup.py::_origin_urls",
        "apps/builtins/auto_improvement/backend/clone_setup.py::_repository_is_safe",
        "apps/builtins/auto_improvement/backend/clone_setup.py::_gh_prefers_ssh",
        "apps/builtins/auto_improvement/backend/clone_setup.py::_run",
        "apps/builtins/auto_improvement/backend/clone_setup.py::list_clone_branches",
        # Renamed from ``setup_safe_clone`` when a thin public wrapper was added
        # to convert IsolationProbeError into the (result, err) shape (#8151);
        # the git-clone spawn itself is unchanged and its argv is built from
        # validated owner/repo components, never raw user text.
        "apps/builtins/auto_improvement/backend/clone_setup.py::_setup_safe_clone",
        # NOT subprocess spawns: the AST heuristic matches ``asyncio.run`` (attr
        # ``run`` on base ``asyncio``), used here only to drive the async
        # ``SessionAgentRunner._approve`` coroutine from a synchronous test. No child
        # process is created — the test's provider is a local stub with no argv at all.
        # Same classification as the ``asyncio.run`` sites above
        # (cli_commands.py::_cleanup_app_crons_from_scheduler, cli_doctor.py::_doctor).
        "apps/builtins/auto_improvement/tests/test_dogfood_learnings.py"
        "::test_approval_is_logged_then_granted",
        "apps/builtins/auto_improvement/tests/test_dogfood_learnings.py"
        "::test_audit_failure_denies_instead_of_approving",
        # Offline clone-setup regressions use only fixed Git argv against pytest
        # tmp_path repositories; no agent/model input reaches command or cwd.
        "apps/builtins/auto_improvement/tests/test_clone_setup_idempotent.py::_seeded_bare",
        "apps/builtins/auto_improvement/tests/test_clone_setup_idempotent.py"
        "::test_extra_origin_value_refuses_reuse",
        "apps/builtins/auto_improvement/tests/test_clone_setup_idempotent.py"
        "::test_disable_push_replaces_every_url_value",
        # A FIXED argv of `[sys.executable, "-c", <literal>]` — the interpreter running the
        # test plus a constant source string with no interpolation, so neither the command
        # nor its args are agent-influenced. The child only imports a module and prints
        # whether a second module ended up in `sys.modules`; a clean interpreter is the
        # point, since measuring "does the boot path pull the profile tree?" inside the test
        # session would read whatever pytest already imported.
        "apps/builtins/auto_improvement/tests/test_dogfood_learnings.py"
        "::test_importing_the_backend_does_not_pull_the_profile_tree",
        "apps/builtins/auto_improvement/backend/commit.py::_git",
        # `git apply --index` on the QUEUED diff, literal argv against the configured clone.
        # Was keyed to `commit_finding` until the checkout+apply block was extracted here so
        # the draft-PR route could reuse it (the detector keys by the ENCLOSING function).
        "apps/builtins/auto_improvement/backend/commit.py::materialize_queued_diff",
        "apps/builtins/auto_improvement/backend/deps.py::_gh_authenticated",
        "apps/builtins/auto_improvement/backend/deps.py::install_deps",
        "apps/builtins/auto_improvement/backend/pr_watchers.py::_gh",
        "apps/builtins/auto_improvement/backend/pr_watchers.py::_git",
        "apps/builtins/auto_improvement/profiles/github_repo/pr_recipe.py::_gh_prefers_ssh",
        "apps/builtins/auto_improvement/profiles/github_repo/pr_recipe.py::_git",
        # Fixed `git rev-parse --verify` argv (shell=False) against the OPERATOR-chosen
        # clone, asking whether the operator's `scopeDiffBase` resolves. The ref comes from
        # config (`_CONFIG_WRITABLE`), not from the agent, and it is passed as one argv
        # element — same class as the clone_setup git spawns above.
        # Its test's fixture: literal `git init/add/commit` against a tmp_path repo.
        "apps/builtins/auto_improvement/tests/test_suite_scope.py::_repo",
        "apps/builtins/auto_improvement/profiles/github_repo/pr_recipe.py::draft",
        # Spine git plumbing: fixed argv (worktree add/remove, diff, rev-parse, status,
        # commit, push) against paths the SPINE derives — a worktree root it created and
        # a branch the operator authorized. The agent never supplies a path or a flag
        # here; it only edits FILES inside the worktree, and executing those files is
        # routed separately (profiles/github_repo/profile.py::_run).
        "apps/builtins/auto_improvement/spine/agent_discovery.py::_git",
        "apps/builtins/auto_improvement/spine/driver.py::_apply",
        "apps/builtins/auto_improvement/spine/driver.py::_stage_winner",
        "apps/builtins/auto_improvement/spine/driver.py::_git",
        "apps/builtins/auto_improvement/spine/driver.py::_push_with_rebase",
        "apps/builtins/auto_improvement/spine/gate.py::_changed_paths",
        "apps/builtins/auto_improvement/spine/gate.py::_changed_status_paths",
        "apps/builtins/auto_improvement/spine/gate.py::_head_sha",
        # `git show <base_sha>:<path>` via the hardened `_git_argv` builder — read-only, literal
        # argv over the ORIGINAL worktree, same class as the three gate helpers above. It was
        # always a subprocess spawn; a cleanup that replaced a function-local `import subprocess
        # as _sp` alias with the module-level `subprocess` is what made the AST scanner finally
        # SEE it (the alias hid it). Not agent-influenced: `base_sha` is a resolved sha and `p`
        # is a repo-relative path from the diff.
        "apps/builtins/auto_improvement/spine/gate.py::_stage_test_only_base",
        "apps/builtins/auto_improvement/spine/proposer.py::_capture_diff",
        "apps/builtins/auto_improvement/spine/proposer.py::_git",
        # The agent runner spawns the CLAUDE CLI itself (argv[0] from a module constant,
        # never from model output). ``run`` IS now routed through
        # ``sandboxed_spawn_argv`` — it launches an agent with
        # ``--dangerously-skip-permissions``, so hiding the operator's credential dirs
        # while keeping the worktree visible is exactly the right layer, and review of
        # the auto-improvement PR asked for it. These two remain listed because the
        # detector attributes the spawn to the enclosing prompt-authoring helpers as
        # well, and those do not spawn anything themselves.
        "apps/builtins/auto_improvement/spine/agent_runner.py::author_bug_fix",
        "apps/builtins/auto_improvement/spine/agent_runner.py::author_perf_fix",
        # NOT a subprocess spawn: the AST heuristic matches ``asyncio.run`` (attr ``run``
        # on base ``asyncio``) in ``SessionAgentRunner.run``, which drives the in-process
        # provider and creates no child at all. Same classification as the other
        # ``asyncio.run`` sites above. The key is ``::run`` because this module has TWO
        # ``run`` methods and the detector keys by name — which is exactly why the REAL
        # spawn lives in the uniquely-named ``_spawn_sandboxed_agent`` (routed through
        # ``sandboxed_spawn_argv``), so it can never be masked by this entry.
        "apps/builtins/auto_improvement/spine/agent_runner.py::run",
        # Test harnesses: fixed `git init/add/commit` argv against pytest tmp_path
        # fixtures. Nothing agent-influenced, and these are tests rather than shipped
        # code — same basis as the ops-mission-control ledger-sync test entries.
        "apps/builtins/auto_improvement/tests/test_agent_discovery_focus.py::_git",
        "apps/builtins/auto_improvement/tests/test_github_profile.py::test_push_disabled_reads_the_sentinel",
        "apps/builtins/auto_improvement/tests/test_perf_track_propose.py::_git",
        "apps/builtins/auto_improvement/tests/test_pr_watchers.py::_git",
        # Same basis: a fixed `git init/config/add/commit` argv against a tmp_path, building
        # a repo that holds a real binary blob to prove host-side `git` decodes its output
        # leniently (D-142) — a strict decode killed the watcher on any repo with a PNG.
        "apps/builtins/auto_improvement/tests/test_pr_watchers.py::_repo_with_binary",
        # Same basis: a fixed `git init` + `git diff no-such-branch..HEAD` against a
        # tmp_path, asserting that a failed diff really does exit non-zero with empty
        # stdout — the premise the direct-push credential gate's guard rests on.
        "apps/builtins/auto_improvement/tests/test_pr_recipe.py"
        "::test_a_failed_git_diff_really_does_exit_nonzero_with_empty_stdout",
        # Same basis: a fixed bare-repo + clone + push against a tmp_path, proving the
        # push-disabled clone cannot reach the remote by NAME or by its fetch url.
        "apps/builtins/auto_improvement/tests/test_pr_recipe.py"
        "::test_both_urls_are_neutralized_and_neither_push_route_works",
        # Same basis: the POSITIVE half — a trusted publisher holding the config-carried
        # url still lands its one generated ref against a tmp_path bare repo.
        "apps/builtins/auto_improvement/tests/test_pr_recipe.py"
        "::test_a_recipe_holding_the_config_url_can_still_push",
        "apps/builtins/auto_improvement/tests/test_pr_recipe.py"
        "::test_without_the_config_url_the_neutralized_clone_degrades_to_the_queue",
        # Same basis: fixed `git init/config` argv against a tmp_path repo, proving an
        # agent-planted external diff helper is refused before any publisher Git call.
        "apps/builtins/auto_improvement/tests/test_pr_recipe.py"
        "::test_push_refuses_before_git_when_repository_safety_changed",
        # Same basis: a fixed `git init/add/commit` against a tmp_path, asserting a diff
        # that cannot apply is refused BEFORE the pipeline drafts.
        "apps/builtins/auto_improvement/tests/test_dogfood_learnings.py"
        "::test_a_diff_that_does_not_apply_never_reaches_the_pipeline",
        # NOT a subprocess spawn: the AST heuristic matches ``asyncio.run`` (attr ``run`` on
        # base ``asyncio``), used to drive the async ``_approve`` coroutine so a REAL SEL
        # write can be read back off disk. No child process is created.
        "apps/builtins/auto_improvement/tests/test_dogfood_learnings.py"
        "::test_a_real_sel_write_produces_a_readable_event",
        # Same basis: a fixed `git init --bare` + clone + push against a tmp_path, driving
        # one-click commit end to end in a clone whose origin is neutralized exactly as
        # production leaves it. Nothing here is agent-influenced — the argv is literal, the
        # cwd is the test's own tmp_path, and the "remote" is a local bare repo. This is
        # the test that proves `commit_finding` can still fetch its base after
        # `_disable_push`; the bug it pins was invisible to every mocked test.
        "apps/builtins/auto_improvement/tests/test_dogfood_learnings.py::_git",
        "apps/builtins/auto_improvement/tests/test_dogfood_learnings.py" "::_upstream_and_clone",
        "apps/builtins/auto_improvement/tests/test_dogfood_learnings.py"
        "::test_a_stale_local_ref_is_not_used_when_a_url_is_configured",
        "apps/builtins/auto_improvement/tests/test_dogfood_learnings.py"
        "::test_the_queued_diff_is_committed_and_pushed",
        # Same basis: a fixed bare-repo + clone against a tmp_path, asserting that a
        # non-default branch is really checked out inside a push-disabled clone (the run
        # was silently measuring the DEFAULT branch). Literal argv, test-owned cwd.
        # Only the functions that CONTAIN a spawn are listed: the detector keys by the
        # enclosing function, so a test that merely calls these helpers is not a spawn
        # site and the staleness check rejects it as a masking entry.
        "apps/builtins/auto_improvement/tests/test_dogfood_learnings.py"
        "::test_a_remote_only_branch_is_checked_out_without_a_fetch",
        # Multi-cycle staging test: literal `git` argv against a tmp_path bare repo,
        # asserting cycle-2's checkout does not orphan cycle-1's kept commit.
        "apps/builtins/auto_improvement/tests/test_dogfood_learnings.py"
        "::test_staging_stays_on_the_local_branch_across_cycles",
        # Its inner `git` helper: literal argv against the tmp_path bare repo above.
        "apps/builtins/auto_improvement/tests/test_dogfood_learnings.py::git",
        # The provisional-commit fail-closed test spawns `git rev-parse HEAD` inline (not
        # via a helper) to assert HEAD did not move; literal argv against a tmp_path repo.
        "apps/builtins/auto_improvement/tests/test_dogfood_learnings.py"
        "::test_a_failing_commit_returns_false_not_true",
        # Same basis: `git status --porcelain` inline against a tmp_path repo, asserting a
        # REJECTED provisional commit left nothing staged for the next candidate to inherit.
        "apps/builtins/auto_improvement/tests/test_dogfood_learnings.py"
        "::test_a_rejected_commit_leaves_no_diff_staged_for_the_next_candidate",
        # Same basis: literal `git show`/`ls-tree` against a tmp_path bare repo + clone,
        # asserting a manual draft stages ITS queued diff instead of publishing whatever a
        # later cycle left at HEAD.
        "apps/builtins/auto_improvement/tests/test_dogfood_learnings.py"
        "::test_drafting_an_older_finding_does_not_publish_a_later_one",
        # Same basis: literal `git rev-parse`/`status` against a tmp_path clone, asserting a
        # failed draft's rollback restores the branch to the base it was fetched at.
        "apps/builtins/auto_improvement/tests/test_dogfood_learnings.py"
        "::test_a_rollback_restores_the_branch_to_its_fetched_base",
        # Same basis: literal `git rev-parse` against a tmp_path clone, asserting a REJECTED
        # push leaves no commit on the branch for the next run to adopt as its baseline.
        "apps/builtins/auto_improvement/tests/test_dogfood_learnings.py"
        "::test_a_failed_push_leaves_no_commit_behind",
        # Same basis: literal `git clone`/`log`/`show` against a tmp_path bare repo, asserting
        # two concurrent operator commits never merge into one commit.
        "apps/builtins/auto_improvement/tests/test_dogfood_learnings.py"
        "::test_two_concurrent_commits_do_not_merge_into_one",
        # Its inner `_repo` helper: fixed `git init/clone/commit/push` argv against a tmp_path
        # bare repo, building the local-vs-remote base case for the credential-scan self-diff.
        "apps/builtins/auto_improvement/tests/test_dogfood_learnings.py::_repo",
        # Same basis: literal `git rev-parse`/`diff`/`reset` against a tmp_path repo, showing a
        # left-behind provisional commit lands in the NEXT bug PR's range.
        "apps/builtins/auto_improvement/tests/test_dogfood_learnings.py"
        "::test_a_chained_head_would_contaminate_the_next_branch",
        "apps/builtins/auto_improvement/tests/test_dogfood_learnings.py" "::_disabled_clone",
        "apps/builtins/auto_improvement/tests/test_dogfood_learnings.py"
        "::_remote_with_two_branches",
        # Same basis: a fixed `git init/add/commit` + an uncommitted edit against a tmp_path
        # repo, proving `_export_is_durable` retains a clone that holds UNCOMMITTED work (an
        # empty committed diff over a dirty tree is not "no work"). Literal argv, test-owned
        # cwd, dead origin — nothing agent-influenced. `_run` is the test's inline git helper;
        # the test function itself also spawns `git init/add/commit` directly.
        "apps/builtins/auto_improvement/tests/test_dogfood_learnings.py::_run",
        "apps/builtins/auto_improvement/tests/test_dogfood_learnings.py"
        "::test_end_to_end_uncommitted_work_survives_teardown",
        # Same basis: fixed bare-repo + clone + push against a tmp_path, asserting the
        # CONTENT that reached the remote branch (a committed fix does, a staged one does
        # not) — the end-to-end property behind the keep/draft ordering invariant.
        "apps/builtins/auto_improvement/tests/test_pr_recipe.py::_repo",
        # Its inner `git` helper: literal argv against a tmp_path repo, asserting the
        # driver's direct-push scan RANGE (HEAD~1..HEAD) actually contains the commit.
        "apps/builtins/auto_improvement/tests/test_pr_recipe.py::git",
        "apps/builtins/auto_improvement/tests/test_pr_recipe.py"
        "::test_a_committed_fix_reaches_the_pushed_branch",
        "apps/builtins/auto_improvement/tests/test_pr_recipe.py"
        "::test_a_merely_staged_fix_would_not_reach_it",
        "apps/builtins/auto_improvement/tests/test_profile_capture.py::_git",
        "apps/builtins/auto_improvement/tests/test_runner.py::_git",
        "apps/builtins/auto_improvement/tests/test_runner.py::_tiny_repo",
        # NOT a subprocess spawn: the AST heuristic matches ``asyncio.run`` (attr
        # ``run`` on base ``asyncio``). This is a TEST helper that drives one
        # in-process aiohttp handler coroutine to completion so the PR-action routes
        # can be exercised without a running loop. No child process is created and
        # nothing is agent-influenced — the payloads are literals in the test file.
        # Same classification as the other ``asyncio.run`` sites in this list
        # (cli_doctor.py::_doctor, cli_commands.py::_cleanup_app_crons_from_scheduler).
        "apps/builtins/issue_radar/tests/test_pr_actions.py::_await",
        # Same construct, same classification, for the assignee-route tests: an
        # ``asyncio.run`` that drives one in-process aiohttp handler coroutine to
        # completion. No child process, and every payload is a literal in the test
        # file.
        "apps/builtins/issue_radar/tests/test_assignees.py::_await",
        # NOT subprocess spawns: the AST heuristic matches ``asyncio.run`` (attr
        # ``run`` on base ``asyncio``). These four TEST functions drive Code Review
        # Sage's ``_save_runs`` coroutine to completion without a running loop --
        # the registry write is a coroutine because its owner-only lockdown spawns
        # ``icacls`` on Windows and so must be offloaded off the event loop (that
        # real spawn is ``platform_compat.py::restrict_to_owner``, allowlisted
        # below). No child process is created here and nothing is
        # agent-influenced: every run record in these tests is a literal dict.
        # Same classification as the other ``asyncio.run`` sites in this list.
        "apps/builtins/code_review_sage/tests/test_backend_routes.py"
        "::test_save_then_load_roundtrip",
        "apps/builtins/code_review_sage/tests/test_backend_routes.py"
        "::test_orphaned_running_becomes_interrupted_on_load",
        "apps/builtins/code_review_sage/tests/test_backend_routes.py" "::test_runs_file_is_0600",
        "apps/builtins/code_review_sage/tests/test_backend_routes.py"
        "::test_the_lockdown_never_runs_on_the_event_loop",
        # Same construct and classification: these two drive ``_save_runs`` to prove
        # the registry write no longer targets a predictable ``runs.json.tmp`` that a
        # prompt-injected worker could pre-plant a symlink at. The pre-planted path is
        # built by the test itself, so nothing here is agent-influenced either.
        "apps/builtins/code_review_sage/tests/test_backend_routes.py"
        "::test_a_planted_tmp_symlink_is_not_followed",
        "apps/builtins/code_review_sage/tests/test_backend_routes.py"
        "::test_the_predictable_tmp_name_is_never_used",
        # md-notebook shells out to the real git binary rather than a pure-Python
        # implementation, because a server refuses a push from the shallow clone
        # isomorphic-git produces. The command is the literal "git"; the remote
        # URL and branch are validated by `validate_remote_url` / `validate_ref`
        # before they reach argv (rejecting a leading "-" and the ext::/fd::
        # transport helpers, which name a program for git to run), and a "--"
        # terminates option parsing ahead of the positionals. No shell.
        "apps/builtins/md_notebook/git_ops.py::run_git",
        # Fixed argv `<gh> auth token`: the subcommand is constant and the
        # binary comes from `_find_gh()`, which probes known install paths —
        # neither is caller- or agent-supplied.
        "apps/builtins/md_notebook/server.py::_gh_token_sync",
        # Fixed argv `osascript -e <constant AppleScript>` for the macOS folder
        # picker; the script is a module constant with nothing substituted in.
        "apps/builtins/md_notebook/server.py::_pick_folder_sync",
        # Fixed argv `<file manager> <dir>` to reveal a vault's `.trash` in
        # Finder / the desktop file manager. No shell. The binary is an absolute
        # module constant (`/usr/bin/open`, `/usr/bin/xdg-open`) resolved from a
        # platform map and existence-checked — deliberately NOT from PATH, whose
        # front is agent-writable and could hold an `open` shim. The single
        # argument is not caller-supplied: `api_trash_open` takes no path and
        # derives the directory from the vault descriptor via
        # `vault_mutation_path`, which rejects `..`, absolute values and any
        # symlink escaping the vault, so the argv cannot be pointed elsewhere.
        "apps/builtins/md_notebook/server.py::_reveal_folder_sync",
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
        # Issue Radar Azure DevOps access — the az counterpart of _gh_run and
        # _glab_run, benign for the same reasons, differing from both in WHERE a
        # request body travels and from glab in how the host is constrained.
        # ALL az calls funnel through ONE chokepoint, _az_run: a fixed
        # `az devops invoke` list-argv (never shell=True). az supplies the host's
        # OWN authenticated session, so it CANNOT be sandbox-routed (the sandbox
        # would hide ~/.azure and break auth). As defense-in-depth WITHIN this
        # benign classification, _az_run resolves az through the shared provider
        # policy (refusing a binary owned by another user, a world-writable one,
        # or one inside the agent-writable project tree) and passes a MINIMAL env.
        # The agent-reachable inputs:
        #   • the HOST — unlike glab's operator-configurable allowlist there is
        #     exactly ONE legal value, the module constant dev.azure.com, because
        #     on-premises Server is out of scope. It is re-resolved inside _az_run
        #     on every call and anything else (including empty) is refused, so a
        #     corrupted config entry cannot retarget the spawn;
        #   • organization / project / repository — charset-validated per segment
        #     by azure_client.parse_azure_repo_url at /connect, then passed as the
        #     --org URL and as --route-parameters values; read routes additionally
        #     gate on store.is_repo_connected, which matches on provider+host too.
        #     `--detect false` is passed so az cannot instead infer an
        #     organization from the cwd's git remote;
        #   • the work item / pull request id — coerced via int() before the path;
        #   • --area / --resource / --api-version — module constants selected by
        #     the calling function, never caller-supplied text;
        #   • write bodies AND every WIQL query string — sent as a request-body
        #     FILE (--in-file), never on argv, because az devops invoke has no
        #     stdin body option the way gh and glab do. The file is uniquely
        #     named, created 0600, and unlinked in a finally, so a body is neither
        #     visible in the process table nor left behind.
        # No binary or cwd is agent-selected.
        "apps/builtins/issue_radar/backend/azure_client.py::_az_run",
        "apps/builtins/design_tweak/backend/server.py::_h_pick_folder",
        "apps/builtins/design_tweak/backend/server.py::_lsof_fields",
        "apps/builtins/design_tweak/backend/server.py::_start_dev_proc",
        "apps/builtins/workflows/server.py::handle_run",
        # _start_run's worker spawns argv that is ALWAYS pre-wrapped by its
        # callers through sandboxed_spawn_argv (sync wraps each step with
        # per-step modes; provision wraps the pod CLI argv) and the spawn
        # carries resource_limit_preexec() — routing again here would nest
        # sandboxes. The chokepoint is applied at the call sites.
        "apps/builtins/dev_fleet/runtime.py::worker",
        # The sync runner (the module worktree_ops's sync snapshots and runs by
        # path) executes step argvs it reads from its steps_json input -- and
        # every one of those argvs was ALREADY wrapped through
        # sandboxed_spawn_argv (with per-step modes) by worktree_ops at
        # composition time, before serialization. Routing again inside the
        # runner would nest sandboxes, exactly as the runtime.py::worker entry
        # above records for the outer spawn; the chokepoint is applied at the
        # composition site.
        "apps/builtins/dev_fleet/sync_runner.py::run_step",
        # Dev Fleet builtin backend: async version routes all git/gh through
        # _run_cmd which calls sandboxed_spawn_argv (the chokepoint). Only
        # _resolve_primary_checkout uses subprocess.run directly (one-shot
        # git rev-parse at startup, no agent input, no sandbox needed).
        "apps/builtins/dev_fleet/repository.py::_resolve_primary_checkout",
        "apps/builtins/dev_fleet/runtime.py::worker",
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
        "dep_sync.py::sync",
        "dep_sync.py::sync_or_reinstall",
        # npm_preflight is the sync's pre-merge installability probe, and like
        # dep_sync it runs AS one of the sync steps -- which server.py already
        # wrapped through sandboxed_spawn_argv before handing it to the runner.
        # So all three of its spawns are already inside that sandbox, and routing
        # them again would nest one sandbox inside itself; the chokepoint is
        # applied at the call site, exactly as for runtime.py::worker.
        #
        # No argv is agent-influenced. _extract spawns
        # `<git> -C <repo> show <remote>/<base branch>:website/<fixed filename>`:
        # the binary comes from _trusted_bin (never PATH), the repo from the
        # operator-configured checkout, the branch from the BASE_BRANCH constant,
        # and the three filenames from a module-level tuple.
        # _install_already_proven spawns
        # `<git> -C <repo> diff --name-only <ref> -- website` from the same three
        # sources, with the pathspec a module-level constant; it only READS, and
        # its answer decides whether the install below is skipped.
        # probe spawns `<npm> ci --ignore-scripts --no-audit --no-fund` with cwd
        # set to its own mkdtemp scratch directory -- not a repository, and not a
        # path any caller supplies.
        #
        # The lockfile it installs IS repo-controlled, but that is input data to
        # npm rather than steering of argv or cwd, it is the same content the real
        # `npm ci` step installs, and --ignore-scripts is what keeps that content
        # from getting code executed. A filesystem-scoped wrapper here would also
        # block the scratch-directory writes that are the whole point of the probe.
        "apps/builtins/dev_fleet/npm_preflight.py::_extract",
        "apps/builtins/dev_fleet/npm_preflight.py::_install_already_proven",
        "apps/builtins/dev_fleet/npm_preflight.py::probe",
        # Foreground last-resort restart (Make Live on hosts with no drivable
        # service manager): a detached `kirocrew restart --port <marker port>`,
        # fixed argv whose binary is validated (basenamed kirocrew, absolute,
        # executable) from the gateway's own keystone-fenced run-marker or
        # shutil.which — never agent input. Deliberately NOT sandboxed for the
        # same reason as cli_server.py::_spawn_detached_gateway above: the child
        # must outlive and REPLACE the gateway (kill + respawn + own session),
        # which a scoped/sandboxed child cannot do.
        "apps/builtins/dev_fleet/gateway_service.py::default_detached_spawn",
        # (apps/dependencies.py::_run_aim removed — App Kit capability deps now
        # resolve through the CapabilityManager seam, so the resolver spawns no
        # subprocess at all and needs no allowlist entry.)
        # Browser Mode setup/install path, run only from the dashboard settings
        # save (off the event loop) or the `kirocrew browse setup` CLI. Fixed
        # argv of trusted node-toolchain tools resolved via find_node_tool
        # (npm/npx/node) plus the ``playwright install <engine>`` subcommand,
        # ``browser_cli`` spawns the Playwright CLI toolchain on fixed argv that
        # the AGENT never contributes to, which is what makes them benign here:
        #   * ``install.py::_run`` runs the three install steps
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
        # The agent's OWN browser commands are not spawned by us at all -- it
        # runs them as ordinary shell tool calls through the standard approval
        # path.
        "browser_cli/install.py::_run",
        "browser_cli/view.py::_spawn",
        "cli.py::_consolidate_cmd",
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
        "cli_commands.py::_cleanup_app_crons_from_scheduler",
        # NOT a subprocess spawn: the AST heuristic matches ``asyncio.run`` (attr
        # ``run`` on base ``asyncio``), used to drive the async
        # ``register_app_crons_with_service`` coroutine from the loop-less CLI
        # enable path — the exact enable-direction mirror of
        # ``_cleanup_app_crons_from_scheduler`` above. No child process is
        # created; the sole input is the operator-typed app name.
        "cli_commands.py::_register_app_crons_to_scheduler",
        "cli_doctor.py::_doctor",
        # NOT a subprocess spawn: the AST heuristic matches ``asyncio.run`` (attr
        # ``run`` on base ``asyncio``), used to drive the async Discord
        # privileged-intent probe from the loop-less doctor path. No child
        # process is created: the probe is one HTTPS GET to Discord's own
        # ``/oauth2/applications/@me`` with a bot token read from the operator's
        # own credential store, and every failure is folded into its result. Same
        # classification as ``cli_doctor.py::_doctor`` above.
        "cli_doctor.py::_discord_intent_grants",
        "cli_doctor.py::_doctor_mcp_tools",
        # The AST heuristic matches ``asyncio.run`` (attr ``run`` on base
        # ``asyncio``), used to drive one async capability-manager read from the
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
        "cli_doctor.py::_credential_vendor_line",
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
        "cli_doctor.py::_aws_profile_names",
        "cli_doctor.py::_aws_auto_refreshes",
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
        "cli_doctor.py::_git_line",
        # ``<kiro-cli> acp --help`` readiness probe for the KAS backend: fixed
        # argv (subcommand and flag are module constants), 15s-capped, no shell,
        # no agent-influenced arguments, and no credential involved — it reads
        # help text to confirm this kiro-cli can select the KAS engine at all.
        # Crew no longer mints a KAS token anywhere; the relay resolves tokens
        # from kiro-cli's own store (see ``acp/kas_transport.py``), so the former
        # ``chat _ get-kas-token`` spawn is gone rather than moved.
        "cli_doctor.py::_kas_relay_help",
        # ``systemctl is-active <unit>`` probes for the memory-pressure
        # preparedness check: argv is hardcoded (systemd-oomd/earlyoom unit
        # names), no agent influence, 5s-capped, read-only query.
        "cli_doctor.py::_detect_userspace_oom_killer",
        # Read-only diagnostic: `loginctl show-user <user> -p Linger --value`,
        # a fixed argv whose only variable is the invoking account name taken
        # from $USER/$LOGNAME (never agent-supplied). Same class as
        # service/linux.py::_current_group — an identity/state query the doctor
        # makes to tell the user whether pods survive logout. No shell, no
        # agent-influenced argument, nothing written.
        "cli_doctor.py::_linger_enabled",
        "cli_server.py::_logs_cmd",
        "cli_server.py::_spawn_detached_gateway",
        "cli_server.py::_update",
        # The agent-only config refresh extracted from _update: a fixed argv
        # (`<this interpreter> -m kiro_crew setup --agent-only`) built from
        # sys.executable plus literals, cwd from the detected install layout —
        # no shell, no PATH lookup, nothing agent-influenced. stdin=DEVNULL
        # and TimeoutExpired handling are pinned by
        # test_update_agent_refresh.py.
        "cli_server.py::_refresh_agent_config",
        # (_divergence_verdict removed — its counting now delegates to the
        # git_divergence module, allowlisted below, and spawns nothing itself.)
        "cli_server.py::_update_wheel",
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
        "cloud/source.py::_tracked_tree_is_dirty",
        "cloud/source.py::_use_git_archive",
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
        "dashboard/handlers/files.py::_run",
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
        # The update check's git side: fixed `git fetch` / `rev-parse` / `show` /
        # `diff` list-argv (no shell=True) run in KIROCREW_PROJECT_DIR, an operator
        # environment value, never agent input. Read-only version comparison —
        # nothing here writes to the tree.
        "dashboard/handlers/updates.py::_check_git_checkout",
        # (_venv_pip_install removed — it now delegates the install to
        # dep_sync.sync_or_reinstall and spawns nothing itself.)
        "dashboard/handlers/updates.py::api_update_apply",
        "dashboard/handlers_system.py::_collect_system_metrics",
        # Split out of _collect_system_metrics above so the whole-machine process
        # walk can be cached on its own (much longer) TTL instead of the live
        # graph's. Identical trust profile to its former enclosing function,
        # which is still listed: one fixed argv (`ps -eo pid,command`) with a 5s
        # timeout, no shell, no cwd, no agent-influenced arguments.
        "dashboard/handlers_system.py::_scan_mcp_processes",
        "dashboard/handlers_system.py::_get_static_system_info",
        "dashboard/port_reclaim.py::_listeners_on_port",
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
        "instances/diagnostics.py::_run_ok",
        "instances/diagnostics.py::_run_stdout",
        "instances/ssh_tunnel_manager.py::start",
        "instances/token_mint.py::mint_remote_token",
        "instances/token_mint.py::run_remote_kirocrew",
        # The iMessage bridge child (`<cli_path> rpc [--db-path <p>]`). Fixed
        # list-argv, no shell: both paths come from the operator's own
        # `config.json` `imessage` section, which the settings API writes only
        # from a direct-local request and which rejects a line break or NUL, so
        # neither value can be agent-supplied or split into extra arguments.
        # Sandboxing it would defeat the point: the child exists to reach
        # Messages.app through the operator's own Full Disk Access and
        # Automation grants, which a scrubbed-env sandbox strips.
        "imessage/rpc.py::start",
        "mcp_core.py::_get_ppid",
        "mcp_gateway/backend.py::spawn_backend",
        "mcp_gateway/gatewayd.py::main",
        "mcp_gateway/manager.py::_spawn_once",
        "mcp_gateway/stub.py::main",
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
        # Feed-manifest signature verification gating the forced-update floor.
        # One fixed argv (`openssl dgst -sha256 -verify …`) whose binary comes
        # from platform_compat.trusted_system_bin (a vetted absolute path,
        # never PATH — asserted by test_feed_trust's PATH-shim test), no
        # shell, a 10s timeout, and no cwd. The untrusted manifest bytes
        # travel as FILE CONTENT inside a private TemporaryDirectory; the
        # three path arguments are that directory's own literals, so nothing
        # agent- or network-influenced ever reaches argv. Must NOT be
        # sandboxed: the spawn's whole purpose is to REJECT tampered input,
        # and every failure (openssl missing included) already fails safe to
        # "no floor".
        "platform/feed_trust.py::verify_manifest_signature",
        "mcp_shared.py::_get_ppid",
        # File-manager launchers for the dashboard's reveal action. The
        # command is an absolute literal resolved in this module (never a bare
        # argv name, so an agent-writable PATH entry cannot supply it), the
        # spawn gets a PATH pinned to the trusted system directories, and the
        # only caller-supplied element is the path being revealed — which is
        # passed as a later argv element, never as the command.
        "platform_compat.py::open_with_default_app",
        "platform_compat.py::_posix_process_parent_map",
        "platform_compat.py::find_port_listeners",
        "platform_compat.py::find_python_interpreter",
        "platform_compat.py::kill_pid",
        "platform_compat.py::kill_process_tree",
        "platform_compat.py::reveal_in_file_manager",
        "platform_compat.py::process_command_line",
        # Same class as process_command_line: a read-only process-attribute query
        # (``ps -o uid=`` / ``/proc/<pid>`` stat) in the platform leaf module,
        # with a fixed argv containing only an int-coerced pid. It cannot route
        # through the sandbox helper because sandbox imports platform_compat.
        "platform_compat.py::process_owner_uid",
        "platform_compat.py::process_matches",
        # Same class as process_matches: a read-only process-attribute query
        # (macOS ``ps -ww -o command= -p <pid>``; Linux reads /proc without
        # spawning) whose only interpolated value is an int-guarded pid — the
        # expected argv is compared IN PYTHON, never passed to the child. It is
        # the strict identity check consulted before reclaiming a recorded
        # forwarder pid, and cannot route through the sandbox helper because
        # sandbox imports platform_compat.
        "platform_compat.py::process_argv_matches_exact",
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
        "pod/provision.py::_run",
        "pod/runtime.py::_git_worktrees",
        "pod/runtime.py::_run",
        "pod/runtime.py::recent_journal",
        "sandbox.py::_probe_sandbox_exec",
        "sandbox.py::_ssh_supports_accept_new",
        # The aggregate slice-ceiling apply: `systemctl --user set-property
        # --runtime kirocrew-agents.slice MemoryMax=<N>M MemorySwapMax=0
        # TasksMax=<N>`. Argv is a fixed verb plus module-constant unit name;
        # the only variable tokens are integers derived from config/sysconf
        # (type-checked, junk falls back to defaults) — not agent-influenced.
        # Runs once at gateway startup, not from an agent turn. Sandboxing it
        # would be circular: it constructs the cgroup boundary agent spawns
        # are confined by.
        "sandbox.py::ensure_agents_slice_limits",
        # The agent-slice MemoryHigh reconciler. Fixed argv: `systemctl --user
        # set-property --runtime kirocrew-agents.slice MemoryHigh=<value>`, where
        # the binary is resolved with shutil.which (never a caller-supplied PATH)
        # and <value> is derived from host RAM, never from agent input.
        # Sandboxing it would also be circular: it CONFIGURES the cgroup
        # containment that agent spawns are wrapped in.
        "sandbox.py::_ensure_agent_slice_memory_high",
        # The chokepoint wrapper itself. It spawns whatever argv it is handed, so
        # it cannot route on its own behalf — its CALLERS are the ones this audit
        # holds to sandboxed_spawn_argv / wrap_argv, and they still appear here
        # individually because _SPAWN_NAMES collects bare-name calls to it.
        "sandbox.py::create_subprocess_limited",
        # The synchronous siblings of the above, same reasoning: they wrap an argv
        # they are handed and cannot route on its behalf.
        "sandbox.py::run_limited",
        "sandbox.py::popen_limited",
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
        "service/linux.py::_current_group",
        "service/linux.py::_sudo_run",
        "service/linux.py::_systemctl",
        "service/linux.py::_write_unit_via_sudo",
        "service/macos.py::_launchctl",
        "session_pid.py::_our_orphan_pids",
        "session_pid.py::find_orphan_mcp_candidates",
        "session_pid.py::kill_orphan_mcps",
        "slack/gateway.py::_auto_apply_update",
        # Wheel/cli.sh auto-update: runs the signed installer command
        # (composed locally from a validated channel name and https-pinned
        # artifact base, never from feed data). The child is the cli.sh
        # installer, which performs its own RSA-SHA256 signature verification.
        # NOT sandbox-routed because the installer must write to the managed
        # venv and symlink ~/.local/bin/kirocrew.
        "slack/gateway.py::_auto_apply_wheel_update",
        # Pluggable update provider: CommandProvider runs operator-configured
        # shell commands from security_policy.json or config.json (sensitive
        # home dirs the agent cannot write). The check command probes for a
        # newer version; the apply command performs the update. Both are
        # operator-authored, not agent-influenced. NOT sandbox-routed because
        # the command must reach the host's package manager / registry.
        "platform/update_provider.py::check",
        "platform/update_provider.py::apply",
        "slack/gateway.py::_check_missing_deps",
        # The kiro-cli version probe, extracted from _init_services (issue
        # #3051). Fixed argv ("kiro-cli --version"), no agent-influenced
        # input; sandboxing the probe would be circular for the same reason
        # as the other boot-time self-checks above.
        "slack/gateway.py::_warn_if_kiro_cli_outdated",
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
        "transcribe.py::_create_ffmpeg_subprocess",
        # The macOS authenticity oracle for the bundled ffmpeg: spawns the
        # absolute /usr/bin/codesign (never PATH) with constant flags and a
        # requirement string built from a module-level team-ID constant. The
        # only variable argument is the path of the process-private snapshot
        # (`/proc/self/fd/N`-style descriptor or the 0o500 snapshot dir this
        # module itself just wrote and digest-verified) — no agent influence
        # over command, args, cwd, or env. It cannot route through
        # sandboxed_spawn_argv: codesign must read the system trust store and
        # evaluate the Apple certificate chain, which the OS sandbox denies.
        "transcribe.py::_macos_developer_id_authentic",
        "transcribe.py::_pcm_via_ffmpeg",
        "transcribe.py::_transcribe_aws",
        # The build probe executes the same authenticated image with the single
        # fixed `-version` argument; it accepts no external input at all. Both
        # streams are CAPTURED rather than discarded, so a refusal can name itself
        # in the build log, and are decoded with errors="replace" because a loader
        # complaint arrives in the host's console encoding.
        "transcribe.py::_packaged_ffmpeg_version_probe",
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


# First-party fixed-argv spawn sites: every call site in ``src/kiro_crew`` that
# passes the ``first_party_fixed_argv`` keyword into the sandbox chokepoint
# (``sandboxed_spawn_argv`` / ``wrap_argv``). The flag buys an UNCONFINED spawn
# on a backend-less host (issue #1563 carve-out), so "first-party" must be a
# reviewed property, not a copy-pasteable kwarg: a new site must be added here
# WITH a justification proving the full argv is derived inside this package
# with zero agent/repo/user-config influence. Keyed by
# ``<relpath>::<enclosing function>``, same discipline as ``BENIGN_SPAWNS``.
FIRST_PARTY_SPAWNS: frozenset[str] = frozenset(
    {
        # The managed-server probe. The flag value is COMPUTED, not literal:
        # ``_is_first_party_managed_argv`` requires the spec's command+args+env
        # to EQUAL what this package derives for the managed server
        # (``agent._kirocrew_mcp_invocation`` + ``agent._managed_mcp_env``, the
        # single sources of truth the specs are force-re-resolved from) — never
        # user-config text. Env is compared because the probe merges the spec's
        # env into the child environment and ``LD_PRELOAD`` changes what code
        # runs for the same argv. Third-party servers and any customized
        # managed command/args/env compare unequal, pass False, and keep the
        # full fail-close + opt-in behavior.
        "mcp_discovery.py::probe_server",
    }
)

_FIRST_PARTY_KWARG = "first_party_fixed_argv"


@functools.lru_cache(maxsize=1)
def _collect_first_party_flag_sites() -> frozenset[str]:
    """``<relpath>::<func>`` for every call passing the first-party kwarg.

    AST-based rather than a substring scan: it matches any ``ast.Call``
    carrying a keyword named ``first_party_fixed_argv`` REGARDLESS of the value
    expression — a site passing a computed bool must be reviewed exactly like
    one passing a literal ``True`` (the computation is part of the claim).
    Like the sibling scans in this file, ``**kwargs`` indirection is out of
    scope (an aliased spawn already hid from the spawn detector once); the
    PR-review gates cover deliberately obfuscated passes.
    ``sandbox.py`` is excluded by design: it OWNS the parameter (``wrap_argv``
    defines it; ``sandboxed_spawn_argv`` threads it through), so its internal
    forwarding is the mechanism under audit, not a spawn site.
    """
    out: set[str] = set()
    for path in _SRC_ROOT.rglob("*.py"):
        rel = path.relative_to(_SRC_ROOT).as_posix()
        if rel == "sandbox.py" or _is_bundled_skill_asset(path):
            continue
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, str(path))
        funcs = [
            n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not any(kw.arg == _FIRST_PARTY_KWARG for kw in node.keywords):
                continue
            enc = "<module>"
            best = -1
            for f in funcs:
                if f.lineno <= node.lineno <= (f.end_lineno or f.lineno) and f.lineno > best:
                    best = f.lineno
                    enc = f.name
            out.add(f"{rel}::{enc}")
    return frozenset(out)


def test_every_first_party_spawn_is_allowlisted():
    """A new site passing ``first_party_fixed_argv`` must be reviewed here.

    The flag buys an unconfined spawn on a backend-less host, so passing it
    from an unreviewed site is a sandbox bypass. Add the ``file::function`` key
    to ``FIRST_PARTY_SPAWNS`` ONLY after confirming the full argv is derived
    inside this package with zero agent/repo/user-config influence, and record
    that reasoning in the allowlist comment.
    """
    unexpected = _collect_first_party_flag_sites() - FIRST_PARTY_SPAWNS
    assert not unexpected, (
        "New site(s) passing first_party_fixed_argv into the sandbox "
        "chokepoint:\n  "
        + "\n  ".join(sorted(unexpected))
        + "\n\nThis flag permits an UNCONFINED spawn on a host with no sandbox "
        "backend (issue #1563 carve-out). Confirm the full argv is derived "
        "inside this package with zero agent/repo/user-config influence, then "
        "add the file::function key to FIRST_PARTY_SPAWNS with a justification."
    )


def test_first_party_allowlist_has_no_stale_entries():
    """Every FIRST_PARTY_SPAWNS entry must still name a real flag-passing site,
    so the allowlist cannot silently accumulate dead exemptions that would mask
    a future regression at the same key."""
    stale = FIRST_PARTY_SPAWNS - _collect_first_party_flag_sites()
    assert not stale, (
        "Stale FIRST_PARTY_SPAWNS entries (no longer a first-party flag site — "
        "remove them):\n  " + "\n  ".join(sorted(stale))
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
        # A skill's own helper scripts are not gateway runtime code paths --
        # see ``_is_bundled_skill_asset`` for why they are out of scope.
        if _is_bundled_skill_asset(path):
            continue
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, str(path))
        funcs = [
            n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        lines = source.splitlines()
        rel = path.relative_to(_SRC_ROOT).as_posix()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name):
                if node.func.id not in _SPAWN_NAMES:
                    continue
            elif isinstance(node.func, ast.Attribute):
                if node.func.attr in _SPAWN_NAMES:
                    pass  # e.g. sandbox.create_subprocess_limited(...)
                elif node.func.attr in _SPAWN_ATTRS:
                    base = node.func.value
                    base_name = (
                        base.id
                        if isinstance(base, ast.Name)
                        else base.attr if isinstance(base, ast.Attribute) else ""
                    )
                    if base_name not in _SPAWN_BASES:
                        continue
                else:
                    continue
            else:
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
    """The off-loop prerequisite adapter must remain a thin sandbox wrapper.

    The off-loop hop itself now lives one level down, in
    ``sandbox.shielded_prepare_off_loop`` (the single owner of the
    shield-and-recover pattern), so this pins both halves where they actually
    live: the adapter must still name the chokepoint and route through that
    owner, and the owner must still take the work off the loop.
    """

    path = _SRC_ROOT / "kiro_prerequisite.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, str(path))
    adapter = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_prepare_sandboxed_spawn"
    )
    adapter_source = ast.get_source_segment(source, adapter) or ""
    assert "shielded_prepare_off_loop" in adapter_source
    assert "sandboxed_spawn_argv" in adapter_source

    sandbox_path = _SRC_ROOT / "sandbox.py"
    sandbox_source = sandbox_path.read_text(encoding="utf-8")
    sandbox_tree = ast.parse(sandbox_source, str(sandbox_path))
    owner = next(
        node
        for node in ast.walk(sandbox_tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "shielded_prepare_off_loop"
    )
    owner_source = ast.get_source_segment(sandbox_source, owner) or ""
    assert "asyncio.to_thread" in owner_source or "run_in_executor" in owner_source


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


def test_bundled_skill_assets_are_not_imported():
    """The skill-asset exemption is only honest while the gateway never imports one.

    ``_is_bundled_skill_asset`` takes a skill's helper scripts out of the spawn
    audit on the premise that they are scripts an agent runs in a shell, not
    code this package runs. That premise has one failure mode: someone imports
    such a script as a module, and its unrouted spawns become gateway spawns
    while staying invisible to the audit. This test forbids that.

    The sibling claim -- that the gateway never EXECS one either -- needs no
    test: a gateway function spawning ``narrate.py`` would itself be a spawn
    site in a non-exempt file, so the audit reviews it there.

    Reading these files as DATA is expected and not what this pins: the skills
    loader lists and reads skill directories, which is the whole point of
    shipping them.
    """
    assets = [p for p in _SRC_ROOT.rglob("*.py") if _is_bundled_skill_asset(p)]
    # Non-vacuity: a predicate matching nothing would make this pass while
    # pinning nothing, and would mean the exemption itself is dead.
    assert assets, "no bundled skill assets found -- the exemption matches nothing"
    app_bundled = [p for p in assets if "builtin_skills" not in p.relative_to(_SRC_ROOT).parts]
    assert app_bundled, "app-bundled skill assets (apps/builtins/*/skills/**) not matched"

    asset_modules = {
        "kiro_crew." + p.relative_to(_SRC_ROOT).with_suffix("").as_posix().replace("/", ".")
        for p in assets
    }
    asset_packages = {
        "kiro_crew." + p.relative_to(_SRC_ROOT).parent.as_posix().replace("/", ".") for p in assets
    }

    offenders: list[str] = []
    for path in _SRC_ROOT.rglob("*.py"):
        if _is_bundled_skill_asset(path):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        rel = path.relative_to(_SRC_ROOT).as_posix()
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                names = [node.module]
            for name in names:
                if name in asset_modules or name in asset_packages:
                    offenders.append(f"{rel}:{node.lineno} imports {name}")

    assert not offenders, (
        "Gateway code imports a bundled skill asset, which the spawn audit "
        "exempts:\n  " + "\n  ".join(sorted(offenders)) + "\n\nEither move the "
        "shared logic into a real module under src/kiro_crew (where the spawn "
        "audit reviews it), or drop the import."
    )


# ═══════════════════════════════════════════════════════════════════════════
# Gateway spawn timeout discipline — issue #4210
#
# `slack/gateway.py` spawns children on the boot and auto-update paths. PR
# #4049 established the discipline for a spawn that can time out: own process
# group (`start_new_session` on POSIX), tree-kill + bounded reap on
# TimeoutError AND on CancelledError. These two ratchets make the discipline
# structural: the NEXT spawn added to the file cannot silently regress to an
# abandoned-child-on-timeout, because the audit below fails until it carries
# the same treatment.
# ═══════════════════════════════════════════════════════════════════════════

_GATEWAY_REL = "slack/gateway.py"

#: Functions that own the kill-the-tree + bounded-reap contract. The shared
#: helper is `platform_compat.kill_and_reap` (`_kill_and_reap` is its delegate
#: in `platform/update_provider.py`); the two `_startup_child` methods are the
#: boot-path equivalent (kill and reap split in two).
_TREE_KILL_FUNCS = frozenset({"kill_and_reap", "_kill_and_reap", "_kill_startup_child"})


@functools.cache
def _gateway_tree() -> ast.Module:
    path = _SRC_ROOT / "slack" / "gateway.py"
    return ast.parse(path.read_text(encoding="utf-8"), str(path))


def _is_spawn_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr.startswith("create_subprocess_")
    )


def _handler_catches(handler: ast.ExceptHandler, name: str) -> bool:
    """Whether *handler* names *name* (bare or attribute-qualified) explicitly.

    A blanket ``except Exception`` deliberately does NOT count: the discipline
    requires the timeout/cancel arm to be explicit so the kill is visibly tied
    to the abandonment hazard, and `_auto_apply_update`'s outer
    ``except Exception`` (which does not kill) must not satisfy this audit.
    """
    types = handler.type
    if types is None:
        return False
    parts = types.elts if isinstance(types, ast.Tuple) else [types]
    for part in parts:
        if isinstance(part, ast.Name) and part.id == name:
            return True
        if isinstance(part, ast.Attribute) and part.attr == name:
            return True
    return False


def _handler_kills(handler: ast.ExceptHandler, proc_name: str) -> bool:
    """Whether *handler*'s body calls a tree-kill helper on *proc_name*."""
    for node in ast.walk(handler):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        fname = (
            fn.id if isinstance(fn, ast.Name) else fn.attr if isinstance(fn, ast.Attribute) else ""
        )
        if fname not in _TREE_KILL_FUNCS:
            continue
        if any(isinstance(a, ast.Name) and a.id == proc_name for a in node.args):
            return True
    return False


def test_gateway_spawns_all_own_session():
    """Every subprocess spawn in slack/gateway.py starts its own session.

    `proc.kill()` signals only the direct child; without
    ``start_new_session`` the process-group tree kill in the timeout/cancel
    arms has no group of its own to address, so grandchildren survive the
    kill and keep running (issue #4210).
    """
    missing: list[str] = []
    spawns = 0
    for node in ast.walk(_gateway_tree()):
        if not _is_spawn_call(node):
            continue
        spawns += 1
        if not any(kw.arg == "start_new_session" for kw in node.keywords):
            missing.append(f"{_GATEWAY_REL}:{node.lineno}")
    assert spawns >= 10, (
        f"only {spawns} spawn sites found in {_GATEWAY_REL} — the audit's "
        "spawn matcher no longer matches the file's spawn idiom; fix the "
        "matcher rather than deleting the ratchet"
    )
    assert not missing, (
        "Spawn(s) in slack/gateway.py without start_new_session — a timeout "
        "kill would reach only the direct child, abandoning its descendants "
        "(issue #4210):\n  " + "\n  ".join(missing) + "\n\nAdd "
        "start_new_session=platform_compat.IS_POSIX to the spawn."
    )


def test_gateway_proc_waits_all_kill_on_timeout_and_cancel():
    """Every ``wait_for(<proc>.communicate()|.wait())`` in slack/gateway.py
    sits under explicit TimeoutError AND CancelledError arms that tree-kill
    that proc.

    Without the arm, the child is ABANDONED on timeout — the exception
    propagates (or is swallowed) while the process keeps running with no
    supervisor, which on the auto-update path means a `git reset` or
    `kiro-cli update` still mutating the installation (issue #4210).
    """
    offenders: list[str] = []
    audited = 0
    for func in ast.walk(_gateway_tree()):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        # Names assigned from a spawn IN THIS function. A proc received as a
        # parameter (the reap helpers) is that caller's site, audited there.
        proc_names = {
            t.id
            for stmt in ast.walk(func)
            if isinstance(stmt, ast.Assign)
            and isinstance(stmt.value, ast.Await)
            and _is_spawn_call(stmt.value.value)
            for t in stmt.targets
            if isinstance(t, ast.Name)
        }
        if not proc_names:
            continue
        # Map each wait_for-on-a-proc to the Trys enclosing it in their body.
        parents: dict[ast.AST, ast.AST] = {}
        for parent in ast.walk(func):
            for child in ast.iter_child_nodes(parent):
                parents[child] = parent
        for node in ast.walk(func):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "wait_for"
                and node.args
                and isinstance(node.args[0], ast.Call)
                and isinstance(node.args[0].func, ast.Attribute)
                and node.args[0].func.attr in ("communicate", "wait")
                and isinstance(node.args[0].func.value, ast.Name)
                and node.args[0].func.value.id in proc_names
            ):
                continue
            audited += 1
            proc_name = node.args[0].func.value.id
            timeout_ok = cancel_ok = False
            cursor: ast.AST | None = node
            while cursor is not None and cursor is not func:
                parent = parents.get(cursor)
                if isinstance(parent, ast.Try) and cursor in ast.walk(parent):
                    # Only count Trys where the wait sits in the BODY (an
                    # already-handling arm re-waiting is the reap, not a site).
                    in_body = any(
                        cursor is stmt or cursor in ast.walk(stmt) for stmt in parent.body
                    )
                    if in_body:
                        for handler in parent.handlers:
                            if _handler_kills(handler, proc_name):
                                if _handler_catches(handler, "TimeoutError"):
                                    timeout_ok = True
                                if _handler_catches(handler, "CancelledError"):
                                    cancel_ok = True
                cursor = parent
            if not (timeout_ok and cancel_ok):
                lacking = " and ".join(
                    what
                    for what, ok in (("TimeoutError", timeout_ok), ("CancelledError", cancel_ok))
                    if not ok
                )
                offenders.append(
                    f"{_GATEWAY_REL}:{node.lineno} ({func.name}: {proc_name}) lacks a "
                    f"{lacking} arm that tree-kills the proc"
                )
    assert audited >= 10, (
        f"only {audited} proc wait sites found in {_GATEWAY_REL} — the audit's "
        "wait matcher no longer matches the file's idiom; fix the matcher "
        "rather than deleting the ratchet"
    )
    assert not offenders, (
        "wait_for on a spawned proc without kill+reap discipline (issue "
        "#4210):\n  " + "\n  ".join(offenders) + "\n\nWrap the wait in explicit "
        "TimeoutError and CancelledError arms that call _kill_and_reap (or the "
        "startup-child kill/reap pair) on the proc before returning/re-raising."
    )
