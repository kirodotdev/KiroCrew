"""Appearance packs: the art a crew or an app wears, and the rules for naming it.

This package is CORE. An app may import it; it imports no app. Crew Companion is
an optional builtin (``defaultEnabled: false``), so a dashboard that reads a pack
cannot reach into that app's tree for the store — the crew's face has to render
whether the app is enabled, disabled, or absent.

Three modules, split by what each is allowed to depend on:

* :mod:`~kiro_crew.appearance_packs.ids` — what a legal pack id is. It imports
  nothing but the standard library, because ``config/sections.py`` reads it and is
  deliberately one-way.
* :mod:`~kiro_crew.appearance_packs.store` — the on-disk library: list, read,
  save, delete.
* :mod:`~kiro_crew.appearance_packs.transfer` — the outside boundary: bundle
  export/import and the PetDex fetch.

Only the id rules are re-exported here. Pulling the store into this ``__init__``
would put ``json``/``shutil`` and the filesystem reader behind every config load,
since ``config/sections.py`` imports this package. Import the store and the
transfer from their own modules.
"""

from __future__ import annotations

from kiro_crew.appearance_packs.ids import (
    DEFAULT_PACK,
    MAX_PACK_ID_LEN,
    pack_id_key,
    safe_pack_id,
)

__all__ = ["DEFAULT_PACK", "MAX_PACK_ID_LEN", "pack_id_key", "safe_pack_id"]
