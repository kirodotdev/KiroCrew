"""A shipped skill the user deletes stays deleted until they restore it.

The gateway's builtin sync reinstalls every packaged skill whose directory is
missing. ``SkillsLoader.delete_skill`` records a deleted shipped skill in
``.uninstalled-builtins.json`` and the sync skips recorded names;
``restore_builtin`` clears the record and reinstalls the packaged copy.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from kiro_crew import skills as skills_mod
from kiro_crew import skills_uninstalled
from kiro_crew.dashboard.handlers import prompts
from kiro_crew.skills import _PROVENANCE_MARKER, SkillsLoader, _ensure_builtin_skills


def _make_skill(root: Path, name: str, body: str) -> Path:
    skill_dir = root / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {body}\n---\n{body}\n", encoding="utf-8"
    )
    return skill_dir


@pytest.fixture()
def builtin_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fake packaged-builtin root, also answering as the packaged-name set."""
    root = tmp_path / "packaged-builtins"
    root.mkdir()
    _make_skill(root, "shipped-a", "shipped A")
    _make_skill(root, "shipped-b", "shipped B")
    monkeypatch.setattr(skills_mod, "_BUILTIN_SKILLS_DIR", root)
    monkeypatch.setattr(skills_mod, "_project_skills_dir", lambda: None)
    monkeypatch.setattr(
        skills_mod, "_packaged_skill_names", lambda: frozenset({"shipped-a", "shipped-b"})
    )
    return root


def _loader(base: Path) -> SkillsLoader:
    """A loader over *base* after one explicit sync, as a gateway start leaves it.

    The constructor's own sync is skipped on a running event loop, which the
    test harness provides, so the sync is run here directly.
    """
    _ensure_builtin_skills(base)
    return SkillsLoader(skills_path=base, install_builtins=False)


@pytest.fixture()
def base(tmp_path: Path) -> Path:
    dest = tmp_path / "skills"
    dest.mkdir()
    return dest


def test_a_deleted_shipped_skill_is_not_reinstalled_by_the_next_sync(
    builtin_root: Path, base: Path
) -> None:
    loader = _loader(base)
    assert (base / "shipped-a" / "SKILL.md").is_file()

    assert loader.delete_skill("shipped-a")
    _ensure_builtin_skills(base)

    assert not (base / "shipped-a").exists()
    assert (base / "shipped-b" / "SKILL.md").is_file()
    assert loader.uninstalled_builtins() == ["shipped-a"]


def test_restore_reinstalls_the_packaged_copy_with_provenance(
    builtin_root: Path, base: Path
) -> None:
    loader = _loader(base)
    loader.delete_skill("shipped-a")

    assert loader.restore_builtin("shipped-a")

    assert (base / "shipped-a" / "SKILL.md").read_text(encoding="utf-8").endswith("shipped A\n")
    assert (base / "shipped-a" / _PROVENANCE_MARKER).is_file()
    assert loader.uninstalled_builtins() == []
    _ensure_builtin_skills(base)
    assert (base / "shipped-a" / "SKILL.md").is_file()


def test_restore_refuses_a_name_that_was_never_uninstalled(builtin_root: Path, base: Path) -> None:
    loader = _loader(base)
    assert not loader.restore_builtin("shipped-a")
    assert not loader.restore_builtin("not-shipped")


def test_deleting_a_user_skill_leaves_no_record(builtin_root: Path, base: Path) -> None:
    loader = _loader(base)
    _make_skill(base, "mine", "my own")

    assert loader.delete_skill("mine")

    assert skills_uninstalled.read_uninstalled(base) == frozenset()
    assert not (base / skills_uninstalled.UNINSTALLED_RECORD).exists()


def test_a_failed_delete_records_nothing(builtin_root: Path, base: Path) -> None:
    loader = _loader(base)
    assert not loader.delete_skill("shipped-missing")
    assert skills_uninstalled.read_uninstalled(base) == frozenset()


def test_the_record_is_not_discovered_as_a_skill(builtin_root: Path, base: Path) -> None:
    loader = _loader(base)
    loader.delete_skill("shipped-a")
    names = {s["key"] if "key" in s else s["name"] for s in loader.list_skills()}
    assert "shipped-a" not in names
    assert not any(n.startswith(".uninstalled") for n in names)


@pytest.mark.parametrize("content", ["not json", '{"a": 1}', '[1, "", null]'])
def test_a_damaged_record_fails_open_to_the_shipped_default(
    builtin_root: Path, base: Path, content: str
) -> None:
    (base / skills_uninstalled.UNINSTALLED_RECORD).write_text(content, encoding="utf-8")
    _ensure_builtin_skills(base)
    assert (base / "shipped-a" / "SKILL.md").is_file()
    assert (base / "shipped-b" / "SKILL.md").is_file()


def test_a_non_utf8_record_fails_open_instead_of_raising(builtin_root: Path, base: Path) -> None:
    (base / skills_uninstalled.UNINSTALLED_RECORD).write_bytes('["shipped-a"]'.encode("utf-16"))
    assert skills_uninstalled.read_uninstalled(base) == frozenset()
    _ensure_builtin_skills(base)
    assert (base / "shipped-a" / "SKILL.md").is_file()


def test_uninstalled_list_hides_names_the_package_no_longer_ships(
    builtin_root: Path, base: Path
) -> None:
    loader = _loader(base)
    skills_uninstalled.mark_uninstalled(base, "retired-long-ago")
    loader.delete_skill("shipped-b")
    assert loader.uninstalled_builtins() == ["shipped-b"]


def test_deploy_skill_install_honors_the_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew import deploy

    src = tmp_path / "deploy-src"
    _make_skill(src, "deploy-x", "deploy X")
    _make_skill(src, "deploy-y", "deploy Y")
    home = tmp_path / "home"
    (home / "skills").mkdir(parents=True)
    monkeypatch.setattr(deploy, "_SKILLS_DIR", src)
    monkeypatch.setattr(deploy, "config_dir", lambda: home)
    skills_uninstalled.mark_uninstalled(home / "skills", "deploy-x")

    deploy._register_core_skills()

    assert not (home / "skills" / "deploy-x").exists()
    assert (home / "skills" / "deploy-y" / "SKILL.md").is_file()


def test_deploy_skill_install_limited_to_one_name_leaves_the_others_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew import deploy

    src = tmp_path / "deploy-src"
    _make_skill(src, "deploy-x", "deploy X")
    _make_skill(src, "deploy-y", "deploy Y")
    home = tmp_path / "home"
    (home / "skills").mkdir(parents=True)
    monkeypatch.setattr(deploy, "_SKILLS_DIR", src)
    monkeypatch.setattr(deploy, "config_dir", lambda: home)

    deploy._register_core_skills(only="deploy-x")

    assert (home / "skills" / "deploy-x" / "SKILL.md").is_file()
    assert not (home / "skills" / "deploy-y").exists()


def test_a_failed_restore_keeps_the_skill_listed_as_uninstalled(
    builtin_root: Path, base: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loader = _loader(base)
    loader.delete_skill("shipped-a")

    def _boom(_dir: Path) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(skills_mod, "_ensure_builtin_skills", _boom)
    with pytest.raises(OSError):
        loader.restore_builtin("shipped-a")

    assert loader.uninstalled_builtins() == ["shipped-a"]


class _Request:
    """Owner-identity request stand-in with a JSON body (see test_skill_write_guard)."""

    def __init__(self, state, body=None):
        self._identity = {"user": "owner"}
        self.app = {"state": state}
        self.method = "POST"
        self.match_info = {}
        self.json = AsyncMock(return_value=body if body is not None else {})

    def get(self, key, default=None):
        return self._identity.get(key, default)

    def __contains__(self, key):
        return key in self._identity

    def __getitem__(self, key):
        return self._identity[key]


@pytest.fixture()
def owner(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(prompts, "is_owner_dashboard_request", lambda _r: True, raising=False)
    monkeypatch.setattr(prompts, "_sel", lambda: Mock())


def _state(loader: SkillsLoader) -> SimpleNamespace:
    return SimpleNamespace(context_builder=SimpleNamespace(skills=loader))


@pytest.mark.asyncio
async def test_the_endpoints_list_and_restore_an_uninstalled_shipped_skill(
    builtin_root: Path, base: Path, owner: None
) -> None:
    loader = _loader(base)
    loader.delete_skill("shipped-a")
    state = _state(loader)

    listed = await prompts.api_skills_uninstalled(_Request(state))
    assert json.loads(listed.body) == {"uninstalled": ["shipped-a"]}

    restored = await prompts.api_skill_restore_builtin(_Request(state, {"name": "shipped-a"}))
    assert restored.status == 200
    assert (base / "shipped-a" / "SKILL.md").is_file()

    again = await prompts.api_skill_restore_builtin(_Request(state, {"name": "shipped-a"}))
    assert again.status == 404
    assert json.loads(again.body)["code"] == "not_uninstalled"


@pytest.mark.asyncio
async def test_restore_requires_a_name(builtin_root: Path, base: Path, owner: None) -> None:
    response = await prompts.api_skill_restore_builtin(_Request(_state(_loader(base)), {}))
    assert response.status == 400
    assert json.loads(response.body)["code"] == "name_required"
