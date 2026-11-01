"""The DeepSeek Harness's reasoning effort: the option id read from the table rather than spelled,
and the effort control answered by the option this harness advertises.
"""

from __future__ import annotations

import inspect
import pathlib
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest
from acp_deepseek_backend_helpers import (  # noqa: F401 -- fixtures are used by name
    _client,
    _fixture_frames,
    _pinned_kiro_cli_version,
)

from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.session_handle import models_from_config_options
from kiro_crew.acp_backends import ACP_BACKEND_CLAUDE, ACP_BACKEND_DEEPSEEK

# ── The effort option id, read rather than spelled ───────────────────────────


def test_the_effort_levels_are_parsed_under_this_harness_own_option_id(tmp_path) -> None:
    """A hard-coded ``effort`` here reads as "no levels offered" rather than a miss."""
    client = _client(tmp_path, ACP_BACKEND_DEEPSEEK)
    client._acp_config_options = [
        {
            "id": "reasoning_effort",
            "options": [{"value": "off"}, {"value": "low"}, {"value": "high"}],
        }
    ]

    assert client.get_valid_effort_levels() == ["off", "low", "high"]


def test_another_harness_still_parses_the_default_option_id(tmp_path) -> None:
    """The other arm of the table, so the read is keyed and not simply renamed."""
    client = _client(tmp_path, ACP_BACKEND_CLAUDE)
    client._acp_config_options = [{"id": "effort", "options": [{"value": "high"}]}]

    assert client.get_valid_effort_levels() == ["high"]


def test_the_levels_parser_reads_the_table(tmp_path) -> None:
    """The id comes from ``effort_config_option_id``, not from a literal at this site."""
    body = inspect.getsource(AcpClient.get_valid_effort_levels)
    assert "effort_config_option_id(self.backend)" in body
    assert '== "effort"' not in body


#: How many sites in ``AcpProvider`` resolve the effort option id per backend, and
#: what each one is. An exact count rather than a floor: a literal at any of them
#: makes the whole channel a silent no-op for a harness that spells the option
#: differently, and a count that only grows cannot tell a new reader from a
#: hard-coded one that slipped in beside a correct one.
_PROVIDER_EFFORT_ID_SITES = (
    "the advertised-option CHECK in ``_set_effort_config_option``",
    "the skip-if-unadvertised check in ``change_effort``",
    "the capability answer in ``supports_effort``, for a harness whose ADVERTISED "
    "option decides that a level applies at all "
    "(``ACP_BACKENDS_EFFORT_FROM_ADVERTISED_OPTION``)",
    "the build check in ``supports_effort``, for a harness that takes a level only "
    "through its advertised option (``ACP_BACKENDS_MODEL_EFFORT_PAIR_IDS``)",
)


def test_no_site_that_asks_about_effort_spells_the_id_itself() -> None:
    """Membership in the effort set is worth nothing if a consumer hard-codes the id.

    The levels parser is one site; ``_PROVIDER_EFFORT_ID_SITES`` names the ones in
    ``providers/acp_model_effort.py``. A literal at any of them makes the whole channel a silent
    no-op for this harness: the check reports the option unsupported, the push never
    happens, and the dropdown still offers levels that can never be applied.
    """
    from kiro_crew.providers import acp as provider_module

    # The class and the owner modules its members live in.
    body = inspect.getsource(provider_module.AcpProvider) + "".join(
        inspect.getsource(sys.modules[name]) for name in provider_module._ACP_PROVIDER_OWNERS
    )
    assert 'supports_config_option("effort")' not in body
    assert 'set_config_option("effort"' not in body
    assert body.count("effort_config_option_id(self._client.backend)") == len(
        _PROVIDER_EFFORT_ID_SITES
    )


@pytest.mark.asyncio
async def test_the_effort_push_uses_this_harness_own_option_id(tmp_path) -> None:
    """The push reaches the wire under the id the harness advertised."""
    from kiro_crew.providers.acp import AcpProvider

    provider = AcpProvider.__new__(AcpProvider)
    provider._client = MagicMock()
    provider._client.backend = ACP_BACKEND_DEEPSEEK
    provider._client.supports_config_option = MagicMock(return_value=True)
    provider._client.get_valid_effort_levels = MagicMock(return_value=["high"])
    provider._client.set_config_option = AsyncMock()
    provider._client._model = "deepseek-v4-flash"

    await provider._set_effort_config_option("high")

    asked = provider._client.supports_config_option.call_args[0][0]
    pushed = provider._client.set_config_option.await_args[0][0]
    assert asked == "reasoning_effort"
    assert pushed == "reasoning_effort"


def test_a_grouped_model_select_is_flattened_before_the_value_filter(tmp_path) -> None:
    """This harness groups its model options, and the capture is the only vocabulary.

    Its ``session/new`` nests the real choices under a ``{"group": …, "options": […]}``
    wrapper that carries no ``value`` of its own. A filter keeping only entries with a
    ``value`` empties on that shape, and an empty list reads as "advertised no
    models" -- which for a harness whose ids exist nowhere else means no model can be
    offered or resolved at all.
    """
    client = _client(tmp_path, ACP_BACKEND_DEEPSEEK)
    grouped = {
        "configOptions": [
            {
                "id": "model",
                "type": "select",
                "currentValue": '["deepseek-official","deepseek-v4-flash"]',
                "options": [
                    {
                        "group": "deepseek-official",
                        "name": "DeepSeek",
                        "options": [
                            {
                                "value": '["deepseek-official","deepseek-v4-flash"]',
                                "name": "DeepSeek-V4-Flash",
                            }
                        ],
                    }
                ],
            }
        ]
    }

    envelope = models_from_config_options(grouped, client.backend)

    assert envelope is not None
    assert [m["modelId"] for m in envelope["availableModels"]] == [
        '["deepseek-official","deepseek-v4-flash"]'
    ]
    assert envelope["currentModelId"] == '["deepseek-official","deepseek-v4-flash"]'


def test_a_flat_model_select_is_still_read_unchanged(tmp_path) -> None:
    """The other arm: a harness that does not group must be unaffected by the flatten."""
    client = _client(tmp_path, ACP_BACKEND_CLAUDE)
    flat = {
        "configOptions": [
            {
                "id": "model",
                "type": "select",
                "currentValue": "sonnet",
                "options": [{"value": "sonnet", "name": "Sonnet"}],
            }
        ]
    }

    envelope = models_from_config_options(flat, client.backend)

    assert envelope is not None
    assert [m["modelId"] for m in envelope["availableModels"]] == ["sonnet"]


@pytest.mark.parametrize(
    "malformed",
    [
        pytest.param(123, id="nested-options-is-a-number"),
        pytest.param(True, id="nested-options-is-a-bool"),
        pytest.param("abc", id="nested-options-is-a-string"),
        pytest.param({"a": {"value": "x"}}, id="nested-options-is-a-mapping"),
        pytest.param([None, 7, "x"], id="nested-options-holds-non-objects"),
    ],
)
def test_a_malformed_group_degrades_instead_of_failing_the_session(tmp_path, malformed) -> None:
    """Every level of this payload comes off the wire, so none of it may be trusted.

    A truthy non-iterable in a group's ``options`` raised ``TypeError`` from inside
    ``session/new`` handling, so a malformed or hostile agent response failed session
    initialization rather than degrading to "this harness advertised no models".
    Parametrized over the shapes a wire payload can actually take, because the outer
    list and the nested one need the SAME narrowing and only the outer one had it.
    """
    client = _client(tmp_path, ACP_BACKEND_DEEPSEEK)
    payload = {
        "configOptions": [
            {
                "id": "model",
                "type": "select",
                "options": [{"group": "g", "name": "G", "options": malformed}],
            }
        ]
    }

    assert models_from_config_options(payload, client.backend) is None


def test_a_malformed_group_beside_a_good_one_keeps_the_good_one(tmp_path) -> None:
    """Degrading must not mean discarding: one bad group cannot cost the whole list."""
    client = _client(tmp_path, ACP_BACKEND_DEEPSEEK)
    payload = {
        "configOptions": [
            {
                "id": "model",
                "type": "select",
                "options": [
                    {"group": "broken", "options": 123},
                    {"group": "good", "options": [{"value": "real-id", "name": "Real"}]},
                ],
            }
        ]
    }

    envelope = models_from_config_options(payload, client.backend)

    assert envelope is not None
    assert [m["modelId"] for m in envelope["availableModels"]] == ["real-id"]


def test_the_committed_fixture_is_the_shape_the_capture_must_read(tmp_path) -> None:
    """Read the real captured frame, so the parser is pinned against the wire itself.

    A hand-written grouped payload could drift from what the harness sends; the
    fixture cannot, because it IS what the harness sent.
    """
    import json

    fixture = (
        pathlib.Path(__file__).parent
        / "fixtures"
        / "acp_frames"
        / "deepseek"
        / "handshake-live.jsonl"
    )
    session_new = next(
        frame["result"]
        for frame in (json.loads(line) for line in fixture.read_text().splitlines()[1:])
        if isinstance(frame.get("result"), dict) and "configOptions" in frame["result"]
    )

    client = _client(tmp_path, ACP_BACKEND_DEEPSEEK)
    envelope = models_from_config_options(session_new, client.backend)

    assert envelope is not None
    assert envelope["availableModels"], "the captured select must yield at least one model"


# ── The effort control, answered by the option this harness advertises ───────

#: The model id the committed handshake recorded: one the operator's own catalog
#: serves and Crew's model registry does not carry.
_DEEPSEEK_MODEL = "deepseek-v4-flash"

#: The levels the base comment in ``agent_sdk/backends_models.py`` names for this
#: harness's ``reasoning_effort`` select. The committed handshake prunes that
#: select to ONE level, so the full list is stated here once rather than read from
#: the capture -- and nothing below depends on it being exhaustive, because the
#: control follows whatever a session actually advertises.
_DEEPSEEK_LEVELS = ["off", "low", "high", "max"]


def _deepseek_effort_option(levels: list[str] | None = None) -> dict:
    """The ``reasoning_effort`` select off the committed ``session/new`` result.

    *levels* replaces its options while keeping the captured shape, so a test that
    needs the full vocabulary still runs against the harness's own option record.
    """
    for frame in _fixture_frames("handshake-live.jsonl"):
        for option in (frame.get("result") or {}).get("configOptions") or []:
            if option.get("id") == "reasoning_effort":
                option = dict(option)
                if levels is not None:
                    option["options"] = [{"value": v, "name": v.title()} for v in levels]
                return option
    raise AssertionError("no reasoning_effort option in the deepseek handshake capture")


def _deepseek_provider(tmp_path, *, levels: list[str] | None = _DEEPSEEK_LEVELS, options=None):
    """An ``AcpProvider`` on deepseek with a session's config options already stored."""
    from kiro_crew.providers.acp import AcpProvider

    provider = AcpProvider(
        acp_backend=ACP_BACKEND_DEEPSEEK, work_dir=tmp_path, model=_DEEPSEEK_MODEL
    )
    provider._client._acp_config_options = (
        [_deepseek_effort_option(levels)] if options is None else options
    )
    provider._client._model = _DEEPSEEK_MODEL
    return provider


class TestTheDeepseekEffortControlIsOffered:
    """The registry's name test answers "no effort" for every ordinary session here.

    ``model_supports_effort`` recognises the Claude and GPT families only, and this
    harness serves the operator's own model ids -- so all four effort verbs declined
    while the harness advertised the option on every ``session/new``.
    """

    def test_the_registry_does_not_recognise_the_model_this_harness_serves(self) -> None:
        """The premise, stated so the tests below cannot pass for the wrong reason."""
        from kiro_crew.effort import model_supports_effort

        assert model_supports_effort(_DEEPSEEK_MODEL) is False

    def test_the_advertised_option_answers_for_this_harness(self) -> None:
        from kiro_crew.agent_sdk.backends import ACP_BACKENDS_EFFORT_FROM_ADVERTISED_OPTION

        assert ACP_BACKEND_DEEPSEEK in ACP_BACKENDS_EFFORT_FROM_ADVERTISED_OPTION

    def test_the_committed_capture_carries_the_select(self) -> None:
        """Membership rests on the capture, not on prose."""
        option = _deepseek_effort_option()

        assert option["type"] == "select"
        assert option["category"] == "thought_level"
        assert [o["value"] for o in option["options"]] == ["high"]

    def test_the_control_is_offered_on_the_ordinary_session(self, tmp_path) -> None:
        provider = _deepseek_provider(tmp_path)

        assert provider.supports_effort() is True
        assert provider.get_valid_effort_levels() == _DEEPSEEK_LEVELS

    def test_the_control_follows_what_the_session_advertised(self, tmp_path) -> None:
        """The committed capture's one level is all a session advertising it offers."""
        provider = _deepseek_provider(tmp_path, levels=None)

        assert provider.supports_effort() is True
        assert provider.get_valid_effort_levels() == ["high"]
        assert provider.accepts_effort_level("high") is True
        assert provider.accepts_effort_level("max") is False

    def test_a_session_advertising_no_option_is_still_refused(self, tmp_path) -> None:
        provider = _deepseek_provider(tmp_path, options=[{"id": "model", "options": []}])

        assert provider.supports_effort() is False


class TestTheUnmappedLevelsAreHidden:
    """``medium`` and ``xhigh`` have no counterpart here, so they are hidden.

    Not folded onto a neighbour (the session would run a level the user did not
    pick while the UI reports the one they did), and not written unmapped (the
    harness never advertised them).
    """

    def test_no_fold_row_exists_for_this_harness(self) -> None:
        from kiro_crew.agent_sdk.backends import effort_config_option_value
        from kiro_crew.effort import EFFORT_LEVELS

        for level in EFFORT_LEVELS:
            assert effort_config_option_value(ACP_BACKEND_DEEPSEEK, level) == level

    @pytest.mark.parametrize("level", ["medium", "xhigh"])
    def test_an_unadvertised_crew_level_is_not_accepted(self, tmp_path, level) -> None:
        provider = _deepseek_provider(tmp_path)

        assert level not in provider.get_valid_effort_levels()
        assert provider.accepts_effort_level(level) is False

    @pytest.mark.parametrize("level", _DEEPSEEK_LEVELS)
    def test_every_advertised_level_is_accepted(self, tmp_path, level) -> None:
        assert _deepseek_provider(tmp_path).accepts_effort_level(level) is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize("level", ["medium", "xhigh"])
    async def test_change_effort_refuses_a_hidden_level_before_it_persists(
        self, tmp_path, level
    ) -> None:
        provider = _deepseek_provider(tmp_path)
        provider._client.set_config_option = AsyncMock()

        with pytest.raises(ValueError, match="not offered"):
            await provider.change_effort(level)

        assert _DEEPSEEK_MODEL not in provider._effort_per_model
        provider._client.set_config_option.assert_not_awaited()

    @pytest.mark.parametrize("level", ["medium", "xhigh"])
    def test_a_stored_hidden_level_is_not_applied_at_startup(self, tmp_path, level) -> None:
        provider = _deepseek_provider(tmp_path)
        provider._effort_per_model = {_DEEPSEEK_MODEL: level}

        assert provider._resolve_effort() is None

    def test_other_harnesses_accept_every_level(self, tmp_path) -> None:
        """Only an advertised-option harness can hide a level."""
        from kiro_crew.providers.acp import AcpProvider

        provider = AcpProvider(acp_backend=ACP_BACKEND_CLAUDE, work_dir=tmp_path)
        provider._client._acp_config_options = [_deepseek_effort_option(["low"])]

        assert provider.accepts_effort_level("max") is True


class TestADeepseekLevelSurvivesAColdStart:
    """The factory carries the level; the advertised list judges it on ``session/new``."""

    def _factory_kwargs(self, level: str) -> dict:
        from unittest.mock import patch

        from kiro_crew.config.loader import KiroCrewConfig

        cfg = KiroCrewConfig()
        cfg.agent.provider = "acp"
        cfg.agent.acp_backend = ACP_BACKEND_DEEPSEEK
        with patch("kiro_crew.providers.acp.AcpProvider") as mock_provider:
            mock_provider.return_value = MagicMock()
            factory = cfg.create_provider_factory()
            factory(
                session_key="dashboard:1",
                model_override=_DEEPSEEK_MODEL,
                reasoning_effort_override=level,
            )
            assert mock_provider.called, "factory did not construct AcpProvider"
            return mock_provider.call_args.kwargs

    @pytest.mark.parametrize("level", ["low", "high", "max", "off"])
    def test_a_stored_advertised_level_is_what_the_session_applies(self, tmp_path, level) -> None:
        carried = self._factory_kwargs(level)
        assert carried.get("effort_per_model") == {_DEEPSEEK_MODEL: level}

        provider = _deepseek_provider(tmp_path)
        provider._effort_per_model = dict(carried["effort_per_model"])

        assert provider._resolve_effort() == level

    def test_a_workspace_default_takes_the_same_route(self, tmp_path) -> None:
        provider = _deepseek_provider(tmp_path)
        provider._effort_defaults = {_DEEPSEEK_MODEL: "max"}

        assert provider._resolve_effort() == "max"


class TestTheDeepseekPushNeverNeedsARefusal:
    """Every value the push can emit for this harness is one it advertised.

    ``_is_config_value_rejection`` makes the joining harness's ``-32602`` semantics
    a precondition, and no deepseek config-value refusal is recorded. The descent
    is therefore restricted to advertised rungs, and a target outside them is not
    written, so the adapter is never asked for a value it did not offer.
    """

    async def _emitted(self, tmp_path, level: str, *, refuse: bool) -> list[str]:
        """Every value written for *level*; with *refuse*, each write is refused."""
        from kiro_crew.acp.client import AcpError

        provider = _deepseek_provider(tmp_path)
        written: list[str] = []

        async def write(option_id: str, value: str) -> None:
            assert option_id == "reasoning_effort"
            written.append(value)
            if refuse:
                raise AcpError("Invalid value for config option reasoning_effort: " + value)

        provider._client.set_config_option = write
        try:
            await provider._set_effort_config_option(level)
        except AcpError:
            pass
        return written

    @pytest.mark.asyncio
    @pytest.mark.parametrize("refuse", [False, True])
    async def test_every_crew_and_advertised_level_emits_only_advertised_values(
        self, tmp_path, refuse
    ) -> None:
        from kiro_crew.effort import EFFORT_LEVELS

        for level in dict.fromkeys([*EFFORT_LEVELS, *_DEEPSEEK_LEVELS]):
            emitted = await self._emitted(tmp_path, level, refuse=refuse)
            unknown = [value for value in emitted if value not in _DEEPSEEK_LEVELS]
            assert unknown == [], f"{level} would send deepseek {unknown}"

    @pytest.mark.asyncio
    async def test_the_descent_skips_a_rung_the_session_did_not_advertise(self, tmp_path) -> None:
        """``max`` refused walks max, high, low -- never xhigh or medium."""
        assert await self._emitted(tmp_path, "max", refuse=True) == ["max", "high", "low"]

    @pytest.mark.asyncio
    async def test_an_advertised_level_is_written_once(self, tmp_path) -> None:
        assert await self._emitted(tmp_path, "high", refuse=False) == ["high"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("level", ["medium", "xhigh"])
    async def test_a_hidden_level_writes_nothing(self, tmp_path, level) -> None:
        assert await self._emitted(tmp_path, level, refuse=False) == []


@pytest.mark.asyncio
async def test_a_model_switch_does_not_reset_over_a_hidden_level(tmp_path) -> None:
    """The in-place switch re-applies the slot level; a hidden one is a no-op there."""
    provider = _deepseek_provider(tmp_path)
    provider._client.set_config_option = AsyncMock()

    assert await provider.reapply_live_effort("medium") is True
    provider._client.set_config_option.assert_not_awaited()
