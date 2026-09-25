"""The notification settings stay fenced from the agent at BOTH gates, and non-vacuously.

``notification_settings.json`` holds ``deliver_to`` -- the bit that authorizes the
notification bridge to send a note off the host as an owner DM on a chat transport. A
prompt-injected agent that could write it would arm egress the owner never consented to,
and the note would leave at the gateway's next start.

Two gates, because one of them alone is not a fence:

* ``security._CREW_SECRET_LEAVES`` refuses the agent's own file tools (read AND write);
* ``sandbox._CREW_HIDDEN_LEAVES`` bind-masks it for every sandboxed process, because a
  spawned shell's ``open()`` never passes through the tool gate.

And a mount target, because ``mount(2)`` cannot mask a path that does not exist and the
crew data-home root is writable in every sandbox. Absent is not an edge case for this
leaf -- it is the state of every install that has never saved a channel setting -- so
without the pre-spawn stub the mask would be vacuous on exactly the hosts nobody has
configured.
"""

from __future__ import annotations

import inspect
import json
import os
import re
import sys

import pytest

from kiro_crew import sandbox, security
from kiro_crew.notifications import settings as notification_settings

_POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="POSIX launcher only")
_MODES = ("standard", "cc", "strict")
_CREW_PREFIXES = (".kiro/crew", ".kirocrew")


def _crew_path(prefix: str, leaf: str) -> str:
    """Spell a crew-home target the way the production builders do (single relative join)."""
    return os.path.join(os.path.expanduser("~"), f"{prefix}/{leaf}")


def _launcher_files(mode: str) -> set[str]:
    script = sandbox._build_launcher_script(mode)
    match = re.search(r"SENSITIVE_FILES = (\[.*?\])\n", script, re.S)
    assert match, "SENSITIVE_FILES missing from the launcher"
    return set(json.loads(match.group(1)))


@pytest.fixture()
def crew_home(tmp_path, monkeypatch):
    """An isolated crew data home, so no test here touches the developer's own settings."""
    home = tmp_path / ".kiro" / "crew"
    home.mkdir(parents=True)
    monkeypatch.setattr(sandbox, "config_dir", lambda: home)
    monkeypatch.setattr(sandbox.Path, "home", staticmethod(lambda: tmp_path))
    return home


class TestTheLiteralsCannotDrift:
    """``sandbox.py`` spells the leaf itself rather than importing the settings module --
    it is a low-level module and that import drags in the config loader. A test-time
    import costs nothing, so the spelling is pinned here instead: a rename on either side
    reddens rather than silently unmasking the file."""

    def test_the_leaf_matches_the_settings_filename(self) -> None:
        assert sandbox._NOTIFICATION_SETTINGS_LEAF == notification_settings._SETTINGS_FILENAME

    def test_the_staging_leaf_matches_the_writer(self) -> None:
        assert sandbox._NOTIFICATION_SETTINGS_STAGING_LEAF == notification_settings._STAGING_LEAF


class TestTheStagingDirectoryIsMaskedToo:
    """The leaf mask covers the settings file's NAME, never a sibling temp.

    ``atomic_write`` stages in the target's parent -- the data-home root, writable and
    visible in every sandbox -- so the owner's own PUT wrote the real ``deliver_to`` bytes
    to an unmasked name, where a same-UID sandbox could hold the temp's descriptor across
    the rename. The writer stages in a masked directory instead, and these pin that the
    directory is masked, exists before any spawn, and is where the write actually goes.
    """

    def test_the_staging_directory_is_hidden(self) -> None:
        assert sandbox._NOTIFICATION_SETTINGS_STAGING_LEAF in sandbox._CREW_HIDDEN_LEAVES

    def test_the_staging_directory_is_precreated(self) -> None:
        """A lazily-created staging dir offers the isdir-guarded mask loop no name, and the
        directory the gateway creates later -- mid-publish, with the real bytes in it --
        would appear inside an already-running namespace."""
        assert (
            sandbox._NOTIFICATION_SETTINGS_STAGING_LEAF in sandbox._CREW_PRECREATE_HIDDEN_DIR_LEAVES
        )

    @pytest.mark.parametrize("mode", _MODES)
    @pytest.mark.parametrize("prefix", _CREW_PREFIXES)
    @_POSIX_ONLY
    def test_the_launcher_masks_the_staging_directory_in_every_mode(
        self, mode: str, prefix: str
    ) -> None:
        script = sandbox._build_launcher_script(mode)
        match = re.search(r"SENSITIVE_DIRS = (\[.*?\])\n", script, re.S)
        assert match, "SENSITIVE_DIRS missing from the launcher"
        hidden = set(json.loads(match.group(1)))
        assert _crew_path(prefix, sandbox._NOTIFICATION_SETTINGS_STAGING_LEAF) in hidden

    def test_the_write_publishes_from_the_staging_directory(self, tmp_path, monkeypatch) -> None:
        """The discriminating assertion: with the previous ``atomic_write`` call the temp's
        parent was the data-home ROOT, so this is what tells the fix from its predecessor."""
        monkeypatch.setattr(notification_settings, "config_dir", lambda: tmp_path)
        seen: list[str] = []
        real_replace = notification_settings.replace_with_retry

        def _watch(src, dst):
            seen.append(str(src.parent))
            return real_replace(src, dst)

        monkeypatch.setattr(notification_settings, "replace_with_retry", _watch)
        store = notification_settings.ChannelSettings()
        store.update("system.agent", deliver_to=["slack"])

        assert seen == [str(tmp_path / notification_settings._STAGING_LEAF)]
        assert (tmp_path / notification_settings._SETTINGS_FILENAME).exists()

    def test_the_data_home_root_holds_no_temp_after_a_write(self, tmp_path, monkeypatch) -> None:
        """No staged name may be left in the visible root once a write finishes.

        Measured to be NON-DISCRIMINATING for the staged-vs-beside question on the success
        path, and said so rather than presented as the pin: ``atomic_write`` also removes
        its temp when the write succeeds, so reverting the writer leaves this green. What
        it does catch is a writer that leaves an orphan behind; the staging-directory pin
        above is what tells the two mechanisms apart.
        """
        monkeypatch.setattr(notification_settings, "config_dir", lambda: tmp_path)
        store = notification_settings.ChannelSettings()
        store.update("system.agent", deliver_to=["slack"])

        root_files = {p.name for p in tmp_path.iterdir() if p.is_file()}
        assert root_files == {notification_settings._SETTINGS_FILENAME}

    def test_the_stored_route_survives_the_staged_write(self, tmp_path, monkeypatch) -> None:
        """The write still has to work: the fix must not trade a leak for a lost setting."""
        monkeypatch.setattr(notification_settings, "config_dir", lambda: tmp_path)
        store = notification_settings.ChannelSettings()
        store.update("system.agent", deliver_to=["slack"])

        reloaded = notification_settings.ChannelSettings()
        assert reloaded.get("system.agent").get("deliver_to") == ["slack"]


class TestTheStagingDirectoryIsFencedFromTheFileToolsToo:
    """The sandbox mask is the Linux plane only, so it is not the whole fence.

    ``sandbox._CREW_HIDDEN_LEAVES`` is a bind-mount list, and bind mounts exist on Linux.
    On macOS and Windows the agent's file tools are the reachable surface and
    ``is_sensitive_path`` is the sole backstop, so a staging temp holding the real
    ``deliver_to`` route was readable there with the directory masked on Linux alone. The
    sibling ``md-notebook-staging`` and ``aws-control-staging`` entries are on BOTH lists
    for exactly this reason, and these pin that this one now is too.
    """

    @pytest.mark.parametrize("prefix", _CREW_PREFIXES)
    def test_the_staging_directory_is_on_the_file_tool_fence(self, prefix: str) -> None:
        assert (
            f"{prefix}/{sandbox._NOTIFICATION_SETTINGS_STAGING_LEAF}"
            in security.sensitive_home_dirs()
        )

    @pytest.mark.parametrize("prefix", _CREW_PREFIXES)
    def test_a_staged_temp_inside_it_is_refused_for_reads_and_for_writes(self, prefix: str) -> None:
        """The discriminating assertion: the temp, not the directory's own name.

        ``mkstemp`` picks the name, so fencing the directory is the only way to cover
        every temp present and future. A fence that matched only the directory would
        leave the bytes readable at the name the writer actually uses.
        """
        staged = os.path.join(
            _crew_path(prefix, sandbox._NOTIFICATION_SETTINGS_STAGING_LEAF), "tmpAbC123.tmp"
        )
        assert security.is_sensitive_path(staged)
        assert security.is_sensitive_write_path(staged)

    def test_the_fenced_name_is_the_one_the_writer_stages_in(self) -> None:
        """Pins the fence to the writer rather than to a literal repeated in three places."""
        assert sandbox._NOTIFICATION_SETTINGS_STAGING_LEAF == notification_settings._STAGING_LEAF


class TestBothGatesCoverTheLeaf:
    """Either gate alone leaves a usable path to the file, so both are pinned."""

    @pytest.mark.parametrize("prefix", _CREW_PREFIXES)
    def test_the_agent_file_tools_refuse_it_under_every_crew_prefix(self, prefix: str) -> None:
        assert f"{prefix}/{sandbox._NOTIFICATION_SETTINGS_LEAF}" in security.sensitive_home_dirs()

    @pytest.mark.parametrize("prefix", _CREW_PREFIXES)
    def test_it_is_refused_for_reads_and_for_writes(self, prefix: str) -> None:
        """The read+write floor, not the write-only tier: a stored routing preference the
        agent can read is one it can also confirm it has armed."""
        path = _crew_path(prefix, sandbox._NOTIFICATION_SETTINGS_LEAF)
        assert security.is_sensitive_path(path)
        assert security.is_sensitive_write_path(path)

    def test_it_is_masked_from_the_sandbox_too(self) -> None:
        assert sandbox._NOTIFICATION_SETTINGS_LEAF in sandbox._CREW_HIDDEN_LEAVES

    @pytest.mark.parametrize("mode", _MODES)
    @pytest.mark.parametrize("prefix", _CREW_PREFIXES)
    @_POSIX_ONLY
    def test_the_launcher_masks_it_in_every_mode(self, mode: str, prefix: str) -> None:
        assert _crew_path(prefix, sandbox._NOTIFICATION_SETTINGS_LEAF) in _launcher_files(mode)


class TestTheMaskGetsAMountTarget:
    """Without a published stub the mask has no name to bind over, and the data-home root
    is writable in every sandbox -- so a child would simply create the real file with a
    route already armed."""

    def test_the_fixture_really_isolates_the_real_home(self, crew_home, tmp_path) -> None:
        """Guard the guard: an unpatched home would publish into the developer's own tree."""
        assert sandbox.Path.home() == tmp_path
        assert sandbox.config_dir() == crew_home

    def test_an_absent_file_is_published(self, crew_home) -> None:
        created = sandbox._materialize_notification_settings_mask_target()

        target = crew_home / sandbox._NOTIFICATION_SETTINGS_LEAF
        assert created == str(target)
        assert target.read_bytes() == sandbox._NOTIFICATION_SETTINGS_PRECREATE_CONTENT

    def test_the_published_stub_has_exactly_one_link(self, crew_home) -> None:
        target = sandbox._materialize_notification_settings_mask_target()
        assert target is not None
        assert os.stat(target).st_nlink == 1

    def test_a_real_settings_file_is_left_byte_for_byte_alone(self, crew_home) -> None:
        """Never truncates and never removes: the owner's armed routes must survive a spawn."""
        target = crew_home / sandbox._NOTIFICATION_SETTINGS_LEAF
        real = b'{"channel_settings": {"system.approval": {"deliver_to": ["slack"]}}}\n'
        target.write_bytes(real)

        assert sandbox._materialize_notification_settings_mask_target() is None
        assert target.read_bytes() == real

    def test_an_absent_data_home_is_left_absent(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(sandbox, "config_dir", lambda: tmp_path / "nope")
        assert sandbox._materialize_notification_settings_mask_target() is None
        assert not (tmp_path / "nope").exists()

    @_POSIX_ONLY
    def test_a_symlink_at_the_name_refuses_the_spawn(self, crew_home) -> None:
        """A mount follows a link, so the mask would bind over the referent while the
        lexical name stayed an agent-replaceable link in a writable directory.

        POSIX-scoped with its three siblings below, and by the function's own contract
        rather than to dodge a red: the materialiser exists to give ``mount(2)`` a target
        and is called only on the Linux spawn path, so its refusal semantics are a POSIX
        property. ``os.symlink`` also needs a privilege on Windows that the runner does
        not hold, which would fail these tests for a reason that says nothing about the
        behaviour under test.
        """
        real = crew_home / "elsewhere.json"
        real.write_text("{}\n", encoding="utf-8")
        os.symlink(real, crew_home / sandbox._NOTIFICATION_SETTINGS_LEAF)
        with pytest.raises(sandbox.SandboxCeilingUnsealable):
            sandbox._materialize_notification_settings_mask_target()

    @_POSIX_ONLY
    def test_a_dangling_symlink_at_the_name_refuses_the_spawn(self, crew_home) -> None:
        os.symlink(crew_home / "gone.json", crew_home / sandbox._NOTIFICATION_SETTINGS_LEAF)
        with pytest.raises(sandbox.SandboxCeilingUnsealable):
            sandbox._materialize_notification_settings_mask_target()

    @_POSIX_ONLY
    def test_a_pre_existing_file_with_a_second_hard_link_refuses_the_spawn(self, crew_home) -> None:
        """A second name on the inode is an unmasked write channel to the routing bits,
        wherever it came from."""
        target = crew_home / sandbox._NOTIFICATION_SETTINGS_LEAF
        target.write_text("{}\n", encoding="utf-8")
        os.link(target, crew_home / "sneaky-alias")
        with pytest.raises(sandbox.SandboxCeilingUnsealable, match="hard links"):
            sandbox._materialize_notification_settings_mask_target()

    @_POSIX_ONLY
    def test_a_link_planted_during_publish_refuses_the_spawn(self, crew_home, monkeypatch):
        """The temp is staged in the target's own parent, so this race is the one that has
        to be DETECTED rather than prevented: the post-publish sole-link check fails the
        spawn closed instead of launching with an unmasked path to the inode."""
        real = sandbox._publish_empty_ceiling

        def _link_after_publish(target, parent, content=sandbox._EMPTY_CEILING_DOCUMENT):
            ok = real(target, parent, content=content)
            if ok:
                os.link(target, crew_home / "racer-alias")
            return ok

        monkeypatch.setattr(sandbox, "_publish_empty_ceiling", _link_after_publish)
        with pytest.raises(sandbox.SandboxCeilingUnsealable, match="hard links"):
            sandbox._materialize_notification_settings_mask_target()

    def test_the_spawn_path_actually_calls_it(self) -> None:
        """A materialiser nothing calls is a comment, not a mount target."""
        assert "_materialize_notification_settings_mask_target()" in inspect.getsource(
            sandbox.namespace_argv
        )


class TestTheStubIsTheReadersAbsentEquivalent:
    """The stub is what an agent sees through the mask, and what the gateway would read if
    it ever read the mask target. It must mean "no channel has settings" -- the
    no-bridging default an install ships with -- not "corrupt"."""

    def test_the_loader_reads_the_stub_exactly_as_absence(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(notification_settings, "config_dir", lambda: tmp_path)
        absent = notification_settings.ChannelSettings().all_settings()

        (tmp_path / notification_settings._SETTINGS_FILENAME).write_bytes(
            sandbox._NOTIFICATION_SETTINGS_PRECREATE_CONTENT
        )
        stubbed = notification_settings.ChannelSettings().all_settings()

        assert absent == {}
        assert stubbed == absent

    def test_the_stub_is_valid_json_rather_than_an_empty_file(self) -> None:
        """A zero-byte file would read as CORRUPT, which is a warning at every start."""
        assert sandbox._NOTIFICATION_SETTINGS_PRECREATE_CONTENT.strip()
        assert json.loads(sandbox._NOTIFICATION_SETTINGS_PRECREATE_CONTENT) == {}

    def test_no_route_is_armed_by_the_stub(self, tmp_path, monkeypatch) -> None:
        """The end the finding cares about: reading the stub arms nothing."""
        monkeypatch.setattr(notification_settings, "config_dir", lambda: tmp_path)
        (tmp_path / notification_settings._SETTINGS_FILENAME).write_bytes(
            sandbox._NOTIFICATION_SETTINGS_PRECREATE_CONTENT
        )
        store = notification_settings.ChannelSettings()
        assert store.get("system.approval").get("deliver_to") is None
