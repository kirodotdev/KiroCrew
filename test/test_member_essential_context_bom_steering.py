"""A steering file saved as "UTF-8 with BOM" keeps its declared ``inclusion``.

Notepad and Windows PowerShell write a leading byte-order mark. The member path
must read past it, so the frontmatter fence is still at column 0 and a manual
file stays out of member essentials, exactly as it does without the mark.

The shared ``env`` fixture is in ``member_essential_context_helpers``.
"""

from pathlib import Path

import member_essential_context_helpers

from kiro_crew import member_essential_context as _mec
from kiro_crew.frontmatter import STEERING_LOADER, split_frontmatter

_close_skills_loaders = member_essential_context_helpers._close_skills_loaders
env = member_essential_context_helpers.env

_BOM = b"\xef\xbb\xbf"
_BODY = "BOM_STEERING_BODY"


def _write_steering(root: Path, inclusion: str, *, bom: bool, name: str = "guide.md") -> Path:
    path = root / ".kiro" / "steering" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    text = f"---\ninclusion: {inclusion}\ndescription: on request only\n---\n{_BODY}\n"
    path.write_bytes((_BOM if bom else b"") + text.encode("utf-8"))
    return path


def _member_sources(env) -> list[str]:
    documents = _mec.documents_for_member("writer-template", str(env.project))
    return [str(source) for source, _ in documents]


def test_a_bom_file_reads_its_declared_inclusion(env):
    path = _write_steering(Path.home(), "manual", bom=True)
    fields, _ = split_frontmatter(_mec._read(path, Path.home()), STEERING_LOADER)
    assert fields.get("inclusion") == "manual"


def test_a_bom_manual_file_stays_out_of_member_essentials(env):
    path = _write_steering(Path.home(), "manual", bom=True)
    assert str(path) not in _member_sources(env)


def test_a_bom_manual_file_stays_out_of_the_launch(env):
    _write_steering(Path.home(), "manual", bom=True)
    bodies = [body for _, body in _mec.kiro_launch_documents("writer-template", str(env.project))]
    assert not any(_BODY in body for body in bodies)


def test_a_bom_manual_file_is_indexed_as_conditional_in_projected_resources(env):
    _write_steering(env.project, "manual", bom=True)
    definition = {"id": "worker", "resources": ["file://.kiro/steering/**/*.md"]}
    documents = _mec.projected_resource_documents(definition, str(env.project))
    assert not any(_BODY in body for body in documents.values())


def test_a_bom_always_file_loads_without_the_mark(env):
    path = _write_steering(Path.home(), "always", bom=True)
    body = _mec._read(path, Path.home())
    assert not body.startswith("\ufeff")
    assert str(path) in _member_sources(env)


def test_files_without_a_bom_are_unchanged(env):
    manual = _write_steering(Path.home(), "manual", bom=False, name="manual.md")
    always = _write_steering(Path.home(), "always", bom=False, name="always.md")
    assert _mec._read(always, Path.home()).encode("utf-8") == always.read_bytes()
    sources = _member_sources(env)
    assert str(manual) not in sources
    assert str(always) in sources
