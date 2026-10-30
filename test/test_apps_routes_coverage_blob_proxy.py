"""Coverage tests for the git-blob proxy, ``GET /api/apps/blob``.

Pins the clone-URL resolution of the no-bundled-entry branch, the SSRF gate and the blob
cache, and the credential posture of the blob clone: which env and sandbox mode
``_fetch_git_blob`` picks, and how the handler decides ``owner_designated``.
``test_apps_routes_coverage.py`` holds install, registration and the app views, and
names the other route modules; the shared helpers live in
``apps_routes_coverage_helpers.py``.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer
from apps_routes_coverage_helpers import (
    _make_app,
    _setup_env,
)

import kiro_crew.apps.routes as routes_mod
from conftest import requires_symlinks
from kiro_crew.apps.routes import _is_safe_repo_identifier

# ---------------------------------------------------------------------------
# No-bundled-entry blob clone-URL resolution (inline URL-form check)
# ---------------------------------------------------------------------------


class TestNoEntryBlobCloneUrlResolution:
    """The no-bundled-entry blob branch resolves the clone URL by an inline
    in-memory URL-form check on the already-validated ``repo`` — NO registry
    read.

    ``_registry_git_url`` (a standalone resolver that re-consulted
    ``get_registry_app_by_repo``) has been DELETED.  The bundled entry lookup
    runs exactly once per request (``get_registry_app_by_repo`` at the top of
    ``handle_blob_proxy``); the no-entry branch then only decides whether the
    validated ``repo`` is itself a clone URL, a pure string-shape test.  That
    removes the second, event-loop-blocking registry read GPT 5.6 flagged and
    the URL-form / no-bundled-entry resolution boundary the old resolver's unit
    tests covered — both are now exercised through the handler here.
    """

    async def _clone_url_for(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        repo: str,
    ) -> tuple[int, str | None]:
        """Drive ``handle_blob_proxy`` for a no-bundled-entry ``repo`` and report
        the HTTP status and the ``git_url`` threaded into ``_fetch_git_blob`` (or
        ``None`` if the fetch was never reached)."""
        _setup_env(tmp_path, monkeypatch)
        # ``repo`` is admitted (known_registry_repos) but has NO bundled entry —
        # the external/federated branch.  Count the registry entry lookups so a
        # reintroduced second read on the clone path fails loudly.
        monkeypatch.setattr(routes_mod, "known_registry_repos", lambda: {repo})

        calls = {"n": 0}

        def _entry_lookup(r: str) -> None:
            calls["n"] += 1
            return None

        monkeypatch.setattr(routes_mod, "get_registry_app_by_repo", _entry_lookup)

        seen: dict[str, Any] = {}

        async def _record(
            repo: str,
            ref: str,
            file_path: str,
            cache_path: Path,
            git_url: str,
            *,
            owner_designated: bool = False,
        ) -> bool:
            seen["git_url"] = git_url
            seen["owner_designated"] = owner_designated
            return False

        monkeypatch.setattr(routes_mod, "_fetch_git_blob", _record)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(
                "/api/apps/blob",
                params={"repo": repo, "path": "logo.png", "ref": "main"},
            )
        # The registry entry lookup runs EXACTLY once (the pre-read at the top of
        # the handler) — the no-entry branch adds no second read.
        assert calls["n"] == 1, calls["n"]
        return resp.status, seen.get("git_url")

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "repo",
        [
            "https://github.com/org/app",
            "git@github.com:org/app.git",
            "ssh://git@example.com:2222/org/app.git",
        ],
    )
    async def test_url_shaped_repo_is_honored_without_a_bundled_entry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, repo: str
    ) -> None:
        # External (federated) registries never resolve a bundled entry, so a
        # validated URL-form ``repo`` is honored directly — threaded as the clone
        # URL, anonymous + strict (``owner_designated`` False).
        status, git_url = await self._clone_url_for(tmp_path, monkeypatch, repo=repo)
        assert status == 502  # the fake fetch returns False → graceful 502
        assert git_url == repo

    @pytest.mark.asyncio
    async def test_bare_name_without_entry_is_blob_no_git_url_502(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A bare (non-URL) token with no bundled entry has no resolvable clone
        # URL, so the handler short-circuits with the ``blob_no_git_url`` 502 and
        # never reaches ``_fetch_git_blob``.
        _setup_env(tmp_path, monkeypatch)
        monkeypatch.setattr(routes_mod, "known_registry_repos", lambda: {"just-a-name"})
        monkeypatch.setattr(routes_mod, "get_registry_app_by_repo", lambda r: None)

        async def _must_not_fetch(*a: Any, **k: Any) -> bool:
            raise AssertionError("no resolvable clone URL must not reach the fetch")

        monkeypatch.setattr(routes_mod, "_fetch_git_blob", _must_not_fetch)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(
                "/api/apps/blob",
                params={"repo": "just-a-name", "path": "logo.png", "ref": "main"},
            )
            assert resp.status == 502
            assert (await resp.json())["code"] == "blob_no_git_url"

    @pytest.mark.asyncio
    async def test_no_entry_branch_does_not_re_read_the_registry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Invariant (registry cache re-read
        # blocks the event loop).  Before the subtraction the no-entry branch ran
        # ``clone_url = _registry_git_url(repo)``, which re-consulted
        # ``get_registry_app_by_repo`` — an unbounded SYNCHRONOUS registry
        # JSON read — a SECOND time on the async request path, stalling the
        # gateway loop and heartbeat under a concurrent registry refresh.  The
        # subtraction deletes that resolver and resolves the clone URL by a pure
        # in-memory URL-form check, so the registry entry lookup happens exactly
        # ONCE per request (the pre-read at the top of the handler) and never on
        # the no-entry branch.  ``_clone_url_for`` asserts the lookup call count
        # is exactly 1; a reintroduced second read would make it 2 and fail here.
        status, git_url = await self._clone_url_for(
            tmp_path, monkeypatch, repo="https://github.com/org/external-app.git"
        )
        assert status == 502
        assert git_url == "https://github.com/org/external-app.git"


# ---------------------------------------------------------------------------
# GET /api/apps/blob — SSRF gate + cache
# ---------------------------------------------------------------------------


class TestBlobProxy:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "query,status",
        [
            ({"repo": "", "path": ""}, 400),
            ({"repo": "acme", "path": ""}, 400),
            ({"repo": "a;rm -rf /", "path": "i.png", "ref": "main"}, 400),
            ({"repo": "acme", "path": "assets/i$.png", "ref": "main"}, 400),
            ({"repo": "acme", "path": "i.png", "ref": "bad ref"}, 400),
            ({"repo": "acme", "path": "../../etc/i.png", "ref": "main"}, 400),
            ({"repo": "acme", "path": ".git/config.png", "ref": "main"}, 400),
            ({"repo": "acme", "path": "payload.svg.exe", "ref": "main"}, 403),
        ],
    )
    async def test_validation_rejects_unsafe_inputs(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        query: dict[str, str],
        status: int,
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        monkeypatch.setattr(routes_mod, "known_registry_repos", lambda: {"acme"})
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get("/api/apps/blob", params=query)
            assert resp.status == status

    @pytest.mark.asyncio
    async def test_repo_outside_registry_is_403(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The SSRF gate: only repos the registry already knows may be cloned.
        _setup_env(tmp_path, monkeypatch)
        monkeypatch.setattr(routes_mod, "known_registry_repos", lambda: {"acme"})
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(
                "/api/apps/blob",
                params={
                    "repo": "https://evil.example/org/app",
                    "path": "i.png",
                    "ref": "main",
                },
            )
            assert resp.status == 403
            assert (await resp.json())["error"] == "repo not in registry"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "ref",
        [
            "../other-repo-key/main",  # climb out of this repo_key into a sibling
            "../../etc/main",  # deeper traversal
            "/main",  # absolute-form leading slash
            "main/../../secret",  # a ``..`` mid-segment
        ],
    )
    async def test_ref_with_traversal_is_rejected_400(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ref: str
    ) -> None:
        # Invariant (``ref`` cache-path
        # traversal).  ``ref`` becomes a path segment in the blob cache tree
        # (``.../{repo_key}/{ref}/{file_path}``).  ``_SAFE_REF_RE`` permits ``.``
        # and ``/``, so ``../<other-repo-key>/main`` matches the regex; the
        # cache-root containment check catches an escape OUT of the cache root but
        # NOT a ``..`` that stays UNDER the root while crossing into a DIFFERENT
        # repo's cache dir — a crafted ``ref`` then yields a cache hit returning
        # another repo's cached (possibly private) bytes.  The guard: reject any
        # ``..`` segment or leading ``/`` in ``ref`` (mirroring the ``file_path``
        # guard) so a ``ref`` can only name a flat branch subtree under its own
        # ``repo_key``.  A traversal ``ref`` must 400 and never reach a fetch or a
        # sibling repo-key cache read.  Fails at 803dddcb (regex + containment let
        # it through); passes after.
        _setup_env(tmp_path, monkeypatch)
        monkeypatch.setattr(routes_mod, "known_registry_repos", lambda: {"acme"})

        async def _must_not_fetch(*args: Any, **kwargs: Any) -> bool:
            raise AssertionError("a traversal ref must be rejected before any fetch")

        monkeypatch.setattr(routes_mod, "_fetch_git_blob", _must_not_fetch)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(
                "/api/apps/blob",
                params={"repo": "acme", "path": "logo.png", "ref": ref},
            )
            assert resp.status == 400
            assert (await resp.json())["error"] == "invalid ref"

    @pytest.mark.asyncio
    async def test_cached_blob_is_served_without_fetching(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        monkeypatch.setattr(routes_mod, "known_registry_repos", lambda: {"acme"})

        async def _must_not_fetch(*args: Any, **kwargs: Any) -> bool:
            raise AssertionError("cache hit must not trigger a clone")

        # The cache key is bound to the resolved clone URL (provenance), so the
        # handler must resolve a URL before it looks up the cache.  Give ``acme``
        # a bundled entry whose clone URL is the same value the cache dir is keyed
        # on, so this exercises a genuine cache HIT on the provenance-bound key.
        clone_url = "ssh://forge.example/org/acme.git"
        monkeypatch.setattr(
            routes_mod,
            "get_registry_app_by_repo",
            lambda repo: {"repo": repo, "gitUrl": clone_url, "branch": "main"},
        )
        monkeypatch.setattr(routes_mod, "_fetch_git_blob", _must_not_fetch)
        cache = (
            routes_mod._blob_cache_dir()
            / routes_mod._blob_cache_key("acme", clone_url)
            / "main"
            / "assets"
        )
        cache.mkdir(parents=True)
        (cache / "logo.png").write_bytes(b"\x89PNG\r\n")

        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(
                "/api/apps/blob",
                params={"repo": "acme", "path": "assets/logo.png", "ref": "main"},
            )
            assert resp.status == 200
            assert resp.headers["Content-Type"] == "image/png"
            assert resp.headers["Cache-Control"] == "public, max-age=86400"
            assert await resp.read() == b"\x89PNG\r\n"

    @pytest.mark.asyncio
    async def test_serving_a_cached_blob_refreshes_its_mtime(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # ``_gc_blob_cache_dir`` reclaims a blob-cache file whose mtime has aged past
        # the grace, and only the owner-tier prewarm re-dates a live row's art. An
        # index-tier blob the proxy wrote once is never re-published, so without a
        # touch on serve a file still being served every day would age out and be
        # reclaimed, then re-cloned. Serving must bump the mtime so "being served"
        # keeps a file young exactly as a rewrite does.
        import os as _os
        import time as _time

        _setup_env(tmp_path, monkeypatch)
        monkeypatch.setattr(routes_mod, "known_registry_repos", lambda: {"acme"})

        async def _must_not_fetch(*args: Any, **kwargs: Any) -> bool:
            raise AssertionError("cache hit must not trigger a clone")

        clone_url = "ssh://forge.example/org/acme.git"
        monkeypatch.setattr(
            routes_mod,
            "get_registry_app_by_repo",
            lambda repo: {"repo": repo, "gitUrl": clone_url, "branch": "main"},
        )
        monkeypatch.setattr(routes_mod, "_fetch_git_blob", _must_not_fetch)
        cache = (
            routes_mod._blob_cache_dir()
            / routes_mod._blob_cache_key("acme", clone_url)
            / "main"
            / "assets"
        )
        cache.mkdir(parents=True)
        blob = cache / "logo.png"
        blob.write_bytes(b"\x89PNG\r\n")
        # Backdate the file well into the past, as an aging blob would be.
        # flake-ok: pre-existing wall-clock read that backdates a cached blob ten days -- moved verbatim; tracked for a de-flake pass
        old = _time.time() - 10 * 86400
        _os.utime(blob, (old, old))

        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(
                "/api/apps/blob",
                params={"repo": "acme", "path": "assets/logo.png", "ref": "main"},
            )
            assert resp.status == 200
            assert await resp.read() == b"\x89PNG\r\n"

        # The serve bumped the mtime back to ~now, so it is young enough to survive GC.
        assert blob.stat().st_mtime > old + 86400

    @pytest.mark.asyncio
    async def test_a_touch_failure_does_not_fail_the_serve(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Negative control / robustness: the mtime refresh is best-effort. If the
        # touch raises (a read-only mount, a racing GC unlink), the blob is served
        # regardless rather than 500-ing. Without the ``try/except`` around the
        # ``os.utime`` this serve would raise.
        import os as _os

        _setup_env(tmp_path, monkeypatch)
        monkeypatch.setattr(routes_mod, "known_registry_repos", lambda: {"acme"})

        async def _must_not_fetch(*args: Any, **kwargs: Any) -> bool:
            raise AssertionError("cache hit must not trigger a clone")

        clone_url = "ssh://forge.example/org/acme.git"
        monkeypatch.setattr(
            routes_mod,
            "get_registry_app_by_repo",
            lambda repo: {"repo": repo, "gitUrl": clone_url, "branch": "main"},
        )
        monkeypatch.setattr(routes_mod, "_fetch_git_blob", _must_not_fetch)
        cache = (
            routes_mod._blob_cache_dir()
            / routes_mod._blob_cache_key("acme", clone_url)
            / "main"
            / "assets"
        )
        cache.mkdir(parents=True)
        (cache / "logo.png").write_bytes(b"\x89PNG\r\n")

        real_utime = _os.utime

        def _boom(path: Any, *args: Any, **kwargs: Any) -> None:
            # Fail the serve's refresh touch, but leave the FileResponse's own
            # stat/open untouched so the rest of the handler behaves normally.
            if str(path).endswith("logo.png"):
                raise OSError("read-only cache")
            return real_utime(path, *args, **kwargs)

        monkeypatch.setattr(routes_mod.os, "utime", _boom)

        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(
                "/api/apps/blob",
                params={"repo": "acme", "path": "assets/logo.png", "ref": "main"},
            )
            assert resp.status == 200, "a failed mtime refresh must not fail the serve"
            assert await resp.read() == b"\x89PNG\r\n"

    def test_touch_without_nofollow_utime_still_refreshes_a_regular_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Windows has no ``os.utime(follow_symlinks=False)``; the touch must still
        # land there instead of raising NotImplementedError and being skipped.
        import os as _os
        import time as _time

        monkeypatch.setattr(routes_mod.os, "supports_follow_symlinks", set())
        blob = tmp_path / "logo.png"
        blob.write_bytes(b"x")
        # flake-ok: pre-existing wall-clock read that backdates a served blob ten days -- moved verbatim; tracked for a de-flake pass
        old = _time.time() - 10 * 86400
        _os.utime(blob, (old, old))
        routes_mod._touch_served_blob(blob)
        assert blob.stat().st_mtime > old + 86400

    @pytest.mark.skipif(sys.platform == "win32", reason="symlink creation needs privilege")
    def test_touch_without_nofollow_utime_leaves_a_link_target_alone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import os as _os
        import time as _time

        monkeypatch.setattr(routes_mod.os, "supports_follow_symlinks", set())
        target = tmp_path / "outside.png"
        target.write_bytes(b"x")
        # flake-ok: pre-existing wall-clock read that backdates the link target ten days -- moved verbatim; tracked for a de-flake pass
        old = _time.time() - 10 * 86400
        _os.utime(target, (old, old))
        link = tmp_path / "logo.png"
        link.symlink_to(target)
        routes_mod._touch_served_blob(link)
        assert target.stat().st_mtime == pytest.approx(old)

    @pytest.mark.asyncio
    async def test_repo_key_reuse_across_registries_does_not_serve_stale_bytes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Invariant (private cache entries
        # outlive their provenance).  ``_blob_cache_key`` once keyed the cache dir
        # on the ``repo`` STRING alone.  Chain: registry A (private) caches a blob
        # under repo key X; A is removed and registry B is later configured reusing
        # key X; B's request would then hit A's cached (possibly private) bytes.
        # The fix binds the cache key to the RESOLVED clone URL (provenance), so a
        # repo-key reuse across registries lands in a DISTINCT cache directory (a
        # miss + a fresh clone of B's own URL) rather than serving A's stale bytes.
        #
        # Simulate the swap: A cached bytes for repo key ``acme`` under a dir keyed
        # on A's URL; the resolver now returns B's entry (a DIFFERENT clone URL) for
        # the same key.  The request must NOT serve A's cached bytes — the
        # provenance-bound key differs, so it is a cache miss and the (faked) fetch
        # runs instead.  Fails at 803dddcb (repo-string key → A's bytes served);
        # passes after.
        _setup_env(tmp_path, monkeypatch)
        monkeypatch.setattr(routes_mod, "known_registry_repos", lambda: {"acme"})

        url_a = "ssh://forge.example/org/registry-a.git"  # the removed, private A
        url_b = "ssh://forge.example/org/registry-b.git"  # B, reusing key ``acme``

        # A's stale private bytes, cached under a dir keyed on A's provenance.
        stale = (
            routes_mod._blob_cache_dir()
            / routes_mod._blob_cache_key("acme", url_a)
            / "main"
            / "assets"
        )
        stale.mkdir(parents=True)
        (stale / "logo.png").write_bytes(b"A-PRIVATE-BYTES")

        # The registry now resolves key ``acme`` to B's clone URL (the swap).
        monkeypatch.setattr(
            routes_mod,
            "get_registry_app_by_repo",
            lambda repo: {"repo": repo, "gitUrl": url_b, "branch": "main"},
        )

        fetched: dict[str, str] = {}

        async def _record(
            repo: str,
            ref: str,
            file_path: str,
            cache_path: Path,
            git_url: str,
            *,
            owner_designated: bool = False,
        ) -> bool:
            # The cache lookup missed (key bound to B's URL, not A's), so the
            # handler fell through to a fresh clone of B's own URL.
            fetched["git_url"] = git_url
            fetched["cache_path"] = str(cache_path)
            return False

        monkeypatch.setattr(routes_mod, "_fetch_git_blob", _record)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(
                "/api/apps/blob",
                params={"repo": "acme", "path": "assets/logo.png", "ref": "main"},
            )
            # Cache miss → the fetch ran and (being faked to fail) yields 502.
            # The load-bearing property: A's private bytes were NOT served.
            assert resp.status == 502
            assert await resp.read() != b"A-PRIVATE-BYTES"

        # The fresh clone targets B's provenance, and the cache path is keyed on
        # B's URL — never A's stale directory.
        assert fetched["git_url"] == url_b
        assert routes_mod._blob_cache_key("acme", url_b) in fetched["cache_path"]
        assert routes_mod._blob_cache_key("acme", url_a) not in fetched["cache_path"]

    @pytest.mark.asyncio
    async def test_failed_fetch_is_502(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        monkeypatch.setattr(routes_mod, "known_registry_repos", lambda: {"acme"})

        async def _fail(*args: Any, **kwargs: Any) -> bool:
            return False

        monkeypatch.setattr(routes_mod, "_fetch_git_blob", _fail)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(
                "/api/apps/blob",
                params={"repo": "acme", "path": "logo.png", "ref": "main"},
            )
            assert resp.status == 502

    @pytest.mark.asyncio
    async def test_ref_defaults_to_registry_entry_branch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        monkeypatch.setattr(routes_mod, "known_registry_repos", lambda: {"acme"})
        monkeypatch.setattr(
            routes_mod,
            "get_registry_app_by_repo",
            lambda repo: {"repo": repo, "branch": "release"},
        )
        seen: dict[str, str] = {}

        async def _record(
            repo: str,
            ref: str,
            file_path: str,
            cache_path: Path,
            git_url: str,
            *,
            owner_designated: bool = False,
        ) -> bool:
            seen["ref"] = ref
            return False

        monkeypatch.setattr(routes_mod, "_fetch_git_blob", _record)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get("/api/apps/blob", params={"repo": "acme", "path": "logo.png"})
            assert resp.status == 502
        assert seen["ref"] == "release"

    @requires_symlinks
    @pytest.mark.asyncio
    async def test_symlinked_cache_dir_escaping_root_is_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The resolved-path check must fire BEFORE any mkdir, so a symlinked
        # cache subtree cannot write outside the blob cache root.
        _setup_env(tmp_path, monkeypatch)
        monkeypatch.setattr(routes_mod, "known_registry_repos", lambda: {"acme"})
        # The cache key is provenance-bound, so the handler resolves a clone URL
        # before the containment check runs; give ``acme`` an entry so the URL
        # resolves and the symlinked cache dir is keyed on the same value.
        clone_url = "ssh://forge.example/org/acme.git"
        monkeypatch.setattr(
            routes_mod,
            "get_registry_app_by_repo",
            lambda repo: {"repo": repo, "gitUrl": clone_url, "branch": "main"},
        )

        async def _must_not_fetch(*args: Any, **kwargs: Any) -> bool:
            raise AssertionError("path validation must reject before fetching")

        monkeypatch.setattr(routes_mod, "_fetch_git_blob", _must_not_fetch)
        outside = tmp_path / "outside-cache"
        outside.mkdir()
        cache_root = routes_mod._blob_cache_dir()
        cache_root.mkdir(parents=True, exist_ok=True)
        (cache_root / routes_mod._blob_cache_key("acme", clone_url)).symlink_to(outside)

        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(
                "/api/apps/blob",
                params={"repo": "acme", "path": "logo.png", "ref": "main"},
            )
            assert resp.status == 400
            assert (await resp.json())["error"] == "invalid path"
        assert not (outside / "main").exists()

    def test_safe_repo_identifier_accepts_vetted_git_forms(self) -> None:
        assert _is_safe_repo_identifier("BareName_1")
        assert _is_safe_repo_identifier("https://github.com/org/app.git")
        assert _is_safe_repo_identifier("git@github.com:org/app.git")
        assert _is_safe_repo_identifier("ssh://git@host:2222/org/app")
        assert _is_safe_repo_identifier("ssh://host/org/app")

    def test_safe_repo_identifier_rejects_untrusted_forms(self) -> None:
        # Plaintext http is refused: registry clones later execute setup code.
        assert not _is_safe_repo_identifier("")
        assert not _is_safe_repo_identifier("http://github.com/org/app")
        assert not _is_safe_repo_identifier("https://host/org/../../app")
        assert not _is_safe_repo_identifier("https://host/org/app;id")
        assert not _is_safe_repo_identifier("org/app")


# ---------------------------------------------------------------------------
# Blob-fetch credential posture (same-repo carve-out at the
# third clone chokepoint).  These pin the env + sandbox-mode PAIR the blob
# clone uses per origin, without asserting raw git argv (wrap_argv is patched
# to capture only the mode it was handed).
# ---------------------------------------------------------------------------


class _FakeProc:
    """A create_subprocess_limited stand-in whose clone always 'fails'.

    Returning rc=1 makes ``_fetch_git_blob`` bail right after the clone, so the
    posture is observable with no real subprocess, checkout, or filesystem read.
    """

    returncode = 1

    async def communicate(self) -> tuple[bytes, bytes]:
        return (b"", b"boom")

    def kill(self) -> None:  # pragma: no cover - timeout path only
        pass


class TestFetchGitBlobCredentialPosture:
    """``_fetch_git_blob`` picks env + sandbox mode from ``owner_designated``."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "raw",
        [
            "deploy:password@example.invalid:Owner/Repo.git",
            "ssh://deploy:password@example.invalid/Owner/Repo.git",
        ],
    )
    async def test_ambiguous_git_target_refuses_before_ssrf_gate_or_clone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raw: str
    ) -> None:
        import kiro_crew.apps.registry as reg_mod

        def _must_not_check_host(url: str) -> bool:
            raise AssertionError("ambiguous Git target must fail before the host gate")

        async def _must_not_spawn(*args: Any, **kwargs: Any):
            raise AssertionError("ambiguous Git target must fail before clone")

        monkeypatch.setattr(reg_mod, "is_clone_host_trusted", _must_not_check_host)
        monkeypatch.setattr(routes_mod, "create_subprocess_limited", _must_not_spawn)

        assert not await routes_mod._fetch_git_blob(
            "acme",
            "main",
            "assets/logo.png",
            tmp_path / "out.png",
            git_url=raw,
        )

    @pytest.mark.asyncio
    async def test_embedded_http_credential_uses_split_fetch_only(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Blob checkout must never inherit the one-shot HTTP credential."""
        import kiro_crew.apps.registry as reg_mod

        raw = "https://user:secret@example.invalid/owner/registry.git"
        safe = "https://example.invalid/owner/registry.git"
        captured: dict[str, Any] = {}

        monkeypatch.setattr(reg_mod, "is_clone_host_trusted", lambda url: True)
        monkeypatch.setattr(routes_mod, "minimal_env", lambda **extra: {"BASE": "1"})
        monkeypatch.setattr(routes_mod, "_context_clone_sandbox_mode", lambda url: "context-mode")
        monkeypatch.setattr(routes_mod, "_sel_credential_grant", lambda *args: None)

        async def _split_fetch(
            git_url: str,
            branch: str,
            dest: Path,
            log_lines: list[str],
            **kwargs: Any,
        ) -> None:
            captured.update(
                git_url=git_url,
                branch=branch,
                dest=dest,
                credential_target=kwargs["credential_target"],
                clone_env=kwargs["clone_env"],
                sandbox_mode=kwargs["sandbox_mode"],
            )
            (dest / "assets").mkdir(parents=True)
            (dest / "assets" / "logo.png").write_bytes(b"png")
            return None

        async def _must_not_clone(*args: Any, **kwargs: Any) -> None:
            raise AssertionError("credentialed blob path must not use combined git clone")

        monkeypatch.setattr(routes_mod, "_git_fetch_branch", _split_fetch)
        monkeypatch.setattr(routes_mod, "create_subprocess_limited", _must_not_clone)

        cache_path = tmp_path / "cache" / "logo.png"
        assert await routes_mod._fetch_git_blob(
            "acme",
            "main",
            "assets/logo.png",
            cache_path,
            git_url=raw,
            owner_designated=True,
        )

        assert cache_path.read_bytes() == b"png"
        assert captured["git_url"] == safe
        assert captured["credential_target"] == raw
        assert captured["branch"] == "main"
        assert captured["clone_env"] == {"BASE": "1"}
        assert captured["sandbox_mode"] == "context-mode"
        assert "secret" not in repr(captured["clone_env"])
        assert not captured["dest"].parent.exists()

    def _capture(
        self,
        monkeypatch: pytest.MonkeyPatch,
        git_url: str,
    ) -> dict[str, Any]:
        import kiro_crew.apps.registry as reg_mod

        captured: dict[str, Any] = {}

        # ``_fetch_git_blob`` resolves NO clone URL from ``repo``: the standalone
        # ``_registry_git_url`` resolver has been deleted and the caller threads
        # the once-decided URL in as the required ``git_url`` param.  The
        # no-re-resolution invariant is therefore STRUCTURAL — there is no
        # resolver left for the callee to call — not a monkeypatched tripwire.
        # ``is_clone_host_trusted`` is still imported function-locally inside
        # ``_fetch_git_blob`` (part of the untouched SSRF gate), so it is patched on
        # the registry module.  The credential-posture helpers, by contrast, were
        # hoisted to ``routes`` module scope, so they are patched there — patching
        # ``reg_mod`` would not intercept the module-level name.
        monkeypatch.setattr(reg_mod, "is_clone_host_trusted", lambda url: True)
        # Sentinel env dicts so the test asserts WHICH builder was used without
        # depending on the host's real environment contents.
        monkeypatch.setattr(routes_mod, "minimal_env", lambda **extra: {"_env": "minimal"})
        monkeypatch.setattr(routes_mod, "anonymous_git_env", lambda **extra: {"_env": "anonymous"})
        monkeypatch.setattr(routes_mod, "_context_clone_sandbox_mode", lambda url: "context-mode")

        def _fake_wrap(cmd: list[str], *, mode: str, **kwargs: Any) -> tuple[list[str], None]:
            captured["mode"] = mode
            # ``wrap_argv_async`` hands the socket request on only when it is set.
            captured["forward"] = kwargs.get("forward_ssh_auth_sock", False)
            # The clone argv is ``git clone ... <git_url> <tmp_root>`` — the URL
            # the process will actually clone is the second-to-last element.  We
            # DON'T assert the full argv (wrap_argv/cgroup lesson); we read back
            # only the URL to confirm the credential decision and the clone agree.
            captured["cloned_url"] = cmd[-2]
            return (cmd, None)

        monkeypatch.setattr(routes_mod, "wrap_argv", _fake_wrap)
        monkeypatch.setattr(routes_mod, "cgroup_scope_argv", lambda cmd: cmd)

        # Capture the SEL credential-grant audit call (a privilege escalation must
        # leave a record) without depending on the real SEL sink.
        grants: list[tuple[str, str]] = []
        monkeypatch.setattr(
            routes_mod,
            "_sel_credential_grant",
            lambda operation, git_url: grants.append((operation, git_url)),
        )
        captured["grants"] = grants

        async def _fake_create(*args: Any, **kwargs: Any) -> _FakeProc:
            captured["env"] = kwargs.get("env")
            return _FakeProc()

        monkeypatch.setattr(routes_mod, "create_subprocess_limited", _fake_create)
        return captured

    @pytest.mark.asyncio
    async def test_owner_designated_uses_minimal_env_and_context_mode(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        url = "ssh://forge.example/org/registry.git"
        cap = self._capture(monkeypatch, url)

        # Control: an owner-designated entry whose URL is ``url``.  The caller
        # threads ``git_url=url`` — the SAME URL the carve-out was decided for —
        # so the carve-out is honored.  ``_fetch_git_blob`` does not re-resolve
        # from ``repo``; it uses the threaded value for both the decision and the
        # clone.
        ok = await routes_mod._fetch_git_blob(
            url,
            "main",
            "assets/logo.png",
            tmp_path / "out.png",
            git_url=url,
            owner_designated=True,
        )

        assert ok is False  # the fake clone fails → graceful fallback
        # Same-repo carve-out flips BOTH knobs together.
        assert cap["mode"] == "context-mode"
        assert cap["env"] == {"_env": "minimal"}
        # The privilege escalation left an SEL audit record for the URL cloned.
        assert cap["grants"] == [("app_blob_proxy", url)]
        # The threaded URL is the one actually cloned — decision and clone agree.
        assert cap["cloned_url"] == url

    @pytest.mark.asyncio
    async def test_default_is_anonymous_env_and_strict_mode(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        cap = self._capture(monkeypatch, "ssh://forge.example/org/sibling.git")

        # A sibling repo on the same trusted host — a DIFFERENT URL from the
        # configured registry — never gets the carve-out.  The caller threads the
        # sibling URL and ``owner_designated=False``.
        ok = await routes_mod._fetch_git_blob(
            "ssh://forge.example/org/sibling.git",
            "main",
            "assets/logo.png",
            tmp_path / "out.png",
            git_url="ssh://forge.example/org/sibling.git",
        )

        assert ok is False
        assert cap["mode"] == "strict"
        assert cap["env"] == {"_env": "anonymous"}
        # The default (anonymous) path grants no credentials → no audit record.
        assert cap["grants"] == []
        assert cap["cloned_url"] == "ssh://forge.example/org/sibling.git"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("owner_designated", [False, True], ids=["default", "owner"])
    async def test_blob_clone_asks_for_the_agent_socket_only_for_the_owners_url(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, owner_designated: bool
    ) -> None:
        """The agent socket goes with the owner credentials and with nothing else."""
        _setup_env(tmp_path, monkeypatch)
        url = "ssh://forge.example/org/registry.git"
        cap = self._capture(monkeypatch, url)

        ok = await routes_mod._fetch_git_blob(
            url,
            "main",
            "assets/logo.png",
            tmp_path / "out.png",
            git_url=url,
            owner_designated=owner_designated,
        )

        assert ok is False  # the fake clone fails → graceful fallback
        assert cap["forward"] is owner_designated

    @pytest.mark.asyncio
    async def test_injected_cloneurl_never_becomes_the_credentialed_clone_target(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Enforced by CONSTRUCTION
        # rather than a downgrade recheck.  Two registries are configured — A
        # (repo=urlA) and B (repo=urlB, a separately-configured PRIVATE registry).
        # A's untrusted index injects an app entry that carries an explicit
        # ``cloneUrl=urlB`` alongside A's own ``gitUrl=urlA``.
        #
        # The earlier finding was that a standalone clone-URL resolver honored that
        # injected ``cloneUrl`` while the credential decision used ``_entry_git_url``,
        # so the two resolvers named different URLs and the clone could reach urlB
        # with owner credentials.  The subtraction deletes the divergence: that
        # resolver is gone and ``cloneUrl`` is not read anywhere, so the only
        # URL that can reach the clone is the one the caller threads — urlA, the
        # entry's own ``gitUrl``, byte-identical to the URL the carve-out was
        # decided for.  urlB is never the clone target, credentialed or otherwise,
        # so there is no cross-registry leak and nothing to downgrade.  (That
        # ``cloneUrl`` is not read by the entry resolver ``_entry_git_url`` is
        # pinned in test_external_registry.py; here we pin the end-to-end
        # clone-target guarantee through the handler + callee.)
        _setup_env(tmp_path, monkeypatch)
        url_a = "ssh://forge.example/org/registry-a.git"
        url_b = "ssh://forge.example/org/private-b.git"

        # When the carve-out is honored (owner_designated + the caller threads
        # the entry's own urlA), credentials apply to urlA only — the SEL grant
        # names urlA, the clone uses urlA, and urlB never appears as a clone
        # target.  ``git_url`` is the threaded value; the callee does not resolve
        # it from ``repo``, so the injected ``cloneUrl=urlB`` cannot reach the
        # clone even if a resolver returned it.
        cap = self._capture(monkeypatch, url_a)
        ok = await routes_mod._fetch_git_blob(
            "acme",
            "main",
            "assets/logo.png",
            tmp_path / "out.png",
            git_url=url_a,
            owner_designated=True,
        )
        assert ok is False
        assert cap["mode"] == "context-mode"
        assert cap["env"] == {"_env": "minimal"}
        assert cap["grants"] == [("app_blob_proxy", url_a)]
        assert cap["cloned_url"] == url_a
        assert all(url_b not in grant for _op, grant in cap["grants"])

    @pytest.mark.asyncio
    async def test_anonymous_env_suppresses_git_credentials(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The load-bearing property of the default posture, checked against the
        # REAL env builders (not the sentinels): the anonymous env disables
        # system+global git config and never prompts, while minimal_env — used
        # for the owner-designated repo — does not strip those.
        _setup_env(tmp_path, monkeypatch)
        import kiro_crew.apps.registry as reg_mod

        anon = reg_mod.anonymous_git_env()
        minimal = reg_mod.minimal_env()
        assert anon.get("GIT_CONFIG_NOSYSTEM") == "1"
        assert anon.get("GIT_TERMINAL_PROMPT") == "0"
        assert "GIT_CONFIG_NOSYSTEM" not in minimal
        assert "GIT_TERMINAL_PROMPT" not in minimal


class TestBlobProxyOwnerDesignatedWiring:
    """The handler decides ``owner_designated`` via ``_is_owner_designated_repo``.

    A same-repo entry (index URL byte-identical to the owner-configured registry
    repo) threads ``owner_designated=True`` into the fetch; a sibling repo on the
    same host and a bundled entry thread ``False``.
    """

    async def _owner_designated_for(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        entry: dict[str, Any] | None,
        repo: str = "acme",
        owner_count: int = 1,
        ref: str = "main",
    ) -> bool:
        _setup_env(tmp_path, monkeypatch)
        monkeypatch.setattr(routes_mod, "known_registry_repos", lambda: {repo})
        monkeypatch.setattr(routes_mod, "get_registry_app_by_repo", lambda r: entry)
        # Provenance: how many configured registry SOURCES publish this ``repo``
        # key.  These cases inject the entry via ``get_registry_app_by_repo``
        # rather than through the real registry files, so pin the count directly.
        # The single-owner default (1) is the control the carve-out is designed
        # for; the multi-owner ambiguity is exercised by the dedicated provenance
        # regression below.
        monkeypatch.setattr(routes_mod, "_repo_key_owner_count", lambda r: owner_count)
        # For the entry-present cases the handler resolves the clone URL from
        # ``_entry_git_url(entry)``.  For the no-entry case the handler resolves
        # it by an inline URL-form check on ``repo`` (no registry read), so the
        # caller must pass a URL-form ``repo`` for the fetch to be reached and
        # ``owner_designated`` observable — a bare name would short-circuit at the
        # ``blob_no_git_url`` 502 before the fetch.
        seen: dict[str, Any] = {}

        async def _record(
            repo: str,
            ref: str,
            file_path: str,
            cache_path: Path,
            git_url: str,
            *,
            owner_designated: bool = False,
        ) -> bool:
            seen["owner_designated"] = owner_designated
            seen["git_url"] = git_url
            return False

        monkeypatch.setattr(routes_mod, "_fetch_git_blob", _record)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(
                "/api/apps/blob",
                params={"repo": repo, "path": "logo.png", "ref": ref},
            )
            assert resp.status == 502
        return seen["owner_designated"]

    @pytest.mark.asyncio
    async def test_same_repo_entry_is_owner_designated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import kiro_crew.apps.registry as reg_mod

        url = "ssh://forge.example/org/registry.git"
        # An external-index entry whose clone URL equals the owner-configured
        # registry repo.  Patch the merged predicate's config source so the
        # byte-identical comparison matches.
        monkeypatch.setattr(
            reg_mod,
            "_effective_registries",
            lambda: [SimpleNamespace(name="corp", repo=url)],
        )
        entry = {"repo": url, "gitUrl": url, "_registry": "corp"}
        assert await self._owner_designated_for(tmp_path, monkeypatch, entry=entry) is True

    @pytest.mark.asyncio
    async def test_sanitized_same_repo_row_rehydrates_transport_for_split_fetch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Retained rows stay public while the exact config target reaches fetch."""
        import kiro_crew.apps.registry as reg_mod

        _setup_env(tmp_path, monkeypatch)
        raw = "https://user:secret@example.invalid/org/registry.git"
        safe = "https://example.invalid/org/registry.git"
        entry = {
            "repo": safe,
            "gitUrl": safe,
            "branch": "main",
            "_registry": "corp",
        }
        monkeypatch.setattr(routes_mod, "known_registry_repos", lambda: {"acme"})
        monkeypatch.setattr(routes_mod, "get_registry_app_by_repo", lambda repo: entry)
        monkeypatch.setattr(routes_mod, "_repo_key_owner_count", lambda repo: 1)
        monkeypatch.setattr(
            reg_mod,
            "_effective_registries",
            lambda: [SimpleNamespace(name="corp", repo=raw)],
        )

        seen: dict[str, Any] = {}

        async def _record(
            repo: str,
            ref: str,
            file_path: str,
            cache_path: Path,
            *,
            git_url: str,
            owner_designated: bool = False,
            credential_target: str | None = None,
        ) -> bool:
            seen.update(
                git_url=git_url,
                owner_designated=owner_designated,
                credential_target=credential_target,
                cache_path=cache_path,
            )
            return False

        monkeypatch.setattr(routes_mod, "_fetch_git_blob", _record)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(
                "/api/apps/blob",
                params={"repo": "acme", "path": "logo.png", "ref": "main"},
            )
            assert resp.status == 502

        assert seen["git_url"] == safe
        assert seen["credential_target"] == raw
        assert seen["owner_designated"] is True
        assert "secret" not in str(seen["cache_path"])

    @pytest.mark.asyncio
    async def test_configured_branch_ref_is_owner_designated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # CONTROL for the F1 gate: the SAME owner-designated entry whose
        # configured branch is ``main``, served on ``ref=main``, IS credentialed.
        # Paired with the differing-ref regression below — together they pin that
        # ONLY the configured branch attaches credentials.
        import kiro_crew.apps.registry as reg_mod

        url = "ssh://forge.example/org/registry.git"
        monkeypatch.setattr(
            reg_mod,
            "_effective_registries",
            lambda: [SimpleNamespace(name="corp", repo=url)],
        )
        entry = {"repo": url, "gitUrl": url, "branch": "main", "_registry": "corp"}
        assert (
            await self._owner_designated_for(tmp_path, monkeypatch, entry=entry, ref="main") is True
        )

    @pytest.mark.asyncio
    async def test_query_ref_differing_from_configured_branch_is_not_owner_designated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Invariant (the credential grant
        # ignored the effective ref).  ``ref`` falls back to the entry's
        # configured branch ONLY when the query param is empty; a caller can
        # otherwise supply any ``_SAFE_REF_RE``-valid ``ref`` (e.g.
        # ``iconPath=logo.png&ref=private``).  Before this gate ``owner_designated``
        # was decided on the ENTRY alone, so a crafted ``ref`` drove an
        # owner-credentialed shallow clone of an UNCONFIGURED (e.g. private) branch
        # of the owner's repo and served its image bytes.  The gate: the owner
        # grant is honored only when the effective ``ref`` equals the entry's
        # configured branch; a differing ``ref`` never attaches credentials
        # (``owner_designated`` False → anonymous+strict, still serving a public
        # branch).  The paired control above pins that ``ref=main`` (the configured
        # branch) on the SAME entry IS credentialed.  Fails at 803dddcb (grant
        # ignores ref); passes after.
        import kiro_crew.apps.registry as reg_mod

        url = "ssh://forge.example/org/registry.git"
        monkeypatch.setattr(
            reg_mod,
            "_effective_registries",
            lambda: [SimpleNamespace(name="corp", repo=url)],
        )
        # Configured branch is ``main``; an attacker-chosen ref (an unconfigured
        # branch) must NOT be credentialed.
        entry = {"repo": url, "gitUrl": url, "branch": "main", "_registry": "corp"}
        assert (
            await self._owner_designated_for(tmp_path, monkeypatch, entry=entry, ref="private")
            is False
        )

    @pytest.mark.asyncio
    async def test_sibling_repo_same_host_is_not_owner_designated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import kiro_crew.apps.registry as reg_mod

        registry_url = "ssh://forge.example/org/registry.git"
        sibling_url = "ssh://forge.example/org/sibling.git"
        monkeypatch.setattr(
            reg_mod,
            "_effective_registries",
            lambda: [SimpleNamespace(name="corp", repo=registry_url)],
        )
        # Same trusted host, DIFFERENT URL → carve-out must not apply.
        entry = {"repo": sibling_url, "gitUrl": sibling_url, "_registry": "corp"}
        assert await self._owner_designated_for(tmp_path, monkeypatch, entry=entry) is False

    @pytest.mark.asyncio
    async def test_bundled_entry_is_not_owner_designated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A bundled entry carries no ``_registry`` marker, so the predicate
        # returns False and the blob clone stays anonymous + strict.
        entry = {"repo": "acme", "gitUrl": "ssh://forge.example/org/acme.git"}
        assert await self._owner_designated_for(tmp_path, monkeypatch, entry=entry) is False

    @pytest.mark.asyncio
    async def test_no_entry_is_not_owner_designated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # No registry entry at all (a full-URL external/federated repo) → the
        # inline URL-form check resolves the clone URL, and the default posture
        # (anonymous + strict, ``owner_designated`` False) applies.
        assert (
            await self._owner_designated_for(
                tmp_path,
                monkeypatch,
                entry=None,
                repo="https://github.com/org/external-app.git",
            )
            is False
        )

    @pytest.mark.asyncio
    async def test_owner_designation_is_entry_scoped_not_global_membership(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The carve-out decision is entry-scoped, not a membership test over the
        # global set of configured registry repos.  Two registries are configured,
        # A (corp) and B (private); the A-owned entry's own URL is urlA.  The
        # handler decides ``owner_designated`` from ``_is_owner_designated_repo``,
        # which matches the entry's own ``_registry`` (A) — so it is True for the
        # A-owned entry.  The URL threaded to the fetch is resolved from the SAME
        # entry (``_entry_git_url``), so the URL the fetch clones is urlA — never
        # B, even though B is also configured.
        import kiro_crew.apps.registry as reg_mod

        url_a = "ssh://forge.example/org/registry-a.git"
        url_b = "ssh://forge.example/org/private-b.git"
        monkeypatch.setattr(
            reg_mod,
            "_effective_registries",
            lambda: [
                SimpleNamespace(name="corp", repo=url_a),
                SimpleNamespace(name="private", repo=url_b),
            ],
        )
        entry = {"repo": url_a, "gitUrl": url_a, "_registry": "corp"}
        assert await self._owner_designated_for(tmp_path, monkeypatch, entry=entry) is True

    @pytest.mark.asyncio
    async def test_concurrent_refresh_cannot_redirect_credentialed_clone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Invariant (TOCTOU between the credential decision and
        # the clone).  ``handle_blob_proxy`` decides ``owner_designated`` and
        # resolves the clone URL from ONE registry entry, then threads that URL
        # into ``_fetch_git_blob``.  The bug this pins: if the callee re-resolved
        # the clone URL from ``repo`` a SECOND time, a concurrent registry refresh
        # landing between the decision and the clone could swap the entry backing
        # ``repo`` to a private sibling — so an owner-credential grant decided for
        # urlA would clone urlB (a different private repo) WITH those credentials.
        #
        # Simulate the concurrent refresh: the entry lookup returns the
        # owner-designated urlA entry (the decision + the threaded URL).  The
        # standalone clone-URL resolver that a callee-side re-resolution would
        # have gone through has been DELETED, so there is structurally no second
        # read to race: the clone uses the THREADED urlA.  At c6fa20c7 the callee
        # re-resolved from ``repo`` and would clone urlB with credentials.  We
        # drive the real ``handle_blob_proxy`` + ``_fetch_git_blob`` with only the
        # subprocess/env/sandbox faked, so the threading is exercised end to end
        # (never the raw git argv — we read back only the cloned URL and the
        # env/mode PAIR).
        import kiro_crew.apps.registry as reg_mod

        _setup_env(tmp_path, monkeypatch)
        url_a = "ssh://forge.example/org/registry-a.git"
        url_b = "ssh://forge.example/org/private-b.git"

        monkeypatch.setattr(routes_mod, "known_registry_repos", lambda: {"acme"})
        # The decided entry: owner-designated, its own URL is urlA.  A concurrent
        # refresh could remap ``repo`` to urlB (a swapped-in private sibling), but
        # the callee never re-reads the registry — the deleted resolver leaves no
        # re-resolution path — so the swap cannot reach the clone.
        entry = {"repo": "acme", "gitUrl": url_a, "_registry": "corp"}
        monkeypatch.setattr(routes_mod, "get_registry_app_by_repo", lambda repo: entry)
        # Single unambiguous owner for the served ``repo`` key — the provenance
        # gate is satisfied, so this exercises the surviving credentialed path.
        monkeypatch.setattr(routes_mod, "_repo_key_owner_count", lambda r: 1)
        monkeypatch.setattr(
            reg_mod,
            "_effective_registries",
            lambda: [SimpleNamespace(name="corp", repo=url_a)],
        )
        monkeypatch.setattr(reg_mod, "is_clone_host_trusted", lambda url: True)

        captured: dict[str, Any] = {}
        monkeypatch.setattr(routes_mod, "minimal_env", lambda **extra: {"_env": "minimal"})
        monkeypatch.setattr(routes_mod, "anonymous_git_env", lambda **extra: {"_env": "anonymous"})
        monkeypatch.setattr(routes_mod, "_context_clone_sandbox_mode", lambda url: "context-mode")

        def _fake_wrap(cmd: list[str], *, mode: str, **_kwargs: Any) -> tuple[list[str], None]:
            captured["mode"] = mode
            captured["cloned_url"] = cmd[-2]
            return (cmd, None)

        monkeypatch.setattr(routes_mod, "wrap_argv", _fake_wrap)
        monkeypatch.setattr(routes_mod, "cgroup_scope_argv", lambda cmd: cmd)

        grants: list[tuple[str, str]] = []
        monkeypatch.setattr(
            routes_mod,
            "_sel_credential_grant",
            lambda operation, git_url: grants.append((operation, git_url)),
        )

        async def _fake_create(*args: Any, **kwargs: Any) -> _FakeProc:
            captured["env"] = kwargs.get("env")
            return _FakeProc()

        monkeypatch.setattr(routes_mod, "create_subprocess_limited", _fake_create)

        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(
                "/api/apps/blob",
                params={"repo": "acme", "path": "logo.png", "ref": "main"},
            )
            # The fake clone fails (rc=1) → graceful 502, but the posture is
            # observable from what reached the subprocess.
            assert resp.status == 502

        # The clone used the THREADED urlA, never the swapped-in urlB.
        assert captured["cloned_url"] == url_a
        assert url_b not in captured["cloned_url"]
        # Owner credentials were granted for urlA — and the SEL grant names urlA,
        # so credentials never reached urlB.
        assert captured["env"] == {"_env": "minimal"}
        assert captured["mode"] == "context-mode"
        assert grants == [("app_blob_proxy", url_a)]
        assert all(url_b not in g for _op, g in grants)

    @pytest.mark.asyncio
    async def test_ambiguous_provenance_downgrades_to_anonymous_strict(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Invariant (cross-registry confused
        # deputy).  ``get_registry_app_by_repo`` selects the entry by ``repo`` key
        # alone (bundled first, then each external registry), provenance-blind.  If
        # two configured registries — A (owner-designated for repo key X) and B (a
        # separately-configured registry that also lists an app keyed X) — both
        # claim X, a request reachable only through B resolves to A's
        # owner-designated entry.  Before the provenance gate the handler then
        # granted A's owner credentials and cloned A's private repo, serving A's
        # private image bytes to a B-reachable caller.
        #
        # The gate: the credentialed carve-out is reachable ONLY when exactly ONE
        # configured source publishes the ``repo`` key
        # (``_repo_key_owner_count == 1``).  With A and B both claiming X the count
        # is 2, so ``owner_designated`` stays False and the clone is
        # anonymous+strict — A's owner credentials are NEVER used and A's private
        # bytes are never cloned with a grant on a B-reachable request.  We drive
        # the real ``handle_blob_proxy`` + ``_fetch_git_blob`` with only the
        # subprocess/env/sandbox faked, reading back the env/mode PAIR and the SEL
        # grants (never the raw git argv).  Fails at 64df951a (repo-keyed lookup
        # grants A's credentials); passes after.
        import kiro_crew.apps.registry as reg_mod

        _setup_env(tmp_path, monkeypatch)
        url_a = "ssh://forge.example/org/registry-a.git"

        monkeypatch.setattr(routes_mod, "known_registry_repos", lambda: {"acme"})
        # The repo-keyed lookup returns A's owner-designated entry (its own URL is
        # urlA); ``_is_owner_designated_repo`` WOULD return True for it in
        # isolation.  What must stop the grant is provenance, not the entry check.
        entry = {"repo": "acme", "gitUrl": url_a, "_registry": "corp-a"}
        monkeypatch.setattr(routes_mod, "get_registry_app_by_repo", lambda repo: entry)
        # Two configured sources publish the same ``repo`` key → ambiguous.
        monkeypatch.setattr(routes_mod, "_repo_key_owner_count", lambda r: 2)
        monkeypatch.setattr(
            reg_mod,
            "_effective_registries",
            lambda: [SimpleNamespace(name="corp-a", repo=url_a)],
        )
        monkeypatch.setattr(reg_mod, "is_clone_host_trusted", lambda url: True)

        captured: dict[str, Any] = {}
        monkeypatch.setattr(routes_mod, "minimal_env", lambda **extra: {"_env": "minimal"})
        monkeypatch.setattr(routes_mod, "anonymous_git_env", lambda **extra: {"_env": "anonymous"})
        monkeypatch.setattr(routes_mod, "_context_clone_sandbox_mode", lambda url: "context-mode")

        def _fake_wrap(cmd: list[str], *, mode: str, **_kwargs: Any) -> tuple[list[str], None]:
            captured["mode"] = mode
            captured["cloned_url"] = cmd[-2]
            return (cmd, None)

        monkeypatch.setattr(routes_mod, "wrap_argv", _fake_wrap)
        monkeypatch.setattr(routes_mod, "cgroup_scope_argv", lambda cmd: cmd)

        grants: list[tuple[str, str]] = []
        monkeypatch.setattr(
            routes_mod,
            "_sel_credential_grant",
            lambda operation, git_url: grants.append((operation, git_url)),
        )

        async def _fake_create(*args: Any, **kwargs: Any) -> _FakeProc:
            captured["env"] = kwargs.get("env")
            return _FakeProc()

        monkeypatch.setattr(routes_mod, "create_subprocess_limited", _fake_create)

        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(
                "/api/apps/blob",
                params={"repo": "acme", "path": "logo.png", "ref": "main"},
            )
            assert resp.status == 502

        # Ambiguous provenance → anonymous + strict.  The load-bearing assertion:
        # owner credentials (minimal_env + context sandbox mode) are NEVER used,
        # so A's private repo is not cloned with a grant on a B-reachable request.
        assert captured["env"] == {"_env": "anonymous"}
        assert captured["mode"] == "strict"
        # No credential grant was made, so nothing is SEL-audited as an escalation.
        assert grants == []

    @pytest.mark.asyncio
    async def test_owner_designated_branch_resolves_sandbox_mode_off_the_event_loop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Invariant (synchronous config-load
        # on the event loop).  Inside ``_fetch_git_blob``'s ``owner_designated``
        # branch, ``_context_clone_sandbox_mode(git_url)`` flows
        # ``_configured_registry_hosts`` -> ``_effective_registries`` ->
        # ``KiroCrewConfig.load`` — an unbounded ``read_text`` + ``json.loads`` +
        # ``jsonschema.validate`` on a cold/invalidated cache (e.g. right after a
        # registry refresh rewrites config).  ``_fetch_git_blob`` runs on the
        # gateway event loop during App Store browsing, so calling it inline would
        # freeze every concurrent chat turn and the liveness heartbeat.  The two
        # sibling reads in the same function are already offloaded via
        # ``asyncio.to_thread``; this one must be too.
        #
        # Assert via thread-identity: the resolver must observe a threadpool-worker
        # thread, NOT the event-loop thread.  At 64df951a the call is synchronous
        # inline, so it runs on the loop thread and this fails; after the offload
        # it runs on a worker and passes.  (Indexing ``ran_on[0]`` is deliberate: a
        # resolver that never ran raises rather than passing vacuously.)
        import kiro_crew.apps.registry as reg_mod

        _setup_env(tmp_path, monkeypatch)
        url = "ssh://forge.example/org/registry.git"
        loop_thread = threading.current_thread().name
        ran_on: list[str] = []

        def _record_sandbox_mode(git_url: str) -> str:
            ran_on.append(threading.current_thread().name)
            return "context-mode"

        monkeypatch.setattr(routes_mod, "_context_clone_sandbox_mode", _record_sandbox_mode)
        monkeypatch.setattr(reg_mod, "is_clone_host_trusted", lambda u: True)
        monkeypatch.setattr(routes_mod, "minimal_env", lambda **extra: {"_env": "minimal"})
        monkeypatch.setattr(routes_mod, "anonymous_git_env", lambda **extra: {"_env": "anonymous"})
        monkeypatch.setattr(routes_mod, "wrap_argv", lambda cmd, *, mode, **_k: (cmd, None))
        monkeypatch.setattr(routes_mod, "cgroup_scope_argv", lambda cmd: cmd)
        monkeypatch.setattr(routes_mod, "_sel_credential_grant", lambda operation, git_url: None)

        async def _fake_create(*args: Any, **kwargs: Any) -> _FakeProc:
            return _FakeProc()

        monkeypatch.setattr(routes_mod, "create_subprocess_limited", _fake_create)

        ok = await routes_mod._fetch_git_blob(
            url,
            "main",
            "assets/logo.png",
            tmp_path / "out.png",
            git_url=url,
            owner_designated=True,
        )

        assert ok is False  # the fake clone fails → graceful fallback
        # The resolver ran off the event-loop thread (on a threadpool worker).
        assert ran_on[0] != loop_thread


# ---------------------------------------------------------------------------
# Blob cache directory creation runs off the event loop
# ---------------------------------------------------------------------------


def _record_mkdir_threads(monkeypatch: pytest.MonkeyPatch, target: Path) -> list[str]:
    """Wrap ``Path.mkdir`` and record the thread each ``target.mkdir`` call ran on.

    Every other ``mkdir`` passes straight through, so the test harness's own
    directories are unaffected.
    """
    real_mkdir = Path.mkdir
    ran_on: list[str] = []

    def _mkdir(self: Path, *args: Any, **kwargs: Any) -> None:
        if self == target:
            ran_on.append(threading.current_thread().name)
        real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", _mkdir)
    return ran_on


class TestBlobCacheMkdirOffLoop:
    """Both blob-cache ``mkdir`` calls run on a worker thread, not the event loop.

    ``handle_blob_proxy`` creates the cache directory after the containment check
    and before the cache lookup; ``_fetch_git_blob`` creates it again before it
    writes the fetched bytes. On a slow or hung data-home mount an inline
    ``mkdir`` would stall every session on the gateway loop.
    """

    @pytest.mark.asyncio
    async def test_handler_creates_cache_dir_off_the_loop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        monkeypatch.setattr(routes_mod, "known_registry_repos", lambda: {"acme"})
        clone_url = "ssh://forge.example/org/acme.git"
        monkeypatch.setattr(
            routes_mod,
            "get_registry_app_by_repo",
            lambda repo: {"repo": repo, "gitUrl": clone_url, "branch": "main"},
        )
        cache_dir = (
            routes_mod._blob_cache_dir()
            / routes_mod._blob_cache_key("acme", clone_url)
            / "main"
            / "assets"
        )
        loop_thread = threading.current_thread().name
        ran_on = _record_mkdir_threads(monkeypatch, cache_dir)

        async def _fail(*args: Any, **kwargs: Any) -> bool:
            return False

        monkeypatch.setattr(routes_mod, "_fetch_git_blob", _fail)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(
                "/api/apps/blob",
                params={"repo": "acme", "path": "assets/logo.png", "ref": "main"},
            )
            assert resp.status == 502

        # The handler created the directory (indexing ``ran_on[0]`` fails rather
        # than passing vacuously if it never did), and not on the loop thread.
        assert cache_dir.is_dir()
        assert ran_on[0] != loop_thread

    @pytest.mark.asyncio
    async def test_fetch_creates_cache_dir_off_the_loop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import kiro_crew.apps.registry as reg_mod

        monkeypatch.setattr(reg_mod, "is_clone_host_trusted", lambda url: True)
        monkeypatch.setattr(routes_mod, "minimal_env", lambda **extra: {"BASE": "1"})
        monkeypatch.setattr(routes_mod, "_context_clone_sandbox_mode", lambda url: "context-mode")
        monkeypatch.setattr(routes_mod, "_sel_credential_grant", lambda *args: None)

        async def _split_fetch(
            git_url: str,
            branch: str,
            dest: Path,
            log_lines: list[str],
            **kwargs: Any,
        ) -> None:
            (dest / "assets").mkdir(parents=True)
            (dest / "assets" / "logo.png").write_bytes(b"png")
            return None

        monkeypatch.setattr(routes_mod, "_git_fetch_branch", _split_fetch)

        cache_path = tmp_path / "cache" / "acme" / "main" / "logo.png"
        loop_thread = threading.current_thread().name
        ran_on = _record_mkdir_threads(monkeypatch, cache_path.parent)

        assert await routes_mod._fetch_git_blob(
            "acme",
            "main",
            "assets/logo.png",
            cache_path,
            git_url="https://user:secret@example.invalid/owner/registry.git",
            owner_designated=True,
        )

        assert cache_path.read_bytes() == b"png"
        assert ran_on[0] != loop_thread
