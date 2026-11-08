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
    _floor_monkeypatch.setattr(catalog, "_recorded_identity", "")
    yield
    _floor_monkeypatch.setattr(catalog, "_recorded", None)


#: The vault identity fingerprint a request is stamped with in these tests.
_ID = "fingerprint-of-account-a"


def _recorded() -> list[dict] | None:
    """What the signed-in account ``_ID`` would be served."""
    return catalog.recorded_catalog_snapshot(_ID)


def _stamped(handle: AcpSessionHandle) -> AcpSessionHandle:
    """A handle whose request went out under account ``_ID``."""
    handle.request_vault_identity = _ID
    return handle


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
        assert catalog.record_catalog(resp, ACP_BACKEND_CODEX, identity=_ID) is False
        assert catalog.record_catalog(resp, ACP_BACKEND_KIRO, identity=_ID) is False
        assert _recorded() is None
        assert catalog.record_catalog(resp, ACP_BACKEND_KAS, identity=_ID) is True
        assert [r["model_name"] for r in _recorded() or []] == ["auto"]

    def test_a_response_without_a_select_does_not_clear_a_recording(self):
        catalog.record_catalog(_select([_option("auto")]), ACP_BACKEND_KAS, identity=_ID)
        assert catalog.record_catalog({"sessionId": "s"}, ACP_BACKEND_KAS, identity=_ID) is False
        assert _recorded() is not None

    def test_the_reader_gets_a_copy(self):
        catalog.record_catalog(_select([_option("auto")]), ACP_BACKEND_KAS, identity=_ID)
        first = _recorded()
        assert first is not None
        first[0]["model_name"] = "tampered"
        assert (_recorded() or [])[0]["model_name"] == "auto"

    def test_the_runtime_handle_records_without_adopting_an_advertised_list(self):
        """``store_session_config`` feeds the catalog; the session's entitlement list
        stays empty, as model-selection.md says it must for KAS."""
        rt = MagicMock()
        rt.acp_backend = ACP_BACKEND_KAS
        handle = AcpSessionHandle("s1", asyncio.Queue(), rt)
        _stamped(handle).store_session_config(_bundle_session_new())
        assert [r["model_name"] for r in _recorded() or []] == ["auto"]
        assert handle.available_models == []

    def test_a_codex_handle_records_nothing(self):
        rt = MagicMock()
        rt.acp_backend = ACP_BACKEND_CODEX
        handle = AcpSessionHandle("s1", asyncio.Queue(), rt)
        _stamped(handle).store_session_config(_select([_option("gpt-x")]))
        assert _recorded() is None


class TestTheVaultIdentity:
    """The rows are served only to the account the vault named when they were asked.

    The fingerprint is read from the vault itself, so the fence holds whichever
    process changed the account -- the dashboard's sign-in card or ``kirocrew kas
    login`` / ``kas logout`` in a shell -- without any notice from the writer.
    """

    def _record_for(self, identity: object) -> bool:
        return catalog.record_catalog(
            _select([_option("auto"), _option("model-a")]), ACP_BACKEND_KAS, identity=identity
        )

    def test_another_account_in_the_vault_is_served_nothing(self):
        assert self._record_for(_ID)
        assert catalog.recorded_catalog_snapshot("fingerprint-of-account-b") is None
        # Nothing was forgotten: the vault naming account A again serves A's rows.
        assert [r["model_name"] for r in _recorded() or []] == ["auto", "model-a"]

    def test_an_empty_vault_matches_nothing(self):
        assert self._record_for(_ID)
        assert catalog.recorded_catalog_snapshot("") is None

    @pytest.mark.parametrize("identity", ["", None, 0, MagicMock()])
    def test_a_request_without_a_vault_identity_is_refused(self, identity):
        assert self._record_for(identity) is False
        assert catalog._recorded is None

    def test_a_late_response_from_the_previous_account_is_not_served_to_the_next(self):
        """The race: a session asked under A, the vault switches to B, A's answer
        lands. It is recorded as A's, and B is served nothing from it."""
        assert self._record_for(_ID)
        assert catalog.recorded_catalog_snapshot("fingerprint-of-account-b") is None

    def test_a_handle_whose_request_was_never_stamped_records_nothing(self):
        rt = MagicMock()
        rt.acp_backend = ACP_BACKEND_KAS
        handle = AcpSessionHandle("s1", asyncio.Queue(), rt)
        assert handle.request_vault_identity == ""
        handle.store_session_config(_bundle_session_new())
        assert catalog._recorded is None

    def test_a_long_lived_runtime_records_for_the_account_of_each_request(self):
        """The stamp lives on the request, not the process: the next ``session/new``
        on the SAME runtime after a switch records for the new account."""
        rt = MagicMock()
        rt.acp_backend = ACP_BACKEND_KAS
        _stamped(AcpSessionHandle("s1", asyncio.Queue(), rt)).store_session_config(
            _bundle_session_new()
        )
        after = AcpSessionHandle("s2", asyncio.Queue(), rt)
        after.request_vault_identity = "fingerprint-of-account-b"
        after.store_session_config(_bundle_session_new())
        assert _recorded() is None
        rows = catalog.recorded_catalog_snapshot("fingerprint-of-account-b")
        assert [r["model_name"] for r in rows or []] == ["auto"]

    @pytest.mark.asyncio
    async def test_the_kas_harness_carries_the_vault_identity_and_kiro_reads_nothing(
        self, monkeypatch
    ):
        """The stamp is read by the KAS harness as it builds the session payload,
        so the Kiro construction path gains no step (harness-parity H13)."""
        from kiro_crew.acp.harness.kas import KasHarness
        from kiro_crew.acp.harness.kiro import KiroHarness

        reads: list[str] = []

        def _read() -> str:
            reads.append("vault")
            return _ID

        monkeypatch.setattr(catalog, "vault_identity", _read)
        kiro = await KiroHarness().session_extras("", work_dir=None)
        assert kiro.vault_identity == ""
        assert reads == []
        kas = await KasHarness().session_extras("", work_dir=None)
        assert kas.vault_identity == _ID
        assert reads == ["vault"]
