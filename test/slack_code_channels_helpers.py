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
from kiro_crew.slack import features
from kiro_crew.slack.code_channels import binding as binding_mod
from kiro_crew.slack.code_channels import recognition, routing, status
from kiro_crew.slack.code_channels.classifier import RouteDecision
from kiro_crew.slack.code_channels.store import CodeChannelStore, set_store

REQUIRED_SCOPE = features.REQUIRED_SCOPE[features.CODE_CHANNELS]
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
        self.reactions: list[tuple[str, str, str]] = []
        self._n = 0

    async def add_reaction(self, channel: str, ts: str, emoji: str) -> None:
        self.reactions.append((channel, ts, emoji))

    async def post_message(self, channel: str, text: str, thread_ts: str | None = None) -> str:
        self._n += 1
        self.posts.append((channel, text, thread_ts))
        return f"200.{self._n:06d}"

    def _inject_team(self, channel: str, params: dict[str, Any]) -> None:
        team = self._channel_team.get(channel)
        if team and "team_id" not in params:
            params["team_id"] = team

    async def post_ephemeral(self, channel: str, user: str, text: str, **_k: Any) -> None:
        self.ephemerals.append((channel, user, text))

    async def fetch_thread_replies(self, channel: str, ts: str, **_k: Any) -> list[dict[str, Any]]:
        return list(self.thread)

    async def fetch_message_detail(self, channel: str, ts: str) -> dict[str, str] | None:
        for message in self.thread:
            if message.get("ts") == ts:
                return {
                    "text": str(message.get("text") or ""),
                    "user": str(message.get("user") or ""),
                }
        return None


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
    routing._gate_pending.clear()
    routing._creating.clear()
    routing._bind_locks.clear()
    routing._follow_up_locks.clear()
    routing._PENDING_TEMPS.clear()


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
        self.senders: list[dict[str, str] | None] = []
        self.created_sessions: list[dict[str, Any]] = []
        self.folders_created: list[str] = []
        self.route_result = True
        self.persist_result = True
        self.persisted: list[str] = []
        set_store(self.store)
        recognition.clear_cache()
        _reset_routing()
        # The install was granted the scope (connect-time probe stand-in).
        features.reset_capabilities()
        features.record_granted_scopes(["chat:write", REQUIRED_SCOPE])

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
            if kwargs.get("before_publish") is not None:
                await kwargs["before_publish"](slot)
            return slot

        monkeypatch.setattr(routing, "create_bound_session", create_bound_session)

        async def ensure_folder_path(state: Any, path: str, *, default_agent: str = "") -> str:
            # The fake store's mkdir -p: one top-level folder per name.
            for folder in state.folders:
                if folder["name"] == path and not folder.get("parent_id"):
                    return str(folder["id"])
            folder_id = f"f{len(state.folders) + 1:08x}"
            state.folders.append(
                {"id": folder_id, "name": path, "parent_id": "", "default_agent": default_agent}
            )
            self.folders_created.append(path)
            return folder_id

        monkeypatch.setattr(binding_mod, "ensure_folder_path", ensure_folder_path)

        async def route_to_slot(
            _orch: Any, binding: Any, text: str, sender_id: str, **kw: Any
        ) -> bool:
            self.routed.append((binding.channel_id, text, sender_id))
            self.senders.append(kw.get("sender"))
            if self.route_result:
                routing._adopt_temps(binding.session_key)
            return self.route_result

        monkeypatch.setattr(routing, "_route_to_slot", route_to_slot)

        async def persist_request(binding: Any) -> bool:
            # The real save writes the session's transcript; the fake state has none.
            self.persisted.append(binding.channel_id)
            return self.persist_result

        self.real_persist_request = routing._persist_request
        monkeypatch.setattr(routing, "_persist_request", persist_request)
        status.set_slack_provider(lambda: self.slack)

    def info(self, channel: str, **kwargs: Any) -> None:
        self.api.responses["conversations.info"] = channel_info(channel, **kwargs)


def make_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Iterator[Env]:
    """Body of each test module's ``env`` fixture (a fixture cannot be imported
    without tripping flake8's redefinition check)."""
    e = Env(monkeypatch, tmp_path)
    yield e
    set_store(None)
    status.set_slack_provider(None)
    recognition.clear_cache()
    features.reset_capabilities()
    _reset_routing()


def mention(
    channel: str = "C0ORIGIN", ts: str = "1700000000.000100", **extra: Any
) -> dict[str, Any]:
    event = {"type": "app_mention", "user": OWNER, "channel": channel, "ts": ts, "team": "T1"}
    event["text"] = f"<@{SELF_USER}> fix the flaky login test and open a PR"
    event.update(extra)
    return event
