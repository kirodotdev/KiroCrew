"""Skills Kiro cloud syncs (the organization's and the user's) on the Skills page.

KAS writes a replica of the account's CloudConfig under
``<kiro home>/cloud-cache/resolved/config`` and loads its skills itself. Crew lists
them read-only (``kiro_crew.kiro_cloud_skills``) and never loads their bodies: the
KAS side is pinned in ``test_kas_skill_scope.py``.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from skill_browser_helpers import fake_home_fixture  # noqa: F401 -- requested by name

from kiro_crew import kiro_cloud_skills
from kiro_crew.dashboard.handlers._shared import _resolve_skill_root, list_kiro_skills
from kiro_crew.dashboard.handlers.prompts_skill_edits import READONLY_SKILL_KEY_PREFIXES


def _replica(home: Path, *, layers=("organization",), state: str | None = None) -> Path:
    root = home / ".kiro" / "cloud-cache" / "resolved" / "config"
    root.mkdir(parents=True, exist_ok=True)
    if state is None:
        listed = [{"scope": layer, "assertions": {}} for layer in layers]
        state = json.dumps({"syncedAt": 1, "files": {}, "layers": listed})
    (root / "state.json").write_text(state, encoding="utf-8")
    return root


def _skill(root: Path, layer: str, name: str, description: str = "org desc") -> Path:
    skill = root / layer / "skills" / name / "SKILL.md"
    skill.parent.mkdir(parents=True, exist_ok=True)
    skill.write_text(f"---\nname: {name}\ndescription: {description}\n---\nbody\n")
    return skill


@pytest.fixture
def home(fake_home, monkeypatch):
    monkeypatch.delenv("KIRO_HOME", raising=False)
    return fake_home


def _rows(source: str) -> dict[str, dict]:
    return {r["key"]: r for r in list_kiro_skills(None) if r["source"] == source}


def test_the_skills_page_lists_the_org_and_cloud_user_skills(home):
    root = _replica(home)
    org = _skill(root, "organization", "org-guide")
    mine = _skill(root, "user", "my-cloud-skill", "mine")

    assert _rows("kiro-org") == {
        "kiro-org/org-guide": {
            "key": "kiro-org/org-guide",
            "name": "org-guide",
            "description": "org desc",
            "path": str(org),
            "dir": str(org.parent),
            "always": False,
            "source": "kiro-org",
        }
    }
    assert set(_rows("kiro-cloud")) == {"kiro-cloud/my-cloud-skill"}
    assert _rows("kiro-cloud")["kiro-cloud/my-cloud-skill"]["path"] == str(mine)


def test_the_replica_follows_kiro_home(home, monkeypatch, tmp_path):
    elsewhere = tmp_path / "other-kiro"
    monkeypatch.setenv("KIRO_HOME", str(elsewhere))
    root = elsewhere / "cloud-cache" / "resolved" / "config"
    root.mkdir(parents=True)
    (root / "state.json").write_text(json.dumps({"layers": [{"scope": "organization"}]}))
    _skill(root, "organization", "org-guide")
    _skill(_replica(home), "organization", "not-this-one")

    assert set(_rows("kiro-org")) == {"kiro-org/org-guide"}


def test_an_org_skill_opens_read_only_on_the_skills_page(home):
    org = _skill(_replica(home), "organization", "org-guide")

    assert _resolve_skill_root("kiro-org/org-guide", MagicMock(_slots={})) == org.parent.resolve()
    assert set(kiro_cloud_skills.REPLICA_KEY_PREFIXES) <= set(READONLY_SKILL_KEY_PREFIXES)


def test_an_org_key_cannot_climb_out_of_the_replica(home):
    _skill(_replica(home), "organization", "org-guide")
    (home / ".kiro" / "skills" / "mine").mkdir(parents=True)

    state = MagicMock(_slots={})
    assert _resolve_skill_root("kiro-org/../../../skills/mine", state) is None
    assert _resolve_skill_root("kiro-org/missing", state) is None


@pytest.mark.parametrize(
    "case",
    [
        "no replica",
        "lock held",
        "state not json",
        "state nested too deep",
        "state not an object",
        "state unreadable",
    ],
)
def test_a_replica_that_is_absent_or_mid_sync_gives_no_skills_and_no_error(home, case):
    if case != "no replica":
        root = _replica(home, layers=("organization", "user"))
        _skill(root, "organization", "org-guide")
        _skill(root, "user", "my-cloud-skill")
        if case == "lock held":
            (root / ".sync.lock.lock").mkdir()
        elif case == "state not json":
            (root / "state.json").write_text('{"layers": [', encoding="utf-8")
        elif case == "state nested too deep":
            (root / "state.json").write_text("[" * 200_000, encoding="utf-8")
        elif case == "state not an object":
            (root / "state.json").write_text("[]", encoding="utf-8")
        else:
            (root / "state.json").unlink()
            (root / "state.json").mkdir()

    assert kiro_cloud_skills.replica_skill_roots() == []
    assert not _rows("kiro-org") and not _rows("kiro-cloud")
    assert _resolve_skill_root("kiro-org/org-guide", MagicMock(_slots={})) is None


def test_a_stale_sync_lock_does_not_hide_the_skills(home):
    root = _replica(home)
    _skill(root, "organization", "org-guide")
    lock = root / ".sync.lock.lock"
    lock.mkdir()
    old = time.time() - 60
    os.utime(lock, (old, old))
    (root / ".sync.lock").touch()  # KAS leaves the lock target in place between syncs

    assert set(_rows("kiro-org")) == {"kiro-org/org-guide"}


def test_the_org_layer_is_read_only_while_the_state_lists_it(home):
    # KAS 3.3.0 reads the organization layer only while state.json lists it, and
    # the user layer always.
    root = _replica(home, layers=())
    _skill(root, "organization", "org-guide")
    _skill(root, "user", "my-cloud-skill")

    assert not _rows("kiro-org")
    assert set(_rows("kiro-cloud")) == {"kiro-cloud/my-cloud-skill"}


def test_only_skill_files_are_read_from_the_replica(home):
    root = _replica(home, layers=("organization", "user"))
    _skill(root, "organization", "org-guide")
    for other in ("mcp-servers/github.json", "hooks/run.json", "agents/x.json", "steering/a.md"):
        (root / "organization" / other).parent.mkdir(parents=True, exist_ok=True)
        (root / "organization" / other).write_text("{}")
    (root / "organization" / "skills" / "notes.md").write_text("not a skill")

    assert kiro_cloud_skills.replica_skill_globs() == [
        str(root / "organization" / "skills" / "*" / "SKILL.md")
    ]
    assert set(_rows("kiro-org")) == {"kiro-org/org-guide"}


def test_crew_never_loads_an_org_skill_itself(home, tmp_path):
    """Crew's own skill catalog (trigger matching, ``$skill``, ``skill_search``)
    holds no replica skill, so an org skill the organization's policy denies by
    name is never put in a prompt by Crew: KAS loads replica skills and enforces
    that policy (RFC ``rfc-crew-lts-edition`` D8)."""
    from kiro_crew.skills import SkillsLoader

    _skill(_replica(home), "organization", "org-guide")
    loader = SkillsLoader(skills_path=tmp_path / "crew-skills", install_builtins=False)

    listed = loader.list_skills()
    assert not any("cloud-cache" in str(s.get("path", "")) for s in listed)
    assert loader.load_skill("org-guide") is None


def test_a_usable_row_for_the_same_file_wins_over_the_list_only_cloud_row(home):
    # ``skills.extra_paths`` naming the replica (the old workaround) lists each
    # file as a usable Kiro Crew row too; the pickers drop list-only rows, so the
    # merged row must stay the usable one, with the cloud key as an alias.
    from kiro_crew.dashboard.handlers._shared import _one_row_per_skill_file

    org = _skill(_replica(home), "organization", "org-guide")
    cloud_row = {
        "key": "kiro-org/org-guide",
        "name": "org-guide",
        "path": str(org),
        "source": "kiro-org",
    }
    crew_row = {
        "key": "org-guide",
        "name": "org-guide",
        "path": str(org),
        "source": "kirocrew",
        "owned": False,
    }

    [row] = _one_row_per_skill_file([cloud_row, crew_row])

    assert (row["key"], row["source"]) == ("org-guide", "kirocrew")
    assert row["aliases"] == ["kiro-org/org-guide"]


def test_a_replica_this_user_cannot_search_gives_no_skills_and_no_error(home, monkeypatch):
    _skill(_replica(home), "organization", "org-guide")

    def refused(self, *_a, **_k):
        raise PermissionError(13, "Permission denied", str(self))

    monkeypatch.setattr(Path, "is_file", refused)

    assert kiro_cloud_skills.replica_skill_roots() == []
