"""Serving an installed app's own store art from its install directory.

``/apps/{name}/art/{path}`` exists because the bytes of an installed app's icon,
hero and screenshots are already on local disk. The blob proxy it replaces
reaches them by a git clone gated by an SSRF allowlist, which is warmed by a
network fetch a page render can outrun -- and an ``<img>`` does not retry a 403,
so a catalog-listed app's art vanished for that paint.

What decides whether this route is correct is not that it serves a file, but
**which** files it refuses to serve. Two narrowings carry that, and each has a
way of failing open:

**Images only.** ``_ALLOWED_EXTENSIONS`` (the UI-bundle route's list) admits
``.json``, so reusing it here would also serve ``installed.json`` and
``app.json`` out of the install directory -- a widening paid to display an icon.

**Declared paths only.** The path must be one the app's own manifest names, so
the manifest -- not the request -- chooses the file. That is what lets the route
carry no traversal reasoning of its own, and it is why the containment check is
STILL required: a manifest is the app's own untrusted content, so a declared
value is not evidence the file lands inside the install directory.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.apps import routes as app_routes

APP = "demo-app"


def _make_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/apps/{name}/art/{path:.*}", app_routes.handle_app_art_file)
    return app


@pytest.fixture()
def installed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An installed app carrying real art bytes, with a manifest declaring them."""
    root = tmp_path / "apps"
    app_dir = root / APP
    (app_dir / "assets" / "screenshots").mkdir(parents=True)
    (app_dir / "assets" / "icon.webp").write_bytes(b"icon-bytes")
    (app_dir / "assets" / "hero-detail.webp").write_bytes(b"hero-bytes")
    (app_dir / "assets" / "screenshots" / "one.webp").write_bytes(b"shot-bytes")
    # Not art, and not declared: the file a path filter over the install dir
    # would have served, since `.json` is in the UI route's extension allowlist.
    (app_dir / "installed.json").write_text(json.dumps({"name": APP}), encoding="utf-8")
    (app_dir / "secret.webp").write_bytes(b"undeclared-bytes")

    manifest = {
        "iconPath": "assets/icon.webp",
        "heroImageDetail": "./assets/hero-detail.webp",
        "screenshots": ["assets/screenshots/one.webp"],
    }
    monkeypatch.setattr(app_routes, "apps_dir", lambda: root)
    monkeypatch.setattr(
        "kiro_crew.apps.routes.get_app_manifest",
        lambda name: _FakeManifest(manifest) if name == APP else None,
    )
    return app_dir


class _FakeManifest:
    """The shape ``get_app_manifest`` returns: declared art lives in ``extra``."""

    def __init__(self, extra: dict[str, Any]) -> None:
        self.extra = extra


async def _get(path: str) -> tuple[int, bytes]:
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.get(path)
        return resp.status, await resp.read()


@pytest.mark.asyncio
async def test_a_declared_art_path_is_served_from_the_install_directory(installed: Path) -> None:
    status, body = await _get(f"/apps/{APP}/art/assets/icon.webp")
    assert status == 200
    assert body == b"icon-bytes"


@pytest.mark.asyncio
async def test_a_declared_dot_slash_path_matches_the_normalized_request(installed: Path) -> None:
    """The manifest may write ``./assets/x``; the frontend strips the ``./`` before
    requesting, so the declaration set must be normalized the same way or the
    request never matches its own declaration."""
    status, body = await _get(f"/apps/{APP}/art/assets/hero-detail.webp")
    assert status == 200
    assert body == b"hero-bytes"


@pytest.mark.asyncio
async def test_a_declared_list_entry_is_served(installed: Path) -> None:
    status, body = await _get(f"/apps/{APP}/art/assets/screenshots/one.webp")
    assert status == 200
    assert body == b"shot-bytes"


@pytest.mark.asyncio
async def test_a_real_file_the_manifest_never_declared_is_refused(installed: Path) -> None:
    """The narrowing that makes the route need no traversal reasoning: existing,
    image-shaped, inside the directory -- and still refused, because the manifest
    does not name it."""
    status, _ = await _get(f"/apps/{APP}/art/secret.webp")
    assert status == 404


@pytest.mark.asyncio
async def test_installed_json_is_not_reachable(installed: Path) -> None:
    """The concrete cost of reusing the UI route's extension allowlist, which
    admits ``.json``. Refused twice over here -- wrong type AND undeclared."""
    status, _ = await _get(f"/apps/{APP}/art/installed.json")
    assert status == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("ext", [".mjs", ".js", ".css", ".json", ".woff2", ".map", ".txt", ""])
async def test_every_non_image_type_is_refused(installed: Path, ext: str) -> None:
    status, _ = await _get(f"/apps/{APP}/art/assets/thing{ext}")
    assert status == 403


@pytest.mark.asyncio
async def test_an_encoded_slash_smuggles_dot_dot_past_the_router_and_the_handler_refuses_it(
    installed: Path,
) -> None:
    """The handler's own ``..`` guard is load-bearing, not belt-and-braces.

    Measured against a real aiohttp router: ``../secret.webp`` and
    ``assets/%2e%2e/…`` are normalized away before matching and never reach the
    handler (404 from the router). But ``assets/..%2fsecret.webp`` DOES -- the
    encoded slash stops the normalization, and ``match_info['path']`` arrives as
    ``assets/../secret.webp``. So the router alone does not contain this route,
    and removing the guard would hand that value to a path join.
    """
    status, _ = await _get(f"/apps/{APP}/art/assets/..%2fsecret.webp")
    assert status == 400


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["../secret.webp", "assets/../../secret.webp", "/etc/x.webp"])
async def test_a_traversal_shaped_request_never_serves_bytes(installed: Path, path: str) -> None:
    """Refusal is what matters, not which layer refuses: these three are
    normalized away by the router before the handler sees them, so asserting a
    specific status here would pin aiohttp's behaviour rather than ours."""
    status, body = await _get(f"/apps/{APP}/art/{path}")
    assert status != 200
    assert b"undeclared-bytes" not in body


@pytest.mark.asyncio
async def test_a_declared_path_escaping_the_directory_by_symlink_is_refused(
    installed: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Why containment is checked even though the path had to be DECLARED: the
    manifest is the app's own untrusted content, so a declared value is not
    evidence the file lands inside the install directory."""
    outside = tmp_path / "outside.webp"
    outside.write_bytes(b"outside-bytes")
    link = installed / "assets" / "linked.webp"
    link.symlink_to(outside)
    monkeypatch.setattr(
        "kiro_crew.apps.routes.get_app_manifest",
        lambda name: _FakeManifest({"iconPath": "assets/linked.webp"}),
    )
    status, _ = await _get(f"/apps/{APP}/art/assets/linked.webp")
    assert status == 404


@pytest.mark.asyncio
async def test_a_declared_path_whose_symlink_loops_answers_404_not_500(
    installed: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``Path.resolve()`` raises ``RuntimeError`` on a symlink loop -- NOT an
    ``OSError`` -- so catching only ``ValueError`` let a declared self-referential
    link escape as a 500 on a route whose every other refusal is a clean status.
    The app that plants the link is the one whose art this route serves, so the
    hostile input is exactly the input this endpoint exists to read."""
    link = installed / "assets" / "loop.webp"
    link.symlink_to("loop.webp")  # points at itself, in its own directory
    monkeypatch.setattr(
        "kiro_crew.apps.routes.get_app_manifest",
        lambda name: _FakeManifest({"iconPath": "assets/loop.webp"}),
    )
    status, _ = await _get(f"/apps/{APP}/art/assets/loop.webp")
    assert status == 404, "a loop is not servable, which is the same answer as missing"


@pytest.mark.asyncio
async def test_a_mutual_symlink_pair_also_answers_404(
    installed: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The two-file form of the same loop, which a single-file guard can miss."""
    a = installed / "assets" / "a.webp"
    b = installed / "assets" / "b.webp"
    a.symlink_to("b.webp")
    b.symlink_to("a.webp")
    monkeypatch.setattr(
        "kiro_crew.apps.routes.get_app_manifest",
        lambda name: _FakeManifest({"iconPath": "assets/a.webp"}),
    )
    status, _ = await _get(f"/apps/{APP}/art/assets/a.webp")
    assert status == 404


@pytest.mark.asyncio
async def test_a_symlink_at_the_declared_NAME_is_refused_even_inside_the_root(
    installed: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The property that closes the check-to-use swap.

    Validating a path and then handing it to ``FileResponse`` opened it a SECOND
    time, so the app that owns this directory could swap a declared name for a
    symlink between the two and have the gateway read the target instead -- and the
    gateway is not sandboxed, so that laundered a read the app's own code can be
    refused. There is no window if the final name may not be a link AT ALL, which is
    what ``O_NOFOLLOW`` on the pinned open enforces. So a link pointing at a
    perfectly legitimate file INSIDE the root is refused too: it is the indirection
    that is refused, not the destination, because only the indirection is swappable.
    """
    real = installed / "assets" / "icon.webp"
    assert real.is_file(), "fixture precondition: the link's target is a real art file"
    link = installed / "assets" / "aliased.webp"
    link.symlink_to("icon.webp")  # inside the root, and a real file
    monkeypatch.setattr(
        "kiro_crew.apps.routes.get_app_manifest",
        lambda name: _FakeManifest({"iconPath": "assets/aliased.webp"}),
    )
    status, body = await _get(f"/apps/{APP}/art/assets/aliased.webp")
    assert status == 404
    assert b"icon-bytes" not in body, "the link's target must not be served"


@pytest.mark.asyncio
async def test_a_declared_path_under_a_SYMLINKED_DIRECTORY_is_refused(
    installed: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What the containment check is still for, once the pinned open exists.

    ``O_NOFOLLOW`` refuses a link at the final NAME, and the handler's own guard
    refuses ``..`` -- so the one route left into the containment check is an ANCESTOR
    that is a link. ``pin_parent`` deliberately does not close that case (its
    contract: a component swapped BEFORE the parent was resolved is followed by that
    resolution), so resolving the parent and proving it is under the resolved root is
    what refuses it. Without the check this serves a file outside the install
    directory entirely.
    """
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "icon.webp").write_bytes(b"outside-bytes")
    linked = installed / "linkdir"
    linked.symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(
        "kiro_crew.apps.routes.get_app_manifest",
        lambda name: _FakeManifest({"iconPath": "linkdir/icon.webp"}),
    )
    status, body = await _get(f"/apps/{APP}/art/linkdir/icon.webp")
    assert status == 404
    assert b"outside-bytes" not in body


@pytest.mark.asyncio
async def test_a_file_over_the_size_ceiling_is_refused(
    installed: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bytes are held rather than streamed, so without a cap an app could make
    the gateway buffer whatever it declared."""
    monkeypatch.setattr(app_routes, "_ART_MAX_BYTES", 64)
    big = installed / "assets" / "big.webp"
    big.write_bytes(b"x" * 65)
    monkeypatch.setattr(
        "kiro_crew.apps.routes.get_app_manifest",
        lambda name: _FakeManifest({"iconPath": "assets/big.webp"}),
    )
    status, _ = await _get(f"/apps/{APP}/art/assets/big.webp")
    assert status == 404, "over the ceiling is not servable, same answer as missing"


@pytest.mark.asyncio
async def test_an_unchanged_file_revalidates_to_304(installed: Path) -> None:
    """`no-cache` means the browser revalidates on every load and the rail renders on
    every load, so without a validator each one would be a full 200. The validator is
    derived from the descriptor the bytes were read from, not a second stat."""
    app = _make_app()
    async with TestClient(TestServer(app)) as client:
        first = await client.get(f"/apps/{APP}/art/assets/icon.webp")
        assert first.status == 200
        etag = first.headers["ETag"]
        assert etag
        again = await client.get(
            f"/apps/{APP}/art/assets/icon.webp", headers={"If-None-Match": etag}
        )
        assert again.status == 304
        assert await again.read() == b"", "a 304 carries no body"


@pytest.mark.asyncio
async def test_a_declared_NUL_name_answers_404_not_500(
    installed: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reachable over HTTP, and it survives every earlier check.

    The extension allowlist reads the suffix AFTER the bad byte
    (``bad\\x00.png`` -> ``.png``), and containment resolves the PARENT, which is
    clean when the bad byte sits in the final component. So the first thing that
    touches it is the open -- where ``os.open`` raises ``ValueError``, never an
    ``OSError`` -- and uncaught that is a 500 on a route whose every other refusal
    is a clean status.
    """
    declared = "assets/bad\x00.png"
    monkeypatch.setattr(
        "kiro_crew.apps.routes.get_app_manifest",
        lambda name: _FakeManifest({"iconPath": declared}),
    )
    # Requested exactly as declared: the declaration check compares the request
    # against the manifest verbatim, so this is the only shape that reaches the open.
    status, _ = await _get(f"/apps/{APP}/art/{declared}")
    assert status == 404
    assert status != 500


@pytest.mark.parametrize(
    "declared",
    [
        # Lone surrogates make `os.open` raise `UnicodeEncodeError` -- a ValueError
        # SUBCLASS -- and they carry NO NUL, which is why the guard has to be on the
        # EXCEPTION and not on the character. Screening NUL at the door would miss
        # every one of these.
        "assets/bad\ud800.png",
        "assets/bad\udfff.png",
        # The bad byte in a DIRECTORY component, where `realpath` on the parent
        # raises before the open does. Different line, same answer.
        "assets\x00/icon.png",
    ],
)
def test_an_unencodable_declared_name_is_refused_at_the_resolver(
    installed: Path, monkeypatch: pytest.MonkeyPatch, declared: str
) -> None:
    """Asserted at the RESOLVER, not over HTTP, because that is where these reach.

    A lone surrogate does not survive the HTTP round trip -- aiohttp cannot encode
    it into a request target -- so an end-to-end test would 404 on the declaration
    check and never touch the open. It would pass while proving nothing, which a
    mutation run showed: removing ``ValueError`` from the open reddened only the NUL
    case. Calling the resolver directly is what makes these cases discriminating.
    """
    monkeypatch.setattr(
        "kiro_crew.apps.routes.get_app_manifest",
        lambda name: _FakeManifest({"iconPath": declared}),
    )
    assert declared in app_routes._declared_art_paths(APP), "precondition: it IS declared"
    assert app_routes._read_declared_art(APP, declared) is None


def test_the_windows_branch_also_refuses_an_unencodable_name(
    installed: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The no-pinned-walk branch needs its own coverage on a POSIX runner.

    ``supports_pinned_walk()`` is True here, so the platform fallback never
    executes and its guard is untested by every case above -- a mutation removing
    ``ValueError`` from it SURVIVED until this test existed. Forcing the predicate
    False exercises the branch Windows actually takes.
    """
    monkeypatch.setattr(app_routes, "supports_pinned_walk", lambda: False)
    declared = "assets/bad\x00.png"
    monkeypatch.setattr(
        "kiro_crew.apps.routes.get_app_manifest",
        lambda name: _FakeManifest({"iconPath": declared}),
    )
    assert app_routes._read_declared_art(APP, declared) is None


def test_the_windows_branch_still_serves_a_normal_declared_file(
    installed: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """And it must still WORK -- a branch that refuses everything would pass the
    test above while breaking the route on the platform it exists for."""
    monkeypatch.setattr(app_routes, "supports_pinned_walk", lambda: False)
    out = app_routes._read_declared_art(APP, "assets/icon.webp")
    assert out is not None and out[0] == b"icon-bytes"


@pytest.mark.asyncio
async def test_a_declared_path_that_is_a_HARDLINK_is_refused(
    installed: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one alias `O_NOFOLLOW` cannot see, and the gateway is not sandboxed.

    A hardlink shares its target's inode, so every path-based guard is blind to it:
    `is_symlink()` is False, `realpath` yields the alias's OWN name so containment
    passes, and `O_NOFOLLOW` has no link to refuse. Measured before the fix: such a
    declared path opened cleanly, reported `S_ISREG`, sat under the size cap, and its
    bytes were served with a 200 -- laundering a read the app's own sandboxed code
    can be refused. `st_nlink` is the only signal, and it is only readable on the
    descriptor.
    """
    outside = tmp_path / "not-art-at-all"
    outside.write_bytes(b"SENSITIVE-BYTES")
    alias = installed / "assets" / "aliased-icon.webp"
    os.link(outside, alias)  # a HARDLINK, not a symlink
    assert not alias.is_symlink(), "precondition: no symlink guard can see this"
    assert alias.stat().st_nlink == 2, "precondition: the alias is a second name"
    monkeypatch.setattr(
        "kiro_crew.apps.routes.get_app_manifest",
        lambda name: _FakeManifest({"iconPath": "assets/aliased-icon.webp"}),
    )
    status, body = await _get(f"/apps/{APP}/art/assets/aliased-icon.webp")
    assert status == 404
    assert b"SENSITIVE-BYTES" not in body


@pytest.mark.asyncio
async def test_an_ordinary_single_link_file_is_still_served(installed: Path) -> None:
    """The mirror: the nlink gate must not refuse a normal art file.

    A gate that refused everything would satisfy the test above while breaking the
    route, and `st_nlink == 1` is the ordinary case it has to keep admitting.
    """
    assert (installed / "assets" / "icon.webp").stat().st_nlink == 1
    status, body = await _get(f"/apps/{APP}/art/assets/icon.webp")
    assert status == 200
    assert body == b"icon-bytes"


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs are POSIX-only")
@pytest.mark.asyncio
async def test_a_declared_FIFO_does_not_hang_the_request(
    installed: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reason the flags carry `O_NONBLOCK`, and why `S_ISREG` alone is too late.

    Opening a FIFO blocks until a writer appears, and this handler runs inside
    `asyncio.to_thread` -- so a declared FIFO parks a thread-pool worker forever and
    enough requests starve every other blocking call in the gateway. The descriptor
    checks cannot help, because the block happens BEFORE `fstat`. Measured: the open
    hangs indefinitely without `O_NONBLOCK` and returns immediately with it.

    If this test ever hangs rather than failing, that IS the regression.
    """
    fifo = installed / "assets" / "piped.webp"
    os.mkfifo(fifo)
    monkeypatch.setattr(
        "kiro_crew.apps.routes.get_app_manifest",
        lambda name: _FakeManifest({"iconPath": "assets/piped.webp"}),
    )
    status, _ = await asyncio.wait_for(_get(f"/apps/{APP}/art/assets/piped.webp"), timeout=10)
    assert status == 404, "a FIFO is not a plain file, so it is not servable"


@pytest.mark.asyncio
async def test_a_declared_DIRECTORY_is_refused(
    installed: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other non-regular shape reachable through a declaration."""
    (installed / "assets" / "adir.webp").mkdir()
    monkeypatch.setattr(
        "kiro_crew.apps.routes.get_app_manifest",
        lambda name: _FakeManifest({"iconPath": "assets/adir.webp"}),
    )
    status, _ = await _get(f"/apps/{APP}/art/assets/adir.webp")
    assert status == 404


@pytest.mark.asyncio
async def test_the_response_neuters_a_scripted_svg_on_this_origin(
    installed: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`.svg` is allowed because an SVG in an `<img>` is script-inert -- but a
    TOP-LEVEL NAVIGATION to this URL makes the response a DOCUMENT on the
    dashboard's own origin, where the base CSP is deliberately permissive
    (``script-src 'self' 'unsafe-inline'``, so widget and MCP-app iframes can run
    inline script) and would NOT stop a scripted SVG an app declared as its art.

    So the response carries its own policy. ``sandbox`` with no tokens gives the
    document an opaque origin and no script; ``default-src 'none'`` stops it
    fetching anything.
    """
    svg = installed / "assets" / "scripted.svg"
    svg.write_bytes(
        b'<svg xmlns="http://www.w3.org/2000/svg"><script>fetch("/api/apps")' b"</script></svg>"
    )
    monkeypatch.setattr(
        "kiro_crew.apps.routes.get_app_manifest",
        lambda name: _FakeManifest({"iconPath": "assets/scripted.svg"}),
    )
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.get(f"/apps/{APP}/art/assets/scripted.svg")
        assert resp.status == 200, "the bytes are still served -- this is not a refusal"
        csp = resp.headers.get("Content-Security-Policy", "")
        assert "sandbox" in csp, csp
        assert "default-src 'none'" in csp, csp
        # Declared type comes from the EXTENSION, not the bytes, so a `.png` holding
        # markup must not be sniffed into a document either.
        assert resp.headers.get("X-Content-Type-Options") == "nosniff"


@pytest.mark.asyncio
async def test_every_served_art_type_carries_the_same_policy(installed: Path) -> None:
    """Not only the SVG: the policy is on the response, so a raster answer carries
    it too. A guard applied per-extension would be one `if` away from a gap."""
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.get(f"/apps/{APP}/art/assets/icon.webp")
        assert resp.status == 200
        assert "sandbox" in resp.headers.get("Content-Security-Policy", "")
        assert resp.headers.get("X-Content-Type-Options") == "nosniff"


def test_both_art_paths_screen_on_the_SAME_extension_set() -> None:
    """This route REPLACES the blob proxy per surface, so a file one serves and the
    other refuses means the same app's art renders or 403s depending only on
    whether it happens to be installed. The two sets were spelled separately and
    were identical member-for-member with nothing pinning them -- a divergence
    waiting for whoever edited one of them next.

    Asserted on MEMBERS, not on names: a reintroduced duplicate under any name is
    the defect. ``_ALLOWED_EXTENSIONS`` (the UI-bundle route's set) is a different
    set on purpose -- it admits ``.json``, which is precisely why this route does
    not reuse it -- so it must NOT be folded in here.
    """
    source = Path(app_routes.__file__).read_text(encoding="utf-8")
    art = app_routes._ART_IMAGE_EXTENSIONS
    duplicates = [
        name
        for name, body in re.findall(
            r"^(_[A-Za-z_]+)\s*=\s*frozenset\(\{([^}]*)\}\)", source, re.MULTILINE
        )
        if name != "_ART_IMAGE_EXTENSIONS"
        and {p.strip().strip("\"'") for p in body.split(",") if p.strip()} == set(art)
    ]
    assert duplicates == [], f"these re-spell the art extension set: {duplicates}"
    assert source.count("_ART_IMAGE_EXTENSIONS") == 3, "one definition plus both screening sites"


@pytest.mark.asyncio
async def test_a_declared_path_whose_file_is_missing_answers_the_same_404(
    installed: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One answer for undeclared, escaping and missing, so a probe cannot use the
    status to map which paths a manifest declares."""
    monkeypatch.setattr(
        "kiro_crew.apps.routes.get_app_manifest",
        lambda name: _FakeManifest({"iconPath": "assets/gone.webp"}),
    )
    status, _ = await _get(f"/apps/{APP}/art/assets/gone.webp")
    assert status == 404


@pytest.mark.asyncio
async def test_an_app_with_no_manifest_serves_nothing(
    installed: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("kiro_crew.apps.routes.get_app_manifest", lambda name: None)
    status, _ = await _get(f"/apps/{APP}/art/assets/icon.webp")
    assert status == 404


@pytest.mark.asyncio
async def test_a_manifest_whose_art_fields_are_the_wrong_type_serves_nothing(
    installed: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``app.json`` is JSON from disk, so its field TYPES are not guaranteed
    either -- a dict where a string belongs must not reach a string operation."""
    monkeypatch.setattr(
        "kiro_crew.apps.routes.get_app_manifest",
        lambda name: _FakeManifest({"iconPath": {}, "screenshots": {}, "heroImage": 42}),
    )
    status, _ = await _get(f"/apps/{APP}/art/assets/icon.webp")
    assert status == 404


@pytest.mark.asyncio
async def test_the_response_carries_the_image_type_and_revalidates(installed: Path) -> None:
    """An app update rewrites these bytes in place under the same URL, so the
    browser must revalidate rather than hold a long max-age copy."""
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.get(f"/apps/{APP}/art/assets/icon.webp")
        assert resp.status == 200
        assert resp.headers["Content-Type"] == "image/webp"
        assert resp.headers["Cache-Control"] == "no-cache"
