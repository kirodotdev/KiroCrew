"""The project/global steering dedup is provider-aware.

A folder may declare (or contain) the project's or the operator's
``.kiro/steering`` tree. On a provider that already delivers those trees --
kiro-cli natively, the Claude Code seam through the explicit steering load,
KAS through ``native_steering`` -- re-sending them through the folder would
double their tokens, so they are skipped. On a provider with NO such path
(Codex and the other harnesses) skipping them would drop the rules with
nothing arriving in their place, so the folder delivers them like any other
document. These pins hold both directions at the collector and at the builder.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew import folder_steering
from kiro_crew.acp.types import PROVIDER_LABEL_CODEX, PROVIDER_LABEL_KAS
from kiro_crew.agent_sdk.provider_identity import PROVIDER_ACP, PROVIDER_CLAUDE_CODE
from kiro_crew.context import ContextBuilder, _project_steering_delivered
from kiro_crew.folder_steering import FOLDER_STEERING_HEADER as _RAW_HEADER
from kiro_crew.folder_steering import collect_folder_steering
from kiro_crew.memory import MemoryStore
from kiro_crew.skills import SkillsLoader

_needs_pinned_walk = pytest.mark.skipif(
    not folder_steering.pinned_fs.supports_pinned_tree_walk(),
    reason="descriptor-relative directory opens are unavailable on this host",
)

# The prompt folds the em dash in the frame; compare against the folded form.
FOLDER_STEERING_HEADER = _RAW_HEADER.replace("\u2014", "--")

PROJECT_RULE = "PROJECT-TREE-RULE-9f1c"
GLOBAL_RULE = "GLOBAL-TREE-RULE-2b7e"
OWN_RULE = "FOLDER-OWN-RULE-6d3a"


def _write(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


@pytest.fixture
def trees(tmp_path, monkeypatch):
    """A project tree, an operator home tree and a folder-owned tree."""
    home = tmp_path / "home"
    project = tmp_path / "project"
    _write(home / ".kiro" / "steering" / "global.md", GLOBAL_RULE)
    _write(project / ".kiro" / "steering" / "project.md", PROJECT_RULE)
    _write(tmp_path / "standards" / "own.md", OWN_RULE)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("KIRO_HOME", str(home / ".kiro"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return {
        "home": home,
        "project": project,
        "dirs": (
            str(project / ".kiro" / "steering"),
            str(home / ".kiro" / "steering"),
            str(tmp_path / "standards"),
        ),
    }


def _opt_out(project: Path) -> None:
    """The workspace tells kiro-cli not to load its default resources."""
    settings = project / ".kiro" / "settings" / "cli.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(
        json.dumps({"chat.disableInheritingDefaultResources": True}), encoding="utf-8"
    )


@pytest.mark.parametrize(
    ("provider_type", "native", "delivered"),
    [
        (PROVIDER_ACP, False, True),
        (PROVIDER_CLAUDE_CODE, False, True),
        (PROVIDER_LABEL_KAS, True, True),
        (PROVIDER_LABEL_CODEX, False, False),
        ("opencode", False, False),
        ("acme-config-authored-harness", False, False),
    ],
)
def test_project_steering_delivered_names_every_existing_path(
    trees, provider_type, native, delivered
):
    assert _project_steering_delivered(provider_type, native, str(trees["project"])) is delivered


@pytest.mark.parametrize(
    ("provider_type", "native", "delivered"),
    [
        (PROVIDER_ACP, False, False),
        (PROVIDER_CLAUDE_CODE, False, True),
        (PROVIDER_LABEL_KAS, True, True),
        (PROVIDER_LABEL_CODEX, False, False),
    ],
)
def test_the_opt_out_removes_only_the_kiro_cli_path(trees, provider_type, native, delivered):
    """``chat.disableInheritingDefaultResources`` is kiro-cli's setting: on kiro it
    means nobody loads the trees, and on every other harness it means nothing."""
    _opt_out(trees["project"])
    assert _project_steering_delivered(provider_type, native, str(trees["project"])) is delivered


@pytest.mark.parametrize("driver_verdict", [False, True])
def test_the_kiro_cli_verdict_is_the_drivers(trees, monkeypatch, driver_verdict):
    """On kiro the verdict is the ACP driver's reading of the setting, nothing else."""
    from kiro_crew.agent_sdk.drivers import acp as acp_driver

    asked: list[object] = []

    def patched(work_dir):
        asked.append(work_dir)
        return driver_verdict

    monkeypatch.setattr(acp_driver, "inherits_default_resources", patched)
    project = str(trees["project"])
    assert _project_steering_delivered(PROVIDER_ACP, False, project) is driver_verdict
    assert asked == [project]


@_needs_pinned_walk
def test_collector_keeps_the_trees_when_told_the_provider_does_not_deliver_them(trees):
    skipped = collect_folder_steering(
        trees["dirs"], project=str(trees["project"]), home=trees["home"]
    )
    assert [Path(p).name for p, _ in skipped] == ["own.md"], "default keeps the dedup"
    kept = collect_folder_steering(
        trees["dirs"],
        project=str(trees["project"]),
        home=trees["home"],
        skip_delivered_roots=False,
    )
    assert [Path(p).name for p, _ in kept] == ["project.md", "global.md", "own.md"]


def _builder(tmp_path):
    return ContextBuilder(
        memory=MemoryStore(workspace=tmp_path / "ws"),
        skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
    )


@_needs_pinned_walk
def test_codex_receives_project_and_global_trees_through_the_folder(tmp_path, trees):
    """Codex has no native or explicit ``.kiro/steering`` load, so the folder
    section carries those documents instead of skipping them."""
    msg, _ = _builder(tmp_path).build_message(
        "hello",
        True,
        "dashboard:codex",
        provider_type=PROVIDER_LABEL_CODEX,
        project=str(trees["project"]),
        steering_dirs=trees["dirs"],
    )
    assert FOLDER_STEERING_HEADER in msg
    assert PROJECT_RULE in msg
    assert GLOBAL_RULE in msg
    assert OWN_RULE in msg


@_needs_pinned_walk
@pytest.mark.parametrize("provider_type", [PROVIDER_ACP, PROVIDER_CLAUDE_CODE])
def test_a_provider_that_delivers_the_trees_does_not_receive_them_twice(
    tmp_path, trees, provider_type
):
    msg, _ = _builder(tmp_path).build_message(
        "hello",
        True,
        f"dashboard:{provider_type}",
        provider_type=provider_type,
        project=str(trees["project"]),
        steering_dirs=trees["dirs"],
    )
    section_start = msg.index(FOLDER_STEERING_HEADER)
    section = msg[section_start:]
    assert OWN_RULE in section
    assert PROJECT_RULE not in section
    assert GLOBAL_RULE not in section


REINJECT_HEADER = "[REINJECTED AFTER COMPACTION -- folder steering]"


def _kiro_turn(tmp_path, trees, *, fresh: bool) -> str:
    """A non-member kiro-cli turn: a fresh session, or the one after a compaction."""
    msg, _ = _builder(tmp_path).build_message(
        "hello",
        fresh,
        "dashboard:kiro",
        provider_type=PROVIDER_ACP,
        project=str(trees["project"]),
        steering_dirs=trees["dirs"],
        needs_reinjection=not fresh,
    )
    return msg


@_needs_pinned_walk
def test_an_opted_out_kiro_chat_receives_the_trees_once_through_the_folder(tmp_path, trees):
    """On kiro in an opted-out workspace nobody else loads the declared trees.

    The folder section carries their always documents, once, both at session
    start and when it is re-injected after a compaction; a manual document under
    the same root keeps its native trigger.
    """
    _write(
        trees["project"] / ".kiro" / "steering" / "manual.md",
        "---\ninclusion: manual\n---\nMANUAL-ONLY-4c2d",
    )
    _opt_out(trees["project"])
    fresh = _kiro_turn(tmp_path, trees, fresh=True)
    section = fresh[fresh.index(FOLDER_STEERING_HEADER) :]
    for rule in (PROJECT_RULE, GLOBAL_RULE, OWN_RULE):
        assert section.count(rule) == 1
    assert "MANUAL-ONLY-4c2d" not in fresh

    reinjected = _kiro_turn(tmp_path, trees, fresh=False)
    block = reinjected[reinjected.index(REINJECT_HEADER) :]
    for rule in (PROJECT_RULE, GLOBAL_RULE, OWN_RULE):
        assert block.count(rule) == 1
    assert "MANUAL-ONLY-4c2d" not in reinjected


@_needs_pinned_walk
def test_an_inheriting_kiro_chat_still_skips_the_trees_after_a_compaction(tmp_path, trees):
    reinjected = _kiro_turn(tmp_path, trees, fresh=False)
    block = reinjected[reinjected.index(REINJECT_HEADER) :]
    assert OWN_RULE in block
    assert PROJECT_RULE not in block
    assert GLOBAL_RULE not in block


@_needs_pinned_walk
def test_an_opted_out_workspace_changes_nothing_for_another_harness(tmp_path, trees):
    """Codex kept delivering the trees through the folder before; the opt-out is
    a kiro-cli setting, so it keeps doing exactly that and nothing more."""
    _opt_out(trees["project"])
    msg, _ = _builder(tmp_path).build_message(
        "hello",
        True,
        "dashboard:codex",
        provider_type=PROVIDER_LABEL_CODEX,
        project=str(trees["project"]),
        steering_dirs=trees["dirs"],
    )
    section = msg[msg.index(FOLDER_STEERING_HEADER) :]
    for rule in (PROJECT_RULE, GLOBAL_RULE, OWN_RULE):
        assert section.count(rule) == 1
