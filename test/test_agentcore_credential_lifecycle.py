"""Integrated AgentCore credential lifecycle.

Human first turn → injected login Gateway → synthetic turn → immediate
revocation. The ACP child must never keep a human JWT after unbind.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from kiro_crew.config import KiroCrewConfig
from kiro_crew.platform.bootstrap import build_default_context
from kiro_crew.platform.context import reset_context, set_context
from kiro_crew.platform.defaults import DefaultAgentIdentityProvider
from kiro_crew.platform.governance import parse_policy
from kiro_crew.platform.interfaces import InboundToken, SessionPrincipal

_GATEWAY_URL = "https://gateway.example.test/mcp"
_TOKEN = "sltok-lifecycle-must-not-survive-unbind"
_STALE_BEARER = "stale-leftover-must-not-reach-handshake"


class _CompanionIdentity(DefaultAgentIdentityProvider):
    def __init__(self) -> None:
        self._token = InboundToken(
            scheme="bearer",
            token=_TOKEN,
            expires_at=4_000_000_000.0,
            audience="g",
        )

    def enabled(self) -> bool:
        return True

    def gateway_mcp_spec(self) -> dict[str, object] | None:
        return {"url": _GATEWAY_URL}

    def status(self) -> dict[str, object]:
        return {"credentialKind": "user", "vaultedOwnerToken": False}

    async def vend_gateway_inbound_token(self, principal: SessionPrincipal) -> InboundToken | None:
        return self._token


class _Sessions:
    def __init__(self) -> None:
        self.principal: Any = None
        self.removed: list[str] = []

    def set_principal(self, key: str, principal: Any) -> None:
        self.principal = principal

    def get_principal(self, key: str) -> Any:
        return self.principal

    async def remove(self, key: str) -> None:
        self.removed.append(key)

    async def retract_principal_credentials(self, key: str) -> None:
        from kiro_crew.platform.agentcore_gateway import (
            _recycle_live_session,
            clear_inbound_sidecar,
        )

        await _recycle_live_session(self, key, why="unbind")
        clear_inbound_sidecar(key)


def _install_login(*, posture: str = "login") -> None:
    base = build_default_context(KiroCrewConfig())
    set_context(
        dataclasses.replace(
            base,
            agent_identity=_CompanionIdentity(),
            governance=parse_policy(
                {
                    "version": 1,
                    "boot": {"fail_closed": True},
                    "capabilities": {"agentcore": {"enabled": True, "posture": posture}},
                }
            ),
        )
    )


def test_expired_inbound_token_is_absent_before_sidecar_write() -> None:
    from kiro_crew.platform.agentcore_gateway import _live_inbound_token

    expired = InboundToken(scheme="bearer", token=_TOKEN, expires_at=1.0, audience="g")
    assert _live_inbound_token(expired) is None
    assert _live_inbound_token(None) is None
    live = InboundToken(scheme="bearer", token=_TOKEN, expires_at=4_000_000_000.0, audience="g")
    assert _live_inbound_token(live) is live


@pytest.mark.asyncio
async def test_expired_vend_token_writes_oauth_challenge_not_bearer() -> None:
    """An already-expired JWT must not be written; session/new would loop."""
    import json

    from kiro_crew.platform.agentcore_gateway import prepare_session_gateway

    class _Expired(_CompanionIdentity):
        def __init__(self) -> None:
            self._token = InboundToken(
                scheme="bearer",
                token=_TOKEN,
                expires_at=1.0,
                audience="g",
            )

    try:
        _install_login()
        from kiro_crew.platform.context import current_context, set_context

        ctx = current_context()
        set_context(dataclasses.replace(ctx, agent_identity=_Expired()))
        path = await prepare_session_gateway(
            "dashboard:expired",
            surface="dashboard",
            raw_id="alice",
            sessions=_Sessions(),
        )
        assert path is not None
        raw = json.loads(path.read_text(encoding="utf-8"))
        assert raw.get("oauth_challenge") is True
        assert "headers" not in raw
        assert "expires_at" not in raw
        assert _TOKEN not in path.read_text(encoding="utf-8")
    finally:
        reset_context()


@pytest.mark.asyncio
async def test_human_first_turn_injects_then_synthetic_revokes() -> None:
    from kiro_crew.platform.agent_identity import clear_session_principal
    from kiro_crew.platform.agentcore_gateway import (
        inbound_sidecar_path,
        prepare_session_gateway,
        session_gateway_servers,
    )

    sessions = _Sessions()
    try:
        _install_login()
        # Human first turn: sidecar exists before session/new.
        attached = await prepare_session_gateway(
            "dashboard:1",
            surface="dashboard",
            raw_id="alice",
            sessions=sessions,
        )
        assert attached is not None
        injected = session_gateway_servers("dashboard:1")
        assert injected
        headers = injected[0].get("headers") or []
        bearer = " ".join(str(item.get("value", "")) for item in headers if isinstance(item, dict))
        assert _TOKEN in bearer
        assert inbound_sidecar_path("dashboard:1").exists()

        # Synthetic turn on the same live session: recycle + drop bearer.
        sessions.principal = SessionPrincipal(
            surface="dashboard",
            subject="dashboard+alice",
            session_key="dashboard:1",
            user_jwt=_TOKEN,
        )
        await prepare_session_gateway("dashboard:1", sessions=sessions)
        assert inbound_sidecar_path("dashboard:1").exists() is False
        assert session_gateway_servers("dashboard:1") == []
        assert "dashboard:1" in sessions.removed

        # Explicit unbind retracts again so a leftover child cannot keep JWT.
        await clear_session_principal(sessions, "dashboard:1")
        assert sessions.principal is None
        assert inbound_sidecar_path("dashboard:1").exists() is False
        assert sessions.removed.count("dashboard:1") >= 2
        assert _TOKEN not in str(session_gateway_servers("dashboard:1"))
    finally:
        reset_context()


@pytest.mark.asyncio
async def test_attach_withholds_when_crew_profile_denies_agentcore(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    """Surface permit + crew deny must not vend a sidecar.

    Attach used to call ``_identity_on(session_key)`` with empty agent, so
    a dashboard surface that permits AgentCore minted a bearer even when
    the selected crew's task profile denied it.
    """
    import json

    from kiro_crew.platform import governance_profiles as gp
    from kiro_crew.platform.agentcore_gateway import (
        inbound_sidecar_path,
        prepare_session_gateway,
        session_gateway_servers,
    )

    pdir = tmp_path / "profiles"
    pdir.mkdir()
    monkeypatch.setattr(gp, "_PROFILES_DIR", pdir)
    (pdir / "dash-permit.json").write_text(
        json.dumps(
            {
                "name": "dash-permit",
                "bind": {"type": "surface", "id": "dashboard"},
                "capabilities": {"agentcore": {"enabled": True}},
            }
        ),
        encoding="utf-8",
    )
    (pdir / "research.json").write_text(
        json.dumps(
            {
                "name": "research",
                "bind": {"type": "task", "id": "research"},
                "capabilities": {"agentcore": {"enabled": False}},
            }
        ),
        encoding="utf-8",
    )
    gp.reset_store()
    try:
        _install_login()
        denied = await prepare_session_gateway(
            "dashboard:1",
            surface="dashboard",
            raw_id="alice",
            sessions=_Sessions(),
            agent="research",
        )
        assert denied is None
        assert inbound_sidecar_path("dashboard:1").exists() is False
        assert session_gateway_servers("dashboard:1") == []

        permitted = await prepare_session_gateway(
            "dashboard:1",
            surface="dashboard",
            raw_id="alice",
            sessions=_Sessions(),
        )
        assert permitted is not None
        injected = session_gateway_servers("dashboard:1")
        assert injected
        headers = injected[0].get("headers") or []
        bearer = " ".join(str(item.get("value", "")) for item in headers if isinstance(item, dict))
        assert _TOKEN in bearer
    finally:
        reset_context()
        gp.reset_store()


@pytest.mark.asyncio
async def test_busy_recycle_raises_before_turn_continues() -> None:
    from kiro_crew.platform.agentcore_gateway import (
        GatewayCredentialError,
        prepare_session_gateway,
    )

    class _Busy:
        def is_busy(self, key: str) -> bool:
            return True

        def get_principal(self, key: str) -> object:
            return object()

    try:
        _install_login()
        await prepare_session_gateway(
            "dashboard:1",
            surface="dashboard",
            raw_id="alice",
            sessions=_Sessions(),
        )
        with pytest.raises(GatewayCredentialError):
            await prepare_session_gateway("dashboard:1", sessions=_Busy())
    finally:
        reset_context()


@pytest.mark.asyncio
async def test_gateway_recycle_releases_held_semaphore(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import AsyncMock

    from kiro_crew.session import SessionManager

    def factory(session_key=None, agent=None, channel_id=None, **kwargs):
        provider = AsyncMock()
        provider.start = AsyncMock()
        provider.shutdown = AsyncMock()
        provider.is_process_alive = lambda: True
        provider.is_alive = lambda: True
        provider.context_usage_pct = lambda: 0.0
        provider.has_active_turn = lambda: False
        return provider

    mgr = SessionManager(KiroCrewConfig(), provider_factory=factory)
    try:
        await mgr.get_or_create("dashboard:recycle")
        mgr.release("dashboard:recycle")
        sess = mgr._sessions["dashboard:recycle"]
        old_sem = sess.semaphore

        recycled = {"n": 0}

        async def _recycle(_sessions, key):
            recycled["n"] += 1
            if recycled["n"] > 1:
                return False
            await mgr.reset(key, skip_if_busy=False)
            return True

        monkeypatch.setattr(
            "kiro_crew.platform.agentcore_gateway.apply_staged_session_gateway",
            _recycle,
        )
        await mgr.get_or_create("dashboard:recycle")
        assert old_sem.locked() is False
        mgr.release("dashboard:recycle")
    finally:
        await mgr.close_all()


@pytest.mark.asyncio
async def test_gateway_apply_error_releases_held_semaphore(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Post-acquire apply failure must not leave the session permanently locked."""
    from unittest.mock import AsyncMock

    from kiro_crew.platform.agentcore_gateway import GatewayCredentialError
    from kiro_crew.session import SessionManager

    def factory(session_key=None, agent=None, channel_id=None, **kwargs):
        provider = AsyncMock()
        provider.start = AsyncMock()
        provider.shutdown = AsyncMock()
        provider.is_process_alive = lambda: True
        provider.is_alive = lambda: True
        provider.context_usage_pct = lambda: 0.0
        provider.has_active_turn = lambda: False
        return provider

    mgr = SessionManager(KiroCrewConfig(), provider_factory=factory)
    try:
        await mgr.get_or_create("dashboard:apply-err")
        mgr.release("dashboard:apply-err")
        sess = mgr._sessions["dashboard:apply-err"]

        async def _boom(_sessions: Any, key: str) -> bool:
            raise GatewayCredentialError("cannot drop leftover bearer")

        monkeypatch.setattr(
            "kiro_crew.platform.agentcore_gateway.apply_staged_session_gateway",
            _boom,
        )
        with pytest.raises(GatewayCredentialError):
            await mgr.get_or_create("dashboard:apply-err")
        assert sess.semaphore.locked() is False

        monkeypatch.setattr(
            "kiro_crew.platform.agentcore_gateway.apply_staged_session_gateway",
            AsyncMock(return_value=False),
        )
        await mgr.get_or_create("dashboard:apply-err")
        assert sess.semaphore.locked() is True
        mgr.release("dashboard:apply-err")
    finally:
        await mgr.close_all()


def _mock_provider_factory():
    from unittest.mock import AsyncMock

    def factory(session_key=None, agent=None, channel_id=None, **kwargs):
        provider = AsyncMock()
        provider.start = AsyncMock()
        provider.shutdown = AsyncMock()
        provider.is_process_alive = lambda: True
        provider.is_alive = lambda: True
        provider.context_usage_pct = lambda: 0.0
        provider.has_active_turn = lambda: False
        provider.cwd = ""
        return provider

    return factory


@pytest.mark.asyncio
async def test_cold_start_applies_gateway_only_after_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two first messages on one new key must not write the sidecar pre-lease."""
    import asyncio

    from kiro_crew.session import SessionManager

    held: list[bool] = []

    async def _apply(sessions, key):
        sess = sessions._sessions.get(key)
        held.append(sess is not None and sess.semaphore.locked())
        return False

    monkeypatch.setattr(
        "kiro_crew.platform.agentcore_gateway.apply_staged_session_gateway",
        _apply,
    )
    mgr = SessionManager(KiroCrewConfig(), provider_factory=_mock_provider_factory())

    async def _claim() -> None:
        await mgr.get_or_create("discord:shared")
        mgr.release("discord:shared")

    try:
        await asyncio.wait_for(asyncio.gather(_claim(), _claim()), timeout=5)
        assert held
        assert all(held)
    finally:
        await mgr.close_all()


@pytest.mark.asyncio
async def test_unstaged_apply_retracts_leftover_bearer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kiro_crew.platform import agentcore_gateway as gw

    recycled: dict[str, object] = {}

    async def _recycle(sessions: Any, key: str, *, why: str, hold_lease: bool = False) -> bool:
        recycled["why"] = why
        recycled["key"] = key
        return True

    monkeypatch.setattr(gw, "_recycle_live_session", _recycle)
    monkeypatch.setattr(gw, "inbound_sidecar_state", lambda key: "present")
    monkeypatch.setattr(
        gw, "clear_inbound_sidecar", lambda key: recycled.setdefault("cleared", True)
    )

    class _Sess:
        def get_principal(self, key: str) -> str:
            return "slack+U1"

    assert await gw.apply_staged_session_gateway(_Sess(), "slack:1") is True
    assert recycled["why"] == "unbound"
    assert recycled["key"] == "slack:1"
    assert recycled["cleared"] is True


@pytest.mark.asyncio
async def test_staged_apply_restages_bind_after_recycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kiro_crew.platform import agentcore_gateway as gw

    gw.stage_session_gateway("dashboard:1", "dashboard", "alice", agent="research")

    async def _apply(
        session_key: str,
        *,
        surface: str | None,
        raw_id: str | None,
        sessions: Any,
        hold_lease: bool,
        agent: str = "",
    ) -> bool:
        assert session_key == "dashboard:1"
        assert surface == "dashboard"
        assert raw_id == "alice"
        assert agent == "research"
        return True

    monkeypatch.setattr(gw, "_apply_session_gateway", _apply)
    assert await gw.apply_staged_session_gateway(None, "dashboard:1") is True
    restaged = gw._staged_for("dashboard:1")
    assert restaged is not None and restaged[3] == "research"
    assert gw.take_staged_gateway("dashboard:1") == ("dashboard", "alice")


def test_get_or_create_does_not_apply_staged_gateway_before_pool() -> None:
    import inspect

    from kiro_crew.session_allocation import SessionAllocationService

    src = inspect.getsource(SessionAllocationService.get_or_create)
    refused = src.index("raise SpeculativeResumeRefused(key)")
    pool = src.index("Try warm pool first")
    start = src.index("await provider.start()")
    assert "await owner._apply_staged_gateway_under_lease" not in src[refused:pool]
    assert "await owner._apply_staged_gateway_under_lease" in src[pool:]
    assert "await owner._install_staged_gateway_sidecar(key)" in src[pool:start]
    creator = src.index("# Creator path:")
    assert "take_staged_gateway(key)" in src[creator:]
    assert "await owner._apply_staged_gateway_under_lease" not in src[creator:]


class _FreshTokenIdentity(_CompanionIdentity):
    """Each vend returns a distinct token so a re-vend changes the fingerprint."""

    def __init__(self) -> None:
        super().__init__()
        self._n = 0

    async def vend_gateway_inbound_token(self, principal: SessionPrincipal) -> InboundToken | None:
        self._n += 1
        return InboundToken(
            scheme="bearer",
            token=f"{_TOKEN}-{self._n}",
            expires_at=4_000_000_000.0,
            audience="g",
        )


def _install_login_fresh_token() -> None:
    base = build_default_context(KiroCrewConfig())
    set_context(
        dataclasses.replace(
            base,
            agent_identity=_FreshTokenIdentity(),
            governance=parse_policy(
                {
                    "version": 1,
                    "boot": {"fail_closed": True},
                    "capabilities": {"agentcore": {"enabled": True, "posture": "login"}},
                }
            ),
        )
    )


@pytest.mark.asyncio
async def test_creator_consumes_staged_bind_without_revend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fresh token per vend must not recycle the just-created child."""
    from unittest.mock import AsyncMock

    from kiro_crew.platform import agentcore_gateway as gw
    from kiro_crew.session import SessionManager

    def factory(session_key=None, agent=None, channel_id=None, **kwargs):
        return _reservation_provider(start=AsyncMock())

    mgr = SessionManager(KiroCrewConfig(), provider_factory=factory)
    key = "dashboard:fresh-vend"
    try:
        _install_login_fresh_token()
        gw.stage_session_gateway(key, "discord", "alice")
        provider, was_new, _was_resumed = await mgr.get_or_create(key)
        assert was_new is True
        assert provider is not None
        assert gw.peek_staged_gateway(key) is None
        mgr.release(key)
    finally:
        reset_context()
        await mgr.close_all()


@pytest.mark.asyncio
async def test_install_staged_sidecar_does_not_take(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kiro_crew.platform import agentcore_gateway as gw

    gw.stage_session_gateway("dashboard:1", "discord", "alice", agent="research")
    applied: list[tuple[str | None, str | None, object, str]] = []

    async def _apply(
        session_key: str,
        *,
        surface: str | None,
        raw_id: str | None,
        sessions: Any,
        hold_lease: bool,
        agent: str = "",
    ) -> bool:
        applied.append((surface, raw_id, sessions, agent))
        return False

    monkeypatch.setattr(gw, "_apply_session_gateway", _apply)
    await gw.install_staged_gateway_sidecar("dashboard:1")
    assert applied == [("discord", "alice", None, "research")]
    assert gw.peek_staged_gateway("dashboard:1") == ("discord", "alice")
    assert gw.take_staged_gateway("dashboard:1") == ("discord", "alice")


def _write_stale_bearer(session_key: str) -> Any:
    import json

    from kiro_crew.platform.agentcore_gateway import GATEWAY_SERVER_NAME, inbound_sidecar_path

    path = inbound_sidecar_path(session_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "name": GATEWAY_SERVER_NAME,
                "url": _GATEWAY_URL,
                "session_key": session_key,
                "subject": "stale+leftover",
                "headers": {"Authorization": f"Bearer {_STALE_BEARER}"},
                "expires_at": 4_000_000_000.0,
                "audience": "g",
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return path


def _reservation_provider(*, start: Any) -> Any:
    from unittest.mock import AsyncMock

    provider = AsyncMock()
    provider.start = start
    provider.shutdown = AsyncMock()
    provider.is_process_alive = lambda: True
    provider.is_alive = lambda: True
    provider.context_usage_pct = lambda: 0.0
    provider.has_active_turn = lambda: False
    provider.cwd = ""
    return provider


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("key", "resume_sid"),
    [
        ("dashboard:stale-new", None),
        ("dashboard:stale-load", "sid-resume-stale"),
    ],
    ids=["session_new", "session_load"],
)
async def test_stale_bearer_replaced_before_session_handshake(
    monkeypatch: pytest.MonkeyPatch,
    key: str,
    resume_sid: str | None,
) -> None:
    """A leftover JWT is gone before start() — both session/new and session/load."""
    import json
    from unittest.mock import AsyncMock

    from kiro_crew.platform import agentcore_gateway as gw
    from kiro_crew.session import SessionManager

    seen: list[dict[str, Any]] = []
    path = _write_stale_bearer(key)
    assert _STALE_BEARER in path.read_text(encoding="utf-8")

    async def _start() -> None:
        seen.append(json.loads(path.read_text(encoding="utf-8")))

    monkeypatch.setattr(
        SessionManager,
        "_apply_staged_gateway",
        AsyncMock(return_value=False),
    )

    def factory(session_key=None, agent=None, channel_id=None, **kwargs):
        return _reservation_provider(start=_start)

    mgr = SessionManager(KiroCrewConfig(), provider_factory=factory)
    if resume_sid is not None:
        # Arms the session/load path (pool bypass + resume sid). Install
        # still runs before start() — the same handshake that issues load.
        mgr._session_map.set(key, resume_sid)
    try:
        _install_login()
        gw.stage_session_gateway(key, "discord", "alice")
        await mgr.get_or_create(key)
        assert len(seen) == 1
        handshake = json.dumps(seen[0])
        assert _STALE_BEARER not in handshake
        assert _TOKEN in handshake
        assert seen[0].get("subject") == "discord+alice"
        mgr.release(key)
    finally:
        reset_context()
        await mgr.close_all()


@pytest.mark.asyncio
async def test_creation_reservation_blocks_pooled_write_during_cold_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Warm-pool and cold creators cannot mutate or read the sidecar together.

    Cold start() observes this turn's bind, never the leftover bearer and
    never the concurrent pool claimant's write. The pool task blocks on
    the per-key lock for the whole start() window.
    """
    import asyncio
    import json
    from unittest.mock import AsyncMock

    from kiro_crew.platform import agentcore_gateway as gw
    from kiro_crew.session import SessionManager

    key = "discord:create-race"
    path = _write_stale_bearer(key)
    in_start = asyncio.Event()
    finish_start = asyncio.Event()
    seen: list[dict[str, Any]] = []

    async def _start() -> None:
        seen.append(json.loads(path.read_text(encoding="utf-8")))
        in_start.set()
        await finish_start.wait()

    monkeypatch.setattr(
        SessionManager,
        "_apply_staged_gateway",
        AsyncMock(return_value=False),
    )

    def factory(session_key=None, agent=None, channel_id=None, **kwargs):
        return _reservation_provider(start=_start)

    mgr = SessionManager(KiroCrewConfig(), provider_factory=factory)
    mgr._pool_size = 1
    mgr._pool_agent = ""
    try:
        _install_login()

        async def _cold() -> Any:
            gw.stage_session_gateway(key, "discord", "alice")
            return await mgr.get_or_create(key)

        cold_task = asyncio.create_task(_cold())
        await asyncio.wait_for(in_start.wait(), timeout=5)
        assert len(seen) == 1
        assert seen[0].get("subject") == "discord+alice"
        assert _STALE_BEARER not in json.dumps(seen[0])
        assert _TOKEN in json.dumps(seen[0])
        mid = json.loads(path.read_text(encoding="utf-8"))
        assert mid.get("subject") == "discord+alice"
        assert _STALE_BEARER not in json.dumps(mid)

        pool_p = _reservation_provider(
            start=AsyncMock(side_effect=AssertionError("pooled start() must not run"))
        )
        mgr._warm_pool.put_nowait((pool_p, 0.0))

        async def _pooled() -> Any:
            gw.stage_session_gateway(key, "discord", "bob")
            return await mgr.get_or_create(key)

        pooled_task = asyncio.create_task(_pooled())
        for _ in range(40):
            if mgr._creation_lock_for(key).locked() and not pooled_task.done():
                break
            await asyncio.sleep(0)
        assert not pooled_task.done()
        assert mgr._creation_lock_for(key).locked()
        still = json.loads(path.read_text(encoding="utf-8"))
        assert still.get("subject") == "discord+alice"
        assert still.get("subject") != "discord+bob"

        finish_start.set()
        await asyncio.wait_for(cold_task, timeout=5)
        assert [row.get("subject") for row in seen] == ["discord+alice"]
        assert not mgr._creation_lock_for(key).locked()
        mgr.release(key)
        await asyncio.wait_for(pooled_task, timeout=5)
        assert not mgr._creation_lock_for(key).locked()
    finally:
        finish_start.set()
        reset_context()
        await mgr.close_all()


@pytest.mark.asyncio
async def test_creation_lock_released_on_factory_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock

    from kiro_crew.session import SessionManager

    monkeypatch.setattr(
        SessionManager,
        "_apply_staged_gateway",
        AsyncMock(return_value=False),
    )
    calls = {"n": 0}

    def factory(session_key=None, agent=None, channel_id=None, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("factory boom")
        return _reservation_provider(start=AsyncMock())

    mgr = SessionManager(KiroCrewConfig(), provider_factory=factory)
    key = "dashboard:factory-leak"
    try:
        with pytest.raises(RuntimeError, match="factory boom"):
            await mgr.get_or_create(key)
        assert not mgr._creation_lock_for(key).locked()
        await mgr.get_or_create(key)
        assert not mgr._creation_lock_for(key).locked()
        mgr.release(key)
    finally:
        await mgr.close_all()


@pytest.mark.asyncio
async def test_creation_lock_released_on_start_sem_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio
    from unittest.mock import AsyncMock

    from kiro_crew.session import SessionManager

    monkeypatch.setattr(
        SessionManager,
        "_apply_staged_gateway",
        AsyncMock(return_value=False),
    )

    def factory(session_key=None, agent=None, channel_id=None, **kwargs):
        return _reservation_provider(start=AsyncMock())

    mgr = SessionManager(KiroCrewConfig(), provider_factory=factory)
    mgr._start_sem = asyncio.Semaphore(0)
    key = "dashboard:sem-cancel"
    task = asyncio.create_task(mgr.get_or_create(key))
    try:
        for _ in range(80):
            if mgr._creation_lock_for(key).locked():
                break
            await asyncio.sleep(0)
        else:
            pytest.fail("creation lock was never acquired")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not mgr._creation_lock_for(key).locked()
        mgr._start_sem = asyncio.Semaphore(1)
        await mgr.get_or_create(key)
        assert not mgr._creation_lock_for(key).locked()
        mgr.release(key)
    finally:
        if not task.done():
            task.cancel()
        await mgr.close_all()


@pytest.mark.asyncio
async def test_pool_hit_runs_session_new_after_sidecar_install(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AgentCore-on pool hit discards the fill process and re-issues session/new."""
    import json
    import time
    from unittest.mock import AsyncMock

    from kiro_crew.platform import agentcore_gateway as gw
    from kiro_crew.session import SessionManager

    key = "dashboard:pool-fresh"
    path = _write_stale_bearer(key)
    seen: list[dict[str, Any]] = []
    killed: list[Any] = []

    monkeypatch.setattr(
        SessionManager,
        "_apply_staged_gateway",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        SessionManager,
        "_dispatch_hard_kill",
        staticmethod(killed.append),
    )

    def factory(session_key=None, agent=None, channel_id=None, **kwargs):
        async def _start() -> None:
            # Replenish / fill uses an empty key — do not count that handshake.
            if session_key:
                seen.append(json.loads(path.read_text(encoding="utf-8")))

        return _reservation_provider(start=_start)

    mgr = SessionManager(KiroCrewConfig(), provider_factory=factory)
    mgr._pool_size = 1
    mgr._pool_agent = ""
    pool_p = _reservation_provider(
        start=AsyncMock(side_effect=AssertionError("pooled start() must not run"))
    )
    mgr._warm_pool.put_nowait((pool_p, time.monotonic()))
    try:
        _install_login()
        gw.stage_session_gateway(key, "discord", "alice")
        provider, _is_new, _resumed = await mgr.get_or_create(key)
        assert provider is not pool_p
        assert pool_p in killed
        assert len(seen) == 1
        handshake = json.dumps(seen[0])
        assert _STALE_BEARER not in handshake
        assert _TOKEN in handshake
        assert seen[0].get("subject") == "discord+alice"
        mgr.release(key)
    finally:
        reset_context()
        await mgr.close_all()


@pytest.mark.asyncio
async def test_pool_hit_discards_when_workload_identity_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Workload inject also requires a fresh session/new after a pool hit."""
    import time
    from unittest.mock import AsyncMock

    from kiro_crew.session import SessionManager

    key = "dashboard:pool-workload"
    started: list[str] = []
    killed: list[Any] = []

    monkeypatch.setattr(
        SessionManager,
        "_apply_staged_gateway",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        SessionManager,
        "_dispatch_hard_kill",
        staticmethod(killed.append),
    )

    def factory(session_key=None, agent=None, channel_id=None, **kwargs):
        async def _start() -> None:
            if session_key:
                started.append(session_key)

        return _reservation_provider(start=_start)

    mgr = SessionManager(KiroCrewConfig(), provider_factory=factory)
    mgr._pool_size = 1
    mgr._pool_agent = ""
    pool_p = _reservation_provider(
        start=AsyncMock(side_effect=AssertionError("pooled start() must not run"))
    )
    mgr._warm_pool.put_nowait((pool_p, time.monotonic()))
    try:
        _install_login(posture="workload")
        provider, _is_new, _resumed = await mgr.get_or_create(key)
        assert provider is not pool_p
        assert pool_p in killed
        assert started == [key]
        mgr.release(key)
    finally:
        reset_context()
        await mgr.close_all()


@pytest.mark.asyncio
async def test_pool_hit_reuses_process_when_identity_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import time
    from unittest.mock import AsyncMock

    from kiro_crew.session import SessionManager

    key = "dashboard:pool-off"
    factory_keys: list[str] = []

    monkeypatch.setattr(
        SessionManager,
        "_apply_staged_gateway",
        AsyncMock(return_value=False),
    )

    def factory(session_key=None, agent=None, channel_id=None, **kwargs):
        factory_keys.append(session_key or "")
        return _reservation_provider(start=AsyncMock())

    mgr = SessionManager(KiroCrewConfig(), provider_factory=factory)
    mgr._pool_size = 1
    mgr._pool_agent = ""
    pool_p = _reservation_provider(
        start=AsyncMock(side_effect=AssertionError("pooled start() must not run"))
    )
    mgr._warm_pool.put_nowait((pool_p, time.monotonic()))
    try:
        provider, _is_new, _resumed = await mgr.get_or_create(key)
        assert provider is pool_p
        assert all(not k for k in factory_keys)
        mgr.release(key)
    finally:
        await mgr.close_all()


@pytest.mark.asyncio
async def test_cancel_waiting_for_create_lock_does_not_claim_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio
    import time
    from unittest.mock import AsyncMock

    from kiro_crew.session import SessionManager

    key = "dashboard:pool-cancel"

    monkeypatch.setattr(
        SessionManager,
        "_apply_staged_gateway",
        AsyncMock(return_value=False),
    )

    def factory(session_key=None, agent=None, channel_id=None, **kwargs):
        raise AssertionError("factory must not run while cancelled before claim")

    mgr = SessionManager(KiroCrewConfig(), provider_factory=factory)
    mgr._pool_size = 1
    mgr._pool_agent = ""
    pool_p = _reservation_provider(start=AsyncMock())
    mgr._warm_pool.put_nowait((pool_p, time.monotonic()))
    lock = mgr._creation_lock_for(key)
    await lock.acquire()
    task = asyncio.create_task(mgr.get_or_create(key))
    try:
        for _ in range(80):
            if not task.done():
                break
            await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert mgr._warm_pool.qsize() == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert mgr._warm_pool.qsize() == 1
        assert not mgr._creation_lock_for(key).locked() or lock.locked()
        lock.release()
        assert not mgr._creation_lock_for(key).locked()
    finally:
        if lock.locked():
            lock.release()
        if not task.done():
            task.cancel()
        await mgr.close_all()


@pytest.mark.asyncio
async def test_install_error_returns_pool_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import time
    from unittest.mock import AsyncMock

    from kiro_crew.session import SessionManager

    key = "dashboard:pool-install-err"

    monkeypatch.setattr(
        SessionManager,
        "_apply_staged_gateway",
        AsyncMock(return_value=False),
    )

    async def _boom(self, session_key: str) -> None:
        raise RuntimeError("sidecar install boom")

    monkeypatch.setattr(SessionManager, "_install_staged_gateway_sidecar", _boom)

    def factory(session_key=None, agent=None, channel_id=None, **kwargs):
        raise AssertionError("factory must not run after install failure")

    mgr = SessionManager(KiroCrewConfig(), provider_factory=factory)
    mgr._pool_size = 1
    mgr._pool_agent = ""
    pool_p = _reservation_provider(start=AsyncMock())
    mgr._warm_pool.put_nowait((pool_p, time.monotonic()))
    try:
        with pytest.raises(RuntimeError, match="sidecar install boom"):
            await mgr.get_or_create(key)
        assert mgr._warm_pool.qsize() == 1
        claimed, _spawn = mgr._warm_pool.get_nowait()
        assert claimed is pool_p
        assert not mgr._creation_lock_for(key).locked()
    finally:
        await mgr.close_all()


@pytest.mark.asyncio
async def test_pool_hit_discards_when_crew_profile_permits_agentcore(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    """Surface deny + crew permit must discard the pooled process.

    Dashboard keeps ``agent`` at the default (pool-eligible) and passes the
    selected crew as ``crew_agent``. The discard check must use that
    resolved identity — passing empty agent would read the surface profile
    and reuse a process that never received Gateway inject.
    """
    import json
    import time
    from unittest.mock import AsyncMock

    from kiro_crew.platform import agentcore_gateway as gw
    from kiro_crew.platform import governance_profiles as gp
    from kiro_crew.session import SessionManager

    pdir = tmp_path / "profiles"
    pdir.mkdir()
    monkeypatch.setattr(gp, "_PROFILES_DIR", pdir)
    (pdir / "dash-deny.json").write_text(
        json.dumps(
            {
                "name": "dash-deny",
                "bind": {"type": "surface", "id": "dashboard"},
                "capabilities": {"agentcore": {"enabled": False}},
            }
        ),
        encoding="utf-8",
    )
    (pdir / "research.json").write_text(
        json.dumps(
            {
                "name": "research",
                "bind": {"type": "task", "id": "research"},
                "capabilities": {"agentcore": {"enabled": True}},
            }
        ),
        encoding="utf-8",
    )
    gp.reset_store()

    key = "dashboard:crew-profile"
    started: list[str] = []
    killed: list[Any] = []

    monkeypatch.setattr(
        SessionManager,
        "_apply_staged_gateway",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        SessionManager,
        "_dispatch_hard_kill",
        staticmethod(killed.append),
    )

    def factory(session_key=None, agent=None, channel_id=None, **kwargs):
        async def _start() -> None:
            if session_key:
                started.append(session_key)

        return _reservation_provider(start=_start)

    mgr = SessionManager(KiroCrewConfig(), provider_factory=factory)
    mgr._pool_size = 1
    mgr._pool_agent = ""
    pool_crew = _reservation_provider(
        start=AsyncMock(side_effect=AssertionError("pooled start() must not run"))
    )
    try:
        _install_login()
        gw.stage_session_gateway(key, "dashboard", "alice")
        mgr._warm_pool.put_nowait((pool_crew, time.monotonic()))
        provider, _is_new, _resumed = await mgr.get_or_create(key, crew_agent="research")
        assert provider is not pool_crew
        assert pool_crew in killed
        assert started == [key]
        mgr.release(key)

        started.clear()
        killed.clear()
        default_key = "dashboard:crew-profile-default"
        pool_default = _reservation_provider(
            start=AsyncMock(side_effect=AssertionError("pooled start() must not run"))
        )
        mgr._warm_pool.put_nowait((pool_default, time.monotonic()))
        reused, _is_new, _resumed = await mgr.get_or_create(default_key)
        assert reused is pool_default
        assert pool_default not in killed
        assert started == []
        mgr.release(default_key)
    finally:
        reset_context()
        gp.reset_store()
        await mgr.close_all()


@pytest.mark.asyncio
async def test_install_error_preserves_pool_spawn_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Returning an unregistered claim must not reset the process's spawn age."""
    import time
    from unittest.mock import AsyncMock

    from kiro_crew.session import SessionManager

    key = "dashboard:pool-spawn-ttl"
    original_spawn = time.monotonic() - 120.0

    monkeypatch.setattr(
        SessionManager,
        "_apply_staged_gateway",
        AsyncMock(return_value=False),
    )

    async def _boom(self, session_key: str) -> None:
        raise RuntimeError("sidecar install boom")

    monkeypatch.setattr(SessionManager, "_install_staged_gateway_sidecar", _boom)

    def factory(session_key=None, agent=None, channel_id=None, **kwargs):
        raise AssertionError("factory must not run after install failure")

    mgr = SessionManager(KiroCrewConfig(), provider_factory=factory)
    mgr._pool_size = 1
    mgr._pool_agent = ""
    pool_p = _reservation_provider(start=AsyncMock())
    mgr._warm_pool.put_nowait((pool_p, original_spawn))
    try:
        before = time.monotonic()
        with pytest.raises(RuntimeError, match="sidecar install boom"):
            await mgr.get_or_create(key)
        assert mgr._warm_pool.qsize() == 1
        claimed, spawn = mgr._warm_pool.get_nowait()
        assert claimed is pool_p
        assert spawn == original_spawn
        assert spawn < before
        assert not mgr._creation_lock_for(key).locked()
    finally:
        await mgr.close_all()


def test_late_claim_reacquire_routes_successor_source() -> None:
    """Both late-claim reacquisitions must recheck the registry."""
    import inspect

    from kiro_crew.session_allocation import SessionAllocationService

    src = inspect.getsource(SessionAllocationService.get_or_create)
    helper = inspect.getsource(SessionAllocationService._reclaim_create_lock_after_late_claim)
    assert src.count("await self._reclaim_create_lock_after_late_claim") >= 2
    assert "self._sessions.get(key)" in helper
    assert "await owner._reacquire_and_validate" in helper
    assert "await owner._apply_staged_gateway_under_lease" in helper


@pytest.mark.asyncio
async def test_reclaim_create_lock_routes_successor() -> None:
    """A successor registered while the lock was released must keep its provider."""
    import asyncio
    from types import SimpleNamespace

    from kiro_crew.session_allocation import SessionAllocationService, SessionRegistryState

    previous = SimpleNamespace(provider="old")
    successor = SimpleNamespace(
        provider="new",
        first_turn=SimpleNamespace(is_new=False, resumed=True),
    )
    calls: list[str] = []

    class _Owner:
        _recycling: dict[str, object] = {}

        async def _reacquire_and_validate(self, key: str, session: object) -> bool:
            calls.append("validate")
            assert session is successor
            return True

        async def _apply_staged_gateway_under_lease(self, key: str, session: object) -> bool:
            calls.append("apply")
            assert session is successor
            return False

        async def _evict_stale_session(self, key: str, session: object) -> None:
            calls.append("evict")

    svc = SessionAllocationService.__new__(SessionAllocationService)
    svc._deps = SimpleNamespace(first_turn_nothing_armed="armed")
    svc.state = SessionRegistryState()
    svc.state.sessions["k"] = successor
    lock = asyncio.Lock()
    ready = await svc._reclaim_create_lock_after_late_claim(
        _Owner(), "k", lock, previous, speculative=False
    )
    assert ready == ("new", False, True)
    assert calls == ["validate", "apply"]
    assert not lock.locked()
    assert successor.first_turn == "armed"

    svc.state.sessions.clear()
    ready = await svc._reclaim_create_lock_after_late_claim(
        _Owner(), "k", lock, previous, speculative=False
    )
    assert ready is None
    assert lock.locked()
    lock.release()
