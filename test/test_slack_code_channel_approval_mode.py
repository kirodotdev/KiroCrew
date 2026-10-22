"""Slack code channels: the approval mode a new code-channel session starts in.

``slack.code_channel_defaults.approval_mode`` is ``ask`` (the default, stored as
unset), ``trust_reads`` or ``trust``. Pins the config parse and round-trip, the
resolution default, the settings PUT/GET, and that the runtime puts the created
slot on the mode through the shared approval seed -- the same per-slot flags and
``mode_change:<mode>`` SEL record a person's Reads / Trust press writes -- only
once the session is bound.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

# The settings route harness the Slack Code settings tests already use.
from test_slack_code_settings import _call, _stored, env  # noqa: F401

import kiro_crew.config.loader as loader
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.config.section_builders import _build_slack_config
from kiro_crew.slack import features
from kiro_crew.slack.code_channels import binding as binding_mod
from kiro_crew.slack.code_channels.binding import SessionSpec, create_bound_session


class _SelRecorder:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def log_api_access(self, **kw: Any) -> None:
        self.calls.append(kw)


@pytest.fixture()
def sel(monkeypatch: pytest.MonkeyPatch) -> _SelRecorder:
    import kiro_crew.sel as sel_mod

    recorder = _SelRecorder()
    monkeypatch.setattr(sel_mod, "sel", lambda: recorder)
    return recorder


def _mode_changes(sel: _SelRecorder) -> list[dict[str, Any]]:
    return [c for c in sel.calls if str(c.get("operation", "")).startswith("mode_change:")]


# ── Config ───────────────────────────────────────────────────────────────────


class TestConfig:
    @pytest.mark.parametrize("mode", ["trust_reads", "trust"])
    def test_a_posture_is_parsed(self, mode: str) -> None:
        slack = _build_slack_config({"code_channel_defaults": {"approval_mode": f" {mode} "}})
        assert slack.code_channel_defaults.approval_mode == mode
        assert features.code_channel_defaults(slack).approval_mode == mode
        assert not features.code_channel_defaults(slack).is_empty

    @pytest.mark.parametrize("raw", ["ask", "", "yolo", "normal", "TRUST", 1, None])
    def test_ask_unset_and_junk_read_as_unset(self, raw: object) -> None:
        # ``ask`` is the default; anything unknown falls to it, never to a
        # posture nobody chose.
        slack = _build_slack_config({"code_channel_defaults": {"approval_mode": raw}})
        assert slack.code_channel_defaults.approval_mode is None
        assert features.code_channel_defaults(slack).approval_mode is None

    def test_the_reader_drops_a_value_the_builder_never_produced(self) -> None:
        raw = SimpleNamespace(code_channel_defaults=SimpleNamespace(approval_mode="yolo"))
        assert features.code_channel_defaults(raw).approval_mode is None

    def test_round_trip_through_config_json(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "config.json"
        monkeypatch.setattr(loader, "config_path", lambda: path)
        monkeypatch.setattr(loader, "env_path", lambda: tmp_path / ".env")
        cfg = KiroCrewConfig.load()
        cfg.slack.code_channel_defaults.approval_mode = "trust_reads"
        cfg.save()
        stored = json.loads(path.read_text(encoding="utf-8"))
        assert stored["slack"]["code_channel_defaults"]["approval_mode"] == "trust_reads"
        again = KiroCrewConfig.load()
        assert again.slack.code_channel_defaults.approval_mode == "trust_reads"


# ── Resolution ───────────────────────────────────────────────────────────────


class TestResolution:
    def test_unset_resolves_to_ask(self) -> None:
        resolved = features.resolve_code_channel_session(features.CodeChannelDefaults(), [])
        assert resolved.approval_mode == "ask"

    def test_an_explicit_mode_is_what_the_session_gets(self) -> None:
        resolved = features.resolve_code_channel_session(
            features.CodeChannelDefaults(approval_mode="trust"), []
        )
        assert resolved.approval_mode == "trust"

    @pytest.mark.asyncio
    async def test_the_session_spec_carries_the_mode(self) -> None:
        cfg = SimpleNamespace(
            agent=SimpleNamespace(default_agent=""),
            slack=SimpleNamespace(
                code_channel_defaults=SimpleNamespace(approval_mode="trust_reads")
            ),
        )
        spec = await binding_mod.resolve_session_spec(SimpleNamespace(), cfg)
        assert spec.approval_mode == "trust_reads"
        assert SessionSpec().approval_mode == "ask"


# ── Settings ─────────────────────────────────────────────────────────────────


class TestSettings:
    def test_unset_reads_as_null_and_effective_ask(self, env: dict) -> None:  # noqa: F811
        status, body = _call(env, "GET")
        assert status == 200
        assert body["code_channel_defaults"]["approval_mode"] is None
        assert body["code_channel_effective"]["approval_mode"] == "ask"

    @pytest.mark.parametrize("mode", ["trust_reads", "trust"])
    def test_a_mode_saves_and_reads_back(self, env: dict, mode: str) -> None:  # noqa: F811
        status, _ = _call(env, "PUT", {"code_channel_defaults": {"approval_mode": mode}})
        assert status == 200
        assert _stored(env)["code_channel_defaults"]["approval_mode"] == mode
        _, body = _call(env, "GET")
        assert body["code_channel_defaults"]["approval_mode"] == mode
        assert body["code_channel_effective"]["approval_mode"] == mode

    @pytest.mark.parametrize("clear", ["ask", None, ""])
    def test_ask_null_and_blank_clear(self, env: dict, clear: object) -> None:  # noqa: F811
        _call(env, "PUT", {"code_channel_defaults": {"approval_mode": "trust"}})
        status, _ = _call(env, "PUT", {"code_channel_defaults": {"approval_mode": clear}})
        assert status == 200
        assert _stored(env)["code_channel_defaults"]["approval_mode"] is None

    @pytest.mark.parametrize("bad", ["yolo", "normal", "Trust", 3])
    def test_other_values_are_refused(self, env: dict, bad: object) -> None:  # noqa: F811
        status, res = _call(env, "PUT", {"code_channel_defaults": {"approval_mode": bad}})
        assert status == 400 and "approval_mode" in res["error"]

    def test_an_unrelated_save_does_not_revalidate_a_hand_edited_value(
        self,
        env: dict,  # noqa: F811
    ) -> None:
        env["cfg"].write_text(
            json.dumps({"slack": {"code_channel_defaults": {"approval_mode": "yolo"}}}),
            encoding="utf-8",
        )
        status, _ = _call(env, "PUT", {"code_channel_defaults": {"reasoning_effort": "high"}})
        assert status == 200


# ── Runtime ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "trust", "reads"), [("trust", True, False), ("trust_reads", False, True)]
)
async def test_the_created_slot_starts_in_the_configured_mode(
    tmp_path: Any, sel: _SelRecorder, mode: str, trust: bool, reads: bool
) -> None:
    from chat_test_helpers import _make_state

    from kiro_crew.dashboard.chat_utils import effective_session_key

    state = _make_state(tmp_path)
    slot = await create_bound_session(
        state,
        SessionSpec(approval_mode=mode),
        title="t",
        channel_id="C0CODE",
        anchor_ts="1.0",
    )
    assert slot._trust is trust and slot._trust_reads is reads
    # The session's policy is set the way the picker sets it.
    state.sessions.set_approval_policy.assert_called_with(
        effective_session_key(slot), "auto" if mode == "trust" else ""
    )
    # Audited like a person's press, under the code channel's own caller.
    assert _mode_changes(sel) == [
        {
            "caller": binding_mod.APPROVAL_SEED_CALLER,
            "operation": f"mode_change:{mode}",
            "outcome": "enabled",
            "resources": slot.key,
        }
    ]


@pytest.mark.asyncio
async def test_ask_leaves_the_created_slot_as_it_is(tmp_path: Any, sel: _SelRecorder) -> None:
    from chat_test_helpers import _make_state

    state = _make_state(tmp_path)
    slot = await create_bound_session(
        state, SessionSpec(), title="t", channel_id="C0CODE", anchor_ts="1.0"
    )
    assert not slot._trust and not slot._trust_reads and not slot._trust_scope
    state.sessions.set_approval_policy.assert_not_called()
    assert _mode_changes(sel) == []


@pytest.mark.asyncio
async def test_a_retracted_session_records_no_mode_change(tmp_path: Any, sel: _SelRecorder) -> None:
    from chat_test_helpers import _make_state

    from kiro_crew.slack.code_channels import store as store_mod

    state = _make_state(tmp_path)

    async def binding_fails(_slot: Any) -> None:
        raise store_mod.StoreWriteError("disk full")

    with pytest.raises(store_mod.StoreWriteError):
        await create_bound_session(
            state,
            SessionSpec(approval_mode="trust"),
            title="t",
            channel_id="C1",
            anchor_ts="7.0",
            before_publish=binding_fails,
        )
    assert _mode_changes(sel) == []


@pytest.mark.asyncio
async def test_the_seed_never_touches_the_global_override(
    tmp_path: Any, sel: _SelRecorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A person's mode press can end a declared YOLO grant; creating a channel must not."""
    from chat_test_helpers import _make_state

    import kiro_crew.dashboard.chat_handlers as chat_handlers

    touched: list[str] = []
    monkeypatch.setattr(
        chat_handlers,
        "safety_override",
        lambda: SimpleNamespace(
            is_declared=True,
            deactivate=lambda *_a: touched.append("deactivate"),
            activate=lambda *_a: touched.append("activate"),
        ),
    )
    state = _make_state(tmp_path)
    await create_bound_session(
        state, SessionSpec(approval_mode="trust"), title="t", channel_id="C1", anchor_ts="1.0"
    )
    assert touched == []
