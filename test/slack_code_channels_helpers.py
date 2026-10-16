"""Shared fakes for the Slack Code channel routing tests.

Recorded Slack payloads in, recorded calls out: ``Env`` wires the routing module
to a fake Slack client, a fake dashboard, a temp store, an SEL recorder and a
controllable config, so a test states only the input it varies.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew.config.loader import ACTIVATION_MENTION, ChannelConfig
from kiro_crew.slack.code_channels import binding as binding_mod
from kiro_crew.slack.code_channels import recognition, routing
from kiro_crew.slack.code_channels import settings as cc_settings
from kiro_crew.slack.code_channels import status
from kiro_crew.slack.code_channels.classifier import RouteDecision
from kiro_crew.slack.code_channels.client import CodeChannelsClient
from kiro_crew.slack.code_channels.settings import REQUIRED_SCOPE
from kiro_crew.slack.code_channels.store import CodeChannelStore, set_store

SELF_USER = "UBOT"
SELF_BOT = "BBOT"
OWNER = "UOWNER"


class FakeApi:
    def __init__(self) -> None:
        self.responses: dict[str, Any] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def api_call(self, method: str, **kwargs: Any) -> Any:
        self.calls.append((method, kwargs.get("json") or kwargs.get("params") or {}))
        resp = self.responses.get(method, {"ok": True})
        if callable(resp):
            resp = resp(kwargs.get("json") or kwargs.get("params") or {})
        if isinstance(resp, BaseException):
            raise resp
        return resp

    def methods(self) -> list[str]:
        return [m for m, _ in self.calls]


class FakeSlack:
    def __init__(self) -> None:
        self._web = FakeApi()
        self._channel_team: dict[str, str] = {}
        self.posts: list[tuple[str, str, str | None]] = []
        self.ephemerals: list[tuple[str, str, str]] = []
        self.thread: list[dict[str, Any]] = []
        self._n = 0

    async def post_message(self, channel: str, text: str, thread_ts: str | None = None) -> str:
        self._n += 1
        self.posts.append((channel, text, thread_ts))
        return f"200.{self._n:06d}"

    async def post_ephemeral(self, channel: str, user: str, text: str, **_k: Any) -> None:
        self.ephemerals.append((channel, user, text))

    async def fetch_thread_replies(self, channel: str, ts: str, **_k: Any) -> list[dict[str, Any]]:
        return list(self.thread)


class FakeState:
    def __init__(self) -> None:
        self.folders: list[dict[str, Any]] = []
        self._folders = self.folders
        self.linked: dict[str, Any] = {}

    async def read_folders(self, fn: Any) -> Any:
        return fn(self.folders)

    def get_linked_slot(self, ts: str) -> Any:
        return self.linked.get(ts)

    def get_or_create_slot(self, *_a: Any, **_k: Any) -> Any:  # presence marks a dashboard
        raise AssertionError("create_bound_session is faked in these tests")


@dataclass
class SelRecorder:
    rows: list[dict[str, Any]] = field(default_factory=list)

    def log_api_access(self, **kwargs: Any) -> None:
        self.rows.append(kwargs)

    def ops(self, operation: str) -> list[dict[str, Any]]:
        return [r for r in self.rows if r["operation"] == operation]


def channel_info(
    channel: str,
    *,
    code: bool = True,
    bots: tuple[str, ...] = (SELF_USER,),
    origin: tuple[str, str] | None = None,
    name: str = "fix-login",
) -> dict[str, Any]:
    """A recorded conversations.info response."""
    session: dict[str, Any] = {"encoded_agent_bot_user_ids": list(bots)}
    if origin:
        session["origin_link"] = {"channel_id": origin[0], "ts": origin[1]}
    props: dict[str, Any] = {"agent_session": session, "code_channel": {}}
    if code:
        props["record_channel"] = {"record_type": "agent_channel"}
    return {"ok": True, "channel": {"id": channel, "name": name, "properties": props}}


def _reset_routing() -> None:
    routing._gate_seen.clear()
    routing._creating.clear()
    routing._bind_locks.clear()


class Env:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
        self.slack = FakeSlack()
        self.api = self.slack._web
        self.orch = SimpleNamespace(slack=self.slack, sessions=object(), channel_history=None)
        self.state: FakeState | None = FakeState()
        self.sel = SelRecorder()
        self.store = CodeChannelStore(tmp_path / "slack-code-channels.json")
        self.on = True
        self.defaults: dict[str, str] = {}
        self.activation: dict[str, str] = {}
        self.decision = RouteDecision("code_channel", title="Fix flaky login test")
        self.classified: list[str] = []
        self.routed: list[tuple[str, str, str]] = []
        self.created_sessions: list[dict[str, Any]] = []
        self.route_result = True
        set_store(self.store)
        recognition.clear_cache()
        _reset_routing()
        # The install was granted the scope (connect-time probe stand-in).
        cc_settings.reset_capability()
        cc_settings.note_granted_scopes(["chat:write", REQUIRED_SCOPE])

        def cfg(_orch: Any = None) -> Any:
            return SimpleNamespace(
                slack=SimpleNamespace(
                    code_channels=self.on,
                    code_channel_defaults=SimpleNamespace(
                        folder=self.defaults.get("folder", ""),
                        agent=self.defaults.get("agent", ""),
                        model=self.defaults.get("model", ""),
                        reasoning_effort=self.defaults.get("reasoning_effort", ""),
                    ),
                ),
                agent=SimpleNamespace(default_agent="slack-default"),
                channel_config=lambda ch: ChannelConfig(
                    activation=self.activation.get(ch, ACTIVATION_MENTION)
                ),
            )

        self.cfg = cfg
        monkeypatch.setattr(routing, "live_config", cfg)
        monkeypatch.setattr(binding_mod, "live_config", cfg)
        monkeypatch.setattr(routing, "sel", lambda: self.sel)
        monkeypatch.setattr(routing, "_self_user_id", lambda: SELF_USER)
        monkeypatch.setattr(routing, "_self_bot_id", lambda: SELF_BOT)
        monkeypatch.setattr(routing, "_dashboard_state", lambda: self.state)

        async def classify(_sessions: Any, text: str, _context: Any = ()) -> RouteDecision:
            self.classified.append(text)
            return self.decision

        monkeypatch.setattr(routing, "classify", classify)

        async def create_bound_session(state: Any, spec: Any, **kwargs: Any) -> Any:
            self.created_sessions.append({"spec": spec, **kwargs})
            slot = SimpleNamespace(key=f"chat-{len(self.created_sessions)}-1", running=False)
            state.linked[kwargs["anchor_ts"]] = slot
            return slot

        monkeypatch.setattr(routing, "create_bound_session", create_bound_session)

        async def route_to_slot(_orch: Any, binding: Any, text: str, sender: str) -> bool:
            self.routed.append((binding.channel_id, text, sender))
            return self.route_result

        monkeypatch.setattr(routing, "_route_to_slot", route_to_slot)
        status.set_client_factory(lambda: CodeChannelsClient.from_slack(self.slack))

    def info(self, channel: str, **kwargs: Any) -> None:
        self.api.responses["conversations.info"] = channel_info(channel, **kwargs)


def make_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Iterator[Env]:
    """Body of each test module's ``env`` fixture (a fixture cannot be imported
    without tripping flake8's redefinition check)."""
    e = Env(monkeypatch, tmp_path)
    yield e
    set_store(None)
    status.set_client_factory(None)
    recognition.clear_cache()
    cc_settings.reset_capability()
    _reset_routing()


def mention(
    channel: str = "C0ORIGIN", ts: str = "1700000000.000100", **extra: Any
) -> dict[str, Any]:
    event = {"type": "app_mention", "user": OWNER, "channel": channel, "ts": ts, "team": "T1"}
    event["text"] = f"<@{SELF_USER}> fix the flaky login test and open a PR"
    event.update(extra)
    return event
