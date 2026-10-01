"""Tests for kernel-attested peer verification of internal-API session claims.

Covers the four layers of the unix-socket peer-identity feature:

* :mod:`kiro_crew.peer_resolve` — the shared /proc ancestry walk (extracted
  from gatewayd; its original tests in ``test_mcp_gateway_claim.py`` keep
  covering the gatewayd wrapper seams).
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
from tmpdir_helpers import SHORT_TMP_PREFIX, short_tmp_base

import kiro_crew.dashboard.token_auth as ta
from kiro_crew import platform_compat
from kiro_crew.loopback_http import loopback_urlopen
from kiro_crew.mcp_gateway.socketsec import PeerCredResult
from kiro_crew.peer_resolve import PeerTenancy, resolve_peer_identity, resolve_peer_tenancy


@pytest.fixture
def short_sock_dir() -> "Iterator[Path]":
    """A SHORT base for every AF_UNIX bind/connect path in this module.

    ``sun_path`` caps at 104 bytes on macOS, and a pytest ``tmp_path`` under a
    deep TMPDIR (or xdist) exceeds it, failing ``bind()`` with ``OSError:
    AF_UNIX path too long``. Same pattern as ``test_socketsec.py``:
    ``mkdtemp`` under :func:`short_tmp_base`, removed at teardown because
    ``mkdtemp`` registers no finalizer.
    """
    base = Path(tempfile.mkdtemp(prefix=SHORT_TMP_PREFIX + "peer-", dir=short_tmp_base()))
    yield base
    shutil.rmtree(base, ignore_errors=True)


SECRET = "test-internal-secret"
INTERNAL = frozenset({"/api/spawn"})


# ---------------------------------------------------------------------------
# peer_resolve — the shared ancestry walk
# ---------------------------------------------------------------------------


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
    """REGRESSION (review finding): the bare .txt is same-uid agent-writable,
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


# ---------------------------------------------------------------------------
# token_auth middleware — unit tests with faked socketsec seams
# ---------------------------------------------------------------------------


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

    ``resolved`` is the ONE session the peer's pid names, which a shared pid
    cannot supply; ``tenants``/``tenant_count`` are the recorded membership the
    middleware verifies a declared key against when it cannot. ``attests`` is the
    session key the per-session token verifies to -- empty for no usable token,
    which is what a caller that presents none or presents a forged one looks
    like from here.

    ``bound_keys`` is what the LEASE and TENANCY tables place on the peer's
    ancestry. Empty by default, which is the tables saying nothing about this pid
    -- no evidence, so every arm behaves as it did before the binding check
    existed and a test that does not care about it need not think about it. The
    helper's own contract is pinned separately, because this seam stubs it out.
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


# ---------------------------------------------------------------------------
# A SHARED pid: one kiro-cli process, several ACP sessions
# ---------------------------------------------------------------------------
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
    # Naming the roster the decision was made against separates this arm from
    # the one whose roster the size bound truncated, and the binding says whether
    # the ownership tables backed the token or had nothing to say -- silent here,
    # which is the state every caller outside this gateway's own placements is in.
    assert allowed[0]["error"] == "roster complete; binding=no evidence"


@_posix_only
@pytest.mark.asyncio
async def test_unix_peer_denies_a_co_tenant_declaring_a_siblings_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recording the membership must not let one tenant BE another.

    The declared key is genuinely on the pid, so membership admits it and the
    kernel's process attestation agrees -- both are satisfied by the attacker.
    What separates them is the token, which names the session that actually
    holds it. Without this the recorded list is an impersonation menu: it is
    published in a file every co-tenant can read.
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
    """A shared pid with no attestation is today's answer, not a new denial.

    Before the tenant section existed this declaration was refused as well, so
    requiring the token takes nothing away from a caller that had it working.
    """
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
    names it and there is no sibling identity to be mistaken for. Requiring a
    token here would deny callers that work today.
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

    The mapping file is size-bounded, so a runtime with very many sessions
    records the count without every key, and a declared key's absence from the
    short set is no grounds to deny. It is no grounds to admit unattested
    either: the count still proves the pid hosts several sessions, and the
    per-session token does not depend on the roster. Truncation is a normal
    publisher outcome, so a caller able to overflow the roster would otherwise
    have skipped the check that the complete-roster path enforces.
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

    The other half of the truncation arm: a legitimate session dropped from the
    enumeration still gets in, because its own per-session token names it. This
    is what keeps the fix from turning a size bound into a 403 for every session
    past the budget.
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

    ``shared`` is the positive evidence that separates the two unknowns hiding
    behind an incomplete membership. With a count of zero nothing resolved --
    no mapping in the ancestry, or one proven stale -- and demanding a token
    here would start denying callers that work today (warm-pool runtimes before
    claim, cron scripts, pooled MCP backends), which is the stricter direction
    this check must never take.
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


# ---------------------------------------------------------------------------
# The token must also name a session the process still HOSTS
# ---------------------------------------------------------------------------
# A token proves which session the caller is. It does not prove that session is
# on the process the call arrived from, and it cannot: it is a bearer credential
# in the runtime's environment, so a same-uid co-tenant reading
# /proc/<pid>/environ can lift a sibling's token and satisfy the name check with
# it. The ownership tables are the fact it cannot forge -- they live in the
# gateway's memory, not in a file the sandbox can write -- so the declared key
# must also hold a live lease or tenancy on this pid.


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

    The stolen-token case, and the only one this arm adds: the actor presents a
    token that verifies to a sibling key -- which the roster also lists, so every
    earlier check passes -- while the tables place that sibling nowhere near this
    process. Its lease was released, or its turn-scoped tenancy ended, so there is
    positive evidence the declaration does not belong here.
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

    The fail-open boundary, and it has to be this way round: the tables describe
    runtimes THIS gateway placed sessions on, and a warm-pool runtime before its
    claim, a pooled MCP backend, a cron script and anything from another install
    are all absent from them while being entirely legitimate. An empty answer that
    denied would 403 every one of those. The token check above still holds, so this
    arm is exactly as strong as it was before the binding existed, never weaker.
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

    This is what makes the binding check usable for the session type it was
    written for. A sub-agent holds no lease and the session manager never
    registers it, so the turn's tenancy is the only entry naming it -- and the
    name has to be the key its stubs put in ``X-Session-Key``, or the check would
    refuse the very caller it is meant to admit.

    Measured at three points because the window is the interesting part: the key
    is bound during the turn and not before or after it.
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

    Its claim still defends the process -- that is the kill gate's business -- but
    it must not appear as a session on the pid, or an auth decision would read the
    OAuth child as a tenant whose key could be declared.
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

    It returns the KEYS, not a count: the arm has to decide whether the DECLARED
    key is one of them, and a count cannot answer that -- it can only say how
    many, which is what let a caller name any key it liked.

    Two things it must get right. It takes only rows that describe a SESSION --
    the snapshot also appends one row per companion RUNTIME carrying display
    text, and a subagent runtime often repeats the pid of a session it serves, so
    taking those would report two where one lives. And it walks the chain
    peer-first, because the peer is an MCP server whose kiro-cli ANCESTOR is the
    pid sessions are keyed by.
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

    The mapping pair sits in the data-home root, which is writable inside the
    agent sandbox, so a co-tenant can delete it -- and it has already read the
    roster, so it knows which sibling key to declare. Disk absence then reads as
    "nothing resolved", which is the permissive arm, and the whole token demand
    costs one `unlink` to skip. Per-turn republication bounds that window without
    closing it, because the deleter picks its moment. The live session manager
    answers from memory about runtimes it owns, so it is the one account of the
    pid's tenancy an agent cannot edit.
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

    Nothing resolved on disk and the manager names no session on the ancestry, so
    there is nothing to check the declared key against and today's semantics hold
    -- a cron script, a warm-pool runtime before claim, a pooled backend outside
    this manager. This is the one arm that stays permissive, and it must, because
    it is the ordinary state of every caller that has not been claimed yet.
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

    The manager answers from memory in this process about runtimes it owns, so
    the key came from a source the caller cannot edit. That makes this positive
    attestation rather than a shrug -- ``peer_verified`` is set, which the empty
    arm above deliberately does not do.
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

    With the pair deleted, a count of one reads as "1:1, no other identity to be
    mistaken for", so the declared key would be taken on the caller's word -- and
    an agent that unlinks its own mapping has already read the roster, so it knows
    a sibling key to name. The KEYS close it: the manager names ONE session on
    this ancestry and it is not the one declared, so the token decides.
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

    The manager's set is not a closed account of the tenancy: a ``spawn_run``
    shared subagent's session lives on its parent's ``_shared_provider`` and is
    never registered with the manager, so a legitimate caller's key can be absent
    from a set that names its parent. A flat denial would 403 exactly that caller
    in the window this arm exists for. Its own token names it, so it passes --
    while the unlinker, which holds the sibling key but not its token, does not.
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

    The pair is replaced in two steps, so a body-changing republication is
    briefly visible as a MAC mismatch -- and a tenancy-changing claim is exactly
    when the body changes. Left on the degrade arm that window is a BYPASS, not a
    degradation: a same-uid co-tenant can read both files, watch `.txt` move
    ahead of `.sig`, and drive its declaration into the gap to skip the
    attestation the resolved mapping would have demanded.
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


# ---------------------------------------------------------------------------
# End-to-end over a real UnixSite (POSIX): kernel-populated peer credentials
# ---------------------------------------------------------------------------


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

    # Publish a SIGNED pidfile for THIS process so the ancestry walk (starting
    # at the kernel-reported peer pid == our pid) resolves immediately under
    # the middleware's signed_only=True discipline. The HMAC trust root is
    # pinned so the sidecar can be computed against the tmp config dir.
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


# ---------------------------------------------------------------------------
# CSRF: the no-Origin branch trusts the unix transport
# ---------------------------------------------------------------------------


@_posix_only
def test_check_origin_trusts_unix_transport_without_origin() -> None:
    """An AF_UNIX request has no loopback request.remote; without this trust
    the CSRF middleware would 403 every mutating internal call on the socket
    before token auth ever ran (review finding)."""
    from kiro_crew.dashboard.origin import check_origin

    req, _store = _make_request(unix=True)
    req.app = {"allowed_origins": set()}
    assert check_origin(req, require=True) is True


def test_check_origin_still_rejects_plain_remote_without_origin() -> None:
    from kiro_crew.dashboard.origin import check_origin

    req, _store = _make_request(remote="10.0.0.1", unix=False)
    req.app = {"allowed_origins": set()}
    assert check_origin(req, require=True) is False


# ---------------------------------------------------------------------------
# server startup — _start_unix_site
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# loopback_http client — unix transport preference + fallback semantics
# ---------------------------------------------------------------------------


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

    tcp = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
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


# ---------------------------------------------------------------------------
# mcp_core client — socket preference wiring
# ---------------------------------------------------------------------------


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

    The loopback arm records both credential fingerprints, so an ABSENT
    credential (a caller that could read no credential file at all) is visibly
    different from a caller holding the wrong one. This arm was left on a bare
    string, so a denial here still said only "wrong secret" -- the exact
    ambiguity the fingerprint exists to remove, in the one place a remote
    mixed-path caller lands.
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

    This arm denies BECAUSE the live session manager reports several sessions on
    the ancestry, while nothing resolved on disk -- so the mapping's own
    ``tenant_count`` is its ``0`` default here. Rendering that produced
    ``hosts 0 sessions (mapping absent, manager reports several)``: a 403 whose
    audit trail denies the very count it acted on, and drops the only number an
    investigation could check the decision against.
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

    On the MAC-mismatch arm the mapping that would carry a count is the very
    thing that would not verify, so no tenancy was measured at all. A ``0`` here
    is not a smaller number -- it is a different and false statement, and one a
    reader cannot tell from a pid that genuinely hosts no session.
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
    ``tenant_count`` is real. Suppressing the clause everywhere would fix the
    contradiction by deleting the useful half, which is why this pin exists
    beside the two above.
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
