"""Budgeted skill directories, complete scoped discovery and explicit activation."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.skills import (
    _FAMILY_LINE_MAX_LABELS,
    SkillContextCapacityError,
    SkillsLoader,
    _family_line,
)

pytestmark = pytest.mark.xdist_group("skill_mapping_and_lazy_default")

_BODY_MARKER = "FULL INSTRUCTIONS MARKER"


def _skill(root: Path, key: str) -> str:
    path = root / key / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nname: {key}\ndescription: desc for {key}\n---\n# H\n{_BODY_MARKER} {key}\n",
        encoding="utf-8",
    )
    return str(path)


def _loader(tmp_path: Path) -> SkillsLoader:
    return SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)


class TestLazyLoadDefault:
    def test_bounded_index_is_the_default(self):
        assert KiroCrewConfig().skills.lazy_load is True

    def test_default_entry_is_the_ranked_index_with_paths(self, tmp_path):
        skills_dir = tmp_path / "skills"
        for n in range(3):
            _skill(skills_dir, f"web-{n}")
        loader = _loader(tmp_path)
        text = loader.get_context(budget=4000)
        assert "## Available Skills" in text
        assert "## Skill discovery" not in text
        assert str(skills_dir / "web-0" / "SKILL.md") in text

    def test_short_entry_still_reachable_when_the_flag_is_false(self, tmp_path):
        """`discovery_only` is what the false setting selects; it must still work."""
        skills_dir = tmp_path / "skills"
        for n in range(3):
            _skill(skills_dir, f"web-{n}")
        loader = _loader(tmp_path)
        text = loader.get_context(budget=4000, discovery_only=True)
        assert "## Skill discovery" in text
        assert "## Available Skills" not in text


class TestFamiliesInTheIndex:
    def test_truncated_index_names_the_families_it_drops(self, tmp_path):
        """A count says how much is missing; a family says what it is about.

        Every label's number is that family's HIDDEN members, and a family with
        fewer than two hidden is left out: its siblings are already rows in the
        index, so the model has the word for it.
        """
        skills_dir = tmp_path / "skills"
        for n in range(6):
            _skill(skills_dir, f"alpha-{n}")
        for n in range(6):
            _skill(skills_dir, f"beta-{n}")
        loader = _loader(tmp_path)
        text = loader.get_context(budget=1500)

        assert "more skill(s) not shown here" in text
        families = [line for line in text.splitlines() if "Families not shown:" in line]
        assert len(families) == 1
        shown = {
            line[4:].split(":", 1)[0].strip("*")
            for line in text.splitlines()
            if line.startswith("- **")
        }
        for label, total in (("alpha-", 6), ("beta-", 6)):
            hidden = total - sum(1 for name in shown if name.startswith(label))
            if hidden > 1:
                assert f"{label}* ({hidden})" in families[0]
            else:
                assert f"{label}*" not in families[0]

    def test_complete_index_names_no_families(self, tmp_path):
        """Nothing is hidden, so a family line would only repeat the rows."""
        skills_dir = tmp_path / "skills"
        for n in range(3):
            _skill(skills_dir, f"web-{n}")
        loader = _loader(tmp_path)
        text = loader.get_context(budget=4000)
        assert "Families not shown" not in text
        assert "more skill(s) not shown here" not in text

    def test_family_line_is_bounded_by_label_count(self):
        """The line cannot grow with the catalog and crowd out a named skill."""
        skills = [{"key": f"fam{n}-a"} for n in range(20)] + [
            {"key": f"fam{n}-b"} for n in range(20)
        ]
        line = _family_line(skills)
        assert line.count("(2)") == _FAMILY_LINE_MAX_LABELS
        assert line.endswith(f"+{20 - _FAMILY_LINE_MAX_LABELS} more")

    def test_family_line_is_empty_without_a_shared_prefix(self):
        assert _family_line([{"key": "alone"}, {"key": "solo"}]) == ""


class TestANonBooleanConfigValueCannotFlipAKnobOn:
    """A malformed config value must fall back to the DECLARED default.

    ``bool()`` makes every non-empty string true, so ``"false"`` read as ON and
    silently enabled the auto-create and auto-refine knobs whose default is OFF:
    the agent would write skills nobody asked for. ``_safe_bool`` is the repo's
    contract for this -- only a real JSON boolean is honoured, anything else is
    the default -- so the string cannot decide either way.

    Note the limit this does NOT cross: for a knob whose default is ON, such as
    ``lazy_load``, the string ``"false"`` is still ignored rather than obeyed.
    Teaching config to parse boolean STRINGS is a change to every setting's
    contract, not a fix to these five fields, so it is deliberately not done here.
    """

    def test_a_string_never_enables_an_off_by_default_knob(self):
        from kiro_crew.config.loader import _build_skills_config

        for field in ("auto_create_from_sessions", "auto_refine_on_deviation"):
            cfg = _build_skills_config({field: "false"})
            assert (
                getattr(cfg, field) is False
            ), f"{field} was switched ON by the string 'false'; bool() truthiness is back"
            # Any other text is equally not a vote.
            assert getattr(_build_skills_config({field: "yes please"}), field) is False

    def test_a_real_boolean_is_still_honoured(self):
        from kiro_crew.config.loader import _build_skills_config

        for field in (
            "lazy_load",
            "auto_create_from_sessions",
            "approval_required",
            "generate_scripts",
        ):
            assert getattr(_build_skills_config({field: False}), field) is False
            assert getattr(_build_skills_config({field: True}), field) is True

        # auto_refine_on_deviation is gated on auto_create_from_sessions and is
        # switched back off on its own when asked for alone, so it is honoured
        # WITH its prerequisite rather than in isolation.
        paired = {"auto_refine_on_deviation": True, "auto_create_from_sessions": True}
        assert _build_skills_config(paired).auto_refine_on_deviation is True
        assert (
            _build_skills_config({"auto_refine_on_deviation": False}).auto_refine_on_deviation
            is False
        )


class TestEverySkillsDefaultComesFromTheDataclass:
    """A default declared twice can drift, and the drift is silent.

    `config.json` omits any key it predates, so a literal in the loader answers for
    exactly those installs. Pinning the whole section, not one field, is the point:
    the first fix of this shape covered `lazy_load` alone.
    """

    def test_an_empty_config_section_reproduces_the_dataclass(self):
        from kiro_crew.config.loader import _build_skills_config

        built = _build_skills_config({})
        assert built == KiroCrewConfig().skills

    def test_an_explicit_value_still_wins(self):
        from kiro_crew.config.loader import _build_skills_config

        built = _build_skills_config({"lazy_load": False, "max_auto_skills": 7})
        assert built.lazy_load is False
        assert built.max_auto_skills == 7


class TestTheFamiliesLineNamesTheRowsActuallyOmitted:
    """The admission loop skips a row that does not fit and keeps going.

    So the admitted rows are NOT a prefix of the candidates, and a tail slice by count
    would name skills the reader can already see while hiding ones it cannot.
    """

    def test_an_oversized_early_row_does_not_shift_the_families_line(self, tmp_path):
        skills_dir = tmp_path / "skills"
        # A row carries the skill's PATH, so a deeply nested key makes a row too
        # long to admit while shorter later rows still fit. Nested rather than one
        # long segment: a single name over 255 bytes is not a legal filename.
        # TWO of them, so their family has more than one hidden member and appears
        # on the line: a tail slice by row count would drop it and inflate zzz-*.
        deep = "/".join("deep" for _ in range(90))
        for n in range(2):
            huge = skills_dir / "aaa" / deep / f"long-{n}" / "SKILL.md"
            huge.parent.mkdir(parents=True, exist_ok=True)
            huge.write_text(
                f"---\nname: aaa-long-{n}\ndescription: a long-pathed skill\n---\nbody\n",
                encoding="utf-8",
            )
        for n in range(12):
            _skill(skills_dir, f"zzz-{n:02d}")
        loader = _loader(tmp_path)
        text = loader.get_context(budget=900)

        named = {
            line[4:].split(":", 1)[0].strip("*")
            for line in text.splitlines()
            if line.startswith("- **")
        }
        assert named, "some rows must be admitted"
        assert not [n for n in named if n.startswith("aaa-long")], "long rows cannot fit"
        # The count must include the skipped rows, which a tail slice by row count
        # would have replaced with rows the reader can already see.
        omission = [line for line in text.splitlines() if "more skill(s) not shown here" in line]
        assert omission
        assert int(omission[0].split("and ", 1)[1].split(" more", 1)[0]) == 14 - len(named)
        families = [line for line in text.splitlines() if "Families not shown:" in line]
        assert families, "with two families hidden the line must render"
        # Both long rows live under one namespace, so the line names it with the
        # count a tail slice by row number would have dropped.
        assert "aaa/ (2)" in families[0]
        hidden_zzz = 12 - len([n for n in named if n.startswith("zzz-")])
        if hidden_zzz > 1:
            assert f"zzz-* ({hidden_zzz})" in families[0]


class TestScopedDiscovery:
    def test_ancestor_mapping_prunes_untrusted_project_tree_before_descent(
        self, tmp_path, monkeypatch
    ):
        import os
        from contextlib import contextmanager

        project = tmp_path / "project"
        project_skills = project / ".kiro" / "skills"
        _skill(project_skills, "hidden")
        _skill(project / ".kiro" / "external", "allowed")
        loader = _loader(tmp_path)
        monkeypatch.setattr(loader, "_trusted_project_key", lambda project: None)
        scanned = []
        original = os.scandir

        class GuardedEntry:
            def __init__(self, entry):
                self.name = entry.name
                self.path = entry.path
                self.entry = entry

            def is_dir(self, **kwargs):
                assert os.path.abspath(self.path) != os.path.abspath(
                    project_skills
                ), "The excluded project root must not be followed, even to classify a junction"
                return self.entry.is_dir(**kwargs)

        @contextmanager
        def guarded_scandir(path):
            with original(path) as entries:
                yield iter(GuardedEntry(entry) for entry in entries)

        def scandir(path):
            scanned.append(os.path.abspath(path))
            if os.path.abspath(path) == os.path.abspath(project / ".kiro"):
                return guarded_scandir(path)
            return original(path)

        only = [str(project / ".kiro" / "**" / "SKILL.md")]
        with monkeypatch.context() as guarded:
            guarded.setattr(os, "scandir", scandir)
            rows = loader.scoped_skills(project_dir=project, only=only)
            assert len(rows) == 1 and rows[0]["key"].endswith("/allowed")
            assert os.path.abspath(project_skills) not in scanned
            assert _BODY_MARKER in loader.read_scoped_skill(
                rows[0]["key"], project_dir=project, only=only
            )

    def test_ancestor_mapping_cannot_readmit_filtered_global_rows(self, tmp_path, monkeypatch):
        _skill(tmp_path / "skills", "hidden")
        _skill(tmp_path / "external", "allowed")
        loader = _loader(tmp_path)
        monkeypatch.setattr(loader, "_get_disabled_app_names", lambda: frozenset({"disabled"}))
        monkeypatch.setattr(loader, "_owning_app", lambda *args: "disabled")
        rows = loader.scoped_skills(only=[str(tmp_path / "**" / "SKILL.md")])
        assert len(rows) == 1 and rows[0]["key"].endswith("/allowed")

    @pytest.mark.asyncio
    async def test_gateway_list_and_exact_read_share_the_session_mapping(
        self, tmp_path, monkeypatch
    ):
        import json
        from types import SimpleNamespace
        from urllib.parse import urlencode

        from aiohttp import web
        from aiohttp.test_utils import make_mocked_request

        from kiro_crew.dashboard.handlers import prompts

        root = tmp_path / "skills"
        paths = [_skill(root, f"team/s{n}") for n in range(3)]
        _skill(root, "outside")
        loader = _loader(tmp_path)
        state = SimpleNamespace(sessions=None)
        monkeypatch.setattr(prompts, "_read_session_key", lambda request: "dashboard:scoped")
        monkeypatch.setattr(prompts, "_deny_foreign_app_skill_slot", lambda *args: None)
        monkeypatch.setattr(prompts, "_get_skills", lambda state: loader)
        monkeypatch.setattr(prompts, "requesting_slot_project", lambda *args: None)
        monkeypatch.setattr(prompts, "_named_slot", lambda *args: SimpleNamespace(agent="custom"))
        monkeypatch.setattr(prompts, "session_skill_globs", lambda *args, **kw: paths)
        app = web.Application()
        app["state"] = state

        async def call(**query):
            request = make_mocked_request(
                "GET", "/api/skills?" + urlencode({"q": "", **query}), app=app
            )
            return json.loads((await prompts.api_skills(request)).body)

        page = await call(action="list", limit=2)
        assert [row["key"] for row in page["matches"]] == ["team/s0", "team/s1"]
        assert page["next_offset"] == 2
        tail = await call(action="list", limit=2, offset=2)
        assert [row["key"] for row in tail["matches"]] == ["team/s2"]
        assert tail["next_offset"] is None
        assert _BODY_MARKER in (await call(action="read", key="team/s2"))["matches"][0]["content"]
        assert (await call(action="read", key="outside"))["matches"] == []
        # Clearing the custom mapping must not widen any discovery surface.
        paths.clear()
        assert (await call(action="list"))["matches"] == []
        assert (await call(action="search", q="outside"))["matches"] == []
        assert (await call(action="read", key="outside"))["matches"] == []

    def test_mapping_bounds_every_surface_and_keeps_the_tail_loadable(self, tmp_path):
        root = tmp_path / "skills"
        for n in range(80):
            _skill(root, f"team/skill-{n:03}")
        _skill(root, "other/hidden")
        only = [str(root / "team" / "*" / "SKILL.md")]
        loader = _loader(tmp_path)
        startup = loader.get_context(budget=1500, only=only)
        assert len(startup) <= 1500
        assert _BODY_MARKER not in startup
        assert "action='list'" in startup and "action='read'" in startup
        keys = []
        for offset in range(0, 80, 7):
            keys += [
                s["key"]
                for s in loader.search_skills("", only=only, browse=True, offset=offset, limit=7)
            ]
        assert keys == [f"team/skill-{n:03}" for n in range(80)]
        assert _BODY_MARKER in loader.read_scoped_skill(keys[-1], only=only)
        assert loader.read_scoped_skill("other/hidden", only=only) is None
        assert loader.resolve_dollar_skills("$other/hidden", only=only) == []

    def test_namespace_is_exact_and_ambiguous_leaf_does_not_pick_a_winner(self, tmp_path):
        for key in ("team-a/review", "team-b/review"):
            _skill(tmp_path / "skills", key)
        loader = _loader(tmp_path)
        assert loader.resolve_dollar_skills("$team-b/review")[0][1] == "team-b/review"
        assert loader.resolve_dollar_skills("$review") == []
        assert loader.resolve_dollar_skills("$missing/review") == []
        only = [str(tmp_path / "skills" / "team-b" / "review" / "SKILL.md")]
        assert loader.resolve_dollar_skills("$review", only=only)[0][1] == "team-b/review"

    def test_external_literal_mappings_do_not_rescan_siblings(self, tmp_path, monkeypatch):
        import kiro_crew.skills as module

        paths = [_skill(tmp_path / "external", f"skill-{n:03}") for n in range(40)]
        original = module._iter_skill_files
        visited = []

        def counting_walk(*args, **kwargs):
            for entry in original(*args, **kwargs):
                visited.append(entry[1])
                yield entry

        monkeypatch.setattr(module, "_iter_skill_files", counting_walk)
        loader = _loader(tmp_path)
        rows = loader.scoped_skills(only=paths)
        assert len(rows) == len(paths)
        assert len(visited) == len(paths)
        assert {str(path) for path in visited} == set(paths)
        assert _BODY_MARKER in loader.read_scoped_skill(rows[-1]["key"], only=paths)

    def test_external_mapping_uses_a_stable_loadable_full_key(self, tmp_path):
        mapped = _skill(tmp_path / "external", "team/review")
        loader = _loader(tmp_path)
        rows = loader.search_skills("", only=[mapped], browse=True)
        assert len(rows) == 1
        key = rows[0]["key"]
        assert key.startswith("mapped/")
        assert _BODY_MARKER in loader.read_scoped_skill(key, only=[mapped])
        assert _loader(tmp_path).search_skills("", only=[mapped], browse=True)[0]["key"] == key
        assert loader.read_scoped_skill(key, only=[]) is None

    def test_pinned_capacity_refuses_instead_of_truncating_required_instructions(
        self, tmp_path, monkeypatch
    ):
        import kiro_crew.skills as module

        monkeypatch.setattr(module, "PINNED_SKILL_BODIES_CAP", 2000)
        for n in range(3):
            path = Path(_skill(tmp_path / "skills", f"required-{n}"))
            path.write_text(
                "---\nname: required\nalways: true\n---\n" + str(n) * 900, encoding="utf-8"
            )
        with pytest.raises(SkillContextCapacityError, match="startup instruction capacity"):
            _loader(tmp_path).get_context(budget=1000)

    def test_deleted_mapping_never_inherits_global_catalog(self, tmp_path):
        _skill(tmp_path / "skills", "other")
        loader = _loader(tmp_path)
        only = [str(tmp_path / "skills" / "gone" / "SKILL.md")]
        assert loader.get_context(budget=4000, only=only) == ""
        assert loader.search_skills("", only=only, browse=True) == []


@pytest.mark.skipif(sys.platform != "linux", reason="Linux descriptor-path swap guarantee")
@pytest.mark.parametrize("indexed", [False, True])
@pytest.mark.parametrize("swap_root", [False, True])
def test_external_mapping_retains_admitted_root_after_swap(
    tmp_path, monkeypatch, indexed, swap_root
):
    """Swap after enumeration; metadata, index/fallback, exact and $ stay confined."""
    root = tmp_path / "external"
    path = Path(_skill(root, "team"))
    outside = tmp_path / "outside"
    escaped = Path(_skill(outside, "team"))
    escaped.write_text(
        "---\nname: stolen\ndescription: zirconium\n---\nzirconium secret", encoding="utf-8"
    )
    loader = _loader(tmp_path)
    try:
        only = [str(root / "**" / "SKILL.md")]
        entries = loader._scoped_entries(None, only)
        assert len(entries) == 1
        key = entries[0][0]
        # Freeze only the enumeration boundary. All subsequent reads are real.
        monkeypatch.setattr(loader, "_scoped_entries", lambda *args: entries)
        victim = root if swap_root else path.parent
        target = outside if swap_root else escaped.parent
        victim.rename(tmp_path / "admitted-original")
        victim.symlink_to(target, target_is_directory=True)
        if not indexed:
            loader._search_index.close()
            loader._search_index = None
        rows = loader.scoped_skills(only=only)
        assert all("zirconium" not in row["description"] for row in rows)
        assert loader.search_skills("zirconium", only=only) == []
        assert loader.read_scoped_skill(key, only=only) is None
        assert loader.resolve_dollar_skills("$" + key, only=only) == []
    finally:
        loader.close()


@pytest.mark.asyncio
async def test_installed_post_exact_read_long_keys_and_scope_errors(tmp_path, monkeypatch):
    """Exercise real HTTP JSON parsing beyond the default request-line ceiling."""
    from types import SimpleNamespace

    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from kiro_crew.agent_discovery import session_skill_globs
    from kiro_crew.dashboard.handlers import prompts
    from kiro_crew.dashboard.routes import skills as routes
    from kiro_crew.dashboard.server import _MIXED_INTERNAL_API_PATHS, _make_csrf_middleware
    from kiro_crew.dashboard.token_auth import token_auth_middleware
    from kiro_crew.validation import MAX_SKILL_KEY_CHARS

    loader = _loader(tmp_path)
    seen = []
    scope = ["admitted/SKILL.md"]
    monkeypatch.setattr(prompts, "_get_skills", lambda state: loader)
    monkeypatch.setattr(prompts, "requesting_slot_project", lambda *args: None)
    monkeypatch.setattr(prompts, "_named_slot", lambda *args: SimpleNamespace(agent="custom"))
    monkeypatch.setattr(prompts, "session_skill_globs", lambda *args, **kw: scope)
    monkeypatch.setattr(prompts, "_deny_foreign_app_skill_slot", lambda *args: None)

    def read(key, *, only, project_dir):
        seen.append((key, only, project_dir))
        return "reference body"

    monkeypatch.setattr(loader, "read_scoped_skill", read)
    app = web.Application(
        middlewares=[
            _make_csrf_middleware("mcp_tool"),
            token_auth_middleware(
                mixed_internal_paths=_MIXED_INTERNAL_API_PATHS,
                internal_secret="synthetic-skill-secret",
            ),
        ]
    )
    app["state"] = SimpleNamespace(sessions=None)
    app["allowed_origins"] = set()
    routes.register(app)
    try:
        async with TestClient(
            TestServer(app),
            headers={
                "X-Internal-Secret": "synthetic-skill-secret",
                "X-Session-Key": "dashboard:scoped",
            },
        ) as client:
            key = "\U0001f600" * MAX_SKILL_KEY_CHARS
            payload = {"scope": "installed", "action": "read", "key": key}
            response = await client.post("/api/skills/-/discover", json=payload)
            assert response.status == 200
            assert (await response.json())["matches"][0]["key"] == key
            assert seen == [(key, scope, None)]
            for bad in (
                {**payload, "key": key + "x"},
                {**payload, "key": []},
                {**payload, "action": "list"},
                {**payload, "scope": "registry"},
            ):
                assert (await client.post("/api/skills/-/discover", json=bad)).status == 400
            assert len(seen) == 1
            install = await client.post("/api/skills/-/discover/install", json={})
            assert install.status == 403
            assert (await install.json())["code"] == "human_only"
            cross_site = await client.post(
                "/api/skills/-/discover",
                json=payload,
                headers={"Origin": "https://foreign.invalid"},
            )
            assert cross_site.status == 403
            response = await client.post("/api/skills/-/discover", data=b" " * (512 * 1024 + 1))
            assert response.status == 413

            # The actual strict resolver cannot resolve this absent custom agent.
            monkeypatch.setattr(prompts, "session_skill_globs", session_skill_globs)
            for response in (
                await client.post("/api/skills/-/discover", json=payload),
                await client.get("/api/skills/-/discover?q=&scope=installed&action=list"),
            ):
                assert response.status == 409
                assert (await response.json())["code"] == "skill_scope_unavailable"
            assert len(seen) == 1
            monkeypatch.setattr(
                prompts,
                "_deny_foreign_app_skill_slot",
                lambda *args: web.json_response({"code": "foreign_slot"}, status=403),
            )
            assert (await client.post("/api/skills/-/discover", json=payload)).status == 403
            assert len(seen) == 1
    finally:
        loader.close()


def test_external_provider_target_keeps_global_budget_and_admitted_root(tmp_path, monkeypatch):
    import os

    from conftest import make_dir_link
    from kiro_crew import skills as skills_module

    provider = tmp_path / "provider"
    path = Path(_skill(provider, "large"))
    body = "zirconium " * 3000
    path.write_text("---\nname: large\n---\n" + body, encoding="utf-8")
    external = tmp_path / "external"
    external.mkdir()
    make_dir_link(external / "linked", path.parent)
    monkeypatch.setattr(
        skills_module, "_trusted_skill_roots", lambda: (os.path.realpath(provider),)
    )
    only = [str(external / "*" / "SKILL.md")]
    loader = _loader(tmp_path)
    try:
        rows = loader.scoped_skills(only=only)
        assert len(rows) == 1
        key = rows[0]["key"]
        assert rows[0]["mapping_root"] == os.path.realpath(provider)
        assert rows[0]["confine_root"] is None
        assert rows[0]["metadata_indexed"] is True
        assert str(external / "linked" / "SKILL.md") in loader._search_index.metadata_snapshot()
        assert body in loader.read_scoped_skill(key, only=only)
        assert loader.search_skills("zirconium", only=only)[0]["key"] == key
        # A reparse of the root itself must not redefine the admitted boundary.
        entries = loader._scoped_entries(None, only)
        monkeypatch.setattr(loader, "_scoped_entries", lambda *args: entries)
        outside = tmp_path / "outside"
        _skill(outside, "large")
        provider.rename(tmp_path / "original-provider")
        make_dir_link(provider, outside)
        assert loader.read_scoped_skill(key, only=only) is None
    finally:
        loader.close()


@pytest.mark.parametrize("key", ["mapped/foo", "mapped/0123456789abcdef/foo"])
def test_catalog_name_does_not_change_admitted_project_provenance(tmp_path, monkeypatch, key):
    """Start at the admitted enumeration seam, preserving real descriptor reads."""
    from kiro_crew import skill_trust
    from kiro_crew.skills import PROJECT_SKILL_BODY_CAP

    project = tmp_path / "project"
    path = Path(_skill(project / ".kiro" / "skills", key))
    path.write_text(
        "---\nname: scoped\ndescription: zirconium\n---\n" + "x" * PROJECT_SKILL_BODY_CAP,
        encoding="utf-8",
    )
    loader = _loader(tmp_path)
    # Windows cannot admit projects via the pinned walk. Isolate that earlier
    # gate while exercising all downstream readers with real project bytes.
    if skill_trust.project_skill_traversal_supported():
        skill_trust.grant_project_trust(project)
    else:
        monkeypatch.setattr(loader, "_iter", lambda *args: [(key, path, str(project))])
    try:
        row = loader.list_skills(project)[0]
        assert row["confine_root"] == str(project)
        assert row["mapping_root"] is None
        assert row["metadata_indexed"] is False
        assert loader._search_index.metadata_snapshot() == {}
        assert loader.read_scoped_skill(key, project_dir=project) is None
        assert loader.search_skills("zirconium", project_dir=project) == []
        assert loader.resolve_dollar_skills("$" + key, project_dir=project) == []
    finally:
        loader.close()


def test_regular_global_mapped_namespace_keeps_normal_reader(tmp_path, monkeypatch):
    from unittest.mock import Mock

    key = "mapped/foo"
    _skill(tmp_path / "skills", key)
    loader = _loader(tmp_path)
    reader = Mock(wraps=loader.load_skill)
    monkeypatch.setattr(loader, "load_skill", reader)
    try:
        row = loader.scoped_skills()[0]
        assert row["confine_root"] is None and row["mapping_root"] is None
        assert _BODY_MARKER in loader.read_scoped_skill(key)
        reader.assert_called_once_with(key, None, max_bytes=99_000)
        loader._search_index.close()
        loader._search_index = None
        reader.reset_mock()
        assert loader.search_skills("instructions")[0]["key"] == key
        reader.assert_called_once_with(key, None, max_bytes=None)
    finally:
        loader.close()
