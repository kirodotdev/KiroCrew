"""kiro-cli's opt-out of inherited default resources, as the member snapshot honours it.

Which harness and entry point reads the ``chat.disableInheritingDefaultResources``
setting, what the member snapshot keeps under it, declared and projected steering-glob
links, and folder-declared steering trees reaching a member once.

Lifecycle injection, member identity and the basic reads are pinned in
``test_member_essential_context.py``.
"""

import gc
import json
import os
from pathlib import Path

import member_essential_context_helpers
import pytest
from member_essential_context_helpers import (
    _declare_steering_glob_only,
    _snapshot_bodies,
    _write_kiro_settings,
)

from conftest import make_dir_link, requires_symlinks
from kiro_crew import context as context_module
from kiro_crew import member_essential_context as _mec
from kiro_crew import pinned_fs
from kiro_crew.agent_sdk.provider_identity import PROVIDER_ACP, PROVIDER_CLAUDE_CODE
from kiro_crew.member_essential_context import MemberEssentialContextError

# Fixtures shared with the sibling modules, bound by assignment so pytest registers
# them for the tests in this module.
_close_skills_loaders = member_essential_context_helpers._close_skills_loaders
env = member_essential_context_helpers.env


@pytest.fixture
def cyclic_gc_quiesced():
    """Keep a cyclic-GC pass (and any finalizer it runs) out of a deep JSON parse.

    A recursion-limit fixture drives the parser to the bottom of the stack; a
    collection firing there could run an inherited finalizer with no stack
    left. Drain first, disable for the parse, then restore and drain again.
    """
    gc.collect()
    was_enabled = gc.isenabled()
    gc.disable()
    try:
        yield
    finally:
        if was_enabled:
            gc.enable()
        gc.collect()


_OPERATOR_RULE = "Operator rule: ask before every comment."
_INHERITED_PROJECT_GUIDES = (
    "Project rules: run the review checks.",
    "Always guide: explain assumptions.",
)


@pytest.fixture
def operator_steering(env):
    """One global steering file, the operator's own rule the member may not want."""
    steering = Path.home() / ".kiro" / "steering"
    steering.mkdir(parents=True, exist_ok=True)
    (steering / "operator.md").write_text(_OPERATOR_RULE, encoding="utf-8")
    return env


def test_workspace_opt_out_keeps_only_what_the_template_declares(operator_steering):
    """kiro-cli gives an opted-out custom agent only its declared resources.

    The snapshot is what the member works from, so it must not hand back the
    operator's global steering, AGENTS.md or undeclared workspace steering.
    """
    env = operator_steering
    assert _OPERATOR_RULE in _snapshot_bodies(env)

    _write_kiro_settings(env.project / ".kiro", {"chat.disableInheritingDefaultResources": True})
    bodies = _snapshot_bodies(env)
    assert _OPERATOR_RULE not in bodies
    for guide in _INHERITED_PROJECT_GUIDES:
        assert guide not in bodies
    assert "Declared guide: examples must be reproducible." in bodies
    assert "Bound Soul: preserve the user's voice." in bodies
    assert "Project Soul: write with empathy." in bodies


def test_opt_out_still_loads_steering_the_template_declares(operator_steering):
    env = operator_steering
    (env.project / ".kiro" / "agents" / "writer-template.json").write_text(
        json.dumps({"name": "writer-template", "resources": ["file://.kiro/steering/**/*.md"]}),
        encoding="utf-8",
    )
    _write_kiro_settings(env.project / ".kiro", {"chat.disableInheritingDefaultResources": True})
    bodies = _snapshot_bodies(env)
    assert "Always guide: explain assumptions." in bodies
    assert _OPERATOR_RULE not in bodies


def _steering_link_target(env, tmp_path, *, inside: bool) -> Path:
    """A Markdown file a steering link can point at: in the project, or beside it."""
    base = env.project / "docs" if inside else tmp_path / "outside"
    base.mkdir(exist_ok=True)
    target = base / "shared-style.md"
    target.write_text("LINKED_TARGET_BODY", encoding="utf-8")
    return target


@requires_symlinks
def test_declared_steering_glob_names_a_link_inside_the_project_instead_of_refusing(env, tmp_path):
    """The shipped steering glob treats an in-project link as the implicit scan does.

    The link is left out unread and named in-band; the regular steering beside it
    still loads.
    """
    _declare_steering_glob_only(env)
    link = env.project / ".kiro" / "steering" / "linked.md"
    link.symlink_to(_steering_link_target(env, tmp_path, inside=True))
    bodies = _snapshot_bodies(env)
    assert "LINKED_TARGET_BODY" not in bodies
    assert "Always guide: explain assumptions." in bodies
    assert f"[Essential source: {_mec.ESSENTIAL_LINKED_SKIP_SOURCE}]" in bodies
    assert str(link) in bodies


@requires_symlinks
def test_declared_steering_glob_names_a_linked_directory_inside_the_project_unread(env, tmp_path):
    _declare_steering_glob_only(env)
    inner = env.project / "docs" / "team-steering"
    inner.mkdir(parents=True)
    (inner / "inner.md").write_text("LINKED_DIR_BODY", encoding="utf-8")
    shared = env.project / ".kiro" / "steering" / "shared"
    make_dir_link(shared, inner)
    bodies = _snapshot_bodies(env)
    assert "LINKED_DIR_BODY" not in bodies
    assert str(shared) in bodies


@requires_symlinks
def test_declared_steering_glob_names_a_linked_steering_root_inside_the_project(env):
    """A linked ``.kiro/steering`` puts the whole tree behind the link: named, nothing read."""
    _declare_steering_glob_only(env)
    steering = env.project / ".kiro" / "steering"
    real = env.project / "docs" / "steering-tree"
    real.parent.mkdir()
    steering.rename(real)
    make_dir_link(steering, real)
    bodies = _snapshot_bodies(env)
    assert "Always guide: explain assumptions." not in bodies
    assert str(steering) in bodies


@requires_symlinks
def test_declared_steering_glob_does_not_name_an_inside_link_it_would_never_read(env, tmp_path):
    """As in the implicit scan, only a link the glob could have used is reported."""
    _declare_steering_glob_only(env)
    notes = env.project / "docs" / "notes.txt"
    notes.parent.mkdir()
    notes.write_text("NOT_STEERING", encoding="utf-8")
    (env.project / ".kiro" / "steering" / "README.txt").symlink_to(notes)
    bodies = _snapshot_bodies(env)
    assert "Always guide: explain assumptions." in bodies
    assert _mec.ESSENTIAL_LINKED_SKIP_SOURCE not in bodies


@requires_symlinks
@pytest.mark.parametrize(
    "shape", ["file", "directory", "dangling", "unrelated-name", "steering-root"]
)
def test_declared_steering_glob_still_refuses_a_link_that_leaves_the_project(env, tmp_path, shape):
    """Only a link that resolves inside the project may be left out; any other refuses."""
    _declare_steering_glob_only(env)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("OUTSIDE_SECRET", encoding="utf-8")
    steering = env.project / ".kiro" / "steering"
    if shape == "file":
        (steering / "linked.md").symlink_to(outside / "secret.md")
    elif shape == "directory":
        make_dir_link(steering / "shared", outside)
    elif shape == "dangling":
        (steering / "linked.md").symlink_to(outside / "gone.md")
    elif shape == "unrelated-name":
        (outside / "notes.txt").write_text("NOT_STEERING", encoding="utf-8")
        (steering / "README.txt").symlink_to(outside / "notes.txt")
    else:
        for child in steering.iterdir():
            child.rename(outside / child.name)
        steering.rmdir()
        make_dir_link(steering, outside)
    with pytest.raises(MemberEssentialContextError, match="linked document or directory"):
        _snapshot_bodies(env)


@requires_symlinks
def test_projected_steering_glob_leaves_a_link_inside_the_project_out(env, tmp_path):
    """The KAS document reader meets the same link and must not stop the session either."""
    link = env.project / ".kiro" / "steering" / "linked.md"
    link.symlink_to(_steering_link_target(env, tmp_path, inside=True))
    definition = {"id": "worker", "resources": ["file://.kiro/steering/**/*.md"]}
    documents = _mec.projected_resource_documents(definition, str(env.project))
    assert "Always guide: explain assumptions." in documents.values()
    assert not any("LINKED_TARGET_BODY" in body for body in documents.values())


@requires_symlinks
def test_projected_global_steering_glob_leaves_a_link_inside_home_out(operator_steering):
    """A projected global steering glob is rooted at home, so a link into home passes."""
    env = operator_steering
    steering = Path.home() / ".kiro" / "steering"
    dotfiles = Path.home() / "dotfiles"
    dotfiles.mkdir()
    (dotfiles / "style.md").write_text("LINKED_DOTFILE_BODY", encoding="utf-8")
    (steering / "style.md").symlink_to(dotfiles / "style.md")
    definition = {"id": "worker", "resources": [f"file://{steering.as_posix()}/**/*.md"]}
    documents = _mec.projected_resource_documents(definition, str(env.project))
    assert _OPERATOR_RULE in documents.values()
    assert not any("LINKED_DOTFILE_BODY" in body for body in documents.values())


@requires_symlinks
def test_projected_steering_glob_still_refuses_a_link_outside_the_project(env, tmp_path):
    (env.project / ".kiro" / "steering" / "linked.md").symlink_to(
        _steering_link_target(env, tmp_path, inside=False)
    )
    definition = {"id": "worker", "resources": ["file://.kiro/steering/**/*.md"]}
    with pytest.raises(MemberEssentialContextError, match="linked document or directory"):
        _mec.projected_resource_documents(definition, str(env.project))


@requires_symlinks
def test_launch_record_stays_withheld_while_a_steering_glob_reaches_a_link(env, tmp_path):
    """The record measures what kiro-cli loads, and a link cannot be measured unfollowed."""
    definition = {"resources": ["file://.kiro/steering/**/*.md"]}
    assert _mec._declared_files_fit(definition, str(env.project)) is True
    assert _mec.projected_launch_paths(definition, str(env.project))
    (env.project / ".kiro" / "steering" / "linked.md").symlink_to(
        _steering_link_target(env, tmp_path, inside=True)
    )
    assert _mec._declared_files_fit(definition, str(env.project)) is False
    with pytest.raises(MemberEssentialContextError, match="linked document or directory"):
        _mec.projected_launch_paths(definition, str(env.project))


def test_a_caller_that_names_no_harness_keeps_inheriting_under_the_opt_out(
    operator_steering, monkeypatch
):
    """The opt-out is kiro-cli's; a build that does not say kiro-cli serves the
    session never reads it, so the global steering stays in the snapshot. A
    future caller that omits ``provider_type`` on another harness therefore
    cannot drop rules nothing else would deliver."""
    env = operator_steering
    _write_kiro_settings(env.project / ".kiro", {"chat.disableInheritingDefaultResources": True})
    reads = _count_setting_reads(monkeypatch)

    bodies = env.builder._build_v2_essentials(
        env.store, member=env.member, project=str(env.project)
    )

    assert _OPERATOR_RULE in bodies
    for guide in _INHERITED_PROJECT_GUIDES:
        assert guide in bodies
    assert reads == []
    assert _OPERATOR_RULE not in _snapshot_bodies(env), "the kiro-cli build still opts out"


def _entry_point_prompt(env, entry_point: str, **kwargs) -> str:
    """A member's session start built through one of the two prompt entry points.

    ``provider_type`` is forwarded only when the test names it, so an omitted
    harness exercises each entry point's own default.
    """
    common = dict(memory_store=env.store, member=env.member, project=str(env.project))
    if entry_point == "build_message":
        message, _ = env.builder.build_message(
            "Continue", True, "dashboard:member", **common, **kwargs
        )
        return message
    return env.builder.build_session_context(**common, **kwargs)


_ENTRY_POINTS = pytest.mark.parametrize("entry_point", ["build_message", "build_session_context"])


@_ENTRY_POINTS
def test_an_entry_point_given_no_harness_keeps_inheriting_under_the_opt_out(
    operator_steering, monkeypatch, entry_point
):
    """``build_message`` and ``build_session_context`` hand the essentials builder
    only the harness their caller (or its context provider) named. A caller that
    names none reaches the builder as an unknown harness, so the opt-out is not
    read and the operator's global steering stays in the envelope."""
    env = operator_steering
    _write_kiro_settings(env.project / ".kiro", {"chat.disableInheritingDefaultResources": True})
    reads = _count_setting_reads(monkeypatch)

    prompt = _entry_point_prompt(env, entry_point)

    assert _OPERATOR_RULE in prompt
    for guide in _INHERITED_PROJECT_GUIDES:
        assert guide in prompt
    assert reads == []
    env.forbidden.assert_not_called()


@_ENTRY_POINTS
def test_an_entry_point_naming_kiro_cli_reads_the_opt_out(
    operator_steering, monkeypatch, entry_point
):
    env = operator_steering
    _write_kiro_settings(env.project / ".kiro", {"chat.disableInheritingDefaultResources": True})
    reads = _count_setting_reads(monkeypatch)

    prompt = _entry_point_prompt(env, entry_point, provider_type=PROVIDER_ACP)

    assert _OPERATOR_RULE not in prompt
    for guide in _INHERITED_PROJECT_GUIDES:
        assert guide not in prompt
    assert "Declared guide: examples must be reproducible." in prompt
    assert len(reads) == 1


@pytest.mark.parametrize(
    ("source", "global_opt_out", "expected"),
    [
        ("local", False, True),
        ("local", True, True),
        ("global", False, True),
        ("global", True, False),
    ],
)
def test_crew_overlay_is_not_read_as_an_opt_out(
    operator_steering, source, global_opt_out, expected
):
    """The projection pins the native key to true and records the real choice beside it."""
    env = operator_steering
    _write_kiro_settings(
        env.project / ".kiro",
        {
            "chat.disableInheritingDefaultResources": True,
            "kirocrew.skillDiscovery.inheritFiles": True,
            "kirocrew.skillDiscovery.inheritSource": source,
        },
    )
    if global_opt_out:
        _write_kiro_settings(
            Path.home() / ".kiro", {"chat.disableInheritingDefaultResources": True}
        )
    assert (_OPERATOR_RULE in _snapshot_bodies(env)) is expected


@pytest.mark.parametrize(("workspace_value", "expected"), [(None, False), (False, True)])
def test_global_opt_out_applies_unless_the_workspace_overrides_it(
    operator_steering, workspace_value, expected
):
    env = operator_steering
    _write_kiro_settings(Path.home() / ".kiro", {"chat.disableInheritingDefaultResources": True})
    if workspace_value is not None:
        _write_kiro_settings(
            env.project / ".kiro", {"chat.disableInheritingDefaultResources": workspace_value}
        )
    assert (_OPERATOR_RULE in _snapshot_bodies(env)) is expected


@pytest.mark.parametrize("value", ["true", 1])
def test_only_the_literal_true_opts_out(operator_steering, value):
    env = operator_steering
    _write_kiro_settings(env.project / ".kiro", {"chat.disableInheritingDefaultResources": value})
    assert _OPERATOR_RULE in _snapshot_bodies(env)


@pytest.mark.parametrize(
    "contents",
    ["{not json", "[" * 100_000 + "]" * 100_000],
    ids=["malformed", "deeply-nested"],
)
def test_unreadable_settings_keep_inherited_guides(operator_steering, contents, cyclic_gc_quiesced):
    env = operator_steering
    settings = env.project / ".kiro" / "settings" / "cli.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(contents, encoding="utf-8")
    assert _OPERATOR_RULE in _snapshot_bodies(env)


@requires_symlinks
def test_cyclic_kiro_home_keeps_inherited_guides(operator_steering, monkeypatch):
    env = operator_steering
    loop = env.project.parent / "kiro-home-loop"
    os.symlink(loop, loop)
    monkeypatch.setenv("KIRO_HOME", str(loop))

    from kiro_crew.acp import skill_projection

    assert skill_projection.inherits_default_resources(env.project) is True
    assert _OPERATOR_RULE in _snapshot_bodies(env)


def test_launch_documents_follow_the_workspace_opt_out(operator_steering):
    from kiro_crew.member_essential_context import kiro_launch_documents

    env = operator_steering

    def launched() -> list[str]:
        return [body for _, body in kiro_launch_documents("writer-template", str(env.project))]

    assert _OPERATOR_RULE in launched()
    _write_kiro_settings(env.project / ".kiro", {"chat.disableInheritingDefaultResources": True})
    bodies = launched()
    assert _OPERATOR_RULE not in bodies
    assert "Project rules: run the review checks." not in bodies
    assert "Declared guide: examples must be reproducible." in bodies


_OMIT_HARNESS = object()


def _envelope_with_folder_steering_trees(env, provider_type: object = PROVIDER_ACP) -> str:
    """A member turn in a folder that declares both ``.kiro/steering`` trees.

    ``provider_type=_OMIT_HARNESS`` leaves the argument out so the turn runs on
    :meth:`ContextBuilder.build_message`'s own default.
    """
    harness = {} if provider_type is _OMIT_HARNESS else {"provider_type": provider_type}
    message, _ = env.builder.build_message(
        "Continue",
        True,
        "dashboard:member",
        memory_store=env.store,
        member=env.member,
        project=str(env.project),
        **harness,
        steering_dirs=(
            str(env.project / ".kiro" / "steering"),
            str(Path.home() / ".kiro" / "steering"),
        ),
    )
    env.forbidden.assert_not_called()
    return message


_OTHER_HARNESSES = pytest.mark.parametrize(
    "provider_type", [PROVIDER_CLAUDE_CODE, "codex", "acme-config-authored-harness"]
)


@_OTHER_HARNESSES
def test_opt_out_binds_only_a_session_kiro_cli_serves(operator_steering, provider_type):
    """``chat.disableInheritingDefaultResources`` is kiro-cli's setting.

    On any other harness the member keeps the operator's global steering,
    ``AGENTS.md`` and the project's always steering: the setting changes what a
    member loads only on a session kiro-cli serves.
    """
    env = operator_steering
    _write_kiro_settings(env.project / ".kiro", {"chat.disableInheritingDefaultResources": True})
    assert _OPERATOR_RULE not in _snapshot_bodies(env)
    bodies = _snapshot_bodies(env, provider_type=provider_type)
    assert _OPERATOR_RULE in bodies
    for guide in _INHERITED_PROJECT_GUIDES:
        assert guide in bodies


def _count_setting_reads(monkeypatch) -> list[object]:
    """Record every call of the one settings reader behind the verdict."""
    from kiro_crew.agent_sdk.drivers import acp as acp_driver

    reads: list[object] = []
    real = acp_driver.inherits_default_resources

    def counted(work_dir):
        reads.append(work_dir)
        return real(work_dir)

    monkeypatch.setattr(acp_driver, "inherits_default_resources", counted)
    return reads


def test_profile_validation_measures_inheriting_envelope_without_reading_setting(
    operator_steering, monkeypatch
):
    """Validation measures the largest envelope, independent of the current harness."""
    env = operator_steering
    _write_kiro_settings(env.project / ".kiro", {"chat.disableInheritingDefaultResources": True})
    reads = _count_setting_reads(monkeypatch)

    validation = env.builder._build_v2_essentials(
        env.store,
        member=env.member,
        project=str(env.project),
        profile_overrides={"preferences.md": "Candidate preference."},
        provider_type=PROVIDER_ACP,
    )

    assert "Candidate preference." in validation
    assert _OPERATOR_RULE in validation
    for guide in _INHERITED_PROJECT_GUIDES:
        assert guide in validation
    assert reads == []

    normal_turn = _snapshot_bodies(env)
    assert _OPERATOR_RULE not in normal_turn
    for guide in _INHERITED_PROJECT_GUIDES:
        assert guide not in normal_turn
    assert len(reads) == 1


def test_documents_for_member_takes_the_verdict_and_never_reads_the_setting(
    operator_steering, monkeypatch
):
    """The snapshot reader is a consumer: without the caller's verdict it inherits."""
    from kiro_crew.member_essential_context import documents_for_member

    env = operator_steering
    _write_kiro_settings(env.project / ".kiro", {"chat.disableInheritingDefaultResources": True})
    reads = _count_setting_reads(monkeypatch)
    bodies = [body for _, body in documents_for_member("writer-template", str(env.project))]
    assert _OPERATOR_RULE in bodies
    opted_out = [
        body
        for _, body in documents_for_member(
            "writer-template", str(env.project), inherits_default_resources=False
        )
    ]
    assert _OPERATOR_RULE not in opted_out
    assert reads == []


_needs_pinned_walk = pytest.mark.skipif(
    not pinned_fs.supports_pinned_tree_walk(),
    reason="folder steering refuses to walk by name on this platform",
)


def _declare_project_steering_in_template(env) -> None:
    path = env.project / ".kiro" / "agents" / "writer-template.json"
    spec = json.loads(path.read_text(encoding="utf-8"))
    spec["resources"].append("file://.kiro/steering/**/*.md")
    path.write_text(json.dumps(spec), encoding="utf-8")


@_needs_pinned_walk
def test_folder_declared_steering_trees_reach_an_opted_out_member_once(operator_steering):
    """The snapshot skips the trees, so the folder dedup must not skip them too.

    Otherwise an always-inclusion document under a declared root reaches the
    member zero times: the snapshot leaves it to kiro-cli, which the workspace
    told not to load it, and the folder section assumes the snapshot sent it.
    """
    env = operator_steering
    _declare_project_steering_in_template(env)
    _write_kiro_settings(env.project / ".kiro", {"chat.disableInheritingDefaultResources": True})
    message = _envelope_with_folder_steering_trees(env)
    assert message.count(_OPERATOR_RULE) == 1
    assert message.count("Always guide: explain assumptions.") == 1
    # AGENTS.md is a kiro-cli default resource, not a steering document, so the
    # folder does not carry it back in.
    assert "Project rules: run the review checks." not in message
    assert "MANUAL_SECRET" not in message


@_needs_pinned_walk
def test_opted_out_folder_dedup_does_not_resolve_source_labels(operator_steering, monkeypatch):
    """Canonical dedup does not re-resolve the collector's emitted labels."""
    from kiro_crew import member_essential_context as essential_context

    env = operator_steering
    _declare_project_steering_in_template(env)
    _write_kiro_settings(env.project / ".kiro", {"chat.disableInheritingDefaultResources": True})

    snapshot_sources: set[str] = set()
    folder_sources: set[str] = set()
    dedup_started = False
    resolved_labels: list[str] = []
    real_documents_for_member = essential_context.documents_for_member
    real_collect_folder_steering = context_module.collect_folder_steering
    real_realpath = os.path.realpath

    def recording_documents_for_member(*args, **kwargs):
        result = real_documents_for_member(*args, **kwargs)
        snapshot_sources.update(source for source, _body in result)
        return result

    def recording_collect_folder_steering(*args, **kwargs):
        nonlocal dedup_started
        result = real_collect_folder_steering(*args, **kwargs)
        folder_sources.update(source for source, _body in result.documents)
        dedup_started = True
        return result

    def recording_realpath(path, *args, **kwargs):
        spelling = str(path)
        if dedup_started and spelling in snapshot_sources | folder_sources:
            resolved_labels.append(spelling)
        return real_realpath(path, *args, **kwargs)

    monkeypatch.setattr(essential_context, "documents_for_member", recording_documents_for_member)
    monkeypatch.setattr(
        context_module, "collect_folder_steering", recording_collect_folder_steering
    )
    monkeypatch.setattr(os.path, "realpath", recording_realpath)

    message = _envelope_with_folder_steering_trees(env)

    assert message.count("Always guide: explain assumptions.") == 1
    assert resolved_labels == []


@_needs_pinned_walk
@pytest.mark.parametrize(
    ("template_name", "folder_name"),
    [
        ("a\ufffdb.md", "a\nb.md"),
        ("a\nb.md", "a\ufffdb.md"),
    ],
    ids=["template-replacement", "template-newline"],
)
def test_opted_out_folder_dedup_keeps_distinct_fold_colliding_sources(
    operator_steering, template_name, folder_name
):
    """Distinct canonical files survive even when their rendered labels collide."""
    env = operator_steering
    steering = env.project / ".kiro" / "steering"
    template_body = "Template-only folded-label guide."
    folder_body = "Folder-only folded-label guide."
    (steering / template_name).write_text(template_body, encoding="utf-8")
    (steering / folder_name).write_text(folder_body, encoding="utf-8")
    spec_path = env.project / ".kiro" / "agents" / "writer-template.json"
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    spec["resources"].append(f"file://.kiro/steering/{template_name}")
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    _write_kiro_settings(env.project / ".kiro", {"chat.disableInheritingDefaultResources": True})

    message = _envelope_with_folder_steering_trees(env)

    assert message.count(template_body) == 1
    assert message.count(folder_body) == 1


@_needs_pinned_walk
def test_opted_out_folder_dedup_matches_control_folded_source_label(operator_steering):
    """A control character folded out of a folder label does not duplicate its body."""
    env = operator_steering
    folded = env.project / ".kiro" / "steering" / "folded\nname.md"
    folded.write_text("Folded label guide: keep one copy.", encoding="utf-8")
    _declare_project_steering_in_template(env)
    _write_kiro_settings(env.project / ".kiro", {"chat.disableInheritingDefaultResources": True})

    message = _envelope_with_folder_steering_trees(env)

    assert message.count("Folded label guide: keep one copy.") == 1


@_needs_pinned_walk
def test_folder_declared_steering_trees_reach_an_inheriting_member_once(operator_steering):
    env = operator_steering
    _declare_project_steering_in_template(env)
    message = _envelope_with_folder_steering_trees(env)
    assert message.count(_OPERATOR_RULE) == 1
    assert message.count("Always guide: explain assumptions.") == 1
    assert message.count("Project rules: run the review checks.") == 1


@_needs_pinned_walk
def test_folder_declared_trees_reach_an_inheriting_member_once_when_no_harness_is_named(
    operator_steering,
):
    """An unnamed harness leaves a private member inheriting, so its essentials
    envelope carries both trees; the folder section is not rendered on a member
    turn, and each guide arrives exactly once."""
    env = operator_steering
    _declare_project_steering_in_template(env)
    message = _envelope_with_folder_steering_trees(env, provider_type=_OMIT_HARNESS)
    assert message.count(_OPERATOR_RULE) == 1
    assert message.count("Always guide: explain assumptions.") == 1
    assert message.count("Project rules: run the review checks.") == 1


@_needs_pinned_walk
@_OTHER_HARNESSES
def test_folder_declared_steering_trees_reach_an_opted_out_member_once_on_another_harness(
    operator_steering, provider_type
):
    """The opted-out workspace is irrelevant off kiro-cli: the snapshot delivers
    the trees, so the folder skips them and each guide arrives exactly once."""
    env = operator_steering
    _declare_project_steering_in_template(env)
    _write_kiro_settings(env.project / ".kiro", {"chat.disableInheritingDefaultResources": True})
    message = _envelope_with_folder_steering_trees(env, provider_type=provider_type)
    assert message.count(_OPERATOR_RULE) == 1
    assert message.count("Always guide: explain assumptions.") == 1
    assert message.count("Project rules: run the review checks.") == 1
    assert "MANUAL_SECRET" not in message


@_needs_pinned_walk
@pytest.mark.parametrize("opted_out", [False, True])
def test_a_kiro_member_turn_reads_the_setting_exactly_once(
    operator_steering, monkeypatch, opted_out
):
    """One verdict per turn feeds the snapshot and the folder dedup alike."""
    env = operator_steering
    _declare_project_steering_in_template(env)
    if opted_out:
        _write_kiro_settings(
            env.project / ".kiro", {"chat.disableInheritingDefaultResources": True}
        )
    reads = _count_setting_reads(monkeypatch)
    message = _envelope_with_folder_steering_trees(env)
    assert message.count(_OPERATOR_RULE) == 1
    assert len(reads) == 1


@_needs_pinned_walk
@_OTHER_HARNESSES
def test_another_harness_never_reads_the_setting(operator_steering, monkeypatch, provider_type):
    env = operator_steering
    _declare_project_steering_in_template(env)
    _write_kiro_settings(env.project / ".kiro", {"chat.disableInheritingDefaultResources": True})
    reads = _count_setting_reads(monkeypatch)
    _envelope_with_folder_steering_trees(env, provider_type=provider_type)
    assert reads == []
