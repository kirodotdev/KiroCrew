"""``MAIN_REPO`` reaches git and the filesystem only through ``_repo()``.

Dev Fleet represents "no main checkout found" as an empty string in
``MAIN_REPO``. That sentinel is fail-open at any call site that consumes the
global directly: ``git -C ""`` does not fail — it silently runs against the
backend process's working directory — and ``Path("")`` is ``Path(".")``, so an
unguarded consumer operates on an arbitrary directory and returns plausible
results. The ``_repo()`` accessor centralizes the guard: it returns the path or
raises ``RepoNotConfigured``, which the HMAC middleware converts to the 409
``repo_not_configured`` boundary.

Two enforcement tiers (same pattern as ``test_apps_instances_loop_offload.py``):

- Behavior tests: ``_repo()`` raises on the empty sentinel and returns the
  path otherwise, preserving the exception type the middleware boundary maps.
- AST ratchet: outside the accessor itself, a ``MAIN_REPO`` load may appear
  ONLY as a bare truthiness guard (``if MAIN_REPO:`` / ``not MAIN_REPO`` / a
  ``BoolOp`` operand). Any other load — a git argv element, a subprocess
  ``cwd=``, a ``Path(...)`` build, an f-string interpolation, a payload
  field — fails this test, so a future call site cannot silently reintroduce
  the fail-open shape.
"""

from __future__ import annotations

import ast
import inspect
import locale
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from kiro_crew.apps.builtins.dev_fleet import (
    fleet_state,
    http_api,
    live,
    repository,
    runtime,
    server,
    worktree_ops,
)

# The accessor is the ONLY function whose body may read the bare global: it IS
# the guard. The startup hook's discovery/re-resolve runs on a local and writes
# the global exactly once (a Store, which this ratchet ignores), so even the
# assignment site needs no exemption — and a git call added to startup, where
# MAIN_REPO is most often still unresolved, is caught like anywhere else.
_DEV_FLEET_MODULES = (
    runtime,
    repository,
    live,
    fleet_state,
    worktree_ops,
    http_api,
    server,
)
_ALLOWED_LOADS = {(repository.__name__, "_repo")}


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
            )
    assert not violations, (
        "MAIN_REPO's empty-string sentinel is fail-open when consumed "
        "directly (git -C '' runs against the process CWD). Use _repo():\n" + "\n".join(violations)
    )


def test_repo_accessor_raises_on_unresolved_checkout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(repository, "MAIN_REPO", "")
    monkeypatch.setattr(repository, "_REPO_CFG_UNREADABLE_MSG", None)
    with pytest.raises(repository.RepoNotConfigured):
        repository._repo()


def test_repo_accessor_surfaces_a_config_parse_failure_over_the_missing_checkout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The config-parse message outranks the missing-checkout message.

    When a config file is present but will not parse, Dev Fleet refuses to resolve a
    checkout — the deliberate behaviour — so ``MAIN_REPO`` stays empty. A message
    attached only to the resolved-but-invalid-path slot would never surface, because
    ``_repo()`` tests ``MAIN_REPO`` for truthiness before it and would raise
    ``RepoNotConfigured`` ("no Kiro Crew checkout found"), sending the operator to look
    for a checkout that was never the problem. ``_REPO_CFG_UNREADABLE_MSG`` is checked
    BEFORE that gate, so the operator is told which file failed to read or parse.
    """
    monkeypatch.setattr(repository, "MAIN_REPO", "")
    monkeypatch.setattr(
        repository,
        "_REPO_CFG_UNREADABLE_MSG",
        "config.local.json is present but could not be read or parsed",
    )
    with pytest.raises(repository.RepoUnreadable, match="config.local.json"):
        repository._repo()


def test_repo_accessor_returns_resolved_checkout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(repository, "MAIN_REPO", "/somewhere/kirocrew")
    assert repository._repo() == "/somewhere/kirocrew"


def test_primary_checkout_resolution_preserves_the_host_text_decoder(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Moving the startup probe must not reinterpret non-ASCII checkout paths."""
    primary = tmp_path / "primary"
    seen: dict[str, object] = {}

    def _run(_argv, **kwargs):
        seen.update(kwargs)
        return SimpleNamespace(returncode=0, stdout=str(primary / ".git"))

    monkeypatch.setattr(runtime, "_trusted_bin", lambda _name: "git")
    monkeypatch.setattr(repository.subprocess, "run", _run)

    assert repository._resolve_primary_checkout(str(tmp_path / "linked")) == str(primary)
    assert seen["text"] is True
    assert seen["encoding"] == locale.getpreferredencoding(False)


# ---------------------------------------------------------------------------
# The base branch is the resolved checkout's own, not a hardcoded "main".
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "name, ok",
    [
        ("main", True),
        ("trunk", True),
        ("release/2.0", True),
        ("feature.x", True),
        # A leading dash is parsed as a FLAG by git once the name is
        # interpolated into an argv, so it must never be accepted.
        ("--exec=touch /nowhere/pwn", False),
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
    fallback loop without ever meeting ``_plausible_branch_name``, which only screens
    the remote's answer. Asserting over the tuple itself keeps a name added later
    from slipping past.
    """
    assert repository._LOCAL_BASE_CANDIDATES
    for candidate in repository._LOCAL_BASE_CANDIDATES:
        assert repository._plausible_branch_name(candidate) is True


def _stub_base_branch_git(
    monkeypatch: pytest.MonkeyPatch,
    *,
    remotes: str,
    published: dict[str, str],
    local: set[str],
    head: str | None = None,
    configured_remote: str | None = None,
    base_configured_remote: str | None = None,
) -> list[str]:
    """Wire the git reads ``_resolve_base_branch`` makes. Returns the ref probes.

    ``configured_remote`` is what ``git config branch.<checked-out>.remote`` returns --
    the remote the checkout tracks, which the resolver prefers over ``origin``. It is
    read only when ``head`` names a plausible checked-out branch.

    ``base_configured_remote`` is what ``git config branch.<resolved-base>.remote``
    returns -- the remote the BASE branch tracks, read on the second pass once the base
    name is known. When it names a different remote than the checkout's, the base is
    re-verified against that remote's advertised HEAD. Defaults to
    ``configured_remote`` so a test that does not exercise the fork-divergence case
    sees the base and checkout tracking the same remote (no second verification).
    """
    probed: list[str] = []
    if base_configured_remote is None:
        base_configured_remote = configured_remote

    async def _git(_repo: str, *args: str, **_kw: object) -> str | None:
        if args[0] == "remote":
            return remotes
        if args[0] == "config":
            # ``config branch.<name>.remote``. The checked-out branch's remote is read
            # first (the provisional pass); the resolved base's remote second.
            key = args[-1]
            probed.append(f"config {key}")
            if head is not None and key == f"branch.{head}.remote":
                return configured_remote
            return base_configured_remote
        if args[0] == "ls-remote":
            # ``ls-remote --symref <remote> HEAD`` -- the remote's LIVE advertised HEAD.
            remote = args[-2]
            probed.append(f"ls-remote {remote} HEAD")
            published_head = published.get(remote)
            if not published_head:
                return None
            return f"ref: refs/heads/{published_head}\tHEAD\n<sha>\tHEAD\n"
        if args[0] == "symbolic-ref":
            ref = args[-1]
            probed.append(ref)
            if ref == "HEAD":
                return head
            return None
        if args[0] == "rev-parse":
            name = args[-1].removeprefix("refs/heads/")
            return name if name in local else None
        raise AssertionError(f"unexpected git call: {args}")

    monkeypatch.setattr(repository, "MAIN_REPO", "/somewhere/other-project")
    monkeypatch.setattr(repository, "_REPO_INVALID_MSG", None)
    monkeypatch.setattr(repository, "BASE_BRANCH", "main")
    # Patched here so EVERY caller gets it restored. ``_resolve_base_branch`` assigns
    # this global for real, so a test that only patches ``BASE_BRANCH`` would leave
    # the verdict behind and decide a later test in another file -- which is how a
    # rebase test passed on leakage instead of on its own setup.
    monkeypatch.setattr(repository, "_BASE_BRANCH_POSITIVE", False)
    monkeypatch.setattr(repository, "_git", _git)
    return probed


@pytest.mark.asyncio
async def test_base_branch_ignores_a_remote_sorted_before_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An alphabetically earlier remote must not decide the rebase base.

    ``archive`` publishes one default and ``origin`` another. Only ``origin`` may be
    consulted, because ``_upstream_remote`` resolves to it and the two answers are
    combined into a single rev range.
    """
    probed = _stub_base_branch_git(
        monkeypatch,
        remotes="archive\norigin\n",
        published={"archive": "legacy-default", "origin": "trunk"},
        local=set(),
    )
    await repository._resolve_base_branch()
    assert repository.BASE_BRANCH == "trunk"
    # After resolving the base against ``origin``, the base's OWN tracking remote is
    # read (second pass); it names none here, so the origin verdict stands.
    assert probed == ["HEAD", "ls-remote origin HEAD", "config branch.trunk.remote"]


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
    # The base's own tracking remote is read on the second pass; it names none, so the
    # sole remote's verdict stands.
    assert probed == ["HEAD", "ls-remote kirocrew HEAD", "config branch.release/3.remote"]


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
    assert probed == ["HEAD"]


@pytest.mark.asyncio
async def test_a_present_candidate_outranks_the_checked_out_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HEAD is the LAST tier, because a dev checkout sits on a feature branch.

    ``main`` exists here, so it is the base even though the checkout is parked on
    someone's branch -- taking HEAD would retarget every rebase and every
    ahead/behind reading at that branch for as long as it stays checked out.
    """
    candidate = repository._LOCAL_BASE_CANDIDATES[0]
    _stub_base_branch_git(
        monkeypatch,
        remotes="origin\n",
        published={},
        local={candidate, "feature/some-work"},
        head="feature/some-work",
    )
    await repository._resolve_base_branch()
    assert repository.BASE_BRANCH == candidate
    # HEAD is read once up front to find the checkout's tracking remote, but the
    # present candidate still outranks it: the base is the candidate, never the
    # checked-out feature branch.
    assert repository.BASE_BRANCH != "feature/some-work"


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
        head="--upload-pack=touch /nowhere/pwned",
    )
    await repository._resolve_base_branch()
    assert repository.BASE_BRANCH == "main"
    assert probed == ["HEAD", "ls-remote origin HEAD", "HEAD"]


@pytest.mark.asyncio
async def test_an_unresolved_checkout_asks_git_nothing_and_keeps_the_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No checkout means no spawn: ``git`` here would answer for the backend's own cwd."""

    async def _never(*_a, **_kw):
        raise AssertionError("no git may run while the checkout is unresolved")

    monkeypatch.setattr(repository, "MAIN_REPO", "")
    monkeypatch.setattr(repository, "BASE_BRANCH", "main")
    monkeypatch.setattr(repository, "_git", _never)
    await repository._resolve_base_branch()
    assert repository.BASE_BRANCH == "main"


@pytest.mark.asyncio
async def test_a_sole_remote_with_an_option_like_name_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sole remote named like a git option never reaches an argv.

    ``git remote`` prints a ``[remote "--upload-pack=…"]`` section name from the
    agent-writable ``.git/config`` verbatim. Feeding it unseparated into
    ``git ls-remote --symref {remote} HEAD`` would make the privileged backend exec a
    program the repository named. The sole-remote fallback passes the same option/name
    gate as the configured candidate, so an option-like sole remote is refused: no base
    resolves against it, and a mutation refuses rather than acting.
    """
    probed = _stub_base_branch_git(
        monkeypatch,
        remotes="--upload-pack=/tmp/pwn\n",
        published={"--upload-pack=/tmp/pwn": "main"},
        local=set(),
    )
    await repository._resolve_base_branch()

    # The option-like remote was never handed to ``ls-remote``.
    assert not any("--upload-pack" in pr for pr in probed)
    assert not any(pr.startswith("ls-remote") for pr in probed)
    # No positive base resolved against it, so a mutation refuses.
    assert repository._BASE_BRANCH_POSITIVE is False
    assert repository.base_branch_mutation_refusal() is not None


def test_plausible_remote_name_refuses_option_like_and_metachar_names() -> None:
    """The remote-name gate accepts real names and refuses option/argv-hazard shapes."""
    assert repository._plausible_remote_name("origin")
    assert repository._plausible_remote_name("upstream")
    assert repository._plausible_remote_name("kirocrew")
    assert repository._plausible_remote_name("my.remote-2")
    assert not repository._plausible_remote_name("")
    assert not repository._plausible_remote_name("--upload-pack=/tmp/x")
    assert not repository._plausible_remote_name("-o")
    assert not repository._plausible_remote_name("a b")
    assert not repository._plausible_remote_name("a/b")


@pytest.mark.asyncio
async def test_the_configured_tracking_remote_is_preferred_over_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The checkout's own tracking remote decides the base, not a fork's ``origin``.

    A fork whose ``origin`` advertises ``main`` while the checkout tracks ``upstream``
    (``branch.<checked-out>.remote = upstream``) is an ordinary dev-box state. Picking
    ``origin`` would verify AND rebase onto ``origin/main`` -- a base the configured
    upstream never stated -- rewriting the worktree with no undo. The resolver reads
    the configured remote first and verifies the base against THAT remote's live HEAD.
    """
    probed = _stub_base_branch_git(
        monkeypatch,
        remotes="origin\nupstream\n",
        # origin advertises 'main'; upstream (the tracked remote) advertises 'trunk'.
        published={"origin": "main", "upstream": "trunk"},
        local=set(),
        head="feature/work",
        configured_remote="upstream",
    )
    await repository._resolve_base_branch()

    # The base came from the tracked remote (upstream/trunk), not origin/main.
    assert repository.BASE_BRANCH == "trunk"
    assert repository._BASE_BRANCH_POSITIVE is True
    # It read the configured remote and verified against upstream, never origin.
    assert "config branch.feature/work.remote" in probed
    assert "ls-remote upstream HEAD" in probed
    assert "ls-remote origin HEAD" not in probed


@pytest.mark.asyncio
async def test_the_base_branchs_own_remote_decides_not_the_checkouts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The BASE branch's tracking remote decides the base, not the checkout's.

    A fork layout diverges the two: the checked-out feature branch tracks ``origin``
    (the fork) while the resolved base ``main`` tracks ``upstream``. The first pass
    resolves a provisional base against ``origin`` (the checkout's remote), but the
    rebase would replay onto the BASE's own remote -- so trusting ``origin``'s answer
    would rewrite the worktree onto history ``upstream`` never stated. The resolver
    reads the base's OWN remote on the second pass and re-verifies the base against
    ``upstream``'s live HEAD, pairing the positive base with that remote.
    """
    probed = _stub_base_branch_git(
        monkeypatch,
        remotes="origin\nupstream\n",
        # origin (the checkout's remote) advertises 'main'; upstream (the BASE's
        # remote) advertises 'release'.
        published={"origin": "main", "upstream": "release"},
        local=set(),
        head="feature/work",
        # The checkout tracks origin; the resolved base 'main' tracks upstream.
        configured_remote="origin",
        base_configured_remote="upstream",
    )
    await repository._resolve_base_branch()

    # The base was re-verified against the base's own remote (upstream), and the
    # snapshot's remote is upstream -- so the rebase fetches and replays from upstream,
    # not the fork's origin.
    assert repository.BASE_BRANCH == "release"
    assert repository._BASE_BRANCH_POSITIVE is True
    assert repository._UPSTREAM_REMOTE == "upstream"
    # It resolved a provisional base against origin, then read the base's own remote
    # and re-verified against upstream.
    assert "config branch.feature/work.remote" in probed
    assert "ls-remote origin HEAD" in probed
    assert "config branch.main.remote" in probed
    assert "ls-remote upstream HEAD" in probed


@pytest.mark.asyncio
async def test_a_base_tracking_an_unconfirmable_remote_is_not_positive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A base whose own remote can't confirm its default is NOT positive.

    The checkout tracks ``origin`` and resolves a provisional base ``main``, but that
    base tracks ``upstream`` which advertises no default (offline, or no symref). The
    checkout's ``origin`` answer must not stand in for the base's unconfirmable remote:
    trusting it would rewrite the worktree onto the wrong history. So the snapshot is
    NOT positive and a rebase refuses rather than act on the guess.
    """
    probed = _stub_base_branch_git(
        monkeypatch,
        remotes="origin\nupstream\n",
        # origin advertises 'main'; upstream advertises nothing (not in published).
        published={"origin": "main"},
        local=set(),
        head="feature/work",
        configured_remote="origin",
        base_configured_remote="upstream",
    )
    await repository._resolve_base_branch()

    # The base's own remote could not confirm, so no positive verdict -- a mutation
    # refuses rather than rebase onto origin's unverified answer.
    assert repository._BASE_BRANCH_POSITIVE is False
    assert repository.base_branch_mutation_refusal() is not None
    # It did try the base's own remote before giving up.
    assert "config branch.main.remote" in probed
    assert "ls-remote upstream HEAD" in probed


@pytest.mark.asyncio
async def test_a_stated_base_branch_is_told_apart_from_a_guessed_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The two answering tiers are positive; the last-resort tier is not.

    The final tier publishes whatever branch is checked out, and its trigger is
    ordinary -- a dev box's checkout sits on a feature branch. That is a fine label
    for a row and a wrong base for a rebase, so the difference is recorded rather
    than left for each consumer to re-derive.
    """
    # A remote that publishes HEAD: the repository's own statement.
    _stub_base_branch_git(
        monkeypatch, remotes="origin\n", published={"origin": "trunk"}, local=set()
    )
    monkeypatch.setattr(repository, "_BASE_BRANCH_POSITIVE", False)
    await repository._resolve_base_branch()
    assert repository.BASE_BRANCH == "trunk"
    assert repository._BASE_BRANCH_POSITIVE is True
    assert repository.base_branch_mutation_refusal() is None

    # No remote HEAD, but a conventional default exists here. It becomes the LABEL and
    # stays a guess: a name existing locally is not the repository stating anything, and
    # a `main` left behind by a rename to `trunk` is the ordinary residue of that
    # rename. Named from the module's own tuple rather than spelled out, so this
    # follows the candidate list if it ever changes.
    legacy = repository._LOCAL_BASE_CANDIDATES[1]
    _stub_base_branch_git(monkeypatch, remotes="origin\n", published={}, local={legacy})
    monkeypatch.setattr(repository, "_BASE_BRANCH_POSITIVE", True)
    await repository._resolve_base_branch()
    assert repository.BASE_BRANCH == legacy
    assert repository._BASE_BRANCH_POSITIVE is False
    assert repository.base_branch_mutation_refusal() is not None

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
async def test_positive_base_comes_from_the_remotes_live_head_not_a_local_ref(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the remote's LIVE advertised HEAD earns positive, never a local ref.

    The local ``refs/remotes/<remote>/HEAD`` tracking ref is recorded once and never
    refreshed by ``fetch``, so when the remote's default moves while the old branch
    still exists it names a branch the remote stopped defaulting to. Trusting it as
    positive would let ``/rebase`` cleanly rewrite a worktree onto that former default
    with no undo. The resolver therefore asks the remote what its HEAD is now
    (``ls-remote --symref``); a stale local ref is not consulted and cannot go positive.
    """
    calls: list[tuple[str, ...]] = []

    async def _git(_repo, *args, **_kw):
        calls.append(tuple(args))
        if args[0] == "remote":
            return "origin\n"
        # The remote's live answer is 'trunk'; the local tracking ref (if it were read)
        # would still say the former default -- but it is never read.
        if args[0] == "ls-remote":
            return "ref: refs/heads/trunk\tHEAD\n<sha>\tHEAD\n"
        if args[0] == "symbolic-ref" and args[-1] == "HEAD":
            return "feature/x"
        return None

    monkeypatch.setattr(repository, "MAIN_REPO", "/somewhere/other-project")
    monkeypatch.setattr(repository, "_REPO_INVALID_MSG", None)
    monkeypatch.setattr(repository, "BASE_BRANCH", "main")
    monkeypatch.setattr(repository, "_BASE_BRANCH_POSITIVE", False)
    monkeypatch.setattr(repository, "_git", _git)

    await repository._resolve_base_branch()

    assert repository.BASE_BRANCH == "trunk"
    assert repository._BASE_BRANCH_POSITIVE is True
    # The live remote probe ran; the stale local tracking ref was never consulted.
    assert any(c[0] == "ls-remote" and c[-1] == "HEAD" for c in calls)
    assert not any(c[0] == "symbolic-ref" and any("refs/remotes/" in a for a in c) for c in calls)


@pytest.mark.asyncio
async def test_a_latched_positive_does_not_survive_a_non_resolving_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A positive verdict is earned fresh each call, never inherited.

    ``_BASE_BRANCH_POSITIVE`` is SET only by tier 1, and the ``RepoUnavailable`` early
    return writes neither it nor ``BASE_BRANCH``. If the flag were not reset up front, a
    ``True`` latched by an earlier resolution would survive a later call that resolved
    nothing, and a mutation would act on a base THIS call never confirmed.
    """
    monkeypatch.setattr(repository, "_BASE_BRANCH_POSITIVE", True)
    monkeypatch.setattr(repository, "BASE_BRANCH", "trunk")

    async def _never(*_a, **_kw):
        raise AssertionError("no git may run while the checkout is unresolved")

    monkeypatch.setattr(repository, "MAIN_REPO", "")
    monkeypatch.setattr(repository, "_git", _never)

    await repository._resolve_base_branch()

    # The early return left BASE_BRANCH alone, but the stale positive is gone, so a
    # mutation now refuses rather than acting on an unconfirmed base.
    assert repository._BASE_BRANCH_POSITIVE is False
    assert repository.base_branch_mutation_refusal() is not None


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

    # The rebase re-resolves into a LOCAL snapshot before reading the gate, so the
    # verdict under test has to come from that resolution rather than from a value
    # planted on the module globals.
    resolved: list[str] = []

    async def _resolve_to_a_guess() -> tuple[str | None, bool, str]:
        resolved.append("resolved")
        return "feature/x", False, "origin"

    monkeypatch.setattr(repository, "_BASE_BRANCH_POSITIVE", True)
    monkeypatch.setattr(repository, "BASE_BRANCH", "main")
    monkeypatch.setattr(repository, "_resolve_base_snapshot", _resolve_to_a_guess)

    res = await worktree_ops._rebase_locked({"path": "/r"})

    assert resolved == ["resolved"], "the rebase must re-resolve, not inherit"
    assert res["ok"] is False
    assert "refusing to rebase" in res["error"]
    assert "feature/x" in res["error"], "the refusal names the base it refused"
    # The refusal is about the LOCAL snapshot; the shared global was left untouched.
    assert repository.BASE_BRANCH == "main"
    # The dirt gate above it still ran; the fetch below it did not.
    assert ("status", "--porcelain") in calls
    assert not [c for c in calls if c and c[0] == "fetch"]


@pytest.mark.asyncio
async def test_a_rebase_re_resolves_so_the_refusal_clears_without_a_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The refusal tells the operator to record the remote's default. That must work.

    Discovery latches once per process, so a base resolved at startup would be the only
    answer this process ever holds -- and the remedy the refusal names would need a
    gateway restart to take effect. The rebase therefore re-resolves immediately before
    reading the gate.
    """
    resolved: list[str] = []

    async def fake_git(_path, *args, **_kw):
        return "" if args and args[0] == "status" else "ok"

    async def _resolve() -> tuple[str | None, bool, str]:
        # The remote now advertises a default, so this attempt resolves a STATED base
        # (into a LOCAL snapshot) where the previous one found only a guess. The base
        # and the remote that stated it travel together.
        resolved.append("resolved")
        return "trunk", True, "origin"

    monkeypatch.setattr(repository, "_git", fake_git)
    monkeypatch.setattr(repository, "_resolve_base_snapshot", _resolve)
    monkeypatch.setattr(runtime, "_run_cmd", AsyncMock(return_value=(0, "", "")))
    monkeypatch.setattr(
        repository, "_git_info", AsyncMock(return_value={"head": "abc1234", "behind": 0})
    )
    # The stale state a latched discovery would have left behind on the globals.
    monkeypatch.setattr(repository, "_BASE_BRANCH_POSITIVE", False)
    monkeypatch.setattr(repository, "BASE_BRANCH", "main")

    res = await worktree_ops._rebase_locked({"path": "/r"})

    assert resolved == ["resolved"], "the rebase must re-resolve, not inherit"
    assert res["ok"] is True, f"a stated base must not be refused: {res}"
    # The rebase acted on its LOCAL snapshot and left the shared global untouched.
    assert repository.BASE_BRANCH == "main"


@pytest.mark.asyncio
async def test_a_rebase_never_mutates_the_shared_base_branch_global(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The rebase resolves its base into a LOCAL, never into ``repository.BASE_BRANCH``.

    ``_sync_start_locked`` checks ``HEAD == BASE_BRANCH`` and then re-reads that global
    across several awaits before it fetches and merges. If a concurrent ``/rebase``
    re-resolved into the global (default ``main`` -> ``trunk``) in that window, the sync
    would fetch and merge a base it never validated, holding only ``_wt_lock`` and never
    ``_SYNC_LOCK``. The rebase therefore keeps its resolved base in a local and rebases
    onto that, leaving the global the sync reads exactly where it was.
    """
    monkeypatch.setattr(repository, "BASE_BRANCH", "main")
    monkeypatch.setattr(repository, "_BASE_BRANCH_POSITIVE", True)

    async def fake_git(_path, *args, **_kw):
        return "" if args and args[0] == "status" else "ok"

    async def _resolve_to_trunk() -> tuple[str | None, bool, str]:
        # The remote's live default is 'trunk' -- a different base than the global --
        # verified against 'origin', which the rebase then fetches and replays from.
        return "trunk", True, "origin"

    rebased_onto: list[str] = []

    async def _run_cmd(cmd, **_kw):
        rebased_onto.append(cmd[-1])
        return 0, "", ""

    monkeypatch.setattr(repository, "_git", fake_git)
    monkeypatch.setattr(repository, "_resolve_base_snapshot", _resolve_to_trunk)
    monkeypatch.setattr(runtime, "_run_cmd", _run_cmd)
    monkeypatch.setattr(
        repository, "_git_info", AsyncMock(return_value={"head": "abc1234", "behind": 0})
    )

    res = await worktree_ops._rebase_locked({"path": "/r"})

    assert res["ok"] is True
    # It rebased onto the LOCAL base it resolved (origin/trunk)...
    assert rebased_onto == ["origin/trunk"]
    # ...and the shared global the sync reads was never touched.
    assert repository.BASE_BRANCH == "main"


@pytest.mark.asyncio
async def test_a_rebase_uses_the_remote_that_verified_the_base(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The rebase fetches and replays from the SAME remote that stated the base.

    The positive verdict is earned from one remote's advertised HEAD (`origin`), so the
    rebase must act on THAT remote. Re-deriving the remote from `branch.<base>.remote`
    could name a different one (`upstream`) in the ordinary fork-plus-upstream layout,
    and a clean replay onto `upstream/main` would then rewrite the worktree onto a base
    the positive verdict never verified. Base and remote are one snapshot; a stray
    `_resolve_remote_for` answer must not reach the fetch/rebase.
    """

    async def fake_git(_path, *args, **_kw):
        return "" if args and args[0] == "status" else "ok"

    async def _snapshot() -> tuple[str | None, bool, str]:
        # origin advertised main -> positive, verified against origin.
        return "main", True, "origin"

    async def _divergent_remote(_base: str) -> str:
        # branch.main.remote = upstream. If the rebase consulted this, it would replay
        # onto upstream/main -- exactly the divergence the snapshot pairing closes.
        return "upstream"

    acted_on: list[str] = []

    async def _run_cmd(cmd, **_kw):
        acted_on.append(cmd[-1])
        return 0, "", ""

    fetched: list[tuple[str, ...]] = []

    async def _git_capturing(path, *args, **_kw):
        if args and args[0] == "status":
            return ""
        if args and args[0] == "fetch":
            fetched.append(tuple(args))
        return "ok"

    monkeypatch.setattr(repository, "_git", _git_capturing)
    monkeypatch.setattr(repository, "_resolve_base_snapshot", _snapshot)
    monkeypatch.setattr(repository, "_resolve_remote_for", _divergent_remote)
    monkeypatch.setattr(runtime, "_run_cmd", _run_cmd)
    monkeypatch.setattr(
        repository, "_git_info", AsyncMock(return_value={"head": "abc1234", "behind": 0})
    )

    res = await worktree_ops._rebase_locked({"path": "/r"})

    assert res["ok"] is True
    # Fetched and rebased from origin (the verifying remote), never upstream.
    assert acted_on == ["origin/main"]
    assert any(c[:2] == ("fetch", "origin") for c in fetched)
    assert not any("upstream" in c for c in acted_on)


# ---------------------------------------------------------------------------
# A read leaves the repository byte-identical.
# ---------------------------------------------------------------------------


def test_optional_locks_are_off_for_every_git_this_handler_runs() -> None:
    """``git status`` rewrites the index unless optional locks are off.

    It is a read to its caller and a write to the repository: it refreshes the
    index's stat cache and saves it back under ``index.lock``. Every fleet render
    runs one per row, so without this the fleet contends with the operator's own git
    for the lock on the ordinary path. Pinned on the env chokepoint rather than per
    call site, which is what makes a read added later inherit it.
    """
    assert runtime._GIT_ENV_NEUTRALIZERS["GIT_OPTIONAL_LOCKS"] == "0"


def test_signature_verification_cannot_exec_a_repo_named_program() -> None:
    """Verification EXECS the program these keys name, and a READ can trigger it.

    ``[log] showSignature=true`` in an agent-writable ``.git/config`` makes every
    ``git log`` verify, and verification runs the named program. All four spellings
    are pinned because ``gpg.openpgp.program`` is a synonym that OVERRIDES the bare
    ``gpg.program``, so pinning one key leaves the other as an unpinned way in.
    """
    n = runtime._GIT_ENV_NEUTRALIZERS
    pinned = {n[f"GIT_CONFIG_KEY_{i}"]: n[f"GIT_CONFIG_VALUE_{i}"] for i in range(9)}
    for key in ("gpg.program", "gpg.openpgp.program", "gpg.ssh.program", "gpg.x509.program"):
        assert pinned[key] == "true", f"{key} must not be left to the repository"
    assert pinned["log.showSignature"] == "false", "the trigger must be pinned too"


@pytest.mark.asyncio
async def test_run_cmd_puts_the_neutralizers_in_the_child_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dict is only a guarantee if the spawn actually carries it.

    Asserted through the spawn preparation, because that is the last place the env
    can be read before the child exists, and an entry dropped anywhere earlier would
    leave the dict stating a pin nothing applies.
    """
    seen: dict[str, str] = {}

    def _prepare(cmd, _mode, env=None, **_kw):
        seen.update(env or {})
        return list(cmd), dict(env or {}), None

    monkeypatch.setattr(runtime, "sandboxed_spawn_argv", _prepare)
    monkeypatch.setattr(runtime, "_trusted_bin", lambda _name: "/usr/bin/git")
    # Trusted helpers are APPENDED after the pins and legitimately raise
    # GIT_CONFIG_COUNT past the pinned value, so this states an empty helper set
    # rather than inherit whatever an earlier test left in the module global.
    # Without it the assertion below reads the helper path's count and fails on test
    # ORDER, not on a dropped pin.
    monkeypatch.setattr(runtime, "_GIT_TRUSTED_HELPERS", {})

    async def _off_loop(fn, executor=None):
        return fn()

    monkeypatch.setattr(runtime, "shielded_prepare_off_loop", _off_loop)

    async def _no_child(*_a, **_kw):
        raise AssertionError("the env is read before the child spawns")

    monkeypatch.setattr(runtime.asyncio, "create_subprocess_exec", _no_child)
    with pytest.raises(AssertionError):
        await runtime._run_cmd(["git", "-C", "/somewhere/other-project", "status"])

    for key, val in runtime._GIT_ENV_NEUTRALIZERS.items():
        assert seen.get(key) == val, f"{key} did not reach the child env"


# ---------------------------------------------------------------------------
# A remote URL's query is a credential, and nothing derives from it.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url, locator",
    [
        ("https://h/o/r.git", "https://h/o/r.git"),
        ("https://h/o/r.git?access_token=SECRET", "https://h/o/r.git"),
        ("https://h/o/r.git#SECRET", "https://h/o/r.git"),
        ("git@h:o/r.git", "git@h:o/r.git"),
        ("  https://h/o/r.git?t=1  ", "https://h/o/r.git"),
        ("", ""),
    ],
)
def test_remote_url_locator_keeps_only_the_locator(url: str, locator: str) -> None:
    assert runtime.remote_url_locator(url) == locator


def test_no_derivation_carries_a_remote_url_query(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every derivation cuts the query, because each anchors on ``$``.

    A retained ``?access_token=...`` sits between a trailing ``.git`` and the end of
    the string, so the suffix a pattern means to strip survives AND the token rides
    into the result: one becomes an issue-link ``href``, the others an ``owner/repo``
    handed to ``gh --repo`` in child argv.
    """
    secret = "access_token=SECRET"
    url = f"https://h/o/r.git?{secret}"

    base = fleet_state._parse_html_repo_base(url)
    assert base == "https://h/o/r", "the href must not carry the token"
    assert secret not in (base or "")

    identity = repository._normalize_repo_identity(url)
    assert identity == ("h", "o/r"), "the identity must not carry the token"
    assert secret not in "".join(identity or ())

    # And the same repository written WITHOUT a query derives the same values, which
    # is the property a surviving query breaks: two spellings of one repo compared
    # as two, and a cache keyed on the difference.
    assert fleet_state._parse_html_repo_base("https://h/o/r.git") == base
    assert repository._normalize_repo_identity("https://h/o/r.git") == identity


# ---------------------------------------------------------------------------
# An unknown exit status is a failure, not a success.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_unknown_exit_status_is_reported_as_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``returncode`` is None when the child's status has not been reaped yet.

    ``or 0`` mapped that to 0, and 0 is what every caller here reads as "that
    worked": a row would report a clean tree, and a mutation's caller would go on to
    the next step, on the strength of an exit status nobody ever saw.
    """

    class _Proc:
        pid = 4321
        returncode = None

        async def communicate(self):
            return b"out", b""

    def _prepare(cmd, _mode, env=None, **_kw):
        return list(cmd), dict(env or {}), None

    monkeypatch.setattr(runtime, "sandboxed_spawn_argv", _prepare)
    monkeypatch.setattr(runtime, "_trusted_bin", lambda _name: "/usr/bin/git")
    monkeypatch.setattr(runtime, "_GIT_TRUSTED_HELPERS", {})

    async def _off_loop(fn, executor=None):
        return fn()

    monkeypatch.setattr(runtime, "shielded_prepare_off_loop", _off_loop)

    async def _spawn(*_a, **_kw):
        return _Proc()

    monkeypatch.setattr(runtime.asyncio, "create_subprocess_exec", _spawn)

    rc, stdout, _stderr = await runtime._run_cmd(["git", "-C", "/r", "status"])
    assert rc == -1, "an unreaped child must not be reported as success"
    assert stdout == "out"
