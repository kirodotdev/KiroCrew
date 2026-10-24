"""The KAS engine's ``model`` select is read as its catalog, on a positive KAS gate.

Measured on the bundled engine (``test/fixtures/acp_frames/kas/bundle-session-live.jsonl``):
``session/new`` carries no ``models`` object, so the select is the only place the
engine says which models it serves and what each costs. These tests pin that
measurement, the row shape the readers consume, the bounds, and that only a KAS
session records -- a codex or kiro session's select is never mistaken for it.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from kiro_crew import model_registry
from kiro_crew.acp.session_handle import AcpSessionHandle
from kiro_crew.acp.types import ACP_BACKEND_CODEX, ACP_BACKEND_KAS, ACP_BACKEND_KIRO
from kiro_crew.agent_sdk import kas_model_catalog as catalog

_FIXTURE = Path(__file__).parent / "fixtures" / "acp_frames" / "kas" / "bundle-session-live.jsonl"


@pytest.fixture(autouse=True)
def _cold_catalog(_floor_monkeypatch):
    # The floor's own MonkeyPatch (D11): a test's ``monkeypatch.undo()`` cannot
    # lift it, so the recording is cold at the start of every test here.
    _floor_monkeypatch.setattr(catalog, "_recorded", None)
    _floor_monkeypatch.setattr(catalog, "_generation", 0)
    yield
    _floor_monkeypatch.setattr(catalog, "_recorded", None)


def _current() -> int:
    return catalog.sign_in_generation()


def _bundle_session_new() -> dict:
    for line in _FIXTURE.read_text(encoding="utf-8").splitlines():
        frame = json.loads(line)
        if frame.get("id") == 2 and "result" in frame:
            return frame["result"]
    raise AssertionError("the bundle fixture has no session/new response")


def _option(value: str, *, meta: dict | None = None, name: str = "", description: str = ""):
    option: dict = {"value": value, "name": name or value, "description": description}
    if meta is not None:
        option["_meta"] = {"kiro": meta}
    return option


def _select(options: list[dict], *, current: str = "auto") -> dict:
    return {
        "configOptions": [
            {"id": "mode", "type": "select", "currentValue": "vibe", "options": []},
            {
                "id": "model",
                "type": "select",
                "category": "model",
                "currentValue": current,
                "options": options,
            },
        ]
    }


class TestTheMeasurement:
    def test_the_bundled_engine_advertises_no_models_object(self):
        """What decided the design: the select is the ONLY source on the bundle route."""
        resp = _bundle_session_new()
        assert "models" not in resp
        assert "availableModels" not in resp
        rows = catalog.catalog_rows_from_session(resp)
        assert [r["model_name"] for r in rows] == ["auto"]

    def test_the_select_carries_a_price_and_no_window(self):
        (row,) = catalog.catalog_rows_from_session(_bundle_session_new())
        assert row["rate_multiplier"] == 1.0
        assert row["display_name"] == "Auto"
        assert "context_window_tokens" not in row
        assert "context_window" not in row


class TestRowShape:
    def test_rows_take_the_list_models_catalog_shape(self):
        rows = catalog.catalog_rows_from_session(
            _select(
                [
                    _option("auto", meta={"rateMultiplier": 1, "hasEffort": False}, name="Auto"),
                    _option(
                        "model-x",
                        meta={"rateMultiplier": 2, "hasEffort": True, "effortLevels": ["low"]},
                        name="Model X",
                        description="Fast",
                    ),
                ]
            )
        )
        assert rows == [
            {
                "model_id": "auto",
                "model_name": "auto",
                "display_name": "Auto",
                "description": "",
                "rate_multiplier": 1.0,
            },
            {
                "model_id": "model-x",
                "model_name": "model-x",
                "display_name": "Model X",
                "description": "Fast",
                "rate_multiplier": 2.0,
            },
        ]

    def test_a_row_without_a_numeric_multiplier_carries_no_price(self):
        rows = catalog.catalog_rows_from_session(
            _select(
                [
                    _option("no-meta"),
                    _option("bool-rate", meta={"rateMultiplier": True}),
                    _option("string-rate", meta={"rateMultiplier": "2"}),
                    _option("float-rate", meta={"rateMultiplier": 0.5}),
                ]
            )
        )
        priced = {r["model_name"]: r.get("rate_multiplier") for r in rows}
        assert priced == {
            "no-meta": None,
            "bool-rate": None,
            "string-rate": None,
            "float-rate": 0.5,
        }

    def test_grouped_options_are_flattened_one_level(self):
        rows = catalog.catalog_rows_from_session(
            _select([{"name": "Group", "options": [_option("a"), _option("b")]}, _option("c")])
        )
        assert [r["model_name"] for r in rows] == ["a", "b", "c"]

    def test_an_over_long_id_is_refused_not_truncated_and_duplicates_drop(self, caplog):
        long_id = "m" * (model_registry.ADVERTISED_MODEL_ID_MAX_CHARS + 1)
        with caplog.at_level(logging.WARNING, logger=catalog.logger.name):
            rows = catalog.catalog_rows_from_session(
                _select([_option(long_id), _option("a"), _option("a"), _option("  ")])
            )
        assert [r["model_name"] for r in rows] == ["a"]
        # The over-long id is refused and SAID: a duplicate and a blank are not.
        assert [r.getMessage() for r in caplog.records] == [
            f"KAS model select refused 1 option(s): over {model_registry.ADVERTISED_MODELS_MAX_IDS} "
            f"ids or over {model_registry.ADVERTISED_MODEL_ID_MAX_CHARS} chars"
        ]

    def test_the_row_count_is_capped_at_the_id_store_bound_and_the_cut_is_logged(self, caplog):
        many = [_option(f"m{i}") for i in range(model_registry.ADVERTISED_MODELS_MAX_IDS + 5)]
        with caplog.at_level(logging.WARNING, logger=catalog.logger.name):
            rows = catalog.catalog_rows_from_session(_select(many))
        assert len(rows) == model_registry.ADVERTISED_MODELS_MAX_IDS
        assert any("refused 5 option(s)" in r.getMessage() for r in caplog.records)

    def test_a_clean_select_logs_nothing(self, caplog):
        with caplog.at_level(logging.WARNING, logger=catalog.logger.name):
            catalog.catalog_rows_from_session(_select([_option("auto"), _option("a")]))
        assert caplog.records == []

    def test_text_fields_are_clamped(self):
        wide = "d" * (model_registry.ADVERTISED_MODEL_ID_MAX_CHARS * 2)
        (row,) = catalog.catalog_rows_from_session(
            _select([_option("a", name=wide, description=wide)])
        )
        assert len(row["display_name"]) == model_registry.ADVERTISED_MODEL_ID_MAX_CHARS
        assert len(row["description"]) == model_registry.ADVERTISED_MODEL_ID_MAX_CHARS

    @pytest.mark.parametrize(
        "resp",
        [
            {},
            {"configOptions": "nope"},
            {"configOptions": [{"id": "model", "type": "text"}]},
            {"configOptions": [{"id": "model", "type": "select", "options": []}]},
            {"configOptions": [{"id": "model", "type": "select", "options": [{"name": "x"}]}]},
        ],
    )
    def test_no_usable_select_is_no_rows(self, resp):
        assert catalog.catalog_rows_from_session(resp) == []


class TestRecording:
    def test_only_a_kas_session_records(self):
        resp = _select([_option("auto", meta={"rateMultiplier": 1})])
        assert catalog.record_catalog(resp, ACP_BACKEND_CODEX, generation=_current()) is False
        assert catalog.record_catalog(resp, ACP_BACKEND_KIRO, generation=_current()) is False
        assert catalog.recorded_catalog() is None
        assert catalog.record_catalog(resp, ACP_BACKEND_KAS, generation=_current()) is True
        assert [r["model_name"] for r in catalog.recorded_catalog() or []] == ["auto"]

    def test_a_response_without_a_select_does_not_clear_a_recording(self):
        catalog.record_catalog(_select([_option("auto")]), ACP_BACKEND_KAS, generation=_current())
        assert (
            catalog.record_catalog({"sessionId": "s"}, ACP_BACKEND_KAS, generation=_current())
            is False
        )
        assert catalog.recorded_catalog() is not None

    def test_the_reader_gets_a_copy(self):
        catalog.record_catalog(_select([_option("auto")]), ACP_BACKEND_KAS, generation=_current())
        first = catalog.recorded_catalog()
        assert first is not None
        first[0]["model_name"] = "tampered"
        assert (catalog.recorded_catalog() or [])[0]["model_name"] == "auto"

    def test_the_runtime_handle_records_without_adopting_an_advertised_list(self):
        """``store_session_config`` feeds the catalog; the session's entitlement list
        stays empty, as model-selection.md says it must for KAS."""
        rt = MagicMock()
        rt.acp_backend = ACP_BACKEND_KAS
        rt.sign_in_generation = _current()
        handle = AcpSessionHandle("s1", asyncio.Queue(), rt)
        handle.store_session_config(_bundle_session_new())
        assert [r["model_name"] for r in catalog.recorded_catalog() or []] == ["auto"]
        assert handle.available_models == []

    def test_a_codex_handle_records_nothing(self):
        rt = MagicMock()
        rt.acp_backend = ACP_BACKEND_CODEX
        rt.sign_in_generation = _current()
        handle = AcpSessionHandle("s1", asyncio.Queue(), rt)
        handle.store_session_config(_select([_option("gpt-x")]))
        assert catalog.recorded_catalog() is None


class TestTheSignInGeneration:
    """A recording belongs to the account whose credential the engine launched with.

    ``forget_catalog`` (sign-out, authorized sign-in) moves the generation on; a
    session that opened under the previous account and answers afterwards carries
    the old generation and is refused, so it cannot restore that account's list.
    """

    def test_a_recording_from_the_current_generation_is_taken(self):
        assert catalog.record_catalog(
            _select([_option("auto")]), ACP_BACKEND_KAS, generation=_current()
        )
        assert catalog.recorded_catalog() is not None

    def test_forget_moves_the_generation_on_and_clears(self):
        before = _current()
        catalog.record_catalog(_select([_option("auto")]), ACP_BACKEND_KAS, generation=before)
        catalog.forget_catalog()
        assert catalog.recorded_catalog() is None
        assert _current() == before + 1

    def test_a_late_response_from_the_previous_account_is_refused(self):
        """The race: a session opened under A, B signs in, A's response lands."""
        launched_under = _current()
        catalog.forget_catalog()
        assert (
            catalog.record_catalog(
                _select([_option("auto"), _option("model-a")]),
                ACP_BACKEND_KAS,
                generation=launched_under,
            )
            is False
        )
        assert catalog.recorded_catalog() is None

    @pytest.mark.parametrize("stamp", [None, "0", 0.0, True, MagicMock()])
    def test_an_unstamped_or_non_int_origin_is_refused(self, stamp):
        assert (
            catalog.record_catalog(_select([_option("auto")]), ACP_BACKEND_KAS, generation=stamp)
            is False
        )
        assert catalog.recorded_catalog() is None

    def test_a_runtime_without_a_launch_stamp_records_nothing(self):
        rt = MagicMock()
        rt.acp_backend = ACP_BACKEND_KAS
        rt.sign_in_generation = None
        handle = AcpSessionHandle("s1", asyncio.Queue(), rt)
        handle.store_session_config(_bundle_session_new())
        assert catalog.recorded_catalog() is None
