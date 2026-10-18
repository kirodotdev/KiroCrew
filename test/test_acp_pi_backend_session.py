"""A pi session: the reasoning-effort channel, the model pin, cancel and sign-out.

pi takes a reasoning-effort change under its own option id and vocabulary, the
control is reachable on this harness and a persisted level survives a cold start;
a model pi cannot select leaves the session on its default; ``session/cancel``
answers an open gate dialog as cancelled; and a signed-out pi names the fix and is
not retried.
"""

from __future__ import annotations

import asyncio
import inspect
import json

import acp_pi_backend_helpers
import pytest
from acp_pi_backend_helpers import ROOT

from kiro_crew.acp import client as acp_client
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.types import JsonRpcMessage
from kiro_crew.acp_backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKEND_DEEPSEEK,
    ACP_BACKEND_GOOSE,
    ACP_BACKEND_KIRO,
    ACP_BACKEND_OPENCODE,
    ACP_BACKEND_PI,
    ACP_BACKENDS_EFFORT_FROM_ADVERTISED_OPTION,
    ACP_BACKENDS_EFFORT_VIA_CONFIG_OPTION,
    ACP_BACKENDS_KNOWN,
    effort_config_option_id,
    effort_config_option_value,
)
from kiro_crew.agent_sdk import backend_cards
from kiro_crew.effort import EFFORT_LEVELS

# The shared autouse fixture, bound here by name so pytest registers it for this module.
_no_ambient_pi_env = acp_pi_backend_helpers._no_ambient_pi_env


def _pi_session_frames() -> list[dict]:
    """Every frame of the live ``session/new`` capture, header line excluded."""
    path = ROOT / "test" / "fixtures" / "acp_frames" / "pi" / "session-live.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()[1:]]


def _pi_thought_level_option() -> dict:
    """The ``thought_level`` select pi advertised on that capture's session/new."""
    for frame in _pi_session_frames():
        for option in (frame.get("result") or {}).get("configOptions") or []:
            if isinstance(option, dict) and option.get("id") == "thought_level":
                return option
    raise AssertionError("no thought_level option in the pi session capture")


class TestTheEffortChannel:
    """pi takes a reasoning-effort change, under its own option id and vocabulary.

    The channel was read as ABSENT for this harness while the evidence for it sat in
    the committed corpus: a different option id is a spelling the tree already
    resolves per harness, not a missing feature, so the card reported no effort
    control on a harness whose own option describes itself as setting one.
    """

    def test_the_fixture_carries_the_select_this_membership_rests_on(self) -> None:
        """Membership is load-bearing, so it is pinned to the capture, not to prose."""
        option = _pi_thought_level_option()

        assert option["type"] == "select"
        assert [o["value"] for o in option["options"]] == [
            "off",
            "minimal",
            "low",
            "medium",
            "high",
            "xhigh",
        ]

    def test_the_option_id_is_this_harness_own_spelling(self) -> None:
        """The table answers a spelling; the harnesses that match keep the default."""
        assert effort_config_option_id(ACP_BACKEND_PI) == "thought_level"
        assert effort_config_option_id(ACP_BACKEND_PI) == _pi_thought_level_option()["id"]
        assert effort_config_option_id(ACP_BACKEND_CLAUDE) == "effort"
        assert effort_config_option_id(ACP_BACKEND_KIRO) == "effort"
        assert effort_config_option_id(ACP_BACKEND_CODEX) == "reasoning_effort"

    def test_the_channel_membership_is_explicit(self) -> None:
        """Written out rather than derived from the set, which would pass tautologically."""
        assert ACP_BACKEND_PI in ACP_BACKENDS_EFFORT_VIA_CONFIG_OPTION
        assert ACP_BACKEND_OPENCODE in ACP_BACKENDS_EFFORT_VIA_CONFIG_OPTION
        assert ACP_BACKEND_KIRO not in ACP_BACKENDS_EFFORT_VIA_CONFIG_OPTION

    def test_the_one_level_pi_does_not_advertise_is_folded_to_its_ceiling(self) -> None:
        """``max`` is Crew's top level and pi has no such value, so it is spelled down.

        The fold's target is checked against the capture rather than written twice:
        a table naming a value this harness never advertised is the same defect as
        pushing ``max`` itself.
        """
        advertised = [o["value"] for o in _pi_thought_level_option()["options"]]

        assert "max" not in advertised
        assert effort_config_option_value(ACP_BACKEND_PI, "max") == "xhigh"
        assert effort_config_option_value(ACP_BACKEND_PI, "max") in advertised

    def test_every_level_pi_does_advertise_is_written_verbatim(self) -> None:
        """The table is one fold, not a translation layer: the rest pass through."""
        advertised = [o["value"] for o in _pi_thought_level_option()["options"]]

        for level in EFFORT_LEVELS:
            if level in advertised:
                assert effort_config_option_value(ACP_BACKEND_PI, level) == level

    def test_no_other_harness_gains_a_fold(self) -> None:
        """A row is an exception; every backend without one writes Crew's own spelling."""
        for backend in ACP_BACKENDS_KNOWN - {ACP_BACKEND_PI}:
            for level in EFFORT_LEVELS:
                assert effort_config_option_value(backend, level) == level

    def test_an_unknown_level_is_not_invented(self) -> None:
        """A value outside Crew's ladder -- a level a harness advertised itself, which
        the dropdown offers and persistence admits -- reaches the wire unchanged."""
        assert effort_config_option_value(ACP_BACKEND_PI, "minimal") == "minimal"
        assert effort_config_option_value(ACP_BACKEND_PI, "off") == "off"

    def test_the_levels_parser_fills_the_dropdown_from_pi_own_option(self, tmp_path) -> None:
        """The reader that fills the dropdown is keyed on the resolved id.

        A hard-coded ``effort`` returns an empty list here, which every caller reads
        as "this model has no effort levels" -- the dropdown then offers nothing on a
        harness that advertised six values.
        """
        client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_PI)
        client._acp_config_options = [_pi_thought_level_option()]

        assert client.get_valid_effort_levels() == [
            "off",
            "minimal",
            "low",
            "medium",
            "high",
            "xhigh",
        ]
        assert client.supports_config_option("thought_level") is True

    def test_the_card_line_reads_available(self) -> None:
        """The card is a projection of the set, so the user-visible answer flips with it.

        ``available`` is pinned together with ``measured``, because the card reads in
        three states and only two of them are this line's to claim. An entry in
        ``DECLARED_UNMEASURED`` would make ``available`` False on a line nobody had
        looked at -- a true statement about Crew's measurements, and the wrong one
        here: this line rests on a captured ``session/new``, so it is measured, and
        the harness does advertise the option.
        """
        card = backend_cards.card_for(ACP_BACKEND_PI)
        line = next(x for x in card.capabilities if x.id == backend_cards.LINE_REASONING_EFFORT)

        assert line.available is True
        assert line.measured is True
        assert line.unmeasured_reason == ""
        assert (
            "ACP_BACKEND_PI",
            backend_cards.LINE_REASONING_EFFORT,
        ) not in backend_cards.DECLARED_UNMEASURED

    def test_the_push_resolves_the_value_before_the_descent(self) -> None:
        """The fold is a declared fact, not something the step-down can be left to find.

        The descent below it only recovers from an unadvertised value when the
        refusal arrives in a shape ``_is_config_value_rejection`` recognises for that
        adapter, and the pi corpus carries no config-value refusal at all -- so a
        push that indexed the ladder with the RAW level would send pi a value it
        never advertised and rest on an unchecked guess about the answer.
        """
        from kiro_crew.providers import acp as provider_module

        body = inspect.getsource(provider_module.AcpProvider._set_effort_config_option)

        assert "effort_config_option_value(self._client.backend, level)" in body
        assert "EFFORT_LEVELS.index(level)" not in body
        assert "EFFORT_LEVELS.index(target)" in body

    def test_every_effort_write_site_resolves_the_value(self) -> None:
        """Four sites write a level; one writing it raw is the divergence the resolver
        exists to prevent -- the session runs a level the UI does not report."""
        from kiro_crew.acp import client as client_module
        from kiro_crew.knowledge import llm_pool
        from kiro_crew.providers import acp as provider_module

        assert "effort_config_option_value(" in inspect.getsource(
            provider_module.AcpProvider._set_effort_config_option
        )
        assert "effort_config_option_value(" in inspect.getsource(
            client_module._push_model_via_effort_split
        )
        assert "effort_config_option_value(" in inspect.getsource(llm_pool.AcpWorker._apply_effort)


# The ordinary pi model: an id out of the operator's own ``models.json``, which is
# what the capture recorded and what Crew's model registry does not carry.
PI_MODEL = "ollama/llama3.2:3b"


def _pi_provider(tmp_path, *, model: str = PI_MODEL, options=None):
    """An ``AcpProvider`` on pi with a session's config options already stored."""
    from kiro_crew.providers.acp import AcpProvider

    provider = AcpProvider(acp_backend=ACP_BACKEND_PI, work_dir=tmp_path, model=model)
    provider._client._acp_config_options = (
        [_pi_thought_level_option()] if options is None else options
    )
    provider._client._model = model
    return provider


class TestTheEffortControlIsReachableOnThisHarness:
    """Joining the channel set is worth nothing while a NAME test hides the control.

    ``model_supports_effort`` answers True for the Claude and GPT families and False
    for every id it does not recognise. pi serves the operator's own
    ``provider/model`` ids, so that test answers False for the ordinary pi session --
    and it guards all four effort verbs. The harness that advertises the option per
    session is asked for the option instead.
    """

    def test_the_registry_does_not_recognise_the_model_pi_normally_serves(self) -> None:
        """The premise, stated so the tests below cannot pass for the wrong reason."""
        from kiro_crew.effort import model_supports_effort

        assert model_supports_effort(PI_MODEL) is False

    def test_the_advertised_option_answers_for_this_harness(self) -> None:
        """Written out per harness, so a silently granted authority names its own."""
        assert ACP_BACKENDS_EFFORT_FROM_ADVERTISED_OPTION == frozenset(
            {ACP_BACKEND_DEEPSEEK, ACP_BACKEND_GOOSE, ACP_BACKEND_OPENCODE, ACP_BACKEND_PI}
        )
        assert ACP_BACKEND_KIRO not in ACP_BACKENDS_EFFORT_FROM_ADVERTISED_OPTION
        assert ACP_BACKEND_CLAUDE not in ACP_BACKENDS_EFFORT_FROM_ADVERTISED_OPTION

    def test_the_dropdown_is_offered_on_the_ordinary_pi_session(self, tmp_path) -> None:
        """The user-visible half: the control appears on a model the registry rejects."""
        provider = _pi_provider(tmp_path)

        assert provider.supports_effort() is True
        assert provider.get_valid_effort_levels() == [
            "off",
            "minimal",
            "low",
            "medium",
            "high",
            "xhigh",
        ]

    def test_a_session_advertising_no_such_option_is_still_refused(self, tmp_path) -> None:
        """Fail-closed stays fail-closed: the option, not the membership, decides."""
        provider = _pi_provider(tmp_path, options=[{"id": "model", "options": []}])

        assert provider.supports_effort() is False

    def test_the_registry_still_answers_for_a_harness_whose_level_rides_the_model(
        self, tmp_path
    ) -> None:
        """The other arm. kiro-cli refuses effort per model, so the name test is right
        there -- and a non-member must not inherit the advertised-option answer."""
        from kiro_crew.providers.acp import AcpProvider

        provider = AcpProvider(acp_backend=ACP_BACKEND_KIRO, work_dir=tmp_path)
        provider._client._model = PI_MODEL
        provider._client._acp_config_options = [_pi_thought_level_option()]

        assert provider._advertised_effort_levels() is None
        assert provider.supports_effort() is False

    def test_a_persisted_level_survives_startup_resolution(self, tmp_path) -> None:
        """The other half of the finding: a stored level the harness advertised.

        ``minimal`` is outside Crew's own ladder, so the canonical validity check
        drops it -- the dashboard offers and stores a level the session then never
        applies, and the UI reports one the session is not running.
        """
        provider = _pi_provider(tmp_path)
        provider._effort_per_model = {PI_MODEL: "minimal"}

        assert provider._resolve_effort() == "minimal"

    def test_a_level_the_harness_never_advertised_is_still_dropped(self, tmp_path) -> None:
        """The advertised list is a filter, not a bypass.

        The example is a level with neither an advertised match nor a row in
        ``EFFORT_CONFIG_OPTION_VALUES``; ``max`` has the latter, and
        ``TestTheFoldAndTheFilterAgree`` is where that pairing is pinned.
        """
        provider = _pi_provider(tmp_path)
        provider._effort_per_model = {PI_MODEL: "gargantuan"}

        assert effort_config_option_value(ACP_BACKEND_PI, "gargantuan") == "gargantuan"
        assert provider._resolve_effort() is None

    def test_a_workspace_default_is_read_through_the_same_filter(self, tmp_path) -> None:
        """Defaults take the same route as overrides, so neither answers alone."""
        provider = _pi_provider(tmp_path)
        provider._effort_defaults = {PI_MODEL: "minimal"}

        assert provider._resolve_effort() == "minimal"

    def test_all_four_effort_verbs_read_one_answer(self) -> None:
        """A second copy of this question is how one verb offers what another refuses.

        ``change_effort`` and ``clear_effort`` gated on the model test directly, so a
        pi user could be shown a dropdown whose every write was declined.
        """
        from kiro_crew.providers import acp as provider_module

        for method in ("change_effort", "clear_effort"):
            body = inspect.getsource(getattr(provider_module.AcpProvider, method))
            assert "self.supports_effort()" in body, method
            assert "model_supports_effort(" not in body, method

        resolve = inspect.getsource(provider_module.AcpProvider._resolve_effort)
        assert "levels=self._advertised_effort_levels()" in resolve

    def test_the_shared_resolver_keeps_its_registry_behaviour_untouched(self) -> None:
        """Every existing caller omits ``levels``, so the model registry still answers."""
        from kiro_crew.effort import resolve_effort_for_model

        assert resolve_effort_for_model("claude-opus-4.8", {"claude-opus-4.8": "max"}) == "max"
        assert resolve_effort_for_model(PI_MODEL, {PI_MODEL: "high"}) is None
        assert resolve_effort_for_model("claude-opus-4.8", {"claude-opus-4.8": "minimal"}) is None
        assert resolve_effort_for_model(PI_MODEL, {PI_MODEL: "high"}, levels=[]) is None
        assert resolve_effort_for_model(None, {"": "high"}, levels=["high"]) is None


class TestAPersistedLevelSurvivesAColdStart:
    """The provider FACTORY is the fourth reader of "does a level apply here".

    It runs before any session exists, so it cannot ask the advertised option --
    and asking the model registry there drops the level on every cold start: the
    session runs the adapter's own default while the dashboard still shows the
    level the operator picked, with nothing to correct it but a manual re-pick.
    """

    def _factory_kwargs(self, backend: str, model: str, level: str) -> dict:
        """The kwargs the provider factory would construct AcpProvider with."""
        from unittest.mock import MagicMock, patch

        from kiro_crew.config.loader import KiroCrewConfig

        cfg = KiroCrewConfig()
        cfg.agent.provider = "acp"
        cfg.agent.acp_backend = backend
        with patch("kiro_crew.providers.acp.AcpProvider") as mock_provider:
            mock_provider.return_value = MagicMock()
            factory = cfg.create_provider_factory()
            factory(
                session_key="dashboard:1",
                model_override=model,
                reasoning_effort_override=level,
            )
            assert mock_provider.called, "factory did not construct AcpProvider"
            return mock_provider.call_args.kwargs

    def test_the_factory_carries_the_level_to_the_session(self) -> None:
        """Carried, not judged: the advertised list judges it once session/new answers."""
        kwargs = self._factory_kwargs(ACP_BACKEND_PI, PI_MODEL, "high")

        assert kwargs.get("effort_per_model") == {PI_MODEL: "high"}

    def test_a_level_outside_crew_ladder_is_carried_too(self) -> None:
        """``minimal`` is pi's own, so the canonical ladder must not be the filter here."""
        kwargs = self._factory_kwargs(ACP_BACKEND_PI, PI_MODEL, "minimal")

        assert kwargs.get("effort_per_model") == {PI_MODEL: "minimal"}

    def test_the_registry_still_filters_a_harness_it_can_answer_for(self) -> None:
        """The other arm: on kiro the level rides the model, and ``auto`` takes none."""
        kwargs = self._factory_kwargs(ACP_BACKEND_KIRO, "auto", "high")

        assert kwargs.get("effort_per_model") == {}

    def test_the_two_halves_meet(self, tmp_path) -> None:
        """The factory's output, fed to the resolver, is what the session applies.

        Stated as one assertion because the halves are only useful together: the
        factory carrying a level the resolver then drops, or the resolver accepting
        one the factory never passes, is the same silent divergence in two places.
        """
        carried = self._factory_kwargs(ACP_BACKEND_PI, PI_MODEL, "minimal")
        provider = _pi_provider(tmp_path)
        provider._effort_per_model = dict(carried["effort_per_model"])

        assert provider._resolve_effort() == "minimal"


class TestTheFoldAndTheFilterAgree:
    """Two rules of this change meet on one stored value, and their ORDER decides.

    ``change_effort`` admits any level the dynamic validation set knows -- which is
    every harness's advertised vocabulary merged, plus Crew's own -- so ``max``
    reaches the slot on pi and the write folds it to ``xhigh``. If the startup
    resolution filtered against pi's advertised list BEFORE applying the same fold,
    it would drop the level the live push had just applied: the session would run
    the adapter default while the slot still recorded ``max``.
    """

    def test_a_stored_level_is_folded_then_filtered(self, tmp_path) -> None:
        """``max`` is not pi's, but it IS pi's ``xhigh``, and that is what applies."""
        provider = _pi_provider(tmp_path)
        provider._effort_per_model = {PI_MODEL: "max"}

        assert provider._resolve_effort() == "xhigh"

    def test_a_workspace_default_takes_the_same_order(self, tmp_path) -> None:
        """Defaults are coerced through the same fold, not only slot overrides."""
        provider = _pi_provider(tmp_path)
        provider._effort_defaults = {PI_MODEL: "max"}

        assert provider._resolve_effort() == "xhigh"

    def test_a_level_that_folds_to_nothing_advertised_is_still_dropped(self, tmp_path) -> None:
        """The fold is not a bypass: a level with no row and no advertised match goes."""
        provider = _pi_provider(tmp_path)
        provider._effort_per_model = {PI_MODEL: "colossal"}

        assert provider._resolve_effort() is None

    def test_the_resolution_hands_the_fold_in_rather_than_applying_it_after(self) -> None:
        """Order is the property, so it is pinned at the seam rather than inferred."""
        from kiro_crew.providers import acp as provider_module

        body = inspect.getsource(provider_module.AcpProvider._resolve_effort)

        assert "normalize=functools.partial(effort_config_option_value" in body

    def test_the_shared_resolver_folds_before_it_checks(self) -> None:
        """Directly, on the resolver: a fold applied after the check cannot pass this."""
        from kiro_crew.effort import resolve_effort_for_model

        fold = {"max": "xhigh"}.get

        def normalize(level: str) -> str:
            return fold(level) or level

        assert (
            resolve_effort_for_model(
                PI_MODEL,
                {PI_MODEL: "max"},
                levels=["low", "high", "xhigh"],
                normalize=normalize,
            )
            == "xhigh"
        )
        assert (
            resolve_effort_for_model(
                PI_MODEL,
                {PI_MODEL: "max"},
                levels=["low", "high", "xhigh"],
            )
            is None
        )


class TestThePushNeverNeedsARefusal:
    """``_is_config_value_rejection`` is never load-bearing for this harness.

    Its docstring makes a join precondition of the joining harness's own ``-32602``
    semantics, because the bare-code half of that reader rests on a per-adapter fact.
    The pi corpus records no config-value refusal, and one cannot be recorded here.

    So the precondition is answered by removing the dependency instead of measuring
    it: every value the push can emit for pi is a value pi's own capture advertises,
    so the adapter has nothing to refuse and the descent that reads a refusal is
    never entered. That is a property of the code, checked here over the whole
    vocabulary rather than one example.
    """

    def _emitted(self, level: str) -> list[str]:
        """Every value ``_set_effort_config_option`` could write for *level*, in order.

        Mirrors the push's own arithmetic: resolve the harness's spelling, then the
        descent from that point down Crew's ladder. The ladder is what a refusal
        would walk, so it counts as emittable even when the first write succeeds.
        """
        from kiro_crew.effort import EFFORT_LEVELS

        target = effort_config_option_value(ACP_BACKEND_PI, level)
        try:
            start = EFFORT_LEVELS.index(target)
        except ValueError:
            return [target]
        return list(reversed(EFFORT_LEVELS[: start + 1]))

    def test_every_crew_level_emits_only_values_pi_advertised(self) -> None:
        """Crew's whole ladder, including the two levels pi does not have."""
        from kiro_crew.effort import EFFORT_LEVELS

        advertised = {o["value"] for o in _pi_thought_level_option()["options"]}

        for level in EFFORT_LEVELS:
            emitted = self._emitted(level)
            assert emitted, level
            unknown = [value for value in emitted if value not in advertised]
            assert unknown == [], f"{level} would send pi {unknown}"

    def test_every_level_pi_advertised_emits_only_advertised_values(self) -> None:
        """And the harness's own vocabulary, which reaches the slot through the dropdown."""
        advertised = [o["value"] for o in _pi_thought_level_option()["options"]]

        for level in advertised:
            unknown = [value for value in self._emitted(level) if value not in advertised]
            assert unknown == [], f"{level} would send pi {unknown}"

    def test_the_descent_is_what_would_read_a_refusal(self) -> None:
        """The property above is only worth anything if it covers the ladder.

        Pinned so a push rewritten to walk some other sequence -- or to write the
        requested level raw -- fails here rather than quietly reintroducing the
        dependency on an unmeasured refusal shape.
        """
        from kiro_crew.providers import acp as provider_module

        body = inspect.getsource(provider_module.AcpProvider._set_effort_config_option)

        assert "EFFORT_LEVELS[: start + 1]" in body
        assert "_is_config_value_rejection(exc, effort_option)" in body
        assert "effort_config_option_value(self._client.backend, level)" in body


# What pi-acp 0.0.34 answered, driving pi 0.87.1, when ``session/set_config_option``
# asked for a ``provider/model`` id absent from pi's ``models.json``. Verbatim off a
# live session but for the model id: pi-acp wraps pi's own ``set_model`` failure in a
# bare -32603, so neither the code nor the option name says the VALUE was refused.
_PI_MODEL_NOT_FOUND = (
    "JSON-RPC error: {'code': -32603, 'message': 'Internal error', 'data': "
    "{'details': 'pi set_model failed: Model not found: ollama/gone-model'}}"
)


class TestAModelPiCannotSelectLeavesTheSessionOnItsDefault:
    """A stale model pin must not kill every pi session at startup.

    The pin can be one pi advertised earlier -- its list follows the operator's
    ``models.json`` and which provider keys are set -- so it goes stale on an edit
    Crew never sees. Every other harness on the config-option channel reads such a
    refusal as a refused VALUE and stays on its default; pi's refusal arrived in a
    shape the shared reader did not know, so it was re-raised as a protocol fault and
    the session never started.
    """

    def _client(self, tmp_path, monkeypatch, backend=ACP_BACKEND_PI):
        client = acp_client.AcpClient(work_dir=tmp_path, acp_backend=backend)
        client._session_id = "pi-sess"
        sent: list[tuple[str, str]] = []

        async def refuse(config_id: str, value: str) -> None:
            sent.append((config_id, value))
            raise acp_client.AcpError(_PI_MODEL_NOT_FOUND, code=-32603)

        monkeypatch.setattr(client, "set_config_option", refuse)
        return client, sent

    def test_an_inherited_pin_falls_back_to_the_default(self, tmp_path, monkeypatch):
        client, sent = self._client(tmp_path, monkeypatch)

        applied = asyncio.run(client._push_model_config_option("ollama/gone-model", strict=False))

        assert applied == ""
        assert sent and sent[0] == ("model", "ollama/gone-model")

    def test_an_explicit_pick_names_the_model(self, tmp_path, monkeypatch):
        client, _sent = self._client(tmp_path, monkeypatch)

        with pytest.raises(acp_client.AcpModelUnavailable):
            asyncio.run(client._push_model_config_option("ollama/gone-model", strict=True))

    def test_the_phrase_is_read_only_for_pi(self, tmp_path, monkeypatch):
        """Another harness's -32603 stays a protocol fault, whatever its text says."""
        client, _sent = self._client(tmp_path, monkeypatch, backend=ACP_BACKEND_OPENCODE)

        with pytest.raises(acp_client.AcpError) as raised:
            asyncio.run(client._push_model_config_option("ollama/gone-model", strict=False))
        assert not isinstance(raised.value, acp_client.AcpModelUnavailable)

    def test_the_phrase_is_read_only_for_the_model_option(self):
        exc = acp_client.AcpError(_PI_MODEL_NOT_FOUND, code=-32603)

        assert acp_client._is_config_value_rejection(exc, "model", ACP_BACKEND_PI)
        assert not acp_client._is_config_value_rejection(exc, "thought_level", ACP_BACKEND_PI)
        assert not acp_client._is_config_value_rejection(exc, "model")


def test_a_cancel_answers_the_open_gate_dialog_as_cancelled(tmp_path):
    """ACP: after ``session/cancel`` the client answers every open permission request.

    pi-acp keeps a cancelled turn open while the gate extension's dialog is unanswered,
    so a Stop from a surface that did not reject the open approval first waited out the
    whole ack budget and hard-killed the process. The frame is the live capture's own.
    """
    from unittest.mock import AsyncMock, MagicMock

    path = ROOT / "test" / "fixtures" / "acp_frames" / "pi" / "permission-request-live.jsonl"
    frame = next(
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if '"session/request_permission"' in line
    )
    client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_PI)
    proc = MagicMock()
    proc.returncode = None
    proc.stdin.drain = AsyncMock()
    client._process = proc
    client._session_id = frame["params"]["sessionId"]
    client._build_permission_event(
        JsonRpcMessage(id=frame["id"], method=frame["method"], params=frame["params"])
    )

    asyncio.run(client.cancel_session())

    written = [
        json.loads(line)
        for call in proc.stdin.write.call_args_list
        for line in call.args[0].decode().splitlines()
    ]
    assert [w.get("method") for w in written] == ["session/cancel", None]
    assert written[1] == {
        "jsonrpc": "2.0",
        "id": frame["id"],
        "result": {"outcome": {"outcome": "cancelled"}},
    }


# ── A signed-out pi: the declared message, and no retry ──

#: The ``session/new`` error pi-acp 0.0.34 returns with an empty pi home and no
#: key, copied off the live wire (``authMethods`` trimmed to its id).
_PI_SIGNED_OUT_ERROR = {
    "code": -32000,
    "message": "Authentication required: Configure an API key or log in with an OAuth provider.",
    "data": {"authMethods": [{"id": "pi_terminal_login", "type": "terminal"}]},
}


def _signed_out_startup_client(backend: str, spawns: list[int]) -> AcpClient:
    """A client whose every ``session/new`` answers pi's live signed-out error.

    Shaped the way ``_send_request`` raises it, so the startup ladder sees the
    same ``AcpError`` text a real pi-acp child produces.
    """
    from unittest.mock import AsyncMock, MagicMock

    client = AcpClient(acp_backend=backend)
    client._process = None
    client._session_id = None
    client._kill_process = AsyncMock()
    client._cleanup_failed_live_spawn = AsyncMock()
    client._snapshot_process_tree = AsyncMock()

    def _reset():
        # The real reset drops the process handle, which is what makes the next
        # pass spawn again; without it a retry would silently be a no-op.
        client._process = None
        client._session_id = None

    client._reset_state = _reset

    async def _spawn():
        spawns.append(1)
        client._process = MagicMock()
        client._process.returncode = None

    async def _session_new():
        raise acp_client.AcpError(f"JSON-RPC error: {_PI_SIGNED_OUT_ERROR}")

    client._spawn = _spawn
    client._initialize_session = _session_new
    return client


def test_a_signed_out_pi_names_the_fix_and_is_not_retried() -> None:
    from kiro_crew.agent_sdk import host_auth

    spawns: list[int] = []
    client = _signed_out_startup_client(ACP_BACKEND_PI, spawns)
    with pytest.raises(acp_client.AcpError) as info:
        asyncio.run(client.ensure_ready())
    assert str(info.value) == host_auth.signed_out_message(ACP_BACKEND_PI)
    # One spawn: a fresh pi reads the same empty credential file.
    assert spawns == [1]
    assert info.value.transient is False
    # Not the Kiro sign-in type: the dashboard would offer the Kiro sign-in card,
    # which signs in the wrong thing for pi.
    assert not isinstance(info.value, acp_client.AcpAuthRequired)
    assert info.value.auth_required is False


def test_the_pi_phrase_does_not_classify_another_harness() -> None:
    """Scoped per harness: kiro-cli meeting the same words keeps its retry."""
    spawns: list[int] = []
    client = _signed_out_startup_client(ACP_BACKEND_KIRO, spawns)
    with pytest.raises(acp_client.AcpError):
        asyncio.run(client.ensure_ready())
    assert spawns == [1, 1]
