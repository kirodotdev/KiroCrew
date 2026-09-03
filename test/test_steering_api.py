"""Tests for the Kiro steering files API (``/api/steering``).

Covers:
- ``steering_roots`` / ``list_steering_blocking`` discovery of the global
  ``~/.kiro/steering`` and workspace ``<project>/.kiro/steering`` locations
- ``resolve_steering_file`` traversal, suffix, symlink and containment guards
- the listing's ``inclusion`` / ``fileMatchPattern`` metadata, and that front
  matter is excluded from the one-line description
- ``GET/POST/PUT/DELETE /api/steering`` end-to-end, including the
  restricted-session write block and SEL audit emission

Tests pin $HOME to tmp_path so the real filesystem is never touched.
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import kiro_crew.dashboard.handlers.steering as steering_mod
from kiro_crew.dashboard.handlers._shared import active_project_dir, active_project_state
from kiro_crew.dashboard.handlers.steering import (
    _STEERING_META_MAX_CHARS,
    STEERING_FILE_MAX_BYTES,
    STEERING_INCLUSION_DEFAULT,
    STEERING_INCLUSION_MODES,
    STEERING_MAX_FILES,
    STEERING_PROJECT_HEADER,
    _project_key,
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


def _project_headers(project: str | Path) -> dict[str, str]:
    """The precondition header a client sends after being listed *project*.

    Mirrors what the frontend echoes from ``project_key``; a workspace write
    without it is refused, so tests that mean to succeed must send it.
    """
    return {STEERING_PROJECT_HEADER: _project_key(Path(project))}


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

    def test_a_comment_only_front_matter_block_is_not_the_description(self, fake_home):
        """A closed fence around nothing but a comment is still a closed fence.

        ``split_frontmatter`` already returns the text AFTER it correctly; the
        bug was discarding that body for the RAW head whenever no ``key: value``
        field was found, so the front matter's own ``#`` line was read as the
        document's first markdown heading — publishing a comment inside the
        declaration as the description, in place of the real title after it.
        """
        _write_steering(
            fake_home / ".kiro" / "steering", "x.md",
            "---\n# just a note\n---\n# Title\nBody text.\n",
        )
        out = list_steering_blocking(None)
        by_key = {f["key"]: f for f in out["files"]}
        assert by_key["user/x.md"]["description"] == "Title"

    def test_a_hardlinked_document_leaks_no_metadata(self, fake_home, tmp_path):
        """A hardlink defeats the symlink and sensitive-PATH checks above it.

        The entry's own path stays innocently inside the steering root while its
        inode is somebody else's secret, so the scan's ``is_symlink`` and
        ``is_sensitive_path`` gates both pass and the file's first line would be
        published as this document's description. The guarded reader fstat()s the
        descriptor it opened and refuses ``st_nlink > 1``.
        """
        secret = tmp_path / "credentials"
        secret.write_text("# aws_secret_access_key = SHOULD-NOT-APPEAR\n", encoding="utf-8")
        root = fake_home / ".kiro" / "steering"
        _write_steering(root, "innocent.md", "# Innocent\n")
        link = root / "linked.md"
        try:
            os.link(secret, link)
        except (OSError, NotImplementedError):
            pytest.skip("filesystem does not support hardlinks")
        if link.stat().st_nlink < 2:
            pytest.skip("filesystem did not create a second link")

        out = list_steering_blocking(None)
        by_key = {f["key"]: f for f in out["files"]}
        # Still listed — the scan sees a regular .md file — but with no metadata
        # read out of it.
        assert by_key["user/linked.md"]["description"] == ""
        assert "SHOULD-NOT-APPEAR" not in json.dumps(out)
        # The ordinary neighbour is unaffected.
        assert by_key["user/innocent.md"]["description"] == "Innocent"

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


class TestInclusionMetadata:
    """The listing reports each document's declared ``inclusion``.

    Kiro Crew does not ACT on the value — on the live ACP path the load
    decision is kiro-cli's, which reads the same front matter itself. These
    pin that the tab shows the author what they declared, and that a
    declaration never masquerades as the document's summary.
    """

    def _entry(self, home: Path, body: str) -> dict:
        _write_steering(home / ".kiro" / "steering", "doc.md", body)
        return list_steering_blocking(None)["files"][0]

    def test_front_matter_does_not_become_the_description(self, fake_home):
        """The regression this metadata replaced: the tab summarized a document
        by its first declaration, so a ``manual`` document was described as the
        string ``inclusion: manual``."""
        entry = self._entry(fake_home, "---\ninclusion: manual\n---\n# Payroll rules\nbody\n")
        assert entry["description"] == "Payroll rules"

    @pytest.mark.parametrize("mode", STEERING_INCLUSION_MODES)
    def test_each_documented_mode_round_trips(self, fake_home, mode):
        entry = self._entry(fake_home, f"---\ninclusion: {mode}\n---\n# Doc\n")
        assert entry["inclusion"] == mode
        assert entry["inclusion_declared"] == mode

    def test_absent_front_matter_reports_the_default(self, fake_home):
        entry = self._entry(fake_home, "# Plain doc\nbody\n")
        assert entry["inclusion"] == STEERING_INCLUSION_DEFAULT
        # Empty, not the resolved mode: the tab distinguishes "declared
        # nothing" from "declared something unreadable", and only the second is
        # worth telling the author about.
        assert entry["inclusion_declared"] == ""
        assert entry["description"] == "Plain doc"

    def test_unrecognized_mode_reports_default_plus_raw_spelling(self, fake_home):
        """A typo resolves to the default — matching what kiro-cli does with a
        value it does not recognize — but the spelling survives, because it is
        the only thing that explains the behavior to its author."""
        entry = self._entry(fake_home, "---\ninclusion: manaul\n---\n# Typo\n")
        assert entry["inclusion"] == STEERING_INCLUSION_DEFAULT
        assert entry["inclusion_declared"] == "manaul"

    def test_mode_spelling_is_canonicalized(self, fake_home):
        entry = self._entry(fake_home, "---\ninclusion: FILEMATCH\n---\n# Doc\n")
        assert entry["inclusion"] == "fileMatch"
        assert entry["inclusion_declared"] == "FILEMATCH"

    def test_file_match_pattern_is_reported(self, fake_home):
        entry = self._entry(
            fake_home,
            '---\ninclusion: fileMatch\nfileMatchPattern: "src/**/*.ts"\n---\n# Doc\n',
        )
        assert entry["file_match_pattern"] == "src/**/*.ts"

    def test_inclusion_in_body_prose_is_not_front_matter(self, fake_home):
        """A document explaining the modes must not be read as declaring one."""
        entry = self._entry(
            fake_home,
            "# Guide\nSet inclusion: manual to keep a runbook out of every turn.\n",
        )
        assert entry["inclusion"] == STEERING_INCLUSION_DEFAULT
        assert entry["inclusion_declared"] == ""

    def test_truncated_crlf_front_matter_yields_no_description(self, fake_home):
        """A CRLF document opens ``---\\r\\n``; missing that opener let the raw
        head through and re-exposed ``inclusion:`` as the description."""
        filler = "".join(f"pad{i}: {'x' * 80}\r\n" for i in range(60))
        entry = self._entry(
            fake_home, f"---\r\ninclusion: manual\r\n{filler}---\r\n# Title\r\n"
        )
        assert entry["description"] == ""

    def test_front_matter_past_the_head_slice_yields_no_description(self, fake_home):
        """With no closing fence inside the slice there is no body to read, and
        falling back to the raw head would reinstate the exact bug above."""
        filler = "".join(f"pad{i}: {'x' * 80}\n" for i in range(60))
        entry = self._entry(fake_home, f"---\ninclusion: manual\n{filler}---\n# Title\n")
        assert entry["description"] == ""

    def test_free_text_metadata_is_redacted(self, fake_home):
        """``inclusion``/``fileMatchPattern`` are author-supplied text on the
        same never-round-tripped path as the description."""
        leak = "AKIAIOSFODNN7EXAMPLE1234567890abcdefghij"
        entry = self._entry(
            fake_home,
            f"---\ninclusion: fileMatch\nfileMatchPattern: {leak}\n---\n# Doc\n",
        )
        assert "AKIAIOSFODNN7EXAMPLE" not in entry["file_match_pattern"]

    def test_free_text_metadata_is_capped(self, fake_home):
        entry = self._entry(
            fake_home,
            "---\ninclusion: fileMatch\nfileMatchPattern: " + "a" * 500 + "\n---\n# Doc\n",
        )
        assert len(entry["file_match_pattern"]) == _STEERING_META_MAX_CHARS


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

    def test_symlinked_leaf_inside_base_rejected_for_write_but_listed(self, fake_home):
        """A leaf link never resolves for write, but an admissible one lists.

        ``.kiro/steering/rules.md -> ../../README.md`` passes a base-containment
        check, so without a leaf-symlink rejection PUT would truncate — and
        DELETE unlink — an unrelated file the user never opened in the tab.
        The listing DOES show it, read-only: the target is under $HOME, a
        regular file and not sensitive, so the session loader reads it.
        """
        root = fake_home / ".kiro" / "steering"
        root.mkdir(parents=True, exist_ok=True)
        victim = fake_home / "README.md"
        victim.write_text("# real readme\n", encoding="utf-8")
        (root / "rules.md").symlink_to(victim)
        assert resolve_steering_file("user/rules.md", None) is None
        entries = list_steering_blocking(None)["files"]
        assert [f["key"] for f in entries] == ["user/rules.md"]
        assert entries[0]["linked"] is True
        assert entries[0]["editable"] is False

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


class TestLinkedEntries:
    """Leaf symlinks list read-only through the loader's own admission gate.

    ``context._load_steering_resources`` follows a leaf symlink and loads its
    target whenever that target is under ``$HOME``, a regular file, and not
    sensitive — so the listing admits exactly the same set
    (``steering_target_admissible``), read-only, or the tab reports
    "Steering (0)" for documents that load into every session.
    """

    @staticmethod
    def _link(fake_home, rel: str, target: Path) -> Path:
        root = fake_home / ".kiro" / "steering"
        root.mkdir(parents=True, exist_ok=True)
        link = root / rel
        link.symlink_to(target)
        return link

    def test_admissible_link_lists_read_only_with_target_meta(self, fake_home):
        target = fake_home / "dotfiles" / "conventions.md"
        target.parent.mkdir(parents=True)
        target.write_text("# Conventions\nrules\n", encoding="utf-8", newline="")
        self._link(fake_home, "conventions.md", target)

        out = list_steering_blocking(None)
        by_key = {f["key"]: f for f in out["files"]}
        entry = by_key["user/conventions.md"]
        assert entry["linked"] is True
        assert entry["editable"] is False
        # The description comes from the TARGET's head, and the resolved
        # target travels with the entry (home collapsed to ``~``). Display
        # paths are OS-native, so fold the separator before comparing.
        assert entry["description"] == "Conventions"
        assert entry["target"].replace("\\", "/") == "~/dotfiles/conventions.md"
        assert entry["size"] == target.stat().st_size

    def test_regular_entries_report_editable(self, fake_home):
        _write_steering(fake_home / ".kiro" / "steering", "plain.md")
        entry = list_steering_blocking(None)["files"][0]
        assert entry["linked"] is False
        assert entry["editable"] is True
        assert entry["target"] == ""

    def test_link_to_sensitive_target_stays_hidden(self, fake_home):
        secret = fake_home / ".aws" / "creds.md"
        secret.parent.mkdir(parents=True)
        secret.write_text("aws_secret_access_key=nope\n", encoding="utf-8")
        self._link(fake_home, "innocent.md", secret)
        out = list_steering_blocking(None)
        assert [f["key"] for f in out["files"]] == []
        assert "nope" not in json.dumps(out)

    def test_link_escaping_home_stays_hidden(self, fake_home, tmp_path):
        outside = tmp_path / "outside.md"
        outside.write_text("# Outside\n", encoding="utf-8")
        self._link(fake_home, "outside.md", outside)
        assert list_steering_blocking(None)["files"] == []

    def test_dangling_link_stays_hidden(self, fake_home):
        self._link(fake_home, "gone.md", fake_home / "nowhere.md")
        assert list_steering_blocking(None)["files"] == []

    def test_looping_link_stays_hidden_not_500(self, fake_home):
        """A self-referential link raises RuntimeError from resolve, not OSError.

        ``Path.resolve(strict=True)`` reports a symlink LOOP as RuntimeError;
        catching only OSError turned one ``loop.md`` into a 500 for the whole
        listing and the detail read.
        """
        root = fake_home / ".kiro" / "steering"
        root.mkdir(parents=True, exist_ok=True)
        (root / "loop.md").symlink_to(root / "loop.md")
        assert list_steering_blocking(None)["files"] == []
        assert resolve_steering_file("user/loop.md", None, follow_links=True) is None

    def test_workspace_link_inside_steering_root_lists_read_only(self, fake_home, tmp_path):
        """Workspace latitude stops at the steering root — a link within it lists."""
        proj = tmp_path / "proj"
        root = proj / ".kiro" / "steering"
        target = root / "shared" / "conventions.md"
        target.parent.mkdir(parents=True)
        target.write_text("# Project conventions\n", encoding="utf-8", newline="")
        (root / "conventions.md").symlink_to(target)
        entries = list_steering_blocking(proj)["files"]
        by_key = {f["key"]: f for f in entries}
        entry = by_key["workspace/conventions.md"]
        assert entry["linked"] is True
        assert entry["editable"] is False
        assert entry["description"] == "Project conventions"
        # The target itself is also a plain listed document.
        assert by_key["workspace/shared/conventions.md"]["linked"] is False

    def test_workspace_link_to_project_file_outside_root_stays_hidden(self, fake_home, tmp_path):
        """A repository-committed link must not read project files through steering.

        ``workspace`` has no session loader following links (kiro-cli reads the
        root itself), so there is no parity to honor — and with the whole
        project as the base, ``leak.md -> ../../.env`` would serve the
        project's own credentials verbatim through the steering GET.
        """
        proj = tmp_path / "proj"
        env = proj / ".env"
        proj.mkdir()
        env.write_text("SECRET_TOKEN=nope\n", encoding="utf-8")
        root = proj / ".kiro" / "steering"
        root.mkdir(parents=True)
        (root / "leak.md").symlink_to(env)
        out = list_steering_blocking(proj)
        assert [f["key"] for f in out["files"]] == []
        assert "nope" not in json.dumps(out)
        assert resolve_steering_file("workspace/leak.md", proj, follow_links=True) is None

    def test_workspace_link_escaping_project_stays_hidden(self, fake_home, tmp_path):
        """A link out of the project entirely (into $HOME) is likewise hidden."""
        notes = fake_home / ".config" / "notes.md"
        notes.parent.mkdir(parents=True)
        notes.write_text("home file a repo link must not reach\n", encoding="utf-8")
        proj = tmp_path / "proj"
        root = proj / ".kiro" / "steering"
        root.mkdir(parents=True)
        (root / "notes.md").symlink_to(notes)
        entries = list_steering_blocking(proj)["files"]
        assert [f["key"] for f in entries] == []
        assert resolve_steering_file("workspace/notes.md", proj, follow_links=True) is None

    @pytest.mark.asyncio
    async def test_read_endpoint_serves_workspace_linked_content(self, fake_home, tmp_path):
        proj = tmp_path / "proj"
        root = proj / ".kiro" / "steering"
        target = root / "shared" / "conventions.md"
        target.parent.mkdir(parents=True)
        target.write_text("# Project conventions\nbody\n", encoding="utf-8", newline="")
        (root / "conventions.md").symlink_to(target)
        async with TestClient(TestServer(_make_app(_state(proj)))) as client:
            resp = await client.get("/api/steering/workspace/conventions.md")
            assert resp.status == 200
            data = await resp.json()
        assert data["content"] == "# Project conventions\nbody\n"

    def test_resolve_follows_links_only_when_asked(self, fake_home):
        target = fake_home / "notes.md"
        target.write_text("# Notes\n", encoding="utf-8")
        self._link(fake_home, "notes.md", target)
        assert resolve_steering_file("user/notes.md", None) is None
        assert (
            resolve_steering_file("user/notes.md", None, follow_links=True)
            == target.resolve()
        )

    def test_resolve_refuses_inadmissible_target_even_following(self, fake_home, tmp_path):
        outside = tmp_path / "outside.md"
        outside.write_text("# Outside\n", encoding="utf-8")
        self._link(fake_home, "outside.md", outside)
        assert resolve_steering_file("user/outside.md", None, follow_links=True) is None

    @pytest.mark.asyncio
    async def test_read_endpoint_serves_linked_content(self, fake_home):
        target = fake_home / "dotfiles" / "conventions.md"
        target.parent.mkdir(parents=True)
        target.write_text("# Conventions\nlinked body\n", encoding="utf-8", newline="")
        self._link(fake_home, "conventions.md", target)
        async with TestClient(TestServer(_make_app(_state()))) as client:
            resp = await client.get("/api/steering/user/conventions.md")
            assert resp.status == 200
            data = await resp.json()
        assert data["content"] == "# Conventions\nlinked body\n"

    @pytest.mark.asyncio
    async def test_put_and_delete_still_refuse_linked_entries(self, fake_home):
        """READ/LIST latitude must not leak into the write verbs."""
        target = fake_home / "dotfiles" / "conventions.md"
        target.parent.mkdir(parents=True)
        target.write_text("# Conventions\noriginal\n", encoding="utf-8", newline="")
        link = self._link(fake_home, "conventions.md", target)
        async with TestClient(TestServer(_make_app(_state()))) as client:
            put = await client.put(
                "/api/steering/user/conventions.md", json={"content": "clobbered"}
            )
            assert put.status == 404
            delete = await client.delete("/api/steering/user/conventions.md")
            assert delete.status == 404
        # Neither the link nor the target moved.
        assert link.is_symlink()
        assert target.read_text(encoding="utf-8") == "# Conventions\noriginal\n"


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


class TestProjectStateReason:
    """``active_project_state`` must say WHY there is no project, not just that.

    ``active_project_dir`` collapses "nothing is set" and "your open chats
    disagree" to ``None``; the UI needs them apart because the remedies differ.
    """

    @staticmethod
    def _multi_slot_state(*projects):
        slots = {
            f"slot{i}": MagicMock(project=str(p), is_restricted=False)
            for i, p in enumerate(projects)
        }
        return MagicMock(_slots=slots, _restricted_keys=set())

    def test_single_slot_with_project_is_set(self, fake_home, tmp_path):
        proj = tmp_path / "proj"
        assert active_project_state(self._multi_slot_state(proj)) == (proj, "set")

    def test_session_key_singles_out_its_own_slot(self, fake_home, tmp_path):
        """A named slot resolves even while another slot names a DIFFERENT project."""
        state = self._multi_slot_state(tmp_path / "a", tmp_path / "b")
        assert active_project_state(state, "dashboard:slot1") == (tmp_path / "b", "set")
        assert active_project_state(state, "dashboard:slot0") == (tmp_path / "a", "set")

    def test_disagreeing_slots_without_a_session_key_are_ambiguous(self, fake_home, tmp_path):
        state = self._multi_slot_state(tmp_path / "a", tmp_path / "b")
        assert active_project_state(state) == (None, "ambiguous")

    def test_no_project_anywhere_is_none(self, fake_home):
        slots = {
            "a": MagicMock(project="", is_restricted=False),
            "b": MagicMock(project="", is_restricted=False),
        }
        assert active_project_state(MagicMock(_slots=slots)) == (None, "none")

    def test_no_slots_at_all_is_none(self, fake_home):
        assert active_project_state(MagicMock(_slots={})) == (None, "none")

    def test_two_slots_on_the_same_project_are_set_not_ambiguous(self, fake_home, tmp_path):
        """Distinct projects is what makes an answer indefensible — not slot count."""
        proj = tmp_path / "proj"
        assert active_project_state(self._multi_slot_state(proj, proj)) == (proj, "set")

    def test_active_project_dir_is_unchanged_by_the_refactor(self, fake_home, tmp_path):
        """Both "no answer" states must still read as a plain ``None`` here."""
        ambiguous = self._multi_slot_state(tmp_path / "a", tmp_path / "b")
        assert active_project_state(ambiguous)[1] == "ambiguous"
        assert active_project_dir(ambiguous) is None

        none = MagicMock(_slots={"a": MagicMock(project="", is_restricted=False)})
        assert active_project_state(none)[1] == "none"
        assert active_project_dir(none) is None

    @pytest.mark.asyncio
    async def test_listing_reports_project_state_set(self, fake_home, tmp_path):
        proj = tmp_path / "proj"
        _write_steering(proj / ".kiro" / "steering", "a.md")
        async with TestClient(TestServer(_make_app(_state(proj)))) as client:
            data = await (await client.get("/api/steering")).json()
        assert data["project_state"] == "set"

    @pytest.mark.asyncio
    async def test_listing_reports_project_state_none(self, fake_home):
        async with TestClient(TestServer(_make_app(_state()))) as client:
            data = await (await client.get("/api/steering")).json()
        assert data["project_state"] == "none"

    @pytest.mark.asyncio
    async def test_listing_reports_project_state_ambiguous(self, fake_home, tmp_path):
        state = self._multi_slot_state(tmp_path / "a", tmp_path / "b")
        async with TestClient(TestServer(_make_app(state))) as client:
            data = await (await client.get("/api/steering")).json()
        assert data["project_state"] == "ambiguous"

    @pytest.mark.asyncio
    async def test_workspace_refusal_reason_and_message_differ_by_cause(
        self, fake_home, tmp_path
    ):
        """Both causes still 400, but the reason AND the wording must differ."""
        body = {"name": "x.md", "content": "y", "source": "workspace"}

        async with TestClient(TestServer(_make_app(_state()))) as client:
            resp = await client.post("/api/steering", json=body)
            assert resp.status == 400
            no_project = await resp.json()

        state = self._multi_slot_state(tmp_path / "a", tmp_path / "b")
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/steering", json=body)
            assert resp.status == 400
            ambiguous = await resp.json()

        # One machine-readable identity for "workspace scope is unavailable", and
        # the CAUSE carried in the human text — no separate `reason` field, which
        # had no consumer. The distinction being pinned is that the two causes do
        # not render as the same sentence.
        assert no_project["code"] == "steering_workspace_unavailable"
        assert ambiguous["code"] == "steering_workspace_unavailable"
        assert "reason" not in no_project and "reason" not in ambiguous
        assert ambiguous["error"] != no_project["error"]
        assert "different projects" in ambiguous["error"]
        assert "no project is set" in no_project["error"]
        assert not (tmp_path / "a" / ".kiro").exists()
        assert not (tmp_path / "b" / ".kiro").exists()


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
            resp = await client.post(
                "/api/steering",
                json={"name": "api.md", "content": "x"},
                headers=_project_headers(proj),
            )
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
    async def test_update_routes_acl_preservation_through_atomic_write(
        self, fake_home, monkeypatch
    ):
        """The update path must hand atomic_write a source descriptor.

        atomic_write's mode= carries permission BITS only; a named POSIX ACL
        survives only when the source's xattrs are carried from
        an OPEN descriptor. Assert the handler opens one and passes it via
        preserve_access_control_from, so a revert to the bits-only call fails
        here. The fd must reference the existing file, so its content matches.
        """
        import os as _os

        import kiro_crew.atomic_write as aw
        from kiro_crew.dashboard.handlers import steering as mod

        root = fake_home / ".kiro" / "steering"
        path = _write_steering(root, "a.md", "original\n")

        captured: dict[str, object] = {}
        original = mod.atomic_write

        def recording(target, content, **kwargs):
            captured["kwargs"] = dict(kwargs)
            src_fd = kwargs.get("preserve_access_control_from")
            if isinstance(src_fd, int):
                # The descriptor must point at the file being replaced.
                captured["source_bytes"] = _os.read(src_fd, 4096)
            original(target, content, **kwargs)

        monkeypatch.setattr(mod, "atomic_write", recording)
        async with TestClient(TestServer(_make_app(_state()))) as client:
            assert (
                await client.put("/api/steering/user/a.md", json={"content": "new"})
            ).status == 200

        kwargs = captured["kwargs"]
        # On a platform with the xattr syscalls the handler must hand over a real
        # descriptor; where they do not exist (Windows) the contract is the
        # opposite -- open_access_control_source returns None ON PURPOSE, because
        # os.replace there fails while any other handle is open on either path.
        # Asserting `int` unconditionally would demand the very handle that would
        # break every save. Either way the kwarg must be PASSED, so a revert to
        # the bits-only call still fails here.
        assert "preserve_access_control_from" in kwargs
        if aw.ACCESS_CONTROL_XATTRS_SUPPORTED:
            assert isinstance(kwargs["preserve_access_control_from"], int)
            assert captured["source_bytes"] == b"original\n"
        else:  # pragma: no cover - exercised on Windows CI only
            assert kwargs["preserve_access_control_from"] is None
        # Additive to the permission-bit carry, not a replacement.
        assert kwargs["newline"] == ""
        assert kwargs["fsync"] is True
        assert kwargs["mode"] == stat.S_IMODE(path.stat().st_mode)
        assert path.read_text() == "new"

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


class TestDeclarationEdit:
    """``PUT`` may edit the document's declaration, not just its text.

    The front matter is rewritten server-side so the editor never has to splice
    YAML into its own textarea — a client that got that subtly wrong would
    corrupt the file whose whole purpose is to be read by the agent.
    """

    async def _put(self, client, body):
        return await client.put("/api/steering/user/doc.md", json=body)

    @pytest.mark.asyncio
    async def test_sets_a_mode_and_preserves_the_body(self, fake_home):
        root = fake_home / ".kiro" / "steering"
        _write_steering(root, "doc.md", "# Payroll\n\nrules  \n\ttabbed\n")
        async with TestClient(TestServer(_make_app(_state()))) as client:
            resp = await self._put(
                client,
                {"content": "# Payroll\n\nrules  \n\ttabbed\n", "inclusion": "manual"},
            )
            data = await resp.json()
        assert resp.status == 200
        stored = (root / "doc.md").read_text()
        assert stored == "---\ninclusion: manual\n---\n# Payroll\n\nrules  \n\ttabbed\n"
        # The response echoes the rewritten text: an editor still holding the
        # old body would otherwise re-send it and undo the mode it just set.
        assert data["content"] == stored
        assert data["inclusion"] == "manual"

    @pytest.mark.asyncio
    async def test_empty_value_removes_the_declaration(self, fake_home):
        root = fake_home / ".kiro" / "steering"
        _write_steering(root, "doc.md", "---\ninclusion: manual\n---\n# Doc\n")
        async with TestClient(TestServer(_make_app(_state()))) as client:
            resp = await self._put(
                client, {"content": "---\ninclusion: manual\n---\n# Doc\n", "inclusion": ""}
            )
            data = await resp.json()
        assert resp.status == 200
        assert (root / "doc.md").read_text() == "# Doc\n"
        assert data["inclusion"] == STEERING_INCLUSION_DEFAULT
        assert data["inclusion_declared"] == ""

    @pytest.mark.asyncio
    async def test_file_match_carries_its_pattern(self, fake_home):
        root = fake_home / ".kiro" / "steering"
        _write_steering(root, "doc.md", "# Doc\n")
        async with TestClient(TestServer(_make_app(_state()))) as client:
            resp = await self._put(
                client,
                {
                    "content": "# Doc\n",
                    "inclusion": "fileMatch",
                    "file_match_pattern": "src/**/*.ts",
                },
            )
            data = await resp.json()
        assert resp.status == 200
        assert data["file_match_pattern"] == "src/**/*.ts"
        assert "fileMatchPattern" in (root / "doc.md").read_text()

    @pytest.mark.asyncio
    async def test_file_match_without_a_pattern_is_refused(self, fake_home):
        """A patternless fileMatch document can never match, so it would be
        withheld forever with nothing to explain why."""
        _write_steering(fake_home / ".kiro" / "steering", "doc.md", "# Doc\n")
        async with TestClient(TestServer(_make_app(_state()))) as client:
            resp = await self._put(client, {"content": "# Doc\n", "inclusion": "fileMatch"})
            data = await resp.json()
        assert resp.status == 400
        assert data["code"] == "steering_file_match_needs_pattern"

    @pytest.mark.asyncio
    async def test_mode_flip_keeps_an_existing_pattern(self, fake_home):
        """The pattern check reads the RESULT, so flipping the mode back on a
        document that already carries a pattern is not a rejection."""
        root = fake_home / ".kiro" / "steering"
        body = '---\ninclusion: manual\nfileMatchPattern: "src/**/*.ts"\n---\n# Doc\n'
        _write_steering(root, "doc.md", body)
        async with TestClient(TestServer(_make_app(_state()))) as client:
            resp = await self._put(client, {"content": body, "inclusion": "fileMatch"})
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_unknown_mode_is_refused(self, fake_home):
        _write_steering(fake_home / ".kiro" / "steering", "doc.md", "# Doc\n")
        async with TestClient(TestServer(_make_app(_state()))) as client:
            resp = await self._put(client, {"content": "# Doc\n", "inclusion": "manaul"})
            data = await resp.json()
        assert resp.status == 400
        assert data["code"] == "steering_unknown_inclusion"

    @pytest.mark.asyncio
    async def test_mode_spelling_is_canonicalized_on_write(self, fake_home):
        root = fake_home / ".kiro" / "steering"
        _write_steering(root, "doc.md", "# Doc\n")
        async with TestClient(TestServer(_make_app(_state()))) as client:
            resp = await self._put(client, {"content": "# Doc\n", "inclusion": "FILEMATCH",
                                            "file_match_pattern": "*.ts"})
            data = await resp.json()
        assert resp.status == 200
        assert data["inclusion_declared"] == "fileMatch"
        assert "inclusion: fileMatch" in (root / "doc.md").read_text()

    @pytest.mark.asyncio
    async def test_a_value_that_cannot_round_trip_is_refused(self, fake_home):
        """The single-line grammar has no escape sequence, so a value ending in
        a quote would read back shorter than it went in. Refuse rather than
        silently truncate the user's text."""
        _write_steering(fake_home / ".kiro" / "steering", "doc.md", "# Doc\n")
        async with TestClient(TestServer(_make_app(_state()))) as client:
            resp = await self._put(
                client,
                {
                    "content": "# Doc\n",
                    "inclusion": "fileMatch",
                    "file_match_pattern": 'ends with a quote"',
                },
            )
            data = await resp.json()
        assert resp.status == 400
        assert data["code"] == "steering_field_unrepresentable"

    @pytest.mark.asyncio
    async def test_content_only_put_leaves_front_matter_alone(self, fake_home):
        """No declaration field in the request means no rewrite at all — the
        plain save path stays byte-exact."""
        root = fake_home / ".kiro" / "steering"
        body = "---\ninclusion: manual\ncustomKey: kept\n---\n# Doc\n"
        _write_steering(root, "doc.md", body)
        async with TestClient(TestServer(_make_app(_state()))) as client:
            resp = await self._put(client, {"content": body})
        assert resp.status == 200
        assert (root / "doc.md").read_text() == body


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


class TestProjectPrecondition:
    """A workspace write must name the project it believed it was acting on.

    A chat slot's project is mutable, so the session key cannot carry this: the
    tab lists project A, the slot is re-pointed at B, and a delete issued from
    the still-visible listing would resolve B and remove B's same-named file.
    """

    @staticmethod
    def _two_projects(tmp_path):
        listed = tmp_path / "listed"
        moved = tmp_path / "moved"
        for proj in (listed, moved):
            _write_steering(proj / ".kiro" / "steering", "api.md", "# listed\n")
        return listed, moved

    @pytest.mark.asyncio
    async def test_listing_publishes_a_project_key(self, fake_home, tmp_path):
        proj = tmp_path / "proj"
        _write_steering(proj / ".kiro" / "steering", "a.md")
        async with TestClient(TestServer(_make_app(_state(proj)))) as client:
            data = await (await client.get("/api/steering")).json()
        assert data["project_key"] == _project_key(proj)
        assert data["project_key"]

    @pytest.mark.asyncio
    async def test_no_project_publishes_an_empty_key(self, fake_home):
        async with TestClient(TestServer(_make_app(_state()))) as client:
            data = await (await client.get("/api/steering")).json()
        assert data["project_key"] == ""

    @pytest.mark.asyncio
    async def test_delete_against_a_moved_slot_is_refused(self, fake_home, tmp_path):
        """The data-loss vector itself: neither project's file may be removed."""
        listed, moved = self._two_projects(tmp_path)
        # The slot now points at `moved`, but the client still holds `listed`.
        async with TestClient(TestServer(_make_app(_state(moved)))) as client:
            resp = await client.delete(
                "/api/steering/workspace/api.md", headers=_project_headers(listed)
            )
            assert resp.status == 409
            body = await resp.json()
        assert body["code"] == "steering_project_changed"
        assert (moved / ".kiro" / "steering" / "api.md").is_file()
        assert (listed / ".kiro" / "steering" / "api.md").is_file()

    @pytest.mark.asyncio
    async def test_update_against_a_moved_slot_is_refused(self, fake_home, tmp_path):
        listed, moved = self._two_projects(tmp_path)
        async with TestClient(TestServer(_make_app(_state(moved)))) as client:
            resp = await client.put(
                "/api/steering/workspace/api.md",
                json={"content": "# overwritten\n"},
                headers=_project_headers(listed),
            )
            assert resp.status == 409
        assert (moved / ".kiro" / "steering" / "api.md").read_text() == "# listed\n"

    @pytest.mark.asyncio
    async def test_create_against_a_moved_slot_is_refused(self, fake_home, tmp_path):
        listed, moved = self._two_projects(tmp_path)
        async with TestClient(TestServer(_make_app(_state(moved)))) as client:
            resp = await client.post(
                "/api/steering",
                json={"name": "new.md", "content": "x", "source": "workspace"},
                headers=_project_headers(listed),
            )
            assert resp.status == 409
        assert not (moved / ".kiro" / "steering" / "new.md").exists()

    @pytest.mark.asyncio
    async def test_absent_header_fails_closed_on_workspace_writes(self, fake_home, tmp_path):
        """A caller that cannot say which project it meant has not earned a write."""
        proj = tmp_path / "proj"
        _write_steering(proj / ".kiro" / "steering", "api.md")
        async with TestClient(TestServer(_make_app(_state(proj)))) as client:
            assert (await client.delete("/api/steering/workspace/api.md")).status == 409
            assert (
                await client.put(
                    "/api/steering/workspace/api.md", json={"content": "x"}
                )
            ).status == 409
        assert (proj / ".kiro" / "steering" / "api.md").is_file()

    @pytest.mark.asyncio
    async def test_matching_key_is_allowed_through(self, fake_home, tmp_path):
        proj = tmp_path / "proj"
        _write_steering(proj / ".kiro" / "steering", "api.md")
        async with TestClient(TestServer(_make_app(_state(proj)))) as client:
            resp = await client.put(
                "/api/steering/workspace/api.md",
                json={"content": "# updated\n"},
                headers=_project_headers(proj),
            )
            assert resp.status == 200
        assert (proj / ".kiro" / "steering" / "api.md").read_text() == "# updated\n"

    @pytest.mark.asyncio
    async def test_user_scoped_writes_need_no_precondition(self, fake_home, tmp_path):
        """``user/`` keys are anchored to $HOME, so no project can move under them."""
        _write_steering(fake_home / ".kiro" / "steering", "mine.md")
        async with TestClient(TestServer(_make_app(_state(tmp_path / "proj")))) as client:
            resp = await client.put(
                "/api/steering/user/mine.md", json={"content": "# mine\n"}
            )
            assert resp.status == 200
            assert (await client.delete("/api/steering/user/mine.md")).status == 200

    @pytest.mark.asyncio
    async def test_a_refused_write_is_audited(self, fake_home, tmp_path, monkeypatch):
        """A denial nobody records is a denial nobody can review.

        Matches the restricted-session refusal, which already audits: a burst of
        stale-header denials is exactly the shape a confused or hostile client
        produces, and it has to be visible in the same place.
        """
        listed, moved = self._two_projects(tmp_path)
        calls: list[dict[str, object]] = []

        class _Sel:
            @staticmethod
            def log_api_access(**kw: object) -> None:
                calls.append(kw)

            @staticmethod
            def log_tool_invocation(**kw: object) -> None:
                pass

        monkeypatch.setattr(steering_mod, "_sel", lambda: _Sel())
        async with TestClient(TestServer(_make_app(_state(moved)))) as client:
            assert (await client.delete(
                "/api/steering/workspace/api.md", headers=_project_headers(listed)
            )).status == 409
            assert (await client.put(
                "/api/steering/workspace/api.md",
                json={"content": "x"},
                headers=_project_headers(listed),
            )).status == 409

        denials = [c for c in calls if c.get("outcome") == "denied"]
        assert [c["operation"] for c in denials] == ["steering.delete", "steering.update"]
        assert {c["resources"] for c in denials} == {"steering_project_changed"}

    @pytest.mark.asyncio
    async def test_reads_are_not_gated(self, fake_home, tmp_path):
        """A stale read misleads but destroys nothing; the save comes back gated."""
        proj = tmp_path / "proj"
        _write_steering(proj / ".kiro" / "steering", "api.md", "# body\n")
        async with TestClient(TestServer(_make_app(_state(proj)))) as client:
            resp = await client.get("/api/steering/workspace/api.md")
            assert resp.status == 200
            assert (await resp.json())["content"] == "# body\n"
