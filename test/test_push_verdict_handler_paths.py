"""The push-verdict handler's plumbing and refusal branches, exercised directly.

The ordering tests in ``test_push_verdict_gate.py`` drive the route with every git read and
the guard subprocess stubbed, so the spawn plumbing (``_run_git``), the mirror priming and
resolution helpers, and several refusal branches never run there. These tests reach those
paths without a real sandbox spawn: the single subprocess chokepoint is stubbed at
``create_subprocess_limited`` and the spawn preparation at ``shielded_prepare_off_loop``, so
what executes is the module's own control flow, not a namespace launcher the test host may
not permit.

The handler module is imported lazily inside each test, the convention the sibling gate file
follows, because eagerly importing ``kiro_crew.dashboard.handlers`` at module scope drags in
the whole handler package and collides with the coverage plugin's re-import.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest


@pytest.fixture
def route():
    from kiro_crew.dashboard.handlers import push_verdict as route

    return route


class _FakeStdout:
    """A minimal ``StreamReader`` stand-in: hands back the canned bytes then EOF.

    ``_read_capped`` calls ``read(n)`` in a loop until it gets ``b""``. Emitting the whole
    buffer in one chunk (capped at ``n``) then EOF exercises both the normal small-output path
    and, with a buffer larger than the cap, the truncation branch.
    """

    def __init__(self, out: bytes) -> None:
        self._buf = out

    async def read(self, n: int) -> bytes:
        if not self._buf:
            return b""
        chunk, self._buf = self._buf[:n], self._buf[n:]
        return chunk


class _FakeProc:
    """The subset of an asyncio subprocess the module touches."""

    def __init__(self, rc: int, out: bytes, *, hang: bool = False) -> None:
        self.returncode = rc
        self._out = out
        self._hang = hang
        self.killed = False
        self.stdout = _FakeStdout(out)

    async def wait(self) -> int:
        if self._hang:
            await asyncio.sleep(3600)
        return self.returncode

    async def communicate(self) -> tuple[bytes, bytes]:
        if self._hang:
            await asyncio.sleep(3600)
        return self._out, b""

    def kill(self) -> None:
        self.killed = True


def _stub_spawn(route, monkeypatch, proc, *, cleanup=None, git_bin="/trusted/bin/git"):
    """Stub the two things ``_run_git`` calls so no real process is launched.

    ``_prepare_sandboxed_spawn`` returns the wrapped argv, a scrubbed env, and a cleanup path
    the caller must unlink; ``create_subprocess_limited`` returns the fake process.

    ``trusted_git_bin`` is pinned to a sentinel so the tests do not depend on whether the test
    host happens to have a git on a trusted system directory: ``_run_git`` resolves argv[0]
    through it and refuses when it returns ``None``, so leaving it unpinned would make these
    tests pass or refuse by host. Pass ``git_bin=None`` to exercise the refusal branch.
    """
    made: dict[str, object] = {"git_bin": git_bin}

    async def _fake_prepare(argv, *, env, visible, writable=()):  # type: ignore[no-untyped-def]
        made["argv"] = argv
        made["visible"] = visible
        made["writable"] = writable
        made["env"] = env
        return (list(argv), {"SCRUBBED": "1"}, cleanup)

    async def _fake_create(*wrapped, **_kwargs):  # type: ignore[no-untyped-def]
        made["wrapped"] = list(wrapped)
        return proc

    monkeypatch.setattr("kiro_crew.platform_compat.trusted_git_bin", lambda: git_bin)
    monkeypatch.setattr(route, "_prepare_sandboxed_spawn", _fake_prepare)
    monkeypatch.setattr(route, "create_subprocess_limited", _fake_create)
    return made


def _canned_run_git(answers, calls=None):
    """A ``_run_git`` replacement that dispatches on a substring of the joined argv."""

    async def _fake(argv, **_kwargs):  # type: ignore[no-untyped-def]
        if calls is not None:
            calls.append(argv)
        joined = " ".join(str(a) for a in argv)
        for needle, result in answers:
            if needle in joined:
                return result
        return (0, "")

    return _fake


# ── _run_git: the single routed spawn ──


def test_run_git_returns_rc_and_decoded_output(route, monkeypatch: pytest.MonkeyPatch) -> None:
    """A normal spawn returns the process's rc and its stripped, decoded stdout."""
    _stub_spawn(route, monkeypatch, _FakeProc(0, b"  hello \n"))
    rc, out = asyncio.run(route._run_git(["git", "status"], visible=("/wt",), timeout=5))
    assert rc == 0
    assert out == "hello"


def test_run_git_unlinks_the_cleanup_path(
    route, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The chokepoint materialises a launcher the caller must remove; the finally does it."""
    leftover = tmp_path / "launcher.tmp"
    leftover.write_text("x", encoding="utf-8")
    _stub_spawn(route, monkeypatch, _FakeProc(0, b"ok"), cleanup=str(leftover))
    rc, out = asyncio.run(route._run_git(["git", "status"], visible=(), timeout=5))
    assert rc == 0
    assert not leftover.exists(), "the caller must unlink the chokepoint's cleanup file"


def test_run_git_missing_cleanup_file_is_suppressed(route, monkeypatch: pytest.MonkeyPatch) -> None:
    """A cleanup path that is already gone must not raise out of the finally."""
    _stub_spawn(route, monkeypatch, _FakeProc(0, b"ok"), cleanup="/nonexistent/launcher.tmp")
    rc, _ = asyncio.run(route._run_git(["git", "status"], visible=(), timeout=5))
    assert rc == 0


def test_run_git_times_out_and_kills(route, monkeypatch: pytest.MonkeyPatch) -> None:
    """A stalled worktree must not hold the request open: the spawn is killed, 124 returned."""
    proc = _FakeProc(0, b"", hang=True)
    _stub_spawn(route, monkeypatch, proc)
    rc, out = asyncio.run(route._run_git(["git", "fetch"], visible=(), timeout=0))
    assert rc == 124
    assert out == "timed out"
    assert proc.killed is True


def test_git_reader_passes_dash_c_and_worktree_visible(
    route, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_git`` reads with ``-C <worktree>`` and makes only that worktree visible."""
    made = _stub_spawn(route, monkeypatch, _FakeProc(0, b"a" * 40))
    rc, out = asyncio.run(route._git("/wt", "rev-parse", "HEAD"))
    assert rc == 0 and out == "a" * 40
    # argv[0] is resolved to the trusted git path (the sentinel here), never left as bare "git".
    assert made["argv"] == ["/trusted/bin/git", "-C", "/wt", "rev-parse", "HEAD"]
    assert made["visible"] == ("/wt",)


# ── _run_git: git-binary resolution (GPT F1 fix) ──


def test_run_git_refuses_when_no_trusted_git(route, monkeypatch: pytest.MonkeyPatch) -> None:
    """When ``trusted_git_bin()`` returns ``None`` the spawn is REFUSED, never falls back to
    bare "git" resolved through the ambient PATH.

    Mutation check: on the un-fixed module ``_run_git`` never consults ``trusted_git_bin`` and
    would prepare a spawn of bare "git" (rc 0 here from the stub), so this assertion fails.
    """
    made = _stub_spawn(route, monkeypatch, _FakeProc(0, b"should-not-run"), git_bin=None)
    rc, out = asyncio.run(route._run_git(["git", "status"], visible=("/wt",), timeout=5))
    assert rc == 127
    assert "trusted git" in out
    assert "argv" not in made, "no spawn must be prepared when git cannot be trusted"


def test_run_git_substitutes_resolved_git_path_as_argv0(
    route, monkeypatch: pytest.MonkeyPatch
) -> None:
    """argv[0] is replaced with the absolute path ``trusted_git_bin()`` returns, not left "git".

    Mutation check: the un-fixed module leaves argv[0] == "git", so ``made["argv"][0]`` would
    be "git" and this assertion fails.
    """
    made = _stub_spawn(route, monkeypatch, _FakeProc(0, b"ok"), git_bin="/opt/trusted/bin/git")
    rc, _ = asyncio.run(route._run_git(["git", "status"], visible=("/wt",), timeout=5))
    assert rc == 0
    argv = made["argv"]
    assert isinstance(argv, list)
    assert argv[0] == "/opt/trusted/bin/git", "argv[0] must be the resolved trusted path"
    assert argv[1:] == ["status"], "the rest of argv is unchanged"


def test_run_git_sets_a_trusted_only_child_path(route, monkeypatch: pytest.MonkeyPatch) -> None:
    """The git spawn's PATH is TRUSTED-ONLY: the trusted git's dir leads, the fixed trusted
    system dirs follow, and the agent-writable remainder is DROPPED so a planted
    ``git-remote-<transport>`` helper on it cannot win the child's lookup on this
    ``gateway_publish`` spawn that holds the publish credentials.

    Mutation check: the un-fixed ``git_env`` copies os.environ whole and only PREPENDS the
    trusted dir, RETAINING ``/opt/agent-writable/bin`` -- so the ``not in`` assertion fails on
    the old code (F2 GPT finding: retained PATH permits remote-helper credential theft).
    """
    from kiro_crew import platform_compat

    monkeypatch.setenv(
        "PATH", os.pathsep.join(["/opt/agent-writable/bin", "/opt/evil/bin", "/usr/bin"])
    )
    made = _stub_spawn(route, monkeypatch, _FakeProc(0, b"ok"), git_bin="/opt/trusted/bin/git")
    rc, _ = asyncio.run(route._run_git(["git", "fetch"], visible=("/wt",), timeout=5))
    assert rc == 0
    env = made["env"]
    assert isinstance(env, dict)
    parts = env["PATH"].split(os.pathsep)
    assert parts[0] == os.path.dirname(
        "/opt/trusted/bin/git"
    ), "the trusted git's dir must lead the child PATH"
    assert "/opt/agent-writable/bin" not in parts, "agent-writable entries must be DROPPED"
    assert "/opt/evil/bin" not in parts, "no retained agent-writable entry may survive"
    # Only the trusted git dir and the fixed trusted system dirs remain.
    assert set(parts) == {"/opt/trusted/bin", *platform_compat._TRUSTED_SYSTEM_BIN_DIRS}


def test_git_env_transport_allowlist_denies_by_default_and_allows_https_ssh(route) -> None:
    """``git_env`` denies every remote transport by default and re-opens only https/ssh/file --
    the ones a gateway publish legitimately uses -- so a ``git-remote-<other>`` transport the
    agent names in the worktree config is refused by git before any PATH lookup.

    Mutation check: the un-fixed ``_NEUTRALIZED_GIT_CONFIG`` has only ``protocol.ext.allow`` and
    no ``protocol.allow=never`` default, so the assembled config would not carry the deny-first
    allowlist and these assertions fail (F2 Opus contained-fix: transport allowlist).
    """
    env = route.git_env()
    count = int(env["GIT_CONFIG_COUNT"])
    config = {env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"] for i in range(count)}
    assert config["protocol.allow"] == "never", "all transports denied by default"
    assert config["protocol.https.allow"] == "always"
    assert config["protocol.ssh.allow"] == "always"
    assert config["protocol.file.allow"] == "always", "the local mirror fetch uses file://"
    assert config["protocol.ext.allow"] == "never", "ext:: stays explicitly blocked"
    # No entry re-opens an arbitrary transport the gateway does not need.
    reopened = {k for k, v in config.items() if k.startswith("protocol.") and v == "always"}
    assert reopened == {
        "protocol.https.allow",
        "protocol.ssh.allow",
        "protocol.file.allow",
    }, "only https/ssh/file may be re-opened"


def test_git_env_cuts_global_and_system_config_out_of_every_gateway_git() -> None:
    """``git_env`` denies git the user and system config scopes, so an agent-writable
    ``~/.gitconfig`` (or system config) carrying ``url.<evil>.pushInsteadOf=<pin>`` cannot
    rewrite the credentialed publish AFTER ``_effective_push_target`` validated the pin against
    the worktree URL -- the exact post-check redirection all three reviewers blocked on this head.

    Mutation check: delete the two ``env[...]`` lines in ``git_env`` and the global/system config
    scopes reach every gateway git again, so these assertions fail -- a planted ``pushInsteadOf``
    would then redirect the push unseen.
    """
    import os as _os

    from kiro_crew.dashboard.handlers import push_verdict as route

    env = route.git_env()
    assert (
        env["GIT_CONFIG_GLOBAL"] == _os.devnull
    ), "the global (~/.gitconfig) scope must be pointed at the null device"
    assert env["GIT_CONFIG_NOSYSTEM"] == "1", "the system config scope must be disabled"


def test_run_git_routes_the_guard_sys_executable_without_raising(
    route, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F1 REGRESSION: ``_run_guard`` routes the packaged guard through ``_run_git`` with
    ``argv[0] == sys.executable`` (NOT "git"). ``_run_git`` must route it -- an absolute
    non-"git" program is spawned as-is -- and must NOT raise.

    Mutation check: the un-fixed ``_run_git`` raises ``AssertionError`` on any argv[0] != "git",
    so on current code this call raises instead of returning, and the activated guard 500s on
    every request. This is the test that would have caught the regression.
    """
    import sys

    made = _stub_spawn(
        route, monkeypatch, _FakeProc(0, b"guard-ok"), git_bin="/opt/trusted/bin/git"
    )
    argv = [sys.executable, "/opt/skills/push_guard.py", "--base", "abc123", "--no-fetch"]
    rc, out = asyncio.run(route._run_git(argv, visible=("/wt",), timeout=5))
    assert rc == 0
    assert out == "guard-ok"
    # argv[0] (sys.executable, absolute) is passed through unchanged -- NOT resolved to git.
    prepared = made["argv"]
    assert isinstance(prepared, list)
    assert prepared[0] == sys.executable, "the guard's interpreter must be spawned as passed"
    assert prepared[1] == "/opt/skills/push_guard.py"


def test_run_git_refuses_a_slashless_non_git_program(
    route, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The anti-PATH-hijack invariant is preserved: a non-"git" argv[0] that is NOT absolute is
    refused, never spawned, because it would resolve through the ambient PATH.

    Mutation check: dropping the ``os.path.isabs`` guard would prepare a spawn of a slash-less
    program (``made["argv"]`` present, rc 0), so ``"argv" not in made`` fails.
    """
    made = _stub_spawn(route, monkeypatch, _FakeProc(0, b"nope"), git_bin="/opt/trusted/bin/git")
    rc, out = asyncio.run(route._run_git(["python3", "-c", "print(1)"], visible=(), timeout=5))
    assert rc == 127
    assert "non-absolute" in out
    assert "argv" not in made, "no spawn may be prepared for a slash-less non-'git' program"


def test_run_git_small_path_returns_rc_and_text_unchanged(
    route, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The normal small-output path still returns ``(rc, stripped_text)`` after the fix."""
    _stub_spawn(route, monkeypatch, _FakeProc(3, b"  boom \n"), git_bin="/opt/trusted/bin/git")
    rc, out = asyncio.run(route._run_git(["git", "status"], visible=(), timeout=5))
    assert rc == 3
    assert out == "boom"


# ── _prime_mirror / _resolve / _remote_tip: the mirror helpers ──


def test_prime_mirror_inits_then_fetches_base_and_candidate(
    route, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A fresh mirror is created, then the base (from remote) and candidate (from worktree)
    are fetched into it."""
    mirror = tmp_path / "m.git"  # no HEAD yet -> triggers init
    calls: list = []
    monkeypatch.setattr(
        route, "_run_git", _canned_run_git([("init", (0, "")), ("fetch", (0, ""))], calls)
    )
    refs = route._JudgementRefs.mint()
    rc, detail = asyncio.run(
        route._prime_mirror("/wt", mirror, "main", "https://ex.invalid/r.git", refs)
    )
    assert rc == 0 and detail == ""
    joined = [" ".join(str(a) for a in c) for c in calls]
    assert any("init" in j for j in joined), "a fresh mirror must be initialised"
    assert any(f"+main:{refs.base}" in j for j in joined), "the base is fetched from the remote"
    assert any(f"+HEAD:{refs.candidate}" in j for j in joined), "the candidate from the worktree"


def test_prime_mirror_reports_a_failed_init(
    route, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(route, "_run_git", _canned_run_git([("init", (2, "disk full"))]))
    refs = route._JudgementRefs.mint()
    rc, detail = asyncio.run(route._prime_mirror("/wt", tmp_path / "m.git", "main", "u", refs))
    assert rc == 2 and "mirror" in detail


def test_prime_mirror_reports_a_failed_base_fetch(
    route, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    mirror = tmp_path / "m.git"
    mirror.mkdir()
    (mirror / "HEAD").write_text("ref: refs/heads/x\n", encoding="utf-8")  # skip init
    monkeypatch.setattr(
        route, "_run_git", _canned_run_git([("+main:", (2, "no such ref")), ("fetch", (0, ""))])
    )
    refs = route._JudgementRefs.mint()
    rc, detail = asyncio.run(route._prime_mirror("/wt", mirror, "main", "u", refs))
    assert rc == 2 and "fetch main" in detail


def test_prime_mirror_reports_a_failed_candidate_fetch(
    route, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    mirror = tmp_path / "m.git"
    mirror.mkdir()
    (mirror / "HEAD").write_text("ref: refs/heads/x\n", encoding="utf-8")
    refs = route._JudgementRefs.mint()

    async def _fake(argv, **_kwargs):  # type: ignore[no-untyped-def]
        joined = " ".join(str(a) for a in argv)
        if f"+HEAD:{refs.candidate}" in joined:
            return (2, "read-only worktree")
        return (0, "")

    monkeypatch.setattr(route, "_run_git", _fake)
    rc, detail = asyncio.run(route._prime_mirror("/wt", mirror, "main", "u", refs))
    assert rc == 2 and "worktree" in detail


def test_resolve_returns_the_commit_or_empty(
    route, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(route, "_run_git", _canned_run_git([("rev-parse", (0, "d" * 40))]))
    assert asyncio.run(route._resolve(tmp_path / "m.git", "some/ref")) == "d" * 40
    monkeypatch.setattr(route, "_run_git", _canned_run_git([("rev-parse", (128, ""))]))
    assert asyncio.run(route._resolve(tmp_path / "m.git", "some/ref")) == ""


def test_remote_tip_reads_the_first_sha_or_empty(
    route, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        route,
        "_run_git",
        _canned_run_git([("ls-remote", (0, "f" * 40 + "\trefs/heads/feature-x\n"))]),
    )
    rc, tip = asyncio.run(route._remote_tip(tmp_path / "m.git", "u", "feature-x"))
    assert rc == 0 and tip == "f" * 40
    # An absent branch answers "" -- how git spells "must not exist".
    monkeypatch.setattr(route, "_run_git", _canned_run_git([("ls-remote", (0, ""))]))
    rc, tip = asyncio.run(route._remote_tip(tmp_path / "m.git", "u", "feature-x"))
    assert rc == 0 and tip == ""
    # A failed read propagates its rc with no tip.
    monkeypatch.setattr(route, "_run_git", _canned_run_git([("ls-remote", (2, ""))]))
    rc, tip = asyncio.run(route._remote_tip(tmp_path / "m.git", "u", "feature-x"))
    assert rc == 2 and tip == ""


def test_delete_refs_removes_both(route, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: list = []
    monkeypatch.setattr(route, "_run_git", _canned_run_git([("update-ref", (0, ""))], seen))
    refs = route._JudgementRefs.mint()
    asyncio.run(route._delete_refs(tmp_path / "m.git", refs))
    joined = [" ".join(str(a) for a in c) for c in seen]
    assert any(refs.base in j for j in joined)
    assert any(refs.candidate in j for j in joined)


# ── _publish: the refusal branches the ordering tests do not reach ──


def _publish_kwargs(route, mirror: Path):
    return dict(
        worktree="/wt",
        mirror=mirror,
        refs=route._JudgementRefs.mint(),
        head="d" * 40,
        source_ref="feature-x",
        base="main",
        base_sha="e" * 40,
        target=route._PushTarget("origin", "https://ex.invalid/r.git"),
    )


def test_publish_refuses_a_detached_head(route, tmp_path: Path) -> None:
    kw = _publish_kwargs(route, tmp_path / "m.git")
    kw["source_ref"] = ""
    result = asyncio.run(route._publish(**kw))
    assert result.ok is False and result.code == "detached_head"


def test_publish_refuses_a_protected_source_ref(route, tmp_path: Path) -> None:
    kw = _publish_kwargs(route, tmp_path / "m.git")
    kw["source_ref"] = "main"
    result = asyncio.run(route._publish(**kw))
    assert result.ok is False and result.code == "protected_source_ref"


def test_publish_refuses_a_case_variant_protected_source_ref(route, tmp_path: Path) -> None:
    """A capitalised protected branch name must be refused the same as its lowercase form: the
    argv publish floor the gateway stands in front of matches the protected branch
    case-insensitively, so a case-sensitive gate here would let the gateway force-push onto a
    branch that floor protects. Pins the case-insensitive check in ``_publish``."""
    for variant in ("Main", "MASTER", "Mainline"):  # wokeignore:rule=master
        kw = _publish_kwargs(route, tmp_path / "m.git")
        kw["source_ref"] = variant
        result = asyncio.run(route._publish(**kw))
        assert (
            result.ok is False and result.code == "protected_source_ref"
        ), f"{variant} slipped the protected-branch gate"


def test_effective_push_target_refuses_a_leading_dash_remote_url(
    route, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A remote URL beginning with ``-`` reaches git as an OPTION, not a repository, on the
    credential-exempt gateway publish spawn (e.g. ``--upload-pack=<path>`` runs a chosen program
    at gateway privilege). The resolver refuses it at its single return.

    Mutation check: without the leading-dash guard the resolver returns the URL verbatim, so the
    ``unsafe_remote_url`` assertion fails and a real ``_PushTarget`` with the dash URL comes back.
    """

    async def _git(_wt, *args):  # type: ignore[no-untyped-def]
        # No pushRemote/pushDefault/branch remote configured -> defaults to "origin"; both the
        # fetch and the --push get-url resolve to the same leading-dash URL.
        if args[:2] == ("remote", "get-url"):
            return (0, "--upload-pack=/tmp/evil")
        return (1, "")

    monkeypatch.setattr(route, "_git", _git)
    target = asyncio.run(route._effective_push_target("/wt", "feature-x"))
    assert target.code == "unsafe_remote_url"
    assert target.url == ""


def test_effective_push_target_refuses_an_activated_install_with_no_pin(
    route, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On an ACTIVATED install with NO pinned destination, the resolver FAILS CLOSED.

    The push destination is resolved from the agent-writable worktree git config, so leaving it
    unconstrained on a gated install lets the credential-exempt gateway publish land the judged
    commit at any agent-chosen same-history repository -- the fail-OPEN this finding closes.
    Activation therefore REQUIRES the operator to pin a destination before publishing at all.

    Mutation check: this asserts on the NEW ``destination_not_pinned`` code. On un-fixed code
    (no activated-no-pin refusal) the resolver returns a usable ``_PushTarget`` with the
    agent-chosen URL and ``code == ""``, so both assertions below fail.
    """

    async def _git(_wt, *args):  # type: ignore[no-untyped-def]
        # No pushRemote/pushDefault/branch remote -> "origin"; fetch and push agree on a
        # perfectly ordinary URL, so the ONLY thing that can refuse here is the no-pin latch.
        if args[:2] == ("remote", "get-url"):
            return (0, "https://ex.invalid/agent-chosen.git")
        return (1, "")

    monkeypatch.setattr(route, "_git", _git)
    target = asyncio.run(
        route._effective_push_target("/wt", "feature-x", activated=True, pinned_push_url="")
    )
    assert target.code == "destination_not_pinned"
    assert target.url == ""


def test_effective_push_target_allows_an_activated_install_with_a_matching_pin(
    route, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pin that MATCHES the resolved push URL resolves normally -- pinning authorizes it."""

    async def _git(_wt, *args):  # type: ignore[no-untyped-def]
        if args[:2] == ("remote", "get-url"):
            return (0, "https://ex.invalid/pinned.git")
        return (1, "")

    monkeypatch.setattr(route, "_git", _git)
    target = asyncio.run(
        route._effective_push_target(
            "/wt", "feature-x", activated=True, pinned_push_url="https://ex.invalid/pinned.git"
        )
    )
    assert target.code == ""
    assert target.url == "https://ex.invalid/pinned.git"


def test_effective_push_target_refuses_a_local_destination_on_an_activated_install(
    route, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A LOCAL destination (bare path or ``file:``) is refused on an activated install.

    git publishes to a local repository with NO credentials, so the gateway's credential mask --
    its one lever for enforcing a verdict -- cannot bind the push; an opaque subprocess reaches
    the path directly, judged by nothing. The resolver refuses it at its single return.

    Mutation check: deleting the ``activated and _is_local_destination(push_url)`` guard lets the
    resolver fall through to ``_absolutize_local_remote`` and return a usable ``_PushTarget``
    with ``code == ""``, so both assertions fail.
    """

    async def _git(_wt, *args):  # type: ignore[no-untyped-def]
        # Resolve to a bare local path; the pin MATCHES it so the no-pin / pin-mismatch latches
        # pass and the ONLY thing that can refuse is the local-destination guard under test.
        if args[:2] == ("remote", "get-url"):
            return (0, "/srv/git/local-repo.git")
        return (1, "")

    monkeypatch.setattr(route, "_git", _git)
    # Neutralize the pre-existing symlink check: on Windows ``os.path.realpath`` prepends a drive
    # to a bare POSIX path so the lexical/real anchors differ and it reads as "symlinked" -- a
    # platform artifact of the synthetic path, unrelated to the guard under test. Stubbing it
    # isolates the ``activated`` local-destination rejection on both POSIX and Windows.
    monkeypatch.setattr(route, "_local_remote_is_symlinked", lambda *_a, **_k: False)
    target = asyncio.run(
        route._effective_push_target(
            "/wt", "feature-x", activated=True, pinned_push_url="/srv/git/local-repo.git"
        )
    )
    assert target.code == "unsafe_remote_url"
    assert target.url == ""


def test_effective_push_target_refuses_a_file_url_destination_on_an_activated_install(
    route, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ``file:`` spelling of a local destination is refused the same way on an activated
    install -- git treats ``file:///path`` as a local repository, credential-free, so it escapes
    the mask exactly as a bare path does."""

    async def _git(_wt, *args):  # type: ignore[no-untyped-def]
        if args[:2] == ("remote", "get-url"):
            return (0, "file:///srv/git/local-repo.git")
        return (1, "")

    monkeypatch.setattr(route, "_git", _git)
    monkeypatch.setattr(route, "_local_remote_is_symlinked", lambda *_a, **_k: False)
    target = asyncio.run(
        route._effective_push_target(
            "/wt", "feature-x", activated=True, pinned_push_url="file:///srv/git/local-repo.git"
        )
    )
    assert target.code == "unsafe_remote_url"
    assert target.url == ""


def test_effective_push_target_leaves_a_local_destination_alone_when_not_activated(
    route, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A NON-activated install is not gated: a local remote is NOT refused by the local guard.

    Mutation check: were the local-destination refusal keyed on locality alone rather than on
    ``activated``, this ordinary non-activated local resolve would be refused and fail.
    """

    async def _git(_wt, *args):  # type: ignore[no-untyped-def]
        if args[:2] == ("remote", "get-url"):
            return (0, "/srv/git/local-repo.git")
        return (1, "")

    monkeypatch.setattr(route, "_git", _git)
    # Same symlink-check neutralization as above: isolate the ``activated`` guard from the
    # platform-dependent realpath behaviour of the synthetic bare path.
    monkeypatch.setattr(route, "_local_remote_is_symlinked", lambda *_a, **_k: False)
    target = asyncio.run(
        route._effective_push_target("/wt", "feature-x", activated=False, pinned_push_url="")
    )
    # The local guard does not fire when not activated, so no ``unsafe_remote_url`` from it; the
    # path resolves through ``_absolutize_local_remote`` (code stays "").
    assert target.code == ""
    assert target.url.endswith("local-repo.git")


def test_effective_push_target_allows_a_network_remote_on_an_activated_install(
    route, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A network remote (https/ssh) is NOT local, so the local-destination guard leaves it
    alone even when activated -- the gateway owns its credentialed push."""

    async def _git(_wt, *args):  # type: ignore[no-untyped-def]
        if args[:2] == ("remote", "get-url"):
            return (0, "https://ex.invalid/net.git")
        return (1, "")

    monkeypatch.setattr(route, "_git", _git)
    target = asyncio.run(
        route._effective_push_target(
            "/wt", "feature-x", activated=True, pinned_push_url="https://ex.invalid/net.git"
        )
    )
    assert target.code == ""
    assert target.url == "https://ex.invalid/net.git"


def test_effective_push_target_refuses_an_activated_pin_mismatch(
    route, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pin SET but not matching the resolved URL keeps refusing (existing ``unpinned_destination``)."""

    async def _git(_wt, *args):  # type: ignore[no-untyped-def]
        if args[:2] == ("remote", "get-url"):
            return (0, "https://ex.invalid/agent-chosen.git")
        return (1, "")

    monkeypatch.setattr(route, "_git", _git)
    target = asyncio.run(
        route._effective_push_target(
            "/wt", "feature-x", activated=True, pinned_push_url="https://ex.invalid/pinned.git"
        )
    )
    assert target.code == "unpinned_destination"
    assert target.url == ""


def test_effective_push_target_leaves_a_non_activated_install_unconstrained(
    route, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A NON-activated install with no pin resolves normally: it is not gated, so its
    destination stays unconstrained exactly as before -- the requirement follows activation,
    it is not a default imposed on an install that never asked to be gated.

    Mutation check: were the no-pin refusal keyed on the pin alone rather than on ``activated``,
    this ordinary non-activated resolve would be refused ``destination_not_pinned`` and fail.
    """

    async def _git(_wt, *args):  # type: ignore[no-untyped-def]
        if args[:2] == ("remote", "get-url"):
            return (0, "https://ex.invalid/repo.git")
        return (1, "")

    monkeypatch.setattr(route, "_git", _git)
    target = asyncio.run(
        route._effective_push_target("/wt", "feature-x", activated=False, pinned_push_url="")
    )
    assert target.code == ""
    assert target.url == "https://ex.invalid/repo.git"


def test_publish_refuses_when_head_is_unreadable(
    route, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def _git(_wt, *args):  # type: ignore[no-untyped-def]
        return (2, "")  # HEAD re-read fails

    monkeypatch.setattr(route, "_git", _git)
    result = asyncio.run(route._publish(**_publish_kwargs(route, tmp_path / "m.git")))
    assert result.ok is False and result.code == "head_unreadable"


def test_publish_refuses_when_the_base_advanced(
    route, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """HEAD still matches and the target is unchanged, but the base tip moved -> base_moved."""

    async def _git(_wt, *args):  # type: ignore[no-untyped-def]
        if "rev-parse" in args:
            return (0, "d" * 40)  # HEAD unchanged
        return (0, "")

    async def _target(_wt, _src, **_kw):  # type: ignore[no-untyped-def]
        return route._PushTarget("origin", "https://ex.invalid/r.git")

    async def _remote_tip(_mirror, _url, ref):  # type: ignore[no-untyped-def]
        # The base advanced from the judged tip; the source-ref lease read is never reached.
        return (0, "f" * 40) if ref == "main" else (0, "")

    monkeypatch.setattr(route, "_git", _git)
    monkeypatch.setattr(route, "_effective_push_target", _target)
    monkeypatch.setattr(route, "_remote_tip", _remote_tip)
    result = asyncio.run(route._publish(**_publish_kwargs(route, tmp_path / "m.git")))
    assert result.ok is False and result.code == "base_moved"


def test_publish_refuses_when_the_base_tip_is_unreadable(
    route, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def _git(_wt, *args):  # type: ignore[no-untyped-def]
        return (0, "d" * 40) if "rev-parse" in args else (0, "")

    async def _target(_wt, _src, **_kw):  # type: ignore[no-untyped-def]
        return route._PushTarget("origin", "https://ex.invalid/r.git")

    async def _remote_tip(_mirror, _url, _ref):  # type: ignore[no-untyped-def]
        return (2, "")  # cannot read the base tip

    monkeypatch.setattr(route, "_git", _git)
    monkeypatch.setattr(route, "_effective_push_target", _target)
    monkeypatch.setattr(route, "_remote_tip", _remote_tip)
    result = asyncio.run(route._publish(**_publish_kwargs(route, tmp_path / "m.git")))
    assert result.ok is False and result.code == "base_unreadable"


def test_publish_refuses_when_the_source_lease_is_unreadable(
    route, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def _git(_wt, *args):  # type: ignore[no-untyped-def]
        return (0, "d" * 40) if "rev-parse" in args else (0, "")

    async def _target(_wt, _src, **_kw):  # type: ignore[no-untyped-def]
        return route._PushTarget("origin", "https://ex.invalid/r.git")

    async def _remote_tip(_mirror, _url, ref):  # type: ignore[no-untyped-def]
        # The base matches the judged tip, but the source-ref lease read fails.
        return (0, "e" * 40) if ref == "main" else (2, "")

    monkeypatch.setattr(route, "_git", _git)
    monkeypatch.setattr(route, "_effective_push_target", _target)
    monkeypatch.setattr(route, "_remote_tip", _remote_tip)
    result = asyncio.run(route._publish(**_publish_kwargs(route, tmp_path / "m.git")))
    assert result.ok is False and result.code == "remote_unreadable"


def test_publish_pushes_and_reports_a_failed_push(
    route, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """All re-checks pass; the final force-with-lease push itself fails -> push_failed."""

    async def _git(_wt, *args):  # type: ignore[no-untyped-def]
        return (0, "d" * 40) if "rev-parse" in args else (0, "")

    async def _target(_wt, _src, **_kw):  # type: ignore[no-untyped-def]
        return route._PushTarget("origin", "https://ex.invalid/r.git")

    async def _remote_tip(_mirror, _url, ref):  # type: ignore[no-untyped-def]
        return (0, "e" * 40) if ref == "main" else (0, "f" * 40)

    async def _run_git(argv, **_kwargs):  # type: ignore[no-untyped-def]
        return (1, "remote rejected") if "push" in argv else (0, "")

    monkeypatch.setattr(route, "_git", _git)
    monkeypatch.setattr(route, "_effective_push_target", _target)
    monkeypatch.setattr(route, "_remote_tip", _remote_tip)
    monkeypatch.setattr(route, "_run_git", _run_git)
    result = asyncio.run(route._publish(**_publish_kwargs(route, tmp_path / "m.git")))
    assert result.ok is False and result.code == "push_failed"


def test_publish_does_not_lease_the_unpushed_base_ref(
    route, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The push leases only the SOURCE ref it actually updates. ``git push`` enforces a
    ``--force-with-lease`` only for a ref the push writes, so a lease on the un-pushed base ref
    is silently ignored -- it must not be emitted, because false protection is worse than none.
    The base is gated by the ``base_now`` re-read in ``_publish`` instead. Pins that the base
    appears in neither a lease nor the refspec of the push argv."""
    captured: list[list[str]] = []

    async def _git(_wt, *args):  # type: ignore[no-untyped-def]
        return (0, "d" * 40) if "rev-parse" in args else (0, "")

    async def _target(_wt, _src, **_kw):  # type: ignore[no-untyped-def]
        return route._PushTarget("origin", "https://ex.invalid/r.git")

    async def _remote_tip(_mirror, _url, ref):  # type: ignore[no-untyped-def]
        return (0, "e" * 40) if ref == "main" else (0, "f" * 40)

    async def _run_git(argv, **_kwargs):  # type: ignore[no-untyped-def]
        if "push" in argv:
            captured.append(list(argv))
        return (0, "")

    monkeypatch.setattr(route, "_git", _git)
    monkeypatch.setattr(route, "_effective_push_target", _target)
    monkeypatch.setattr(route, "_remote_tip", _remote_tip)
    monkeypatch.setattr(route, "_run_git", _run_git)
    result = asyncio.run(route._publish(**_publish_kwargs(route, tmp_path / "m.git")))
    assert result.ok is True and result.code == "published"
    assert len(captured) == 1, "expected exactly one push"
    push = captured[0]
    # Source lease pins the source ref to the tip the gateway just read.
    assert f"--force-with-lease=refs/heads/feature-x:{'f' * 40}" in push, push
    # The base ref is neither leased nor pushed.
    assert not any("refs/heads/main" in arg for arg in push), push


def test_publish_succeeds_when_every_recheck_holds(
    route, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def _git(_wt, *args):  # type: ignore[no-untyped-def]
        return (0, "d" * 40) if "rev-parse" in args else (0, "")

    async def _target(_wt, _src, **_kw):  # type: ignore[no-untyped-def]
        return route._PushTarget("origin", "https://ex.invalid/r.git")

    async def _remote_tip(_mirror, _url, ref):  # type: ignore[no-untyped-def]
        return (0, "e" * 40) if ref == "main" else (0, "f" * 40)

    async def _run_git(argv, **_kwargs):  # type: ignore[no-untyped-def]
        return (0, "pushed") if "push" in argv else (0, "")

    monkeypatch.setattr(route, "_git", _git)
    monkeypatch.setattr(route, "_effective_push_target", _target)
    monkeypatch.setattr(route, "_remote_tip", _remote_tip)
    monkeypatch.setattr(route, "_run_git", _run_git)
    result = asyncio.run(route._publish(**_publish_kwargs(route, tmp_path / "m.git")))
    assert result.ok is True and result.code == "published"


# ── _run_guard: the guard-untrusted and mirror-unresolved branches ──


def test_run_guard_refuses_when_the_guard_is_untrusted(
    route, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unvouched guard means no trustworthy verdict: rc 2, no mirror."""

    def _snapshot(_digest):  # type: ignore[no-untyped-def]
        raise route._GuardUntrusted("no pin")

    monkeypatch.setattr(route, "_guard_snapshot", _snapshot)
    run = asyncio.run(route._run_guard("/wt", "main", common_dir="/g", digest="", url="u"))
    assert run.rc == 2 and run.mirror is None and "no pin" in run.output


def test_run_guard_reports_an_io_failure_establishing_its_copy(
    route, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _snapshot(_digest):  # type: ignore[no-untyped-def]
        raise OSError("disk gone")

    monkeypatch.setattr(route, "_guard_snapshot", _snapshot)
    run = asyncio.run(route._run_guard("/wt", "main", common_dir="/g", digest="c" * 64, url="u"))
    assert run.rc == 2 and run.mirror is None and "own copy" in run.output


def test_run_guard_refuses_when_the_mirror_cannot_resolve_the_pair(
    route, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A primed mirror that resolves neither ref refuses rather than judging nothing."""

    def _snapshot(_digest):  # type: ignore[no-untyped-def]
        return tmp_path / "guard.py"

    async def _prime(*_a, **_k):  # type: ignore[no-untyped-def]
        return (0, "")

    async def _resolve(_mirror, _ref):  # type: ignore[no-untyped-def]
        return ""  # neither the candidate nor the base resolves

    monkeypatch.setattr(route, "_guard_snapshot", _snapshot)
    monkeypatch.setattr(route, "_prime_mirror", _prime)
    monkeypatch.setattr(route, "_resolve", _resolve)
    monkeypatch.setattr(route, "_mirror_for", lambda _g: tmp_path / "m.git")
    run = asyncio.run(route._run_guard("/wt", "main", common_dir="/g", digest="c" * 64, url="u"))
    assert run.rc == 2 and "could not resolve" in run.output
    # The refs ride along so the caller can clean them up.
    assert run.refs is not None


# ── _publish F2: the remote source_ref tip must be an ancestor of the candidate ──
#
# A fresh ``--force-with-lease`` against the tip the gateway itself just read matches even when
# the candidate does NOT descend from that tip, degrading to a plain force that overwrites a
# collaborator's unseen commit. ``_publish`` now refuses with ``source_diverged`` unless the
# remote tip is an ancestor of the candidate (fast-forward). Every case fixes the earlier
# re-checks (HEAD unmoved, base unmoved) so the ancestry check is the sole decider.


def _f2_common(route, monkeypatch, *, source_tip: str):
    """Stub the pre-ancestry re-checks green, with the remote source_ref tip = ``source_tip``."""

    async def _git(_wt, *args):  # type: ignore[no-untyped-def]
        return (0, "d" * 40) if "rev-parse" in args else (0, "")

    async def _target(_wt, _src, **_kw):  # type: ignore[no-untyped-def]
        return route._PushTarget("origin", "https://ex.invalid/r.git")

    async def _remote_tip(_mirror, _url, ref):  # type: ignore[no-untyped-def]
        return (0, "e" * 40) if ref == "main" else (0, source_tip)

    monkeypatch.setattr(route, "_git", _git)
    monkeypatch.setattr(route, "_effective_push_target", _target)
    monkeypatch.setattr(route, "_remote_tip", _remote_tip)


def test_publish_refuses_when_the_candidate_does_not_descend_from_the_remote_tip(
    route, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A non-empty remote tip that merge-base says is NOT an ancestor -> source_diverged."""
    _f2_common(route, monkeypatch, source_tip="f" * 40)

    async def _run_git(argv, **_kwargs):  # type: ignore[no-untyped-def]
        if "merge-base" in argv:
            return (1, "")  # NOT an ancestor: the candidate diverged from the remote tip
        return (0, "")  # the fetch (and any push, which must never be reached) succeed

    monkeypatch.setattr(route, "_run_git", _run_git)
    result = asyncio.run(route._publish(**_publish_kwargs(route, tmp_path / "m.git")))
    assert result.ok is False and result.code == "source_diverged"


def test_publish_refuses_when_the_remote_tip_cannot_be_fetched(
    route, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An unfetchable remote tip cannot be proven an ancestor, so it is treated as diverged."""
    _f2_common(route, monkeypatch, source_tip="f" * 40)

    async def _run_git(argv, **_kwargs):  # type: ignore[no-untyped-def]
        if "fetch" in argv:
            return (1, "could not fetch")  # cannot bring the remote tip into the mirror
        return (0, "")

    monkeypatch.setattr(route, "_run_git", _run_git)
    result = asyncio.run(route._publish(**_publish_kwargs(route, tmp_path / "m.git")))
    assert result.ok is False and result.code == "source_diverged"


def test_publish_proceeds_when_the_remote_tip_is_an_ancestor(
    route, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A remote tip merge-base confirms IS an ancestor is a fast-forward: the push proceeds."""
    _f2_common(route, monkeypatch, source_tip="f" * 40)
    seen: list = []

    async def _run_git(argv, **_kwargs):  # type: ignore[no-untyped-def]
        seen.append(argv)
        if "merge-base" in argv:
            return (0, "")  # ancestor: safe fast-forward
        if "push" in argv:
            return (0, "pushed")
        return (0, "")

    monkeypatch.setattr(route, "_run_git", _run_git)
    result = asyncio.run(route._publish(**_publish_kwargs(route, tmp_path / "m.git")))
    assert result.ok is True and result.code == "published"
    assert any("push" in a for a in seen), "an ancestor tip must let the push run"


def test_publish_skips_the_ancestry_check_for_a_new_branch(
    route, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An empty remote tip is a new branch with nothing to overwrite: no ancestry check, push."""
    _f2_common(route, monkeypatch, source_tip="")  # new branch
    seen: list = []

    async def _run_git(argv, **_kwargs):  # type: ignore[no-untyped-def]
        seen.append(argv)
        return (0, "pushed") if "push" in argv else (0, "")

    monkeypatch.setattr(route, "_run_git", _run_git)
    result = asyncio.run(route._publish(**_publish_kwargs(route, tmp_path / "m.git")))
    assert result.ok is True and result.code == "published"
    assert not any("merge-base" in a for a in seen), "an empty tip must skip the ancestry check"


# ── _run_git F3: the gateway buffer is bounded, so a huge remote reply cannot OOM it ──


def test_run_git_truncates_output_past_the_cap(route, monkeypatch: pytest.MonkeyPatch) -> None:
    """A child that emits more than ``_MAX_GIT_OUTPUT_BYTES`` is killed with a truncation notice."""
    huge = b"x" * (route._MAX_GIT_OUTPUT_BYTES + 4096)
    proc = _FakeProc(0, huge)
    _stub_spawn(route, monkeypatch, proc)
    rc, out = asyncio.run(route._run_git(["git", "fetch"], visible=(), timeout=5))
    assert rc != 0
    assert "exceeded" in out and "truncated" in out
    assert proc.killed is True, "the over-cap child must be killed, not left buffering"


def test_run_git_returns_normally_for_small_output(route, monkeypatch: pytest.MonkeyPatch) -> None:
    """The common small-output path is unchanged: the child's rc and stripped stdout come back."""
    _stub_spawn(route, monkeypatch, _FakeProc(0, b"  small ok \n"))
    rc, out = asyncio.run(route._run_git(["git", "status"], visible=(), timeout=5))
    assert rc == 0 and out == "small ok"


def test_run_git_output_exactly_at_the_cap_is_not_truncated(
    route, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Output of exactly the cap size is legitimate and returns normally (boundary)."""
    at_cap = b"y" * route._MAX_GIT_OUTPUT_BYTES
    proc = _FakeProc(0, at_cap)
    _stub_spawn(route, monkeypatch, proc)
    rc, out = asyncio.run(route._run_git(["git", "fetch"], visible=(), timeout=5))
    assert rc == 0
    assert "truncated" not in out
    assert proc.killed is False


def test_absolutize_local_remote_anchors_a_relative_path_to_the_worktree() -> None:
    """A RELATIVE local remote path is resolved against the worktree, not the gateway CWD (F3).

    The mirror fetches and the publish pass the remote URL positionally with no ``-C worktree``
    (they run ``--git-dir <mirror>``), so a git process resolves a relative local path against
    the gateway's own working directory. That lets a pinned relative remote name one repository
    at compare time and force-push a DIFFERENT one at publish time. The resolver now anchors a
    relative local path to the worktree so compare and use name the same repository.
    """
    import os

    from kiro_crew.dashboard.handlers import push_verdict as route

    wt = os.path.join(os.sep, "srv", "wt")  # OS-native worktree root (nonexistent -> no symlinks)
    # A bare relative path is anchored to the worktree and CANONICALIZED (realpath). For a
    # nonexistent path there are no symlinks to follow, so realpath is the lexical normpath.
    assert route._absolutize_local_remote(os.path.join("..", "peer.git"), wt) == os.path.realpath(
        os.path.join(wt, "..", "peer.git")
    )
    assert route._absolutize_local_remote(os.path.join("sub", "repo.git"), wt) == os.path.realpath(
        os.path.join(wt, "sub", "repo.git")
    )
    # An absolute local path is canonicalized to its realpath (identity is the real target).
    abs_local = os.path.join(os.sep, "srv", "abs", "repo.git")
    assert os.path.isabs(abs_local)
    assert route._absolutize_local_remote(abs_local, wt) == os.path.realpath(abs_local)
    # A scheme URL is a remote identity, never gateway-relative -> untouched.
    assert (
        route._absolutize_local_remote("https://h.invalid/r.git", wt) == "https://h.invalid/r.git"
    )
    assert (
        route._absolutize_local_remote("ssh://git@h.invalid/r.git", wt)
        == "ssh://git@h.invalid/r.git"
    )
    # scp-like ``user@host:path`` is a remote identity -> untouched.
    assert (
        route._absolutize_local_remote("git@h.invalid:path/r.git", wt) == "git@h.invalid:path/r.git"
    )
    # A ``file:`` URL is a LOCAL repository, NOT a pass-through scheme (GPT F2): it is unwrapped
    # to its path and canonicalized exactly like a bare absolute local path, so the same real
    # target is compared and pushed.
    file_url = "file://" + abs_local.replace(os.sep, "/")
    assert route._absolutize_local_remote(file_url, wt) == os.path.realpath(abs_local)
    assert route._absolutize_local_remote(
        "file://localhost" + abs_local.replace(os.sep, "/"), wt
    ) == os.path.realpath(abs_local)


def test_redacted_output_redacts_before_clipping_the_tail() -> None:
    """A credential near the clip boundary is redacted, not sliced out of the redactor's reach.

    ``_redacted_output`` redacts the WHOLE string and takes the clip to the last 2000 chars
    AFTER it. A clip taken first would hand the redactor only the fragment inside the tail, so a
    secret whose label/prefix falls outside it -- or that straddles the boundary -- would reach
    the model unmatched. This plants an AWS-key-shaped token straddling the clip boundary of a
    >2000-char body and asserts no fragment of the raw token is in the returned (clipped) detail.
    """
    from kiro_crew.dashboard.handlers import push_verdict as route

    secret = "AKIA" + "Q" * 16  # 20 chars
    # Straddle the clip boundary: the token begins 10 chars before the last-2000 cut, so a
    # clip-THEN-redact order would hand the redactor only the token's second half (a fragment
    # that matches nothing) and the surviving prefix would ship. Redact-THEN-clip catches it.
    prefix = "x" * (4000)
    tail = "y" * 1990  # keeps total > 2000 and puts the token's start just outside the tail
    body = prefix + secret + tail
    out = route._redacted_output(body)
    assert len(out) <= 2000
    # No fragment of the raw token survives (neither the whole token nor its 2nd half).
    assert secret not in out
    assert secret[10:] not in out
    # The empty-string contract is preserved.
    assert route._redacted_output("") == ""


def test_local_remote_symlink_is_detected_and_scheme_remotes_are_not(tmp_path) -> None:
    """A local destination reached through a symlink is flagged; real dirs and remotes are not.

    Codex F1: the destination pin comes from agent-writable config and the worktree is
    agent-writable, so a planted symlink could repoint a pinned local path to a repository the
    operator never authorized. ``_local_remote_is_symlinked`` compares the lexical anchor to its
    realpath so any traversed symlink (including the final component) is caught; the resolver
    refuses it. A genuine directory and any scheme/scp remote are NOT flagged.
    """
    import sys

    import pytest

    from kiro_crew.dashboard.handlers import push_verdict as route

    wt = str(tmp_path)
    real = tmp_path / "real.git"
    real.mkdir()
    # A real directory (not a symlink) is never flagged, on every platform.
    assert route._local_remote_is_symlinked("real.git", wt) is False
    assert route._local_remote_is_symlinked(str(real), wt) is False
    # Scheme and scp-like remotes are never local paths -> never flagged, on every platform.
    assert route._local_remote_is_symlinked("https://h.invalid/r.git", wt) is False
    assert route._local_remote_is_symlinked("ssh://git@h.invalid/r.git", wt) is False
    assert route._local_remote_is_symlinked("git@h.invalid:path/r.git", wt) is False

    # The symlink-detection half needs a symlink the OS actually creates AND that realpath
    # resolves the same way this helper expects. Windows CI symlink privilege and realpath
    # case/short-path normalization make that unreliable, and the threat this closes is a
    # POSIX agent-planted symlink (the production check is os.path.realpath, cross-platform),
    # so run the link assertions on POSIX only.
    if sys.platform == "win32":
        pytest.skip("symlink-detection assertions are POSIX-only (Windows realpath/privilege)")
    link = tmp_path / "link.git"
    try:
        link.symlink_to(real)
    except (OSError, NotImplementedError):
        pytest.skip("platform cannot create a symlink for this test")
    # A symlinked local destination (relative or absolute) is flagged.
    assert route._local_remote_is_symlinked("link.git", wt) is True
    assert route._local_remote_is_symlinked(str(link), wt) is True
    # A ``file:`` URL naming the symlinked destination is ALSO flagged (GPT F2): file: is a
    # local repository, so the symlink refusal that exists for local destinations applies to
    # this spelling too instead of being bypassed as a "scheme".
    assert route._local_remote_is_symlinked("file://" + str(link).replace(os.sep, "/"), wt) is True
    # ...and a file: URL at the REAL (non-symlinked) directory is not flagged.
    assert route._local_remote_is_symlinked("file://" + str(real).replace(os.sep, "/"), wt) is False


def test_prime_mirror_gives_the_gateway_a_writable_view_of_its_own_mirror(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """The mirror-writing spawns carry the mirror as ``writable`` (Opus mirror-sealed finding).

    ``push-verdict-mirrors`` is a crew-home readonly leaf the sandbox seals against every agent
    subprocess, so ``git init --bare`` and the mirror fetches would fail EROFS and no verdict
    could EVER be issued. The one trusted gateway spawn that owns the mirror passes its leaf as
    ``writable`` -> ``extra_writable_dirs`` (a scoped carve-out inside the readonly guard).
    Without it, every push-verdict request errors.
    """
    import asyncio

    from kiro_crew.dashboard.handlers import push_verdict as route

    writables: list[tuple[str, ...]] = []

    async def _fake_run_git(argv, *, visible, timeout, env=None, writable=()):  # type: ignore[no-untyped-def]
        writables.append(tuple(writable))
        return 0, ""

    monkeypatch.setattr(route, "_run_git", _fake_run_git)
    mirror = tmp_path / "push-verdict-mirrors" / "d.git"
    refs = route._JudgementRefs("refs/x/base", "refs/x/cand")
    rc, _ = asyncio.run(route._prime_mirror("/srv/wt", mirror, "main", "/srv/repo.git", refs))
    assert rc == 0
    # Every mirror-writing spawn named a non-empty writable carve-out; none ran without one.
    assert writables, "no spawn was made"
    assert all(w for w in writables), f"a mirror-writing spawn had no writable carve: {writables}"
    # The carve-out is the mirror (or its parent for the create), not some unrelated path.
    assert all(
        any(str(mirror).startswith(p) or str(mirror.parent) == p for p in w) for w in writables
    ), writables
