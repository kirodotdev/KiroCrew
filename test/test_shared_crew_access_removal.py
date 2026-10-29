"""Removing a person holds against a racing invite sync, a renamed login and a damaged file.

The access set's own state is stubbed where the race needs it; the lock, the
login record and the fail-closed snapshot are real.
"""

from __future__ import annotations

import asyncio
from typing import Any, Iterator

import pytest

from kiro_crew.dashboard import tailnet, tailnet_live_allowlist
from kiro_crew.shared_crew import access_set
from kiro_crew.shared_crew import tailscale_invite_store as store
from kiro_crew.shared_crew import tailscale_invites
from kiro_crew.shared_crew.contracts import Provenance
from kiro_crew.shared_crew.identity import tailnet_principal


@pytest.fixture(autouse=True)
def _fresh() -> Iterator[None]:
    tailnet_live_allowlist.reset_for_tests()
    access_set.reset_for_tests()
    yield
    tailnet_live_allowlist.reset_for_tests()
    access_set.reset_for_tests()


def _pid(login: str) -> str:
    return tailnet_principal(login, "", Provenance.TAILSCALE_SERVE).id


def test_removal_reaches_every_login_the_daemon_named_for_the_person() -> None:
    tailnet_live_allowlist.note_login("31", "Old@Example.com")
    tailnet_live_allowlist.note_login("31", "new@example.com")
    ids = access_set.principal_ids_for("old@example.com", frozenset({"31"}))
    assert ids == {_pid("old@example.com"), _pid("new@example.com")}
    assert access_set.principal_ids_for("", frozenset()) == frozenset()


def test_the_login_record_is_bounded_per_person() -> None:
    for n in range(20):
        tailnet_live_allowlist.note_login("31", f"u{n}@example.com")
    assert (
        len(tailnet_live_allowlist.logins_for("31")) == tailnet_live_allowlist._LOGINS_PER_USER_MAX
    )
    tailnet_live_allowlist.note_login("", "x@example.com")
    assert tailnet_live_allowlist.logins_for("") == frozenset()


@pytest.mark.asyncio
async def test_removal_waits_for_an_invite_commit_in_flight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = {"approvals": {"31": {"user_id": "31", "login": "m@example.com"}}}
    order: list[str] = []
    monkeypatch.setattr(access_set, "_state", lambda: state)
    monkeypatch.setattr(access_set, "_save", lambda _s: order.append("removed"))
    monkeypatch.setattr(store, "update_state", lambda _fn: None)
    monkeypatch.setattr(access_set, "rebuild", lambda: None)

    async def stop(*_a: Any, **_kw: Any) -> dict[str, int]:
        return {}

    monkeypatch.setattr(access_set, "stop_access", stop)
    lock = tailscale_invites._commit_lock()
    await lock.acquire()
    task = asyncio.create_task(access_set.remove_approval("31", "local:owner"))
    try:
        # Wait on the state, bounded: the removal queues behind the held lock,
        # and a removal that skips the lock finishes instead.
        for _ in range(1000):
            if lock._waiters or task.done():
                break
            await asyncio.sleep(0)
        assert not task.done(), "the removal ran without waiting for the commit lock"
        assert lock._waiters
        assert order == []
        order.append("commit done")
    finally:
        lock.release()
    await asyncio.wait_for(task, timeout=10)
    assert order == ["commit done", "removed"]


def test_a_damaged_access_file_admits_only_the_owner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    bad = tmp_path / "access.json"
    bad.write_bytes(b"\xff\xfe not utf-8")
    monkeypatch.setattr(access_set, "_path", lambda: bad)
    snap = access_set.install(
        ("alice@example.com",), owner_login="owner@example.com", owner_user_id="11"
    )
    assert snap.unreadable is True
    assert tailnet.login_allowed("alice@example.com", ("alice@example.com",)) is False
    assert tailnet.login_allowed("owner@example.com", ()) is True


def test_a_set_that_fails_to_build_still_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken() -> Any:
        raise RuntimeError("store exploded")

    monkeypatch.setattr(access_set, "_state", broken)
    snap = access_set.install(("alice@example.com",), owner_login="owner@example.com")
    assert snap.unreadable is True
    assert tailnet.login_allowed("alice@example.com", ("alice@example.com",)) is False


def test_a_login_removed_from_allowed_logins_leaves_the_seed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    monkeypatch.setattr(access_set, "_path", lambda: tmp_path / "access.json")
    monkeypatch.setattr(access_set, "_invite_logins", lambda: set())
    access_set.install(("alice@example.com", "bob@example.com"), owner_login="owner@example.com")
    assert tailnet.login_allowed("bob@example.com", ()) is True
    access_set.reset_for_tests()
    access_set.install(("alice@example.com", "carol@example.com"), owner_login="owner@example.com")
    assert tailnet.login_allowed("bob@example.com", ()) is False
    assert tailnet.login_allowed("alice@example.com", ()) is True
    # A login added after the first start admits nobody.
    assert tailnet.login_allowed("carol@example.com", ()) is False


def test_a_seed_withdrawal_the_disk_refuses_still_holds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """A store that reads but cannot write must not hand a withdrawn login back."""
    monkeypatch.setattr(access_set, "_path", lambda: tmp_path / "access.json")
    monkeypatch.setattr(access_set, "_invite_logins", lambda: set())
    access_set.install(("alice@example.com", "bob@example.com"), owner_login="owner@example.com")
    access_set.reset_for_tests()

    def read_only(*_a: Any, **_kw: Any) -> None:
        raise OSError("read-only file system")

    monkeypatch.setattr(access_set, "atomic_write", read_only)
    access_set.install(("alice@example.com",), owner_login="owner@example.com")
    assert tailnet.login_allowed("bob@example.com", ()) is False
    # A later rebuild (the owner learned after the bind) keeps it withdrawn.
    access_set.set_owner("owner@example.com", "11")
    assert tailnet.login_allowed("bob@example.com", ()) is False
    assert tailnet.login_allowed("alice@example.com", ()) is True


class _Pieces:
    """A request body that arrives in pieces: each ``read`` returns one."""

    def __init__(self, pieces: list[bytes]) -> None:
        self._pieces = list(pieces)

    async def read(self, n: int = -1) -> bytes:
        if not self._pieces:
            return b""
        piece = self._pieces.pop(0)
        if 0 <= n < len(piece):
            self._pieces.insert(0, piece[n:])
            piece = piece[:n]
        return piece


@pytest.mark.asyncio
async def test_an_avatar_body_split_across_reads_is_read_whole(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import base64
    import json

    from aiohttp.test_utils import make_mocked_request

    from kiro_crew.shared_crew import routes_invites

    async def owner(_r: Any) -> None:
        return None

    saved: list[bytes] = []
    monkeypatch.setattr(routes_invites, "_owner_refusal", owner)
    monkeypatch.setattr(store, "save_avatar", lambda data: saved.append(data) or "av-1")
    body = json.dumps({"data_base64": base64.b64encode(b"\x89PNG" + b"x" * 5000).decode()})
    raw = body.encode()
    request = make_mocked_request(
        "POST",
        "/api/shared-crew/tailscale/avatars",
        payload=_Pieces([raw[:10], raw[10:3000], raw[3000:]]),
    )
    resp = await routes_invites.api_upload_avatar(request)
    assert resp.status == 200
    assert saved == [b"\x89PNG" + b"x" * 5000]


@pytest.mark.asyncio
async def test_removal_revokes_a_grant_under_a_renamed_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = {"approvals": {"31": {"user_id": "31", "login": "old@example.com"}}}
    members = {"members": {"31": {"user_id": "31", "login": "new@example.com"}}}
    monkeypatch.setattr(access_set, "_state", lambda: state)
    monkeypatch.setattr(access_set, "_save", lambda _s: None)
    monkeypatch.setattr(store, "update_state", lambda fn: fn(members))
    monkeypatch.setattr(access_set, "rebuild", lambda: None)

    async def stop(*_a: Any, **_kw: Any) -> dict[str, int]:
        return {}

    monkeypatch.setattr(access_set, "stop_access", stop)
    tailnet_live_allowlist.grant("new@example.com")
    tailnet_live_allowlist.note_login("31", "aka@example.com")
    tailnet_live_allowlist.grant("aka@example.com")
    await access_set.remove_approval("31", "local:owner")
    assert tailnet_live_allowlist.decision("new@example.com") is False
    assert tailnet_live_allowlist.decision("aka@example.com") is False
    assert members["members"]["31"]["removed"] is True


@pytest.mark.asyncio
async def test_the_poller_syncs_slowly_with_nothing_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A console-created invite has no local row, so a round runs with none pending."""
    synced: list[bool] = []
    delays: list[float] = []

    class _Stop(Exception):
        pass

    async def sleep(delay: float) -> None:
        delays.append(delay)
        if len(delays) > 2:
            raise _Stop

    async def sync(**_kw: Any) -> dict[str, Any]:
        synced.append(True)
        return {}

    monkeypatch.setattr(store, "credential_present", lambda: True)
    monkeypatch.setattr(tailscale_invites, "pending_count", lambda: 0)
    monkeypatch.setattr(tailscale_invites, "sync", sync)
    with pytest.raises(_Stop):
        await tailscale_invites.poll_forever(sleep)
    assert synced == [True, True]
    assert delays == [
        tailscale_invites.POLL_BASE_SECS,
        tailscale_invites.POLL_MAX_SECS,
        tailscale_invites.POLL_MAX_SECS,
    ]


def test_a_revoked_seed_login_stays_out_after_a_restart(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """The config mirror may still name them; the seed does not."""
    monkeypatch.setattr(access_set, "_path", lambda: tmp_path / "access.json")
    monkeypatch.setattr(access_set, "_invite_logins", lambda: set())
    access_set.install(("ann@example.com",), owner_login="owner@example.com")
    access_set.drop_from_seed("Ann@Example.com")
    access_set.reset_for_tests()
    access_set.install(("ann@example.com",), owner_login="owner@example.com")
    assert tailnet.login_allowed("ann@example.com", ()) is False


def test_a_seed_that_cannot_be_saved_refuses_the_revoke(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    monkeypatch.setattr(access_set, "_path", lambda: tmp_path / "access.json")
    monkeypatch.setattr(access_set, "_invite_logins", lambda: set())
    access_set.install(("ann@example.com",), owner_login="owner@example.com")

    def broken(_state: Any) -> None:
        raise OSError("read-only")

    monkeypatch.setattr(access_set, "_save", broken)
    with pytest.raises(access_set.AccessError):
        access_set.drop_from_seed("ann@example.com")


def test_the_boot_placeholder_reads_nothing_and_admits_only_the_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_io() -> Any:
        raise AssertionError("the placeholder read the store")

    monkeypatch.setattr(access_set, "_state", no_io)
    snap = access_set.install_placeholder(local_authorization=True)
    assert snap.unreadable is True
    assert tailnet.login_allowed("alice@example.com", ("alice@example.com",)) is False


def test_the_headless_server_publishes_the_set_before_and_reads_it_after_the_bind() -> None:
    import inspect

    from kiro_crew.dashboard import server

    source = inspect.getsource(server.start_api_server)
    prepare = source.index("tailnet_direct_listener.prepare_access_set(_cfg)")
    bind = source.index("await _start_site(site, port)")
    finish = source.index("await tailnet_direct_listener.finish_access_set()")
    assert source.index("_install_api_middlewares(") < prepare < bind < finish


@pytest.mark.asyncio
async def test_an_approval_waits_for_a_removal_to_finish_stopping_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aiohttp.test_utils import make_mocked_request

    from kiro_crew.shared_crew import routes_access

    order: list[str] = []
    stopping = asyncio.Event()
    release = asyncio.Event()
    state = {"approvals": {"31": {"user_id": "31", "login": "m@example.com"}}}
    monkeypatch.setattr(access_set, "_state", lambda: state)
    monkeypatch.setattr(access_set, "_save", lambda _s: None)
    monkeypatch.setattr(store, "update_state", lambda _fn: None)
    monkeypatch.setattr(access_set, "rebuild", lambda: None)

    async def stop(*_a: Any, **_kw: Any) -> dict[str, int]:
        stopping.set()
        await release.wait()
        order.append("work stopped")
        return {}

    async def owner(_r: Any) -> None:
        return None

    monkeypatch.setattr(access_set, "stop_access", stop)
    monkeypatch.setattr(access_set, "approve", lambda uid, _a: order.append("approved") or {})
    monkeypatch.setattr(access_set, "local_authorization", lambda: True)
    monkeypatch.setattr(routes_access, "_owner_refusal", owner)
    monkeypatch.setattr(routes_access, "_actor", lambda _r: "local:owner")
    removal = asyncio.create_task(access_set.remove_approval("31", "local:owner"))
    await asyncio.wait_for(stopping.wait(), timeout=10)
    request = make_mocked_request("POST", "/x", match_info={"user_id": "31"})
    approval = asyncio.create_task(routes_access.api_approve(request))
    try:
        for _ in range(1000):
            if approval.done():
                break
            await asyncio.sleep(0)
        assert not approval.done(), "the approval ran while the removal was still stopping work"
    finally:
        release.set()
    await asyncio.wait_for(asyncio.gather(removal, approval), timeout=10)
    assert order == ["work stopped", "approved"]
