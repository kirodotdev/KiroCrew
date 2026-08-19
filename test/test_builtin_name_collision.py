"""A builtin may not take a name the bundled seed already gives a third party.

``list_registry`` deduplicates by NAME, and the order is fixed: official/seed
entries enter first, then user-configured registries, and a later row whose name
is already taken is skipped with no diagnostic. So the day a builtin is added
under a name the seed already publishes for a third-party app, that third-party
app stops appearing on every machine running the new wheel -- its author cannot
see why, and the only remedy is another release.

The seed (``app-registry.json``) is the right thing to compare against rather
than the live catalog: a catalog ``git`` row is only kept when the seed or an
external registry also names it, so the seed is the offline-complete list of
third-party names that are actually installable, and reading it keeps this gate
deterministic and network-free.
"""

from __future__ import annotations

import json
from pathlib import Path

from kiro_crew.apps import registry
from kiro_crew.apps.discovery import _get_builtins_dir, discover_builtin_apps


def _seed_third_party_names() -> set[str]:
    """Names the bundled seed publishes, read straight from the shipped file."""
    path: Path = registry._REGISTRY_FILE
    assert path.is_file(), f"bundled seed missing at {path}"
    rows = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(rows, list), "bundled seed must be a JSON array"
    return {r["name"] for r in rows if isinstance(r, dict) and isinstance(r.get("name"), str)}


def _shipped_builtin_names() -> set[str]:
    """Names this wheel's ``builtins/`` dir declares.

    Deliberately NOT ``execution.builtin_app_names()``: that consults
    ``installed.json`` to withdraw trust, so its answer depends on what the
    machine running the test happens to have installed. A gate must read the
    tree, not the desk.
    """
    return {
        app["name"]
        for app in discover_builtin_apps(_get_builtins_dir())
        if isinstance(app.get("name"), str)
    }


def test_no_builtin_shadows_a_seeded_third_party_app() -> None:
    builtins = _shipped_builtin_names()
    seeded = _seed_third_party_names()
    assert builtins, "no builtin apps discovered -- the gate would pass vacuously"
    assert seeded, "no seed rows read -- the gate would pass vacuously"

    collisions = sorted(builtins & seeded)
    assert not collisions, (
        f"builtin app name(s) {collisions} are already published to third parties by "
        f"{registry._REGISTRY_FILE.name}. list_registry dedupes by name and the seed "
        "wins, so shipping this would silently remove those apps from the store for "
        "every user, with no message to their authors. Rename the builtin, or remove "
        "the seed entry deliberately in the same change."
    )
