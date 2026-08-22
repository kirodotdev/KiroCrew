"""Every built-in app declares a logo, and every logo it declares ships.

WHY THIS FILE EXISTS. An app's icon is read from its manifest's top-level
``iconUrl`` by three independent consumers, and none of them can invent one:

- the store's DETAIL page merges the installed manifest (``mergeBuiltinRow``);
- the store's LIST surfaces (Discover rows, Library cards) render rows from the
  published catalog, which bakes ``iconUrl`` into the wire field ``iconRef``;
- the published catalog itself is generated in ``kirodotdev/KiroCrewApps`` by
  reading this very file at a pinned commit.

So a built-in whose manifest omits ``iconUrl`` does not fail anywhere. It draws a
name-hashed gradient with a generic box instead, on every surface that renders a
row — and a placeholder is indistinguishable from an icon the pipeline dropped,
so the store reads as broken rather than the manifest as incomplete. That is not
hypothetical: ``channels``, ``dev-fleet`` and ``workflows`` each shipped an
``icon.svg`` in ``website/public/app-assets/`` for days while their manifests
named none, so the catalog published no ``iconRef`` and the store showed boxes.

The publish pipeline now refuses to publish an iconless first-party entry. This
file is the other half of that gate and the earlier one: it stops such a manifest
from landing on ``main`` at all, where the pipeline can only refuse it after the
fact.

DECLARED IS NOT ENOUGH, so the asset is resolved too. A path that names nothing
is worse than an absent key: the client fetches ``/app-assets/...``, gets a 404
and falls back to the same placeholder, while the catalog happily publishes and
signs the dead ref.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_BUILTINS = _REPO_ROOT / "src/kiro_crew/apps/builtins"
#: Where the referenced bytes live in the source tree. Vite copies this directory
#: into the built dashboard verbatim, so a file present here is a file served at
#: ``/app-assets/...`` — checking the source is checking the served surface.
_PUBLIC = _REPO_ROOT / "website/public"

#: A path the client can serve AND the catalog can publish. Deliberately stricter
#: than "starts with /app-assets/": the published document's schema refuses a
#: scheme, a protocol-relative ``//host``, a ``..`` segment, a backslash and
#: anything outside a conservative charset, and a value it refuses reaches schema
#: validation on the ASSEMBLED catalog — where the error withholds every OTHER
#: app's release too. Catching it here keeps that blast radius at one PR.
_ICON_REF_RE = re.compile(r"^/app-assets/[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+$")
#: Raster or vector both render; the constraint that matters is that the client
#: can draw it. First-party SVGs are the norm because they theme from CSS
#: variables, but ``mochi`` ships a PNG and that is fine.
_ICON_EXTS = {".svg", ".png", ".webp"}

#: Manifest keys naming a display asset the client serves itself. ``iconUrl`` is
#: required; the rest are optional, and are checked only for RESOLUTION — a
#: declared-but-missing hero is the same dead-ref defect as a missing icon, just
#: on a bigger surface.
_OPTIONAL_ASSET_KEYS = (
    "iconUrlDark",
    "iconInactiveUrl",
    "heroImage",
    "heroImageDark",
    "heroImageDetail",
    "heroImageDetailDark",
)


def _manifests() -> list[tuple[str, dict]]:
    """Every shipped built-in manifest, as ``(app dir name, parsed json)``.

    Read off the directory rather than a list in this file: a list is a second
    place to remember, and the app that gets forgotten is exactly the new one
    this test exists to catch.
    """
    out = []
    for app_json in sorted(_BUILTINS.glob("*/app.json")):
        out.append((app_json.parent.name, json.loads(app_json.read_text(encoding="utf-8"))))
    return out


_MANIFESTS = _manifests()


def test_there_are_builtin_manifests_to_check():
    """A guard on the guard: ``glob`` returning nothing would make every
    parametrized test below vacuously pass, and a moved directory is the likeliest
    way for this file to stop testing anything."""
    assert len(_MANIFESTS) >= 20, f"only found {len(_MANIFESTS)} builtin manifests"


@pytest.mark.parametrize("app,manifest", _MANIFESTS, ids=[a for a, _ in _MANIFESTS])
class TestEveryBuiltinDeclaresAnIconThatShips:
    def test_declares_a_top_level_icon_url(self, app, manifest):
        """``ui.pages[].icon`` (a lucide name) is NOT a substitute, which is the
        trap the three iconless built-ins fell into: they named a nav glyph, the
        detail page fell back to it, and the catalog — which reads only the
        top-level key — published nothing."""
        icon = manifest.get("iconUrl")
        assert isinstance(icon, str) and icon, (
            f"{app}: no top-level 'iconUrl'. Every store surface then renders a "
            f"gradient placeholder; a 'ui.pages[].icon' lucide name does not reach "
            f"the published catalog."
        )

    def test_the_icon_path_is_one_the_catalog_can_publish(self, app, manifest):
        icon = manifest.get("iconUrl", "")
        assert _ICON_REF_RE.match(icon), (
            f"{app}: iconUrl {icon!r} is not a publishable client-local path "
            f"(expected /app-assets/<dir>/<file>)"
        )
        assert Path(icon).suffix.lower() in _ICON_EXTS, (
            f"{app}: iconUrl {icon!r} has an extension the client does not render"
        )

    def test_the_icon_it_declares_actually_ships(self, app, manifest):
        icon = manifest.get("iconUrl", "")
        asset = _PUBLIC / icon.lstrip("/")
        assert asset.is_file(), (
            f"{app}: iconUrl {icon!r} names no file under {_PUBLIC.name}/ — the "
            f"client 404s and renders the same placeholder as no icon at all"
        )
        assert asset.stat().st_size > 0, f"{app}: {icon} is empty"

    def test_every_other_declared_asset_also_ships(self, app, manifest):
        """Same dead-ref defect, one surface wider: a hero image or a dark icon
        variant that names nothing degrades silently at render time."""
        for key in _OPTIONAL_ASSET_KEYS:
            value = manifest.get(key)
            if not isinstance(value, str) or not value:
                continue
            if not value.startswith("/app-assets/"):
                # A non-app-asset ref (e.g. an http URL) is a different contract
                # and not this file's business.
                continue
            assert _ICON_REF_RE.match(value), f"{app}: {key} {value!r} is not a safe path"
            assert (_PUBLIC / value.lstrip("/")).is_file(), (
                f"{app}: {key} {value!r} names no file under {_PUBLIC.name}/"
            )
