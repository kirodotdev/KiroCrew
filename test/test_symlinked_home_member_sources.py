"""Member sources on a host whose home is reached through a symlink.

Cloud desktops spell ``$HOME`` as ``/home/<user>``, a link to
``/local/home/<user>``. Two member-turn readers refused ordinary files there:

* the V2 memory reader compared the opened descriptor's REAL path with the
  requested path's LEXICAL spelling, so every anchor under a link-spelled home
  looked relocated and the read was refused;
* the essential-context reader treated the workspace's implicit ``AGENTS.md`` /
  ``SOUL.md`` like a declared source, so a guide symlinked to a repository (or a
  link whose target was gone) aborted every session start of the agent.

Each fix keeps the refusal it exists for: an escaping link is still not read.
These tests pin both halves -- the admitted layout and the escaping one.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from conftest import requires_symlinks
from kiro_crew.member_essential_context import MemberEssentialContextError, documents_for_member
from kiro_crew.memory import MemoryStore

pytestmark = [
    requires_symlinks,
    pytest.mark.skipif(
        os.name == "nt",
        reason="a linked home ancestor is refused on Windows by design; this is the POSIX layout",
    ),
]


@pytest.fixture
def linked_home(tmp_path, monkeypatch):
    """``<tmp>/home/u`` -> ``<tmp>/local/home/u``, with HOME in the link spelling."""
    real = tmp_path / "local" / "home" / "u"
    real.mkdir(parents=True)
    (tmp_path / "home").mkdir()
    link = tmp_path / "home" / "u"
    link.symlink_to(real, target_is_directory=True)
    monkeypatch.setenv("HOME", str(link))
    monkeypatch.setenv("USERPROFILE", str(link))
    monkeypatch.setenv("KIRO_HOME", str(link / ".kiro"))
    monkeypatch.delenv("KIROCREW_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: link))
    agents = link / ".kiro" / "agents"
    agents.mkdir(parents=True)
    monkeypatch.setattr("kiro_crew.agent.KIRO_AGENTS_DIR", agents)
    monkeypatch.setattr("kiro_crew.agent_discovery._KIRO_AGENTS_DIR", agents)
    return link, real


# ── V2 memory reads ──


def _member_files(link: Path) -> MemoryStore:
    workspace = link / ".kiro" / "crew" / "memory_stores" / "member-a"
    (workspace / "memory" / "history").mkdir(parents=True)
    return MemoryStore(workspace=workspace, memory_version=2)


def test_member_memory_reads_through_a_symlinked_home(linked_home):
    link, _ = linked_home
    files = _member_files(link)
    anchor = files._memory_dir / "preferences.md"
    anchor.write_text("Preference anchor: keep drafts short.", encoding="utf-8")
    dated = files._history_dir / "2026-10-01.md"
    dated.write_text("A day of work.", encoding="utf-8")

    assert files._guarded_entry(anchor, require_readable=True)["content"].startswith(
        "Preference anchor"
    )
    assert files._guarded_entry(dated, require_readable=True)["content"] == "A day of work."


def test_member_memory_still_refuses_a_link_under_a_symlinked_home(linked_home, tmp_path):
    """Only the root's spelling is normalized: every link below it still refuses."""
    link, real = linked_home
    files = _member_files(link)
    memory_dir = files._memory_dir
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("OUTSIDE_SECRET", encoding="utf-8")
    # A directory link leaving the store.
    (memory_dir / "escape").symlink_to(outside, target_is_directory=True)
    # A directory link that stays inside the store but is still a link.
    (memory_dir / "real-sub").mkdir()
    (memory_dir / "real-sub" / "note.md").write_text("INSIDE_VIA_LINK", encoding="utf-8")
    (memory_dir / "alias").symlink_to(memory_dir / "real-sub", target_is_directory=True)
    # A leaf link and a hard link.
    (memory_dir / "leaf.md").symlink_to(outside / "secret.md")
    (outside / "hard-src.md").write_text("HARDLINK_SECRET", encoding="utf-8")
    os.link(outside / "hard-src.md", memory_dir / "hard.md")

    for path in (
        memory_dir / "escape" / "secret.md",
        memory_dir / "alias" / "note.md",
        memory_dir / "leaf.md",
        memory_dir / "hard.md",
    ):
        with pytest.raises(OSError):
            files._read_entry_bytes(path)
        assert files._guarded_entry(path)["content"] == ""
        with pytest.raises(OSError, match="Memory read refused"):
            files._guarded_entry(path, require_readable=True)


# ── Implicit workspace AGENTS.md / SOUL.md ──


def _workspace(link: Path) -> Path:
    from kiro_crew.config import config_dir

    workspace = config_dir() / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (link / ".kiro" / "agents" / "aim-agent.json").write_text(
        json.dumps({"name": "aim-agent", "prompt": "AIM persona."}), encoding="utf-8"
    )
    return workspace


def _bodies(documents: list[tuple[str, str]]) -> str:
    return "\n".join(body for _, body in documents)


@pytest.mark.parametrize("spelling", ["link", "real"])
@pytest.mark.parametrize("target", ["relative", "link-spelled", "real-spelled"])
def test_workspace_guide_linked_inside_the_workspace_is_read(linked_home, spelling, target):
    link, real = linked_home
    workspace = _workspace(link)
    (workspace / "guides").mkdir()
    (workspace / "guides" / "AGENTS.md").write_text("LINKED_GUIDE", encoding="utf-8")
    destination = {
        "relative": Path("guides/AGENTS.md"),
        "link-spelled": workspace / "guides" / "AGENTS.md",
        "real-spelled": Path(os.path.realpath(workspace)) / "guides" / "AGENTS.md",
    }[target]
    (workspace / "AGENTS.md").symlink_to(destination)
    project = str(workspace) if spelling == "link" else os.path.realpath(workspace)

    documents = documents_for_member("aim-agent", project)

    assert "LINKED_GUIDE" in _bodies(documents)
    assert not any(source.endswith("#omitted") for source, _ in documents)


@pytest.mark.parametrize("spelling", ["link", "real"])
@pytest.mark.parametrize("name", ["AGENTS.md", "SOUL.md"])
@pytest.mark.parametrize("shape", ["repo-link", "dangling", "managed", "hardlink"])
def test_unreadable_workspace_guide_is_omitted_not_fatal(linked_home, spelling, name, shape):
    """The session starts; the escaping or unreadable guide is named, never read."""
    link, real = linked_home
    workspace = _workspace(link)
    guide = workspace / name
    if shape == "repo-link":
        (real / "repo").mkdir()
        (real / "repo" / name).write_text("REPO_SECRET", encoding="utf-8")
        guide.symlink_to(link / "repo" / name)
    elif shape == "dangling":
        guide.symlink_to("removed-by-update.md")
    elif shape == "managed":
        (workspace / "memory").mkdir()
        (workspace / "memory" / "preferences.md").write_text("MANAGED_SECRET", encoding="utf-8")
        guide.symlink_to(workspace / "memory" / "preferences.md")
    else:
        (real / "elsewhere.md").write_text("HARDLINK_SECRET", encoding="utf-8")
        os.link(real / "elsewhere.md", guide)
    project = str(workspace) if spelling == "link" else os.path.realpath(workspace)

    documents = documents_for_member("aim-agent", project)

    text = _bodies(documents)
    for secret in ("REPO_SECRET", "MANAGED_SECRET", "HARDLINK_SECRET"):
        assert secret not in text
    omitted = [(source, body) for source, body in documents if source.endswith("#omitted")]
    assert len(omitted) == 1
    assert omitted[0][0].startswith(str(Path(os.path.realpath(workspace)) / name))
    assert name in omitted[0][1] and "NOT LOADED" in omitted[0][1]
    assert "AIM persona." in text


def test_declared_resource_linked_outside_still_refuses(linked_home):
    """A source the template DECLARES keeps failing closed; only implicit guides degrade."""
    link, real = linked_home
    workspace = _workspace(link)
    (real / "repo").mkdir()
    (real / "repo" / "AGENTS.md").write_text("REPO_SECRET", encoding="utf-8")
    (workspace / "AGENTS.md").symlink_to(link / "repo" / "AGENTS.md")
    (link / ".kiro" / "agents" / "aim-agent.json").write_text(
        json.dumps({"name": "aim-agent", "prompt": "P", "resources": ["file://AGENTS.md"]}),
        encoding="utf-8",
    )

    with pytest.raises(MemberEssentialContextError, match="outside the admitted document root"):
        documents_for_member("aim-agent", str(workspace))
