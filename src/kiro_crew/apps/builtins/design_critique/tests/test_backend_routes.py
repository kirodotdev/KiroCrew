"""The backend mounts its three routes on the gateway aiohttp app."""

from __future__ import annotations

import asyncio
import json
import os
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
    import time

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
