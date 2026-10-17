"""JID (Jabber ID) helpers for the WhatsApp channel.

WhatsApp addresses everything with ``user@server`` JIDs: direct chats end in
``@s.whatsapp.net``, groups in ``@g.us``, and multi-device "linked identity"
aliases in ``@lid``. neonize hands them over as protobuf objects with
``User``/``Server``/``Device`` fields; this module owns the string form the
rest of the channel uses for config, session keys, and the echo tracker.

Pure string logic — no neonize import, so every consumer stays testable
without the optional dependency.
"""

from __future__ import annotations

USER_SERVER = "s.whatsapp.net"
GROUP_SERVER = "g.us"
LID_SERVER = "lid"


def jid_to_str(jid: object) -> str:
    """``user@server`` for a neonize JID proto (or ``""`` for empty/None).

    Drops the per-device suffix (``Device``/``RawAgent``): chat identity is
    the account, not the handset, and echo keys must not vary by device.
    """
    if jid is None:
        return ""
    user = str(getattr(jid, "User", "") or "")
    server = str(getattr(jid, "Server", "") or "")
    if not user or not server:
        return ""
    return f"{user}@{server}"


def normalize_jid(value: str) -> str:
    """Normalize a JID string: strip whitespace, lowercase the server,
    drop a ``:device`` suffix inside the user part (``447…:12@s.whatsapp.net``)."""
    value = (value or "").strip()
    if not value or "@" not in value:
        return value
    user, _, server = value.partition("@")
    user = user.split(":", 1)[0]
    return f"{user}@{server.lower()}"


def is_group_jid(value: str) -> bool:
    return normalize_jid(value).endswith(f"@{GROUP_SERVER}")


def wa_id_to_user_jid(wa_id: str) -> str:
    """``447700900000`` (config form: digits, country code, no ``+``) →
    ``447700900000@s.whatsapp.net``. Passes through values that already look
    like a JID."""
    wa_id = (wa_id or "").strip().lstrip("+")
    if not wa_id:
        return ""
    if "@" in wa_id:
        return normalize_jid(wa_id)
    return f"{wa_id}@{USER_SERVER}"


def jid_user(value: str) -> str:
    """The bare user part (phone number for ``s.whatsapp.net`` JIDs)."""
    return normalize_jid(value).partition("@")[0]


def same_account(a: str, b: str) -> bool:
    """True when two JID strings address the same account, ignoring server
    (a phone-number JID and its ``@lid`` alias have different users, so this
    is deliberately NOT an alias resolver — pass both own-JIDs explicitly)."""
    ua, ub = jid_user(a), jid_user(b)
    return bool(ua) and ua == ub


class OwnIdentity:
    """The linked account's own addresses (phone JID + lid alias).

    ``matches`` answers "is this JID me?" against both forms, which is how
    inbound Sender/SenderAlt fields are checked (multi-device delivers either
    depending on the chat's addressing mode).
    """

    def __init__(self, jid: str = "", lid: str = "") -> None:
        self.jid = normalize_jid(jid)
        self.lid = normalize_jid(lid)

    def matches(self, *candidates: str) -> bool:
        for candidate in candidates:
            norm = normalize_jid(candidate)
            if not norm:
                continue
            if same_account(norm, self.jid) or (self.lid and same_account(norm, self.lid)):
                return True
        return False

    @property
    def wa_id(self) -> str:
        return jid_user(self.jid)

    def __bool__(self) -> bool:
        return bool(self.jid or self.lid)
