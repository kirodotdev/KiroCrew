"""The MCP ``send_message`` tool's advertised shape must match what validates.

``validate_tool_args`` REJECTS an unknown field, so a property advertised in the
tool's ``inputSchema`` but missing from ``SEND_MESSAGE_SCHEMA`` does not merely go
unvalidated — the whole call fails and the capability is 0% reachable over MCP,
while the dashboard path that shares the handler keeps working. That asymmetry is
invisible to every test that exercises only one side, which is why the agreement
itself is pinned here.
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


# ── The channel-session roster (issue #6514) ──
#
# The tool refuses a ``session`` value it does not recognise, so this roster is a
# gate in FRONT of the gateway's owner-DM leg. That leg
# (``_deliver_channel_dm``) is channel-neutral by construction, so a roster
# narrower than the gateway's does not disable a feature visibly -- it refuses a
# destination the plumbing behind it would have served, which is what #6514
# reported for Webex.


def test_the_channel_session_roster_is_the_gateways_minus_owner_inference_gaps() -> None:
    """The two rosters are derived from one source and differ only by reason.

    ``channel_type`` names a CONVERSATION (this session's, or an explicit
    ``target_id``), so it needs no owner. A channel ``session`` INFERS a recipient
    through ``_owner_dm_target``, so it additionally needs the transport to tell
    configured recipients from peers learned off inbound traffic. Every member of
    the narrower set is a member of the wider one, and each exclusion carries its
    reason at the definition -- so this stays a derivation, not the hand-kept drift
    that #6514 was.
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

    Both fold identities learned from inbound traffic into
    ``configured_targets()`` -- Weixin's ``_known_users``, WeCom's ``_warm_chats``
    (which under ``allow_all_users`` become the list outright) -- so a peer who
    messaged the bot once can be the single available direct target, which is what
    ``_owner_dm_target`` reads as the owner. Neither is caught downstream: both
    ``may_send_to`` implementations return True unconditionally under their open
    policy.

    ``channel_type`` is NOT withdrawn for either: it addresses a conversation
    rather than inferring a recipient, so it never consults ``_owner_dm_target``.
    """
    from kiro_crew.constants import CHANNEL_SEND_NAMESPACES
    from kiro_crew.dashboard.handlers.messaging import _SEND_MESSAGE_CHANNEL_TYPES

    assert channel not in _CHANNEL_SESSIONS
    assert channel not in _advertised()["session"]["enum"]
    assert channel in CHANNEL_SEND_NAMESPACES
    assert channel in _SEND_MESSAGE_CHANNEL_TYPES


def test_no_owner_dm_channel_advertises_learned_identities() -> None:
    """The ratchet that keeps the exclusion list from being hand-kept.

    ``_owner_dm_target`` infers an owner from ``configured_targets()``, and its
    safety claim is that the agent can only reach somebody the USER configured. A
    transport that folds a learned/warm/known set into that method breaks the claim
    silently -- the roster still admits it and the send still succeeds, just to the
    wrong human. Deriving the check from the transports means a channel that starts
    mixing learned identities in fails HERE rather than becoming an owner-DM target
    and being found later by review.
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

    Channels differ because platforms differ, and a roster is the wrong place to
    encode that: Feishu can only reply to an inbound message, so it declares its
    DM targets unavailable with a reason. Being accepted by ``session`` and being
    deliverable are therefore separate questions, answered in separate places --
    the roster admits the value, and ``_owner_dm_target`` declines the send at the
    side-effect boundary where live config can be read.

    Webex passes the same gate because its platform genuinely permits the send
    (``toPersonEmail`` opens the 1:1 space server-side), not because the gate is
    lenient. Pinned on the real ``FeishuTransport`` because a stub with
    ``available=False`` would prove only that the filter reads the flag, not that
    a shipped channel sets it.
    """
    from kiro_crew.dashboard.handlers.messaging import _owner_dm_target
    from kiro_crew.feishu.transport import FeishuTransport

    transport = FeishuTransport(client=object(), allowed_open_ids=["ou_notarealid"])

    assert "feishu" in _CHANNEL_SESSIONS
    assert [t.available for t in transport.configured_targets()] == [False]
    assert _owner_dm_target(transport) == ""


def test_the_roster_move_did_not_change_the_roster() -> None:
    """``constants`` is the new home; ``messaging.link`` re-exports the same object.

    The move exists to break an import cycle, so it must be observationally inert
    for the roster's existing readers -- several of which import it from
    ``messaging.link`` and are unaware of the move.
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

    A channel ``session`` leaves over the same transports as ``channel_type``, so
    omitting ``caller_session`` makes the gateway fall back to the host sentinel
    and vet the wrong principal. Resolved STRICTLY: the lenient resolver walks
    process ancestors, which would hand a sub-agent its parent's channel
    permissions at the egress gate.
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
        post.return_value = {"ok": True, "delivered_to": "webex"}
        tool.send_message("send_message", {"text": "hi", "session": "webex"})

    assert post.call_args.args[1]["caller_session"] == "webex:a:dm:u1"
    assert post.call_args.kwargs["session_key"] == "webex:a:dm:u1"


def test_a_channel_session_without_a_strict_key_is_refused() -> None:
    """An unattributable channel-session send is refused, and refusing is the point.

    ``gov_session`` falls back to the LENIENT resolver, which walks process
    ancestors, so an unidentified sub-agent resolves to its parent and the
    channel-agent containment check (keyed on an identity starting ``channel:``)
    does not fire for a contained agent. That is a confinement bypass onto the
    owner-DM egress surface, and the gateway's fail-closed ``channels`` re-vet
    does not backstop it -- that gate covers the transport scope, not containment.

    It costs no legitimate caller: the gateway injects ``KIROCREW_SESSION_KEY``
    into every agent subprocess and cron runs carry ``cron:<job_id>`` in it, so
    the refusal only reaches a caller that genuinely cannot be attributed.
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
