"""Request prologue shared by every Spec Builder route.

Authentication, the JSON body, and the creation identity a client rendered. The
identity is what makes a stale tab detectable: a name alone does not identify a
creation, because delete plus re-import can reuse both the name and the
directory, while the per-creation slot key cannot be reused.
"""

from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

from aiohttp import web

from ..repository import _audit, _touch_spec


def _require_auth(request: web.Request) -> web.Response | None:
    """Trust only the middleware-set user; otherwise return a 401 response."""
    if request.get("user") is not None:
        return None
    return web.json_response({"code": "unauthorized", "error": "Unauthorized"}, status=401)


def _require_interactive_user(request: web.Request) -> web.Response | None:
    """Refuse app-token callers where the request becomes a human-authored turn."""
    if denied := _require_auth(request):
        return denied
    if request.get("app"):
        _audit("spec_interactive_user_denied", outcome="denied")
        return web.json_response(
            {
                "code": "interactive_user_required",
                "error": "an interactive user is required for this action",
            },
            status=403,
        )
    return None


async def _read_json(request: web.Request):
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"code": "invalid_json", "error": "invalid JSON body"}, status=400)
    if not isinstance(body, dict):
        return web.json_response(
            {"code": "body_not_object", "error": "body must be a JSON object"}, status=400
        )
    return body


#: Returned when the client's rendered spec identity does not match the index.
_STALE_CLIENT_ERROR = "spec was deleted or recreated; reload and retry"


class _ClientClaim(NamedTuple):
    """What the client believes it is acting on. Both fields are optional."""

    spec_dir: str
    slot_key: str


async def _client_claim(request: web.Request) -> _ClientClaim:
    """The identity the CLIENT rendered, from the JSON body or the query string.

    Carries the per-creation ``slot_key`` as well as ``spec_dir``, because a
    directory does NOT identify a creation: deleting a spec leaves its documents on
    disk by design, so re-importing under the same name AND path produces a
    different spec with the same spec_dir -- and a stale tab's Pause would then
    cancel the replacement's run. The slot key is minted per creation, so it is the
    field that actually distinguishes them.

    Optional by design: a control that sends nothing cannot be pinned (an older tab
    predates these fields), so callers treat "" as unpinned rather than refusing. A
    DELETE carries them as query parameters because it has no body.
    """
    dir_claim = str(request.query.get("spec_dir", "") or "").strip()
    key_claim = str(request.query.get("slot_key", "") or "").strip()
    if not (dir_claim and key_claim) and request.can_read_body:
        try:
            body = await request.json()
        except Exception:
            body = None
        if isinstance(body, dict):
            dir_claim = dir_claim or str(body.get("spec_dir", "") or "").strip()
            key_claim = key_claim or str(body.get("slot_key", "") or "").strip()
    return _ClientClaim(dir_claim, key_claim)


def _client_identity_mismatch(
    claim: _ClientClaim, actual_dir: Path | str, actual_slot_key: str = ""
) -> bool:
    """True when the client named a DIFFERENT spec than the one we resolved.

    Either field is enough to refuse, and the SLOT KEY is the decisive one: two
    specs can share a directory across a delete + re-import, but never a
    per-creation key. A field the client did not send is not compared, so an older
    tab keeps working (unpinned, as before).
    """
    if claim.spec_dir and claim.spec_dir != str(actual_dir):
        return True
    return bool(claim.slot_key) and bool(actual_slot_key) and claim.slot_key != actual_slot_key


async def _pinned_entry(request: web.Request, name: str, body: dict) -> dict | web.Response:
    """Resolve the spec FRESH, pinned to the identity the client rendered.

    The shared prologue for the lifecycle controls (approve, task, title, archive,
    duplicate), kept in one place because the pinning argument is subtle and six
    copies of it would drift: the body read is an await, so the entry has to be
    re-read after it, and the client's captured ``spec_dir`` + ``slot_key`` are what
    make a stale tab detectable. These controls require both fields: treating an
    absent claim as unpinned would let a control rendered before detail loaded
    mutate whichever creation currently owns the same name.
    """
    claimed_dir = str(body.get("spec_dir", "") or "").strip()
    claimed_key = str(body.get("slot_key", "") or "").strip()
    if not claimed_dir or not claimed_key:
        return web.json_response({"code": "stale_client", "error": _STALE_CLIENT_ERROR}, status=409)
    fresh = await _touch_spec(name, expect_spec_dir=claimed_dir, expect_slot_key=claimed_key)
    if fresh is None:
        return web.json_response({"code": "stale_client", "error": _STALE_CLIENT_ERROR}, status=409)
    return fresh
