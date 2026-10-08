"""``api_mediated_secret_request`` — the HOST-side mediation endpoint.

The in-sandbox ``kirocrew-secrets`` tool cannot read the vault (managed MCP
servers share the agent's mount namespace, where ``.vault`` and
``secret_request_policy.json`` are masked). So the tool forwards the non-secret
request intent to this endpoint in the unsandboxed dashboard process, which is
the only place the vault is readable. These tests pin that the endpoint:

* runs the blocking vault-read + dispatch OFF the event loop,
* returns ONLY the sanitized response fields,
* turns a fail-closed mediation error into a safe, secret-free 400, and
* denies a request with no resolvable session identity.
"""

from __future__ import annotations

import json
import threading
from typing import Any
from unittest.mock import MagicMock

import pytest

from kiro_crew import mediated_request_capability
from kiro_crew.dashboard.handlers import sessions as sessions_mod
from kiro_crew.secrets_mediation import dispatch as dispatch_mod
from kiro_crew.secrets_mediation.dispatch import SanitizedResponse
from kiro_crew.secrets_mediation.policy import PolicyError

SESSION = "dashboard:reviewer-slot"


def _attach_body(request: MagicMock, payload: Any) -> None:
    """Serve ``payload`` through the bounded-body read path the handlers use.

    The handlers read the raw body (bounded at the 1 MiB mediated-request cap)
    BEFORE JSON-decoding, so a faithful mock must expose request.content.read and
    Content-Length, not only request.json.
    """
    encoded = payload if isinstance(payload, (bytes, bytearray)) else json.dumps(payload).encode()
    request.content_length = len(encoded)

    async def _read(n: int = -1) -> bytes:
        return bytes(encoded) if n < 0 else bytes(encoded)[:n]

    request.content = MagicMock()
    request.content.read = _read


def _request(
    payload: dict[str, Any],
    *,
    session: str = SESSION,
    internal_auth: bool = True,
    approved_capability: bool = True,
) -> MagicMock:
    request = MagicMock()
    request.headers = {"X-Session-Key": session} if session else {}
    if session and approved_capability:
        call_id = f"test-call-{id(payload)}"
        mediated_request_capability.mint_for_approved_call(
            session_key=session,
            tool_call_id=call_id,
            mcp_server_name="kirocrew-secrets",
            tool_name="call_api_with_secret",
            tool_args=payload,
            identity_trusted=True,
            args_trusted=True,
        )
        capability = mediated_request_capability.claim(session, call_id, payload)
        request.headers["X-Mediated-Secret-Capability"] = capability

    async def _json() -> Any:
        return payload

    request.json = _json
    _attach_body(request, payload)
    request.app = {"state": MagicMock()}
    request.get = lambda key, default=None: internal_auth if key == "internal_auth" else default
    return request


def _body(response: Any) -> Any:
    return json.loads(response.body.decode("utf-8"))


def _patch_common(monkeypatch: pytest.MonkeyPatch) -> None:
    # internal_memory_scope returns (scope, refusal); no private boundary here.
    async def _scope(*_a: Any, **_k: Any) -> tuple[None, None]:
        return None, None

    monkeypatch.setattr(sessions_mod, "internal_memory_scope", _scope)
    monkeypatch.setattr(sessions_mod, "_sel", lambda: MagicMock())
    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: "/nonexistent-config-dir")
    # By default the caller presents a VERIFIED member scope for its own session
    # (the approved-invocation path). member_request_scope is the gate the handler
    # now uses in place of the removed proof token: a MemberScope with
    # verified=True, the same session, and a non-empty store is a genuine member
    # session. Tests that exercise an unverified/non-member scope override this.
    from kiro_crew.dashboard.handlers import _shared as _shared_mod

    async def _verified_scope(request: Any) -> Any:
        session = request.headers.get("X-Session-Key", "")
        return _shared_mod.MemberScope(session, True, "member:%s" % session)

    monkeypatch.setattr(sessions_mod, "member_request_scope", _verified_scope, raising=False)
    monkeypatch.setattr(_shared_mod, "member_request_scope", _verified_scope)


@pytest.mark.asyncio
async def test_happy_path_returns_only_sanitized_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_common(monkeypatch)
    call_threads: list[int] = []

    def fake_perform(req: Any, _config_dir: Any) -> SanitizedResponse:
        call_threads.append(threading.get_ident())
        assert req.secret_name == "STRIPE_KEY"
        return SanitizedResponse(
            status=200,
            headers={"content-type": "application/json"},
            body='{"ok":true}',
            truncated=False,
            final_url_origin="https://api.stripe.com",
        )

    monkeypatch.setattr(dispatch_mod, "perform_mediated_request", fake_perform)

    loop_thread = threading.get_ident()
    resp = await sessions_mod.api_mediated_secret_request(
        _request(
            {"secret_name": "STRIPE_KEY", "method": "GET", "url": "https://api.stripe.com/v1/x"}
        )
    )
    assert resp.status == 200
    assert _body(resp) == {
        "status": 200,
        "headers": {"content-type": "application/json"},
        "body": '{"ok":true}',
        "truncated": False,
        "final_url_origin": "https://api.stripe.com",
    }
    # The blocking vault-read + dispatch must not run on the loop thread.
    assert call_threads and loop_thread not in call_threads


@pytest.mark.asyncio
async def test_mediation_error_becomes_a_safe_400(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_common(monkeypatch)

    def fake_perform(req: Any, _config_dir: Any) -> SanitizedResponse:
        raise PolicyError(
            "Secret 'STRIPE_KEY' is authorized only for https://api.stripe.com, not "
            "https://evil.example. The request was refused before the secret was read."
        )

    monkeypatch.setattr(dispatch_mod, "perform_mediated_request", fake_perform)

    resp = await sessions_mod.api_mediated_secret_request(
        _request({"secret_name": "STRIPE_KEY", "method": "GET", "url": "https://evil.example/x"})
    )
    assert resp.status == 400
    payload = _body(resp)
    assert payload["code"] == "mediation_refused"
    assert "refused before the secret was read" in payload["error"]


@pytest.mark.asyncio
async def test_missing_session_key_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_common(monkeypatch)
    resp = await sessions_mod.api_mediated_secret_request(
        _request(
            {"secret_name": "K", "method": "GET", "url": "https://api.example.com"}, session=""
        )
    )
    assert resp.status == 400
    assert _body(resp)["error"] == "X-Session-Key required"


@pytest.mark.asyncio
async def test_missing_secret_name_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_common(monkeypatch)
    resp = await sessions_mod.api_mediated_secret_request(
        _request({"method": "GET", "url": "https://api.example.com"})
    )
    assert resp.status == 400
    assert _body(resp)["error"] == "secret_name is required"


@pytest.mark.asyncio
async def test_a_non_internal_secret_caller_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """A credential-bearing egress must require the proven internal-secret
    authority — a browser cookie that reaches the strict path (or a mixed-mode
    reclassification) must not drive a mediated request that skips MCP approval.
    Fails without the ``internal_auth is True`` guard at the top of the handler.
    """
    _patch_common(monkeypatch)

    def fake_perform(req: Any, _config_dir: Any) -> SanitizedResponse:  # pragma: no cover
        raise AssertionError("dispatch must not run for a non-internal caller")

    monkeypatch.setattr(dispatch_mod, "perform_mediated_request", fake_perform)
    resp = await sessions_mod.api_mediated_secret_request(
        _request(
            {"secret_name": "K", "method": "GET", "url": "https://api.example.com"},
            internal_auth=False,
        )
    )
    assert resp.status == 403
    assert _body(resp)["code"] == "internal_auth_required"


@pytest.mark.asyncio
async def test_a_missing_or_invalid_member_proof_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller with the loopback secret + session key but WITHOUT an attested
    member scope (a raw in-sandbox shell that cannot present the Unix-socket peer
    attestation or the launcher's signed token) is refused 403 before any
    dispatch — binding the egress to an approved kirocrew-secrets invocation.
    """
    _patch_common(monkeypatch)
    # Override the default: member_request_scope returns an UNVERIFIED scope
    # (the transport cannot vouch for the declared session), exactly what a raw
    # in-sandbox caller gets.
    from kiro_crew.dashboard.handlers import _shared as _shared_mod

    async def _unverified(request: Any) -> Any:
        session = request.headers.get("X-Session-Key", "")
        return _shared_mod.MemberScope(session, False, None)

    monkeypatch.setattr(sessions_mod, "member_request_scope", _unverified)
    monkeypatch.setattr(_shared_mod, "member_request_scope", _unverified)

    def fake_perform(req: Any, _config_dir: Any) -> SanitizedResponse:  # pragma: no cover
        raise AssertionError("dispatch must not run without a verified member scope")

    monkeypatch.setattr(dispatch_mod, "perform_mediated_request", fake_perform)
    resp = await sessions_mod.api_mediated_secret_request(
        _request({"secret_name": "K", "method": "GET", "url": "https://api.example.com"})
    )
    assert resp.status == 403
    assert _body(resp)["code"] == "member_scope_required"


@pytest.mark.asyncio
async def test_replayed_session_key_without_approved_call_capability_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_common(monkeypatch)
    payload = {"secret_name": "K", "method": "GET", "url": "https://api.example.com"}
    resp = await sessions_mod.api_mediated_secret_request(
        _request(payload, approved_capability=False)
    )
    assert resp.status == 403
    assert _body(resp)["code"] == "approved_call_capability_required"


@pytest.mark.asyncio
async def test_approved_call_capability_is_consumed_once(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_common(monkeypatch)
    payload = {"secret_name": "K", "method": "GET", "url": "https://api.example.com"}

    def fake_perform(req: Any, _config_dir: Any) -> SanitizedResponse:
        return SanitizedResponse(200, {}, "", False, "https://api.example.com")

    monkeypatch.setattr(dispatch_mod, "perform_mediated_request", fake_perform)
    request = _request(payload)
    first = await sessions_mod.api_mediated_secret_request(request)
    assert first.status == 200
    replay = await sessions_mod.api_mediated_secret_request(request)
    assert replay.status == 403
    assert _body(replay)["code"] == "approved_call_capability_required"


class _RecordingSel:
    """A ``_sel()`` stand-in that records every ``log_api_access`` call."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def log_api_access(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)


def _capability_request(
    payload: dict[str, Any],
    *,
    session: str = SESSION,
    internal_auth: bool = True,
) -> MagicMock:
    request = MagicMock()
    request.headers = {"X-Session-Key": session} if session else {}

    async def _json() -> Any:
        return payload

    request.json = _json
    _attach_body(request, payload)
    request.app = {"state": MagicMock()}
    request.get = lambda key, default=None: internal_auth if key == "internal_auth" else default
    return request


@pytest.mark.asyncio
async def test_capability_grant_is_audited(monkeypatch: pytest.MonkeyPatch) -> None:
    """F2: a GRANTED single-use capability leaves an audit record.

    ``api_mediated_secret_capability`` mints the authorization that later unlocks
    a credential-bearing request, so the grant itself must be recorded, matching
    the four decisions the adjacent request handler audits.
    """
    _reset_capability_registries()
    _patch_common(monkeypatch)
    recording = _RecordingSel()
    monkeypatch.setattr(sessions_mod, "_sel", lambda: recording)
    intent = {"secret_name": "K", "method": "GET", "url": "https://api.example.com"}
    call_id = "approved-call-1"
    mediated_request_capability.mint_for_approved_call(
        session_key=SESSION,
        tool_call_id=call_id,
        mcp_server_name="kirocrew-secrets",
        tool_name="call_api_with_secret",
        tool_args=intent,
        identity_trusted=True,
        args_trusted=True,
    )
    resp = await sessions_mod.api_mediated_secret_capability(
        _capability_request({"tool_call_id": call_id, "request": intent})
    )
    assert resp.status == 200
    assert "capability" in _body(resp)
    grants = [c for c in recording.calls if c["operation"] == "mediated_secret_capability"]
    assert grants and grants[-1]["outcome"] == "ok"
    _reset_capability_registries()


@pytest.mark.asyncio
async def test_capability_refusal_is_audited(monkeypatch: pytest.MonkeyPatch) -> None:
    """F2: a claim matching no approved tool call is a REFUSED authorization and
    must also leave a record (a replay or a forged claim is not silent)."""
    _reset_capability_registries()
    _patch_common(monkeypatch)
    recording = _RecordingSel()
    monkeypatch.setattr(sessions_mod, "_sel", lambda: recording)
    resp = await sessions_mod.api_mediated_secret_capability(
        _capability_request({"tool_call_id": "never-approved", "request": {"secret_name": "K"}})
    )
    assert resp.status == 403
    denials = [
        c
        for c in recording.calls
        if c["operation"] == "mediated_secret_capability" and c["outcome"] == "denied"
    ]
    assert denials
    _reset_capability_registries()


@pytest.mark.asyncio
async def test_a_collapsed_refusal_is_audited_as_ok_like_a_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A post-send refusal collapsed to the withheld constant is audited as the
    SAME constant ``ok`` a success is -- ``security_events.jsonl`` is a
    sandbox-visible leaf, so a refused-vs-ok bit there would be a readable one-bit
    oracle on the authorized origin's behaviour. The external response is the fixed
    constant with no marker field, and the audit line reveals nothing either."""
    _patch_common(monkeypatch)
    recording = _RecordingSel()
    monkeypatch.setattr(sessions_mod, "_sel", lambda: recording)

    def fake_perform(req: Any, _config_dir: Any) -> SanitizedResponse:
        # A post-send collapse returns the plain withheld constant -- there is no
        # longer any internal marker to set.
        return SanitizedResponse(
            dispatch_mod._WITHHELD_STATUS, {}, "", False, "https://api.example.com"
        )

    monkeypatch.setattr(dispatch_mod, "perform_mediated_request", fake_perform)
    resp = await sessions_mod.api_mediated_secret_request(
        _request({"secret_name": "K", "method": "GET", "url": "https://api.example.com"})
    )
    # The HTTP envelope is a normal 200; the withheld status 0 rides in the body,
    # and the body carries no internal marker field.
    assert resp.status == 200
    body = _body(resp)
    assert body["status"] == dispatch_mod._WITHHELD_STATUS
    assert "refusal_outcome" not in body
    completions = [c for c in recording.calls if c["operation"] == "mediated_secret_request"]
    # The audit line for a post-send result records the constant ``ok`` -- it does
    # NOT distinguish refused from ok, so it leaks no outcome bit.
    assert completions and completions[-1]["outcome"] == "ok"


def test_sandboxed_tool_has_no_import_path_to_the_vault() -> None:
    """``mcp_secrets`` runs in the agent sandbox where the vault is masked.

    It MUST forward to the host endpoint rather than read the vault itself, so
    its module must not import the vault or the host-only dispatch module. This
    pins the fix for the placement bug: putting ``SecretVault``/
    ``perform_mediated_request`` back into the sandboxed process would make every
    call fail at runtime (empty vault) and is exactly the regression to catch.
    """
    import ast
    from pathlib import Path

    import kiro_crew.mcp_secrets as mcp_secrets_mod

    source = Path(mcp_secrets_mod.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name)

    forbidden = {
        "kiro_crew.secrets",
        "kiro_crew.secrets.SecretVault",
        "kiro_crew.secrets_mediation.dispatch",
        "kiro_crew.secrets_mediation.policy",
        "kiro_crew.secrets_mediation.ssrf",
    }
    leaked = imported & forbidden
    assert not leaked, f"the sandboxed tool must not import the vault/dispatch: {leaked}"


def _reset_capability_registries() -> None:
    with mediated_request_capability._lock:
        mediated_request_capability._pending.clear()
        mediated_request_capability._active.clear()
        mediated_request_capability._overflow_refusals = 0
        mediated_request_capability._overflow_last_logged_at = float("-inf")
        mediated_request_capability._overflow_suppressed = 0


def _mint(session_key: str, call_id: str) -> str:
    return mediated_request_capability.mint_for_approved_call(
        session_key=session_key,
        tool_call_id=call_id,
        mcp_server_name="kirocrew-secrets",
        tool_name="call_api_with_secret",
        tool_args={"secret_name": "K", "method": "GET", "url": "https://api.example.com"},
        identity_trusted=True,
        args_trusted=True,
    )


def test_capability_shared_cap_fails_closed_and_counts_overflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ONE ceiling bounds pending AND active grants together. At the ceiling a new
    mint is refused (empty token) and counted — never a silent eviction of a valid
    grant, and claiming (pending->active) never lets the total exceed the cap."""
    _reset_capability_registries()
    monkeypatch.setattr(mediated_request_capability, "_MAX_OUTSTANDING", 3)
    try:
        assert _mint("s", "c0")
        assert _mint("s", "c1")
        # Move c1 pending->active; total is still 2, cap not reached.
        tok = mediated_request_capability.claim(
            "s", "c1", {"secret_name": "K", "method": "GET", "url": "https://api.example.com"}
        )
        assert tok
        assert _mint("s", "c2")  # total now 3 (2 pending + 1 active) — at cap
        # A NEW (session, call) mint at the ceiling is refused, counted, and evicts nothing.
        assert _mint("s", "c3") == ""
        assert mediated_request_capability.outstanding_overflow_refusals() == 1
        assert (
            len(mediated_request_capability._pending) + len(mediated_request_capability._active)
            == 3
        )
        # Re-minting an EXISTING key replaces its own grant and is exempt from the cap.
        assert _mint("s", "c0")
    finally:
        _reset_capability_registries()


def test_capability_overflow_emits_a_bounded_coalescing_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Overflow is OBSERVABLE but BOUNDED: the first refusal in a window logs a
    warning naming capacity overflow (not a no-match), and further refusals within
    the window are coalesced (no new line) and reported in the next emitted
    warning — so a sustained burst at the ceiling cannot flood the log."""
    import logging

    _reset_capability_registries()
    monkeypatch.setattr(mediated_request_capability, "_MAX_OUTSTANDING", 1)
    try:
        assert _mint("s", "c0")  # fills the single slot
        with caplog.at_level(logging.WARNING, logger=mediated_request_capability.__name__):
            # First overflow in the window -> one warning, naming capacity overflow.
            assert _mint("s", "c1") == ""
            # Two more within the window -> coalesced, no additional warning line.
            assert _mint("s", "c2") == ""
            assert _mint("s", "c3") == ""
        overflow_lines = [r for r in caplog.records if "capacity overflow" in r.getMessage()]
        assert len(overflow_lines) == 1, "burst must coalesce to one line, not flood"
        assert mediated_request_capability.outstanding_overflow_refusals() == 3
        assert mediated_request_capability._overflow_suppressed == 2

        # Advancing past the window lets the next overflow emit, reporting the
        # coalesced count.
        mediated_request_capability._overflow_last_logged_at = float("-inf")
        with caplog.at_level(logging.WARNING, logger=mediated_request_capability.__name__):
            caplog.clear()
            assert _mint("s", "c4") == ""
        after = [r for r in caplog.records if "capacity overflow" in r.getMessage()]
        assert len(after) == 1
        assert "coalesced" in after[0].getMessage()
        assert mediated_request_capability._overflow_suppressed == 0
    finally:
        _reset_capability_registries()


def test_capability_rejects_overlong_identifiers() -> None:
    """Retained caller identifiers are length-bounded at the retention point."""
    _reset_capability_registries()
    try:
        long_id = "x" * (mediated_request_capability._MAX_ID_LEN + 1)
        assert _mint(long_id, "c0") == ""
        assert _mint("s", long_id) == ""
        assert not mediated_request_capability._pending
    finally:
        _reset_capability_registries()


def test_capability_codex_wrapper_approval_matches_inner_arguments_claim() -> None:
    """codex-acp hands the approval a wrapped {server, tool, arguments} payload,
    while the tool's claim at the endpoint digests the inner request arguments.
    The mint must digest the SAME inner arguments or the two digests never match
    and every approved Codex MCP call is refused 403. Non-Codex (bare arguments)
    stays unchanged.
    """
    _reset_capability_registries()
    try:
        inner = {"secret_name": "K", "method": "GET", "url": "https://api.example.com"}
        # Approval sees the Codex wrapper...
        token = mediated_request_capability.mint_for_approved_call(
            session_key="s",
            tool_call_id="c0",
            mcp_server_name="kirocrew-secrets",
            tool_name="call_api_with_secret",
            tool_args={
                "server": "kirocrew-secrets",
                "tool": "call_api_with_secret",
                "arguments": inner,
            },
            identity_trusted=True,
            args_trusted=True,
        )
        assert token
        # ...and the claim, which carries the inner request the tool sends, matches.
        claimed = mediated_request_capability.claim("s", "c0", inner)
        assert claimed == token

        # A wrapper carrying DIFFERENT inner arguments must NOT match the claim.
        _reset_capability_registries()
        token2 = mediated_request_capability.mint_for_approved_call(
            session_key="s",
            tool_call_id="c1",
            mcp_server_name="kirocrew-secrets",
            tool_name="call_api_with_secret",
            tool_args={
                "server": "kirocrew-secrets",
                "tool": "call_api_with_secret",
                "arguments": {**inner, "url": "https://evil.example.com"},
            },
            identity_trusted=True,
            args_trusted=True,
        )
        assert token2
        assert mediated_request_capability.claim("s", "c1", inner) == ""
    finally:
        _reset_capability_registries()


def test_capability_oversized_int_timeout_is_caught_not_crash() -> None:
    """An int timeout_s beyond float range raises OverflowError in float(); the
    mint/claim/consume digest paths must catch it and refuse (empty token / False),
    never let it escape and terminate the approving turn."""
    _reset_capability_registries()
    try:
        huge = 10**400  # int-typed, beyond float range -> float() OverflowError
        args = {
            "secret_name": "K",
            "method": "GET",
            "url": "https://api.example.com",
            "timeout_s": huge,
        }
        token = mediated_request_capability.mint_for_approved_call(
            session_key="s",
            tool_call_id="c0",
            mcp_server_name="kirocrew-secrets",
            tool_name="call_api_with_secret",
            tool_args=args,
            identity_trusted=True,
            args_trusted=True,
        )
        assert token == ""
        assert mediated_request_capability.claim("s", "c0", args) == ""
        assert mediated_request_capability.consume("s", "tok", args) is False
    finally:
        _reset_capability_registries()


def test_capability_canceled_then_denied_call_id_cannot_claim_a_stale_grant() -> None:
    """A grant must not outlive the approval decision for its call id. Approve a
    call (mint), cancel it before the token is claimed, then reuse the SAME call id
    with IDENTICAL arguments and deny it: the stale approval must not be claimable,
    so no secret-bearing egress can be authorized by the earlier approval.
    invalidate_call (the cancel/deny hook) removes the grant by call id, so the
    reused+denied id has nothing to claim."""
    _reset_capability_registries()
    args = {"secret_name": "K", "method": "GET", "url": "https://api.example.com"}
    try:
        # 1. Approve: a grant is minted and pending.
        token1 = _mint("s", "call-X")
        assert token1
        assert ("s", "call-X") in mediated_request_capability._pending

        # 2. Cancel BEFORE the token is claimed — the grant must be invalidated,
        #    not left behind for a reused id to pick up.
        mediated_request_capability.invalidate_call("s", "call-X")
        assert ("s", "call-X") not in mediated_request_capability._pending

        # 3. The SAME call id is reused with IDENTICAL args, but this time DENIED.
        #    The deny path invalidates the call id again (idempotent) and mints
        #    nothing, so there is no grant for this id at all.
        mediated_request_capability.invalidate_call("s", "call-X")

        # 4. A claim against the reused id with the identical args finds no grant —
        #    the stale approval is gone, so no egress can be authorized.
        assert mediated_request_capability.claim("s", "call-X", args) == ""
        # And the original token can never be consumed (it was never claimed/active).
        assert mediated_request_capability.consume("s", token1, args) is False
    finally:
        _reset_capability_registries()


def test_capability_invalidate_call_drops_an_already_claimed_active_grant() -> None:
    """Defense in depth: if a claim raced in and moved the grant to _active before
    the deny landed, invalidate_call must drop that active grant too, so a denial
    can still stop egress the claim had already staged."""
    _reset_capability_registries()
    args = {"secret_name": "K", "method": "GET", "url": "https://api.example.com"}
    try:
        token = _mint("s", "call-Y")
        assert token
        # A claim races in, moving the grant pending -> active.
        claimed = mediated_request_capability.claim("s", "call-Y", args)
        assert claimed == token
        assert token in mediated_request_capability._active

        # The deny lands AFTER the claim: invalidate_call must still drop the
        # active grant so the staged token cannot be consumed.
        mediated_request_capability.invalidate_call("s", "call-Y")
        assert token not in mediated_request_capability._active
        assert mediated_request_capability.consume("s", token, args) is False
    finally:
        _reset_capability_registries()


def _oversize_payload() -> bytes:
    """Valid JSON whose encoded size exceeds the 1 MiB mediated-request cap.

    Valid JSON on purpose: the only reason the handler may reject it is the size
    bound BEFORE decoding — a parse failure would be the wrong signal.
    """
    cap = mediated_request_capability.MAX_REQUEST_PAYLOAD_BYTES
    big = {
        "secret_name": "K",
        "method": "GET",
        "url": "https://api.example.com",
        "pad": "x" * (cap + 4096),
    }
    body = json.dumps(big).encode("utf-8")
    assert len(body) > cap
    json.loads(body)  # it IS parseable; the cap, not a parse error, must reject it
    return body


@pytest.mark.asyncio
async def test_request_endpoint_rejects_oversize_body_before_parsing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An oversize body to the request endpoint is rejected (413) BEFORE JSON
    decoding and before any vault-read/dispatch — not OOM, not full-parse."""
    _reset_capability_registries()
    _patch_common(monkeypatch)
    dispatched: list[int] = []

    def fake_perform(req: Any, _config_dir: Any) -> SanitizedResponse:
        dispatched.append(1)
        raise AssertionError("dispatch must not run for an oversize body")

    monkeypatch.setattr(dispatch_mod, "perform_mediated_request", fake_perform)

    request = _request({"secret_name": "K", "method": "GET", "url": "https://api.example.com"})
    _attach_body(request, _oversize_payload())
    # No declared Content-Length: force the body-READ cap (not the header check)
    # to be the thing that rejects, so a client that omits/lies about the length
    # cannot stream an unbounded body that is parsed in full.
    request.content_length = None
    # request.json would succeed (valid JSON) — prove the SIZE path rejects first.
    resp = await sessions_mod.api_mediated_secret_request(request)
    assert resp.status == 413
    assert _body(resp)["code"] == "request_too_large"
    assert dispatched == []


@pytest.mark.asyncio
async def test_capability_endpoint_rejects_oversize_body_before_parsing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An oversize body to the capability endpoint is rejected (413) BEFORE JSON
    decoding and before any claim lookup."""
    _reset_capability_registries()
    _patch_common(monkeypatch)
    claimed: list[int] = []

    def fake_claim(*_a: Any, **_k: Any) -> str:
        claimed.append(1)
        raise AssertionError("claim must not run for an oversize body")

    monkeypatch.setattr(mediated_request_capability, "claim", fake_claim)

    request = _capability_request({"tool_call_id": "c0", "request": {"secret_name": "K"}})
    _attach_body(request, _oversize_payload())
    resp = await sessions_mod.api_mediated_secret_capability(request)
    assert resp.status == 413
    assert _body(resp)["code"] == "request_too_large"
    assert claimed == []
