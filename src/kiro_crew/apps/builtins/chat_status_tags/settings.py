"""The reconcile prompt store — one editable raw-text file.

Unlike Mochi's JSON settings, there is exactly ONE setting here and it is free
text (the reconcile prompt an agent reads and follows), so it is stored as a
plain ``.md`` file rather than a JSON document — the agent-facing cron reads the
same bytes with a bare ``cat``, no parsing.

On-disk path: ``app_data_dir("chat-status-tags") / reconcile-prompt.md`` — i.e.
``$KIROCREW_HOME/apps/chat-status-tags/data/reconcile-prompt.md`` (``KIROCREW_HOME``
defaults to ``~/.kiro/crew``). ``app_data_dir`` is the same resolver every other
builtin uses (ops-mission-control's ``store`` included), so the file follows a
relocated data home for free.

Write discipline mirrors ``mochi/settings.py``: ``atomic_write`` for the write
itself and a sibling ``.lock`` (``file_lock``) around the read-modify-write, so a
concurrent PUT from the app page and a startup seed cannot interleave into a
torn file. Reads are lock-free — the atomic replace guarantees a reader sees a
whole document.

``set_prompt("")`` (or whitespace) DELETES the file, which is how "reset to
default" is expressed: ``get_prompt`` then falls back to
``DEFAULT_RECONCILE_PROMPT`` and ``is_default`` reports True.
"""

from __future__ import annotations

import json
import logging
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator

from kiro_crew.apps.builtins.chat_status_tags.prompts import DEFAULT_RECONCILE_PROMPT
from kiro_crew.apps.manager import app_data_dir
from kiro_crew.atomic_write import atomic_write
from kiro_crew.platform_compat import file_lock

logger = logging.getLogger(__name__)

APP_NAME = "chat-status-tags"
PROMPT_FILENAME = "reconcile-prompt.md"
FLAGS_FILENAME = "toggles.json"

#: Cap the stored prompt so a hand-crafted PUT cannot bloat the data dir. The
#: route enforces the same bound; this is the floor for any other writer.
MAX_PROMPT_LEN = 20000

#: The behaviour toggles this app persists, with their shipped defaults. Both
#: paid automatic behaviours (the hourly LLM reconciler cron and the
#: credit-spending auto-resume loop) ship ENABLED; a credit-conscious operator
#: turns them off here. The zero-token health sweep has no toggle — it is always
#: on. A missing/corrupt file, or any absent key, degrades to these defaults.
_FLAG_DEFAULTS: Dict[str, bool] = {
    "reconciler_enabled": True,
    "auto_resume_enabled": True,
}


def prompt_path() -> Path:
    """Absolute path to the reconcile-prompt file (its parent is created)."""
    return app_data_dir(APP_NAME) / PROMPT_FILENAME


@contextmanager
def _prompt_mutation() -> Iterator[None]:
    """Cross-process lock for a prompt read-modify-write.

    Same defect/remedy as ``mochi.settings.settings_mutation``: ``atomic_write``
    makes each WRITE atomic, but seed-then-edit is a load-modify-write with more
    than one writer (the startup seed and an app-page PUT can race). Serialise on
    a sibling ``.lock`` — flock follows the inode, so locking the data file would
    guard an inode the rename is about to replace.
    """
    target = prompt_path()
    lock_path = target.with_name(target.name + ".lock")
    target.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        with file_lock(fd, exclusive=True):
            yield
    finally:
        os.close(fd)


def get_prompt() -> str:
    """The stored reconcile prompt, or the shipped default.

    Never raises: an unreadable file degrades to the default, matching how the
    app's other readers treat a broken file — the cron must always have SOMETHING
    to follow. An empty/whitespace file is treated as absent, so a file that was
    somehow blanked still yields the default rather than an empty instruction.
    """
    try:
        text = prompt_path().read_text(encoding="utf-8")
    except FileNotFoundError:
        return DEFAULT_RECONCILE_PROMPT
    except OSError:
        logger.warning("chat-status-tags: unreadable reconcile prompt — using default")
        return DEFAULT_RECONCILE_PROMPT
    return text if text.strip() else DEFAULT_RECONCILE_PROMPT


def set_prompt(text: str) -> None:
    """Persist a custom reconcile prompt, or reset to default.

    Empty or whitespace-only ``text`` DELETES the file (reset to default). A
    non-empty value is written verbatim (capped at ``MAX_PROMPT_LEN``) with
    ``newline=""`` so it lands byte-for-byte — the agent reads exactly what the
    operator typed.
    """
    with _prompt_mutation():
        target = prompt_path()
        if not text or not text.strip():
            try:
                target.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                logger.warning("chat-status-tags: could not delete reconcile prompt", exc_info=True)
            return
        atomic_write(target, text[:MAX_PROMPT_LEN], newline="")


def is_default() -> bool:
    """True when no custom prompt is stored (the default is in effect)."""
    return get_prompt() == DEFAULT_RECONCILE_PROMPT


def seed_default() -> None:
    """Write the default to disk if the file is absent. Used by ``on_startup``.

    Idempotent and lock-guarded: only writes when nothing is there, so it never
    clobbers an operator's edit on a later restart. A pre-existing file (custom
    OR a previously-seeded default) is left exactly as it is.
    """
    with _prompt_mutation():
        target = prompt_path()
        if target.exists():
            return
        try:
            atomic_write(target, DEFAULT_RECONCILE_PROMPT, newline="")
        except OSError:
            logger.warning("chat-status-tags: could not seed reconcile prompt", exc_info=True)


# ── behaviour toggles (JSON) ───────────────────────────────────────────────
#
# A separate, tiny JSON document living in the SAME app data dir as the prompt
# and written with the SAME atomic_write + file_lock discipline. It is kept
# apart from the prompt (which is free text stored as raw bytes) so neither read
# can corrupt the other and each degrades to its own default independently.


def flags_path() -> Path:
    """Absolute path to the toggles JSON file (its parent is created)."""
    return app_data_dir(APP_NAME) / FLAGS_FILENAME


@contextmanager
def _flags_mutation() -> Iterator[None]:
    """Cross-process lock for a flags read-modify-write.

    Same discipline as ``_prompt_mutation``: ``atomic_write`` makes each WRITE
    atomic, but ``set_flags`` is a load-modify-write (merge a partial onto the
    current document), so a concurrent PUT must serialise. Lock a sibling
    ``.lock`` file — flock follows the inode, and locking the data file would
    guard an inode the atomic rename is about to replace.
    """
    target = flags_path()
    lock_path = target.with_name(target.name + ".lock")
    target.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        with file_lock(fd, exclusive=True):
            yield
    finally:
        os.close(fd)


def _read_flags_raw() -> Dict[str, bool]:
    """The full effective flag map: shipped defaults overlaid with the file.

    Never raises. A missing file, unreadable file, non-object JSON, or malformed
    JSON all degrade to the defaults. Individual keys are only adopted from the
    file when present AND boolean — a corrupt/foreign value for one key falls
    back to that key's default without discarding the others. Unknown keys in
    the file are ignored, so the schema can shrink without a migration.
    """
    flags = dict(_FLAG_DEFAULTS)
    try:
        raw = flags_path().read_text(encoding="utf-8")
    except FileNotFoundError:
        return flags
    except OSError:
        logger.warning("chat-status-tags: unreadable toggles file — using defaults")
        return flags
    try:
        data = json.loads(raw)
    except ValueError:
        logger.warning("chat-status-tags: corrupt toggles file — using defaults")
        return flags
    if not isinstance(data, dict):
        return flags
    for key in _FLAG_DEFAULTS:
        val = data.get(key)
        if isinstance(val, bool):
            flags[key] = val
    return flags


def get_flags() -> Dict[str, bool]:
    """The effective behaviour toggles (defaults merged with the stored file)."""
    return _read_flags_raw()


def set_flags(**partial: Any) -> Dict[str, bool]:
    """Merge a partial set of toggles onto the stored document; return the result.

    Every value must be a real ``bool`` — a non-bool (including a JSON ``null``,
    number, or string) raises ``ValueError``, since a coerced flag would silently
    turn a paid loop on or off against the operator's intent. An unknown key
    raises ``ValueError`` too, so a typo is refused rather than written into a
    file where it is then ignored. An empty call is a no-op that returns the
    current effective flags.
    """
    for key, val in partial.items():
        if key not in _FLAG_DEFAULTS:
            raise ValueError(f"unknown flag: {key}")
        if not isinstance(val, bool):
            raise ValueError(f"flag {key} must be a bool")
    with _flags_mutation():
        current = _read_flags_raw()
        current.update({k: bool(v) for k, v in partial.items()})
        try:
            atomic_write(flags_path(), json.dumps(current, sort_keys=True), newline="")
        except OSError:
            # PROPAGATE: a swallowed write here would let the HTTP layer report
            # success while the stored flags disagree with the live automation
            # (the cron may already have been mutated on this request). The
            # route catches this, rolls back the cron change, and returns 500.
            logger.warning("chat-status-tags: could not persist toggles", exc_info=True)
            raise
        return current
