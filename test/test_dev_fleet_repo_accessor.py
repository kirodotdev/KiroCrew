"""``MAIN_REPO`` reaches git and the filesystem only through ``_repo()``.

``MAIN_REPO`` reaches git and the filesystem only through the accessors.

Dev Fleet represents "no main checkout found" as an empty string in
``MAIN_REPO``. That sentinel is fail-open at any call site that consumes the
global directly: ``git -C ""`` does not fail — it silently runs against the
backend process's working directory — and ``Path("")`` is ``Path(".")``, so an
unguarded consumer operates on an arbitrary directory and returns plausible
results. ``_repo_read()`` centralizes the guard: it returns the path or
raises ``RepoNotConfigured``, which the HMAC middleware converts to the 409
``repo_not_configured`` boundary. ``_repo()`` is the MUTATING accessor and adds
one refusal on top — a checkout served read-only — and it reaches the path
through ``_repo_read()``, so exactly one function still reads the global.

Two enforcement tiers (same pattern as ``test_apps_instances_loop_offload.py``):

- Behavior tests: ``_repo_read()`` raises on the empty sentinel and returns the
  path otherwise; ``_repo()`` additionally raises ``RepoReadOnly`` while the
  read-only state is set. Both preserve the exception types the middleware
  boundary maps.
- AST ratchet: outside the read accessor itself, a ``MAIN_REPO`` load may appear
  ONLY as a bare truthiness guard (``if MAIN_REPO:`` / ``not MAIN_REPO`` / a
  ``BoolOp`` operand). Any other load — a git argv element, a subprocess
  ``cwd=``, a ``Path(...)`` build, an f-string interpolation, a payload
  field — fails this test, so a future call site cannot silently reintroduce
  the fail-open shape.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
import os
import pathlib
import sys
import tempfile
from types import SimpleNamespace

import pytest

from conftest import requires_symlinks
from kiro_crew.apps.builtins.dev_fleet import (
    fleet_state,
    gateway_routes,
    http_api,
    live,
    repository,
    runtime,
    server,
    worktree_ops,
)

# The read accessor is the ONLY function whose body may read the bare global: it
# IS the guard, and the mutating accessor delegates to it rather than loading the
# global a second time, so the count of functions touching MAIN_REPO stays at one.
# The startup hook's discovery/re-resolve runs on a local and writes the global
# exactly once (a Store, which this ratchet ignores), so even the assignment site
# needs no exemption — and a git call added to startup, where MAIN_REPO is most
# often still unresolved, is caught like anywhere else.
_DEV_FLEET_MODULES = (
    runtime,
    repository,
    live,
    fleet_state,
    worktree_ops,
    http_api,
    server,
)
_ALLOWED_LOADS = {(repository.__name__, "_repo_read")}


def _parent_map(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return parents


def _enclosing_function(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> str | None:
    cur: ast.AST | None = node
    while cur is not None:
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return cur.name
        cur = parents.get(cur)
    return None


def _is_bare_truthiness(node: ast.expr, parents: dict[ast.AST, ast.AST]) -> bool:
    """True when the load feeds a truthiness test and nothing else.

    Walking up from the Name, only ``BoolOp`` and ``not`` may intervene before
    the expression lands as the ``test`` of an ``if``/``while`` or a ternary.
    Any other intervening node (a call argument, a container literal, an
    f-string, an assignment value) means the VALUE escapes, which is exactly
    the shape the accessor exists to prevent.
    """
    child: ast.AST = node
    cur = parents.get(node)
    while cur is not None:
        if isinstance(cur, (ast.BoolOp, ast.UnaryOp)):
            if isinstance(cur, ast.UnaryOp) and not isinstance(cur.op, ast.Not):
                return False
            child = cur
            cur = parents.get(cur)
            continue
        if isinstance(cur, (ast.If, ast.While)):
            return cur.test is child
        if isinstance(cur, ast.IfExp):
            return cur.test is child
        return False
    return False


def test_main_repo_loads_only_via_accessor_or_truthiness() -> None:
    violations: list[str] = []
    for module in _DEV_FLEET_MODULES:
        tree = ast.parse(inspect.getsource(module))
        parents = _parent_map(tree)
        for node in ast.walk(tree):
            is_main_repo = (isinstance(node, ast.Name) and node.id == "MAIN_REPO") or (
                isinstance(node, ast.Attribute) and node.attr == "MAIN_REPO"
            )
            if not is_main_repo or not isinstance(node.ctx, ast.Load):
                continue  # assignments (Store) stay on the global by design
            func = _enclosing_function(node, parents)
            if (module.__name__, func) in _ALLOWED_LOADS:
                continue
            if _is_bare_truthiness(node, parents):
                continue
            violations.append(
                f"{module.__name__}:{node.lineno}: MAIN_REPO load in "
                f"{func or '<module>'} — route it through repository._repo()"
                " (or _repo_read() for a read-only consumer)"
            )
    assert not violations, (
        "MAIN_REPO's empty-string sentinel is fail-open when consumed "
        "directly (git -C '' runs against the process CWD). Use _repo():\n" + "\n".join(violations)
    )


def test_repo_accessor_raises_on_unresolved_checkout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(repository, "MAIN_REPO", "")
    with pytest.raises(repository.RepoNotConfigured):
        repository._repo()


def test_repo_accessor_returns_resolved_checkout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(repository, "MAIN_REPO", "/somewhere/kirocrew")
    assert repository._repo() == "/somewhere/kirocrew"


def test_read_accessor_raises_on_unresolved_checkout(monkeypatch: pytest.MonkeyPatch) -> None:
    """The weaker accessor still refuses the fail-open sentinel.

    It is the one the ratchet admits, so if it ever stopped raising here the
    ratchet would be guarding a function that hands out ``""``.
    """
    monkeypatch.setattr(repository, "MAIN_REPO", "")
    with pytest.raises(repository.RepoNotConfigured):
        repository._repo_read()


def test_read_accessor_serves_a_checkout_the_app_may_only_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Read-only is the whole point of the split: the generic surface still works."""
    monkeypatch.setattr(repository, "MAIN_REPO", "/somewhere/other-project")
    monkeypatch.setattr(repository, "_REPO_READ_ONLY_MSG", "read-only: no markers")
    assert repository._repo_read() == "/somewhere/other-project"


def test_mutating_accessor_refuses_a_checkout_the_app_may_only_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The refusal every mutating call site inherits without being touched.

    ``RepoReadOnly`` must keep sharing the ``RepoUnavailable`` base, because the
    degrade sites catch that base and their "not derivable" answer is the right
    one here too.
    """
    monkeypatch.setattr(repository, "MAIN_REPO", "/somewhere/other-project")
    monkeypatch.setattr(repository, "_REPO_READ_ONLY_MSG", "read-only: no markers")
    with pytest.raises(repository.RepoReadOnly) as caught:
        repository._repo()
    assert isinstance(caught.value, repository.RepoUnavailable)
    assert "read-only" in str(caught.value)


def test_mutating_accessor_allows_a_marker_checkout(monkeypatch: pytest.MonkeyPatch) -> None:
    """The gate does not fire when the state is genuinely empty."""
    monkeypatch.setattr(repository, "MAIN_REPO", "/somewhere/kirocrew")
    monkeypatch.setattr(repository, "_REPO_READ_ONLY_MSG", None)
    assert repository._repo() == "/somewhere/kirocrew"


def test_read_only_reason_reports_the_state_both_ways(monkeypatch: pytest.MonkeyPatch) -> None:
    """One reader for the route boundary, the payload and the row fields."""
    monkeypatch.setattr(repository, "_REPO_READ_ONLY_MSG", None)
    assert repository._read_only_reason() is None
    monkeypatch.setattr(repository, "_REPO_READ_ONLY_MSG", "read-only: no markers")
    assert repository._read_only_reason() == "read-only: no markers"


@pytest.mark.parametrize(
    "name, ok",
    [
        ("main", True),
        ("trunk", True),
        ("release/2.0", True),
        ("feature.x", True),
        # A leading dash is parsed as a FLAG by git once the name is
        # interpolated into an argv, so it must never be accepted.
        ("--exec=touch /tmp/pwn", False),
        ("-main", False),
        # ``..`` splits a rev range at the wrong place: ``origin/a..b..HEAD``.
        ("a..b", False),
        ("", False),
        ("main branch", False),
        ("main;rm", False),
    ],
)
def test_base_branch_names_are_constrained_before_reaching_an_argv(name: str, ok: bool) -> None:
    assert repository._plausible_branch_name(name) is ok


def test_every_local_base_candidate_survives_the_argv_constraint() -> None:
    """The fallback list and the argv guard must agree.

    A candidate the guard rejects would be published into ``BASE_BRANCH`` by the
    fallback loop without ever meeting ``_plausible_branch_name``, which only
    screens the remote's answer. Asserting over the tuple itself keeps a name
    added later from slipping past.
    """
    assert repository._LOCAL_BASE_CANDIDATES
    for candidate in repository._LOCAL_BASE_CANDIDATES:
        assert repository._plausible_branch_name(candidate) is True


def test_primary_checkout_resolution_reads_the_layout_instead_of_asking_git(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """The primary is resolved from git's layout files, with NO git spawn at all.

    git would answer this with `rev-parse --git-common-dir` and parse the repository's
    config to do it, following any `include.path` it names before answering -- the very
    hazard the include scan exists to bound, reached by the question that was supposed
    to precede it. So the answer comes from the ``.git`` pointer and ``commondir``, and
    the non-ASCII directory keeps the decode load-bearing: ``os.fsdecode``, so a path
    byte that is not valid UTF-8 survives as a surrogate rather than being replaced.
    """

    def _never(*_args, **_kwargs):
        raise AssertionError("resolving the primary checkout must not spawn git")

    monkeypatch.setattr(repository.subprocess, "run", _never)

    primary = tmp_path / "primär"
    gitdir = primary / ".git" / "worktrees" / "wt"
    gitdir.mkdir(parents=True)
    (gitdir / "commondir").write_text("../..\n", encoding="utf-8")
    linked = tmp_path / "linked"
    linked.mkdir()
    (linked / ".git").write_text(f"gitdir: {gitdir}\n", encoding="utf-8")

    assert repository._resolve_primary_checkout(str(linked)) == str(primary)

    # A primary checkout resolves to itself: its own `.git` IS the common dir.
    plain = tmp_path / "plain"
    (plain / ".git").mkdir(parents=True)
    assert repository._resolve_primary_checkout(str(plain)) == str(plain)

    # A path that is not a checkout comes back as named, for the fence to judge.
    bare = tmp_path / "not-a-checkout"
    bare.mkdir()
    assert repository._resolve_primary_checkout(str(bare)) == str(bare)


# --- base-branch resolution reads ONE remote ---------------------------------
#
# ``git remote`` lists names alphabetically, so a checkout carrying an archive or
# fork remote beside ``origin`` hands the first-listed one the casting vote. That
# remote decides ``BASE_BRANCH`` while ``_upstream_remote`` resolves to ``origin``
# independently, and ``/rebase`` rewrites onto ``{remote}/{BASE_BRANCH}`` — a base
# the upstream never published. These three pin which remote is consulted.


def _stub_base_branch_git(
    monkeypatch: pytest.MonkeyPatch,
    *,
    remotes: str,
    published: dict[str, str],
    local: set[str],
    head: str | None = None,
) -> list[str]:
    """Wire the two git readers ``_resolve_base_branch`` uses. Returns the ref probes."""
    probed: list[str] = []

    async def _run_cmd(argv, **_kwargs):
        assert argv[-1] == "remote", argv
        return 0, remotes, ""

    async def _git(_repo: str, *args: str, **_kw: object) -> str | None:
        if args[0] == "symbolic-ref":
            ref = args[-1]
            probed.append(ref)
            if ref == "HEAD":
                return head
            remote = ref.split("/")[2]
            published_head = published.get(remote)
            return f"{remote}/{published_head}" if published_head else None
        if args[0] == "rev-parse":
            name = args[-1].removeprefix("refs/heads/")
            return name if name in local else None
        raise AssertionError(f"unexpected git call: {args}")

    monkeypatch.setattr(repository, "MAIN_REPO", "/somewhere/other-project")
    monkeypatch.setattr(repository, "_REPO_INVALID_MSG", None)
    monkeypatch.setattr(repository, "BASE_BRANCH", "main")
    monkeypatch.setattr(runtime, "_run_cmd", _run_cmd)
    monkeypatch.setattr(repository, "_git", _git)
    return probed


@pytest.mark.asyncio
async def test_base_branch_ignores_a_remote_sorted_before_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An alphabetically earlier remote must not decide the rebase base.

    ``archive`` publishes one default and ``origin`` another. Only ``origin`` may
    be consulted, because ``_upstream_remote`` resolves to it and the two answers
    are combined into a single rev range.
    """
    probed = _stub_base_branch_git(
        monkeypatch,
        remotes="archive\norigin\n",
        published={"archive": "legacy-default", "origin": "trunk"},
        local=set(),
    )
    await repository._resolve_base_branch()
    assert repository.BASE_BRANCH == "trunk"
    assert probed == ["refs/remotes/origin/HEAD"]


@pytest.mark.asyncio
async def test_base_branch_reads_a_sole_remote_under_another_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One remote is unambiguous whatever it is called, so its answer is taken."""
    probed = _stub_base_branch_git(
        monkeypatch,
        remotes="kirocrew\n",
        published={"kirocrew": "release/3"},
        local=set(),
    )
    await repository._resolve_base_branch()
    assert repository.BASE_BRANCH == "release/3"
    assert probed == ["refs/remotes/kirocrew/HEAD"]


@pytest.mark.asyncio
async def test_base_branch_falls_back_locally_when_no_remote_is_unambiguous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Several remotes and no ``origin`` is ambiguous: ask the local branches."""
    local_default = repository._LOCAL_BASE_CANDIDATES[-1]
    probed = _stub_base_branch_git(
        monkeypatch,
        remotes="fork\nupstream\n",
        published={"fork": "a", "upstream": "b"},
        local={local_default},
    )
    await repository._resolve_base_branch()
    assert repository.BASE_BRANCH == local_default
    assert probed == []


@pytest.mark.asyncio
async def test_base_branch_takes_the_checked_out_branch_when_no_candidate_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A repository whose base is named something else answers with its own HEAD.

    No published remote default, and no name in ``_LOCAL_BASE_CANDIDATES`` exists, so
    every earlier tier declines. Keeping the default would label the primary row with
    a name that matches no ref and query ``{remote}/<that name>`` against it.
    """
    probed = _stub_base_branch_git(
        monkeypatch,
        remotes="origin\n",
        published={},
        local={"trunk"},
        head="trunk",
    )
    await repository._resolve_base_branch()
    assert repository.BASE_BRANCH == "trunk"
    assert probed == ["refs/remotes/origin/HEAD", "HEAD"]


@pytest.mark.asyncio
async def test_a_present_candidate_outranks_the_checked_out_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HEAD is the LAST tier, because a dev checkout sits on a feature branch.

    ``main`` exists here, so it is the base even though the checkout is parked on
    someone's branch -- taking HEAD would retarget every rebase and every ahead/behind
    reading at that branch for as long as it stays checked out.
    """
    candidate = repository._LOCAL_BASE_CANDIDATES[0]
    probed = _stub_base_branch_git(
        monkeypatch,
        remotes="origin\n",
        published={},
        local={candidate, "feature/some-work"},
        head="feature/some-work",
    )
    await repository._resolve_base_branch()
    assert repository.BASE_BRANCH == candidate
    assert "HEAD" not in probed


@pytest.mark.asyncio
async def test_an_implausible_head_is_refused_like_every_other_tier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The last tier is validated too, or it becomes the way a bad name gets in."""
    probed = _stub_base_branch_git(
        monkeypatch,
        remotes="origin\n",
        published={},
        local=set(),
        head="--upload-pack=touch /tmp/pwned",
    )
    await repository._resolve_base_branch()
    assert repository.BASE_BRANCH == "main"
    assert probed == ["refs/remotes/origin/HEAD", "HEAD"]


# --- reads leave the repository byte-identical -------------------------------


def test_optional_locks_are_off_for_every_git_this_handler_runs() -> None:
    """``git status`` rewrites the index unless optional locks are off.

    It is a read to its caller and a write to the repository: it refreshes the
    index's stat cache and saves it back under ``index.lock``. Every fleet render
    runs one per row, so without this a checkout the app may only read is modified
    on its ordinary path. Pinned on the env chokepoint rather than per call site,
    which is what makes a read added later inherit it.
    """
    assert runtime._GIT_ENV_NEUTRALIZERS["GIT_OPTIONAL_LOCKS"] == "0"


@pytest.mark.asyncio
async def test_run_cmd_puts_the_neutralizers_in_the_child_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dict is only a guarantee if the spawn actually carries it.

    Asserted through the spawn preparation, because that is the last place the env
    can be read before the child exists, and an entry dropped anywhere earlier
    would leave the dict stating a pin nothing applies.
    """
    seen: dict[str, str] = {}
    hidden: list[tuple[str, ...]] = []

    def _prepare(cmd, _mode, env=None, extra_hidden_dirs=()):
        seen.update(env or {})
        hidden.append(tuple(extra_hidden_dirs))
        return list(cmd), dict(env or {}), None

    monkeypatch.setattr(runtime, "sandboxed_spawn_argv", _prepare)
    monkeypatch.setattr(runtime, "_trusted_bin", lambda _name: "/usr/bin/git")

    async def _off_loop(fn, executor=None):
        return fn()

    monkeypatch.setattr(runtime, "shielded_prepare_off_loop", _off_loop)

    async def _no_child(*_a, **_kw):
        raise AssertionError("the env is read before the child spawns")

    monkeypatch.setattr(runtime.asyncio, "create_subprocess_exec", _no_child)
    with pytest.raises(AssertionError):
        await runtime._run_cmd(["git", "-C", "/somewhere/other-project", "status"])

    for key, value in runtime._GIT_ENV_NEUTRALIZERS.items():
        assert seen[key] == value
    # A bare `_run_cmd` asks for no extra mask: the credential homes are requested by
    # the FOREIGN read path in `_run_gated_git`, not by every spawn this handler makes.
    assert hidden == [()]


# --- the gateway's own boundary carries the refusal --------------------------


@pytest.mark.asyncio
async def test_gateway_repo_resolution_refuses_a_read_only_checkout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gateway routes skip the backend middleware, so the refusal lives here.

    Make Live is why it matters: it reaches ``_find_worktree_by_path`` and writes
    the live-target pointer, which would aim the running gateway at a checkout
    this app may only read.
    """

    async def _discovered() -> None:
        return None

    monkeypatch.setattr(repository, "ensure_main_repo_discovered", _discovered)
    monkeypatch.setattr(repository, "_REPO_READ_ONLY_MSG", "read-only: no markers")

    refused = await gateway_routes._ensure_repo()
    assert refused is not None
    assert refused.status == 409
    assert json.loads(refused.body)["code"] == "repo_read_only"

    monkeypatch.setattr(repository, "_REPO_READ_ONLY_MSG", None)
    assert await gateway_routes._ensure_repo() is None


# --- a fenced path is refused, never served read-only ------------------------


def test_read_only_adoption_asks_the_central_path_gate() -> None:
    """The gate runs off the loop, on BOTH spellings, and BEFORE the probes.

    ``sensitive_path_refusal`` waits on the bounded path-resolution pool, so calling
    it in the coroutine body pauses every other task on the gateway loop. It also
    has to see the path the operator NAMED: ``_resolve_primary_checkout`` rewrites a
    linked worktree to its primary, and a fenced worktree can have a primary outside
    the fence, so gating only the rewritten form clears it by a path that is not it.
    """
    body = inspect.getsource(repository.ensure_main_repo_discovered)
    assert "sensitive_path_refusal" not in body, "the gate must not run in the coroutine body"
    # Both spellings, each asked at its own time: the named path before anything
    # reads inside it, the resolved primary before the probes that read inside THAT.
    named = body.index('_fenced_reason, configured, ""')
    resolved = body.index('_fenced_reason, "", discovered')
    assert named < resolved
    # Anchored on the CALL text, not the bare names: the comment above these calls
    # names both functions, so a bare-name index would compare against prose.
    assert named < body.index("_discover_main_repo, configured"), "asked before the path is walked"
    assert resolved < body.index(
        "_configured_filter_commands(discovered)"
    ), "asked before git reads inside it"

    probe = inspect.getsource(repository._fenced_reason)
    assert "for candidate in (configured, resolved)" in probe


def test_a_foreign_read_runs_in_the_strict_tier_with_the_clearance_bound_to_the_spawn() -> None:
    """The sandbox tier bounds the harm; the clearance's PLACEMENT bounds the window.

    ``_run_cmd`` hands ``_GIT_TRUSTED_HELPERS`` only to ``standard``, which its own
    comment calls the gateway-controlled tier. A foreign checkout is repo-controlled,
    so its reads belong in ``strict``: a filter driver that wins the race then runs
    with no credential store visible and no helper config, which is the part no
    check-then-spawn can achieve. The clearance sits in ``pre_spawn``, evaluated
    after sandbox preparation with the spawn as the only await that follows, so the
    config proved clean is the config the child reads. Taking it ahead of the call
    instead would leave the preparation hop in between, and that hop is unbounded.
    """
    # The gated spawn, which `_git` is now a thin wrapper over: every foreign read
    # goes through it, so a call site cannot reach the credential-bearing tier by
    # forgetting to ask for anything.
    body = inspect.getsource(repository._run_gated_git)
    assert 'mode = "strict"' in body
    assert "pre_spawn=_clear" in body
    # The clearance lives inside the hook, not in a bare await ahead of the call.
    bare = body.index("_assert_read_cleared")
    assert "except RepoUnreadable" in body[bare : bare + 300]


@pytest.mark.asyncio
async def test_the_clearance_uses_the_disposition_its_caller_captured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A concurrent switch must not turn the probe off for a read aimed at the old path.

    ``_git`` samples the read-only disposition to choose its branch, and the spawn's
    target path is fixed at that moment. Sampling the same global again inside the
    clearance can answer differently, because a poll re-resolving to the app's own
    managed checkout clears it -- and the probe would then be skipped entirely while
    the child still runs git inside the foreign checkout.
    """
    probed: list[tuple[str, bool]] = []

    async def record(path: str, *, read_only: bool) -> None:
        probed.append((path, read_only))

    monkeypatch.setattr(repository, "_assert_read_cleared", record)
    monkeypatch.setattr(repository, "_REPO_READ_ONLY_MSG", "read-only: no markers")

    async def run_cmd(argv, *, timeout=6, mode="standard", pre_spawn=None, **_kw):
        # The switch the operator's own remedy produces, landing between the
        # disposition sample and the moment the hook runs.
        monkeypatch.setattr(repository, "_REPO_READ_ONLY_MSG", None)
        if pre_spawn is not None:
            await pre_spawn()
        return 0, "ok", ""

    monkeypatch.setattr(runtime, "_run_cmd", run_cmd)

    assert await repository._git("/foreign/checkout", "rev-parse", "HEAD") == "ok"
    assert probed == [("/foreign/checkout", True)]


@pytest.mark.asyncio
async def test_the_clearance_honours_its_argument_over_the_global(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The probe runs on the caller's ``read_only=True`` even with the global cleared.

    This is the other half: ``_git`` passing the captured disposition buys nothing if
    the clearance reads the global anyway. Driven directly, with the global already
    switched, the probe must still run and still refuse a configured driver.
    """

    def drivers(_path: str) -> tuple[set[str], str | None]:
        return {"filter.lfs.clean"}, None

    monkeypatch.setattr(repository, "_configured_filter_commands", drivers)
    monkeypatch.setattr(repository, "_REPO_READ_ONLY_MSG", None)

    with pytest.raises(repository.RepoUnreadable, match="filter drivers"):
        await repository._assert_read_cleared("/foreign/checkout", read_only=True)

    # And the opposite direction: the app's own checkout is not probed at all.
    def unreachable(_path: str) -> tuple[set[str], str | None]:
        raise AssertionError("the managed checkout must not be probed")

    monkeypatch.setattr(repository, "_configured_filter_commands", unreachable)
    monkeypatch.setattr(repository, "_REPO_READ_ONLY_MSG", "read-only: no markers")
    await repository._assert_read_cleared("/our/own/checkout", read_only=False)


@pytest.mark.asyncio
async def test_a_read_whose_checkout_moved_is_refused_before_it_spawns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The clearance proved against the old checkout says nothing about the new one.

    A read already in flight carries the path it captured, so once a different
    checkout resolves, spawning would run git inside the one being left behind. The
    refusal is transient by design -- nothing latches, and the next poll reads the
    checkout now configured.
    """
    spawned: list[list[str]] = []

    async def cleared(path: str, *, read_only: bool) -> None:
        return None

    monkeypatch.setattr(repository, "_assert_read_cleared", cleared)
    monkeypatch.setattr(repository, "_REPO_READ_ONLY_MSG", "read-only: no markers")

    async def run_cmd(argv, *, timeout=6, mode="standard", pre_spawn=None, **_kw):
        repository._drop_checkout_derived_state()
        if pre_spawn is not None:
            await pre_spawn()
        spawned.append(list(argv))
        return 0, "ok", ""

    monkeypatch.setattr(runtime, "_run_cmd", run_cmd)

    with pytest.raises(repository.RepoUnreadable, match="checkout changed while reading"):
        await repository._git("/foreign/checkout", "rev-parse", "HEAD")


def test_every_foreign_repo_probe_is_sandboxed() -> None:
    """No probe reads a checkout this app does not own outside the sandbox.

    git parses the target repository's config on EVERY command and follows
    ``include.path`` while doing so, so a foreign repo can point a probe at any
    file, a credential store included. ``--includes`` decides only whether an
    include is followed for the value PRINTED, not whether the file is opened, and
    the fence gates the checkout path rather than the files its config names.
    """
    src = inspect.getsource(repository)
    # Every spawn in this module goes through the helper that wraps the chokepoint.
    assert "subprocess.run(" not in src
    # NO call sites remain on the checkout probe: discovery answers its ref reads from
    # git's layout files, and the config probes ask about a vetted SNAPSHOT instead of
    # the repository. Asking git anything about a live foreign checkout makes it parse
    # that repository's config and follow its includes to answer, so the entry point is
    # kept -- with its include gate -- and nothing in this module reaches for it.
    assert src.count("_probe_git(") == 1  # the definition alone
    assert src.count("_probe_git_snapshot(") == 3  # the definition plus its two uses
    # ONE sandboxed spawn, shared by both probe entry points, so neither can drift to
    # an unwrapped one. The checkout probe adds the include gate; the snapshot probe
    # asserts it was handed a snapshot and no `-C`.
    core = inspect.getsource(repository._spawn_bounded_probe)
    assert 'sandboxed_spawn_argv(argv, mode="strict", env=env)' in core
    assert "run_limited(" in core
    assert "os.unlink(cleanup)" in core
    assert src.count("sandboxed_spawn_argv(") == 1

    checkout_probe = inspect.getsource(repository._probe_git)
    assert "_include_refusal(path)" in checkout_probe
    assert "_spawn_bounded_probe(" in checkout_probe

    snapshot_probe = inspect.getsource(repository._probe_git_snapshot)
    assert '"-C" in argv' in snapshot_probe
    assert "_spawn_bounded_probe(" in snapshot_probe


def test_an_unbuildable_sandbox_reads_as_unread_not_as_clean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A probe that could not run has not answered ``no``.

    The filter cascade must report the scope unread, and the extension question
    must keep its third answer -- ``False`` there would drop ``--worktree`` and
    admit a repo whose worktree scope holds a driver.
    """

    def _no_sandbox(*_args, **_kwargs):
        raise RuntimeError("sandbox unavailable: no backend")

    monkeypatch.setattr(repository, "sandboxed_spawn_argv", _no_sandbox)
    drivers, unread = repository._snapshot_filter_keys(
        repository._ConfigRead("config", "[core]\n", None), "/usr/bin/git", {}
    )
    assert drivers == []
    # The reason names the SCOPE, not the mechanism: an unbuildable sandbox and a
    # reply too large to retain are both unread, and the banner needs to say which
    # scope went unread either way.
    assert unread and "config" in unread
    # And the boolean question keeps its third answer: an unbuildable sandbox is
    # unread, not "the scope is not live" -- dropping the scope on a repository that
    # HAS it would admit a driver nobody looked for.
    assert repository._snapshot_bool("[core]\n", "/usr/bin/git", {}, "a.b") is None


def test_permanent_security_refusals_are_audited(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each permanent refusal is a permission decision, so it leaves a record.

    The sibling denials this app takes at its HTTP boundaries are audited; these two
    are taken during discovery and surfaced only as banner text, so without an event
    the decision is invisible. The UNMEASURED verdicts stay unaudited on purpose:
    an unresolved path gate and an unread config scope decided nothing.
    """
    body = inspect.getsource(repository.ensure_main_repo_discovered)
    assert '_audit_security_refusal("path-fenced"' in body
    # Wrapped by the formatter, so the kind sits on its own line.
    assert '"filter-drivers", discovered' in body
    assert body.count("_audit_security_refusal(") == 2
    # The two unmeasured branches compose their message and audit nothing.
    unverified_at = body.index("unverified = True")
    assert "_audit_security_refusal" not in body[unverified_at : body.index("else:", unverified_at)]

    events: list[dict] = []

    class _Sel:
        def log_tool_invocation(self, **kwargs):
            events.append(kwargs)

    monkeypatch.setattr(runtime, "_sel", lambda: _Sel())
    repository._audit_security_refusal("path-fenced", "/home/someone/.aws", "protected")
    assert events and events[0]["outcome"] == "denied"
    assert events[0]["tool_name"] == "dev-fleet:repo-path-fenced"

    # A failing sink must not mask the refusal it describes.
    class _Boom:
        def log_tool_invocation(self, **_kwargs):
            raise RuntimeError("sink down")

    monkeypatch.setattr(runtime, "_sel", lambda: _Boom())
    repository._audit_security_refusal("filter-drivers", "/repo", "filter.evil.process")


def test_the_path_gate_is_asked_in_both_directions(monkeypatch: pytest.MonkeyPatch) -> None:
    """A path that CONTAINS a protected location is refused too.

    ``sensitive_path_refusal`` answers "is this inside a protected location", which
    leaves the ancestor open: a home directory carrying a ``.git`` is not itself
    protected yet holds ``~/.aws`` and ``~/.ssh`` underneath, and ``/api/disk`` walks
    every worktree root recursively.
    """
    monkeypatch.setattr(repository, "sensitive_path_refusal", lambda _p: None)
    monkeypatch.setattr(repository, "path_contains_sensitive", lambda _p: True)
    reason = repository._fenced_reason("/home/someone", "")
    assert reason and "contains a protected location" in reason

    monkeypatch.setattr(repository, "path_contains_sensitive", lambda _p: False)
    assert repository._fenced_reason("/srv/other-project", "") is None


def test_a_fenced_path_is_never_read_before_it_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The refusal must come before any probe that reads INSIDE the path.

    ``git config --includes`` follows ``include.path``, so a probe of a protected
    location is exactly the read the fence exists to prevent: a repo-controlled
    include can name a credential file and git will read it as config. Walking the
    path and resolving its primary read inside it too. So a fenced candidate must be
    refused with NOTHING having touched it.
    """

    def _boom(*_args, **_kwargs):  # pragma: no cover - must not be reached
        raise AssertionError("a fenced path was read before it was refused")

    monkeypatch.setattr(repository, "_discover_main_repo", _boom)
    monkeypatch.setattr(repository, "_resolve_primary_checkout", _boom)
    monkeypatch.setattr(repository, "_configured_filter_commands", _boom)
    monkeypatch.setattr(repository, "_is_kirocrew_checkout", _boom)
    monkeypatch.setattr(repository, "_is_git_checkout", _boom)
    monkeypatch.setattr(repository, "_repo_source_hint", lambda: "Configured in dev_fleet.repo.")
    monkeypatch.setattr(
        repository,
        "_configured_main_repo_checked",
        lambda: ("/home/someone/.aws", True),
    )
    monkeypatch.setattr(
        repository, "sensitive_path_refusal", lambda p: "protected: credential store"
    )
    monkeypatch.setattr(repository, "_DISCOVERY_DONE", False)
    monkeypatch.setattr(repository, "_DISCOVERY_LOCK", None)
    monkeypatch.setattr(repository, "MAIN_REPO", "")
    monkeypatch.setattr(repository, "_REPO_INVALID_MSG", None)
    monkeypatch.setattr(repository, "_REPO_READ_ONLY_MSG", None)

    asyncio.run(repository.ensure_main_repo_discovered())

    assert repository._REPO_INVALID_MSG is not None
    assert "protected location" in repository._REPO_INVALID_MSG
    # Served as unreadable, never as a read-only fleet.
    assert repository._REPO_READ_ONLY_MSG is None


def test_a_fenced_path_is_refused_outright(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fenced verdict publishes an unreadable refusal, never a read-only fleet."""
    monkeypatch.setattr(repository, "MAIN_REPO", "/somewhere/fenced")
    monkeypatch.setattr(repository, "_REPO_READ_ONLY_MSG", None)
    monkeypatch.setattr(repository, "_REPO_INVALID_MSG", "refused: protected location")

    assert repository._read_only_reason() is None
    with pytest.raises(repository.RepoUnreadable):
        repository._repo_read()


def test_both_path_spellings_reach_the_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """The named path and the primary it normalizes to are asked separately."""
    asked: list[str] = []

    def _refusal(path: str, base_dir=None):
        asked.append(path)
        return "fenced" if path == "/named/linked-worktree" else None

    monkeypatch.setattr(repository, "sensitive_path_refusal", _refusal)
    assert repository._fenced_reason("/named/linked-worktree", "/other/primary") == "fenced"
    assert asked == ["/named/linked-worktree"]

    asked.clear()
    assert repository._fenced_reason("/clean/named", "/clean/primary") is None
    assert asked == ["/clean/named", "/clean/primary"]


def test_an_unverified_gate_answer_does_not_latch() -> None:
    """A resolver stall is a measurement nobody took, so the next poll retries.

    Latched, one transient timeout would stand as a permanent refusal asserting a
    verdict that was never reached, and the retry the gate itself advises could
    never fire.
    """
    body = inspect.getsource(repository.ensure_main_repo_discovered)
    assert "is_unverifiable_path_refusal(fenced)" in body
    assert "_DISCOVERY_DONE = bool(discovered) and not unverified" in body
    stall = body.index("is_unverifiable_path_refusal(fenced)")
    fence = body.index("is a protected location")
    assert stall < fence, "the stall branch must be taken before the protected-location wording"


def test_a_read_only_verdict_reopens_on_a_changed_config() -> None:
    """A read-only resolution is against a path the operator named and may correct.

    Both reopen gates key on the verdict pair, so a corrected `repo_path` re-resolves
    instead of serving the stranger's repository until the gateway restarts.
    """
    for source in (
        inspect.getsource(repository._invalid_resolution_is_stale),
        inspect.getsource(repository.ensure_main_repo_discovered),
    ):
        assert "(_REPO_INVALID_MSG or _REPO_READ_ONLY_MSG) and MAIN_REPO" in source


# --- a repository that would execute code on a read is refused ---------------


def test_executable_filter_drivers_refuse_the_adoption() -> None:
    """A filter driver is a COMMAND, and ``git status`` is what would run it.

    The env neutralizers pin the named execution vectors but cannot enumerate driver
    names, so a repo-local filter is the one that stays reachable on a read. Reading
    config executes nothing, which is what makes asking first the whole remedy.
    """
    body = inspect.getsource(repository.ensure_main_repo_discovered)
    assert "_configured_filter_commands(discovered)" in body
    assert "if filters:" in body
    # The unread scope is its own branch: refusing a repository for a driver nobody
    # saw asserts a measurement that was never taken.
    assert "elif filters_unread:" in body
    assert repository._FILTER_COMMAND_SUFFIXES == (".clean", ".smudge", ".process")


# The filter probe's own regexp, which is what tells its config read apart from
# the include-directive read that shares every other token with it.
_FILTER_RE = r"^filter\."


def _unwrapped_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let a probe reach the scripted git answer on a host with no sandbox backend.

    ``_probe_git`` fail-closes when no backend exists (CI containers and Windows
    have none), which is the production behaviour these tests are NOT about: they
    are about which question git is asked and what its answer means. The chokepoint
    itself is pinned by ``test_every_foreign_repo_probe_is_sandboxed``, and the
    fail-closed mapping by ``test_an_unbuildable_sandbox_reads_as_unread_not_as_clean``,
    so neutralising the wrapper here cannot hide either.
    """
    monkeypatch.setattr(
        repository,
        "sandboxed_spawn_argv",
        lambda argv, mode="standard", **kw: (list(argv), dict(kw.get("env") or {}), None),
    )


def _filter_probe_calls(
    monkeypatch: pytest.MonkeyPatch,
    answers: dict[str, tuple[int, str]],
    *,
    texts: dict[str, str] | None = None,
) -> list[list[str]]:
    """Run the probe against scripted git answers, returning the argv it used.

    ``answers`` maps a distinguishing argv token to the ``(returncode, stdout)`` git
    should answer with, and ``texts`` supplies the CONFIG CONTENT each scope holds --
    which is what the probe reads now, because git is asked about a snapshot of bytes
    this module already vetted rather than about the live repository. Nothing executes:
    the point is which questions are asked, of what.
    """
    seen: list[list[str]] = []
    content = {"config": "[core]\n", "config.worktree": "[core]\n"} if texts is None else texts

    monkeypatch.setattr(
        repository,
        "_repo_owned_config_reads",
        lambda _path: (
            [repository._ConfigRead(name, text, None) for name, text in content.items()],
            None,
        ),
    )

    def _run(argv, _env=None, **kwargs):
        seen.append(list(argv))
        rc, out = 1, ""
        for token, (code, text) in answers.items():
            if token in argv:
                rc, out = code, text
                break
        return repository.subprocess.CompletedProcess(argv, rc, out, "")

    monkeypatch.setattr(runtime, "_trusted_bin", lambda _name: "git")
    monkeypatch.setattr(repository, "_probe_git_snapshot", _run)
    return seen


def test_filter_probe_follows_includes_on_every_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``include.path`` resolves during a read, so the probe must follow it too.

    For a SPECIFIC scope git defaults include-following OFF, so a driver reached
    through ``[include] path = other.cfg`` answers an empty list to a probe that
    omits ``--includes`` while still executing on the next content-touching read.
    """
    seen = _filter_probe_calls(monkeypatch, {"extensions.worktreeConfig": (0, "true\n")})
    drivers, unread = repository._configured_filter_commands("/somewhere/other-project")

    assert (drivers, unread) == ([], None)
    # The FILTER reads, specifically. The include-directive probe deliberately
    # omits ``--includes``, so that the question cannot follow the thing it asks
    # about; asserting over every config read would forbid that.
    filter_reads = [argv for argv in seen if _FILTER_RE in argv]
    assert filter_reads
    for argv in filter_reads:
        assert "--includes" in argv


def test_filter_probe_asks_the_worktree_scope_only_when_it_is_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--worktree`` off is not a scope git reads, and asking anyway errors.

    Without ``extensions.worktreeConfig`` git ignores ``config.worktree`` entirely
    and refuses the query outright on any repository that has a linked worktree —
    the ordinary shape of what this app enumerates — so an unconditional ask turns
    a filter-free repository into a refusal.
    """
    # The scope flags are gone: each scope is asked about its OWN snapshot, so what is
    # observable is how many filter reads happen. Off, `config.worktree` is skipped
    # entirely; on, it is read as well.
    off = _filter_probe_calls(monkeypatch, {"extensions.worktreeConfig": (1, "")})
    assert repository._configured_filter_commands("/repo") == ([], None)
    assert len([argv for argv in off if _FILTER_RE in argv]) == 1

    on = _filter_probe_calls(monkeypatch, {"extensions.worktreeConfig": (0, "true\n")})
    assert repository._configured_filter_commands("/repo") == ([], None)
    assert len([argv for argv in on if _FILTER_RE in argv]) == 2


def test_filter_probe_reports_an_unread_scope_apart_from_a_driver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unreadable scope is a measurement nobody took, not a configured driver."""
    _filter_probe_calls(
        monkeypatch,
        {
            "extensions.worktreeConfig": (0, "true\n"),
            _FILTER_RE: (128, ""),
        },
    )
    drivers, unread = repository._configured_filter_commands("/repo")

    assert drivers == []
    assert unread and "could not be read" in unread


def test_filter_probe_reports_a_reason_when_git_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime, "_trusted_bin", lambda _name: None)
    drivers, unread = repository._configured_filter_commands("/somewhere/other-project")
    assert drivers == []
    assert unread and "unverified" in unread


def test_every_read_on_a_read_only_checkout_re_takes_the_clearance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Discovery's answer is about the repository as it was THEN.

    A driver written into the config after the adoption is read by the next git
    invocation and by nothing else, so the clearance is re-taken at the chokepoint
    every read passes through, and the repository stops being served at once. It is
    re-taken in ``pre_spawn``, so the stub below has to honour that hook the way
    ``_run_cmd`` does: the gate's whole point is that it sits after sandbox
    preparation with only the spawn behind it.
    """
    monkeypatch.setattr(repository, "_REPO_READ_ONLY_MSG", "read-only: /repo ...")
    monkeypatch.setattr(
        repository,
        "_configured_filter_commands",
        lambda _path: (["filter.evil.process"], None),
    )

    async def _never(*_args, pre_spawn=None, **_kwargs):
        if pre_spawn is not None:
            refusal = await pre_spawn()
            if refusal is not None:
                return -1, "", refusal
        raise AssertionError("git ran against a checkout that would execute its driver")

    monkeypatch.setattr(runtime, "_run_cmd", _never)

    with pytest.raises(repository.RepoUnreadable) as caught:
        asyncio.run(repository._git("/repo", "rev-parse", "HEAD"))
    assert "filter.evil.process" in str(caught.value)


def test_the_product_s_own_checkout_pays_no_clearance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The trust boundary predates this mode: its config is the operator's own."""
    monkeypatch.setattr(repository, "_REPO_READ_ONLY_MSG", None)

    def _refuse(_path):  # pragma: no cover - must not be reached
        raise AssertionError("probed the checkout this app owns")

    monkeypatch.setattr(repository, "_configured_filter_commands", _refuse)

    async def _ok(*_args, **_kwargs):
        return 0, "clean\n", ""

    monkeypatch.setattr(runtime, "_run_cmd", _ok)
    assert asyncio.run(repository._git("/repo", "status", "--porcelain")) == "clean"


def test_a_read_aimed_at_a_replaced_checkout_is_refused_in_either_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The checkout gate is not a sub-clause of the read-only gate.

    ``git_dir`` is fixed by the caller, and the resolution that lands on a managed
    checkout CLEARS the read-only verdict. A read still aimed at the foreign path it
    captured then samples the cleared disposition, so gating on the generation only
    inside the read-only branch leaves that read running in ``standard`` mode -- the
    tier ``_run_cmd`` hands the trusted credential helpers to. Both values are
    captured at entry and the generation is compared on every path.
    """
    monkeypatch.setattr(repository, "_REPO_READ_ONLY_MSG", None)

    def _refuse(_path):  # pragma: no cover - the disposition is genuinely not read-only
        raise AssertionError("probed a checkout this app resolved as its own")

    monkeypatch.setattr(repository, "_configured_filter_commands", _refuse)
    spawned: list[list[str]] = []

    async def _run_cmd(argv, *, timeout=6, mode="standard", pre_spawn=None, **_kw):
        # The resolution the operator's own remedy produces, landing after the
        # disposition and generation are sampled and before the child execs.
        repository._drop_checkout_derived_state()
        if pre_spawn is not None:
            refusal = await pre_spawn()
            if refusal is not None:
                return -1, "", refusal
        spawned.append(list(argv))
        return 0, "ok", ""

    monkeypatch.setattr(runtime, "_run_cmd", _run_cmd)

    with pytest.raises(repository.RepoUnreadable) as caught:
        asyncio.run(repository._git("/foreign/checkout", "status", "--porcelain"))
    assert "/foreign/checkout" in str(caught.value)
    assert spawned == []


def test_a_foreign_checkout_never_gets_a_content_converting_git_verb(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sandbox tier cannot be the bound, so the bound is which verbs are run.

    A foreign read asks for the strict tier, but this app's backend is itself spawned
    inside a standard sandbox and a nested wrap is impossible by design, so the
    stricter tier contributes its ENV scrub only -- its file-level hides stay at the
    outer tier, where the credential directories remain visible. A driver written
    between the clearance and the exec would therefore run with them readable. So a
    verb that can invoke a repo-configured driver is not run at all: `status` reaches
    a `filter.*.clean` driver and `cherry` reaches `diff.*.textconv`, while resolving
    a ref converts no blob and can reach neither.
    """
    monkeypatch.setattr(repository, "_REPO_READ_ONLY_MSG", "read-only: /foreign ...")

    spawned: list[list[str]] = []

    async def _run_cmd(argv, *, timeout=6, mode="standard", pre_spawn=None, **_kw):
        if pre_spawn is not None:
            refusal = await pre_spawn()
            if refusal is not None:
                return -1, "", refusal
        spawned.append(list(argv))
        return 0, "answered\n", ""

    monkeypatch.setattr(runtime, "_run_cmd", _run_cmd)
    monkeypatch.setattr(repository, "_configured_filter_commands", lambda _p: ([], None))

    # Refused, and refused as UNMEASURED rather than as an error: the banner is for a
    # repository that cannot be read at all, not for a field this mode never measures.
    for verb in ("status", "cherry"):
        assert asyncio.run(repository._git("/foreign/checkout", verb, "--porcelain")) is None
    assert spawned == []

    # A ref read still goes through, so the fleet keeps every field it can honestly
    # measure -- this is a narrowing of what is asked, not of what is served.
    assert asyncio.run(repository._git("/foreign/checkout", "rev-parse", "HEAD")) == "answered"
    assert [a[3] for a in spawned] == ["rev-parse"]


def test_an_unmeasured_working_tree_is_not_reported_clean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`dirty` False asserts a measurement; on a foreign checkout nobody took one."""
    monkeypatch.setattr(repository, "_REPO_READ_ONLY_MSG", "read-only: /foreign ...")

    async def _answer(path: str, *args: str, **_kw) -> str | None:
        # Exactly what the safelist leaves reachable: refs answer, `status` does not.
        return None if args[0] == "status" else "abcdef1234567"

    async def _remote() -> str:
        return "origin"

    monkeypatch.setattr(repository, "_git", _answer)
    monkeypatch.setattr(repository, "_upstream_remote", _remote)
    info = asyncio.run(repository._git_info("/foreign/checkout"))
    assert info["dirty"] is None


def test_a_mutation_is_refused_while_the_new_checkout_s_identity_is_still_derived(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Publishing the path and deriving what it MEANS cannot be one step.

    The resolvers read the checkout, so the path is visible to them first -- and in
    that window the mutation gate sees a managed checkout while ``BASE_BRANCH`` is
    still the shared default the switch reset it to. A rebase then replays a worktree
    onto whichever base that name happens to match in the NEW repository, which
    succeeds silently when it exists and which nothing here can undo.
    """
    monkeypatch.setattr(repository, "MAIN_REPO", "/srv/ours")
    monkeypatch.setattr(repository, "_REPO_INVALID_MSG", None)
    monkeypatch.setattr(repository, "_REPO_READ_ONLY_MSG", None)

    # Settled: the mutating accessor answers, which is the state every other test is in.
    monkeypatch.setattr(repository, "_REPO_DERIVING", False)
    assert repository._repo() == "/srv/ours"
    assert repository._mutations_settling_reason() is None

    # Mid-derivation: refused, and refused as READ-ONLY so the route boundary's
    # existing branch owns the response rather than a new code the client must learn.
    monkeypatch.setattr(repository, "_REPO_DERIVING", True)
    with pytest.raises(repository.RepoReadOnly) as caught:
        repository._repo()
    assert "still being read" in str(caught.value)

    # The READ accessor is deliberately unaffected: the resolvers themselves go
    # through it, so refusing there would deadlock the very window this closes.
    assert repository._repo_read() == "/srv/ours"

    # And the page's read-only mode is untouched, so a sub-second transition does not
    # flash the banner or withdraw every control on each re-resolution.
    assert repository._read_only_reason() is None


def test_the_route_boundary_refuses_a_mutation_mid_derivation_too() -> None:
    """A rebase is rooted at the worktree it rebases and never passes the accessor.

    So the method gate has to ask the same question, or the one mutation that most
    needs the answer is the one that never hears it.
    """
    source = inspect.getsource(http_api)
    gate = source[source.index("read_only = repository._read_only_reason()") :][:1400]
    assert "repository._mutations_settling_reason()" in gate
    assert "raise repository.RepoReadOnly(settling)" in gate


def test_the_resolvers_themselves_run_inside_the_window(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Observed from inside, not asserted about the source.

    A gate nobody opens is the same as no gate, so the proof has to come from a
    resolver asking the question while it runs -- and the same run must show the
    window CLOSED once discovery returns, or one failed attempt would refuse every
    mutation for the life of the process.
    """
    seen: list[str | None] = []

    async def _observe() -> None:
        seen.append(repository._mutations_settling_reason())

    async def _observe_fallbacks() -> None:
        seen.append(repository._mutations_settling_reason())

    async def _observe_remote() -> str:
        seen.append(repository._mutations_settling_reason())
        return "origin"

    repo = tmp_path / "ours"
    (repo / "src" / "kiro_crew").mkdir(parents=True)
    (repo / ".git").mkdir()
    (repo / "pyproject.toml").write_text("[project]\n", encoding="utf-8")

    monkeypatch.setattr(repository, "_resolve_base_branch", _observe)
    monkeypatch.setattr(repository, "_load_fallback_repos", _observe_fallbacks)
    monkeypatch.setattr(repository, "_upstream_remote", _observe_remote)
    monkeypatch.setattr(repository, "_configured_main_repo_checked", lambda: (str(repo), True))
    monkeypatch.setattr(repository, "_fenced_reason", lambda *_a: None)
    monkeypatch.setattr(repository, "_resolve_primary_checkout", lambda p: p)
    monkeypatch.setattr(repository, "_DISCOVERY_DONE", False)
    monkeypatch.setattr(repository, "_DISCOVERY_LOCK", None)
    monkeypatch.setattr(repository, "_LATCHED_RESOLVED", "")
    monkeypatch.setattr(repository, "_REPO_DERIVING", False)
    monkeypatch.setattr(runtime, "_GIT_TRUSTED_HELPERS", {})

    asyncio.run(repository.ensure_main_repo_discovered())

    # Every resolver ran inside the window.
    assert seen and all(r is not None for r in seen), seen
    # And the window closed, so the next mutation is not refused forever.
    assert repository._mutations_settling_reason() is None


def test_an_unanswerable_worktree_scope_question_is_not_a_clean_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The extension question failing must not silently drop the scope.

    A repository with ``extensions.worktreeConfig`` ON and ``filter.x.process`` in
    ``config.worktree`` would otherwise be admitted as filter-free, and the next
    ``git status`` would run that driver. Both failure shapes answer unread: a
    non-zero exit git does not use for "key absent", and a spawn that raises.
    """
    _filter_probe_calls(monkeypatch, {"extensions.worktreeConfig": (128, "")})
    drivers, unread = repository._configured_filter_commands("/repo")
    assert drivers == []
    assert unread and "--worktree" in unread

    def _raise(_argv, **_kwargs):
        raise OSError("spawn failed")

    monkeypatch.setattr(runtime, "_trusted_bin", lambda _name: "git")
    _unwrapped_sandbox(monkeypatch)
    monkeypatch.setattr(repository.subprocess, "run", _raise)
    drivers, unread = repository._configured_filter_commands("/repo")
    assert drivers == []
    assert unread


def test_a_local_driver_outranks_an_unread_worktree_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A measurement beats an unread verdict: the refusal names what was SEEN."""
    _filter_probe_calls(
        monkeypatch,
        {
            _FILTER_RE: (0, "filter.evil.process\n"),
            "extensions.worktreeConfig": (128, ""),
        },
    )
    drivers, unread = repository._configured_filter_commands("/repo")
    assert drivers == ["filter.evil.process"]
    assert unread is None


def test_a_changed_checkout_drops_every_cache_derived_from_the_old_one() -> None:
    """A memo about checkout A is an answer about a repository this app left.

    The read-only mode is what makes this reachable: a foreign checkout now
    RESOLVES, so `_upstream_remote` and `fleet_state._get_owner_repo` latch against
    it, where before they declined because `_repo_read()` raised. Carried across a
    corrected `repo_path`, that remote name reaches `git rebase {remote}/{base}` and
    the prune ancestry gate against the NEW checkout.
    """
    body = inspect.getsource(repository.ensure_main_repo_discovered)
    assert "if discovered != _LATCHED_RESOLVED:" in body
    assert "_drop_checkout_derived_state()" in body
    # Before the resolvers below re-read: each returns early on a latched value, so a
    # reset placed after them would leave the old answer standing.
    assert body.index("_drop_checkout_derived_state()") < body.index("_resolve_base_branch()")

    # fleet_state's caches live above repository in the component DAG, so they are
    # reached through the registry rather than by an import that would invert it.
    assert fleet_state._reset_checkout_derived_caches in repository._CHECKOUT_RESETS

    repository._UPSTREAM_REMOTE = "their-remote"
    repository._FALLBACK_REPOS = ["them/theirs"]
    repository.BASE_BRANCH = "trunk"
    fleet_state._OWNER_REPO = "them/theirs"
    fleet_state._PR_CACHE["branch"] = {"number": 1}
    fleet_state._FLEET_CACHE["data"] = {"worktrees": []}
    fleet_state._HTML_BASE = "https://github.com/them/theirs"
    fleet_state._CTX_CACHE["branch"] = {"issues": []}
    fleet_state._DISK.update({"status": "done", "total_mb": 42, "per": {"theirs": 42}})
    generation = repository._checkout_generation()

    repository._drop_checkout_derived_state()

    assert repository._UPSTREAM_REMOTE is None
    assert repository._FALLBACK_REPOS is None
    assert repository.BASE_BRANCH == "main"
    assert fleet_state._OWNER_REPO is None
    assert fleet_state._PR_CACHE == {}
    assert fleet_state._FLEET_CACHE["data"] is None
    # `_HTML_BASE` has no expiry, so nothing but this clear ends A's issue links.
    assert fleet_state._HTML_BASE is None
    assert fleet_state._CTX_CACHE == {}
    # The per-worktree map names A's worktrees, so freshness alone is not enough:
    # the numbers go back to the pre-measurement state, not merely to stale.
    assert fleet_state._DISK == {"status": "idle", "total_mb": None, "per": {}}
    assert fleet_state._DISK_COMPUTED_AT == 0.0
    # The counter is what lets a read already in flight see that it finished
    # against a different checkout than it started against.
    assert repository._checkout_generation() > generation


def test_a_fleet_build_that_lands_on_another_checkout_is_discarded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Clearing a cache does not stop a read already in flight from re-seeding it.

    The rows this build read describe checkout A. The reset that moved the counter
    emptied the cache, and this write lands after it, so storing the rows would put
    A's fleet back and serving them would answer a request about B with A's
    worktrees. The accessor resolves B by now, so building again reads the right one.
    """
    calls: list[str] = []

    async def build_then_switch() -> dict:
        calls.append("build")
        if len(calls) == 1:
            repository._drop_checkout_derived_state()
            return {"worktrees": [{"name": "theirs"}]}
        return {"worktrees": [{"name": "ours"}]}

    monkeypatch.setattr(fleet_state, "_build_fleet", build_then_switch)
    fleet_state._FLEET_CACHE["data"] = None
    fleet_state._FLEET_CACHE["ts"] = 0.0

    data = asyncio.run(fleet_state._fleet_build())

    assert len(calls) == 2
    assert data == {"worktrees": [{"name": "ours"}]}
    assert fleet_state._FLEET_CACHE["data"] == {"worktrees": [{"name": "ours"}]}


def test_a_checkout_that_keeps_moving_reports_instead_of_serving_an_empty_fleet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The retry is bounded, and the bound does not fall back to a false answer.

    An empty snapshot asserts an empty fleet, which is a measurement nobody took, so
    the exhausted case raises. ``RepoUnavailable`` is the base the degrade sites
    catch, and the `/fleet` route renders it as a banner the next poll clears.
    """

    async def always_switch() -> dict:
        repository._drop_checkout_derived_state()
        return {"worktrees": [{"name": "theirs"}]}

    monkeypatch.setattr(fleet_state, "_build_fleet", always_switch)
    fleet_state._FLEET_CACHE["data"] = None
    fleet_state._FLEET_CACHE["ts"] = 0.0

    with pytest.raises(repository.RepoUnavailable):
        asyncio.run(fleet_state._fleet_build())

    assert fleet_state._FLEET_CACHE["data"] is None


def test_a_disk_aggregation_that_lands_on_another_checkout_publishes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Withholding the freshness stamp is not enough: the numbers are A's worktrees.

    ``_disk`` serves stale-while-revalidate, so an unstamped result keeps being
    handed out under its own worktree names. The cleared state is restored instead,
    and the status must not stay "computing" or every later read short-circuits.
    """

    async def discover_then_switch() -> list[dict]:
        repository._drop_checkout_derived_state()
        return [{"path": "/elsewhere/theirs"}]

    async def measure(path: str, timeout: int = 60) -> int:
        return 42

    monkeypatch.setattr(repository, "_discover_worktrees", discover_then_switch)
    monkeypatch.setattr(fleet_state, "_measure_dir_mb", measure)

    async def scenario() -> dict:
        fleet_state._DISK.update({"status": "idle", "total_mb": None, "per": {}})
        fleet_state._DISK_COMPUTING = False
        fleet_state._DISK_COMPUTED_AT = 0.0
        await fleet_state._disk()
        for _ in range(100):
            if not fleet_state._DISK_COMPUTING:
                break
            await asyncio.sleep(0.01)
        return dict(fleet_state._DISK)

    snapshot = asyncio.run(scenario())

    assert snapshot["per"] == {}
    assert snapshot["total_mb"] is None
    assert snapshot["status"] == "idle"
    assert fleet_state._DISK_COMPUTED_AT == 0.0


def test_no_post_await_cache_write_survives_a_checkout_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every cache here is written AFTER an await, so clearing is only half the guard.

    The owner/repo name and the browser base URL have no expiry at all, and the PR
    verdict keyed under the wrong repository is what the unattended reaper reads as
    MERGED before deleting a worktree. Each write therefore asks whether the checkout
    it started against is still the resolved one, and declines when it is not.
    """
    switched: list[str] = []

    def switch() -> None:
        repository._drop_checkout_derived_state()
        switched.append("switched")

    # 1. owner/repo -- cached permanently on success, so a stale write never expires.
    async def owner_then_switch() -> str | None:
        switch()
        return "them/theirs"

    monkeypatch.setattr(fleet_state, "_repo_owner_name", owner_then_switch)
    fleet_state._OWNER_REPO = None
    fleet_state._OWNER_REPO_RETRY_AT = 0.0
    assert asyncio.run(fleet_state._get_owner_repo()) is None
    assert fleet_state._OWNER_REPO is None
    # The retry deadline is a write too: a failure against the old checkout must not
    # suppress the new one's first attempt.
    assert fleet_state._OWNER_REPO_RETRY_AT == 0.0

    # 2. the PR verdict -- `_prunable` reads MERGED as permission to delete.
    async def pr_then_switch(branch: str) -> dict | None:
        switch()
        return {"number": 7, "state": "MERGED", "_head_oid": "abc"}

    monkeypatch.setattr(fleet_state, "_fetch_pr_status", pr_then_switch)
    fleet_state._PR_CACHE.clear()
    monkeypatch.setattr(repository, "BASE_BRANCH", "main")
    assert asyncio.run(fleet_state._pr_status_cached("wt-branch")) is None
    assert fleet_state._PR_CACHE == {}

    # 3. the browser base URL -- no TTL, so A's issue links would stand until restart.
    # Both of its writes are covered: the remote's own URL, and the owner/repo fallback.
    async def owner_for_html() -> str | None:
        return "them/theirs"

    async def remote_then_switch() -> str:
        switch()
        return "origin"

    async def get_url(argv, **_kwargs):
        # Stubbed rather than left to real git: on a host where the accessor is
        # unresolved this call raises and the fallback below is the only write
        # exercised, which would leave the first one unmeasured.
        assert argv[-1] == "origin", argv
        return 0, "git@github.com:them/theirs.git\n", ""

    monkeypatch.setattr(fleet_state, "_repo_owner_name", owner_for_html)
    monkeypatch.setattr(repository, "_upstream_remote", remote_then_switch)
    monkeypatch.setattr(repository, "MAIN_REPO", "/somewhere/theirs")
    monkeypatch.setattr(repository, "_REPO_INVALID_MSG", None)
    monkeypatch.setattr(repository, "_REPO_READ_ONLY_MSG", None)
    monkeypatch.setattr(runtime, "_run_cmd", get_url)
    fleet_state._HTML_BASE = None
    fleet_state._OWNER_REPO = None
    fleet_state._OWNER_REPO_RETRY_AT = 0.0
    assert asyncio.run(fleet_state._html_repo_base()) is None
    assert fleet_state._HTML_BASE is None

    # 4. the per-branch context -- issues and tickets resolved against A's repository.
    # Mirrors the real signature, `generation` included: a stub that rejects a keyword
    # the caller passes raises a TypeError the broad `except` here turns into an EMPTY
    # context -- which the fence then happily caches, so the pin would pass for the
    # wrong reason while asserting nothing about the fence.
    async def context_then_switch(
        branch: str, path: str, pr: dict | None, *, generation: int | None = None
    ) -> dict:
        switch()
        return {"issues": [{"number": 1}], "tickets": [], "summary": "theirs"}

    monkeypatch.setattr(fleet_state, "_build_context", context_then_switch)
    fleet_state._CTX_CACHE.clear()
    got = asyncio.run(fleet_state._context_cached("wt-branch", "/wt", None))
    assert got == {"issues": [], "tickets": [], "summary": None}
    assert fleet_state._CTX_CACHE == {}

    assert len(switched) == 4


def test_this_module_s_own_resolvers_decline_to_publish_across_a_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The three resolvers here write AFTER their awaits, exactly like the caches above.

    Each value is the checkout's identity: the base branch names the ref ``/rebase``
    rewrites onto, the upstream remote is the other half of ``{remote}/{base}`` and has
    no expiry, and the fallback list decides which worktree-name prefixes count as
    legacy and whose merged verdict is trusted. A value resolved against the checkout
    being left behind must not be published against the one now configured.
    """
    switched: list[str] = []

    def switch() -> None:
        repository._drop_checkout_derived_state()
        switched.append("switched")

    monkeypatch.setattr(repository, "MAIN_REPO", "/somewhere/theirs")
    monkeypatch.setattr(repository, "_REPO_INVALID_MSG", None)
    monkeypatch.setattr(repository, "_REPO_READ_ONLY_MSG", None)

    # 1. the base branch. The switch restores the shared default for the resolvers to
    # re-read, so publishing the old checkout's name is what must not happen.
    async def _remotes(argv, **_kwargs):
        assert argv[-1] == "remote", argv
        return 0, "origin\n", ""

    async def _git_then_switch(_repo: str, *args: str, **_kw: object) -> str | None:
        switch()
        return "origin/their-trunk" if args[0] == "symbolic-ref" else None

    monkeypatch.setattr(runtime, "_run_cmd", _remotes)
    monkeypatch.setattr(repository, "_git", _git_then_switch)
    monkeypatch.setattr(repository, "BASE_BRANCH", "main")
    asyncio.run(repository._resolve_base_branch())
    assert repository.BASE_BRANCH == "main"

    # 2. the upstream remote. Cached permanently on success, and the degraded answer
    # is git's own default rather than the name this checkout carries.
    async def _remote_then_switch(argv, **_kwargs):
        if argv[-1] == "remote":
            switch()
            return 0, "theirs\n", ""
        return 0, "theirs\n", ""

    monkeypatch.setattr(runtime, "_run_cmd", _remote_then_switch)
    monkeypatch.setattr(repository, "_UPSTREAM_REMOTE", None)
    monkeypatch.setattr(repository, "BASE_BRANCH", "main")
    assert asyncio.run(repository._upstream_remote()) == "origin"
    assert repository._UPSTREAM_REMOTE is None

    # 3. the fallback repos. Left at the not-yet-loaded sentinel so the next call
    # enumerates the remotes of the checkout now configured.
    async def _upstream() -> str:
        return "origin"

    async def _enumerate_then_switch(argv, **_kwargs):
        if argv[-1] == "remote":
            switch()
            return 0, "origin\nlegacy\n", ""
        return 0, "git@github.com:them/theirs.git\n", ""

    monkeypatch.setattr(repository, "_upstream_remote", _upstream)
    monkeypatch.setattr(runtime, "_run_cmd", _enumerate_then_switch)
    monkeypatch.setattr(repository, "_FALLBACK_REPOS", None)
    asyncio.run(repository._load_fallback_repos())
    assert repository._FALLBACK_REPOS is None

    assert len(switched) == 3


def _porcelain_for(paths: list[str]) -> str:
    return "\n".join(f"worktree {p}\nHEAD abc123\nbranch refs/heads/b\n" for p in paths)


def test_a_worktree_root_inside_a_protected_location_is_withheld(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A linked worktree's location comes from the repository's own admin files.

    The adoption fence judges the path the operator named and the primary it resolves
    to; a ``gitdir`` record naming a credential directory is neither, and ``/api/disk``
    walks every root it is handed recursively. One bad record costs its own row only.
    """
    monkeypatch.setattr(repository, "MAIN_REPO", "/somewhere/theirs")
    monkeypatch.setattr(repository, "_REPO_INVALID_MSG", None)
    monkeypatch.setattr(repository, "_REPO_READ_ONLY_MSG", None)
    asked: list[str] = []

    def _fence(configured: str, resolved: str) -> str | None:
        asked.append(configured)
        return "it is a protected location" if "/home/someone/.ssh" in configured else None

    monkeypatch.setattr(repository, "_fenced_reason", _fence)

    async def _list(argv, **_kwargs):
        assert "worktree" in argv, argv
        return 0, _porcelain_for(["/somewhere/theirs", "/home/someone/.ssh/keys"]), ""

    monkeypatch.setattr(runtime, "_run_cmd", _list)
    got = asyncio.run(repository._worktree_porcelain_entries())
    assert [e["path"] for e in got] == ["/somewhere/theirs"]
    # Asked about EVERY root, the primary included -- not only the linked ones.
    assert asked == ["/somewhere/theirs", "/home/someone/.ssh/keys"]


def test_a_fenced_primary_refuses_the_whole_worktree_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dropping the primary would promote a linked worktree to main.

    ``is_main`` anchors the fleet, so a fenced primary is refused outright rather than
    filtered: filtering it serves the operator a fleet rooted somewhere nobody named.
    """
    monkeypatch.setattr(repository, "MAIN_REPO", "/home/someone")
    monkeypatch.setattr(repository, "_REPO_INVALID_MSG", None)
    monkeypatch.setattr(repository, "_REPO_READ_ONLY_MSG", None)
    monkeypatch.setattr(
        repository,
        "_fenced_reason",
        lambda configured, resolved: "it contains a protected location",
    )

    async def _list(argv, **_kwargs):
        return 0, _porcelain_for(["/home/someone", "/home/someone/wt-1"]), ""

    monkeypatch.setattr(runtime, "_run_cmd", _list)
    with pytest.raises(repository.RepoUnreadable) as caught:
        asyncio.run(repository._worktree_porcelain_entries())
    assert "protected location" in str(caught.value)


def test_the_poll_path_reopens_a_read_only_latch() -> None:
    """The reopen-on-changed-config path runs through the fleet poll, not only /fleet.

    ``_ensure_repo_resolved`` returns before ``ensure_main_repo_discovered`` for a
    resolved checkout, so a short-circuit that reads only the invalid verdict keeps
    serving a stranger's repository — base branch and upstream remote resolved
    against it — until the gateway restarts.
    """
    source = inspect.getsource(worktree_ops._ensure_repo_resolved)
    assert "not repository._REPO_INVALID_MSG" in source
    assert "not repository._REPO_READ_ONLY_MSG" in source


# --- a read-only checkout reports its live state as unknown -------------------


def test_read_only_checkout_reports_live_state_unknown() -> None:
    """`null` badges are falsy in the page, so the refusal needs the unknown flag.

    Make live is refused on a read-only checkout, and the page already disables the
    control and states the reason when the live state is unknown. Without this the
    row renders as "nothing is live" and still offers the button that answers 409.
    """
    body = inspect.getsource(fleet_state._build_fleet)
    assert "if repository._read_only_reason():" in body
    gate = body.index("if repository._read_only_reason():")
    assert body.index("live_state_known = False", gate) > gate


# --- the read-only denial reaches the audit trail ----------------------------


def test_read_only_denial_is_audited(monkeypatch: pytest.MonkeyPatch) -> None:
    """A refusal on an AUTHENTICATED request is a permission decision.

    Recorded on BOTH surfaces that take it: the backend middleware, whose HMAC
    denials beside it are already audited, and the gateway's own resolution step,
    which returns before the route reaches its ``_audit`` call.
    """
    for source in (
        inspect.getsource(http_api.hmac_proxy_middleware),
        inspect.getsource(gateway_routes._ensure_repo),
    ):
        assert "log_tool_invocation" in source
        assert 'outcome="denied"' in source
        assert 'tool_name="dev-fleet:repo-read-only"' in source
        # The 409 must survive a failing audit sink: auditing may not mask the answer.
        assert "except Exception" in source


def test_the_two_discovery_refusals_this_module_takes_are_audited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A withheld worktree root and a refused converting verb are permission decisions.

    This app audits its sibling denials at its HTTP boundaries, so each of these emits a
    denied SEL event too. EVERY occurrence is audited, not the first per verb: a trail
    that records one cannot answer how often or how recently a repository was refused,
    which is most of what it is read for. Deduplication applies to the operator-facing
    log line alone, because that one is advice.
    """
    events: list[dict] = []

    class _Sel:
        def log_tool_invocation(self, **kwargs):
            events.append(kwargs)

    monkeypatch.setattr(runtime, "_sel", lambda: _Sel())
    monkeypatch.setattr(repository, "MAIN_REPO", "/somewhere/theirs")
    monkeypatch.setattr(repository, "_REPO_INVALID_MSG", None)
    monkeypatch.setattr(repository, "_REPO_READ_ONLY_MSG", None)
    monkeypatch.setattr(
        repository,
        "_fenced_reason",
        lambda configured, _resolved: (
            "it is a protected location" if "/home/someone/.ssh" in configured else None
        ),
    )

    async def _list(_argv, **_kwargs):
        return 0, _porcelain_for(["/somewhere/theirs", "/home/someone/.ssh/keys"]), ""

    monkeypatch.setattr(runtime, "_run_cmd", _list)
    kept = asyncio.run(repository._worktree_porcelain_entries())

    assert [e["path"] for e in kept] == ["/somewhere/theirs"]
    assert [e["tool_name"] for e in events] == ["dev-fleet:repo-worktree-root-fenced"]
    assert events[0]["outcome"] == "denied"
    assert "/home/someone/.ssh/keys" in events[0]["resources"]

    events.clear()
    repository._REFUSED_CONVERTING_VERBS_LOGGED.clear()
    monkeypatch.setattr(repository, "_REPO_READ_ONLY_MSG", "read-only: /foreign ...")
    monkeypatch.setattr(repository, "_configured_filter_commands", lambda _p: ([], None))

    async def _never(_argv, **_kwargs):
        raise AssertionError("a content-converting verb must not reach a spawn")

    monkeypatch.setattr(runtime, "_run_cmd", _never)
    for _ in range(3):
        assert asyncio.run(repository._git("/foreign/checkout", "status", "--porcelain")) is None

    # Three refusals, three events -- the count is the point.
    assert [e["tool_name"] for e in events] == ["dev-fleet:repo-content-conversion-refused"] * 3
    assert all(e["outcome"] == "denied" for e in events)

    # A filter driver that appears AFTER adoption is a refusal on an admitted repository,
    # so it is audited as well rather than only raised at the caller.
    events.clear()
    monkeypatch.setattr(
        repository, "_configured_filter_commands", lambda _p: (["clean=evil"], None)
    )
    for _ in range(2):
        with pytest.raises(repository.RepoUnreadable):
            asyncio.run(repository._assert_read_cleared("/foreign/checkout", read_only=True))
    assert [e["tool_name"] for e in events] == ["dev-fleet:repo-filter-driver-refused"] * 2

    # And doubt: a filter config that could not be read refuses, and that is recorded.
    events.clear()
    monkeypatch.setattr(
        repository, "_configured_filter_commands", lambda _p: ([], "config unreadable")
    )
    with pytest.raises(repository.RepoUnreadable):
        asyncio.run(repository._assert_read_cleared("/foreign/checkout", read_only=True))
    assert [e["tool_name"] for e in events] == ["dev-fleet:repo-filter-scan-unverified"]


def test_an_oversized_probe_reply_is_dropped_instead_of_retained(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A foreign repo answering a small question with an unbounded one is refused.

    ``capture_output`` reads a pipe to EOF into this process, and the ``tool`` rlimit
    profile bounds the CHILD rather than what the parent accumulates from it -- so the
    bound is checked on the file the spawn writes into, BEFORE the bytes are decoded
    or split into keys. A reply inside the bound still reads normally, and the refusal
    routes to the unread answer so the checkout is declined, never served driver-free.
    """
    monkeypatch.setattr(runtime, "_trusted_bin", lambda _name: "git")
    _unwrapped_sandbox(monkeypatch)

    def _flood(_argv, **kwargs):
        kwargs["stdout"].write(b"filter.evil.clean\n")
        kwargs["stdout"].write(b"x" * (repository._PROBE_OUTPUT_MAX_BYTES + 1))
        return SimpleNamespace(returncode=0, stdout=None)

    monkeypatch.setattr(repository.subprocess, "run", _flood)
    drivers, unread = repository._snapshot_filter_keys(
        repository._ConfigRead("config", "[core]\n", None), "git", {}
    )

    assert drivers == []
    assert unread and "config" in unread

    def _small(_argv, **kwargs):
        kwargs["stdout"].write(b"filter.evil.clean\n")
        return SimpleNamespace(returncode=0, stdout=None)

    monkeypatch.setattr(repository.subprocess, "run", _small)
    assert repository._snapshot_filter_keys(
        repository._ConfigRead("config", "[core]\n", None), "git", {}
    ) == (
        ["filter.evil.clean"],
        None,
    )


def test_a_repo_that_includes_another_file_is_declined_before_the_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """git follows an include while parsing config on EVERY command, so no git runs.

    The refusal cannot be a git question: asking would already have opened whatever
    the include names. So the repository's own config files are read by hand, before
    any spawn, and the first probe never happens. Detected by SECTION, because an
    include may name a file that includes a third and following the chain would
    reimplement the resolution this avoids triggering.
    """
    repo = tmp_path / "theirs"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "config").write_text(
        "[core]\n\trepositoryformatversion = 0\n[include]\n\tpath = /home/someone/.aws/credentials\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(runtime, "_trusted_bin", lambda _name: "git")
    _unwrapped_sandbox(monkeypatch)

    def _never(*_args, **_kwargs):
        raise AssertionError("no git may run in a checkout whose config names an include")

    monkeypatch.setattr(repository.subprocess, "run", _never)

    # Every probe refuses at the chokepoint, so the cascade reports unread and the
    # startup resolution falls back to the path as named rather than reading it.
    drivers, unread = repository._configured_filter_commands(str(repo))
    assert drivers == []
    assert unread and "includes another file" in unread
    assert repository._resolve_primary_checkout(str(repo)) == str(repo)

    # A repository naming none is read as before, and the reason is reported from the
    # file rather than from a git answer.
    plain = tmp_path / "ours"
    (plain / ".git").mkdir(parents=True)
    (plain / ".git" / "config").write_text("[core]\n\tbare = false\n", encoding="utf-8")
    assert repository._include_refusal(str(plain)) is None
    assert repository._include_refusal(str(repo)) is not None
    # A path that is not a checkout has no config of its own to follow.
    assert repository._include_refusal(str(tmp_path / "nothing-here")) is None

    # A config too large to hold is refused unread, and by SIZE rather than by a
    # patched reader, so the bound itself is what the pin exercises. The read takes one
    # byte over the cap and never allocates the rest.
    huge = tmp_path / "huge"
    (huge / ".git").mkdir(parents=True)
    (huge / ".git" / "config").write_text(
        "[core]\n\tbare = false\n" + "#" + "x" * repository._METADATA_MAX_BYTES + "\n",
        encoding="utf-8",
    )
    assert repository._include_refusal(str(huge)) is not None
    # Bounded AT the read, not after it: a length check on an already-slurped file
    # would refuse the same config having first held all of it in this process.
    assert "read(_METADATA_MAX_BYTES + 1)" in inspect.getsource(repository._read_bounded_bytes)


def test_a_linked_worktree_is_judged_by_the_config_its_pointer_names(tmp_path) -> None:
    """A linked worktree keeps its config elsewhere, and that file is the one that counts.

    ``.git`` is a FILE naming the real gitdir, and the shared config sits in the common
    dir that gitdir names in ``commondir``. Both are one line of text, so the layout is
    followed without a config parse -- and an include in the SHARED config is reached
    by a read of the linked worktree just the same.
    """
    gitdir = tmp_path / "primary" / ".git" / "worktrees" / "wt"
    gitdir.mkdir(parents=True)
    (gitdir / "commondir").write_text("../..\n", encoding="utf-8")
    (tmp_path / "primary" / ".git" / "config").write_text(
        '[includeIf "gitdir:/"]\n\tpath = /home/someone/.ssh/config\n', encoding="utf-8"
    )
    linked = tmp_path / "linked"
    linked.mkdir()
    (linked / ".git").write_text(f"gitdir: {gitdir}\n", encoding="utf-8")

    assert repository._include_refusal(str(linked)) is not None


def test_git_metadata_cannot_redirect_the_config_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """``.git`` and ``commondir`` are written by the repository, so they are input.

    The gate reads the checkout's own config, and the repository chooses where that is:
    a ``gitdir:`` naming a credential directory aims the read there. The adoption fence
    does not cover it -- that judges the checkout path and the primary it resolves to,
    not a third path the metadata points at -- so every named target goes through the
    same fence.

    The fenced target is built from ``tmp_path`` and compared with separators
    normalised, because ``Path`` renders backslashes on Windows and a POSIX-spelled
    predicate silently matches nothing there.
    """
    protected = tmp_path / "protected-store"

    def _fence(_configured: str, resolved: str) -> str | None:
        if "protected-store" in resolved.replace("\\", "/"):
            return "it is a protected location"
        return None

    monkeypatch.setattr(repository, "_fenced_reason", _fence)

    # 1. the pointer file names a fenced directory
    aimed = tmp_path / "aimed"
    aimed.mkdir()
    (aimed / ".git").write_text(f"gitdir: {protected}\n", encoding="utf-8")
    reason = repository._include_refusal(str(aimed))
    assert reason and "protected location" in reason

    # 2. commondir extends the redirect, and is judged too
    linked = tmp_path / "linked"
    gitdir = tmp_path / "gd"
    gitdir.mkdir()
    (gitdir / "commondir").write_text(f"{protected}\n", encoding="utf-8")
    linked.mkdir()
    (linked / ".git").write_text(f"gitdir: {gitdir}\n", encoding="utf-8")
    reason = repository._include_refusal(str(linked))
    assert reason and "protected location" in reason


@requires_symlinks
def test_symlinked_git_metadata_is_refused_rather_than_resolved(tmp_path) -> None:
    """A symlink names nothing, so the fence cannot judge it -- it is refused instead.

    Following one means trusting its target, which is the decision being withheld.
    ``commondir`` is judged ITSELF and not only the path it names, because ``is_file``
    follows a symlinked ``commondir`` and would open its target before anything judged
    it. Behind the repo's symlink PROBE, since a Windows runner without the privilege
    cannot create one.
    """
    # a symlinked .git, refused without being resolved
    elsewhere = tmp_path / "real-repo"
    (elsewhere / ".git").mkdir(parents=True)
    (elsewhere / ".git" / "config").write_text("[core]\n\tbare = false\n", encoding="utf-8")
    sym = tmp_path / "sym"
    sym.mkdir()
    (sym / ".git").symlink_to(elsewhere / ".git")
    reason = repository._include_refusal(str(sym))
    assert reason and "symlink" in reason

    # a symlinked commondir, refused before its target is opened
    slinked = tmp_path / "slinked"
    sgitdir = tmp_path / "sgd"
    sgitdir.mkdir()
    secret = tmp_path / "pypirc"
    secret.write_text("[pypi]\n\tpassword = hunter2\n", encoding="utf-8")
    (sgitdir / "commondir").symlink_to(secret)
    slinked.mkdir()
    (slinked / ".git").write_text(f"gitdir: {sgitdir}\n", encoding="utf-8")
    reason = repository._include_refusal(str(slinked))
    assert reason and "symlink" in reason


@pytest.mark.asyncio
async def test_the_worktree_enumeration_uses_the_gated_spawn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fleet's own enumeration is a foreign read, so it takes the same clearance.

    It spawned at the default ``standard`` tier with no clearance, which is the tier
    that hands over this app's trusted credential helpers -- while every per-row read
    was forcing ``strict`` and re-clearing precisely because discovery's one-time
    answer stops being true. An include written into the config AFTER adoption was
    followed by this very command.
    """
    seen: list[dict] = []

    async def _run_cmd(argv, *, timeout=6, mode="standard", pre_spawn=None, **_kw):
        if pre_spawn is not None:
            refusal = await pre_spawn()
            if refusal is not None:
                return -1, "", refusal
        seen.append({"argv": list(argv), "mode": mode, "cleared": pre_spawn is not None})
        return 0, _porcelain_for(["/somewhere/theirs"]), ""

    monkeypatch.setattr(repository, "MAIN_REPO", "/somewhere/theirs")
    monkeypatch.setattr(repository, "_REPO_INVALID_MSG", None)
    monkeypatch.setattr(repository, "_REPO_READ_ONLY_MSG", "read-only: /somewhere/theirs ...")
    monkeypatch.setattr(repository, "_fenced_reason", lambda _c, _r: None)
    monkeypatch.setattr(repository, "_configured_filter_commands", lambda _p: ([], None))
    monkeypatch.setattr(runtime, "_run_cmd", _run_cmd)

    got = await repository._worktree_porcelain_entries()

    assert [e["path"] for e in got] == ["/somewhere/theirs"]
    assert seen and "worktree" in seen[0]["argv"]
    # The tier a foreign read requires, and the clearance bound to the spawn.
    assert seen[0]["mode"] == "strict"
    assert seen[0]["cleared"] is True


def test_the_settling_window_opens_before_every_await_that_follows_publication() -> None:
    """A rebase can land on any await taken after the path is published.

    The credential-helper warm is one such await -- reached only when a first attempt
    left the helpers unloaded, which a recovered startup config read does -- and in
    that window the checkout is visible while the base branch still holds what the
    switch reset it to. The flag is therefore set before it, and the ``try`` covers it
    so a warm that raises cannot leave every mutation refused for the process's life.
    """
    body = inspect.getsource(repository.ensure_main_repo_discovered)
    flag = body.index("_REPO_DERIVING = True")
    warm = body.index("_load_trusted_credential_helpers()")
    guard = body.index("try:", flag)

    assert flag < guard < warm, "the warm must sit inside the window and inside the try"
    assert "_REPO_DERIVING = False" in body[warm:], "cleared in the finally after the warm"


@requires_symlinks
@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="O_NOFOLLOW is POSIX-only")
def test_the_metadata_read_itself_refuses_a_symlink(tmp_path) -> None:
    """The symlink test and the read are a check-then-use pair, so the OPEN enforces it too.

    The repository owns the directory between the two, so it can replace a config it
    has just had approved with a link to a credential store. ``O_NOFOLLOW`` makes that
    swap fail at the syscall instead of succeeding quietly, which is the same reason
    the spawn clearance sits in ``pre_spawn`` rather than ahead of the call.
    """
    secret = tmp_path / "credentials"
    secret.write_text("[default]\n\taws_access_key_id = AKIAEXAMPLE\n", encoding="utf-8")
    link = tmp_path / "config"
    link.symlink_to(secret)

    # Read directly, bypassing the caller's own is_symlink test, so what is pinned is
    # the read's refusal rather than the check that normally precedes it.
    assert repository._read_bounded(link) is None
    # The same reader still answers for a real file.
    assert "aws_access_key_id" in (repository._read_bounded(secret) or "")


def test_no_read_of_a_foreign_checkout_spawns_git_outside_the_gate() -> None:
    """A convention is not a bound: the module must leave no bare checkout-scoped spawn.

    Twice now a single call site has been found spawning git in a foreign checkout at
    the default tier with no clearance -- the worktree enumeration, then the base-branch
    resolver's `git remote` -- while every other read forced strict and re-cleared. The
    invariant is therefore asserted on the source rather than trusted: exactly two
    `runtime._run_cmd` sites remain, the gated spawn itself and the credential-helper
    load, and the second is repo-INDEPENDENT, which is what makes it exempt.
    """
    src = inspect.getsource(repository)
    assert src.count("runtime._run_cmd(") == 2, "a new bare spawn appeared -- gate it"

    # The exempt one names no checkout: --system and --global scope only.
    for block in src.split("runtime._run_cmd(")[1:]:
        head = block[:240]
        if '"-C"' in head:
            assert "pre_spawn=_clear" in head, "a checkout-scoped spawn must be the gated one"
        else:
            assert "credential" in head, "only the repo-independent helper load is exempt"


def test_no_module_in_this_app_reads_a_foreign_checkout_ungated() -> None:
    """The chokepoint covers the APP, not one module of it.

    The first version of this pin read ``repository.py`` alone, and ``fleet_state.py``
    was meanwhile spawning `git -C <repo> remote get-url` and a `merge-base` bare -- at
    the default tier, which hands over the trusted credential helpers, and with no
    clearance to catch a config the repository changed after adoption.

    ``worktree_ops.py`` is exempt and its exemption is checked rather than assumed: its
    checkout-scoped spawns are MUTATIONS, and a mutation is bounded by a different
    mechanism -- ``_repo()`` raises ``RepoReadOnly`` for a foreign checkout and the route
    boundary gates every non-GET on that accessor, so a rebase or a fetch cannot reach a
    repository this app may only read. The pin holds it to that story by allowing only
    mutation verbs there, so a plain READ added to it fails here instead of quietly
    bypassing the clearance.
    """
    package = pathlib.Path(inspect.getfile(repository)).parent
    mutation_only = {"worktree_ops.py"}
    mutation_verbs = ('"rebase"', '"fetch"', '"merge-base"', '"worktree"', '"update-ref"')
    offenders: list[str] = []
    for module in sorted(package.rglob("*.py")):
        source = module.read_text(encoding="utf-8")
        for block in source.split("runtime._run_cmd(")[1:]:
            head = block[:260]
            if '"-C"' not in head:
                continue  # not scoped to a checkout at all
            if "pre_spawn=_clear" in head:
                continue  # the gated spawn itself
            if module.name in mutation_only and any(v in head for v in mutation_verbs):
                continue  # a mutation, refused for a foreign checkout at the boundary
            offenders.append(f"{module.name}: {' '.join(head.split())[:70]}")

    assert not offenders, f"ungated checkout-scoped git read(s): {offenders}"


def test_a_byte_order_mark_does_not_hide_an_include(tmp_path) -> None:
    """git strips a leading BOM, so a scanner that does not is reading a different file.

    A config opening ``\ufeff[include]`` is an include as far as git is concerned, and it
    would be followed on the first command. The mark is removed at the one decode the
    config, pointer and commondir reads share, so it cannot hide a section from this scan
    or a ``gitdir:`` from the pointer parse.
    """
    repo = tmp_path / "bom"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "config").write_bytes(
        "\ufeff[include]\n\tpath = /home/someone/.aws/credentials\n".encode("utf-8")
    )
    reason = repository._include_refusal(str(repo))
    assert reason and "includes another file" in reason

    # The same mark in front of a pointer file must not hide the gitdir it names.
    linked = tmp_path / "bom-linked"
    linked.mkdir()
    gitdir = tmp_path / "bom-gd"
    gitdir.mkdir()
    (gitdir / "config").write_text("[include]\n\tpath = /elsewhere\n", encoding="utf-8")
    (linked / ".git").write_bytes(f"\ufeffgitdir: {gitdir}\n".encode("utf-8"))
    reason = repository._include_refusal(str(linked))
    assert reason and "includes another file" in reason


@pytest.mark.asyncio
async def test_a_path_captured_before_a_switch_is_refused_not_read_as_the_new_checkout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The generation a path was captured under is the caller's, not the reader's.

    Sampling it inside the read was a hole with a precise shape: the fleet build
    captures a foreign path, a config switch lands BEFORE the read is entered, and the
    switch also CLEARS the read-only verdict -- so the read samples the NEW generation,
    compares it against itself, passes, finds `read_only` False, and runs git inside the
    foreign checkout at the `standard` tier, which is where the trusted credential
    helpers are handed over. The `pre_spawn` gate never saw it, because that gate only
    catches a switch landing between the sample and the spawn.
    """
    spawned: list[dict] = []

    async def _run_cmd(argv, *, timeout=6, mode="standard", pre_spawn=None, **_kw):
        if pre_spawn is not None:
            refusal = await pre_spawn()
            if refusal is not None:
                return -1, "", refusal
        spawned.append({"argv": list(argv), "mode": mode})
        return 0, "answered\n", ""

    monkeypatch.setattr(runtime, "_run_cmd", _run_cmd)
    monkeypatch.setattr(repository, "_configured_filter_commands", lambda _p: ([], None))

    # The path was enumerated at generation 7; the app has since resolved elsewhere and
    # the new checkout is managed, so the read-only verdict is gone.
    monkeypatch.setattr(repository, "_REPO_READ_ONLY_MSG", None)
    # The GLOBAL, not the accessor: `_still_same_checkout` compares against it directly,
    # so patching only the accessor would make the two disagree and refuse everything.
    monkeypatch.setattr(repository, "_CHECKOUT_GEN", 9)

    with pytest.raises(repository.RepoUnreadable) as refused:
        await repository._run_gated_git("/foreign/checkout", "status", "--porcelain", generation=7)

    assert "changed before reading" in str(refused.value)
    assert spawned == [], "nothing may spawn in a checkout the app has left"

    # A caller that resolved the path at the CURRENT generation still reads.
    assert (
        await repository._git("/managed/checkout", "rev-parse", "HEAD", generation=9) == "answered"
    )
    assert [s["argv"][3] for s in spawned] == ["rev-parse"]


def test_every_fleet_read_of_a_captured_path_carries_its_generation() -> None:
    """The binding is only worth anything if the callers actually pass it.

    Both fleet reads capture the generation where the PATH is produced -- the snapshot
    build beside its one-per-snapshot `read_only`, and the lazy per-worktree detail with
    the lookup that resolves the name -- so a switch between that capture and the read is
    refused rather than silently reinterpreted.
    """
    src = inspect.getsource(fleet_state)
    assert src.count("_git_info(path, generation=generation)") == 2
    assert src.count("generation = repository._checkout_generation()") == 2

    # Captured BEFORE the enumeration, or a switch landing mid-enumeration is stamped
    # with the NEW generation and the stale paths it returned are called current.
    build = inspect.getsource(fleet_state._build_fleet)
    assert build.index("generation = repository._checkout_generation()") < build.index(
        "await repository._discover_worktrees()"
    )

    # ENUMERATED over the SYNTAX of BOTH modules, not by text and not per module. A
    # text scan of the caller cannot see a read that lives inside the callee, and that
    # blind spot is how `_dirty_split`'s own `status` read keeps its default while the
    # signature around it is threaded. Two shapes fail here:
    #
    #   * a function that takes a generation and REASSIGNS it -- a fresh reading binds
    #     the path to the checkout in force after discovery handed it over, which is the
    #     window the capture closes. Allowed only under `if generation is None`, the
    #     fallback for a caller that has none.
    #   * a read called from such a function WITHOUT forwarding it.
    reads = {
        "_git",
        "_git_info",
        "_git_ahead",
        "_own_commits_count",
        "_real_dirty",
        "_dirty_split",
        "_run_gated_git_soft",
        "_run_gated_git",
    }

    def _callee(node: ast.Call) -> str | None:
        if isinstance(node.func, ast.Attribute):
            return node.func.attr
        return node.func.id if isinstance(node.func, ast.Name) else None

    faults: list[str] = []
    for module in (fleet_state, repository):
        tree = ast.parse(inspect.getsource(module))
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if "generation" not in [a.arg for a in fn.args.args + fn.args.kwonlyargs]:
                continue
            guarded: set[int] = set()
            for node in ast.walk(fn):
                if (
                    isinstance(node, ast.If)
                    and isinstance(node.test, ast.Compare)
                    and isinstance(node.test.left, ast.Name)
                    and node.test.left.id == "generation"
                ):
                    guarded.update(id(inner) for inner in ast.walk(node))
            for node in ast.walk(fn):
                if (
                    isinstance(node, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == "generation" for t in node.targets)
                    and id(node) not in guarded
                ):
                    faults.append(f"{module.__name__}:{fn.name} reassigns the generation")
                if isinstance(node, ast.Call) and _callee(node) in reads:
                    if not any(kw.arg == "generation" for kw in node.keywords):
                        faults.append(
                            f"{module.__name__}:{fn.name} calls {_callee(node)}()"
                            " without forwarding the generation"
                        )
    assert not faults, "; ".join(faults)


def test_a_retained_pr_body_is_bounded_at_capture_and_at_retention() -> None:
    """A PR body is written by whoever opened it and is cached per branch for a TTL.

    Same shape as the commit-context read, different field: the capture is bounded so
    the gateway never holds an answer whose size someone else chose, and the retained
    copy is truncated because the cache keeps it long after the read.
    """
    src = inspect.getsource(fleet_state)
    assert "max_output_bytes=_PR_CAPTURE_MAX_BYTES" in src
    assert 'pr["_body"] = (pr.pop("body") or "")[:_PR_BODY_MAX_CHARS]' in src

    # COUNTED, because review found the bound applied to one log read and not the other:
    # the context read, the detail's commit read and its diff read are all three
    # repository-controlled, and both retained-subject sites are truncated.
    assert src.count("max_output_bytes=_LOG_CAPTURE_MAX_BYTES") == 3
    assert src.count("[:_SUBJECT_MAX_CHARS]") == 2


@pytest.mark.asyncio
async def test_a_bounded_capture_stops_at_the_cap_and_does_not_hang(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """A foreign read's capture is bounded, and overflowing it must not wedge the read.

    The hazard is in the fix, not the finding: `communicate` drains both pipes at once
    precisely because a child writing hard to one blocks forever when the other is not
    read, so a bounded reader that simply stops would reintroduce that as a hang. The
    reader that overflows therefore KILLS the child, which brings the sibling pipe to
    EOF at once. Driven through a real subprocess, because the thing under test is the
    pipe behaviour rather than a fake's.
    """
    # CI has no sandbox backend, so the chokepoint fail-closes there and the read never
    # reaches a pipe at all -- the pipe behaviour is what this pins, so the wrapper is
    # neutralised exactly as `_unwrapped_sandbox` does for the probe chokepoint.
    monkeypatch.setattr(
        runtime,
        "sandboxed_spawn_argv",
        lambda argv, mode="standard", **kw: (list(argv), dict(kw.get("env") or {}), None),
    )

    cap = 64 * 1024
    # Writes far past the cap on stdout while stderr stays open -- the exact shape that
    # deadlocks a sequential bounded read.
    script = (
        "import sys\n"
        "buf = b'x' * 65536\n"
        "for _ in range(200):\n"
        "    sys.stdout.buffer.write(buf)\n"
        "    sys.stdout.buffer.flush()\n"
    )
    spam = tmp_path / "spam.py"
    spam.write_text(script, encoding="utf-8")

    rc, out, err = await asyncio.wait_for(
        runtime._run_cmd(
            [sys.executable, str(spam)],
            timeout=30,
            max_output_bytes=cap,
        ),
        # Well under _run_cmd's own timeout: if the overflow path hangs, this fails as a
        # timeout here rather than passing after a 30s stall.
        timeout=20,
    )

    assert rc == -1
    assert "bound" in err
    assert out == ""

    # The same helper with no bound still reads a small answer whole.
    quiet = tmp_path / "quiet.py"
    quiet.write_text("print('small')\n", encoding="utf-8")
    rc2, out2, _ = await runtime._run_cmd([sys.executable, str(quiet)], timeout=30)
    assert rc2 == 0 and out2.strip() == "small"


def test_the_foreign_commit_read_is_bounded_at_the_capture_and_per_field() -> None:
    """Both halves: the capture the gateway holds, and each field it retains.

    The outer bound stops an answer whose size the repository chose from being held at
    all; the inner two stop one oversized subject or body from riding through inside a
    capture that fits.
    """
    src = inspect.getsource(fleet_state)
    assert "max_output_bytes=_LOG_CAPTURE_MAX_BYTES" in src
    assert "subjects.append(subj[:_SUBJECT_MAX_CHARS])" in src
    assert "bodies.append(body[:_BODY_MAX_CHARS])" in src


def test_a_common_dir_that_does_not_own_this_worktree_is_refused(tmp_path) -> None:
    """``commondir`` is repository-controlled, so its claim is checked, not trusted.

    The common dir is reached by walking ``..`` through components inside the
    repository's own ``.git``, and lexical normalisation answers differently from the
    kernel when one of them is a symlink. A common dir that does not actually own this
    gitdir -- no ``worktrees/<name>`` entry that IS this directory -- is therefore
    refused rather than used.
    """
    gitdir = tmp_path / "real" / ".git" / "worktrees" / "wt"
    gitdir.mkdir(parents=True)
    # Names a directory that exists but owns nothing.
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    (gitdir / "commondir").write_text(f"{decoy}\n", encoding="utf-8")
    linked = tmp_path / "linked"
    linked.mkdir()
    (linked / ".git").write_text(f"gitdir: {gitdir}\n", encoding="utf-8")

    _g, _c, reason = repository._repo_metadata_dirs(str(linked))
    assert reason and "does not own" in reason
    # And the resolution degrades to the path as named, for the fence to judge.
    assert repository._resolve_primary_checkout(str(linked)) == str(linked)

    # The sharper case: a decoy that DOES carry `worktrees/wt`, but a different
    # directory of that name. Only comparing identity refuses this -- comparing the
    # name, or merely checking the entry exists, accepts a common dir the worktree
    # never belonged to.
    (decoy / "worktrees" / "wt").mkdir(parents=True)
    _g2, _c2, reason2 = repository._repo_metadata_dirs(str(linked))
    assert reason2 and "does not own" in reason2


@pytest.mark.skipif(os.name != "posix", reason="a non-UTF-8 path byte needs POSIX")
def test_a_non_utf8_checkout_path_survives_the_metadata_read(tmp_path) -> None:
    """The decode is ``os.fsdecode``, and a valid-UTF-8 fixture cannot prove it.

    An earlier version of this pin used an accented name, which every codec round-trips
    -- so it passed just as well with ``errors="replace"``, the decoder that DESTROYS
    the byte this one carries. The answer reaches an ``lstat``, so a replaced byte is a
    path that does not exist.
    """
    # A lone 0xFF byte: valid on a POSIX filesystem, not valid UTF-8.
    primary_b = os.fsencode(str(tmp_path)) + b"/prim\xffry"
    os.mkdir(primary_b)
    primary = os.fsdecode(primary_b)
    gitdir = pathlib.Path(primary) / ".git" / "worktrees" / "wt"
    gitdir.mkdir(parents=True)
    (gitdir / "commondir").write_text("../..\n", encoding="utf-8")
    linked = tmp_path / "linked"
    linked.mkdir()
    # Written as BYTES, exactly as git writes it.
    with open(linked / ".git", "wb") as handle:
        handle.write(b"gitdir: " + os.fsencode(str(gitdir)) + b"\n")

    resolved = repository._resolve_primary_checkout(str(linked))

    assert resolved == primary
    # The round trip is what the lstat needs: the surrogate must re-encode to the byte.
    assert os.fsencode(resolved) == primary_b
    assert os.path.isdir(resolved)


def test_an_oversized_worktree_listing_is_refused_not_served_short() -> None:
    """Record count and field width are repository-controlled, so both are bounded.

    The records come from the repository's own admin files, and every one is parsed,
    held in the fleet cache and rendered. Past the bound the fleet is REFUSED rather
    than served short: a missing row is a worktree nobody was told about, and one of
    them could be the main checkout every other row is anchored to.
    """
    over = "".join(
        f"worktree /tmp/wt{i}\nHEAD {'0' * 40}\nbranch refs/heads/b{i}\n\n"
        for i in range(repository._WORKTREE_RECORD_MAX + 5)
    )
    entries = repository._parse_worktree_porcelain(over)
    # EXACTLY one past the bound: that is the caller's overflow signal, and it is also
    # what proves the parse stopped. A `>` test alone passes just as well when nothing
    # stops it, since an unbounded parse returns even more.
    assert len(entries) == repository._WORKTREE_RECORD_MAX + 1

    # A single absurdly wide field REFUSES: a clipped lock reason would read as the
    # whole reason, and the operator would decide against text its author never wrote.
    wide = f"worktree /tmp/w\nHEAD {'0' * 40}\nlocked {'x' * 99_000}\n\n"
    with pytest.raises(repository.RepoUnreadable) as caught:
        repository._parse_worktree_porcelain(wide)
    assert "truncated value presented as complete" in str(caught.value)


@pytest.mark.asyncio
async def test_a_worktree_listing_over_the_capture_bound_names_its_own_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The capture bound is reported as itself, not as a corrupt repository.

    `_run_cmd` signals the bound through stderr on rc -1, and the generic git-error
    path below would render that as "worktree discovery failed", sending the operator
    to debug a checkout that is merely large.
    """

    async def _over(_repo, *_args, **_kwargs):
        bound = repository._WORKTREE_LIST_MAX_BYTES
        return -1, "", f"output passed the {bound}-byte bound"

    monkeypatch.setattr(repository, "_run_gated_git", _over)

    with pytest.raises(repository.RepoUnreadable) as caught:
        await repository._discover_worktrees()

    said = str(caught.value)
    assert "byte bound" in said
    assert "served incomplete" in said
    # NOT the generic path: that one blames discovery and invites a repo-corruption hunt.
    assert "worktree discovery failed" not in said


@pytest.mark.asyncio
async def test_a_listing_past_the_record_bound_refuses_the_whole_fleet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Over the record bound the fleet is refused, so nobody is served a short fleet."""

    listing = "".join(
        f"worktree /tmp/wt{i}\nHEAD {'0' * 40}\nbranch refs/heads/b{i}\n\n"
        for i in range(repository._WORKTREE_RECORD_MAX + 3)
    )

    async def _many(_repo, *_args, **_kwargs):
        return 0, listing, ""

    monkeypatch.setattr(repository, "_run_gated_git", _many)

    with pytest.raises(repository.RepoUnreadable) as caught:
        await repository._discover_worktrees()

    said = str(caught.value)
    assert str(repository._WORKTREE_RECORD_MAX) in said
    assert "served incomplete" in said


@requires_symlinks
@pytest.mark.skipif(
    not hasattr(os, "O_NOFOLLOW"),
    reason="the leak this proves, and the proof itself, are POSIX no-follow semantics",
)
def test_a_git_dir_swapped_for_a_symlink_after_the_check_cannot_be_read_through(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ancestor is PINNED, so a swap between the check and the read reaches nothing.

    `O_NOFOLLOW` on a file open guards the FINAL component only. Validating that `.git`
    is a real directory and then opening `.git/config` by path leaves the window this
    module exists to close: the repository renames `.git` to a symlink in between, the
    open walks through it, and the final component is a genuine file at the attacker's
    chosen target. Reading under a descriptor opened no-follow on the directory removes
    the second name resolution entirely.

    The fixture performs the swap for real rather than asserting on source text, because
    the leaf-only version of this code READS the planted secret -- verified directly
    below, so the test would pass against a mitigation that does not actually hold.
    """
    secret_dir = tmp_path / "credential-home"
    secret_dir.mkdir()
    (secret_dir / "config").write_text("aws_access_key_id = AKIAEXAMPLE\n", encoding="utf-8")

    checkout = tmp_path / "checkout"
    checkout.mkdir()
    real_git = checkout / ".git"
    real_git.mkdir()
    (real_git / "config").write_text("[core]\n\tbare = false\n", encoding="utf-8")

    # The adversary WINS THE RACE: the swap lands after the check answered "not a
    # symlink" and before the read. Patched rather than raced for real, because the
    # window is microseconds wide and a flaky pin proves nothing -- what is simulated is
    # only the TIMING, the swap itself is genuine.
    def _check_then_swap(target):
        verdict = original_judge(target)
        if target.name == ".git" and target.parent == checkout:
            real_git.rename(checkout / ".git-displaced")
            (checkout / ".git").symlink_to(secret_dir)
        return verdict

    original_judge = repository._metadata_redirect_reason
    monkeypatch.setattr(repository, "_metadata_redirect_reason", _check_then_swap)

    # A leaf-only O_NOFOLLOW open DOES read the secret through a swapped ancestor, so
    # the assertion below is about a defence that has to do real work. Proven after the
    # patch installs, since the swap happens inside it.
    reads, refusal = repository._repo_owned_config_reads(str(checkout))

    leaked = os.open(str(checkout / ".git" / "config"), os.O_RDONLY | os.O_NOFOLLOW)
    try:
        assert "AKIAEXAMPLE" in os.read(leaked, 200).decode()
    finally:
        os.close(leaked)

    # The module read nothing through it: the descriptor was opened on the directory
    # that passed the check, and a renamed name cannot redirect it.
    assert not [entry for entry in reads if entry.text and "AKIAEXAMPLE" in entry.text]
    assert refusal is None or "AKIAEXAMPLE" not in refusal


@requires_symlinks
def test_a_config_that_is_itself_a_symlink_is_refused_within_a_valid_git_dir(
    tmp_path,
) -> None:
    """The leaf is classified too, inside a `.git` that is entirely legitimate."""
    checkout = tmp_path / "checkout"
    gitdir = checkout / ".git"
    gitdir.mkdir(parents=True)
    secret = tmp_path / "netrc"
    secret.write_text("machine example.com password hunter2\n", encoding="utf-8")
    (gitdir / "config").symlink_to(secret)

    reason = repository._include_refusal(str(checkout))

    assert reason and "symlink" in reason
    assert "hunter2" not in reason


def test_metadata_reads_resolve_names_under_a_pinned_descriptor() -> None:
    """Every config read passes a NAME and a descriptor, never a rebuilt path.

    A path handed back to a caller is a path reopened by name, and the repository owns
    those names. The reads therefore travel as content in `_ConfigRead`, and the pinned
    open is the only way the bytes are reached.
    """
    src = inspect.getsource(repository._repo_metadata)
    assert "_open_pinned_dir(dot)" in src
    assert "dir_fd=gitdir_fd" in src
    # The directory open must be no-follow AND directory-only, or the pin is decorative.
    pinned = inspect.getsource(repository._open_pinned_dir)
    assert "O_NOFOLLOW" in pinned and "O_DIRECTORY" in pinned

    # No caller is handed a path to reopen: the accessor returns reads, not files.
    assert not hasattr(repository, "_repo_owned_config_files")
    scan = inspect.getsource(repository._include_refusal)
    assert "entry.text" in scan
    assert "_read_bounded(" not in scan


def test_metadata_refusals_hold_on_a_platform_without_pinned_descriptors(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The no-descriptor fallback is exercised HERE, because CI's Windows runner has it.

    Windows supports neither `dir_fd` nor `O_NOFOLLOW`, so every pinned read there takes
    the fallback branch. Left to the Windows shard alone, a break in that branch would be
    found by a red lane on another platform hours later; forcing the capability flag off
    finds it in the same run that wrote it.
    """
    monkeypatch.setattr(repository, "_PINNED_READS", False)
    # Windows cannot open a DIRECTORY with `os.open` at all, so the pinned open is not
    # merely useless there -- it fails. Modelled, because a guard that still calls it
    # would work on this platform and collapse every checkout to "not a git checkout"
    # on that one.
    monkeypatch.setattr(repository, "_open_pinned_dir", lambda *_a, **_k: None)

    # A plain checkout is still read and judged.
    plain = tmp_path / "plain"
    gitdir = plain / ".git"
    gitdir.mkdir(parents=True)
    (gitdir / "config").write_text("[core]\n\tbare = false\n", encoding="utf-8")
    assert repository._include_refusal(str(plain)) is None

    # An include is still found.
    (gitdir / "config").write_text("[include]\n\tpath = /etc/passwd\n", encoding="utf-8")
    reason = repository._include_refusal(str(plain))
    assert reason and "includes another file" in reason

    # A config that cannot be read within the bound still refuses.
    (gitdir / "config").write_text("x" * (repository._METADATA_MAX_BYTES + 10), encoding="utf-8")
    over = repository._include_refusal(str(plain))
    assert over and "could not be read within the bound" in over

    # And the layout still resolves, so discovery does not depend on the descriptor.
    dirs = repository._repo_metadata_dirs(str(plain))
    assert dirs[0] == gitdir and dirs[2] is None


def test_every_dev_fleet_stub_mirrors_the_generation_parameter() -> None:
    """A stub that rejects a forwarded keyword fails SILENTLY, so the shape is pinned.

    `_context_cached` and the read helpers forward the captured generation. A test that
    replaces one of them with a narrower stub raises TypeError on the call — and these
    call sites are wrapped in best-effort `except` clauses, because a context or a dirty
    reading must never break the fleet. The exception is therefore swallowed, the stub is
    never recorded as called, and the assertion that fails is about caching or counting,
    several steps from the cause. It cost two review rounds before this pin existed.

    Scoped to the dev-fleet suites: another module's `_git` is a different function with
    its own signature, and widening this would assert on code it does not describe.
    """
    targets = {
        "_build_context",
        "_context_cached",
        "_git",
        "_git_info",
        "_git_ahead",
        "_own_commits_count",
        "_real_dirty",
        "_dirty_split",
    }
    suite = pathlib.Path(__file__).parent
    offenders: list[str] = []
    for path in sorted(suite.glob("test_dev_fleet*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        defs = {
            node.name: node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "setattr"
                and len(node.args) >= 3
                and isinstance(node.args[1], ast.Constant)
                and node.args[1].value in targets
            ):
                continue
            replacement = node.args[2]
            if not isinstance(replacement, ast.Name):
                continue  # a lambda or call expression: its arity is visible inline
            stub = defs.get(replacement.id)
            if stub is None:
                continue
            named = [a.arg for a in stub.args.args + stub.args.kwonlyargs]
            if "generation" in named or stub.args.kwarg is not None:
                continue
            offenders.append(
                f"{path.name}:{stub.lineno} {replacement.id} replaces "
                f"{node.args[1].value} but takes no generation"
            )
    assert not offenders, "; ".join(offenders)


def test_a_commented_include_is_not_an_include(tmp_path) -> None:
    """git honours neither `#` nor `;` lines, so neither does the scan.

    The predicate strips comments before matching, and both directions matter: a
    commented `[include]` that refused would decline a repository over a line git never
    reads, while a real one on any line but the first must still refuse -- the section
    regex is start-anchored, so the scan walks lines rather than searching the text.
    """
    checkout = tmp_path / "checkout"
    gitdir = checkout / ".git"
    gitdir.mkdir(parents=True)

    (gitdir / "config").write_text(
        '[core]\n\tbare = false\n# [include]\n#\tpath = other.cfg\n; [includeIf "x"]\n',
        encoding="utf-8",
    )
    assert repository._include_refusal(str(checkout)) is None
    assert repository._names_an_include((gitdir / "config").read_text(encoding="utf-8")) is False

    # The same file with the directive live, and NOT on the first line.
    (gitdir / "config").write_text(
        "[core]\n\tbare = false\n[include]\n\tpath = other.cfg\n", encoding="utf-8"
    )
    reason = repository._include_refusal(str(checkout))
    assert reason and "includes another file" in reason


# ---------------------------------------------------------------------------
# The per-read clearance covers INCLUDES, not filter drivers alone.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_clearance_re_takes_the_include_gate_on_every_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An include written AFTER adoption is refused by the read it would reach.

    The include gate lived on the probe chokepoint (``_probe_git``) only, and the
    fleet's own reads spawn through ``_run_gated_git`` instead -- so a repository that
    named no include when it was adopted and added one afterwards had every later read
    open whatever it named. git follows ``include.path`` while parsing config on EVERY
    command, so the verb safelist does not bound this one: an include is followed
    before the verb is considered at all.

    The ORDERING is asserted too, and it is not cosmetic. The filter probe asks git
    with ``--includes``, so run first against a config naming one it would itself open
    the included file -- the exact read being refused.
    """
    monkeypatch.setattr(
        repository,
        "_include_refusal",
        lambda _path: "its own git config includes another file",
    )

    def unreachable(_path: str) -> tuple[list[str], str | None]:
        raise AssertionError(
            "the filter probe asks git with --includes, so it must not run against a "
            "config that names one"
        )

    monkeypatch.setattr(repository, "_configured_filter_commands", unreachable)

    with pytest.raises(repository.RepoUnreadable, match="includes another file"):
        await repository._assert_read_cleared("/foreign/checkout", read_only=True)

    # And the app's own checkout pays neither probe: the disposition gates both.
    def never(_path: str) -> str | None:
        raise AssertionError("the managed checkout must not be include-scanned")

    monkeypatch.setattr(repository, "_include_refusal", never)
    await repository._assert_read_cleared("/our/own/checkout", read_only=False)


@pytest.mark.asyncio
async def test_a_foreign_read_asks_for_the_credential_homes_to_be_hidden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gated spawn requests a credential mask, and only for a foreign checkout.

    The clearance cannot authorize mutable config -- the repository can rewrite it
    between the clearance and the exec, and the module's own comment concedes no
    check-then-spawn closes that. So the window is made to pay less: where the sandbox
    can enforce a hidden-dir mask, the credential homes are hidden for that child.

    Asked for on the foreign path ONLY. This product's own checkout is the pre-existing
    trust boundary and its reads legitimately use the operator's credentials.
    """
    asked: list[tuple[str, ...]] = []

    async def _capture(_cmd, **kwargs):
        asked.append(tuple(kwargs.get("extra_hidden_dirs", ())))
        return 0, "", ""

    monkeypatch.setattr(runtime, "_run_cmd", _capture)

    async def cleared(_path: str, *, read_only: bool) -> None:
        return None

    monkeypatch.setattr(repository, "_assert_read_cleared", cleared)

    monkeypatch.setattr(repository, "_REPO_READ_ONLY_MSG", "read-only: no markers")
    await repository._run_gated_git("/foreign/checkout", "rev-parse", "HEAD")

    monkeypatch.setattr(repository, "_REPO_READ_ONLY_MSG", None)
    await repository._run_gated_git("/our/own/checkout", "rev-parse", "HEAD")

    foreign, own = asked
    assert foreign == runtime._credential_store_dirs()
    # Named rather than inferred: the point is WHICH trees, and a tuple that merely
    # differs from the managed one would pass while masking the wrong directories.
    assert [os.path.basename(p) for p in foreign] == [".aws", ".ssh", ".kube"]
    assert all(os.path.isabs(p) for p in foreign)
    assert own == ()


# ---------------------------------------------------------------------------
# Signature verification is a third repo-controlled driver class.
# ---------------------------------------------------------------------------


def test_a_repo_cannot_name_the_program_git_would_verify_signatures_with(tmp_path) -> None:
    """Real git, real precedence: the repository's own config loses to the pins.

    ``log`` is on the foreign-checkout safelist because it converts no content, which
    is true and does not cover this: ``[log] showSignature = true`` makes git VERIFY
    every signature it prints, and verification execs the program ``gpg.*.program``
    names. That is a read running a program of the repository's choosing.

    Asserted through git's own resolution rather than by reading the dict, because the
    claim is about PRECEDENCE -- that ``GIT_CONFIG_*`` outranks repo-local config --
    and a dict assertion cannot say whether git agrees. ``update_governance`` pins the
    same key against the same vector.
    """
    import shutil
    import subprocess as sp

    git = shutil.which("git")
    if git is None:
        pytest.skip("git is required to exercise real config precedence")

    repo = tmp_path / "theirs"
    repo.mkdir()
    sp.run([git, "init", "-q", str(repo)], check=True, capture_output=True)
    # Exactly what a foreign repository would write to be exec'd on a plain read.
    payload = tmp_path / "payload.sh"
    payload.write_text("#!/bin/sh\ntouch /tmp/pwned\n", encoding="utf-8")
    sp.run(
        [git, "-C", str(repo), "config", "--local", "gpg.program", str(payload)],
        check=True,
        capture_output=True,
    )
    for key in ("gpg.openpgp.program", "gpg.ssh.program", "gpg.x509.program"):
        sp.run(
            [git, "-C", str(repo), "config", "--local", key, str(payload)],
            check=True,
            capture_output=True,
        )
    sp.run(
        [git, "-C", str(repo), "config", "--local", "log.showSignature", "true"],
        check=True,
        capture_output=True,
    )

    # The repository gets its way with a bare environment -- that is the hazard.
    bare = sp.run(
        [git, "-C", str(repo), "config", "--get", "gpg.program"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert bare.stdout.strip() == str(payload)

    # And loses to the env the handler spawns every git with.
    env = {**os.environ, **runtime._GIT_ENV_NEUTRALIZERS}
    for key in (
        "gpg.program",
        "gpg.openpgp.program",
        "gpg.ssh.program",
        "gpg.x509.program",
    ):
        answer = sp.run(
            [git, "-C", str(repo), "config", "--get", key],
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )
        assert answer.stdout.strip() == "true", key
    trigger = sp.run(
        [git, "-C", str(repo), "config", "--get", "log.showSignature"],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    assert trigger.stdout.strip() == "false"


def test_every_config_pin_is_inside_the_count_git_reads() -> None:
    """A pair past ``GIT_CONFIG_COUNT`` is silently inert, so the two must agree.

    git reads ``GIT_CONFIG_KEY_0``..``KEY_{COUNT-1}`` and ignores the rest, so a pin
    added without bumping the count looks present in the dict and does nothing at all.
    """
    pins = runtime._GIT_ENV_NEUTRALIZERS
    count = int(pins["GIT_CONFIG_COUNT"])
    keys = [k for k in pins if k.startswith("GIT_CONFIG_KEY_")]
    assert len(keys) == count
    for index in range(count):
        assert f"GIT_CONFIG_KEY_{index}" in pins
        assert f"GIT_CONFIG_VALUE_{index}" in pins


# ---------------------------------------------------------------------------
# A bounded capture must not report an unknown exit status as success.
# ---------------------------------------------------------------------------


class _FakeStream:
    """A pipe that reaches EOF immediately."""

    def __init__(self, payload: bytes = b"") -> None:
        self._payload = payload

    async def read(self, _n: int) -> bytes:
        chunk, self._payload = self._payload, b""
        return chunk


class _FakeProc:
    """A child whose status arrives only when it is WAITED for.

    Which is how asyncio really behaves: the exit status is delivered by a separate
    child-watcher callback, so both pipes can reach EOF while ``returncode`` is still
    ``None``. ``communicate`` ends with ``await self.wait()`` for exactly this reason.
    """

    def __init__(self, code: int | None = 128) -> None:
        self.stdout = _FakeStream(b"")
        self.stderr = _FakeStream(b"fatal: not a git repository\n")
        self.returncode: int | None = None
        self.pid = -1
        self._code = code
        self.waited = 0

    async def wait(self) -> int | None:
        self.waited += 1
        self.returncode = self._code
        return self.returncode


@pytest.mark.asyncio
async def test_a_bounded_capture_waits_for_the_child_before_reporting() -> None:
    """EOF on both pipes says the child closed them, not that it exited."""
    proc = _FakeProc(code=128)

    async def _kill() -> None:
        raise AssertionError("nothing overflowed")

    out, err, overflowed = await runtime._capture_bounded(proc, 1024, _kill)

    assert overflowed is False
    assert out == b""
    assert b"not a git repository" in err
    assert proc.waited == 1
    # The reading this exists for: a caller may now trust the status.
    assert proc.returncode == 128


@pytest.mark.asyncio
async def test_a_bounded_read_reports_gits_real_failure_not_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A git that exits 128 must not be reported as rc 0 with empty output.

    ``proc.returncode or 0`` mapped an unknown status onto SUCCESS, and only the
    bounded branch was exposed -- ``communicate`` awaits the child itself. The call
    site that matters gates on ``rc != 0`` to raise with git's stderr, so scoring the
    failure as 0 made a foreign or corrupt checkout render the "no worktrees found"
    empty state instead of the reason.
    """
    monkeypatch.setattr(runtime, "_trusted_bin", lambda _name: "/usr/bin/git")

    def _prepare(cmd, _mode, env=None, extra_hidden_dirs=()):
        return list(cmd), dict(env or {}), None

    monkeypatch.setattr(runtime, "sandboxed_spawn_argv", _prepare)

    async def _off_loop(fn, executor=None):
        return fn()

    monkeypatch.setattr(runtime, "shielded_prepare_off_loop", _off_loop)

    async def _spawn(*_a, **_kw):
        return _FakeProc(code=128)

    monkeypatch.setattr(runtime, "create_subprocess_limited", _spawn)

    rc, _out, err = await runtime._run_cmd(
        ["git", "-C", "/foreign", "worktree", "list", "--porcelain"],
        max_output_bytes=4096,
    )
    assert rc == 128
    assert "not a git repository" in err

    # And a status that never resolves is a FAILURE, not a success: -1 is the honest
    # answer where `or 0` claimed the command worked.
    async def _spawn_unknown(*_a, **_kw):
        return _FakeProc(code=None)

    monkeypatch.setattr(runtime, "create_subprocess_limited", _spawn_unknown)
    rc_unknown, _o, _e = await runtime._run_cmd(
        ["git", "-C", "/foreign", "worktree", "list", "--porcelain"],
        max_output_bytes=4096,
    )
    assert rc_unknown == -1


# ---------------------------------------------------------------------------
# A rebase must not act on a base branch nobody stated.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_stated_base_branch_is_told_apart_from_a_guessed_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The two answering tiers are positive; the last-resort tier is not.

    The final tier publishes whatever branch is checked out, and its own comment
    concedes the trigger is ordinary -- a dev box's checkout sits on a feature branch.
    That is a fine label for a row and a wrong base for a rebase, so the difference is
    recorded rather than left for each consumer to re-derive.
    """
    monkeypatch.setattr(repository, "_repo_read", lambda: "/repo")

    # A remote that publishes HEAD: the repository's own statement.
    _stub_base_branch_git(
        monkeypatch, remotes="origin\n", published={"origin": "trunk"}, local=set()
    )
    monkeypatch.setattr(repository, "_BASE_BRANCH_POSITIVE", False)
    await repository._resolve_base_branch()
    assert repository.BASE_BRANCH == "trunk"
    assert repository._BASE_BRANCH_POSITIVE is True
    assert repository.base_branch_mutation_refusal() is None

    # No remote HEAD, but a conventional default exists here. Named from the module's
    # own tuple rather than spelled out, so this assertion follows the candidate list
    # if it ever changes.
    legacy = repository._LOCAL_BASE_CANDIDATES[1]
    _stub_base_branch_git(monkeypatch, remotes="origin\n", published={}, local={legacy})
    monkeypatch.setattr(repository, "_BASE_BRANCH_POSITIVE", False)
    await repository._resolve_base_branch()
    assert repository.BASE_BRANCH == legacy
    assert repository._BASE_BRANCH_POSITIVE is True

    # Neither: the checked-out branch is published as a LABEL, not as a base.
    _stub_base_branch_git(
        monkeypatch, remotes="origin\n", published={}, local=set(), head="feature/x"
    )
    monkeypatch.setattr(repository, "_BASE_BRANCH_POSITIVE", True)
    await repository._resolve_base_branch()
    assert repository.BASE_BRANCH == "feature/x"
    assert repository._BASE_BRANCH_POSITIVE is False
    refusal = repository.base_branch_mutation_refusal()
    assert refusal and "feature/x" in refusal


@pytest.mark.asyncio
async def test_a_rebase_refuses_a_guessed_base_before_it_fetches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing is fetched and nothing is rewritten while the base is a guess.

    A clean replay onto the wrong base returns ``ok`` and names no rollback, so this
    is the one operation here that cannot be undone from its own result -- the gate
    therefore sits before the fetch rather than after it.
    """
    calls: list[tuple[str, ...]] = []

    async def fake_git(_path, *args, **_kw):
        calls.append(tuple(args))
        return "" if args and args[0] == "status" else "ok"

    monkeypatch.setattr(repository, "_git", fake_git)

    async def _never(*_a, **_kw):
        raise AssertionError("no rebase may spawn while the base branch is a guess")

    monkeypatch.setattr(runtime, "_run_cmd", _never)
    monkeypatch.setattr(repository, "_BASE_BRANCH_POSITIVE", False)
    monkeypatch.setattr(repository, "BASE_BRANCH", "feature/x")

    res = await worktree_ops._rebase_locked({"path": "/r"})

    assert res["ok"] is False
    assert "refusing to rebase" in res["error"]
    # The dirt gate above it still ran; the fetch below it did not.
    assert ("status", "--porcelain") in calls
    assert not [c for c in calls if c and c[0] == "fetch"]


# ---------------------------------------------------------------------------
# Only a filter key that names a PROGRAM is a driver.
# ---------------------------------------------------------------------------


def test_a_filter_flag_that_names_no_program_is_not_a_driver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``filter.<name>.required`` is a boolean, so refusing on it refuses a safe repo.

    The probe asks for the whole ``filter.`` section because git has no regexp for
    "the command keys of any driver name", and that section also carries ``required``
    -- which only says git should fail when the filter fails. Returning it produced a
    refusal claiming the repository "configures executable git filter drivers", a
    statement that of that key was simply false, and it blocked a repository that had
    configured nothing executable at all.

    ``_FILTER_COMMAND_SUFFIXES`` already named the right predicate; nothing applied it.
    """
    # A repository that sets the flag and no command: safe, and served.
    _filter_probe_calls(monkeypatch, {_FILTER_RE: (0, "filter.demo.required\n")})
    assert repository._configured_filter_commands("/repo") == ([], None)

    # The same flag beside a real driver: the driver is still found, alone.
    _filter_probe_calls(
        monkeypatch,
        {_FILTER_RE: (0, "filter.demo.required\nfilter.lfs.clean\n")},
    )
    drivers, unread = repository._configured_filter_commands("/repo")
    assert drivers == ["filter.lfs.clean"]
    assert unread is None

    # Every command spelling counts, and a driver NAME keeps its case: git lowercases
    # the section and the final key only, so the suffix test must not be case-blind to
    # the name it reports back to the operator.
    _filter_probe_calls(
        monkeypatch,
        {
            _FILTER_RE: (
                0,
                "filter.MyDriver.smudge\nfilter.MyDriver.process\n"
                "filter.MyDriver.required\nfilter.MyDriver.clean\n",
            )
        },
    )
    found, _ = repository._configured_filter_commands("/repo")
    assert found == [
        "filter.MyDriver.smudge",
        "filter.MyDriver.process",
        "filter.MyDriver.clean",
    ]
    assert all(key.lower().endswith(repository._FILTER_COMMAND_SUFFIXES) for key in found)


def test_no_snapshot_is_written_for_config_bytes_that_name_an_include(tmp_path) -> None:
    """The snapshot writer itself refuses an include, not just its callers.

    ``_probe_git_snapshot`` points git's repository discovery at the snapshot's own
    directory, where the file is read as a repository's config -- and at repository
    scope git follows ``include.path`` by DEFAULT, no flag asked for. So a snapshot
    holding an include would reach straight through the isolation that stops git
    touching the real checkout.

    Guarded where the file is CREATED so every probe shape inherits it, including one
    added later, rather than depending on which caller happened to check first. The
    directory must be gone too: a refusal that leaves a private temp dir behind
    accumulates one per poll.
    """
    before = set(pathlib.Path(tempfile.gettempdir()).glob("dev-fleet-cfg-*"))

    with pytest.raises(repository.ProbeIncludes, match="names an include"):
        with repository._config_snapshot("[core]\n\tbare = false\n[include]\n\tpath = x\n"):
            raise AssertionError("the body must not run")

    after = set(pathlib.Path(tempfile.gettempdir()).glob("dev-fleet-cfg-*"))
    assert after == before

    # And the ordinary path still yields a readable 0600 file that is removed on exit.
    with repository._config_snapshot("[core]\n\tbare = false\n") as snapshot:
        assert os.path.isfile(snapshot)
        assert oct(os.stat(snapshot).st_mode & 0o777) == "0o600"
        held = snapshot
    assert not os.path.exists(held)
