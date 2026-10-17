"""Guards that builtin app icon/hero art referenced as ``/app-assets/...`` exists.

Builtin manifests point at brand SVGs via absolute ``/app-assets/<app>/<file>``
paths (top-level ``iconUrl`` / ``heroImage`` / ``heroImageDark``). Those files
live in ``website/public/app-assets/`` and are served by the gateway's
``/app-assets`` static mount. A manifest that references a non-existent file
renders the ``<img onError>`` placeholder instead of the intended art — the exact
failure mode this suite prevents (e.g. Agent Worlds shipping a lucide glyph with
its ``icon.svg`` left unreferenced, or an iconUrl pointing at a missing file).
"""

from __future__ import annotations

import re
from pathlib import Path

import kiro_crew.apps.manager as mgr
from kiro_crew.apps.discovery import discover_builtin_apps

_REPO_ROOT = Path(__file__).resolve().parents[1]
_APP_ASSETS_DIR = _REPO_ROOT / "website" / "public" / "app-assets"
#: Every top-level manifest field that can name an ``/app-assets/`` file. The
#: two detail banners were missing, so ten builtins referenced art this guard
#: never checked — a typo in a ``heroImageDetail`` path would have shipped the
#: broken-image placeholder on the app's own detail page with CI green.
#: ``iconInactiveUrl`` is in the list for the same reason and not because a
#: builtin uses it today: it is a real ``AppManifest`` field (manifest.py), so the
#: first builtin to declare one must be covered by the guard already, rather than
#: by whoever notices the omission after a dead ref ships.
_ASSET_FIELDS = (
    "iconUrl",
    "iconInactiveUrl",
    "heroImage",
    "heroImageDark",
    "heroImageDetail",
    "heroImageDetailDark",
)
_ASSET_LIST_FIELDS = ("screenshots", "screenshotsDark")

#: A reference the client can serve AND the published catalog can carry.
#: Deliberately stricter than "starts with /app-assets/": the store's catalog
#: bakes a builtin's ``iconUrl`` verbatim into the published document's
#: ``iconRef``, whose schema refuses a scheme, a protocol-relative ``//host``, a
#: ``..`` segment and anything outside a conservative charset — and a value it
#: refuses fails validation on the ASSEMBLED catalog, where the error withholds
#: every OTHER app's release too. Existence alone cannot see that class: a
#: traversal ref like ``/app-assets/../README.md`` resolves to a real file.
_PUBLISHABLE_REF_RE = re.compile(r"^/app-assets/[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+$")


def _asset_refs(app: dict) -> list[tuple[str, str]]:
    """Return (field, path) for each /app-assets/ reference on an app dict."""
    refs: list[tuple[str, str]] = []
    for field in _ASSET_FIELDS:
        val = app.get(field)
        if isinstance(val, str) and val.startswith("/app-assets/"):
            refs.append((field, val))
    for field in _ASSET_LIST_FIELDS:
        values = app.get(field, [])
        if isinstance(values, list):
            refs.extend(
                (field, val)
                for val in values
                if isinstance(val, str) and val.startswith("/app-assets/")
            )
    return refs


def _resolve(app_assets_path: str) -> Path:
    """Map a served ``/app-assets/<rel>`` URL to its public/ source file."""
    rel = app_assets_path[len("/app-assets/") :]
    return _APP_ASSETS_DIR / rel


def _all_builtin_apps() -> list[dict]:
    return [*mgr._BUILTIN_APPS, *discover_builtin_apps()]


def test_all_builtin_app_assets_exist() -> None:
    """Every ``/app-assets/...`` icon/hero referenced by a builtin exists on disk."""
    missing: list[str] = []
    for app in _all_builtin_apps():
        for field, path in _asset_refs(app):
            if not _resolve(path).is_file():
                missing.append(f"{app.get('name')}.{field} -> {path}")
    assert not missing, "builtin app-asset references with no file:\n" + "\n".join(missing)


def test_builtin_discovery_is_not_vacuous() -> None:
    """A floor under every other test in this file.

    All of them iterate ``_all_builtin_apps()`` and assert an empty problem list,
    so a discovery path that returns nothing — a moved builtins directory, a
    refactor of ``discover_builtin_apps()`` — makes the whole suite pass while
    checking zero manifests. That is the one failure mode a guard cannot report on
    itself, so it is pinned as a count.
    """
    apps = _all_builtin_apps()
    with_icons = [a for a in apps if isinstance(a.get("iconUrl"), str) and a["iconUrl"]]
    assert len(apps) >= 20, f"builtin discovery returned only {len(apps)} apps"
    assert len(with_icons) >= 20, (
        f"only {len(with_icons)} discovered builtins carry an iconUrl; the asset "
        f"guards below would be near-vacuous"
    )


def test_builtin_app_assets_are_not_empty() -> None:
    """A referenced file that is zero bytes is a dead ref that passes existence.

    Separate from the existence guard because the cause is different and the fix
    is different: the manifest is right and the ASSET is broken (a truncated
    export, a git-lfs pointer that never resolved), and the store renders the same
    nothing as a missing file.
    """
    empty: list[str] = []
    for app in _all_builtin_apps():
        for field, path in _asset_refs(app):
            resolved = _resolve(path)
            if resolved.is_file() and resolved.stat().st_size == 0:
                empty.append(f"{app.get('name')}.{field} -> {path}")
    assert not empty, "builtin app-asset references to an EMPTY file:\n" + "\n".join(empty)


def test_builtin_asset_refs_are_publishable_paths() -> None:
    """Every ref is a path the client can serve and the catalog can publish.

    See ``_PUBLISHABLE_REF_RE``: the store's published catalog copies a builtin's
    ``iconUrl`` into ``iconRef`` verbatim, and a value its schema rejects fails
    validation on the assembled document — withholding every other app's release,
    not just this one's icon. Caught here, the blast radius is one PR.
    """
    unpublishable: list[str] = []
    for app in _all_builtin_apps():
        for field, path in _asset_refs(app):
            if not _PUBLISHABLE_REF_RE.match(path):
                unpublishable.append(f"{app.get('name')}.{field} -> {path}")
    assert not unpublishable, (
        "builtin asset refs the published catalog schema would reject "
        "(scheme, //host, .. segment, or an unexpected character):\n" + "\n".join(unpublishable)
    )


def test_every_builtin_declares_an_icon() -> None:
    """Every builtin names its own mark in ``iconUrl``.

    Existence checks alone leave the store green when a manifest omits ``iconUrl``
    entirely: the app then falls through ``AppIconTile``'s last resort — a
    name-hashed gradient carrying the generic lucide ``Package`` glyph — and reads
    as a stray placeholder among 20 apps that have real identities. That is how
    Channels, Dev Fleet and Workflows shipped iconless, and how Dev Fleet's
    ``icon.svg`` sat on disk unreferenced. The lucide name under
    ``ui.pages[].icon`` does not substitute: it dresses the sidebar nav row, and
    ``AppIcon``'s ICON_MAP carries only a handful of names, so most fall back to
    ``Package`` too.
    """
    iconless = [
        a.get("name")
        for a in _all_builtin_apps()
        if not isinstance(a.get("iconUrl"), str) or not a["iconUrl"]
    ]
    assert not iconless, (
        "builtins with no iconUrl (they render the gradient + Package "
        f"placeholder): {sorted(map(str, iconless))}"
    )


def test_every_discovered_builtin_declares_a_real_screenshot() -> None:
    """A hero is marketing art, not proof of the product surface.

    Every manifest-backed builtin must give its detail page at least one real UI
    capture. Keeping this on discovered manifests avoids the legacy static
    registration records, which are runtime shims rather than Store listings.
    """
    missing = [
        app.get("name")
        for app in discover_builtin_apps()
        if not isinstance(app.get("screenshots"), list) or not app["screenshots"]
    ]
    assert not missing, f"builtin manifests with no screenshots: {sorted(map(str, missing))}"


def test_builtin_svg_icons_are_themeable() -> None:
    """Builtin SVG icons paint through the ``--ico-a``/``--ico-b`` tokens.

    ``AppIcon`` inlines these SVGs specifically so the active theme cascades in,
    driving idle (muted + accent highlight) versus selected (accent-dominant)
    from two custom properties. An icon that hardcodes its colours — a lucide
    glyph pasted in with ``stroke="#8b90a5"``, say — opts out of both states: it
    never lights up on selection and it keeps a light-theme grey on dark themes,
    where it is the one low-contrast mark in the list. Raster icons are exempt;
    their bytes are fixed, which is what ``iconUrlDark`` exists for.
    """
    unthemed: list[str] = []
    for app in _all_builtin_apps():
        for field, path in _asset_refs(app):
            if field != "iconUrl" or not path.endswith(".svg"):
                continue
            markup = _resolve(path).read_text(encoding="utf-8")
            if "--ico-a" not in markup and "--ico-b" not in markup:
                unthemed.append(f"{app.get('name')} -> {path}")
    assert (
        not unthemed
    ), "builtin SVG icons with hardcoded colours instead of --ico-a/--ico-b:\n" + "\n".join(
        unthemed
    )


def test_agent_worlds_icon_wired_to_svg() -> None:
    """Agent Worlds' ``iconUrl`` still points at ``worlds/icon.svg`` specifically.

    Only the literal path is this test's own: that the value exists is covered by
    ``test_every_builtin_declares_an_icon`` and that it resolves by
    ``test_all_builtin_app_assets_exist``. What neither can see is a manifest
    silently repointed at some other app's art, which renders a plausible icon
    and reads as green.
    """
    worlds = next((a for a in _all_builtin_apps() if a["name"] == "agent-worlds"), None)
    assert worlds is not None, "agent-worlds builtin missing from every registration path"
    assert worlds.get("iconUrl") == "/app-assets/worlds/icon.svg"
