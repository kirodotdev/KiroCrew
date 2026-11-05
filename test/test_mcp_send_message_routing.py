"""The MCP ``send_message`` tool's advertised shape must match what validates.

``validate_tool_args`` rejects an unknown field, so a property advertised in the
``inputSchema`` but missing from ``SEND_MESSAGE_SCHEMA`` fails every MCP call while the
dashboard path keeps working. A test of one side alone cannot see that.
"""

from __future__ import annotations

import importlib
import re

import pytest

from kiro_crew.constants import CHANNEL_SESSION_NAMESPACES
from kiro_crew.mcp_tools.messaging import _CHANNEL_SESSIONS, schemas
from kiro_crew.validation import SEND_MESSAGE_SCHEMA, ValidationError, validate_tool_args


def _advertised() -> dict:
    for tool in schemas():
        if tool["name"] == "send_message":
            return tool["inputSchema"]["properties"]
    raise AssertionError("send_message is not advertised")


def test_every_advertised_property_is_accepted_by_the_validator() -> None:
    known = {spec.name for spec in SEND_MESSAGE_SCHEMA.fields}
    missing = sorted(set(_advertised()) - known)

    assert not missing, (
        f"advertised but unvalidatable, so every call carrying one fails: {missing}. "
        "Add a FieldSpec to SEND_MESSAGE_SCHEMA in the same change."
    )


def test_the_routing_pair_validates() -> None:
    cleaned = validate_tool_args(
        {"text": "hi", "channel_type": "webex", "target_id": "user:a@b.com"},
        SEND_MESSAGE_SCHEMA,
    )

    assert cleaned["channel_type"] == "webex"
    assert cleaned["target_id"] == "user:a@b.com"


def test_a_target_id_without_a_channel_type_is_refused() -> None:
    """A destination id with no transport to resolve it against is under-specified.

    Ignoring the lone field would fall back to the default Slack/dashboard
    destination — delivering the message somewhere the caller did not name.
    """
    with pytest.raises(ValidationError):
        validate_tool_args({"text": "hi", "target_id": "user:a@b.com"}, SEND_MESSAGE_SCHEMA)


def test_a_channel_type_alone_is_complete() -> None:
    """``channel_type`` on its own is NOT half a pair.

    It names the non-Slack conversation this session already belongs to; adding
    ``target_id`` narrows that transport to one explicit configured destination on
    it. So only the reverse is under-specified.
    """
    cleaned = validate_tool_args({"text": "hi", "channel_type": "webex"}, SEND_MESSAGE_SCHEMA)

    assert cleaned["channel_type"] == "webex"
    assert "target_id" not in cleaned or not cleaned["target_id"]


@pytest.mark.parametrize(
    "channel_type",
    ["WEBEX", "we bex", "1webex", "webex!", "x" * 40, "../etc", ""],
)
def test_a_channel_type_that_is_not_a_channel_name_shape_is_refused(channel_type: str) -> None:
    with pytest.raises(ValidationError):
        validate_tool_args(
            {"text": "hi", "channel_type": channel_type, "target_id": "x"},
            SEND_MESSAGE_SCHEMA,
        )


@pytest.mark.parametrize("target_id", ["with space", "line\nbreak", "tab\there", "x" * 513])
def test_a_target_id_with_control_characters_or_over_length_is_refused(target_id: str) -> None:
    # The id is opaque and channel-defined, so this bounds length and excludes
    # whitespace/control characters rather than pretending to know the grammar.
    with pytest.raises(ValidationError):
        validate_tool_args(
            {"text": "hi", "channel_type": "webex", "target_id": target_id},
            SEND_MESSAGE_SCHEMA,
        )


def test_an_opaque_base64_room_id_is_accepted() -> None:
    # A real Webex room id is a ~90-char base64 Hydra blob.
    room = "Y2lzY29zcGFyazovL3VzL1JPT00vZXhhbXBsZS1yb29tLWlkZW50aWZpZXItdGhhdC1pcy1sb25n"
    cleaned = validate_tool_args(
        {"text": "hi", "channel_type": "webex", "target_id": f"room:{room}"},
        SEND_MESSAGE_SCHEMA,
    )

    assert cleaned["target_id"].endswith(room)


def test_a_plain_send_still_validates_without_the_pair() -> None:
    # The fields are additive: a caller that never routes is unaffected.
    assert validate_tool_args({"text": "hi"}, SEND_MESSAGE_SCHEMA)["text"] == "hi"


# ── The channel-session roster ──
#
# The tool refuses a ``session`` value it does not recognise, so this roster is a
# gate in FRONT of the gateway's owner-DM leg. That leg
# (``_deliver_channel_dm``) is channel-neutral by construction, so a roster
# narrower than the gateway's does not disable a feature visibly -- it refuses a
# destination the plumbing behind it would have served, as it does for Webex.


def test_the_channel_session_roster_is_the_gateways_minus_owner_inference_gaps() -> None:
    """The two rosters are derived from one source and differ only by reason.

    ``channel_type`` names a conversation and needs no owner. A channel ``session``
    infers a recipient through ``_owner_dm_target``, so its transport must also tell
    configured recipients from peers learned off inbound traffic.
    """
    from kiro_crew.constants import CHANNEL_OWNER_DM_NAMESPACES, CHANNEL_SEND_NAMESPACES
    from kiro_crew.dashboard.handlers.messaging import _SEND_MESSAGE_CHANNEL_TYPES

    assert _CHANNEL_SESSIONS is CHANNEL_OWNER_DM_NAMESPACES
    assert set(_SEND_MESSAGE_CHANNEL_TYPES) == set(CHANNEL_SEND_NAMESPACES)
    assert set(CHANNEL_OWNER_DM_NAMESPACES) <= set(CHANNEL_SEND_NAMESPACES)
    assert set(CHANNEL_SEND_NAMESPACES) == set(CHANNEL_SESSION_NAMESPACES) - {
        "slack",
        "unified",
    }


@pytest.mark.parametrize("channel", ["weixin", "wecom"])
def test_a_learned_peer_channel_takes_channel_type_but_never_an_inferred_owner_dm(
    channel: str,
) -> None:
    """The exclusions that keep an inferred owner DM off a LEARNED peer.

    Both fold learned identities into ``configured_targets()`` (Weixin's
    ``_known_users``, WeCom's ``_warm_chats``), so a peer who messaged the bot once can
    read as the owner, and both ``may_send_to`` allow it under the open policy.
    ``channel_type`` stays: it never consults ``_owner_dm_target``.
    """
    from kiro_crew.constants import CHANNEL_SEND_NAMESPACES
    from kiro_crew.dashboard.handlers.messaging import _SEND_MESSAGE_CHANNEL_TYPES

    assert channel not in _CHANNEL_SESSIONS
    assert channel not in _advertised()["session"]["enum"]
    assert channel in CHANNEL_SEND_NAMESPACES
    assert channel in _SEND_MESSAGE_CHANNEL_TYPES


def test_no_owner_dm_channel_advertises_learned_identities() -> None:
    """The ratchet that keeps the exclusion list from being hand-kept.

    ``_owner_dm_target`` must reach only someone the user configured. A transport that
    folds learned identities into ``configured_targets()`` would send to the wrong
    person silently, so it fails here instead.
    """
    import inspect

    from kiro_crew.constants import CHANNEL_OWNER_DM_NAMESPACES

    learned = re.compile(r"_(known|learned|warm|seen|peers|authorized)[a-z_]*")
    offenders = {}
    for channel in CHANNEL_OWNER_DM_NAMESPACES:
        try:
            module = importlib.import_module(f"kiro_crew.{channel}.transport")
        except ModuleNotFoundError:
            continue  # a rostered namespace with no transport package in this fork
        transport_cls = next(
            (
                obj
                for _, obj in inspect.getmembers(module, inspect.isclass)
                if obj.__module__ == module.__name__ and hasattr(obj, "configured_targets")
            ),
            None,
        )
        if transport_cls is None:
            continue
        src = inspect.getsource(transport_cls.configured_targets)
        hits = sorted(set(learned.findall(src)))
        if hits:
            offenders[channel] = hits

    assert not offenders, (
        f"these owner-DM channels advertise learned identities: {offenders}. "
        "Either exclude them from CHANNEL_OWNER_DM_NAMESPACES with a reason, or "
        "separate configured recipients from learned peers in configured_targets()."
    )


@pytest.mark.parametrize("session", ["unified", "slack", "origin"])
def test_a_reserved_value_is_never_a_channel_session(session: str) -> None:
    """``unified`` is a session-key bucket and ``slack``/``origin`` are modes.

    Widening the roster must not sweep these in: ``slack`` has its own client and
    is absent from ``channel_transports``, so routing it down the channel leg
    would fail every such send closed for no stateable reason.
    """
    assert session not in _CHANNEL_SESSIONS


def test_webex_advertises_a_resolvable_owner_dm_target() -> None:
    """The capability claim behind the roster widening, on the real transport.

    Real, not a double: the claim is that the gateway's channel-neutral resolver
    can serve Webex from Webex's OWN configured-target allowlist, so a stub that
    invents the target id would prove nothing about Webex's spelling of it.
    """
    from kiro_crew.dashboard.handlers.messaging import _owner_dm_target
    from kiro_crew.webex.transport import WebexTransport

    transport = WebexTransport(client=object(), allowed_emails=["owner@example.invalid"])

    assert _owner_dm_target(transport) == "user:owner@example.invalid"


def test_an_ambiguous_webex_allowlist_is_refused_rather_than_guessed() -> None:
    """Why widening the roster does not widen the audience.

    With no owner field on the channel, two allow-listed people mean no inferable
    recipient, and picking one would send private agent output to the wrong human.
    The empty answer degrades to the dashboard notification instead.
    """
    from kiro_crew.dashboard.handlers.messaging import _owner_dm_target
    from kiro_crew.webex.transport import WebexTransport

    transport = WebexTransport(
        client=object(), allowed_emails=["a@example.invalid", "b@example.invalid"]
    )

    assert _owner_dm_target(transport) == ""


def test_a_channel_that_cannot_dm_proactively_is_in_the_roster_but_yields_no_target() -> None:
    """The asymmetry the derivation must NOT flatten, on a real transport.

    Feishu can only reply to an inbound message, so the roster admits it and
    ``_owner_dm_target`` declines the send. Uses the real ``FeishuTransport``: a stub
    with ``available=False`` would not prove a shipped channel sets the flag.
    """
    from kiro_crew.dashboard.handlers.messaging import _owner_dm_target
    from kiro_crew.feishu.transport import FeishuTransport

    transport = FeishuTransport(client=object(), allowed_open_ids=["ou_notarealid"])

    assert "feishu" in _CHANNEL_SESSIONS
    assert [t.available for t in transport.configured_targets()] == [False]
    assert _owner_dm_target(transport) == ""


def test_the_roster_move_did_not_change_the_roster() -> None:
    """``messaging.link`` re-exports the same roster object that ``constants`` defines.

    Several readers import it from ``messaging.link``.
    """
    from kiro_crew.constants import CHANNEL_SESSION_NAMESPACES as canonical
    from kiro_crew.messaging.link import CHANNEL_SESSION_NAMESPACES as re_exported

    assert canonical is re_exported
    assert canonical == (
        "slack",
        "discord",
        "telegram",
        "whatsapp",
        "webex",
        "wecom",
        "teams",
        "weixin",
        "imessage",
        "feishu",
        "unified",
    )


def test_a_channel_session_forwards_a_strict_caller_session_when_one_exists() -> None:
    """The gateway must re-vet a channel session under the CALLER's identity.

    Without ``caller_session`` the gateway vets the host sentinel instead. Resolved
    strictly, since the lenient resolver would give a sub-agent its parent's rights.
    """
    from unittest.mock import patch

    from kiro_crew.mcp_tools import messaging as tool

    with (
        patch.object(tool.mcp_core, "_resolve_session_key", return_value="dashboard:7"),
        patch.object(
            tool.mcp_core, "require_strict_session_key", return_value=("webex:a:dm:u1", "")
        ),
        patch.object(tool.mcp_core, "_post") as post,
    ):

        def _gateway(_path: str, body: dict, *, session_key: str = "", **_k: object) -> dict:
            # The gateway re-vets as the caller it is handed; any other principal is refused.
            if body.get("caller_session") != "webex:a:dm:u1":
                return {"error": "channel session not permitted for this caller"}
            return {"ok": True, "delivered_to": "webex"}

        post.side_effect = _gateway
        result = tool.send_message("send_message", {"text": "hi", "session": "webex"})

    assert not result.startswith("Error:"), result
    assert "webex" in result

    assert post.call_args.args[1]["caller_session"] == "webex:a:dm:u1"
    assert post.call_args.kwargs["session_key"] == "webex:a:dm:u1"


def test_a_channel_session_without_a_strict_key_is_refused() -> None:
    """An unattributable channel-session send is refused, and refusing is the point.

    The lenient resolver walks process ancestors, so an unidentified sub-agent would
    resolve to its parent and skip the ``channel:`` containment check; the gateway's
    ``channels`` re-vet covers transport scope, not containment. Every agent
    subprocess and cron run has ``KIROCREW_SESSION_KEY``, so no real caller is refused.
    """
    from unittest.mock import patch

    from kiro_crew.mcp_tools import messaging as tool

    with (
        patch.object(tool.mcp_core, "_resolve_session_key", return_value=""),
        patch.object(
            tool.mcp_core, "require_strict_session_key", return_value=("", "Error: refused")
        ),
        patch.object(tool.mcp_core, "_post") as post,
    ):
        result = tool.send_message("send_message", {"text": "hi", "session": "webex"})

    assert result.startswith("Error:")
    post.assert_not_called()
