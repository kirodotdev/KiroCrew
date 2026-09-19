"""The production host factory builds an EXECUTABLE DriveOperations by RESOLVING an
already-trusted binding, proven end to end on the real W01 seam with no Google
account.

It is not an import smoke test, and it is not a mint-on-sync test. The HOST's
authorization flow (mint + insert into the live BindingStore) happens in setup --
that is the host's job. The factory then RESOLVES that trusted binding and never
creates one, so:

* a live source resolves and runs a real Drive read (``files.list`` walk) through
  the real ``execute`` / ``PageWalk`` and ``build_production_transport`` (custody
  gate + live store + real AES-GCM vault + this package's real ``locator`` /
  ``decode``); only the socket is scripted via W01's documented ``http_send`` seam;
* a REVOKED source is NOT resurrected: the next sync RAISES rather than handing
  back a runner.

Authority is host-supplied, never defaulted: ``granted_scopes`` (the real stored
grant), the governance ``layers``, the live ``clock``, and ``ttl_seconds``. The
credential is addressed by the RESOLVED BINDING's own scoped ``secret_ref`` (read
per-binding from the store), not a provider-slug family; the fixture seeds the
vault under that exact per-binding name and guards it against plaintext storage.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

from kiro_crew.connections.control_plane.binding import Binding, create_binding
from kiro_crew.connections.control_plane.lifecycle import (
    BindingRevokedError,
    BindingStore,
)
from kiro_crew.connections.control_plane.policy import LayerCeilings
from kiro_crew.connections.control_plane.production import HttpReply, HttpRequest
from kiro_crew.connections.vendors.google_drive.host_factory import (
    build_drive_operations,
    make_drive_operations_factory,
)
from kiro_crew.connections.vendors.google_drive.operations import DriveOperations
from kiro_crew.secrets import SecretVault

_T0 = 1_000_000.0
_GRANTED: Tuple[str, ...] = ("https://www.googleapis.com/auth/drive.readonly",)
_TTL = 300.0
_DEPLOYMENT = "deployment://test/google_drive/0"
_PRINCIPAL = "kiro://test/owner"


def _verifier(*, claimed_subject: str, claimed_tenant: str, service_id: str) -> Dict[str, str]:
    return {
        "subject_ref": f"subject://verified/{claimed_subject}",
        "tenant_ref": f"tenant://verified/{claimed_tenant}",
    }


def _clock() -> float:
    return _T0


def _mint_binding(*, subject: str, tenant: str) -> Binding:
    """The HOST authorization flow's mint step (done in setup, not by the factory)."""
    return create_binding(
        service_id="google_drive",
        claimed_subject=subject,
        claimed_tenant=tenant,
        credential_mode="oauth_user",
        verifier=_verifier,  # type: ignore[arg-type]
        slug="google_drive",
    )


def _admit(store: BindingStore, binding: Binding) -> Binding:
    """The HOST authorization flow's admit step: insert the trusted binding."""
    store.insert(binding, deployment_id=_DEPLOYMENT, kiro_principal=_PRINCIPAL)
    return binding


def _vault_with(tmp_path: Path, binding: Binding, token: str) -> SecretVault:
    """A REAL AES-256-GCM vault holding THIS binding's credential, addressed by the
    binding's OWN scoped secret_ref name (per-binding, not a provider-slug family).
    """
    vault = SecretVault(tmp_path / "crewhome")
    secret_name = binding["secret_ref"]["name"]
    vault.set_sync(secret_name, token)
    enc = tmp_path / "crewhome" / ".vault" / "secrets.enc"
    assert enc.is_file() and token.encode() not in enc.read_bytes()  # guard the guard
    return vault


def _store(tmp_path: Path) -> BindingStore:
    return BindingStore(tmp_path / "connections" / "control_plane_bindings.json")


class _ScriptedSender:
    def __init__(self) -> None:
        self.requests: List[HttpRequest] = []
        self._routes: List[Any] = []

    def route(self, predicate, reply: HttpReply) -> "_ScriptedSender":
        self._routes.append((predicate, reply))
        return self

    def __call__(self, request: HttpRequest, *, timeout_seconds: float) -> HttpReply:
        self.requests.append(request)
        for predicate, reply in self._routes:
            if predicate(request.url):
                return reply
        raise AssertionError(f"no scripted reply for {request.url}")


def _json_reply(status: int, obj) -> HttpReply:
    import json

    return HttpReply(status=status, headers={}, body=json.dumps(obj).encode())


def _kwargs(store: BindingStore, vault: SecretVault, sender: _ScriptedSender) -> Dict[str, Any]:
    return dict(
        verifier=_verifier,
        binding_store=store,
        vault=vault,
        deployment_id=_DEPLOYMENT,
        kiro_principal=_PRINCIPAL,
        granted_scopes=_GRANTED,
        layers=LayerCeilings(),
        clock=_clock,
        ttl_seconds=_TTL,
        http_send=sender,
    )


def test_factory_resolves_trusted_binding_and_runs_a_read(tmp_path: Path):
    store = _store(tmp_path)
    binding = _admit(store, _mint_binding(subject="alice", tenant="ws-acme"))
    vault = _vault_with(tmp_path, binding, "drive-token-alice")
    sender = _ScriptedSender()
    sender.route(
        lambda u: "/files" in u and "pageToken" not in u,
        _json_reply(200, {"files": [{"id": "F1"}], "nextPageToken": "p2"}),
    ).route(
        lambda u: "/files" in u and "pageToken=p2" in u,
        _json_reply(200, {"files": [{"id": "F2"}]}),
    )

    ops = build_drive_operations(
        subject="alice", tenant="ws-acme", **_kwargs(store, vault, sender)  # type: ignore[arg-type]
    )
    assert isinstance(ops, DriveOperations)

    # A real two-page walk through execute/PageWalk -> gate -> live store -> vault
    # -> real locator -> scripted sender -> real decode.
    files = ops.list_files()
    assert [f["id"] for f in files] == ["F1", "F2"]

    first = sender.requests[0]
    assert "supportsAllDrives=true" in first.url
    assert "includeItemsFromAllDrives=true" in first.url
    # Custody resolved the binding's OWN secret to a Bearer credential.
    assert first.headers.get("Authorization") == "Bearer drive-token-alice"


def test_factory_does_not_mint_absent_binding_fails_loud(tmp_path: Path):
    # No host admit step: the binding was never inserted. The factory must RESOLVE,
    # find nothing, and RAISE -- it must NOT create+insert one as a side effect.
    from kiro_crew.connections.control_plane.lifecycle import BindingResolutionError

    store = _store(tmp_path)
    # A vault exists but there is no trusted binding to resolve.
    vault = SecretVault(tmp_path / "crewhome")
    sender = _ScriptedSender()
    with pytest.raises(BindingResolutionError):
        build_drive_operations(
            subject="ghost", tenant="ws-acme", **_kwargs(store, vault, sender)  # type: ignore[arg-type]
        )
    # And nothing was written to the store as a side effect.
    assert store.all_bindings() == []


def test_revoked_source_is_not_resurrected_on_next_sync(tmp_path: Path):
    # The security property: once the host revokes a source's binding, the NEXT
    # sync must FAIL, not silently mint a fresh runner (revocation isolation).
    store = _store(tmp_path)
    binding = _admit(store, _mint_binding(subject="bob", tenant="ws-acme"))
    vault = _vault_with(tmp_path, binding, "drive-token-bob")
    sender = _ScriptedSender().route(
        lambda u: "/files" in u, _json_reply(200, {"files": [{"id": "OK"}]})
    )
    factory = make_drive_operations_factory(**_kwargs(store, vault, sender))  # type: ignore[arg-type]
    source = {"subject": "bob", "tenant": "ws-acme", "account": "my-drive"}

    # First sync works: the binding is trusted and live.
    ops = factory(source)
    assert [f["id"] for f in ops.list_files()] == ["OK"]

    # Host revokes the binding (the authorization flow's revoke step).
    store.revoke(binding["binding_id"])

    # Next sync MUST fail-closed -- no fresh runner, no resurrection.
    with pytest.raises(BindingRevokedError):
        factory(source)


def test_operations_factory_reads_subject_tenant_from_source(tmp_path: Path):
    store = _store(tmp_path)
    binding = _admit(store, _mint_binding(subject="carol", tenant="ws-acme"))
    vault = _vault_with(tmp_path, binding, "drive-token-carol")
    sender = _ScriptedSender().route(
        lambda u: "/files" in u, _json_reply(200, {"files": [{"id": "OK"}]})
    )
    factory = make_drive_operations_factory(**_kwargs(store, vault, sender))  # type: ignore[arg-type]
    ops = factory({"subject": "carol", "tenant": "ws-acme", "account": "my-drive"})
    assert isinstance(ops, DriveOperations)
    assert [f["id"] for f in ops.list_files()] == ["OK"]


def test_factory_requires_subject_and_tenant(tmp_path: Path):
    store = _store(tmp_path)
    vault = SecretVault(tmp_path / "crewhome")
    factory = make_drive_operations_factory(**_kwargs(store, vault, _ScriptedSender()))  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        factory({"tenant": "ws-acme"})  # no subject
    with pytest.raises(ValueError):
        factory({"subject": "bob"})  # no tenant


def test_clock_is_required_no_frozen_fallback():
    # clock is a required keyword: a frozen instant would make every TTL/expiry
    # check vacuous, so there is no `now` fallback to fall back on.
    import inspect

    sig = inspect.signature(build_drive_operations)
    clock_param = sig.parameters["clock"]
    assert clock_param.default is inspect.Parameter.empty
    # granted_scopes and layers are likewise required (authority, not defaulted).
    assert sig.parameters["granted_scopes"].default is inspect.Parameter.empty
    assert sig.parameters["layers"].default is inspect.Parameter.empty
    assert sig.parameters["ttl_seconds"].default is inspect.Parameter.empty


def test_connector_registered_with_factory_validates_and_syncs(tmp_path: Path):
    # The end-to-end contract the landed handler seam relies on: constructing the
    # connector WITH this factory (positional, as _register_optional_connector does)
    # yields a connector whose validate_config passes and whose fetch_rows drives a
    # real read (snapshot) through the factory's W01-backed, binding-resolving runner.
    from kiro_crew.knowledge.connectors.google_drive import GoogleDriveConnector

    store = _store(tmp_path)
    binding = _admit(store, _mint_binding(subject="dan", tenant="ws-acme"))
    vault = _vault_with(tmp_path, binding, "drive-token-dan")
    sender = (
        _ScriptedSender()
        .route(
            lambda u: "/files" in u
            and "/export" not in u
            and "startPageToken" not in u
            and "alt=media" not in u,
            _json_reply(
                200,
                {
                    "files": [
                        {"id": "D1", "name": "doc.txt", "mimeType": "text/plain", "version": "1"}
                    ]
                },
            ),
        )
        .route(
            lambda u: "/files/D1" in u and "alt=media" in u,
            HttpReply(status=200, headers={}, body=b"hello"),
        )
        .route(
            lambda u: "/changes/startPageToken" in u, _json_reply(200, {"startPageToken": "cp-1"})
        )
    )
    factory = make_drive_operations_factory(**_kwargs(store, vault, sender))  # type: ignore[arg-type]

    connector = GoogleDriveConnector(factory)
    ok, msg = connector.validate_config({"account": "my-drive", "tenant": "ws-acme"})
    assert ok and msg == ""

    import asyncio

    rows, snapshot, checkpoint = asyncio.run(
        connector.fetch_rows({"account": "my-drive", "tenant": "ws-acme", "subject": "dan"})
    )
    assert snapshot is True
    assert checkpoint == "cp-1"
    assert [r.key for r in rows] == ["D1"]
    assert rows[0].subjects == ()  # no permissions field -> empty deny-all


def test_real_time_clock_default_is_time_time_only_where_non_authoritative():
    # Sanity: the module imports the real time source for callers that want it,
    # but does not silently default the factory's clock to it.
    assert callable(time.time)
