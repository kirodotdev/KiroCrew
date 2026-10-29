"""A steering file with an unrecognised ``inclusion`` value reads as ``always``.

``src/kiro_crew/docs/steering-and-hooks.md`` documents that an absent or misspelled
``inclusion`` reads as ``always``. Every reader in ``member_essential_context`` must
follow it: a bad value is a warning, never a refused member session.

The shared ``env`` fixture is in ``member_essential_context_helpers``.
"""

from pathlib import Path

import member_essential_context_helpers

from kiro_crew import member_essential_context as _mec

_close_skills_loaders = member_essential_context_helpers._close_skills_loaders
env = member_essential_context_helpers.env

_BODY = "UNKNOWN_INCLUSION_BODY"


def _write_steering(root: Path, inclusion: str) -> Path:
    path = root / ".kiro" / "steering" / "odd.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\ninclusion: {inclusion}\n---\n{_BODY}\n", encoding="utf-8")
    return path


def test_member_session_starts_and_loads_the_file(env, caplog):
    odd = _write_steering(Path.home(), "optional")
    with caplog.at_level("WARNING"):
        message, _ = env.builder.build_message(
            "Continue", False, memory_store=env.store, member=env.member, project=str(env.project)
        )
    assert _BODY in message
    assert "Always guide: explain assumptions." in message
    warned = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any(str(odd) in w and "'optional'" in w for w in warned)


def test_projected_resources_read_it_as_always(env):
    _write_steering(env.project, "optional")
    definition = {"id": "worker", "resources": ["file://.kiro/steering/**/*.md"]}
    documents = _mec.projected_resource_documents(definition, str(env.project))
    assert any(_BODY in body for body in documents.values())


def test_launch_documents_keep_it_with_the_always_files(env):
    _write_steering(Path.home(), "optional")
    bodies = [body for _, body in _mec.kiro_launch_documents("writer-template", str(env.project))]
    assert any(_BODY in body for body in bodies)


def test_a_known_conditional_mode_still_stays_out_of_the_launch(env):
    _write_steering(Path.home(), "manual")
    bodies = [body for _, body in _mec.kiro_launch_documents("writer-template", str(env.project))]
    assert not any(_BODY in body for body in bodies)
