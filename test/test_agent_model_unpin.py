"""Returning the global ``agent.model`` to "auto" clears a managed spec pin.

``_refresh_dynamic_fields`` propagates the global ``agent.model`` from
config.json INTO the kiro agent spec. A spec pin outranks the global in
``resolve_effective_model``, so if the propagation never reverses itself the
"auto" setting is unreachable from the configuration surface: the write succeeds
and is honoured nowhere, and the stale pin keeps going out on the wire.

Ownership is the sidecar's ``model_managed`` flag, and it is the whole scope
boundary here:

- ``True``  -> the propagation owns the field, so it may clear it.
- ``False`` -> an explicit user pick, frozen against default bumps; untouched.
- unset     -> legacy status (ownership unknown); untouched while the global
  defers. Migrating those is tracked separately and deliberately NOT done here;
  only a published spec carrying a concrete global claims one.

The shipped template that ships today happens to pin "auto", which masks the
defect, so the tests below pin a template that declares no model — the condition
under which a managed spec kept a stale concrete pin forever.
"""

from __future__ import annotations

import json
import tempfile
import unittest.mock
from pathlib import Path

import pytest

from kiro_crew import agent_state
from kiro_crew.agent import _claim_propagated_model, _refresh_dynamic_fields
from kiro_crew.config import config_path
from kiro_crew.config.loader import DEFAULT_MODEL, KiroCrewConfig, resolve_effective_model

_AGENT = "kirocrew"
_PINNED = "claude-opus-5"


@pytest.fixture
def shipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point ``_shipped_defaults`` at a template whose model we control."""

    def _write(model: str | None) -> Path:
        payload: dict[str, object] = {"name": _AGENT}
        if model is not None:
            payload["model"] = model
        path = tmp_path / "shipped-defaults.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        monkeypatch.setattr("kiro_crew.agent._shipped_defaults", lambda: path)
        return path

    return _write


def _set_global(model: str) -> None:
    """Write the user-facing global ``agent.model`` the propagation reads."""
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"agent": {"model": model}}), encoding="utf-8")


def _refresh(spec_model: str) -> dict:
    config: dict = {"name": _AGENT, "model": spec_model, "tools": []}
    _refresh_dynamic_fields(config)
    return config


class TestManagedPinFollowsTheGlobal:
    """A pin this propagation wrote must come back off when the global does."""

    def test_managed_pin_cleared_when_the_global_returns_to_auto(self, shipped) -> None:
        shipped(None)
        agent_state.set_model_managed(_AGENT, True)
        _set_global("auto")

        # Cleared means the inherit sentinel, not a deleted key: it is what a
        # clean install writes, and the resolver reads the two identically.
        assert _refresh(_PINNED)["model"] == DEFAULT_MODEL

    def test_a_concrete_global_still_propagates(self, shipped) -> None:
        """The forward direction is unchanged: an explicit global reaches the spec."""
        shipped(None)
        agent_state.set_model_managed(_AGENT, True)
        _set_global("claude-haiku-4.5")

        assert _refresh(_PINNED)["model"] == "claude-haiku-4.5"

    def test_managed_spec_still_tracks_a_concrete_shipped_default(self, shipped) -> None:
        """Clearing must not fight the re-sync branch it sits next to.

        A managed spec under a global of "auto" resolves to the SHIPPED default,
        not to the sentinel — otherwise a shipped default bump would stop
        reaching existing installs, and the two branches would disagree about
        what a managed spec holds.
        """
        shipped("claude-sonnet-4.6")
        agent_state.set_model_managed(_AGENT, True)
        _set_global("auto")

        assert _refresh(_PINNED)["model"] == "claude-sonnet-4.6"

    def test_an_absent_global_is_treated_as_auto(self, shipped) -> None:
        """No ``agent.model`` key at all is the same deferral as "auto"."""
        shipped(None)
        agent_state.set_model_managed(_AGENT, True)
        config_path().parent.mkdir(parents=True, exist_ok=True)
        config_path().write_text(json.dumps({}), encoding="utf-8")

        assert _refresh(_PINNED)["model"] == DEFAULT_MODEL


class TestTheGlobalIsReadThroughTheResolversNormalizer:
    """config.json is hand-editable, and the spec it feeds is schema-validated.

    kiro-cli validates ``~/.kiro/agents/*.json`` with ``deny_unknown_fields`` and
    rejects the whole spec on a bad value, silently falling back to the default
    agent. Judging "does the global defer?" with ``normalize_agent_model`` — the
    same chokepoint the resolver uses — keeps a junk value out of the spec and
    keeps the two surfaces from disagreeing about what "auto" means.
    """

    @pytest.mark.parametrize("spelling", ["auto", "  auto  ", "", "   "])
    def test_an_inherit_spelling_defers_instead_of_being_propagated(
        self, shipped, spelling: str
    ) -> None:
        shipped(None)
        agent_state.set_model_managed(_AGENT, True)
        _set_global(spelling)

        assert _refresh(_PINNED)["model"] == DEFAULT_MODEL

    @pytest.mark.parametrize("junk", [123, 1.5, [], {}, None])
    def test_a_non_string_global_never_reaches_the_spec(self, shipped, junk: object) -> None:
        shipped(None)
        agent_state.set_model_managed(_AGENT, True)
        config_path().parent.mkdir(parents=True, exist_ok=True)
        config_path().write_text(json.dumps({"agent": {"model": junk}}), encoding="utf-8")

        assert _refresh(_PINNED)["model"] == DEFAULT_MODEL

    def test_a_concrete_global_is_propagated_trimmed(self, shipped) -> None:
        shipped(None)
        agent_state.set_model_managed(_AGENT, True)
        _set_global("  claude-haiku-4.5  ")

        assert _refresh(_PINNED)["model"] == "claude-haiku-4.5"


class TestOwnershipBoundary:
    """Only a pin the propagation owns may be cleared."""

    def test_a_spec_with_no_sidecar_entry_keeps_its_pin(self, shipped) -> None:
        """A legacy-status agent is untouched: ownership is unknown, so clearing
        could silently drop a model the user chose. Migrating them is a separate
        decision and this test is the guard that it did not happen here."""
        shipped(None)
        assert agent_state.get_model_managed(_AGENT) is None
        _set_global("auto")

        assert _refresh(_PINNED)["model"] == _PINNED

    def test_an_explicit_user_pick_keeps_its_pin(self, shipped) -> None:
        """``model_managed=False`` is a pick frozen against default bumps."""
        shipped(None)
        agent_state.set_model_managed(_AGENT, False)
        _set_global("auto")

        assert _refresh(_PINNED)["model"] == _PINNED


class TestPropagationClaimsALegacyPin:
    """A published spec carrying the concrete global makes that pin the propagation's.

    A legacy-status spec (no sidecar entry) keeps an unknown-owner pin, but once
    a spec holding the concrete global is on disk, whatever it held before is
    gone. Leaving the entry unset there strands that value: setting the global
    back to "auto" is a no-op for a legacy spec, so the model the user just
    turned off keeps running.
    """

    def _publish(self, spec_model: str) -> dict:
        """Refresh, then claim exactly as ``rebuild_agent_config`` does after its write."""
        config = _refresh(spec_model)
        _claim_propagated_model(config, _AGENT)
        return config

    def test_concrete_then_auto_clears_a_legacy_spec(self, shipped) -> None:
        shipped(None)
        assert agent_state.get_model_managed(_AGENT) is None

        _set_global(_PINNED)
        pinned = self._publish("claude-sonnet-4.6")
        assert pinned["model"] == _PINNED
        assert agent_state.get_model_managed(_AGENT) is True

        _set_global("auto")
        assert self._publish(pinned["model"])["model"] == DEFAULT_MODEL

    def test_the_refresh_alone_claims_nothing(self, shipped) -> None:
        """Ownership is recorded only after publication, never by the refresh."""
        shipped(None)
        _set_global(_PINNED)
        _refresh("claude-sonnet-4.6")

        assert agent_state.get_model_managed(_AGENT) is None

    def test_an_explicit_pick_is_not_claimed(self, shipped) -> None:
        """``False`` stays ``False``: the flag is the editor's record of a pick, and
        flipping it would also flip that agent's pinned badge and the worker mirror's
        pin check. Pinned as the CURRENT behaviour, the residual case: after a
        concrete global and back to "auto", the spec keeps the global's old value,
        not the user's original pick, which is not stored anywhere."""
        shipped(None)
        agent_state.set_model_managed(_AGENT, False)

        _set_global(_PINNED)
        pinned = self._publish("claude-sonnet-4.6")
        assert agent_state.get_model_managed(_AGENT) is False

        _set_global("auto")
        assert self._publish(pinned["model"])["model"] == _PINNED

    def test_an_auto_global_does_not_claim_a_legacy_spec(self, shipped) -> None:
        """A deferring global wrote nothing, so there is nothing to claim."""
        shipped(None)
        _set_global("auto")
        self._publish(_PINNED)

        assert agent_state.get_model_managed(_AGENT) is None

    def test_a_spec_that_does_not_carry_the_global_is_not_claimed(self, shipped) -> None:
        """A fork refresh, or a global changed after the write, leaves a different
        model on disk; that value is not the propagation's."""
        shipped(None)
        _set_global(_PINNED)
        _claim_propagated_model({"name": _AGENT, "model": "claude-sonnet-4.6"}, _AGENT)

        assert agent_state.get_model_managed(_AGENT) is None

    def test_an_unreadable_sidecar_claims_nothing(
        self, shipped, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        shipped(None)
        _set_global(_PINNED)

        def _unreadable(name: str) -> bool:
            raise ValueError("agent_state_file_invalid")

        monkeypatch.setattr(agent_state, "claim_model_managed_if_unset", _unreadable)

        _claim_propagated_model({"name": _AGENT, "model": _PINNED}, _AGENT)  # no raise
        assert agent_state.get_model_managed(_AGENT) is None


class TestTheClaimFollowsPublication:
    """``rebuild_agent_config`` claims ownership only once the spec write returned."""

    def _legacy_spec(self, agents_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from kiro_crew import agent

        agents_dir.mkdir()
        (agents_dir / agent.AGENT_FILENAME).write_text(
            json.dumps({"name": _AGENT, "model": "claude-sonnet-4.6", "tools": []}),
            encoding="utf-8",
        )
        monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: agents_dir)
        monkeypatch.setattr(agent, "_decline_shared_agent_home", lambda **_k: None)

    def test_a_failed_spec_write_leaves_a_legacy_spec_unclaimed(
        self, shipped, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew import agent
        from kiro_crew.agent_materialization import default_spec_commit

        shipped(None)
        self._legacy_spec(tmp_path / "agents", monkeypatch)
        _set_global(_PINNED)

        def _enospc(*args: object, **kwargs: object) -> None:
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(default_spec_commit, "write_default_spec", _enospc)
        with pytest.raises(OSError):
            agent.rebuild_agent_config()

        assert agent_state.get_model_managed(_AGENT) is None

    def test_a_published_spec_write_claims_a_legacy_spec(
        self, shipped, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew import agent

        shipped(None)
        self._legacy_spec(tmp_path / "agents", monkeypatch)
        _set_global(_PINNED)

        written = agent.rebuild_agent_config()

        assert json.loads(written.read_text(encoding="utf-8"))["model"] == _PINNED
        assert agent_state.get_model_managed(_AGENT) is True


class TestResolverOracle:
    """``resolve_effective_model`` is the regression oracle: "" means Auto."""

    def test_round_trip_concrete_then_auto_reports_auto(
        self, shipped, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        shipped(None)
        agent_state.set_model_managed(_AGENT, True)
        specs = tmp_path / "agents"
        specs.mkdir()
        monkeypatch.setattr("kiro_crew.config.loader.kiro_agents_dir", lambda: specs)

        # Global set to a concrete model: the spec carries it, and the resolver
        # reports it.
        _set_global(_PINNED)
        pinned_spec = _refresh("")
        (specs / f"{_AGENT}.json").write_text(json.dumps(pinned_spec), encoding="utf-8")
        assert resolve_effective_model(_load_config(), _AGENT) == _PINNED

        # Global back to "auto": the pin must come off, so the resolver defers.
        _set_global("auto")
        cleared_spec = _refresh(pinned_spec["model"])
        (specs / f"{_AGENT}.json").write_text(json.dumps(cleared_spec), encoding="utf-8")
        assert resolve_effective_model(_load_config(), _AGENT) == ""


def _load_config() -> KiroCrewConfig:
    """Load a config whose global ``agent.model`` is the one just written."""
    data = json.loads(config_path().read_text(encoding="utf-8"))
    data.setdefault("agents", {_AGENT: {"kiro_agent": _AGENT}})
    data.setdefault("default_agent", _AGENT)
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(data, f)
        tmp = Path(f.name)
    try:
        with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=tmp):
            return KiroCrewConfig.load()
    finally:
        tmp.unlink(missing_ok=True)
