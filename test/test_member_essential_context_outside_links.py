"""An absolute declaration whose glob crosses a link out of home is left out, not refused.

On a cloud desktop a folder under home, such as ``~/.tools``, can be a link to another
volume. kiro-cli follows it; the member snapshot never reads outside home, so the link
is left out unread and named in-band, and the member still starts. A link into
managed memory state, a relative non-steering declaration, and the launch-record size
check keep refusing.
"""

from pathlib import Path

import member_essential_context_helpers
import pytest

from conftest import make_dir_link
from kiro_crew import member_essential_context as _mec
from kiro_crew.member_essential_context import (
    MemberEssentialContextError,
    documents_for_member,
    projected_resource_documents,
)

# Fixtures shared with the sibling modules, bound by assignment so pytest registers
# them for the tests in this module.
_close_skills_loaders = member_essential_context_helpers._close_skills_loaders
env = member_essential_context_helpers.env

_TEMPLATE = "linked-agent"


def _link_tools_to_volume(tmp_path: Path) -> Path:
    """Make ``~/.tools`` a link to a volume outside home holding installed guides."""
    volume = tmp_path / "linked-volume" / ".tools"
    steering = volume / "packages" / "Pkg-1.0" / "steering"
    steering.mkdir(parents=True)
    (steering / "guide.md").write_text("OUTSIDE_VOLUME_BODY", encoding="utf-8")
    link = Path.home() / ".tools"
    make_dir_link(link, volume)
    return link


def _write_global_template(resources: list[str]) -> None:
    agents = Path.home() / ".kiro" / "agents"
    agents.mkdir(parents=True, exist_ok=True)
    member_essential_context_helpers._write_template(agents, _TEMPLATE, resources)


def _home_guide() -> str:
    guide = Path.home() / "notes" / "home-guide.md"
    guide.parent.mkdir(parents=True, exist_ok=True)
    guide.write_text("HOME_GUIDE_BODY", encoding="utf-8")
    return f"file://{guide}"


@pytest.mark.parametrize(
    "declared",
    [
        ".tools/packages/*/steering/*.md",
        ".tools/**/*.md",
        ".tools/packages/Pkg-1.0/*/guide.md",
        # The link met by a wildcard component instead of a literal one.
        ".to*/packages/*/steering/*.md",
    ],
)
def test_absolute_glob_through_a_link_out_of_home_is_named_not_read(env, tmp_path, declared):
    link = _link_tools_to_volume(tmp_path)
    _write_global_template([f"file://{Path.home() / declared}", _home_guide()])

    documents = documents_for_member(_TEMPLATE, None)

    bodies = "\n".join(body for _, body in documents)
    assert "OUTSIDE_VOLUME_BODY" not in bodies
    assert "HOME_GUIDE_BODY" in bodies
    notes = dict(documents)
    note = notes.get(f"{_mec.ESSENTIAL_OUTSIDE_SKIP_SOURCE}:{_TEMPLATE}")
    assert note is not None, sorted(notes)
    assert str(link) in note
    assert "left out unread" in note


def test_projected_reader_leaves_the_same_link_out(env, tmp_path):
    _link_tools_to_volume(tmp_path)
    definition = {"resources": [f"file://{Path.home()}/.tools/packages/*/steering/*.md"]}
    assert projected_resource_documents(definition, str(env.project)) == {}


def test_a_link_out_of_home_into_the_data_home_still_refuses(env):
    """Leaving a link out must not turn another member's store into a quiet skip."""
    from kiro_crew.config import config_dir

    peer_store = config_dir() / "memory_stores" / "peer-member"
    peer_store.mkdir(parents=True, exist_ok=True)
    (peer_store / "secret.md").write_text("PEER_MEMORY_BODY", encoding="utf-8")
    make_dir_link(Path.home() / ".tools", peer_store)
    _write_global_template([f"file://{Path.home()}/.tools/*.md"])

    with pytest.raises(MemberEssentialContextError, match=r"\.tools"):
        documents_for_member(_TEMPLATE, None)


def test_a_link_out_of_home_into_an_admitted_managed_root_still_refuses(env, tmp_path, monkeypatch):
    """A managed root the path screen admits is refused by the managed-state check.

    The data home is already refused by the path screen; a managed root elsewhere
    (an admin root on another volume) reaches the walk's own managed-state check,
    which must stay a refusal rather than a named skip.
    """
    managed = tmp_path / "other-volume" / "crew-data"
    managed.mkdir(parents=True)
    (managed / "notes.md").write_text("MANAGED_BODY", encoding="utf-8")
    assert _mec.validate_file_path(str(managed)) is not None
    real_table = _mec._managed_root_table

    def _with_extra_admin_root():
        roots, workspaces, resolved = real_table()
        resolved = {**resolved, managed: _mec._comparable_root(managed)}
        return [*roots, managed], workspaces, resolved

    monkeypatch.setattr(_mec, "_managed_root_table", _with_extra_admin_root)
    make_dir_link(Path.home() / ".tools", managed)
    _write_global_template([f"file://{Path.home()}/.tools/*.md"])

    with pytest.raises(MemberEssentialContextError, match="managed"):
        documents_for_member(_TEMPLATE, None)


def test_a_relative_glob_through_a_link_out_of_the_project_still_refuses(env, tmp_path):
    volume = tmp_path / "outside-volume"
    volume.mkdir()
    (volume / "guide.md").write_text("OUTSIDE_VOLUME_BODY", encoding="utf-8")
    make_dir_link(env.project / "vendor", volume)
    project_agents = env.project / ".kiro" / "agents"
    member_essential_context_helpers._write_template(
        project_agents, "writer-template", ["file://vendor/*.md"]
    )

    with pytest.raises(MemberEssentialContextError, match="linked document or directory"):
        documents_for_member("writer-template", str(env.project))


def test_an_absolute_glob_through_a_link_inside_home_still_refuses(env, tmp_path):
    """Only a link that leaves home is left out; one that stays inside keeps its refusal."""
    inside = Path.home() / "real-aim"
    inside.mkdir()
    (inside / "guide.md").write_text("INSIDE_BODY", encoding="utf-8")
    make_dir_link(Path.home() / ".tools", inside)
    _write_global_template([f"file://{Path.home()}/.tools/*.md"])

    with pytest.raises(MemberEssentialContextError, match="linked document or directory"):
        documents_for_member(_TEMPLATE, None)


def test_the_launch_size_check_still_cannot_measure_the_link(env, tmp_path):
    _link_tools_to_volume(tmp_path)
    definition = {"resources": [f"file://{Path.home()}/.tools/packages/*/steering/*.md"]}
    assert _mec._declared_files_fit(definition, str(env.project)) is False


def test_outside_note_names_each_path_once():
    path = Path("/home/u/.tools")
    note = _mec._outside_declared_note([path, path], "t")
    assert note is not None
    assert note[0] == f"{_mec.ESSENTIAL_OUTSIDE_SKIP_SOURCE}:t"
    assert note[1].count(str(path)) == 1
    assert "1 path the agent template declares" in note[1]
    assert _mec._outside_declared_note([], "t") is None
