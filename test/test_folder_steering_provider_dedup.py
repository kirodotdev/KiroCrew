"""The project/global steering dedup is provider-aware.

A folder may declare (or contain) the project's or the operator's
``.kiro/steering`` tree. On a provider that already delivers those trees --
kiro-cli natively, the Claude Code seam and Codex through the explicit steering
load, KAS through ``native_steering`` -- re-sending them through the folder would
double their tokens, so they are skipped. On a provider with NO such path
(OpenCode and the other harnesses) skipping them would drop the rules with
nothing arriving in their place, so the folder delivers them like any other
document. These pins hold both directions at the collector and at the builder.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew import agent_sdk, folder_steering
from kiro_crew.acp import skill_projection as projection
from kiro_crew.acp.types import (
    PROVIDER_LABEL_BY_BACKEND,
    PROVIDER_LABEL_CODEX,
    PROVIDER_LABEL_KAS,
    PROVIDER_LABEL_OPENCODE,
)
from kiro_crew.agent_sdk.drivers import acp as acp_driver
from kiro_crew.agent_sdk.provider_identity import PROVIDER_ACP, PROVIDER_CLAUDE_CODE
from kiro_crew.context import ContextBuilder, _project_steering_delivered
from kiro_crew.essential_delivery import EssentialDelivery
from kiro_crew.folder_steering import FOLDER_STEERING_HEADER as _RAW_HEADER
from kiro_crew.folder_steering import collect_folder_steering
from kiro_crew.member_essential_context import (
    projected_launch_paths,
    projected_resource_documents,
)
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
        (PROVIDER_LABEL_CODEX, False, True),
        ("opencode", False, False),
        ("acme-config-authored-harness", False, False),
        (None, False, False),
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
        (PROVIDER_LABEL_CODEX, False, True),
    ],
)
def test_the_opt_out_removes_only_the_kiro_cli_path(trees, provider_type, native, delivered):
    """``chat.disableInheritingDefaultResources`` is kiro-cli's setting: on kiro it
    means the trees are not delivered wholesale (only what the session agent
    declares still loads), and on every other harness it means nothing."""
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
def test_codex_receives_project_and_global_trees_once_beside_the_folder(tmp_path, trees):
    """Codex gets the ``.kiro/steering`` trees from Crew's explicit steering load,
    so a folder that declares them skips them and each rule arrives once."""
    _declare([SHIPPED_STEERING_RESOURCE])
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
        assert msg.count(rule) == 1, rule
    assert section.count(OWN_RULE) == 1
    assert PROJECT_RULE not in section
    assert GLOBAL_RULE not in section


@_needs_pinned_walk
@pytest.mark.parametrize(
    "harness", [PROVIDER_LABEL_CODEX, PROVIDER_CLAUDE_CODE], ids=["codex", "claude"]
)
def test_a_custom_agent_receives_the_trees_through_the_folder(tmp_path, trees, harness):
    """Crew's steering load sends nothing for a custom agent, so the folder that
    declares the trees still carries them, and each rule arrives once."""
    _declare([SHIPPED_STEERING_RESOURCE])
    msg, _ = _builder(tmp_path).build_message(
        "hello",
        True,
        f"dashboard:{harness}",
        provider_type=harness,
        project=str(trees["project"]),
        steering_dirs=trees["dirs"],
        agent="my-custom-agent",
    )
    section = msg[msg.index(FOLDER_STEERING_HEADER) :]
    for rule in (PROJECT_RULE, GLOBAL_RULE, OWN_RULE):
        assert msg.count(rule) == 1, rule
        assert section.count(rule) == 1, rule


@pytest.mark.parametrize(
    ("agent", "delivered"),
    [(None, True), ("kirocrew", True), ("my-custom-agent", False)],
)
def test_the_steering_load_counts_as_delivered_only_for_the_default_agent(agent, delivered):
    """The folder skips the trees for Crew's steering load only when that load runs."""
    for harness in (PROVIDER_LABEL_CODEX, PROVIDER_CLAUDE_CODE):
        assert _project_steering_delivered(harness, False, None, agent=agent) is delivered


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


def _kiro_turn(
    tmp_path,
    trees,
    *,
    fresh: bool,
    agent: str | None = None,
    builder: ContextBuilder | None = None,
    context_provider=None,
    steering_dirs: tuple[str, ...] | None = None,
) -> str:
    """A non-member kiro-cli turn: a fresh session, or the one after a compaction."""
    msg, _ = (builder or _builder(tmp_path)).build_message(
        "hello",
        fresh,
        "dashboard:kiro",
        provider_type=PROVIDER_ACP,
        project=str(trees["project"]),
        steering_dirs=trees["dirs"] if steering_dirs is None else steering_dirs,
        needs_reinjection=not fresh,
        agent=agent,
        context_provider=context_provider,
    )
    return msg


def _launch_provider(project: Path, documents: dict[str, str]):
    """A kiro-cli context provider whose process recorded *documents* at launch."""
    return SimpleNamespace(
        essential_delivery=EssentialDelivery(),
        context_provider_type=PROVIDER_ACP,
        cwd=str(project),
        native_context_documents=dict(documents),
        native_steering=False,
        member_dispatch_mounted=None,
    )


def _launch_record(*paths: Path) -> dict[str, str]:
    """The runtime's launch record: each declared guide by path, no body."""
    return dict.fromkeys((str(path) for path in paths), "")


def _declare(resources: list[str], name: str = "kirocrew") -> None:
    """Write the session agent spec that kiro-cli reads for ``--agent``."""
    spec = {"name": name, "resources": resources}
    _write(Path.home() / ".kiro" / "agents" / f"{name}.json", json.dumps(spec))


SHIPPED_STEERING_RESOURCE = "file://.kiro/steering/**/*.md"


def _prepare_projection(trees, monkeypatch):
    agents = trees["home"] / ".kiro" / "agents"
    monkeypatch.setattr(projection, "kiro_home", lambda: trees["home"] / ".kiro")
    monkeypatch.setattr(projection, "data_home", lambda: trees["home"] / "crew")
    monkeypatch.setattr(projection, "kiro_agents_dir", lambda: agents)
    monkeypatch.setattr(projection.platform_compat, "path_volume_is_remote", lambda path: False)
    monkeypatch.setattr(projection.platform_compat, "first_linked_ancestor", lambda path: None)
    monkeypatch.setattr(
        projection,
        "list_agents",
        lambda **kwargs: [
            SimpleNamespace(
                name="kirocrew",
                filename="kirocrew.json",
                scope="global",
                kirocrew_owned=True,
            )
        ],
    )
    monkeypatch.setattr(
        "kiro_crew.agent.managed_mcp_spec_entry",
        lambda name, **_kw: {"command": "test-core", "args": []},
    )
    monkeypatch.setattr(
        "kiro_crew.agent._KIRO_MCP_JSON", trees["home"] / ".kiro" / "settings" / "mcp.json"
    )
    return projection.prepare_native_skill_projection(trees["project"])


@_needs_pinned_walk
def test_an_opted_out_kiro_chat_receives_declared_trees_once(tmp_path, trees, monkeypatch):
    """The folder skips only the declared documents recorded at process launch,
    which kiro-cli loads itself; the record names them by path."""
    _write(
        trees["project"] / ".kiro" / "steering" / "manual.md",
        "---\ninclusion: manual\n---\nMANUAL-ONLY-4c2d",
    )
    _declare([SHIPPED_STEERING_RESOURCE])
    _opt_out(trees["project"])
    prepared = _prepare_projection(trees, monkeypatch)
    recorded = projected_launch_paths(prepared.specs["kirocrew"], str(trees["project"]))
    # The always documents kiro-cli loads from the declared resources; the
    # record names every declared markdown path, conditional ones included,
    # which the folder leaves to their native trigger anyway.
    native = projected_resource_documents(prepared.specs["kirocrew"], str(trees["project"]))
    assert set(native) <= set(recorded)
    provider = _launch_provider(trees["project"], dict.fromkeys(recorded, ""))
    monkeypatch.setattr(agent_sdk, "context_provider_of", lambda value: value)
    builder = _builder(tmp_path)

    fresh = _kiro_turn(
        tmp_path,
        trees,
        fresh=True,
        builder=builder,
        context_provider=provider,
    )
    section = fresh[fresh.index(FOLDER_STEERING_HEADER) :]
    reinjected = _kiro_turn(
        tmp_path,
        trees,
        fresh=False,
        builder=builder,
        context_provider=provider,
    )
    block = reinjected[reinjected.index(REINJECT_HEADER) :]

    for rule in (PROJECT_RULE, GLOBAL_RULE, OWN_RULE):
        native_count = sum(body.count(rule) for body in native.values())
        assert native_count + section.count(rule) == 1
        assert native_count + block.count(rule) == 1
    assert all("MANUAL-ONLY-4c2d" not in body for body in native.values())
    assert "MANUAL-ONLY-4c2d" not in fresh
    assert "MANUAL-ONLY-4c2d" not in reinjected


@_needs_pinned_walk
def test_opted_out_plain_kiro_without_folder_steering_keeps_managed_declaration(
    tmp_path, trees, monkeypatch
):
    """Kiro-cli loads the declared project tree when no folder can carry it."""
    _declare([SHIPPED_STEERING_RESOURCE])
    _opt_out(trees["project"])

    prepared = _prepare_projection(trees, monkeypatch)
    message = _kiro_turn(tmp_path, trees, fresh=True, steering_dirs=())

    assert SHIPPED_STEERING_RESOURCE in prepared.specs["kirocrew"]["resources"]
    assert FOLDER_STEERING_HEADER not in message


@_needs_pinned_walk
def test_opted_out_kiro_folder_skips_documents_recorded_at_launch(tmp_path, trees, monkeypatch):
    """A folder omits only steering captured in the running process's launch set."""
    _opt_out(trees["project"])
    project_rule_path = trees["project"] / ".kiro" / "steering" / "project.md"
    provider = _launch_provider(trees["project"], _launch_record(project_rule_path))
    monkeypatch.setattr(agent_sdk, "context_provider_of", lambda value: value)

    message = _kiro_turn(tmp_path, trees, fresh=True, context_provider=provider)
    section = message[message.index(FOLDER_STEERING_HEADER) :]

    assert PROJECT_RULE not in section
    assert section.count(GLOBAL_RULE) == 1
    assert section.count(OWN_RULE) == 1


def _bounded_launch_provider(project: Path, documents: dict[str, str], fit):
    from kiro_crew.acp.session_provider import AcpSessionProvider

    handle = SimpleNamespace(
        native_context_documents=dict(documents),
        native_context_documents_fit=fit,
    )
    runtime = SimpleNamespace(acp_backend="", process_instance="runtime")
    return AcpSessionProvider(handle, runtime)


@_needs_pinned_walk
def test_compaction_render_sends_every_guide_after_declared_files_outgrow_bound(
    tmp_path, trees, monkeypatch
):
    from kiro_crew import member_essential_context

    _opt_out(trees["project"])
    project_rule_path = trees["project"] / ".kiro" / "steering" / "project.md"
    resources = [SHIPPED_STEERING_RESOURCE, "file://large.txt"]

    def fit() -> bool:
        return member_essential_context._declared_files_fit(
            {"resources": resources}, str(trees["project"])
        )

    provider = _bounded_launch_provider(trees["project"], _launch_record(project_rule_path), fit)
    monkeypatch.setattr(agent_sdk, "context_provider_of", lambda value: value)
    builder = _builder(tmp_path)

    under = _kiro_turn(
        tmp_path,
        trees,
        fresh=False,
        builder=builder,
        context_provider=provider,
    )
    under_block = under[under.index(REINJECT_HEADER) :]
    assert PROJECT_RULE not in under_block
    assert under_block.count(GLOBAL_RULE) == 1
    assert under_block.count(OWN_RULE) == 1

    room = member_essential_context._LAUNCH_RECORD_MAX_BYTES - project_rule_path.stat().st_size
    _write(trees["project"] / "large.txt", "a" * (room + 1))
    over = _kiro_turn(
        tmp_path,
        trees,
        fresh=False,
        builder=builder,
        context_provider=provider,
    )
    over_block = over[over.index(REINJECT_HEADER) :]
    for rule in (PROJECT_RULE, GLOBAL_RULE, OWN_RULE):
        assert over_block.count(rule) == 1


@_needs_pinned_walk
def test_ordinary_turn_does_not_check_declared_file_fit(tmp_path, trees, monkeypatch):
    from unittest.mock import MagicMock

    _opt_out(trees["project"])
    project_rule_path = trees["project"] / ".kiro" / "steering" / "project.md"
    fit = MagicMock(side_effect=AssertionError("ordinary turn checked declared files"))
    provider = _bounded_launch_provider(trees["project"], _launch_record(project_rule_path), fit)
    monkeypatch.setattr(agent_sdk, "context_provider_of", lambda value: value)

    message, _ = _builder(tmp_path).build_message(
        "hello",
        False,
        "dashboard:kiro",
        provider_type=PROVIDER_ACP,
        project=str(trees["project"]),
        steering_dirs=trees["dirs"],
        context_provider=provider,
    )

    assert FOLDER_STEERING_HEADER not in message
    fit.assert_not_called()


EDITED_PROJECT_RULE = "PROJECT-TREE-RULE-EDITED-71a0"


@_needs_pinned_walk
@pytest.mark.parametrize("fresh", [True, False], ids=["fresh-render", "compaction-reinjection"])
def test_opted_out_kiro_folder_skips_a_recorded_document_by_path(
    tmp_path, trees, monkeypatch, fresh
):
    """The launch record names each declared guide by path. kiro-cli re-reads
    the guide for every request, so the folder skips it whether or not it has
    been edited since launch: the edited text reaches the model through
    kiro-cli, and the folder never sends a recorded guide in any version."""
    _opt_out(trees["project"])
    project_rule_path = trees["project"] / ".kiro" / "steering" / "project.md"
    provider = _launch_provider(trees["project"], _launch_record(project_rule_path))
    monkeypatch.setattr(agent_sdk, "context_provider_of", lambda value: value)
    builder = _builder(tmp_path)
    header = FOLDER_STEERING_HEADER if fresh else REINJECT_HEADER

    unchanged = _kiro_turn(tmp_path, trees, fresh=fresh, builder=builder, context_provider=provider)
    unchanged_section = unchanged[unchanged.index(header) :]
    assert PROJECT_RULE not in unchanged_section
    assert unchanged_section.count(GLOBAL_RULE) == 1
    assert unchanged_section.count(OWN_RULE) == 1

    _write(project_rule_path, EDITED_PROJECT_RULE)

    edited = _kiro_turn(tmp_path, trees, fresh=fresh, builder=builder, context_provider=provider)
    edited_section = edited[edited.index(header) :]
    assert EDITED_PROJECT_RULE not in edited_section
    assert PROJECT_RULE not in edited_section
    assert edited_section.count(GLOBAL_RULE) == 1
    assert edited_section.count(OWN_RULE) == 1


@_needs_pinned_walk
def test_opted_out_kiro_folder_sends_a_guide_created_after_launch(tmp_path, trees, monkeypatch):
    """A guide the record does not name is collected as usual, even under a root
    the record names another guide from."""
    _opt_out(trees["project"])
    project_rule_path = trees["project"] / ".kiro" / "steering" / "project.md"
    provider = _launch_provider(trees["project"], _launch_record(project_rule_path))
    monkeypatch.setattr(agent_sdk, "context_provider_of", lambda value: value)
    _write(trees["project"] / ".kiro" / "steering" / "later.md", "LATER-RULE-9e1b")

    message = _kiro_turn(tmp_path, trees, fresh=True, context_provider=provider)
    section = message[message.index(FOLDER_STEERING_HEADER) :]

    assert PROJECT_RULE not in section
    assert section.count("LATER-RULE-9e1b") == 1
    assert section.count(GLOBAL_RULE) == 1
    assert section.count(OWN_RULE) == 1


@_needs_pinned_walk
@pytest.mark.parametrize("harness", [PROVIDER_LABEL_OPENCODE, None], ids=["opencode", "unnamed"])
def test_a_launch_record_applies_only_to_a_kiro_cli_harness(tmp_path, trees, monkeypatch, harness):
    """The record names guides kiro-cli loads. A provider on another harness, or
    one that names no harness, has nothing loading them, so the folder skips
    none of them."""
    _opt_out(trees["project"])
    project_rule_path = trees["project"] / ".kiro" / "steering" / "project.md"
    provider = _launch_provider(trees["project"], _launch_record(project_rule_path))
    provider.context_provider_type = harness
    monkeypatch.setattr(agent_sdk, "context_provider_of", lambda value: value)

    message, _ = _builder(tmp_path).build_message(
        "hello",
        True,
        "dashboard:other",
        project=str(trees["project"]),
        steering_dirs=trees["dirs"],
        context_provider=provider,
    )
    section = message[message.index(FOLDER_STEERING_HEADER) :]

    for rule in (PROJECT_RULE, GLOBAL_RULE, OWN_RULE):
        assert section.count(rule) == 1


@_needs_pinned_walk
def test_opted_out_kiro_folder_without_launch_set_sends_every_document(
    tmp_path, trees, monkeypatch
):
    """An empty launch set prefers duplicate delivery over losing a guide."""
    _opt_out(trees["project"])
    provider = _launch_provider(trees["project"], {})
    monkeypatch.setattr(agent_sdk, "context_provider_of", lambda value: value)

    message = _kiro_turn(tmp_path, trees, fresh=True, context_provider=provider)
    section = message[message.index(FOLDER_STEERING_HEADER) :]

    for rule in (PROJECT_RULE, GLOBAL_RULE, OWN_RULE):
        assert section.count(rule) == 1


@_needs_pinned_walk
def test_an_inheriting_kiro_chat_still_skips_the_trees_after_a_compaction(tmp_path, trees):
    reinjected = _kiro_turn(tmp_path, trees, fresh=False)
    block = reinjected[reinjected.index(REINJECT_HEADER) :]
    assert OWN_RULE in block
    assert PROJECT_RULE not in block
    assert GLOBAL_RULE not in block


@_needs_pinned_walk
def test_a_plain_chat_naming_no_harness_receives_the_trees_through_the_folder(tmp_path, trees):
    """A harness that is not named is not assumed to be kiro-cli: nothing is
    skipped as delivered, so an inheriting workspace's project and global trees
    reach the model through the folder along with the folder's own guide."""
    msg, _ = _builder(tmp_path).build_message(
        "hello",
        True,
        "dashboard:kiro",
        project=str(trees["project"]),
        steering_dirs=trees["dirs"],
    )
    section = msg[msg.index(FOLDER_STEERING_HEADER) :]
    for rule in (PROJECT_RULE, GLOBAL_RULE, OWN_RULE):
        assert section.count(rule) == 1


@_needs_pinned_walk
@pytest.mark.parametrize("harness", [PROVIDER_LABEL_CODEX, PROVIDER_LABEL_OPENCODE])
def test_an_opted_out_workspace_changes_nothing_for_another_harness(tmp_path, trees, harness):
    """The opt-out is a kiro-cli setting. Codex keeps getting the trees from
    Crew's steering load and OpenCode through the folder, each rule once."""
    _declare([SHIPPED_STEERING_RESOURCE])
    _opt_out(trees["project"])
    msg, _ = _builder(tmp_path).build_message(
        "hello",
        True,
        f"dashboard:{harness}",
        provider_type=harness,
        project=str(trees["project"]),
        steering_dirs=trees["dirs"],
    )
    for rule in (PROJECT_RULE, GLOBAL_RULE, OWN_RULE):
        assert msg.count(rule) == 1, rule


AGENTS_GUIDE = "PROJECT-AGENTS-GUIDE-5e8b"


def _claude_turn(tmp_path, trees, *, fresh: bool, steering_dirs: tuple[str, ...]) -> str:
    """A non-member Claude Code turn: a fresh session, or the one after a compaction."""
    msg, _ = _builder(tmp_path).build_message(
        "hello",
        fresh,
        "dashboard:claude",
        provider_type=PROVIDER_CLAUDE_CODE,
        project=str(trees["project"]),
        steering_dirs=steering_dirs,
        needs_reinjection=not fresh,
    )
    return msg


@_needs_pinned_walk
@pytest.mark.parametrize("fresh", [True, False], ids=["fresh", "after-compaction"])
@pytest.mark.parametrize("declared_in_folder", [True, False], ids=["folder", "no-folder"])
def test_claude_receives_project_steering_and_agents_md_once(
    tmp_path, trees, fresh, declared_in_folder
):
    """What kiro-cli loads for a project chat reaches Claude Code too, exactly once.

    The shipped relative glob anchors at the project, as kiro-cli anchors it at
    its working directory, and the global steering and ``AGENTS.md`` kiro-cli
    inherits come with it. A folder declaring the trees still skips them as
    delivered, so nothing arrives twice and nothing is lost.
    """
    _declare([SHIPPED_STEERING_RESOURCE])
    _write(trees["project"] / "AGENTS.md", AGENTS_GUIDE)
    msg = _claude_turn(
        tmp_path, trees, fresh=fresh, steering_dirs=trees["dirs"] if declared_in_folder else ()
    )
    for rule in (PROJECT_RULE, GLOBAL_RULE, AGENTS_GUIDE):
        assert msg.count(rule) == 1, rule
    assert msg.count(OWN_RULE) == (1 if declared_in_folder else 0)


@pytest.mark.parametrize("fresh", [True, False], ids=["fresh", "after-compaction"])
@pytest.mark.parametrize(
    "resources",
    [[SHIPPED_STEERING_RESOURCE], [SHIPPED_STEERING_RESOURCE, "file://AGENTS.md"]],
    ids=["shipped-spec", "spec-lists-agents-md"],
)
def test_codex_receives_project_and_global_steering_once_and_not_agents_md(
    tmp_path, trees, fresh, resources
):
    """A Codex chat with no folder gets what kiro-cli loads for the project, once.

    Codex is handed no spec, so Crew's steering load sends the project and the
    global steering, at session start and again after a compaction. Codex reads
    the project's ``AGENTS.md`` itself from its working directory, so the load
    leaves that file out, also when the spec lists it.
    """
    _declare(resources)
    _write(trees["project"] / "AGENTS.md", AGENTS_GUIDE)
    msg, _ = _builder(tmp_path).build_message(
        "hello",
        fresh,
        "dashboard:codex",
        provider_type=PROVIDER_LABEL_CODEX,
        project=str(trees["project"]),
        steering_dirs=(),
        needs_reinjection=not fresh,
    )
    for rule in (PROJECT_RULE, GLOBAL_RULE):
        assert msg.count(rule) == 1, rule
    assert AGENTS_GUIDE not in msg


def test_crew_loads_spec_steering_only_for_claude_and_codex():
    """No other harness gets the steering load from this change."""
    labels = set(PROVIDER_LABEL_BY_BACKEND.values())
    assert {label for label in labels if acp_driver.crew_loads_spec_steering(label)} == {
        PROVIDER_CLAUDE_CODE,
        PROVIDER_LABEL_CODEX,
    }
    assert {label for label in labels if acp_driver.reads_project_agents_md(label)} == {
        PROVIDER_LABEL_CODEX
    }


def test_one_unglobbable_entry_does_not_empty_the_others(tmp_path, trees):
    """An absolute and a ``~/`` entry anchor at their own roots, and neither
    takes the global rule down with it."""
    from kiro_crew.context import _load_steering_resources

    _write(tmp_path / "abs-rules" / "abs.md", "ABSOLUTE-RULE-3a9d")
    _write(trees["home"] / "notes" / "note.md", "HOME-NOTE-RULE-8c0f")
    _declare(
        [
            f"file://{(tmp_path / 'abs-rules').as_posix()}/*.md",
            "file://~/notes/*.md",
            SHIPPED_STEERING_RESOURCE,
        ]
    )
    loaded = _load_steering_resources()
    for rule in (GLOBAL_RULE, "ABSOLUTE-RULE-3a9d", "HOME-NOTE-RULE-8c0f"):
        assert loaded.count(rule) == 1, rule


@pytest.mark.skipif(not hasattr(Path, "symlink_to"), reason="no symlinks")
def test_a_project_steering_link_out_of_the_project_is_refused(tmp_path, trees):
    """A project document is admitted against the project, not the home."""
    from kiro_crew.context import _load_steering_resources

    outside = _write(tmp_path / "home" / "elsewhere" / "leak.md", "OUTSIDE-RULE-1d4e")
    link = trees["project"] / ".kiro" / "steering" / "leak.md"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable on this host")
    _declare([SHIPPED_STEERING_RESOURCE])
    loaded = _load_steering_resources(str(trees["project"]))
    assert PROJECT_RULE in loaded
    assert "OUTSIDE-RULE-1d4e" not in loaded


def test_a_target_under_a_filesystem_root_base_is_admitted(tmp_path):
    """A root anchor (``file:///rules.md``) already ends in a separator, so the
    containment prefix is the root itself, not a doubled separator nothing
    starts with."""
    from kiro_crew.context import steering_target_admissible

    doc = _write(tmp_path / "rules.md", "ROOT-ANCHORED-RULE").resolve()
    assert steering_target_admissible(doc, base=Path(doc.anchor))


@_needs_pinned_walk
@pytest.mark.parametrize("fresh", [True, False], ids=["fresh", "after-compaction"])
def test_a_relocated_kiro_home_keeps_folder_selected_global_steering(
    tmp_path, trees, monkeypatch, fresh
):
    """The global root the load reads is the root the folder dedup skips.

    Under a ``KIRO_HOME`` outside the home, a folder selecting the host
    ``~/.kiro/steering`` still delivers its rule exactly once, and the moved
    tree, which nothing selected, is not read.
    """
    moved = tmp_path / "kiro-home"
    _write(moved / "steering" / "moved.md", "MOVED-GLOBAL-RULE-7e2c")
    _write(
        moved / "agents" / "kirocrew.json",
        json.dumps({"name": "kirocrew", "resources": [SHIPPED_STEERING_RESOURCE]}),
    )
    monkeypatch.setenv("KIRO_HOME", str(moved))
    msg = _claude_turn(tmp_path, trees, fresh=fresh, steering_dirs=trees["dirs"])
    assert msg.count(GLOBAL_RULE) == 1
    assert "MOVED-GLOBAL-RULE-7e2c" not in msg
