"""Clone-time naming in ``routes._handle_clone_project``.

A cloned paper is named once, from its document title. These tests sit beside
``test_papyrus_routes.py`` instead of in it because that file is already past the
size where the code style puts new work in a new module. Handlers are driven
directly with ``make_mocked_request`` and ``gitops.clone`` is faked, as in that
file, so no server is bound and no subprocess is spawned.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from conftest import requires_symlinks
from kiro_crew.apps.builtins.papyrus.backend import gitops, routes, store

_CLONE = "/api/apps/papyrus/projects/clone"
_URL = "https://example.com/g/6a16ddbf.git"
_FOLDER = "6a16ddbf"
_FAKE_PAT = "ghp_" + "A" * 36


@pytest.fixture()
def enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(routes, "is_app_enabled", lambda _name: True)


@pytest.fixture()
def data_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the store at a tmp data dir so no test touches the real app home."""
    root = tmp_path / "papyrus-data"
    root.mkdir()
    monkeypatch.setattr(store, "app_data_dir", lambda _name: root)
    return root


def _request(method: str, path: str, body: dict[str, Any]) -> web.Request:
    request = make_mocked_request(method, path)
    request.json = mock.AsyncMock(return_value=body)  # type: ignore[method-assign]
    request._payload_length = len(json.dumps(body).encode())  # type: ignore[attr-defined]
    return request


async def _clone(
    files: dict[str, str],
    *,
    links: dict[str, Path] | None = None,
    body: dict[str, Any] | None = None,
) -> web.StreamResponse:
    """Clone a fake repository holding *files*, plus symlinks named in *links*.

    Like the real clone, the fake returns the new paper's generation.
    """

    async def fake_clone(_url: str, destination: Path) -> str:
        destination.mkdir(parents=True)
        for relative, content in files.items():
            (destination / relative).write_text(content, encoding="utf-8")
        for relative, target in (links or {}).items():
            (destination / relative).symlink_to(target)
        return store.project_generation(destination)

    with mock.patch.object(gitops, "clone", fake_clone):
        return await routes._handle_clone_project(
            _request("POST", _CLONE, {"url": _URL, **(body or {})})
        )


async def _listed_titles() -> dict[str, str]:
    """The display name the paper list shows for each paper, by folder name."""
    response = await routes._handle_list_projects(
        make_mocked_request("GET", "/api/apps/papyrus/projects")
    )
    assert isinstance(response, web.Response)
    assert isinstance(response.body, (bytes, bytearray))
    return {row["name"]: row["title"] for row in json.loads(response.body)["projects"]}


@pytest.mark.asyncio
class TestCloneNamesThePaper:
    async def test_names_the_paper_from_its_document_title(
        self, enabled: None, data_root: Path
    ) -> None:
        response = await _clone({"main.tex": "\\title{MACKEREL}\n\\begin{document}"})
        assert response.status == 201
        assert await _listed_titles() == {_FOLDER: "MACKEREL"}

    async def test_keeps_a_display_name_the_repository_ships(
        self, enabled: None, data_root: Path
    ) -> None:
        await _clone(
            {"main.tex": "\\title{From the document}", ".papyrus.json": '{"title": "Shipped"}'}
        )
        assert await _listed_titles() == {_FOLDER: "Shipped"}

    async def test_a_request_that_names_the_folder_reads_no_title(
        self, enabled: None, data_root: Path
    ) -> None:
        await _clone({"main.tex": "\\title{From the document}"}, body={"name": "chosen"})
        assert await _listed_titles() == {"chosen": "chosen"}

    @pytest.mark.parametrize(
        "main_tex",
        [
            pytest.param("\\documentclass{article}\n\\begin{document}", id="no-title"),
            pytest.param("\\title{\u200b\u202e}", id="title-sanitizing-refuses"),
        ],
    )
    async def test_without_a_usable_title_the_folder_name_stays(
        self, enabled: None, data_root: Path, main_tex: str
    ) -> None:
        response = await _clone({"main.tex": main_tex})
        assert response.status == 201
        assert await _listed_titles() == {_FOLDER: _FOLDER}

    @requires_symlinks
    async def test_a_main_tex_linking_outside_the_paper_never_names_it(
        self, enabled: None, data_root: Path, tmp_path: Path
    ) -> None:
        outside = tmp_path / "outside.tex"
        outside.write_text("\\title{Leaked}", encoding="utf-8")
        response = await _clone({"paper.tex": "\\title{Real}"}, links={"main.tex": outside})
        assert response.status == 201
        assert await _listed_titles() == {_FOLDER: "Real"}

    @requires_symlinks
    async def test_an_unwritable_settings_file_leaves_the_folder_name(
        self, enabled: None, data_root: Path, tmp_path: Path
    ) -> None:
        outside = tmp_path / "outside.json"
        outside.write_text("{}", encoding="utf-8")
        response = await _clone({"main.tex": "\\title{MACKEREL}"}, links={".papyrus.json": outside})
        assert response.status == 201
        assert await _listed_titles() == {_FOLDER: _FOLDER}
        assert outside.read_text(encoding="utf-8") == "{}"

    async def test_a_disk_error_saving_the_name_leaves_the_folder_name(
        self, enabled: None, data_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def disk_full(*_args: object, **_kwargs: object) -> None:
            raise OSError("No space left on device")

        monkeypatch.setattr(store, "atomic_write", disk_full)
        response = await _clone({"main.tex": "\\title{MACKEREL}"})
        assert response.status == 201
        assert await _listed_titles() == {_FOLDER: _FOLDER}

    async def test_a_paper_replaced_during_the_clone_keeps_its_own_name(
        self, enabled: None, data_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real_resolve = store.resolve_main_file

        def replaced_then_resolved(project: Path) -> str | None:
            # Another client deletes the fresh clone and creates a paper of the
            # same name before the clone request names it.
            store.delete_project(project)
            project.mkdir()
            (project / "main.tex").write_text("\\title{Untitled Paper}", encoding="utf-8")
            return real_resolve(project)

        monkeypatch.setattr(store, "resolve_main_file", replaced_then_resolved)
        response = await _clone({"main.tex": "\\title{MACKEREL}"})
        monkeypatch.setattr(store, "resolve_main_file", real_resolve)
        assert response.status == 201
        assert await _listed_titles() == {_FOLDER: _FOLDER}

    async def test_a_credential_in_the_title_is_redacted(
        self, enabled: None, data_root: Path
    ) -> None:
        await _clone({"main.tex": f"\\title{{Leaked {_FAKE_PAT}}}"})
        title = (await _listed_titles())[_FOLDER]
        assert title.startswith("Leaked")
        assert _FAKE_PAT not in title

    async def test_a_long_title_is_cut_to_the_display_cap(
        self, enabled: None, data_root: Path
    ) -> None:
        await _clone({"main.tex": "\\title{" + "word " * 1000 + "}"})
        title = (await _listed_titles())[_FOLDER]
        assert title.startswith("word")
        assert len(title) <= store.MAX_TITLE_CHARS
