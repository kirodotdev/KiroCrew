"""Agent-spec home isolation — the KIRO_HOME seam, the worktree guard, and the
anti-regression guard that keeps ``~/.kiro/agents`` from being hard-coded again.

Regression context: a gateway booted from a linked git worktree rewrote the
machine-wide ``~/.kiro/agents/*.json`` on startup, stamping its own ``.venv``
binary into every managed server's ``command`` and its own data home into their
``env``. The real install's MCP servers then ran the worktree's code and read the
worktree's credential while still calling the live gateway, so every managed MCP
call returned HTTP 403 — and once the worktree was removed those specs pointed at
paths that no longer existed.
"""

from __future__ import annotations

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


def test_kiro_home_defaults_to_dot_kiro(monkeypatch):
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
    """
    monkeypatch.setattr(agent_mod, "KIRO_AGENTS_DIR", agents_dir)
    monkeypatch.setattr(agent_mod, "kiro_agents_dir", lambda: agents_dir)


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
    monkeypatch.setattr(agent, "kiro_agents_dir", lambda: real)

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


def test_does_not_decline_from_an_ordinary_clone(monkeypatch, tmp_path):
    from kiro_crew import agent

    monkeypatch.delenv("KIRO_HOME", raising=False)
    # No isolated data home either: an ordinary install owns its shared specs.
    monkeypatch.delenv("KIROCREW_HOME", raising=False)
    clone = tmp_path / "KiroCrew"
    (clone / "src" / "kiro_crew").mkdir(parents=True)
    (clone / ".git").mkdir()  # a DIRECTORY -> ordinary clone
    monkeypatch.setattr(agent, "__file__", str(clone / "src" / "kiro_crew" / "agent.py"))
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
    clone = tmp_path / "KiroCrew"
    (clone / "src" / "kiro_crew").mkdir(parents=True)
    (clone / ".git").mkdir()
    monkeypatch.setattr(agent, "__file__", str(clone / "src" / "kiro_crew" / "agent.py"))
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
# Transcripts follow the same home as the specs
# --------------------------------------------------------------------------
def test_sessions_dir_follows_kiro_home(monkeypatch, tmp_path):
    """The transcripts dir must move WITH the agent dir, or resume breaks.

    ``KIRO_HOME`` is directory-wide: kiro-cli writes transcripts under it. If
    KiroCrew kept reading the machine-wide path, an instance with its own agent
    home would look for transcripts that are not there — losing session resume and
    letting ``SessionMap`` prune mappings whose files it can no longer see.
    """
    from kiro_crew.config.paths import kiro_agents_dir, kiro_sessions_dir

    monkeypatch.delenv("KIROCREW_HOME", raising=False)
    monkeypatch.setenv("KIRO_HOME", str(tmp_path / "pod-kiro"))

    root = (tmp_path / "pod-kiro").resolve()
    assert kiro_sessions_dir() == root / "sessions" / "cli"
    assert kiro_agents_dir() == root / "agents"


def test_sessions_dir_defaults_to_dot_kiro(monkeypatch):
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
# ``config/paths.py`` is the resolver that defines the default. ``security.py``
# holds the sensitive-path denylist, whose entries are HOME-RELATIVE literals
# (the matcher anchors ``$HOME``-relative strings; ``kiro_agents_dir()`` returns
# an absolute path, so the resolver's value cannot be used here) and which is
# kept literal on purpose to avoid a config->security import cycle — the same
# convention as the ``.data-home-ready`` marker literal. It does NOT read or
# write the dir (it only matches path strings) and it re-anchors ``KIRO_HOME``
# for that entry, so it cannot reintroduce the reader/writer split-brain this
# guard exists to catch; ``TestKiroAgentsDirWriteProtection`` pins the literal to
# ``kiro_agents_dir()`` so drift still fails loudly.
_ALLOWED = {"config/paths.py", "security.py"}


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


def test_repo_has_no_python_syntax_regression():
    """Cheap compile-all so a rewrite typo fails here rather than at import."""
    proc = subprocess.run(
        [sys.executable, "-m", "compileall", "-q", str(SRC)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
