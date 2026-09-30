"""The gateway's admission record for a mirror row that names its peer.

A dashboard-born session's mirror link records the peer it was admitted for
(``ChannelLink.principal``) so the per-send recipient check can reach a roster
that a Discord DM channel id alone cannot answer. That record lives in
``session_map.json``, which in-sandbox code can write, so on its own it is a
claim: a rewritten row could name an allow-listed user for a different DM, or
point one session's replies at another allow-listed user's DM, and the roster
would pass it. What makes the record trustworthy is a MAC the gateway alone can
mint -- HMAC-SHA256 under a key derived from ``token_signing.key`` with a purpose
label of its own. That key is masked from every agent plane by the sandbox
(``sandbox._CREW_HIDDEN_LEAVES``) and is already what the tag-grant store's key
certificate rests on (``dashboard.chat_tag_grants._key_cert``), so this is the
repository's established way to make an agent-writable record unforgeable: no new
seal, no widening of what the sandbox masks.

The MAC covers the whole admission -- the SESSION the row belongs to, the channel,
the conversation id, the thread id and the peer -- so moving a signed row to another
session, another conversation or another peer invalidates it. The residual is stated
rather than hidden: a row the gateway once signed for this session, location and
principal can be replayed after an unlink -- in-sandbox code that copied the signed
row while the binding existed writes it back, and after a restart nothing contradicts
it until the transport learns the DM's pairing. Refusing that replay needs revocation
state the agent cannot write back, which this file cannot hold; that is the
agent-writable-row class tracked in its own issue, and sealing a store is the
operator's sandbox decision, not this module's. It is MINTED in exactly
two places, the two paths that authorize a peer for a conversation: the dashboard's
mirror-link handler (``dashboard.chat_mirror.api_chat_slot_mirror_link``) and the
session-resume controller's pick commit
(``messaging.session_resume.SessionResumeController.choose``). Every other writer of
a mirror row -- the map itself, a rollback, a restore -- carries the stored bytes
through verbatim and never mints, because a writer that signs whatever it is handed
would launder a forged row into a valid one the moment any path re-set it
(:func:`restorable_link` is what a restore calls instead). It is verified in one
place, the cross-surface ladder's recipient leg
(``dashboard.chat_runner._recipient_principal``). A row whose MAC is absent or does
not verify is refused, audited and logged once; a token-key rotation therefore
refuses every such mirror until it is re-linked, which mints a fresh record, and the
refusal says so. The check REFUSES, it never raises: every malformed shape a planted
row can take -- a non-string or non-hex admission, a non-string peer, fields of any
type -- verifies ``False``, because this decision sits on the send path of a
dashboard turn and an exception there would abort the turn on exactly the row the
check exists to reject.

Only the row class this record exists for is signed: a link that names no peer
(an origin bind, a thread or room target, an in-channel ``/link``) carries no
principal and no MAC, and its recipient decision is unchanged.
"""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
import logging
import re
from typing import Any

from kiro_crew.dashboard import token_secret
from kiro_crew.messaging.link import canonical_key
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

#: Domain separation for the purpose key: the token signing secret signs dashboard
#: auth tokens and certifies the tag-grant store's key, and every purpose gets its
#: own derived key so a value minted for one can never verify as another. The
#: trailing NUL closes the label, the same shape ``chat_tag_grants`` uses.
_ADMISSION_KEY_DOMAIN = b"kiro-crew:mirror-admission:key:v1\x00"

#: Domain prefix of the signed material, so the MAC is over an admission record
#: and cannot collide with any other HMAC computed under the purpose key.
_ADMISSION_MATERIAL_DOMAIN = "kiro-crew-mirror-admission-v1"

#: The only shape a stored admission may take: the hex digest the signer emits.
#: Anything else is refused before it reaches the constant-time comparison, which
#: raises on a non-ASCII string -- and a raise here would abort a dashboard turn.
_HEX_DIGEST = re.compile(r"[0-9a-f]{64}")


def _purpose_key() -> bytes:
    """The admission key: HMAC of the purpose label under the token signing secret."""
    return hmac.new(token_secret._get_secret(), _ADMISSION_KEY_DOMAIN, hashlib.sha256).digest()


def _material(session_key: str, link: Any) -> bytes:
    """The canonical bytes an admission signs: the session and the whole location.

    NUL-joined with a leading domain, like ``messaging.link.binding_token``, so two
    records that differ in any field -- session, channel, conversation, thread or
    peer -- sign different material. The session key is canonicalized the way the
    map stores it, so the writer (which stores the canonical key) and the reader
    (which is handed the session's key by the ladder) sign the same bytes. Every
    field is coerced to text first, so a planted row of any shape yields bytes to
    compare rather than an exception.
    """
    parts = [
        _ADMISSION_MATERIAL_DOMAIN,
        canonical_key(str(session_key or "")),
        str(getattr(link, "channel_type", "") or "").lower(),
        str(getattr(link, "channel_id", "") or ""),
        str(getattr(link, "thread_id", "") or ""),
        str(getattr(link, "principal", "") or ""),
    ]
    return "\0".join(parts).encode("utf-8", "surrogatepass")


def sign_mirror_admission(session_key: str, link: Any) -> str:
    """The admission MAC for *link* as *session_key*'s mirror, hex-encoded.

    For the two authorized creation paths only (see the module docstring); a test
    pins that no other call site exists. A link that names no peer has nothing to
    admit and gets ``""``, so an unsigned principal-less row is the ordinary shape
    and never reads as a broken one.
    """
    if not getattr(link, "principal", None):
        return ""
    return hmac.new(_purpose_key(), _material(session_key, link), hashlib.sha256).hexdigest()


def verify_mirror_admission(session_key: str, link: Any) -> bool:
    """Whether *link*'s stored admission was minted by this gateway for this row.

    ``False`` for a link with no admission, one whose admission does not verify
    (a rewritten row, a row moved to another session, or a rotated key), and any
    failure to read the key -- an admission that cannot be checked admits nobody,
    because this feeds a network egress boundary. Never raises: the row comes from
    a file in-sandbox code can write, so every shape it can take -- an admission
    that is not a string or not a hex digest, a peer that is not a string, fields of
    any type -- is a refusal, not an exception on the send path. Constant-time
    comparison.
    """
    try:
        recorded = getattr(link, "admission", None)
        if not isinstance(recorded, str) or not _HEX_DIGEST.fullmatch(recorded):
            return False
        principal = getattr(link, "principal", None)
        if not isinstance(principal, str) or not principal:
            return False
        expected = sign_mirror_admission(session_key, link)
        return bool(expected) and hmac.compare_digest(expected, recorded)
    except Exception:
        return False


def restorable_link(session_key: str, link: Any) -> Any:
    """*link* as a rollback may put it back on *session_key*'s row.

    A rollback re-sets a row it read from the store -- a store in-sandbox code can
    write -- so it is a WRITER of rows it did not authorize, and it must not become
    the path that turns a forged row into a trusted one. A link that names no peer,
    or whose stored admission verifies for this session and location, goes back
    verbatim. A link that names a peer under an admission that is absent or does
    not verify goes back WITHOUT its peer and admission: the location is restored
    (the binding the user had), the recipient check refuses it at send, fail-closed,
    and one audit row plus one log line say so, naming re-linking as the remedy.
    Nothing is minted here, ever.

    What the strip IS: the security control is that nothing but the two creation
    paths mints an admission, so an unverifiable peer put back verbatim would be
    refused at send anyway. The strip does not add refusal; it makes the refusal
    LEGIBLE and durable -- the row stops carrying a peer it cannot vouch for, the
    audit names the rollback that saw it, and a later reader is not left to work
    out why a row with a peer delivers nothing. A rollback site that forgets to call
    this therefore loses an audit line, not the guarantee -- and it fails the AST pin
    that classifies every ``set_mirror_link`` caller as a signer, a rollback that
    restores through this function, or a peer-less writer, so the convention is
    enforced by a test rather than remembered at review.
    """
    if not getattr(link, "principal", None):
        return link
    if verify_mirror_admission(session_key, link):
        return link
    stripped = dataclasses.replace(link, principal=None, admission=None)
    channel_type = str(getattr(link, "channel_type", "") or "channel")
    # Nothing off the row reaches the log line: at a rollback the channel type is
    # whatever the file says, so even that is kept for the redacted audit field.
    logger.warning(
        "mirror rollback: a mirror row names a peer without a valid gateway admission; "
        "restoring the binding without it, so deliveries into it are refused until the "
        "session is re-linked from the dashboard"
    )
    try:
        # Constants for the two fields the log stores verbatim; the row's own ids,
        # which come from a file in-sandbox code can write, ride only in the
        # redacted ``resources`` field.
        sel().log_api_access(
            caller="mirror-rollback",
            operation="channel.mirror_admission",
            outcome="stripped_on_restore",
            source="session_map",
            resources=(
                f"{session_key} -> {channel_type}:"
                f"{getattr(link, 'channel_id', '') or 'unknown'}"
            ),
        )
    except Exception:
        logger.debug("SEL logging failed for a stripped mirror admission", exc_info=True)
    return stripped
