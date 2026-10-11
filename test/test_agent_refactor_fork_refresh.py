"""The fork refresh fails closed on every branch that cannot vouch for a fork.

A fork carries ``allowedTools`` and ``autoApprove`` grants that never reach the
PreToolUse gate, so a fork whose last refresh did not re-filter them must not start a
session. ``_fork_refresh_failed`` is how the refresh says so: ``"*"`` when the pass
died before per-fork accounting, the fork's name when that one fork could not be
refreshed. These pin each branch, and that the spawn gate's settled event is set
again whenever no pass is left to set it.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Iterator

import pytest

from conftest import requires_symlinks
from kiro_crew import agent, agent_state
from kiro_crew.agent_materialization import fork_refresh
from kiro_crew.config.loader import KiroCrewConfig

# Captured before any fixture stubs it, so a test can show what the real resolver picks.
_REAL_AGENT_SPEC_PATH = agent.agent_spec_path


class _Boom(Exception):
    pass


def _raise(*_args: object, **_kwargs: object) -> None:
    raise _Boom("refresh died")


@pytest.fixture(autouse=True)
def _restore_refresh_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Every test starts settled with no failures, and leaves it that way."""
    monkeypatch.setattr(agent, "_fork_refresh_failed", frozenset())
    monkeypatch.setattr(agent, "_fork_refresh_pending", 0)
    monkeypatch.setattr(fork_refresh, "_shared_template_held", False)
    monkeypatch.setattr(agent, "_conductor_spec_held", False)
    fork_refresh._fork_refresh_settled.set()
    yield
    fork_refresh._fork_refresh_settled.set()


class _InlineThread:
    """Runs its target on ``start`` so the deferred pass finishes before the assert."""

    def __init__(self, *, target: Callable[[], None], name: str, daemon: bool) -> None:
        assert name == "fork-refresh" and daemon
        self._target = target

    def start(self) -> None:
        self._target()


class _UnstartableThread(_InlineThread):
    def start(self) -> None:
        raise RuntimeError("can't start new thread")


def _threads(monkeypatch: pytest.MonkeyPatch, thread: type) -> None:
    monkeypatch.setattr(fork_refresh, "threading", SimpleNamespace(Thread=thread))


def test_a_deferred_pass_that_dies_blocks_every_fork(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _threads(monkeypatch, _InlineThread)
    monkeypatch.setattr(agent, "_refresh_forked_templates", _raise)
    with caplog.at_level(logging.WARNING, logger=agent.logger.name):
        fork_refresh.refresh_after_rebuild("defer", frozenset())
    assert agent._fork_refresh_failed == frozenset({"*"})
    assert "deferred fork refresh failed" in caplog.text
    # The deferral cleared the event and only a finished pass re-sets it.
    assert not fork_refresh._fork_refresh_settled.is_set()


def test_a_deferred_pass_whose_thread_never_starts_fails_closed_and_releases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _threads(monkeypatch, _UnstartableThread)
    with pytest.raises(RuntimeError, match="can't start"):
        fork_refresh.refresh_after_rebuild("defer", frozenset())
    assert agent._fork_refresh_failed == frozenset({"*"})
    assert fork_refresh._fork_refresh_settled.is_set()


def test_a_synchronous_pass_that_dies_does_not_fail_the_rebuild(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(agent, "_refresh_forked_templates", _raise)
    with caplog.at_level(logging.DEBUG, logger=agent.logger.name):
        fork_refresh.refresh_after_rebuild(True, frozenset())
    assert "forked template refresh failed" in caplog.text


def test_no_refresh_at_all_when_the_caller_opts_out(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent, "_refresh_forked_templates", _raise)
    fork_refresh.refresh_after_rebuild(False, frozenset())
    assert agent._fork_refresh_failed == frozenset()


def test_a_pass_that_dies_before_accounting_records_the_wildcard_and_re_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(agent, "_refresh_forked_templates_locked", _raise)
    with pytest.raises(_Boom):
        agent._refresh_forked_templates(gated_off=frozenset())
    assert agent._fork_refresh_failed == frozenset({"*"})
    assert agent._fork_refresh_pending == 0
    assert fork_refresh._fork_refresh_settled.is_set()


def _forks(monkeypatch: pytest.MonkeyPatch, forks: dict[str, dict[str, Any]]) -> None:
    monkeypatch.setattr(agent_state, "all_fork_info", lambda: forks)
    monkeypatch.setattr(agent_state, "get_capabilities", lambda _name: None)


def _bindings(monkeypatch: pytest.MonkeyPatch, bound: dict[str, str]) -> None:
    agents = {crew: SimpleNamespace(kiro_agent=name) for crew, name in bound.items()}
    monkeypatch.setattr(
        KiroCrewConfig, "load", classmethod(lambda cls: SimpleNamespace(agents=agents))
    )


def test_no_forks_clears_the_failure_record(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent, "_fork_refresh_failed", frozenset({"stale"}))
    _forks(monkeypatch, {})
    agent._refresh_forked_templates_locked(gated_off=frozenset())
    assert agent._fork_refresh_failed == frozenset()


def test_an_unreadable_config_corroborates_nothing(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _forks(monkeypatch, {"crewfork": {"private_to": "crew", "forked_from": "kirocrew"}})
    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(_raise))
    with caplog.at_level(logging.WARNING, logger=agent.logger.name):
        agent._refresh_forked_templates_locked(gated_off=frozenset())
    assert agent._fork_refresh_failed == frozenset({"*"})
    assert "config unreadable" in caplog.text


@pytest.fixture
def one_fork(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """One corroborated fork of the owned template, and an owned spec listed as a fork."""
    _forks(
        monkeypatch,
        {
            "crewfork": {"private_to": "crew", "forked_from": "kirocrew"},
            "kirocrew-lite": {"private_to": "other", "forked_from": "kirocrew"},
        },
    )
    _bindings(monkeypatch, {"crew": "crewfork", "other": "kirocrew-lite"})
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    return tmp_path


def test_a_fork_with_no_spec_on_disk_has_nothing_to_refresh(
    one_fork: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(agent, "agent_spec_path", lambda _name: None)
    agent._refresh_forked_templates_locked(gated_off=frozenset())
    assert agent._fork_refresh_failed == frozenset()


def test_a_fork_resolving_to_a_markdown_spec_stays_blocked(
    one_fork: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    spec = one_fork / "crewfork.md"
    spec.write_text("---\nname: crewfork\n---\n", encoding="utf-8")
    monkeypatch.setattr(agent, "agent_spec_path", lambda _name: spec)
    with caplog.at_level(logging.WARNING, logger=agent.logger.name):
        agent._refresh_forked_templates_locked(gated_off=frozenset())
    assert agent._fork_refresh_failed == frozenset({"crewfork"})
    assert "markdown spec" in caplog.text


def test_a_fork_whose_spec_does_not_read_as_an_object_stays_blocked(
    one_fork: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = one_fork / "crewfork.json"
    spec.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(agent, "agent_spec_path", lambda _name: spec)
    monkeypatch.setattr(agent, "_load_json", lambda _path: [])
    agent._refresh_forked_templates_locked(gated_off=frozenset())
    assert agent._fork_refresh_failed == frozenset({"crewfork"})


def test_an_unparseable_fork_spec_stays_blocked_and_is_not_rewritten(
    one_fork: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = one_fork / "crewfork.json"
    spec.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(agent, "agent_spec_path", lambda _name: spec)
    written: list[dict[str, Any]] = []
    monkeypatch.setattr(agent, "_atomic_json_write", lambda _path, config: written.append(config))
    agent._refresh_forked_templates_locked(gated_off=frozenset())
    assert written == []
    assert agent._fork_refresh_failed == frozenset({"crewfork"})


def test_a_fork_spec_saved_with_a_byte_order_mark_is_refreshed_whole(
    one_fork: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = one_fork / "crewfork.json"
    doc = {"name": "crewfork", "tools": [], "allowedTools": [], "prompt": "keep me"}
    spec.write_bytes(b"\xef\xbb\xbf" + json.dumps(doc).encode("utf-8"))
    monkeypatch.setattr(agent, "agent_spec_path", lambda _name: spec)
    monkeypatch.setattr(agent, "_refresh_dynamic_fields", lambda *_a, **_k: None)
    written: list[dict[str, Any]] = []
    monkeypatch.setattr(agent, "_atomic_json_write", lambda _path, config: written.append(config))
    agent._refresh_forked_templates_locked(gated_off=frozenset())
    assert [config.get("prompt") for config in written] == ["keep me"]
    assert agent._fork_refresh_failed == frozenset()


def test_a_plumbing_failure_still_writes_the_governance_passes(
    one_fork: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    spec = one_fork / "crewfork.json"
    spec.write_text(json.dumps({"name": "crewfork", "tools": [], "allowedTools": []}))
    monkeypatch.setattr(agent, "agent_spec_path", lambda _name: spec)
    monkeypatch.setattr(agent, "_refresh_dynamic_fields", _raise)
    written: list[dict[str, Any]] = []
    monkeypatch.setattr(agent, "_atomic_json_write", lambda _path, config: written.append(config))
    with caplog.at_level(logging.DEBUG, logger=agent.logger.name):
        agent._refresh_forked_templates_locked(gated_off=frozenset())
    assert "refresh failed for forked template 'crewfork'" in caplog.text
    assert [config["name"] for config in written] == ["crewfork"]
    assert agent._fork_refresh_failed == frozenset()


def test_dashboard_author_file_is_installers_is_fail_closed_on_an_unreadable_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GPT 6.1 F2: a user-owned dashboard-author ``.md`` with a JSON crew fork leaves no
    readable ``.json`` origin, so the capped reader returns ``None``. The predicate must be
    FAIL-CLOSED -- a ``None`` spec is NOT confirmed ours, so it returns False and the fork
    refresh leaves the fork's custom ``preToolUse`` guards in place rather than replacing
    them with bundled hooks. A ``.json`` that reproduces the installer-recorded ownership
    digest still returns True."""
    from kiro_crew import agent_state

    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    absent = tmp_path / "kirocrew-dashboard-author.json"
    # Absent / unreadable -> capped reader returns None -> NOT ours.
    assert fork_refresh._dashboard_author_file_is_installers(absent) is False
    # A user file with no recorded ownership digest -> NOT ours.
    managed = {"name": "kirocrew-dashboard-author", "mcpServers": {"kirocrew-core": {}}}
    absent.write_text(json.dumps(managed), encoding="utf-8")
    assert fork_refresh._dashboard_author_file_is_installers(absent) is False
    # Record the ownership digest of these exact bytes -> now ours (reproduces the digest).
    absent.write_text(json.dumps(managed, indent=2) + "\n", encoding="utf-8")
    agent_state.set_managed_digest("kirocrew-dashboard-author", agent_state.spec_digest(managed))
    assert fork_refresh._dashboard_author_file_is_installers(absent) is True
    # A hand-edit after recording changes the bytes -> digest mismatch -> NOT ours.
    edited = dict(managed, prompt="user hand-edit")
    absent.write_text(json.dumps(edited, indent=2) + "\n", encoding="utf-8")
    assert fork_refresh._dashboard_author_file_is_installers(absent) is False


def test_a_fork_of_an_unconfirmed_dashboard_author_origin_is_not_plumbing_refreshed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dashboard-author stem was a user-creatable template name before it became owned,
    so a fork can descend from a USER template at that stem. ``_origin_is_owned`` treats it
    as an owned origin (and refreshes the fork's hooks/MCP plumbing) ONLY when the sidecar
    confirms the origin spec is ours; an unconfirmed origin leaves the fork's plumbing
    untouched -- a corroborated fork still gets its governance passes."""
    name = "kirocrew-dashboard-author"
    _forks(monkeypatch, {"crewfork": {"private_to": "crew", "forked_from": name}})
    _bindings(monkeypatch, {"crew": "crewfork"})
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    spec = tmp_path / "crewfork.json"
    spec.write_text(json.dumps({"name": "crewfork", "tools": [], "allowedTools": []}))
    monkeypatch.setattr(agent, "agent_spec_path", lambda _name: spec)
    plumbed: list[str] = []
    monkeypatch.setattr(agent, "_refresh_dynamic_fields", lambda config, *a, **k: plumbed.append(1))
    monkeypatch.setattr(agent, "_atomic_json_write", lambda _p, _c: None)

    # Origin NOT confirmed -> no plumbing refresh (the fork's hooks/MCP are left alone).
    monkeypatch.setattr(fork_refresh, "_dashboard_author_file_is_installers", lambda p: False)
    agent._refresh_forked_templates_locked(gated_off=frozenset())
    assert plumbed == []
    assert agent._fork_refresh_failed == frozenset()  # governance still ran; fork not blocked

    # Origin confirmed ours -> plumbing refresh runs.
    monkeypatch.setattr(fork_refresh, "_dashboard_author_file_is_installers", lambda p: True)
    agent._refresh_forked_templates_locked(gated_off=frozenset())
    assert plumbed == [1]


def test_the_settled_event_stays_cleared_for_the_whole_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A spawn cannot consume grants a running pass has not re-filtered yet."""
    release = threading.Event()
    entered = threading.Event()
    calls = 0

    def slow_then_fast(*, gated_off: frozenset[str] | None = None) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            assert release.wait(timeout=10)

    monkeypatch.setattr(agent, "_refresh_forked_templates_locked", slow_then_fast)
    first = threading.Thread(target=agent._refresh_forked_templates)
    first.start()
    try:
        assert entered.wait(timeout=10)
        assert not fork_refresh._fork_refresh_settled.is_set()
    finally:
        release.set()
        first.join(timeout=10)
    assert not first.is_alive()
    assert fork_refresh._fork_refresh_settled.is_set()
    assert agent._fork_refresh_pending == 0


def test_an_unconfirmed_private_copy_at_the_dashboard_author_stem_is_governance_filtered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F4: a fork NAMED AT the dashboard-author stem (a pre-upgrade private copy) has no
    owned writer re-filtering it unless the managed install would actually LAND on it. The
    loop must NOT skip it as "owned" when the install would be refused (no confirmation, or a
    blocking ``.md`` sibling) -- it must run the governance passes, so a grant the ceiling
    later tightened against is stripped rather than left live. When the install WOULD land,
    the owned installer handles it and the loop skips it."""
    name = fork_refresh._DASHBOARD_AUTHOR_STEM
    from kiro_crew.agent_materialization import worker_agent

    # The fork's OWN name equals the owned stem (a private copy at that filename), corroborated.
    _forks(monkeypatch, {name: {"private_to": "crew", "forked_from": name}})
    _bindings(monkeypatch, {"crew": name})
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    spec = tmp_path / (name + ".json")
    spec.write_text(
        json.dumps({"name": name, "tools": [], "allowedTools": ["@kirocrew-core/some_verb"]})
    )
    monkeypatch.setattr(agent, "agent_spec_path", lambda _n: spec)
    monkeypatch.setattr(agent, "_refresh_dynamic_fields", lambda *_a, **_k: None)
    written: list[dict[str, Any]] = []
    monkeypatch.setattr(agent, "_atomic_json_write", lambda _p, config: written.append(config))

    # Install would NOT land (refused: unconfirmed, or a blocking .md) -> NOT skipped as
    # owned; governance runs and the config is written (re-filtered).
    monkeypatch.setattr(worker_agent, "_managed_dashboard_author_install_lands", lambda p: False)
    agent._refresh_forked_templates_locked(gated_off=frozenset())
    assert [c["name"] for c in written] == [name], "unconfirmed stem copy must be filtered"
    assert agent._fork_refresh_failed == frozenset()

    # Install WOULD land -> the owned installer owns it; the loop skips it.
    written.clear()
    monkeypatch.setattr(worker_agent, "_managed_dashboard_author_install_lands", lambda p: True)
    agent._refresh_forked_templates_locked(gated_off=frozenset())
    assert written == [], "a confirmed-owned stem spec is left to its owned writer"
    assert agent._fork_refresh_failed == frozenset()


# --- crew-bound shared templates (not forks) -------------------------------------------


@pytest.fixture
def shared_template(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """No forks; one crew bound to a shared template carrying a denied grant."""
    from kiro_crew.agent_materialization import auto_approve

    _forks(monkeypatch, {})
    _bindings(monkeypatch, {"reviewer-crew": "reviewer", "default": "kirocrew"})
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    spec = tmp_path / "reviewer.json"
    monkeypatch.setattr(agent, "agent_spec_path", lambda name: tmp_path / f"{name}.json")
    # The tightened ceiling: denies the shell grant and every governed server's autoApprove.
    monkeypatch.setattr(auto_approve, "_may_auto_approve", lambda ref: ref != "execute_bash")

    def strip(servers: dict[str, Any]) -> dict[str, Any]:
        return {
            k: {kk: vv for kk, vv in v.items() if kk != "autoApprove"} for k, v in servers.items()
        }

    monkeypatch.setattr(auto_approve, "_strip_ungoverned_auto_approve", strip)
    return spec


def test_a_bound_shared_template_is_re_filtered_after_the_ceiling_tightens(
    shared_template: Path,
) -> None:
    doc = {
        "name": "reviewer",
        "prompt": "review carefully",
        "tools": ["fs_read", "execute_bash"],
        "allowedTools": ["fs_read", "execute_bash"],
        "mcpServers": {"gh": {"command": "gh-mcp", "autoApprove": ["*"]}},
    }
    shared_template.write_text(json.dumps(doc), encoding="utf-8")
    agent._refresh_forked_templates_locked(gated_off=frozenset())
    written = json.loads(shared_template.read_text(encoding="utf-8"))
    assert written["allowedTools"] == ["fs_read"]
    assert written["mcpServers"] == {"gh": {"command": "gh-mcp"}}
    # Mount list and human-authored fields are untouched.
    assert written["tools"] == ["fs_read", "execute_bash"]
    assert written["prompt"] == "review carefully"
    # A shared template is never a fork, so the spawn gate's failure record is unchanged.
    assert agent._fork_refresh_failed == frozenset()


def test_a_bound_shared_template_with_nothing_denied_is_not_rewritten(
    shared_template: Path,
) -> None:
    raw = '{ "name": "reviewer",   "allowedTools": ["fs_read"] }\n'
    shared_template.write_text(raw, encoding="utf-8")
    agent._refresh_forked_templates_locked(gated_off=frozenset())
    assert shared_template.read_text(encoding="utf-8") == raw


def test_an_unbound_template_is_never_written(
    shared_template: Path,
) -> None:
    loose = shared_template.parent / "loose.json"
    raw = json.dumps({"name": "loose", "allowedTools": ["execute_bash"]})
    loose.write_text(raw, encoding="utf-8")
    agent._refresh_forked_templates_locked(gated_off=frozenset())
    assert loose.read_text(encoding="utf-8") == raw


def test_an_owned_spec_bound_to_a_crew_is_left_to_its_own_writer(
    shared_template: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owned = shared_template.parent / "kirocrew.json"
    raw = json.dumps({"name": "kirocrew", "allowedTools": ["execute_bash"]})
    owned.write_text(raw, encoding="utf-8")
    agent._refresh_forked_templates_locked(gated_off=frozenset())
    assert owned.read_text(encoding="utf-8") == raw


def test_a_second_file_claiming_an_owned_name_is_filtered(
    shared_template: Path,
) -> None:
    """The owned installer rewrites only ``<name>.json``. Another file that declares the
    owned name is one kiro-cli may load as it, so it is filtered like a shared template
    while the owned file stays with its own writer."""
    owned = shared_template.parent / "kirocrew.json"
    raw = json.dumps({"name": "kirocrew", "allowedTools": ["execute_bash"]})
    owned.write_text(raw, encoding="utf-8")
    claimant = shared_template.parent / "pkg-kirocrew.json"
    claimant.write_text(json.dumps({"name": "kirocrew", "allowedTools": ["execute_bash"]}))
    agent._refresh_forked_templates_locked(gated_off=frozenset())
    assert json.loads(claimant.read_text(encoding="utf-8"))["allowedTools"] == []
    assert owned.read_text(encoding="utf-8") == raw
    assert fork_refresh._shared_template_held is False


def test_a_second_file_claiming_a_fork_name_is_filtered(
    shared_template: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fork refresh rewrites only the file the resolver picks for the fork. Another
    file that claims the fork's name is filtered here; the fork's own file is not
    written a second time."""
    _forks(monkeypatch, {"crewfork": {"private_to": "crew", "forked_from": "custom"}})
    _bindings(monkeypatch, {"crew": "crewfork"})
    fork_spec = shared_template.parent / "crewfork.json"
    fork_spec.write_text(json.dumps({"name": "crewfork", "allowedTools": ["fs_read"]}))
    claimant = shared_template.parent / "squat.json"
    claimant.write_text(json.dumps({"name": "crewfork", "allowedTools": ["execute_bash"]}))
    real_write = agent._atomic_json_write
    written: list[str] = []

    def record(path: Path, config: dict[str, Any]) -> None:
        written.append(path.name)
        real_write(path, config)

    monkeypatch.setattr(agent, "_atomic_json_write", record)
    agent._refresh_forked_templates_locked(gated_off=frozenset())
    assert json.loads(claimant.read_text(encoding="utf-8"))["allowedTools"] == []
    assert written == ["crewfork.json", "squat.json"]
    assert agent._fork_refresh_failed == frozenset()


def test_every_file_claiming_an_ambiguous_fork_name_is_filtered(
    shared_template: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two files declare the fork's name, so the fork refresh wrote neither and the fork
    is refused at the spawn gate; both claimants are still filtered here."""
    _forks(monkeypatch, {"crewfork": {"private_to": "crew", "forked_from": "custom"}})
    _bindings(monkeypatch, {"crew": "crewfork"})

    def ambiguous(_name: str) -> Path:
        raise ValueError("two specs declare crewfork")

    monkeypatch.setattr(agent, "agent_spec_path", ambiguous)
    paths = [shared_template.parent / n for n in ("a-fork.json", "b-fork.json")]
    for path in paths:
        path.write_text(json.dumps({"name": "crewfork", "allowedTools": ["execute_bash"]}))
    agent._refresh_forked_templates_locked(gated_off=frozenset())
    for path in paths:
        assert json.loads(path.read_text(encoding="utf-8"))["allowedTools"] == [], path.name
    assert agent._fork_refresh_failed == frozenset({"crewfork"})


def test_a_markdown_shared_template_is_warned_about_not_blocked(
    shared_template: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    md = shared_template.parent / "reviewer.md"
    md.write_text("---\nname: reviewer\n---\n", encoding="utf-8")
    monkeypatch.setattr(agent, "agent_spec_path", lambda _name: md)
    with caplog.at_level(logging.WARNING, logger=agent.logger.name):
        agent._refresh_forked_templates_locked(gated_off=frozenset())
    assert "markdown spec" in caplog.text
    assert agent._fork_refresh_failed == frozenset()


def test_a_fork_is_not_governed_twice_by_the_shared_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _forks(monkeypatch, {"crewfork": {"private_to": "crew", "forked_from": "custom"}})
    _bindings(monkeypatch, {"crew": "crewfork"})
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    spec = tmp_path / "crewfork.json"
    spec.write_text(json.dumps({"name": "crewfork", "tools": [], "allowedTools": []}))
    monkeypatch.setattr(agent, "agent_spec_path", lambda _name: spec)
    written: list[str] = []
    monkeypatch.setattr(agent, "_atomic_json_write", lambda _p, c: written.append(c["name"]))
    agent._refresh_forked_templates_locked(gated_off=frozenset())
    assert written == ["crewfork"]


def test_no_forks_and_an_unreadable_config_records_no_failure(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(agent, "_fork_refresh_failed", frozenset({"stale"}))
    _forks(monkeypatch, {})
    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(_raise))
    with caplog.at_level(logging.WARNING, logger=agent.logger.name):
        agent._refresh_forked_templates_locked(gated_off=frozenset())
    assert agent._fork_refresh_failed == frozenset()
    assert "shared template governance skipped" in caplog.text


def test_a_shared_template_whose_write_raises_is_held_for_a_retry(
    shared_template: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shared_template.write_text(json.dumps({"name": "reviewer", "allowedTools": ["execute_bash"]}))
    monkeypatch.setattr(agent, "_atomic_json_write", _raise)
    agent._refresh_forked_templates_locked(gated_off=frozenset())
    assert fork_refresh._shared_template_held is True
    assert agent._fork_refresh_failed == frozenset()


def test_a_shared_template_a_retry_cannot_fix_is_not_held(
    shared_template: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    md = shared_template.parent / "reviewer.md"
    md.write_text("---\nname: reviewer\n---\n", encoding="utf-8")
    monkeypatch.setattr(agent, "agent_spec_path", lambda _name: md)
    agent._refresh_forked_templates_locked(gated_off=frozenset())
    assert fork_refresh._shared_template_held is False


def test_a_landed_pass_clears_the_hold(
    shared_template: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fork_refresh, "_shared_template_held", True)
    shared_template.write_text(json.dumps({"name": "reviewer", "allowedTools": ["execute_bash"]}))
    agent._refresh_forked_templates_locked(gated_off=frozenset())
    assert fork_refresh._shared_template_held is False
    assert json.loads(shared_template.read_text(encoding="utf-8"))["allowedTools"] == []


def test_a_deferred_pass_that_holds_a_shared_template_sets_the_rebuild_hold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _threads(monkeypatch, _InlineThread)

    def holds(*, gated_off: frozenset[str] | None = None) -> None:
        monkeypatch.setattr(fork_refresh, "_shared_template_held", True)

    monkeypatch.setattr(agent, "_refresh_forked_templates_locked", holds)
    fork_refresh.refresh_after_rebuild("defer", frozenset())
    assert agent._conductor_spec_held is True


def test_a_capability_refusal_is_warned_about_not_held(
    shared_template: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from kiro_crew import agent_capabilities

    monkeypatch.setattr(agent_state, "get_capabilities", lambda _name: {"schema_version": 1})

    def refuse(_member: str) -> None:
        raise agent_capabilities.CapabilityError("materialization_changed")

    monkeypatch.setattr(agent_capabilities, "reconcile_member_capabilities", refuse)
    with caplog.at_level(logging.WARNING, logger=agent.logger.name):
        agent._refresh_forked_templates_locked(gated_off=frozenset())
    assert fork_refresh._shared_template_held is False
    assert "capability reconcile" in caplog.text


def test_an_unresolved_template_beside_an_unreadable_spec_is_held(
    shared_template: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shared_template.write_text(json.dumps({"name": "reviewer", "allowedTools": ["execute_bash"]}))
    monkeypatch.setattr(agent, "agent_spec_path", lambda _name: None)
    from kiro_crew import agent_discovery

    real_read = agent_discovery._read_spec_bytes

    def unreadable(path: Path) -> bytes:
        if path.name == "reviewer.json":
            raise OSError(errno.EIO, "input/output error")
        return real_read(path)

    monkeypatch.setattr(agent_discovery, "_read_spec_bytes", unreadable)
    agent._refresh_forked_templates_locked(gated_off=frozenset())
    assert fork_refresh._shared_template_held is True


def _unreadable(monkeypatch: pytest.MonkeyPatch, filename: str, exc: OSError) -> None:
    """Make every read of *filename* in the agents directory raise *exc*."""
    from kiro_crew import agent_discovery

    real_read = agent_discovery._read_spec_bytes

    def read(path: Path) -> bytes:
        if path.name == filename:
            raise exc
        return real_read(path)

    monkeypatch.setattr(agent_discovery, "_read_spec_bytes", read)


@pytest.mark.parametrize("filename", ["unrelated.json", "reviewer.json"])
def test_a_spec_this_user_may_not_read_never_holds(
    shared_template: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    filename: str,
) -> None:
    """kiro-cli runs as this same user, so a file that refuses this process's read is
    one it cannot load either. Holding on it would rebuild on every poll for nothing."""
    (shared_template.parent / filename).write_text(
        json.dumps({"name": "reviewer", "allowedTools": ["execute_bash"]})
    )
    _unreadable(monkeypatch, filename, PermissionError(errno.EACCES, "permission denied"))
    with caplog.at_level(logging.WARNING, logger=agent.logger.name):
        for _ in range(3):
            agent._refresh_forked_templates_locked(gated_off=frozenset())
            assert fork_refresh._shared_template_held is False
    assert "permission denied" in caplog.text


def _sharing_violation() -> PermissionError:
    """The error Windows raises while another process holds the file without read sharing."""
    exc = PermissionError(errno.EACCES, "the process cannot access the file")
    exc.winerror = 32  # type: ignore[attr-defined]
    return exc


@pytest.mark.parametrize("failing_read", [1, 2], ids=["scan", "locked-re-read"])
def test_a_windows_sharing_violation_is_held_not_skipped(
    shared_template: Path, monkeypatch: pytest.MonkeyPatch, failing_read: int
) -> None:
    """Windows raises a sharing violation as PermissionError, but it clears when the
    other handle closes: skipping it would let the memo advance on a template whose
    denied grant is still on disk, so both the scan and the re-read under the lock
    hold it."""
    shared_template.write_text(json.dumps({"name": "reviewer", "allowedTools": ["execute_bash"]}))
    from kiro_crew import agent_discovery

    real_read = agent_discovery._read_spec_bytes
    reads: list[str] = []

    def read(path: Path) -> bytes:
        if path.name == "reviewer.json":
            reads.append(path.name)
            if len(reads) == failing_read:
                raise _sharing_violation()
        return real_read(path)

    monkeypatch.setattr(agent_discovery, "_read_spec_bytes", read)
    agent._refresh_forked_templates_locked(gated_off=frozenset())
    assert fork_refresh._shared_template_held is True


def test_a_spec_that_keeps_failing_stays_held_until_its_read_succeeds(
    shared_template: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Releasing the hold while the read still fails would let the ceiling memo
    advance with a denied grant possibly still on disk, and nothing would retry it
    after the failure cleared. So a failure holds on every pass, and only the pass
    that reads the file clears it."""
    (shared_template.parent / "unrelated.json").write_text(json.dumps({"name": "unrelated"}))
    from kiro_crew import agent_discovery

    real_read = agent_discovery._read_spec_bytes
    _unreadable(monkeypatch, "unrelated.json", OSError(errno.EIO, "input/output error"))
    for _ in range(6):
        agent._refresh_forked_templates_locked(gated_off=frozenset())
        assert fork_refresh._shared_template_held is True
    monkeypatch.setattr(agent_discovery, "_read_spec_bytes", real_read)
    agent._refresh_forked_templates_locked(gated_off=frozenset())
    assert fork_refresh._shared_template_held is False


def test_a_stem_match_declaring_another_name_is_filtered_beside_a_declared_match(
    shared_template: Path,
) -> None:
    """The single-file resolver picks the spec that DECLARES the name and leaves the
    one that only matches by filename, so resolving through it would leave the
    stem match's denied grant on disk. This pass filters both."""
    shared_template.write_text(json.dumps({"name": "legacy", "allowedTools": ["execute_bash"]}))
    declared = shared_template.parent / "renamed.json"
    declared.write_text(json.dumps({"name": "reviewer", "allowedTools": ["execute_bash"]}))
    assert _REAL_AGENT_SPEC_PATH("reviewer", agents_dir=shared_template.parent) == declared
    agent._refresh_forked_templates_locked(gated_off=frozenset())
    for path in (shared_template, declared):
        assert json.loads(path.read_text(encoding="utf-8"))["allowedTools"] == [], path.name
    assert fork_refresh._shared_template_held is False


def test_an_unreadable_spec_is_read_once_per_pass_whatever_the_number_of_bindings(
    shared_template: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _bindings(monkeypatch, {"a": "reviewer", "b": "second", "c": "third"})
    (shared_template.parent / "unrelated.json").write_text(json.dumps({"name": "unrelated"}))
    _unreadable(monkeypatch, "unrelated.json", PermissionError(errno.EACCES, "permission denied"))
    with caplog.at_level(logging.WARNING, logger=agent.logger.name):
        agent._refresh_forked_templates_locked(gated_off=frozenset())
    assert caplog.text.count("permission denied") == 1


@requires_symlinks
def test_the_retry_probe_never_follows_a_link_the_resolver_refuses(
    shared_template: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    outside = tmp_path_factory.mktemp("outside") / "secret.json"
    outside.write_text("{}", encoding="utf-8")
    (shared_template.parent / "linked.json").symlink_to(outside)
    monkeypatch.setattr(agent, "agent_spec_path", lambda _name: None)
    from kiro_crew import agent_discovery

    opened: list[str] = []
    real_read = agent_discovery._read_spec_bytes

    def recording(path: Path) -> bytes:
        opened.append(path.name)
        return real_read(path)

    monkeypatch.setattr(agent_discovery, "_read_spec_bytes", recording)
    agent._refresh_forked_templates_locked(gated_off=frozenset())
    assert "linked.json" not in opened
    assert fork_refresh._shared_template_held is False


def test_an_unresolved_template_with_every_spec_readable_is_not_held(
    shared_template: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (shared_template.parent / "other.json").write_text(json.dumps({"name": "other"}))
    monkeypatch.setattr(agent, "agent_spec_path", lambda _name: None)
    agent._refresh_forked_templates_locked(gated_off=frozenset())
    assert fork_refresh._shared_template_held is False


def test_a_template_declaring_the_name_under_another_filename_is_filtered(
    shared_template: Path,
) -> None:
    renamed = shared_template.parent / "renamed.json"
    renamed.write_text(json.dumps({"name": "reviewer", "allowedTools": ["execute_bash"]}))
    agent._refresh_forked_templates_locked(gated_off=frozenset())
    assert json.loads(renamed.read_text(encoding="utf-8"))["allowedTools"] == []
    assert fork_refresh._shared_template_held is False


def test_every_spec_declaring_a_contested_name_is_filtered(
    shared_template: Path,
) -> None:
    """Two specs declare the bound name, so which one kiro-cli loads is undefined:
    both are filtered, and a markdown claimant does not stop the JSON one."""
    shared_template.write_text(json.dumps({"name": "reviewer", "allowedTools": ["execute_bash"]}))
    other = shared_template.parent / "z-other.json"
    other.write_text(json.dumps({"name": "reviewer", "allowedTools": ["execute_bash"]}))
    (shared_template.parent / "y-other.md").write_text("---\nname: reviewer\n---\n")
    agent._refresh_forked_templates_locked(gated_off=frozenset())
    for path in (shared_template, other):
        assert json.loads(path.read_text(encoding="utf-8"))["allowedTools"] == [], path.name
    assert fork_refresh._shared_template_held is False


def test_a_malformed_spec_at_the_bound_stem_is_not_held(
    shared_template: Path,
) -> None:
    shared_template.write_text("{not json", encoding="utf-8")
    agent._refresh_forked_templates_locked(gated_off=frozenset())
    assert shared_template.read_text(encoding="utf-8") == "{not json"
    assert fork_refresh._shared_template_held is False


@pytest.mark.parametrize("unreadable", ["_base_unreadable", "_overlay_unreadable"])
def test_a_config_load_that_fell_back_to_defaults_holds(
    shared_template: Path, monkeypatch: pytest.MonkeyPatch, unreadable: str
) -> None:
    degraded = SimpleNamespace(agents={}, **{unreadable: True})
    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: degraded))
    agent._refresh_forked_templates_locked(gated_off=frozenset())
    assert fork_refresh._shared_template_held is True


def test_a_config_degradation_seen_earlier_in_the_process_does_not_hold(
    shared_template: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole-config marker in ``degraded_sections`` lasts the process. A later load
    that read both files whole has every binding, so the hold must follow that load's
    own read, or one bad save would rebuild on every poll until a restart."""
    from kiro_crew.config.resolution import DEGRADED_WHOLE_CONFIG

    shared_template.write_text(json.dumps({"name": "reviewer", "allowedTools": ["execute_bash"]}))
    repaired = SimpleNamespace(
        agents={"reviewer-crew": SimpleNamespace(kiro_agent="reviewer")},
        degraded_sections=frozenset({DEGRADED_WHOLE_CONFIG, f"{DEGRADED_WHOLE_CONFIG}config.json"}),
        _base_unreadable=False,
        _overlay_unreadable=False,
    )
    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: repaired))
    agent._refresh_forked_templates_locked(gated_off=frozenset())
    assert json.loads(shared_template.read_text(encoding="utf-8"))["allowedTools"] == []
    assert fork_refresh._shared_template_held is False


def test_a_directory_listing_that_fails_after_its_stat_is_held(
    shared_template: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``Path.glob`` swallows a listing error and yields nothing. Read that way, a
    transient I/O error on the directory looks like an empty one, the memo advances,
    and the bound template keeps its revoked grants after the directory recovers."""
    shared_template.write_text(json.dumps({"name": "reviewer", "allowedTools": ["execute_bash"]}))
    agents_dir = os.fspath(shared_template.parent)
    real_scandir = os.scandir

    def failing(path: Any = ".") -> Any:
        if isinstance(path, (str, os.PathLike)) and os.fspath(path) == agents_dir:
            raise OSError(errno.EIO, "input/output error")
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", failing)
    agent._refresh_forked_templates_locked(gated_off=frozenset())
    assert fork_refresh._shared_template_held is True


def _hard_link(source: Path, alias: Path) -> None:
    os.link(source, alias)


def test_a_hard_linked_bound_template_is_held_until_the_extra_link_goes(
    shared_template: Path,
    tmp_path_factory: pytest.TempPathFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The fenced reader refuses a second hard link for good, while kiro-cli reads the
    same bytes without that fence. A refusal read as absence would advance the memo,
    and removing the extra link later would not bring the filter back."""
    shared_template.write_text(
        json.dumps({"name": "reviewer", "allowedTools": ["fs_read", "execute_bash"]})
    )
    alias = tmp_path_factory.mktemp("elsewhere") / "reviewer-copy.json"
    _hard_link(shared_template, alias)
    with caplog.at_level(logging.WARNING, logger=agent.logger.name):
        agent._refresh_forked_templates_locked(gated_off=frozenset())
    assert fork_refresh._shared_template_held is True
    assert json.loads(alias.read_text(encoding="utf-8"))["allowedTools"] == [
        "fs_read",
        "execute_bash",
    ], "nothing is written through the shared inode"
    assert "hard link" in caplog.text

    alias.unlink()
    agent._refresh_forked_templates_locked(gated_off=frozenset())
    assert json.loads(shared_template.read_text(encoding="utf-8"))["allowedTools"] == ["fs_read"]
    assert fork_refresh._shared_template_held is False


def test_a_hard_link_made_between_the_scan_and_the_locked_re_read_is_held(
    shared_template: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    shared_template.write_text(json.dumps({"name": "reviewer", "allowedTools": ["execute_bash"]}))
    alias = tmp_path_factory.mktemp("elsewhere") / "alias.json"
    real_scan = fork_refresh._scan_agent_specs

    def scan_then_link(agents_dir: Path) -> Any:
        scanned = real_scan(agents_dir)
        _hard_link(shared_template, alias)
        return scanned

    monkeypatch.setattr(fork_refresh, "_scan_agent_specs", scan_then_link)
    agent._refresh_forked_templates_locked(gated_off=frozenset())
    assert fork_refresh._shared_template_held is True


def test_a_hold_set_after_the_baseline_was_seeded_still_rebuilds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []

    def fake_reporting(**kw: Any) -> tuple[Path, bool]:
        calls.append(1)
        kw["_held_out"].append(False)
        return Path("kirocrew.json"), True

    monkeypatch.setattr(agent, "rebuild_agent_config_reporting", fake_reporting)
    # The memo the hook compares against: the ceiling plus every governance profile.
    monkeypatch.setattr(
        agent, "_projected_ceiling_generation", agent._answer_generation_after_profile_poll()
    )
    agent.reproject_for_ceiling_change()
    assert calls == [], "an unchanged generation with no hold rebuilds nothing"
    monkeypatch.setattr(agent, "_conductor_spec_held", True)
    agent.reproject_for_ceiling_change()
    assert calls == [1], "a hold must not be skipped as an unchanged generation"


def test_a_regular_file_the_reader_refuses_is_held_and_a_directory_is_not(
    shared_template: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refusal of a regular file is never read as absence: its link count or kernel
    path changed between the open and the check, and a retry may read it. A directory
    at a spec name is no spec kiro-cli loads, so the reader's refusal of it holds
    nothing."""
    from kiro_crew.agent_discovery import _SpecReadRefused

    (shared_template.parent / "folder.json").mkdir()
    agent._refresh_forked_templates_locked(gated_off=frozenset())
    assert fork_refresh._shared_template_held is False

    shared_template.write_text(json.dumps({"name": "reviewer", "allowedTools": ["execute_bash"]}))
    _unreadable(monkeypatch, "reviewer.json", _SpecReadRefused("refusing to read a file"))
    agent._refresh_forked_templates_locked(gated_off=frozenset())
    assert fork_refresh._shared_template_held is True


def test_a_directory_at_a_spec_name_holds_nothing_where_the_open_reports_it(
    shared_template: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows reports a directory from the open (``IsADirectoryError``) rather than
    from the reader's inode check; it is no more a spec there than on POSIX."""
    (shared_template.parent / "folder.json").mkdir()
    _unreadable(monkeypatch, "folder.json", IsADirectoryError(errno.EISDIR, "is a directory"))
    agent._refresh_forked_templates_locked(gated_off=frozenset())
    assert fork_refresh._shared_template_held is False
