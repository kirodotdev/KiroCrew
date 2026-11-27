"""What chat search matches: a message's or title's redacted form, the text the
results show.

``redact`` is the costly step of a search that scans transcripts: on real chats it
takes about eight times as long as reading and folding the text, and it almost
never changes anything (one message in 84,000 on a 300-chat sample). The fold
memo cannot hold every chat, so the same text is redacted again on later
searches and after every write to its chat. :func:`shown_text` therefore keeps a
bounded memo keyed by a digest of the text: a text redaction left as it is costs
one digest the next time, and the few that redaction changed keep their
redacted form.

The memo describes one redaction, so it is keyed on what decides the redaction,
the way the display caches are (``dashboard/display_verdicts``): the loaded rule
table (``display_battery.LOADED_DIGEST``) and the owner's on/off setting as
published in this process (``redaction_seams.published_off`` and its publish
count, which moves on every flip, a round trip included), and the token-parameter
URL prefixes the installed platform context exempts
(``redaction._token_param_exempt_prefixes``, read as ``redact`` reads it). When any of them
changes the memo is emptied, and an answer computed across a change is not
kept. A call where the credential pass stands down for its caller
(``redaction_switch.credential_pass_bypassed``) neither reads nor fills it.
"""

from __future__ import annotations

import hashlib
import os
import threading
from collections import OrderedDict
from collections.abc import Callable

from kiro_crew.security import redact

#: Digests of texts ``redact`` left unchanged, oldest dropped first. An 8-byte
#: digest is an int key, about 60 bytes an entry, so the cap holds ~30 MB.
CLEAN_MEMO_MAX = 500_000
#: Texts ``redact`` changed, by digest, with their redacted form.
CHANGED_MEMO_MAX = 4_096
#: Total characters in kept redacted forms; oldest dropped first.
CHANGED_MEMO_CHARS = 8 * 1024 * 1024
#: Longer redacted forms are returned in full without being kept.
CHANGED_TEXT_MAX_CHARS = 256 * 1024

_lock = threading.Lock()
_clean: OrderedDict[int, None] = OrderedDict()
_changed: OrderedDict[int, str] = OrderedDict()
_changed_chars = 0
_memo_epoch: tuple[object, ...] | None = None


#: A per-process key, so no text can be crafted to share another text's digest.
_DIGEST_KEY = os.urandom(16)


def _digest(text: str) -> int:
    raw = hashlib.blake2b(
        text.encode("utf-8", "surrogatepass"), digest_size=8, key=_DIGEST_KEY
    ).digest()
    return int.from_bytes(raw, "big")


def redaction_epoch() -> tuple[object, ...]:
    """What the memo's answers depend on besides the text itself."""
    from kiro_crew import redaction_seams
    from kiro_crew.dashboard import display_battery
    from kiro_crew.security.redaction import _token_param_exempt_prefixes

    return (
        display_battery.LOADED_DIGEST,
        redaction_seams.published_off(),
        redaction_seams.generation(),
        _token_param_exempt_prefixes(),
    )


def _bypassed() -> bool:
    from kiro_crew.security.redaction_switch import credential_pass_bypassed

    return credential_pass_bypassed()


def shown_text(text: str) -> str:
    """*text* after ``redact``, from the memo when it was redacted before under
    the same rules and setting."""
    if _bypassed():
        return redact(text)
    global _memo_epoch, _changed_chars
    key = _digest(text)
    epoch = redaction_epoch()
    with _lock:
        if epoch != _memo_epoch:
            _clean.clear()
            _changed.clear()
            _changed_chars = 0
            _memo_epoch = epoch
        if key in _clean:
            return text
        found = _changed.get(key)
        if found is not None:
            return found
    shown = redact(text)
    with _lock:
        # A flip while this text was redacted: the answer is returned, not kept.
        if _memo_epoch != epoch or redaction_epoch() != epoch:
            return shown
        if shown == text:
            _clean[key] = None
            if len(_clean) > CLEAN_MEMO_MAX:
                _clean.popitem(last=False)
        elif len(shown) <= CHANGED_TEXT_MAX_CHARS and key not in _changed:
            _changed[key] = shown
            _changed_chars += len(shown)
            while len(_changed) > CHANGED_MEMO_MAX or _changed_chars > CHANGED_MEMO_CHARS:
                _, dropped = _changed.popitem(last=False)
                _changed_chars -= len(dropped)
    return shown


def shown_title(title: object) -> str:
    """A chat title as search matches it: redacted, the way the results show it."""
    return shown_text(title) if isinstance(title, str) and title else ""


def shown_title_folded(title: object) -> str:
    """:func:`shown_title`, case-folded."""
    return shown_title(title).casefold()


def on_shown_title(match: Callable[[str], bool]) -> Callable[[str], bool]:
    """*match* applied to a title's redacted form."""
    return lambda title: match(shown_title(title))


__all__ = [
    "on_shown_title",
    "redaction_epoch",
    "shown_text",
    "shown_title",
    "shown_title_folded",
]
