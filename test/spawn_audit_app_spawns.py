"""The spawn audit's benign-spawn rows for the builtin apps under ``apps/builtins/``.

``test_spawn_audit.py`` checks the unrouted spawns in ``src/kiro_crew`` against
``BENIGN_SPAWNS``, the union of this table and ``BENIGN_PACKAGE_SPAWNS`` in
``spawn_audit_helpers.py``. A row is keyed by ``<relpath>::<enclosing function>``, and a
comment above a row or a run of rows justifies them; the rule for adding one is the
comment above ``BENIGN_SPAWNS`` in ``test_spawn_audit.py``.
"""

from __future__ import annotations

BENIGN_APP_SPAWNS: frozenset[str] = frozenset(
    {
        # Eight pre-existing spawns in one app's own test module, invisible to this
        # audit until receivers were derived from each file's imports: they are
        # reached through a function-local ``import subprocess as sp``. Every one is
        # a fixed ``git`` argv (``init``, ``config``, ``add``, ``commit``,
        # ``worktree add``, ``rev-parse``) against a repository the test just created
        # under ``tmp_path``; nothing is agent-influenced and no shell is used. They
        # are tests of the dogfood spine's own refusals -- a planted pre-commit hook,
        # a planted clean filter, a repointed worktree gitdir -- so the git repo IS
        # the fixture, and routing them through the agent sandbox would sandbox the
        # fixture rather than the thing under test. Same class as the Ops Mission
        # Control ledger-sync tests below.
        "apps/builtins/auto_improvement/tests/test_dogfood_learnings_clone.py::test_a_planted_clean_filter_does_not_run",
        "apps/builtins/auto_improvement/tests/test_dogfood_learnings_clone.py::test_a_planted_clean_filter_does_not_run_in_a_linked_worktree",
        "apps/builtins/auto_improvement/tests/test_dogfood_learnings_clone.py::test_a_planted_pre_commit_hook_does_not_run",
        "apps/builtins/auto_improvement/tests/test_dogfood_learnings_clone.py::test_a_repointed_worktree_gitdir_is_refused",
        "apps/builtins/auto_improvement/tests/test_dogfood_learnings_clone.py::test_clone_setup_checkout_pins_the_attributes",
        "apps/builtins/auto_improvement/tests/test_dogfood_learnings_clone.py::test_red_base_staging_does_not_dereference_a_credential_symlink",
        "apps/builtins/auto_improvement/tests/test_dogfood_learnings_clone.py::test_the_pin_refuses_a_symlink_and_fails_closed",
        "apps/builtins/auto_improvement/tests/test_dogfood_learnings_clone.py::test_the_pin_survives_a_normal_repo",
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
        "apps/builtins/auto_improvement/backend/clone_setup.py::_origin_urls",
        "apps/builtins/auto_improvement/backend/clone_setup.py::_repository_is_safe",
        "apps/builtins/auto_improvement/backend/clone_setup.py::_gh_prefers_ssh",
        "apps/builtins/auto_improvement/backend/clone_setup.py::_run",
        "apps/builtins/auto_improvement/backend/clone_setup.py::list_clone_branches",
        # The git-clone spawn's argv is built from validated owner/repo
        # components, never raw user text, so it is benign.
        "apps/builtins/auto_improvement/backend/clone_setup.py::_setup_safe_clone",
        # NOT subprocess spawns: the AST heuristic matches ``asyncio.run`` (attr
        # ``run`` on base ``asyncio``), used here only to drive the async
        # ``SessionAgentRunner._approve`` coroutine from a synchronous test. No child
        # process is created — the test's provider is a local stub with no argv at all.
        # Same classification as the ``asyncio.run`` sites above
        # (cli_commands_app.py::_cleanup_app_crons_from_scheduler, cli_doctor.py::_doctor).
        "apps/builtins/auto_improvement/tests/test_dogfood_learnings_tool_gate.py"
        "::test_approval_is_logged_then_granted",
        "apps/builtins/auto_improvement/tests/test_dogfood_learnings_tool_gate.py"
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
        "apps/builtins/auto_improvement/tests/test_dogfood_learnings_run_lifecycle.py"
        "::test_importing_the_backend_does_not_pull_the_profile_tree",
        "apps/builtins/auto_improvement/backend/commit.py::_git",
        # `git apply --index` on the QUEUED diff, literal argv against the configured clone.
        # Was keyed to `commit_finding` until the checkout+apply block was extracted here so
        # the draft-PR route could reuse it (the detector keys by the ENCLOSING function).
        "apps/builtins/auto_improvement/backend/commit.py::materialize_queued_diff",
        "apps/builtins/auto_improvement/backend/deps.py::_gh_authenticated",
        # `<running interpreter> -m pytest --version` from an empty temp dir: a literal
        # argv, no repo or agent input, so no conftest can load. It asks whether the gate's
        # own interpreter has pytest before a run starts.
        "apps/builtins/auto_improvement/backend/deps.py::gate_pytest_problem",
        # Its test fixture: `python -m venv --without-pip` into tmp_path, then that bare
        # interpreter's `-c "import pytest"`. Literal argv, nothing agent-influenced; the
        # bare venv is the fixture (an interpreter without pytest, like the desktop bundle).
        "apps/builtins/auto_improvement/tests/test_gate_pytest_preflight.py::python_without_pytest",
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
        "apps/builtins/auto_improvement/spine/driver_commit.py::_apply",
        "apps/builtins/auto_improvement/spine/driver_commit.py::_stage_winner",
        "apps/builtins/auto_improvement/spine/driver_git.py::_git",
        "apps/builtins/auto_improvement/spine/driver_publish.py::_push_with_rebase",
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
        "apps/builtins/auto_improvement/spine/agent_runner_authoring.py::author_bug_fix",
        "apps/builtins/auto_improvement/spine/agent_runner_authoring.py::author_perf_fix",
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
        "apps/builtins/auto_improvement/tests/test_dogfood_learnings_publish.py"
        "::test_a_diff_that_does_not_apply_never_reaches_the_pipeline",
        # Same basis: literal `git config/add/commit/cat-file/diff` against a tmp_path clone,
        # proving a credential planted in the COMMITTER IDENTITY is refused. Real git is
        # required rather than a stub: the point is which bytes git itself puts in the commit
        # object, which canned output cannot demonstrate. Nothing is agent-influenced -- the
        # argv is literal and the cwd is the test's own tmp_path.
        "apps/builtins/auto_improvement/tests/test_dogfood_learnings_publish.py"
        "::test_a_credential_in_the_committer_identity_refuses_to_publish",
        # NOT a subprocess spawn: the AST heuristic matches ``asyncio.run`` (attr ``run`` on
        # base ``asyncio``) driving the async ``_approve`` coroutine so a REAL SEL
        # write can be read back off disk. No child process is created.
        "apps/builtins/auto_improvement/tests/test_dogfood_learnings_tool_gate.py"
        "::test_a_real_sel_write_produces_a_readable_event",
        # Same basis: a fixed `git init --bare` + clone + push against a tmp_path, driving
        # one-click commit end to end in a clone whose origin is neutralized exactly as
        # production leaves it. Nothing here is agent-influenced — the argv is literal, the
        # cwd is the test's own tmp_path, and the "remote" is a local bare repo. This is
        # the test that proves `commit_finding` can still fetch its base after
        # `_disable_push`; the bug it pins was invisible to every mocked test.
        "apps/builtins/auto_improvement/tests/test_dogfood_learnings_clone.py::_git",
        "apps/builtins/auto_improvement/tests/test_dogfood_learnings_commit.py::_git",
        "apps/builtins/auto_improvement/tests/test_dogfood_learnings_publish.py::_git",
        "apps/builtins/auto_improvement/tests/test_dogfood_learnings_tracks.py::_git",
        "apps/builtins/auto_improvement/tests/test_dogfood_learnings_commit.py"
        "::_upstream_and_clone",
        "apps/builtins/auto_improvement/tests/test_dogfood_learnings_commit.py"
        "::test_a_stale_local_ref_is_not_used_when_a_url_is_configured",
        "apps/builtins/auto_improvement/tests/test_dogfood_learnings_commit.py"
        "::test_the_queued_diff_is_committed_and_pushed",
        # Same basis: a fixed bare-repo + clone against a tmp_path, asserting that a
        # non-default branch is really checked out inside a push-disabled clone (the run
        # was silently measuring the DEFAULT branch). Literal argv, test-owned cwd.
        # Only the functions that CONTAIN a spawn are listed: the detector keys by the
        # enclosing function, so a test that merely calls these helpers is not a spawn
        # site and the staleness check rejects it as a masking entry.
        "apps/builtins/auto_improvement/tests/test_dogfood_learnings_clone.py"
        "::test_a_remote_only_branch_is_checked_out_without_a_fetch",
        # Multi-cycle staging test: literal `git` argv against a tmp_path bare repo,
        # asserting cycle-2's checkout does not orphan cycle-1's kept commit.
        "apps/builtins/auto_improvement/tests/test_dogfood_learnings_publish.py"
        "::test_staging_stays_on_the_local_branch_across_cycles",
        # Its inner `git` helper: literal argv against the tmp_path bare repo above.
        "apps/builtins/auto_improvement/tests/test_dogfood_learnings_publish.py::git",
        # The provisional-commit fail-closed test spawns `git rev-parse HEAD` inline (not
        # via a helper) to assert HEAD did not move; literal argv against a tmp_path repo.
        "apps/builtins/auto_improvement/tests/test_dogfood_learnings_publish.py"
        "::test_a_failing_commit_returns_false_not_true",
        # Same basis: `git status --porcelain` inline against a tmp_path repo, asserting a
        # REJECTED provisional commit left nothing staged for the next candidate to inherit.
        "apps/builtins/auto_improvement/tests/test_dogfood_learnings_publish.py"
        "::test_a_rejected_commit_leaves_no_diff_staged_for_the_next_candidate",
        # Same basis: literal `git show`/`ls-tree` against a tmp_path bare repo + clone,
        # asserting a manual draft stages ITS queued diff instead of publishing whatever a
        # later cycle left at HEAD.
        "apps/builtins/auto_improvement/tests/test_dogfood_learnings_commit.py"
        "::test_drafting_an_older_finding_does_not_publish_a_later_one",
        # Same basis: literal `git rev-parse`/`status` against a tmp_path clone, asserting a
        # failed draft's rollback restores the branch to the base it was fetched at.
        "apps/builtins/auto_improvement/tests/test_dogfood_learnings_commit.py"
        "::test_a_rollback_restores_the_branch_to_its_fetched_base",
        # Same basis: literal `git rev-parse` against a tmp_path clone, asserting a REJECTED
        # push leaves no commit on the branch for the next run to adopt as its baseline.
        "apps/builtins/auto_improvement/tests/test_dogfood_learnings_commit.py"
        "::test_a_failed_push_leaves_no_commit_behind",
        # Same basis: literal `git clone`/`log`/`show` against a tmp_path bare repo, asserting
        # two concurrent operator commits never merge into one commit.
        "apps/builtins/auto_improvement/tests/test_dogfood_learnings_commit.py"
        "::test_two_concurrent_commits_do_not_merge_into_one",
        # Its inner `_repo` helper: fixed `git init/clone/commit/push` argv against a tmp_path
        # bare repo, building the local-vs-remote base case for the credential-scan self-diff.
        "apps/builtins/auto_improvement/tests/test_dogfood_learnings_publish.py::_repo",
        # The direct-push HEAD-identity tests. `::run` is a stub pre-push reviewer that
        # amends the test's OWN tmp_path clone to reproduce the race; the test function spawns
        # `git rev-parse --short HEAD` inline to assert that amend really landed before it
        # asserts the refusal. Both are fixed `git` argv against a per-test tmp_path repo with
        # no remote -- nothing in the argv, the cwd or the resolved binary is agent-influenced,
        # and the enclosing `::git` helper above already covers the repo builder.
        "apps/builtins/auto_improvement/tests/test_dogfood_learnings_publish.py::run",
        "apps/builtins/auto_improvement/tests/test_dogfood_learnings_publish.py"
        "::test_a_reviewer_amend_between_commit_and_push_refuses_to_publish",
        # Same basis: the shadow-ref variant additionally spawns `git branch <short-sha> HEAD`
        # and two `git rev-list -1` reads, all literal argv with cwd and `-C` pinned to the
        # same per-test tmp_path clone, asserting the injection applied before the refusal.
        "apps/builtins/auto_improvement/tests/test_dogfood_learnings_publish.py"
        "::test_a_ref_named_the_abbreviation_cannot_shadow_the_committed_object",
        # The time-of-check/time-of-use pair, same basis again: `::_scan_then_amend` stands in
        # for a background amend landing while the credential scan runs, and the two test
        # functions read HEAD back with `git rev-list -1` to prove the injection applied and
        # to pin the published revision to a full object id.
        "apps/builtins/auto_improvement/tests/test_dogfood_learnings_publish.py::_scan_then_amend",
        # `::_gate_then_swap_head` is the same injection one step earlier -- it moves HEAD
        # right after the pre-scan gate returns, to prove the scan is bound to the object id
        # rather than to `HEAD`.
        "apps/builtins/auto_improvement/tests/test_dogfood_learnings_publish.py"
        "::_gate_then_swap_head",
        "apps/builtins/auto_improvement/tests/test_dogfood_learnings_publish.py"
        "::test_the_credential_scan_reads_the_committed_object_not_head",
        # Same basis: this one additionally spawns `git replace` and a `git show` control, to
        # prove a replacement ref cannot substitute what the credential scan reads.
        "apps/builtins/auto_improvement/tests/test_dogfood_learnings_publish.py"
        "::test_a_replacement_ref_cannot_substitute_what_the_scan_reads",
        "apps/builtins/auto_improvement/tests/test_dogfood_learnings_publish.py"
        "::test_a_move_after_the_scan_cannot_change_what_is_published",
        "apps/builtins/auto_improvement/tests/test_dogfood_learnings_publish.py"
        "::test_an_unmoved_head_still_publishes",
        # Same basis: literal `git rev-parse`/`diff`/`reset` against a tmp_path repo, showing a
        # left-behind provisional commit lands in the NEXT bug PR's range.
        "apps/builtins/auto_improvement/tests/test_dogfood_learnings_publish.py"
        "::test_a_chained_head_would_contaminate_the_next_branch",
        "apps/builtins/auto_improvement/tests/test_dogfood_learnings_clone.py" "::_disabled_clone",
        "apps/builtins/auto_improvement/tests/test_dogfood_learnings_clone.py"
        "::_remote_with_two_branches",
        # Same basis: a fixed `git init/add/commit` + an uncommitted edit against a tmp_path
        # repo, proving `_export_is_durable` retains a clone that holds UNCOMMITTED work (an
        # empty committed diff over a dirty tree is not "no work"). Literal argv, test-owned
        # cwd, dead origin — nothing agent-influenced. `_run` is the test's inline git helper;
        # the test function itself also spawns `git init/add/commit` directly.
        "apps/builtins/auto_improvement/tests/test_dogfood_learnings_run_lifecycle.py::_run",
        "apps/builtins/auto_improvement/tests/test_dogfood_learnings_run_lifecycle.py"
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
        # Stand-in for a run's writer: fixed command and argv, cwd=tmp_path, not agent-influenced.
        "apps/builtins/auto_improvement/tests/test_runner.py"
        "::test_a_run_another_live_process_wrote_reads_as_running_until_that_process_exits",
        # NOT a subprocess spawn: the AST heuristic matches ``asyncio.run`` (attr
        # ``run`` on base ``asyncio``). This is a TEST helper that drives one
        # in-process aiohttp handler coroutine to completion so the PR-action routes
        # can be exercised without a running loop. No child process is created and
        # nothing is agent-influenced — the payloads are literals in the test file.
        # Same classification as the other ``asyncio.run`` sites in this list
        # (cli_doctor.py::_doctor, cli_commands_app.py::_cleanup_app_crons_from_scheduler).
        "apps/builtins/issue_radar/tests/test_pr_actions.py::_await",
        # Same construct, same classification, for the assignee-route tests: an
        # ``asyncio.run`` that drives one in-process aiohttp handler coroutine to
        # completion. No child process, and every payload is a literal in the test
        # file.
        "apps/builtins/issue_radar/tests/test_assignees.py::_await",
        # NOT subprocess spawns: the AST heuristic matches ``asyncio.run`` (attr
        # ``run`` on base ``asyncio``). These four TEST functions drive Code Review
        # Sage's ``_save_runs`` coroutine to completion without a running loop --
        # the registry write is a coroutine because it is blocking file IO that
        # must be offloaded off the event loop (see the offload rationale on
        # ``_write_runs``). No child process is created here and nothing is
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
        # the registry write does not target a predictable ``runs.json.tmp`` that a
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
        "apps/builtins/md_notebook/server_host.py::_pick_folder_sync",
        # Fixed argv `<file manager> <dir>` to reveal a vault's `.trash` in
        # Finder / the desktop file manager. No shell. The binary is an absolute
        # module constant (`/usr/bin/open`, `/usr/bin/xdg-open`) resolved from a
        # platform map and existence-checked — deliberately NOT from PATH, whose
        # front is agent-writable and could hold an `open` shim. The single
        # argument is not caller-supplied: `api_trash_open` takes no path and
        # derives the directory from the vault descriptor via
        # `vault_mutation_path`, which rejects `..`, absolute values and any
        # symlink escaping the vault, so the argv cannot be pointed elsewhere.
        "apps/builtins/md_notebook/server_host.py::_reveal_folder_sync",
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
        "apps/builtins/issue_radar/backend/azure_client_cli.py::_az_run",
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
        # _scratch_name_is_ignored decides WHERE the probe's scratch may live, and
        # is the fourth spawn of the same probe -- so it is already inside the
        # sandbox server.py applied to the sync step, exactly as the three above.
        # It spawns `<git> -C <repo> check-ignore -q --no-index <name>` from the
        # same sources: the binary is the sync's _trusted_bin git (never a PATH
        # search), the repo is the operator-configured checkout, the four flags are
        # literals, and <name> is `_SCRATCH_PREFIX` plus a fixed literal suffix --
        # a module-level constant, not a path any caller supplies and not a path
        # that exists. It only READS: `check-ignore` resolves the ignore rules and
        # answers in its exit code, writing nothing. An unanswerable exit is read
        # as "not ignored", so a blocked or failing spawn can only make the probe
        # fall back to TMPDIR, never widen what it may touch.
        "apps/builtins/dev_fleet/npm_preflight.py::_scratch_name_is_ignored",
        # _frontend_build_already_current is the STRONGER build-skip predicate
        # that wraps _install_already_proven (listed directly above) and adds one
        # read-only spawn: `<git> -C <repo> rev-parse <ref>:website`. Same three
        # sources as its wrapped sibling -- the binary is the sync's _trusted_bin
        # git (never a PATH search), the repo is the operator-configured checkout,
        # and <ref> is the sync's own per-PID base ref, never agent-supplied. It
        # is fixed list-argv, shell-free, and only READS (it resolves a tree id to
        # compare against the staged bundle's fingerprint); nothing is written.
        # Consistent with npm_preflight.py::_install_already_proven / ::_extract
        # above and git_divergence.py::count_divergence below -- and refusing it
        # while the function it wraps is listed would make the same subprocess
        # benign when called directly and forbidden through a one-line wrapper.
        "apps/builtins/dev_fleet/npm_preflight.py::_frontend_build_already_current",
        # _frontend_worktree_clean is one of that predicate's two guards. One
        # read-only spawn: `<git> -C <repo> status --porcelain
        # --untracked-files=normal -- website`. Fixed list-argv, shell-free, no
        # agent-supplied component -- the binary is the sync's _trusted_bin git
        # (threaded in via _frontend_build_already_current, never a PATH search),
        # the repo is the operator-configured checkout, and the subcommand,
        # flags and pathspec are literals. It only READS the working-tree status
        # (writes nothing). Same class as
        # npm_preflight.py::_install_already_proven / ::_extract above and
        # git_divergence.py::count_divergence below.
        "apps/builtins/dev_fleet/npm_preflight.py::_frontend_worktree_clean",
        # _frontend_tree_complete is the predicate's other guard: the on-disk
        # node_modules completeness check. One read-only spawn: `<npm> ls --all`
        # with cwd set to <repo>/website. Fixed list-argv, shell-free, no
        # agent-supplied component -- the binary is the sync's _trusted_bin npm
        # (the same npm probe() uses), the cwd is the operator-configured
        # checkout's website subtree, and the args are literals. `npm ls` only
        # WALKS the installed tree against the lockfile (writes nothing, runs no
        # lifecycle scripts). Same class as npm_preflight.py::probe, whose npm
        # spawn is listed above.
        "apps/builtins/dev_fleet/npm_preflight.py::_frontend_tree_complete",
        # Foreground last-resort restart (Make Live on hosts with no drivable
        # service manager): a detached `kirocrew restart --port <marker port>`,
        # fixed argv whose binary is validated (basenamed kirocrew, absolute,
        # executable) from the gateway's own keystone-fenced run-marker or
        # shutil.which — never agent input. Deliberately NOT sandboxed for the
        # same reason as cli_server_restart.py::_spawn_detached_gateway above: the child
        # must outlive and REPLACE the gateway (kill + respawn + own session),
        # which a scoped/sandboxed child cannot do.
        "apps/builtins/dev_fleet/gateway_service.py::default_detached_spawn",
        # TEST-ONLY, and the spawn IS the thing under test: the crew bundle
        # curator's contract (PACKAGING-CONTRACT T1) is a COMMAND -- `python -m
        # packaging.build ... ` printing `SMC_BUNDLE_JSON=<path>` as its last
        # stdout line -- and the deploy driver invokes it exactly that way. An
        # in-process call would prove the function works and leave the contract
        # the driver actually depends on untested. Fixed argv (`sys.executable
        # -m packaging.build`), no shell=True, cwd is the crew root (which is the
        # driver's own cwd, and what makes this tree's `packaging` win over the
        # PyPA distribution for that child), and every path argument is a
        # tmp_path. Nothing here is agent-derived.
        "apps/builtins/aws_control/crew/packaging/tests/test_producer.py"
        "::test_cli_build_prints_bundle_json_last_line",
        "apps/builtins/aws_control/crew/packaging/tests/test_producer.py"
        "::test_cli_plan_writes_template_without_bundle",
        # TEST-ONLY, and the spawn is the same fixed `sys.executable -m
        # packaging.build` CLI invocation as its two siblings above — this one
        # proves the COLD-CACHE property that the child writes no __pycache__
        # beside the module it imports (nor into the real checkout). No
        # shell=True; cwd is the test's own tmp_path; the module is made
        # importable through PYTHONPATH pointing at a `tmp_path` copy of the
        # package plus CREW_ROOT (via `_child_env`), and every path argument
        # (--out, --source, the copy root) is a tmp_path. Nothing here is
        # agent-derived. Sandbox-routing would defeat the test: it exists to
        # observe where a real interpreter drops bytecode on a real cold cache,
        # which a scrubbed-env/filesystem-scoped wrapper would move or forbid.
        "apps/builtins/aws_control/crew/packaging/tests/test_producer.py"
        "::test_cli_subprocess_leaves_no_pycache_in_the_source_tree",
    }
)
