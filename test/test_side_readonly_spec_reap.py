"""Reaping orphaned derived read-only side-chat specs (``side_readonly_spec``).

Every test runs the real module against a temporary kiro agent registry
(``agent.KIRO_AGENTS_DIR``) and temporary checkouts under ``tmp_path``, never
the live home.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from kiro_crew import agent as agent_mod
from kiro_crew import agent_discovery
from kiro_crew.dashboard import side_readonly_spec as srs

#: A checkout name that needs quoting in JSON (a double quote is not a legal
#: Windows file name character, so it is left out there).
_ODD_NAME = "we\"ird pro'ject \u00fc" if sys.platform != "win32" else "weird pro'ject \u00fc"

_BASE = {
    "name": "ghost",
    "description": "a user agent",
    "prompt": "You help.",
    "tools": ["fs_read"],
    "allowedTools": ["fs_read"],
}


@pytest.fixture
def agents_dir(tmp_path, monkeypatch):
    """A temporary kiro agent registry, isolated from the live home."""
    registry = tmp_path / "agents"
    registry.mkdir()
    monkeypatch.setattr(agent_mod, "KIRO_AGENTS_DIR", registry)
    monkeypatch.setattr(srs, "_refresh_materialized_snapshot", lambda: None)
    return registry


def _write(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


def _publish_user(agents_dir: Path, base: str = "ghost") -> Path:
    """Publish ``<base>--readonly`` from a user-scope base; return the derived file."""
    _write(agents_dir / f"{base}.json", dict(_BASE, name=base))
    published = srs.publish_readonly_spec(base)
    return agents_dir / f"{published.name}.json"


def _project(tmp_path: Path, name: str = "proj", agent: str = "helper") -> Path:
    project = tmp_path / name
    (project / ".kiro" / "agents").mkdir(parents=True)
    _write(project / ".kiro" / "agents" / f"{agent}.json", dict(_BASE, name=agent))
    return project


def _publish_project(project: Path, agent: str = "helper") -> str:
    return srs.publish_readonly_spec(agent, str(project)).name


# ── user scope ──────────────────────────────────────────────────────────────


def test_an_owned_spec_whose_base_was_deleted_is_removed(agents_dir):
    derived = _publish_user(agents_dir)
    (agents_dir / "ghost.json").unlink()

    assert srs.reap_orphaned_readonly_specs() == ["ghost--readonly"]
    assert not derived.exists()


def test_an_owned_spec_with_a_live_base_survives(agents_dir):
    derived = _publish_user(agents_dir)
    before = derived.read_bytes()

    assert srs.reap_orphaned_readonly_specs() == []
    assert derived.read_bytes() == before


def test_a_base_declared_under_another_filename_still_resolves(agents_dir):
    derived = _publish_user(agents_dir)
    (agents_dir / "ghost.json").rename(agents_dir / "renamed.json")

    assert srs.reap_orphaned_readonly_specs() == []
    assert derived.exists()


def test_a_base_two_specs_declare_still_resolves(agents_dir):
    derived = _publish_user(agents_dir)
    (agents_dir / "ghost.json").rename(agents_dir / "one.json")
    _write(agents_dir / "two.json", dict(_BASE, name="ghost"))

    assert srs.reap_orphaned_readonly_specs() == []
    assert derived.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
def test_a_same_named_base_entry_of_any_kind_keeps_the_spec(agents_dir, tmp_path):
    """The publisher skips a symlinked base, but kiro-cli may load it: keep."""
    derived = _publish_user(agents_dir)
    outside = tmp_path / "dotfiles" / "ghost.json"
    outside.parent.mkdir()
    (agents_dir / "ghost.json").rename(outside)
    (agents_dir / "ghost.json").symlink_to(outside)

    assert srs.reap_orphaned_readonly_specs() == []
    assert derived.exists()

    # An unparseable base keeps it too, and so does the markdown form.
    (agents_dir / "ghost.json").unlink()
    (agents_dir / "ghost.json").write_text("{not json", encoding="utf-8")
    assert srs.reap_orphaned_readonly_specs() == []
    (agents_dir / "ghost.json").unlink()
    (agents_dir / "ghost.md").write_text("---\nname: ghost\n---\nhi\n", encoding="utf-8")
    assert srs.reap_orphaned_readonly_specs() == []
    assert derived.exists()


def test_a_spec_a_live_session_runs_under_is_kept(agents_dir):
    derived = _publish_user(agents_dir)
    (agents_dir / "ghost.json").unlink()

    assert srs.reap_orphaned_readonly_specs(in_use={"ghost--readonly"}) == []
    assert derived.exists()
    assert srs.reap_orphaned_readonly_specs(in_use={"other--readonly"}) == ["ghost--readonly"]


# ── files that are not provably ours ────────────────────────────────────────


def test_a_file_without_the_owner_marker_is_never_touched(agents_dir):
    foreign = agents_dir / "ghost--readonly.json"
    _write(foreign, dict(_BASE, name="ghost--readonly", description="my own read-only agent"))
    text = foreign.read_text(encoding="utf-8")

    assert srs.reap_orphaned_readonly_specs() == []
    assert foreign.read_text(encoding="utf-8") == text


def test_an_owned_file_whose_name_or_description_disagrees_is_kept(agents_dir):
    derived = _publish_user(agents_dir)
    (agents_dir / "ghost.json").unlink()
    data = json.loads(derived.read_text(encoding="utf-8"))

    _write(derived, dict(data, name="something-else"))
    assert srs.reap_orphaned_readonly_specs() == []

    _write(derived, dict(data, description=data["description"].replace("'ghost'", "'other'")))
    assert srs.reap_orphaned_readonly_specs() == []

    _write(derived, dict(data, description=srs.OWNER_MARKER + " hand-written"))
    assert srs.reap_orphaned_readonly_specs() == []
    assert derived.exists()


def test_only_derived_json_names_are_candidates(agents_dir):
    """A marked file without a derived name, a markdown twin, a directory: ignored."""
    derived = _publish_user(agents_dir)
    (agents_dir / "ghost.json").unlink()
    data = json.loads(derived.read_text(encoding="utf-8"))
    derived.unlink()
    plain = agents_dir / "ghost-ro.json"
    _write(plain, dict(data, name="ghost-ro"))
    md = agents_dir / "ghost--readonly.md"
    md.write_text("---\nname: ghost--readonly\n---\nhi\n", encoding="utf-8")
    (agents_dir / "twin--readonly.json").mkdir()

    assert srs.reap_orphaned_readonly_specs() == []
    assert plain.exists() and md.exists() and (agents_dir / "twin--readonly.json").is_dir()


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
def test_a_symlink_is_neither_followed_nor_removed(agents_dir, tmp_path):
    derived = _publish_user(agents_dir)
    (agents_dir / "ghost.json").unlink()
    outside = tmp_path / "outside" / "ghost--readonly.json"
    outside.parent.mkdir()
    derived.rename(outside)
    derived.symlink_to(outside)
    target_text = outside.read_text(encoding="utf-8")

    assert srs.reap_orphaned_readonly_specs() == []
    assert derived.is_symlink()
    assert outside.read_text(encoding="utf-8") == target_text


@pytest.mark.skipif(not hasattr(os, "link"), reason="no hardlinks")
def test_a_hardlinked_spec_is_kept(agents_dir, tmp_path):
    derived = _publish_user(agents_dir)
    (agents_dir / "ghost.json").unlink()
    other = tmp_path / "other-link.json"
    os.link(derived, other)

    assert srs.reap_orphaned_readonly_specs() == []
    assert derived.exists() and other.exists()


def test_a_missing_or_unlistable_registry_reaps_nothing(agents_dir, monkeypatch):
    shutil.rmtree(agents_dir)
    assert srs.reap_orphaned_readonly_specs() == []

    agents_dir.mkdir()
    real_scandir = os.scandir

    def refusing(path=".", *a, **k):
        if isinstance(path, (str, os.PathLike)) and Path(path) == agents_dir:
            raise PermissionError("denied")
        return real_scandir(path, *a, **k)

    monkeypatch.setattr(os, "scandir", refusing)
    assert srs.reap_orphaned_readonly_specs() == []


# ── project scope ───────────────────────────────────────────────────────────


def test_a_project_variant_records_its_checkout_in_the_description(agents_dir, tmp_path):
    project = _project(tmp_path, name=_ODD_NAME)
    name = _publish_project(project)

    data = json.loads((agents_dir / f"{name}.json").read_text(encoding="utf-8"))
    assert data["description"].startswith(srs.OWNER_MARKER)
    candidate = srs._owned_candidate(agents_dir, f"{name}.json")
    assert candidate is not None
    assert (candidate.base, candidate.source) == ("helper", str(project.resolve()))
    # A user-scope variant records no checkout.
    user = json.loads(_publish_user(agents_dir).read_text(encoding="utf-8"))
    assert "in project" not in user["description"]


def test_a_variant_whose_checkout_was_removed_is_reaped(agents_dir, tmp_path):
    project = _project(tmp_path, name=_ODD_NAME)
    name = _publish_project(project)
    shutil.rmtree(project)

    assert srs.reap_orphaned_readonly_specs() == [name]
    assert not (agents_dir / f"{name}.json").exists()


def test_a_variant_whose_checkout_is_now_a_file_is_reaped(agents_dir, tmp_path):
    project = _project(tmp_path)
    name = _publish_project(project)
    shutil.rmtree(project)
    project.write_text("not a checkout", encoding="utf-8")

    assert srs.reap_orphaned_readonly_specs() == [name]


def test_a_variant_whose_checkout_dropped_the_agent_is_reaped(agents_dir, tmp_path):
    project = _project(tmp_path)
    name = _publish_project(project)
    (project / ".kiro" / "agents" / "helper.json").unlink()
    _write(project / ".kiro" / "agents" / "unrelated.json", dict(_BASE, name="unrelated"))

    assert srs.reap_orphaned_readonly_specs() == [name]

    # No agents directory at all is the same answer.
    other = _project(tmp_path, name="other")
    second = _publish_project(other)
    shutil.rmtree(other / ".kiro" / "agents")
    assert srs.reap_orphaned_readonly_specs() == [second]


def test_a_checkout_that_still_declares_the_agent_keeps_its_variant(agents_dir, tmp_path):
    project = _project(tmp_path)
    name = _publish_project(project)
    target = agents_dir / f"{name}.json"
    assert srs.reap_orphaned_readonly_specs() == []

    # Declared under another filename, or present by name but unparseable.
    agents = project / ".kiro" / "agents"
    (agents / "helper.json").rename(agents / "renamed.json")
    assert srs.reap_orphaned_readonly_specs() == []
    (agents / "renamed.json").unlink()
    (agents / "Helper.json").write_text("{broken", encoding="utf-8")
    assert srs.reap_orphaned_readonly_specs() == []
    assert target.exists()


def test_a_variant_whose_recorded_checkout_does_not_match_its_name_is_kept(agents_dir, tmp_path):
    """The name carries a digest of the checkout path: an edited path is not ours."""
    project = _project(tmp_path)
    name = _publish_project(project)
    target = agents_dir / f"{name}.json"
    data = json.loads(target.read_text(encoding="utf-8"))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    data["description"] = data["description"].replace(
        json.dumps(str(project.resolve())), json.dumps(str(elsewhere))
    )
    _write(target, data)

    assert srs.reap_orphaned_readonly_specs() == []
    assert target.exists()


def test_a_variant_that_records_no_checkout_is_kept(agents_dir, tmp_path):
    project = _project(tmp_path)
    name = _publish_project(project)
    target = agents_dir / f"{name}.json"
    data = json.loads(target.read_text(encoding="utf-8"))
    head, _, tail = data["description"].partition(" in project ")
    data["description"] = head + tail[tail.index(";") :]
    _write(target, data)
    shutil.rmtree(project)

    assert srs.reap_orphaned_readonly_specs() == []
    assert target.exists()


def test_an_unreadable_or_protected_checkout_keeps_its_variant(agents_dir, tmp_path, monkeypatch):
    project = _project(tmp_path)
    name = _publish_project(project)
    (project / ".kiro" / "agents" / "helper.json").unlink()
    target = agents_dir / f"{name}.json"
    project_agents = project / ".kiro" / "agents"
    real_scandir, real_stat = os.scandir, os.stat

    def refusing_scandir(path=".", *a, **k):
        if isinstance(path, (str, os.PathLike)) and Path(path) == project_agents:
            raise PermissionError("denied")
        return real_scandir(path, *a, **k)

    with monkeypatch.context() as patch:
        patch.setattr(os, "scandir", refusing_scandir)
        assert srs.reap_orphaned_readonly_specs() == []

    # Resolved before os.stat is patched: on Python 3.12 ``Path.resolve()`` calls
    # ``os.stat`` itself, so a refusal that resolves inside the patch recurses until
    # a RecursionError keeps the variant, never the PermissionError under test.
    checkout = str(project.resolve())
    refused: list[str] = []

    def refusing_stat(path, *a, **k):
        if isinstance(path, (str, os.PathLike)) and str(path) == checkout:
            refused.append(str(path))
            raise PermissionError("denied")
        return real_stat(path, *a, **k)

    with monkeypatch.context() as patch:
        patch.setattr(os, "stat", refusing_stat)
        assert srs.reap_orphaned_readonly_specs() == []
    assert refused, "the checkout's stat was never refused, so nothing above was tested"

    with monkeypatch.context() as patch:
        patch.setattr(agent_discovery, "_project_scope_denied", lambda *a, **k: True)
        assert srs.reap_orphaned_readonly_specs() == []

    assert target.exists()
    assert srs.reap_orphaned_readonly_specs() == [name]


def _plant_variant(agents_dir: Path, source: str, agent: str = "helper") -> Path:
    """Write an owner-marked variant that records *source*, as if publication had."""
    derived = srs.derive_readonly_spec(dict(_BASE, name=agent), base_name=agent, source_id=source)
    target = agents_dir / f"{derived['name']}.json"
    _write(target, derived)
    return target


def _spy_probes(monkeypatch, marker: str) -> list[str]:
    """Record every stat/scandir/spec read whose path contains *marker*, never touching it."""
    probed: list[str] = []
    real_stat, real_scandir = os.stat, os.scandir
    real_name = agent_discovery.project_agent_name

    def spying_stat(path, *a, **k):
        if isinstance(path, (str, os.PathLike)) and marker in str(path):
            probed.append(f"stat {path}")
            raise FileNotFoundError(path)
        return real_stat(path, *a, **k)

    def spying_scandir(path=".", *a, **k):
        if isinstance(path, (str, os.PathLike)) and marker in str(path):
            probed.append(f"scandir {path}")
            raise FileNotFoundError(path)
        return real_scandir(path, *a, **k)

    def spying_name(spec):
        if marker in str(spec):
            probed.append(f"read {spec}")
        return real_name(spec)

    monkeypatch.setattr(os, "stat", spying_stat)
    monkeypatch.setattr(os, "scandir", spying_scandir)
    monkeypatch.setattr(agent_discovery, "project_agent_name", spying_name)
    return probed


def _screen_refusing(monkeypatch, marker: str) -> list[str]:
    """Make ``hooks.validate_file_path`` refuse what resolves into *marker*, as the
    Windows UNC gate and link screen do for a share; return the paths it was asked."""
    from kiro_crew import hooks

    asked: list[str] = []
    real = hooks.validate_file_path

    def screening(raw):
        asked.append(raw)
        if marker in raw:
            return None
        canonical = real(raw)
        return None if canonical is None or marker in canonical else canonical

    monkeypatch.setattr(hooks, "validate_file_path", screening)
    return asked


def test_a_relative_or_screen_refused_checkout_is_kept_and_never_probed(agents_dir, monkeypatch):
    """The recorded checkout is text from a shared directory: a relative one is
    not ours, and one the path screen refuses (an untrusted UNC share on Windows)
    is kept before any stat, resolve or sensitive-path check could reach it."""
    relative = _plant_variant(agents_dir, "relative/checkout")
    unc = _plant_variant(agents_dir, "//attacker/share/checkout")
    asked = _screen_refusing(monkeypatch, "attacker")
    scoped: list[str] = []
    monkeypatch.setattr(
        agent_discovery,
        "_project_scope_denied",
        lambda project_dir, **k: scoped.append(project_dir) or False,
    )
    probed = _spy_probes(monkeypatch, "attacker")

    assert srs.reap_orphaned_readonly_specs() == []
    assert relative.exists() and unc.exists()
    assert probed == [] and scoped == []
    assert asked == ["//attacker/share/checkout"]


def test_an_agents_dir_linked_into_a_refused_location_is_never_listed(
    agents_dir, tmp_path, monkeypatch
):
    """A checkout whose ``.kiro/agents`` is a link the screen refuses (a share,
    on Windows) keeps its variant without the link ever being followed."""
    project = _project(tmp_path)
    name = _publish_project(project)
    share = tmp_path / "attacker-share"
    share.mkdir()
    agents = project / ".kiro" / "agents"
    shutil.rmtree(agents)
    try:
        agents.symlink_to(share, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    asked = _screen_refusing(monkeypatch, "attacker-share")
    probed = _spy_probes(monkeypatch, "attacker-share")

    assert srs.reap_orphaned_readonly_specs() == []
    assert (agents_dir / f"{name}.json").exists()
    assert probed == []
    assert str(project.resolve() / ".kiro" / "agents") in asked


def test_a_spec_named_link_in_the_agents_dir_keeps_the_variant_unfollowed(
    agents_dir, tmp_path, monkeypatch
):
    """Only regular files are parsed: a spec-named link could lead anywhere,
    including a share, so it keeps the variant and is never read through."""
    project = _project(tmp_path)
    name = _publish_project(project)
    agents = project / ".kiro" / "agents"
    (agents / "helper.json").unlink()
    elsewhere = tmp_path / "linked-elsewhere"
    elsewhere.mkdir()
    _write(elsewhere / "other.json", dict(_BASE, name="other"))
    try:
        (agents / "other.json").symlink_to(elsewhere / "other.json")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    probed = _spy_probes(monkeypatch, "other.json")

    assert srs.reap_orphaned_readonly_specs() == []
    assert (agents_dir / f"{name}.json").exists()
    assert probed == []

    # The same checkout with the link gone is reaped: the guard is the link.
    (agents / "other.json").unlink()
    assert srs.reap_orphaned_readonly_specs() == [name]


@pytest.mark.skipif(os.name != "nt", reason="the UNC trusted-root gate exists only on Windows")
def test_the_real_screen_refuses_an_untrusted_unc_checkout_on_windows(agents_dir, monkeypatch):
    unc = _plant_variant(agents_dir, "\\\\attacker.invalid\\share\\checkout")
    probed = _spy_probes(monkeypatch, "attacker.invalid")

    assert srs.reap_orphaned_readonly_specs() == []
    assert unc.exists()
    assert probed == []


# ── races and caches ────────────────────────────────────────────────────────


def test_publication_holds_the_lock_the_reap_deletes_under(agents_dir, monkeypatch):
    _write(agents_dir / "ghost.json", _BASE)
    held: list[bool] = []
    real = srs._read_base_spec

    def recording(*args, **kwargs):
        held.append(srs._SPEC_FILE_LOCK.locked())
        return real(*args, **kwargs)

    monkeypatch.setattr(srs, "_read_base_spec", recording)
    srs.publish_readonly_spec("ghost")
    assert held == [True]
    assert not srs._SPEC_FILE_LOCK.locked()


def test_a_base_that_returns_before_the_unlink_keeps_the_spec(agents_dir, monkeypatch):
    derived = _publish_user(agents_dir)
    (agents_dir / "ghost.json").unlink()
    real = srs._orphan_reason
    verdicts: list[str | None] = []

    def racing(candidate, directory):
        verdict = real(candidate, directory)
        if not verdicts:
            _write(agents_dir / "ghost.json", _BASE)  # republished between the two looks
        verdicts.append(verdict)
        return verdict

    monkeypatch.setattr(srs, "_orphan_reason", racing)
    assert srs.reap_orphaned_readonly_specs() == []
    assert verdicts == ["base agent removed", None]
    assert derived.exists()


def test_a_file_replaced_before_the_unlink_is_kept(agents_dir, monkeypatch):
    derived = _publish_user(agents_dir)
    (agents_dir / "ghost.json").unlink()
    replacement = json.loads(derived.read_text(encoding="utf-8"))
    replacement["prompt"] = "rewritten meanwhile"
    real = srs._orphan_reason
    calls: list[int] = []

    def replacing(candidate, directory):
        if not calls:
            staged = agents_dir / "staged.tmp"
            _write(staged, replacement)
            os.replace(staged, derived)
        calls.append(1)
        return real(candidate, directory)

    monkeypatch.setattr(srs, "_orphan_reason", replacing)
    assert srs.reap_orphaned_readonly_specs() == []
    assert json.loads(derived.read_text(encoding="utf-8"))["prompt"] == "rewritten meanwhile"


def test_a_failure_on_one_file_keeps_that_file_and_reaps_the_rest(agents_dir, monkeypatch):
    first = _publish_user(agents_dir, "ghost")
    second = _publish_user(agents_dir, "spook")
    (agents_dir / "ghost.json").unlink()
    (agents_dir / "spook.json").unlink()
    real = srs._orphan_reason

    def failing(candidate, directory):
        if candidate.base == "ghost":
            raise RuntimeError("boom")
        return real(candidate, directory)

    monkeypatch.setattr(srs, "_orphan_reason", failing)
    assert srs.reap_orphaned_readonly_specs() == ["spook--readonly"]
    assert first.exists() and not second.exists()


def test_a_removal_invalidates_the_agent_caches_and_a_no_op_does_not(agents_dir, monkeypatch):
    refreshed: list[int] = []
    monkeypatch.setattr(srs, "_refresh_materialized_snapshot", lambda: refreshed.append(1))
    _publish_user(agents_dir)
    refreshed.clear()

    generation = agent_discovery.spec_cache_generation()
    assert srs.reap_orphaned_readonly_specs() == []
    assert agent_discovery.spec_cache_generation() == generation and refreshed == []

    (agents_dir / "ghost.json").unlink()
    assert srs.reap_orphaned_readonly_specs() == ["ghost--readonly"]
    assert agent_discovery.spec_cache_generation() > generation
    assert refreshed == [1]


def test_the_agents_dir_memo_never_serves_a_reaped_spec(agents_dir):
    derived = _publish_user(agents_dir)
    _write(agents_dir / "keep.json", dict(_BASE, name="keep"))
    memo: agent_discovery.AgentsDirMemo[list[str]] = agent_discovery.AgentsDirMemo()

    def names() -> list[str]:
        return sorted(p.name for p in agents_dir.iterdir())

    assert "ghost--readonly.json" in memo.get(agents_dir, "names", names)
    (agents_dir / "ghost.json").unlink()
    assert srs.reap_orphaned_readonly_specs() == ["ghost--readonly"]
    assert not derived.exists()

    assert memo.get(agents_dir, "names", names) == ["keep.json"]


# ── gateway wiring ──────────────────────────────────────────────────────────


def test_live_session_agents_are_the_registered_providers_agents():
    from kiro_crew.slack import gateway

    assert gateway._live_session_agents(None) == set()
    side = MagicMock(spec=["_client"])
    side._client = MagicMock(spec=["_agent"], _agent="ghost--readonly")
    blank = MagicMock(spec=["_agent"], _agent="")
    sessions = MagicMock()
    sessions.active_providers.return_value = [side, blank, MagicMock(spec=[])]
    assert gateway._live_session_agents(sessions) == {"ghost--readonly"}


def test_the_gateway_reaps_in_the_boot_janitor_and_on_the_hourly_wake():
    from kiro_crew.slack.gateway_runtime import process_entry

    source = Path(process_entry.__file__).read_text(encoding="utf-8")
    janitor = source.index("async def _run_agents_janitor() -> None:")
    janitor_end = source.index('name="agents-dir-janitor"', janitor)
    assert "reap_orphaned_readonly_specs()" in source[janitor:janitor_end]
    assert "await asyncio.to_thread(_sweep_in_thread)" in source[janitor:janitor_end]

    hourly = source.index("async def _run_agent_scratch_sweep() -> None:")
    hourly_end = source.index('name="agent-scratch-sweep"', hourly)
    wake = source[hourly:hourly_end]
    assert wake.index("await asyncio.sleep(3600)") < wake.index("reap_orphaned_readonly_specs,")
    assert "_live_session_agents(" in wake
