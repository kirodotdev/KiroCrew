"""An unrecognised steering ``inclusion:`` value on the member paths reads as ``always``.

``src/kiro_crew/docs/steering-and-hooks.md`` documents that an absent or misspelled
``inclusion`` reads as ``always``, and the non-member reader (``folder_steering``)
already loads such a document. The member snapshot and the projected KAS document
reader do the same rather than refuse the whole member session.
"""

from pathlib import Path

import member_essential_context_helpers
import pytest
from member_essential_context_helpers import _snapshot_bodies

from kiro_crew import member_essential_context as _mec

# Fixtures shared with the sibling modules, bound by assignment so pytest registers
# them for the tests in this module.
_close_skills_loaders = member_essential_context_helpers._close_skills_loaders
env = member_essential_context_helpers.env

_UNKNOWN_INCLUSIONS = ("optional", "Optional", "filematchh")


def _unknown_steering(directory: Path, inclusion: str) -> str:
    """Write one steering document declaring *inclusion*; return its body marker."""
    marker = f"UNKNOWN_INCLUSION_BODY_{inclusion}"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "unknown.md").write_text(
        f"---\ninclusion: {inclusion}\n---\n{marker}", encoding="utf-8"
    )
    return marker


@pytest.mark.parametrize("inclusion", _UNKNOWN_INCLUSIONS)
def test_member_snapshot_loads_global_steering_with_an_unknown_inclusion(env, inclusion):
    """The reported case: a global steering file an installer wrote with ``optional``."""
    marker = _unknown_steering(Path.home() / ".kiro" / "steering", inclusion)
    bodies = _snapshot_bodies(env)
    assert marker in bodies
    assert "Always guide: explain assumptions." in bodies
    assert "MANUAL_SECRET" not in bodies


@pytest.mark.parametrize("inclusion", _UNKNOWN_INCLUSIONS)
def test_member_snapshot_loads_project_steering_with_an_unknown_inclusion(env, inclusion):
    marker = _unknown_steering(env.project / ".kiro" / "steering", inclusion)
    bodies = _snapshot_bodies(env)
    assert marker in bodies
    assert "MANUAL_SECRET" not in bodies


@pytest.mark.parametrize("inclusion", _UNKNOWN_INCLUSIONS)
def test_kiro_launch_documents_keep_global_steering_with_an_unknown_inclusion(env, inclusion):
    """The model of kiro-cli's own implicit scan reads the value the same way."""
    marker = _unknown_steering(Path.home() / ".kiro" / "steering", inclusion)
    bodies = [body for _, body in _mec.kiro_launch_documents("writer-template", str(env.project))]
    assert any(marker in body for body in bodies)
    assert not any("MANUAL_SECRET" in body for body in bodies)


@pytest.mark.parametrize("inclusion", _UNKNOWN_INCLUSIONS)
def test_projected_documents_load_steering_with_an_unknown_inclusion(env, inclusion):
    """The KAS document reader keeps the same rule as the member snapshot."""
    marker = _unknown_steering(env.project / ".kiro" / "steering", inclusion)
    definition = {"id": "worker", "resources": ["file://.kiro/steering/**/*.md"]}
    documents = _mec.projected_resource_documents(definition, str(env.project))
    # The stored body keeps its front matter, so match by containment.
    assert any(marker in body for body in documents.values())
    assert "Always guide: explain assumptions." in documents.values()
    assert not any("MANUAL_SECRET" in body for body in documents.values())
