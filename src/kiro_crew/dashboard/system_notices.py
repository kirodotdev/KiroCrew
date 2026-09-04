"""Assistant-role system notices the gateway injects into a slot's feed.

Status reports -- the auto-compaction notices and the session-reload
confirmation -- not real turns. Every scan that walks for "the last real
message" (the conversation floor, the sidebar preview, backfill replay) must
skip them, and the frontend keeps a twin of this set
(``website/src/lib/systemNotice.ts``): a kind skipped on one side but not the
other leaves the sidebar showing notice boilerplate while the chat pane shows
the real turn, or vice versa.
"""

SESSION_RELOAD_KIND = "session_reload"

SYSTEM_NOTICE_KINDS: frozenset[str] = frozenset({"compaction", SESSION_RELOAD_KIND})


def is_system_notice(role: object, meta: object) -> bool:
    """True for an assistant-role system notice row.

    ``meta`` is checked with ``isinstance`` rather than the ``or {}`` idiom:
    ``append``'s ``meta: dict | None`` is not enforced at runtime, so a truthy
    non-dict would raise ``AttributeError`` on ``.get``.
    """
    return (
        role == "assistant"
        and isinstance(meta, dict)
        and meta.get("kind") in SYSTEM_NOTICE_KINDS
    )
