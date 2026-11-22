"""An agent-config rebuild that changes nothing leaves the sandbox's pinned files alone.

Each sandboxed spawn pins the crew-home read-only file leaves, and the launcher refuses
to start when the object under a pinned name changed between the pass that saw it and
the pin. A rename-in-place write gives a file a new inode even for the same bytes, so a
rebuild that rewrote ``agent_model_state.json`` with unchanged content refused every
hook or session that started in that second.

The witness is the rename itself, recorded at ``os.replace`` / ``os.rename``: comparing
inode numbers before and after is not one, because a filesystem may hand a freed inode
number straight back to the next temp file, so two replaces can end on the original
number.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from kiro_crew import agent_state
from kiro_crew.unchanged_file import already_holds

posix_only = pytest.mark.skipif(os.name == "nt", reason="the skip is POSIX-only by design")


@pytest.fixture
def replaced(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every by-name rename destination, resolved, for as long as the test runs."""
    seen: list[str] = []
    real_replace, real_rename = os.replace, os.rename

    def _record(dst: object, kwargs: dict) -> None:
        if kwargs.get("dst_dir_fd") is None:
            seen.append(os.path.realpath(os.fspath(dst)))  # type: ignore[arg-type]

    def _replace(src, dst, **kwargs):
        _record(dst, kwargs)
        return real_replace(src, dst, **kwargs)

    def _rename(src, dst, **kwargs):
        _record(dst, kwargs)
        return real_rename(src, dst, **kwargs)

    monkeypatch.setattr(os, "replace", _replace)
    monkeypatch.setattr(os, "rename", _rename)
    return seen


def _real(path: Path) -> str:
    return os.path.realpath(path)


@posix_only
class TestAlreadyHolds:
    def test_same_bytes_owner_only_is_held(self, tmp_path: Path) -> None:
        path = tmp_path / "f.json"
        path.write_bytes(b"{}\n")
        os.chmod(path, 0o600)
        assert already_holds(path, b"{}\n", mode=0o600)
        assert already_holds(path, b"{}\n")

    @pytest.mark.parametrize("payload", [b"{} \n", b"{}\n\n", b"[]\n"])
    def test_other_bytes_are_not_held(self, tmp_path: Path, payload: bytes) -> None:
        path = tmp_path / "f.json"
        path.write_bytes(b"{}\n")
        os.chmod(path, 0o600)
        assert not already_holds(path, payload)

    def test_a_mode_the_writer_would_change_is_not_held(self, tmp_path: Path) -> None:
        path = tmp_path / "f.json"
        path.write_bytes(b"{}\n")
        os.chmod(path, 0o644)
        assert not already_holds(path, b"{}\n")
        os.chmod(path, 0o400)
        assert not already_holds(path, b"{}\n", mode=0o600)

    def test_a_hard_link_or_symlink_is_not_held(self, tmp_path: Path) -> None:
        target = tmp_path / "target.json"
        target.write_bytes(b"{}\n")
        os.chmod(target, 0o600)
        link = tmp_path / "link.json"
        link.symlink_to(target)
        assert not already_holds(link, b"{}\n")
        os.link(target, tmp_path / "second.json")
        assert not already_holds(target, b"{}\n")

    def test_an_absent_file_is_not_held(self, tmp_path: Path) -> None:
        assert not already_holds(tmp_path / "absent.json", b"{}\n")


@posix_only
class TestTheSidecarIsNotReplacedForNothing:
    def test_an_unchanged_write_does_not_replace_it(self, replaced: list[str]) -> None:
        agent_state.set_cc_model("kirocrew-heartbeat", "auto")
        replaced.clear()
        agent_state.set_cc_model("kirocrew-heartbeat", "auto")
        assert _real(agent_state._state_path()) not in replaced

    def test_a_changed_write_still_lands(self, replaced: list[str]) -> None:
        agent_state.set_cc_model("kirocrew-heartbeat", "auto")
        replaced.clear()
        agent_state.set_cc_model("kirocrew-heartbeat", "other")
        assert _real(agent_state._state_path()) in replaced
        assert agent_state.get_cc_model("kirocrew-heartbeat") == "other"

    def test_an_unchanged_managed_refresh_writes_nothing(self, replaced: list[str]) -> None:
        agent_state.begin_managed_write("owned", "d1")
        agent_state.finalize_managed_write("owned")
        replaced.clear()
        agent_state.begin_managed_write("owned", "d1", current="d1")
        agent_state.finalize_managed_write("owned")
        assert _real(agent_state._state_path()) not in replaced
        assert agent_state.managed_digest_matches("owned", "d1")

    def test_a_stale_pending_is_overwritten_not_promoted(self) -> None:
        agent_state.begin_managed_write("owned", "d1")
        agent_state.finalize_managed_write("owned")
        agent_state.begin_managed_write("owned", "stale", current="d1")
        # The replace for "stale" never landed; the next refresh writes d1's bytes again.
        agent_state.begin_managed_write("owned", "d1", current="d1")
        agent_state.finalize_managed_write("owned")
        assert agent_state.managed_digest_matches("owned", "d1")
        assert not agent_state.managed_digest_matches("owned", "stale")


@posix_only
def test_an_unchanged_spec_write_does_not_replace_it(tmp_path: Path, replaced: list[str]) -> None:
    from kiro_crew import agent

    path = tmp_path / "spec.json"
    data = {"name": "x", "tools": ["a"]}
    agent._atomic_json_write_unguarded(path, data)
    replaced.clear()
    agent._atomic_json_write_unguarded(path, data)
    assert _real(path) not in replaced
    agent._atomic_json_write_unguarded(path, {**data, "tools": ["b"]})
    assert _real(path) in replaced
    assert json.loads(path.read_text(encoding="utf-8"))["tools"] == ["b"]


@posix_only
def test_an_unchanged_rebuild_replaces_no_pinned_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, replaced: list[str]
) -> None:
    """No crew-home hidden or read-only file leaf is renamed over by a no-op rebuild."""
    from kiro_crew import agent
    from kiro_crew.config.paths import config_dir
    from kiro_crew.sandbox_leaves import _CREW_HIDDEN_LEAVES, _CREW_READONLY_LEAVES
    from kiro_crew.sandbox_precreate import _CREW_PRECREATE_READONLY_FILE_LEAVES

    monkeypatch.setenv("KIRO_HOME", str(tmp_path / "kiro"))
    # The suite pins the sidecar to its own dir; put it back in the data home, where
    # the sandbox pins it, so the whole leaf set is checked in one place.
    monkeypatch.setattr(agent_state, "config_dir", config_dir)

    agent.rebuild_agent_config()
    agent.rebuild_agent_config()
    home = config_dir()
    pinned = {
        _real(home / leaf)
        for leaf in {
            *_CREW_HIDDEN_LEAVES,
            *_CREW_READONLY_LEAVES,
            *_CREW_PRECREATE_READONLY_FILE_LEAVES,
        }
        if (home / leaf).is_file()
    }
    assert _real(home / "agent_model_state.json") in pinned
    # The first rebuild created the sidecar through a rename, so the recorder sees them.
    assert _real(home / "agent_model_state.json") in replaced
    replaced.clear()
    agent.rebuild_agent_config()
    assert sorted(pinned.intersection(replaced)) == []
