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
from kiro_crew.subprocess_utf8 import UTF8_TEXT

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src" / "kiro_crew"


def _child_env(**overrides: str) -> dict[str, str]:
    """Env for a probe interpreter that must import THIS worktree.

    Two corrections to a plain ``os.environ`` copy, both needed before the
    child can report on the code under test:

    ``PYTHONPATH`` leads with this worktree's ``src``, because otherwise the
    child resolves ``kiro_crew`` through whatever editable install the ambient
    environment carries — on a machine with more than one checkout that is a
    different tree, and the probe then describes code this test is not
    measuring.

    The parent's ``site-packages`` entries follow it, because the autouse
    ``_isolate_default_home`` fixture repoints ``HOME`` at a temp directory.
    That redirect is wanted (it is what makes ``Path.home()`` assertable), but
    it also moves ``$HOME/.local/.../site-packages`` out from under the child,
    so third-party imports in the package's own import chain would fail and the
    probe would report an ``ImportError`` rather than a path.
    """
    deps = [p for p in sys.path if p and ("site-packages" in p or "dist-packages" in p)]
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([str(REPO_ROOT / "src"), *deps]),
    }
    env.pop("KIROCREW_HOME", None)
    env.update(overrides)
    return env


@pytest.fixture(autouse=True)
def _isolate_default_home(monkeypatch, tmp_path):
    """Unpinning KIROCREW_HOME must remain safe even when SEL initializes cold."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home


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
def _pin_default_home_and_breadcrumb(request, monkeypatch, tmp_path):
    """Keep every test in this module off the operator's real data home.

    The guard's writability probe compares the override against the DEFAULT
    home, and several tests exercise the default path outright (no override),
    so production would resolve — and create — the real ``~/.kiro/crew`` plus
    the ``~/.kirocrew.breadcrumb`` beside it. Conftest's real-home ratchet
    fails such tests at teardown, and its breadcrumb guard fails them at write
    time. None of these tests is ABOUT the breadcrumb or the real default, so
    the whole module pins the default to tmp and stubs the breadcrumb writer,
    the two remedies the ratchet's messages prescribe.

    Stands aside for a test that requests ``_isolate_default_home``: that
    fixture redirects the WHOLE home — ``HOME``, ``USERPROFILE``,
    ``Path.home`` — to tmp, so the real default is unreachable anyway, and
    such a test's subject is exactly the real cold-resolution path (the
    memoization into ``_resolved_home`` and the breadcrumb write) that this
    pin would otherwise stub out. Only the cache reset is shared: cold-start
    assertions must hold regardless of test order.
    """
    from kiro_crew.config import paths

    monkeypatch.setattr(paths, "_resolved_home", None, raising=False)
    if "_isolate_default_home" in request.fixturenames:
        return
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


def test_cold_sel_decline_keeps_default_home_synthetic(
    monkeypatch, tmp_path, _isolate_default_home
):
    """Exercise key creation through the real writer after the override is removed."""
    from kiro_crew import agent
    from kiro_crew.config import paths
    from kiro_crew.sel import SecurityEventLog

    _no_overrides(monkeypatch)
    monkeypatch.setattr(SecurityEventLog, "_instance", None)
    audit_root = tmp_path / "cold-audit"
    assert not audit_root.exists()
    assert paths._resolved_home is None
    # The real synchronous mode avoids leaving a new daemon writer behind.
    audit = SecurityEventLog(base_dir=audit_root, sync=True)
    home = _isolate_default_home
    assert paths._resolved_home == home / ".kiro" / "crew"
    assert (home / paths.RECOVERY_BREADCRUMB_NAME).is_file()

    wt = _make_linked_worktree(tmp_path)
    monkeypatch.setattr(agent, "__file__", str(wt / "src" / "kiro_crew" / "agent.py"))
    shared = tmp_path / "shared-agents"
    _pretend_target_is_shared(monkeypatch, agent, shared)
    assert agent._decline_shared_agent_home() == shared / agent.AGENT_FILENAME
    assert not shared.exists()
    events = audit.recent()
    assert len(events) == 1
    assert events[0]["operation"] == "agent_home_write"
    assert events[0]["outcome"] == "denied"
    assert audit.verify_integrity() == (1, 1)


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
        # The worktree arm walks up from the checkout to the NEAREST `.git`
        # marker, and the temp root can itself live inside a linked worktree (a
        # developer's `TMPDIR=./tmp`, the hygiene sweep's pinned scratch). An
        # ordinary-clone marker at the temp root makes that walk answer on the
        # fixture, so only the temp arm -- the one this test is about -- decides.
        (Path(scratch_name) / ".git").mkdir()
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
    import stat as _stat

    import pytest as _pytest

    from kiro_crew import agent

    own_home = (tmp_path / "my-home").resolve()
    monkeypatch.delenv("KIRO_HOME", raising=False)
    monkeypatch.delenv("KIROCREW_POD", raising=False)
    monkeypatch.setenv("KIROCREW_HOME", str(own_home))
    _durable_checkout(monkeypatch, agent)
    shared = tmp_path / "agents"
    shared.mkdir()
    spec_path = shared / agent.AGENT_FILENAME
    if hasattr(os, "mkfifo"):
        os.mkfifo(spec_path)
    else:
        # No FIFO primitive on this platform (Windows shards). The ratchet —
        # non-regular specs refuse from the no-follow lstat, never opened —
        # must still be verified here, so plant a regular file and present a
        # FIFO ``st_mode`` for it through ``lstat``, the exact evidence the
        # probe judges. Everything downstream of the mode check stays real.
        spec_path.write_text("{}", encoding="utf-8")
        real_lstat = Path.lstat

        def fifo_lstat(self, *args, **kwargs):
            st = real_lstat(self, *args, **kwargs)
            if self == spec_path:
                return os.stat_result((_stat.S_IFIFO | 0o600,) + tuple(st)[1:])
            return st

        monkeypatch.setattr(Path, "lstat", fifo_lstat)
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


def test_dashboard_author_stem_is_always_skipped_in_the_home_probe(monkeypatch, tmp_path):
    """The dashboard-author stem must never drive the shared-home verdict, regardless of
    sidecar confirmation.

    Its installer pins no ``KIROCREW_HOME`` in the managed server entry on either branch --
    the installer's own write omits it, and a leftover user file at the stem never had one.
    So this stem can never contribute a legitimate home-pin signal: letting it into the
    probe would make a foreign/absent pin read as foreign and decline every rebuild. That
    strands the governance ceiling both for OUR OWN file (ours, pin-less) AND for an
    untouched USER leftover at the stem (whose presence must not refuse creating every
    other required spec). It is therefore excluded from the pin probe unconditionally; its
    provenance is the digest record enforced at the gated sites, not a home pin.
    """
    from kiro_crew import agent

    own_home = (tmp_path / "my-home").resolve()
    foreign = (tmp_path / "someone-elses-home").resolve()
    monkeypatch.delenv("KIRO_HOME", raising=False)
    monkeypatch.delenv("KIROCREW_POD", raising=False)
    monkeypatch.setenv("KIROCREW_HOME", str(own_home))
    _durable_checkout(monkeypatch, agent)
    shared = tmp_path / "agents"
    shared.mkdir()
    # Our own self-pinned spec + a foreign-pinned dashboard-author file at the stem.
    (shared / agent.AGENT_FILENAME).write_text(_spec_pinned_to(str(own_home)), encoding="utf-8")
    da = shared / agent._DASHBOARD_AUTHOR_FILENAME
    da.write_text(_spec_pinned_to(str(foreign)), encoding="utf-8")
    _pretend_target_is_shared(monkeypatch, agent, shared)

    # The dashboard-author stem is excluded from the home-pin probe unconditionally (it
    # carries no KIROCREW_HOME pin on either branch), so the foreign-pinned file at the stem
    # never declines the rebuild -- the remaining self-pinned spec reads as ours.
    assert agent._existing_specs_are_mine(shared, own_home) is True


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
    # A third case, and a different kind. The AWS Control crew container runs as its
    # own process inside a Linux image where ``kiro_crew`` is not importable, so
    # ``supervisor/bundle.py`` re-implements this resolver rather than calling it.
    # The exempt file is that module's own TEST, which asserts what the
    # re-implementation returns against a tmp_path: it neither reads nor writes the
    # owner's home.
    "apps/builtins/aws_control/crew/runtime/container_tests/test_supervisor_bundle.py",
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


# --------------------------------------------------------------------------
# Derived specs obey the same owner as the template spec
# --------------------------------------------------------------------------
# ``rebuild_agent_config`` consults ``_decline_shared_agent_home`` and returns
# without writing when this instance may not own the shared agents directory.
# The DERIVED specs did not: ``kirocrew-worker.json``, the conductor agents, the
# service agents (guest/lite/knowledge/research) and the read-only side spec all
# write through ``agent._atomic_json_write`` on dispatch, long after boot, with
# no ownership check. A second gateway on its own ``KIROCREW_HOME`` therefore
# re-pinned the machine-wide ``kirocrew-worker.json`` to its own venv and data
# home every time it dispatched a worker, which is the exact poisoning the
# template-spec guard exists to prevent.
class TestForeignHomeDerivedSpecWrites:
    """A non-default home must not write DERIVED specs into a shared agents dir."""

    @staticmethod
    def _foreign_shared_dir(monkeypatch, agent_mod, tmp_path) -> Path:
        """A shared agents dir whose owned specs are pinned to ANOTHER home."""
        monkeypatch.delenv("KIRO_HOME", raising=False)
        monkeypatch.delenv("KIROCREW_POD", raising=False)
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "my-home"))
        _durable_checkout(monkeypatch, agent_mod)
        shared = tmp_path / "agents"
        shared.mkdir()
        (shared / agent_mod.AGENT_FILENAME).write_text(
            _spec_pinned_to(tmp_path / "someone-elses-home"), encoding="utf-8"
        )
        _pretend_target_is_shared(monkeypatch, agent_mod, shared)
        return shared

    def test_a_new_derived_spec_is_not_created(self, monkeypatch, tmp_path):
        """The write is refused, not merely warned about."""
        from kiro_crew import agent
        from kiro_crew.agent_files import WORKER_AGENT_FILENAME

        shared = self._foreign_shared_dir(monkeypatch, agent, tmp_path)
        target = shared / WORKER_AGENT_FILENAME

        with pytest.raises(agent.SharedAgentHomeRefused):
            agent._atomic_json_write(target, {"name": "kirocrew-worker"})

        assert (
            not target.exists()
        ), "a non-default-home instance created a derived spec in the shared agents dir"

    def test_the_refusal_is_an_oserror(self, monkeypatch, tmp_path):
        """Callers report the refusal through the ``except OSError`` arm they have.

        The dashboard's spec editor, the read-only side spec and the Connections
        mint each already treat a failed spec write as a failure with
        ``except OSError``. Making the refusal one is what turns a discarded
        edit into a reported failure in all of them at once, so the base class
        is the contract — not an implementation detail.
        """
        from kiro_crew import agent
        from kiro_crew.agent_files import WORKER_AGENT_FILENAME

        shared = self._foreign_shared_dir(monkeypatch, agent, tmp_path)

        assert issubclass(agent.SharedAgentHomeRefused, OSError)
        with pytest.raises(OSError):
            agent._atomic_json_write(shared / WORKER_AGENT_FILENAME, {"name": "x"})

    def test_an_existing_derived_spec_is_left_byte_identical(self, monkeypatch, tmp_path):
        """The harm is an OVERWRITE of the real install's working spec."""
        from kiro_crew import agent
        from kiro_crew.agent_files import WORKER_AGENT_FILENAME

        shared = self._foreign_shared_dir(monkeypatch, agent, tmp_path)
        target = shared / WORKER_AGENT_FILENAME
        sentinel = _spec_pinned_to(tmp_path / "someone-elses-home")
        target.write_text(sentinel, encoding="utf-8")

        with pytest.raises(agent.SharedAgentHomeRefused):
            agent._atomic_json_write(target, {"name": "kirocrew-worker", "sentinel": "mine"})

        assert (
            target.read_text(encoding="utf-8") == sentinel
        ), "a non-default-home instance re-pinned another home's derived spec"

    def test_a_real_derived_writer_is_covered(self, monkeypatch, tmp_path):
        """Not just the primitive: an actual materialization path must refuse too.

        ``_install_guest_agent`` is the smallest real writer (no locks, no
        dispatch machinery) and reaches the shared directory the same way the
        other writers that go through :func:`_atomic_json_write` do.
        """
        from kiro_crew import agent
        from kiro_crew.agent_files import GUEST_AGENT_FILENAME
        from kiro_crew.agent_materialization import service_agents

        shared = self._foreign_shared_dir(monkeypatch, agent, tmp_path)

        with pytest.raises(agent.SharedAgentHomeRefused):
            service_agents._install_guest_agent()

        assert not (
            shared / GUEST_AGENT_FILENAME
        ).exists(), "a real derived-spec writer bypassed the shared-home ownership guard"

    def test_every_denial_is_audited(self, monkeypatch, tmp_path):
        """Every refusal records its own SEL event; only the WARNING is deduped.

        Derived specs are written per dispatch, so the second and later
        refusals against one directory are the ordinary path rather than noise.
        ``_declined_home_warned`` exists to stop the operator-facing log line
        repeating ~1/min for the process lifetime, and it says in its own
        comment that the denied event is deliberately not throttled: a
        permission decision over the shared resource that is absent from the
        audit trail cannot be reviewed afterwards.

        Recording here is also what covers a flip after boot. Boot evaluates
        the guard before any ownership change and returns ``None``, recording
        nothing, so a flip afterwards would otherwise leave every derived
        denial untraced until the next rebuild.
        """
        from kiro_crew import agent
        from kiro_crew.agent_files import GUEST_AGENT_FILENAME, WORKER_AGENT_FILENAME

        shared = self._foreign_shared_dir(monkeypatch, agent, tmp_path)
        events: list[dict] = []
        warned: list[tuple] = []

        class _Sel:
            def log_api_access(self, **kw):
                events.append(kw)

        monkeypatch.setattr(agent, "sel", lambda: _Sel())
        monkeypatch.setattr(agent, "_declined_home_warned", set())
        monkeypatch.setattr(
            agent.logger, "warning", lambda *a, **k: warned.append(a), raising=False
        )

        for name in (WORKER_AGENT_FILENAME, GUEST_AGENT_FILENAME):
            with pytest.raises(agent.SharedAgentHomeRefused):
                agent._atomic_json_write(shared / name, {"name": "x"})

        denied = [e for e in events if e.get("operation") == "agent_home_write"]
        assert len(denied) == 2, (
            f"expected one audited denial per refused write, got {len(denied)}; "
            f"a throttled audit trail loses permission decisions"
        )
        assert all(e["outcome"] == "denied" for e in denied)
        assert all(e["source"] == "derived-spec" for e in denied)
        assert all(str(shared) in e["resources"] for e in denied)
        assert len(warned) == 1, (
            f"the operator-facing WARNING must still dedupe per directory, "
            f"got {len(warned)} lines"
        )

    def test_the_worker_mirror_is_not_recorded_as_derived(self, monkeypatch, tmp_path):
        """A refused worker write must not stamp the mirror as re-derived.

        This is the consequence that is not merely cosmetic. ``_write_worker_spec``
        records ``set_mirrored_from`` immediately after the write, and the spawn
        path's freshness gate reads that record: a mirror stamped for bytes that
        were never written passes the gate while still carrying grants revoked
        from ``kirocrew.json``. The refusal has to stop the bookkeeping, not just
        the bytes.
        """
        from kiro_crew import agent, agent_state
        from kiro_crew.agent_files import WORKER_AGENT_FILENAME
        from kiro_crew.agent_materialization import worker_agent

        shared = self._foreign_shared_dir(monkeypatch, agent, tmp_path)
        recorded: list[tuple] = []
        monkeypatch.setattr(
            agent_state,
            "set_mirrored_from",
            lambda *a, **k: recorded.append(a),
        )

        with pytest.raises(OSError):
            worker_agent._write_worker_spec(
                {"name": "kirocrew-worker"},
                shared / WORKER_AGENT_FILENAME,
                template_grants=[],
            )

        assert not recorded, (
            "a refused worker spec write still recorded the mirror as re-derived; "
            "the spawn-path freshness gate would accept revoked grants"
        )

    @pytest.mark.parametrize(
        "module, function, needs_specific_arm",
        [
            ("kiro_crew.dashboard.agent_admin.agent_detail", "api_agent_detail", True),
            ("kiro_crew.dashboard.side_readonly_spec", "publish_readonly_spec", False),
            ("kiro_crew.dashboard.handlers.mcp", "_sync_mcp_to_agent_unlocked", True),
            ("kiro_crew.dashboard.handlers.mcp", "_sync_mcp_to_agent_batch_unlocked", True),
        ],
    )
    def test_person_facing_writers_report_the_refusal(self, module, function, needs_specific_arm):
        """Each path that reports success to a PERSON carries an arm for the refusal.

        Named one function at a time rather than scanned per module on purpose:
        a module also holds writes this guard never refuses (the global MCP
        registry, which is not in the agents directory) and generic plumbing
        whose caller chooses the path, so a module-wide scan reports on code
        this contract is not about.

        The arm is what separates "this edit was discarded" from "this edit was
        saved". Without one the dashboard answered 200 on a spec it never wrote,
        and the side spec and the mint handed out an agent name with no file
        behind it.

        ``needs_specific_arm`` is the difference between an arm that REPORTS the
        refusal and one that merely catches it. The MCP syncs hold an
        ``except OSError`` that logs a warning and returns, so inheriting from
        ``OSError`` is precisely what made the refusal look like a successful
        removal; those must name the subtype. ``publish_readonly_spec`` turns
        its ``OSError`` into a raised ``ReadOnlySpecError``, so a generic arm
        there already fails the call rather than reporting a save.
        """
        import ast as _ast
        import importlib

        mod = importlib.import_module(module)
        tree = _ast.parse(Path(mod.__file__).read_text(encoding="utf-8"))
        target = next(
            (
                node
                for node in _ast.walk(tree)
                if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef))
                and node.name == function
            ),
            None,
        )
        assert target is not None, f"{module}.{function} is gone; has the call site moved?"

        arms = [
            _ast.unparse(handler.type) if handler.type is not None else "bare"
            for node in _ast.walk(target)
            if isinstance(node, _ast.Try)
            for handler in node.handlers
        ]
        if needs_specific_arm:
            assert any("SharedAgentHomeRefused" in arm for arm in arms), (
                f"{module}.{function} catches the refusal only as a generic OSError "
                f"(arms found: {arms}); that arm logs and returns, so the caller still "
                f"reports a removal the spec never took"
            )
        else:
            assert any(
                "SharedAgentHomeRefused" in arm or "OSError" in arm or arm == "bare" for arm in arms
            ), (
                f"{module}.{function} has no arm that catches the shared-home refusal "
                f"(arms found: {arms}); a refused spec write would be reported as a save"
            )

    @pytest.mark.asyncio
    async def test_the_mcp_remove_reports_the_refusal(self, monkeypatch, tmp_path):
        """A refused removal must not answer ``removed: true``.

        This is the security-shaped half. The handler popped the entry from the
        config it held in memory, the write was refused, and the generic
        ``except OSError`` arm in the sync logged a warning and returned — so
        the operator was told the server was uninstalled while the spec its own
        sessions load kept it in ``mcpServers`` with whatever ``autoApprove``
        it carried, which is a grant that never reaches the PreToolUse gate.
        """
        import json
        from unittest.mock import MagicMock

        from aiohttp import web
        from body_stream_helpers import attach_body

        from kiro_crew import agent
        from kiro_crew.dashboard.handlers import mcp as mcp_mod
        from kiro_crew.dashboard.handlers.mcp import api_mcp_remove

        shared = self._foreign_shared_dir(monkeypatch, agent, tmp_path)
        monkeypatch.setattr(agent, "KIRO_AGENTS_DIR", shared)
        spec = shared / agent.AGENT_FILENAME
        before = spec.read_text(encoding="utf-8")

        global_mcp = tmp_path / "settings" / "mcp.json"
        global_mcp.parent.mkdir(parents=True, exist_ok=True)
        global_mcp.write_text(
            json.dumps({"mcpServers": {"some-server": {"command": "x"}}}), encoding="utf-8"
        )
        monkeypatch.setattr(mcp_mod, "_GLOBAL_MCP_JSON", global_mcp)
        # The lock sidecar is a module constant derived from the config path at
        # IMPORT time, so redirecting only the config leaves the handler touching
        # a lock under whatever HOME the suite happened to have when this module
        # was first imported -- a FileNotFoundError that depends on test order.
        monkeypatch.setattr(mcp_mod, "_MCP_LOCK_PATH", global_mcp.with_suffix(".lock"))

        async def _no_denial(request, operation):
            return None

        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers._shared.require_owner_dashboard_request",
            _no_denial,
        )

        request = MagicMock(spec=web.Request)
        request.app = {"state": MagicMock()}
        attach_body(request, {"name": "some-server"})

        async def _json():
            return {"name": "some-server"}

        request.json = _json
        resp = await api_mcp_remove(request)

        assert resp.status == 409, "a refused removal was reported as a successful uninstall"
        assert b"agent_home_not_owned" in resp.body
        assert b'"removed": true' not in resp.body
        assert (
            spec.read_text(encoding="utf-8") == before
        ), "the refused removal still modified the spec on disk"
        # The registry is the mutation that runs FIRST. Refusing only at the
        # spec write would leave the server gone from its source while the
        # spec this instance cannot rewrite keeps mounting it.
        assert (
            "some-server" in json.loads(global_mcp.read_text(encoding="utf-8"))["mcpServers"]
        ), "the refused removal still purged the user-level registry"

    @pytest.mark.asyncio
    async def test_the_mcp_toggle_reports_the_refusal(self, monkeypatch, tmp_path):
        """A refused disable must not answer ``applied: true``.

        Same shape as the removal: the toggle marks the entry ``disabled`` in
        memory, and a swallowed refusal leaves the server mounted while the UI
        shows it off.
        """
        import json
        from unittest.mock import MagicMock

        from aiohttp import web
        from body_stream_helpers import attach_body

        from kiro_crew import agent
        from kiro_crew.dashboard.handlers import mcp as mcp_mod
        from kiro_crew.dashboard.handlers.mcp import api_mcp_toggle

        shared = self._foreign_shared_dir(monkeypatch, agent, tmp_path)
        monkeypatch.setattr(agent, "KIRO_AGENTS_DIR", shared)
        spec = shared / agent.AGENT_FILENAME
        before = spec.read_text(encoding="utf-8")

        global_mcp = tmp_path / "settings" / "mcp.json"
        global_mcp.parent.mkdir(parents=True, exist_ok=True)
        global_mcp.write_text(
            json.dumps({"mcpServers": {"some-server": {"command": "x"}}}), encoding="utf-8"
        )
        monkeypatch.setattr(mcp_mod, "_GLOBAL_MCP_JSON", global_mcp)
        # The lock sidecar is a module constant derived from the config path at
        # IMPORT time, so redirecting only the config leaves the handler touching
        # a lock under whatever HOME the suite happened to have when this module
        # was first imported -- a FileNotFoundError that depends on test order.
        monkeypatch.setattr(mcp_mod, "_MCP_LOCK_PATH", global_mcp.with_suffix(".lock"))

        async def _no_denial(request, operation):
            return None

        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers._shared.require_owner_dashboard_request",
            _no_denial,
        )

        request = MagicMock(spec=web.Request)
        request.app = {"state": MagicMock()}
        attach_body(request, {"name": "some-server", "enabled": False})

        async def _json():
            return {"name": "some-server", "enabled": False}

        request.json = _json
        resp = await api_mcp_toggle(request)

        assert resp.status == 409, "a refused disable was reported as applied"
        assert b"agent_home_not_owned" in resp.body
        assert b'"applied": true' not in resp.body
        assert (
            spec.read_text(encoding="utf-8") == before
        ), "the refused toggle still modified the spec on disk"
        # Same ordering: the disabled flag is written to the registry before
        # the spec, so a late refusal shows the row off while the spec mounts it.
        assert (
            "disabled"
            not in json.loads(global_mcp.read_text(encoding="utf-8"))["mcpServers"]["some-server"]
        ), "the refused toggle still wrote the registry flag"

    def test_the_spawn_gate_names_the_unowned_home(self, monkeypatch, tmp_path):
        """A refused mirror re-derive must not read as a generation mismatch.

        ``rederive_worker_agent`` never raises, so without an attributed
        refusal the spawn gate reports a mirror that "could not be
        re-derived": a permanent dispatch outage described as stale
        bookkeeping, with nothing naming the directory or the remedy. The gate
        asks the ownership question itself, ahead of the re-derive, for the
        same reason it refuses a foreign worker spec there rather than later.

        The remedy named is the existing non-default-home one -- remove the
        stale ``kirocrew*.json`` specs and restart -- deliberately NOT
        ``KIRO_HOME``, whose own scope caveat says it relocates kiro-cli's
        session storage that Kiro Crew still reads from the host path. The
        rebuild's own refusal suggests no remedy for exactly that reason.
        """
        import json

        from kiro_crew import agent
        from kiro_crew.agent_materialization import worker_agent

        shared = self._foreign_shared_dir(monkeypatch, agent, tmp_path)
        monkeypatch.setattr(agent, "KIRO_AGENTS_DIR", shared)
        # A mirror that exists, parses, and carries the two marks every
        # derivation writes (the declared name and a kirocrew-work reference),
        # so the gate attributes it as ours and reaches the re-derive instead
        # of refusing earlier as a foreign spec.
        (shared / worker_agent._WORKER_AGENT_FILENAME).write_text(
            json.dumps(
                {
                    "name": "kirocrew-worker",
                    "mcpServers": {"kirocrew-work": {"command": "x"}},
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(worker_agent, "_derived_spec_matches_default", lambda name: False)

        with pytest.raises(worker_agent.DerivedSpecStale) as caught:
            worker_agent._require_fresh_worker_spec(None)

        message = str(caught.value)
        assert str(shared) in message, "the refusal does not name the directory it cannot write"
        assert "kirocrew*.json" in message, "the refusal does not name the remedy"

    def test_a_refused_fork_refresh_blocks_its_sessions(self, monkeypatch, tmp_path):
        """An ownership refusal must record the fork as unrefreshed.

        ``_refresh_forked_templates_locked`` adds a fork to
        ``_fork_refresh_failed``, and ``require_fork_governance`` then refuses
        to start a session on it. A refusal has to count: ``allowedTools`` and
        ``autoApprove`` are the grants that never reach the PreToolUse gate, so
        this record is the only control between a tightened ceiling and a call
        the operator revoked. Nothing else closes the gap either -- the fork
        inventory and its crew binding are both read per data home, so a fork
        corroborated here is iterated by no other instance. Blocking the
        fork's sessions is the correct outcome; the remedy is a KIRO_HOME of
        this instance's own, which the refusal's warning names.
        """
        import json
        from types import SimpleNamespace

        from kiro_crew import agent, agent_state
        from kiro_crew.agent_materialization import fork_refresh

        shared = self._foreign_shared_dir(monkeypatch, agent, tmp_path)
        fork_name = "kirocrew-fork-of-something"
        crew = "some-crew"
        (shared / f"{fork_name}.json").write_text(
            json.dumps({"name": fork_name, "mcpServers": {}}),
            encoding="utf-8",
        )

        # The fork must clear every gate BEFORE the write, or the test would
        # pass on a fork that never reached the refused call at all: lineage
        # onto an owned template, a crew binding that corroborates it, and no
        # capability record (that branch reconciles and returns early).
        monkeypatch.setattr(
            agent_state,
            "all_fork_info",
            lambda: {fork_name: {"forked_from": "kirocrew", "private_to": crew}},
        )
        monkeypatch.setattr(agent_state, "get_capabilities", lambda name: None)

        from kiro_crew.config.loader import KiroCrewConfig

        monkeypatch.setattr(
            KiroCrewConfig,
            "load",
            classmethod(
                lambda cls: SimpleNamespace(agents={crew: SimpleNamespace(kiro_agent=fork_name)})
            ),
        )
        monkeypatch.setattr(fork_refresh, "_fork_refresh_failed", frozenset())

        fork_refresh._refresh_forked_templates_locked(gated_off=frozenset())

        assert fork_name in fork_refresh._fork_refresh_failed, (
            "a refused fork refresh left the fork unrecorded; require_fork_governance "
            "would admit sessions on allowedTools and autoApprove this instance never "
            "re-filtered"
        )

    @pytest.mark.asyncio
    async def test_the_dashboard_patch_reports_the_refusal(self, monkeypatch, tmp_path):
        """A PATCH on a mis-homed gateway must not answer 200 for a discarded edit.

        The whole harm of a silent refusal, end to end: the handler replied
        ``{"ok": true}`` with the merged state it had computed in memory while
        the file on disk was byte-identical, so the editor showed the save as
        applied and the next GET quietly disagreed. The refusal now reaches the
        handler, which reports it and leaves the file alone.
        """
        from unittest.mock import MagicMock

        from aiohttp import web

        from kiro_crew import agent
        from kiro_crew.dashboard.handlers.agents import api_agent_detail

        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
            lambda request: True,
        )
        shared = self._foreign_shared_dir(monkeypatch, agent, tmp_path)
        spec = shared / "kirocrew.json"
        before = spec.read_text(encoding="utf-8")

        request = MagicMock(spec=web.Request)
        request.method = "PATCH"
        request.match_info = {"name": "kirocrew"}
        request.app = {"state": MagicMock()}

        async def _json():
            return {"model": "a-model-the-user-just-picked"}

        request.json = _json

        monkeypatch.setattr(agent, "KIRO_AGENTS_DIR", shared)
        resp = await api_agent_detail(request)

        assert resp.status != 200, "a discarded edit was reported as a successful save"
        assert resp.status == 409
        assert b"agent_home_not_owned" in resp.body
        assert (
            spec.read_text(encoding="utf-8") == before
        ), "the refused PATCH still modified the spec on disk"
        # The model branch writes a per-home sidecar BEFORE the refusable spec
        # write, so a refusal taken only at the write answered 409 with the pin
        # already flipped: the sidecar then records a model the spec on disk
        # never carried. The ownership question is asked ahead of it instead.
        from kiro_crew import agent_state

        assert not agent_state._state_path().exists(), (
            "the refused model PATCH still wrote the per-home sidecar, which now "
            "disagrees with the unwritten spec"
        )

    def test_a_broken_audit_sink_still_refuses(self, monkeypatch, tmp_path):
        """A lost record must not turn a refusal into a write.

        The audit calls are tolerant because an unwritable SEL log must cost one
        record and not the agent a conductor dispatches to -- which is the
        module's existing contract for every other audit in it. Tolerant is not
        the same as skipped: the decision has to stand on its own, so the
        refusal still holds with the sink raising on every call.
        """
        from kiro_crew import agent
        from kiro_crew.agent_files import WORKER_AGENT_FILENAME

        shared = self._foreign_shared_dir(monkeypatch, agent, tmp_path)

        class _Broken:
            def log_api_access(self, **kw):
                raise OSError("audit sink unavailable")

        monkeypatch.setattr(agent, "sel", lambda: _Broken())

        with pytest.raises(agent.SharedAgentHomeRefused):
            agent._atomic_json_write(shared / WORKER_AGENT_FILENAME, {"name": "x"})
        assert not (
            shared / WORKER_AGENT_FILENAME
        ).exists(), "a broken audit sink let a foreign-home write through"

    def test_the_mint_write_is_authorized_not_exempt_by_name(self, monkeypatch, tmp_path):
        """A Connect must still work on an instance that owns nothing here.

        The mint writes ONE server entry, copied verbatim out of the owner's own
        on-disk spec, into a uniquely named file its manifest sweep reaps. There
        is nothing of this instance in it, so refusing it protects nothing and
        fails every OAuth Connect on a relocated or worktree-booted instance.

        Authorized by the WRITER, at the call, rather than by a filename this
        guard recognizes: a name is not evidence of who produced the bytes, so a
        guard trusting one would let any writer reach the shared directory by
        choosing the right name. Both halves are pinned -- an authorized write
        lands, and the same path without the authorization does not.
        """
        from kiro_crew import agent
        from kiro_crew.connections.mint import _mint_spec_name

        shared = self._foreign_shared_dir(monkeypatch, agent, tmp_path)
        target = shared / f"{_mint_spec_name('some-provider')}.json"

        agent.write_owner_derived_spec(target, {"name": target.stem, "mcpServers": {}})
        assert target.is_file(), (
            "the ownership guard refused an authorized mint write; every Connect on a "
            "non-owning instance would fail"
        )

        unauthorized = shared / f"{_mint_spec_name('other-provider')}.json"
        with pytest.raises(agent.SharedAgentHomeRefused):
            agent._atomic_json_write(unauthorized, {"name": unauthorized.stem})
        assert not unauthorized.exists(), "a mint-shaped NAME bought access to the directory"

    def test_the_mint_itself_passes_the_authorization(self):
        """The mint's own write is the authorized one, asserted at its call site.

        The entry point only helps if the real writer calls it; a test that calls
        it directly proves the guard honours it and nothing about the mint. Read
        structurally so a refactor that routes the mint back through the guarded
        primitive is a red here rather than a failed Connect in production.
        """
        import ast as _ast

        source = (
            Path(__file__).resolve().parents[1] / "src" / "kiro_crew" / "connections" / "mint.py"
        ).read_text(encoding="utf-8")
        authorized = [
            node.lineno
            for node in _ast.walk(_ast.parse(source))
            if isinstance(node, _ast.Call)
            and isinstance(node.func, _ast.Attribute)
            and node.func.attr == "write_owner_derived_spec"
        ]
        assert authorized, (
            "no spec write in connections/mint.py goes through "
            "write_owner_derived_spec; the ownership guard would refuse the mint spec "
            "and every Connect on a non-owning instance would fail"
        )

    def test_the_batch_uninstall_arms_its_purge_against_the_refusal(self):
        """The batched uninstall's purge must not surface the refusal as a 500.

        ``_purge_server_config`` strips every scope FIRST and the rendered spec
        last, so a refusal there leaves the sources gone while the spec still
        mounts the server with its ``autoApprove`` -- and nothing reconciles it,
        because this instance's own rebuild is declined too. Uncaught it
        propagated as a 500, past the post-apply recheck, and the name was never
        recorded as purged so the guaranteed-cleanup sweep re-attempted the same
        refused write.

        Read structurally: the arm's absence is the hazard, and reaching it
        behaviourally needs the whole two-phase apply plus an ownership flip
        mid-request.
        """
        import ast as _ast

        source = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "kiro_crew"
            / "dashboard"
            / "handlers"
            / "mcp.py"
        ).read_text(encoding="utf-8")
        tree = _ast.parse(source)
        apply_fn = next(
            node
            for node in _ast.walk(tree)
            if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef))
            and node.name == "_do_mcp_apply"
        )
        armed = False
        for node in _ast.walk(apply_fn):
            if not isinstance(node, _ast.Try):
                continue
            calls = {
                inner.func.id
                for inner in _ast.walk(node)
                if isinstance(inner, _ast.Call) and isinstance(inner.func, _ast.Name)
            }
            if "_purge_server_config" not in calls and "_offload_config_write" not in calls:
                continue
            for handler in node.handlers:
                if handler.type is not None and "SharedAgentHomeRefused" in _ast.dump(handler.type):
                    armed = True
        assert armed, (
            "the batched apply's purge has no arm for the shared-home refusal; the scopes "
            "are stripped before the spec, so a refusal there answers 500 and leaves the "
            "server mounted with nothing to reconcile it"
        )

    def test_the_default_home_instance_still_writes_derived_specs(self, monkeypatch, tmp_path):
        """The other half of the contract: the real install keeps owning them."""
        from kiro_crew import agent
        from kiro_crew.agent_files import WORKER_AGENT_FILENAME

        monkeypatch.delenv("KIRO_HOME", raising=False)
        monkeypatch.delenv("KIROCREW_HOME", raising=False)
        monkeypatch.delenv("KIROCREW_POD", raising=False)
        _durable_checkout(monkeypatch, agent)
        shared = tmp_path / "agents"
        shared.mkdir()
        _pretend_target_is_shared(monkeypatch, agent, shared)
        target = shared / WORKER_AGENT_FILENAME

        agent._atomic_json_write(target, {"name": "kirocrew-worker"})

        assert target.is_file(), "the default-home instance must still write derived specs"

    def test_self_pinned_specs_keep_their_writer(self, monkeypatch, tmp_path):
        """A relocated install that wrote these specs must not lock itself out."""
        from kiro_crew import agent
        from kiro_crew.agent_files import WORKER_AGENT_FILENAME

        own_home = (tmp_path / "relocated-home").resolve()
        monkeypatch.delenv("KIRO_HOME", raising=False)
        monkeypatch.delenv("KIROCREW_POD", raising=False)
        monkeypatch.setenv("KIROCREW_HOME", str(own_home))
        _durable_checkout(monkeypatch, agent)
        shared = tmp_path / "agents"
        shared.mkdir()
        (shared / agent.AGENT_FILENAME).write_text(_spec_pinned_to(own_home), encoding="utf-8")
        _pretend_target_is_shared(monkeypatch, agent, shared)
        target = shared / WORKER_AGENT_FILENAME

        agent._atomic_json_write(target, {"name": "kirocrew-worker"})

        assert target.is_file(), "provenance matching must let the writer keep refreshing"

    def test_a_private_target_is_untouched(self, monkeypatch, tmp_path):
        """A redirected (private) agents dir is nobody else's — write it."""
        from kiro_crew import agent
        from kiro_crew.agent_files import WORKER_AGENT_FILENAME

        monkeypatch.delenv("KIRO_HOME", raising=False)
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "my-home"))
        monkeypatch.delenv("KIROCREW_POD", raising=False)
        _durable_checkout(monkeypatch, agent)
        private = tmp_path / "private-agents"
        private.mkdir()
        monkeypatch.setattr(agent, "KIRO_AGENTS_DIR", private)
        monkeypatch.setattr(agent, "ambient_agents_dir", lambda: tmp_path / "ambient-agents")
        target = private / WORKER_AGENT_FILENAME

        agent._atomic_json_write(target, {"name": "kirocrew-worker"})

        assert target.is_file(), "a private target must not be guarded"

    def test_a_non_spec_write_is_never_guarded(self, monkeypatch, tmp_path):
        """The guard is scoped to the agents dir; other JSON writers are untouched."""
        from kiro_crew import agent

        self._foreign_shared_dir(monkeypatch, agent, tmp_path)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        target = elsewhere / "config.json"

        agent._atomic_json_write(target, {"unrelated": True})

        assert target.is_file(), "a write outside the agents dir must not be refused"


# --------------------------------------------------------------------------
# The Kiro user-level MCP registry stays on the home its writer writes
# --------------------------------------------------------------------------
# One hard-coded ``Path.home() / ".kiro" / "settings" / "mcp.json"`` looks like
# two questions -- the servers a rebuild MERGES into the specs it writes, and
# the restrictions a session reads -- and it is tempting to isolate the first
# because it has no writer. It cannot be isolated: the same read also carries
# the per-server mute, whose writer resolves a fixed home, so a split reads an
# operator's switch-off as absent. This test holds the whole registry to the
# file its writer writes.
class TestTheMcpRegistryStaysWithItsWriter:
    """The user-level MCP registry is read from the file its writer writes.

    An earlier revision of this change pointed the SPEC-MERGE read at a
    ``KIRO_HOME``-scoped registry, on the reasoning that a read-only input with
    no writer is safe to isolate. It is not: the same read supplies the
    per-server ``disabled`` mute, whose writer is the dashboard's fixed
    ``Path.home()`` registry. Isolated, the mute reads as absent, the rebuild
    pops ``disabled`` from the rendered entry and re-appends the server to
    ``allowedTools`` -- the list that never reaches the PreToolUse gate -- so an
    operator's switch-off came back auto-approved. Reader and writer move
    together or not at all.
    """

    def test_the_restriction_reader_stays_with_its_writer(self, tmp_path):
        """The registry a RESTRICTION and a MUTE are read from must not follow ``KIRO_HOME``.

        ``dashboard.handlers.mcp`` writes both ``disabledTools`` and the
        per-server ``disabled`` mute to a fixed
        ``Path.home() / ".kiro" / "settings" / "mcp.json"`` that ignores
        ``KIRO_HOME``, and for Crew's own managed servers that file is the only
        place such an entry can live. Moving the reader alone reads the entry as
        absent: the restriction degrades to allow, and the rebuild pops
        ``disabled`` from the rendered entry and re-appends the server to
        ``allowedTools``, which never reaches the PreToolUse gate. So the
        registry is pinned to the host until its writer moves with it.
        """
        kiro_home_dir = tmp_path / "gateway-kiro"
        kiro_home_dir.mkdir()
        probe = (
            "import json, pathlib\n"
            "from kiro_crew import agent\n"
            "from kiro_crew.agent_materialization import mcp_sources\n"
            "from kiro_crew.dashboard.handlers import mcp as mcp_handlers\n"
            "import inspect\n"
            "print(json.dumps({\n"
            "    'reader': str(agent._KIRO_MCP_JSON),\n"
            "    'merge_names_the_constant':\n"
            "        '_KIRO_MCP_JSON' in inspect.getsource(mcp_sources.merge_mcp_sources),\n"
            "    'writer': str(mcp_handlers._GLOBAL_MCP_JSON),\n"
            "    'home': str(pathlib.Path.home()),\n"
            "}))\n"
        )
        env = _child_env(
            KIRO_HOME=str(kiro_home_dir),
            PYTHONPYCACHEPREFIX=str(tmp_path / "pycache"),
        )
        proc = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            env=env,
            cwd=str(REPO_ROOT),
            **UTF8_TEXT,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        import json as _json

        seen = _json.loads(proc.stdout.strip().splitlines()[-1])
        assert seen["reader"] == seen["writer"], (
            "the MCP restriction reader and its writer must name the SAME file; "
            "a split lets a disabled tool read as enabled"
        )
        assert seen["reader"] == str(
            Path(seen["home"]) / ".kiro" / "settings" / "mcp.json"
        ), "the restriction registry must stay on the host home its writer uses"
        assert str(kiro_home_dir) not in seen["reader"]
        assert seen["merge_names_the_constant"], (
            "the rebuild's merge reads some other registry than the one its writer "
            "writes; the per-server mute lives in that file, so a split re-enables a "
            "muted server and re-appends it to allowedTools"
        )


def _stub_disconnect_collaborators(monkeypatch, *, url: str, purge) -> None:
    """Stub everything ``remove_provider_entry`` reaches, leaving its judgment real.

    Patched at each name's DEFINING module rather than on
    ``kiro_crew.connections.ownership``: that function imports ``list_servers``,
    ``revoke_local_grant``, ``surviving_grant_artifacts`` and
    ``_purge_server_config`` inside its own body, so the module attribute a test
    would patch is never read and such a patch silently does nothing -- the real
    collaborators run instead and the assertion measures them. The function-local
    import does run on every call, which is why patching the source module works.

    ``spec_census`` is the exception: it is defined in ``ownership`` itself, so it
    is patched there. It returns one ``kirocrew``-scope entry at ``url``, which is
    the minimum that makes the slug OWNED (``is_scope_label`` rejects ``agent:``
    and ``mirror:`` labels) with no second holder, so the census is complete and
    the revoke is reached. The ownership judgment, the lock and the shielded
    offload stay real, because they decide the field under test.
    """
    monkeypatch.setattr(
        "kiro_crew.connections.ownership.spec_census",
        lambda *a, **k: ({"kirocrew": {"demo": {"url": url}}}, ()),
    )
    monkeypatch.setattr("kiro_crew.mcp_discovery.list_servers", lambda *a, **k: [])
    monkeypatch.setattr("kiro_crew.mcp_grant.revoke_local_grant", lambda *a, **k: [])
    monkeypatch.setattr("kiro_crew.mcp_grant.surviving_grant_artifacts", lambda *a, **k: ())
    monkeypatch.setattr("kiro_crew.dashboard.handlers.mcp._purge_server_config", purge)
    monkeypatch.setattr("kiro_crew.agent.rebuild_agent_config", lambda *a, **k: None)
    monkeypatch.setattr(
        "kiro_crew.connections.warm.rearm_invalidated_provider", lambda *a, **k: None
    )


# A refusal is not a commit, and that applies to what the caller is TOLD as much
# as to what lands on disk. Both paths below reach the operator through the
# dashboard: one renders "entry removed", the other asserts a package "was
# uninstalled". Each is read as the end of the work, so a refusal reported as a
# success is what stops the operator from ever fixing the one thing still broken.
class TestARefusedWriteIsNotReportedAsDone:
    """A refused spec write must not be reported to the caller as a completed one."""

    @pytest.mark.asyncio
    async def test_a_refused_entry_purge_is_not_reported_as_removed(self, monkeypatch):
        """A disconnect whose config purge is refused must answer ``entry_removed`` false.

        The purge is deliberately allowed to fail without stopping the grant
        revoke: a live credential is worse than a stale entry. But the refusal
        leaves the server configured, and this instance's own
        ``rebuild_agent_config`` is declined too, so nothing reconciles it. The
        dashboard publishes this field as ``entryRemoved``, so reporting the
        removal sends the operator away from the only repair left.
        """
        from kiro_crew.agent import SharedAgentHomeRefused
        from kiro_crew.connections import ownership as own

        url = "https://example.invalid/mcp"
        refused: list[str] = []

        def _purge(*_a, **_k):
            refused.append("purge")
            raise SharedAgentHomeRefused("another data home owns the shared agent specs")

        _stub_disconnect_collaborators(monkeypatch, url=url, purge=_purge)

        scope = await own.remove_provider_entry("demo", url, ())

        assert refused == ["purge"], "the purge never ran, so this proves nothing about a refusal"
        assert scope.entry_removed is False, (
            "a refused config purge was reported as entry_removed; the dashboard renders "
            "that as removed while the server is still mounted with its autoApprove"
        )

    @pytest.mark.asyncio
    async def test_a_permitted_entry_purge_is_still_reported_as_removed(self, monkeypatch):
        """The control: a purge that COMMITS must still report the removal.

        Without this, making the refusal honest could be satisfied by reporting
        false unconditionally, which would tell every operator their disconnect
        failed.
        """
        from kiro_crew.connections import ownership as own

        url = "https://example.invalid/mcp"
        _stub_disconnect_collaborators(
            monkeypatch, url=url, purge=lambda *_a, **_k: {"purged": True}
        )

        scope = await own.remove_provider_entry("demo", url, ())

        assert scope.entry_removed is True, "a committed purge must still report the removal"


# The guard is asked at the primitive, which is also reached by the rebuild's own
# derived installs. Those run AFTER the rebuild has written kirocrew.json, and the
# temp-checkout arm declines precisely when a spec is present -- so a guard that
# re-derives its answer per write refuses the rebuild against the file it just
# created. That is a fresh install left with its main spec and none of the derived
# ones, and a conductor that cannot dispatch a worker at all.
class TestTheRebuildsOwnDecisionCoversItsDerivedSpecs:
    """One ownership decision per rebuild, not one per spec it writes."""

    @staticmethod
    def _temp_checkout(monkeypatch, agent_mod, shared: Path) -> None:
        """Present a plain clone under the system temp root, with an empty shared dir.

        Built under ``tempfile.gettempdir()`` rather than ``tmp_path`` for the
        reason ``test_declines_from_a_clone_under_the_temp_dir`` gives: pytest's
        basetemp is created before the suite redirects the tempfile base, so
        ``tmp_path`` is not under the root the predicate answers against and the
        arm under test would be missed.
        """
        monkeypatch.delenv("KIRO_HOME", raising=False)
        monkeypatch.delenv("KIROCREW_HOME", raising=False)
        monkeypatch.delenv("KIROCREW_POD", raising=False)
        _pretend_target_is_shared(monkeypatch, agent_mod, shared)

    def test_a_fresh_temp_install_is_refused_once_its_own_main_spec_exists(
        self, monkeypatch, tmp_path
    ):
        """The defect, stated as the guard's own answer flipping mid-rebuild.

        With an empty shared directory the temp arm ALLOWS the write -- there is
        nothing to preserve, which ``test_does_not_decline_from_a_temp_clone_when
        _no_spec_exists`` already pins. Writing ``kirocrew.json`` is what makes
        the next answer different, and this test holds that unchanged behaviour
        of :func:`_decline_shared_agent_home` so the fix is visibly about the
        rebuild's scope rather than about weakening the arm.
        """
        import tempfile

        from kiro_crew import agent

        shared = tmp_path / "agents"
        shared.mkdir()
        with tempfile.TemporaryDirectory(prefix="kc-fresh-") as scratch_name:
            clone = Path(scratch_name) / "repo"
            (clone / "src" / "kiro_crew").mkdir(parents=True)
            (clone / ".git").mkdir()  # a DIRECTORY -> ordinary clone, not a worktree
            monkeypatch.setattr(agent, "__file__", str(clone / "src" / "kiro_crew" / "agent.py"))
            self._temp_checkout(monkeypatch, agent, shared)

            assert agent._decline_shared_agent_home(audit=False) is None, (
                "an empty shared home must be writable from a temp clone; this test's "
                "premise is gone and the fix below proves nothing"
            )

            (shared / agent.AGENT_FILENAME).write_text("{}", encoding="utf-8")

            assert agent._decline_shared_agent_home(audit=False) is not None, (
                "the temp arm no longer declines once a spec is present, so the "
                "mid-rebuild flip this class exists for cannot happen"
            )

    def test_a_derived_write_inside_an_admitted_rebuild_is_not_refused(self, monkeypatch, tmp_path):
        """The fix: the rebuild's admission covers the specs it installs.

        Driven through :func:`_declined_foreign_spec_write`, the function every
        derived writer reaches via ``_atomic_json_write``, in exactly the state
        the rebuild is in when it installs them: a temp checkout whose main spec
        is already on disk. Without the fix this answers True and the knowledge,
        research, heartbeat, conductor and worker specs are all refused.
        """
        import tempfile

        from kiro_crew import agent
        from kiro_crew.agent_files import WORKER_AGENT_FILENAME

        shared = tmp_path / "agents"
        shared.mkdir()
        (shared / agent.AGENT_FILENAME).write_text("{}", encoding="utf-8")
        with tempfile.TemporaryDirectory(prefix="kc-fresh-") as scratch_name:
            clone = Path(scratch_name) / "repo"
            (clone / "src" / "kiro_crew").mkdir(parents=True)
            (clone / ".git").mkdir()
            monkeypatch.setattr(agent, "__file__", str(clone / "src" / "kiro_crew" / "agent.py"))
            self._temp_checkout(monkeypatch, agent, shared)
            target = shared / WORKER_AGENT_FILENAME

            assert agent._declined_foreign_spec_write(target) is True, (
                "outside a rebuild this write must still be refused; if it is not, the "
                "assertion below cannot tell the fix from no guard at all"
            )

            monkeypatch.setattr(agent, "_rebuild_spec_install_admitted", True)
            assert agent._declined_foreign_spec_write(target) is False, (
                "a derived spec was refused inside the rebuild that was already "
                "admitted, so a fresh temp-checkout install writes its main spec and "
                "none of its derived ones"
            )

    def test_the_admission_does_not_outlive_a_raising_rebuild(self, monkeypatch, tmp_path):
        """A rebuild that raises mid-install must not leave the guard exempted.

        Several installs inside that block can raise past their own arms. If the
        flag were reset after the block instead of in a ``finally``, one raise
        would exempt every later write in the process -- a worker dispatch, a
        dashboard spec edit -- from the guard this change adds, which is strictly
        worse than not having it.
        """
        from kiro_crew import agent
        from kiro_crew.agent_files import WORKER_AGENT_FILENAME

        shared = self._foreign_shared_dir_for_rebuild(monkeypatch, agent, tmp_path)

        def _boom() -> None:
            raise RuntimeError("AIM capabilities install blew up")

        monkeypatch.setattr(agent, "_install_aim_capabilities", _boom)
        monkeypatch.setattr(agent, "_decline_shared_agent_home", lambda **_k: None)
        monkeypatch.setattr(agent, "migrate_agent_specs", lambda: None)
        monkeypatch.setattr(agent.default_spec_commit, "write_default_spec", lambda *a, **k: None)

        with pytest.raises(RuntimeError):
            agent.rebuild_agent_config()

        assert agent._rebuild_spec_install_admitted is False, (
            "the rebuild's admission survived an exception, so every later shared "
            "write in this process is exempt from the ownership guard"
        )
        # And the guard is demonstrably live again, not merely flagged off.
        monkeypatch.setattr(agent, "_decline_shared_agent_home", lambda **_k: shared)
        assert agent._declined_foreign_spec_write(shared / WORKER_AGENT_FILENAME) is True

    @staticmethod
    def _foreign_shared_dir_for_rebuild(monkeypatch, agent_mod, tmp_path) -> Path:
        """A shared agents dir this instance does not own, for the raise test."""
        monkeypatch.delenv("KIRO_HOME", raising=False)
        monkeypatch.delenv("KIROCREW_POD", raising=False)
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "my-home"))
        _durable_checkout(monkeypatch, agent_mod)
        shared = tmp_path / "agents"
        shared.mkdir()
        (shared / agent_mod.AGENT_FILENAME).write_text(
            _spec_pinned_to(tmp_path / "someone-elses-home"), encoding="utf-8"
        )
        _pretend_target_is_shared(monkeypatch, agent_mod, shared)
        return shared
