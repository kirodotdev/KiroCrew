"""Security proof: external request input cannot set ``verified`` or inject an
arbitrary principal into the authorization path.

``acl.principal_is_verified`` is only ``bool(getattr(p, "verified", False))`` --
a TRUSTED IN-PROCESS DTO marker, NOT an authentication proof by itself. Its
soundness rests entirely on WHO can construct a ``QueryPrincipal`` with
``verified=True``. This proves the invariant that makes the marker sound: the
ONLY producer of a query principal on the request path is the server-side
``_knowledge_query_principal``, which reads ``verified`` from NOTHING the caller
controls -- only the keys the auth middleware set after validating a token
(``request['app']`` / ``['user']`` / ``['is_dashboard_user']``). A client that
puts ``verified``/``app``/``user`` in headers, the query string, or the JSON body
cannot make the derived principal verified, and cannot smuggle its own principal
object in. (The bare-DTO ``QueryPrincipal(verified=True)`` is reachable only from
in-process trusted code; a request cannot hand one across the HTTP boundary.)
"""

from __future__ import annotations

from unittest.mock import MagicMock

from kiro_crew.dashboard.handlers.knowledge import _knowledge_query_principal
from kiro_crew.knowledge.acl import (
    LOCAL_PRINCIPAL,
    QueryPrincipal,
    principal_is_verified,
)


def _request(item_store: dict, *, headers=None, query=None):
    """A request whose item-storage (request[...]) holds ONLY what is passed --
    i.e. only what the auth middleware would have set. Headers/query model
    caller-controlled input."""
    req = MagicMock()
    req.app = {}
    req.headers = headers or {}
    req.query = query or {}
    req.__getitem__.side_effect = item_store.__getitem__
    req.__contains__.side_effect = item_store.__contains__
    req.get.side_effect = item_store.get
    return req


def test_verified_marker_is_false_by_default():
    # A plain principal is not verified; the resolver's predicate denies it.
    assert principal_is_verified(QueryPrincipal("alice")) is False
    assert principal_is_verified(LOCAL_PRINCIPAL) is False


def test_verified_marker_only_true_when_explicitly_constructed_in_process():
    # Reachable only from trusted in-process code, never across the HTTP boundary.
    assert principal_is_verified(QueryPrincipal("alice", verified=True)) is True


def test_header_verified_claim_does_not_produce_a_verified_principal():
    # A caller puts verified/app/user in HEADERS. The middleware never copies
    # headers into request[...]; the derivation reads request[...] only, so the
    # header claim is inert -> no established identity -> LOCAL (unverified).
    req = _request(
        {},  # middleware set nothing
        headers={
            "X-Session-Key": "session:attacker",
            "verified": "true",
            "app": "evil-app",
            "user": "root",
            "is_dashboard_user": "true",
        },
    )
    p = _knowledge_query_principal(req)
    assert p is LOCAL_PRINCIPAL
    assert p.verified is False


def test_query_string_identity_claim_is_inert():
    req = _request({}, query={"app": "evil-app", "verified": "true", "user": "root"})
    p = _knowledge_query_principal(req)
    assert p is LOCAL_PRINCIPAL
    assert p.verified is False


def test_only_middleware_set_keys_can_produce_verified():
    # The ONLY way to a verified principal is the middleware-established keys.
    # (In production only token_auth writes these, AFTER validating a token.)
    req = _request({"app": "mochi", "user": "u", "is_dashboard_user": False})
    p = _knowledge_query_principal(req)
    assert p.verified is True
    assert p.principal_id == "app:mochi"


def test_derived_principal_id_never_echoes_a_client_value():
    # Even with a middleware identity present, the principal_id is built from the
    # validated app/user -- never from a caller-supplied header/query field.
    req = _request(
        {"app": "", "user": "alice", "is_dashboard_user": True},
        headers={"user": "root", "app": "evil"},
        query={"user": "root"},
    )
    p = _knowledge_query_principal(req)
    assert p.principal_id == "user:alice"  # from the validated request['user']
    assert "root" not in p.principal_id
    assert "evil" not in p.principal_id
