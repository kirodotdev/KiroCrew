"""Every skill browser read stays inside the admitted skill root.

The package-skill detail read goes through the descriptor gate; a symlinked
skill leaf cannot escape its provider root (for every provider), the catalog
and the tree endpoint never offer one that does, and the file read is pinned
to a descriptor the check opened, so neither a leaf nor a root swapped for a
link afterwards is read. Shared helpers live in ``skill_browser_helpers.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from skill_browser_helpers import (  # noqa: F401 -- the fixture is requested by name
    _make_app,
    _write_skill,
    fake_home_fixture,
)

from kiro_crew.dashboard.handlers._shared import (
    _resolve_skill_root,
    enumerate_skill_catalog,
    read_skill_file,
)

# ── The package-skill detail read goes through the descriptor gate ──


class TestPackageSkillDetailReadsThroughTheGate:
    """``api_skill_detail``'s ``package/`` branch reads what it validated.

    ``validate_file_path(row["path"])`` canonicalizes and refuses a sensitive
    target, and a naive read would open that same name a second time. A
    HARDLINK defeats the first resolution with no race and no link: it shares its
    target's inode, so ``realpath`` yields the alias's own innocent path and
    ``is_sensitive_path`` judges that instead of the file whose bytes come back.
    The gate ``fstat``s the one descriptor it opened and refuses ``st_nlink > 1``.
    """

    @staticmethod
    def _request(name: str):
        req = MagicMock()
        req.method = "GET"
        req.match_info = {"name": name}
        req.app = {"state": MagicMock()}
        return req

    @staticmethod
    def _wire(monkeypatch, path: Path) -> None:
        from kiro_crew.dashboard.handlers import prompts

        loader = MagicMock()
        loader.load_skill = lambda _name: None
        monkeypatch.setattr(prompts, "_get_skills", lambda _state: loader)

        mgr = MagicMock()
        mgr.available = lambda: True

        async def _list_skills():
            return [{"key": "package/aliased", "name": "aliased", "path": str(path)}]

        mgr.list_skills = _list_skills
        monkeypatch.setattr(prompts, "_capability_manager", lambda: mgr)

    @pytest.mark.asyncio
    async def test_a_hardlinked_package_skill_is_not_served(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.handlers import api_skill_detail

        secret = tmp_path / "credentials"
        secret.write_text("# aws_secret_access_key = SHOULD-NOT-APPEAR\n", encoding="utf-8")
        alias = tmp_path / "pkg" / "SKILL.md"
        alias.parent.mkdir(parents=True)
        try:
            import os as _os

            _os.link(secret, alias)
        except (OSError, NotImplementedError) as exc:  # pragma: no cover - host capability
            pytest.skip(f"filesystem does not support hardlinks: {exc}")
        if alias.stat().st_nlink < 2:  # pragma: no cover - host capability
            pytest.skip("filesystem did not create a second link")
        self._wire(monkeypatch, alias)

        resp = await api_skill_detail(self._request("package/aliased"))
        # The same 404 an unreadable skill already produced, so a refusal is not
        # distinguishable from I/O trouble.
        assert resp.status == 404
        assert b"SHOULD-NOT-APPEAR" not in resp.body

    @pytest.mark.asyncio
    async def test_an_ordinary_package_skill_is_still_served(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.handlers import api_skill_detail

        real = tmp_path / "pkg" / "SKILL.md"
        real.parent.mkdir(parents=True)
        real.write_text("# Real Skill\nBody.\n", encoding="utf-8")
        self._wire(monkeypatch, real)

        resp = await api_skill_detail(self._request("package/aliased"))
        assert resp.status == 200
        assert "Body." in json.loads(resp.body)["content"]

    @pytest.mark.asyncio
    async def test_a_refused_package_skill_leaves_an_audit_line(self, tmp_path, monkeypatch):
        """The 404 is deliberately indistinguishable; the SEL line is not.

        A 404 is also what a skill name nobody installed produces, so without an
        audit record a planted alias leaves the operator nothing to find. The
        line says only THAT bytes were withheld: the gate judges and reads
        through one descriptor and answers a bare ``None``.
        """
        from kiro_crew.dashboard import handlers as handlers_pkg
        from kiro_crew.dashboard.handlers import api_skill_detail

        secret = tmp_path / "credentials"
        secret.write_text("# aws_secret_access_key = SHOULD-NOT-APPEAR\n", encoding="utf-8")
        alias = tmp_path / "pkg" / "SKILL.md"
        alias.parent.mkdir(parents=True)
        try:
            import os as _os

            _os.link(secret, alias)
        except (OSError, NotImplementedError) as exc:  # pragma: no cover - host capability
            pytest.skip(f"filesystem does not support hardlinks: {exc}")
        if alias.stat().st_nlink < 2:  # pragma: no cover - host capability
            pytest.skip("filesystem did not create a second link")
        self._wire(monkeypatch, alias)
        recorder = MagicMock()
        monkeypatch.setattr(handlers_pkg, "sel", lambda: recorder)

        assert (await api_skill_detail(self._request("package/aliased"))).status == 404
        refusals = [
            c
            for c in recorder.log_tool_invocation.call_args_list
            if c.kwargs["tool_name"] == "api_skill_detail"
        ]
        assert [c.kwargs["outcome"] for c in refusals] == ["error"]
        assert refusals[0].kwargs["tool_kind"] == "skill"

    @pytest.mark.asyncio
    async def test_the_package_skill_read_runs_off_the_event_loop(self, tmp_path, monkeypatch):
        """No caller cap applies here, so the gate reads up to its own 50 MB.

        Asserted as a shape rather than a duration: the read must happen on some
        thread other than the one running the loop, which is what
        ``asyncio.to_thread`` guarantees and a direct call cannot.
        """
        import threading

        from kiro_crew.dashboard.handlers import api_skill_detail, prompts

        real = tmp_path / "pkg" / "SKILL.md"
        real.parent.mkdir(parents=True)
        real.write_text("# Real Skill\nBody.\n", encoding="utf-8")
        self._wire(monkeypatch, real)

        loop_thread = threading.current_thread()
        read_threads: list[threading.Thread] = []
        gate = prompts.safe_read_file_bytes_nolink

        def _record_thread(*args, **kwargs):
            read_threads.append(threading.current_thread())
            return gate(*args, **kwargs)

        monkeypatch.setattr(prompts, "safe_read_file_bytes_nolink", _record_thread)

        assert (await api_skill_detail(self._request("package/aliased"))).status == 200
        assert read_threads and loop_thread not in read_threads


# ── A symlinked skill leaf must not escape the skills root ──


class TestSymlinkedSkillLeafContainment:
    """Only ``kiro-user/`` carries the leaf-symlink allowance.

    Containment on ``candidate.parent`` alone lets ``<root>/anything -> /etc``
    resolve to ``/etc``, and the tree/file endpoints then enumerate and serve
    that directory. The allowance exists for editions that install
    ``~/.kiro/skills/<name>`` as a link, which is the ``kiro-user/`` prefix and
    nothing else; every other prefix requires the RESOLVED leaf to sit at or
    under its own root.
    """

    @staticmethod
    def _outside(tmp_path: Path) -> Path:
        """A skill-shaped directory that lives outside every skills root."""
        outside = tmp_path / "outside-tree"
        outside.mkdir(parents=True, exist_ok=True)
        (outside / "SKILL.md").write_text("---\nname: x\n---\nsecret", encoding="utf-8")
        (outside / "passwd").write_text("root:x:0:0:", encoding="utf-8")
        return outside

    def test_kirocrew_leaf_symlink_escape_refused(self, fake_home, tmp_path):
        """Default (kirocrew) prefix: the leaf may not point out of the root."""
        outside = self._outside(tmp_path)
        skills_root = fake_home / ".kiro" / "crew" / "skills"
        skills_root.mkdir(parents=True)
        (skills_root / "anything").symlink_to(outside, target_is_directory=True)

        state = MagicMock(_slots={})
        assert _resolve_skill_root("anything", state) is None

    def test_kiro_workspace_leaf_symlink_escape_refused(self, fake_home, tmp_path):
        """The project-scoped prefix from the report: no escape, keyed or not."""
        outside = self._outside(tmp_path)
        proj = tmp_path / "proj"
        skills_root = proj / ".kiro" / "skills"
        skills_root.mkdir(parents=True)
        (skills_root / "anything").symlink_to(outside, target_is_directory=True)

        state = MagicMock(_slots={"slot-a": MagicMock(project=str(proj))})
        assert _resolve_skill_root("kiro-workspace/anything", state, "slot-a") is None
        assert _resolve_skill_root("kiro-workspace/anything", state) is None

    def test_edition_root_leaf_symlink_escape_refused(self, fake_home, tmp_path, monkeypatch):
        """An edition skill root is a plain root here — no leaf allowance."""
        from kiro_crew.dashboard.handlers import _shared

        outside = self._outside(tmp_path)
        edition_root = tmp_path / "edition" / "skills"
        edition_root.mkdir(parents=True)
        (edition_root / "anything").symlink_to(outside, target_is_directory=True)
        monkeypatch.setattr(_shared, "_edition_skill_roots", lambda: [edition_root])

        state = MagicMock(_slots={})
        assert _resolve_skill_root("anything", state) is None

    def test_extra_path_root_leaf_symlink_escape_refused(self, fake_home, tmp_path, monkeypatch):
        """Same for a user-configured ``skills.extra_paths`` root."""
        from kiro_crew.dashboard.handlers import _shared

        outside = self._outside(tmp_path)
        extra_root = tmp_path / "extra" / "skills"
        extra_root.mkdir(parents=True)
        (extra_root / "anything").symlink_to(outside, target_is_directory=True)
        cfg = MagicMock()
        cfg.load.return_value.skills.extra_paths = [str(extra_root)]
        monkeypatch.setattr(_shared, "KiroCrewConfig", cfg)

        state = MagicMock(_slots={})
        assert _resolve_skill_root("anything", state) is None

    def test_package_prefix_leaf_symlink_escape_refused(self, fake_home, tmp_path, monkeypatch):
        """``package/`` has its own early return, and no leaf allowance.

        An edition packager can plant ``<edition-root>/x -> /outside`` exactly
        like any other root, so the same containment applies before the branch
        returns.
        """
        from kiro_crew.dashboard.handlers import _shared

        outside = self._outside(tmp_path)
        pkg_root = tmp_path / "edition-pkgs"
        (pkg_root / "pkg").mkdir(parents=True)
        (pkg_root / "pkg" / "escaping").symlink_to(outside, target_is_directory=True)
        monkeypatch.setattr(_shared, "_edition_skill_roots", lambda: [pkg_root])

        state = MagicMock(_slots={})
        assert _resolve_skill_root("package/pkg/escaping", state) is None
        assert _resolve_skill_root("package/escaping", state) is None

    def test_package_prefix_contained_skill_still_resolves(self, fake_home, tmp_path, monkeypatch):
        """The edition-package path itself keeps working."""
        from kiro_crew.dashboard.handlers import _shared

        pkg_root = tmp_path / "edition-pkgs"
        skill = pkg_root / "pkg" / "real"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("---\nname: real\n---\nbody", encoding="utf-8")
        monkeypatch.setattr(_shared, "_edition_skill_roots", lambda: [pkg_root])

        state = MagicMock(_slots={})
        assert _resolve_skill_root("package/pkg/real", state) == skill.resolve()
        assert _resolve_skill_root("package/real", state) == skill.resolve()

    def test_app_registered_leaf_links_still_resolve(self, fake_home, monkeypatch):
        """``apps.bridges._register_skills`` installs exactly this shape.

        An app ships its skills in its own tree and the bridge symlinks each one
        into the kirocrew skills root twice — flat and namespaced — so both
        resolve outside that root by construction. They are admitted because the
        app DECLARES that directory as a skill, read from the same manifest the
        bridge registers from.
        """
        from kiro_crew.apps import bridges

        app_root = fake_home / ".kiro" / "crew" / "apps" / "demo-app"
        app_skill = app_root / "skills" / "appy"
        app_skill.mkdir(parents=True)
        (app_skill / "SKILL.md").write_text("---\nname: appy\n---\nbody", encoding="utf-8")
        monkeypatch.setattr(
            bridges,
            "_registration_source",
            lambda name: (SimpleNamespace(skills=["skills/appy"]), app_root),
        )

        skills_root = fake_home / ".kiro" / "crew" / "skills"
        (skills_root / "demo-app").mkdir(parents=True)
        (skills_root / "appy").symlink_to(app_skill, target_is_directory=True)
        (skills_root / "demo-app" / "appy").symlink_to(app_skill, target_is_directory=True)

        state = MagicMock(_slots={})
        assert _resolve_skill_root("appy", state) == app_skill.resolve()
        assert _resolve_skill_root("demo-app/appy", state) == app_skill.resolve()
        catalog = enumerate_skill_catalog(state)
        assert "appy" in catalog and "demo-app/appy" in catalog

    def test_shipped_builtin_skill_resolves_when_dir_and_manifest_names_differ(self, fake_home):
        """A shipped builtin's package dir need not match its manifest name.

        ``auto_improvement/app.json`` declares ``"name": "auto-improvement"``.
        The bridge links its declared skills by manifest name, so the browse
        side must find the app from the package root it resolves into, not by
        treating the directory name as the app name.
        """
        from kiro_crew.apps.execution import _BUILTINS_DIR

        pkg_root = (_BUILTINS_DIR / "auto_improvement").resolve()
        assert json.loads((pkg_root / "app.json").read_text(encoding="utf-8"))["name"] == (
            "auto-improvement"
        )
        app_skill = (pkg_root / "skills" / "ai-discover").resolve()
        assert (app_skill / "SKILL.md").is_file()

        skills_root = fake_home / ".kiro" / "crew" / "skills"
        (skills_root / "auto-improvement").mkdir(parents=True)
        (skills_root / "ai-discover").symlink_to(app_skill, target_is_directory=True)
        (skills_root / "auto-improvement" / "ai-discover").symlink_to(
            app_skill, target_is_directory=True
        )

        state = MagicMock(_slots={})
        assert _resolve_skill_root("ai-discover", state) == app_skill
        assert _resolve_skill_root("auto-improvement/ai-discover", state) == app_skill
        # Still the declared skill dirs only, never the rest of the package.
        (skills_root / "peek-pkg").symlink_to(pkg_root, target_is_directory=True)
        assert _resolve_skill_root("peek-pkg", state) is None

    def test_a_link_into_an_apps_private_files_is_refused(self, fake_home, monkeypatch):
        """The exception is the DECLARED skill directories, not the app root.

        An app tree also holds that app's data, tokens and rendered configs, so
        admitting the root would turn a planted link into a reader for them.
        """
        from kiro_crew.apps import bridges

        app_root = fake_home / ".kiro" / "crew" / "apps" / "demo-app"
        declared = app_root / "skills" / "appy"
        declared.mkdir(parents=True)
        (declared / "SKILL.md").write_text("---\nname: appy\n---\nbody", encoding="utf-8")
        (app_root / "data").mkdir()
        (app_root / "data" / "SKILL.md").write_text("---\nname: d\n---\nsecret", encoding="utf-8")
        (app_root / "credentials").mkdir()
        (app_root / "credentials" / "SKILL.md").write_text("---\nname: c\n---\nk", encoding="utf-8")

        monkeypatch.setattr(
            bridges,
            "_registration_source",
            lambda name: (SimpleNamespace(skills=["skills/appy"]), app_root),
        )

        skills_root = fake_home / ".kiro" / "crew" / "skills"
        skills_root.mkdir(parents=True)
        (skills_root / "appy").symlink_to(declared, target_is_directory=True)
        (skills_root / "peek-data").symlink_to(app_root / "data", target_is_directory=True)
        (skills_root / "peek-creds").symlink_to(app_root / "credentials", target_is_directory=True)

        state = MagicMock(_slots={})
        assert _resolve_skill_root("appy", state) == declared.resolve()
        assert _resolve_skill_root("peek-data", state) is None
        assert _resolve_skill_root("peek-creds", state) is None
        catalog = enumerate_skill_catalog(state)
        assert "appy" in catalog
        assert "peek-data" not in catalog and "peek-creds" not in catalog

    def test_a_leaf_link_outside_every_provider_root_is_still_refused(self, fake_home, tmp_path):
        """The allowance is the provider roots, not "outside is fine now"."""
        outside = self._outside(tmp_path)
        skills_root = fake_home / ".kiro" / "crew" / "skills"
        skills_root.mkdir(parents=True)
        (skills_root / "not-an-app").symlink_to(outside, target_is_directory=True)

        state = MagicMock(_slots={})
        assert _resolve_skill_root("not-an-app", state) is None
        assert "not-an-app" not in enumerate_skill_catalog(state)

    def test_kiro_user_leaf_symlink_still_allowed(self, fake_home, tmp_path):
        """The edition install shape keeps working — this is the exception."""
        target = tmp_path / "agents-tree" / "skills" / "linked"
        target.mkdir(parents=True)
        (target / "SKILL.md").write_text("---\nname: linked\n---\nbody", encoding="utf-8")
        kiro_skills = fake_home / ".kiro" / "skills"
        kiro_skills.mkdir(parents=True)
        (kiro_skills / "linked").symlink_to(target, target_is_directory=True)

        state = MagicMock(_slots={})
        assert _resolve_skill_root("kiro-user/linked", state) == target.resolve()

    def test_leaf_symlink_inside_the_root_still_resolves(self, fake_home):
        """Containment, not a symlink ban: a link to a sibling skill is fine."""
        skills_root = fake_home / ".kiro" / "crew" / "skills"
        real = _write_skill(skills_root, "real-skill")
        (skills_root / "alias").symlink_to(real, target_is_directory=True)

        state = MagicMock(_slots={})
        assert _resolve_skill_root("alias", state) == real.resolve()

    def test_a_standing_symlink_loop_is_simply_absent(self, fake_home):
        """A loop that is already there never reaches a resolve: ``is_dir()``
        is False for it, so it is skipped with every other non-directory."""
        skills_root = fake_home / ".kiro" / "crew" / "skills"
        skills_root.mkdir(parents=True)
        _write_skill(skills_root, "real-skill")
        loop = skills_root / "loop"
        loop.symlink_to(skills_root / "loop-b", target_is_directory=True)
        (skills_root / "loop-b").symlink_to(loop, target_is_directory=True)

        state = MagicMock(_slots={})
        catalog = enumerate_skill_catalog(state)
        assert "real-skill" in catalog
        assert "loop" not in catalog and "loop-b" not in catalog
        assert _resolve_skill_root("loop", state) is None

    def test_a_leaf_that_becomes_a_loop_is_refused_not_raised(self, fake_home, monkeypatch):
        """The reachable case is the RACE: a directory that turns into a loop
        after ``is_dir()`` said yes, so the resolve is what meets it.

        ``Path.resolve`` answers a loop with ``RuntimeError`` — not ``OSError``
        — on CPython, and both of these surfaces answer an HTTP request, so the
        entry is refused rather than propagated as a 500. The stub raises for
        exactly one path and delegates every other resolve to the real one.
        """
        skills_root = fake_home / ".kiro" / "crew" / "skills"
        _write_skill(skills_root, "real-skill")
        racing = _write_skill(skills_root, "racing-skill")

        real_resolve = Path.resolve

        def _loop_for_one(self: Path, *args, **kwargs):
            if self == racing:
                raise RuntimeError(f"Symlink loop from {str(self)!r}")
            return real_resolve(self, *args, **kwargs)

        monkeypatch.setattr(Path, "resolve", _loop_for_one)

        state = MagicMock(_slots={})
        catalog = enumerate_skill_catalog(state)
        assert "real-skill" in catalog
        assert "racing-skill" not in catalog
        assert _resolve_skill_root("racing-skill", state) is None
        assert _resolve_skill_root("real-skill", state) == real_resolve(skills_root / "real-skill")

    def test_catalog_does_not_offer_an_escaping_leaf(self, fake_home, tmp_path):
        """Enumeration and resolution must agree, so the catalog drops it too.

        An enumerated key the resolver refuses is a phantom entry in the
        agent-template editor — and its ``skill://`` URI would name a file
        outside the root.
        """
        outside = self._outside(tmp_path)
        proj = tmp_path / "proj"
        ws_root = proj / ".kiro" / "skills"
        ws_root.mkdir(parents=True)
        (ws_root / "anything").symlink_to(outside, target_is_directory=True)
        # kiro-user keeps its allowance, so its linked skill stays enumerated.
        user_target = tmp_path / "linked-user"
        user_target.mkdir()
        (user_target / "SKILL.md").write_text("---\nname: u\n---\nb", encoding="utf-8")
        user_root = fake_home / ".kiro" / "skills"
        user_root.mkdir(parents=True)
        (user_root / "linked").symlink_to(user_target, target_is_directory=True)

        state = MagicMock(_slots={"slot-a": MagicMock(project=str(proj))})
        catalog = enumerate_skill_catalog(state, "slot-a")
        assert "kiro-workspace/anything" not in catalog
        assert "kiro-user/linked" in catalog

    @pytest.mark.asyncio
    async def test_tree_endpoint_refuses_an_escaping_workspace_leaf(self, fake_home, tmp_path):
        """The reported threat path, end to end: 404 instead of 500 entries."""
        outside = self._outside(tmp_path)
        proj = tmp_path / "proj"
        ws_root = proj / ".kiro" / "skills"
        ws_root.mkdir(parents=True)
        (ws_root / "anything").symlink_to(outside, target_is_directory=True)

        state = MagicMock(
            _slots={"slot-a": MagicMock(project=str(proj), _app="")},
            context_builder=None,
        )
        async with TestClient(TestServer(_make_app(state))) as client:
            tree = await client.get(
                "/api/skills/kiro-workspace/anything/-/tree",
                headers={"X-Session-Key": "slot-a"},
            )
            assert tree.status == 404
            f = await client.get(
                "/api/skills/kiro-workspace/anything/-/file?path=passwd",
                headers={"X-Session-Key": "slot-a"},
            )
            assert f.status == 404


class TestSkillFileIsReadThroughAPinnedDescriptor:
    """``read_skill_file`` validates the inode it actually reads.

    Resolving the path, checking containment, then re-opening by name with
    ``Path.read_text`` leaves a check-to-use window and no hardlink guard at
    all, so the bytes come from a descriptor-pinned read instead.
    """

    def test_hardlinked_file_inside_a_skill_is_refused(self, fake_home, tmp_path):
        """``resolve()`` does not follow a hardlink, so a path-only containment
        check passes while the read serves a file from outside the root."""
        import os

        secret = tmp_path / "secret.txt"
        secret.write_text("TOP SECRET", encoding="utf-8")
        skill = fake_home / ".kiro" / "crew" / "skills" / "demo"
        skill.mkdir(parents=True)
        os.link(secret, skill / "notes.txt")

        content, err = read_skill_file(skill, "notes.txt")
        assert err == "access denied"
        assert "TOP SECRET" not in content

    def test_a_directory_swapped_for_a_symlink_after_the_check_is_refused(
        self, fake_home, tmp_path, monkeypatch
    ):
        """Deterministic check-to-use race: the swap happens inside the
        sensitive-path guard, i.e. after containment was verified and before
        the bytes are read."""
        from kiro_crew.dashboard.handlers import _shared

        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "file.txt").write_text("TOP SECRET", encoding="utf-8")

        skill = fake_home / ".kiro" / "crew" / "skills" / "demo"
        (skill / "sub").mkdir(parents=True)
        (skill / "sub" / "file.txt").write_text("harmless", encoding="utf-8")

        real_guard = _shared.is_sensitive_path
        swapped: list[str] = []

        def _swap_then_check(path: str) -> bool:
            if not swapped:
                swapped.append(path)
                (skill / "sub" / "file.txt").unlink()
                (skill / "sub").rmdir()
                (skill / "sub").symlink_to(outside, target_is_directory=True)
            return real_guard(path)

        monkeypatch.setattr(_shared, "is_sensitive_path", _swap_then_check)

        content, err = read_skill_file(skill, "sub/file.txt")
        assert swapped, "the guard hook never ran — the race was not exercised"
        assert err == "access denied"
        assert "TOP SECRET" not in content

    def test_a_leaf_swapped_for_a_link_before_the_read_is_refused(self, fake_home, tmp_path):
        """The bound is the root that was ADMITTED, not one resolved later.

        Resolving the root inside the read would compute the bound from the
        replacement, so an fd under the link's target would satisfy it.
        """
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "SKILL.md").write_text("TOP SECRET", encoding="utf-8")

        skills_root = fake_home / ".kiro" / "crew" / "skills"
        admitted = skills_root / "demo"
        admitted.mkdir(parents=True)
        (admitted / "SKILL.md").write_text("harmless", encoding="utf-8")

        # The endpoint resolves first, then reads; the swap lands in between.
        state = MagicMock(_slots={})
        root = _resolve_skill_root("demo", state)
        assert root == admitted.resolve()
        (admitted / "SKILL.md").unlink()
        admitted.rmdir()
        admitted.symlink_to(outside, target_is_directory=True)

        content, err = read_skill_file(root, "SKILL.md")
        assert err == "access denied"
        assert "TOP SECRET" not in content

    def test_a_symlinked_file_inside_the_root_still_reads(self, fake_home):
        """No regression: an in-root link is content, not an escape."""
        skill = fake_home / ".kiro" / "crew" / "skills" / "demo"
        (skill / "docs").mkdir(parents=True)
        (skill / "docs" / "real.md").write_text("hello\n", encoding="utf-8")
        (skill / "SKILL.md").symlink_to(skill / "docs" / "real.md")

        content, err = read_skill_file(skill, "SKILL.md")
        assert err is None
        assert content == "hello\n"


class TestTheAdmittedSkillRootCannotBeRedefined:
    """A resolved root is an admission, and the name it came from is not.

    ``read_skill_file`` addresses the root by name after the resolver admitted
    it, so replacing that directory with a symlink would otherwise redefine what
    "inside the root" means: the reader re-``realpath``s its ``within_root``
    unless it is told the root is already canonical.
    """

    @staticmethod
    def _skill_with_a_sibling_target(fake_home: Path, tmp_path: Path) -> tuple[Path, Path]:
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "SKILL.md").write_text("TOP SECRET", encoding="utf-8")
        (outside / "extra.txt").write_text("TOP SECRET", encoding="utf-8")

        skill = fake_home / ".kiro" / "crew" / "skills" / "demo"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("harmless", encoding="utf-8")
        return skill, outside

    @staticmethod
    def _swap_on_first_guard_call(monkeypatch, skill: Path, outside: Path) -> list[str]:
        """Swap the skill ROOT for a link to *outside* inside the guard hook.

        ``is_sensitive_path`` runs after the root was admitted and before the
        bytes are read / the entries are returned, which makes the swap land in
        the window under test without depending on timing.
        """
        from kiro_crew.dashboard.handlers import _shared

        real_guard = _shared.is_sensitive_path
        swapped: list[str] = []

        def _swap_then_check(path: str) -> bool:
            if not swapped:
                swapped.append(path)
                for child in skill.iterdir():
                    child.unlink()
                skill.rmdir()
                skill.symlink_to(outside, target_is_directory=True)
            return real_guard(path)

        monkeypatch.setattr(_shared, "is_sensitive_path", _swap_then_check)
        return swapped

    def test_a_root_swapped_for_a_link_is_refused_by_the_read(
        self, fake_home, tmp_path, monkeypatch
    ):
        skill, outside = self._skill_with_a_sibling_target(fake_home, tmp_path)
        swapped = self._swap_on_first_guard_call(monkeypatch, skill, outside)

        content, err = read_skill_file(skill, "SKILL.md")
        assert swapped, "the guard hook never ran — the swap window was not exercised"
        assert err == "access denied"
        assert "TOP SECRET" not in content
