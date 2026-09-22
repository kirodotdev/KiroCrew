"""Tests for the gateway-owned push verdict store and its trusted activation.

The properties under test are the ones the gate's acceptance names, and two of them are
explicit requirements: an agent-written config can neither enable nor disable the gate, and
a trusted activation survives a restart.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew.security import push_verdict


@pytest.fixture(autouse=True)
def _clean_store() -> None:
    """Each test starts with an empty store.

    The store is module state in the gateway process, so a leaked verdict would make a
    later test pass for the wrong reason.
    """
    push_verdict._VERDICTS.clear()
    yield
    push_verdict._VERDICTS.clear()


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    return tmp_path


def _activate(home: Path, *, enabled: bool = True) -> Path:
    leaf = home / push_verdict.ACTIVATION_LEAF
    leaf.parent.mkdir(parents=True, exist_ok=True)
    leaf.write_text(json.dumps({"enabled": enabled}), encoding="utf-8")
    return leaf


def _record(session_key: str, **over: str) -> None:
    """Record a verdict with every bound field supplied.

    Every field is required with no default in the store on purpose, so a test that wants
    one value changed says so here rather than each call restating all six.
    """
    fields: dict[str, str] = {
        "gitdir": "/g",
        "worktree": "/some/worktree",
        "head": "a" * 40,
        "base": "main",
        "base_sha": "b" * 40,
        "remote": "origin",
        "source_ref": "feature-x",
    }
    fields.update(over)
    push_verdict.record(session_key, **fields)


# ── The store ──


def test_a_session_with_no_verdict_reads_none() -> None:
    assert push_verdict.verdict_for("session-1") is None


def test_a_recorded_verdict_is_readable_by_its_own_session_only() -> None:
    _record("session-1")
    mine = push_verdict.verdict_for("session-1")
    assert mine is not None and mine.head == "a" * 40
    # The key is the CALLING SESSION, so a second session inherits nothing. This is the
    # property that makes "ask about one worktree, publish from another" impossible.
    assert push_verdict.verdict_for("session-2") is None


def test_an_empty_session_key_can_neither_record_nor_read() -> None:
    with pytest.raises(ValueError):
        _record("")
    assert push_verdict.verdict_for("") is None


def test_a_verdict_older_than_the_ceiling_is_not_returned(monkeypatch: pytest.MonkeyPatch) -> None:
    _record("session-1")
    later = push_verdict._VERDICTS["session-1"].recorded_at + push_verdict.MAX_AGE_SECONDS + 1
    monkeypatch.setattr(push_verdict.time, "time", lambda: later)
    assert push_verdict.verdict_for("session-1") is None
    # Expired on read, not merely hidden: a later reader cannot see it either.
    assert "session-1" not in push_verdict._VERDICTS


# ── Observed mutation, which is how the gate avoids reading the tree ──


@pytest.mark.parametrize(
    "command",
    [
        "git commit --amend --no-edit",
        "git rebase origin/main",
        "git reset --soft HEAD~1",
        "git cherry-pick abc1234",
        "git fetch origin",
    ],
)
def test_an_authorized_mutating_command_drops_the_verdict(command: str) -> None:
    _record("session-1")
    push_verdict.invalidate_on("session-1", command)
    assert push_verdict.verdict_for("session-1") is None


@pytest.mark.parametrize(
    "command",
    ["git status", "git log --oneline -5", "ls -la", "git diff --stat"],
)
def test_a_read_only_command_leaves_the_verdict_alone(command: str) -> None:
    _record("session-1")
    push_verdict.invalidate_on("session-1", command)
    assert push_verdict.verdict_for("session-1") is not None


def test_a_command_that_both_mutates_and_publishes_is_flagged() -> None:
    """The compound-command gap: a pre-execution gate cannot judge post-mutation state."""
    fused = "git commit --amend --no-edit && git " + "push origin HEAD"
    assert push_verdict.mutates_head(fused) is True


def test_a_publish_on_its_own_is_not_flagged_as_mutating() -> None:
    assert push_verdict.mutates_head("git " + "push origin HEAD") is False


# ── Trusted activation, the conductor's two required properties ──


def test_activation_is_off_on_an_installation_nobody_activated(home: Path) -> None:
    assert push_verdict.activation_enabled() is False


def test_activation_is_on_when_the_keystone_leaf_says_so(home: Path) -> None:
    _activate(home)
    assert push_verdict.activation_enabled() is True


def test_an_agent_written_config_can_neither_enable_nor_disable_the_gate(home: Path) -> None:
    """config.json may REQUEST activation; only the keystone AUTHORIZES it."""
    config = home / "config.json"
    config.write_text(
        json.dumps({"security": {"push_verdict_required": True, "push_verdict_enabled": True}}),
        encoding="utf-8",
    )
    # Config alone does not enable it.
    assert push_verdict.activation_enabled() is False

    # And config cannot switch OFF what the keystone turned on.
    _activate(home)
    config.write_text(
        json.dumps({"security": {"push_verdict_required": False, "push_verdict_enabled": False}}),
        encoding="utf-8",
    )
    assert push_verdict.activation_enabled() is True


def test_trusted_activation_survives_a_restart(home: Path) -> None:
    """Activation is on disk, so a fresh process still sees it.

    The verdicts are deliberately the opposite: a restart clears them, which costs one
    guard re-run and never leaves a stale pass behind. Both halves are asserted here
    because the pair is the design, and a change that persisted verdicts would pass the
    first assertion alone.
    """
    _activate(home)
    _record("session-1")

    # A restart is a fresh module state: the store is process memory, the leaf is not.
    push_verdict._VERDICTS.clear()

    assert push_verdict.activation_enabled() is True
    assert push_verdict.verdict_for("session-1") is None


def test_a_malformed_activation_leaf_refuses_instead_of_failing_open(home: Path) -> None:
    """An activation leaf that cannot be PARSED must not read as "gating off".

    Absence and unreadability are different facts. Absence means nobody activated gating,
    and false is the honest answer. A leaf that EXISTS but is malformed means the operator's
    activation state is unknown, and answering false there would make corrupting one file a
    way to disable the gate on an installation that had turned it on -- the gate's own off
    switch, reachable by damage rather than by authorization.

    So the store raises and the floor refuses the publish rather than allowing it. An
    installation that never activated never reaches this branch: absence still returns
    false rather than raising, which the neighbouring test pins.
    """
    leaf = home / push_verdict.ACTIVATION_LEAF
    leaf.parent.mkdir(parents=True, exist_ok=True)
    leaf.write_text("{not json", encoding="utf-8")
    with pytest.raises(push_verdict.ActivationUnreadable):
        push_verdict.activation_enabled()


def test_a_non_object_activation_leaf_refuses_through_the_wrapper_too(home: Path) -> None:
    """The wrapper must not swallow what the reader raises.

    ``activation_enabled()`` is what the floor calls, so a refusal only ``activation()`` raised
    would be a fail-open at the one call site that matters. A suite that agrees with a hole
    cannot find it: while a test asserted ``[true]`` left the gate off, no mutation could be
    killed by the distinction, because both the code and the test read it the same way.
    """
    leaf = home / push_verdict.ACTIVATION_LEAF
    leaf.parent.mkdir(parents=True, exist_ok=True)
    leaf.write_text("[true]", encoding="utf-8")
    with pytest.raises(push_verdict.ActivationUnreadable):
        push_verdict.activation_enabled()


# ── The floor, where the refusal actually reaches a push ──


def test_a_fused_mutation_and_publish_is_refused_by_the_floor(home: Path) -> None:
    """The compound-command gap, at the real entry point rather than the predicate.

    Built by concatenation because a literal publish string in this file's source is not
    the point of the test, and the product's own floor matches publish text structurally.
    """
    from kiro_crew import security

    _activate(home)
    fused = "git commit --amend --no-edit && git " + "push origin feature-x"
    reason = security.is_denied(fused, [])
    assert reason, "a command that both amends and publishes must be refused"
    assert "commit" in reason


def test_an_installation_that_never_activated_is_unaffected(home: Path) -> None:
    """The blast-radius guard: no activation, no new refusal.

    Without this the change would refuse ``git commit && git push`` on every install on
    the next release, which is a behaviour change nobody asked for.
    """
    from kiro_crew import security

    fused = "git commit --amend --no-edit && git " + "push origin feature-x"
    assert security.is_denied(fused, []) is None


def test_an_ordinary_publish_is_allowed_when_this_session_has_a_verdict(home: Path) -> None:
    """The floor must still allow the publishes it allowed before, given a real verdict."""
    from kiro_crew import security

    _activate(home)
    _record("session-1")
    plain = "git " + "push origin feature-x"
    assert security.is_denied(plain, [], session_key="session-1") is None


def test_an_activated_install_denies_a_publish_with_no_verdict(home: Path) -> None:
    """The omission this issue exists to catch, at the real entry point."""
    from kiro_crew import security

    _activate(home)
    plain = "git " + "push origin feature-x"
    reason = security.is_denied(plain, [], session_key="session-1")
    assert reason and push_verdict.GATE_TOOL in reason


def test_one_session_cannot_publish_on_another_sessions_verdict(home: Path) -> None:
    """The session keying, proven where it matters rather than only on the store."""
    from kiro_crew import security

    _activate(home)
    _record("session-1")
    plain = "git " + "push origin feature-x"
    assert security.is_denied(plain, [], session_key="session-2") is not None


def test_a_publish_fused_with_a_rebase_is_refused_and_names_the_verb(home: Path) -> None:
    from kiro_crew import security

    _activate(home)
    fused = "git rebase origin/main && git " + "push --force-with-lease origin feature-x"
    reason = security.is_denied(fused, [])
    assert reason and "rebase" in reason


# ── The route's auth classification, which is what keeps the writer trustworthy ──


def test_the_route_is_registered_and_classified_strict() -> None:
    """Registered by the shared registrar AND strict, asserted together.

    Together because either one alone is a false reassurance: a path in the frozenset that
    no server registers is dead, and a registered path missing from the frozenset falls
    through to cookie auth, which would let a browser request stand in for the agent's
    session. Registering through ``_register_mcp_routes`` is also what stops the two
    servers from drifting, which that frozenset's own comment calls an auth bypass.
    """
    from aiohttp import web

    from kiro_crew.dashboard import server

    app = web.Application()
    server._register_mcp_routes(app)
    registered = {str(route.resource.canonical) for route in app.router.routes()}
    assert "/api/push-verdict/run" in registered
    assert server.internal_path_matches("/api/push-verdict/run", server._STRICT_INTERNAL_API_PATHS)


def test_the_handler_reasserts_loopback_and_the_internal_secret() -> None:
    """Both re-asserts must be present in the handler itself.

    Being listed in the strict frozenset does not prove the secret was checked: with the
    header absent the middleware falls through to cookie auth, and a ``local_only=False``
    deployment reclassifies strict paths as mixed. This asserts the source carries both
    guards, which is the cheapest way to catch their removal.
    """
    from pathlib import Path

    package = Path(push_verdict.__file__).parent.parent
    handler = (package / "dashboard" / "handlers" / "push_verdict.py").read_text(encoding="utf-8")
    assert "is_loopback(request.remote" in handler
    assert 'request.get("internal_auth") is not True' in handler


def test_a_caller_with_no_session_identity_cannot_borrow_a_verdict(home: Path) -> None:
    """An unidentified publish denies even while some other session holds a verdict.

    This is the shape a mutation found surviving: a floor that fell back to "any recorded
    verdict" when the session key was empty would let a caller with no identity publish on
    someone else's pass. The empty key must be a denial, not a wildcard.
    """
    from kiro_crew import security

    _activate(home)
    _record("session-1")
    plain = "git " + "push origin feature-x"
    assert security.is_denied(plain, []) is not None


# ── The route runs the REAL guard, which is what makes the verdict mean what it says ──


def _route_source() -> str:
    from pathlib import Path

    package = Path(push_verdict.__file__).parent.parent
    return (package / "dashboard" / "handlers" / "push_verdict.py").read_text(encoding="utf-8")


def test_the_route_runs_the_packaged_guard_and_it_exists() -> None:
    """The guard the route invokes must actually be there.

    A relocated or renamed script would otherwise make every request an environment error,
    which denies every publish on an activated install -- silently, since no other test
    resolves this path.
    """
    from kiro_crew.dashboard.handlers import push_verdict as route

    assert route._PUSH_GUARD.is_file()
    assert route._PUSH_GUARD.name == "push_guard.py"


def test_the_route_runs_the_packaged_copy_not_the_agent_writable_one() -> None:
    """The installed skills copy is agent-writable; the packaged one is not.

    Running the installed copy would hand the gated party the very check that authorizes
    it, which is the defect this whole design exists to remove, one layer further out.
    """
    from pathlib import Path

    import kiro_crew
    from kiro_crew.dashboard.handlers import push_verdict as route

    assert route._PUSH_GUARD.is_relative_to(Path(kiro_crew.__file__).parent / "builtin_skills")


def test_the_route_pins_the_base_rather_than_letting_the_guard_detect_it() -> None:
    """The recorded base must be the base that was judged, so the gateway names it."""
    assert '"--base"' in _route_source()


def test_the_route_reads_the_guards_own_exit_contract() -> None:
    """A cross-file contract nothing else pins.

    The route decides whether to record from the guard's exit code. If the guard's contract
    changed, the route would misread a refusal as a pass with no test failing anywhere, so
    this asserts the numbers against the guard script's own documented contract.
    """
    from kiro_crew.dashboard.handlers import push_verdict as route

    contract = route._PUSH_GUARD.read_text(encoding="utf-8")
    assert "0 SAFE" in contract and "40 REFUSED" in contract
    assert route._GUARD_SAFE == 0
    assert route._GUARD_REFUSED == 40


def test_the_route_records_only_after_the_guard_verdict_is_checked() -> None:
    """Ordering, not presence: recording before the check would record on any exit."""
    source = _route_source()
    check = source.index("run.rc != _GUARD_SAFE")
    recorded = source.index("push_verdict.record(")
    assert check < recorded


def test_the_route_does_not_take_the_base_from_the_branchs_upstream() -> None:
    """Staleness measured against a branch's own remote branch passes by construction.

    The base has to be the repository's default branch. This pins the resolution against a
    regression that looks harmless and makes the check unable to fail.
    """
    source = _route_source()
    assert "refs/remotes/origin/HEAD" in source
    assert "HEAD@{upstream}" not in source


# ── The protection that makes activation trusted, and the invalidation that makes it live ──
#
# Both properties were asserted in prose in an earlier revision and enforced by nothing, and
# three reviewers blocked on exactly that. These tests drive the REAL tables and the REAL
# hook, because a test that calls the helper directly proves the helper works and proves
# nothing about whether anything calls it.


def test_the_activation_leaf_is_write_protected_on_the_tool_path(home: Path) -> None:
    """An agent file-write to the activation leaf is refused.

    This is the property the whole design rests on: if the gated party can write this file it
    switches its own gate off, and the gate is worth nothing.
    """
    from kiro_crew.security import paths as security_paths

    assert security_paths.is_sensitive_path(str(home / push_verdict.ACTIVATION_LEAF))


def test_the_activation_leaf_is_sealed_in_every_disposition() -> None:
    """The keystone list plus all three sandbox dispositions, named one by one.

    Named individually rather than looped so that a table dropped from the set fails by the
    name of the table, which is what a reader of the failure needs.
    """
    from kiro_crew import sandbox
    from kiro_crew.security import paths as security_paths

    leaf = push_verdict.ACTIVATION_LEAF
    assert leaf in security_paths._CREW_SECRET_LEAVES
    assert leaf in sandbox._CREW_READONLY_LEAVES
    assert leaf in sandbox._CREW_CHILD_WITHHELD_LEAVES
    assert leaf in sandbox._CREW_PRECREATE_READONLY_FILE_LEAVES

    # The mirror holds the commits the gateway judges. An agent that can write it plants the
    # base its own push is measured against, so it is sealed too -- but in the DIRECTORY
    # dispositions only. `_CREW_PRECREATE_READONLY_FILE_LEAVES` pre-creates each entry as an
    # empty FILE, so listing a directory there materializes a file at the mirror's path and the
    # first `mkdir` raises, which would disable the feature outright rather than protect it.
    mirror = push_verdict.MIRROR_DIR
    assert mirror in security_paths._CREW_SECRET_LEAVES
    assert mirror in sandbox._CREW_READONLY_LEAVES
    assert mirror in sandbox._CREW_CHILD_WITHHELD_LEAVES
    assert mirror not in sandbox._CREW_PRECREATE_READONLY_FILE_LEAVES


def test_activation_requires_a_real_json_true(home: Path) -> None:
    """A truthy value is not an enable, and it is not a silent disable either.

    The JSON string ``"false"`` and the number ``1`` are both truthy in Python, so a
    ``bool(...)`` read would activate the gate on either -- that half has always held. The
    other half was wrong: reading them as OFF made a corrupted leaf disable the gate on an
    installation whose operator had enabled it, so a present non-boolean now REFUSES. Only
    two shapes are an operator's: a real ``true`` and a real ``false``. An explicit ``null``
    is indistinguishable from an absent key through ``.get()``, so it keeps the absent
    reading, which is off.
    """
    leaf = _activate(home)
    for corrupted in ("false", 1, "true"):
        leaf.write_text(json.dumps({"enabled": corrupted}), encoding="utf-8")
        with pytest.raises(push_verdict.ActivationUnreadable):
            push_verdict.activation_enabled()
    leaf.write_text(json.dumps({"enabled": None}), encoding="utf-8")
    assert push_verdict.activation_enabled() is False
    leaf.write_text(json.dumps({"enabled": False}), encoding="utf-8")
    assert push_verdict.activation_enabled() is False
    leaf.write_text(json.dumps({"enabled": True}), encoding="utf-8")
    assert push_verdict.activation_enabled() is True


def test_an_observed_mutating_command_drops_the_sessions_verdict(home: Path) -> None:
    """Invalidation, driven through the real hook rather than by calling the helper."""
    _activate(home)
    _record("session-1")
    assert push_verdict.verdict_for("session-1") is not None

    _hook_verdict("git commit --amend --no-edit", session_key="session-1")
    assert push_verdict.verdict_for("session-1") is None


def test_an_observed_read_only_command_keeps_the_verdict(home: Path) -> None:
    """The companion: invalidation is about MUTATION, not about any command at all.

    Without this the test above would also pass if the hook dropped every verdict on every
    command, which would make the gate demand a guard run between any two commands.
    """
    _activate(home)
    _record("session-1")
    _hook_verdict("git status --porcelain", session_key="session-1")
    assert push_verdict.verdict_for("session-1") is not None


# ── A verdict describes ONE tree, so a redirected publish is refused ──


def test_a_publish_redirected_at_another_repository_is_refused(home: Path) -> None:
    """``git -C other push`` must not pass on this session's verdict for its own tree.

    The floor cannot resolve which tree a command runs in, so the honest answer to a
    redirected publish is a refusal rather than a judgement against the wrong repository.
    """
    from kiro_crew import security

    _activate(home)
    _record("session-1")
    redirected = "git -C /other/repo " + "push origin feature-x"
    assert security._is_git_publish(redirected), "the publish detector missed the redirected form"
    assert push_verdict.redirects_repository(redirected) == "-C"
    reason = security.is_denied(redirected, [], session_key="session-1")
    assert reason and "-C" in reason


def test_a_publish_fused_with_a_directory_change_is_refused(home: Path) -> None:
    """The same redirection by another spelling, which ``-C`` matching alone would miss."""
    from kiro_crew import security

    _activate(home)
    _record("session-1")
    moved = "cd /other/repo && git " + "push origin feature-x"
    reason = security.is_denied(moved, [], session_key="session-1")
    assert reason and "cd" in reason


def test_a_config_option_is_not_read_as_a_redirection(home: Path) -> None:
    """``git -c k=v`` sets configuration; ``git -C dir`` redirects. They differ by CASE only.

    A lowercasing predicate collapses the two and refuses an ordinary configured publish,
    which is a false refusal on a global floor. This is the test that caught exactly that.
    """
    from kiro_crew import security

    _activate(home)
    _record("session-1")
    configured = "git -c user.name=x " + "push origin feature-x"
    assert security.is_denied(configured, [], session_key="session-1") is None


def test_a_branch_named_like_a_directory_verb_is_not_a_redirection(home: Path) -> None:
    """``cd`` counts in command position only, so a branch of that name still publishes.

    Without this the predicate would refuse an ordinary publish whose ref happens to be
    spelled like a shell verb, which is a false refusal on a global floor.
    """
    from kiro_crew import security

    _activate(home)
    _record("session-1", source_ref="cd")
    plain = "git " + "push origin cd"
    assert security.is_denied(plain, [], session_key="session-1") is None


def test_an_unactivated_install_allows_a_redirected_publish(home: Path) -> None:
    """Blast radius again: the redirection refusal is behind activation like the rest."""
    from kiro_crew import security

    redirected = "git -C /other/repo " + "push origin feature-x"
    assert security.is_denied(redirected, []) is None


# ── The presenter renders what the route actually answers ──


def _render(payload: dict, monkeypatch: pytest.MonkeyPatch) -> str:
    """Drive the real presenter against one route response."""
    from kiro_crew import mcp_core
    from kiro_crew.mcp_tools import push_verdict as tool

    monkeypatch.setattr(mcp_core, "require_strict_session_key", lambda _msg: ("session-1", ""))
    monkeypatch.setattr(mcp_core, "_post", lambda *_a, **_k: payload)
    return tool.push_verdict_run("push_verdict_run", {})


def test_the_presenter_renders_each_exit_the_route_can_return(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All three route exits, because a name the route never sends is a silent mistranslation.

    An earlier revision matched ``"stale"`` and read ``base_ref``; the route answers
    ``"refused"``/``"ok"``/``"error"`` and sends ``base``, so a genuine refusal rendered as
    "unrecognised verdict" to the one reader who had to act on it.
    """
    ok = _render({"verdict": "ok", "head": "a" * 40, "base": "main"}, monkeypatch)
    assert "head=" + "a" * 12 in ok and "base=main" in ok

    refused = _render({"verdict": "refused", "base": "main", "detail": "stale base"}, monkeypatch)
    assert "REFUSED" in refused and "main" in refused and "stale base" in refused

    errored = _render({"verdict": "error", "base": "main", "detail": "guard exploded"}, monkeypatch)
    assert "could not complete" in errored and "guard exploded" in errored


def test_the_presenter_refuses_to_read_an_unknown_verdict_as_a_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A verdict the presenter does not know is an error, never a pass."""
    out = _render({"verdict": "probably-fine"}, monkeypatch)
    assert "unrecognised verdict" in out


# ── The gateway runs git inside an AGENT-WRITABLE worktree, so executable config is neutered ──


def test_executable_git_configuration_is_neutralized_for_the_gateways_git() -> None:
    """The worktree is agent-writable and this git runs at gateway privilege.

    Without this, an agent writing ``core.sshCommand`` into its own ``.git/config`` has the
    gateway run that command for it, outside the agent's sandbox, just by asking for a
    verdict. Environment configuration rather than ``-c`` because it must also govern the git
    processes the guard script starts for itself.
    """
    from kiro_crew.dashboard.handlers import push_verdict as route

    env = route.git_env()
    count = int(env["GIT_CONFIG_COUNT"])
    keys = {env[f"GIT_CONFIG_KEY_{index}"] for index in range(count)}
    for executable_directive in (
        "core.sshCommand",
        "core.fsmonitor",
        "core.gitProxy",
        "core.askPass",
        "core.hooksPath",
        "core.alternateRefsCommand",
        "credential.helper",
        "diff.external",
        "uploadpack.packObjectsHook",
    ):
        assert executable_directive in keys
    assert env["GIT_TERMINAL_PROMPT"] == "0"


def test_every_git_the_gateway_launches_uses_that_environment() -> None:
    """One spawn site, and the neutralized environment is its DEFAULT.

    Asserted as the default rather than per call site: a caller that forgets to pass an
    environment gets the neutralized one, so a new git call cannot arrive unprotected by
    omission. The guard run overrides it only to add ``GIT_DIR``, on top of the same base.
    """
    source = _route_source()
    assert "env=env or git_env()" in source
    assert "env=scrubbed" in source
    assert 'env["GIT_DIR"]' in source


def test_the_gateways_spawns_stay_routed_through_the_sandbox_chokepoint() -> None:
    """Both spawns go through the chokepoint, with limits, and no bare spawn remains.

    `test/test_spawn_audit.py` is the repository-wide tripwire for this. This one fails in the
    file that owns the behaviour, so a reader who breaks it sees WHY here: the command runs
    against an agent-writable repository, and git reads executable directives out of that
    repository's own configuration, so the child has to be confined even when it does something
    the agent chose.
    """
    import ast

    source = _route_source()
    tree = ast.parse(source)
    adapter = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_prepare_sandboxed_spawn"
    )
    # Read the SYNTAX, not the text: the import line and this adapter's own docstring both
    # carry these names, so counting occurrences is satisfied while the real call is replaced
    # by something that confines nothing. A mutation doing exactly that survived a text check.
    # `test/test_spawn_audit.py` reads the tree for the same reason.
    called = {
        node.func.id
        for node in ast.walk(adapter)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    referenced = {node.id for node in ast.walk(adapter) if isinstance(node, ast.Name)}
    assert "shielded_prepare_off_loop" in called
    assert "sandboxed_spawn_argv" in referenced
    # Exactly ONE spawn site in the module, which is the point rather than an accident: the
    # repository's spawn audit reads the enclosing function, so every additional site is
    # another place the routing can be forgotten. Every git this module runs goes through it.
    assert source.count("create_subprocess_limited(") == 1
    assert "asyncio.create_subprocess_exec" not in source
    # The mode is the difference between a confined child and an unconfined one, and "off" is
    # a one-word edit away from here. Routing through the chokepoint in "off" mode would
    # satisfy the repository's spawn audit while confining nothing.
    assert 'mode="standard"' in source
    assert 'mode="off"' not in source


def test_the_mirror_lives_inside_the_sealed_directory(home: Path) -> None:
    """The mirror's PATH is what the seal protects, so it must land inside it.

    A mirror placed anywhere else is unprotected, and an unprotected mirror is one the agent can
    plant its own base commit into. The seal and the path have to agree or the seal guards an
    empty directory.
    """
    from kiro_crew.dashboard.handlers import push_verdict as route

    mirror = route._mirror_for("/some/repo/.git")
    assert mirror.parent == home / push_verdict.MIRROR_DIR
    assert mirror.suffix == ".git"
    # Keyed on the git directory: two repositories never collide, one repository is stable.
    assert route._mirror_for("/other/repo/.git") != mirror
    assert route._mirror_for("/some/repo/.git") == mirror


def test_the_guard_is_invoked_in_out_of_place_mode() -> None:
    """The three flags that make the guard judge the mirror instead of the worktree.

    Without them the guard would judge `HEAD` and `origin/<base>` in a bare mirror that has
    neither, so the run would fail rather than judge the wrong thing -- but it would fail for a
    reason no test explains. These assert the mode the whole read-only design depends on.
    """
    source = _route_source()
    assert '"--no-fetch",' in source
    assert '"--base-ref",' in source
    assert '"--candidate-ref",' in source
    assert 'env["GIT_DIR"] = str(mirror)' in source


def test_the_candidate_is_read_out_of_the_worktree_never_into_it(home: Path) -> None:
    """The fetch DIRECTION is the property that keeps the worktree unwritten.

    A fetch into the worktree fails on its own `FETCH_HEAD` once that tree is read-only, and
    before it fails it would be writing to the state being judged. So the candidate is fetched
    out of the worktree into the mirror, and the only `-C <worktree>` uses are reads.
    """
    source = _route_source()
    assert 'f"+HEAD:{refs.candidate}"' in source
    # Every worktree-scoped git call is a read: no fetch, no write verb, carries -C.
    assert '"git", "-C", worktree, *args' in source
    assert '"-C", worktree, "fetch"' not in source


def test_the_refusals_name_the_entry_point() -> None:
    assert push_verdict.GATE_TOOL in push_verdict.absent_detail()
    assert push_verdict.GATE_TOOL in push_verdict.mutation_detail("commit")
    assert "commit" in push_verdict.mutation_detail("commit")


# ── The calling session actually reaching the floor, end to end ──
#
# Two mutations survived a green 32-test file by replacing the forwarded session key with
# ``""`` -- once in ``PolicyAuthority.is_denied`` and once at the hook's call site. Neither
# was caught, because every other test in this file calls ``security.is_denied`` directly
# and so skips both hops. That gap matters more than a missing verdict would: with an empty
# key arriving at the floor, an ACTIVATED installation denies every publish by every
# session, so the feature ships as a total block rather than as a gate.
#
# These two tests drive the real ``HookManager`` and the real authority, so they fail if
# either hop drops the key. They are deliberately a pair: the first alone would also pass
# if the floor ignored the key entirely, and the second is what proves the key SELECTS.


def _hook_verdict(command: str, *, session_key: str) -> "str | None":
    """The hook's refusal reason for *command*, or ``None`` when it was allowed.

    Reads ``action`` rather than any attribute named ``deny``: ``ToolHookResult.deny`` is a
    CONSTRUCTOR, so an attribute check on that name is truthy for an allowed result too and
    would make this helper report every outcome as a denial.
    """
    from kiro_crew.hooks import TOOL_DENY, HookManager, HooksConfig

    result = HookManager(HooksConfig()).on_tool_call(
        command, command=command, is_shell=True, session_key=session_key
    )
    return result.reason if result.action == TOOL_DENY else None


def test_the_calling_sessions_own_verdict_reaches_the_floor_through_the_hook(
    home: Path,
) -> None:
    """A publish is allowed when the session driving the hook holds the verdict."""
    _activate(home)
    _record("session-1")
    assert _hook_verdict("git " + "push origin feature-x", session_key="session-1") is None


def test_the_hook_does_not_hand_one_session_another_sessions_verdict(home: Path) -> None:
    """And it is the key that selects: a different session is refused on the same store."""
    _activate(home)
    _record("session-1")
    reason = _hook_verdict("git " + "push origin feature-x", session_key="session-2")
    assert reason and push_verdict.GATE_TOOL in reason


# ── The audit record and the effect as one transaction, driven through the real handler ──


def _drive_route(
    monkeypatch: pytest.MonkeyPatch,
    *,
    audit_raises: bool,
    activated: bool = True,
    config_answers: "dict[str, str] | None" = None,
    push_url: str = "https://example.invalid/repo.git",
    publish_result: object = None,
    capture: "list | None" = None,
    deletions: "list | None" = None,
):
    """Call the real route handler with its I/O stubbed, returning ``(status, payload)``.

    Everything stubbed here is I/O the handler does not own: the loopback check, session
    recognition, the slot lookup, git reads, and the guard subprocess. What is NOT stubbed
    is the ordering under test.
    """
    import asyncio
    import json as _json

    from aiohttp import web
    from aiohttp.test_utils import make_mocked_request

    from kiro_crew.dashboard.handlers import push_verdict as route

    class _Slot:
        project = "/some/worktree"

    class _State:
        def get_slot(self, _key: str) -> "_Slot":
            return _Slot()

    def _fake_sel():
        class _Sel:
            def log_api_access(self, **kwargs: object) -> None:
                # Mirrors the REAL helper rather than being a convenient stub. Without
                # `critical=True` that helper ENQUEUES the event and returns success even
                # when the write fails; only a critical write is synchronous and re-raises.
                # A stub that raised either way would hold no one to `critical=True` -- drop
                # the keyword and a sink that is down would look like a clean audit, so the
                # unaudited verdict this file exists to forbid would be recorded with every
                # test still green.
                if audit_raises and kwargs.get("critical") is True:
                    raise OSError("audit sink is down")

        return _Sel()

    async def _fake_git(_worktree: str, *args: str) -> tuple[int, str]:
        if "--absolute-git-dir" in args:
            return 0, "/some/worktree/.git"
        for arg in args:
            if arg.startswith("refs/remotes/") and arg.endswith("/HEAD"):
                # Any remote's default branch, so a test can move the effective push remote.
                return 0, arg[len("refs/remotes/") : -len("/HEAD")] + "/main"
        if "symbolic-ref" in args:
            return 0, "feature-x"
        if args[:2] == ("config", "--get"):
            answers = config_answers or {}
            key = args[2] if len(args) > 2 else ""
            return (0, answers[key]) if key in answers else (1, "")
        if args[:2] == ("remote", "get-url"):
            # Fetch and push URL agree unless a test says otherwise.
            return 0, push_url if "--push" in args else "https://example.invalid/repo.git"
        return 0, "a" * 40

    async def _fake_guard(
        _worktree: str, _base: str, *, gitdir: str = "", digest: str = "", url: str = ""
    ) -> "route._GuardRun":
        # The mirror and refs ride along because the operation PUBLISHES from them: a pass with
        # no mirror is refused rather than published, so a stub that omitted them would be
        # testing the refusal instead of the ordering.
        return route._GuardRun(
            0,
            "STATUS: SAFE TO PUSH",
            "d" * 40,
            "e" * 40,
            Path("/some/mirror"),
            route._JudgementRefs.mint(),
        )

    async def _fake_publish(**_kwargs: object) -> "route._PublishResult":
        # The receipt exists only INSIDE the operation now, so this is the one place a test can
        # see it. Captured here rather than asserted after the call, because after the call it
        # is gone by design -- which is itself what the callers assert.
        if capture is not None:
            capture.append(route.push_verdict.verdict_for("session-1"))
        return publish_result or route._PublishResult(True, "published", "pushed")

    async def _fake_delete_refs(*args: object, **_kwargs: object) -> None:
        if deletions is not None:
            deletions.append(args)
        return None

    async def _fake_recognize(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(route, "is_loopback", lambda _remote: True)
    monkeypatch.setattr(route, "_recognize_session", _fake_recognize)
    monkeypatch.setattr(route, "_git", _fake_git)
    monkeypatch.setattr(route, "_run_guard", _fake_guard)
    monkeypatch.setattr(route, "_publish", _fake_publish)
    monkeypatch.setattr(route, "_delete_refs", _fake_delete_refs)
    monkeypatch.setattr(route, "sel", _fake_sel)
    # An activated installation with a digest pinned. The keystone's own behaviour has its
    # own tests; what these drive is the route's ordering, which never runs without one.
    monkeypatch.setattr(
        route.push_verdict,
        "activation",
        lambda: route.push_verdict.Activation(enabled=activated, guard_sha256="c" * 64),
    )

    app = web.Application()
    app["state"] = _State()
    request = make_mocked_request(
        "POST", "/api/push-verdict/run", headers={"X-Session-Key": "session-1"}, app=app
    )
    request["internal_auth"] = True

    response = asyncio.run(route.api_push_verdict_run(request))
    return response.status, _json.loads(response.text)


def test_a_verdict_that_could_not_be_audited_is_not_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The acceptance clause, as behaviour: no audit record, no recorded pass.

    A pass whose only trace failed to be written is an unaudited pass, which is the class
    this design was told to remove. The audit therefore goes FIRST and its failure refuses
    the request, so the store is left exactly as it was.
    """
    status, payload = _drive_route(monkeypatch, audit_raises=True)
    assert status == 500
    assert payload["code"] == "audit_failed"
    assert push_verdict.verdict_for("session-1") is None


def test_an_audited_guard_pass_publishes_and_leaves_no_reusable_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The happy path, and the two halves of it that matter.

    The receipt the operation binds carries what the GUARD resolved -- the fake worktree reader
    answers a different sha on purpose, because re-reading the worktree after the run was its own
    gap: a commit landing in between was recorded as judged while nothing had judged it.

    And it does not outlive the operation. A receipt that survived would be standing authority to
    publish again, which is the authority the agent must not hold: the only thing that can
    honestly spend it is this operation, which has already finished. This half is asserted from
    OUTSIDE while the first half is captured from inside, because after the call there is
    deliberately nothing left to read.
    """
    seen: list = []
    status, payload = _drive_route(monkeypatch, audit_raises=False, capture=seen)
    assert status == 200
    assert payload["verdict"] == "published"

    bound = seen[0]
    assert bound is not None, "the operation published without binding a receipt"
    assert bound.head == "d" * 40, "the bound head did not come from the guard's own run"
    assert bound.base_sha == "e" * 40
    assert bound.remote == "origin"
    assert bound.source_ref == "feature-x"
    assert bound.base == "main"

    assert push_verdict.verdict_for("session-1") is None, "the receipt outlived its operation"


@pytest.mark.parametrize(
    "document",
    [
        "[]",
        "5",
        '"true"',
        "null",
        '{"enabled": 1}',
        '{"enabled": "true"}',
        '{"enabled": "false"}',
    ],
)
def test_a_present_but_malformed_activation_refuses_rather_than_reading_off(
    home: Path, document: str
) -> None:
    """Off is the honest answer for an ABSENT leaf only.

    Unparseable bytes already raised. A document that PARSES but is not an object, or whose
    ``enabled`` is present and not a real boolean, was returning off -- a fail-open with a
    narrower entrance than the parse error: truncating the leaf to ``[]``, or writing
    ``{"enabled": 1}``, silently disabled the gate on an installation whose operator had
    turned it on. Note ``"false"`` is in here too: a STRING is a corrupted write whichever
    word it spells, and guessing the operator meant a disable is the same guess as guessing
    they meant an enable.
    """
    leaf = home / push_verdict.ACTIVATION_LEAF
    leaf.parent.mkdir(parents=True, exist_ok=True)
    leaf.write_text(document, encoding="utf-8")

    with pytest.raises(push_verdict.ActivationUnreadable):
        push_verdict.activation()


def test_the_two_shapes_an_operator_actually_writes_are_honoured(home: Path) -> None:
    """The other side of the refusal above: a real boolean is read, and absence is off."""
    leaf = home / push_verdict.ACTIVATION_LEAF
    leaf.parent.mkdir(parents=True, exist_ok=True)

    leaf.write_text('{"enabled": false}', encoding="utf-8")
    assert push_verdict.activation().enabled is False, "an operator's disable must be honoured"

    leaf.write_text('{"enabled": true, "guard_sha256": "' + "a" * 64 + '"}', encoding="utf-8")
    assert push_verdict.activation().enabled is True

    leaf.write_text("{}", encoding="utf-8")
    assert push_verdict.activation().enabled is False, "absent enabled is a never-activated leaf"


def test_an_unreadable_activation_refuses_the_publish_rather_than_allowing_it(
    home: Path,
) -> None:
    """Fail-closed, driven through the real hook.

    The store raising is only half the property; this is the half a caller sees. An
    activation leaf that cannot be parsed leaves the operator's intent UNKNOWN, and allowing
    the publish there would make damaging one file equivalent to turning the gate off -- an
    off switch reachable without authorization. So the publish is refused, and the refusal
    names the file an operator has to repair.
    """
    leaf = home / push_verdict.ACTIVATION_LEAF
    leaf.parent.mkdir(parents=True, exist_ok=True)
    leaf.write_text("{not json", encoding="utf-8")

    reason = _hook_verdict("git " + "push origin feature-x", session_key="session-1")
    assert reason is not None
    assert push_verdict.ACTIVATION_LEAF in reason


def test_the_guard_the_gateway_runs_is_verified_against_the_keystone_pin(
    home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Editing the packaged guard must not change what the gateway executes.

    In an editable or source install the packaged guard sits in the repository the agent
    edits, so executing it there would hand the gated party the script that authorizes it:
    print ``SAFE TO PUSH`` unconditionally and every push is recorded as checked.

    A copy taken on first use did not answer this, and that is why the pin exists. First use
    made TIMING the authority, so an edit landing before any verdict was ever requested became
    the authorized version permanently. The digest in the keystone is the authority instead,
    and it is the one statement about these bytes written outside the tree they live in.
    """
    import hashlib

    from kiro_crew.dashboard.handlers import push_verdict as route

    packaged = tmp_path / "push_guard.py"
    packaged.write_bytes(b"print('STATUS: SAFE TO PUSH')\n")
    monkeypatch.setattr(route, "_PUSH_GUARD", packaged)
    pinned = hashlib.sha256(packaged.read_bytes()).hexdigest()

    snapshot = route._guard_snapshot(pinned)
    # Inside the sealed directory, which is the whole reason the agent cannot reach it.
    assert push_verdict.MIRROR_DIR in snapshot.parts
    assert snapshot.read_bytes() == packaged.read_bytes()

    # The agent's edit: make the guard pass unconditionally. It does not match the pin, so
    # nothing runs at all -- not the edit, and not a stale copy passed off as authorized.
    packaged.write_bytes(b"import sys\nprint('STATUS: SAFE TO PUSH')\nsys.exit(0)\n")
    with pytest.raises(route._GuardUntrusted):
        route._guard_snapshot(pinned)

    # And an installation with nothing pinned runs no guard, which is what stops "activated"
    # from quietly meaning "runs whatever is in the tree".
    with pytest.raises(route._GuardUntrusted):
        route._guard_snapshot("")


def test_the_runner_executes_the_snapshot_and_not_the_packaged_path(
    home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The snapshot must be what the RUNNER launches, not merely what a helper can produce.

    Mutation testing is why this test exists: pointing the invocation back at the packaged
    path left every other test in this file green, because the one that covers the snapshot
    calls the helper directly and the route-level test stubs the runner out. Neither can see
    which path is actually handed to the spawn, so this one reads the argv.
    """
    import asyncio
    import hashlib

    from kiro_crew.dashboard.handlers import push_verdict as route

    packaged = tmp_path / "push_guard.py"
    packaged.write_text("print('STATUS: SAFE TO PUSH')\n", encoding="utf-8")
    monkeypatch.setattr(route, "_PUSH_GUARD", packaged)

    launched: list[list[str]] = []

    async def _fake_git(_worktree: str, *_args: str) -> tuple[int, str]:
        return 0, "https://example.invalid/repo.git"

    async def _fake_prime(*_args: object, **_kwargs: object) -> tuple[int, str]:
        return 0, ""

    async def _fake_run_git(argv: list[str], **_kwargs: object) -> tuple[int, str]:
        launched.append(list(argv))
        return 0, "STATUS: SAFE TO PUSH"

    monkeypatch.setattr(route, "_git", _fake_git)
    monkeypatch.setattr(route, "_prime_mirror", _fake_prime)
    monkeypatch.setattr(route, "_run_git", _fake_run_git)

    asyncio.run(
        route._run_guard(
            "/some/worktree",
            "main",
            gitdir="/some/worktree/.git",
            digest=hashlib.sha256(packaged.read_bytes()).hexdigest(),
            url="https://example.invalid/repo.git",
        )
    )

    assert launched, "the runner launched nothing"
    # The runner launches several commands -- it resolves the judged pair and cleans its refs
    # up -- so the guard is picked out by name rather than assumed to be the first.
    guard_argv = [argv for argv in launched if any(a.endswith("push_guard.py") for a in argv)]
    assert len(guard_argv) == 1, "the guard was not launched exactly once"
    argv = guard_argv[0]
    assert str(packaged) not in argv, "the runner executed the agent-writable packaged guard"
    guard_arg = next(a for a in argv if a.endswith("push_guard.py"))
    assert push_verdict.MIRROR_DIR in Path(guard_arg).parts


# ── Reading git's grammar at the position git reads it ──
#
# A verb matched anywhere in the line reads ARGUMENTS as verbs. Ordinary branch names carry
# these words, and a security review found the floor refusing legitimate publishes because of
# it while also invalidating verdicts on commands that changed nothing.


@pytest.mark.parametrize(
    "branch",
    ["revert-12643-fix-crash", "fix/reset-password-flow", "merge-queue-cleanup", "am-i-ready"],
)
def test_a_branch_name_that_contains_a_verb_is_not_a_mutation(branch: str) -> None:
    """The false-refusal half. These are ordinary branch names, not mutations."""
    assert push_verdict.git_mutating_subcommand("git " + f"push origin {branch}") == ""


def test_a_read_only_subverb_is_not_a_mutation() -> None:
    """``git stash list`` prints; ``git stash`` saves. The verb alone cannot tell them apart."""
    assert push_verdict.git_mutating_subcommand("git stash list") == ""
    assert push_verdict.git_mutating_subcommand("git stash --quiet show") == ""
    # The bare verb DOES save, so absence of a subverb is not a read.
    assert push_verdict.git_mutating_subcommand("git stash") == "stash"


def test_a_mutation_is_still_found_when_it_is_fused_or_prefixed() -> None:
    """The gap the position rule must not open: a real mutation still has to be seen."""
    assert push_verdict.git_mutating_subcommand("git reset --hard && git " + "push") == "reset"
    # Prefixed invocations are ordinary, so the walk finds git rather than demanding position 0.
    assert push_verdict.git_mutating_subcommand("timeout 60 git rebase origin/main") == "rebase"
    assert push_verdict.git_mutating_subcommand("/usr/bin/git commit -m wip") == "commit"


def test_a_mutation_after_a_background_operator_is_found() -> None:
    """A lone ``&`` separates two commands as surely as ``&&`` does.

    This is why the segment splitter is ``shell_normalizer``'s and not a local copy. The copy
    that lived here did not know the background operator, so the whole line was ONE segment,
    the walk read ``push`` as its only subcommand, and the reset rode along unseen.
    """
    assert push_verdict.git_mutating_subcommand("git " + "push origin x & git reset --hard") == (
        "reset"
    )
    assert push_verdict.git_mutating_subcommand("git reset --hard & echo done") == "reset"


def test_a_global_option_value_is_not_read_as_the_subcommand() -> None:
    """``git -C build push``: the subcommand is ``push``, and ``build`` is a path."""
    assert push_verdict.git_subcommand("git -C build push origin x")[0] == "push"
    assert push_verdict.git_subcommand("git -c user.name=x commit")[0] == "commit"
    assert push_verdict.git_subcommand("git --git-dir=/g reset")[0] == "reset"
    assert push_verdict.git_subcommand("ls -la")[0] == ""


# ── What the verdict is bound to ──
#
# Existence is not application. A verdict that only had to EXIST authorized a different branch,
# a different remote and a ref the guard never examined, all on one recorded pass.


def _bound(**over: str) -> push_verdict.Verdict:
    fields: dict[str, object] = {
        "gitdir": "/g",
        "worktree": "/some/worktree",
        "head": "a" * 40,
        "base": "main",
        "base_sha": "b" * 40,
        "remote": "origin",
        "source_ref": "feature-x",
        "recorded_at": 0.0,
    }
    fields.update(over)
    return push_verdict.Verdict(**fields)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "command",
    [
        "git push origin feature-x",
        "git push origin HEAD",
        "git push --force-with-lease origin HEAD:refs/heads/feature-x",
        "git push -u origin feature-x",
        "git push origin " + "a" * 40 + ":refs/heads/feature-x",
        # git's precedence: a positional repository wins over ``--repo``, so this publishes to
        # the judged remote and reading the option first would refuse it.
        "git push --repo fork origin feature-x",
    ],
)
def test_a_publish_of_what_was_judged_is_allowed(command: str) -> None:
    """Everything the guard did judge, in the spellings people actually type."""
    assert push_verdict.publish_mismatch(_bound(), command) == ""


@pytest.mark.parametrize("command", ["git push", "git push origin"])
def test_a_publish_naming_no_refspec_is_refused(command: str) -> None:
    """A bare push publishes whatever ``push.default`` expands to, which this gate cannot read.

    With ``push.default=matching`` a bare push publishes EVERY branch both sides share, so one
    branch's pass covered all of them. Establishing the real expansion means reading git config,
    and this predicate is consumed inside the permission gate, which reads no filesystem by
    design. So it refuses what it cannot establish.

    These two spellings are the ones a suite is most likely to bless, because they are what
    people type. Refusing them costs nothing an operator wants: on an activated installation the
    gateway performs the publish and releases the receipt with the operation, so an agent's bare
    push has no receipt to spend either way.
    """
    reason = push_verdict.publish_mismatch(_bound(), command)
    assert reason, "a publish naming no refspec was allowed"
    assert "push.default" in reason, "the refusal must say what decides the expansion"


@pytest.mark.parametrize(
    ("command", "names"),
    [
        ("git push fork feature-x", "fork"),
        ("git push origin other-branch", "other-branch"),
        ("git push origin HEAD:refs/heads/main", "refs/heads/main"),
        # The remote's own HEAD is not the judged branch either, and it is spelled like the
        # source position's one legitimate value.
        ("git push origin HEAD:HEAD", "HEAD"),
        # A commit sharing a prefix with the judged one is a DIFFERENT commit.
        ("git push origin " + "a" * 4 + "b" * 36 + ":refs/heads/feature-x", "a" * 4 + "b" * 8),
        ("git push --repo fork", "fork"),
        ("git push --repo=fork", "fork"),
        ("git push origin " + "c" * 40 + ":refs/heads/feature-x", "c" * 12),
    ],
)
def test_a_publish_of_something_else_is_refused_and_named(command: str, names: str) -> None:
    """Each of these passed a mere existence check while being nothing the guard looked at."""
    reason = push_verdict.publish_mismatch(_bound(), command)
    assert reason and names in reason


def test_a_detached_head_verdict_covers_only_its_own_commit() -> None:
    """With no branch judged there is no branch name to match, so a named ref is refused."""
    detached = _bound(source_ref="")
    assert push_verdict.publish_mismatch(detached, "git push origin HEAD") == ""
    assert push_verdict.publish_mismatch(detached, "git push origin " + "a" * 40) == ""
    assert push_verdict.publish_mismatch(detached, "git push origin feature-x") != ""


def test_the_floor_refuses_a_publish_the_verdict_does_not_cover(home: Path) -> None:
    """The binding at the REAL entry point, not just in the predicate.

    A test that calls the predicate proves the predicate works. This one proves the floor
    consults it, which is the half a mutation to the call site would otherwise survive.
    """
    from kiro_crew import security

    _activate(home)
    _record("session-1", source_ref="feature-x")
    wrong = "git " + "push origin someone-elses-branch"
    reason = security.is_denied(wrong, [], session_key="session-1")
    assert reason and "someone-elses-branch" in reason


# ── One judgement per request ──


def test_each_judgement_gets_its_own_refs() -> None:
    """Fixed ref names in a shared mirror let two requests overwrite each other's candidate.

    One mirror serves one REPOSITORY, so two sessions judging two of its branches at once wrote
    the same two refs: the second fetch replaced the first's candidate and the guard then
    measured one session's branch and answered for the other's.
    """
    from kiro_crew.dashboard.handlers import push_verdict as route

    first = route._JudgementRefs.mint()
    second = route._JudgementRefs.mint()
    assert first.base != second.base
    assert first.candidate != second.candidate
    assert first.base != first.candidate
    # Namespaced so they cannot collide with refs a repository already has.
    assert first.base.startswith("refs/kirocrew/")


def test_the_runner_hands_its_refs_to_the_operation_that_publishes(
    home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The refs outlive the JUDGING, because the push source is the candidate ref itself.

    Ownership sits with whoever finishes the operation: the runner hands the refs over and the
    caller removes them in its ``finally``, which the companion test drives. A runner that
    deleted its own refs would destroy the one thing the push needs, since the operation pushes
    ``candidate:refs/heads/<ref>``.
    """
    import asyncio
    import hashlib

    from kiro_crew.dashboard.handlers import push_verdict as route

    packaged = tmp_path / "push_guard.py"
    packaged.write_bytes(b"print('STATUS: SAFE TO PUSH')\n")
    monkeypatch.setattr(route, "_PUSH_GUARD", packaged)

    launched: list[list[str]] = []

    async def _fake_git(_worktree: str, *_args: str) -> tuple[int, str]:
        return 0, "https://example.invalid/repo.git"

    async def _fake_prime(*_args: object, **_kwargs: object) -> tuple[int, str]:
        return 0, ""

    async def _fake_run_git(argv: list[str], **_kwargs: object) -> tuple[int, str]:
        launched.append(list(argv))
        return 0, "f" * 40

    monkeypatch.setattr(route, "_git", _fake_git)
    monkeypatch.setattr(route, "_prime_mirror", _fake_prime)
    monkeypatch.setattr(route, "_run_git", _fake_run_git)

    run = asyncio.run(
        route._run_guard(
            "/some/worktree",
            "main",
            gitdir="/some/worktree/.git",
            digest=hashlib.sha256(packaged.read_bytes()).hexdigest(),
            url="https://example.invalid/repo.git",
        )
    )

    deleted = {argv[-1] for argv in launched if "update-ref" in argv and "-d" in argv}
    assert not deleted, "the runner deleted refs the publish still needs"
    assert run.refs is not None and run.mirror is not None, "the runner kept its refs to itself"


def test_a_publish_that_refuses_still_leaves_no_reusable_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The failure paths are the ones that matter for reusable authority.

    A publish refused because HEAD moved must not leave a pass behind: the next command would
    spend a receipt describing a commit the guard examined but the tree does not hold. Released
    in a ``finally``, so this holds on every way out of the operation rather than on the one the
    happy path takes.
    """
    from kiro_crew.dashboard.handlers import push_verdict as route

    status, payload = _drive_route(
        monkeypatch,
        audit_raises=False,
        publish_result=route._PublishResult(False, "head_moved", "HEAD moved from d... to f..."),
    )
    assert status == 409, "a tree that moved under a judgement is a conflict, not a server error"
    assert payload["verdict"] == "not_published"
    assert payload["code"] == "head_moved"
    assert push_verdict.verdict_for("session-1") is None, "a refused publish left a pass behind"


def _publish_env(monkeypatch, *, head_now: str, remote_now: str = "origin"):
    """Stub the three reads ``_publish`` makes, returning the argv list it launches."""
    from kiro_crew.dashboard.handlers import push_verdict as route

    launched: list[list[str]] = []

    async def _fake_git(_worktree: str, *args: str) -> tuple[int, str]:
        if args[:1] == ("rev-parse",):
            return 0, head_now
        if args[:2] == ("config", "--get"):
            key = args[2] if len(args) > 2 else ""
            return (0, remote_now) if key.endswith("pushRemote") else (1, "")
        if args[:2] == ("remote", "get-url"):
            return 0, "https://example.invalid/repo.git"
        return 0, ""

    async def _fake_run_git(argv: list[str], **_kwargs: object) -> tuple[int, str]:
        launched.append(list(argv))
        if "ls-remote" in argv:
            return 0, "b" * 40 + "\trefs/heads/feature-x"
        return 0, "pushed"

    monkeypatch.setattr(route, "_git", _fake_git)
    monkeypatch.setattr(route, "_run_git", _fake_run_git)
    return launched


def test_the_publish_pushes_the_judged_commit_under_a_lease_the_gateway_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The two properties that close the post-snapshot gap, read off the real argv.

    The SOURCE is the candidate ref -- the one holding the commit the guard examined -- so what
    lands is what was judged however the worktree has moved since. And the lease's expected
    value is the tip THIS gateway read from the remote, never a value a caller supplied: a lease
    against an agent-supplied sha would let the agent describe a remote state that never existed,
    which is the same defect as a receipt the agent writes.
    """
    import asyncio

    from kiro_crew.dashboard.handlers import push_verdict as route

    launched = _publish_env(monkeypatch, head_now="d" * 40)
    refs = route._JudgementRefs.mint()
    result = asyncio.run(
        route._publish(
            worktree="/some/worktree",
            mirror=Path("/some/mirror"),
            refs=refs,
            head="d" * 40,
            source_ref="feature-x",
            target=route._PushTarget("origin", "https://example.invalid/repo.git"),
        )
    )
    assert result.ok and result.code == "published"

    push = next(argv for argv in launched if "push" in argv)
    assert (
        f"{refs.candidate}:refs/heads/feature-x" in push
    ), "the push source was not the judged ref"
    assert f"--force-with-lease=refs/heads/feature-x:{'b' * 40}" in push


def test_a_head_that_moved_after_the_guard_refuses_to_publish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The finding's own case: a mutation the gateway never observed, then a publish."""
    import asyncio

    from kiro_crew.dashboard.handlers import push_verdict as route

    _publish_env(monkeypatch, head_now="f" * 40)
    result = asyncio.run(
        route._publish(
            worktree="/some/worktree",
            mirror=Path("/some/mirror"),
            refs=route._JudgementRefs.mint(),
            head="d" * 40,
            source_ref="feature-x",
            target=route._PushTarget("origin", "https://example.invalid/repo.git"),
        )
    )
    assert not result.ok and result.code == "head_moved"


def test_a_destination_that_moved_after_the_guard_refuses_to_publish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The destination half: a ``pushRemote`` written after the judging sends it elsewhere."""
    import asyncio

    from kiro_crew.dashboard.handlers import push_verdict as route

    _publish_env(monkeypatch, head_now="d" * 40, remote_now="fork")
    result = asyncio.run(
        route._publish(
            worktree="/some/worktree",
            mirror=Path("/some/mirror"),
            refs=route._JudgementRefs.mint(),
            head="d" * 40,
            source_ref="feature-x",
            target=route._PushTarget("origin", "https://example.invalid/repo.git"),
        )
    )
    assert not result.ok and result.code == "target_moved"


def test_a_wrapped_publish_to_another_remote_is_still_bound(home: Path) -> None:
    """The binding has to descend the same wrapper the existence gate descends.

    ``bash -c 'git push fork feature-x'`` matched the publish floor on the DESCENDED payload, so
    a session holding a verdict for ``origin`` passed the existence gate; the binding then read
    the OUTER line, where the token is ``'git`` -- quote unstripped, not a git program name -- so
    it found no target, answered empty, and allowed the push to an unjudged remote. The gate and
    the binding have to read the same thing, or the wrapper buys exactly what descending was
    added to take away.
    """
    from kiro_crew import security

    _activate(home)
    _record("session-1", remote="origin", source_ref="feature-x")
    wrapped = "bash -c 'git " + "push fork feature-x'"
    reason = security.is_denied(wrapped, [], session_key="session-1")
    assert reason, "a wrapped publish to an unjudged remote was allowed"


def test_a_wrapped_publish_to_the_judged_remote_is_allowed(home: Path) -> None:
    """The other direction, so the fix above is a binding and not a blanket refusal."""
    from kiro_crew import security

    _activate(home)
    _record("session-1", remote="origin", source_ref="feature-x")
    wrapped = "bash -c 'git " + "push origin feature-x'"
    assert security.is_denied(wrapped, [], session_key="session-1") is None


def test_a_guard_run_that_raises_still_removes_its_refs(
    home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The refs go to the caller on every ORDINARY exit, and are cleaned up on the others.

    Moving ownership to the caller left one path the caller never reaches: an exception inside
    the runner, where there is no return value to hand anything over with. Without this the
    mirror keeps a pair per crash, and nothing said so -- the mutation that removes the cleanup
    survived a whole suite until this test existed.
    """
    import asyncio
    import hashlib

    from kiro_crew.dashboard.handlers import push_verdict as route

    packaged = tmp_path / "push_guard.py"
    packaged.write_bytes(b"print('STATUS: SAFE TO PUSH')\n")
    monkeypatch.setattr(route, "_PUSH_GUARD", packaged)

    deleted: list[object] = []

    async def _fake_git(_worktree: str, *_args: str) -> tuple[int, str]:
        return 0, "https://example.invalid/repo.git"

    async def _boom(*_args: object, **_kwargs: object) -> tuple[int, str]:
        raise RuntimeError("the mirror went away mid-judgement")

    async def _record_delete(_mirror: object, refs: object) -> None:
        deleted.append(refs)

    monkeypatch.setattr(route, "_git", _fake_git)
    monkeypatch.setattr(route, "_prime_mirror", _boom)
    monkeypatch.setattr(route, "_delete_refs", _record_delete)

    with pytest.raises(RuntimeError):
        asyncio.run(
            route._run_guard(
                "/some/worktree",
                "main",
                gitdir="/some/worktree/.git",
                digest=hashlib.sha256(packaged.read_bytes()).hexdigest(),
                url="https://example.invalid/repo.git",
            )
        )
    assert len(deleted) == 1, "a crashed judgement left its refs in the long-lived mirror"


def test_the_operation_removes_the_refs_when_it_finishes(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of the ownership move: nothing accumulates in the long-lived mirror."""

    removed: list = []
    status, _payload = _drive_route(monkeypatch, audit_raises=False, deletions=removed)
    assert status == 200
    assert len(removed) == 1, "the operation did not remove the judgement's refs"


def test_a_mixed_case_branch_still_publishes(home: Path) -> None:
    """Refs are CASE-SENSITIVE, so the binding has to be read before any lowercasing.

    The floor lowercases its payload sources for the rest of its matching. Comparing a ref
    against that copy refuses ``Feature-X`` for not being ``feature-x``, which is a false
    refusal on a global floor -- the same trap that made ``git -C`` read as ``git -c``.
    """
    from kiro_crew import security

    _activate(home)
    _record("session-1", source_ref="Feature-X")
    assert security.is_denied("git " + "push origin Feature-X", [], session_key="session-1") is None


@pytest.mark.parametrize("pinned", ["", "abc123", "z" * 64, "A" * 63, 42, None])
def test_only_a_real_digest_counts_as_a_pin(home: Path, pinned: object) -> None:
    """A malformed pin reads as ABSENT, which is what produces the message that fixes it.

    Absence and "a digest that can never match" behave alike at the comparison, but only
    absence tells the operator to pin one. A 64-character non-hex string is the case a bare
    length check would wave through.
    """
    leaf = home / push_verdict.ACTIVATION_LEAF
    leaf.parent.mkdir(parents=True, exist_ok=True)
    leaf.write_text(json.dumps({"enabled": True, "guard_sha256": pinned}), encoding="utf-8")
    read = push_verdict.activation()
    assert read.enabled is True
    assert read.guard_sha256 == ""


def test_a_real_digest_is_read_back_lowercased(home: Path) -> None:
    """The companion, or the test above passes for an empty reader."""
    leaf = home / push_verdict.ACTIVATION_LEAF
    leaf.parent.mkdir(parents=True, exist_ok=True)
    leaf.write_text(json.dumps({"enabled": True, "guard_sha256": "A" * 64}), encoding="utf-8")
    assert push_verdict.activation().guard_sha256 == "a" * 64


def test_an_unpinned_installation_is_told_to_pin_rather_than_told_it_mismatched(
    home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Both refusals stop the run; only one of them is actionable.

    Falling through to the comparison would refuse with "these are not the bytes an operator
    authorized", which sends an operator looking for a changed file when nothing was ever
    pinned.
    """
    from kiro_crew.dashboard.handlers import push_verdict as route

    packaged = tmp_path / "push_guard.py"
    packaged.write_bytes(b"print('STATUS: SAFE TO PUSH')\n")
    monkeypatch.setattr(route, "_PUSH_GUARD", packaged)

    with pytest.raises(route._GuardUntrusted) as raised:
        route._guard_snapshot("")
    assert "no guard digest is pinned" in str(raised.value)
    assert push_verdict.ACTIVATION_LEAF in str(raised.value)


def test_a_snapshot_that_disagrees_with_the_pin_is_replaced(
    home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A stale copy must not be executed just because it is the copy that exists.

    Refreshing is safe HERE and only here: the bytes were verified against the pin first, so
    what replaces the copy is authorized rather than merely newer.
    """
    import hashlib

    from kiro_crew.dashboard.handlers import push_verdict as route

    packaged = tmp_path / "push_guard.py"
    packaged.write_bytes(b"print('STATUS: SAFE TO PUSH')\n")
    monkeypatch.setattr(route, "_PUSH_GUARD", packaged)
    pinned = hashlib.sha256(packaged.read_bytes()).hexdigest()

    stale = route.data_home() / push_verdict.MIRROR_DIR / "push_guard.py"
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_bytes(b"import sys\nsys.exit(0)\n")

    assert route._guard_snapshot(pinned).read_bytes() == packaged.read_bytes()


def test_two_judgements_do_not_share_refs(
    home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The runner has to mint PER REQUEST, not merely be capable of minting.

    A test that calls the minting helper twice proves the helper varies. It says nothing about
    whether the runner calls it, which is where two concurrent judgements of one repository
    overwrote each other's candidate.
    """
    import asyncio
    import hashlib

    from kiro_crew.dashboard.handlers import push_verdict as route

    packaged = tmp_path / "push_guard.py"
    packaged.write_bytes(b"print('STATUS: SAFE TO PUSH')\n")
    monkeypatch.setattr(route, "_PUSH_GUARD", packaged)
    digest = hashlib.sha256(packaged.read_bytes()).hexdigest()

    launched: list[list[str]] = []

    async def _fake_git(_worktree: str, *_args: str) -> tuple[int, str]:
        return 0, "https://example.invalid/repo.git"

    async def _fake_prime(*_args: object, **_kwargs: object) -> tuple[int, str]:
        return 0, ""

    async def _fake_run_git(argv: list[str], **_kwargs: object) -> tuple[int, str]:
        launched.append(list(argv))
        return 0, "f" * 40

    monkeypatch.setattr(route, "_git", _fake_git)
    monkeypatch.setattr(route, "_prime_mirror", _fake_prime)
    monkeypatch.setattr(route, "_run_git", _fake_run_git)

    for _ in range(2):
        asyncio.run(
            route._run_guard(
                "/some/worktree",
                "main",
                gitdir="/some/worktree/.git",
                digest=digest,
                url="https://example.invalid/repo.git",
            )
        )

    used = {
        argv[argv.index("--candidate-ref") + 1] for argv in launched if "--candidate-ref" in argv
    }
    assert len(used) == 2, "both judgements pointed the guard at the same candidate ref"


def test_an_unactivated_installation_banks_no_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    """With gating off the floor never reads a verdict, so recording one banks a pass nobody
    asked for -- and it would be spent the moment an operator did activate."""
    from kiro_crew.dashboard.handlers import push_verdict as route

    status, payload = _drive_route(monkeypatch, audit_raises=False, activated=False)
    assert status == 200
    assert payload["verdict"] == "not_activated"
    assert push_verdict.verdict_for("session-1") is None
    assert route is not None


def test_the_subsystem_stays_off_the_gateway_boot_path() -> None:
    """Importing this module at package import time puts its whole dependency tree on boot.

    Read as an AST rather than as text: a name appears in comments and docstrings, so counting
    occurrences pins nothing, which is the same reason the repository's own spawn audit parses
    instead of grepping. The precedent being followed is ``work_ledger``, deferred the same way.
    """
    import ast
    from pathlib import Path as _Path

    import kiro_crew.dashboard.handlers as handlers_pkg
    import kiro_crew.dashboard.server as server_mod

    tree = ast.parse(_Path(handlers_pkg.__file__).read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.extend(alias.name for alias in node.names)
            imported.append(node.module or "")
        elif isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
    assert not [
        name for name in imported if name.endswith("push_verdict")
    ], "handlers/__init__ imports the push-verdict module, putting it on the boot path"

    # And the registration still happens, through the deferred accessor rather than not at all.
    assert hasattr(server_mod, "_deferred_push_verdict")


# ── Round four: what six blocking findings said was missing ──


def test_the_mirror_root_is_materialised_before_any_sandbox_starts() -> None:
    """A fence seals an EXISTING path, so an absent mirror root was never sealed at all.

    On a fresh activated install no judgement has run, so the directory does not exist, the
    read-only mount is skipped, and the name the agent would create is exactly the one the
    read-only listing exists to deny. Pre-creation is what makes that listing mean anything,
    and the DIRECTORY list is the right one: the FILE list would materialise an empty file at
    the mirror root's own path and the first ``mkdir`` would raise.
    """
    from kiro_crew import sandbox

    assert push_verdict.MIRROR_DIR in sandbox._CREW_PRECREATE_READONLY_DIR_LEAVES
    assert push_verdict.MIRROR_DIR not in sandbox._CREW_PRECREATE_READONLY_FILE_LEAVES
    # And the NAME must stay the mounted name: a resolving symlink would let the agent choose
    # the repository the ancestry check runs in.
    assert push_verdict.MIRROR_DIR in sandbox._CREW_NOFOLLOW_READONLY_DIR_LEAVES
    # The repository requires every nofollow seal to say what an agent could do with it.
    assert push_verdict.MIRROR_DIR in sandbox._DELEGATED_OVERLAP_LEAF_REASONS


@pytest.mark.parametrize(
    "path",
    [
        "/repo/.git/refs/heads/feature-x",
        "/repo/.git/HEAD",
        "/repo/.git",
        "repo\\.git\\refs\\heads\\feature-x",
        # Re-cased, because a case-insensitive filesystem lands these in the real `.git`.
        "/repo/.GIT/refs/heads/feature-x",
        "C:\\repo\\.Git\\HEAD",
    ],
)
def test_a_write_inside_a_git_directory_drops_the_verdict(path: str) -> None:
    """A ref moves with no git verb for the command matcher to see.

    ``.git/refs/heads/<branch>`` written by a file tool leaves the store describing a commit
    the branch does not point at, and the floor cannot notice because it reads no
    filesystem. Both separators are read: a payload's spelling is the agent's choice.
    """
    _record("session-1")
    assert push_verdict.invalidate_on_write("session-1", [path]) == path
    assert push_verdict.verdict_for("session-1") is None


@pytest.mark.parametrize(
    "path", ["/repo/src/main.py", "/repo/notes.git.md", "/repo/gitignore", "/repo/.gitignore"]
)
def test_an_ordinary_write_keeps_the_verdict(path: str) -> None:
    """Matched on the path COMPONENT, so a file merely NAMED like git is not a ref write."""
    _record("session-1")
    assert push_verdict.invalidate_on_write("session-1", [path]) == ""
    assert push_verdict.verdict_for("session-1") is not None


def test_the_hook_drops_the_verdict_on_a_real_git_metadata_write(home: Path) -> None:
    """Driven through the REAL hook, because the predicate working proves nothing about wiring.

    This is the same gap the reviewers found twice: a helper with no consumer reads as a
    control and is not one.
    """
    from kiro_crew.hooks import HookManager, HooksConfig

    _activate(home)
    _record("session-1")
    HookManager(HooksConfig()).on_tool_call(
        "Writing .git/refs/heads/feature-x",
        raw_params={"path": "/some/worktree/.git/refs/heads/feature-x"},
        is_shell=False,
        session_key="session-1",
    )
    assert push_verdict.verdict_for("session-1") is None


def test_a_publish_naming_several_refspecs_is_refused() -> None:
    """Reading only the second positional left every later refspec unjudged."""
    reason = push_verdict.publish_mismatch(_bound(), "git push origin feature-x other-branch")
    assert reason and "2 refspecs" in reason


@pytest.mark.parametrize(
    ("judged", "published"),
    [("feat/foo", "bug/foo"), ("release/1.2", "hotfix/1.2"), ("feature-x", "refs/tags/feature-x")],
)
def test_two_refs_sharing_a_last_component_are_not_the_same_ref(
    judged: str, published: str
) -> None:
    """Comparing the LAST component made ``bug/foo`` equal ``feat/foo``.

    A tag is not a branch either: only the branch-namespace prefix is stripped, so
    ``refs/tags/x`` and ``x`` stay different refs.
    """
    assert push_verdict.publish_mismatch(_bound(source_ref=judged), f"git push origin {published}")


@pytest.mark.parametrize(
    "published", ["feature-x", "refs/heads/feature-x", "+refs/heads/feature-x"]
)
def test_the_two_spellings_of_one_branch_compare_equal(published: str) -> None:
    """The companion: normalising must not refuse the fully-qualified spelling of what was judged."""
    assert push_verdict.publish_mismatch(_bound(), f"git push origin {published}") == ""


@pytest.mark.parametrize(
    "command",
    [
        "(cd /other/repo && git " + "push origin feature-x)",
        "{ cd /other/repo; git " + "push origin feature-x; }",
        "`cd /other/repo && git " + "push origin feature-x`",
    ],
)
def test_a_wrapped_directory_change_is_still_a_redirection(command: str) -> None:
    """A grouping construct WRAPS a command, it does not start a new one.

    Read literally, ``(cd x && git push)`` puts ``(cd`` at position zero and ``{ cd x; ... }``
    puts ``{`` there, so a position-zero test for a directory verb answered no to both -- and
    the publish that followed was measured against a verdict for a different tree, with
    nothing else able to catch it.
    """
    assert push_verdict.redirects_repository(command) == "cd"


def test_a_wrapped_ordinary_publish_is_not_a_redirection() -> None:
    """The companion, or the fix above would refuse every subshell-wrapped publish."""
    assert push_verdict.redirects_repository("(git " + "push origin feature-x)") == ""
    assert push_verdict.publish_mismatch(_bound(), "(git " + "push origin feature-x)") == ""


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("git config remote.origin.pushurl https://elsewhere.invalid/x.git", "config"),
        ("git remote set-url --push origin https://elsewhere.invalid/x.git", "remote"),
        ("git config --get remote.origin.url", ""),
        ("git config --list", ""),
        ("git remote -v", ""),
        ("git remote", ""),
        ("git remote get-url origin", ""),
        ("git remote show origin", ""),
    ],
)
def test_a_command_that_moves_the_destination_counts_as_a_mutation(
    command: str, expected: str
) -> None:
    """A verdict describes one DESTINATION as much as one pair of commits.

    Writing ``remote.<name>.pushurl`` sends the next publish to another repository without
    touching a commit, so ``config`` and ``remote`` are mutating verbs -- and their READS are
    spelled three different ways, which is why the exception knows options, subverbs and the
    bare form separately.
    """
    assert push_verdict.git_mutating_subcommand(command) == expected


def test_a_remote_that_pushes_elsewhere_is_refused_rather_than_judged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gateway judges what it can READ; a pushurl sends the commit somewhere else.

    Answering would describe the wrong repository under an accepted remote name, so the
    request is refused and says why.
    """
    status, payload = _drive_route(
        monkeypatch, audit_raises=False, push_url="https://elsewhere.invalid/other.git"
    )
    assert status == 400
    assert payload["code"] == "push_url_differs"
    assert push_verdict.verdict_for("session-1") is None


def test_the_recorded_remote_is_the_one_a_publish_actually_reaches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Assuming ``origin`` was a gap: a bare ``git push`` names no remote to compare.

    With ``branch.<name>.pushRemote`` set, the commit landed in a repository the guard never
    looked at while the floor had nothing to hold the command to.
    """
    seen: list = []
    status, payload = _drive_route(
        monkeypatch,
        audit_raises=False,
        config_answers={"branch.feature-x.pushRemote": "fork"},
        capture=seen,
    )
    assert status == 200
    assert seen[0] is not None and seen[0].remote == "fork"
    # And the base comes from THAT remote's default branch, not origin's.
    assert payload["base"] == "main"


def test_remote_push_default_is_honoured_when_the_branch_names_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """git's own precedence order, one rung down."""
    seen: list = []
    _drive_route(
        monkeypatch,
        audit_raises=False,
        config_answers={"remote.pushDefault": "upstream"},
        capture=seen,
    )
    assert seen[0] is not None and seen[0].remote == "upstream"


def test_moving_a_slots_project_drops_the_verdict_earned_in_the_old_one() -> None:
    """The invalidation lives on the one write every caller goes through.

    It was wired to ``SessionMap.set_project_override``, which has no callers at all, so the
    control was inert while the live mutation was this attribute -- assigned from the HTTP
    project route, the session directive, the fork path and the channel and member binders.
    """
    from kiro_crew.dashboard.state import _ChatSlot

    slot = _ChatSlot("slot-1")
    slot.project = "/repo-a"
    _record(slot.key, gitdir="/repo-a/.git", worktree="/repo-a")
    assert push_verdict.verdict_for(slot.key) is not None

    slot.project = "/repo-b"
    assert push_verdict.verdict_for(slot.key) is None
    assert slot.project == "/repo-b"


def test_setting_the_same_project_again_keeps_the_verdict() -> None:
    """Only a CHANGE costs a guard re-run; a rewrite of the same value is not a move."""
    from kiro_crew.dashboard.state import _ChatSlot

    slot = _ChatSlot("slot-1")
    slot.project = "/repo-a"
    _record(slot.key, gitdir="/repo-a/.git", worktree="/repo-a")
    slot.project = "/repo-a"
    assert push_verdict.verdict_for(slot.key) is not None


def test_the_worktree_sweep_reaches_a_verdict_under_another_key() -> None:
    """A caller holding a slot may not be able to name the key the floor will read.

    The tree is recorded on the verdict, so the sweep reaches it either way. Over-invalidation
    is the safe direction: it costs one guard re-run.
    """
    _record("some-other-key", worktree="/repo-a")
    _record("unrelated", worktree="/repo-b")
    assert push_verdict.invalidate_for_worktree("/repo-a") == 1
    assert push_verdict.verdict_for("some-other-key") is None
    assert push_verdict.verdict_for("unrelated") is not None


def test_a_session_that_moved_repositories_loses_its_old_pass_at_record_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What READS the recorded gitdir.

    A verdict for another repository must not stay available for the window between this
    request and the record it produces.
    """
    _record("session-1", gitdir="/elsewhere/.git", worktree="/elsewhere")
    status, _payload = _drive_route(monkeypatch, audit_raises=True)
    assert status == 500
    assert push_verdict.verdict_for("session-1") is None


def test_the_activation_read_is_live_on_every_publish(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Activation is re-read per publish, and that is the deliberate choice.

    A reviewer reads the floor's "no expensive I/O" contract and sees an ``open()`` plus a JSON
    parse, which is a fair objection. A stat-keyed cache of the parse was written and removed:
    its key -- modification time, size and inode -- cannot tell two writes of the SAME byte
    length within one clock tick apart, and the value it would serve is an ENABLE decision, so
    the failure mode is the gate reading as off after an operator turned it on. This test is the
    pin that the cheap-but-wrong version does not come back.
    """
    leaf = _activate(home)
    parses = 0
    real_load = push_verdict.json.load

    def _counting_load(handle):
        nonlocal parses
        parses += 1
        return real_load(handle)

    monkeypatch.setattr(push_verdict.json, "load", _counting_load)

    for _ in range(3):
        assert push_verdict.activation().enabled is True
    assert parses == 3, "activation was served from a cache"

    # The property that matters: a change takes effect on the very next read, whatever its size.
    leaf.write_text(json.dumps({"enabled": False}), encoding="utf-8")
    assert push_verdict.activation().enabled is False


def test_the_presenter_renders_the_routes_fourth_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    """``not_activated`` is an ordinary, correct answer and must not read as a product fault.

    The presenter's fall-through reported it as "unrecognised verdict", which is what most
    installations would have seen, since most have not turned this gate on.
    """
    from kiro_crew.mcp_tools import push_verdict as presenter

    monkeypatch.setattr(presenter, "_strict_session_key", lambda: ("session-1", None))
    monkeypatch.setattr(
        presenter.mcp_core,
        "_post",
        lambda *a, **k: {"verdict": "not_activated", "base": "main", "detail": "not gated"},
    )

    answer = presenter.push_verdict_run("push_verdict_run", {})
    assert "not activated" in answer
    assert "unrecognised" not in answer
