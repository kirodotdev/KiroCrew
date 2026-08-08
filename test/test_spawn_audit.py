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

# Spawn helpers called as a BARE NAME rather than ``module.attr`` -- they are
# imported directly, so the receiver check above cannot see them. Without this
# the audit goes blind the moment a call site moves to the wrapper.
_SPAWN_NAMES = {"create_subprocess_limited"}

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
# ``create_subprocess_limited`` is the preferred form: it delivers the same
# limits AFTER exec via the spawn shim instead of in a fork child of this
# threaded gateway. The two ``*_preexec`` names remain valid only for the
# synchronous spawns that have not moved yet.
_PREEXEC_TOKENS = (
    "create_subprocess_limited",
    "resource_limit_preexec",
    "session_host_preexec",
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
        "acp/runtime.py::_get_rss_tree_mb",
        # Console-entry self-heal for stale editable installs: ONE fixed
        # `python -m pip install -e <repo>` argv, no shell. The repo path is
        # derived from the module's own __file__ (never user/agent input) and
        # only when setup.cfg + src/kiro_crew exist there. Runs before the
        # package imports, so it cannot route through sandboxed_spawn_argv —
        # mirrors dashboard/handlers/updates.py::_venv_pip_install below.
        "_bootstrap.py::_self_heal",
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
        # Syntax-checks the auth recipe the SOPs hand to an agent, via `bash -n` on the
        # extracted code block. Fixed argv, no shell, input piped on stdin and never
        # executed. The snippet contains `${URL%%\?*}`, whose backslash is easy to
        # mangle when editing markdown, and a recipe that will not parse sends the cron
        # agent back to improvising — which is the failure this whole test exists for.
        "apps/builtins/ops_mission_control/tests/test_config_routes.py"
        "::test_the_auth_recipe_is_runnable_shell",
        # Diagnostics support-bundle version probe: fixed argv
        # ``["kiro-cli", "--version"]`` with a 5s timeout, no shell, no cwd, and
        # no agent-influenced args — it only stamps the collected kiro-cli
        # version into versions.txt. The binary name is a module constant; a
        # resource ceiling / sandbox adds nothing to a `--version` call.
        "diagnostics.py::_kiro_cli_version",
        # Tailnet origin derivation (RFC: rfc-tailnet-dashboard-access): one
        # fixed argv, ``["<tailscale>", "status", "--json"]``, with a 3s timeout,
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
        "dashboard/tailnet.py::_run_json",
        "apps/backend.py::_proc_start_time",
        "apps/backend.py::_resolve_nvm_path",
        "apps/backend.py::stop_app_backend",
        # py-spy attach for `kirocrew perf sample --pid`: fixed list-argv (no
        # shell=True), binary resolved via shutil.which rather than from input,
        # and every value is either a range-validated int (pid/seconds/rate) or a
        # path passed as a flag VALUE. NOT sandboxed because py-spy's whole job is
        # reading another process's memory (ptrace / task_for_pid) — a sandbox that
        # scrubbed that capability would break the feature it is guarding. Gated
        # behind KIROCREW_DEBUG and reachable only from the CLI.
        "cli_perf.py::_sample_out_of_process",
        # gh-CLI open-PR enumeration: fixed `gh api` list-argv (no shell=True);
        # owner/repo are validated to ^[A-Za-z0-9._-]+$ by adapters.parse_repo_url
        # and only fill the API path (bounded to api.github.com). NOT sandboxed
        # because gh needs the host's own authenticated credentials.
        "apps/builtins/code_review_sage/sage_lib/pipeline.py::list_open_prs",
        # TEST-ONLY: spawns `sys.executable -c <literal>` to prove the candidate
        # read-modify-write lock holds across PROCESSES, which is what review
        # workers actually are. A single-process test cannot observe the loss it
        # covers. Fixed argv, no shell=True, no model-derived input -- the only
        # variables are a tmpdir path and a loop index.
        "apps/builtins/code_review_sage/tests/test_learning.py::test_concurrent_processes_both_land",
        # auto-improvement: fixed `git`/`gh`/`ruff` argv against the OPERATOR-chosen
        # repository. Same class as code_reviewer/git.py and issue_radar's gh/glab
        # spawns: the repo is selected by the operator through the Connect endpoint,
        # and `clone`/`target_url` are deliberately EXCLUDED from the config PUT
        # allowlist precisely so the agent cannot retarget them. No shell=True, no
        # argv[0] from model output. The agent's own edits happen inside a throwaway
        # worktree of a push-disabled clone, which is where its blast radius is
        # contained; these calls are the harness around it, not the agent's hands.
        "apps/builtins/auto_improvement/backend/clone_setup.py::_disable_push",
        "apps/builtins/auto_improvement/backend/clone_setup.py::_gh_prefers_ssh",
        "apps/builtins/auto_improvement/backend/clone_setup.py::_ok",
        "apps/builtins/auto_improvement/backend/clone_setup.py::_run",
        "apps/builtins/auto_improvement/backend/clone_setup.py::list_clone_branches",
        "apps/builtins/auto_improvement/backend/clone_setup.py::setup_safe_clone",
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
        # Code Review Sage repo discovery — same rationale as list_open_prs above
        # and as Issue Radar's _gh_run: fixed `gh api` list-argv (never
        # shell=True), bounded to api.github.com, and NOT sandbox-routed because
        # gh must reach the host's OWN authenticated credentials (~/.config/gh +
        # the keychain), which the sandbox would hide.
        #   • run_gh_json — the single `gh api` chokepoint. The only non-constant
        #     input is the API path, and every caller in this module builds it
        #     from a module constant plus a URL-encoded login (see below); the jq
        #     filters are hardcoded module constants.
        #   • current_login — a wholly FIXED argv (`gh api user --jq .login`) with
        #     no interpolation at all. It is a separate spawn site only because
        #     `--jq .login` emits a bare string, which the JSONL dict parser in
        #     run_gh_json cannot represent.
        # The login that reaches the events path is what gh itself reported for
        # the authenticated user (not agent input) and is quoted with
        # urllib.parse.quote(safe="") before interpolation. The `gh` binary is
        # resolved through discovery.gh_bin(), which reuses source_providers'
        # validated resolution, so a shim on the agent-writable front of PATH is
        # refused rather than executed.
        "apps/builtins/code_review_sage/sage_lib/discovery.py::current_login",
        "apps/builtins/code_review_sage/sage_lib/discovery.py::run_gh_json",
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
        # NOT a subprocess spawn: the AST heuristic matches ``asyncio.run`` (attr
        # ``run`` on base ``asyncio``). This is a TEST helper that drives one
        # in-process aiohttp handler coroutine to completion so the PR-action routes
        # can be exercised without a running loop. No child process is created and
        # nothing is agent-influenced — the payloads are literals in the test file.
        # Same classification as the other ``asyncio.run`` sites in this list
        # (cli_doctor.py::_doctor, cli_commands.py::_cleanup_app_crons_from_scheduler).
        "apps/builtins/issue_radar/tests/test_pr_actions.py::_await",
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
        # (apps/dependencies.py::_run_aim removed — App Kit capability deps now
        # resolve through the CapabilityManager seam, so the resolver spawns no
        # subprocess at all and needs no allowlist entry.)
        # Browser Mode setup/install path, run only from the dashboard settings
        # save (off the event loop) or the `kirocrew browse setup` CLI. Fixed
        # argv of trusted node-toolchain tools resolved via find_node_tool
        # (npm/npx/node) plus the ``playwright install <engine>`` subcommand,
        # where ``engine`` is validated against the fixed BROWSER_ENGINES
        # allowlist before it can reach argv — never free agent input. Mirrors
        # cli.py::_ensure_node / env.py::_run below, which shell the same
        # node/ensure-node toolchain and are benign for the same reason.
        "browser/setup.py::_resolve_playwright_core_cli",
        "browser/setup.py::_run",
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
        # The update check's git side: fixed `git fetch` / `rev-parse` / `show` /
        # `diff` list-argv (no shell=True) run in KIROCREW_PROJECT_DIR, an operator
        # environment value, never agent input. Read-only version comparison —
        # nothing here writes to the tree.
        "dashboard/handlers/updates.py::_check_git_checkout",
        "dashboard/handlers/updates.py::_venv_pip_install",
        "dashboard/handlers/updates.py::api_update_apply",
        "dashboard/handlers_system.py::_collect_system_metrics",
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
        "instances/diagnostics.py::_run_ok",
        "instances/diagnostics.py::_run_stdout",
        "instances/ssh_tunnel_manager.py::_ps_lines",
        "instances/ssh_tunnel_manager.py::start",
        "instances/token_mint.py::mint_remote_token",
        "instances/token_mint.py::run_remote_kirocrew",
        "mcp_core.py::_get_ppid",
        "mcp_discovery.py::sync_to_agent_config",
        "mcp_gateway/backend.py::spawn_backend",
        "mcp_gateway/gatewayd.py::main",
        "mcp_gateway/manager.py::_spawn_once",
        "mcp_gateway/stub.py::main",
        "mcp_playwright_proxy.py::run_proxy",
        # Read-only `git config` / `git ls-remote --get-url` resolving which
        # remote the update would fetch from, for the `updates.source` pin. Fixed
        # list-argv (no shell=True), no agent input: the branch lands mid-key
        # (`branch.<x>.remote`) so it cannot lead with a dash, and the remote
        # name — which is read out of git config and COULD — is passed after
        # `--`. Must NOT be sandboxed: it reads the real checkout's git metadata.
        "platform/update_governance.py::_git",
        "mcp_shared.py::_get_ppid",
        "platform_compat.py::_current_user_sid",
        "platform_compat.py::_posix_process_parent_map",
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
        "pod/runtime.py::derive_port",
        "pod/runtime.py::recent_journal",
        "sandbox.py::_probe_sandbox_exec",
        "sandbox.py::_ssh_supports_accept_new",
        # The chokepoint wrapper itself. It spawns whatever argv it is handed, so
        # it cannot route on its own behalf — its CALLERS are the ones this audit
        # holds to sandboxed_spawn_argv / wrap_argv, and they still appear here
        # individually because _SPAWN_NAMES collects bare-name calls to it.
        "sandbox.py::create_subprocess_limited",
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
        "slack/gateway.py::_check_missing_deps",
        "slack/gateway.py::_init_services",
        "testing/harness.py::spawn_feature_gateway",
        # Apple on-device speech (macOS only). None of these takes an agent-authored
        # command: the argv is a fixed toolchain path, the helper Kiro Crew itself
        # compiled, or ffmpeg — and every variable part is a positional argument to
        # execve (no shell), so a hostile value can only be a bad filename, not a
        # second command. `_to_native_audio` mirrors the already-allowlisted
        # `transcribe.py::_run_whisper_cli`: same ffmpeg invocation on the same
        # user-supplied audio path. `_build_helper` runs swiftc over a file that ships
        # inside the package, writing to the data home's `run/` dir (sensitive-path
        # fenced, 0700). The three spawns that EXECUTE the compiled helper
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
