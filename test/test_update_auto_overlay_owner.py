"""Who owns the ``config.local.json`` entry that holds the auto-update switch.

The core cannot tell a person's own ``auto_update`` entry from one an edition writes
back on every boot: both go through the same writer. An edition that enforces the key
registers an owner (:mod:`kiro_crew.config.overlay_owners`), and every answer that
reports the pin says whose it is (``overlay_source``) and, for a policy, which one
(``overlay_policy``), so About names the policy instead of advising a removal the
edition undoes. With no owner registered, every pin is the person's own.
"""

from __future__ import annotations

import importlib
import json
import logging
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock

import pytest
from dashboard_updates_coverage_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _isolated_module_state,
    _request,
)

from kiro_crew.dashboard.handlers import updates
from kiro_crew.platform import update_layout
from kiro_crew.platform.update_layout import InstallLayout

_POLICY = {"label": "Example update policy", "detail": "Example installs updates for you."}


@pytest.fixture(autouse=True)
def overlay_owners(_floor_monkeypatch) -> ModuleType:
    """The seam module, with no owner registered for the test and none left behind.

    Autouse so every test starts clean, and resolved here instead of imported at the
    top of the file: a module missing at collection stops pytest before it runs any
    test, in this file or in any other file of the same run. The reset goes through
    the floor's own ``MonkeyPatch``, so a test that calls ``monkeypatch.undo()``
    cannot lift it and leak an owner into the real registry.
    """
    module = importlib.import_module("kiro_crew.config.overlay_owners")
    _floor_monkeypatch.setattr(module, "_OWNERS", {})
    return module


@pytest.fixture
def overlay(monkeypatch, tmp_path):
    """Write ``config.local.json`` (``None`` removes it) where the loader reads it."""
    from kiro_crew.config import loader

    path = tmp_path / "config.local.json"
    monkeypatch.setattr(loader, "config_local_path", lambda: path)

    def _write(document: object) -> Path:
        if document is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(json.dumps(document), encoding="utf-8")
        return path

    return _write


def _claiming(seen: list[object] | None = None, answer: object = _POLICY):
    """An owner that records the value it was asked about and answers *answer*."""

    def _owner(value: object) -> object:
        if seen is not None:
            seen.append(value)
        return answer

    return _owner


def _body(resp) -> dict:
    return json.loads(resp.body.decode())


class TestTheSeam:
    def test_without_an_owner_a_set_key_is_the_persons_own_entry(self, overlay_owners, overlay):
        overlay({"auto_update": False})

        pin = overlay_owners.overlay_pin("auto_update")

        assert pin.fields() == {
            "overlay_override": True,
            "overlay_source": "user",
            "overlay_policy": None,
        }

    def test_an_unset_key_has_no_source_and_its_owner_is_not_asked(self, overlay_owners, overlay):
        seen: list[object] = []
        overlay_owners.register_overlay_owner("auto_update", _claiming(seen))
        overlay({"timezone": "UTC"})

        pin = overlay_owners.overlay_pin("auto_update")

        assert pin.fields() == {
            "overlay_override": False,
            "overlay_source": None,
            "overlay_policy": None,
        }
        assert seen == []

    def test_an_owner_claiming_the_entry_makes_it_a_policy_pin(self, overlay_owners, overlay):
        seen: list[object] = []
        overlay_owners.register_overlay_owner("auto_update", _claiming(seen))
        overlay({"auto_update": False})

        pin = overlay_owners.overlay_pin("auto_update")

        assert pin.fields() == {
            "overlay_override": True,
            "overlay_source": "policy",
            "overlay_policy": _POLICY,
        }
        # Asked with the value the overlay holds, read as the loader reads it.
        assert seen == [False]

    def test_an_owner_that_declines_leaves_the_persons_own_entry(self, overlay_owners, overlay):
        overlay_owners.register_overlay_owner("auto_update", _claiming(answer=None))
        overlay({"auto_update": True})

        assert overlay_owners.overlay_pin("auto_update").source == "user"

    def test_an_owner_that_raises_reads_as_the_persons_own_entry(
        self, overlay_owners, overlay, caplog
    ):
        def _broken(_value: object) -> object:
            raise RuntimeError("zzq")

        overlay_owners.register_overlay_owner("auto_update", _broken)
        overlay({"auto_update": False})

        with caplog.at_level(logging.WARNING, logger=overlay_owners.logger.name):
            pin = overlay_owners.overlay_pin("auto_update")

        assert (pin.pinned, pin.source, pin.policy) == (True, "user", None)
        assert "auto_update" in caplog.text

    @pytest.mark.parametrize(
        "answer",
        [{"label": ""}, {"label": "   "}, {"detail": "no label"}, {"label": 7}, ["x"], "text"],
        ids=["empty", "blank", "missing", "not-text", "a-list", "a-string"],
    )
    def test_an_answer_without_a_usable_label_is_not_a_policy(
        self, overlay_owners, overlay, answer
    ):
        overlay_owners.register_overlay_owner("auto_update", _claiming(answer=answer))
        overlay({"auto_update": False})

        assert overlay_owners.overlay_pin("auto_update").source == "user"

    def test_a_policy_with_no_detail_has_an_empty_detail(self, overlay_owners, overlay):
        overlay_owners.register_overlay_owner(
            "auto_update", _claiming(answer={"label": "Example", "detail": None})
        )
        overlay({"auto_update": False})

        assert overlay_owners.overlay_pin("auto_update").policy == overlay_owners.OverlayPolicy(
            "Example", ""
        )

    def test_long_policy_text_is_cut_to_the_row(self, overlay_owners, overlay):
        overlay_owners.register_overlay_owner(
            "auto_update", _claiming(answer={"label": "L" * 500, "detail": "D " * 500})
        )
        overlay({"auto_update": False})

        policy = overlay_owners.overlay_pin("auto_update").policy

        assert policy is not None
        assert len(policy.label) == overlay_owners.MAX_LABEL_CHARS
        assert len(policy.detail) <= overlay_owners.MAX_DETAIL_CHARS

    def test_a_nested_key_is_owned_by_its_path(self, overlay_owners, overlay):
        overlay_owners.register_overlay_owner(("telemetry", "enabled"), _claiming())
        overlay({"telemetry": {"enabled": False}, "auto_update": False})

        assert overlay_owners.overlay_pin("telemetry", "enabled").source == "policy"
        assert overlay_owners.overlay_pin("auto_update").source == "user"

    def test_a_second_registration_replaces_the_first_and_unregister_drops_it(
        self, overlay_owners, overlay
    ):
        overlay({"auto_update": False})
        overlay_owners.register_overlay_owner("auto_update", _claiming(answer={"label": "One"}))
        overlay_owners.register_overlay_owner("auto_update", _claiming(answer={"label": "Two"}))

        policy = overlay_owners.overlay_pin("auto_update").policy
        assert policy is not None and policy.label == "Two"

        overlay_owners.unregister_overlay_owner("auto_update")
        assert overlay_owners.overlay_pin("auto_update").source == "user"

    @pytest.mark.parametrize("key_path", ["", (), ("telemetry", ""), (1,)])
    def test_a_key_path_must_name_a_key(self, overlay_owners, key_path):
        with pytest.raises(ValueError):
            overlay_owners.register_overlay_owner(key_path, _claiming())

    def test_an_owner_must_be_callable(self, overlay_owners):
        with pytest.raises(TypeError):
            overlay_owners.register_overlay_owner("auto_update", "not callable")  # type: ignore[arg-type]


class TestTheAutoUpdateAnswers:
    @pytest.mark.asyncio
    async def test_the_pin_read_names_an_edition_policy(self, overlay_owners, overlay):
        overlay_owners.register_overlay_owner("auto_update", _claiming())
        overlay({"auto_update": False})

        resp = await updates.api_update_auto_pin(MagicMock())

        assert resp.status == 200
        assert _body(resp) == {
            "overlay_override": True,
            "overlay_source": "policy",
            "overlay_policy": _POLICY,
        }

    @pytest.mark.asyncio
    async def test_a_policy_refusal_names_the_policy_and_gives_no_removal_advice(
        self, overlay_owners, overlay, monkeypatch
    ):
        overlay_owners.register_overlay_owner("auto_update", _claiming())
        overlay({"auto_update": False})
        wrote: list[dict] = []
        monkeypatch.setattr(
            updates, "update_config_locked", lambda _p, *, mutate: wrote.append(mutate({}))
        )

        resp = await updates.api_update_auto(_request({"enabled": True}))

        body = _body(resp)
        assert resp.status == 409
        assert body["code"] == "auto_update_overlay_owned"
        assert (body["overlay_override"], body["overlay_source"]) == (True, "policy")
        assert body["overlay_policy"] == _POLICY
        assert _POLICY["label"] in body["error"] and _POLICY["detail"] in body["error"]
        assert "remove" not in body["error"].lower()
        assert wrote == []

    @pytest.mark.asyncio
    async def test_a_persons_own_entry_is_still_refused_with_the_removal_advice(self, overlay):
        overlay({"auto_update": True})

        resp = await updates.api_update_auto(_request({"enabled": False}))

        body = _body(resp)
        assert resp.status == 409
        assert body["overlay_source"] == "user"
        assert body["overlay_policy"] is None
        assert "remove it" in body["error"]

    @pytest.mark.asyncio
    async def test_an_accepted_write_reports_the_owner_still_deciding(
        self, overlay_owners, overlay, monkeypatch
    ):
        overlay({"timezone": "UTC"})

        def _write(_path, *, mutate):
            # The edition's entry lands between the refusal check and the read-back.
            mutate({})
            overlay({"auto_update": False})

        monkeypatch.setattr(updates, "update_config_locked", _write)
        overlay_owners.register_overlay_owner("auto_update", _claiming())

        resp = await updates.api_update_auto(_request({"enabled": True}))

        body = _body(resp)
        assert resp.status == 200
        assert body["overlay_source"] == "policy"
        assert body["overlay_policy"] == _POLICY

    @pytest.mark.asyncio
    async def test_the_check_reports_whose_pin_it_is(self, overlay_owners, overlay, monkeypatch):
        overlay_owners.register_overlay_owner("auto_update", _claiming())
        overlay({"auto_update": False})

        async def _fake_check() -> None:
            updates._set_update_info(check_status="succeeded", update_available=False)

        async def _no_effect() -> None:
            return None

        monkeypatch.setattr(updates, "_do_update_check", _fake_check)
        monkeypatch.setattr(updates, "_refresh_auto_update_effect_once", _no_effect)
        monkeypatch.setattr(updates, "min_version", lambda: "")
        monkeypatch.setattr(updates, "update_required", lambda _v: False)

        resp = await updates.api_update_check(_request())

        body = _body(resp)
        assert resp.status == 200
        assert (body["overlay_override"], body["overlay_source"]) == (True, "policy")
        assert body["overlay_policy"] == _POLICY

    @pytest.mark.asyncio
    async def test_a_channel_switch_reports_whose_pin_it_is(
        self, overlay_owners, overlay, monkeypatch
    ):
        # About adopts the switch's answer as the check's (adoptCheckedAutoUpdate), so
        # an answer without the source would put the removal advice back on the switch.
        overlay_owners.register_overlay_owner("auto_update", _claiming())
        overlay({"auto_update": False})

        async def _fake_check() -> None:
            updates._set_update_info(check_status="succeeded", update_available=False)

        wheel = InstallLayout(
            kind="wheel", proj="", is_git=False, is_externally_managed=False, guidance=""
        )
        monkeypatch.setattr(updates, "resolve_provider", lambda: None)
        monkeypatch.setattr(updates, "detect_install_layout", lambda: wheel)
        monkeypatch.setattr(update_layout, "policy_release_channel", lambda: "")
        monkeypatch.setattr(updates, "_do_update_check", _fake_check)

        resp = await updates.api_update_channel(_request({"channel": "insider"}))

        body = _body(resp)
        assert resp.status == 200
        assert body["channel"] == "insider"
        assert (body["overlay_override"], body["overlay_source"]) == (True, "policy")
        assert body["overlay_policy"] == _POLICY
