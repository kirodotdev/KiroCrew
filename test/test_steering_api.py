"""Tests for the Kiro steering files API (``/api/steering``).

Covers:
- ``steering_roots`` / ``list_steering_blocking`` discovery of the global
  ``~/.kiro/steering`` and workspace ``<project>/.kiro/steering`` locations
- ``resolve_steering_file`` traversal, suffix, symlink and containment guards
- ``GET/POST/PUT/DELETE /api/steering`` end-to-end, including the
  restricted-session write block and SEL audit emission

Tests pin $HOME to tmp_path so the real filesystem is never touched.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.handlers._shared import active_project_dir
from kiro_crew.dashboard.handlers.steering import (
    STEERING_FILE_MAX_BYTES,
    STEERING_MAX_FILES,
    _safe_rel_name,
    _split_key,
    api_steering,
    api_steering_create,
    api_steering_detail,
    list_steering_blocking,
    resolve_steering_file,
    steering_roots,
)

# ── Fixtures / helpers ──


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """Pin $HOME to tmp_path so Path.home() returns a writable sandbox."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("KIROCREW_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: home)
    return home


def _write_steering(root: Path, rel: str, body: str = "# Title\nrules\n") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    # newline="": keep fixtures byte-exact so assertions hold on Windows too.
    path.write_text(body, encoding="utf-8", newline="")
    return path


def _state(project: str | Path | None = None, *, restricted: bool = False):
    """A MagicMock DashboardState exposing one slot with a project dir."""
    slot = MagicMock(project=str(project) if project else "", is_restricted=restricted)
    return MagicMock(_slots={"default": slot}, _restricted_keys=set())


def _make_app(state):
    app = web.Application()
    app["state"] = state
    app.router.add_get("/api/steering", api_steering)
    app.router.add_post("/api/steering", api_steering_create)
    app.router.add_get("/api/steering/{key:.+}", api_steering_detail)
    app.router.add_put("/api/steering/{key:.+}", api_steering_detail)
    app.router.add_delete("/api/steering/{key:.+}", api_steering_detail)
    return app


# ── Roots + listing ──


class TestRoots:
    def test_user_root_listed_even_when_missing(self, fake_home):
        roots = steering_roots(None)
        assert [s for s, _ in roots] == ["user"]
        assert roots[0][1] == fake_home / ".kiro" / "steering"

    def test_workspace_root_added_when_project_set(self, fake_home, tmp_path):
        proj = tmp_path / "proj"
        roots = steering_roots(proj)
        assert [s for s, _ in roots] == ["user", "workspace"]
        assert roots[1][1] == proj / ".kiro" / "steering"


class TestListing:
    def test_lists_both_sources_with_provenance(self, fake_home, tmp_path):
        _write_steering(fake_home / ".kiro" / "steering", "personal.md", "# Personal\n")
        proj = tmp_path / "proj"
        _write_steering(proj / ".kiro" / "steering", "api-standards.md", "# API standards\n")

        out = list_steering_blocking(proj)
        keys = {f["key"] for f in out["files"]}
        assert keys == {"user/personal.md", "workspace/api-standards.md"}
        by_key = {f["key"]: f for f in out["files"]}
        assert by_key["user/personal.md"]["description"] == "Personal"
        assert by_key["user/personal.md"]["source"] == "user"
        assert by_key["workspace/api-standards.md"]["source"] == "workspace"
        assert [r["source"] for r in out["roots"]] == ["user", "workspace"]
        assert all(r["exists"] for r in out["roots"])

    def test_nested_files_and_home_redaction(self, fake_home):
        _write_steering(fake_home / ".kiro" / "steering", "team/style.md")
        out = list_steering_blocking(None)
        entry = out["files"][0]
        assert entry["key"] == "user/team/style.md"
        assert entry["rel"] == "team/style.md"
        # The real home must never leak to the client.
        assert entry["path"].startswith("~")
        assert str(fake_home) not in entry["path"]

    def test_skips_dotfiles_and_non_markdown(self, fake_home):
        root = fake_home / ".kiro" / "steering"
        _write_steering(root, ".hidden.md")
        root.mkdir(parents=True, exist_ok=True)
        (root / "notes.txt").write_text("nope", encoding="utf-8")
        assert list_steering_blocking(None)["files"] == []

    def test_missing_root_reports_not_exists(self, fake_home):
        out = list_steering_blocking(None)
        assert out["files"] == []
        assert out["roots"][0]["exists"] is False

    def test_symlinked_root_escaping_home_is_ignored(self, fake_home, tmp_path):
        outside = tmp_path / "outside"
        _write_steering(outside, "leak.md")
        kiro = fake_home / ".kiro"
        kiro.mkdir(parents=True, exist_ok=True)
        (kiro / "steering").symlink_to(outside, target_is_directory=True)
        assert list_steering_blocking(None)["files"] == []

    def test_file_cap_enforced(self, fake_home):
        root = fake_home / ".kiro" / "steering"
        for i in range(STEERING_MAX_FILES + 5):
            _write_steering(root, f"doc{i:04d}.md")
        assert len(list_steering_blocking(None)["files"]) == STEERING_MAX_FILES


# ── Key parsing + resolution guards ──


class TestKeyParsing:
    @pytest.mark.parametrize("key", [
        "", "user", "user/", "bogus/x.md", "user/../../etc/passwd",
        "user//etc/passwd", "user/~/x.md", "user/notes.txt", "user/a\\b.md",
    ])
    def test_rejects_bad_keys(self, key):
        assert _split_key(key) is None

    def test_accepts_nested_markdown(self):
        assert _split_key("workspace/team/style.md") == ("workspace", "team/style.md")

    @pytest.mark.parametrize("raw,expected", [
        ("api standards", "api standards.md"),
        ("API_Standards.md", "API_Standards.md"),
        ("../../etc/passwd", "etc/passwd.md"),
        ("nested/../thing", "nested/thing.md"),
        ("bad;chars|here", "bad-chars-here.md"),
        ("   ", ""),
        ("/", ""),
    ])
    def test_safe_rel_name(self, raw, expected):
        assert _safe_rel_name(raw) == expected


class TestResolution:
    def test_resolves_existing_file(self, fake_home):
        target = _write_steering(fake_home / ".kiro" / "steering", "a.md")
        assert resolve_steering_file("user/a.md", None) == target.resolve()

    def test_missing_file_is_none_for_read(self, fake_home):
        assert resolve_steering_file("user/missing.md", None) is None

    def test_traversal_rejected(self, fake_home):
        assert resolve_steering_file("user/../../../etc/passwd", None) is None

    def test_workspace_requires_project(self, fake_home, tmp_path):
        proj = tmp_path / "proj"
        _write_steering(proj / ".kiro" / "steering", "w.md")
        assert resolve_steering_file("workspace/w.md", None) is None
        assert resolve_steering_file("workspace/w.md", proj) is not None

    def test_symlinked_leaf_escaping_base_rejected(self, fake_home, tmp_path):
        root = fake_home / ".kiro" / "steering"
        root.mkdir(parents=True, exist_ok=True)
        secret = tmp_path / "secret.md"
        secret.write_text("secret", encoding="utf-8")
        (root / "link.md").symlink_to(secret)
        assert resolve_steering_file("user/link.md", None) is None

    def test_symlinked_leaf_inside_base_also_rejected(self, fake_home):
        """A link that resolves INSIDE the base is still not a steering file.

        ``.kiro/steering/rules.md -> ../../README.md`` passes a base-containment
        check, so without a leaf-symlink rejection PUT would truncate — and
        DELETE unlink — an unrelated file the user never opened in the tab.
        """
        root = fake_home / ".kiro" / "steering"
        root.mkdir(parents=True, exist_ok=True)
        victim = fake_home / "README.md"
        victim.write_text("# real readme\n", encoding="utf-8")
        (root / "rules.md").symlink_to(victim)
        assert resolve_steering_file("user/rules.md", None) is None
        assert [f["key"] for f in list_steering_blocking(None)["files"]] == []

    def test_symlinked_subdir_escaping_base_rejected(self, fake_home, tmp_path):
        root = fake_home / ".kiro" / "steering"
        root.mkdir(parents=True, exist_ok=True)
        outside = tmp_path / "outside"
        _write_steering(outside, "x.md")
        (root / "sub").symlink_to(outside, target_is_directory=True)
        assert resolve_steering_file("user/sub/x.md", None) is None

    def test_write_target_need_not_exist(self, fake_home):
        target = resolve_steering_file("user/new/doc.md", None, for_write=True)
        assert target == fake_home / ".kiro" / "steering" / "new" / "doc.md"


# ── HTTP endpoints ──


class TestProjectResolution:
    """Workspace scope must never act on an arbitrarily chosen project."""

    @staticmethod
    def _multi_slot_state(*projects):
        slots = {
            f"slot{i}": MagicMock(project=str(p), is_restricted=False)
            for i, p in enumerate(projects)
        }
        return MagicMock(_slots=slots, _restricted_keys=set())

    def test_single_shared_project_is_used(self, fake_home, tmp_path):
        proj = tmp_path / "proj"
        state = self._multi_slot_state(proj, proj)
        assert active_project_dir(state) == proj

    def test_disagreeing_slots_resolve_to_none(self, fake_home, tmp_path):
        state = self._multi_slot_state(tmp_path / "a", tmp_path / "b")
        assert active_project_dir(state) is None

    def test_session_key_selects_its_own_slot(self, fake_home, tmp_path):
        state = self._multi_slot_state(tmp_path / "a", tmp_path / "b")
        assert active_project_dir(state, "dashboard:slot1") == tmp_path / "b"

    def test_slots_without_projects_are_ignored(self, fake_home, tmp_path):
        proj = tmp_path / "proj"
        slots = {
            "a": MagicMock(project="", is_restricted=False),
            "b": MagicMock(project=str(proj), is_restricted=False),
        }
        state = MagicMock(_slots=slots, _restricted_keys=set())
        assert active_project_dir(state) == proj

    @pytest.mark.asyncio
    async def test_workspace_create_refused_when_projects_disagree(self, fake_home, tmp_path):
        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()
        state = self._multi_slot_state(tmp_path / "a", tmp_path / "b")
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/steering", json={"name": "x.md", "content": "y", "source": "workspace"}
            )
            assert resp.status == 400
        assert not (tmp_path / "a" / ".kiro").exists()
        assert not (tmp_path / "b" / ".kiro").exists()

    @pytest.mark.asyncio
    async def test_listing_omits_workspace_when_projects_disagree(self, fake_home, tmp_path):
        _write_steering(tmp_path / "a" / ".kiro" / "steering", "a.md")
        _write_steering(tmp_path / "b" / ".kiro" / "steering", "b.md")
        state = self._multi_slot_state(tmp_path / "a", tmp_path / "b")
        async with TestClient(TestServer(_make_app(state))) as client:
            data = await (await client.get("/api/steering")).json()
        assert [r["source"] for r in data["roots"]] == ["user"]
        assert data["files"] == []


class TestRedaction:
    """Listing metadata is redacted; editor content deliberately is not."""

    FAKE = "aws_secret_access_key=AKIAIOSFODNN7EXAMPLE1234567890abcdefghij"

    @pytest.mark.asyncio
    async def test_listing_description_is_redacted(self, fake_home):
        _write_steering(fake_home / ".kiro" / "steering", "leaky.md", f"# {self.FAKE}\nbody\n")
        async with TestClient(TestServer(_make_app(_state()))) as client:
            data = await (await client.get("/api/steering")).json()
        desc = data["files"][0]["description"]
        assert "AKIAIOSFODNN7EXAMPLE" not in desc

    @pytest.mark.asyncio
    async def test_detail_content_is_verbatim(self, fake_home):
        """Redacting the editor payload would overwrite the file on save."""
        body = f"# Rules\n{self.FAKE}\n"
        _write_steering(fake_home / ".kiro" / "steering", "leaky.md", body)
        async with TestClient(TestServer(_make_app(_state()))) as client:
            data = await (await client.get("/api/steering/user/leaky.md")).json()
        assert data["content"] == body
        # And a save round-trip must not mutate what the user never edited.
        async with TestClient(TestServer(_make_app(_state()))) as client:
            assert (
                await client.put("/api/steering/user/leaky.md", json={"content": data["content"]})
            ).status == 200
        assert (fake_home / ".kiro" / "steering" / "leaky.md").read_text() == body


class TestListEndpoint:
    @pytest.mark.asyncio
    async def test_get_returns_files_and_roots(self, fake_home, tmp_path):
        _write_steering(fake_home / ".kiro" / "steering", "personal.md")
        proj = tmp_path / "proj"
        _write_steering(proj / ".kiro" / "steering", "project.md")
        async with TestClient(TestServer(_make_app(_state(proj)))) as client:
            resp = await client.get("/api/steering")
            assert resp.status == 200
            data = await resp.json()
        assert {f["key"] for f in data["files"]} == {"user/personal.md", "workspace/project.md"}
        assert data["project"].endswith("proj")

    @pytest.mark.asyncio
    async def test_get_audits(self, fake_home, monkeypatch):
        sel_mock = MagicMock()
        monkeypatch.setattr("kiro_crew.dashboard.handlers.sel", lambda: sel_mock)
        async with TestClient(TestServer(_make_app(_state()))) as client:
            assert (await client.get("/api/steering")).status == 200
        names = [c.kwargs.get("tool_name") for c in sel_mock.log_tool_invocation.call_args_list]
        assert "api_steering_list" in names


class TestReadEndpoint:
    @pytest.mark.asyncio
    async def test_reads_content_verbatim(self, fake_home):
        _write_steering(fake_home / ".kiro" / "steering", "a.md", "# A\nbody\n")
        async with TestClient(TestServer(_make_app(_state()))) as client:
            resp = await client.get("/api/steering/user/a.md")
            assert resp.status == 200
            data = await resp.json()
        assert data["content"] == "# A\nbody\n"
        assert data["source"] == "user"
        assert data["path"].startswith("~")

    @pytest.mark.asyncio
    async def test_unknown_is_404(self, fake_home):
        async with TestClient(TestServer(_make_app(_state()))) as client:
            assert (await client.get("/api/steering/user/nope.md")).status == 404

    @pytest.mark.asyncio
    async def test_traversal_is_404(self, fake_home):
        async with TestClient(TestServer(_make_app(_state()))) as client:
            resp = await client.get("/api/steering/user/..%2F..%2Fsecrets.md")
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_read_refuses_symlink_swapped_after_resolution(self, fake_home, tmp_path):
        """The read path must validate the inode it actually opens.

        ``resolve_steering_file()`` rejects a symlink, but a regular file can be
        swapped for one before the read. The read goes through
        ``safe_read_file_bytes_nolink`` (O_NOFOLLOW + fstat), so the swap yields
        an error rather than the symlink target's contents.
        """
        from kiro_crew.dashboard.handlers import steering as mod

        root = fake_home / ".kiro" / "steering"
        real = _write_steering(root, "a.md", "real content\n")
        secret = tmp_path / "credentials"
        secret.write_text("[default]\naws_secret_access_key=nope\n", encoding="utf-8")

        assert resolve_steering_file("user/a.md", None) is not None
        real.unlink()
        real.symlink_to(secret)

        content, _display, err = mod._resolve_and_read_blocking("user/a.md", None)
        assert err is not None
        assert "aws_secret_access_key" not in content

    @pytest.mark.asyncio
    async def test_growth_past_cap_mid_read_is_413_not_500(self, fake_home, monkeypatch):
        """The helper raises FileTooLargeError if the file grows after the lstat."""
        from kiro_crew import hooks
        from kiro_crew.dashboard.handlers import steering as mod

        _write_steering(fake_home / ".kiro" / "steering", "a.md", "small\n")

        def _boom(*_a, **_kw):
            raise hooks.FileTooLargeError("grew")

        # steering.py imports the helper at module scope, so patch it there.
        monkeypatch.setattr(mod, "safe_read_file_bytes_nolink", _boom)
        _content, _display, err = mod._resolve_and_read_blocking("user/a.md", None)
        assert err is not None and err.startswith("toolarge:")

        async with TestClient(TestServer(_make_app(_state()))) as client:
            assert (await client.get("/api/steering/user/a.md")).status == 413

    @pytest.mark.asyncio
    async def test_oversize_file_is_413(self, fake_home):
        root = fake_home / ".kiro" / "steering"
        _write_steering(root, "big.md", "x" * (STEERING_FILE_MAX_BYTES + 1))
        async with TestClient(TestServer(_make_app(_state()))) as client:
            assert (await client.get("/api/steering/user/big.md")).status == 413


class TestCreateEndpoint:
    @pytest.mark.asyncio
    async def test_creates_user_file_when_no_project(self, fake_home):
        async with TestClient(TestServer(_make_app(_state()))) as client:
            resp = await client.post(
                "/api/steering", json={"name": "My Rules", "content": "# Rules\n"}
            )
            assert resp.status == 200
            data = await resp.json()
        assert data["key"] == "user/My Rules.md"
        assert (fake_home / ".kiro" / "steering" / "My Rules.md").read_text() == "# Rules\n"

    @pytest.mark.asyncio
    async def test_defaults_to_workspace_when_project_set(self, fake_home, tmp_path):
        proj = tmp_path / "proj"
        proj.mkdir()
        async with TestClient(TestServer(_make_app(_state(proj)))) as client:
            resp = await client.post("/api/steering", json={"name": "api.md", "content": "x"})
            assert resp.status == 200
            assert (await resp.json())["key"] == "workspace/api.md"
        assert (proj / ".kiro" / "steering" / "api.md").is_file()

    @pytest.mark.asyncio
    async def test_workspace_without_project_is_400(self, fake_home):
        async with TestClient(TestServer(_make_app(_state()))) as client:
            resp = await client.post(
                "/api/steering", json={"name": "a.md", "content": "x", "source": "workspace"}
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_duplicate_is_409(self, fake_home):
        _write_steering(fake_home / ".kiro" / "steering", "dup.md")
        async with TestClient(TestServer(_make_app(_state()))) as client:
            resp = await client.post("/api/steering", json={"name": "dup.md", "content": "x"})
            assert resp.status == 409

    @pytest.mark.asyncio
    async def test_traversal_name_is_confined_to_root(self, fake_home):
        async with TestClient(TestServer(_make_app(_state()))) as client:
            resp = await client.post(
                "/api/steering", json={"name": "../../escape", "content": "x"}
            )
            assert resp.status == 200
            key = (await resp.json())["key"]
        assert key == "user/etc/passwd.md" or key.startswith("user/")
        assert not (fake_home.parent / "escape.md").exists()

    @pytest.mark.asyncio
    async def test_missing_fields_are_400(self, fake_home):
        async with TestClient(TestServer(_make_app(_state()))) as client:
            assert (await client.post("/api/steering", json={"content": "x"})).status == 400
            assert (await client.post("/api/steering", json={"name": "a.md"})).status == 400

    @pytest.mark.asyncio
    async def test_oversize_content_is_413(self, fake_home):
        async with TestClient(TestServer(_make_app(_state()))) as client:
            resp = await client.post(
                "/api/steering",
                json={"name": "a.md", "content": "x" * (STEERING_FILE_MAX_BYTES + 1)},
            )
            assert resp.status == 413

    @pytest.mark.asyncio
    async def test_audits_creation(self, fake_home, monkeypatch):
        sel_mock = MagicMock()
        monkeypatch.setattr("kiro_crew.dashboard.handlers.sel", lambda: sel_mock)
        async with TestClient(TestServer(_make_app(_state()))) as client:
            await client.post("/api/steering", json={"name": "a.md", "content": "x"})
        ops = [c.kwargs.get("operation") for c in sel_mock.log_api_access.call_args_list]
        assert "steering.create" in ops


class TestUpdateDeleteEndpoints:
    @pytest.mark.asyncio
    async def test_update_writes_content(self, fake_home):
        _write_steering(fake_home / ".kiro" / "steering", "a.md", "old")
        async with TestClient(TestServer(_make_app(_state()))) as client:
            resp = await client.put("/api/steering/user/a.md", json={"content": "new"})
            assert resp.status == 200
        assert (fake_home / ".kiro" / "steering" / "a.md").read_text() == "new"

    @pytest.mark.asyncio
    async def test_update_unknown_is_404(self, fake_home):
        async with TestClient(TestServer(_make_app(_state()))) as client:
            resp = await client.put("/api/steering/user/nope.md", json={"content": "x"})
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_update_empty_content_is_400(self, fake_home):
        _write_steering(fake_home / ".kiro" / "steering", "a.md")
        async with TestClient(TestServer(_make_app(_state()))) as client:
            resp = await client.put("/api/steering/user/a.md", json={"content": "  "})
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_update_through_symlink_is_refused(self, fake_home):
        """PUT must not truncate a file reached via a steering-dir symlink."""
        root = fake_home / ".kiro" / "steering"
        root.mkdir(parents=True, exist_ok=True)
        victim = fake_home / "README.md"
        victim.write_text("# real readme\n", encoding="utf-8")
        (root / "rules.md").symlink_to(victim)
        async with TestClient(TestServer(_make_app(_state()))) as client:
            resp = await client.put("/api/steering/user/rules.md", json={"content": "pwned"})
            assert resp.status == 404
        assert victim.read_text() == "# real readme\n"

    @pytest.mark.asyncio
    async def test_delete_through_symlink_is_refused(self, fake_home):
        root = fake_home / ".kiro" / "steering"
        root.mkdir(parents=True, exist_ok=True)
        victim = fake_home / "README.md"
        victim.write_text("# real readme\n", encoding="utf-8")
        (root / "rules.md").symlink_to(victim)
        async with TestClient(TestServer(_make_app(_state()))) as client:
            assert (await client.delete("/api/steering/user/rules.md")).status == 404
        assert victim.exists()

    def test_write_flags_tolerate_missing_o_nofollow(self):
        """``os.O_NOFOLLOW`` is absent on Windows — the flag must be optional.

        Referencing ``os.O_NOFOLLOW`` directly would raise AttributeError there
        and turn every create/update into a 500.
        """
        from kiro_crew.dashboard.handlers import steering as mod

        assert mod._O_NOFOLLOW == getattr(os, "O_NOFOLLOW", 0)

    @pytest.mark.asyncio
    async def test_update_is_atomic_and_preserves_content_on_write_failure(
        self, fake_home, monkeypatch
    ):
        """A failed write must leave the original document intact.

        The update path replaces the file (temp write + os.replace) instead of
        truncating in place, so a write that dies part-way — a full filesystem,
        say — cannot destroy the user's steering document.
        """
        from kiro_crew.dashboard.handlers import steering as mod

        path = _write_steering(fake_home / ".kiro" / "steering", "a.md", "original\n")

        def _boom(*_a, **_kw):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(mod, "atomic_write", _boom)
        async with TestClient(TestServer(_make_app(_state()))) as client:
            resp = await client.put("/api/steering/user/a.md", json={"content": "new"})
            assert resp.status == 500
        assert path.read_text() == "original\n"

    @pytest.mark.asyncio
    async def test_crlf_content_round_trips_byte_exact(self, fake_home):
        """Line endings must survive create -> read -> save unchanged.

        Text-mode writes translate ``\\n`` to ``\\r\\n`` on Windows, so a document
        read back and saved again would accumulate carriage returns on every
        cycle (CRLF -> CR CR LF -> ...). Both write paths use newline="" to
        keep the bytes exactly as the editor sent them.
        """
        crlf = "# Windows doc\r\nline two\r\n"
        async with TestClient(TestServer(_make_app(_state()))) as client:
            assert (
                await client.post("/api/steering", json={"name": "crlf.md", "content": crlf})
            ).status == 200
            first = await (await client.get("/api/steering/user/crlf.md")).json()
            assert first["content"] == crlf
            assert (
                await client.put("/api/steering/user/crlf.md", json={"content": first["content"]})
            ).status == 200
            second = await (await client.get("/api/steering/user/crlf.md")).json()
        assert second["content"] == crlf
        raw = (fake_home / ".kiro" / "steering" / "crlf.md").read_bytes()
        assert raw == crlf.encode("utf-8")

    @pytest.mark.asyncio
    @pytest.mark.skipif(os.name == "nt", reason="POSIX file-mode semantics")
    async def test_update_preserves_existing_file_mode(self, fake_home):
        """A save must not silently tighten a group-readable steering file."""
        root = fake_home / ".kiro" / "steering"
        path = _write_steering(root, "a.md", "old")
        path.chmod(0o644)
        async with TestClient(TestServer(_make_app(_state()))) as client:
            assert (
                await client.put("/api/steering/user/a.md", json={"content": "new"})
            ).status == 200
        assert stat.S_IMODE(path.stat().st_mode) == 0o644
        assert path.read_text() == "new"

    @pytest.mark.asyncio
    async def test_update_leaves_no_temp_files_behind(self, fake_home):
        root = fake_home / ".kiro" / "steering"
        _write_steering(root, "a.md", "old")
        async with TestClient(TestServer(_make_app(_state()))) as client:
            assert (
                await client.put("/api/steering/user/a.md", json={"content": "new"})
            ).status == 200
        assert (root / "a.md").read_text() == "new"
        assert [p.name for p in root.iterdir()] == ["a.md"]

    @pytest.mark.asyncio
    async def test_delete_removes_file(self, fake_home):
        path = _write_steering(fake_home / ".kiro" / "steering", "a.md")
        async with TestClient(TestServer(_make_app(_state()))) as client:
            assert (await client.delete("/api/steering/user/a.md")).status == 200
        assert not path.exists()

    @pytest.mark.asyncio
    async def test_delete_unknown_is_404(self, fake_home):
        async with TestClient(TestServer(_make_app(_state()))) as client:
            assert (await client.delete("/api/steering/user/nope.md")).status == 404


class TestRestrictedSessions:
    """Incognito / guest sessions may read steering but never modify it."""

    @staticmethod
    def _restricted_state(project=None):
        state = _state(project)
        state._restricted_keys = {"dashboard:incognito"}
        return state

    @pytest.mark.asyncio
    async def test_reads_allowed(self, fake_home):
        _write_steering(fake_home / ".kiro" / "steering", "a.md")
        async with TestClient(TestServer(_make_app(self._restricted_state()))) as client:
            resp = await client.get(
                "/api/steering/user/a.md", headers={"X-Session-Key": "dashboard:incognito"}
            )
            assert resp.status == 200

    @pytest.mark.asyncio
    async def test_writes_blocked(self, fake_home):
        path = _write_steering(fake_home / ".kiro" / "steering", "a.md")
        hdr = {"X-Session-Key": "dashboard:incognito"}
        async with TestClient(TestServer(_make_app(self._restricted_state()))) as client:
            assert (
                await client.post("/api/steering", json={"name": "b.md", "content": "x"},
                                  headers=hdr)
            ).status == 403
            assert (
                await client.put("/api/steering/user/a.md", json={"content": "y"}, headers=hdr)
            ).status == 403
            assert (await client.delete("/api/steering/user/a.md", headers=hdr)).status == 403
        assert path.read_text() == "# Title\nrules\n"
