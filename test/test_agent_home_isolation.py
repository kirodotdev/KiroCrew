"""Agent-spec home isolation — the KIRO_HOME seam, the worktree guard, and the
anti-regression guard that keeps ``~/.kiro/agents`` from being hard-coded again.

Regression context: a gateway booted from a linked git worktree rewrote the
machine-wide ``~/.kiro/agents/*.json`` on startup, stamping its own ``.venv``
binary into every managed server's ``command`` and its own data home into their
``env``. The real install's MCP servers then ran the worktree's code and read the
worktree's credential while still calling the live gateway, so every managed MCP
call returned HTTP 403 — and once the worktree was removed those specs pointed at
paths that were gone.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import make_dir_link
from kiro_crew.config.paths import kiro_agents_dir, kiro_home

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src" / "kiro_crew"


# --------------------------------------------------------------------------
# The KIRO_HOME seam
# --------------------------------------------------------------------------
def _no_overrides(monkeypatch) -> None:
    """No KIRO_HOME and no KIROCREW_HOME — the plain-install baseline.

    Both must be cleared: with a KIROCREW_HOME override active AND the code
    running from a worktree (which is how this repo is developed), the derived
    isolated home legitimately kicks in.
    """
    monkeypatch.delenv("KIRO_HOME", raising=False)
    monkeypatch.delenv("KIROCREW_HOME", raising=False)
    monkeypatch.delenv("KIROCREW_POD", raising=False)


def test_kiro_home_defaults_to_dot_kiro(monkeypatch, unpinned_agent_spec_home):
    """The shipped default, with the suite's agents-dir pin lifted.

    ``kiro_agents_dir()`` honours ``config.paths._agents_dir_override``, which the
    host-mutation floor installs for every test, so this assertion is about the
    resolver's own default rather than what a test run resolves.
    """
    _no_overrides(monkeypatch)
    assert kiro_home() == Path.home() / ".kiro"
    assert kiro_agents_dir() == Path.home() / ".kiro" / "agents"


def test_kiro_home_honors_override(monkeypatch, tmp_path):
    monkeypatch.setenv("KIRO_HOME", str(tmp_path / "pod-kiro"))
    assert kiro_home() == (tmp_path / "pod-kiro").resolve()
    assert kiro_agents_dir() == (tmp_path / "pod-kiro").resolve() / "agents"


def test_kiro_home_expands_user(monkeypatch):
    monkeypatch.delenv("KIROCREW_HOME", raising=False)
    monkeypatch.setenv("KIRO_HOME", "~/some-kiro-home")
    assert kiro_home() == (Path.home() / "some-kiro-home").resolve()


def test_kiro_home_refuses_a_root(monkeypatch):
    """The root check is portable: a root is its own parent on every OS.

    On Windows a bare "/" resolves to the current DRIVE root (``C:\\``), which is
    still its own parent, so the same assertion holds without special-casing.
    """
    _no_overrides(monkeypatch)
    monkeypatch.setenv("KIRO_HOME", "/")
    assert kiro_home() == Path.home() / ".kiro"


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="/usr, /etc, /System are POSIX system dirs; on Windows they resolve to "
    "ordinary per-drive folders (D:/usr) and are not privileged",
)
@pytest.mark.parametrize("bad", ["/usr", "/etc", "/System"])
def test_kiro_home_refuses_posix_system_dirs(monkeypatch, bad):
    """A POSIX system dir must degrade to the default, never be written into."""
    _no_overrides(monkeypatch)
    monkeypatch.setenv("KIRO_HOME", bad)
    assert kiro_home() == Path.home() / ".kiro"


def test_kiro_home_matches_kirocrew_home_safety_rules():
    """Both overrides share one predicate, so they refuse the same targets."""
    from kiro_crew.config.paths import _is_unsafe_home

    # Portable on every OS: a root is its own parent.
    assert _is_unsafe_home(Path(Path("/").resolve().anchor))
    if sys.platform != "win32":
        assert _is_unsafe_home(Path("/usr"))
    assert not _is_unsafe_home(Path.home() / ".kiro")


@pytest.mark.skipif(sys.platform != "darwin", reason="/etc -> /private/etc is a macOS symlink")
def test_macos_private_etc_is_refused():
    """The RESOLVED spelling of /etc must be refused, not just the literal one.

    Regression test: callers resolve() the override before handing it here, and
    on macOS ``/etc`` resolves to ``/private/etc`` — whose first two components
    are ``("/", "private")``. A guard that only knew ``("/", "etc")`` therefore
    accepted ``KIRO_HOME=/etc`` and would create agent JSON inside a system
    directory.
    """
    from kiro_crew.config.paths import _is_unsafe_home

    assert _is_unsafe_home(Path("/etc").resolve())
    assert _is_unsafe_home(Path("/private/etc"))
    # The whole TREE, not just the bare directory: ("/", "etc") is already a
    # prefix match on Linux, so refusing only the exact resolved path would let
    # KIROCREW_HOME=/etc/kirocrew through on macOS alone — the two platforms
    # would disagree about the same override.
    assert _is_unsafe_home(Path("/etc/kirocrew").resolve())
    assert _is_unsafe_home(Path("/private/etc/kirocrew"))
    assert _is_unsafe_home(Path("/private/etc/foo/bar"))


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX tempdir layout")
def test_temp_dir_home_is_still_allowed():
    """The /etc fix must not refuse a temp-dir home.

    On macOS ``tempfile.gettempdir()`` resolves under ``/private/var/folders/...``,
    so refusing the whole ``/private`` tree would reject every temp-dir data home
    — which tests, pods and worktree previews all rely on.
    """
    import tempfile

    from kiro_crew.config.paths import _is_unsafe_home

    assert not _is_unsafe_home(Path(tempfile.gettempdir()).resolve())


def test_under_system_tmp_answers_on_the_call_time_root():
    """``_under_system_tmp`` covers the temp root and its subtrees, nothing else.

    The DATA-home predicate above deliberately allows a temp-dir home; this
    CHECKOUT predicate deliberately condemns a temp-dir checkout. Both answer
    against ``tempfile.gettempdir()`` as configured at call time.
    """
    import tempfile

    from kiro_crew.config.paths import _under_system_tmp

    root = Path(tempfile.gettempdir()).resolve()
    assert _under_system_tmp(root)
    assert _under_system_tmp(root / "kc-task-1234" / "repo" / "src")
    assert not _under_system_tmp(Path("/durable-install/KiroCrew").resolve())


# --------------------------------------------------------------------------
# The worktree decline guard
# --------------------------------------------------------------------------
def _make_linked_worktree(tmp_path: Path) -> Path:
    """A directory whose ``.git`` is a linked-worktree gitdir pointer file."""
    wt = tmp_path / "kirocrew-wt-example"
    (wt / "src" / "kiro_crew").mkdir(parents=True)
    (wt / ".git").write_text(
        "gitdir: /somewhere/KiroCrew/.git/worktrees/kirocrew-wt-example\n",
        encoding="utf-8",
    )
    return wt


def _pretend_target_is_shared(monkeypatch, agent_mod, agents_dir: Path) -> None:
    """Present *agents_dir* as BOTH the write target and what the AMBIENT
    environment resolves — which is what makes a target "shared".

    A target the ambient environment would never produce is by definition private
    to whoever redirected it, so a test must line the two up to exercise the
    guard.

    The ambient side is ``config.paths.ambient_agents_dir``, patched at its
    DEFINITION: it is the override-blind resolver the guard reads, and ``agent``
    binds it by name so a module-attribute patch on ``agent`` would be a second
    copy that the guard's own call still ignores. ``KIRO_AGENTS_DIR`` stays the
    target side because ``kiro_agents_dir_path()`` prefers it.
    """
    monkeypatch.setattr(agent_mod, "KIRO_AGENTS_DIR", agents_dir)
    monkeypatch.setattr(agent_mod, "ambient_agents_dir", lambda: agents_dir)


@pytest.fixture(autouse=True)
def _pin_default_home_and_breadcrumb(monkeypatch, tmp_path):
    """Keep every test in this module off the operator's real data home.

    The guard's writability probe compares the override against the DEFAULT
    home, and several tests exercise the default path outright (no override),
    so production would resolve — and create — the real ``~/.kiro/crew`` plus
    the ``~/.kirocrew.breadcrumb`` beside it. Conftest's real-home ratchet
    fails such tests at teardown, and its breadcrumb guard fails them at write
    time. None of these tests is ABOUT the breadcrumb or the real default, so
    the whole module pins the default to tmp and stubs the breadcrumb writer,
    the two remedies the ratchet's messages prescribe.
    """
    from kiro_crew.config import paths

    monkeypatch.setattr(paths, "_resolved_home", None, raising=False)
    monkeypatch.setattr(paths, "_resolve_default_home", lambda: tmp_path / "pinned-default-home")
    monkeypatch.setattr(paths, "_write_recovery_breadcrumb", lambda data_home: None, raising=False)


def test_private_target_is_never_declined(monkeypatch, tmp_path):
    """A pod/test target is private, so a worktree may write it freely.

    This is what keeps the guard from breaking KiroCrew's own suite: development
    happens in worktrees by hard rule, and those tests write to ``tmp_path``.
    """
    from kiro_crew import agent

    monkeypatch.delenv("KIRO_HOME", raising=False)
    wt = _make_linked_worktree(tmp_path)
    monkeypatch.setattr(agent, "__file__", str(wt / "src" / "kiro_crew" / "agent.py"))
    # Target the ambient environment would never produce -> private by definition.
    monkeypatch.setattr(agent, "KIRO_AGENTS_DIR", tmp_path / "private" / "agents")
    monkeypatch.setattr(agent, "kiro_agents_dir", lambda: tmp_path / "elsewhere")

    assert agent._decline_shared_agent_home() is None


def test_symlinked_shared_home_still_declines(monkeypatch, tmp_path):
    """A symlinked shared home must NOT be mistaken for a private target.

    Regression: the target comparison was lexical, so a symlinked ``~/.kiro``
    (or a ``KIRO_HOME`` spelling the same directory differently) compared unequal
    to the default and waved the worktree through — overwriting exactly the specs
    the guard exists to protect.

    The link is a DIRECTORY link, so on Windows it is a junction (no privilege
    needed, and resolved by the same ``resolve()`` the guard relies on) — keeping
    this regression exercised there via ``make_dir_link`` rather than skipped.
    """
    from kiro_crew import agent

    real = tmp_path / "real-kiro" / "agents"
    real.mkdir(parents=True)
    link = tmp_path / "linked-kiro"
    make_dir_link(link, tmp_path / "real-kiro")

    wt = _make_linked_worktree(tmp_path)
    monkeypatch.setattr(agent, "__file__", str(wt / "src" / "kiro_crew" / "agent.py"))
    # Same directory, two spellings: the target is reached through the symlink,
    # the machine-wide default through the real path.
    monkeypatch.setattr(agent, "KIRO_AGENTS_DIR", link / "agents")
    monkeypatch.setattr(agent, "ambient_agents_dir", lambda: real)

    assert (
        agent._decline_shared_agent_home() is not None
    ), "symlinked shared home was treated as private — guard bypassed"


def test_declines_when_running_from_worktree_without_kiro_home(monkeypatch, tmp_path):
    from kiro_crew import agent

    monkeypatch.delenv("KIRO_HOME", raising=False)
    wt = _make_linked_worktree(tmp_path)
    monkeypatch.setattr(agent, "__file__", str(wt / "src" / "kiro_crew" / "agent.py"))
    _pretend_target_is_shared(monkeypatch, agent, tmp_path / "agents")

    declined = agent._decline_shared_agent_home()
    assert declined is not None
    assert declined.name == agent.AGENT_FILENAME


def test_agent_home_inside_own_data_home_is_private(monkeypatch, tmp_path):
    """The supported opt-in: put the agent home INSIDE this instance's data home.

    That is provable privacy — the instance's own teardown owns the directory — so
    an ephemeral instance may write it freely.
    """
    from kiro_crew import agent
    from kiro_crew.config.paths import isolated_agents_dir

    own_home = tmp_path / "wt" / ".kirocrew-dev"
    own_home.mkdir(parents=True)
    agents = isolated_agents_dir(own_home)

    wt = _make_linked_worktree(tmp_path)
    monkeypatch.setattr(agent, "__file__", str(wt / "src" / "kiro_crew" / "agent.py"))
    monkeypatch.setenv("KIROCREW_HOME", str(own_home))
    monkeypatch.setenv("KIRO_HOME", str(own_home / "kiro"))
    _pretend_target_is_shared(monkeypatch, agent, agents)

    assert agent._decline_shared_agent_home() is None


def test_data_home_that_is_an_ancestor_does_not_make_shared_private(monkeypatch, tmp_path):
    """Closed bypass: an ANCESTOR data home must not make the shared dir private.

    Regression: the privacy test was ``target.is_relative_to(own_home)``. With
    ``KIROCREW_HOME=$HOME`` the machine-wide ``~/.kiro/agents`` sits beneath the
    data home, so it read as private and a worktree gateway was handed the very
    specs the guard exists to protect. The exemption is now an EXACT match on the
    dedicated ``<data home>/kiro/agents``.
    """
    from kiro_crew import agent

    fake_home = tmp_path / "home"
    shared = fake_home / ".kiro" / "agents"
    shared.mkdir(parents=True)

    wt = _make_linked_worktree(tmp_path)
    monkeypatch.setattr(agent, "__file__", str(wt / "src" / "kiro_crew" / "agent.py"))
    monkeypatch.delenv("KIRO_HOME", raising=False)
    monkeypatch.delenv("KIROCREW_POD", raising=False)
    # The data home is an ANCESTOR of the shared agents dir.
    monkeypatch.setenv("KIROCREW_HOME", str(fake_home))
    _pretend_target_is_shared(monkeypatch, agent, shared)

    assert (
        agent._decline_shared_agent_home() is not None
    ), "an ancestor data home made the shared agent home look private"


def test_global_kiro_home_in_a_worktree_still_declines(monkeypatch, tmp_path):
    """Closed bypass: a globally exported KIRO_HOME moves the SHARED directory.

    Comparing against a hard-coded ``~/.kiro/agents`` read "not the shared one"
    and waved the write through; the comparison is against what the ambient
    environment resolves instead.
    """
    from kiro_crew import agent

    monkeypatch.delenv("KIROCREW_HOME", raising=False)
    monkeypatch.setenv("KIRO_HOME", str(tmp_path / "kiro-alt"))
    wt = _make_linked_worktree(tmp_path)
    monkeypatch.setattr(agent, "__file__", str(wt / "src" / "kiro_crew" / "agent.py"))
    _pretend_target_is_shared(monkeypatch, agent, tmp_path / "kiro-alt" / "agents")

    assert (
        agent._decline_shared_agent_home() is not None
    ), "a globally exported KIRO_HOME bypassed the guard"


def test_declines_from_a_clone_under_the_temp_dir(monkeypatch, tmp_path):
    """A throwaway clone under the system temp dir must not own the shared home.

    The failure shape: automation clones the repo into a per-task temp directory,
    something in that tree reaches ``rebuild_agent_config``, and the machine-wide
    spec ends up naming a launcher venv (and possibly a pinned data home) that is
    deleted when the task ends.

    The clone is built under ``tempfile.gettempdir()`` — the root the predicate
    answers against at call time — NOT under ``tmp_path``: pytest's basetemp is
    created before the suite redirects the tempfile base, so ``tmp_path`` does
    not live under the redirected root and would miss the arm under test.

    A spec is planted first because that is the harm: the failure is an OVERWRITE of a
    working spec, and the guard's remedy ("use the specs that already worked")
    only exists when one is there. The empty-home case is the next test.
    """
    import tempfile

    from kiro_crew import agent

    monkeypatch.delenv("KIRO_HOME", raising=False)
    monkeypatch.delenv("KIROCREW_HOME", raising=False)
    shared = tmp_path / "agents"
    shared.mkdir()
    (shared / agent.AGENT_FILENAME).write_text("{}", encoding="utf-8")
    with tempfile.TemporaryDirectory(prefix="kc-clone-") as scratch_name:
        clone = Path(scratch_name) / "repo"
        (clone / "src" / "kiro_crew").mkdir(parents=True)
        (clone / ".git").mkdir()  # a DIRECTORY -> ordinary clone, not a worktree
        monkeypatch.setattr(agent, "__file__", str(clone / "src" / "kiro_crew" / "agent.py"))
        _pretend_target_is_shared(monkeypatch, agent, shared)

        assert (
            agent._decline_shared_agent_home() is not None
        ), "a temp-dir clone was allowed to rewrite the shared agent home"


def test_does_not_decline_from_a_temp_clone_when_no_spec_exists(monkeypatch, tmp_path):
    """With an EMPTY shared home the temp arm must write, not decline.

    Declining is only ever a redirect to "the specs that already worked". When
    there are none, refusing protects nothing and leaves the install with no
    spec at all -- every turn then fails with ``Mode 'kirocrew' not found``.
    """
    import tempfile

    from kiro_crew import agent

    monkeypatch.delenv("KIRO_HOME", raising=False)
    monkeypatch.delenv("KIROCREW_HOME", raising=False)
    with tempfile.TemporaryDirectory(prefix="kc-clone-") as scratch_name:
        clone = Path(scratch_name) / "repo"
        (clone / "src" / "kiro_crew").mkdir(parents=True)
        (clone / ".git").mkdir()
        monkeypatch.setattr(agent, "__file__", str(clone / "src" / "kiro_crew" / "agent.py"))
        # Target resolves shared but holds no spec -> nothing to preserve.
        _pretend_target_is_shared(monkeypatch, agent, tmp_path / "agents")

        assert agent._decline_shared_agent_home(audit=False) is None, (
            "an empty shared agent home was refused its first spec, so the install "
            "would have no agent to run"
        )


def test_does_not_decline_from_an_appimage_runtime_mount(monkeypatch, tmp_path):
    """An AppImage lives under the temp root yet must keep writing its specs.

    The runtime unpacks to ``/tmp/.mount_<name>XXXXXX`` and picks a NEW random
    mount every launch, so the durable ``.AppImage`` behind it can only have
    working managed servers by rewriting the spec on each start. Declining would
    freeze the spec on a previous launch's mount and ENOENT every managed server
    -- that same ENOENT symptom, manufactured on a shipped channel -- and on a fresh
    install would leave no spec at all.
    """
    import tempfile

    from kiro_crew import agent

    monkeypatch.delenv("KIRO_HOME", raising=False)
    monkeypatch.delenv("KIROCREW_HOME", raising=False)
    monkeypatch.delenv("APPDIR", raising=False)  # env-free child: `.mount_` is the signal
    with tempfile.TemporaryDirectory(prefix="kc-appimage-") as scratch_name:
        mount = Path(scratch_name) / ".mount_KiroXk3Qm9"
        (mount / "usr" / "lib" / "kiro_crew").mkdir(parents=True)
        monkeypatch.setattr(
            agent, "__file__", str(mount / "usr" / "lib" / "kiro_crew" / "agent.py")
        )
        _pretend_target_is_shared(monkeypatch, agent, tmp_path / "agents")

        assert agent._decline_shared_agent_home(audit=False) is None, (
            "an AppImage runtime mount was refused the shared agent home, so its "
            "managed servers would stay pinned to a stale mount path"
        )


def test_under_system_tmp_covers_posix_tmp_when_tmpdir_points_elsewhere(monkeypatch, tmp_path):
    """The macOS shape: ``$TMPDIR`` is per-user, so ``/tmp`` needs its own arm.

    launchd sets ``$TMPDIR`` to ``/var/folders/.../T``, so ``gettempdir()`` does
    not contain ``/tmp`` there — and ``/tmp/kc-fix-XXXX`` is the literal clone
    path this arm refuses. Simulated by pointing ``gettempdir()`` away from
    ``/tmp``, which is what the platform difference amounts to.

    POSIX-only: on Windows ``/tmp`` is a drive-relative path with no reboot-reaped
    meaning, which is exactly why the predicate documents it as never matching
    there.
    """
    import tempfile as _tempfile

    if sys.platform == "win32":
        pytest.skip("no POSIX /tmp tree on Windows")

    from kiro_crew.config import paths as paths_mod

    monkeypatch.setattr(_tempfile, "gettempdir", lambda: str(tmp_path / "T"))

    assert paths_mod._under_system_tmp(Path("/tmp/kc-fix-4781/repo/src")) is True
    # And the configured root still answers, so neither arm shadows the other.
    assert paths_mod._under_system_tmp(tmp_path / "T" / "scratch") is True
    assert paths_mod._under_system_tmp(Path("/var/tmp/durable")) is False


def test_under_system_tmp_omits_the_posix_literal_off_posix(monkeypatch, tmp_path):
    """``/tmp`` is drive-relative on Windows, so the literal arm must not apply.

    ``Path("/tmp").resolve()`` anchors to the current drive there (``C:\\tmp``),
    which carries none of the reboot-reaped meaning the rule rests on and may
    hold a perfectly durable checkout. Asserted NATIVELY on Windows rather than
    by simulating ``os.name``: pathlib picks its flavour from that same name, so
    patching it cannot instantiate a path at all.
    """
    import tempfile as _tempfile

    from kiro_crew.config import paths as paths_mod

    if sys.platform != "win32":
        pytest.skip("the drive-relative resolution being pinned is Windows-only")

    monkeypatch.setattr(_tempfile, "gettempdir", lambda: str(tmp_path / "T"))
    drive_tmp = Path("/tmp").resolve()
    if drive_tmp == (tmp_path / "T").resolve() or drive_tmp in (tmp_path / "T").resolve().parents:
        pytest.skip("this host's %TEMP% is itself under the drive-anchored /tmp")

    assert paths_mod._under_system_tmp(drive_tmp / "kc-fix-4781" / "repo") is False
    # The configured root is still the one root every platform keeps.
    assert paths_mod._under_system_tmp(tmp_path / "T" / "scratch") is True


def test_under_system_tmp_still_answers_yes_for_an_appimage_mount():
    """The carve-out belongs to the CALLER, not to the predicate.

    ``_under_system_tmp`` stays a plain "is it under the temp root" answer so a
    second caller is not silently handed the agent-home guard's exemption.

    The probe path is resolved because the predicate resolves only its ROOTS and
    documents the caller as resolving the path: on Windows ``gettempdir()``
    hands back an 8.3 short form (``RUNNER~1``) that is unequal to the long form
    the root resolves to, so an unresolved probe would miss there and there
    only.
    """
    import tempfile

    from kiro_crew.config.paths import _in_ephemeral_tree, _under_system_tmp

    mount = (Path(tempfile.gettempdir()) / ".mount_KiroXk3Qm9" / "usr" / "lib").resolve()
    assert _under_system_tmp(mount) is True
    assert _in_ephemeral_tree(mount, env={}) is True


def test_does_not_decline_from_a_durable_clone(monkeypatch, tmp_path):
    """An ordinary install outside the temp dir still owns its shared specs.

    The path is fabricated (never created) precisely because a real path a test
    can create lives under the redirected temp root: the guard's predicates are
    lexical on the resolved path, so existence is not required, and a
    non-temp, non-worktree location is the durable-install shape.
    """
    from kiro_crew import agent

    monkeypatch.delenv("KIRO_HOME", raising=False)
    monkeypatch.delenv("KIROCREW_HOME", raising=False)
    durable = Path("/durable-install/KiroCrew/src/kiro_crew/agent.py")
    monkeypatch.setattr(agent, "__file__", str(durable))
    _pretend_target_is_shared(monkeypatch, agent, tmp_path / "agents")

    assert agent._decline_shared_agent_home() is None


def test_rebuild_agent_config_writes_nothing_when_declined(monkeypatch, tmp_path):
    """The guard must stop the write, not merely warn after it."""
    from kiro_crew import agent

    monkeypatch.delenv("KIRO_HOME", raising=False)
    agents_dir = tmp_path / "agents"
    _pretend_target_is_shared(monkeypatch, agent, agents_dir)
    wt = _make_linked_worktree(tmp_path)
    monkeypatch.setattr(agent, "__file__", str(wt / "src" / "kiro_crew" / "agent.py"))

    returned = agent.rebuild_agent_config()

    assert returned == agents_dir / agent.AGENT_FILENAME
    assert not agents_dir.exists(), "declined rebuild must not create the agent home"

    # The reporting variant's verdict comes from the same real guard: a
    # declined home must read wrote=False, or a caller's memo records a
    # projection that never landed. The guard is evaluated EXACTLY ONCE per
    # rebuild — a second evaluation (a pre-probe, a re-check before the
    # write) reopens the probe/rebuild window a concurrent default-home boot
    # can slip through, which is the race this contract exists to close.
    real_guard = agent._decline_shared_agent_home
    guard_calls: list[int] = []

    def counting_guard(**kwargs):
        guard_calls.append(1)
        return real_guard(**kwargs)

    monkeypatch.setattr(agent, "_decline_shared_agent_home", counting_guard)
    probe: list[bool] = []
    reported = agent.rebuild_agent_config(_wrote_out=probe)
    assert reported == agents_dir / agent.AGENT_FILENAME
    assert probe == [False], "a refused rebuild must report exactly one False verdict"
    assert len(guard_calls) == 1, "the guard must be evaluated exactly once per rebuild"
    reported2, wrote = agent.rebuild_agent_config_reporting()
    assert reported2 == agents_dir / agent.AGENT_FILENAME
    assert wrote is False, "a refused rebuild must report wrote=False"
    assert len(guard_calls) == 2, "reporting adds exactly one more guard evaluation"


def test_refusal_is_sel_audited(monkeypatch, tmp_path):
    """The refusal is a permission decision, so it must reach the audit trail.

    A silent refusal is indistinguishable from "no write was attempted" when
    reconstructing what an ephemeral instance did to the host, so the log line
    alone is not enough.
    """
    from kiro_crew import agent

    events = _capture_sel(monkeypatch, agent)
    monkeypatch.delenv("KIRO_HOME", raising=False)
    monkeypatch.delenv("KIROCREW_HOME", raising=False)
    monkeypatch.delenv("KIROCREW_POD", raising=False)
    wt = _make_linked_worktree(tmp_path)
    monkeypatch.setattr(agent, "__file__", str(wt / "src" / "kiro_crew" / "agent.py"))
    _pretend_target_is_shared(monkeypatch, agent, tmp_path / "agents")

    assert agent._decline_shared_agent_home() is not None

    denied = [e for e in events if e.get("outcome") == "denied"]
    assert len(denied) == 1, f"expected exactly one denied event, got {events}"
    assert denied[0]["operation"] == "agent_home_write"
    assert denied[0]["source"] == "rebuild_agent_config"
    assert str(tmp_path / "agents") in denied[0]["resources"]


def _capture_sel(monkeypatch, agent_mod) -> list[dict]:
    events: list[dict] = []

    class _Sel:
        def log_api_access(self, **kw):
            events.append(kw)

    monkeypatch.setattr(agent_mod, "sel", lambda: _Sel())
    return events


def test_allowed_shared_home_write_is_audited(monkeypatch, tmp_path):
    """The GRANT is audited too, not just the denial.

    Both outcomes of a permission decision over the shared agent home must be
    reconstructable from the audit log alone — otherwise "no event" is ambiguous
    between "permitted" and "never attempted". Mirrors how ``api_lessons_create``
    records its allow and deny branches.
    """
    from kiro_crew import agent

    events = _capture_sel(monkeypatch, agent)
    monkeypatch.delenv("KIRO_HOME", raising=False)
    monkeypatch.delenv("KIROCREW_HOME", raising=False)
    monkeypatch.delenv("KIROCREW_POD", raising=False)
    # Fabricated non-temp location: a clone created under tmp_path would now be
    # (correctly) declined by the temp-dir arm, and this test is about the GRANT
    # branch. The predicates are lexical on the resolved path, so the file need
    # not exist.
    durable = Path("/durable-install/KiroCrew/src/kiro_crew/agent.py")
    monkeypatch.setattr(agent, "__file__", str(durable))
    _pretend_target_is_shared(monkeypatch, agent, tmp_path / "agents")

    assert agent._decline_shared_agent_home() is None

    allowed = [e for e in events if e.get("outcome") == "allowed"]
    assert len(allowed) == 1, f"expected exactly one allowed event, got {events}"
    assert allowed[0]["operation"] == "agent_home_write"
    assert allowed[0]["source"] == "rebuild_agent_config"
    assert str(tmp_path / "agents") in allowed[0]["resources"]


def test_private_target_emits_no_audit_event(monkeypatch, tmp_path):
    """A private target is not a decision ABOUT the shared home, so it is silent.

    This bounds the audit to shared-resource decisions: without it every test and
    every pod boot would add events that carry no traceability.
    """
    from kiro_crew import agent

    events = _capture_sel(monkeypatch, agent)
    monkeypatch.delenv("KIRO_HOME", raising=False)
    monkeypatch.delenv("KIROCREW_POD", raising=False)
    wt = _make_linked_worktree(tmp_path)
    monkeypatch.setattr(agent, "__file__", str(wt / "src" / "kiro_crew" / "agent.py"))
    monkeypatch.setattr(agent, "KIRO_AGENTS_DIR", tmp_path / "private" / "agents")
    monkeypatch.setattr(agent, "kiro_agents_dir", lambda: tmp_path / "elsewhere")

    assert agent._decline_shared_agent_home() is None
    assert events == []


# --------------------------------------------------------------------------
# Pods get their own agent home
# --------------------------------------------------------------------------
@pytest.mark.skipif(sys.platform != "linux", reason="pods are systemd --user, Linux-only")
def test_pod_env_gives_the_pod_its_own_homes():
    """A pod owns its agent specs AND its transcripts, so it shares nothing.

    Both halves matter together. With only its own specs, a pod would write
    transcripts somewhere KiroCrew never reads and lose session resume. With
    neither, a pod is refused the write and falls back to the shared spec — whose
    env pins the LIVE data home, so a pod's ``learn_add`` would write the real
    user's lessons.
    """
    from kiro_crew.pod.config import PodConfig
    from kiro_crew.pod.runtime import build_pod_env

    cfg = PodConfig.load()
    home = Path("/tmp/kirocrew-pods/example")
    env = build_pod_env(cfg, home, 7811, Path("/workplace/example"))

    assert env["KIRO_HOME"] == str(home / "kiro")
    assert env["KIROCREW_HOME"] == str(home)
    # both live inside the pod home, so teardown reclaims them
    assert Path(env["KIRO_HOME"]).is_relative_to(home)


@pytest.mark.skipif(sys.platform != "linux", reason="pods are systemd --user, Linux-only")
def test_pod_target_is_private_so_the_guard_stands_aside(monkeypatch, tmp_path):
    """A pod writes its OWN specs rather than being refused.

    Being refused is not harmless for a pod: it would inherit the shared spec,
    which pins the live data home.
    """
    from kiro_crew import agent
    from kiro_crew.config.paths import isolated_agents_dir

    pod_home = tmp_path / "pods" / "example"
    pod_home.mkdir(parents=True)
    monkeypatch.setenv("KIROCREW_HOME", str(pod_home))
    monkeypatch.setenv("KIRO_HOME", str(pod_home / "kiro"))
    wt = _make_linked_worktree(tmp_path)
    monkeypatch.setattr(agent, "__file__", str(wt / "src" / "kiro_crew" / "agent.py"))
    _pretend_target_is_shared(monkeypatch, agent, isolated_agents_dir(pod_home))

    assert agent._decline_shared_agent_home() is None


# --------------------------------------------------------------------------
# The non-default data home arm
# --------------------------------------------------------------------------
def _durable_checkout(monkeypatch, agent_mod) -> None:
    """Present the checkout as a durable install (non-temp, non-worktree).

    Fabricated, never created: the guard's predicates are lexical on the
    resolved path (same trick as ``test_does_not_decline_from_a_durable_clone``),
    and a real path a test can create lives under the redirected temp root,
    which the temp arm would (correctly) decline for its own reason.
    """
    durable = Path("/durable-install/KiroCrew/src/kiro_crew/agent.py")
    monkeypatch.setattr(agent_mod, "__file__", str(durable))


def test_override_home_declines_shared_write_even_from_durable_checkout(monkeypatch, tmp_path):
    """A KIROCREW_HOME-override instance must not overwrite existing shared
    specs.

    The reported writer was a DURABLE checkout — not a worktree, not a temp
    clone, not a pod — booted with a scratch ``KIROCREW_HOME``. Its rebuild
    pinned that home into every managed server entry of ``~/.kiro/agents``, so
    every stub spawned afterwards resolved ``config_dir()`` to a home the real
    gateway never writes and strict identity failed closed in ALL sessions.
    Ephemerality arms never see this shape; the data-home arm must.
    """
    from kiro_crew import agent

    events = _capture_sel(monkeypatch, agent)
    monkeypatch.delenv("KIRO_HOME", raising=False)
    monkeypatch.delenv("KIROCREW_POD", raising=False)
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "scratch-home"))
    _durable_checkout(monkeypatch, agent)
    shared = tmp_path / "agents"
    shared.mkdir()
    # An existing spec is the audience being protected: the arm declines only
    # when there is something to preserve, mirroring the temp arm's rule.
    (shared / agent.AGENT_FILENAME).write_text("{}", encoding="utf-8")
    _pretend_target_is_shared(monkeypatch, agent, shared)

    assert (
        agent._decline_shared_agent_home() is not None
    ), "an override-home instance was handed the shared agent home"

    denied = [e for e in events if e.get("outcome") == "denied"]
    assert len(denied) == 1, f"expected exactly one denied event, got {events}"
    assert denied[0]["operation"] == "agent_home_write"
    assert "non-default data home" in denied[0]["error"]


def test_override_home_with_no_existing_spec_still_writes(monkeypatch, tmp_path):
    """A relocated-home install on a spec-less machine is NOT refused.

    With no shared spec present there is no default-home audience to poison,
    and refusing would leave the install with no spec at all — no boot error
    (``missing_required_agent_specs`` is suppressed by the same guard) and
    every turn dying at "Mode 'kirocrew' not found". Same precondition as the
    temp arm: decline only when there is something to preserve.
    """
    from kiro_crew import agent

    monkeypatch.delenv("KIRO_HOME", raising=False)
    monkeypatch.delenv("KIROCREW_POD", raising=False)
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "relocated-home"))
    _durable_checkout(monkeypatch, agent)
    _pretend_target_is_shared(monkeypatch, agent, tmp_path / "agents")

    assert agent._decline_shared_agent_home() is None


def test_override_home_declines_when_only_a_sibling_spec_exists(monkeypatch, tmp_path):
    """A surviving SIBLING spec is enough audience to refuse for.

    ``kirocrew.json`` and its siblings are written by separate installers, so a
    deleted main with a surviving ``kirocrew-lite.json`` is a reachable state.
    A main-only precondition would wave the rebuild through and overwrite the
    surviving siblings with the foreign home — the same poison, one file over.
    The precondition therefore covers every ``OWNED_KIRO_AGENT_FILES`` entry.
    """
    from kiro_crew import agent
    from kiro_crew.agent_files import LITE_AGENT_FILENAME

    monkeypatch.delenv("KIRO_HOME", raising=False)
    monkeypatch.delenv("KIROCREW_POD", raising=False)
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "scratch-home"))
    _durable_checkout(monkeypatch, agent)
    shared = tmp_path / "agents"
    shared.mkdir()
    # Only a sibling survives; the main spec is absent.
    (shared / LITE_AGENT_FILENAME).write_text("{}", encoding="utf-8")
    _pretend_target_is_shared(monkeypatch, agent, shared)

    assert (
        agent._decline_shared_agent_home() is not None
    ), "a surviving sibling spec was overwritten by an override-home rebuild"


def _spec_pinned_to(home) -> str:
    """A minimal owned spec whose managed server records *home* as its writer."""
    import json

    return json.dumps(
        {
            "name": "kirocrew",
            "mcpServers": {
                "kirocrew-core": {"command": "kirocrew", "env": {"KIROCREW_HOME": str(home)}}
            },
        }
    )


def test_self_pinned_specs_keep_their_writer(monkeypatch, tmp_path):
    """An override-home install keeps refreshing the specs IT wrote.

    The specs carry their writer's home in every managed server's
    ``env.KIROCREW_HOME``. An ownership-blind existence check would read this
    instance's own first write as "someone's specs" and refuse every later
    rebuild — a self-lockout where the install's specs go permanently stale.
    Provenance matching is what lets it keep writing.
    """
    from kiro_crew import agent

    own_home = (tmp_path / "relocated-home").resolve()
    monkeypatch.delenv("KIRO_HOME", raising=False)
    monkeypatch.delenv("KIROCREW_POD", raising=False)
    monkeypatch.setenv("KIROCREW_HOME", str(own_home))
    _durable_checkout(monkeypatch, agent)
    shared = tmp_path / "agents"
    shared.mkdir()
    (shared / agent.AGENT_FILENAME).write_text(_spec_pinned_to(own_home), encoding="utf-8")
    _pretend_target_is_shared(monkeypatch, agent, shared)

    assert agent._decline_shared_agent_home() is None


def test_foreign_pinned_specs_refuse(monkeypatch, tmp_path):
    """Specs pinned to a DIFFERENT home are someone else's — refuse."""
    from kiro_crew import agent

    monkeypatch.delenv("KIRO_HOME", raising=False)
    monkeypatch.delenv("KIROCREW_POD", raising=False)
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "my-home"))
    _durable_checkout(monkeypatch, agent)
    shared = tmp_path / "agents"
    shared.mkdir()
    (shared / agent.AGENT_FILENAME).write_text(
        _spec_pinned_to(tmp_path / "someone-elses-home"), encoding="utf-8"
    )
    _pretend_target_is_shared(monkeypatch, agent, shared)

    assert agent._decline_shared_agent_home() is not None


def test_unc_pin_refuses_without_touching_the_filesystem(monkeypatch, tmp_path):
    """A recorded pin is compared, never interpreted — no OS call sees it.

    On Windows, resolving a UNC-valued pin (``\\\\attacker\\share\\...``) fires
    outbound SMB authentication to the named host — spec content choosing a
    network destination. The provenance check must decide ownership by string
    comparison alone, so the hostile value must never reach ``resolve()``,
    ``expanduser()``, ``stat()``, ``realpath()`` or any other filesystem
    interpretation. Every guarded primitive asserts, so a future refactor
    that routes the value through a different OS call fails here rather than
    reopening the class.
    """
    import os as os_mod
    from pathlib import Path

    from kiro_crew import agent

    own_home = (tmp_path / "my-home").resolve()
    monkeypatch.delenv("KIRO_HOME", raising=False)
    monkeypatch.delenv("KIROCREW_POD", raising=False)
    monkeypatch.setenv("KIROCREW_HOME", str(own_home))
    _durable_checkout(monkeypatch, agent)
    shared = tmp_path / "agents"
    shared.mkdir()
    (shared / agent.AGENT_FILENAME).write_text(
        _spec_pinned_to(r"\\attacker\share\kirocrew-home"), encoding="utf-8"
    )
    _pretend_target_is_shared(monkeypatch, agent, shared)

    def _guard(real, label):
        def guarded(*args, **kwargs):
            for arg in args:
                assert "attacker" not in str(arg), f"spec content reached {label}"
            return real(*args, **kwargs)

        return guarded

    monkeypatch.setattr(os_mod, "stat", _guard(os_mod.stat, "os.stat"))
    monkeypatch.setattr(os_mod, "lstat", _guard(os_mod.lstat, "os.lstat"))
    monkeypatch.setattr(os_mod.path, "realpath", _guard(os_mod.path.realpath, "os.path.realpath"))
    monkeypatch.setattr(Path, "resolve", _guard(Path.resolve, "Path.resolve"))
    monkeypatch.setattr(Path, "expanduser", _guard(Path.expanduser, "Path.expanduser"))
    monkeypatch.setattr(Path, "stat", _guard(Path.stat, "Path.stat"))
    monkeypatch.setattr(Path, "exists", _guard(Path.exists, "Path.exists"))

    # Direct: the provenance reader itself judges the pin foreign.
    assert agent._existing_specs_are_mine(shared, own_home) is False
    # Integration: the guard consumes that verdict and refuses the write.
    assert agent._decline_shared_agent_home() is not None


def test_symlinked_spec_is_present_and_unvouchable(monkeypatch, tmp_path):
    """A symlink at a spec path reads as a refusal, not as absence.

    ``exists()`` follows symlinks, so a dangling planted link would read as
    "no spec here" — an absence verdict an attacker can manufacture in the
    same-uid agents directory, flipping the guard to a fresh write. The
    no-follow probe sees it, and the :func:`_spec_path_is_safe` fence every
    other spec reader in the module applies refuses to read through it.
    """
    from kiro_crew import agent

    own_home = (tmp_path / "my-home").resolve()
    monkeypatch.delenv("KIRO_HOME", raising=False)
    monkeypatch.delenv("KIROCREW_POD", raising=False)
    monkeypatch.setenv("KIROCREW_HOME", str(own_home))
    _durable_checkout(monkeypatch, agent)
    shared = tmp_path / "agents"
    shared.mkdir()
    # Dangling symlink: exists() would say "absent", lexists says "present".
    (shared / agent.AGENT_FILENAME).symlink_to(tmp_path / "does-not-exist")
    _pretend_target_is_shared(monkeypatch, agent, shared)

    assert agent._existing_specs_are_mine(shared, own_home) is False
    assert agent._decline_shared_agent_home() is not None


def test_case_differing_pin_reads_foreign(monkeypatch, tmp_path):
    """The comparison is case-preserving — a case-folded match is fail-open.

    ``normcase`` would fold two homes that differ only by letter case (legal
    and distinct on case-sensitive filesystems, including case-sensitive NTFS
    directories) into one, reading the other home's spec as this instance's
    own. Distinct spellings must compare foreign; the writer and reader share
    one resolver, so a self-written pin never differs by case.
    """
    from kiro_crew import agent

    own_home = (tmp_path / "CrewHome").resolve()
    monkeypatch.delenv("KIRO_HOME", raising=False)
    monkeypatch.delenv("KIROCREW_POD", raising=False)
    monkeypatch.setenv("KIROCREW_HOME", str(own_home))
    _durable_checkout(monkeypatch, agent)
    shared = tmp_path / "agents"
    shared.mkdir()
    (shared / agent.AGENT_FILENAME).write_text(
        _spec_pinned_to(str(own_home).lower()), encoding="utf-8"
    )
    _pretend_target_is_shared(monkeypatch, agent, shared)

    assert agent._existing_specs_are_mine(shared, own_home) is False
    assert agent._decline_shared_agent_home() is not None


def test_resolution_equivalent_pin_reads_foreign(monkeypatch, tmp_path):
    """Equivalence-by-resolution does not vouch — comparison is lexical.

    The writer pins ``str(_valid_override_home())``, which is already
    resolved, so a pin that only becomes this home after following a symlink
    was not written by this instance. Reading it as "mine" would require
    handing spec content to the filesystem — the interpretation the
    provenance check forbids — so it reads foreign and refuses, the
    conservative direction every unparseable shape already takes.
    """
    from kiro_crew import agent

    own_home = (tmp_path / "real-home").resolve()
    own_home.mkdir()
    alias = tmp_path / "alias-home"
    alias.symlink_to(own_home)
    monkeypatch.delenv("KIRO_HOME", raising=False)
    monkeypatch.delenv("KIROCREW_POD", raising=False)
    monkeypatch.setenv("KIROCREW_HOME", str(own_home))
    _durable_checkout(monkeypatch, agent)
    shared = tmp_path / "agents"
    shared.mkdir()
    # Resolution-equivalent spelling: same directory, different string.
    (shared / agent.AGENT_FILENAME).write_text(_spec_pinned_to(alias), encoding="utf-8")
    _pretend_target_is_shared(monkeypatch, agent, shared)

    assert agent._decline_shared_agent_home() is not None


def test_oversized_spec_refuses_before_parsing(monkeypatch, tmp_path):
    """The ownership probe is bounded — an over-cap spec refuses, unread.

    The boot-path rebuild can run on the event loop, so the probe must never
    be the place a pathological "spec" gets slurped into memory. A file over
    the per-spec cap refuses via lstat before any open — even when its
    CONTENT would vouch as this instance's own — the same conservative
    direction every unparseable shape takes. The sentinel proves the
    ordering: the reader is never invoked for an over-cap spec, so a
    refactor that reads first and checks later fails here, not in
    production.
    """
    import pytest as _pytest

    from kiro_crew import agent

    own_home = (tmp_path / "my-home").resolve()
    monkeypatch.delenv("KIRO_HOME", raising=False)
    monkeypatch.delenv("KIROCREW_POD", raising=False)
    monkeypatch.setenv("KIROCREW_HOME", str(own_home))
    _durable_checkout(monkeypatch, agent)
    shared = tmp_path / "agents"
    shared.mkdir()
    # A self-pinned spec whose content vouches, padded past the per-spec cap
    # (JSON tolerates trailing whitespace, so a parse WOULD succeed).
    padded = _spec_pinned_to(own_home) + " " * (agent._PROVENANCE_SPEC_CAP_BYTES + 1)
    (shared / agent.AGENT_FILENAME).write_text(padded, encoding="utf-8")
    _pretend_target_is_shared(monkeypatch, agent, shared)

    def no_read(*args, **kwargs):
        _pytest.fail("the probe read an over-cap spec instead of refusing on lstat")

    monkeypatch.setattr(agent, "safe_read_file_bytes_nolink", no_read)
    assert agent._existing_specs_are_mine(shared, own_home) is False
    assert agent._decline_shared_agent_home() is not None


def test_non_regular_spec_refuses_without_opening(monkeypatch, tmp_path):
    """A FIFO at a spec path refuses via lstat — it is never opened.

    Opening a FIFO with no writer blocks indefinitely; on the boot-path
    rebuild that parks the gateway's event loop before readiness. The probe
    rejects non-regular files from the no-follow lstat, so the open never
    happens. The sentinels fail fast (instead of hanging the suite) if a
    regression routes the FIFO into either reader.
    """
    import pytest as _pytest

    if not hasattr(os, "mkfifo"):
        _pytest.skip("no mkfifo on this platform")

    from kiro_crew import agent

    own_home = (tmp_path / "my-home").resolve()
    monkeypatch.delenv("KIRO_HOME", raising=False)
    monkeypatch.delenv("KIROCREW_POD", raising=False)
    monkeypatch.setenv("KIROCREW_HOME", str(own_home))
    _durable_checkout(monkeypatch, agent)
    shared = tmp_path / "agents"
    shared.mkdir()
    os.mkfifo(shared / agent.AGENT_FILENAME)
    _pretend_target_is_shared(monkeypatch, agent, shared)

    def no_open(*args, **kwargs):
        _pytest.fail("the probe tried to open a non-regular spec (a FIFO parks the open)")

    monkeypatch.setattr(agent, "safe_read_file_bytes_nolink", no_open)
    monkeypatch.setattr(agent, "_read_spec_capped", no_open)
    assert agent._existing_specs_are_mine(shared, own_home) is False
    assert agent._decline_shared_agent_home() is not None


def test_spec_growing_after_lstat_fails_closed(monkeypatch, tmp_path):
    """A spec that outgrows the bound between lstat and read refuses.

    The descriptor reader raises ``FileTooLargeError`` when the opened file
    exceeds ``max_bytes`` — a concurrent writer can grow the file after the
    lstat passed. Memory stays capped either way; the probe must refuse
    (``False``) rather than let the raise abort the whole boot-path rebuild
    over a spec that changed underneath it.
    """
    from kiro_crew import agent
    from kiro_crew.hooks import FileTooLargeError

    own_home = (tmp_path / "my-home").resolve()
    monkeypatch.delenv("KIRO_HOME", raising=False)
    monkeypatch.delenv("KIROCREW_POD", raising=False)
    monkeypatch.setenv("KIROCREW_HOME", str(own_home))
    _durable_checkout(monkeypatch, agent)
    shared = tmp_path / "agents"
    shared.mkdir()
    (shared / agent.AGENT_FILENAME).write_text(_spec_pinned_to(own_home), encoding="utf-8")
    _pretend_target_is_shared(monkeypatch, agent, shared)

    def grew(*args, **kwargs):
        raise FileTooLargeError("file grew past max_bytes")

    monkeypatch.setattr(agent, "safe_read_file_bytes_nolink", grew)
    assert agent._existing_specs_are_mine(shared, own_home) is False
    assert agent._decline_shared_agent_home() is not None


def test_unnormalized_spelling_of_own_pin_stays_mine(monkeypatch, tmp_path):
    """Lexical means normalized, not byte-identical.

    ``os.path.normpath`` folds redundant separators and dot segments — pure
    string work — so a pin differing only in that way still reads as this
    writer's. Anything beyond normalization (a symlink, a moved home) is
    interpretation and reads foreign.
    """
    import os

    from kiro_crew import agent

    own_home = (tmp_path / "relocated-home").resolve()
    monkeypatch.delenv("KIRO_HOME", raising=False)
    monkeypatch.delenv("KIROCREW_POD", raising=False)
    monkeypatch.setenv("KIROCREW_HOME", str(own_home))
    _durable_checkout(monkeypatch, agent)
    shared = tmp_path / "agents"
    shared.mkdir()
    unnormalized = f"{own_home}{os.sep}{os.curdir}"
    (shared / agent.AGENT_FILENAME).write_text(_spec_pinned_to(unnormalized), encoding="utf-8")
    _pretend_target_is_shared(monkeypatch, agent, shared)

    assert agent._decline_shared_agent_home() is None


def test_malformed_specs_refuse(monkeypatch, tmp_path):
    """An unparseable spec proves nothing about ownership — refuse."""
    from kiro_crew import agent

    monkeypatch.delenv("KIRO_HOME", raising=False)
    monkeypatch.delenv("KIROCREW_POD", raising=False)
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "my-home"))
    _durable_checkout(monkeypatch, agent)
    shared = tmp_path / "agents"
    shared.mkdir()
    (shared / agent.AGENT_FILENAME).write_text("{not json", encoding="utf-8")
    _pretend_target_is_shared(monkeypatch, agent, shared)

    assert agent._decline_shared_agent_home() is not None


def test_custom_server_entries_cannot_vouch_ownership(monkeypatch, tmp_path):
    """Only MANAGED entries carry the writer's signature.

    Custom server entries flow in from user configuration, so a pin inside one
    must not be read as provenance: a spec whose only matching pin lives in an
    unmanaged entry stays foreign.
    """
    import json

    from kiro_crew import agent

    own_home = (tmp_path / "my-home").resolve()
    monkeypatch.delenv("KIRO_HOME", raising=False)
    monkeypatch.delenv("KIROCREW_POD", raising=False)
    monkeypatch.setenv("KIROCREW_HOME", str(own_home))
    _durable_checkout(monkeypatch, agent)
    shared = tmp_path / "agents"
    shared.mkdir()
    spec = {
        "name": "kirocrew",
        "mcpServers": {
            "my-custom-server": {"command": "x", "env": {"KIROCREW_HOME": str(own_home)}}
        },
    }
    (shared / agent.AGENT_FILENAME).write_text(json.dumps(spec), encoding="utf-8")
    _pretend_target_is_shared(monkeypatch, agent, shared)

    assert agent._decline_shared_agent_home() is not None


def test_garbage_recorded_home_refuses_without_crashing(monkeypatch, tmp_path):
    """A garbage recorded value reads foreign, never raises.

    Spec content is not trusted input. The comparison is lexical, so a NUL
    byte never reaches path resolution at all: the value is refused by the
    explicit NUL guard (some platforms' C ``normpath`` can raise on it) and
    would compare unequal regardless — either way agent setup is never
    aborted by garbage in a spec.
    """
    import json

    from kiro_crew import agent

    monkeypatch.delenv("KIRO_HOME", raising=False)
    monkeypatch.delenv("KIROCREW_POD", raising=False)
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "my-home"))
    _durable_checkout(monkeypatch, agent)
    shared = tmp_path / "agents"
    shared.mkdir()
    spec = {
        "name": "kirocrew",
        "mcpServers": {"kirocrew-core": {"command": "x", "env": {"KIROCREW_HOME": "bad\x00home"}}},
    }
    (shared / agent.AGENT_FILENAME).write_text(json.dumps(spec), encoding="utf-8")
    _pretend_target_is_shared(monkeypatch, agent, shared)

    assert agent._decline_shared_agent_home() is not None


def test_mixed_pins_within_one_spec_refuse(monkeypatch, tmp_path):
    """EVERY managed entry must vouch — one matching entry is not ownership.

    The writer pins all managed entries in one rebuild, so a spec whose
    entries disagree (one pinned to this home, another pinned elsewhere or
    not at all) was not written whole by this instance and must refuse.
    """
    import json

    from kiro_crew import agent

    own_home = (tmp_path / "my-home").resolve()
    monkeypatch.delenv("KIRO_HOME", raising=False)
    monkeypatch.delenv("KIROCREW_POD", raising=False)
    monkeypatch.setenv("KIROCREW_HOME", str(own_home))
    _durable_checkout(monkeypatch, agent)
    shared = tmp_path / "agents"
    shared.mkdir()
    spec = {
        "name": "kirocrew",
        "mcpServers": {
            "kirocrew-core": {"command": "x", "env": {"KIROCREW_HOME": str(own_home)}},
            "kirocrew-cron": {
                "command": "x",
                "env": {"KIROCREW_HOME": str(tmp_path / "someone-else")},
            },
        },
    }
    (shared / agent.AGENT_FILENAME).write_text(json.dumps(spec), encoding="utf-8")
    _pretend_target_is_shared(monkeypatch, agent, shared)

    assert agent._decline_shared_agent_home() is not None

    spec["mcpServers"]["kirocrew-cron"] = {"command": "x"}
    (shared / agent.AGENT_FILENAME).write_text(json.dumps(spec), encoding="utf-8")
    assert (
        agent._decline_shared_agent_home() is not None
    ), "an unpinned managed entry beside a pinned one must refuse"


def test_rebuild_round_trip_keeps_self_ownership(monkeypatch, tmp_path):
    """Specs written by the REAL writer round-trip as this instance's own.

    Writes via ``rebuild_agent_config()`` under an override home (fresh shared
    target), then asserts the same instance's guard still returns ``None`` —
    proving ``_managed_mcp_env``'s pin and the ownership read agree, rather
    than both being asserted against hand-fabricated fixtures.
    """
    from kiro_crew import agent

    own_home = tmp_path / "relocated-home"
    own_home.mkdir()
    monkeypatch.delenv("KIRO_HOME", raising=False)
    monkeypatch.delenv("KIROCREW_POD", raising=False)
    monkeypatch.setenv("KIROCREW_HOME", str(own_home))
    _durable_checkout(monkeypatch, agent)
    shared = tmp_path / "agents"
    _pretend_target_is_shared(monkeypatch, agent, shared)

    written = agent.rebuild_agent_config()

    assert written == shared / agent.AGENT_FILENAME
    assert written.exists(), "a fresh relocated-home install must write its specs"
    assert (
        agent._decline_shared_agent_home() is None
    ), "the instance's own freshly written specs must read back as its own"
    # And through the real guard again: a landed write reports wrote=True,
    # exactly once per rebuild.
    reported, wrote = agent.rebuild_agent_config_reporting()
    assert reported == shared / agent.AGENT_FILENAME
    assert wrote is True, "a landed rebuild must report wrote=True"
    probe: list[bool] = []
    agent.rebuild_agent_config(_wrote_out=probe)
    assert probe == [True], "a landed rebuild must report exactly one True verdict"


def test_rebuild_under_override_home_never_touches_shared_dir(monkeypatch, tmp_path):
    """``rebuild_agent_config`` under an override home must not rewrite an
    existing shared spec — the guard stops the write, not merely warns."""
    from kiro_crew import agent

    monkeypatch.delenv("KIRO_HOME", raising=False)
    monkeypatch.delenv("KIROCREW_POD", raising=False)
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "scratch-home"))
    _durable_checkout(monkeypatch, agent)
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    sentinel = '{"name": "kirocrew", "sentinel": "pre-existing"}'
    (agents_dir / agent.AGENT_FILENAME).write_text(sentinel, encoding="utf-8")
    _pretend_target_is_shared(monkeypatch, agent, agents_dir)

    returned = agent.rebuild_agent_config()

    assert returned == agents_dir / agent.AGENT_FILENAME
    assert (agents_dir / agent.AGENT_FILENAME).read_text(
        encoding="utf-8"
    ) == sentinel, "override-home rebuild must leave the existing shared spec byte-identical"


def test_default_home_instance_still_owns_shared_write(monkeypatch, tmp_path):
    """The other half of the contract: the DEFAULT-home instance keeps
    owning the shared specs — the new arm must not widen into refusing it."""
    from kiro_crew import agent

    monkeypatch.delenv("KIRO_HOME", raising=False)
    monkeypatch.delenv("KIROCREW_HOME", raising=False)
    monkeypatch.delenv("KIROCREW_POD", raising=False)
    _durable_checkout(monkeypatch, agent)
    agents_dir = tmp_path / "agents"
    _pretend_target_is_shared(monkeypatch, agent, agents_dir)

    assert agent._decline_shared_agent_home() is None


def test_override_equal_to_default_home_still_owns_shared_write(monkeypatch, tmp_path):
    """A belt-and-braces ``KIROCREW_HOME=<default>`` export IS the default-home
    instance in substance; refusing it would leave its specs never refreshed."""
    from kiro_crew import agent
    from kiro_crew.config import paths

    monkeypatch.delenv("KIRO_HOME", raising=False)
    monkeypatch.delenv("KIROCREW_POD", raising=False)
    default_home = (tmp_path / "default-home").resolve()
    monkeypatch.setattr(paths, "_resolved_home", default_home)
    monkeypatch.setenv("KIROCREW_HOME", str(default_home))
    _durable_checkout(monkeypatch, agent)
    _pretend_target_is_shared(monkeypatch, agent, tmp_path / "agents")

    assert agent._decline_shared_agent_home() is None


def test_shared_kiro_agents_writable_predicate(monkeypatch, tmp_path):
    """The paths-level predicate: POD refuses, override refuses, default allows."""
    from kiro_crew.config import paths

    monkeypatch.delenv("KIROCREW_POD", raising=False)
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "override"))
    assert not paths.shared_kiro_agents_writable()

    monkeypatch.delenv("KIROCREW_HOME", raising=False)
    assert paths.shared_kiro_agents_writable()

    monkeypatch.setenv("KIROCREW_POD", "1")
    assert not paths.shared_kiro_agents_writable(), "the settings-guard predicate is reused"


# --------------------------------------------------------------------------
# Transcripts follow the same home as the specs
# --------------------------------------------------------------------------
def test_sessions_dir_follows_kiro_home(monkeypatch, tmp_path, unpinned_kiro_sessions_dir):
    """The transcripts dir must move WITH the agent dir, or resume breaks.

    ``KIRO_HOME`` is directory-wide: kiro-cli writes transcripts under it. If
    KiroCrew kept reading the machine-wide path, an instance with its own agent
    home would look for transcripts that are not there — losing session resume and
    letting ``SessionMap`` prune mappings whose files it cannot see.
    """
    from kiro_crew.config.paths import kiro_agents_dir, kiro_sessions_dir

    monkeypatch.delenv("KIROCREW_HOME", raising=False)
    monkeypatch.setenv("KIRO_HOME", str(tmp_path / "pod-kiro"))

    root = (tmp_path / "pod-kiro").resolve()
    assert kiro_sessions_dir() == root / "sessions" / "cli"
    assert kiro_agents_dir() == root / "agents"


def test_sessions_dir_defaults_to_dot_kiro(monkeypatch, unpinned_kiro_sessions_dir):
    _no_overrides(monkeypatch)
    from kiro_crew.config.paths import kiro_sessions_dir

    assert kiro_sessions_dir() == Path.home() / ".kiro" / "sessions" / "cli"


def test_no_hardcoded_transcripts_dir():
    """Every transcripts reader must resolve through ``kiro_sessions_dir()``.

    One hard-coded path here is what makes an instance write transcripts to one
    place and read them from another.
    """
    offenders: list[str] = []
    pat = re.compile(r'"\.kiro"\s*/\s*"sessions"')
    for py in SRC.rglob("*.py"):
        rel = py.relative_to(SRC).as_posix()
        if rel in _ALLOWED:
            continue
        for i, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
            if line.strip().startswith("#") or not pat.search(line):
                continue
            offenders.append(f"{rel}:{i}: {line.strip()}")
    assert not offenders, "hard-coded transcripts dir -- use kiro_sessions_dir():\n" + "\n".join(
        offenders
    )


# --------------------------------------------------------------------------
# Anti-regression guard
# --------------------------------------------------------------------------
# Files allowed to mention the literal default: the resolver that defines it,
# and prose/comments that describe the mechanism.
# Both spellings must be caught. The tuple form ``"​.kiro" / "agents"`` was the
# obvious one; the string form ``".kiro/agents/..."`` (e.g. inside a ``glob()``)
# slipped through the first version of this guard and was caught in review.
_LITERAL_RE = re.compile(r'"\.kiro"\s*/\s*"agents"' r"|[\"']\.kiro/agents")
# ``config/paths.py`` is the resolver that defines the default.
# ``security/paths.py`` holds the sensitive-path denylist, whose entries are
# HOME-RELATIVE literals
# (the matcher anchors ``$HOME``-relative strings; ``kiro_agents_dir()`` returns
# an absolute path, so the resolver's value cannot be used here) and which is
# kept literal on purpose to avoid a config->security import cycle — the same
# convention as the ``.data-home-ready`` marker literal. It does NOT read or
# write the dir (it only matches path strings) and it re-anchors ``KIRO_HOME``
# for that entry, so it cannot reintroduce the reader/writer split-brain this
# guard exists to catch; ``TestKiroAgentsDirWriteProtection`` pins the literal to
# ``kiro_agents_dir()`` so drift still fails loudly.
# string it refuses to ship in a curated bundle. It only matches path components
# and never reads or writes the agents dir, and the packager runs in a standalone
# deployment venv where ``config.paths`` is not importable, so it cannot route
# through ``kiro_agents_dir()`` even in principle.
_ALLOWED = {
    "config/paths.py",
    "security/paths.py",
}


def test_no_new_hardcoded_global_agents_dir():
    """Every reader/writer must resolve through ``kiro_agents_dir()``.

    A single hard-coded ``Path.home() / ".kiro" / "agents"`` reintroduces the
    split brain this module guards: writers honoring KIRO_HOME while a reader
    still looks at the machine-wide directory (or vice versa).
    """
    offenders: list[str] = []
    for py in SRC.rglob("*.py"):
        rel = py.relative_to(SRC).as_posix()
        if rel in _ALLOWED:
            continue
        for i, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#") or not _LITERAL_RE.search(line):
                continue
            # ``user_home`` is an explicit caller-supplied override, not the
            # machine-wide default.
            if "user_home" in line:
                continue
            offenders.append(f"{rel}:{i}: {stripped}")
    assert (
        not offenders
    ), "hard-coded global agents dir — use kiro_agents_dir() instead:\n" + "\n".join(offenders)


def test_repo_has_no_python_syntax_regression(tmp_path):
    """Cheap compile-all so a rewrite typo fails here rather than at import.

    Bytecode goes to a tmp cache prefix so the checkout stays clean.
    """
    env = {**os.environ, "PYTHONPYCACHEPREFIX": str(tmp_path / "pycache")}
    proc = subprocess.run(
        [sys.executable, "-m", "compileall", "-q", str(SRC)],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(tmp_path),
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
