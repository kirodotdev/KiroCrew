"""Shared test harness: a REAL W01 execute/PageWalk-backed OperationRunner over a
scripted HTTP-reply transport, plus a Drive binding/handle.

This is what lets the Google Drive connector, operations sequences, and ACL probe
be proved with NO Google account while still running on the TRUE W01 path: the
runner calls the real :func:`~kiro_crew.connections.control_plane.executor.execute`
and :class:`~kiro_crew.connections.control_plane.executor.PageWalk`, and the fake
transport (a) builds the request through the REAL
:func:`kiro_crew.connections.vendors.google_drive.locator.locate`, (b) matches a
scripted :class:`HttpReply` by URL, and (c) decodes it through the REAL
:func:`kiro_crew.connections.vendors.google_drive.decode.for_operation`. So the
Drive locator + decode + the full gate chain are all exercised; only the socket
is scripted.
"""

from __future__ import annotations

from typing import Any, Callable, List, Mapping, Optional, Tuple

from kiro_crew.connections.control_plane.auth_modes import declare_permitted_modes
from kiro_crew.connections.control_plane.binding import Binding, VerifiedIdentity, create_binding
from kiro_crew.connections.control_plane.executor import (
    ExecutionOutcome,
    PageWalk,
    TransportResponse,
    execute,
)
from kiro_crew.connections.control_plane.handle import DerivedHandle, derive_handle
from kiro_crew.connections.control_plane.operation import OperationDescriptor
from kiro_crew.connections.control_plane.policy import LayerCeilings
from kiro_crew.connections.control_plane.production import HttpReply, HttpRequest
from kiro_crew.connections.vendors.google_drive import decode as drive_decode
from kiro_crew.connections.vendors.google_drive import locator as drive_locator

_T0 = 1_000_000.0
_GRANTED = ("drive.readonly",)


def _verifier(*, claimed_subject, claimed_tenant, service_id) -> VerifiedIdentity:
    return {
        "subject_ref": f"subject://verified/{claimed_subject}",
        "tenant_ref": f"tenant://verified/{claimed_tenant}",
    }


def drive_binding(subject: str = "alice", account: str = "0AShared") -> Binding:
    return create_binding(
        service_id="google_drive",
        claimed_subject=subject,
        claimed_tenant=account,
        credential_mode="oauth_user",
        verifier=_verifier,
        slug="google_drive",
    )


def drive_handle(binding: Optional[Binding] = None, ttl: float = 300.0) -> DerivedHandle:
    return derive_handle(
        binding or drive_binding(),
        granted_scopes=_GRANTED,
        requested_scopes=("drive.readonly",),
        now=_T0,
        ttl_seconds=ttl,
    )


class ScriptedReplyTransport:
    """A W01 Transport that builds the request via the REAL Drive locator, matches
    a scripted HttpReply by URL predicate, and decodes via the REAL Drive decode.

    Records every built request so a test can assert (e.g.) that both drive flags
    are present, that a native type went to /export, that a binary used alt=media.
    """

    def __init__(self):
        self._routes: List[Tuple[Callable[[str, Mapping[str, Any]], bool], HttpReply]] = []
        self.requests: List[HttpRequest] = []

    def route(self, predicate, reply: HttpReply):
        self._routes.append((predicate, reply))
        return self

    def __call__(
        self,
        *,
        service_id,
        credential_mode,
        descriptor,
        request_args,
        request_idempotency_key="",
        trusted_view=None,
        **_ignored,
    ) -> TransportResponse:
        # (a) REAL locator builds the concrete request.
        request = drive_locator.locate(
            service_id=service_id,
            credential_mode=credential_mode,
            descriptor=descriptor,
            request_args=request_args,
            request_idempotency_key=request_idempotency_key,
        )
        self.requests.append(request)
        # (b) match a scripted reply by URL.
        reply = None
        for predicate, scripted in self._routes:
            if predicate(request.url, request_args):
                reply = scripted
                break
        if reply is None:
            raise AssertionError(f"no scripted reply for {request.url}")
        # non-2xx -> a classified error outcome (executor maps status to class).
        if reply.status < 200 or reply.status >= 300:
            return TransportResponse(
                http_status=reply.status,
                detail=_detail_from(reply),
            )
        # (c) REAL decode produces the OperationResult.
        result = drive_decode.for_operation(descriptor)(reply)
        return TransportResponse(http_status=reply.status, result=result)


def _detail_from(reply: HttpReply) -> str:
    """Surface a Drive error reason in the detail so the operations layer's
    stale-page-token recogniser (which reads the detail) can see it -- mirrors
    what W01's production transport passes through."""
    try:
        import json

        body = json.loads(reply.body.decode("utf-8")) if reply.body else {}
        err = body.get("error", {}) if isinstance(body, dict) else {}
        errs = err.get("errors") if isinstance(err, dict) else None
        reason = ""
        if isinstance(errs, list) and errs and isinstance(errs[0], dict):
            reason = errs[0].get("reason", "")
        return f"{err.get('message', '')} {reason}".strip()
    except Exception:
        return ""


class W01Runner:
    """An OperationRunner that runs each op through the REAL execute/PageWalk."""

    def __init__(self, transport: ScriptedReplyTransport, handle: Optional[DerivedHandle] = None):
        self._transport = transport
        self._handle = handle or drive_handle()

    def _kw(self, descriptor: OperationDescriptor):
        # A connector operation is governed under the live ``tools`` catalog
        # scope (the same scope W01's own executor tests use), with the
        # per-operation id as the governed item. "knowledge" is NOT a
        # SCOPE_CATALOG member, so it is deny-by-default and never reaches the
        # transport -- see policy.decide / SCOPE_CATALOG.
        return dict(
            now=_T0,
            offered_mode="oauth_user",
            permitted=declare_permitted_modes(("oauth_user",)),
            layers=LayerCeilings(),
            governance_scope="tools",
            governance_item=descriptor["operation_id"],
        )

    def run(self, descriptor: OperationDescriptor, request_args) -> ExecutionOutcome:
        return execute(
            descriptor,
            self._handle,
            self._transport,
            request_args=dict(request_args),
            **self._kw(descriptor),
        )

    def walk(self, descriptor: OperationDescriptor, base_args):
        walk = PageWalk(
            descriptor=descriptor,
            handle=self._handle,
            transport=self._transport,
            offered_mode="oauth_user",
            permitted=declare_permitted_modes(("oauth_user",)),
            layers=LayerCeilings(),
            governance_scope="tools",
            governance_item=descriptor["operation_id"],
            clock=lambda: _T0,
            base_args=dict(base_args),
        )
        while not walk.done:
            yield walk.next()


def json_reply(status: int, obj, headers=None) -> HttpReply:
    import json

    return HttpReply(status=status, headers=headers or {}, body=json.dumps(obj).encode())


def bytes_reply(status: int, data: bytes, headers=None) -> HttpReply:
    return HttpReply(status=status, headers=headers or {}, body=data)
