"""Tests for kernel-attested peer verification of internal-API session claims.

Covers the four layers of the unix-socket peer-identity feature:

* :mod:`kiro_crew.peer_resolve` — the shared /proc ancestry walk (the gatewayd
  wrapper seams are covered in ``test_mcp_gateway_claim.py``).
* ``dashboard.token_auth`` — the middleware branch: deny-on-mismatch,
  allow-on-match (with ``peer_verified`` set), status-quo on unresolvable,
  and the guarantee that TCP requests never engage the branch.
* ``dashboard.server._start_unix_site`` — POSIX-only bind with 0700 dir,
  Windows skip, degrade-to-TCP-only on bind failure.
* ``loopback_http`` — the stdlib unix-socket client transport and its
  fall-back-only-when-nothing-answered semantics.

The end-to-end test uses a real ``web.UnixSite`` + ``AF_UNIX`` connection so
the kernel populates real peer credentials (Linux ``SO_PEERCRED`` / macOS
``LOCAL_PEERPID``); pure-unit tests fake the socketsec seams instead so they
run identically on every platform.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import socket
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from loopback_servers import LoopbackThreadingHTTPServer
from tmpdir_helpers import SHORT_TMP_PREFIX, short_tmp_base

import kiro_crew.dashboard.token_auth as ta
from kiro_crew import platform_compat
from kiro_crew.loopback_http import loopback_urlopen
from kiro_crew.mcp_gateway.socketsec import PeerCredResult
from kiro_crew.peer_resolve import PeerTenancy, resolve_peer_identity, resolve_peer_tenancy


@pytest.fixture
def short_sock_dir() -> "Iterator[Path]":
    """A SHORT base for every AF_UNIX bind/connect path in this module.

    ``sun_path`` caps at 104 bytes on macOS, which a deep pytest ``tmp_path``
    exceeds. Removed at teardown because ``mkdtemp`` registers no finalizer.
    """
    base = Path(tempfile.mkdtemp(prefix=SHORT_TMP_PREFIX + "peer-", dir=short_tmp_base()))
    yield base
    shutil.rmtree(base, ignore_errors=True)


SECRET = "test-internal-secret"
INTERNAL = frozenset({"/api/spawn"})


# peer_resolve — the shared ancestry walk


def _ppid_map(mapping: dict[int, int]):
    def _fn(pid: int) -> int:
        return mapping.get(pid, 0)

    return _fn


def test_resolve_peer_identity_finds_key_and_chain(tmp_path: Path) -> None:
    (tmp_path / "session_pid_50.txt").write_text("dashboard:chat-1-abc", encoding="utf-8")
    key, chain = resolve_peer_identity(
        100,
        config_dir_fn=lambda: tmp_path,
        ppid_fn=_ppid_map({100: 50, 50: 20, 20: 1}),
    )
    assert key == "dashboard:chat-1-abc"
    assert chain == [100, 50, 20]


def test_resolve_peer_identity_no_pidfile_returns_empty_key(tmp_path: Path) -> None:
    key, chain = resolve_peer_identity(
        300, config_dir_fn=lambda: tmp_path, ppid_fn=_ppid_map({300: 1})
    )
    assert key == ""
    assert chain == [300]


def test_resolve_peer_identity_config_dir_error_degrades(tmp_path: Path) -> None:
    def _boom() -> Path:
        raise RuntimeError("boom")

    assert resolve_peer_identity(999, config_dir_fn=_boom, ppid_fn=_ppid_map({})) == ("", [])


def test_resolve_peer_identity_cycle_terminates(tmp_path: Path) -> None:
    """A pid cycle (possible with pid reuse mid-walk) must not loop forever."""
    key, chain = resolve_peer_identity(
        10, config_dir_fn=lambda: tmp_path, ppid_fn=_ppid_map({10: 20, 20: 10})
    )
    assert key == ""
    assert chain == [10, 20]


def test_signed_only_refuses_forged_unsigned_mapping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bare .txt is same-uid agent-writable,
    so an attacker planting session_pid_<own_pid>.txt with a victim's key
    must NOT satisfy the authorization walk — signed_only requires the HMAC
    sidecar the attacker cannot produce."""
    from kiro_crew import session_pid_sig as sps

    monkeypatch.setattr(sps, "_load_hmac_key", lambda: b"K" * 32)
    (tmp_path / "session_pid_50.txt").write_text("dashboard:chat-victim", encoding="utf-8")
    key, chain = resolve_peer_identity(
        50, config_dir_fn=lambda: tmp_path, ppid_fn=_ppid_map({50: 1}), signed_only=True
    )
    assert key == ""  # unsigned mapping refused
    assert chain == [50]
    # ...and a forged sidecar (wrong MAC) is refused too.
    (tmp_path / "session_pid_50.sig").write_text("0" * 64, encoding="utf-8")
    key, _ = resolve_peer_identity(
        50, config_dir_fn=lambda: tmp_path, ppid_fn=_ppid_map({50: 1}), signed_only=True
    )
    assert key == ""


@pytest.mark.parametrize("sidecar", ["é" * 64, "0" * 63 + "é"])
def test_signed_only_reports_a_non_hex_sidecar_as_unverifiable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sidecar: str
) -> None:
    """A sidecar holding non-ASCII text is a MAC mismatch the walk carries out.

    It must not raise: ``resolve_peer_tenancy`` catches only ``OSError``, so a
    ``TypeError`` from comparing the MAC as ``str`` would reach the middleware's
    resolver guard and read as "no mapping anywhere" -- the degrade arm --
    instead of the token demand an untrustable mapping gets.
    """
    from kiro_crew import session_pid_sig as sps

    monkeypatch.setattr(sps, "_load_hmac_key", lambda: b"K" * 32)
    (tmp_path / "session_pid_50.txt").write_text("dashboard:chat-victim", encoding="utf-8")
    (tmp_path / "session_pid_50.sig").write_text(sidecar, encoding="utf-8")
    tenancy = resolve_peer_tenancy(
        50, config_dir_fn=lambda: tmp_path, ppid_fn=_ppid_map({50: 1}), signed_only=True
    )
    assert tenancy.session_key == ""
    assert tenancy.unverifiable is True


def test_signed_only_accepts_gateway_signed_mapping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew import session_pid_sig as sps

    _hmac_key = b"K" * 32
    monkeypatch.setattr(sps, "_load_hmac_key", lambda: _hmac_key)
    (tmp_path / "session_pid_50.txt").write_text("dashboard:chat-1", encoding="utf-8")
    (tmp_path / "session_pid_50.sig").write_text(
        sps._compute_sig(_hmac_key, 50, "dashboard:chat-1"), encoding="utf-8"
    )
    key, chain = resolve_peer_identity(
        100,
        config_dir_fn=lambda: tmp_path,
        ppid_fn=_ppid_map({100: 50, 50: 1}),
        signed_only=True,
    )
    assert key == "dashboard:chat-1"
    assert chain == [100, 50]


# token_auth middleware — unit tests with faked socketsec seams


class _FakeUnixSock:
    # getattr guard: socket.AF_UNIX does not exist on Windows CPython; module
    # collection must still succeed there (unit tests fake the family value —
    # the middleware compares against the same getattr-resolved constant).
    family = getattr(socket, "AF_UNIX", None)


def _make_request(
    path: str = "/api/spawn",
    headers: dict | None = None,
    remote: str | None = None,
    unix: bool = False,
) -> tuple[MagicMock, dict]:
    """Mock request + the dict backing its item assignment (``store``)."""
    req = MagicMock(spec=web.Request)
    req.path = path
    req.query = {}
    req.cookies = {}
    req.remote = remote if remote is not None else ("" if unix else "127.0.0.1")
    req.headers = headers or {}
    req.method = "POST"
    store: dict = {}
    req.__setitem__.side_effect = store.__setitem__
    req.__getitem__.side_effect = store.__getitem__
    transport = MagicMock()
    transport.get_extra_info = (
        (lambda name: _FakeUnixSock() if name == "socket" else None)
        if unix
        else (lambda name: None)
    )
    req.transport = transport
    return req, store


async def _ok_handler(request: web.Request) -> web.Response:
    return web.Response(text="ok")


# The middleware deliberately never engages on Windows (AF_UNIX resolves to
# None and the server binds no UnixSite), so a faked unix-transport request —
# an impossible shape there — is treated as plain non-loopback and denied by
# the ordinary internal-path rules. Every test that fakes a unix transport is
# therefore POSIX-only, engagement and pass-through alike.
_posix_only = pytest.mark.skipif(
    platform_compat.IS_WINDOWS, reason="AF_UNIX transport is POSIX-only"
)


def _wire_peer(
    monkeypatch: pytest.MonkeyPatch,
    *,
    verdict: PeerCredResult = PeerCredResult.MATCH,
    peer_pid: int | None = 4242,
    resolved: str = "",
    tenants: tuple[str, ...] = (),
    tenant_count: int = 0,
    attests: str = "",
    unverifiable: bool = False,
    live_keys: tuple[str, ...] = (),
    bound_keys: tuple[str, ...] = (),
) -> list[dict]:
    """Fake socketsec + resolver seams; return the captured SEL calls.

    ``resolved`` is the ONE session the peer's pid names (a shared pid names none);
    ``tenants``/``tenant_count`` are the recorded membership checked instead.
    ``attests`` is the session key the per-session token verifies to, empty for a
    missing or forged token.

    ``bound_keys`` is what the LEASE and TENANCY tables place on the peer's ancestry;
    empty means no evidence, so the binding check stays silent. The helper's own
    contract is pinned separately, because this seam stubs it out.
    """
    calls: list[dict] = []

    class _FakeSel:
        def log_api_access(self, **kw):
            calls.append(kw)

    tenancy = PeerTenancy(
        session_key=resolved,
        tenants=tenants,
        tenant_count=tenant_count,
        chain=[peer_pid] if peer_pid is not None else [],
        unverifiable=unverifiable,
    )
    monkeypatch.setattr(ta, "_sel_fn", lambda: _FakeSel())
    monkeypatch.setattr(ta, "check_peer_is_self", lambda sock: verdict)
    monkeypatch.setattr(ta, "get_peer_pid", lambda sock: peer_pid)
    monkeypatch.setattr(ta, "resolve_peer_tenancy", lambda pid, **kw: tenancy)
    monkeypatch.setattr(
        ta, "_live_session_keys_on_chain", lambda request, chain: frozenset(live_keys)
    )
    monkeypatch.setattr(ta, "verify_session_token", lambda token: attests if token else "")
    monkeypatch.setattr(ta, "_bound_session_keys_on_chain", lambda chain: frozenset(bound_keys))
    return calls


@_posix_only
@pytest.mark.asyncio
async def test_unix_peer_match_allowed_and_marked(monkeypatch: pytest.MonkeyPatch) -> None:
    _wire_peer(monkeypatch, resolved="dashboard:chat-1-abc")
    mw = ta.token_auth_middleware(internal_paths=INTERNAL, internal_secret=SECRET)
    req, store = _make_request(
        headers={"X-Internal-Secret": SECRET, "X-Session-Key": "dashboard:chat-1-abc"},
        unix=True,
    )
    resp = await mw(req, _ok_handler)
    assert resp.status == 200
    assert store.get("peer_verified") is True


@_posix_only
@pytest.mark.asyncio
async def test_unix_peer_mismatch_denied_with_sel(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _wire_peer(monkeypatch, resolved="dashboard:chat-1-abc")
    mw = ta.token_auth_middleware(internal_paths=INTERNAL, internal_secret=SECRET)
    req, store = _make_request(
        headers={"X-Internal-Secret": SECRET, "X-Session-Key": "dashboard:chat-9-EVIL"},
        unix=True,
    )
    resp = await mw(req, _ok_handler)
    assert resp.status == 403
    assert json.loads(resp.body) == {
        "error": "Forbidden",
        "code": "peer_session_mismatch",
    }
    mismatches = [c for c in calls if c.get("operation") == "dashboard.peer-identity-mismatch"]
    assert len(mismatches) == 1
    assert mismatches[0]["outcome"] == "denied"
    assert "peer_pid=4242" in mismatches[0]["error"]


@_posix_only
@pytest.mark.asyncio
async def test_unix_peer_mismatch_denied_even_with_wrong_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The peer check runs before either auth flavor — impersonation is denied
    regardless of what credentials the caller carries."""
    _wire_peer(monkeypatch, resolved="dashboard:chat-1-abc")
    mw = ta.token_auth_middleware(internal_paths=INTERNAL, internal_secret=SECRET)
    req, store = _make_request(
        headers={"X-Internal-Secret": "wrong", "X-Session-Key": "dashboard:chat-9-EVIL"},
        unix=True,
    )
    resp = await mw(req, _ok_handler)
    assert resp.status == 403


@_posix_only
@pytest.mark.asyncio
async def test_unix_peer_unresolvable_proceeds_status_quo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty resolved key (warm pool before claim, cron, pooled backend) must
    keep today's semantics: valid secret grants."""
    _wire_peer(monkeypatch, resolved="")
    mw = ta.token_auth_middleware(internal_paths=INTERNAL, internal_secret=SECRET)
    req, store = _make_request(
        headers={"X-Internal-Secret": SECRET, "X-Session-Key": "dashboard:chat-1-abc"},
        unix=True,
    )
    resp = await mw(req, _ok_handler)
    assert resp.status == 200
    assert "peer_verified" not in store


# A SHARED pid: one kiro-cli process, several ACP sessions
# A pid names a PROCESS, so on a shared runtime it names none of the sessions
# on it. Comparing a declared key against the one key the mapping holds then
# denies whichever co-tenant did not publish last — a legitimate session, on
# its own internal API call. The recorded membership is what turns that into a
# real check: a declared key IN the set is kernel-attested as living on this
# peer's process, and one outside it is the impersonation the check exists for.

_SHARED = ("dashboard:chat-1-abc", "subagent:chat-1-abc-child")


@_posix_only
@pytest.mark.asyncio
async def test_unix_peer_admits_a_recorded_co_tenant(monkeypatch: pytest.MonkeyPatch) -> None:
    """The co-tenant that did NOT publish the mapping is still on this process.

    It proves WHICH co-tenant it is with its own per-session token; membership
    alone cannot, because every session on the pid can read the same list.
    """
    calls = _wire_peer(
        monkeypatch, resolved="", tenants=_SHARED, tenant_count=2, attests=_SHARED[1]
    )
    mw = ta.token_auth_middleware(internal_paths=INTERNAL, internal_secret=SECRET)
    req, store = _make_request(
        headers={
            "X-Internal-Secret": SECRET,
            "X-Session-Key": _SHARED[1],
            "X-Session-Token": "tok-for-the-second-tenant",
        },
        unix=True,
    )
    resp = await mw(req, _ok_handler)
    assert resp.status == 200
    # Positively attested, not merely un-denied.
    assert store.get("peer_verified") is True
    assert not [c for c in calls if c.get("outcome") == "denied"]
    # And RECORDED: this is the arm a cross-session declaration would have used,
    # so an investigation needs a trail of who was admitted on a shared pid.
    allowed = [c for c in calls if c.get("operation") == "dashboard.peer-identity-co-tenant"]
    assert allowed
    # The record names the roster (complete vs truncated) and whether the ownership
    # tables backed the token; they are silent for anything this gateway did not place.
    assert allowed[0]["error"] == "roster complete; binding=no evidence"


@_posix_only
@pytest.mark.asyncio
async def test_unix_peer_denies_a_co_tenant_declaring_a_siblings_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recording the membership must not let one tenant BE another.

    Membership and the kernel attestation both pass for the attacker, and the
    roster is readable by every co-tenant; only the token names the real holder.
    """
    calls = _wire_peer(
        monkeypatch, resolved="", tenants=_SHARED, tenant_count=2, attests=_SHARED[0]
    )
    mw = ta.token_auth_middleware(internal_paths=INTERNAL, internal_secret=SECRET)
    req, store = _make_request(
        headers={
            "X-Internal-Secret": SECRET,
            # Declares the SIBLING while holding its own token.
            "X-Session-Key": _SHARED[1],
            "X-Session-Token": "tok-for-the-first-tenant",
        },
        unix=True,
    )
    resp = await mw(req, _ok_handler)
    assert resp.status == 403
    assert json.loads(resp.body)["code"] == "peer_session_unattested"
    assert store.get("peer_verified") is not True
    assert [c for c in calls if c.get("outcome") == "denied"]


@_posix_only
@pytest.mark.asyncio
async def test_unix_peer_denies_a_recorded_co_tenant_with_no_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A recorded co-tenant on a shared pid with no token is refused."""
    calls = _wire_peer(
        monkeypatch, resolved="", tenants=_SHARED, tenant_count=2, attests=_SHARED[1]
    )
    mw = ta.token_auth_middleware(internal_paths=INTERNAL, internal_secret=SECRET)
    req, store = _make_request(
        headers={"X-Internal-Secret": SECRET, "X-Session-Key": _SHARED[1]},
        unix=True,
    )
    resp = await mw(req, _ok_handler)
    assert resp.status == 403
    assert json.loads(resp.body)["code"] == "peer_session_unattested"
    assert store.get("peer_verified") is not True
    assert [c for c in calls if c.get("outcome") == "denied"]


@_posix_only
@pytest.mark.asyncio
async def test_unix_peer_on_a_sole_tenant_pid_needs_no_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The 1:1 path is untouched, token or no token.

    One session lives on that pid, so the kernel's process attestation already
    names it and there is no sibling to be mistaken for.
    """
    calls = _wire_peer(monkeypatch, resolved="dashboard:chat-1-abc", attests="")
    mw = ta.token_auth_middleware(internal_paths=INTERNAL, internal_secret=SECRET)
    req, store = _make_request(
        headers={"X-Internal-Secret": SECRET, "X-Session-Key": "dashboard:chat-1-abc"},
        unix=True,
    )
    resp = await mw(req, _ok_handler)
    assert resp.status == 200
    assert store.get("peer_verified") is True
    assert not [c for c in calls if c.get("outcome") == "denied"]


@_posix_only
@pytest.mark.asyncio
async def test_unix_peer_denies_a_key_that_is_not_a_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sharing widens the admitted set; it does not remove the check."""
    calls = _wire_peer(monkeypatch, resolved="", tenants=_SHARED, tenant_count=2)
    mw = ta.token_auth_middleware(internal_paths=INTERNAL, internal_secret=SECRET)
    req, store = _make_request(
        headers={"X-Internal-Secret": SECRET, "X-Session-Key": "dashboard:chat-9-EVIL"},
        unix=True,
    )
    resp = await mw(req, _ok_handler)
    assert resp.status == 403
    assert json.loads(resp.body)["code"] == "peer_session_mismatch"
    assert [c for c in calls if c.get("outcome") == "denied"]


@_posix_only
@pytest.mark.asyncio
async def test_unix_peer_demands_a_token_when_membership_is_incomplete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A truncated roster withholds membership evidence, not the attestation.

    The size-bounded mapping file can record the count without every key. The
    count still proves the pid is shared, so the token is still required, or
    overflowing the roster would skip the check.
    """
    calls = _wire_peer(monkeypatch, resolved="", tenants=(), tenant_count=2)
    mw = ta.token_auth_middleware(internal_paths=INTERNAL, internal_secret=SECRET)
    req, store = _make_request(
        headers={"X-Internal-Secret": SECRET, "X-Session-Key": _SHARED[1]},
        unix=True,
    )
    resp = await mw(req, _ok_handler)
    assert resp.status == 403
    assert json.loads(resp.body)["code"] == "peer_session_unattested"
    assert "peer_verified" not in store
    denied = [c for c in calls if c.get("outcome") == "denied"]
    assert denied and denied[0]["operation"] == "dashboard.peer-identity-unattested"
    # The record must say which roster the decision was made against, or an
    # investigation cannot tell this arm from the complete-roster one.
    assert "truncated" in denied[0]["error"]


@_posix_only
@pytest.mark.asyncio
async def test_unix_peer_admits_an_unenumerated_co_tenant_with_its_own_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The token, not the roster, is what admits a member the set omitted.

    A session dropped from the truncated roster still gets in on its own token,
    so the size bound is not a 403 for every session past the budget.
    """
    calls = _wire_peer(monkeypatch, resolved="", tenants=(), tenant_count=2, attests=_SHARED[1])
    mw = ta.token_auth_middleware(internal_paths=INTERNAL, internal_secret=SECRET)
    req, store = _make_request(
        headers={
            "X-Internal-Secret": SECRET,
            "X-Session-Key": _SHARED[1],
            "X-Session-Token": "tok",
        },
        unix=True,
    )
    resp = await mw(req, _ok_handler)
    assert resp.status == 200
    assert store.get("peer_verified") is True
    allowed = [c for c in calls if c.get("outcome") == "allowed"]
    assert allowed and allowed[0]["operation"] == "dashboard.peer-identity-co-tenant"
    assert "truncated" in allowed[0]["error"]


@_posix_only
@pytest.mark.asyncio
async def test_unix_peer_unresolved_tenancy_still_proceeds_without_a_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No tenancy evidence at all must stay the degrade arm, token or not.

    A count of zero means no mapping resolved. Demanding a token here would deny
    warm-pool runtimes before claim, cron scripts and pooled MCP backends.
    """
    calls = _wire_peer(monkeypatch, resolved="", tenants=(), tenant_count=0)
    mw = ta.token_auth_middleware(internal_paths=INTERNAL, internal_secret=SECRET)
    req, store = _make_request(
        headers={"X-Internal-Secret": SECRET, "X-Session-Key": _SHARED[1]},
        unix=True,
    )
    resp = await mw(req, _ok_handler)
    assert resp.status == 200
    assert "peer_verified" not in store
    assert not [c for c in calls if c.get("outcome") == "denied"]


# The token must also name a session the process still HOSTS
# A same-uid co-tenant can lift a sibling's token from /proc/<pid>/environ. The
# ownership tables live in the gateway's memory, so the declared key must also
# hold a live lease or tenancy on this pid.


class _StubRuntime:
    """A runtime for the table tests: a pid and a liveness answer, nothing else.

    A real object rather than a ``MagicMock`` on purpose -- the tables reject a
    mock's pid (it coerces to 1 through ``__index__``) and read ``is_alive`` as a
    truthy mock whatever the process is doing.
    """

    def __init__(self, pid: int, alive: bool = True) -> None:
        self.pid = pid
        self._alive = alive

    def is_alive(self) -> bool:
        return self._alive


@_posix_only
@pytest.mark.asyncio
async def test_unix_peer_admits_a_co_tenant_its_tables_still_place_here(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Token names the session AND a table places it on the pid: admitted.

    The ordinary shared-runtime call. Both questions are answered positively, so
    the record says the binding was confirmed rather than merely unchallenged.
    """
    calls = _wire_peer(
        monkeypatch,
        resolved="",
        tenants=_SHARED,
        tenant_count=2,
        attests=_SHARED[1],
        bound_keys=_SHARED,
    )
    mw = ta.token_auth_middleware(internal_paths=INTERNAL, internal_secret=SECRET)
    req, store = _make_request(
        headers={
            "X-Internal-Secret": SECRET,
            "X-Session-Key": _SHARED[1],
            "X-Session-Token": "tok-for-the-second-tenant",
        },
        unix=True,
    )
    resp = await mw(req, _ok_handler)
    assert resp.status == 200
    assert store.get("peer_verified") is True
    assert not [c for c in calls if c.get("outcome") == "denied"]
    allowed = [c for c in calls if c.get("operation") == "dashboard.peer-identity-co-tenant"]
    assert allowed and allowed[0]["error"] == "roster complete; binding=confirmed"


@_posix_only
@pytest.mark.asyncio
async def test_unix_peer_denies_a_token_for_a_session_the_pid_no_longer_hosts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid token for a session whose claim is gone is refused.

    The stolen-token case: the token verifies to a listed sibling, so every other
    check passes, but the tables place that sibling nowhere near this process.
    """
    calls = _wire_peer(
        monkeypatch,
        resolved="",
        tenants=_SHARED,
        tenant_count=2,
        attests=_SHARED[1],
        # The table names the OTHER session on the pid and not the declared one:
        # non-empty, so it is speaking, and the declared key is absent from what
        # it says.
        bound_keys=(_SHARED[0],),
    )
    mw = ta.token_auth_middleware(internal_paths=INTERNAL, internal_secret=SECRET)
    req, store = _make_request(
        headers={
            "X-Internal-Secret": SECRET,
            "X-Session-Key": _SHARED[1],
            "X-Session-Token": "a-token-lifted-from-the-siblings-environment",
        },
        unix=True,
    )
    resp = await mw(req, _ok_handler)
    assert resp.status == 403
    assert json.loads(resp.body)["code"] == "peer_session_unbound"
    assert "peer_verified" not in store
    denied = [c for c in calls if c.get("operation") == "dashboard.peer-session-unbound"]
    assert len(denied) == 1
    assert denied[0]["outcome"] == "denied" and denied[0]["caller"] == _SHARED[1]
    # The record carries the pid, the roster judged, and how many sessions WERE
    # bound there -- enough to tell a released claim from a wrong process.
    assert "peer_pid=4242" in denied[0]["error"]
    assert "roster complete" in denied[0]["error"] and "1 bound there" in denied[0]["error"]


@_posix_only
@pytest.mark.asyncio
async def test_unix_peer_silent_tables_keep_todays_semantics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tables that cannot answer are no evidence, so nothing new is denied.

    The tables only cover runtimes THIS gateway placed, so warm-pool runtimes,
    pooled MCP backends and cron scripts are absent while legitimate. The token
    check still holds.
    """
    calls = _wire_peer(
        monkeypatch,
        resolved="",
        tenants=_SHARED,
        tenant_count=2,
        attests=_SHARED[1],
        bound_keys=(),
    )
    mw = ta.token_auth_middleware(internal_paths=INTERNAL, internal_secret=SECRET)
    req, store = _make_request(
        headers={
            "X-Internal-Secret": SECRET,
            "X-Session-Key": _SHARED[1],
            "X-Session-Token": "tok",
        },
        unix=True,
    )
    resp = await mw(req, _ok_handler)
    assert resp.status == 200
    assert store.get("peer_verified") is True
    assert not [c for c in calls if c.get("code") == "peer_session_unbound"]
    assert not [c for c in calls if c.get("outcome") == "denied"]
    allowed = [c for c in calls if c.get("operation") == "dashboard.peer-identity-co-tenant"]
    assert allowed and allowed[0]["error"].endswith("binding=no evidence")


def test_bound_session_keys_union_both_tables_and_walk_the_chain() -> None:
    """The helper's own contract, because the arm tests stub it out.

    Three properties in one place: both tables are read (a sharing sub-agent has
    only a tenancy, a chat session only a lease), the chain is walked peer-first
    so the answer comes from the kiro-cli ANCESTOR rather than the MCP stub that
    connected, and a pid in neither table answers empty.
    """
    from kiro_crew import runtime_ownership as ro

    ro._reset_for_tests()
    try:
        runtime = _StubRuntime(pid=9100)
        ro.RUNTIME_OWNERSHIP._entries.append(
            ro._Entry(runtime=runtime, key="k", leases={"lease-1": "dashboard:chat-1-abc"})
        )
        assert ro.RUNTIME_TENANCY.claim(
            runtime, holder="subagent:x", session_key="subagent:chat-1-abc-child"
        )
        # The stub pid is NOT in either table; its ancestor is, and that is the
        # entry that decides.
        assert ta._bound_session_keys_on_chain([9999, 9100]) == frozenset(
            {"dashboard:chat-1-abc", "subagent:chat-1-abc-child"}
        )
        assert ta._bound_session_keys_on_chain([4242]) == frozenset()
    finally:
        ro._reset_for_tests()


@pytest.mark.asyncio
async def test_a_shared_turn_binds_the_session_its_stubs_declare() -> None:
    """The wiring: a sharing sub-agent's turn records ITS OWN key, not a label.

    A sub-agent holds no lease, so the turn's tenancy is the only entry naming it,
    and it must use the key its stubs send in ``X-Session-Key``. Measured before,
    during and after the turn.
    """
    from kiro_crew import runtime_ownership as ro
    from kiro_crew.acp.session_provider import AcpSessionProvider

    principal, child = _SHARED
    ro._reset_for_tests()
    try:
        runtime = _StubRuntime(pid=9400)
        runtime._mcp_gateway_socket = ""  # type: ignore[attr-defined]
        ro.RUNTIME_OWNERSHIP._entries.append(
            ro._Entry(runtime=runtime, key="k", leases={"lease-1": principal})
        )

        handle = SimpleNamespace(session_id="s", stub_session_token="", prompt=None)
        provider = AcpSessionProvider(handle, runtime, session_key=child)
        provider._owns_runtime = False

        during: list[frozenset[str]] = []

        async def _fake_stream(message, prompt, incarnation):
            during.append(ro.session_keys_bound_to_pid(9400))
            yield "event"

        provider.essential_delivery = SimpleNamespace(stream=_fake_stream)  # type: ignore[assignment]
        provider.reclaim = lambda: None  # type: ignore[method-assign]

        before = ro.session_keys_bound_to_pid(9400)
        async for _ in provider.stream("hi"):
            pass
        after = ro.session_keys_bound_to_pid(9400)

        assert during == [frozenset({principal, child})]
        # The principal's lease spans the whole session; the turn's tenancy does
        # not, which is the scope D10 chose and the reason this is not a lease.
        assert before == frozenset({principal})
        assert after == frozenset({principal})
    finally:
        ro._reset_for_tests()


def test_bound_session_keys_report_no_evidence_when_a_table_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A probe that could not ask must not become a verdict."""

    def _boom(pid: int) -> frozenset[str]:
        raise RuntimeError("registry unreadable")

    monkeypatch.setattr(ta, "session_keys_bound_to_pid", _boom)
    assert ta._bound_session_keys_on_chain([4242]) == frozenset()


def test_a_tenancy_with_no_session_key_binds_nothing() -> None:
    """The mint child holds a claim and declares no session, so it binds none.

    Its claim still defends the process for the kill gate, but the OAuth child
    must not read as a tenant whose key could be declared.
    """
    from kiro_crew import runtime_ownership as ro

    ro._reset_for_tests()
    try:
        runtime = _StubRuntime(pid=9200)
        assert ro.RUNTIME_TENANCY.claim(runtime, holder="mint:connect-flow")
        assert ro.RUNTIME_TENANCY.claims_on_pid(9200) == 1
        assert ro.RUNTIME_TENANCY.tenant_keys_on_pid(9200) == frozenset()
        assert ro.session_keys_bound_to_pid(9200) == frozenset()
    finally:
        ro._reset_for_tests()


def test_a_dead_runtime_binds_no_session() -> None:
    """Death releases ownership, so a dead process names nobody.

    Same rule the kill gate's counts follow, and it matters here too: a caller
    declaring a key on a pid whose runtime has exited is not on that process, and
    the OS is free to hand the number to something else.
    """
    from kiro_crew import runtime_ownership as ro

    ro._reset_for_tests()
    try:
        runtime = _StubRuntime(pid=9300, alive=False)
        ro.RUNTIME_OWNERSHIP._entries.append(
            ro._Entry(runtime=runtime, key="k", leases={"lease-1": "dashboard:chat-1-abc"})
        )
        assert ro.RUNTIME_TENANCY.claim(
            runtime, holder="subagent:x", session_key="subagent:chat-1-abc-child"
        )
        assert ro.session_keys_bound_to_pid(9300) == frozenset()
    finally:
        ro._reset_for_tests()


def _app_with_rows(rows: list[dict] | Exception):
    """A request whose ``app["state"].sessions.runtime_pids()`` answers *rows*."""

    class _Sessions:
        def runtime_pids(self):
            if isinstance(rows, Exception):
                raise rows
            return rows

    req = MagicMock(spec=web.Request)
    req.app = {"state": SimpleNamespace(sessions=_Sessions())}
    return req


def test_live_session_keys_name_sessions_only_and_walk_the_chain() -> None:
    """The helper's own contract, because the arm tests stub it out.

    It returns KEYS, not a count, so the arm can check the DECLARED key. It takes
    only SESSION rows (companion RUNTIME rows carry display text and repeat a
    session's pid), and walks the chain peer-first to reach the kiro-cli ANCESTOR.
    """
    rows = [
        {"key": "dashboard:chat-1", "sid": "s1", "pid": 120},
        {"key": "subagent:chat-1-child", "sid": "s2", "pid": 120},
        {"key": "Subagent runtime (dashboard:chat-1)", "pid": 120},
        {"key": "dashboard:chat-9", "sid": "s9", "pid": 999},
    ]
    req = _app_with_rows(rows)

    # peer 130 is unknown; its ancestor 120 hosts two SESSIONS (not three), and
    # the display-text runtime row is not one of them.
    assert ta._live_session_keys_on_chain(req, [130, 120, 110]) == frozenset(
        {"dashboard:chat-1", "subagent:chat-1-child"}
    )
    # A pid hosting one session names it, which is not evidence of sharing.
    assert ta._live_session_keys_on_chain(req, [999]) == frozenset({"dashboard:chat-9"})
    # Nothing in the chain is known: no evidence.
    assert ta._live_session_keys_on_chain(req, [7, 8, 9]) == frozenset()


def test_live_session_keys_report_no_evidence_when_the_manager_is_unreachable() -> None:
    """An unanswerable manager must read as "no evidence", never as sharing: this
    runs on every request that reaches the unresolvable arm, so a raising probe
    turning into a denial would take down every tokenless caller at once."""
    assert (
        ta._live_session_keys_on_chain(_app_with_rows(RuntimeError("no manager")), [120])
        == frozenset()
    )
    assert ta._live_session_keys_on_chain(MagicMock(spec=web.Request), [120]) == frozenset()


@_posix_only
@pytest.mark.asyncio
async def test_a_deleted_mapping_cannot_reach_the_degrade_arm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HEADLINE: an UNLINK must not buy the attestation-free path.

    The mapping pair is writable inside the agent sandbox, so a co-tenant that read
    the roster can delete it to reach the permissive arm. The live session manager
    answers from memory, so it is the account of tenancy an agent cannot edit.
    """
    calls = _wire_peer(monkeypatch, resolved="", tenants=(), tenant_count=0, live_keys=_SHARED)
    mw = ta.token_auth_middleware(internal_paths=INTERNAL, internal_secret=SECRET)
    req, store = _make_request(
        headers={"X-Internal-Secret": SECRET, "X-Session-Key": _SHARED[1]},
        unix=True,
    )
    resp = await mw(req, _ok_handler)
    assert resp.status == 403
    assert json.loads(resp.body)["code"] == "peer_session_unattested"
    denied = [c for c in calls if c.get("outcome") == "denied"]
    assert denied and "manager reports several" in denied[0]["error"]
    assert "peer_verified" not in store


@_posix_only
@pytest.mark.asyncio
async def test_a_deleted_mapping_still_admits_a_caller_with_its_own_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The converse: the remedy is attestation, not denial. A legitimate session
    on a shared runtime whose mapping is momentarily gone still gets through on
    its own token, so this cannot become an outage whenever the pair is absent."""
    calls = _wire_peer(
        monkeypatch, resolved="", tenant_count=0, live_keys=_SHARED, attests=_SHARED[1]
    )
    mw = ta.token_auth_middleware(internal_paths=INTERNAL, internal_secret=SECRET)
    req, store = _make_request(
        headers={
            "X-Internal-Secret": SECRET,
            "X-Session-Key": _SHARED[1],
            "X-Session-Token": "a-token-naming-the-declared-key",
        },
        unix=True,
    )
    resp = await mw(req, _ok_handler)
    assert resp.status == 200
    assert store.get("peer_verified") is True
    allowed = [c for c in calls if c.get("outcome") == "allowed"]
    assert allowed and "manager reports several" in allowed[0]["error"]


@_posix_only
@pytest.mark.asyncio
async def test_a_manager_that_knows_nothing_still_degrades(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The must-not-harden direction: an EMPTY key set is no evidence at all.

    Nothing on disk and no session on the ancestry is the ordinary state of an
    unclaimed caller (a cron script, a warm-pool runtime, a pooled backend).
    """
    calls = _wire_peer(monkeypatch, resolved="", tenant_count=0, live_keys=())
    mw = ta.token_auth_middleware(internal_paths=INTERNAL, internal_secret=SECRET)
    req, store = _make_request(
        headers={"X-Internal-Secret": SECRET, "X-Session-Key": _SHARED[1]},
        unix=True,
    )
    resp = await mw(req, _ok_handler)
    assert resp.status == 200
    # Permissive, but NOT attested: nothing here vouched for the declared key.
    assert "peer_verified" not in store
    assert not [c for c in calls if c.get("outcome") == "denied"]


@_posix_only
@pytest.mark.asyncio
async def test_a_sole_live_session_that_is_the_declared_key_is_attested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One session on the ancestry AND it is the declared key: admitted, no token.

    The manager answers from memory the caller cannot edit, so this is positive
    attestation and ``peer_verified`` is set, unlike the empty arm above.
    """
    calls = _wire_peer(monkeypatch, resolved="", tenant_count=0, live_keys=(_SHARED[1],))
    mw = ta.token_auth_middleware(internal_paths=INTERNAL, internal_secret=SECRET)
    req, store = _make_request(
        headers={"X-Internal-Secret": SECRET, "X-Session-Key": _SHARED[1]},
        unix=True,
    )
    resp = await mw(req, _ok_handler)
    assert resp.status == 200
    assert store.get("peer_verified") is True
    assert not [c for c in calls if c.get("outcome") == "denied"]


@_posix_only
@pytest.mark.asyncio
async def test_a_sole_live_session_that_is_NOT_the_declared_key_demands_a_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HEADLINE: the unlink hole a COUNT cannot see.

    With the pair deleted, a count of one would look 1:1. The manager names ONE
    session and it is not the declared one, so the token decides.
    """
    calls = _wire_peer(monkeypatch, resolved="", tenant_count=0, live_keys=(_SHARED[0],))
    mw = ta.token_auth_middleware(internal_paths=INTERNAL, internal_secret=SECRET)
    req, store = _make_request(
        headers={"X-Internal-Secret": SECRET, "X-Session-Key": _SHARED[1]},
        unix=True,
    )
    resp = await mw(req, _ok_handler)
    assert resp.status == 403
    assert json.loads(resp.body)["code"] == "peer_session_unattested"
    denied = [c for c in calls if c.get("outcome") == "denied"]
    assert denied and "manager hosts a different session" in denied[0]["error"]
    assert "peer_verified" not in store


@_posix_only
@pytest.mark.asyncio
async def test_a_key_absent_from_the_managers_set_still_passes_on_its_own_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The converse, and why that arm attests instead of denying outright.

    A ``spawn_run`` shared subagent lives on its parent's ``_shared_provider`` and
    is never registered with the manager, so its key can be absent from the set.
    Its own token names it.
    """
    calls = _wire_peer(
        monkeypatch, resolved="", tenant_count=0, live_keys=(_SHARED[0],), attests=_SHARED[1]
    )
    mw = ta.token_auth_middleware(internal_paths=INTERNAL, internal_secret=SECRET)
    req, store = _make_request(
        headers={
            "X-Internal-Secret": SECRET,
            "X-Session-Key": _SHARED[1],
            "X-Session-Token": "a-token-naming-the-declared-key",
        },
        unix=True,
    )
    resp = await mw(req, _ok_handler)
    assert resp.status == 200
    assert store.get("peer_verified") is True
    allowed = [c for c in calls if c.get("outcome") == "allowed"]
    assert allowed and "manager hosts a different session" in allowed[0]["error"]


@_posix_only
@pytest.mark.asyncio
async def test_unix_peer_unverifiable_mapping_is_denied_without_a_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HEADLINE: a mapping that EXISTS and could not be read is not an absence.

    The pair is replaced in two steps, so a republication is briefly a MAC
    mismatch. A same-uid co-tenant can watch `.txt` move ahead of `.sig` and aim
    its declaration at that gap, so it must not take the degrade arm.
    """
    calls = _wire_peer(monkeypatch, resolved="", tenant_count=0, unverifiable=True)
    mw = ta.token_auth_middleware(internal_paths=INTERNAL, internal_secret=SECRET)
    req, store = _make_request(
        headers={"X-Internal-Secret": SECRET, "X-Session-Key": _SHARED[1]},
        unix=True,
    )
    resp = await mw(req, _ok_handler)
    assert resp.status == 403
    assert json.loads(resp.body)["code"] == "peer_session_unattested"
    denied = [c for c in calls if c.get("outcome") == "denied"]
    assert denied and "unverifiable" in denied[0]["error"]
    assert "peer_verified" not in store


@_posix_only
@pytest.mark.asyncio
async def test_unix_peer_unverifiable_mapping_admits_its_own_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The converse, and why the remedy is attestation rather than denial: the
    caller that legitimately owns the declared key holds a token naming it, and
    a republication window must not take its calls down. A sibling reading that
    key out of the roster holds no such token."""
    calls = _wire_peer(
        monkeypatch, resolved="", tenant_count=0, unverifiable=True, attests=_SHARED[1]
    )
    mw = ta.token_auth_middleware(internal_paths=INTERNAL, internal_secret=SECRET)
    req, store = _make_request(
        headers={
            "X-Internal-Secret": SECRET,
            "X-Session-Key": _SHARED[1],
            "X-Session-Token": "a-token-naming-the-declared-key",
        },
        unix=True,
    )
    resp = await mw(req, _ok_handler)
    assert resp.status == 200
    assert store.get("peer_verified") is True
    allowed = [c for c in calls if c.get("outcome") == "allowed"]
    assert allowed and "unverifiable" in allowed[0]["error"]


@_posix_only
@pytest.mark.asyncio
async def test_unix_peer_no_pid_proceeds(monkeypatch: pytest.MonkeyPatch) -> None:
    _wire_peer(monkeypatch, peer_pid=None, resolved="never-consulted")
    mw = ta.token_auth_middleware(internal_paths=INTERNAL, internal_secret=SECRET)
    req, store = _make_request(
        headers={"X-Internal-Secret": SECRET, "X-Session-Key": "dashboard:chat-1-abc"},
        unix=True,
    )
    resp = await mw(req, _ok_handler)
    assert resp.status == 200


@_posix_only
@pytest.mark.asyncio
async def test_unix_peer_uid_mismatch_denied(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _wire_peer(monkeypatch, verdict=PeerCredResult.MISMATCH)
    mw = ta.token_auth_middleware(internal_paths=INTERNAL, internal_secret=SECRET)
    req, store = _make_request(
        headers={"X-Internal-Secret": SECRET, "X-Session-Key": "dashboard:chat-1-abc"},
        unix=True,
    )
    resp = await mw(req, _ok_handler)
    assert resp.status == 403
    assert json.loads(resp.body) == {
        "error": "Forbidden",
        "code": "unix_peer_unverified",
    }
    assert any(c.get("outcome") == "denied" for c in calls)


@_posix_only
@pytest.mark.asyncio
async def test_unix_peer_uid_unverifiable_denied(monkeypatch: pytest.MonkeyPatch) -> None:
    """UNVERIFIABLE peer principal is DENIED (deny-by-default, mirroring
    gatewayd's register-path policy): on supported POSIX platforms an
    accepted AF_UNIX connection always yields peer credentials, so a failed
    read means the attestation mechanism itself broke."""
    calls = _wire_peer(monkeypatch, verdict=PeerCredResult.UNVERIFIABLE, resolved="")
    mw = ta.token_auth_middleware(internal_paths=INTERNAL, internal_secret=SECRET)
    req, store = _make_request(
        headers={"X-Internal-Secret": SECRET, "X-Session-Key": "dashboard:chat-1-abc"},
        unix=True,
    )
    resp = await mw(req, _ok_handler)
    assert resp.status == 403
    assert json.loads(resp.body) == {
        "error": "Forbidden",
        "code": "unix_peer_unverified",
    }
    assert any("unverifiable" in c.get("error", "") for c in calls)


@_posix_only
@pytest.mark.asyncio
async def test_unix_no_session_key_skips_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No X-Session-Key → nothing session-scoped claimed → no walk at all."""

    def _explode(pid, **kw):  # pragma: no cover — the assertion IS that it never runs
        raise AssertionError("resolver must not run without a session claim")

    _wire_peer(monkeypatch)
    monkeypatch.setattr(ta, "resolve_peer_tenancy", _explode)
    mw = ta.token_auth_middleware(internal_paths=INTERNAL, internal_secret=SECRET)
    req, store = _make_request(headers={"X-Internal-Secret": SECRET}, unix=True)
    resp = await mw(req, _ok_handler)
    assert resp.status == 200


@pytest.mark.asyncio
async def test_tcp_request_never_engages_peer_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _explode(sock):  # pragma: no cover — the assertion IS that it never runs
        raise AssertionError("peer check must not run for TCP requests")

    monkeypatch.setattr(ta, "check_peer_is_self", _explode)
    mw = ta.token_auth_middleware(internal_paths=INTERNAL, internal_secret=SECRET)
    req, store = _make_request(
        headers={"X-Internal-Secret": SECRET, "X-Session-Key": "dashboard:chat-1-abc"},
        unix=False,
    )
    resp = await mw(req, _ok_handler)
    assert resp.status == 200


@_posix_only
@pytest.mark.asyncio
async def test_resolver_exception_degrades_to_status_quo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _wire_peer(monkeypatch)

    def _boom(pid, **kw):
        raise RuntimeError("proc walk exploded")

    monkeypatch.setattr(ta, "resolve_peer_tenancy", _boom)
    mw = ta.token_auth_middleware(internal_paths=INTERNAL, internal_secret=SECRET)
    req, store = _make_request(
        headers={"X-Internal-Secret": SECRET, "X-Session-Key": "dashboard:chat-1-abc"},
        unix=True,
    )
    resp = await mw(req, _ok_handler)
    assert resp.status == 200


# End-to-end over a real UnixSite (POSIX): kernel-populated peer credentials


def _unix_http_request(
    sock_path: str, path: str, headers: dict[str, str], timeout: float = 5.0
) -> tuple[int, bytes]:
    """Minimal raw-HTTP client over AF_UNIX (independent of loopback_http)."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.connect(sock_path)
    try:
        lines = [f"GET {path} HTTP/1.1", "Host: localhost:5476", "Connection: close"]
        lines += [f"{k}: {v}" for k, v in headers.items()]
        s.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        buf = b""
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
        status = int(buf.split(b" ", 2)[1])
        return status, buf
    finally:
        s.close()


@pytest.mark.skipif(platform_compat.IS_WINDOWS, reason="AF_UNIX transport is POSIX-only")
@pytest.mark.asyncio
async def test_unix_site_end_to_end_peer_verification(
    tmp_path: Path, short_sock_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real UnixSite + real AF_UNIX connect: the kernel reports OUR pid/uid,
    so with a session_pid file for an ancestor of this test process the
    middleware must deny a foreign declared key and allow the matching one."""
    import os

    from kiro_crew import session_pid_sig as sps

    # A SIGNED pidfile for THIS process (the kernel-reported peer pid), as
    # signed_only=True requires; the HMAC key is pinned to compute the sidecar.
    _hmac_key = b"K" * 32
    monkeypatch.setattr(sps, "_load_hmac_key", lambda: _hmac_key)
    pid = os.getpid()
    (tmp_path / f"session_pid_{pid}.txt").write_text("dashboard:chat-e2e", encoding="utf-8")
    (tmp_path / f"session_pid_{pid}.sig").write_text(
        sps._compute_sig(_hmac_key, pid, "dashboard:chat-e2e"), encoding="utf-8"
    )
    monkeypatch.setattr(
        ta,
        "resolve_peer_tenancy",
        lambda p, **kw: resolve_peer_tenancy(p, config_dir_fn=lambda: tmp_path, **kw),
    )

    app = web.Application()
    app.middlewares.append(
        ta.token_auth_middleware(internal_paths=INTERNAL, internal_secret=SECRET)
    )

    async def handler(request: web.Request) -> web.Response:
        return web.json_response({"peer_verified": bool(request.get("peer_verified"))})

    app.router.add_get("/api/spawn", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    sock_path = str(short_sock_dir / "dash-test.sock")
    site = web.UnixSite(runner, sock_path)
    await site.start()
    try:
        loop = asyncio.get_running_loop()
        status_ok, body_ok = await loop.run_in_executor(
            None,
            _unix_http_request,
            sock_path,
            "/api/spawn",
            {"X-Internal-Secret": SECRET, "X-Session-Key": "dashboard:chat-e2e"},
        )
        assert status_ok == 200
        assert b'"peer_verified": true' in body_ok
        status_evil, _ = await loop.run_in_executor(
            None,
            _unix_http_request,
            sock_path,
            "/api/spawn",
            {"X-Internal-Secret": SECRET, "X-Session-Key": "dashboard:chat-OTHER"},
        )
        assert status_evil == 403
    finally:
        await runner.cleanup()


# CSRF: the no-Origin branch trusts the unix transport


@_posix_only
def test_check_origin_trusts_unix_transport_without_origin() -> None:
    """An AF_UNIX request has no loopback request.remote; without this trust
    the CSRF middleware would 403 every mutating internal call on the socket
    before token auth ever ran."""
    from kiro_crew.dashboard.origin import check_origin

    req, _store = _make_request(unix=True)
    req.app = {"allowed_origins": set()}
    assert check_origin(req, require=True) is True


def test_check_origin_still_rejects_plain_remote_without_origin() -> None:
    from kiro_crew.dashboard.origin import check_origin

    req, _store = _make_request(remote="10.0.0.1", unix=False)
    req.app = {"allowed_origins": set()}
    assert check_origin(req, require=True) is False


# server startup — _start_unix_site


@pytest.mark.skipif(platform_compat.IS_WINDOWS, reason="AF_UNIX transport is POSIX-only")
@pytest.mark.asyncio
async def test_start_unix_site_binds_and_removes_stale(
    short_sock_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.dashboard import server as srv

    monkeypatch.setattr(
        "kiro_crew.dashboard.server.dashboard_socket_path",
        lambda port: short_sock_dir / f"dashboard-{port}.sock",
    )
    # Plant a stale socket file (bound then abandoned) to prove self-healing.
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(short_sock_dir / "dashboard-5999.sock"))
    stale.close()

    app = web.Application()
    runner = web.AppRunner(app)
    await runner.setup()
    try:
        path = await srv._start_unix_site(runner, 5999)
        assert path is not None
        assert path.exists()
        import stat as _stat

        assert _stat.S_ISSOCK(path.stat().st_mode)
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_start_unix_site_skipped_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    from kiro_crew.dashboard import server as srv

    monkeypatch.setattr(srv.platform_compat, "IS_WINDOWS", True)
    assert await srv._start_unix_site(MagicMock(), 5999) is None


@pytest.mark.skipif(platform_compat.IS_WINDOWS, reason="AF_UNIX transport is POSIX-only")
@pytest.mark.asyncio
async def test_start_unix_site_bind_failure_degrades(
    short_sock_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-socket file squatting the path makes the bind fail; startup must
    degrade to TCP-only (return None), never raise."""
    from kiro_crew.dashboard import server as srv

    squatter = short_sock_dir / "dashboard-6001.sock"
    squatter.write_text("not a socket", encoding="utf-8")
    monkeypatch.setattr("kiro_crew.dashboard.server.dashboard_socket_path", lambda port: squatter)
    app = web.Application()
    runner = web.AppRunner(app)
    await runner.setup()
    try:
        assert await srv._start_unix_site(runner, 6001) is None
        assert squatter.read_text(encoding="utf-8") == "not a socket"  # left in place
    finally:
        await runner.cleanup()


# loopback_http client — unix transport preference + fallback semantics


@pytest.fixture()
def unix_http_server(short_sock_dir: Path):
    """A minimal threaded HTTP server on an AF_UNIX socket."""
    import http.server
    import socketserver
    import threading

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 — stdlib contract
            body = json.dumps({"via": "unix"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):  # silence
            pass

    class _UnixServer(socketserver.ThreadingUnixStreamServer):
        daemon_threads = True

        # BaseHTTPRequestHandler derives client_address[0]; unix sockets
        # provide '' — normalize so the handler does not crash.
        def get_request(self):
            request, _ = super().get_request()
            return request, ("unix", 0)

    sock_path = str(short_sock_dir / "client-test.sock")
    server = _UnixServer(sock_path, _Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield sock_path
    server.shutdown()
    server.server_close()


@pytest.mark.skipif(platform_compat.IS_WINDOWS, reason="AF_UNIX transport is POSIX-only")
def test_loopback_urlopen_uses_unix_socket(unix_http_server: str) -> None:
    req = urllib.request.Request("http://localhost:59999/api/anything")
    with loopback_urlopen(req, timeout=5, unix_socket_path=unix_http_server) as resp:
        assert json.loads(resp.read()) == {"via": "unix"}


def test_loopback_urlopen_absent_socket_falls_back_to_tcp(short_sock_dir: Path) -> None:
    """Socket file missing → straight to TCP (refused on a dead port)."""
    req = urllib.request.Request("http://127.0.0.1:1/api/x")
    with pytest.raises(urllib.error.URLError):
        loopback_urlopen(req, timeout=2, unix_socket_path=str(short_sock_dir / "nope.sock"))


@pytest.mark.skipif(platform_compat.IS_WINDOWS, reason="AF_UNIX transport is POSIX-only")
def test_loopback_urlopen_stale_socket_falls_back_to_tcp(short_sock_dir: Path) -> None:
    """Socket file exists but nobody listens → connect refused → TCP fallback.

    The TCP side serves a real response, proving the fallback actually runs
    the request rather than re-raising."""
    import http.server
    import threading

    stale_path = short_sock_dir / "stale.sock"
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(str(stale_path))
    s.close()  # bound but never listened/accepting → ECONNREFUSED

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 — stdlib contract
            body = b'{"via": "tcp"}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    tcp = LoopbackThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = tcp.server_address[1]
    t = threading.Thread(target=tcp.serve_forever, daemon=True)
    t.start()
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/x")
        with loopback_urlopen(req, timeout=5, unix_socket_path=str(stale_path)) as resp:
            assert json.loads(resp.read()) == {"via": "tcp"}
    finally:
        tcp.shutdown()
        tcp.server_close()


@pytest.mark.skipif(platform_compat.IS_WINDOWS, reason="AF_UNIX transport is POSIX-only")
def test_loopback_urlopen_http_error_propagates_no_fallback(short_sock_dir: Path) -> None:
    """A 4xx over the unix socket is a REAL response — it must propagate as
    HTTPError, never trigger a duplicate TCP send."""
    import http.server
    import socketserver
    import threading

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 — stdlib contract
            self.send_response(403)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *a):
            pass

    class _UnixServer(socketserver.ThreadingUnixStreamServer):
        daemon_threads = True

        def get_request(self):
            request, _ = super().get_request()
            return request, ("unix", 0)

    sock_path = str(short_sock_dir / "err.sock")
    server = _UnixServer(sock_path, _Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        req = urllib.request.Request("http://127.0.0.1:1/api/x")
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            loopback_urlopen(req, timeout=5, unix_socket_path=sock_path)
        assert exc_info.value.code == 403
    finally:
        server.shutdown()
        server.server_close()


# mcp_core client — socket preference wiring


@pytest.mark.skipif(platform_compat.IS_WINDOWS, reason="AF_UNIX transport is POSIX-only")
def test_mcp_core_post_prefers_unix_socket(
    unix_http_server: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import kiro_crew.mcp_core as mcp_core

    monkeypatch.setattr(mcp_core, "_API_UNIX_SOCKET", unix_http_server)
    monkeypatch.setattr(mcp_core, "_API", "http://127.0.0.1:1")  # TCP would refuse
    monkeypatch.setattr(mcp_core, "_internal_secret", lambda: "s")
    monkeypatch.setattr(mcp_core, "_resolve_session_key", lambda: "dashboard:chat-1")
    assert mcp_core._get("/api/anything") == {"via": "unix"}


def test_mcp_core_post_falls_back_to_tcp_when_socket_absent(
    short_sock_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Error shape of _get is unchanged when neither transport answers."""
    import kiro_crew.mcp_core as mcp_core

    monkeypatch.setattr(mcp_core, "_API_UNIX_SOCKET", str(short_sock_dir / "absent.sock"))
    monkeypatch.setattr(mcp_core, "_API", "http://127.0.0.1:1")
    monkeypatch.setattr(mcp_core, "_internal_secret", lambda: "s")
    monkeypatch.setattr(mcp_core, "_resolve_session_key", lambda: "dashboard:chat-1")
    out = mcp_core._get("/api/anything")
    assert "error" in out


@_posix_only
@pytest.mark.asyncio
async def test_non_loopback_mixed_denial_names_which_credential_was_wrong(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The mixed-path arm must name the sides too, like the loopback arm does.

    Both credential fingerprints tell an ABSENT credential (no credential file
    readable) from a wrong one.
    """
    calls = _wire_peer(monkeypatch)
    mw = ta.token_auth_middleware(
        internal_paths=INTERNAL,
        internal_secret=SECRET,
        mixed_internal_paths=INTERNAL,
        local_only=True,
    )

    # Non-loopback remote on a mixed internal path, header present but EMPTY.
    req, _ = _make_request(headers={"X-Internal-Secret": ""}, remote="10.1.2.3")
    resp = await mw(req, _ok_handler)
    assert resp.status == 403

    denials = [
        c for c in calls if c.get("operation") == "internal_auth" and c.get("outcome") == "denied"
    ]
    assert len(denials) == 1
    err = denials[0]["error"]
    assert "non-loopback mixed" in err, err
    assert (
        "received=absent" in err
    ), "the mixed arm still cannot say an absent credential from a wrong one"
    assert f"expected={ta._credential_fingerprint(SECRET)}" in err, err


@_posix_only
@pytest.mark.asyncio
async def test_unattested_record_names_the_live_count_it_decided_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A denial must not report a tenancy that contradicts its own reason.

    This arm denies because the live manager reports several sessions while the
    mapping's ``tenant_count`` is its ``0`` default, so the record must show the
    manager's count, not ``hosts 0 sessions``.
    """
    calls = _wire_peer(
        monkeypatch,
        resolved="",
        tenants=(),
        tenant_count=0,
        live_keys=_SHARED + ("dashboard:chat-2-def",),
    )
    mw = ta.token_auth_middleware(internal_paths=INTERNAL, internal_secret=SECRET)
    req, store = _make_request(
        headers={"X-Internal-Secret": SECRET, "X-Session-Key": _SHARED[1]},
        unix=True,
    )
    resp = await mw(req, _ok_handler)
    assert resp.status == 403
    assert json.loads(resp.body)["code"] == "peer_session_unattested"
    denied = [c for c in calls if c.get("outcome") == "denied"]
    assert denied, "the deny arm must leave a record at all"
    err = denied[0]["error"]
    assert "manager reports several" in err, err
    assert "hosts 3 sessions" in err, err
    assert "hosts 0 sessions" not in err, "the measured count was dropped for a zero"


@_posix_only
@pytest.mark.asyncio
async def test_unattested_record_says_unmeasured_rather_than_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three states, not two: unmeasured is not the same claim as "hosts none".

    On the MAC-mismatch arm the mapping that would carry a count did not verify,
    so ``0`` would be a false claim.
    """
    calls = _wire_peer(monkeypatch, resolved="", tenant_count=0, unverifiable=True)
    mw = ta.token_auth_middleware(internal_paths=INTERNAL, internal_secret=SECRET)
    req, store = _make_request(
        headers={"X-Internal-Secret": SECRET, "X-Session-Key": _SHARED[1]},
        unix=True,
    )
    resp = await mw(req, _ok_handler)
    assert resp.status == 403
    denied = [c for c in calls if c.get("outcome") == "denied"]
    assert denied, "the deny arm must leave a record at all"
    err = denied[0]["error"]
    assert "unverifiable" in err, err
    assert "tenancy not measured" in err, err
    assert "sessions" not in err.split("(")[0], "an unmeasured tenancy claimed a count"


@_posix_only
@pytest.mark.asyncio
async def test_unattested_record_keeps_the_recorded_count_on_the_roster_arms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The converse direction: where a count WAS recorded it must still show.

    Both roster arms reach ``_attest_shared`` from a resolved mapping, so their
    ``tenant_count`` is real and must not be suppressed.
    """
    for tenants, shape in ((), "truncated"), (_SHARED, "roster complete"):
        calls = _wire_peer(monkeypatch, resolved="", tenants=tenants, tenant_count=2)
        mw = ta.token_auth_middleware(internal_paths=INTERNAL, internal_secret=SECRET)
        req, _store = _make_request(
            headers={"X-Internal-Secret": SECRET, "X-Session-Key": _SHARED[1]},
            unix=True,
        )
        resp = await mw(req, _ok_handler)
        assert resp.status == 403, shape
        denied = [c for c in calls if c.get("outcome") == "denied"]
        assert denied, shape
        err = denied[0]["error"]
        assert shape in err, err
        assert "hosts 2 sessions" in err, err


# A spawn_run shared subagent on a pid whose mapping names only its parent
# The session manager never registers a shared subagent, so the COMPLETE roster
# omits it. Its stubs declare its key with its token, and the tenancy table binds
# that key to the pid for the turn, which is what admits it.

_PARENT = "dashboard:chat-1-parent"
_CHILD = "subagent:run-7-child"


@_posix_only
@pytest.mark.asyncio
async def test_a_shared_subagent_the_roster_omits_is_admitted_on_its_token_and_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _wire_peer(
        monkeypatch,
        resolved=_PARENT,
        attests=_CHILD,
        bound_keys=(_PARENT, _CHILD),
    )
    mw = ta.token_auth_middleware(internal_paths=INTERNAL, internal_secret=SECRET)
    req, store = _make_request(
        headers={
            "X-Internal-Secret": SECRET,
            "X-Session-Key": _CHILD,
            "X-Session-Token": "tok-for-the-child",
        },
        unix=True,
    )
    resp = await mw(req, _ok_handler)
    assert resp.status == 200
    assert store.get("peer_verified") is True
    assert not [c for c in calls if c.get("outcome") == "denied"]
    allowed = [c for c in calls if c.get("operation") == "dashboard.peer-identity-co-tenant"]
    assert allowed and allowed[0]["error"].endswith("binding=confirmed")


@_posix_only
@pytest.mark.asyncio
async def test_a_bound_subagent_key_without_its_token_is_still_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The binding only routes to attestation; it never admits on its own.

    The parent shares the process and can read the tenancy-bound child key, so a
    declaration of it with no token naming it must not pass.
    """
    _wire_peer(monkeypatch, resolved=_PARENT, attests="", bound_keys=(_PARENT, _CHILD))
    mw = ta.token_auth_middleware(internal_paths=INTERNAL, internal_secret=SECRET)
    req, store = _make_request(
        headers={"X-Internal-Secret": SECRET, "X-Session-Key": _CHILD},
        unix=True,
    )
    resp = await mw(req, _ok_handler)
    assert resp.status == 403
    assert json.loads(resp.body)["code"] == "peer_session_unattested"
    assert "peer_verified" not in store


@_posix_only
@pytest.mark.asyncio
async def test_an_unbound_key_is_still_a_mismatch_even_with_its_own_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A session the tables do not place on this pid keeps the flat denial.

    A valid token proves who the caller is, not that it lives here, so an
    unrelated session calling through another runtime's process is refused.
    """
    _wire_peer(
        monkeypatch,
        resolved=_PARENT,
        attests="dashboard:chat-9-EVIL",
        bound_keys=(_PARENT, _CHILD),
    )
    mw = ta.token_auth_middleware(internal_paths=INTERNAL, internal_secret=SECRET)
    req, store = _make_request(
        headers={
            "X-Internal-Secret": SECRET,
            "X-Session-Key": "dashboard:chat-9-EVIL",
            "X-Session-Token": "tok-for-evil",
        },
        unix=True,
    )
    resp = await mw(req, _ok_handler)
    assert resp.status == 403
    assert json.loads(resp.body)["code"] == "peer_session_mismatch"
    assert "peer_verified" not in store
