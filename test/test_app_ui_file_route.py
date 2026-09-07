"""Serving an app's UI bundle through a pinned descriptor (#6809).

``/apps/{name}/ui/{path}`` serves the SAME app-owned directory the art route
serves, and until #6809 it kept the validate-then-``FileResponse`` shape #6794
removed from that sibling: validating a path and then handing it to
``FileResponse`` opens it a SECOND time, so the app that owns the directory can
swap a validated name for a symlink between the check and that open and have
the unsandboxed gateway read the target on its behalf. Worse than parity: this
route's extension allowlist admits ``.json`` and ``.mjs``, so the route with
the weaker open had the broader reach.

The suite mirrors ``test_app_art_route.py`` (the #6794 shape) with this
route's own behaviour contract: 400 on ``..``/absolute/escaping paths, 403 on
a disallowed extension, 404 on anything else unservable, Content-Type from the
extension map, and body-less 304s for conditional requests.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from conftest import make_dir_link, requires_symlinks
from kiro_crew.apps import dev_mode
from kiro_crew.apps import routes as app_routes

APP = "demo-app"


def _make_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/apps/{name}/ui/{path:.*}", app_routes.handle_app_ui_file)
    return app


@pytest.fixture()
def ui_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An installed app carrying one real UI asset per allowed extension."""
    root = tmp_path / "apps"
    ui = root / APP / "ui"
    (ui / "chunks").mkdir(parents=True)
    for ext in sorted(app_routes._ALLOWED_EXTENSIONS):
        (ui / f"asset{ext}").write_bytes(f"bytes-for-{ext}".encode())
    (ui / "chunks" / "lazy.mjs").write_bytes(b"chunk-bytes")
    monkeypatch.setattr(app_routes, "apps_dir", lambda: root)
    return ui


async def _get(path: str, **kw: object) -> tuple[int, bytes]:
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.get(path, **kw)  # type: ignore[arg-type]
        return resp.status, await resp.read()


@pytest.mark.asyncio
@pytest.mark.parametrize("ext", sorted(app_routes._ALLOWED_EXTENSIONS))
async def test_each_allowed_extension_serves_with_its_content_type(ui_root: Path, ext: str) -> None:
    """The happy path, per extension: the pinned open must keep ADMITTING every
    file the old open admitted, with the same Content-Type."""
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.get(f"/apps/{APP}/ui/asset{ext}")
        assert resp.status == 200
        assert await resp.read() == f"bytes-for-{ext}".encode()
        assert resp.headers["Content-Type"] == app_routes._CONTENT_TYPES[ext]
        assert resp.headers["Cache-Control"] == "no-cache"


@pytest.mark.asyncio
async def test_a_nested_path_is_served(ui_root: Path) -> None:
    """Vite code-splits into ``chunks/``, so multi-component paths are the
    normal case, not an edge."""
    status, body = await _get(f"/apps/{APP}/ui/chunks/lazy.mjs")
    assert status == 200
    assert body == b"chunk-bytes"


@requires_symlinks
@pytest.mark.asyncio
async def test_a_symlink_at_the_final_NAME_is_refused_even_inside_the_root(
    ui_root: Path,
) -> None:
    """The property that closes the check-to-use swap.

    A link pointing at a perfectly legitimate file INSIDE the root is refused
    too: it is the indirection that is refused, not the destination, because
    only the indirection is swappable. The old code served this with a 200 —
    the resolved path sat inside the root, so containment passed, and
    ``FileResponse`` followed the link on its second open.
    """
    real = ui_root / "asset.js"
    assert real.is_file(), "fixture precondition: the link's target is a real asset"
    link = ui_root / "aliased.js"
    link.symlink_to("asset.js")  # inside the root, and a real file
    status, body = await _get(f"/apps/{APP}/ui/aliased.js")
    assert status == 404
    assert b"bytes-for-.js" not in body, "the link's target must not be served"


@requires_symlinks
@pytest.mark.asyncio
async def test_a_mutual_symlink_pair_answers_404(ui_root: Path) -> None:
    """The two-file loop form, which a single-file guard can miss."""
    a = ui_root / "a.css"
    b = ui_root / "b.css"
    a.symlink_to("b.css")
    b.symlink_to("a.css")
    status, _ = await _get(f"/apps/{APP}/ui/a.css")
    assert status == 404


@requires_symlinks
@pytest.mark.asyncio
async def test_an_escaping_symlink_at_the_final_name_keeps_answering_400(
    ui_root: Path, tmp_path: Path
) -> None:
    """The historic contract: an escape answers 400 ``invalid path``. Pinned
    open or not, the status must not drift under the refactor."""
    outside = tmp_path / "outside.json"
    outside.write_text('{"secret": true}', encoding="utf-8")
    (ui_root / "escape.json").symlink_to(outside)
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.get(f"/apps/{APP}/ui/escape.json")
        assert resp.status == 400
        assert (await resp.json())["error"] == "invalid path"
        assert b"secret" not in await resp.read()


@pytest.mark.asyncio
async def test_a_symlinked_ANCESTOR_is_refused(ui_root: Path, tmp_path: Path) -> None:
    """What the caller-side containment check is still for, once the pinned
    open exists.

    ``O_NOFOLLOW`` refuses a link at the final NAME, and the handler's own
    guard refuses ``..`` — so the one route left into the containment check is
    an ANCESTOR that is a link. ``pin_parent`` deliberately does not close that
    case (its contract: a component swapped BEFORE the parent was resolved is
    followed by that resolution), so resolving the parent and proving it lands
    under the resolved root is load-bearing — a genuine #6794 coverage gap
    until a mutation surfaced it. ``make_dir_link`` so Windows gets a junction
    and the branch without the pinned walk is covered by the same test.
    """
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "app.js").write_bytes(b"outside-bytes")
    make_dir_link(ui_root / "vendor", outside)
    status, body = await _get(f"/apps/{APP}/ui/vendor/app.js")
    assert status == 400
    assert b"outside-bytes" not in body


@pytest.mark.asyncio
async def test_a_LINKED_ui_root_still_serves_under_the_dev_grant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The legitimate link the hardening must NOT refuse.

    The documented dev-mode setup links ``ui/`` at the developer's source tree
    (junction on Windows). The root is resolved BEFORE containment, so the walk
    and the check both happen where the link really lands — and the escape from
    the install directory is allowed exactly because the OPERATOR granted dev
    mode: a grant record (written only by the dev-mode toggle, never created
    by the startup reconcile) BOUND to this resolved root, plus the metadata
    flag — the grant half exercised for real here.
    """
    root = tmp_path / "apps"
    (root / APP).mkdir(parents=True)
    real = tmp_path / "source-ui"
    real.mkdir()
    (real / "index.mjs").write_bytes(b"dev-bytes")
    make_dir_link(root / APP / "ui", real)
    monkeypatch.setattr(app_routes, "apps_dir", lambda: root)
    monkeypatch.setattr(dev_mode, "apps_dir", lambda: root)
    # The operator's half of the grant, bound to the root they approved.
    dev_mode._write_dev_grants({APP: os.path.realpath(real)})
    monkeypatch.setattr(dev_mode, "is_dev_mode", lambda name: name == APP)
    status, body = await _get(f"/apps/{APP}/ui/index.mjs")
    assert status == 200
    assert body == b"dev-bytes"


@pytest.mark.asyncio
async def test_a_linked_ui_root_WITHOUT_the_grant_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The vector the install-containment check closes: an app SHIPS ``ui`` as
    a link to a credential directory (a git install preserves symlinks), and
    this route bypasses token auth — so without the check, an unauthenticated
    request serves ``config.json`` out of it (``.json`` is in the allowlist),
    laundering a read the app's own sandboxed code is refused."""
    root = tmp_path / "apps"
    (root / APP).mkdir(parents=True)
    outside = tmp_path / "not-the-install"
    outside.mkdir()
    (outside / "config.json").write_bytes(b'{"auths": "CREDENTIAL-BYTES"}')
    make_dir_link(root / APP / "ui", outside)
    monkeypatch.setattr(app_routes, "apps_dir", lambda: root)
    monkeypatch.setattr(dev_mode, "apps_dir", lambda: root)  # no sentinel exists
    status, body = await _get(f"/apps/{APP}/ui/config.json")
    assert status == 400
    assert b"CREDENTIAL-BYTES" not in body


@pytest.mark.asyncio
async def test_a_SELF_GRANTED_dev_flag_does_not_authorize_the_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Why the grant is a separate operator record, not metadata-backed.

    ``installed.json`` lives inside the install directory the APP ITSELF can
    write, so an app that edits it to ``dev: true`` must not thereby authorize
    its own out-of-install ui root — the grant record at the apps ROOT, which
    only the owner's dashboard toggle writes, is the half it cannot forge.
    ``is_dev_mode`` is forced True here (the forged metadata); with no grant
    record the check must still refuse.
    """
    root = tmp_path / "apps"
    (root / APP).mkdir(parents=True)
    outside = tmp_path / "not-the-install"
    outside.mkdir()
    (outside / "config.json").write_bytes(b'{"auths": "CREDENTIAL-BYTES"}')
    make_dir_link(root / APP / "ui", outside)
    monkeypatch.setattr(app_routes, "apps_dir", lambda: root)
    monkeypatch.setattr(dev_mode, "apps_dir", lambda: root)
    monkeypatch.setattr(dev_mode, "is_dev_mode", lambda name: True)  # forged
    status, body = await _get(f"/apps/{APP}/ui/config.json")
    assert status == 400
    assert b"CREDENTIAL-BYTES" not in body


@pytest.mark.asyncio
async def test_the_reconciled_sentinel_alone_does_not_authorize_the_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The restart-laundering vector: sentinel != grant.

    The startup reconcile rebuilds the WATCH sentinel from each app's own
    (app-writable) ``installed.json`` — so across a restart, a forged
    ``dev: true`` DOES put the app's name in the sentinel. That must buy
    watching and no-store serving at most: the authorization half is the
    separate grant record only the operator toggle writes. Sentinel present +
    metadata forged + no grant record → the out-of-install ui root is still
    refused.
    """
    root = tmp_path / "apps"
    (root / APP).mkdir(parents=True)
    outside = tmp_path / "not-the-install"
    outside.mkdir()
    (outside / "config.json").write_bytes(b'{"auths": "CREDENTIAL-BYTES"}')
    make_dir_link(root / APP / "ui", outside)
    monkeypatch.setattr(app_routes, "apps_dir", lambda: root)
    monkeypatch.setattr(dev_mode, "apps_dir", lambda: root)
    dev_mode._write_dev_sentinel({APP})  # what the restart reconcile rebuilds
    monkeypatch.setattr(dev_mode, "is_dev_mode", lambda name: True)  # forged
    status, body = await _get(f"/apps/{APP}/ui/config.json")
    assert status == 400
    assert b"CREDENTIAL-BYTES" not in body


@pytest.mark.asyncio
async def test_a_symlinked_INSTALL_ENTRY_cannot_vouch_for_its_own_containment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The containment anchor is the resolved apps ROOT + the literal name.

    Re-resolving through the app's own install entry would let that entry
    vouch for itself: an install entry that IS a link to an outside tree made
    `realpath(apps_dir()/name)` land in that same outside tree, so the
    escaping ui root read as "inside the install" and served with no grant —
    and the two independent realpath calls could be raced by swapping the
    entry between them. With the literal-name anchor the resolved root lands
    outside and takes the grant path like any other escape: refused without
    an operator grant.
    """
    root = tmp_path / "apps"
    root.mkdir()
    outside = tmp_path / "not-the-install"
    (outside / "ui").mkdir(parents=True)
    (outside / "ui" / "config.json").write_bytes(b'{"auths": "CREDENTIAL-BYTES"}')
    # The app's install-dir ENTRY is itself a link to the outside tree.
    make_dir_link(root / APP, outside)
    monkeypatch.setattr(app_routes, "apps_dir", lambda: root)
    monkeypatch.setattr(dev_mode, "apps_dir", lambda: root)
    status, body = await _get(f"/apps/{APP}/ui/config.json")
    assert status == 400
    assert b"CREDENTIAL-BYTES" not in body


@pytest.mark.asyncio
async def test_a_grant_bound_to_a_DIFFERENT_root_does_not_authorize_the_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The grant covers ONE resolved root — the one the operator approved.

    A stale or inherited grant (crash mid-revoke, an app update that repoints
    ``ui``, a reinstall under the same name) must authorize only the exact
    tree bound at toggle time: the current resolved root must EQUAL the
    granted root. Here the operator granted a legitimate source tree, then
    ``ui`` was repointed at a credential directory — refused, credential
    bytes never leaked.
    """
    root = tmp_path / "apps"
    (root / APP).mkdir(parents=True)
    approved = tmp_path / "approved-source"
    approved.mkdir()
    outside = tmp_path / "not-the-install"
    outside.mkdir()
    (outside / "config.json").write_bytes(b'{"auths": "CREDENTIAL-BYTES"}')
    # ui points at the credential dir, but the grant binds the approved tree.
    make_dir_link(root / APP / "ui", outside)
    monkeypatch.setattr(app_routes, "apps_dir", lambda: root)
    monkeypatch.setattr(dev_mode, "apps_dir", lambda: root)
    dev_mode._write_dev_grants({APP: os.path.realpath(approved)})
    monkeypatch.setattr(dev_mode, "is_dev_mode", lambda name: True)
    status, body = await _get(f"/apps/{APP}/ui/config.json")
    assert status == 400
    assert b"CREDENTIAL-BYTES" not in body


@pytest.mark.asyncio
async def test_the_windows_branch_validates_the_OPENED_descriptor_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The name-based reparse probe leaves a probe-to-open window; the
    descriptor's own final path is the race-free witness. Simulate a swap that
    beat the probe: the opened descriptor's real path resolves OUTSIDE the
    root — refused, even though every name-based check passed."""
    root = tmp_path / "apps"
    ui = root / APP / "ui"
    ui.mkdir(parents=True)
    (ui / "app.js").write_bytes(b"const x = 1\n")
    monkeypatch.setattr(app_routes, "apps_dir", lambda: root)
    monkeypatch.setattr(app_routes, "supports_pinned_walk", lambda: False)
    monkeypatch.setattr(
        app_routes, "fd_real_path", lambda fd: str(tmp_path / "elsewhere" / "app.js")
    )
    status, body = await _get(f"/apps/{APP}/ui/app.js")
    assert status == 404
    assert b"const x" not in body


@pytest.mark.asyncio
async def test_the_windows_branch_fails_closed_when_the_fd_path_is_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On the no-pinned-walk branch the descriptor is the only trustworthy
    witness: if its final path cannot be read, refuse rather than serve."""
    root = tmp_path / "apps"
    ui = root / APP / "ui"
    ui.mkdir(parents=True)
    (ui / "app.js").write_bytes(b"const x = 1\n")
    monkeypatch.setattr(app_routes, "apps_dir", lambda: root)
    monkeypatch.setattr(app_routes, "supports_pinned_walk", lambda: False)
    monkeypatch.setattr(app_routes, "fd_real_path", lambda fd: None)
    status, _body = await _get(f"/apps/{APP}/ui/app.js")
    assert status == 404


@pytest.mark.asyncio
async def test_an_unchanged_file_revalidates_to_a_bodyless_304(ui_root: Path) -> None:
    """Buffered bytes must not cost a full 200 per load: ``no-cache`` means the
    browser revalidates every time, so the validator — derived from the
    DESCRIPTOR the bytes were read from, not a second stat — answers
    If-None-Match with a body-less 304."""
    async with TestClient(TestServer(_make_app())) as client:
        first = await client.get(f"/apps/{APP}/ui/asset.js")
        assert first.status == 200
        etag = first.headers["ETag"]
        assert etag
        again = await client.get(f"/apps/{APP}/ui/asset.js", headers={"If-None-Match": etag})
        assert again.status == 304
        assert await again.read() == b"", "a 304 carries no body"


@pytest.mark.asyncio
async def test_if_modified_since_still_revalidates_to_304(ui_root: Path) -> None:
    """``FileResponse`` answered If-Modified-Since, so clients that revalidate
    by date (no ETag support) must keep getting their 304 after the refactor."""
    async with TestClient(TestServer(_make_app())) as client:
        first = await client.get(f"/apps/{APP}/ui/asset.js")
        assert first.status == 200
        last_modified = first.headers["Last-Modified"]
        assert last_modified
        again = await client.get(
            f"/apps/{APP}/ui/asset.js", headers={"If-Modified-Since": last_modified}
        )
        assert again.status == 304
        assert await again.read() == b""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "header_fmt",
    [
        'W/"{etag}"',  # RFC 7232 §3.2: If-None-Match uses the WEAK comparison
        '"unrelated", "{etag}"',  # a list containing the current tag
        "*",  # any current representation
    ],
)
async def test_non_trivial_if_none_match_forms_still_revalidate(
    ui_root: Path, header_fmt: str
) -> None:
    """A raw string-equality check missed every one of these and re-sent the
    full body; the parsed accessor applies RFC 7232 semantics."""
    async with TestClient(TestServer(_make_app())) as client:
        first = await client.get(f"/apps/{APP}/ui/asset.js")
        assert first.status == 200
        etag = first.headers["ETag"].strip('"')
        again = await client.get(
            f"/apps/{APP}/ui/asset.js",
            headers={"If-None-Match": header_fmt.format(etag=etag)},
        )
        assert again.status == 304
        assert await again.read() == b""


@pytest.mark.asyncio
async def test_a_non_matching_etag_gets_the_full_body(ui_root: Path) -> None:
    """The mirror: revalidation must not 304 a changed representation."""
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.get(
            f"/apps/{APP}/ui/asset.js", headers={"If-None-Match": '"stale-tag"'}
        )
        assert resp.status == 200
        assert await resp.read() == b"bytes-for-.js"


@pytest.mark.asyncio
async def test_a_file_over_the_size_ceiling_is_refused(
    ui_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bytes are held rather than streamed, so without a cap an app could
    make the gateway buffer whatever it ships."""
    monkeypatch.setattr(app_routes, "_UI_MAX_BYTES", 64)
    (ui_root / "big.js").write_bytes(b"x" * 65)
    status, _ = await _get(f"/apps/{APP}/ui/big.js")
    assert status == 404, "over the ceiling is not servable, same answer as missing"


@pytest.mark.asyncio
async def test_a_file_at_exactly_the_ceiling_is_served(
    ui_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mirror: the ceiling is a bound, not an off-by-one."""
    monkeypatch.setattr(app_routes, "_UI_MAX_BYTES", 64)
    (ui_root / "exact.js").write_bytes(b"x" * 64)
    status, body = await _get(f"/apps/{APP}/ui/exact.js")
    assert status == 200
    assert body == b"x" * 64


@pytest.mark.asyncio
async def test_a_HARDLINK_alias_is_refused(ui_root: Path, tmp_path: Path) -> None:
    """The one alias ``O_NOFOLLOW`` cannot see: a hardlink shares its target's
    inode, so ``is_symlink()`` is False, ``realpath`` yields the alias's OWN
    name (containment passes), and there is no link for the pinned open to
    refuse. ``st_nlink`` on the DESCRIPTOR is the only signal."""
    outside = tmp_path / "not-ui-at-all"
    outside.write_bytes(b"SENSITIVE-BYTES")
    alias = ui_root / "aliased-asset.js"
    os.link(outside, alias)  # a HARDLINK, not a symlink
    assert not alias.is_symlink(), "precondition: no symlink guard can see this"
    assert alias.stat().st_nlink == 2, "precondition: the alias is a second name"
    status, body = await _get(f"/apps/{APP}/ui/aliased-asset.js")
    assert status == 404
    assert b"SENSITIVE-BYTES" not in body


@pytest.mark.asyncio
async def test_an_ordinary_single_link_file_is_still_served(ui_root: Path) -> None:
    """The mirror: the nlink gate must not refuse a normal asset."""
    assert (ui_root / "asset.js").stat().st_nlink == 1
    status, body = await _get(f"/apps/{APP}/ui/asset.js")
    assert status == 200
    assert body == b"bytes-for-.js"


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs are POSIX-only")
@pytest.mark.asyncio
async def test_a_FIFO_does_not_hang_the_request(ui_root: Path) -> None:
    """Why the flags carry ``O_NONBLOCK``: opening a FIFO blocks until a writer
    appears, and this handler runs inside ``asyncio.to_thread``, so a shipped
    FIFO would park a thread-pool worker forever. If this test ever hangs
    rather than failing, that IS the regression."""
    os.mkfifo(ui_root / "piped.js")
    status, _ = await asyncio.wait_for(_get(f"/apps/{APP}/ui/piped.js"), timeout=10)
    assert status == 404, "a FIFO is not a plain file, so it is not servable"


@pytest.mark.asyncio
async def test_a_DIRECTORY_with_an_allowed_extension_is_refused(ui_root: Path) -> None:
    """The other non-regular shape: a directory opens fine under ``O_RDONLY``,
    so ``S_ISREG`` on the descriptor is what refuses it."""
    (ui_root / "adir.js").mkdir()
    status, _ = await _get(f"/apps/{APP}/ui/adir.js")
    assert status == 404


def test_a_NUL_in_the_final_name_is_refused_at_the_resolver(ui_root: Path) -> None:
    """``os.open`` raises ``ValueError`` — never an OSError — for a name the OS
    cannot encode, and such a name survives the earlier checks (the extension
    allowlist reads the suffix AFTER the bad byte, and containment resolves the
    PARENT). Asserted at the resolver because a NUL does not survive the HTTP
    round trip."""
    assert app_routes._open_ui_file(APP, "bad\x00.js") in ("invalid", "not_found")


def test_the_windows_branch_also_refuses_an_unencodable_name(
    ui_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The no-pinned-walk branch needs its own coverage on a POSIX runner —
    ``supports_pinned_walk()`` is True here, so nothing above exercises it."""
    monkeypatch.setattr(app_routes, "supports_pinned_walk", lambda: False)
    assert app_routes._open_ui_file(APP, "bad\x00.js") in ("invalid", "not_found")


def test_the_windows_branch_still_serves_a_normal_file(
    ui_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """And it must still WORK — a branch that refuses everything would pass the
    test above while breaking the route on the platform it exists for."""
    monkeypatch.setattr(app_routes, "supports_pinned_walk", lambda: False)
    out = app_routes._open_ui_file(APP, "asset.js")
    assert isinstance(out, tuple)
    fd, st = out
    try:
        assert os.read(fd, st.st_size) == b"bytes-for-.js"
    finally:
        os.close(fd)


@pytest.mark.asyncio
async def test_a_multi_chunk_file_streams_complete_and_bounded(
    ui_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The OOM-class fix: the body is STREAMED from the validated descriptor,
    so per-request memory is one chunk, not the file. Forcing a tiny chunk
    proves the loop reassembles a multi-chunk body byte-for-byte with the
    right Content-Length."""
    monkeypatch.setattr(app_routes, "_UI_STREAM_CHUNK", 7)  # force many chunks
    payload = bytes(range(256)) * 13  # 3328 bytes, not chunk-aligned
    (ui_root / "big-ish.js").write_bytes(payload)
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.get(f"/apps/{APP}/ui/big-ish.js")
        assert resp.status == 200
        assert resp.headers["Content-Length"] == str(len(payload))
        assert await resp.read() == payload


@pytest.mark.asyncio
async def test_the_response_neuters_a_navigated_document(ui_root: Path) -> None:
    """The allowlist admits ``.svg`` and the Content-Type comes from the
    EXTENSION, so a top-level navigation to this URL must not become a scripted
    document on the dashboard's origin. Response CSP does not apply to
    subresource loads, so app module/style/img fetches are unaffected."""
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.get(f"/apps/{APP}/ui/asset.svg")
        assert resp.status == 200
        csp = resp.headers.get("Content-Security-Policy", "")
        assert "sandbox" in csp, csp
        assert "default-src 'none'" in csp, csp
        assert resp.headers.get("X-Content-Type-Options") == "nosniff"
