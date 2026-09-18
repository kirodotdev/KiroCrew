"""``monitoring.prefer_structured_arming`` -- which monitoring path is the default.

What this key can be tested on, and what it cannot
--------------------------------------------------
Nothing in the gateway CHOOSES between ``monitor_start`` and ``monitor_watch``.
The choice is made by the model reading the two tool descriptions, so the only
effect this key has -- and therefore the only effect a test can measure -- is
the TEXT those descriptions carry. Every assertion here is about that text. None
of them claims the agent then picks the named path, because no test in this
repository could establish that, and a suite that implied otherwise would be
read as covering a behaviour change it never measured.

Two failure modes shaped the assertions:

* A test that only checked the ON position could be satisfied by hardcoding the
  structured text, and a test that only checked OFF could be satisfied by a
  WRAPPING mutation (``if False and prefer_structured``) that leaves both
  strings in the module and never reads the key. So both positions assert the
  presence of their own text AND the absence of the other one's.
* A value captured at import would look identical to a live read on a single
  build, and would strand an operator whose Settings change appears to do
  nothing. ``test_the_preference_is_read_on_every_build`` flips the value inside
  one process and reads the descriptors again.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.config.schema import SCHEMA_REGISTRY, requires_restart
from kiro_crew.config.sections import MonitoringConfig
from kiro_crew.mcp_tools import control

_KEY = "monitoring.prefer_structured_arming"

#: Substrings that identify each position. The off mark is the CONDITION it puts
#: on the structured path; the on mark is the default it declares instead.
_CONDITIONAL_MARK = "only when the objective is fully determined by typed provider facts"
_BY_DEFAULT_MARK = "arms monitor_watch by default"


def _pin(monkeypatch: pytest.MonkeyPatch, *, on: bool) -> None:
    """Pin the resolved config the descriptor builder reads."""
    pinned = KiroCrewConfig(monitoring=MonitoringConfig(prefer_structured_arming=on))
    monkeypatch.setattr(control.KiroCrewConfig, "load", classmethod(lambda cls: pinned))


def _descriptions() -> tuple[str, str]:
    """``(monitor_start, monitor_watch)`` descriptions from one build."""
    built = {item["name"]: item for item in control.schemas()}
    return built["monitor_start"]["description"], built["monitor_watch"]["description"]


class TestTheTwoPositionsSayDifferentThings:
    """Each position makes its own positive claim, so the pair is measurable."""

    def test_off_names_the_prompt_loop_and_never_the_structured_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _pin(monkeypatch, on=False)
        start, watch = _descriptions()

        assert _CONDITIONAL_MARK in start
        assert _BY_DEFAULT_MARK not in start
        assert control._WATCH_STEER_STRUCTURED_DEFAULT.strip() not in watch

    def test_on_names_the_structured_default_and_drops_the_legacy_steer(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _pin(monkeypatch, on=True)
        start, watch = _descriptions()

        assert _BY_DEFAULT_MARK in start
        assert _CONDITIONAL_MARK not in start, (
            "the on position still carries the exception-shaped steer, so both "
            "sentences ship at once and the descriptor names two defaults"
        )
        assert control._WATCH_STEER_STRUCTURED_DEFAULT.strip() in watch, (
            "the preference is stated only on the tool it points AWAY from; "
            "monitor_watch's own description never says it is the default"
        )

    def test_the_two_steers_are_not_the_same_sentence(self) -> None:
        """Equal constants would make every presence check above pass trivially."""
        assert (
            control._ARMING_STEER_STRUCTURED_ON_CONDITION
            != control._ARMING_STEER_STRUCTURED_BY_DEFAULT
        )

    def test_only_the_on_position_names_the_prompt_loop_an_exception(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The burden, which is the only thing that actually moves.

        Two different strings are not enough to show the key does anything: both
        positions route advisory evidence to the prompt loop, so a pair that
        merely reworded the same routing would satisfy every other test here
        while the preference stayed semantically dead. What separates them is
        which side needs a reason. Off states a CONDITION on the structured path
        and never calls the loop an exception; on declares the structured path
        the default and names the loop the exception.
        """
        _pin(monkeypatch, on=False)
        off_start, _ = _descriptions()
        _pin(monkeypatch, on=True)
        on_start, _ = _descriptions()

        assert "the exception" in on_start
        assert "the exception" not in off_start, (
            "the off position already frames the prompt loop as the exception, so "
            "both positions put the burden on the same side and the key moves "
            "nothing an agent could act on"
        )
        assert "by default" in on_start
        assert _CONDITIONAL_MARK in off_start and _CONDITIONAL_MARK not in on_start


class TestTheReadIsLiveAndFailsToTheShippedPosition:
    def test_the_preference_is_read_on_every_build(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Off, on, off -- inside one process, with no reimport between them.

        This is the assertion an operator depends on: ``build_tool_list`` rebuilds
        descriptors per call, so a Settings change needs no gateway restart. A
        value captured at import time passes the first read and fails the second.
        """
        seen = []
        for position in (False, True, False):
            _pin(monkeypatch, on=position)
            start, _ = _descriptions()
            seen.append(_BY_DEFAULT_MARK in start)

        assert seen == [False, True, False]

    def test_a_config_read_error_resolves_to_the_shipped_off_position(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Off is what ships, so an unreadable config must not re-point arming."""

        def _boom(cls: type) -> KiroCrewConfig:
            raise OSError("config.json is unreadable")

        monkeypatch.setattr(control.KiroCrewConfig, "load", classmethod(_boom))
        start, watch = _descriptions()

        assert _CONDITIONAL_MARK in start
        assert _BY_DEFAULT_MARK not in start
        assert control._WATCH_STEER_STRUCTURED_DEFAULT.strip() not in watch


class TestTheKeyChangesTextAndNothingElse:
    """The scope claim, pinned: no tool is withdrawn and no argument moves."""

    def test_both_monitor_tools_are_offered_in_both_positions(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for position in (False, True):
            _pin(monkeypatch, on=position)
            offered = {item["name"] for item in control.schemas()}
            assert {"monitor_start", "monitor_watch"} <= offered, (
                f"a monitoring tool disappeared with {_KEY}={position}; this key "
                "expresses a preference and must never withdraw a tool"
            )

    def test_the_input_schemas_are_identical_in_both_positions(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only prose moves. An argument that changed would be a real refusal."""
        captured = {}
        for position in (False, True):
            _pin(monkeypatch, on=position)
            captured[position] = {
                item["name"]: json.dumps(item.get("inputSchema"), sort_keys=True)
                for item in control.schemas()
            }

        assert captured[False] == captured[True]


class TestAnInstallThatPredatesTheKey:
    def test_a_stored_document_with_no_monitoring_section_resolves_to_off(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Adding a key needs nothing written; the absent section IS the default.

        The hazard this sidesteps belongs to a SHIPPED DEFAULT that changes,
        because ``config.json`` materializes every key and the stored value then
        outranks the new default forever. A key that did not exist when the
        document was written has no stored value to outrank it, so the miss
        resolves to the dataclass default -- the off position that ships.
        """
        stored = tmp_path / "config.json"
        stored.write_text(json.dumps({"agent": {}, "dashboard": {}}), encoding="utf-8")
        monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: stored)

        resolved = KiroCrewConfig.load()

        assert resolved.monitoring.prefer_structured_arming is False

    def test_a_stored_true_is_honoured(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stored = tmp_path / "config.json"
        stored.write_text(
            json.dumps({"monitoring": {"prefer_structured_arming": True}}), encoding="utf-8"
        )
        monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: stored)

        assert KiroCrewConfig.load().monitoring.prefer_structured_arming is True

    def test_an_operator_choice_survives_a_save_and_reload(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``to_dict`` has to carry the section, or a save erases the choice.

        ``config.json`` is written from ``to_dict``, so a section the dataclass
        holds but that serializer omits is dropped on the next write: the
        operator turns the key on, some later save round-trips the document, and
        their setting is gone with nothing reporting it. The field-presence gate
        in ``test_config_roundtrip`` catches the omission; this catches what the
        omission COSTS, which is the half an operator would notice.
        """
        stored = tmp_path / "config.json"
        monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: stored)
        chosen = KiroCrewConfig(monitoring=MonitoringConfig(prefer_structured_arming=True))

        stored.write_text(json.dumps(chosen.to_dict()), encoding="utf-8")

        assert KiroCrewConfig.load().monitoring.prefer_structured_arming is True


class TestTheOperatorFacingClaimsSurviveMeasurement:
    """Three sentences an operator acts on, measured against the code.

    A help string can assert the opposite of the code and go green through every
    other test here, because none of them read the prose. These do.
    """

    def _entry(self) -> object:
        for entry in SCHEMA_REGISTRY:
            if entry.path == _KEY:
                return entry
        raise AssertionError(f"{_KEY} missing from SCHEMA_REGISTRY")

    def test_the_shipped_default_is_the_off_position(self) -> None:
        assert self._entry().default_value is False
        assert MonitoringConfig().prefer_structured_arming is False

    def test_the_key_is_declared_hot_because_the_read_is_per_build(self) -> None:
        """The help says no restart is needed; the schema has to agree.

        A ``restart=True`` entry renders a Restart-required badge in the
        dashboard, so the two would contradict each other on the same screen.
        """
        assert requires_restart(_KEY) is False
        assert "no gateway restart is needed" in self._entry().help

    def test_the_help_states_what_the_key_cannot_do(self) -> None:
        """The non-guarantee is the half a reviewer would otherwise assume away."""
        help_text = " ".join(self._entry().help.split())

        assert "does not refuse either tool" in help_text
        assert "cannot guarantee which one the agent picks" in help_text
        assert "armable with it off" in help_text

    def test_the_help_does_not_sell_the_key_as_a_swap_of_two_defaults(self) -> None:
        """Both positions route advisory evidence the same way, so say so.

        Copy promising an operator that off makes the prompt loop the default
        would be false: the shipped off wording sends a typed-decidable
        supported pull request to ``monitor_watch`` already. What the key moves
        is which side needs a reason, and the help has to name that instead.
        """
        help_text = " ".join(self._entry().help.split())

        assert "moves the burden rather than swapping two defaults" in help_text
        assert "Both positions send evidence the typed provider cannot observe" in help_text

    def test_the_help_names_what_the_structured_path_cannot_observe(self) -> None:
        """The advisory half is what an operator is giving up, so it is named."""
        help_text = " ".join(self._entry().help.split())

        assert "not generic comments or advisory review findings" in help_text
        assert "still needs the prompt loop" in help_text

    def test_no_operator_facing_copy_claims_an_enforcement(self) -> None:
        """The falsified direction, in the spellings that would ship it.

        ``test_both_monitor_tools_are_offered_in_both_positions`` and
        ``test_the_input_schemas_are_identical_in_both_positions`` prove the key
        withdraws nothing. Copy telling an operator it blocks, requires or
        disables a path contradicts both.
        """
        overclaims = ("blocks monitor_start", "disables monitor_start", "requires monitor_watch")
        sites = {
            "src/kiro_crew/config/sections.py (schema help)": self._entry().help,
            "config-baseline.json": _baseline_help(_KEY),
            str(_SPEC): _spec_paragraph_about("prefer_structured_arming"),
        }
        for name, text in sites.items():
            flat = " ".join(text.split()).lower()
            for phrase in overclaims:
                assert phrase not in flat, (
                    f"{name} tells an operator the key {phrase!r}; it only "
                    "rewrites descriptor text and refuses nothing"
                )

    def test_the_owning_spec_states_the_non_guarantee_too(self) -> None:
        """The spec is where a reviewer looks for the scope, so it carries it."""
        flat = " ".join(_spec_paragraph_about("prefer_structured_arming").split())

        assert "refuses neither tool" in flat
        assert "cannot guarantee which path the agent then" in flat


#: The owning spec for the monitoring engine. A claim about this key lives in
#: exactly one of its paragraphs, which is what lets the assertion above be
#: scoped rather than satisfied by a word the rest of a long file happens to
#: carry.
_SPEC = Path(__file__).resolve().parent.parent / "docs/system-specs/modules/monitor-architecture.md"


class TestTheReversibilityClaimIsMeasuredNotAsserted:
    """The help says the key reverses but an armed monitor does not.

    That second half is a claim about ``autonudge``, not about this key, and a
    help string can state its opposite and pass every other test here. So it is
    measured against the predicate that decides it, the way
    ``TestSpawnQueueWaitHelpMatchesTheDaemon`` measures its own key's sentence.
    """

    @staticmethod
    def _stopped_row(outcome: object) -> object:
        from kiro_crew.autonudge import NudgeLoop
        from kiro_crew.monitoring.models import MonitorState

        state = MonitorState(
            kind="github_pull_request",
            target="https://github.com/acme/widgets/pull/1",
            objective="review_ready",
            created_ts=1.0,
        )
        state.outcome = outcome  # type: ignore[assignment]
        state.stopped_reason = "stopped"
        loop = NudgeLoop(id="m1", slot_key="dashboard:chat-1", message="")
        loop.active = False
        loop.monitor = state
        return loop

    def test_a_user_stop_is_retained_evidence_and_refuses_a_re_arm(self) -> None:
        from kiro_crew.autonudge import _stopped_row_is_replaceable
        from kiro_crew.monitoring.models import MonitorOutcome

        assert not _stopped_row_is_replaceable(self._stopped_row(MonitorOutcome.USER_STOP))

    def test_only_the_system_imposed_outcomes_are_replaceable(self) -> None:
        """The four the spec names, and no consumer-recorded one beside them."""
        from kiro_crew.autonudge import _stopped_row_is_replaceable
        from kiro_crew.monitoring.models import MonitorOutcome

        replaceable = {
            outcome
            for outcome in MonitorOutcome
            if _stopped_row_is_replaceable(self._stopped_row(outcome))
        }

        assert replaceable == {
            MonitorOutcome.BUDGET,
            MonitorOutcome.SUCCESS,
            MonitorOutcome.BLOCKED,
            MonitorOutcome.TARGET_UNAVAILABLE,
        }

    def test_the_help_tells_an_operator_who_can_undo_an_armed_monitor(self) -> None:
        for entry in SCHEMA_REGISTRY:
            if entry.path == _KEY:
                help_text = " ".join(entry.help.split())
                break
        else:  # pragma: no cover - the key is asserted present elsewhere
            raise AssertionError(f"{_KEY} missing from SCHEMA_REGISTRY")

        assert "retained USER_STOP outcome that refuses a re-arm" in help_text
        assert "owner to clear that record" in help_text


class TestTheSettingsPromiseHasWiringBehindIt:
    """The no-restart story presupposes an operator can save the key.

    Every text and schema test here passes whether or not the key is PATCH-able,
    so a dropped or mistyped `_EDITABLE_CONFIG` entry would render a toggle that
    then fails to save with "field not editable" and nothing would catch it. The
    repository already asserts this for the same reason -- see
    ``test_agent_backend_editable.py`` ("must be PATCH-able or the switch cannot
    save"), and the membership pins in ``test_computer_use_api.py`` and
    ``test_trusted_apps_api.py``.
    """

    def test_the_key_is_patchable_from_settings(self) -> None:
        from kiro_crew.dashboard.handlers.core import _EDITABLE_CONFIG

        assert _KEY in _EDITABLE_CONFIG, (
            f"{_KEY} must be PATCH-able or Settings renders a toggle that cannot "
            "save, and the help text's no-restart promise has no way to be used"
        )
        assert _EDITABLE_CONFIG[_KEY]["type"] == "bool"


class TestNothingReadsConfigOnTheGatewayLoop:
    """The descriptor build must not charge a config read to the event loop.

    A running loop means the caller is
    ``mcp_discovery._managed_tools_in_process``, reaching ``_list_tools()`` from
    ``async def probe_server``. That caller keeps only tool NAMES and discards
    every description, so the preference cannot be observed there and reading it
    would buy nothing for the cost. ``mcp_tools/spawn.py::_agent_roster_hint``
    applies the identical rule for the identical caller.
    """

    def test_a_running_loop_skips_the_read_entirely(self, monkeypatch: pytest.MonkeyPatch) -> None:
        loads: list[int] = []

        def _counted(cls: type) -> KiroCrewConfig:
            loads.append(1)
            return KiroCrewConfig(monitoring=MonitoringConfig(prefer_structured_arming=True))

        monkeypatch.setattr(control.KiroCrewConfig, "load", classmethod(_counted))

        async def _on_loop() -> tuple[bool, str]:
            start = next(
                item["description"] for item in control.schemas() if item["name"] == "monitor_start"
            )
            return control._prefers_structured_arming(), start

        preferred, start = asyncio.run(_on_loop())

        assert preferred is False
        assert _CONDITIONAL_MARK in start
        assert loads == [], (
            "the descriptor build read config while an event loop was running; on "
            "that path the caller discards every description, so the read is pure "
            "cost on the gateway's loop"
        )

    def test_the_same_call_off_loop_does_read(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The serving process has no loop, so the preference is not dead.

        ``mcp_shared.run_mcp_stdio_loop`` is a plain select/readline loop that
        never imports asyncio, so this is the branch that reaches a model.
        """
        _pin(monkeypatch, on=True)

        assert control._prefers_structured_arming() is True
        assert _BY_DEFAULT_MARK in _descriptions()[0]

    def test_the_serving_loop_module_never_imports_asyncio(self) -> None:
        """Pins the premise the branch above rests on, in the module itself."""
        import kiro_crew.mcp_shared as mcp_shared

        source = Path(mcp_shared.__file__).read_text(encoding="utf-8")

        assert "import asyncio" not in source, (
            "mcp_shared now imports asyncio; if the stdio serving loop ever runs "
            "one, the on-loop skip above would silence the preference on the path "
            "that actually reaches a model"
        )


def _spec_paragraph_about(needle: str) -> str:
    """The one blank-line-delimited paragraph of the spec that names *needle*."""
    paragraphs = [p for p in _SPEC.read_text(encoding="utf-8").split("\n\n") if needle in p]
    assert len(paragraphs) == 1, (
        f"{_SPEC} mentions {needle!r} in {len(paragraphs)} paragraphs; the claim "
        "about this key has to live in exactly one of them"
    )
    return paragraphs[0]


def _baseline_help(path: str) -> str:
    """The same help string as the committed snapshot ships it."""
    root = Path(__file__).resolve().parent.parent
    with open(root / "config-baseline.json", encoding="utf-8") as handle:
        for entry in json.load(handle)["entries"]:
            if entry["path"] == path:
                return str(entry["help"])
    raise AssertionError(f"{path} missing from config-baseline.json")
