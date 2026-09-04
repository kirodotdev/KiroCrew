"""The backend mounts its three routes on the gateway aiohttp app."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web

from kiro_crew.apps.builtins.design_critique import register_routes
from kiro_crew.apps.builtins.design_critique.backend import routes


def test_register_routes_mounts_the_three_endpoints() -> None:
    app = web.Application()
    register_routes(app)
    mounted = {
        (r.method, r.resource.canonical) for r in app.router.routes() if r.resource is not None
    }
    assert ("GET", "/api/apps/design-critique/method") in mounted
    assert ("POST", "/api/apps/design-critique/discover") in mounted
    assert ("POST", "/api/apps/design-critique/render") in mounted


def test_only_http_urls_are_renderable() -> None:
    assert routes._is_http_url("https://example.com")
    assert routes._is_http_url("http://localhost:3000")
    # A file:// URL would turn the renderer into a local-file read primitive.
    assert not routes._is_http_url("file:///etc/passwd")
    assert not routes._is_http_url("ftp://host/x")


@pytest.mark.asyncio
async def test_read_capped_truncates_and_flags_overflow() -> None:
    async def go() -> tuple[bytes, bool]:
        reader = asyncio.StreamReader()
        reader.feed_data(b"x" * 100)
        reader.feed_eof()
        return await routes._read_capped(reader, 10)

    data, over = await go()
    assert over is True
    assert len(data) == 10


@pytest.mark.asyncio
async def test_read_capped_small_output_not_flagged() -> None:
    async def go() -> tuple[bytes, bool]:
        reader = asyncio.StreamReader()
        reader.feed_data(b"hello")
        reader.feed_eof()
        return await routes._read_capped(reader, 1024)

    data, over = await go()
    assert over is False
    assert data == b"hello"


def test_resolve_vetted_returns_ips_and_blocks_internal() -> None:
    run = asyncio.run
    # Loopback is allowed for url-preview and the vetted IP is returned for pinning.
    assert "127.0.0.1" in (run(routes._resolve_vetted("http://127.0.0.1:3000/")) or [])
    # A clone (allow_loopback=False) refuses loopback; internal ranges refused too.
    assert run(routes._resolve_vetted("http://127.0.0.1/", allow_loopback=False)) is None
    assert run(routes._resolve_vetted("http://10.0.0.5/")) is None
    # Carrier-grade NAT is not is_private but must still be refused.
    assert run(routes._resolve_vetted("http://100.64.0.5/")) is None
    # Deprecated IPv6 site-local (fec0::/10) reports is_global True but is
    # internal — the allowlist invariant must still refuse it.
    assert run(routes._resolve_vetted("http://[fec0::1]/")) is None
    # A genuinely global IPv6 host is allowed.
    assert run(routes._resolve_vetted("http://[2606:2800:220:1:248:1893:25c8:1946]/")) is not None


def test_resolve_vetted_loopback_only_for_typed_loopback(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    run = asyncio.run

    def fake_gai(host, port, *a, **k):  # type: ignore[no-untyped-def]
        return [(2, 1, 6, "", ("127.0.0.1", port))]

    monkeypatch.setattr(routes.socket, "getaddrinfo", fake_gai)
    # A TYPED loopback host is allowed (the localhost preview).
    assert run(routes._resolve_vetted("http://localhost:3000/")) == ["127.0.0.1"]
    # An arbitrary hostname that merely RESOLVES to loopback is refused, so an
    # attacker name cannot front a localhost service.
    assert run(routes._resolve_vetted("http://evil.example/")) is None


def test_sweep_removes_probe_and_clones_keeps_render(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(routes, "_uploads_dir", lambda: tmp_path)
    aged = time.time() - routes._CLONE_TTL_SEC - 60
    for name in ("dc-probe-old", "dc-clones/repo-old", "dc-render-old"):
        d = tmp_path / name
        d.mkdir(parents=True, exist_ok=True)
        os.utime(d, (aged, aged))
    routes._sweep_clones()
    assert not (tmp_path / "dc-probe-old").exists()
    assert not (tmp_path / "dc-clones" / "repo-old").exists()
    # dc-render-* is referenced by saved critique history — must NOT be swept.
    assert (tmp_path / "dc-render-old").exists()


@pytest.mark.asyncio
async def test_malformed_ipv6_url_refused_not_crash() -> None:
    # A bad IPv6 authority makes urlparse raise ValueError; the guard must refuse
    # (return False) rather than let the exception crash discovery.
    for bad in ("http://[::1", "http://[gggg::]/", "http://[::1]:notaport/"):
        assert await routes._url_target_allowed(bad) is False


@pytest.mark.asyncio
async def test_clone_rejects_loopback_url() -> None:
    # A repo clone has no localhost-preview use, so loopback is refused there.
    ok = await routes._url_target_allowed("http://127.0.0.1:8080/repo.git", allow_loopback=False)
    assert ok is False


@pytest.mark.asyncio
async def test_url_allows_loopback_for_preview() -> None:
    ok = await routes._url_target_allowed("http://127.0.0.1:5173/", allow_loopback=True)
    assert ok is True


def test_script_env_pins_path_and_disables_git_prompt() -> None:
    env = routes._script_env()
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    node = routes._tool("node")
    if node:
        # PATH is pinned to the resolved toolchain dir, not the ambient PATH.
        assert os.path.dirname(node) in env["PATH"].split(os.pathsep)


def test_credential_dirs_are_refused() -> None:
    assert routes._is_sensitive_dir(Path.home() / ".ssh")
    # Plain credential dot-dirs the is_sensitive_path floor does not enumerate.
    assert routes._is_sensitive_dir(Path.home() / ".gnupg")
    assert routes._is_sensitive_dir(Path("/Users/x/.docker/buildx"))
    # A normal project folder is not refused (intended product behaviour).
    assert not routes._is_sensitive_dir(Path("/Users/x/Developer/myapp"))


class _Req:
    """Minimal stand-in exposing the one method the handler awaits."""

    def __init__(self, payload: object) -> None:
        self._payload = payload

    async def json(self) -> object:
        return self._payload


@pytest.mark.asyncio
async def test_render_rejects_non_object_picks() -> None:
    # {"picks": [null]} must not reach .get() on a non-dict and 500.
    resp = await routes._handle_render(
        _Req({"kind": "local", "value": "/tmp", "picks": [None]})  # type: ignore[arg-type]
    )
    assert resp.status == 400


@pytest.mark.asyncio
async def test_render_rejects_overlong_field(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # An overlong ref would raise OSError at the filesystem/exec layer (HTTP 500);
    # the handler must refuse it with 400 up front.
    monkeypatch.setattr(routes, "_node", lambda: "/usr/bin/node")
    resp = await routes._handle_render(
        _Req(
            {
                "kind": "url",
                "value": "https://example.com",
                "picks": [{"ref": "/" + "a" * 5000, "label": "x"}],
            }
        )  # type: ignore[arg-type]
    )
    assert resp.status == 400
    assert isinstance(resp.body, bytes) and b"field_too_long" in resp.body


@pytest.mark.asyncio
async def test_render_rejects_too_many_picks(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(routes, "_node", lambda: "/usr/bin/node")
    resp = await routes._handle_render(
        _Req(
            {
                "kind": "url",
                "value": "https://example.com",
                "picks": [{"ref": "/", "label": "x"}] * (routes._MAX_PICKS + 1),
            }
        )  # type: ignore[arg-type]
    )
    assert resp.status == 400
    assert isinstance(resp.body, bytes) and b"too_many_picks" in resp.body


@pytest.mark.asyncio
async def test_render_rejects_nul_in_ref(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # A NUL in a posted ref would make create_subprocess_exec raise ValueError
    # (HTTP 500); the handler must refuse it up front with 400.
    monkeypatch.setattr(routes, "_node", lambda: "/usr/bin/node")
    resp = await routes._handle_render(
        _Req(
            {
                "kind": "url",
                "value": "https://example.com",
                "picks": [{"ref": "/\x00", "label": "x"}],
            }
        )  # type: ignore[arg-type]
    )
    assert resp.status == 400
    assert isinstance(resp.body, bytes) and b"bad_ref" in resp.body


@pytest.mark.asyncio
async def test_render_rejects_repo_handle_escape(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # A crafted "../.." handle must not let render escape the clones dir.
    monkeypatch.setattr(routes, "_node", lambda: "/usr/bin/node")
    resp = await routes._handle_render(
        _Req({"kind": "repo", "handle": "../../etc", "picks": [{"id": "a", "label": "A"}]})  # type: ignore[arg-type]
    )
    assert resp.status == 400
    assert isinstance(resp.body, bytes) and b"bad_handle" in resp.body


def test_url_target_allows_loopback_blocks_internal() -> None:
    # Loopback is the advertised localhost-preview target; internal ranges and the
    # cloud-metadata endpoint are blocked; public and file:// are handled too.
    run = asyncio.run
    assert run(routes._url_target_allowed("http://127.0.0.1:3000/"))
    assert run(routes._url_target_allowed("https://93.184.216.34/"))
    assert not run(routes._url_target_allowed("http://169.254.169.254/"))
    assert not run(routes._url_target_allowed("http://10.0.0.5/"))
    assert not run(routes._url_target_allowed("file:///etc/passwd"))
    # A malformed authority (bad port) must be refused, not raise.
    assert not run(routes._url_target_allowed("http://host:notaport/"))


@pytest.mark.asyncio
async def test_discover_repo_rejects_non_http_url() -> None:
    # The git remote-helper RCE vector (`ext::sh -c …`) is refused before git runs.
    resp = await routes._handle_discover(_Req({"kind": "repo", "value": "ext::sh -c id"}))  # type: ignore[arg-type]
    assert resp.status == 200
    assert isinstance(resp.body, bytes) and b"no-access" in resp.body


class _QReq:
    """Minimal stand-in exposing the .query mapping the GET status handler reads."""

    def __init__(self, job_id: str) -> None:
        self.query = {"job": job_id}


async def _drain(job_id: str) -> dict[str, Any] | None:
    # Poll the in-memory record until the detached task finishes (or give up).
    for _ in range(400):
        rec = routes._get_job(job_id)
        if rec and rec["status"] != "running":
            return rec
        await asyncio.sleep(0.005)
    return routes._get_job(job_id)


def _bump(path: Path, seconds: float = 10.0) -> None:
    # Move a path's mtime forward by an explicit amount instead of relying on the
    # wall clock: two writes in quick succession can land on the same timestamp on a
    # filesystem with coarse mtime granularity, which would make any assertion about
    # a changed mtime timing-dependent.
    stamp = os.stat(path).st_mtime + seconds
    os.utime(path, (stamp, stamp))


def _reset_probe_cache(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # Isolate BOTH module-level probe maps so a test neither leaks entries nor inherits
    # another test's ordering stamp — a leftover stamp for a shared handle would make
    # _probe_put refuse a later test's write, which is order-dependent and invisible.
    monkeypatch.setattr(routes, "_PROBE_CACHE", {})
    monkeypatch.setattr(routes, "_PROBE_STARTS", {})


def _cache_probe(handle: str, probe_dir: Path, proj: Path, route_pngs: dict[str, str]) -> Path:
    # Cache a retained probe the way /discover does, and return the build dir the
    # capture served. The staleness token is taken over THAT dir, not over the
    # project root, because the build output is what a reused PNG depicts.
    build = proj / "dist"
    build.mkdir(parents=True, exist_ok=True)
    (build / "index.html").write_text("<html></html>", encoding="utf-8")
    token = routes._served_signature(build)
    assert token is not None
    routes._probe_put(
        handle, routes._probe_claim(handle), str(probe_dir), route_pngs, str(build), token.digest
    )
    return build


@pytest.mark.asyncio
async def test_job_registry_start_returns_id_and_poll_returns_result() -> None:
    async def work_ok() -> dict[str, Any]:
        return {"screens": [1, 2]}

    ok = await _drain(routes._start_job(work_ok))

    async def work_bad() -> dict[str, Any]:
        raise RuntimeError("boom")

    bad = await _drain(routes._start_job(work_bad))

    assert ok is not None and ok["status"] == "done" and ok["result"] == {"screens": [1, 2]}
    # A failing job records status=error with the message, never crashes the poll.
    assert bad is not None and bad["status"] == "error" and "boom" in (bad["error"] or "")


@pytest.mark.asyncio
async def test_job_status_unknown_id_is_404() -> None:
    resp = await routes._handle_job_status(_QReq("does-not-exist"))  # type: ignore[arg-type]
    assert resp.status == 404
    assert isinstance(resp.body, bytes) and b"unknown_job" in resp.body


@pytest.mark.asyncio
async def test_discover_missing_field_is_400_synchronously() -> None:
    # Obvious bad input is rejected with 400 before any job starts.
    resp = await routes._handle_discover(_Req({"kind": "repo"}))  # type: ignore[arg-type]
    assert resp.status == 400
    assert isinstance(resp.body, bytes) and b"missing_field" in resp.body


@pytest.mark.asyncio
async def test_render_starts_a_job_then_poll_returns_the_screens(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    # The POST returns {job} synchronously (no capture inline); the detached job
    # runs the vetted command and the GET poll returns its result.
    monkeypatch.setattr(routes, "_uploads_dir", lambda: tmp_path)
    monkeypatch.setattr(routes, "_node", lambda: "/usr/bin/node")

    async def fake_resolve(u, allow_loopback=True):  # type: ignore[no-untyped-def]
        return ["93.184.216.34"]

    async def fake_run(cmd, timeout, env=None):  # type: ignore[no-untyped-def]
        # capture-site.mjs emits one JSON line per screen.
        return (0, '{"ok": true, "file": "/x/page.png", "label": "Page"}', "")

    monkeypatch.setattr(routes, "_resolve_vetted", fake_resolve)
    monkeypatch.setattr(routes, "_run", fake_run)

    resp = await routes._handle_render(
        _Req(
            {
                "kind": "url",
                "value": "https://example.com",
                "picks": [{"ref": "/", "label": "Page"}],
            }
        )  # type: ignore[arg-type]
    )
    assert resp.status == 200
    assert isinstance(resp.body, bytes)
    started = json.loads(resp.body)
    assert "job" in started
    rec = await _drain(started["job"])
    assert rec is not None
    assert rec["status"] == "done"
    assert rec["result"]["screens"][0]["path"] == "/x/page.png"


@pytest.mark.asyncio
async def test_render_starts_a_job_even_when_bad_input_would_fail_later(
    monkeypatch, tmp_path  # type: ignore[no-untyped-def]
) -> None:
    # A bad ref (NUL) is still rejected 400 synchronously — the job path does not
    # swallow the input validation.
    monkeypatch.setattr(routes, "_node", lambda: "/usr/bin/node")
    resp = await routes._handle_render(
        _Req(
            {
                "kind": "url",
                "value": "https://example.com",
                "picks": [{"ref": "/\x00", "label": "x"}],
            }
        )  # type: ignore[arg-type]
    )
    assert resp.status == 400
    assert isinstance(resp.body, bytes) and b"bad_ref" in resp.body


# ── _run: the sandboxed subprocess wrapper ──


class _FakeProc:
    """Stands in for the child returned by create_subprocess_limited."""

    def __init__(self, out: bytes = b"", err: bytes = b"", rc: int = 0, hang: bool = False) -> None:
        self.returncode = rc
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        if not hang:
            self.stdout.feed_data(out)
            self.stdout.feed_eof()
            self.stderr.feed_data(err)
            self.stderr.feed_eof()
        # hang=True leaves both pipes without EOF so reads block until wait_for times out.
        self.waited = False

    async def wait(self) -> int:
        self.waited = True
        return self.returncode


def _patch_run_sandbox(monkeypatch, proc: _FakeProc, cleanup: str | None = None) -> dict:  # type: ignore[no-untyped-def]
    calls = {"killed": 0}

    async def fake_spawn(cmd, mode="standard", env=None, _prepare=None):  # type: ignore[no-untyped-def]
        return list(cmd), dict(env or {}), cleanup

    async def fake_limited(*args, **kwargs):  # type: ignore[no-untyped-def]
        return proc

    async def fake_kill(_proc):  # type: ignore[no-untyped-def]
        calls["killed"] += 1

    monkeypatch.setattr(routes.sandbox, "sandboxed_spawn_argv_async", fake_spawn)
    monkeypatch.setattr(routes.sandbox, "create_subprocess_limited", fake_limited)
    monkeypatch.setattr(routes.platform_compat, "kill_and_reap", fake_kill)
    return calls


@pytest.mark.asyncio
async def test_run_returns_rc_stdout_stderr(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    proc = _FakeProc(out=b"hello-out", err=b"warn-err", rc=0)
    calls = _patch_run_sandbox(monkeypatch, proc)
    rc, out, err = await routes._run(["/usr/bin/node", "x"], timeout=5)
    assert rc == 0
    assert out == "hello-out"
    assert err == "warn-err"
    assert proc.waited is True
    assert calls["killed"] == 0


@pytest.mark.asyncio
async def test_run_kills_child_on_output_overflow(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(routes, "_MAX_OUTPUT_BYTES", 4)
    proc = _FakeProc(out=b"x" * 50, err=b"", rc=0)
    calls = _patch_run_sandbox(monkeypatch, proc)
    rc, out, err = await routes._run(["/usr/bin/node", "x"], timeout=5)
    # Overflow path kills the tree and does not wait().
    assert calls["killed"] == 1
    assert proc.waited is False
    assert len(out) == 4


@pytest.mark.asyncio
async def test_run_kills_and_reraises_on_timeout_and_unlinks_cleanup(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    launcher = tmp_path / "launcher.tmp"
    launcher.write_text("x", encoding="utf-8")
    proc = _FakeProc(hang=True)
    calls = _patch_run_sandbox(monkeypatch, proc, cleanup=str(launcher))
    with pytest.raises(asyncio.TimeoutError):
        await routes._run(["/usr/bin/node", "x"], timeout=0)
    assert calls["killed"] == 1
    # The finally block unlinks the materialized launcher even on the timeout path.
    assert not launcher.exists()


# ── GET /method ──


@pytest.mark.asyncio
async def test_handle_method_returns_checklist(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    (tmp_path / "frameworks").mkdir()
    (tmp_path / "frameworks" / "main-checklist.md").write_text("# rubric", encoding="utf-8")
    monkeypatch.setattr(routes, "_SKILL_DIR", tmp_path)
    resp = await routes._handle_method(_Req({}))  # type: ignore[arg-type]
    assert resp.status == 200
    assert isinstance(resp.body, bytes) and b"rubric" in resp.body


@pytest.mark.asyncio
async def test_handle_method_missing_files_is_500(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(routes, "_SKILL_DIR", tmp_path / "does-not-exist")
    resp = await routes._handle_method(_Req({}))  # type: ignore[arg-type]
    assert resp.status == 500
    assert isinstance(resp.body, bytes) and b"method_missing" in resp.body


# ── _discover_from_dir ──


@pytest.mark.asyncio
async def test_discover_from_dir_no_node(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(routes, "_node", lambda: None)
    out = await routes._discover_from_dir(Path("/tmp/proj"), handle="h1")
    assert out["blocked"]["reason"] == "other"
    assert "node is not installed" in out["blocked"]["detail"]
    assert out["screens"] == [] and out["handle"] == "h1"


@pytest.mark.asyncio
async def test_discover_from_dir_builds_screens_and_flows(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(routes, "_node", lambda: "/usr/bin/node")
    monkeypatch.setattr(routes, "_uploads_dir", lambda: tmp_path)

    async def fake_run(cmd, timeout, env=None):  # type: ignore[no-untyped-def]
        if any("discover-routes" in c for c in cmd):
            return (
                0,
                json.dumps(
                    {
                        "framework": "React",
                        "routing": "file",
                        "routes": [
                            {"path": "/"},
                            {"path": "/about"},
                            {"path": "/about/team"},
                            {"path": ""},
                        ],
                        "notes": ["a note"],
                    }
                ),
                "",
            )
        # capture-build probe: one screen renders; a gate blocks the rest.
        return (
            0,
            json.dumps(
                {
                    "screens": [{"route": "/"}],
                    "blockedBy": {"onScreens": 2, "ofScreens": 3, "likely": "login"},
                    "buildDir": None,
                    "notes": ["no build dir"],
                }
            ),
            "",
        )

    monkeypatch.setattr(routes, "_run", fake_run)
    out = await routes._discover_from_dir(tmp_path / "proj", handle="clone1")
    assert out["framework"] == "React"
    assert out["note"] == "a note"
    ids = [s["id"] for s in out["screens"]]
    assert any(i.endswith("-about") or "about" in i for i in ids)
    # "/" probed True; the others default canSee False.
    home = next(s for s in out["screens"] if s["ref"] == "/")
    assert home["canSee"] is True
    # about + about/team share the top-level group -> one guessed flow.
    assert any(f["label"] == "about" and f["basis"] == "guess" for f in out["flows"])
    assert any("blocked by a login" in c for c in out["cannotSee"])
    assert "no build dir" in out["cannotSee"]


@pytest.mark.asyncio
async def test_discover_from_dir_bad_discover_json_no_routes(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(routes, "_node", lambda: "/usr/bin/node")
    monkeypatch.setattr(routes, "_uploads_dir", lambda: tmp_path)

    async def fake_run(cmd, timeout, env=None):  # type: ignore[no-untyped-def]
        return (0, "not json", "boom")

    monkeypatch.setattr(routes, "_run", fake_run)
    out = await routes._discover_from_dir(tmp_path / "proj", handle="h")
    # Bad JSON -> default disc with no routes -> no probe, empty screens.
    assert out["screens"] == [] and out["flows"] == []


# ── _discover_repo_job ──


@pytest.mark.asyncio
async def test_discover_repo_job_clone_ok_delegates(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(routes, "_uploads_dir", lambda: tmp_path)

    async def fake_run(cmd, timeout, env=None):  # type: ignore[no-untyped-def]
        # The last argv entry is the clone target dir; create it so exists() passes.
        os.makedirs(cmd[-1], exist_ok=True)
        return (0, "", "")

    async def fake_discover(directory, handle):  # type: ignore[no-untyped-def]
        return {"screens": [{"id": "00-x"}], "handle": handle}

    monkeypatch.setattr(routes, "_run", fake_run)
    monkeypatch.setattr(routes, "_discover_from_dir", fake_discover)
    out = await routes._discover_repo_job(
        "https://example.com/r.git", ["93.184.216.34"], "/usr/bin/git"
    )
    assert out["screens"] == [{"id": "00-x"}]


@pytest.mark.asyncio
async def test_discover_repo_job_clone_failure_blocked(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(routes, "_uploads_dir", lambda: tmp_path)

    async def fake_run(cmd, timeout, env=None):  # type: ignore[no-untyped-def]
        return (128, "", "fatal: repository not found")

    monkeypatch.setattr(routes, "_run", fake_run)
    out = await routes._discover_repo_job(
        "https://example.com/r.git", ["93.184.216.34"], "/usr/bin/git"
    )
    assert out["blocked"]["reason"] == "no-access"
    assert "repository not found" in out["blocked"]["detail"]


# ── POST /discover: every kind branch ──


@pytest.mark.asyncio
async def test_discover_invalid_json_body(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(routes, "_uploads_dir", lambda: tmp_path)
    resp = await routes._handle_discover(_Req("not a dict"))  # type: ignore[arg-type]
    assert resp.status == 400
    assert isinstance(resp.body, bytes) and b"body_not_object" in resp.body


@pytest.mark.asyncio
async def test_discover_value_too_long(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(routes, "_uploads_dir", lambda: tmp_path)
    resp = await routes._handle_discover(
        _Req({"kind": "repo", "value": "http://x/" + "a" * 5000})  # type: ignore[arg-type]
    )
    assert resp.status == 400
    assert isinstance(resp.body, bytes) and b"field_too_long" in resp.body


@pytest.mark.asyncio
async def test_discover_figma_is_routed_to_screenshots(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(routes, "_uploads_dir", lambda: tmp_path)
    resp = await routes._handle_discover(_Req({"kind": "figma", "value": "file123"}))  # type: ignore[arg-type]
    assert resp.status == 200
    assert isinstance(resp.body, bytes) and b"figma-export-needed" in resp.body


@pytest.mark.asyncio
async def test_discover_repo_starts_job(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(routes, "_uploads_dir", lambda: tmp_path)

    async def fake_resolve(u, allow_loopback=True):  # type: ignore[no-untyped-def]
        return ["93.184.216.34"]

    monkeypatch.setattr(routes, "_resolve_vetted", fake_resolve)
    monkeypatch.setattr(routes, "_tool", lambda name: "/usr/bin/git")
    monkeypatch.setattr(routes, "_start_job", lambda work: "job-repo")
    resp = await routes._handle_discover(
        _Req({"kind": "repo", "value": "https://example.com/r.git"})  # type: ignore[arg-type]
    )
    assert resp.status == 200
    assert isinstance(resp.body, bytes)
    assert json.loads(resp.body)["job"] == "job-repo"


@pytest.mark.asyncio
async def test_discover_repo_internal_host_blocked(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(routes, "_uploads_dir", lambda: tmp_path)

    async def fake_resolve(u, allow_loopback=True):  # type: ignore[no-untyped-def]
        return None

    monkeypatch.setattr(routes, "_resolve_vetted", fake_resolve)
    resp = await routes._handle_discover(
        _Req({"kind": "repo", "value": "https://10.0.0.1/r.git"})  # type: ignore[arg-type]
    )
    assert resp.status == 200
    assert isinstance(resp.body, bytes) and b"public host" in resp.body


@pytest.mark.asyncio
async def test_discover_repo_git_unavailable(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(routes, "_uploads_dir", lambda: tmp_path)

    async def fake_resolve(u, allow_loopback=True):  # type: ignore[no-untyped-def]
        return ["93.184.216.34"]

    monkeypatch.setattr(routes, "_resolve_vetted", fake_resolve)
    monkeypatch.setattr(routes, "_tool", lambda name: None)
    resp = await routes._handle_discover(
        _Req({"kind": "repo", "value": "https://example.com/r.git"})  # type: ignore[arg-type]
    )
    assert resp.status == 200
    assert isinstance(resp.body, bytes) and b"git is not available" in resp.body


@pytest.mark.asyncio
async def test_discover_local_protected_path(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(routes, "_uploads_dir", lambda: tmp_path)
    resp = await routes._handle_discover(
        _Req({"kind": "local", "value": str(Path.home() / ".ssh")})  # type: ignore[arg-type]
    )
    assert resp.status == 200
    assert isinstance(resp.body, bytes) and b"protected" in resp.body


@pytest.mark.asyncio
async def test_discover_local_not_found(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(routes, "_uploads_dir", lambda: tmp_path)
    resp = await routes._handle_discover(
        _Req({"kind": "local", "value": str(tmp_path / "nope")})  # type: ignore[arg-type]
    )
    assert resp.status == 200
    assert isinstance(resp.body, bytes) and b"not-found" in resp.body


@pytest.mark.asyncio
async def test_discover_local_exists_starts_job(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(routes, "_uploads_dir", lambda: tmp_path)
    monkeypatch.setattr(routes, "_start_job", lambda work: "job-local")
    proj = tmp_path / "proj"
    proj.mkdir()
    resp = await routes._handle_discover(_Req({"kind": "local", "value": str(proj)}))  # type: ignore[arg-type]
    assert resp.status == 200
    assert isinstance(resp.body, bytes)
    assert json.loads(resp.body)["job"] == "job-local"


@pytest.mark.asyncio
async def test_discover_url_allowed_returns_one_screen(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(routes, "_uploads_dir", lambda: tmp_path)

    async def fake_allowed(u, allow_loopback=True):  # type: ignore[no-untyped-def]
        return True

    monkeypatch.setattr(routes, "_url_target_allowed", fake_allowed)
    resp = await routes._handle_discover(
        _Req({"kind": "url", "value": "https://example.com"})  # type: ignore[arg-type]
    )
    assert resp.status == 200
    assert isinstance(resp.body, bytes)
    body = json.loads(resp.body)
    assert body["screens"][0]["id"] == "page"
    assert body["handle"] == "url:https://example.com"


@pytest.mark.asyncio
async def test_discover_url_internal_blocked(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(routes, "_uploads_dir", lambda: tmp_path)

    async def fake_allowed(u, allow_loopback=True):  # type: ignore[no-untyped-def]
        return False

    monkeypatch.setattr(routes, "_url_target_allowed", fake_allowed)
    resp = await routes._handle_discover(
        _Req({"kind": "url", "value": "http://10.0.0.1"})  # type: ignore[arg-type]
    )
    assert resp.status == 200
    assert isinstance(resp.body, bytes) and b"internal/private host" in resp.body


@pytest.mark.asyncio
async def test_discover_unknown_kind(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(routes, "_uploads_dir", lambda: tmp_path)
    resp = await routes._handle_discover(_Req({"kind": "zip", "value": "x"}))  # type: ignore[arg-type]
    assert resp.status == 400
    assert isinstance(resp.body, bytes) and b"bad_kind" in resp.body


# ── _render_capture_job ──


@pytest.mark.asyncio
async def test_render_capture_job_repo_shapes_screens(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    out_dir = tmp_path / "dc-render-1"
    out_dir.mkdir()

    async def fake_run(cmd, timeout, env=None):  # type: ignore[no-untyped-def]
        return (
            0,
            json.dumps(
                {
                    "screens": [{"route": "/", "path": "/x/home.png"}],
                    "blockedBy": {"onScreens": 1},
                }
            ),
            "",
        )

    monkeypatch.setattr(routes, "_run", fake_run)
    res = await routes._render_capture_job(
        "repo", ["node", "cap"], out_dir, ["/", "/missing"], ["Home", "Missing"]
    )
    assert res["screens"][0]["path"] == "/x/home.png"
    assert res["screens"][0]["step"] == 1
    # ref not returned by the capture -> couldNotSee; gate note also appended.
    assert "Missing" in res["couldNotSee"]
    assert any("login or consent gate" in c for c in res["couldNotSee"])
    # A successful screen means the dir is kept (referenced by history).
    assert out_dir.exists()


@pytest.mark.asyncio
async def test_render_capture_job_repo_bad_json_raises_and_cleans(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    out_dir = tmp_path / "dc-render-2"
    out_dir.mkdir()

    async def fake_run(cmd, timeout, env=None):  # type: ignore[no-untyped-def]
        return (0, "not json", "")

    monkeypatch.setattr(routes, "_run", fake_run)
    with pytest.raises(RuntimeError):
        await routes._render_capture_job("local", ["node"], out_dir, ["/"], ["Home"])
    # No screens produced -> the finally block drops the throwaway dir.
    assert not out_dir.exists()


@pytest.mark.asyncio
async def test_render_capture_job_url_parses_lines_and_cleans_on_empty(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    out_dir = tmp_path / "dc-render-3"
    out_dir.mkdir()

    async def fake_run(cmd, timeout, env=None):  # type: ignore[no-untyped-def]
        # One blank line, one non-JSON line, one failed record -> no screens.
        return (0, "\nnot-json\n" + json.dumps({"ok": False, "label": "Broken"}), "")

    monkeypatch.setattr(routes, "_run", fake_run)
    res = await routes._render_capture_job("url", ["node"], out_dir, ["/"], ["Page"])
    assert res["screens"] == []
    assert "Broken" in res["couldNotSee"]
    assert not out_dir.exists()


# ── POST /render: remaining branches ──


@pytest.mark.asyncio
async def test_render_missing_picks(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    resp = await routes._handle_render(_Req({"kind": "url", "value": "https://example.com"}))  # type: ignore[arg-type]
    assert resp.status == 400
    assert isinstance(resp.body, bytes) and b"missing_picks" in resp.body


@pytest.mark.asyncio
async def test_render_node_missing(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(routes, "_node", lambda: None)
    resp = await routes._handle_render(
        _Req({"kind": "url", "value": "https://example.com", "picks": [{"ref": "/", "label": "x"}]})  # type: ignore[arg-type]
    )
    assert resp.status == 400
    assert isinstance(resp.body, bytes) and b"node_missing" in resp.body


@pytest.mark.asyncio
async def test_render_local_starts_job(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(routes, "_node", lambda: "/usr/bin/node")
    monkeypatch.setattr(routes, "_uploads_dir", lambda: tmp_path)
    monkeypatch.setattr(routes, "_start_job", lambda work: "job-r-local")
    proj = tmp_path / "proj"
    proj.mkdir()
    resp = await routes._handle_render(
        _Req(
            {
                "kind": "local",
                "value": str(proj),
                "handle": f"local:{proj}",
                "picks": [{"ref": "/", "label": "Home"}],
            }
        )  # type: ignore[arg-type]
    )
    assert resp.status == 200
    assert isinstance(resp.body, bytes)
    assert json.loads(resp.body)["job"] == "job-r-local"


@pytest.mark.asyncio
async def test_render_local_protected_path(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(routes, "_node", lambda: "/usr/bin/node")
    monkeypatch.setattr(routes, "_uploads_dir", lambda: tmp_path)
    resp = await routes._handle_render(
        _Req(
            {
                "kind": "local",
                "value": str(Path.home() / ".aws"),
                "picks": [{"ref": "/", "label": "x"}],
            }
        )  # type: ignore[arg-type]
    )
    assert resp.status == 400
    assert isinstance(resp.body, bytes) and b"protected_path" in resp.body


@pytest.mark.asyncio
async def test_render_local_handle_expired(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(routes, "_node", lambda: "/usr/bin/node")
    monkeypatch.setattr(routes, "_uploads_dir", lambda: tmp_path)
    resp = await routes._handle_render(
        _Req(
            {
                "kind": "local",
                "value": str(tmp_path / "gone"),
                "picks": [{"ref": "/", "label": "x"}],
            }
        )  # type: ignore[arg-type]
    )
    assert resp.status == 400
    assert isinstance(resp.body, bytes) and b"handle_expired" in resp.body


@pytest.mark.asyncio
async def test_render_repo_handle_ok_starts_job(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(routes, "_node", lambda: "/usr/bin/node")
    monkeypatch.setattr(routes, "_uploads_dir", lambda: tmp_path)
    monkeypatch.setattr(routes, "_start_job", lambda work: "job-r-repo")
    (tmp_path / "dc-clones" / "clone42").mkdir(parents=True)
    resp = await routes._handle_render(
        _Req(
            {
                "kind": "repo",
                "handle": "clone42",
                "picks": [{"ref": "/", "label": "Home"}],
            }
        )  # type: ignore[arg-type]
    )
    assert resp.status == 200
    assert isinstance(resp.body, bytes)
    assert json.loads(resp.body)["job"] == "job-r-repo"


@pytest.mark.asyncio
async def test_render_url_full_url_pick_and_resolve(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(routes, "_node", lambda: "/usr/bin/node")
    monkeypatch.setattr(routes, "_uploads_dir", lambda: tmp_path)
    monkeypatch.setattr(routes, "_start_job", lambda work: "job-r-url")

    async def fake_resolve(u, allow_loopback=True):  # type: ignore[no-untyped-def]
        return ["93.184.216.34"]

    monkeypatch.setattr(routes, "_resolve_vetted", fake_resolve)
    resp = await routes._handle_render(
        _Req(
            {
                "kind": "url",
                "value": "",
                "picks": [{"ref": "https://other.example/x", "label": "X"}],
            }
        )  # type: ignore[arg-type]
    )
    assert resp.status == 200
    assert isinstance(resp.body, bytes)
    assert json.loads(resp.body)["job"] == "job-r-url"


@pytest.mark.asyncio
async def test_render_url_internal_blocked(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(routes, "_node", lambda: "/usr/bin/node")
    monkeypatch.setattr(routes, "_uploads_dir", lambda: tmp_path)

    async def fake_resolve(u, allow_loopback=True):  # type: ignore[no-untyped-def]
        return None

    monkeypatch.setattr(routes, "_resolve_vetted", fake_resolve)
    resp = await routes._handle_render(
        _Req(
            {
                "kind": "url",
                "value": "http://10.0.0.1",
                "picks": [{"ref": "/", "label": "x"}],
            }
        )  # type: ignore[arg-type]
    )
    assert resp.status == 400
    assert isinstance(resp.body, bytes) and b"bad_url" in resp.body


@pytest.mark.asyncio
async def test_render_bad_kind(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(routes, "_node", lambda: "/usr/bin/node")
    resp = await routes._handle_render(
        _Req({"kind": "figma", "value": "x", "picks": [{"ref": "/", "label": "x"}]})  # type: ignore[arg-type]
    )
    assert resp.status == 400
    assert isinstance(resp.body, bytes) and b"bad_kind" in resp.body


# ── GET job status: done + error branches ──


@pytest.mark.asyncio
async def test_job_status_done_returns_result() -> None:
    async def work_ok() -> dict[str, Any]:
        return {"screens": ["a"]}

    rec = await _drain(routes._start_job(work_ok))
    assert rec is not None
    # find the job id again via a fresh status call
    # (start_job returns the id; re-run to grab it deterministically)
    job_id = routes._start_job(lambda: work_ok())
    await _drain(job_id)
    resp = await routes._handle_job_status(_QReq(job_id))  # type: ignore[arg-type]
    assert resp.status == 200
    assert isinstance(resp.body, bytes)
    assert json.loads(resp.body)["status"] == "done"


@pytest.mark.asyncio
async def test_job_status_error_returns_message() -> None:
    async def work_bad() -> dict[str, Any]:
        raise RuntimeError("kaboom")

    job_id = routes._start_job(work_bad)
    await _drain(job_id)
    resp = await routes._handle_job_status(_QReq(job_id))  # type: ignore[arg-type]
    assert resp.status == 200
    assert isinstance(resp.body, bytes)
    body = json.loads(resp.body)
    assert body["status"] == "error" and "kaboom" in body["error"]


# ── probe-PNG cache: reuse the discovery capture in /render ──


def test_probe_put_get_round_trip(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    # Isolate the module-level cache so the test does not leak entries.
    _reset_probe_cache(monkeypatch)
    pdir = tmp_path / "dc-probe-abc"
    pdir.mkdir()
    claim = routes._probe_claim("clone1")
    routes._probe_put("clone1", claim, str(pdir), {"/": "/x/home.png"}, "/proj/dist", "sig-a")
    rec = routes._probe_get("clone1")
    assert rec is not None
    assert rec["dir"] == str(pdir)
    assert rec["routes"] == {"/": "/x/home.png"}
    assert rec["build_dir"] == "/proj/dist"
    assert rec["served_signature"] == "sig-a"
    # A shallow copy is returned: mutating it must not corrupt the stored record.
    rec["routes"]["/"] = "tampered"
    again = routes._probe_get("clone1")
    assert again is not None and again["routes"]["/"] == "/x/home.png"
    # Unknown handle -> None; a falsy handle is never stored.
    assert routes._probe_get("nope") is None
    routes._probe_put("", 0, str(pdir), {"/": "/x/home.png"}, "/proj/dist", "sig-a")
    assert routes._probe_get("") is None


def test_probe_put_overwrite_drops_prior_dir(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    # A stable "local:<path>" handle re-discovered before TTL overwrites its entry.
    # The prior retained probe dir must be removed on overwrite so it is not
    # orphaned until the sweep, while the NEW dir is kept intact.
    _reset_probe_cache(monkeypatch)
    first = tmp_path / "dc-probe-first"
    first.mkdir()
    second = tmp_path / "dc-probe-second"
    second.mkdir()
    handle = "local:/some/project"
    # One claim shared by both writes, so this exercises _probe_put's own overwrite
    # path rather than the eviction _probe_claim would otherwise have done first: a
    # write carrying the CURRENT claim is not superseded and must install.
    claim = routes._probe_claim(handle)
    routes._probe_put(handle, claim, str(first), {"/": "/x/a.png"}, "/proj/dist", "sig-1")
    routes._probe_put(handle, claim, str(second), {"/": "/x/b.png"}, "/proj/dist", "sig-2")
    # Prior dir gone, new dir retained, cache points at the new record.
    assert not first.exists()
    assert second.exists()
    rec = routes._probe_get(handle)
    assert rec is not None and rec["dir"] == str(second)

    # Re-storing the SAME dir (unchanged) must NOT delete it (no self-destruct).
    routes._probe_put(handle, claim, str(second), {"/": "/x/b.png"}, "/proj/dist", "sig-3")
    assert second.exists()
    kept = routes._probe_get(handle)
    assert kept is not None and kept["served_signature"] == "sig-3"


def test_probe_get_expired_returns_none(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    _reset_probe_cache(monkeypatch)
    routes._probe_put(
        "clone1",
        routes._probe_claim("clone1"),
        str(tmp_path),
        {"/": "/x/home.png"},
        str(tmp_path),
        "sig-a",
    )
    # The reuse window is the cutoff, not the retained dir's longer _CLONE_TTL_SEC
    # lifetime: an entry whose dir is still on disk can already be too old to reuse.
    # Age it just past the reuse window but well inside the dir's TTL, and _probe_get
    # must still refuse it.
    assert routes._PROBE_REUSE_TTL_SEC < routes._CLONE_TTL_SEC
    routes._PROBE_CACHE["clone1"]["created_at"] = time.time() - routes._PROBE_REUSE_TTL_SEC - 60
    assert routes._probe_get("clone1") is None


def _build_tree(build: Path) -> None:
    (build / "assets").mkdir(parents=True)
    (build / "index.html").write_text("<html>v1</html>", encoding="utf-8")
    (build / "assets" / "app.js").write_text("v1", encoding="utf-8")


def test_served_signature_none_on_missing_and_moves_on_a_rebuild(tmp_path) -> None:
    # The token must cover the BUILD OUTPUT, because that is what a reused PNG
    # depicts: a rebuild rewrites index.html under an existing dist/, which leaves
    # the project root's own st_mtime untouched.
    assert routes._served_signature(tmp_path / "does-not-exist") is None
    proj = tmp_path / "proj"
    build = proj / "dist"
    _build_tree(build)
    sig = routes._served_signature(build)
    assert sig is not None and isinstance(sig.digest, str)

    root_before = os.stat(proj).st_mtime_ns
    (build / "index.html").write_text("<html>v2</html>", encoding="utf-8")
    _bump(build / "index.html")
    assert os.stat(proj).st_mtime_ns == root_before
    assert routes._served_signature(build) != sig


def test_served_signature_moves_on_a_nested_in_place_overwrite(tmp_path) -> None:
    # The hard case: a nested file overwritten under an UNCHANGED name. Neither the
    # build dir's own mtime nor its direct entries' mtimes move (asserted), so a
    # token that read only the top level would call this tree unchanged and /render
    # would adopt the pre-edit PNG. The digest covers every file, so it moves.
    build = tmp_path / "dist"
    _build_tree(build)
    sig = routes._served_signature(build)
    top_before = [os.stat(build).st_mtime_ns, os.stat(build / "assets").st_mtime_ns]

    (build / "assets" / "app.js").write_text("v2-different-length", encoding="utf-8")
    _bump(build / "assets" / "app.js")
    assert [os.stat(build).st_mtime_ns, os.stat(build / "assets").st_mtime_ns] == top_before
    assert routes._served_signature(build) != sig


def test_served_signature_moves_when_a_file_is_added_or_removed(tmp_path) -> None:
    build = tmp_path / "dist"
    _build_tree(build)
    sig = routes._served_signature(build)
    assert sig is not None
    (build / "assets" / "extra.js").write_text("x", encoding="utf-8")
    added = routes._served_signature(build)
    assert added is not None and added.digest != sig.digest
    (build / "assets" / "extra.js").unlink()
    # Removing it again restores the original DIGEST: it describes the tree, not the
    # history of edits to it. The digest is the only field asserted, for two reasons: it
    # is the only one /render compares, and newest_mtime_ns is a high-water mark that
    # also reads directory mtimes — so whether creating and removing extra.js leaves
    # assets/'s own mtime raised depends on the filesystem's timestamp granularity
    # versus how fast the test ran, which is not a property to assert either way.
    back = routes._served_signature(build)
    assert back is not None and back.digest == sig.digest


def test_served_signature_refuses_a_tree_it_cannot_read_in_full(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    # A token over PART of a tree is no evidence about the rest, so an over-bound
    # tree yields None (reuse refused) rather than a partial digest.
    monkeypatch.setattr(routes, "_SIGNATURE_MAX_FILES", 2)
    build = tmp_path / "dist"
    build.mkdir()
    for i in range(3):
        (build / f"f{i}.js").write_text("x", encoding="utf-8")
    assert routes._served_signature(build) is None
    monkeypatch.setattr(routes, "_SIGNATURE_MAX_FILES", 3)
    assert routes._served_signature(build) is not None


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="platform has no symlink")
def test_served_signature_ignores_what_the_server_will_not_serve(tmp_path) -> None:
    # capture-build.mjs's file index counts neither a symlinked dir nor a symlinked
    # file as one, and skips dot-entries, so none of them is reachable over the
    # preview server. The token must agree: signing them would only cause needless
    # re-captures when something the critic never saw changed.
    build = tmp_path / "dist"
    _build_tree(build)
    sig = routes._served_signature(build)
    assert sig is not None

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "extra.js").write_text("x", encoding="utf-8")
    try:
        (build / "linked").symlink_to(outside, target_is_directory=True)
        (build / "linked.js").symlink_to(outside / "extra.js")
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted here")
    (build / ".vite-manifest").write_text("x", encoding="utf-8")
    (build / ".cache").mkdir()
    (build / ".cache" / "x.js").write_text("x", encoding="utf-8")
    # The DIGEST is what /render compares, and it is the only field that can hold still
    # here: creating these entries moves dist/'s own mtime, and newest_mtime_ns reads
    # directory mtimes on purpose so a deletion cannot hide from the discover-time
    # mid-capture check.
    unserved = routes._served_signature(build)
    assert unserved is not None and unserved.digest == sig.digest

    # And a change behind the symlink is likewise invisible, because the server would
    # never have served it either.
    (outside / "extra.js").write_text("changed-and-longer", encoding="utf-8")
    _bump(outside / "extra.js")
    behind = routes._served_signature(build)
    assert behind is not None and behind.digest == sig.digest


def test_probe_build_dir_rejects_a_path_outside_the_project(tmp_path) -> None:
    # A manifest path is only ever stat()ed, but a token taken over an unrelated
    # tree would stand still and permit reuse of a stale PNG for the whole TTL.
    proj = tmp_path / "proj"
    proj.mkdir()
    assert routes._probe_build_dir(str(proj / "dist"), proj) == proj / "dist"
    assert routes._probe_build_dir(str(proj), proj) == proj
    assert routes._probe_build_dir(str(tmp_path / "elsewhere"), proj) is None
    assert routes._probe_build_dir("dist", proj) is None
    assert routes._probe_build_dir(None, proj) is None
    assert routes._probe_build_dir("", proj) is None
    assert routes._probe_build_dir(123, proj) is None


def test_probe_build_dir_matches_a_relative_project_dir(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    # capture-build.mjs resolves a relative project path against the cwd it inherits
    # from the gateway and reports an ABSOLUTE buildDir. Comparing that against the
    # relative path as given would never match and would silently disable reuse.
    monkeypatch.chdir(tmp_path)
    (tmp_path / "proj" / "dist").mkdir(parents=True)
    # Derive from the real cwd, not from tmp_path: on macOS tmp_path sits under a
    # symlinked /tmp, so the two spellings differ and a lexical check would not match.
    reported = str(Path(os.getcwd()) / "proj" / "dist")
    assert routes._probe_build_dir(reported, Path("proj")) == Path(reported)
    assert routes._probe_build_dir(reported, Path("other")) is None


def test_sweep_purges_probe_cache_entry_when_dir_swept(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(routes, "_uploads_dir", lambda: tmp_path)
    _reset_probe_cache(monkeypatch)
    aged = time.time() - routes._CLONE_TTL_SEC - 60
    probe_dir = tmp_path / "dc-probe-old"
    probe_dir.mkdir()
    os.utime(probe_dir, (aged, aged))
    # A cache entry pointing at the soon-to-be-swept dir must be purged too, so the
    # cache never hands /render a path for a directory that no longer exists.
    routes._probe_put(
        "clone-old",
        routes._probe_claim("clone-old"),
        str(probe_dir),
        {"/": str(probe_dir / "home.png")},
        str(tmp_path),
        "sig-a",
    )
    routes._sweep_clones()
    assert not probe_dir.exists()
    assert routes._probe_get("clone-old") is None


@pytest.mark.asyncio
async def test_render_all_covered_skips_capture_subprocess(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    # When the probe covered every pick, /render must NOT spawn the capture
    # subprocess and must return the reused PNG paths in pick order.
    monkeypatch.setattr(routes, "_node", lambda: "/usr/bin/node")
    monkeypatch.setattr(routes, "_uploads_dir", lambda: tmp_path)
    _reset_probe_cache(monkeypatch)
    proj = tmp_path / "dc-clones" / "clone42"
    proj.mkdir(parents=True)
    probe_dir = tmp_path / "dc-probe-x"
    probe_dir.mkdir()
    home_png = probe_dir / "home.png"
    about_png = probe_dir / "about.png"
    home_png.write_bytes(b"home-bytes")
    about_png.write_bytes(b"about-bytes")

    called = {"run": 0}

    async def fake_run(cmd, timeout, env=None):  # type: ignore[no-untyped-def]
        called["run"] += 1
        return (0, "{}", "")

    monkeypatch.setattr(routes, "_run", fake_run)
    # Cache keyed by the repo handle (clone id), token taken over the build output.
    _cache_probe(
        "clone42",
        probe_dir,
        proj.resolve(),
        {"/": str(home_png), "/about": str(about_png)},
    )

    resp = await routes._handle_render(
        _Req(
            {
                "kind": "repo",
                "handle": "clone42",
                "picks": [
                    {"ref": "/", "label": "Home"},
                    {"ref": "/about", "label": "About"},
                ],
            }
        )  # type: ignore[arg-type]
    )
    assert resp.status == 200
    assert isinstance(resp.body, bytes)
    rec = await _drain(json.loads(resp.body)["job"])
    assert rec is not None and rec["status"] == "done"
    screens = rec["result"]["screens"]
    assert [s["step"] for s in screens] == [1, 2]
    # The capture subprocess was never invoked.
    assert called["run"] == 0
    # Every returned path is an ADOPTED COPY inside the never-swept dc-render-* dir,
    # never the probe path itself: history keeps these paths forever while the
    # dc-probe-* dir is TTL-swept, so serving the probe path would lose the images.
    paths = [Path(s["path"]) for s in screens]
    assert [p.parent.name.startswith("dc-render-") for p in paths] == [True, True]
    assert {p.parent for p in paths} == {paths[0].parent}
    assert str(home_png) not in [str(p) for p in paths]
    # The copies carry the probe's bytes, in pick order.
    assert [p.read_bytes() for p in paths] == [b"home-bytes", b"about-bytes"]
    # Deleting the probe dir (a sweep, or a local re-discovery overwrite) leaves the
    # saved critique's screenshots intact.
    shutil.rmtree(probe_dir)
    assert all(p.exists() for p in paths)


@pytest.mark.asyncio
async def test_render_mixed_reuse_captures_only_uncovered(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    # One pick covered by the probe, one not: the capture --routes CSV must contain
    # ONLY the uncovered ref, and the result merges reused + fresh in pick order.
    monkeypatch.setattr(routes, "_node", lambda: "/usr/bin/node")
    monkeypatch.setattr(routes, "_uploads_dir", lambda: tmp_path)
    _reset_probe_cache(monkeypatch)
    proj = tmp_path / "dc-clones" / "clone7"
    proj.mkdir(parents=True)
    probe_dir = tmp_path / "dc-probe-y"
    probe_dir.mkdir()
    home_png = probe_dir / "home.png"
    home_png.write_bytes(b"home-bytes")

    seen_cmds: list[list[str]] = []

    async def fake_run(cmd, timeout, env=None):  # type: ignore[no-untyped-def]
        seen_cmds.append(cmd)
        # The fresh capture renders the uncovered route /about.
        return (0, json.dumps({"screens": [{"route": "/about", "path": "/x/about.png"}]}), "")

    monkeypatch.setattr(routes, "_run", fake_run)
    _cache_probe("clone7", probe_dir, proj.resolve(), {"/": str(home_png)})

    resp = await routes._handle_render(
        _Req(
            {
                "kind": "repo",
                "handle": "clone7",
                "picks": [
                    {"ref": "/", "label": "Home"},
                    {"ref": "/about", "label": "About"},
                ],
            }
        )  # type: ignore[arg-type]
    )
    assert resp.status == 200
    assert isinstance(resp.body, bytes)
    rec = await _drain(json.loads(resp.body)["job"])
    assert rec is not None and rec["status"] == "done"
    # The single capture call requested ONLY the uncovered ref.
    assert len(seen_cmds) == 1
    routes_flag = next(a for a in seen_cmds[0] if a.startswith("--routes="))
    assert routes_flag == "--routes=/about"
    # Reused (step 1) then fresh (step 2), in original pick order. The reused screen
    # is served from the copy adopted into the same dc-render-* dir as the fresh
    # capture, carrying the probe's bytes — never from the probe path itself.
    screens = rec["result"]["screens"]
    assert screens[1] == {"step": 2, "label": "About", "path": "/x/about.png"}
    assert screens[0]["step"] == 1 and screens[0]["label"] == "Home"
    adopted = Path(screens[0]["path"])
    assert adopted.parent.name.startswith("dc-render-")
    assert adopted.read_bytes() == b"home-bytes"
    assert str(adopted) != str(home_png)


@pytest.mark.asyncio
async def test_render_covered_png_deleted_falls_back_to_fresh(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    # The cache holds a route->png for a pick, but the PNG was deleted from disk
    # between /discover and /render (e.g. swept). That pick must be treated as
    # UNCOVERED — it lands in the fresh capture --routes CSV — rather than reused.
    monkeypatch.setattr(routes, "_node", lambda: "/usr/bin/node")
    monkeypatch.setattr(routes, "_uploads_dir", lambda: tmp_path)
    _reset_probe_cache(monkeypatch)
    proj = tmp_path / "dc-clones" / "clone-gone"
    proj.mkdir(parents=True)
    probe_dir = tmp_path / "dc-probe-gone"
    probe_dir.mkdir()
    # The cached PNG for "/" is recorded but NEVER written to disk (deleted/missing).
    home_png = probe_dir / "home.png"

    seen_cmds: list[list[str]] = []

    async def fake_run(cmd, timeout, env=None):  # type: ignore[no-untyped-def]
        seen_cmds.append(cmd)
        return (0, json.dumps({"screens": [{"route": "/", "path": "/x/home-fresh.png"}]}), "")

    monkeypatch.setattr(routes, "_run", fake_run)
    _cache_probe("clone-gone", probe_dir, proj.resolve(), {"/": str(home_png)})
    # Precondition: the cache references a path that does not exist on disk.
    assert not home_png.exists()

    resp = await routes._handle_render(
        _Req(
            {
                "kind": "repo",
                "handle": "clone-gone",
                "picks": [{"ref": "/", "label": "Home"}],
            }
        )  # type: ignore[arg-type]
    )
    assert resp.status == 200
    assert isinstance(resp.body, bytes)
    rec = await _drain(json.loads(resp.body)["job"])
    assert rec is not None and rec["status"] == "done"
    # The pick was NOT reused: the capture subprocess ran with the pick in --routes.
    assert len(seen_cmds) == 1
    routes_flag = next(a for a in seen_cmds[0] if a.startswith("--routes="))
    assert routes_flag == "--routes=/"
    # The result serves the FRESH capture, not the missing probe PNG.
    paths = [s["path"] for s in rec["result"]["screens"]]
    assert paths == ["/x/home-fresh.png"]
    assert str(home_png) not in paths


def test_adopt_reused_copies_into_out_dir_and_omits_failures(tmp_path) -> None:
    # The adopted copy always lands INSIDE out_dir (only the source basename is
    # used, so a manifest path cannot escape it) and carries the source bytes.
    probe_dir = tmp_path / "dc-probe-a"
    probe_dir.mkdir()
    out_dir = tmp_path / "dc-render-a"
    out_dir.mkdir()
    good = probe_dir / "build-home-1.png"
    good.write_bytes(b"good")
    escape = probe_dir / "esc.png"
    escape.write_bytes(b"esc")

    copies = routes._adopt_reused(
        {
            "/": str(good),
            "/esc": f"{probe_dir}/../dc-probe-a/esc.png",
            "/missing": str(probe_dir / "not-there.png"),
        },
        out_dir,
    )
    # A source that cannot be copied is OMITTED rather than yielding a path to nothing.
    # The handler reads that omission as "this ref is uncovered" and captures it fresh
    # (see test_render_failed_adoption_falls_back_to_a_fresh_capture).
    assert "/missing" not in copies
    for ref in ("/", "/esc"):
        dest = Path(copies[ref])
        assert dest.parent == out_dir
        assert dest.exists()
    assert Path(copies["/"]).read_bytes() == b"good"


@pytest.mark.asyncio
async def test_render_failed_adoption_falls_back_to_a_fresh_capture(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    # Every pick was covered, so on the happy path no capture runs — but the probe dir
    # vanishes before the copy (a sweep, or a concurrent local re-discovery). A pick
    # whose adoption failed is one nothing has rendered yet, so it must be DEMOTED to
    # uncovered and captured fresh. Reporting "could not see" instead would turn a
    # render that always worked before into zero screens.
    monkeypatch.setattr(routes, "_node", lambda: "/usr/bin/node")
    monkeypatch.setattr(routes, "_uploads_dir", lambda: tmp_path)
    _reset_probe_cache(monkeypatch)
    proj = tmp_path / "dc-clones" / "clone-race"
    proj.mkdir(parents=True)
    probe_dir = tmp_path / "dc-probe-race"
    probe_dir.mkdir()
    home_png = probe_dir / "home.png"
    home_png.write_bytes(b"x")

    seen_cmds: list[list[str]] = []

    async def fake_run(cmd, timeout, env=None):  # type: ignore[no-untyped-def]
        seen_cmds.append(cmd)
        return (0, json.dumps({"screens": [{"route": "/", "path": "/x/home-fresh.png"}]}), "")

    monkeypatch.setattr(routes, "_run", fake_run)

    real_adopt = routes._adopt_reused

    def racing_adopt(reused, out_dir):  # type: ignore[no-untyped-def]
        # Stand in for the probe dir being removed between the handler's exists()
        # check and the copy: every copy fails, so nothing is adopted.
        shutil.rmtree(probe_dir, ignore_errors=True)
        return real_adopt(reused, out_dir)

    monkeypatch.setattr(routes, "_adopt_reused", racing_adopt)
    _cache_probe("clone-race", probe_dir, proj.resolve(), {"/": str(home_png)})

    resp = await routes._handle_render(
        _Req(
            {
                "kind": "repo",
                "handle": "clone-race",
                "picks": [{"ref": "/", "label": "Home"}],
            }
        )  # type: ignore[arg-type]
    )
    assert resp.status == 200
    assert isinstance(resp.body, bytes)
    rec = await _drain(json.loads(resp.body)["job"])
    assert rec is not None and rec["status"] == "done"
    # The un-adoptable pick was captured fresh rather than reported unseeable.
    assert len(seen_cmds) == 1
    assert next(a for a in seen_cmds[0] if a.startswith("--routes=")) == "--routes=/"
    assert [s["path"] for s in rec["result"]["screens"]] == ["/x/home-fresh.png"]
    assert rec["result"]["couldNotSee"] == []


@pytest.mark.asyncio
async def test_render_local_does_not_read_a_foreign_handles_cache(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    # A local render takes `directory` from `value` when the handle is not a
    # "local:<path>" one, so the cache lookup must key off the VALIDATED target. Key
    # it off the raw handle and a local render for project B reads repo-A's entry and
    # hands back A's screenshots as B's screens.
    monkeypatch.setattr(routes, "_node", lambda: "/usr/bin/node")
    monkeypatch.setattr(routes, "_uploads_dir", lambda: tmp_path)
    _reset_probe_cache(monkeypatch)
    other = tmp_path / "dc-clones" / "clone-a"
    other.mkdir(parents=True)
    target = tmp_path / "project-b"
    target.mkdir()
    probe_dir = tmp_path / "dc-probe-a"
    probe_dir.mkdir()
    foreign_png = probe_dir / "home.png"
    foreign_png.write_bytes(b"project-a-bytes")

    seen_cmds: list[list[str]] = []

    async def fake_run(cmd, timeout, env=None):  # type: ignore[no-untyped-def]
        seen_cmds.append(cmd)
        return (0, json.dumps({"screens": [{"route": "/", "path": "/x/b-fresh.png"}]}), "")

    monkeypatch.setattr(routes, "_run", fake_run)
    # Repo A's probe is cached under its clone id, exactly as /discover records it.
    _cache_probe("clone-a", probe_dir, other.resolve(), {"/": str(foreign_png)})

    resp = await routes._handle_render(
        _Req(
            {
                "kind": "local",
                "value": str(target),
                "handle": "clone-a",
                "picks": [{"ref": "/", "label": "Home"}],
            }
        )  # type: ignore[arg-type]
    )
    assert resp.status == 200
    assert isinstance(resp.body, bytes)
    rec = await _drain(json.loads(resp.body)["job"])
    assert rec is not None and rec["status"] == "done"
    # The pick went to a fresh capture of project B; none of A's bytes came back.
    assert len(seen_cmds) == 1
    assert str(target) in seen_cmds[0]
    paths = [s["path"] for s in rec["result"]["screens"]]
    assert paths == ["/x/b-fresh.png"]
    assert str(foreign_png) not in paths
    assert not any((p / "reused-0-home.png").exists() for p in tmp_path.glob("dc-render-*"))


@pytest.mark.asyncio
async def test_render_local_reuses_its_own_cache_entry(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    # The other side of the key derivation: a local render whose validated target IS
    # the discovered project must still find its entry, whether the client echoes the
    # "local:<path>" handle back or sends only `value`.
    monkeypatch.setattr(routes, "_node", lambda: "/usr/bin/node")
    monkeypatch.setattr(routes, "_uploads_dir", lambda: tmp_path)
    _reset_probe_cache(monkeypatch)
    target = tmp_path / "project-b"
    target.mkdir()
    probe_dir = tmp_path / "dc-probe-b"
    probe_dir.mkdir()
    home_png = probe_dir / "home.png"
    home_png.write_bytes(b"project-b-bytes")

    called = {"run": 0}

    async def fake_run(cmd, timeout, env=None):  # type: ignore[no-untyped-def]
        called["run"] += 1
        return (0, "{}", "")

    monkeypatch.setattr(routes, "_run", fake_run)
    _cache_probe(f"local:{target}", probe_dir, target, {"/": str(home_png)})

    for handle in (f"local:{target}", ""):
        resp = await routes._handle_render(
            _Req(
                {
                    "kind": "local",
                    "value": str(target),
                    "handle": handle,
                    "picks": [{"ref": "/", "label": "Home"}],
                }
            )  # type: ignore[arg-type]
        )
        assert resp.status == 200
        assert isinstance(resp.body, bytes)
        rec = await _drain(json.loads(resp.body)["job"])
        assert rec is not None and rec["status"] == "done"
        paths = [s["path"] for s in rec["result"]["screens"]]
        assert len(paths) == 1
        assert Path(paths[0]).read_bytes() == b"project-b-bytes"
        assert Path(paths[0]).parent.name.startswith("dc-render-")
    assert called["run"] == 0


@pytest.mark.asyncio
async def test_render_past_the_reuse_window_recaptures(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    # A build-output token cannot see data a built SPA fetches at runtime, so the
    # reuse window — not the retained dir's lifetime — is what bounds how stale a
    # reused capture can be. An entry aged past it must be re-captured even though its
    # dir, its PNG and its signature are all still perfectly valid.
    monkeypatch.setattr(routes, "_node", lambda: "/usr/bin/node")
    monkeypatch.setattr(routes, "_uploads_dir", lambda: tmp_path)
    _reset_probe_cache(monkeypatch)
    proj = tmp_path / "dc-clones" / "clone-aged"
    proj.mkdir(parents=True)
    probe_dir = tmp_path / "dc-probe-aged"
    probe_dir.mkdir()
    home_png = probe_dir / "home.png"
    home_png.write_bytes(b"stale-bytes")

    seen_cmds: list[list[str]] = []

    async def fake_run(cmd, timeout, env=None):  # type: ignore[no-untyped-def]
        seen_cmds.append(cmd)
        return (0, json.dumps({"screens": [{"route": "/", "path": "/x/home-fresh.png"}]}), "")

    monkeypatch.setattr(routes, "_run", fake_run)
    _cache_probe("clone-aged", probe_dir, proj.resolve(), {"/": str(home_png)})
    aged_by = routes._PROBE_REUSE_TTL_SEC + 60
    # The age used here must land strictly INSIDE the dir's _CLONE_TTL_SEC lifetime,
    # or the test would pass just as well with the two windows collapsed into one.
    assert aged_by < routes._CLONE_TTL_SEC
    routes._PROBE_CACHE["clone-aged"]["created_at"] = time.time() - aged_by
    assert home_png.exists()

    resp = await routes._handle_render(
        _Req(
            {
                "kind": "repo",
                "handle": "clone-aged",
                "picks": [{"ref": "/", "label": "Home"}],
            }
        )  # type: ignore[arg-type]
    )
    assert resp.status == 200
    assert isinstance(resp.body, bytes)
    rec = await _drain(json.loads(resp.body)["job"])
    assert rec is not None and rec["status"] == "done"
    assert len(seen_cmds) == 1
    assert next(a for a in seen_cmds[0] if a.startswith("--routes=")) == "--routes=/"
    assert [s["path"] for s in rec["result"]["screens"]] == ["/x/home-fresh.png"]


@pytest.mark.asyncio
async def test_render_unknown_signature_is_not_a_match(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    # An unreadable build output yields no token, and "unknown" is not "unchanged":
    # a build dir deleted between /discover and /render is no evidence the bytes the
    # probe captured still stand, so reuse must be refused rather than permitted.
    monkeypatch.setattr(routes, "_node", lambda: "/usr/bin/node")
    monkeypatch.setattr(routes, "_uploads_dir", lambda: tmp_path)
    _reset_probe_cache(monkeypatch)
    proj = tmp_path / "dc-clones" / "clone-unknown"
    proj.mkdir(parents=True)
    probe_dir = tmp_path / "dc-probe-unknown"
    probe_dir.mkdir()
    home_png = probe_dir / "home.png"
    home_png.write_bytes(b"x")

    seen_cmds: list[list[str]] = []

    async def fake_run(cmd, timeout, env=None):  # type: ignore[no-untyped-def]
        seen_cmds.append(cmd)
        return (0, json.dumps({"screens": [{"route": "/", "path": "/x/home-fresh.png"}]}), "")

    monkeypatch.setattr(routes, "_run", fake_run)
    build = _cache_probe("clone-unknown", probe_dir, proj.resolve(), {"/": str(home_png)})
    shutil.rmtree(build)

    resp = await routes._handle_render(
        _Req(
            {
                "kind": "repo",
                "handle": "clone-unknown",
                "picks": [{"ref": "/", "label": "Home"}],
            }
        )  # type: ignore[arg-type]
    )
    assert resp.status == 200
    assert isinstance(resp.body, bytes)
    rec = await _drain(json.loads(resp.body)["job"])
    assert rec is not None and rec["status"] == "done"
    # Reuse was refused: the pick went to a fresh capture.
    assert len(seen_cmds) == 1
    assert next(a for a in seen_cmds[0] if a.startswith("--routes=")) == "--routes=/"
    assert [s["path"] for s in rec["result"]["screens"]] == ["/x/home-fresh.png"]


@pytest.mark.asyncio
async def test_render_staleness_mismatch_recaptures_all(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    # An IN-PLACE rebuild between /discover and /render must bypass reuse so ALL
    # picks are re-captured. This is the case the guard exists for and the one a
    # token over the project root would miss: a rebuild writes underneath an
    # already-existing dist/, which leaves the root's own st_mtime untouched (the
    # test asserts that below), so only a token over the build output catches it.
    monkeypatch.setattr(routes, "_node", lambda: "/usr/bin/node")
    monkeypatch.setattr(routes, "_uploads_dir", lambda: tmp_path)
    _reset_probe_cache(monkeypatch)
    proj = tmp_path / "dc-clones" / "clone9"
    proj.mkdir(parents=True)
    probe_dir = tmp_path / "dc-probe-z"
    probe_dir.mkdir()
    home_png = probe_dir / "home.png"
    home_png.write_bytes(b"x")

    seen_cmds: list[list[str]] = []

    async def fake_run(cmd, timeout, env=None):  # type: ignore[no-untyped-def]
        seen_cmds.append(cmd)
        return (
            0,
            json.dumps(
                {
                    "screens": [
                        {"route": "/", "path": "/x/home-fresh.png"},
                        {"route": "/about", "path": "/x/about.png"},
                    ]
                }
            ),
            "",
        )

    monkeypatch.setattr(routes, "_run", fake_run)
    build = _cache_probe("clone9", probe_dir, proj.resolve(), {"/": str(home_png)})
    # Rebuild in place: index.html is rewritten, and nothing is added to or removed
    # from the project root, so the root's own mtime does not move.
    root_mtime = os.stat(proj).st_mtime_ns
    (build / "index.html").write_text("<html>v2</html>", encoding="utf-8")
    _bump(build / "index.html")
    assert os.stat(proj).st_mtime_ns == root_mtime

    resp = await routes._handle_render(
        _Req(
            {
                "kind": "repo",
                "handle": "clone9",
                "picks": [
                    {"ref": "/", "label": "Home"},
                    {"ref": "/about", "label": "About"},
                ],
            }
        )  # type: ignore[arg-type]
    )
    assert resp.status == 200
    assert isinstance(resp.body, bytes)
    rec = await _drain(json.loads(resp.body)["job"])
    assert rec is not None and rec["status"] == "done"
    # All picks re-captured (the CSV contains both refs); no probe PNG reused.
    assert len(seen_cmds) == 1
    routes_flag = next(a for a in seen_cmds[0] if a.startswith("--routes="))
    assert routes_flag == "--routes=/,/about"
    paths = [s["path"] for s in rec["result"]["screens"]]
    assert paths == ["/x/home-fresh.png", "/x/about.png"]
    assert str(home_png) not in paths


@pytest.mark.asyncio
async def test_discover_from_dir_retains_probe_and_caches(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    # A probe that produced a usable screen must RETAIN its dir and cache the
    # route->PNG map keyed by the handle.
    monkeypatch.setattr(routes, "_node", lambda: "/usr/bin/node")
    monkeypatch.setattr(routes, "_uploads_dir", lambda: tmp_path)
    _reset_probe_cache(monkeypatch)
    proj = tmp_path / "proj"
    build = proj / "dist"
    build.mkdir(parents=True)
    (build / "index.html").write_text("<html></html>", encoding="utf-8")

    async def fake_run(cmd, timeout, env=None):  # type: ignore[no-untyped-def]
        if any("discover-routes" in c for c in cmd):
            return (0, json.dumps({"framework": "", "routes": [{"path": "/"}]}), "")
        # Probe manifest: the captured PNG path lives inside the probe --out dir, and
        # buildDir names the already-built output the capture served. fullPageCoverage
        # marks the page as having fit the viewport, so the PNG is reusable.
        out_dir = next(a[len("--out=") :] for a in cmd if a.startswith("--out="))
        png = os.path.join(out_dir, "build-home.png")
        return (
            0,
            json.dumps(
                {
                    "buildDir": str(build),
                    "screens": [{"route": "/", "path": png, "fullPageCoverage": True}],
                }
            ),
            "",
        )

    monkeypatch.setattr(routes, "_run", fake_run)
    out = await routes._discover_from_dir(proj, handle="clone-keep")
    assert out["handle"] == "clone-keep"
    rec = routes._probe_get("clone-keep")
    assert rec is not None
    assert "/" in rec["routes"]
    # The token is recorded over the build output, so /render compares the bytes the
    # reused PNG actually depicts rather than the project root's directory entry.
    assert rec["build_dir"] == str(build)
    fresh = routes._served_signature(build)
    assert fresh is not None and rec["served_signature"] == fresh.digest
    # The retained probe dir still exists on disk and is a dc-probe-* dir.
    assert Path(rec["dir"]).exists()
    assert "dc-probe-" in Path(rec["dir"]).name


def test_probe_claim_drops_the_entry_and_its_dir(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    _reset_probe_cache(monkeypatch)
    pdir = tmp_path / "dc-probe-e"
    pdir.mkdir()
    routes._probe_put(
        "h", routes._probe_claim("h"), str(pdir), {"/": "/x/a.png"}, "/proj/dist", "sig-a"
    )
    routes._probe_claim("h")
    assert routes._probe_get("h") is None
    assert not pdir.exists()
    # Claiming an unknown handle is a no-op eviction, not an error; a falsy handle has
    # no /render counterpart and yields the sentinel claim.
    routes._probe_claim("nope")
    assert routes._probe_claim("") == 0


def test_probe_claim_stamps_strictly_increase_per_handle(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # monotonic_ns is only non-decreasing, so two claims inside one clock tick would
    # compare equal and _probe_put would read the older one as "not superseded". Seeding
    # a stamp above any real reading forces that tie-break branch deterministically,
    # without patching the clock the rest of the process shares.
    _reset_probe_cache(monkeypatch)
    seeded = 2**62
    routes._PROBE_STARTS["h"] = seeded
    assert routes._probe_claim("h") == seeded + 1
    assert routes._probe_claim("h") == seeded + 2


def test_probe_put_refuses_a_write_a_newer_discovery_superseded(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    # Two discoveries of the same stable "local:<path>" handle interleave and finish out
    # of order. The OLDER capture must not land on top of the newer one: its route map
    # can differ in ways a staleness token cannot see (the newer probe may have withheld
    # a route it found behind a login gate, and an auth state is not served bytes), so
    # last-writer-wins would hand /render a screenshot the newer discovery rejected.
    _reset_probe_cache(monkeypatch)
    handle = "local:/some/project"
    old_dir = tmp_path / "dc-probe-old"
    old_dir.mkdir()
    new_dir = tmp_path / "dc-probe-new"
    new_dir.mkdir()

    old_claim = routes._probe_claim(handle)
    new_claim = routes._probe_claim(handle)
    # The newer discovery finishes first.
    routes._probe_put(handle, new_claim, str(new_dir), {"/": "/x/new.png"}, "/proj/dist", "sig-new")
    # Then the older one finishes and tries to write.
    routes._probe_put(handle, old_claim, str(old_dir), {"/": "/x/old.png"}, "/proj/dist", "sig-old")

    rec = routes._probe_get(handle)
    assert rec is not None
    assert rec["dir"] == str(new_dir)
    assert rec["routes"] == {"/": "/x/new.png"}
    assert rec["served_signature"] == "sig-new"
    # The newer dir survives; the refused write drops its OWN dir rather than leaking it
    # to the TTL sweep, since nothing may reuse a capture that lost the ordering.
    assert new_dir.exists()
    assert not old_dir.exists()

    # And the guard is not one-shot: the newest claim can still write afterwards.
    newer_dir = tmp_path / "dc-probe-newer"
    newer_dir.mkdir()
    routes._probe_put(
        handle, routes._probe_claim(handle), str(newer_dir), {"/": "/x/n.png"}, "/p/dist", "sig-n"
    )
    latest = routes._probe_get(handle)
    assert latest is not None and latest["dir"] == str(newer_dir)


def test_sweep_bounds_the_ordering_stamp_map(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    # A stamp is not tied to a retained dir, so without its own bound the map would grow
    # one entry per repo discovery forever (each is a fresh clone id).
    _reset_probe_cache(monkeypatch)
    monkeypatch.setattr(routes, "_uploads_dir", lambda: tmp_path)
    fresh = routes._probe_claim("clone-fresh")
    routes._PROBE_STARTS["clone-ancient"] = fresh - int((routes._CLONE_TTL_SEC + 60) * 1e9)
    routes._sweep_clones()
    assert "clone-fresh" in routes._PROBE_STARTS
    assert "clone-ancient" not in routes._PROBE_STARTS


@pytest.mark.asyncio
async def test_rediscovery_with_nothing_reusable_evicts_the_prior_entry(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    # A stable "local:<path>" handle is re-discovered in place. If THIS discovery finds
    # nothing reusable — every screen now behind a gate, the probe timed out, the build
    # output gone — the earlier entry must not survive as a fallback, or /render would
    # serve the older capture for a project whose discovery just failed. The prior
    # token can still match, so nothing downstream would notice.
    monkeypatch.setattr(routes, "_node", lambda: "/usr/bin/node")
    monkeypatch.setattr(routes, "_uploads_dir", lambda: tmp_path)
    _reset_probe_cache(monkeypatch)
    proj = tmp_path / "proj"
    build = proj / "dist"
    build.mkdir(parents=True)
    (build / "index.html").write_text("<html></html>", encoding="utf-8")
    old_probe = tmp_path / "dc-probe-old"
    old_probe.mkdir()
    old_png = old_probe / "home.png"
    old_png.write_bytes(b"stale")
    handle = f"local:{proj}"
    _cache_probe(handle, old_probe, proj, {"/": str(old_png)})
    assert routes._probe_get(handle) is not None

    async def fake_run(cmd, timeout, env=None):  # type: ignore[no-untyped-def]
        if any("discover-routes" in c for c in cmd):
            return (0, json.dumps({"framework": "", "routes": [{"path": "/"}]}), "")
        # Re-discovery renders nothing usable this time — the build output is intact,
        # so the OLD entry's token would still match if it survived.
        return (0, json.dumps({"buildDir": str(build), "screens": []}), "")

    monkeypatch.setattr(routes, "_run", fake_run)
    await routes._discover_from_dir(proj, handle=handle)
    assert routes._probe_get(handle) is None
    assert not old_probe.exists()
    assert not any(p.name.startswith("dc-probe-") for p in tmp_path.iterdir())


@pytest.mark.asyncio
async def test_rediscovery_evicts_even_when_it_never_reaches_the_probe(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    # The eviction runs FIRST, so it cannot be skipped by a discovery that dies before
    # any store: a node/git failure, a timeout, an exception the job wrapper catches.
    # A stale entry surviving one of those is invisible downstream, because its token
    # can still match and /render would serve the earlier capture.
    monkeypatch.setattr(routes, "_uploads_dir", lambda: tmp_path)
    _reset_probe_cache(monkeypatch)
    proj = tmp_path / "proj"
    build = proj / "dist"
    build.mkdir(parents=True)
    (build / "index.html").write_text("<html></html>", encoding="utf-8")
    handle = f"local:{proj}"

    for break_it in ("no-node", "raises"):
        old_probe = tmp_path / f"dc-probe-{break_it}"
        old_probe.mkdir()
        _cache_probe(handle, old_probe, proj, {"/": str(old_probe / "home.png")})
        assert routes._probe_get(handle) is not None

        if break_it == "no-node":
            monkeypatch.setattr(routes, "_node", lambda: None)
            await routes._discover_from_dir(proj, handle=handle)
        else:
            monkeypatch.setattr(routes, "_node", lambda: "/usr/bin/node")

            async def boom(cmd, timeout, env=None):  # type: ignore[no-untyped-def]
                raise RuntimeError("discovery died")

            monkeypatch.setattr(routes, "_run", boom)
            with pytest.raises(RuntimeError):
                await routes._discover_from_dir(proj, handle=handle)

        assert routes._probe_get(handle) is None, break_it
        assert not old_probe.exists(), break_it


@pytest.mark.asyncio
async def test_rediscovery_with_no_routes_evicts_the_prior_entry(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    # The same must hold when discover-routes itself returns nothing, which skips the
    # probe block entirely — the eviction is a single exit precisely so this path cannot
    # forget it.
    monkeypatch.setattr(routes, "_node", lambda: "/usr/bin/node")
    monkeypatch.setattr(routes, "_uploads_dir", lambda: tmp_path)
    _reset_probe_cache(monkeypatch)
    proj = tmp_path / "proj"
    proj.mkdir()
    old_probe = tmp_path / "dc-probe-old"
    old_probe.mkdir()
    handle = f"local:{proj}"
    _cache_probe(handle, old_probe, proj, {"/": str(old_probe / "home.png")})

    async def fake_run(cmd, timeout, env=None):  # type: ignore[no-untyped-def]
        return (0, json.dumps({"framework": "", "routes": []}), "")

    monkeypatch.setattr(routes, "_run", fake_run)
    await routes._discover_from_dir(proj, handle=handle)
    assert routes._probe_get(handle) is None
    assert not old_probe.exists()


@pytest.mark.asyncio
async def test_discover_does_not_cache_when_a_file_is_deleted_mid_capture(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    # A DELETION leaves no file behind to carry a recent mtime, so a high-water mark
    # over files alone would miss a served file removed while the probe was capturing —
    # and /render would then reuse a PNG depicting content that is gone. The mark has to
    # read directory mtimes too. Only the directory's mtime is moved here, so the test
    # fails if that stat is dropped.
    monkeypatch.setattr(routes, "_node", lambda: "/usr/bin/node")
    monkeypatch.setattr(routes, "_uploads_dir", lambda: tmp_path)
    _reset_probe_cache(monkeypatch)
    proj = tmp_path / "proj"
    build = proj / "dist"
    (build / "assets").mkdir(parents=True)
    (build / "index.html").write_text("<html></html>", encoding="utf-8")
    (build / "assets" / "legacy.js").write_text("old", encoding="utf-8")

    async def fake_run(cmd, timeout, env=None):  # type: ignore[no-untyped-def]
        if any("discover-routes" in c for c in cmd):
            return (0, json.dumps({"framework": "", "routes": [{"path": "/"}]}), "")
        out_dir = next(a[len("--out=") :] for a in cmd if a.startswith("--out="))
        # Stand in for a build's cleanup step removing a served file mid-capture. No
        # remaining file's mtime moves; _bump makes the directory's move
        # deterministically rather than relying on the filesystem's mtime granularity.
        (build / "assets" / "legacy.js").unlink()
        _bump(build / "assets", 60.0)
        return (
            0,
            json.dumps(
                {
                    "buildDir": str(build),
                    "screens": [{"route": "/", "path": os.path.join(out_dir, "home.png")}],
                }
            ),
            "",
        )

    monkeypatch.setattr(routes, "_run", fake_run)
    out = await routes._discover_from_dir(proj, handle="clone-deleting-build")
    assert out["screens"][0]["canSee"] is True
    assert routes._probe_get("clone-deleting-build") is None
    assert not any(p.name.startswith("dc-probe-") for p in tmp_path.iterdir())


@pytest.mark.asyncio
async def test_discover_does_not_cache_when_a_build_lands_mid_capture(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    # The token can only be taken AFTER the capture, because the manifest is what names
    # the build dir. So a build finishing while the probe screenshots would leave the
    # token describing the NEW bytes and the PNGs depicting the old ones — and /render
    # would find the token matching and serve them. Nothing may be cached in that case.
    monkeypatch.setattr(routes, "_node", lambda: "/usr/bin/node")
    monkeypatch.setattr(routes, "_uploads_dir", lambda: tmp_path)
    _reset_probe_cache(monkeypatch)
    proj = tmp_path / "proj"
    build = proj / "dist"
    build.mkdir(parents=True)
    (build / "index.html").write_text("<html>v1</html>", encoding="utf-8")

    async def fake_run(cmd, timeout, env=None):  # type: ignore[no-untyped-def]
        if any("discover-routes" in c for c in cmd):
            return (0, json.dumps({"framework": "", "routes": [{"path": "/"}]}), "")
        out_dir = next(a[len("--out=") :] for a in cmd if a.startswith("--out="))
        # Stand in for `vite build` completing while the probe is screenshotting: the
        # served bytes are rewritten after the capture started. _bump makes the mtime
        # move deterministically rather than relying on the clock's granularity.
        (build / "index.html").write_text("<html>v2</html>", encoding="utf-8")
        _bump(build / "index.html", 60.0)
        return (
            0,
            json.dumps(
                {
                    "buildDir": str(build),
                    "screens": [{"route": "/", "path": os.path.join(out_dir, "home.png")}],
                }
            ),
            "",
        )

    monkeypatch.setattr(routes, "_run", fake_run)
    out = await routes._discover_from_dir(proj, handle="clone-racing-build")
    # Discovery itself still succeeds and the route is still seeable.
    assert out["screens"][0]["canSee"] is True
    # Nothing cached, and no probe dir left for the sweep.
    assert routes._probe_get("clone-racing-build") is None
    assert not any(p.name.startswith("dc-probe-") for p in tmp_path.iterdir())


@pytest.mark.asyncio
async def test_discover_does_not_cache_a_gated_screen(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    # A screen captured under a login / consent overlay must NOT be cached for reuse.
    # /render raises its gate warning from the capture it runs, and a fully-covered
    # render runs no capture — so reusing a gate screenshot would show the critic the
    # wall with the warning silently missing. The clean route still caches, and the
    # check keys on the per-screen `overlay`, not the `blockedBy` summary (absent here,
    # since the script only sets that when one overlay covers most screens).
    monkeypatch.setattr(routes, "_node", lambda: "/usr/bin/node")
    monkeypatch.setattr(routes, "_uploads_dir", lambda: tmp_path)
    _reset_probe_cache(monkeypatch)
    proj = tmp_path / "proj"
    build = proj / "dist"
    build.mkdir(parents=True)
    (build / "index.html").write_text("<html></html>", encoding="utf-8")

    async def fake_run(cmd, timeout, env=None):  # type: ignore[no-untyped-def]
        if any("discover-routes" in c for c in cmd):
            return (
                0,
                json.dumps({"framework": "", "routes": [{"path": "/"}, {"path": "/app"}]}),
                "",
            )
        out_dir = next(a[len("--out=") :] for a in cmd if a.startswith("--out="))
        return (
            0,
            json.dumps(
                {
                    "buildDir": str(build),
                    "blockedBy": None,
                    "screens": [
                        {
                            "route": "/",
                            "path": os.path.join(out_dir, "home.png"),
                            "fullPageCoverage": True,
                        },
                        {
                            "route": "/app",
                            "path": os.path.join(out_dir, "app.png"),
                            "fullPageCoverage": True,
                            "overlay": {"text": "Sign in to continue", "area": 0.8},
                        },
                    ],
                }
            ),
            "",
        )

    monkeypatch.setattr(routes, "_run", fake_run)
    out = await routes._discover_from_dir(proj, handle="clone-gated")
    # Discovery still reports BOTH routes as seeable — the gated one did render.
    assert {s["ref"]: s["canSee"] for s in out["screens"]} == {"/": True, "/app": True}
    rec = routes._probe_get("clone-gated")
    assert rec is not None
    # ...but only the clean route is offered for reuse.
    assert list(rec["routes"]) == ["/"]


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", [False, None])
async def test_discover_does_not_cache_a_screen_taller_than_the_viewport(monkeypatch, tmp_path, flag) -> None:  # type: ignore[no-untyped-def]
    # The probe captures WITHOUT --full and /render captures WITH it, so a probe PNG of
    # a page taller than the viewport holds strictly less than the render it would
    # replace. Reuse is therefore confined to screens capture-build.mjs certified as
    # fullPageCoverage; an overflowing route is left uncovered and /render captures it
    # fresh. `None` stands for a manifest that predates the flag: absent must read as
    # "cannot vouch", never as permission to reuse.
    monkeypatch.setattr(routes, "_node", lambda: "/usr/bin/node")
    monkeypatch.setattr(routes, "_uploads_dir", lambda: tmp_path)
    _reset_probe_cache(monkeypatch)
    proj = tmp_path / "proj"
    build = proj / "dist"
    build.mkdir(parents=True)
    (build / "index.html").write_text("<html></html>", encoding="utf-8")

    async def fake_run(cmd, timeout, env=None):  # type: ignore[no-untyped-def]
        if any("discover-routes" in c for c in cmd):
            return (
                0,
                json.dumps({"framework": "", "routes": [{"path": "/"}, {"path": "/tall"}]}),
                "",
            )
        out_dir = next(a[len("--out=") :] for a in cmd if a.startswith("--out="))
        tall: dict[str, object] = {
            "route": "/tall",
            "path": os.path.join(out_dir, "tall.png"),
        }
        if flag is not None:
            tall["fullPageCoverage"] = flag
        return (
            0,
            json.dumps(
                {
                    "buildDir": str(build),
                    "screens": [
                        {
                            "route": "/",
                            "path": os.path.join(out_dir, "home.png"),
                            "fullPageCoverage": True,
                        },
                        tall,
                    ],
                }
            ),
            "",
        )

    monkeypatch.setattr(routes, "_run", fake_run)
    out = await routes._discover_from_dir(proj, handle="clone-tall")
    # The tall route still RENDERED, so discovery reports it as seeable; only its
    # reusability is withheld.
    assert {s["ref"]: s["canSee"] for s in out["screens"]} == {"/": True, "/tall": True}
    rec = routes._probe_get("clone-tall")
    assert rec is not None
    assert list(rec["routes"]) == ["/"]


@pytest.mark.asyncio
async def test_discover_from_dir_drops_probe_when_build_dir_is_foreign(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    # A manifest naming a build dir OUTSIDE the project must cache nothing: a token
    # taken over an unrelated tree would stand still and permit reuse of a stale PNG
    # for the whole TTL. The probe dir is dropped rather than leaked to the sweep.
    monkeypatch.setattr(routes, "_node", lambda: "/usr/bin/node")
    monkeypatch.setattr(routes, "_uploads_dir", lambda: tmp_path)
    _reset_probe_cache(monkeypatch)
    proj = tmp_path / "proj"
    proj.mkdir()
    foreign = tmp_path / "elsewhere"
    foreign.mkdir()

    async def fake_run(cmd, timeout, env=None):  # type: ignore[no-untyped-def]
        if any("discover-routes" in c for c in cmd):
            return (0, json.dumps({"framework": "", "routes": [{"path": "/"}]}), "")
        out_dir = next(a[len("--out=") :] for a in cmd if a.startswith("--out="))
        png = os.path.join(out_dir, "build-home.png")
        return (
            0,
            json.dumps({"buildDir": str(foreign), "screens": [{"route": "/", "path": png}]}),
            "",
        )

    monkeypatch.setattr(routes, "_run", fake_run)
    out = await routes._discover_from_dir(proj, handle="clone-foreign")
    # Discovery still succeeds and the route is still reported seeable — only the
    # reuse cache is withheld.
    assert out["screens"][0]["canSee"] is True
    assert routes._probe_get("clone-foreign") is None
    assert not any(p.name.startswith("dc-probe-") for p in tmp_path.iterdir())


@pytest.mark.asyncio
async def test_discover_from_dir_drops_empty_probe(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    # A probe with no usable screen must delete its dir immediately and cache nothing.
    monkeypatch.setattr(routes, "_node", lambda: "/usr/bin/node")
    monkeypatch.setattr(routes, "_uploads_dir", lambda: tmp_path)
    _reset_probe_cache(monkeypatch)
    proj = tmp_path / "proj"
    proj.mkdir()

    async def fake_run(cmd, timeout, env=None):  # type: ignore[no-untyped-def]
        if any("discover-routes" in c for c in cmd):
            return (0, json.dumps({"framework": "", "routes": [{"path": "/"}]}), "")
        # No screens -> nothing usable to retain.
        return (0, json.dumps({"screens": []}), "")

    monkeypatch.setattr(routes, "_run", fake_run)
    await routes._discover_from_dir(proj, handle="clone-empty")
    assert routes._probe_get("clone-empty") is None
    # No retained dc-probe-* dir was left behind.
    assert not any(p.name.startswith("dc-probe-") for p in tmp_path.iterdir())
