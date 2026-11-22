"""Local authorization: an app on this machine gets a scoped, revocable token only after
the owner agrees (``dashboard/local_authorization.py`` and its four routes)."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import threading
from pathlib import Path
from typing import Any
from unittest.mock import Mock
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.atomic_write import fsync_dir
from kiro_crew.dashboard import local_authorization as la
from kiro_crew.dashboard.routes import local_authorization as routes
from kiro_crew.dashboard.token_auth_paths import _BYPASS_EXACT_METHODS, _exact_method_bypass

VERIFIER = "v" * 43
CHALLENGE = (
    base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).rstrip(b"=").decode()
)
REDIRECT = "http://127.0.0.1:53124/callback"
ISSUER = "http://127.0.0.1:5476"
#: The subject token auth gives the host owner; deliberately not the literal "owner".
SUBJECT = "U_HOST_OWNER"
ASK = {
    "response_type": "code",
    "client_id": "notely-desktop",
    "client_name": "Notely",
    "redirect_uri": REDIRECT,
    "scope": "responses:write conversations:read",
    "state": "xyz",
    "code_challenge": CHALLENGE,
    "code_challenge_method": "S256",
}


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def audit(monkeypatch):
    audit = Mock(spec=["log_api_access"])
    monkeypatch.setattr(la, "sel", lambda: audit)
    return audit


@pytest.fixture
def store(tmp_path: Path, clock: Clock, audit) -> la.LocalAuthorization:
    return la.LocalAuthorization(tmp_path / "local-grants" / "state.json", b"k" * 32, clock)


def _pending(store: la.LocalAuthorization, **changes: str) -> str:
    kind, view = store.authorize({**ASK, **changes})
    assert kind == "created"
    return str(view["request_id"])


def _code(store: la.LocalAuthorization, clock: Clock, request_id: str) -> str:
    clock.now += la.POLL_INTERVAL_SECS
    kind, location = store.authorize({**ASK, "request_id": request_id})
    assert kind == "redirect"
    query = parse_qs(urlsplit(location).query)
    assert location.startswith(REDIRECT) and query["state"] == ["xyz"]
    return query["code"][0]


def _tokens(
    store: la.LocalAuthorization, clock: Clock, scopes: list[str] | None = None
) -> dict[str, Any]:
    request_id = _pending(store)
    store.decide(request_id, True, scopes, caller=SUBJECT)
    code = _code(store, clock, request_id)
    return store.exchange_code("notely-desktop", code, REDIRECT, VERIFIER, ISSUER)


def test_without_the_owners_consent_there_is_no_code_and_no_token(store, clock):
    request_id = _pending(store)
    clock.now += la.POLL_INTERVAL_SECS
    kind, view = store.authorize({**ASK, "request_id": request_id})
    assert (kind, view["status"]) == ("pending", "pending")
    with pytest.raises(la.AuthError) as refused:
        store.exchange_code("notely-desktop", "lc_guess", REDIRECT, VERIFIER, ISSUER)
    assert refused.value.error == "invalid_grant"


def test_after_consent_the_app_gets_a_token_for_the_scopes_it_was_given(store, clock):
    tokens = _tokens(store, clock)
    assert tokens["token_type"] == "Bearer" and tokens["expires_in"] == 300
    assert tokens["scope"] == "responses:write conversations:read"
    credential = store.verify(tokens["access_token"])
    assert credential is not None
    assert credential.client_id == "notely-desktop"
    assert credential.grant_id == tokens["grant_id"]
    assert credential.namespace == "default"
    assert credential.allows("responses:write") and credential.allows("conversations:read")


def test_a_denied_request_sends_the_app_back_with_access_denied(store, clock):
    request_id = _pending(store)
    store.decide(request_id, False, caller=SUBJECT)
    clock.now += la.POLL_INTERVAL_SECS
    kind, location = store.authorize({**ASK, "request_id": request_id})
    assert kind == "redirect"
    assert parse_qs(urlsplit(location).query) == {"error": ["access_denied"], "state": ["xyz"]}


def test_a_scope_outside_the_grant_is_refused(store, clock):
    tokens = _tokens(store, clock, scopes=["conversations:read"])
    credential = store.verify(tokens["access_token"])
    assert credential is not None and credential.scopes == {"conversations:read"}
    assert not credential.allows("responses:write")
    assert not credential.allows("grants:manage")
    with pytest.raises(la.AuthError) as widened:
        store.refresh("notely-desktop", tokens["refresh_token"], "responses:write", ISSUER)
    assert widened.value.error == "invalid_scope"


def test_the_owner_cannot_give_more_than_was_asked_and_unknown_scopes_never_wait(store):
    request_id = _pending(store)
    with pytest.raises(la.ScopesNotRequested):
        store.decide(request_id, True, ["bots:chat"], caller=SUBJECT)
    kind, location = store.authorize({**ASK, "scope": "responses:write grants:manage"})
    assert kind == "redirect" and "error=invalid_scope" in location
    assert [r["request_id"] for r in store.pending()] == [request_id]


def test_a_token_is_refused_once_its_grant_is_revoked(store, clock):
    by_owner = _tokens(store, clock)
    assert store.revoke_grant(by_owner["grant_id"], caller=SUBJECT) is True
    assert store.verify(by_owner["access_token"]) is None
    with pytest.raises(la.AuthError):
        store.refresh("notely-desktop", by_owner["refresh_token"], "", ISSUER)

    by_app = _tokens(store, clock)
    assert store.revoke_token(by_app["refresh_token"]) is True
    assert store.verify(by_app["access_token"]) is None
    assert store.revoke_token(by_app["refresh_token"]) is False  # already ended
    assert store.revoke_token("lr_unknown") is False  # an unknown token changes nothing


@pytest.mark.parametrize("operation", ["first_write", "revoke"])
@pytest.mark.parametrize("fail_at", [None, "record_dir", "parent_dir"])
def test_record_directory_sync_follows_write(
    store, clock, tmp_path, monkeypatch, operation, fail_at
):
    path = tmp_path / "local-grants" / "state.json"
    tokens = _tokens(store, clock) if operation == "revoke" else None
    if tokens is None:
        assert not path.parent.exists()
    synced = []

    def sync(directory, *, best_effort=False):
        # Read the actual replacement, not a stubbed write, before each directory sync.
        record = json.loads(path.read_text(encoding="utf-8"))
        if tokens is None:
            assert len(record["requests"]) == 1
            assert next(iter(record["requests"].values()))["status"] == "pending"
        else:
            assert record["grants"][tokens["grant_id"]]["status"] == "revoked"
        assert best_effort is False
        synced.append(directory)
        fsync_dir(directory, best_effort=best_effort)
        if (fail_at == "record_dir" and directory == path.parent) or (
            fail_at == "parent_dir" and directory == path.parent.parent
        ):
            raise OSError("directory sync failed")

    monkeypatch.setattr(la, "fsync_dir", sync)

    def perform():
        if tokens is None:
            return _pending(store)
        return store.revoke_grant(tokens["grant_id"], caller=SUBJECT)

    if fail_at is None:
        assert perform()
    else:
        with pytest.raises(OSError, match="directory sync failed"):
            perform()
    expected = [path.parent]
    if fail_at != "record_dir":
        expected.append(path.parent.parent)
    assert synced == expected
    if tokens is not None:
        assert store.verify(tokens["access_token"]) is None


def test_expired_tokens_codes_and_requests_are_refused(store, clock):
    tokens = _tokens(store, clock)
    clock.now += la.ACCESS_TTL_SECS
    assert store.verify(tokens["access_token"]) is None
    fresh = store.refresh("notely-desktop", tokens["refresh_token"], "", ISSUER)
    assert store.verify(fresh["access_token"]) is not None
    clock.now += la.REFRESH_IDLE_SECS
    with pytest.raises(la.AuthError):
        store.refresh("notely-desktop", fresh["refresh_token"], "", ISSUER)

    request_id = _pending(store)
    store.decide(request_id, True, caller=SUBJECT)
    code = _code(store, clock, request_id)
    clock.now += la.CODE_TTL_SECS
    with pytest.raises(la.AuthError):
        store.exchange_code("notely-desktop", code, REDIRECT, VERIFIER, ISSUER)

    stale = _pending(store)
    clock.now += la.REQUEST_TTL_SECS
    with pytest.raises(la.UnknownRequest):
        store.decide(stale, True, caller=SUBJECT)


def test_an_app_cannot_answer_its_own_request(store, clock):
    request_id = _pending(store)
    clock.now += la.POLL_INTERVAL_SECS
    answer = {"decision": "approve", "status": "approved", "granted_scopes": ASK["scope"]}
    kind, view = store.authorize({**ASK, **answer, "request_id": request_id})
    assert (kind, view["status"]) == ("pending", "pending")
    # A second program that knows the request id but not the first one's parameters.
    clock.now += la.POLL_INTERVAL_SECS
    with pytest.raises(la.AuthError):
        store.authorize({**ASK, "code_challenge": "w" * 43, "request_id": request_id})
    # An answered request cannot be answered again.
    store.decide(request_id, False, caller=SUBJECT)
    with pytest.raises(la.RequestAlreadyAnswered):
        store.decide(request_id, True, caller=SUBJECT)


def test_a_code_needs_the_asking_apps_verifier_and_works_once(store, clock):
    request_id = _pending(store)
    store.decide(request_id, True, caller=SUBJECT)
    code = _code(store, clock, request_id)
    for client_id, redirect_uri, verifier in (
        ("other-app", REDIRECT, VERIFIER),
        ("notely-desktop", REDIRECT + "/x", VERIFIER),
        ("notely-desktop", REDIRECT, "w" * 43),
    ):
        with pytest.raises(la.AuthError):
            store.exchange_code(client_id, code, redirect_uri, verifier, ISSUER)
    tokens = store.exchange_code("notely-desktop", code, REDIRECT, VERIFIER, ISSUER)
    with pytest.raises(la.AuthError):
        store.exchange_code("notely-desktop", code, REDIRECT, VERIFIER, ISSUER)
    assert store.verify(tokens["access_token"]) is None  # the reuse revoked the grant


def test_a_refresh_token_changes_on_use_and_a_replayed_one_revokes_the_grant(store, clock):
    first = _tokens(store, clock)
    second = store.refresh("notely-desktop", first["refresh_token"], "", ISSUER)
    assert second["refresh_token"] != first["refresh_token"]
    assert second["grant_id"] == first["grant_id"]
    with pytest.raises(la.AuthError):
        store.refresh("notely-desktop", first["refresh_token"], "", ISSUER)
    assert store.verify(second["access_token"]) is None


@pytest.mark.parametrize("elapsed", [la.CODE_TTL_SECS, la.REQUEST_TTL_SECS])
def test_exchanged_code_replay_revokes_after_expiry_and_restart(store, clock, tmp_path, elapsed):
    request_id = _pending(store)
    store.decide(request_id, True, caller=SUBJECT)
    code = _code(store, clock, request_id)
    tokens = store.exchange_code("notely-desktop", code, REDIRECT, VERIFIER, ISSUER)
    clock.now += elapsed
    tokens = store.refresh("notely-desktop", tokens["refresh_token"], "", ISSUER)
    restarted = la.LocalAuthorization(tmp_path / "local-grants" / "state.json", b"k" * 32, clock)
    assert restarted.verify(tokens["access_token"]) is not None
    with pytest.raises(la.AuthError):
        restarted.exchange_code("notely-desktop", code, REDIRECT, VERIFIER, ISSUER)
    assert restarted.verify(tokens["access_token"]) is None
    with pytest.raises(la.AuthError):
        restarted.refresh("notely-desktop", tokens["refresh_token"], "", ISSUER)


def test_refresh_replay_survives_many_rotations_and_restart(store, clock, tmp_path):
    first = tokens = _tokens(store, clock)
    for _ in range(200):
        tokens = store.refresh("notely-desktop", tokens["refresh_token"], "", ISSUER)
    restarted = la.LocalAuthorization(tmp_path / "local-grants" / "state.json", b"k" * 32, clock)
    assert restarted.verify(tokens["access_token"]) is not None
    with pytest.raises(la.AuthError):
        restarted.refresh("notely-desktop", first["refresh_token"], "", ISSUER)
    assert restarted.verify(tokens["access_token"]) is None
    with pytest.raises(la.AuthError):
        restarted.refresh("notely-desktop", tokens["refresh_token"], "", ISSUER)


def test_capacity_refusal_preserves_readable_record(store, clock, tmp_path, monkeypatch):
    tokens = _tokens(store, clock)
    path = tmp_path / "local-grants" / "state.json"
    before = path.read_bytes()
    monkeypatch.setattr(la, "_STATE_MAX_BYTES", len(before) + 1)
    with pytest.raises(ValueError, match="local authorization record is full"):
        store.authorize({**ASK, "client_id": "second-app"})
    assert path.read_bytes() == before
    assert store.verify(tokens["access_token"]) is not None
    assert store.revoke_grant(tokens["grant_id"], caller=SUBJECT)  # a revocation shrinks the record
    grant = json.loads(path.read_text(encoding="utf-8"))["grants"][tokens["grant_id"]]
    assert grant["status"] == "revoked"


def _grant_bytes(path: Path, grant_id: str) -> int:
    return len(json.dumps(json.loads(path.read_text(encoding="utf-8"))["grants"][grant_id]))


def test_a_grant_record_does_not_grow_with_rotations(store, clock, tmp_path):
    path = tmp_path / "local-grants" / "state.json"
    tokens = store.refresh("notely-desktop", _tokens(store, clock)["refresh_token"], "", ISSUER)
    after_one = _grant_bytes(path, tokens["grant_id"])
    for _ in range(199):
        tokens = store.refresh("notely-desktop", tokens["refresh_token"], "", ISSUER)
    grant = json.loads(path.read_text(encoding="utf-8"))["grants"][tokens["grant_id"]]
    assert grant["refresh_n"] == 200
    # Only the counter's own digits grow: "1" became "200".
    assert _grant_bytes(path, tokens["grant_id"]) == after_one + 2
    assert not any(isinstance(value, list) and value != grant["scopes"] for value in grant.values())


def test_a_revoked_grant_keeps_only_what_refuses_it(store, clock, tmp_path):
    tokens = _tokens(store, clock)
    request_id = _pending(store)
    store.decide(request_id, True, caller=SUBJECT)
    code = _code(store, clock, request_id)
    reused = store.exchange_code("notely-desktop", code, REDIRECT, VERIFIER, ISSUER)
    assert store.revoke_grant(tokens["grant_id"], caller=SUBJECT)
    record = json.loads((tmp_path / "local-grants" / "state.json").read_text(encoding="utf-8"))
    assert set(record["grants"][tokens["grant_id"]]) == {"status", "expires_at"}
    assert store.verify(tokens["access_token"]) is None
    for token in (tokens["refresh_token"], reused["refresh_token"]):
        store.revoke_token(token)  # neither raises on the reduced record
    with pytest.raises(la.AuthError):
        store.refresh("notely-desktop", tokens["refresh_token"], "", ISSUER)
    with pytest.raises(la.AuthError):
        store.exchange_code("notely-desktop", code, REDIRECT, VERIFIER, ISSUER)
    assert store.verify(reused["access_token"]) is None  # code reuse still revokes


def test_a_forged_or_altered_refresh_token_revokes_nothing(store, clock, tmp_path):
    first = _tokens(store, clock)
    tokens = store.refresh("notely-desktop", first["refresh_token"], "", ISSUER)
    prefix, rotation, mac = tokens["refresh_token"].split(".")
    flipped = mac[:-1] + ("A" if mac[-1] != "A" else "B")
    other_key = la.LocalAuthorization(tmp_path / "local-grants" / "state.json", b"z" * 32, clock)
    forged = (
        f"{prefix}.0.{mac}",  # an earlier rotation under the current MAC
        f"{prefix}.{rotation}.{flipped}",
        f"{prefix}.0{rotation}.{mac}",
        f"{prefix}.{rotation}.{mac}\n",
        other_key._refresh_token(first["grant_id"], 0),
        store._refresh_token("grt_" + "x" * 16, 0),
        "lr_unknown",
        "",
    )
    for token in forged:
        with pytest.raises(la.AuthError) as refused:
            store.refresh("notely-desktop", token, "", ISSUER)
        assert refused.value.error == "invalid_grant"
        store.revoke_token(token)
        assert store.verify(tokens["access_token"]) is not None, token
    renewed = store.refresh("notely-desktop", tokens["refresh_token"], "", ISSUER)
    assert store.verify(renewed["access_token"]) is not None


@pytest.mark.parametrize("fragment", ["#", "#fragment", "?#", "?x=1#"])
def test_redirect_fragment_delimiter_is_refused(store, fragment):
    with pytest.raises(la.AuthError) as refused:
        store.authorize({**ASK, "redirect_uri": REDIRECT + fragment})
    assert refused.value.error == "invalid_request"
    assert store.pending() == []


@pytest.mark.parametrize("suffix", ["?", "?existing=a%2Bb&empty="])
@pytest.mark.parametrize("outcome", ["approved", "denied", "invalid_scope"])
def test_redirect_query_preserves_parts(store, clock, suffix, outcome):
    query = {**ASK, "redirect_uri": REDIRECT + suffix}
    if outcome == "invalid_scope":
        kind, location = store.authorize({**query, "scope": "grants:manage"})
    else:
        _, pending = store.authorize(query)
        store.decide(pending["request_id"], outcome == "approved", caller=SUBJECT)
        clock.now += la.POLL_INTERVAL_SECS
        kind, location = store.authorize({**query, "request_id": pending["request_id"]})
    assert kind == "redirect"
    parts = urlsplit(location)
    assert (parts.scheme, parts.netloc, parts.path, parts.fragment) == (
        "http",
        "127.0.0.1:53124",
        "/callback",
        "",
    )
    fields = parse_qs(parts.query, keep_blank_values=True)
    expected = parse_qs(urlsplit(query["redirect_uri"]).query, keep_blank_values=True)
    expected["state"] = ["xyz"]
    if outcome == "approved":
        code = fields.pop("code")[0]
        tokens = store.exchange_code(
            "notely-desktop", code, query["redirect_uri"], VERIFIER, ISSUER
        )
        assert store.verify(tokens["access_token"]) is not None
    else:
        expected["error"] = ["access_denied" if outcome == "denied" else "invalid_scope"]
    assert fields == expected


@pytest.mark.parametrize("approve", [True, False])
def test_owner_decision_audits_only_gateway_request_id(store, audit, approve):
    request_id = _pending(store, client_id="private-client-id", client_name="private-client-name")
    store.decide(request_id, approve, caller=SUBJECT)
    audit.log_api_access.assert_called_once_with(
        caller=SUBJECT,
        operation="local_auth.decide",
        outcome="approved" if approve else "denied",
        source="local-authorization",
        resources=f"request={request_id}",
        critical=True,
    )
    with pytest.raises(la.RequestAlreadyAnswered):
        store.decide(request_id, approve, caller=SUBJECT)
    with pytest.raises(la.UnknownRequest):
        store.decide("untrusted-request-text", approve, caller=SUBJECT)
    assert audit.log_api_access.call_count == 1


def test_owner_revoke_audits_only_gateway_grant_id(store, clock, audit):
    tokens = _tokens(store, clock)
    audit.reset_mock()
    assert store.revoke_grant(tokens["grant_id"], caller=SUBJECT)
    audit.log_api_access.assert_called_once_with(
        caller=SUBJECT,
        operation="local_auth.revoke",
        outcome="revoked",
        source="local-authorization",
        resources=f"grant={tokens['grant_id']}",
        critical=True,
    )
    assert not store.revoke_grant("untrusted-grant-text", caller=SUBJECT)
    assert audit.log_api_access.call_count == 1
    assert store.verify(tokens["access_token"]) is None


@pytest.mark.parametrize("subject", ["", None])
def test_an_owner_action_without_a_subject_changes_nothing(store, clock, audit, tmp_path, subject):
    tokens = _tokens(store, clock)
    request_id = _pending(store)
    path = tmp_path / "local-grants" / "state.json"
    before = path.read_bytes()
    audit.reset_mock()
    with pytest.raises(ValueError):
        store.decide(request_id, True, caller=subject)
    with pytest.raises(ValueError):
        store.revoke_grant(tokens["grant_id"], caller=subject)
    with pytest.raises(TypeError):
        store.revoke_grant(tokens["grant_id"])  # no default subject to fall back on
    audit.log_api_access.assert_not_called()
    assert path.read_bytes() == before


@pytest.mark.parametrize("action", ["approve", "deny", "revoke"])
def test_owner_mutation_requires_durable_audit(store, clock, audit, tmp_path, action):
    tokens = _tokens(store, clock)
    request_id = _pending(store)
    path = tmp_path / "local-grants" / "state.json"
    before = path.read_bytes()
    audit.log_api_access.side_effect = OSError("audit unavailable")
    with pytest.raises(OSError, match="audit unavailable"):
        if action == "revoke":
            store.revoke_grant(tokens["grant_id"], caller=SUBJECT)
        else:
            store.decide(request_id, action == "approve", caller=SUBJECT)
    assert path.read_bytes() == before
    assert store.verify(tokens["access_token"]) is not None


def test_requests_and_grants_store_the_namespace_and_only_digests(store, clock, tmp_path):
    waiting = _pending(store, client_id="second-app")
    tokens = _tokens(store, clock)
    text = (tmp_path / "local-grants" / "state.json").read_text(encoding="utf-8")
    record = json.loads(text)
    assert record["requests"][waiting]["namespace"] == "default"
    assert record["grants"][tokens["grant_id"]]["namespace"] == "default"
    for secret in (tokens["access_token"], tokens["refresh_token"], VERIFIER):
        assert secret not in text


def test_a_restart_keeps_grants_and_pending_requests(store, clock, tmp_path):
    tokens = _tokens(store, clock)
    waiting = _pending(store)
    restarted = la.LocalAuthorization(tmp_path / "local-grants" / "state.json", b"k" * 32, clock)
    assert restarted.verify(tokens["access_token"]) is not None
    assert [r["request_id"] for r in restarted.pending()] == [waiting]
    restarted.decide(waiting, True, caller=SUBJECT)
    code = _code(restarted, clock, waiting)
    assert restarted.exchange_code("notely-desktop", code, REDIRECT, VERIFIER, ISSUER)["grant_id"]


def test_a_forged_altered_or_foreign_token_is_refused(store, clock, tmp_path):
    tokens = _tokens(store, clock)
    head, body, signature = tokens["access_token"].split(".")
    claims = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    claims["scope"] = " ".join(la.SCOPES)
    widened = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    other_key = la.LocalAuthorization(tmp_path / "local-grants" / "state.json", b"z" * 32, clock)
    for token in (f"{head}.{widened}.{signature}", f"{head}.{body}", "", "a.b.c", "é.é.é"):
        assert store.verify(token) is None
    assert other_key.verify(tokens["access_token"]) is None


def test_an_unreadable_record_grants_nothing(store, clock, tmp_path):
    tokens = _tokens(store, clock)
    (tmp_path / "local-grants" / "state.json").write_text("{not json", encoding="utf-8")
    assert store.verify(tokens["access_token"]) is None
    with pytest.raises(ValueError):
        store.authorize(ASK)


@pytest.mark.parametrize(
    "changes",
    [
        {"redirect_uri": "http://example.com/callback"},
        {"redirect_uri": "https://127.0.0.1:1/cb"},
        {"redirect_uri": "http://127.0.0.1/cb"},
        {"redirect_uri": "http://user@127.0.0.1:1/cb"},
        {"redirect_uri": "http://localhost:1/cb"},
        {"code_challenge_method": "plain"},
        {"code_challenge": "short"},
        {"response_type": "token"},
        {"client_id": ""},
        {"state": ""},
    ],
)
def test_a_bad_request_is_refused_and_never_redirected(store, changes):
    with pytest.raises(la.AuthError) as refused:
        store.authorize({**ASK, **changes})
    assert (refused.value.error, refused.value.status) == ("invalid_request", 400)
    assert store.pending() == []


def test_waiting_requests_are_capped_and_asking_again_too_soon_is_slowed(store, clock):
    first = _pending(store)
    with pytest.raises(la.AuthError) as soon:
        store.authorize({**ASK, "request_id": first})
    assert (soon.value.error, soon.value.status) == ("slow_down", 429)
    for _ in range(la.MAX_PENDING_PER_CLIENT - 1):
        _pending(store)
    with pytest.raises(la.AuthError) as full:
        store.authorize(ASK)
    assert full.value.status == 429
    clock.now += la.REQUEST_TTL_SECS  # expired requests free their place
    _pending(store)


def _redirect_of(length: int) -> str:
    return REDIRECT + "/" + "p" * (length - len(REDIRECT) - 1)


def test_a_redirect_uri_is_bounded(store, tmp_path):
    with pytest.raises(la.AuthError) as refused:
        store.authorize({**ASK, "redirect_uri": _redirect_of(la.REDIRECT_URI_MAX + 1)})
    assert (refused.value.error, refused.value.status) == ("invalid_request", 400)
    assert not (tmp_path / "local-grants" / "state.json").exists()
    at_limit = _redirect_of(la.REDIRECT_URI_MAX)
    request_id = _pending(store, redirect_uri=at_limit)
    assert [r["redirect_uri"] for r in store.pending()] == [at_limit]
    assert [r["request_id"] for r in store.pending()] == [request_id]


def test_a_long_client_id_without_a_name_is_kept_whole_and_named_short(store):
    client_id = "c" * la.CLIENT_ID_MAX
    kind, view = store.authorize(
        {k: v for k, v in ASK.items() if k != "client_name"} | {"client_id": client_id}
    )
    assert kind == "created"
    assert view["client_id"] == client_id
    assert view["client_name"] == client_id[: la.CLIENT_NAME_MAX]
    with pytest.raises(la.AuthError) as refused:
        store.authorize({**ASK, "client_name": "n" * (la.CLIENT_NAME_MAX + 1)})
    assert (refused.value.error, refused.value.status) == ("invalid_request", 400)
    assert "client_name" in refused.value.description
    assert [r["client_id"] for r in store.pending()] == [client_id]


def test_the_owner_cannot_grant_a_scope_twice(store, clock, tmp_path):
    request_id = _pending(store)
    with pytest.raises(la.ScopesNotRequested):
        store.decide(request_id, True, ["conversations:read"] * 2, caller=SUBJECT)
    assert [r["status"] for r in store.pending()] == ["pending"]


def test_refresh_cannot_repeat_a_scope(store, clock):
    tokens = _tokens(store, clock)
    with pytest.raises(la.AuthError) as refused:
        store.refresh(ASK["client_id"], tokens["refresh_token"], "conversations:read " * 2, ISSUER)
    assert refused.value.error == "invalid_scope"
    renewed = store.refresh(ASK["client_id"], tokens["refresh_token"], "", ISSUER)
    assert store.verify(renewed["access_token"]) is not None


def test_presented_values_are_bounded_before_they_are_hashed(store, clock, monkeypatch):
    tokens = _tokens(store, clock)
    request_id = _pending(store)
    store.decide(request_id, True, caller=SUBJECT)
    code = _code(store, clock, request_id)
    long = "x" * (la.PRESENTED_MAX + 1)
    hashed = []
    monkeypatch.setattr(la, "_digest", lambda value: hashed.append(value) or "0")
    for args in (
        (long, code, REDIRECT, VERIFIER),
        (ASK["client_id"], long, REDIRECT, VERIFIER),
        (ASK["client_id"], code, long, VERIFIER),
        (ASK["client_id"], code, REDIRECT, long),
    ):
        with pytest.raises(la.AuthError) as refused:
            store.exchange_code(*args, ISSUER)
        assert refused.value.error == "invalid_request"
    assert hashed == []
    for args in (
        (long, tokens["refresh_token"], ""),
        (ASK["client_id"], long, ""),
        (ASK["client_id"], tokens["refresh_token"], long),
    ):
        with pytest.raises(la.AuthError) as refused:
            store.refresh(*args, ISSUER)
        assert refused.value.error == "invalid_request"
    store.revoke_token(tokens["access_token"] + "." * (la.PRESENTED_MAX))
    assert store.verify(tokens["access_token"]) is not None
    assert len(tokens["access_token"]) < la.PRESENTED_MAX // 2


# ── the routes ────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def client(store, audit, monkeypatch):
    monkeypatch.setattr(la, "gateway_local_authorization", lambda: store)
    monkeypatch.setattr(routes, "sel", lambda: audit)
    app = web.Application()
    # The gateway sets this at startup; the authorize route reads it through check_origin.
    app["allowed_origins"] = set()
    routes.register(app)
    async with TestClient(TestServer(app)) as test_client:
        yield test_client


FORM = {"Content-Type": "application/x-www-form-urlencoded"}
#: Every await on the network in these tests is bounded, so a handler that waits for
#: the rest of a body fails the test instead of hanging it.
WAIT = 30


def _held_body(sent: list[int], chunks: int, release: asyncio.Event):
    """A body that sends *chunks* KiB, then waits for *release* before it ends."""

    async def body():
        for _ in range(chunks):
            sent.append(1024)
            yield b"p" * 1024
        await release.wait()

    return body()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/oauth/token", "/oauth/revoke"])
async def test_a_chunked_form_is_read_like_one_with_a_length(client, store, clock, path):
    for chunked in (False, True):
        tokens = _tokens(store, clock)
        form = {"client_id": ASK["client_id"], "grant_type": "refresh_token"}
        form.update(refresh_token=tokens["refresh_token"], token=tokens["refresh_token"])
        raw = urlencode(form).encode()

        async def pieces():
            yield raw[:10]
            yield raw[10:]

        data = pieces() if chunked else raw
        answer = await asyncio.wait_for(client.post(path, data=data, headers=FORM), WAIT)
        if chunked:
            assert answer.request_info.headers.get("Transfer-Encoding") == "chunked"
            assert "Content-Length" not in answer.request_info.headers
        assert answer.status == 200, (chunked, await answer.text())
        if path == "/oauth/token":
            renewed = await answer.json()
            assert store.verify(renewed["access_token"]) is not None
            assert renewed["grant_id"] == tokens["grant_id"]
        else:
            assert store.verify(tokens["access_token"]) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/oauth/token", "/oauth/revoke"])
@pytest.mark.parametrize("declared", [False, True], ids=["chunked", "content-length"])
async def test_an_oversized_body_is_refused_without_reading_the_rest(
    client, store, clock, monkeypatch, path, declared
):
    tokens = _tokens(store, clock)
    calls = []
    monkeypatch.setattr(routes, "_run", lambda *args: calls.append(args))
    sent, release = [], asyncio.Event()
    chunks = 1 if declared else routes._BODY_MAX // 1024 + 4
    headers = {**FORM, **({"Content-Length": str(1024 * 1024)} if declared else {})}
    body = _held_body(sent, chunks, release)
    try:
        answer = await asyncio.wait_for(client.post(path, data=body, headers=headers), WAIT)
        refused = await asyncio.wait_for(answer.json(), WAIT)
    finally:
        release.set()
    assert answer.status == 413
    assert refused["error"] == refused["code"] == "invalid_request"
    # The response came while the body was still held open: the handler never waited
    # for the rest of it, and no store call ran.
    assert sum(sent) <= chunks * 1024 < 1024 * 1024
    assert calls == []
    assert store.verify(tokens["access_token"]) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/oauth/token", "/oauth/revoke"])
async def test_no_body_or_another_content_type_is_still_a_400(client, path):
    for data, headers in ((None, {}), (b"token=x", {"Content-Type": "text/plain"})):
        answer = await asyncio.wait_for(client.post(path, data=data, headers=headers), WAIT)
        assert answer.status == 400
        expected = "unsupported_grant_type" if path == "/oauth/token" else "invalid_request"
        assert (await answer.json())["error"] == expected


@pytest.mark.asyncio
async def test_a_revoke_row_says_whether_a_grant_was_ended(client, store, clock, audit):
    tokens = _tokens(store, clock)
    for token, outcome in ((tokens["refresh_token"], "success"), ("lr_unknown", "no_grant")):
        audit.reset_mock()
        assert (await client.post("/oauth/revoke", data={"token": token})).status == 200
        audit.log_api_access.assert_called_once_with(
            caller="local-process",
            operation="local_auth.revoke",
            outcome=outcome,
            source="local-authorization",
            resources="token",
        )


def _creation_rows(audit: Mock) -> list[dict[str, Any]]:
    return [
        call.kwargs
        for call in audit.log_api_access.call_args_list
        if call.kwargs["operation"] == "local_auth.request"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", [{}, {"request_id": ""}], ids=["first-call", "empty-request-id"])
async def test_every_new_request_writes_one_creation_row(client, store, audit, extra):
    audit.reset_mock()
    json_only = {"Accept": "application/json"}
    asked = await client.get("/oauth/authorize", params={**ASK, **extra}, headers=json_only)
    assert asked.status == 202
    request_id = (await asked.json())["request_id"]
    assert [r["request_id"] for r in store.pending()] == [request_id]
    assert _creation_rows(audit) == [
        {
            "caller": "local-process",
            "operation": "local_auth.request",
            "outcome": "pending",
            "source": "local-authorization",
            "resources": f"request={request_id}",
        }
    ]


@pytest.mark.asyncio
async def test_asking_again_writes_no_creation_row(client, store, audit, clock):
    request_id = _pending(store)
    clock.now += la.POLL_INTERVAL_SECS
    audit.reset_mock()
    again = await client.get(
        "/oauth/authorize",
        params={**ASK, "request_id": request_id},
        headers={"Accept": "application/json"},
    )
    assert again.status == 202 and (await again.json())["request_id"] == request_id
    assert _creation_rows(audit) == []


def _denied_cross_site(audit: Mock) -> None:
    audit.log_api_access.assert_called_once_with(
        caller="local-process",
        operation="local_auth.request",
        outcome="denied",
        source="local-authorization",
        resources="cross-site",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("site", ["cross-site", "same-site", "Cross-Site"])
async def test_a_page_in_the_owners_browser_cannot_open_a_request(client, store, audit, site):
    headers = {"Sec-Fetch-Site": site, "Accept": "application/json"}
    answer = await client.get("/oauth/authorize", params=ASK, headers=headers)
    assert answer.status == 403
    assert answer.headers["Cache-Control"] == "no-store"
    assert (await answer.json())["error"] == "invalid_request"
    assert store.pending() == []
    _denied_cross_site(audit)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "https://evil.example"},
        {"Referer": "https://evil.example/page?x=1"},
        {"Origin": "null"},
    ],
)
async def test_a_foreign_origin_cannot_open_or_ask_again(client, store, audit, headers):
    request_id = _pending(store)
    for params in (ASK, {**ASK, "request_id": request_id}):
        audit.reset_mock()
        answer = await client.get("/oauth/authorize", params=params, headers=headers)
        assert answer.status == 403
        assert (await answer.json())["error"] == "invalid_request"
        _denied_cross_site(audit)
    assert [r["request_id"] for r in store.pending()] == [request_id]


@pytest.mark.asyncio
@pytest.mark.parametrize("headers", [{"Sec-Fetch-Site": "none"}, {}])
async def test_the_app_and_a_program_still_open_a_request(client, store, headers):
    headers = {**headers, "Accept": "application/json"}
    answer = await client.get("/oauth/authorize", params=ASK, headers=headers)
    assert answer.status == 202
    body = await answer.json()
    assert [r["request_id"] for r in store.pending()] == [body["request_id"]]


@pytest.mark.asyncio
async def test_the_waiting_pages_refresh_still_asks_again(client, store, clock):
    page = await client.get("/oauth/authorize", params=ASK, headers={"Sec-Fetch-Site": "none"})
    assert page.status == 200
    again = page.headers["Refresh"].split("url=", 1)[1]
    store.decide(store.pending()[0]["request_id"], True, caller=SUBJECT)
    clock.now += la.POLL_INTERVAL_SECS
    own = str(client.make_url(again))
    browser = {"Sec-Fetch-Site": "same-origin", "Referer": own}
    sent = await client.get(again, headers=browser, allow_redirects=False)
    assert sent.status == 302
    assert sent.headers["Location"].startswith(REDIRECT + "?code=")


@pytest.mark.asyncio
async def test_an_app_goes_from_request_to_token_over_http(client, store, clock):
    metadata = await (await client.get("/.well-known/oauth-authorization-server")).json()
    assert metadata["code_challenge_methods_supported"] == ["S256"]
    assert metadata["token_endpoint"].endswith("/oauth/token")
    assert metadata["scopes_supported"] == list(la.SCOPES)

    json_only = {"Accept": "application/json"}
    asked = await client.get("/oauth/authorize", params=ASK, headers=json_only)
    pending = await asked.json()
    assert asked.status == 202 and pending["status"] == "pending" and pending["interval"] == 2
    page = await client.get("/oauth/authorize", params={**ASK, "client_id": "<b>x"})
    assert page.status == 200 and page.content_type == "text/plain"
    assert "<b>x" not in await page.text() and "request_id=greq_" in page.headers["Refresh"]

    store.decide(pending["request_id"], True, caller=SUBJECT)
    clock.now += la.POLL_INTERVAL_SECS
    again = {**ASK, "request_id": pending["request_id"]}
    sent = await client.get("/oauth/authorize", params=again, allow_redirects=False)
    assert sent.status == 302
    code = parse_qs(urlsplit(sent.headers["Location"]).query)["code"][0]

    form = {
        "grant_type": "authorization_code",
        "client_id": "notely-desktop",
        "code": code,
        "redirect_uri": REDIRECT,
        "code_verifier": VERIFIER,
    }
    answered = await client.post("/oauth/token", data=form)
    tokens = await answered.json()
    assert answered.status == 200 and answered.headers["Cache-Control"] == "no-store"
    assert store.verify(tokens["access_token"]) is not None
    reused = await client.post("/oauth/token", data=form)
    assert reused.status == 400 and (await reused.json())["error"] == "invalid_grant"

    assert (await client.post("/oauth/revoke", data={})).status == 400
    assert (await client.post("/oauth/revoke", data={"token": "lr_unknown"})).status == 200
    other = await client.post("/oauth/token", data={"grant_type": "password"})
    assert (await other.json())["error"] == "unsupported_grant_type"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["init", "write", None])
@pytest.mark.parametrize("operation", ["authorize", "exchange", "refresh", "refusal", "revoke"])
async def test_route_audit_preserves_result_off_loop(
    client, store, clock, monkeypatch, failure, operation
):
    tokens = _tokens(store, clock)
    request_id = _pending(store)
    store.decide(request_id, True, caller=SUBJECT)
    code = _code(store, clock, request_id)
    loop_thread = threading.current_thread()
    init_threads, write_threads = [], []
    route_audit = Mock(spec=["log_api_access"])

    def write(**kwargs):
        write_threads.append(threading.current_thread())
        if failure == "write":
            raise OSError("audit write unavailable")

    def sink():
        init_threads.append(threading.current_thread())
        if failure == "init":
            raise ValueError("audit trust key invalid")
        return route_audit

    route_audit.log_api_access.side_effect = write
    monkeypatch.setattr(routes, "sel", sink)
    if operation == "authorize":
        response = await client.get(
            "/oauth/authorize", params=ASK, headers={"Accept": "application/json"}
        )
        assert response.status == 202
        body = await response.json()
        assert body["request_id"] in {r["request_id"] for r in store.pending()}
    elif operation == "revoke":
        response = await client.post("/oauth/revoke", data={"token": tokens["refresh_token"]})
        assert response.status == 200
        assert store.verify(tokens["access_token"]) is None
        with pytest.raises(la.AuthError):
            store.refresh(ASK["client_id"], tokens["refresh_token"], "", ISSUER)
    else:
        form = {"client_id": ASK["client_id"]}
        if operation == "exchange":
            form.update(
                grant_type="authorization_code",
                code=code,
                redirect_uri=REDIRECT,
                code_verifier=VERIFIER,
            )
        else:
            form.update(
                grant_type="refresh_token",
                refresh_token="lr_unknown" if operation == "refusal" else tokens["refresh_token"],
            )
        response = await client.post("/oauth/token", data=form)
        assert response.status == (400 if operation == "refusal" else 200)
        body = await response.json()
        if operation == "refusal":
            assert body["error"] == body["code"] == "invalid_grant"
            assert store.verify(tokens["access_token"]) is not None
        else:
            assert store.verify(body["access_token"]) is not None
            assert body["refresh_token"] != tokens["refresh_token"]
            if operation == "refresh":
                assert body["grant_id"] == tokens["grant_id"]
            renewed = store.refresh(ASK["client_id"], body["refresh_token"], "", ISSUER)
            assert store.verify(renewed["access_token"]) is not None
    assert response.headers["Cache-Control"] == "no-store"
    assert len(init_threads) == 1
    assert init_threads[0] is not loop_thread
    assert len(write_threads) == (0 if failure == "init" else 1)
    assert all(thread is not loop_thread for thread in write_threads)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["authorize", "exchange_code", "refresh", "revoke_token"])
@pytest.mark.parametrize("factory_fails", [False, True])
async def test_store_factory_runs_off_loop(
    client, store, clock, monkeypatch, operation, factory_fails
):
    request_id = _pending(store)
    store.decide(request_id, True, caller=SUBJECT)
    code = _code(store, clock, request_id)
    tokens = _tokens(store, clock)
    loop_thread = threading.current_thread()
    factory_threads = []

    def factory():
        factory_threads.append(threading.current_thread())
        if factory_fails:
            raise OSError("store unavailable")
        return store

    monkeypatch.setattr(la, "gateway_local_authorization", factory)
    if operation == "authorize":
        response = await client.get(
            "/oauth/authorize", params=ASK, headers={"Accept": "application/json"}
        )
        expected_status = 202
    elif operation == "revoke_token":
        response = await client.post("/oauth/revoke", data={"token": tokens["refresh_token"]})
        expected_status = 200
    else:
        form = {"client_id": ASK["client_id"]}
        if operation == "exchange_code":
            form.update(
                grant_type="authorization_code",
                code=code,
                redirect_uri=REDIRECT,
                code_verifier=VERIFIER,
            )
        else:
            form.update(grant_type="refresh_token", refresh_token=tokens["refresh_token"])
        response = await client.post("/oauth/token", data=form)
        expected_status = 200
    assert len(factory_threads) == 1
    assert factory_threads[0] is not loop_thread
    assert response.status == (503 if factory_fails else expected_status)
    if factory_fails:
        assert (await response.json())["code"] == "server_error"


@pytest.mark.asyncio
@pytest.mark.parametrize("audit_fails", [False, True])
async def test_a_request_that_came_through_an_entry_gets_404(
    client, store, audit, monkeypatch, audit_fails
):
    loop_thread = threading.current_thread()
    audit_threads = []

    def audit_sink():
        audit_threads.append(threading.current_thread())
        return audit

    monkeypatch.setattr(routes, "sel", audit_sink)
    if audit_fails:
        audit.log_api_access.side_effect = OSError("audit unavailable")
    forwarded = {"X-Forwarded-For": "203.0.113.7"}
    for method, path in (
        ("GET", "/.well-known/oauth-authorization-server"),
        ("GET", "/oauth/authorize"),
        ("POST", "/oauth/token"),
        ("POST", "/oauth/revoke"),
    ):
        audit.reset_mock()
        answer = await client.request(method, path, params=ASK, headers=forwarded)
        assert answer.status == 404, path
        assert (await answer.json())["error"] == "not_found"
        audit.log_api_access.assert_called_once_with(
            caller="local-process",
            operation="local_auth.admission",
            outcome="denied",
            source="local-authorization",
            resources="not-direct-local",
        )
    assert len(audit_threads) == 4
    assert all(thread is not loop_thread for thread in audit_threads)
    assert store.pending() == []


def test_token_auth_lets_through_one_method_per_route_and_no_owner_route():
    for path, method in (
        ("/.well-known/oauth-authorization-server", "GET"),
        ("/oauth/authorize", "GET"),
        ("/oauth/token", "POST"),
        ("/oauth/revoke", "POST"),
    ):
        assert _BYPASS_EXACT_METHODS[path] == frozenset({method})
    assert not _exact_method_bypass("/oauth/authorize", "POST")
    assert not any(path.startswith("/v1/") for path in _BYPASS_EXACT_METHODS)
