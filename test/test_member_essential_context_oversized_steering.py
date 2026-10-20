"""Steering over the per-file read cap is left out and named, never fatal to startup.

Global and project steering are read because they exist, so one oversized file must
not stop every member from starting. It is left out unread, whatever its inclusion
mode, and an in-band note names the file, its size and the cap. Declared non-steering
sources and a project ``AGENTS.md`` over the cap keep refusing, now with the file, its
size and the cap in the message instead of a cap rounded down to ``0 MB``.

The shared ``env`` fixture is in ``member_essential_context_helpers``.
"""

from pathlib import Path

import member_essential_context_helpers
import pytest
from member_essential_context_helpers import _declare_steering_glob_only, _snapshot_bodies

from kiro_crew import member_essential_context as _mec
from kiro_crew.hooks import safe_read_file_bytes_nolink
from kiro_crew.member_essential_context import MemberEssentialContextError

_close_skills_loaders = member_essential_context_helpers._close_skills_loaders
env = member_essential_context_helpers.env

_CAP = _mec._MAX_SOURCE_BYTES
_SIZE = _CAP + 9_585  # the reported 265,585-byte file


def _member_message(env) -> str:
    message, _ = env.builder.build_message(
        "Continue", False, memory_store=env.store, member=env.member, project=str(env.project)
    )
    return message


def _write_oversized(path: Path, inclusion: str | None) -> Path:
    head = f"---\ninclusion: {inclusion}\n---\n" if inclusion else ""
    path.parent.mkdir(parents=True, exist_ok=True)
    body = head + "OVERSIZED_STEERING_HEAD\n"
    path.write_bytes((body + "z" * (_SIZE - len(body))).encode("utf-8"))
    assert path.stat().st_size == _SIZE
    return path


def _plain_global_steering() -> None:
    steering = Path.home() / ".kiro" / "steering"
    steering.mkdir(parents=True, exist_ok=True)
    (steering / "plain.md").write_text("PLAIN_STEERING_BODY", encoding="utf-8")


@pytest.mark.parametrize("inclusion", [None, "always", "auto", "manual", "fileMatch"])
def test_oversized_global_steering_is_named_and_the_member_starts(env, inclusion, caplog):
    _plain_global_steering()
    big = _write_oversized(Path.home() / ".kiro" / "steering" / "big.md", inclusion)
    with caplog.at_level("WARNING"):
        message = _member_message(env)
    assert "OVERSIZED_STEERING_HEAD" not in message
    assert "PLAIN_STEERING_BODY" in message
    assert "Always guide: explain assumptions." in message
    assert f"[Essential source: {_mec.ESSENTIAL_OVERSIZED_SKIP_SOURCE}:writer-template]" in message
    assert str(big) in message
    assert f"{_SIZE:,} bytes" in message and f"{_CAP:,}-byte" in message
    assert "0 MB" not in message
    warned = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any(str(big) in w and f"{_SIZE:,} bytes" in w for w in warned)


def test_oversized_project_steering_is_named_and_the_member_starts(env):
    big = _write_oversized(env.project / ".kiro" / "steering" / "big.md", None)
    message = _member_message(env)
    assert "OVERSIZED_STEERING_HEAD" not in message
    assert "Always guide: explain assumptions." in message
    assert str(big) in message


def test_declared_steering_glob_names_an_oversized_file(env):
    _declare_steering_glob_only(env)
    big = _write_oversized(env.project / ".kiro" / "steering" / "big.md", None)
    bodies = _snapshot_bodies(env)
    assert "OVERSIZED_STEERING_HEAD" not in bodies
    assert "Always guide: explain assumptions." in bodies
    assert str(big) in bodies


def test_kiro_launch_documents_leaves_oversized_global_steering_out(env):
    """The spawn-time native list must not refuse the session start either."""
    _plain_global_steering()
    big = _write_oversized(Path.home() / ".kiro" / "steering" / "big.md", None)
    sources = dict(_mec.kiro_launch_documents("writer-template", str(env.project)))
    assert str(big) not in sources
    assert "PLAIN_STEERING_BODY" in sources.values()


def test_projected_steering_glob_leaves_an_oversized_file_out(env):
    _write_oversized(env.project / ".kiro" / "steering" / "big.md", None)
    definition = {"id": "worker", "resources": ["file://.kiro/steering/**/*.md"]}
    documents = _mec.projected_resource_documents(definition, str(env.project))
    assert "Always guide: explain assumptions." in documents.values()
    assert not any("OVERSIZED_STEERING_HEAD" in body for body in documents.values())


def test_oversized_declared_non_steering_source_still_refuses_and_names_the_file(env):
    guide = _write_oversized(env.project / "declared-guide.md", None)
    with pytest.raises(MemberEssentialContextError) as raised:
        _member_message(env)
    text = str(raised.value)
    assert str(guide.name) in text
    assert f"{_SIZE:,} bytes" in text and f"{_CAP:,}-byte" in text
    assert "0 MB" not in text


def test_oversized_project_agents_md_still_refuses_and_names_the_file(env):
    """A project guide the user can shorten keeps refusing, with a clear reason."""
    _write_oversized(env.project / "AGENTS.md", None)
    with pytest.raises(MemberEssentialContextError) as raised:
        _member_message(env)
    text = str(raised.value)
    assert "AGENTS.md" in text and f"{_SIZE:,} bytes" in text


def test_nolink_read_names_the_size_and_cap_in_bytes(tmp_path):
    from kiro_crew.hooks import FileTooLargeError

    target = tmp_path / "big.md"
    target.write_bytes(b"x" * 1_001)
    with pytest.raises(FileTooLargeError) as raised:
        safe_read_file_bytes_nolink(str(target), max_bytes=1_000)
    assert str(raised.value) == "File is 1,001 bytes, over the 1,000-byte safety cap"


def test_oversized_note_names_each_file_once():
    first, second = Path("a.md"), Path("b.md")
    assert _mec._skipped_oversized_note([], "t") is None
    note = _mec._skipped_oversized_note([(first, 10), (second, 20), (first, 10)], "t")
    assert note is not None
    source, body = note
    assert source == f"{_mec.ESSENTIAL_OVERSIZED_SKIP_SOURCE}:t"
    assert body.count(str(first)) == 1 and "2 steering files" in body


def test_read_error_carries_the_size_the_read_measured(tmp_path):
    from kiro_crew.hooks import FileTooLargeError

    target = tmp_path / "big.md"
    target.write_bytes(b"x" * 1_001)
    with pytest.raises(FileTooLargeError) as raised:
        safe_read_file_bytes_nolink(str(target), max_bytes=1_000)
    assert raised.value.size == 1_001
    assert FileTooLargeError("no size").size is None
