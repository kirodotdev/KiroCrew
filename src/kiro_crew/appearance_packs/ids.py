"""What a legal appearance-pack id is, shared by the config and the pack store.

A pack id is two things at once. On disk it is a DIRECTORY NAME under the
appearance library, so it is the boundary that stops ``../`` escaping the
library. In ``config.json`` it is a VALUE — ``agents.*.avatar.id`` — naming
which pack a crew wears. Both sides have to agree on what is legal, or a crew
could persist a reference the store is obliged to refuse.

The two sides cannot share the store's own copy of the rule.
``config/sections.py`` is deliberately one-way ("it must not import the loader,
schema, or validation modules") and the store reads the filesystem, so pulling
it into a config load would put ``json``/``shutil`` and a directory walk behind
every load. So the rule lives here, in a module that imports nothing but the
standard library, and both sides read it.

The character class is what the pack store has always enforced, unchanged:
``str.isalnum`` plus dash and underscore. ``isalnum`` is Unicode-aware, so the
class is wider than ASCII — deliberately kept that way, because narrowing it
here would make an already-installed pack whose directory carries a non-ASCII
letter stop listing, which loses the user's art from the gallery for no
security gain (the name still cannot contain a separator, a dot, a drive
prefix or a NUL).
"""

from __future__ import annotations

import unicodedata
from typing import Any

#: The pack every install has: its art ships inside the frontend bundle, so it
#: has no directory on disk and is never imported, exported or deleted. Lives
#: here rather than in the store module so the config and the dashboard can name
#: it without importing a filesystem reader.
DEFAULT_PACK = "kiro-ghost"

#: Cap on a pack id. Long enough for a descriptive name, short enough that the
#: resulting path stays well inside every platform's limit.
MAX_PACK_ID_LEN = 64


def safe_pack_id(raw: Any) -> str | None:
    """Validate a pack id as a single safe path segment, or ``None``.

    Rejecting is correct here rather than sanitising: a caller sending a
    traversal is not making a typo, and silently rewriting it would hide that.
    """
    if not isinstance(raw, str):
        return None
    ident = raw.strip()
    if not ident or len(ident) > MAX_PACK_ID_LEN:
        return None
    if ident in (".", ".."):
        return None
    # Letters, digits, dash and underscore only — no separators, no dots.
    if not all(c.isalnum() or c in "-_" for c in ident):
        return None
    return ident


def pack_id_key(ident: str) -> str:
    """The key on which two pack ids count as THE SAME pack.

    A pack id is a directory name, and the filesystems the gateway runs on do
    not all agree on identity: macOS and Windows fold case, and macOS (HFS+,
    and APFS for lookups) also treats the NFC and NFD spellings of one string as
    one name. ``safe_pack_id`` keeps both spellings legal -- Hangul syllables
    decompose into conjoining Jamo that are all letters -- so ``str.casefold``
    alone still told ``Aurora`` from ``aurora`` but not the two spellings of
    ``아우로라``. Any guard that asks "is a crew wearing the pack I am about to
    delete" must compare on this key, or a differently-normalized id names the
    same directory, misses the wearer, and removes the art out from under it.

    NFC first, then casefold: casefold can itself produce combining sequences,
    so normalizing afterwards would re-open the gap it closed. Comparison only
    -- the id written to disk is never rewritten, so an installed directory
    keeps listing under whichever spelling created it.
    """
    return unicodedata.normalize("NFC", ident).casefold()
