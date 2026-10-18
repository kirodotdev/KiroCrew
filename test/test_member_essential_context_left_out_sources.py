"""Sources left out of the member snapshot and named in-band instead of refusing the turn.

Linked and hard-linked steering, guides that do not fit the budget and absent declared
files are left out with a note naming them, while the shapes that must still refuse
(a link that leaves its root, an unreadable file, a core over budget) keep refusing.

Lifecycle injection, member identity and the basic reads are pinned in
``test_member_essential_context.py``.
"""

import json
import os
from pathlib import Path

import member_essential_context_helpers
import pytest
from member_essential_context_helpers import (
    _declare_steering_glob_only,
    _snapshot_bodies,
    _write_kiro_settings,
    _write_template,
)

from conftest import make_dir_link, requires_symlinks
from kiro_crew import member_essential_context as _mec
from kiro_crew.agent_sdk.provider_identity import PROVIDER_ACP
from kiro_crew.member_essential_context import MemberEssentialContextError

# Fixtures shared with the sibling modules, bound by assignment so pytest registers
# them for the tests in this module.
_close_skills_loaders = member_essential_context_helpers._close_skills_loaders
env = member_essential_context_helpers.env


def _link_global_steering(env, tmp_path) -> Path:
    """Make ``~/.kiro/steering/context/style.md`` a link into an outside repo."""
    repo = tmp_path / "steering-repo"
    repo.mkdir()
    (repo / "style.md").write_text("LINKED_STEERING_BODY", encoding="utf-8")
    context = Path.home() / ".kiro" / "steering" / "context"
    context.mkdir(parents=True, exist_ok=True)
    (Path.home() / ".kiro" / "steering" / "plain.md").write_text(
        "PLAIN_STEERING_BODY", encoding="utf-8"
    )
    link = context / "style.md"
    link.symlink_to(repo / "style.md")
    return link


@requires_symlinks
def test_linked_global_steering_file_is_named_instead_of_refusing_the_turn(env, tmp_path):
    """A steering link left out of the snapshot does not stop the member.

    Global steering is read because it exists, like a project's ``AGENTS.md``,
    so a link there is left out unread and named in-band; the regular steering
    file beside it still loads.
    """
    link = _link_global_steering(env, tmp_path)
    message = _member_message(env)
    assert "LINKED_STEERING_BODY" not in message
    assert "PLAIN_STEERING_BODY" in message
    assert f"[Essential source: {_mec.ESSENTIAL_LINKED_SKIP_SOURCE}]" in message
    assert str(link) in message
    # kiro-cli may load the link natively, so the note must not tell the model to
    # disregard it or ask the user to restructure a working setup.
    assert "may still load linked steering natively" in message
    note = _mec._skipped_linked_note([link])
    assert note is not None and "Do not assume" not in note[1]


@requires_symlinks
def test_linked_global_steering_directory_is_named_not_enumerated(env, tmp_path):
    repo = tmp_path / "steering-repo"
    repo.mkdir()
    (repo / "inner.md").write_text("LINKED_DIR_BODY", encoding="utf-8")
    steering = Path.home() / ".kiro" / "steering"
    steering.mkdir(parents=True, exist_ok=True)
    make_dir_link(steering / "shared", repo)
    message = _member_message(env)
    assert "LINKED_DIR_BODY" not in message
    assert str(steering / "shared") in message


@requires_symlinks
def test_linked_non_steering_entry_is_not_named(env, tmp_path):
    """A link the glob would never have read (not ``*.md``) is not reported."""
    steering = Path.home() / ".kiro" / "steering"
    steering.mkdir(parents=True, exist_ok=True)
    target = tmp_path / "notes.txt"
    target.write_text("NOT_STEERING", encoding="utf-8")
    (steering / "README.txt").symlink_to(target)
    linked: list[Path] = []
    _mec._matches(Path.home(), ".kiro/steering/**/*.md", linked=linked)
    assert linked == []


@requires_symlinks
def test_linked_steering_entry_is_screened_before_its_target_is_probed(env, tmp_path, monkeypatch):
    """A link whose target the path gate refuses is named without a following probe.

    ``entry.is_dir()`` follows the link; on Windows a UNC target would be an
    outbound SMB probe, so the gate must run first and short-circuit it.
    """
    from kiro_crew import member_essential_context as mec

    steering = Path.home() / ".kiro" / "steering"
    steering.mkdir(parents=True, exist_ok=True)
    target = tmp_path / "remote-share"
    target.mkdir()
    link = steering / "shared"
    link.symlink_to(target, target_is_directory=True)
    real_validate = mec.validate_file_path
    monkeypatch.setattr(
        mec,
        "validate_file_path",
        lambda p, *a, **k: None if Path(p) == link else real_validate(p, *a, **k),
    )
    probed: list[str] = []
    real_is_dir = os.DirEntry.is_dir

    def _spy(entry, *, follow_symlinks=True):
        if follow_symlinks and Path(entry.path) == link:
            probed.append(entry.path)
        return real_is_dir(entry, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(mec.os, "scandir", _wrap_scandir(os.scandir, _spy))
    linked: list[Path] = []
    mec._matches(Path.home(), ".kiro/steering/**/*.md", linked=linked)
    assert linked == [link]
    assert probed == []


def _wrap_scandir(real_scandir, is_dir):
    """``os.scandir`` whose entries route ``is_dir`` through *is_dir*."""

    class _Entry:
        def __init__(self, entry):
            self._entry = entry
            self.name = entry.name
            self.path = entry.path

        def is_dir(self, *, follow_symlinks=True):
            return is_dir(self._entry, follow_symlinks=follow_symlinks)

        def __getattr__(self, attr):
            return getattr(self._entry, attr)

    class _Iter:
        """Usable as a context manager and as a plain iterator, like ``os.scandir``."""

        def __init__(self, path):
            self._it = real_scandir(path)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._it.close()

        def __iter__(self):
            return self

        def __next__(self):
            return _Entry(next(self._it))

        def close(self):
            self._it.close()

    return _Iter


@requires_symlinks
def test_kiro_launch_documents_leaves_a_linked_global_steering_file_out(env, tmp_path):
    """The spawn-time native list must not refuse the session start either."""
    link = _link_global_steering(env, tmp_path)
    sources = dict(_mec.kiro_launch_documents("writer-template", str(env.project)))
    assert str(link) not in sources
    assert "PLAIN_STEERING_BODY" in sources.values()


@requires_symlinks
def test_walk_without_a_collector_still_refuses_a_linked_steering_file(env, tmp_path):
    """A walk handed no ``linked`` collector refuses a link instead of leaving it out."""
    _link_global_steering(env, tmp_path)
    with pytest.raises(MemberEssentialContextError, match="linked document"):
        _mec._matches(Path.home(), ".kiro/steering/**/*.md")


def _declare_resources(env, names: list[str]) -> None:
    spec = env.project / ".kiro" / "agents" / "writer-template.json"
    data = json.loads(spec.read_text(encoding="utf-8"))
    data["resources"] = [f"file://{name}" for name in names]
    spec.write_text(json.dumps(data), encoding="utf-8")


def _member_message(env) -> str:
    message, _ = env.builder.build_message(
        "Continue", False, memory_store=env.store, member=env.member, project=str(env.project)
    )
    return message


def _envelope(message: str) -> str:
    start = message.index("[V2 ESSENTIAL CONTEXT")
    end = message.index("[END V2 ESSENTIAL CONTEXT]\n\n", start)
    return message[start : end + len("[END V2 ESSENTIAL CONTEXT]\n\n")]


def test_over_budget_declared_resources_leave_out_the_tail_and_the_turn_runs(env, caplog):
    """The reported shape: an agent spec whose ``resources`` grew past the envelope.

    Guides leave from the tail of declaration order, whole, only as many as needed;
    every one left out is named with its size in one in-band notice; the member's
    core (persona prompt, SOUL.md, profile anchors, recall note) is all still there;
    the result fits the envelope; and the drop is logged at warning.
    """
    names = [f"context/g{i}.md" for i in range(4)]
    (env.project / "context").mkdir()
    for i, name in enumerate(names):
        (env.project / name).write_text(f"GUIDE_{i}_HEAD\n" + "y" * 25_000, encoding="utf-8")
    _declare_resources(env, names)
    with caplog.at_level("WARNING"):
        message = _member_message(env)
    assert "GUIDE_0_HEAD" in message and "GUIDE_1_HEAD" in message
    assert "GUIDE_2_HEAD" not in message and "GUIDE_3_HEAD" not in message
    assert "[Essential source: essential-context#omitted]" in message
    for name in names[2:]:
        assert f"{env.project / name} (25,013 characters)" in message
    for name in names[:2]:
        assert f"{env.project / name} (25,013 characters)" not in message
    for core in (
        "Bound Soul: preserve the user's voice.",
        "Project Soul: write with empathy.",
        "Preference anchor: 请保留中文原文。",
        "Project anchor: the launch guide is authoritative.",
        "memory_recall",
    ):
        assert core in message
    assert len(_envelope(message)) <= 64_000
    warned = [r for r in caplog.records if r.levelname == "WARNING" and "g2.md" in r.getMessage()]
    assert len(warned) == 1 and "g3.md" in warned[0].getMessage()


def test_a_fitting_envelope_carries_no_omission_notice(env):
    assert "essential-context#omitted" not in _member_message(env)


@pytest.mark.parametrize("core", ["prompt", "SOUL.md"])
def test_an_over_budget_core_still_refuses_with_the_source_named(env, core):
    if core == "prompt":
        spec = env.project / ".kiro" / "agents" / "writer-template.json"
        data = json.loads(spec.read_text(encoding="utf-8"))
        data["prompt"] = "p" * 64_001
        spec.write_text(json.dumps(data), encoding="utf-8")
        named = "writer-template.json#prompt"
    else:
        (env.project / "SOUL.md").write_text("s" * 64_001, encoding="utf-8")
        named = "SOUL.md"
    with pytest.raises(MemberEssentialContextError, match="exceeds 64000") as raised:
        _member_message(env)
    assert named in str(raised.value)


def test_a_refused_declared_source_still_refuses_when_the_envelope_is_over_budget(env):
    """Budget fitting never turns a source the reader refuses into an omission."""
    (env.project / "AGENTS.md").write_text("x" * 64_001, encoding="utf-8")
    (env.project / "declared-guide.md").unlink()
    (env.project / "declared-guide.md").mkdir()
    with pytest.raises(MemberEssentialContextError, match="declared-guide"):
        _member_message(env)


def test_guides_whose_names_do_not_fit_are_counted_by_the_short_notice():
    """When the core leaves no room to list every left-out guide, they are counted."""
    from kiro_crew.context_assembly.member import _fit_member_guides_into_envelope

    guides = [(f"/project/context/{'n' * 200}-{i}.md", "g") for i in range(40)]
    core = ("template#prompt", "c" * 63_200)
    fitted, left_out = _fit_member_guides_into_envelope(
        [core, *guides], frozenset(source for source, _ in guides), identity="I", owner="w"
    )
    assert left_out == 40
    assert fitted[0] == core and len(fitted) == 2
    source, notice = fitted[1]
    assert source == "essential-context#omitted"
    assert "40 guide document(s)" in notice and "nnnn" not in notice
    big_core = ("template#prompt", "c" * 64_000)
    documents = [big_core, *guides]
    assert _fit_member_guides_into_envelope(
        documents, frozenset(source for source, _ in guides), identity="I", owner="w"
    ) == (documents, 0)


def test_a_guide_too_large_to_fit_does_not_take_the_smaller_guides_after_it_out(env):
    """Only the guides that cannot fit are left out; later small ones are given back."""
    (env.project / "AGENTS.md").write_text("x" * 64_001, encoding="utf-8")
    message = _member_message(env)
    assert f"{env.project / 'AGENTS.md'} (64,001 characters)" in message
    assert "1 guide document(s)" in message
    assert "Declared guide: examples must be reproducible." in message
    assert "Always guide: explain assumptions." in message
    assert len(_envelope(message)) <= 64_000


def _declare(env, resources: list[str]) -> None:
    spec = env.project / ".kiro" / "agents" / "writer-template.json"
    spec.write_text(
        json.dumps({"name": "writer-template", "prompt": "Bound Soul.", "resources": resources}),
        encoding="utf-8",
    )


def test_absent_declared_literal_resource_is_skipped_with_a_note(env):
    """kiro-cli skips a declared file that is not there; the session must still start.

    AIM templates declare ``file://AGENTS.md`` and a subagent starts in an empty
    folder, so the absent file is the ordinary case, not an attack.
    """
    from kiro_crew.member_essential_context import (
        ESSENTIAL_MISSING_SKIP_SOURCE,
        documents_for_member,
    )

    (env.project / "AGENTS.md").unlink()
    _declare(env, ["file://AGENTS.md", "file://notes/README.md", "file://declared-guide.md"])
    message, _ = env.builder.build_message(
        "Continue", False, memory_store=env.store, member=env.member, project=str(env.project)
    )
    assert "Declared guide: examples must be reproducible." in message
    assert f"[Essential source: {ESSENTIAL_MISSING_SKIP_SOURCE}:writer-template]" in message
    assert "DECLARED RESOURCES NOT FOUND. 2 files" in message
    assert str(env.project.resolve() / "AGENTS.md") in message
    assert str(env.project.resolve() / "notes" / "README.md") in message
    core: set[str] = set()
    documents = documents_for_member("writer-template", str(env.project), core_sources_out=core)
    assert f"{ESSENTIAL_MISSING_SKIP_SOURCE}:writer-template" in core
    native = documents_for_member("writer-template", str(env.project), native_only=True)
    assert not any(s.startswith(ESSENTIAL_MISSING_SKIP_SOURCE) for s, _ in native)
    assert any(s.startswith(ESSENTIAL_MISSING_SKIP_SOURCE) for s, _ in documents)


def test_present_declared_resources_add_no_missing_note(env):
    from kiro_crew.member_essential_context import (
        ESSENTIAL_MISSING_SKIP_SOURCE,
        documents_for_member,
    )

    documents = documents_for_member("writer-template", str(env.project))
    assert not any(s.startswith(ESSENTIAL_MISSING_SKIP_SOURCE) for s, _ in documents)


def test_projected_resources_skip_an_absent_file_in_an_empty_cwd(env, tmp_path):
    """A subagent's empty working folder has no README.md; KAS loads nothing for it."""
    from kiro_crew.member_essential_context import projected_resource_documents

    cwd = tmp_path / "subagent_0001"
    cwd.mkdir()
    definition = {"id": "worker", "resources": ["file://README.md", "file://AGENTS.md"]}
    assert projected_resource_documents(definition, str(cwd)) == {}
    (cwd / "README.md").write_text("PRESENT_README", encoding="utf-8")
    assert projected_resource_documents(definition, str(cwd)) == {
        str(cwd.resolve() / "README.md"): "PRESENT_README"
    }


@requires_symlinks
@pytest.mark.parametrize("shape", ["dangling-leaf", "leaf-to-outside", "dangling-parent"])
def test_declared_literal_link_still_refuses(env, tmp_path, shape):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "GUIDE.md").write_text("OUTSIDE_SECRET", encoding="utf-8")
    if shape == "dangling-leaf":
        (env.project / "GUIDE.md").symlink_to(outside / "gone.md")
        declared = "file://GUIDE.md"
    elif shape == "leaf-to-outside":
        (env.project / "GUIDE.md").symlink_to(outside / "GUIDE.md")
        declared = "file://GUIDE.md"
    else:
        (env.project / "notes").symlink_to(tmp_path / "gone-dir", target_is_directory=True)
        declared = "file://notes/GUIDE.md"
    _declare(env, [declared])
    with pytest.raises(MemberEssentialContextError, match="GUIDE.md"):
        env.builder.build_message(
            "Continue", False, memory_store=env.store, member=env.member, project=str(env.project)
        )


def test_declared_literal_outside_the_root_still_refuses_when_absent(env):
    _declare(env, ["file://../never-created.md"])
    with pytest.raises(MemberEssentialContextError, match="outside"):
        env.builder.build_message(
            "Continue", False, memory_store=env.store, member=env.member, project=str(env.project)
        )


@pytest.mark.skipif(
    os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="POSIX permission bits; root reads mode-000 files",
)
def test_declared_literal_unreadable_file_still_refuses(env):
    guide = env.project / "declared-guide.md"
    guide.chmod(0)
    try:
        with pytest.raises(MemberEssentialContextError, match="declared-guide.md"):
            env.builder.build_message(
                "Continue",
                False,
                memory_store=env.store,
                member=env.member,
                project=str(env.project),
            )
    finally:
        guide.chmod(0o644)


@pytest.mark.skipif(
    os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="POSIX permission bits; root ignores a missing search bit",
)
def test_declared_literal_under_an_unsearchable_directory_still_refuses(env):
    """An lstat that fails for permission is not absence: the source stays refused."""
    locked = env.project / "locked"
    locked.mkdir()
    (locked / "GUIDE.md").write_text("LOCKED_GUIDE", encoding="utf-8")
    locked.chmod(0o600)
    _declare(env, ["file://locked/GUIDE.md"])
    try:
        with pytest.raises(MemberEssentialContextError, match="GUIDE.md"):
            env.builder.build_message(
                "Continue",
                False,
                memory_store=env.store,
                member=env.member,
                project=str(env.project),
            )
    finally:
        locked.chmod(0o755)


def test_absent_managed_literal_resource_still_refuses(env):
    """A missing managed-state path keeps its refusal rather than becoming a skip."""
    from kiro_crew.config import config_dir
    from kiro_crew.member_essential_context import documents_for_member

    project = config_dir() / "workspace"
    agents = project / ".kiro" / "agents"
    agents.mkdir(parents=True, exist_ok=True)
    _write_template(agents, "writer-template", ["file://memory/never-written.md"])
    with pytest.raises(MemberEssentialContextError, match="managed"):
        documents_for_member("writer-template", str(project))


def _hard_link_from_store(tmp_path: Path, name: str, body: str, dest: Path) -> Path:
    """Place *dest* as a second hard link to a file in an installer-owned store.

    The shape an installer leaves when it cannot create symbolic links: the
    steering name is a regular file whose inode has another name elsewhere.
    """
    store = tmp_path / "installer-store"
    store.mkdir(exist_ok=True)
    original = store / name
    original.write_text(body, encoding="utf-8")
    dest.parent.mkdir(parents=True, exist_ok=True)
    os.link(original, dest)
    assert os.stat(dest).st_nlink == 2
    return dest


def test_declared_steering_glob_names_a_hard_linked_file_instead_of_refusing(env, tmp_path):
    """A hard-linked steering file under the declared glob does not stop the member.

    The no-follow read still refuses the second name, so the file stays unread;
    it is named in-band and the regular steering beside it loads.
    """
    _declare_steering_glob_only(env)
    linked = _hard_link_from_store(
        tmp_path,
        "style.md",
        "HARDLINKED_STEERING_BODY",
        env.project / ".kiro" / "steering" / "vendor-style.md",
    )
    bodies = _snapshot_bodies(env)
    assert "HARDLINKED_STEERING_BODY" not in bodies
    assert "Always guide: explain assumptions." in bodies
    assert f"[Essential source: {_mec.ESSENTIAL_UNREADABLE_SKIP_SOURCE}:writer-template]" in bodies
    assert str(linked) in bodies


def test_two_templates_leaving_out_different_hard_links_still_build(env, tmp_path):
    """Each template's unreadable-steering note carries its own label.

    Merging an owner and an execution template that each leave out a
    different hard-linked steering file must not trip the
    changed-during-preparation guard.
    """
    steering = env.project / ".kiro" / "steering"
    first = _hard_link_from_store(tmp_path, "a.md", "HARDLINK_A", steering / "a" / "a.md")
    second = _hard_link_from_store(tmp_path, "b.md", "HARDLINK_B", steering / "b" / "b.md")
    agents = env.project / ".kiro" / "agents"
    _write_template(agents, "writer-template", ["file://.kiro/steering/a/*.md"])
    _write_template(agents, "runner-template", ["file://.kiro/steering/b/*.md"])
    _write_kiro_settings(env.project / ".kiro", {"chat.disableInheritingDefaultResources": True})
    message = env.builder._build_v2_essentials(
        env.store,
        member=env.member,
        project=str(env.project),
        provider_type=PROVIDER_ACP,
        execution_template="runner-template",
    )
    assert f"{_mec.ESSENTIAL_UNREADABLE_SKIP_SOURCE}:writer-template" in message
    assert f"{_mec.ESSENTIAL_UNREADABLE_SKIP_SOURCE}:runner-template" in message
    assert str(first) in message and str(second) in message
    assert "HARDLINK_A" not in message and "HARDLINK_B" not in message


def test_implicit_global_steering_names_a_hard_linked_file_instead_of_refusing(env, tmp_path):
    steering = Path.home() / ".kiro" / "steering"
    steering.mkdir(parents=True, exist_ok=True)
    (steering / "plain.md").write_text("PLAIN_STEERING_BODY", encoding="utf-8")
    linked = _hard_link_from_store(
        tmp_path, "rules.md", "HARDLINKED_GLOBAL_BODY", steering / "vendor" / "rules.md"
    )
    message = _member_message(env)
    assert "HARDLINKED_GLOBAL_BODY" not in message
    assert "PLAIN_STEERING_BODY" in message
    assert str(linked) in message
    sources = dict(_mec.kiro_launch_documents("writer-template", str(env.project)))
    assert str(linked) not in sources
    assert "PLAIN_STEERING_BODY" in sources.values()


def test_projected_steering_glob_leaves_a_hard_linked_file_out(env, tmp_path):
    _hard_link_from_store(
        tmp_path,
        "style.md",
        "HARDLINKED_STEERING_BODY",
        env.project / ".kiro" / "steering" / "vendor-style.md",
    )
    definition = {"id": "worker", "resources": ["file://.kiro/steering/**/*.md"]}
    documents = _mec.projected_resource_documents(definition, str(env.project))
    assert "Always guide: explain assumptions." in documents.values()
    assert not any("HARDLINKED_STEERING_BODY" in body for body in documents.values())


def test_declared_non_steering_hard_link_still_refuses(env, tmp_path):
    """Only steering is left out; any other declared source the read refuses still refuses."""
    _hard_link_from_store(
        tmp_path, "guide.md", "HARDLINKED_GUIDE", env.project / "notes" / "guide.md"
    )
    _declare(env, ["file://notes/guide.md"])
    with pytest.raises(MemberEssentialContextError, match="missing, unreadable"):
        _snapshot_bodies(env)


def test_unreadable_steering_note_names_each_file_once():
    first, second = Path("a.md"), Path("b.md")
    assert _mec._skipped_unreadable_note([], "t") is None
    note = _mec._skipped_unreadable_note([first, second, first], "t")
    assert note is not None
    source, body = note
    assert source == f"{_mec.ESSENTIAL_UNREADABLE_SKIP_SOURCE}:t"
    assert body.count(str(first)) == 1 and "2 steering files" in body
