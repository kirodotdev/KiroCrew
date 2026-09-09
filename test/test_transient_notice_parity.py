"""Guard: the transient-5xx notice tokens must match the frontend's table.

``TRANSIENT_NOTICE_RETRYING`` / ``TRANSIENT_NOTICE_RESUMING`` /
``TRANSIENT_NOTICE_GIVE_UP`` in ``kiro_crew.dashboard.chat_utils`` are the
``meta["notice"]`` wire tokens chat_runner stamps on the ``error`` rows it
appends while retrying an upstream model 5xx. ``NOTICE_TOKENS`` in
``website/src/pages/chat/transientNotice.ts`` is the frontend's copy of that
list, keyed to pick which localized card a row renders as. Nothing links the two
but the token string -- the same shape as the ``*_RECOVERY_PREFIX`` markers
guarded by ``test_recovery_marker_parity.py`` -- so an edit on one side silently
drops the row back to raw English on the other. Discovered by regex rather than
listed, so a fourth token is caught automatically.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_BACKEND = _REPO / "src" / "kiro_crew" / "dashboard" / "chat_utils.py"
_FRONTEND = _REPO / "website" / "src" / "pages" / "chat" / "transientNotice.ts"

# `TRANSIENT_NOTICE_SOMETHING = "..."` at column 0, excluding the META_KEY entry
# (which names the meta field the tokens ride in, not a token).
_BACKEND_RE = re.compile(
    r'^(?P<name>TRANSIENT_NOTICE_(?!META_KEY)[A-Z_]+)\s*=\s*"(?P<token>[^"]+)"', re.MULTILINE
)
_BACKEND_META_KEY_RE = re.compile(
    r'^TRANSIENT_NOTICE_META_KEY\s*=\s*"(?P<key>[^"]+)"', re.MULTILINE
)
# `const NOTICE_TOKENS: ... = { transient_x: 'shape', ... }`
_FRONTEND_BLOCK_RE = re.compile(
    r"const NOTICE_TOKENS:[^=]*=\s*\{(?P<body>.*?)^\}", re.DOTALL | re.MULTILINE
)
_FRONTEND_ENTRY_RE = re.compile(r"^\s*(?P<token>[a-z_]+):\s*'(?P<shape>[a-z_]+)'", re.MULTILINE)


def _backend_tokens() -> dict[str, str]:
    found = _BACKEND_RE.findall(_BACKEND.read_text(encoding="utf-8"))
    assert found, "no TRANSIENT_NOTICE_* token constants found in chat_utils.py"
    return dict(found)


def _frontend_tokens() -> dict[str, str]:
    match = _FRONTEND_BLOCK_RE.search(_FRONTEND.read_text(encoding="utf-8"))
    assert match, "could not find the NOTICE_TOKENS table in transientNotice.ts"
    entries = _FRONTEND_ENTRY_RE.findall(match.group("body"))
    assert entries, "NOTICE_TOKENS parsed as empty"
    return dict(entries)


def test_backend_transient_tokens_match_the_frontend_table() -> None:
    backend = _backend_tokens()
    frontend = _frontend_tokens()
    missing_in_frontend = set(backend.values()) - set(frontend)
    missing_in_backend = set(frontend) - set(backend.values())
    assert not missing_in_frontend, (
        "backend token(s) the frontend cannot resolve, so their rows render as raw "
        f"English on every locale: {sorted(missing_in_frontend)}. Add them to "
        "NOTICE_TOKENS in transientNotice.ts."
    )
    assert not missing_in_backend, (
        "frontend NOTICE_TOKENS entr(ies) with no backend constant, so nothing emits "
        f"them: {sorted(missing_in_backend)}. Remove them, or add the constant to chat_utils.py."
    )
    assert len(backend) == len(frontend)


def test_frontend_reads_the_meta_field_the_backend_writes() -> None:
    key = _BACKEND_META_KEY_RE.search(_BACKEND.read_text(encoding="utf-8"))
    assert key, "TRANSIENT_NOTICE_META_KEY not found in chat_utils.py"
    # The frontend reads `meta.notice` by property name; a renamed field on
    # either side would make every token invisible.
    assert key.group("key") == "notice"
    assert "?.notice" in _FRONTEND.read_text(encoding="utf-8")


def test_backend_transient_texts_avoid_internal_vocabulary() -> None:
    # The rename's whole point: a person reading the English fallback is not
    # told about "backends" or "hiccups". Pins the wording class, not the copy.
    texts = re.findall(
        r'^TRANSIENT_[A-Z_]+_TEXT\s*=\s*"([^"]+)"', _BACKEND.read_text(encoding="utf-8"), re.M
    )
    assert len(texts) == 3
    for text in texts:
        lowered = text.lower()
        assert "hiccup" not in lowered and "backend" not in lowered, text
