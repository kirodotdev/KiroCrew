"""Tests for Papyrus's HTTP surface (``backend/routes.py``).

Lives in the repo-level ``test/`` tree (not the app's in-package ``tests/``)
because ``setup.cfg`` sets ``testpaths = test transfer`` — a test under
``src/kiro_crew/apps/builtins/...`` is never collected by CI.

Handlers are driven directly with ``aiohttp.test_utils.make_mocked_request``, the
same approach ``issue_radar``'s route tests take, so no server is bound and no
subprocess is spawned.

Coverage targets, in the order the request is authorized:

  1. ``_require_enabled`` — the app is opt-in, so EVERY route must refuse with 403
     while it is disabled. Routes are registered once at gateway startup, so
     without this gate a default-disabled app stays fully callable.
  2. ``_project`` — an invalid project name is a 400 and a missing one a 404, both
     before any filesystem touch.
  3. ``_safe_relative`` — a traversal in any path-bearing route is a 400.

Plus the response contracts the frontend depends on: the compile result's shape,
the PDF's security headers, and the git error-to-status mapping (401 for an auth
failure, 409 for a conflict).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew.apps.builtins.papyrus.backend import gitops, latex, routes, store, tectonic


def _request(method: str, path: str, body: dict[str, Any] | None = None) -> web.Request:
    """A mocked request, with a JSON body when one is given."""
    payload = json.dumps(body or {}).encode()
    request = make_mocked_request(method, path)
    # `make_mocked_request` gives no readable body, so stub the one accessor the
    # handlers use. This keeps the tests on the real handler code path.
    request.json = mock.AsyncMock(return_value=body if body is not None else {})  # type: ignore[method-assign]
    request._payload_length = len(payload)  # type: ignore[attr-defined]
    return request


def _json_of(response: web.StreamResponse) -> dict[str, Any]:
    assert isinstance(response, web.Response)
    assert isinstance(response.body, (bytes, bytearray))
    return json.loads(response.body)


@pytest.fixture()
def enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Report the app as enabled, so the deny-by-default gate lets requests through."""
    monkeypatch.setattr(routes, "is_app_enabled", lambda _name: True)


@pytest.fixture()
def data_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the store at a tmp data dir so no test touches the real app home."""
    root = tmp_path / "papyrus-data"
    root.mkdir()
    monkeypatch.setattr(store, "app_data_dir", lambda _name: root)
    return root


@pytest.fixture()
def project(data_root: Path) -> Path:
    proj = store.projects_dir() / "my-paper"
    proj.mkdir(parents=True)
    (proj / "main.tex").write_text(r"\documentclass{article}", encoding="utf-8")
    (proj / "references.bib").write_text("", encoding="utf-8")
    return proj


# ── the deny-by-default gate ─────────────────────────────────────────────────


@pytest.mark.asyncio
class TestRequireEnabled:
    async def test_denies_every_route_while_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Routes are registered once at startup, so an opt-in app would otherwise
        stay callable the moment the gateway booted."""
        monkeypatch.setattr(routes, "is_app_enabled", lambda _name: False)
        guarded = routes._require_enabled(routes._handle_list_projects)
        response = await guarded(_request("GET", "/api/apps/papyrus/projects"))
        assert response.status == 403
        assert "disabled" in _json_of(response)["error"]

    async def test_allows_the_route_when_enabled(self, enabled: None, data_root: Path) -> None:
        guarded = routes._require_enabled(routes._handle_list_projects)
        response = await guarded(_request("GET", "/api/apps/papyrus/projects"))
        assert response.status == 200


class TestRouteRegistration:
    """Structural guards on ``register_routes`` — no request needed."""

    def test_every_registered_route_is_guarded(self) -> None:
        """A route added without the gate is the regression this pins: it would be
        reachable while the app is disabled, and nothing else would notice."""
        app = web.Application()
        routes.register_routes(app)
        registered = [r for r in app.router.routes() if r.resource is not None]
        assert registered, "no routes registered"
        for route in registered:
            resource = route.resource
            assert resource is not None
            assert getattr(route.handler, "__wrapped__", None) is not None, (
                f"{route.method} {resource.canonical} is not wrapped in "
                "_require_enabled — it would answer while papyrus is disabled"
            )

    def test_every_route_lives_under_the_app_namespace(self) -> None:
        """A route outside /api/apps/papyrus would fall outside the manifest's
        declared ``permissions.api`` scope."""
        app = web.Application()
        routes.register_routes(app)
        for route in app.router.routes():
            if route.resource is not None:
                assert route.resource.canonical.startswith(routes.API_BASE)


# ── project-name authorization ───────────────────────────────────────────────


@pytest.mark.asyncio
class TestProjectAuthorization:
    async def test_missing_name_is_a_400(self, enabled: None, data_root: Path) -> None:
        with pytest.raises(web.HTTPBadRequest):
            await routes._handle_get_project(_request("GET", "/api/apps/papyrus/project"))

    @pytest.mark.parametrize("name", ["../escape", "a/b", "-rf", "..", "a\\b"])
    async def test_an_unsafe_name_is_a_400(
        self, enabled: None, data_root: Path, name: str
    ) -> None:
        request = make_mocked_request("GET", f"/api/apps/papyrus/project?name={name}")
        with pytest.raises(web.HTTPBadRequest):
            await routes._handle_get_project(request)

    async def test_an_unknown_project_is_a_404(self, enabled: None, data_root: Path) -> None:
        request = make_mocked_request("GET", "/api/apps/papyrus/project?name=absent")
        with pytest.raises(web.HTTPNotFound):
            await routes._handle_get_project(request)


# ── projects ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestProjects:
    async def test_list_is_empty_on_a_fresh_install(self, enabled: None, data_root: Path) -> None:
        response = await routes._handle_list_projects(_request("GET", "/api/apps/papyrus/projects"))
        assert _json_of(response) == {"projects": []}

    async def test_list_reports_a_project(self, enabled: None, project: Path) -> None:
        response = await routes._handle_list_projects(_request("GET", "/api/apps/papyrus/projects"))
        projects = _json_of(response)["projects"]
        assert [p["name"] for p in projects] == ["my-paper"]

    async def test_create_writes_the_template_and_a_bib(self, enabled: None, data_root: Path) -> None:
        response = await routes._handle_create_project(
            _request("POST", "/api/apps/papyrus/projects", {"name": "New Paper"})
        )
        assert response.status == 201
        assert _json_of(response)["name"] == "new-paper"
        created = store.projects_dir() / "new-paper"
        assert r"\documentclass" in (created / "main.tex").read_text(encoding="utf-8")
        assert (created / "references.bib").is_file()

    async def test_create_accepts_a_custom_template(self, enabled: None, data_root: Path) -> None:
        await routes._handle_create_project(
            _request("POST", "/api/apps/papyrus/projects", {"name": "p", "template": "CUSTOM"})
        )
        assert (store.projects_dir() / "p" / "main.tex").read_text(encoding="utf-8") == "CUSTOM"

    async def test_create_refuses_a_duplicate(self, enabled: None, project: Path) -> None:
        with pytest.raises(web.HTTPConflict):
            await routes._handle_create_project(
                _request("POST", "/api/apps/papyrus/projects", {"name": "my-paper"})
            )

    async def test_create_reports_a_conflict_when_it_loses_the_mkdir_race(
        self, enabled: None, data_root: Path
    ) -> None:
        """The `exists()` probe narrows the window but cannot close it.

        Two worker threads can both pass it, and the loser's `mkdir` then raised an
        unhandled `FileExistsError` — a 500 for what is really a name clash the user can
        fix by picking another name. Simulated by letting the probe pass and having the
        directory appear before the `mkdir`, which is exactly the interleaving.
        """
        real_mkdir = Path.mkdir

        def _racing_mkdir(self, *args, **kwargs):  # noqa: ANN001, ANN202
            # The competitor wins between the check and this call.
            real_mkdir(self, parents=True, exist_ok=True)
            return real_mkdir(self, *args, **kwargs)

        with mock.patch.object(Path, "mkdir", _racing_mkdir):
            with pytest.raises(web.HTTPConflict):
                await routes._handle_create_project(
                    _request("POST", "/api/apps/papyrus/projects", {"name": "raced"})
                )

    async def test_an_oversized_template_strands_no_project_name(
        self, enabled: None, data_root: Path
    ) -> None:
        """Validated BEFORE anything is created.

        `write_file` refuses an oversized body, but by then `mkdir` has taken the name —
        so a 9 MiB template answered 500 and left an empty directory behind, making every
        later create of that name answer 409. The user could neither use the name nor see
        why.
        """
        oversized = "x" * (store.MAX_FILE_BYTES + 1)
        with pytest.raises(web.HTTPRequestEntityTooLarge):
            await routes._handle_create_project(
                _request(
                    "POST",
                    "/api/apps/papyrus/projects",
                    {"name": "too-big", "template": oversized},
                )
            )
        # No trace: the name is still free, so a retry with a sane template works.
        assert not (store.projects_dir() / "too-big").exists()
        await routes._handle_create_project(
            _request("POST", "/api/apps/papyrus/projects", {"name": "too-big"})
        )
        assert (store.projects_dir() / "too-big" / "main.tex").is_file()

    async def test_create_refuses_a_traversal(self, enabled: None, data_root: Path) -> None:
        with pytest.raises(web.HTTPBadRequest):
            await routes._handle_create_project(
                _request("POST", "/api/apps/papyrus/projects", {"name": "../escape"})
            )

    async def test_create_refuses_a_non_string_name(self, enabled: None, data_root: Path) -> None:
        """A truthy non-string must be a 400, not an AttributeError-shaped 500."""
        with pytest.raises(web.HTTPBadRequest):
            await routes._handle_create_project(
                _request("POST", "/api/apps/papyrus/projects", {"name": 42})
            )

    async def test_get_returns_the_metadata_the_frontend_needs(
        self, enabled: None, project: Path
    ) -> None:
        request = make_mocked_request("GET", "/api/apps/papyrus/project?name=my-paper")
        payload = _json_of(await routes._handle_get_project(request))
        assert payload["main_file"] == "main.tex"
        assert payload["files"] == ["main.tex", "references.bib"]
        assert payload["has_pdf"] is False

    async def test_get_404s_a_project_with_no_tex(self, enabled: None, data_root: Path) -> None:
        proj = store.projects_dir() / "not-a-paper"
        proj.mkdir(parents=True)
        (proj / "README.md").write_text("", encoding="utf-8")
        request = make_mocked_request("GET", "/api/apps/papyrus/project?name=not-a-paper")
        with pytest.raises(web.HTTPNotFound):
            await routes._handle_get_project(request)

    async def test_delete_removes_the_tree(self, enabled: None, project: Path) -> None:
        request = make_mocked_request("DELETE", "/api/apps/papyrus/project?name=my-paper")
        assert _json_of(await routes._handle_delete_project(request))["ok"] is True
        assert not project.exists()


@pytest.mark.asyncio
class TestClone:
    async def test_missing_url_is_a_400(self, enabled: None, data_root: Path) -> None:
        with pytest.raises(web.HTTPBadRequest):
            await routes._handle_clone_project(
                _request("POST", "/api/apps/papyrus/projects/clone", {})
            )

    async def test_clone_failure_is_a_422_carrying_git_output(
        self, enabled: None, data_root: Path
    ) -> None:
        error = gitops.GitError("git clone failed", output="fatal: repository not found")
        with mock.patch.object(gitops, "clone", mock.AsyncMock(side_effect=error)):
            response = await routes._handle_clone_project(
                _request(
                    "POST", "/api/apps/papyrus/projects/clone",
                    {"url": "https://example.com/g/p.git"},
                )
            )
        assert response.status == 422
        assert "not found" in _json_of(response)["output"]

    async def test_a_repo_with_no_tex_is_rejected_and_removed(
        self, enabled: None, data_root: Path
    ) -> None:
        """Leaving an unopenable project would also hold its name hostage."""
        async def fake_clone(_url: str, destination: Path) -> None:
            destination.mkdir(parents=True)
            (destination / "README.md").write_text("", encoding="utf-8")

        with mock.patch.object(gitops, "clone", fake_clone):
            response = await routes._handle_clone_project(
                _request(
                    "POST", "/api/apps/papyrus/projects/clone",
                    {"url": "https://example.com/g/p.git"},
                )
            )
        assert response.status == 422
        assert not (store.projects_dir() / "p").exists()

    async def test_derives_the_name_from_the_url(self, enabled: None, data_root: Path) -> None:
        async def fake_clone(_url: str, destination: Path) -> None:
            destination.mkdir(parents=True)
            (destination / "paper.tex").write_text("", encoding="utf-8")

        with mock.patch.object(gitops, "clone", fake_clone):
            response = await routes._handle_clone_project(
                _request(
                    "POST", "/api/apps/papyrus/projects/clone",
                    {"url": "https://example.com/g/My-Paper.git"},
                )
            )
        payload = _json_of(response)
        assert payload["name"] == "my-paper"
        assert payload["main_file"] == "paper.tex"


# ── files ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestFiles:
    async def test_list(self, enabled: None, project: Path) -> None:
        request = make_mocked_request("GET", "/api/apps/papyrus/files?name=my-paper")
        assert _json_of(await routes._handle_list_files(request))["files"] == [
            "main.tex", "references.bib",
        ]

    async def test_read(self, enabled: None, project: Path) -> None:
        request = make_mocked_request(
            "GET", "/api/apps/papyrus/file?name=my-paper&path=main.tex"
        )
        payload = _json_of(await routes._handle_read_file(request))
        assert payload["path"] == "main.tex"
        assert r"\documentclass" in payload["content"]

    async def test_read_missing_path_is_a_400(self, enabled: None, project: Path) -> None:
        request = make_mocked_request("GET", "/api/apps/papyrus/file?name=my-paper")
        with pytest.raises(web.HTTPBadRequest):
            await routes._handle_read_file(request)

    @pytest.mark.parametrize("path", ["../../etc/passwd", "/etc/passwd", "a\\b", "foo/../x"])
    async def test_read_refuses_a_traversal(
        self, enabled: None, project: Path, path: str
    ) -> None:
        request = make_mocked_request(
            "GET", f"/api/apps/papyrus/file?name=my-paper&path={path}"
        )
        with pytest.raises(web.HTTPBadRequest):
            await routes._handle_read_file(request)

    async def test_read_absent_file_is_a_404(self, enabled: None, project: Path) -> None:
        request = make_mocked_request(
            "GET", "/api/apps/papyrus/file?name=my-paper&path=absent.tex"
        )
        with pytest.raises(web.HTTPNotFound):
            await routes._handle_read_file(request)

    async def test_save(self, enabled: None, project: Path) -> None:
        response = await routes._handle_save_file(
            _request(
                "PUT", "/api/apps/papyrus/file",
                {"name": "my-paper", "path": "main.tex", "content": "NEW"},
            )
        )
        assert _json_of(response)["ok"] is True
        assert (project / "main.tex").read_text(encoding="utf-8") == "NEW"

    async def test_save_requires_string_content(self, enabled: None, project: Path) -> None:
        with pytest.raises(web.HTTPBadRequest):
            await routes._handle_save_file(
                _request(
                    "PUT", "/api/apps/papyrus/file",
                    {"name": "my-paper", "path": "main.tex", "content": {"not": "a string"}},
                )
            )

    async def test_save_refuses_a_traversal(self, enabled: None, project: Path) -> None:
        with pytest.raises(web.HTTPBadRequest):
            await routes._handle_save_file(
                _request(
                    "PUT", "/api/apps/papyrus/file",
                    {"name": "my-paper", "path": "../evil.tex", "content": "x"},
                )
            )

    async def test_create(self, enabled: None, project: Path) -> None:
        response = await routes._handle_create_file(
            _request("POST", "/api/apps/papyrus/file", {"name": "my-paper", "path": "methods.tex"})
        )
        assert response.status == 201
        assert (project / "methods.tex").is_file()

    async def test_create_refuses_a_duplicate(self, enabled: None, project: Path) -> None:
        with pytest.raises(web.HTTPConflict):
            await routes._handle_create_file(
                _request(
                    "POST", "/api/apps/papyrus/file", {"name": "my-paper", "path": "main.tex"}
                )
            )

    async def test_delete(self, enabled: None, project: Path) -> None:
        request = make_mocked_request(
            "DELETE", "/api/apps/papyrus/file?name=my-paper&path=references.bib"
        )
        assert _json_of(await routes._handle_delete_file(request))["ok"] is True
        assert not (project / "references.bib").exists()

    async def test_delete_refuses_the_main_document(self, enabled: None, project: Path) -> None:
        request = make_mocked_request(
            "DELETE", "/api/apps/papyrus/file?name=my-paper&path=main.tex"
        )
        with pytest.raises(web.HTTPBadRequest):
            await routes._handle_delete_file(request)

    async def test_set_main(self, enabled: None, project: Path) -> None:
        (project / "thesis.tex").write_text("", encoding="utf-8")
        response = await routes._handle_set_main(
            _request("PUT", "/api/apps/papyrus/main", {"name": "my-paper", "path": "thesis.tex"})
        )
        assert _json_of(response)["main_file"] == "thesis.tex"
        assert store.get_main_file(project) == "thesis.tex"

    async def test_set_main_404s_an_absent_file(self, enabled: None, project: Path) -> None:
        with pytest.raises(web.HTTPNotFound):
            await routes._handle_set_main(
                _request(
                    "PUT", "/api/apps/papyrus/main", {"name": "my-paper", "path": "absent.tex"}
                )
            )

    async def test_set_main_refuses_a_traversal(self, enabled: None, project: Path) -> None:
        with pytest.raises(web.HTTPBadRequest):
            await routes._handle_set_main(
                _request(
                    "PUT", "/api/apps/papyrus/main", {"name": "my-paper", "path": "../evil.tex"}
                )
            )


# ── compile + pdf ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestCompile:
    async def test_a_successful_compile_is_a_200(self, enabled: None, project: Path) -> None:
        result = latex.CompileResult(ok=True, log="ok", duration_ms=1234)
        with mock.patch.object(latex, "compile_project", mock.AsyncMock(return_value=result)):
            response = await routes._handle_compile(
                _request("POST", "/api/apps/papyrus/compile", {"name": "my-paper"})
            )
        assert response.status == 200
        payload = _json_of(response)
        assert payload["ok"] is True
        assert payload["duration_ms"] == 1234

    async def test_a_failed_compile_is_a_422_that_still_carries_the_log(
        self, enabled: None, project: Path
    ) -> None:
        """The log is exactly what the user needs to fix the document, so it must
        survive the non-2xx status."""
        result = latex.CompileResult(
            ok=False,
            log="./main.tex:9: Undefined control sequence.",
            diagnostics=latex.parse_log("./main.tex:9: Undefined control sequence."),
        )
        with mock.patch.object(latex, "compile_project", mock.AsyncMock(return_value=result)):
            response = await routes._handle_compile(
                _request("POST", "/api/apps/papyrus/compile", {"name": "my-paper"})
            )
        assert response.status == 422
        payload = _json_of(response)
        assert payload["ok"] is False
        assert payload["errors"][0]["line"] == 9

    async def test_a_sandbox_refusal_carries_the_remedy_in_log(
        self, enabled: None, project: Path
    ) -> None:
        """The remedy must ride `log`, because that is the field the client renders.

        `api.ts`'s contract is "if the body has `ok`, it IS a CompileResult", so a
        body carrying `ok` plus only a sibling `error` short-circuits that branch and
        the remedy is discarded — leaving an empty diagnostics pane on the one
        failure a user cannot diagnose from the document. Reachable on every Windows
        host, which has no OS sandbox backend at all.
        """
        result = latex.CompileResult(
            ok=False, sandbox_error="Sandbox backend unavailable: set agent.foo=true"
        )
        with mock.patch.object(latex, "compile_project", mock.AsyncMock(return_value=result)):
            response = await routes._handle_compile(
                _request("POST", "/api/apps/papyrus/compile", {"name": "my-paper"})
            )
        assert response.status == 422
        payload = _json_of(response)
        assert payload["code"] == "compiler_sandbox_unavailable"
        # The whole CompileResult shape, so the client's `'ok' in data` branch
        # renders it instead of falling through to a bare status line.
        assert payload["ok"] is False
        assert "agent.foo=true" in payload["log"]
        assert payload["errors"] == []
        assert payload["duration_ms"] == 0

    async def test_compiles_the_resolved_main_document(self, enabled: None, project: Path) -> None:
        (project / "main.tex").unlink()
        (project / "amlc.tex").write_text("", encoding="utf-8")
        compile_mock = mock.AsyncMock(return_value=latex.CompileResult(ok=True))
        with mock.patch.object(latex, "compile_project", compile_mock):
            await routes._handle_compile(
                _request("POST", "/api/apps/papyrus/compile", {"name": "my-paper"})
            )
        assert compile_mock.await_args is not None
        assert compile_mock.await_args.args[1] == "amlc.tex"

    async def test_404s_a_project_with_no_tex(self, enabled: None, data_root: Path) -> None:
        proj = store.projects_dir() / "empty"
        proj.mkdir(parents=True)
        with pytest.raises(web.HTTPNotFound):
            await routes._handle_compile(
                _request("POST", "/api/apps/papyrus/compile", {"name": "empty"})
            )


@pytest.mark.asyncio
class TestPdf:
    async def test_404s_before_a_compile(self, enabled: None, project: Path) -> None:
        request = make_mocked_request("GET", "/api/apps/papyrus/pdf?name=my-paper")
        with pytest.raises(web.HTTPNotFound):
            await routes._handle_pdf(request)

    async def test_serves_the_pdf_bytes(self, enabled: None, project: Path) -> None:
        (project / "main.pdf").write_bytes(b"%PDF-1.4 body")
        request = make_mocked_request("GET", "/api/apps/papyrus/pdf?name=my-paper")
        response = await routes._handle_pdf(request)
        # A `FileResponse`, which STREAMS from disk. It used to be a buffered
        # `web.Response` built from `pdf.read_bytes()`, which put the whole file in
        # gateway memory — and a PDF's size is decided by the document being compiled
        # (or by a cloned repo shipping a large `main.pdf`), so one open of the viewer
        # could exhaust the process. Asserting on the PATH rather than `.body`,
        # because a streamed response has no body until it is prepared.
        assert isinstance(response, web.FileResponse)
        assert response._path.read_bytes() == b"%PDF-1.4 body"
        assert response.headers["Content-Type"] == "application/pdf"

    async def test_carries_a_restrictive_csp(self, enabled: None, project: Path) -> None:
        """The PDF is content the agent or a cloned repo produced, so it must not be
        able to script the dashboard's origin."""
        (project / "main.pdf").write_bytes(b"%PDF-1.4")
        request = make_mocked_request("GET", "/api/apps/papyrus/pdf?name=my-paper")
        response = await routes._handle_pdf(request)
        csp = response.headers["Content-Security-Policy"]
        assert "sandbox" in csp
        assert "default-src 'none'" in csp
        assert response.headers["X-Content-Type-Options"] == "nosniff"

    async def test_is_served_inline_and_not_cached(self, enabled: None, project: Path) -> None:
        (project / "main.pdf").write_bytes(b"%PDF-1.4")
        request = make_mocked_request("GET", "/api/apps/papyrus/pdf?name=my-paper")
        response = await routes._handle_pdf(request)
        assert response.headers["Content-Disposition"].startswith("inline")
        assert response.headers["Cache-Control"] == "no-store"

    async def test_the_pdf_is_never_buffered_whole(self, enabled: None, project: Path) -> None:
        """Structural: the handler must not read the file into memory.

        The size is attacker-influenced (a cloned repo can ship a large `main.pdf`,
        and a document decides its own output size), so a buffered read is an OOM of
        the whole gateway — every chat session, cron and the heartbeat with it.
        """
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(routes))
        handler = next(
            fn
            for fn in ast.walk(tree)
            if isinstance(fn, ast.AsyncFunctionDef) and fn.name == "_handle_pdf"
        )
        body = ast.dump(handler)
        assert "attr='read_bytes'" not in body, (
            "_handle_pdf reads the PDF into memory again; stream it with FileResponse"
        )
        assert "FileResponse" in body

    async def test_follows_a_non_default_main_document(self, enabled: None, project: Path) -> None:
        (project / "amlc.tex").write_text("", encoding="utf-8")
        store.set_main_file(project, "amlc.tex")
        (project / "amlc.pdf").write_bytes(b"%PDF-amlc")
        request = make_mocked_request("GET", "/api/apps/papyrus/pdf?name=my-paper")
        response = await routes._handle_pdf(request)
        assert isinstance(response, web.FileResponse)
        assert response._path.read_bytes() == b"%PDF-amlc"


# ── git ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestGitRoutes:
    async def test_status_reports_a_non_repo(self, enabled: None, project: Path) -> None:
        request = make_mocked_request("GET", "/api/apps/papyrus/git?name=my-paper")
        assert _json_of(await routes._handle_git_status(request)) == {"is_git": False}

    async def test_status_surfaces_a_probe_failure_without_a_5xx(
        self, enabled: None, project: Path
    ) -> None:
        """A broken git must leave the toolbar quiet, not fail the whole view."""
        with mock.patch.object(
            gitops, "status", mock.AsyncMock(side_effect=gitops.GitError("git is not installed"))
        ):
            request = make_mocked_request("GET", "/api/apps/papyrus/git?name=my-paper")
            payload = _json_of(await routes._handle_git_status(request))
        assert payload["is_git"] is False
        assert "not installed" in payload["error"]

    async def test_commit_success(self, enabled: None, project: Path) -> None:
        with mock.patch.object(gitops, "commit", mock.AsyncMock(return_value="[main abc] msg")):
            response = await routes._handle_git_commit(
                _request(
                    "POST", "/api/apps/papyrus/git/commit",
                    {"name": "my-paper", "message": "msg"},
                )
            )
        assert _json_of(response)["ok"] is True

    async def test_commit_defaults_the_message(self, enabled: None, project: Path) -> None:
        commit_mock = mock.AsyncMock(return_value="")
        with mock.patch.object(gitops, "commit", commit_mock):
            await routes._handle_git_commit(
                _request("POST", "/api/apps/papyrus/git/commit", {"name": "my-paper"})
            )
        assert commit_mock.await_args is not None
        assert commit_mock.await_args.args[1] == gitops.DEFAULT_COMMIT_MESSAGE

    async def test_commit_failure_is_a_422(self, enabled: None, project: Path) -> None:
        with mock.patch.object(
            gitops, "commit", mock.AsyncMock(side_effect=gitops.GitError("nope", output="detail"))
        ):
            response = await routes._handle_git_commit(
                _request("POST", "/api/apps/papyrus/git/commit", {"name": "my-paper"})
            )
        assert response.status == 422
        assert _json_of(response)["output"] == "detail"

    async def test_push_auth_failure_is_a_401(self, enabled: None, project: Path) -> None:
        """Distinct from 422 so the UI can say "log in" rather than "it broke"."""
        error = gitops.GitError("authentication failed", output="denied", auth=True)
        with mock.patch.object(gitops, "push", mock.AsyncMock(side_effect=error)):
            response = await routes._handle_git_push(
                _request("POST", "/api/apps/papyrus/git/push", {"name": "my-paper"})
            )
        assert response.status == 401

    async def test_push_other_failure_is_a_422(self, enabled: None, project: Path) -> None:
        error = gitops.GitError("rejected", output="non-fast-forward")
        with mock.patch.object(gitops, "push", mock.AsyncMock(side_effect=error)):
            response = await routes._handle_git_push(
                _request("POST", "/api/apps/papyrus/git/push", {"name": "my-paper"})
            )
        assert response.status == 422

    async def test_pull_success_reports_whether_it_stashed(
        self, enabled: None, project: Path
    ) -> None:
        with mock.patch.object(gitops, "pull", mock.AsyncMock(return_value=("Fast-forward", True))):
            response = await routes._handle_git_pull(
                _request("POST", "/api/apps/papyrus/git/pull", {"name": "my-paper"})
            )
        payload = _json_of(response)
        assert payload["ok"] is True
        assert payload["stashed"] is True

    async def test_pull_conflict_is_a_409(self, enabled: None, project: Path) -> None:
        error = gitops.GitConflict("conflict", output="CONFLICT in main.tex")
        with mock.patch.object(gitops, "pull", mock.AsyncMock(side_effect=error)):
            response = await routes._handle_git_pull(
                _request("POST", "/api/apps/papyrus/git/pull", {"name": "my-paper"})
            )
        assert response.status == 409
        assert "CONFLICT" in _json_of(response)["output"]

    async def test_pull_other_failure_is_a_422(self, enabled: None, project: Path) -> None:
        with mock.patch.object(
            gitops, "pull", mock.AsyncMock(side_effect=gitops.GitError("offline"))
        ):
            response = await routes._handle_git_pull(
                _request("POST", "/api/apps/papyrus/git/pull", {"name": "my-paper"})
            )
        assert response.status == 422


# ── health ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestHealth:
    async def test_reports_the_compiler_basename_and_git(self, enabled: None) -> None:
        with mock.patch.object(
            latex, "find_compiler", mock.AsyncMock(return_value="/usr/bin/pdflatex")
        ), mock.patch.object(gitops, "git_available", return_value=True), mock.patch.object(
            tectonic, "managed_status", return_value={"installed": False}
        ):
            payload = _json_of(await routes._handle_health(_request("GET", "/api/apps/papyrus/health")))
        assert payload["status"] == "ok"
        assert payload["compiler"] == "pdflatex"
        assert payload["git"] is True

    async def test_reports_an_absent_compiler_as_empty(self, enabled: None) -> None:
        with mock.patch.object(latex, "find_compiler", mock.AsyncMock(return_value=None)), \
                mock.patch.object(gitops, "git_available", return_value=False), \
                mock.patch.object(tectonic, "managed_status", return_value={"installed": False}):
            payload = _json_of(await routes._handle_health(_request("GET", "/api/apps/papyrus/health")))
        assert payload["compiler"] == ""
        assert payload["git"] is False

    async def test_carries_the_managed_compiler_block(self, enabled: None) -> None:
        """The UI drives both the warning banner and the install progress from ONE
        poll, so /health must carry the managed-install status."""
        managed = {
            "supported": True,
            "installed": False,
            "release": tectonic.TECTONIC_RELEASE_TAG,
            "version": tectonic.TECTONIC_VERSION,
            "job": {"state": "downloading", "bytes_downloaded": 1024},
        }
        with mock.patch.object(latex, "find_compiler", mock.AsyncMock(return_value=None)), \
                mock.patch.object(gitops, "git_available", return_value=False), \
                mock.patch.object(tectonic, "managed_status", return_value=managed):
            payload = _json_of(await routes._handle_health(_request("GET", "/api/apps/papyrus/health")))
        assert payload["managed"] == managed


# ── managed compiler provisioning ────────────────────────────────────────────


@pytest.mark.asyncio
class TestProvisionCompiler:
    """``POST /compiler/provision`` — the endpoint that makes a stock machine work.

    It downloads ONE digest-pinned binary into the app's own data dir. It is NOT
    the system-package install ``pptx-maker`` refuses (no package manager, no
    privilege, nothing outside the data dir) — see ``tectonic.py``'s docstring.
    """

    async def test_starts_a_background_job_and_answers_202(self, enabled: None) -> None:
        """202 + poll, never a held-open request: the transfer is ~10-22MB."""
        with mock.patch.object(tectonic, "binary_installed", return_value=False), \
                mock.patch.object(tectonic, "platform_supported", return_value=True), \
                mock.patch.object(
                    tectonic, "provision_in_background", return_value=True
                ) as start:
            response = await routes._handle_provision_compiler(
                _request("POST", "/api/apps/papyrus/compiler/provision")
            )
        assert response.status == 202
        assert _json_of(response)["state"] == tectonic.STATE_DOWNLOADING
        assert start.call_count == 1

    async def test_an_already_installed_compiler_does_not_download_again(
        self, enabled: None
    ) -> None:
        with mock.patch.object(tectonic, "binary_installed", return_value=True), \
                mock.patch.object(
                    tectonic, "provision_in_background", return_value=True
                ) as start:
            response = await routes._handle_provision_compiler(
                _request("POST", "/api/apps/papyrus/compiler/provision")
            )
        assert response.status == 200
        assert _json_of(response)["state"] == tectonic.STATE_DONE
        assert start.call_count == 0, "idempotent — must not re-download"

    async def test_an_unsupported_platform_is_a_422_with_a_code(self, enabled: None) -> None:
        """No pinned build (32-bit Linux, BSD, Windows-on-ARM) must degrade with an
        actionable message, leaving the manual install path intact."""
        with mock.patch.object(tectonic, "binary_installed", return_value=False), \
                mock.patch.object(tectonic, "platform_supported", return_value=False), \
                mock.patch.object(
                    tectonic, "provision_in_background", return_value=True
                ) as start:
            response = await routes._handle_provision_compiler(
                _request("POST", "/api/apps/papyrus/compiler/provision")
            )
        assert response.status == 422
        assert _json_of(response)["code"] == "compiler_unsupported_platform"
        assert start.call_count == 0

    async def test_a_second_click_while_running_does_not_start_a_second_job(
        self, enabled: None
    ) -> None:
        with mock.patch.object(tectonic, "binary_installed", return_value=False), \
                mock.patch.object(tectonic, "platform_supported", return_value=True), \
                mock.patch.object(tectonic, "provision_in_background", return_value=False):
            response = await routes._handle_provision_compiler(
                _request("POST", "/api/apps/papyrus/compiler/provision")
            )
        assert response.status == 202

    async def test_the_handler_never_blocks_the_event_loop(self, enabled: None) -> None:
        """Every filesystem/network probe on this path must be offloaded.

        ``asyncio.to_thread`` is patched to REFUSE, so a handler that called a
        blocking helper inline would still pass — instead the test asserts each
        blocking call arrived through ``to_thread``, which is the invariant
        ``no-blocking-call-on-event-loop`` actually needs.
        """
        seen: list[str] = []
        real_to_thread = asyncio.to_thread

        async def recording_to_thread(fn, *args, **kwargs):  # noqa: ANN001, ANN202
            seen.append(getattr(fn, "__name__", repr(fn)))
            return await real_to_thread(fn, *args, **kwargs)

        with mock.patch.object(tectonic, "binary_installed", return_value=False), \
                mock.patch.object(tectonic, "platform_supported", return_value=True), \
                mock.patch.object(tectonic, "provision_in_background", return_value=True), \
                mock.patch.object(routes.asyncio, "to_thread", recording_to_thread):
            await routes._handle_provision_compiler(
                _request("POST", "/api/apps/papyrus/compiler/provision")
            )
        # Every blocking probe on this path arrived through to_thread. Matched as a
        # substring because a patched callable's repr carries the mock's name.
        joined = " ".join(seen)
        for probe in ("binary_installed", "platform_supported", "provision_in_background"):
            assert probe in joined, f"{probe} ran inline on the event loop"


# ── body handling ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestBodyHandling:
    async def test_a_non_json_body_is_a_400(self, enabled: None) -> None:
        request = make_mocked_request("POST", "/api/apps/papyrus/projects")
        request.json = mock.AsyncMock(side_effect=ValueError("not json"))  # type: ignore[method-assign]
        with pytest.raises(web.HTTPBadRequest):
            await routes._json_body(request)

    async def test_a_non_object_body_is_a_400(self, enabled: None) -> None:
        request = make_mocked_request("POST", "/api/apps/papyrus/projects")
        request.json = mock.AsyncMock(return_value=["a", "list"])  # type: ignore[method-assign]
        with pytest.raises(web.HTTPBadRequest):
            await routes._json_body(request)


class TestFieldCoercion:
    def test_str_field_coerces_a_non_string_to_empty(self) -> None:
        """A truthy non-string must read as MISSING (a 400), not stringify into a
        value that then fails a later, less specific check."""
        assert routes._str_field({"name": 1}, "name") == ""
        assert routes._str_field({"name": []}, "name") == ""
        assert routes._str_field({"name": "  ok  "}, "name") == "ok"
        assert routes._str_field({}, "name") == ""


# ── redaction of subprocess output ───────────────────────────────────────────


_FAKE_PAT = "ghp_" + "A" * 36
_FAKE_AWS_SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"


@pytest.mark.asyncio
class TestSubprocessOutputIsRedacted:
    """Compiler and ``git`` output must not carry credentials to the dashboard.

    ``backend-security-controls`` requires both redaction passes before content
    reaches an external surface. Neither of these two surfaces is authored by
    this app: a ``.tex`` is written by the agent or arrives wholesale from
    ``POST /projects/clone`` and can emit anything into the log, and a failed
    push makes ``git`` echo the remote URL — which is exactly where a token
    embedded in a remote lives.
    """

    async def test_compile_log_and_diagnostics_are_redacted(
        self, enabled: None, project: Path
    ) -> None:
        leaky = f"! LaTeX Error: aws_secret_access_key = {_FAKE_AWS_SECRET}"
        result = latex.CompileResult(
            ok=False,
            log=leaky,
            diagnostics=[latex.Diagnostic(level="error", message=leaky, line=7, file="main.tex")],
        )
        with mock.patch.object(latex, "compile_project", mock.AsyncMock(return_value=result)):
            response = await routes._handle_compile(
                _request("POST", "/api/apps/papyrus/compile", {"name": "my-paper"})
            )
        payload = _json_of(response)
        assert _FAKE_AWS_SECRET not in payload["log"]
        assert "[REDACTED" in payload["log"]
        assert _FAKE_AWS_SECRET not in payload["errors"][0]["message"]

    async def test_the_diagnostic_file_field_is_redacted_too(
        self, enabled: None, project: Path
    ) -> None:
        """`file` is parsed out of the SAME compiler line as `message`.

        A `\\input{...}` names any path the document chose and TeX echoes it back, so
        redacting `message` and not its sibling left the identical leak one key over.
        """
        leaky_path = f"/tmp/{_FAKE_AWS_SECRET}.tex"
        result = latex.CompileResult(
            ok=False,
            log="! LaTeX Error",
            diagnostics=[
                latex.Diagnostic(level="error", message="broken", line=7, file=leaky_path)
            ],
        )
        with mock.patch.object(latex, "compile_project", mock.AsyncMock(return_value=result)):
            response = await routes._handle_compile(
                _request("POST", "/api/apps/papyrus/compile", {"name": "my-paper"})
            )
        payload = _json_of(response)
        assert _FAKE_AWS_SECRET not in payload["errors"][0]["file"]
        assert "[REDACTED" in payload["errors"][0]["file"]

    async def test_the_git_branch_name_is_redacted_too(
        self, enabled: None, project: Path
    ) -> None:
        """A ref name is chosen by whoever authored the repository, it is rendered in
        the toolbar, and a branch can legally be named almost anything. It was the one
        string field in this payload that skipped the pass — the same omission shape as
        the diagnostic `file`: redact the list fields, miss the scalar beside them."""
        status = gitops.GitStatus(
            is_git=True,
            branch=f"feature/{_FAKE_AWS_SECRET}",
            changes=["M main.tex"],
            recent_commits=["abc123 fix"],
        )
        with mock.patch.object(gitops, "status", mock.AsyncMock(return_value=status)):
            response = await routes._handle_git_status(
                _request("GET", "/api/apps/papyrus/git/status?name=my-paper", None)
            )
        payload = _json_of(response)
        assert _FAKE_AWS_SECRET not in payload["branch"]
        assert "[REDACTED" in payload["branch"]

    async def test_a_push_failure_does_not_echo_the_remote_token(
        self, enabled: None, project: Path
    ) -> None:
        stderr = (
            "remote: Invalid username or password\n"
            f"fatal: Authentication failed for 'https://user:{_FAKE_PAT}@github.com/o/r.git/'"
        )
        exc = gitops.GitError("push failed", output=stderr, auth=True)
        with mock.patch.object(gitops, "push", mock.AsyncMock(side_effect=exc)):
            response = await routes._handle_git_push(
                _request("POST", "/api/apps/papyrus/git/push", {"name": "my-paper"})
            )
        assert response.status == 401
        body = json.dumps(_json_of(response))
        assert _FAKE_PAT not in body
        assert "[REDACTED" in body

    async def test_git_status_commit_subjects_are_redacted(
        self, enabled: None, project: Path
    ) -> None:
        status = gitops.GitStatus(
            is_git=True,
            branch="main",
            recent_commits=[f"abc1234 wire up token {_FAKE_PAT}"],
            changes=[f" M creds-{_FAKE_AWS_SECRET}.tex"],
        )
        with mock.patch.object(gitops, "status", mock.AsyncMock(return_value=status)):
            response = await routes._handle_git_status(
                _request("GET", "/api/apps/papyrus/git?name=my-paper")
            )
        body = json.dumps(_json_of(response))
        assert _FAKE_PAT not in body
        assert _FAKE_AWS_SECRET not in body

    async def test_file_content_is_deliberately_not_redacted(
        self, enabled: None, project: Path
    ) -> None:
        """The editor writes this value straight back, so redacting it would
        overwrite the user's own source with ``[REDACTED]`` markers."""
        source = f"% my key is {_FAKE_AWS_SECRET}\n\\documentclass{{article}}\n"
        (project / "main.tex").write_text(source, encoding="utf-8")
        response = await routes._handle_read_file(
            _request("GET", "/api/apps/papyrus/file?name=my-paper&path=main.tex")
        )
        assert _json_of(response)["content"] == source


class TestCleanFailsClosed:
    """No async work here, so this stays outside the ``asyncio``-marked class."""

    def test_clean_withholds_output_without_the_security_module(self) -> None:
        with mock.patch.object(routes, "_HAS_SECURITY", False):
            assert routes._clean("anything at all") == "[output withheld: redaction unavailable]"


# ── event-loop discipline ────────────────────────────────────────────────────


class TestNoBlockingCallsOnTheLoop:
    """No route handler may touch the filesystem inline — including to VALIDATE.

    The gateway runs every session, cron job and the liveness heartbeat on ONE
    asyncio loop, so a synchronous filesystem call in an ``async def`` freezes all
    of them until the watchdog kills the process — the wedge the AUTOSDE
    ``no-blocking-call-on-event-loop`` rule exists to prevent.

    **The path-validation gate was the reported instance.** ``_project`` /
    ``_project_for_create`` / ``_safe_relative`` look like cheap string checks but
    each calls ``Path.resolve()`` plus a ``stat``-family probe, so a
    ``KIROCREW_HOME`` on a stalled network mount wedges the gateway inside the
    authorization check — before the handler has done any of its own work. Twenty-two
    call sites across the handlers had that shape.

    This is an AST assertion rather than a per-handler source grep so a NEW handler
    that validates or reads inline fails too, without anyone remembering to extend a
    list. A handler may NAME a blocking helper (handing it to ``asyncio.to_thread``);
    it may never CALL one.
    """

    #: Module-level helpers in ``routes`` itself that block. The validation gate is
    #: the headline: it resolves and stats before any handler work happens.
    _BLOCKING_LOCAL_FNS = frozenset({"_project", "_project_for_create", "_safe_relative"})

    #: Every ``store`` function that resolves, opens, walks, stats, or writes.
    _BLOCKING_STORE_FNS = frozenset(
        {
            "data_dir",
            "projects_dir",
            "safe_project_dir",
            "safe_child",
            "read_project_config",
            "write_project_config",
            "get_main_file",
            "resolve_main_file",
            "set_main_file",
            "list_files",
            "list_projects",
            # `pdf_path` was pure path arithmetic when this list was written, so it was
            # correctly absent. Routing it through `safe_child` for containment gave it
            # a `Path.resolve()` and a sensitive-path probe — real syscalls — and the
            # inline call in `latex.compile_project` then blocked the loop on a stalled
            # network mount. Listing it is what stops a security fix silently
            # reintroducing an event-loop stall.
            "pdf_path",
            "read_text_file",
            "write_file",
            "create_file",
            "delete_file",
        }
    )

    #: Synchronous helpers in the app's sibling backend modules. Each module's own
    #: ``async`` wrapper (``latex.find_compiler``, ``gitops.status``) is fine — these
    #: are the raw sync entry points that must be offloaded.
    _BLOCKING_MODULE_FNS = frozenset(
        {
            ("shutil", "rmtree"),
            ("shutil", "which"),
            ("shutil", "copy2"),
            ("shutil", "move"),
            ("gitops", "is_git_repo"),
            ("gitops", "git_available"),
            ("latex", "find_compiler_sync"),
            ("tectonic", "binary_installed"),
            ("tectonic", "platform_supported"),
            ("tectonic", "managed_status"),
            ("tectonic", "provision_in_background"),
            ("tectonic", "binary_path"),
            ("tectonic", "vendor_dir"),
        }
    )

    #: ``pathlib.Path`` methods that are syscalls. Matched on the attribute name
    #: alone, because the receiver is a local (``project``, ``pdf``, ``tex``) whose
    #: type the AST cannot see — over-matching here is the safe direction, and the
    #: names are specific enough not to collide with the app's own vocabulary.
    _BLOCKING_PATH_METHODS = frozenset(
        {
            "is_dir",
            "is_file",
            "exists",
            "is_symlink",
            "mkdir",
            "rmdir",
            "iterdir",
            "glob",
            "rglob",
            "stat",
            "lstat",
            "read_text",
            "write_text",
            "read_bytes",
            "write_bytes",
            "unlink",
            "rename",
            "replace",
            "touch",
            "chmod",
            "resolve",
            "samefile",
        }
    )

    #: Bare-name builtins/imports that block.
    #:
    #: ``sandboxed_spawn_argv`` does not look like a syscall, which is exactly why
    #: it is here: it reaches ``wrap_argv`` -> ``detect_backend``, which cold-probes
    #: the sandbox backend with a synchronous ``subprocess.run(..., timeout=5)``.
    #: On macOS nothing warms that cache first (``prewarm_backend()`` returns early
    #: on non-Linux), so the FIRST compile or push of the gateway's lifetime stalls
    #: the single loop for up to five seconds — chat, cron and the liveness
    #: heartbeat with it. Both call sites offload it; this is what keeps them that
    #: way.
    _BLOCKING_BARE_FNS = frozenset(
        {"open", "is_app_enabled", "sandboxed_spawn_argv"}
    )

    def _route_modules(self) -> list:
        """Every app module whose ``async def``s are scanned.

        ``latex`` and ``gitops`` are included, not just ``routes``: they are where
        the spawns live, and the blocking cold-probe above was reached from an
        ``async def`` in each of them rather than from a handler.
        """
        from kiro_crew.apps.builtins.papyrus.backend import gitops as papyrus_gitops
        from kiro_crew.apps.builtins.papyrus.backend import latex as papyrus_latex
        from kiro_crew.apps.builtins.papyrus.backend import routes as papyrus_routes

        return [papyrus_routes, papyrus_latex, papyrus_gitops]

    def _inline_blocking_calls(self, module) -> list[str]:
        """``file:line handler -> callee()`` for every blocking call in an ``async def``."""
        import inspect

        return self._scan(inspect.getsource(module), module.__name__)

    def _scan(self, source: str, label: str) -> list[str]:
        """``label:line handler -> callee()`` for every blocking call in an ``async def``.

        Nested plain ``def``s are skipped: a sync closure inside an ``async def`` is
        exactly what ``asyncio.to_thread`` runs, so its body is already off the loop.
        That is also the fix's shape — validation moves INTO such a closure alongside
        the work it authorizes — so the guard must not flag it.
        """
        import ast

        tree = ast.parse(source)
        offenders: list[str] = []
        for handler in [n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)]:
            nested = {
                id(n)
                for outer in ast.walk(handler)
                if isinstance(outer, ast.FunctionDef)
                for n in ast.walk(outer)
            }
            for node in ast.walk(handler):
                if id(node) in nested or not isinstance(node, ast.Call):
                    continue
                callee = node.func
                if isinstance(callee, ast.Attribute):
                    owner = callee.value
                    owner_id = owner.id if isinstance(owner, ast.Name) else "<expr>"
                    blocking = (
                        callee.attr in self._BLOCKING_PATH_METHODS
                        or (owner_id == "store" and callee.attr in self._BLOCKING_STORE_FNS)
                        or (owner_id, callee.attr) in self._BLOCKING_MODULE_FNS
                    )
                    callee_name = f"{owner_id}.{callee.attr}"
                elif isinstance(callee, ast.Name):
                    blocking = (
                        callee.id in self._BLOCKING_LOCAL_FNS
                        or callee.id in self._BLOCKING_BARE_FNS
                    )
                    callee_name = callee.id
                else:
                    continue
                if blocking:
                    offenders.append(f"{label}:{node.lineno} {handler.name} -> {callee_name}()")
        return offenders

    def test_no_handler_blocks_the_loop(self) -> None:
        offenders: list[str] = []
        for module in self._route_modules():
            offenders.extend(self._inline_blocking_calls(module))
        assert offenders == [], (
            "these run blocking filesystem IO on the gateway event loop; move them "
            "into a sync helper behind ONE asyncio.to_thread hop (group a handler's "
            "validation together with the work it authorizes):\n  " + "\n  ".join(offenders)
        )

    def test_the_guard_detects_an_inline_call(self) -> None:
        """The guard must actually fire — a scanner that never trips is not a gate.

        Runs the real detector over source shaped exactly like the reported defect, so
        a future refactor that quietly breaks the AST walk (a renamed attribute, a node
        type the walk stops visiting) fails here instead of going green on zero
        findings.
        """
        offenders = self._scan(
            "async def _handle_thing(request):\n"
            "    project = _project(request.query['name'])\n"
            "    if not project.is_dir():\n"
            "        raise web.HTTPNotFound()\n"
            "    return store.list_files(project)\n",
            "synthetic",
        )
        assert len(offenders) == 3, offenders
        assert any("_project()" in o for o in offenders)
        assert any("project.is_dir()" in o for o in offenders)
        assert any("store.list_files()" in o for o in offenders)

    def test_the_guard_allows_a_grouped_offload(self) -> None:
        """The fix's own shape must pass, or the guard would forbid the remedy.

        A blocking call inside a nested sync closure — and the same helper NAMED as a
        ``to_thread`` argument — are both correct and must not be reported.
        """
        offenders = self._scan(
            "async def _handle_thing(request):\n"
            "    name = request.query['name']\n"
            "    def _work():\n"
            "        project = _project(name)\n"
            "        return store.list_files(project)\n"
            "    files = await asyncio.to_thread(_work)\n"
            "    other = await asyncio.to_thread(store.list_projects)\n"
            "    return files, other\n",
            "synthetic",
        )
        assert offenders == [], offenders

    def test_the_reported_gate_is_offloaded_everywhere_it_is_used(self) -> None:
        """The exact helpers the CI reviewer flagged, at every call site.

        ``safe_project_dir`` / ``safe_child`` are the containment gate, so they run in
        EVERY path-bearing handler. Each occurrence must therefore be inside a sync
        closure or an ``asyncio.to_thread`` argument.
        """
        import inspect

        src = inspect.getsource(routes)
        assert "_project(" in src, "the gate helper must still exist"
        # Every `async def` body is clean per the AST walk above; assert the two
        # store entry points are only ever reached from sync code.
        assert self._inline_blocking_calls(routes) == []

    def test_every_path_handler_takes_one_thread_hop(self) -> None:
        """Validation and the work it authorizes must not straddle two hops.

        Two ``to_thread`` awaits with the check in the first and the use in the second
        widens the check/use window: another request runs in the gap. The handlers
        whose "use" is itself an ``await`` (a git subprocess, the compiler) cannot
        group and are asserted separately in ``test_the_subprocess_handlers_...``.
        """
        import inspect

        for handler in (
            routes._handle_create_project,
            routes._handle_get_project,
            routes._handle_delete_project,
            routes._handle_list_files,
            routes._handle_read_file,
            routes._handle_save_file,
            routes._handle_create_file,
            routes._handle_delete_file,
            routes._handle_set_main,
            routes._handle_pdf,
        ):
            src = inspect.getsource(handler)
            hops = src.count("asyncio.to_thread")
            assert hops == 1, (
                f"{handler.__name__} makes {hops} thread hops; group its validation "
                "and filesystem work into ONE sync helper"
            )

    def test_the_subprocess_handlers_validate_off_the_loop(self) -> None:
        """The handlers that cannot group must still offload the gate itself."""
        import inspect

        for handler in (
            routes._handle_clone_project,
            routes._handle_compile,
            routes._handle_git_status,
            routes._handle_git_commit,
            routes._handle_git_push,
            routes._handle_git_pull,
        ):
            src = inspect.getsource(handler)
            assert (
                "asyncio.to_thread" in src
            ), f"{handler.__name__} must resolve the project off the event loop"

    def test_each_offloaded_helper_documents_that_it_blocks(self) -> None:
        """The helpers a worker thread runs say so, per the repo convention."""
        import inspect

        for helper in (routes._project, routes._project_for_create, routes._safe_relative):
            doc = inspect.getdoc(helper) or ""
            assert "BLOCKING" in doc, f"{helper.__name__} must document that it blocks"


class TestHttpExceptionsAreNotSwallowed:
    """No async work here, so this stays outside the ``asyncio``-marked class below."""

    def test_an_http_exception_is_not_caught_by_the_handlers_except_ladders(self) -> None:
        """Each handler catches ``ValueError`` / ``OSError`` / ``FileExistsError`` and
        converts them. If an aiohttp HTTP exception shared any of those bases, moving
        the gate into the worker closure would turn a 400 into a 500."""
        for exc_type in (web.HTTPBadRequest, web.HTTPNotFound, web.HTTPConflict):
            assert not issubclass(exc_type, ValueError)
            assert not issubclass(exc_type, OSError)
            assert not issubclass(exc_type, FileExistsError)
            assert not issubclass(exc_type, store.PathRejected)


@pytest.mark.asyncio
class TestValidationErrorsSurviveTheOffload:
    """The gate's 400/404/409 must read identically from a worker thread.

    ``web.HTTPException`` raised inside ``asyncio.to_thread`` propagates through the
    ``await`` unchanged, and subclasses none of ``ValueError`` / ``OSError`` /
    ``FileExistsError`` — so the handlers' ``except`` ladders cannot swallow it and
    convert an authorization refusal into a 500. That is what makes moving the gate
    off the loop safe, and it is load-bearing enough to assert rather than assume.
    """

    async def test_a_rejection_raised_in_a_worker_thread_reaches_the_caller(self) -> None:
        def refuse() -> None:
            routes._project("../escape")

        with pytest.raises(web.HTTPBadRequest):
            await asyncio.to_thread(refuse)

    @pytest.mark.parametrize(
        ("handler", "query", "expected"),
        [
            ("_handle_get_project", "name=../escape", web.HTTPBadRequest),
            ("_handle_get_project", "name=absent", web.HTTPNotFound),
            ("_handle_list_files", "name=a/b", web.HTTPBadRequest),
            ("_handle_list_files", "name=absent", web.HTTPNotFound),
            ("_handle_delete_project", "name=-rf", web.HTTPBadRequest),
            ("_handle_pdf", "name=absent", web.HTTPNotFound),
            ("_handle_git_status", "name=..", web.HTTPBadRequest),
        ],
    )
    async def test_query_route_rejections_keep_their_status(
        self, enabled: None, data_root: Path, handler: str, query: str, expected: type
    ) -> None:
        request = make_mocked_request("GET", f"/api/apps/papyrus/x?{query}")
        with pytest.raises(expected):
            await getattr(routes, handler)(request)

    async def test_a_create_conflict_still_reads_as_409(self, enabled: None, project: Path) -> None:
        """The 409 is raised inside the worker closure now, alongside the mkdir."""
        with pytest.raises(web.HTTPConflict):
            await routes._handle_create_project(
                _request("POST", "/api/apps/papyrus/projects", {"name": "my-paper"})
            )

    async def test_a_clone_conflict_still_reads_as_409(self, enabled: None, project: Path) -> None:
        with pytest.raises(web.HTTPConflict):
            await routes._handle_clone_project(
                _request(
                    "POST",
                    "/api/apps/papyrus/projects/clone",
                    {"url": "https://example.com/g/p.git", "name": "my-paper"},
                )
            )

    @pytest.mark.parametrize(
        "handler", ["_handle_save_file", "_handle_create_file", "_handle_set_main"]
    )
    async def test_body_route_traversals_keep_their_400(
        self, enabled: None, project: Path, handler: str
    ) -> None:
        with pytest.raises(web.HTTPBadRequest):
            await getattr(routes, handler)(
                _request(
                    "PUT",
                    "/api/apps/papyrus/x",
                    {"name": "my-paper", "path": "../evil.tex", "content": "x"},
                )
            )

    @pytest.mark.parametrize(
        "handler",
        ["_handle_compile", "_handle_git_commit", "_handle_git_push", "_handle_git_pull"],
    )
    async def test_subprocess_route_rejections_keep_their_400(
        self, enabled: None, data_root: Path, handler: str
    ) -> None:
        with pytest.raises(web.HTTPBadRequest):
            await getattr(routes, handler)(
                _request("POST", "/api/apps/papyrus/x", {"name": "../escape"})
            )

    async def test_the_gate_runs_before_any_subprocess_is_spawned(
        self, enabled: None, data_root: Path
    ) -> None:
        """Ordering: a refused name must never reach ``gitops``.

        Offloading the check must not reorder it behind the use it authorizes.
        """
        push = mock.AsyncMock(return_value="")
        with mock.patch.object(gitops, "push", push):
            with pytest.raises(web.HTTPBadRequest):
                await routes._handle_git_push(
                    _request("POST", "/api/apps/papyrus/git/push", {"name": "a/b"})
                )
        assert push.await_count == 0, "git ran on an unvalidated project name"

    async def test_the_gate_runs_before_the_compiler_is_invoked(
        self, enabled: None, data_root: Path
    ) -> None:
        compile_mock = mock.AsyncMock(return_value=latex.CompileResult(ok=True))
        with mock.patch.object(latex, "compile_project", compile_mock):
            with pytest.raises(web.HTTPNotFound):
                await routes._handle_compile(
                    _request("POST", "/api/apps/papyrus/compile", {"name": "absent"})
                )
        assert compile_mock.await_count == 0

    async def test_the_gate_runs_before_a_clone_is_attempted(
        self, enabled: None, data_root: Path
    ) -> None:
        clone = mock.AsyncMock()
        with mock.patch.object(gitops, "clone", clone):
            with pytest.raises(web.HTTPBadRequest):
                await routes._handle_clone_project(
                    _request(
                        "POST",
                        "/api/apps/papyrus/projects/clone",
                        {"url": "https://example.com/g/p.git", "name": "../escape"},
                    )
                )
        assert clone.await_count == 0
