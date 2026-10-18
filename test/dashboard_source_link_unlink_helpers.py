"""Links, slots and request builders shared by the source-link unlink tests.

``test_dashboard_source_link_unlink.py`` and its ``_hydration``, ``_audit`` and
``_publication`` siblings import these by name: the PR and issue URLs, the identity key a
chip is dismissed under, a slot whose transcript carries the links, and the mocked DELETE
request with its identity pin and ``expect`` triple.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from aiohttp import web

from kiro_crew.dashboard.handlers.source_providers import parse_source_url
from kiro_crew.dashboard.source_providers.contract import source_ref_identity_key
from kiro_crew.dashboard.state import _ChatSlot

PR_A = "https://github.com/acme/widgets/pull/11"
PR_B = "https://github.com/acme/widgets/pull/12"
PR_C = "https://github.com/acme/widgets/pull/13"
ISSUE_A = "https://github.com/acme/widgets/issues/21"


def _identity_key(url: str) -> str:
    return source_ref_identity_key(parse_source_url(url).identity)


def _slot(*urls: str) -> _ChatSlot:
    slot = _ChatSlot("s1")
    slot.append("assistant", "\n".join(urls or (PR_A, PR_B, ISSUE_A)), ts="t1")
    return slot


def _pin_ok(state, created_at: str = "t"):
    """Configure a mock state's conversation_log so the unlink handler's
    mandatory identity-pin read (get_metadata_status) returns a readable
    transcript — production transcripts are readable, so this mirrors it. The
    write guard requires meta.created_at == this value, so side_effects that
    invoke the guard should pass a meta with the same created_at."""
    state.conversation_log.get_metadata_status.return_value = (
        {"_type": "metadata", "created_at": created_at},
        True,
    )
    return state


def _expect_for_req(req) -> str:
    """The ``expect`` triple the handler will accept for *req*'s target slot,
    mirroring the handler's ``current_identity`` (``resolved_row_identity`` +
    ``created_at`` + ``linked_session_key``). Tests that are not exercising the
    identity gate build their manual request's query with this so they reach
    their intended path rather than the required-``expect`` 400."""
    from kiro_crew.dashboard.chat_handlers import resolved_row_identity

    slot = req.app["state"]._slots.get(req.match_info["slot"])
    if slot is None:
        return ""
    return (
        f"{resolved_row_identity(slot)}"
        f"|{getattr(slot, 'created_at', '') or ''}"
        f"|{getattr(slot, 'linked_session_key', '') or ''}"
    )


def _request(
    slot_key: str,
    identity: str,
    slots: dict,
    *,
    app: str = "",
    expect: str | None = None,
    omit_expect: bool = False,
):
    request = MagicMock(spec=web.Request)
    request.method = "DELETE"
    request.match_info = {"slot": slot_key, "identity": identity}
    request.get = lambda key, default=None: app if key == "app" else default
    # ``expect`` is REQUIRED by the handler. Tests that are not exercising the
    # identity gate itself default it to the matching triple of the target slot
    # (when that slot exists), so they reach their intended path (publication,
    # persistence, refusals past the gate) rather than tripping the 400. A test
    # that means to send NO ``expect`` sets ``omit_expect=True``; one that means
    # to send a WRONG value passes ``expect=`` explicitly.
    if not omit_expect and expect is None:
        target = slots.get(slot_key)
        if target is not None:
            expect = (
                f"{getattr(target, 'key', slot_key)}"
                f"|{getattr(target, 'created_at', '') or ''}"
                f"|{getattr(target, 'linked_session_key', '') or ''}"
            )
    # A real query mapping so the ``expect`` gate reads a concrete value (or is
    # absent) rather than an auto-generated MagicMock, which would be truthy.
    request.rel_url = MagicMock()
    request.rel_url.query = {"expect": expect} if expect is not None else {}
    request.app = {"state": _pin_ok(MagicMock(_slots=slots))}
    return request
