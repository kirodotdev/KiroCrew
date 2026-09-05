"""Agent-record free text must leave GET /api/config/kirocrew masked (#8717).

``description`` and ``triggers`` on an agent record are agent- and
package-writable: an agent can edit ``config.json`` directly, and agent sync
copies ``description`` straight off a discovered agent spec. They are not
schema-``sensitive`` (they are not owner secrets), so ``_masked_config_dict``'s
schema-driven walk never touched them and a credential- or
exfiltration-URL-shaped value shipped to the browser verbatim — from BOTH
response sites of the endpoint (the GET body and the PATCH echo). The roster
endpoint masks the same class of strings (#8472); these tests pin the config
endpoint's side of that rule.

The rule under test (``_mask_agent_free_text``): a value ``_redact_external``
would alter is replaced WHOLESALE by ``_SENSITIVE_MASK``; a non-string is
masked too; benign content is byte-identical. The save() path is never masked —
masking there would persist the sentinel over the stored value.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.config import loader as L
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.config.sections import KiroCrewAgentConfig
from kiro_crew.dashboard.handlers.core import (
    _AGENT_UNTRUSTED_TEXT_FIELDS,
    _SENSITIVE_MASK,
    _mask_agent_free_text,
    _masked_config_dict,
)

# Shapes _redact_external verifiably alters (probed against the real chain):
# a credential-shaped token, and a URL with a conventionally-named secret param.
_CRED_DESCRIPTION = "helper agent AKIAIOSFODNN7EXAMPLE for builds"
_EXFIL_TRIGGERS = "post results to https://collector.example.com/x?token=abc123"
# Benign strings the redactors leave byte-identical, including non-ASCII.
_BENIGN_DESCRIPTION = "Reviews pull requests and summarizes diffs."
_BENIGN_TRIGGERS = "use for code review, PR summaries, 代码审查"


def _cfg_with_agents() -> KiroCrewConfig:
    cfg = KiroCrewConfig()
    cfg.agents["shady"] = KiroCrewAgentConfig(
        description=_CRED_DESCRIPTION, triggers=_EXFIL_TRIGGERS
    )
    cfg.agents["plain"] = KiroCrewAgentConfig(
        description=_BENIGN_DESCRIPTION, triggers=_BENIGN_TRIGGERS
    )
    return cfg


def test_payload_preconditions_pin_the_redactor():
    """If a fixture stops tripping/passing ``_redact_external``, fail HERE.

    Localizes a detector regression: without these, a redactor change makes the
    masking tests fail with no signal whether the rule or the detector moved.
    """
    from kiro_crew.dashboard.handlers.discover import _redact_external

    assert _redact_external(_CRED_DESCRIPTION) != _CRED_DESCRIPTION
    assert _redact_external(_EXFIL_TRIGGERS) != _EXFIL_TRIGGERS
    assert _redact_external(_BENIGN_DESCRIPTION) == _BENIGN_DESCRIPTION
    assert _redact_external(_BENIGN_TRIGGERS) == _BENIGN_TRIGGERS


def test_every_unguarded_str_field_is_in_the_masked_set():
    """INVARIANT: the masked tuple plus the shape-guarded exceptions must
    cover every ``str`` field of ``KiroCrewAgentConfig``.

    This is the ratchet against the class recurring: a newly added free-text
    record field fails here instead of shipping to the browser unmasked. Each
    exception names the load-time guard that justifies it — a guard that pins
    the SHAPE (not just the type) to something the redactors cannot alter.
    """
    import dataclasses

    shape_guarded = {
        "reasoning_effort",  # coerce_effort: unknown level collapses to ""
        "session_color",  # _safe_color: pinned to #rrggbb or ""
    }
    str_fields = {f.name for f in dataclasses.fields(KiroCrewAgentConfig) if f.type in ("str", str)}
    assert str_fields, "field-type introspection returned nothing — check f.type handling"
    covered = set(_AGENT_UNTRUSTED_TEXT_FIELDS) | shape_guarded
    assert str_fields == covered, (
        f"unmasked free-text field(s): {sorted(str_fields - covered)}; "
        f"stale tuple entries: {sorted(covered - str_fields)}"
    )


class TestMaskAgentFreeText:
    def test_credential_shaped_value_is_masked_wholesale(self):
        assert _mask_agent_free_text(_CRED_DESCRIPTION) == _SENSITIVE_MASK

    def test_url_secret_param_value_is_masked_wholesale(self):
        assert _mask_agent_free_text(_EXFIL_TRIGGERS) == _SENSITIVE_MASK

    def test_benign_value_is_byte_identical(self):
        # Byte-identical, not merely equal-after-normalization: the browser must
        # render exactly what the owner stored.
        assert _mask_agent_free_text(_BENIGN_DESCRIPTION) is _BENIGN_DESCRIPTION

    def test_empty_string_passes_through(self):
        assert _mask_agent_free_text("") == ""

    def test_non_string_is_masked(self):
        # Load-bearing, not defensive: ``description`` is a bare ``entry.get``
        # on the load path (no type guard, unlike ``model``/``triggers``), so a
        # non-string genuinely reaches this function. Masking it also pins the
        # wire shape: the frontend types the field as ``string``, and an object
        # must not ship where a string is declared.
        assert _mask_agent_free_text({"k": "v"}) == _SENSITIVE_MASK
        assert _mask_agent_free_text(None) == _SENSITIVE_MASK

    def test_partial_credential_masks_the_whole_value(self):
        # Wholesale, not in-place scrub: the benign remainder is lost with the
        # token. This is the named cost of a view that cannot be mistaken for
        # content — pin it so an in-place scrub cannot sneak back in.
        out = _mask_agent_free_text(_CRED_DESCRIPTION)
        assert "helper agent" not in str(out)


class TestMaskedConfigDict:
    def test_suspicious_agent_fields_leave_masked(self):
        masked = _masked_config_dict(_cfg_with_agents())
        assert masked["agents"]["shady"]["description"] == _SENSITIVE_MASK
        assert masked["agents"]["shady"]["triggers"] == _SENSITIVE_MASK
        body = json.dumps(masked)
        assert "AKIAIOSFODNN7EXAMPLE" not in body
        assert "collector.example.com" not in body

    def test_benign_agent_fields_are_untouched(self):
        masked = _masked_config_dict(_cfg_with_agents())
        assert masked["agents"]["plain"]["description"] == _BENIGN_DESCRIPTION
        assert masked["agents"]["plain"]["triggers"] == _BENIGN_TRIGGERS

    def test_other_record_fields_are_not_masked(self):
        # The pass is scoped to the named free-text fields; structural fields
        # (workspace, source, ...) must keep rendering in the Settings UI.
        masked = _masked_config_dict(_cfg_with_agents())
        record = masked["agents"]["shady"]
        for key, val in record.items():
            if key in _AGENT_UNTRUSTED_TEXT_FIELDS:
                continue
            assert val != _SENSITIVE_MASK, f"unexpected mask on agents.*.{key}"

    def test_extended_fields_mask_when_suspicious(self):
        # The wider field set (#8717 review): every unguarded record string is
        # covered, not just the two the issue named. A credential-shaped
        # workspace/kiro_agent must mask; the benign defaults must not.
        cfg = KiroCrewConfig()
        cfg.agents["odd"] = KiroCrewAgentConfig(
            kiro_agent=_CRED_DESCRIPTION, workspace=_EXFIL_TRIGGERS
        )
        masked = _masked_config_dict(cfg)
        record = masked["agents"]["odd"]
        assert record["kiro_agent"] == _SENSITIVE_MASK
        assert record["workspace"] == _SENSITIVE_MASK
        # Untouched benign defaults keep rendering in the Settings UI.
        assert record["memory_store"] == "default"
        assert record["source"] == "kirocrew"

    def test_suspicious_record_key_is_removed_not_leaked(self):
        # Agent sync stores a discovered agent's NAME as the map key, so a
        # credential-shaped package name would ship verbatim as a key. Removal,
        # not masking: masking a key would collide two suspicious records.
        cfg = KiroCrewConfig()
        cfg.agents[_CRED_DESCRIPTION] = KiroCrewAgentConfig(description="x")
        cfg.agents["plain"] = KiroCrewAgentConfig(description=_BENIGN_DESCRIPTION)
        masked = _masked_config_dict(cfg)
        assert _CRED_DESCRIPTION not in masked["agents"]
        assert "plain" in masked["agents"]
        assert "AKIAIOSFODNN7EXAMPLE" not in json.dumps(masked)
        # save() still carries the record — only the browser view drops it.
        assert _CRED_DESCRIPTION in cfg.to_dict()["agents"]

    def test_name_references_to_removed_record_are_masked(self):
        # default_agent / session.pool_agent spell an agent name verbatim, so a
        # removed record's name must not survive through them.
        cfg = KiroCrewConfig()
        cfg.agents[_CRED_DESCRIPTION] = KiroCrewAgentConfig()
        cfg.default_agent = _CRED_DESCRIPTION
        cfg.session.pool_agent = _CRED_DESCRIPTION
        masked = _masked_config_dict(cfg)
        assert masked["default_agent"] == _SENSITIVE_MASK
        assert masked["session"]["pool_agent"] == _SENSITIVE_MASK
        assert "AKIAIOSFODNN7EXAMPLE" not in json.dumps(masked)

    def test_benign_name_references_are_untouched(self):
        cfg = KiroCrewConfig()
        cfg.agents["plain"] = KiroCrewAgentConfig()
        cfg.default_agent = "plain"
        cfg.session.pool_agent = "plain"
        masked = _masked_config_dict(cfg)
        assert masked["default_agent"] == "plain"
        assert masked["session"]["pool_agent"] == "plain"

    def test_save_path_is_never_masked(self):
        # to_dict()/save() must keep the stored value — masking there would
        # persist the sentinel over the operator's config.
        cfg = _cfg_with_agents()
        _masked_config_dict(cfg)
        stored = cfg.to_dict()["agents"]["shady"]
        assert stored["description"] == _CRED_DESCRIPTION
        assert stored["triggers"] == _EXFIL_TRIGGERS


def _point_loader_at(tmp_path, monkeypatch, data: dict) -> None:
    cfgp = tmp_path / "config.json"
    cfgp.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(L, "config_path", lambda: cfgp)
    monkeypatch.setattr(L, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(L, "config_local_path", lambda: tmp_path / "config.local.json")


def _seed_config() -> dict:
    return {
        "agents": {
            "kirocrew": {
                "kiro_agent": "kirocrew",
                "workspace": "default",
                "memory_store": "default",
                "description": _CRED_DESCRIPTION,
                "triggers": _EXFIL_TRIGGERS,
            }
        },
        "default_agent": "kirocrew",
        "auto_update": False,
    }


class TestConfigEndpointWire:
    """Both response sites, over HTTP — the wire is what the browser reads."""

    @pytest.mark.asyncio
    async def test_get_response_masks_agent_free_text(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.handlers import core as core_mod

        _point_loader_at(tmp_path, monkeypatch, _seed_config())
        app = web.Application()
        app.router.add_route("*", "/api/config/kirocrew", core_mod.api_kirocrew_config)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/config/kirocrew")
            assert resp.status == 200
            body = await resp.json()
        assert body["agents"]["kirocrew"]["description"] == _SENSITIVE_MASK
        assert body["agents"]["kirocrew"]["triggers"] == _SENSITIVE_MASK
        assert "AKIAIOSFODNN7EXAMPLE" not in json.dumps(body)

    @pytest.mark.asyncio
    async def test_patch_echo_masks_agent_free_text(self, tmp_path, monkeypatch):
        """The PATCH handler's config echo is the second response site."""
        from kiro_crew.dashboard.handlers import api_kirocrew_config_patch

        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(json.dumps(_seed_config()), encoding="utf-8")
        app = web.Application()
        app.router.add_patch("/api/config/kirocrew", api_kirocrew_config_patch)
        app["state"] = SimpleNamespace(subagents=MagicMock(spec=["update_completion_keep"]))
        with patch("kiro_crew.config.loader.config_path", return_value=cfg_path):
            async with TestClient(TestServer(app)) as client:
                resp = await client.patch(
                    "/api/config/kirocrew", json={"path": "auto_update", "value": True}
                )
                assert resp.status == 200
                body = await resp.json()
        assert body["agents"]["kirocrew"]["description"] == _SENSITIVE_MASK
        assert body["agents"]["kirocrew"]["triggers"] == _SENSITIVE_MASK
        assert "AKIAIOSFODNN7EXAMPLE" not in json.dumps(body)
        # The PATCH wrote only its own path; the stored free text is untouched.
        stored = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert stored["agents"]["kirocrew"]["description"] == _CRED_DESCRIPTION
