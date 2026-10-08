"""The fork refresh fails closed on every branch that cannot vouch for a fork.

A fork carries ``allowedTools`` and ``autoApprove`` grants that never reach the
PreToolUse gate, so a fork whose last refresh did not re-filter them must not start a
session. ``_fork_refresh_failed`` is how the refresh says so: ``"*"`` when the pass
died before per-fork accounting, the fork's name when that one fork could not be
refreshed. These pin each branch, and that the spawn gate's settled event is set
again whenever no pass is left to set it.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Iterator

import pytest

from kiro_crew import agent, agent_state
from kiro_crew.agent_materialization import fork_refresh
from kiro_crew.config.loader import KiroCrewConfig


class _Boom(Exception):
    pass


def _raise(*_args: object, **_kwargs: object) -> None:
    raise _Boom("refresh died")


@pytest.fixture(autouse=True)
def _restore_refresh_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Every test starts settled with no failures, and leaves it that way."""
    monkeypatch.setattr(agent, "_fork_refresh_failed", frozenset())
    monkeypatch.setattr(agent, "_fork_refresh_pending", 0)
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


def test_a_declined_private_copy_at_a_declining_stem_is_governance_filtered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The team-lead stem's installer can DECLINE, so a private copy sitting at it may have
    NO owned writer re-filtering its grants. The loop must not skip such a file as "owned":
    its ``allowedTools``/``autoApprove`` would stay live against a ceiling tightened after
    it was written, while ``require_fork_governance`` still admits sessions on it.

    Driven through the real gate rather than a stubbed predicate, both ways, so the two
    answers come from the installer's own attribution:

    * a lineage record naming this name a crew's copy makes the install decline, so the
      governance passes must RUN and write the re-filtered config;
    * with no record the install lands, the owned writer re-filters it every rebuild, and
      the loop must SKIP it. That is the control -- without it a loop that filtered
      everything would pass the first half on its own.
    """
    from kiro_crew.agent_files import TEAM_LEAD_AGENT_FILENAME

    name = Path(TEAM_LEAD_AGENT_FILENAME).stem
    _forks(monkeypatch, {name: {"private_to": "crew", "forked_from": "kirocrew-conductor"}})
    _bindings(monkeypatch, {"crew": name})
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    # Exactly the shape a fork of a conductor leaves at this name: both attribution marks
    # hold, so only the lineage record can tell it from the installer's own write.
    spec = tmp_path / (name + ".json")
    spec.write_text(
        json.dumps(
            {
                "name": name,
                "mcpServers": {"kirocrew-dashboard": {}, "kirocrew-work": {}},
                "tools": [],
                "allowedTools": ["@kirocrew-core/some_verb"],
            }
        )
    )
    monkeypatch.setattr(agent, "agent_spec_path", lambda _n: spec)
    monkeypatch.setattr(agent, "_refresh_dynamic_fields", lambda *_a, **_k: None)
    written: list[dict[str, Any]] = []
    monkeypatch.setattr(agent, "_atomic_json_write", lambda _p, config: written.append(config))

    agent_state.set_fork_info(name, forked_from="kirocrew-conductor", private_to="crew")
    agent._refresh_forked_templates_locked(gated_off=frozenset())
    assert [c["name"] for c in written] == [name], "a declined copy must be governance-filtered"
    assert agent._fork_refresh_failed == frozenset()

    # With no record AND the ownership digest recorded, the install lands, so the owned
    # writer re-filters it and the loop skips it. The digest is what ownership means here:
    # the file's shape alone is not authorship.
    written.clear()
    agent_state.clear_fork_info(name)
    agent_state.set_managed_digest(
        name, agent_state.spec_digest(json.loads(spec.read_text(encoding="utf-8")))
    )
    agent._refresh_forked_templates_locked(gated_off=frozenset())
    assert written == [], "a spec the installer owns is left to its owned writer"
    assert agent._fork_refresh_failed == frozenset()


def test_a_plain_owned_stem_needs_no_provenance_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """The other half of the registry's rule, and what keeps it from spreading: a stem whose
    installer writes every rebuild has no gate, so its filename stays the whole answer and
    the loop skips it without reading anything off the disk."""
    assert agent.owned_provenance_gate("kirocrew-worker") is None
    assert agent.owned_provenance_gate("kirocrew-conductor") is None
    # The two that DO decline are gated, which is the control for the Nones above.
    assert agent.owned_provenance_gate("kirocrew-dashboard-author") is not None
    assert agent.owned_provenance_gate("kirocrew-team-lead") is not None
