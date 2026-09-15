"""Tests for the rendered-slides endpoints (handlers/office_slides.py).

The pipeline's external half -- ``soffice`` -- is not installed on CI, so the
conversion step is stubbed at ``_run_soffice`` with a PDF ``pypdfium2`` writes;
everything on either side of it (the shared path prefix, the content-hash cache,
the per-slide serving, the degrade answer, eviction, the spawn contract) runs
for real.
"""

from __future__ import annotations

import functools
import hashlib
import json
import os
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.handlers import office_slides as mod
from kiro_crew.dashboard.handlers.office_slides import (
    api_file_office_slide,
    api_file_office_slides,
)

_MOD = "kiro_crew.dashboard.handlers.office_slides"


def _make_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/api/file-office-slides", api_file_office_slides)
    app.router.add_get("/api/file-office-slide", api_file_office_slide)
    return app


@pytest.fixture
def mock_sel():
    with (
        patch("kiro_crew.sel.sel") as m,
        patch("kiro_crew.dashboard.handlers.files.is_sensitive_path", return_value=False),
    ):
        instance = MagicMock()
        m.return_value = instance
        yield instance


_TEST_ROOT_KEY = b"k" * 48

#: Rendering is pinned by descriptor-relative file operations and FAILS CLOSED
#: without them; these tests exercise the render path and so need them.
_PINNED = pytest.mark.skipif(
    not mod._DIR_FD_OPS, reason="rendering needs descriptor-relative file operations"
)


@pytest.fixture
def cache(tmp_path):
    """An isolated cache root AND a loadable trust root: without the key the
    cache is deliberately inert (nothing read, nothing written)."""
    root = tmp_path / "cache" / "slide-previews"
    with (
        patch(f"{_MOD}.cache_root", return_value=root),
        patch("kiro_crew.dashboard.token_secret._get_secret", return_value=_TEST_ROOT_KEY),
    ):
        yield root


def _write_pptx(path: Path, slides: int = 2) -> str:
    """A minimal deck; returns its sha256 -- the cache key the endpoint derives."""
    with zipfile.ZipFile(path, "w") as zf:
        for i in range(1, slides + 1):
            zf.writestr(
                f"ppt/slides/slide{i}.xml",
                '<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
                ' xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">'
                f"<p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r><a:t>Slide {i}</a:t>"
                "</a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld></p:sld>",
            )
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_pdf(path: Path, pages: int) -> None:
    """A real multi-page PDF, written by the same library the endpoint rasterizes with."""
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument.new()
    for _ in range(pages):
        doc.new_page(960, 540)
    doc.save(str(path))
    doc.close()


def _fake_soffice(pages: int):
    """Stand in for the conversion: write ``out/deck.pdf`` into the work dir."""

    async def _run(argv, work):  # noqa: ARG001 - the contract is the work dir layout
        _write_pdf(Path(work) / "out" / "deck.pdf", pages)

    return _run


def _seed_cache(root: Path, digest: str, count: int, *, signed: bool = True) -> None:
    deck = root / digest
    deck.mkdir(parents=True, exist_ok=True)
    slides = []
    hashes = {}
    for n in range(1, count + 1):
        data = b"\x89PNG-fake-" + bytes([n])
        (deck / f"slide-{n}.png").write_bytes(data)
        slides.append({"n": n, "width": 1280, "height": 720})
        hashes[f"slide-{n}.png"] = hashlib.sha256(data).hexdigest()
    manifest = {
        "status": "ready",
        "digest": digest,
        "count": count,
        "slides": slides,
        "sha256": hashes,
    }
    if signed:
        manifest = mod._sign(manifest, mod._signing_key())
    (deck / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def _validated(path: Path):
    return patch("kiro_crew.dashboard.handlers._validate_dashboard_path", return_value=str(path))


# --- degrade: no LibreOffice on the host --------------------------------------


@pytest.mark.asyncio
async def test_without_soffice_the_manifest_says_unavailable_and_names_the_install_hint(
    tmp_path, mock_sel, cache
):
    f = tmp_path / "deck.pptx"
    _write_pptx(f)
    with (
        _validated(f),
        patch(f"{_MOD}.soffice_path", return_value=None),
        patch(f"{_MOD}.soffice_hint", return_value="sudo apt install libreoffice"),
    ):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-office-slides?path={f}")
            assert resp.status == 200
            body = await resp.json()
    assert body["status"] == "unavailable"
    assert body["reason"] == "soffice_unavailable"
    assert body["hint"] == "sudo apt install libreoffice"
    # Nothing was rendered or staged: the cache is untouched.
    assert not cache.exists()
    denied = [
        c
        for c in mock_sel.log_tool_invocation.call_args_list
        if c.kwargs.get("outcome") == "denied" and c.kwargs.get("error") == "soffice_unavailable"
    ]
    assert denied, "the degrade must be SEL-audited"


# --- the render path -----------------------------------------------------------


@_PINNED
@pytest.mark.asyncio
async def test_renders_once_then_serves_every_later_request_from_the_cache(
    tmp_path, mock_sel, cache
):
    f = tmp_path / "deck.pptx"
    digest = _write_pptx(f)
    soffice = AsyncMock(side_effect=_fake_soffice(3))
    with (
        _validated(f),
        patch(f"{_MOD}.soffice_path", return_value="/usr/bin/soffice"),
        patch(f"{_MOD}._run_soffice", soffice),
    ):
        async with TestClient(TestServer(_make_app())) as client:
            first = await client.get(f"/api/file-office-slides?path={f}")
            assert first.status == 200
            body = await first.json()
            second = await client.get(f"/api/file-office-slides?path={f}")
            assert second.status == 200
            again = await second.json()
    assert body["status"] == "ready"
    assert body["digest"] == digest
    assert body["count"] == 3
    assert body["truncated"] is False
    assert [s["n"] for s in body["slides"]] == [1, 2, 3]
    # Rendered at the fixed width, aspect preserved from the 16:9 page.
    assert body["slides"][0]["width"] == mod.SLIDE_WIDTH_PX
    assert body["slides"][0]["height"] == 720
    assert again == body
    # ONE conversion for two requests: the second answered from the cache.
    assert soffice.await_count == 1
    # The published manifest is signed and names every slide's bytes.
    assert mod._verified(body, mod._signing_key())
    assert sorted(body["sha256"]) == ["slide-1.png", "slide-2.png", "slide-3.png"]
    deck = cache / digest
    assert (deck / "manifest.json").is_file()
    assert sorted(p.name for p in deck.glob("slide-*.png")) == [
        "slide-1.png",
        "slide-2.png",
        "slide-3.png",
    ]
    # The intermediates (staged deck, PDF, profile) do not outlive the render,
    # and no work directory is left behind.
    assert not (deck / "deck.pptx").exists()
    assert not (deck / "out").exists()
    assert not (deck / "profile").exists()
    assert not [p for p in cache.iterdir() if p.name.startswith(".work-")]


@_PINNED
@pytest.mark.asyncio
async def test_a_conversion_failure_is_a_502_and_leaves_no_work_dir(tmp_path, mock_sel, cache):
    f = tmp_path / "deck.pptx"
    _write_pptx(f)

    async def _boom(argv, work):
        raise mod._ConvertFailed("soffice exited 77: bad deck")

    with (
        _validated(f),
        patch(f"{_MOD}.soffice_path", return_value="/usr/bin/soffice"),
        patch(f"{_MOD}._run_soffice", _boom),
    ):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-office-slides?path={f}")
            assert resp.status == 502
            body = await resp.json()
    assert body["code"] == "convert_failed"
    assert not list(cache.iterdir())


@_PINNED
@pytest.mark.asyncio
async def test_the_staged_copy_is_what_the_child_converts(tmp_path, mock_sel, cache):
    """soffice is handed a copy in the work dir, never the user's path.

    The copy is the bytes the prefix opened, fstat-gated and hashed, so what is
    converted is exactly what was measured; and the child's only input lives in
    the one directory it needs to write, not in the user's tree.
    """
    f = tmp_path / "deck.pptx"
    _write_pptx(f)
    seen: dict[str, object] = {}

    async def _capture(argv, work):
        seen["argv"] = list(argv)
        seen["work"] = Path(work)
        seen["staged"] = (Path(work) / "deck.pptx").read_bytes()
        _write_pdf(Path(work) / "out" / "deck.pdf", 1)

    with (
        _validated(f),
        patch(f"{_MOD}.soffice_path", return_value="/opt/lo/program/soffice"),
        patch(f"{_MOD}._run_soffice", _capture),
    ):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-office-slides?path={f}")
            assert resp.status == 200
    argv = seen["argv"]
    assert isinstance(argv, list)
    assert argv[0] == "/opt/lo/program/soffice"
    assert "--headless" in argv and "--convert-to" in argv and "pdf" in argv
    assert str(f) not in argv, "the user's path must not reach the child"
    assert argv[-1] == str(seen["work"] / "deck.pptx")
    assert seen["staged"] == f.read_bytes()
    assert any(a.startswith("-env:UserInstallation=file://") for a in argv)


@pytest.mark.asyncio
async def test_spawn_routes_through_the_strict_sandbox_chokepoint(tmp_path):
    """``_run_soffice`` prepares the argv with ``sandboxed_spawn_argv(..., "strict")``.

    The input is a user-supplied deck and LibreOffice reads whatever a deck
    links to, so the child must get the credential-scrubbed environment and the
    hidden trust root. Pinned on the chokepoint call itself: a refactor that
    spawned ``argv`` directly would still convert decks and pass every other
    test here.
    """
    work = tmp_path / "work"
    (work / "out").mkdir(parents=True)
    captured: dict[str, object] = {}

    async def _prepare(partial_fn, executor=None):  # noqa: ARG001
        assert isinstance(partial_fn, functools.partial)
        captured["fn"] = partial_fn.func
        captured["args"] = partial_fn.args
        captured["kwargs"] = partial_fn.keywords
        return list(partial_fn.args[0]), dict(partial_fn.keywords["env"]), None

    proc = MagicMock()
    proc.stdout = None
    proc.stderr = None
    proc.returncode = 0
    proc.wait = AsyncMock(return_value=0)
    argv = mod._soffice_argv("/usr/bin/soffice", work / "profile", work / "out", work / "deck.pptx")
    with (
        patch(f"{_MOD}.shielded_prepare_off_loop", _prepare),
        patch(f"{_MOD}.create_subprocess_limited", AsyncMock(return_value=proc)) as spawn,
    ):
        await mod._run_soffice(argv, work)
    assert captured["fn"] is mod.sandboxed_spawn_argv
    assert captured["args"][1] == "strict"
    env = captured["kwargs"]["env"]
    # Minimal environment: the child's temp files land in the work dir, and no
    # DISPLAY reaches a headless soffice.
    assert env["TMPDIR"] == str(work)
    assert "DISPLAY" not in env
    assert captured["kwargs"]["extra_hidden_dirs"], "the trust root must be hidden from the child"
    spawned = spawn.await_args
    assert list(spawned.args) == argv
    assert spawned.kwargs["cwd"] == str(work)


def test_soffice_argv_is_a_list_with_a_private_profile(tmp_path):
    argv = mod._soffice_argv(
        "/usr/bin/soffice", tmp_path / "p", tmp_path / "o", tmp_path / "deck.pptx"
    )
    assert argv[0] == "/usr/bin/soffice"
    assert argv[1].startswith("-env:UserInstallation=file://")
    assert (tmp_path / "p").resolve().as_uri() in argv[1]
    assert argv[-1] == str(tmp_path / "deck.pptx")
    assert "--outdir" in argv and argv[argv.index("--outdir") + 1] == str(tmp_path / "o")
    # Flags are separate elements, never a pre-joined shell string.
    assert argv.index("--convert-to") + 1 == argv.index("pdf")
    assert "--norestore" in argv and "--nologo" in argv


# --- serving a slide -------------------------------------------------------------


@pytest.mark.asyncio
async def test_slide_endpoint_serves_the_cached_png_for_the_authorized_path(
    tmp_path, mock_sel, cache
):
    f = tmp_path / "deck.pptx"
    digest = _write_pptx(f)
    _seed_cache(cache, digest, 2)
    with _validated(f):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-office-slide?path={f}&n=2&digest={digest}")
            assert resp.status == 200
            assert resp.headers["Content-Type"] == "image/png"
            assert resp.headers["X-Content-Type-Options"] == "nosniff"
            # Cacheable ONLY because the URL is pinned to the content digest.
            assert resp.headers["Cache-Control"] == "private, max-age=3600"
            data = await resp.read()
            bare = await client.get(f"/api/file-office-slide?path={f}&n=2")
            assert bare.status == 200
            assert bare.headers["Cache-Control"] == "no-store"
            # A digest from before an edit is refused rather than answered from
            # either version -- and never from the browser cache, since the URL
            # changed with it.
            stale = await client.get(f"/api/file-office-slide?path={f}&n=2&digest={'0' * 64}")
            assert stale.status == 409
            assert (await stale.json())["code"] == "stale_digest"
            assert (
                await client.get(f"/api/file-office-slide?path={f}&n=2&digest=zz")
            ).status == 400
            # Out of range and malformed slide numbers are refused, not clamped.
            assert (await client.get(f"/api/file-office-slide?path={f}&n=3")).status == 404
            assert (await client.get(f"/api/file-office-slide?path={f}&n=0")).status == 404
            assert (await client.get(f"/api/file-office-slide?path={f}&n=two")).status == 400
    assert data == b"\x89PNG-fake-\x02"


# --- the cache is not a trust root ------------------------------------------


@pytest.mark.asyncio
async def test_an_unsigned_or_forged_manifest_is_a_cache_miss(tmp_path, mock_sel, cache):
    """A same-uid agent can write under the data home; a manifest it plants must
    never be served -- neither as a manifest nor as authority for slide bytes."""
    f = tmp_path / "deck.pptx"
    digest = _write_pptx(f)
    _seed_cache(cache, digest, 1, signed=False)
    with _validated(f), patch(f"{_MOD}.soffice_path", return_value=None):
        async with TestClient(TestServer(_make_app())) as client:
            manifest = await client.get(f"/api/file-office-slides?path={f}")
            assert (await manifest.json())["status"] == "unavailable"  # a MISS, then no soffice
            slide = await client.get(f"/api/file-office-slide?path={f}&n=1")
            assert slide.status == 404
            assert (await slide.json())["code"] == "not_rendered"
    # Tampering with one signed field also breaks the signature.
    _seed_cache(cache, digest, 1)
    path = cache / digest / "manifest.json"
    forged = json.loads(path.read_text())
    forged["count"] = 99
    path.write_text(json.dumps(forged))
    assert mod.read_manifest(digest) is None


@pytest.mark.asyncio
async def test_a_slide_file_that_is_a_symlink_or_altered_is_refused_and_audited(
    tmp_path, mock_sel, cache
):
    """The signed manifest vouches for slide BYTES: a link planted at the slide's
    name, or a file whose bytes do not hash to the record, is never served."""
    f = tmp_path / "deck.pptx"
    digest = _write_pptx(f)
    _seed_cache(cache, digest, 2)
    secret = tmp_path / "credentials"
    secret.write_bytes(b"AKIA-not-for-you")
    (cache / digest / "slide-1.png").unlink()
    (cache / digest / "slide-1.png").symlink_to(secret)
    (cache / digest / "slide-2.png").write_bytes(b"\x89PNG-replaced")
    with _validated(f):
        async with TestClient(TestServer(_make_app())) as client:
            linked = await client.get(f"/api/file-office-slide?path={f}&n=1")
            linked_body = await linked.read()
            altered = await client.get(f"/api/file-office-slide?path={f}&n=2")
    assert linked.status == 404 and altered.status == 404
    assert b"AKIA" not in linked_body
    integrity = [
        c
        for c in mock_sel.log_tool_invocation.call_args_list
        if c.kwargs.get("outcome") == "denied" and c.kwargs.get("error") == "cache_integrity"
    ]
    assert len(integrity) == 2, "each refused slide read must be SEL-audited"


@pytest.mark.asyncio
async def test_without_a_trust_root_the_cache_is_inert(tmp_path, mock_sel, cache):
    f = tmp_path / "deck.pptx"
    digest = _write_pptx(f)
    _seed_cache(cache, digest, 1)
    with (
        _validated(f),
        patch("kiro_crew.dashboard.token_secret._get_secret", side_effect=OSError("unwritable")),
        patch(f"{_MOD}.soffice_path", return_value="/usr/bin/soffice"),
        patch(f"{_MOD}._run_soffice", AsyncMock()) as soffice,
    ):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-office-slides?path={f}")
            assert resp.status == 503
            assert (await resp.json())["code"] == "cache_unsigned"
            assert (await client.get(f"/api/file-office-slide?path={f}&n=1")).status == 404
    # Neither read from the seeded cache nor rendered into it.
    assert soffice.await_count == 0


@_PINNED
def test_a_planted_symlink_at_the_deck_directory_is_replaced_not_followed(tmp_path, cache):
    digest = "c" * 64
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    (decoy / "keep-me").write_text("victim tree")
    cache.mkdir(parents=True)
    (cache / digest).symlink_to(decoy)
    work = cache / ".work-test"
    work.mkdir()
    staging = mod._Staging(work)
    with staging.create("slide-1.png") as out:
        out.write(b"\x89PNG-real")
    hashes = {"slide-1.png": hashlib.sha256(b"\x89PNG-real").hexdigest()}
    manifest = mod._finish(
        staging,
        digest,
        [{"n": 1, "width": 1, "height": 1}],
        False,
        ".pptx",
        mod._signing_key(),
        hashes,
    )
    assert manifest["digest"] == digest
    assert not (cache / digest).is_symlink()
    assert (cache / digest / "slide-1.png").read_bytes() == b"\x89PNG-real"
    # The link was removed; what it pointed at was never touched.
    assert (decoy / "keep-me").read_text() == "victim tree"


@_PINNED
@pytest.mark.asyncio
async def test_a_deck_whose_slides_show_a_credential_is_not_rendered(tmp_path, mock_sel, cache):
    """A slide PNG is a picture of the deck's text and cannot be redacted after
    the fact, so the same screens the download path applies gate the render:
    credential-like text on any page means no slides (the redacted text outline
    is the only preview), nothing published, and a SEL denial."""
    f = tmp_path / "deck.pptx"
    digest = _write_pptx(f)
    soffice = AsyncMock(side_effect=_fake_soffice(2))
    with (
        _validated(f),
        patch(f"{_MOD}.soffice_path", return_value="/usr/bin/soffice"),
        patch(f"{_MOD}._run_soffice", soffice),
        patch(f"{_MOD}._deck_text", return_value="rotate me: AKIAIOSFODNN7EXAMPLE"),
    ):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-office-slides?path={f}")
            assert resp.status == 200
            body = await resp.json()
            assert body == {"status": "unavailable", "reason": "content_redacted"}
            assert (await client.get(f"/api/file-office-slide?path={f}&n=1")).status == 404
    assert not (cache / digest).exists(), "a refused deck publishes nothing"
    assert not list(cache.glob(".work-*")), "the staging dir is discarded"
    denied = [
        c
        for c in mock_sel.log_tool_invocation.call_args_list
        if c.kwargs.get("outcome") == "denied" and c.kwargs.get("error") == "content_redacted"
    ]
    assert len(denied) == 1


@pytest.mark.asyncio
async def test_a_served_slide_is_audited_as_a_successful_invocation(tmp_path, mock_sel, cache):
    f = tmp_path / "deck.pptx"
    digest = _write_pptx(f)
    _seed_cache(cache, digest, 1)
    with _validated(f):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-office-slide?path={f}&n=1&digest={digest}")
            assert resp.status == 200
    successes = [
        c
        for c in mock_sel.log_tool_invocation.call_args_list
        if c.kwargs.get("outcome") == "success" and c.kwargs.get("tool_name") == mod._TOOL_NAME
    ]
    assert len(successes) == 1
    assert successes[0].kwargs.get("resources") == str(f)


@pytest.mark.asyncio
async def test_without_descriptor_relative_operations_rendering_fails_closed(
    tmp_path, mock_sel, cache
):
    """A platform with no ``dir_fd`` operations cannot pin the staging tree, so it does
    not render: the manifest says so, nothing is staged, the refusal is audited, and
    the panel falls back to the text outline."""
    f = tmp_path / "deck.pptx"
    _write_pptx(f)
    soffice = AsyncMock()
    with (
        _validated(f),
        patch(f"{_MOD}._DIR_FD_OPS", False),
        patch(f"{_MOD}.soffice_path", return_value="/usr/bin/soffice"),
        patch(f"{_MOD}._run_soffice", soffice),
    ):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-office-slides?path={f}")
            assert resp.status == 200
            assert await resp.json() == {"status": "unavailable", "reason": "platform_unsupported"}
    assert soffice.await_count == 0
    assert not cache.exists() or not list(cache.iterdir())
    denied = [
        c
        for c in mock_sel.log_tool_invocation.call_args_list
        if c.kwargs.get("outcome") == "denied" and c.kwargs.get("error") == "platform_unsupported"
    ]
    assert len(denied) == 1


@_PINNED
def test_a_cache_root_swapped_for_a_link_is_refused_not_followed(tmp_path, cache):
    """``_open_cache_root`` reaches the cache root without following a link at
    either name below the data home: a link planted at the root is refused, so
    nothing is ever staged or published into the tree it points at."""
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.symlink_to(decoy)
    with pytest.raises(mod._ConvertFailed):
        mod._open_cache_root()
    assert list(decoy.iterdir()) == []


@_PINNED
def test_a_staging_directory_swapped_for_a_link_is_never_published_or_deleted_through(
    tmp_path, cache
):
    """The staging dir is pinned by descriptor: swapping its NAME for a link mid-render
    changes nothing the renderer writes or deletes, and the publish step refuses to
    put a link where a deck belongs -- the link's target is never touched."""
    cache.mkdir(parents=True)
    work = cache / ".work-swap"
    work.mkdir()
    staging = mod._Staging(work)
    staging.mkdir("out")
    with staging.create("slide-1.png") as out:
        out.write(b"\x89PNG-real")
    hashes = {"slide-1.png": hashlib.sha256(b"\x89PNG-real").hexdigest()}
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    (decoy / "victim").write_text("keep")
    (decoy / "out").mkdir()
    # The swap: the real directory moves aside, a link to the decoy takes its name.
    work.rename(cache / ".work-aside")
    work.symlink_to(decoy)
    digest = "d" * 64
    with pytest.raises(mod._ConvertFailed):
        mod._finish(
            staging,
            digest,
            [{"n": 1, "width": 1, "height": 1}],
            False,
            ".pptx",
            mod._signing_key(),
            hashes,
        )
    # Nothing at the decoy was deleted or written, and no deck was published.
    assert (decoy / "victim").read_text() == "keep"
    assert not (decoy / "manifest.json").exists()
    assert not (cache / digest).exists()
    # The intermediates cleanup and the manifest went to the REAL directory.
    assert (cache / ".work-aside" / "manifest.json").exists()
    assert not (cache / ".work-aside" / "out").exists()
    staging.close()


@pytest.mark.asyncio
async def test_slide_endpoint_answers_404_for_a_deck_that_was_never_rendered(
    tmp_path, mock_sel, cache
):
    f = tmp_path / "deck.pptx"
    _write_pptx(f)
    with _validated(f):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-office-slide?path={f}&n=1")
            assert resp.status == 404
            body = await resp.json()
    assert body["code"] == "not_rendered"


@pytest.mark.asyncio
async def test_an_edited_deck_misses_the_cache(tmp_path, mock_sel, cache):
    """The key is the content: same path, new bytes, new digest, no stale slides."""
    f = tmp_path / "deck.pptx"
    old_digest = _write_pptx(f, slides=1)
    _seed_cache(cache, old_digest, 1)
    with _validated(f):
        async with TestClient(TestServer(_make_app())) as client:
            hit = await client.get(f"/api/file-office-slides?path={f}")
            assert (await hit.json())["digest"] == old_digest
            new_digest = _write_pptx(f, slides=2)
            assert new_digest != old_digest
            with patch(f"{_MOD}.soffice_path", return_value=None):
                miss = await client.get(f"/api/file-office-slides?path={f}")
                body = await miss.json()
    assert (
        body["status"] == "unavailable"
    )  # a miss with no soffice: honest degrade, not the old render


# --- the shared security envelope -------------------------------------------------


@pytest.mark.asyncio
async def test_unsupported_extension_is_415(tmp_path, mock_sel, cache):
    f = tmp_path / "report.docx"
    f.write_bytes(b"PK\x03\x04 not a deck")
    with _validated(f):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-office-slides?path={f}")
            assert resp.status == 415
            assert (await resp.json())["code"] == "unsupported_slide_format"
            resp2 = await client.get(f"/api/file-office-slide?path={f}&n=1")
            assert resp2.status == 415


@pytest.mark.asyncio
async def test_oversized_deck_is_413_before_hashing_or_converting(tmp_path, mock_sel, cache):
    f = tmp_path / "huge.pptx"
    _write_pptx(f)
    os.truncate(str(f), 51 * 1024 * 1024)  # sparse: st_size > cap, no disk cost
    with _validated(f), patch(f"{_MOD}._run_soffice", AsyncMock()) as soffice:
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-office-slides?path={f}")
            assert resp.status == 413
            assert (await resp.json())["code"] == "file_too_large"
    assert soffice.await_count == 0


@pytest.mark.asyncio
async def test_sensitive_path_is_403(tmp_path, mock_sel, cache):
    f = tmp_path / "deck.pptx"
    _write_pptx(f)
    with (
        _validated(f),
        patch("kiro_crew.dashboard.handlers.files.is_sensitive_path", return_value=True),
    ):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-office-slides?path={f}")
            assert resp.status == 403
            assert (await resp.json())["code"] == "sensitive_path"


@pytest.mark.asyncio
async def test_forbidden_path_is_400(mock_sel, cache):
    with patch("kiro_crew.dashboard.handlers._validate_dashboard_path", return_value=None):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get("/api/file-office-slides?path=/etc/passwd.pptx")
            assert resp.status == 400
            assert (await resp.json())["code"] == "forbidden_path"


# --- eviction -----------------------------------------------------------------


def test_eviction_drops_the_least_recently_used_decks_and_spares_the_one_just_rendered(cache):
    digests = [hashlib.sha256(bytes([i])).hexdigest() for i in range(3)]
    for i, d in enumerate(digests):
        _seed_cache(cache, d, 1)
        # Seeded oldest-first; the byte payloads are equal so order decides.
        os.utime(cache / d, (1_000 + i, 1_000 + i))
    size_each = sum(p.stat().st_size for p in (cache / digests[0]).iterdir())
    removed = mod.evict_to_budget(budget=size_each * 2, keep=digests[0])
    # Two fit; the oldest would go first, but it is the deck being kept, so the
    # next-oldest goes instead.
    assert removed == size_each
    assert (cache / digests[0]).exists()
    assert not (cache / digests[1]).exists()
    assert (cache / digests[2]).exists()


def test_eviction_sweeps_an_abandoned_work_dir(cache):
    stale = cache / ".work-abandoned"
    stale.mkdir(parents=True)
    (stale / "deck.pptx").write_bytes(b"x" * 10)
    long_ago = 1_000
    os.utime(stale, (long_ago, long_ago))
    mod.evict_to_budget(budget=10**9)
    assert not stale.exists()


def test_read_manifest_rejects_a_manifest_for_another_digest(cache):
    digest = "a" * 64
    _seed_cache(cache, digest, 1)
    other = cache / ("b" * 64)
    other.mkdir()
    (other / "manifest.json").write_text(
        json.dumps(mod._sign({"digest": digest, "count": 1, "slides": []}, mod._signing_key()))
    )
    assert mod.read_manifest(digest) is not None
    # Correctly signed, but for another digest: a miss.
    assert mod.read_manifest("b" * 64) is None
