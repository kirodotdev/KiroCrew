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
from pathlib import Path

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

        # Every publish -- the settings file and its write stamp -- stages in the mask.
        assert seen and set(seen) == {str(tmp_path / notification_settings._STAGING_LEAF)}
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
    def test_a_symlink_at_the_name_is_unmaskable(self, crew_home) -> None:
        """A mount follows a link, so the mask would bind over the referent while the
        lexical name stayed an agent-replaceable link in a writable directory.

        POSIX-scoped with its siblings below, and by the function's own contract rather
        than to dodge a red: the publisher exists to give ``mount(2)`` a target and is
        called only on the Linux spawn path. ``os.symlink`` also needs a privilege on
        Windows that the runner does not hold. Each of these drives the PUBLISHER, which
        still raises; that the raise stops at the feature rather than the spawn is
        :class:`TestAnUnfitLeafDisablesRoutingNotSpawning`.
        """
        real = crew_home / "elsewhere.json"
        real.write_text("{}\n", encoding="utf-8")
        os.symlink(real, crew_home / sandbox._NOTIFICATION_SETTINGS_LEAF)
        with pytest.raises(sandbox.SandboxCeilingUnsealable):
            sandbox._publish_notification_settings_mask_target()

    @_POSIX_ONLY
    def test_a_dangling_symlink_at_the_name_is_unmaskable(self, crew_home) -> None:
        os.symlink(crew_home / "gone.json", crew_home / sandbox._NOTIFICATION_SETTINGS_LEAF)
        with pytest.raises(sandbox.SandboxCeilingUnsealable):
            sandbox._publish_notification_settings_mask_target()

    @_POSIX_ONLY
    def test_a_pre_existing_file_with_a_second_hard_link_is_unmaskable(self, crew_home) -> None:
        """A second name on the inode is an unmasked write channel to the routing bits,
        wherever it came from."""
        target = crew_home / sandbox._NOTIFICATION_SETTINGS_LEAF
        target.write_text("{}\n", encoding="utf-8")
        os.link(target, crew_home / "sneaky-alias")
        with pytest.raises(sandbox.SandboxCeilingUnsealable, match="hard links"):
            sandbox._publish_notification_settings_mask_target()

    @_POSIX_ONLY
    def test_a_link_planted_during_publish_is_detected(self, crew_home, monkeypatch):
        """The temp is staged in the target's own parent, so this race is the one that has
        to be DETECTED rather than prevented: the post-publish sole-link check reports the
        leaf unmaskable instead of treating the inode as masked."""
        real = sandbox._publish_empty_ceiling

        def _link_after_publish(target, parent, content=sandbox._EMPTY_CEILING_DOCUMENT):
            ok = real(target, parent, content=content)
            if ok:
                os.link(target, crew_home / "racer-alias")
            return ok

        monkeypatch.setattr(sandbox, "_publish_empty_ceiling", _link_after_publish)
        with pytest.raises(sandbox.SandboxCeilingUnsealable, match="hard links"):
            sandbox._publish_notification_settings_mask_target()

    @_POSIX_ONLY
    def test_a_lost_publish_race_to_an_unfit_winner_is_unmaskable(self, crew_home, monkeypatch):
        """A race loser must reach the same verdict its siblings do.

        When ``os.link`` loses the publish race (EEXIST) the loser re-checks the winner's
        file. If the winner's file is UNFIT -- a second hard link, a non-regular file --
        the leaf is not maskable, and the publisher raises
        :class:`SandboxCeilingUnsealable` rather than reporting the leaf masked.
        """
        target = crew_home / sandbox._NOTIFICATION_SETTINGS_LEAF
        target.write_text("{}\n", encoding="utf-8")
        os.link(target, crew_home / "winner-alias")  # winner is multilinked -> unfit

        # Force the lost-publish arm: drive _publish_empty_ceiling to report the loss and
        # bypass the early exists() branch so the lost-race re-check is the raising site.
        monkeypatch.setattr(sandbox, "_publish_empty_ceiling", lambda *a, **k: False)
        monkeypatch.setattr(os.path, "exists", lambda _p: False)
        with pytest.raises(sandbox.SandboxCeilingUnsealable):
            sandbox._publish_notification_settings_mask_target()

    @_POSIX_ONLY
    def test_a_lost_publish_race_whose_winner_vanished_is_unmaskable(self, crew_home, monkeypatch):
        """A winner that unlinked the name leaves the mask with no mount target.

        The loser's re-check raises ``FileNotFoundError`` from ``os.lstat``. The
        publisher converts that bare exception to :class:`SandboxCeilingUnsealable` (no
        mount target means the leaf is unmaskable), the class the spawn-side wrapper
        catches.
        """
        monkeypatch.setattr(sandbox, "_publish_empty_ceiling", lambda *a, **k: False)
        monkeypatch.setattr(os.path, "exists", lambda _p: False)  # winner already removed it
        # No file at the target -> _refuse_unless_sole_regular_notification_leaf's lstat
        # raises FileNotFoundError; the arm swallows the bare exception and refuses with
        # the descriptive remedy sentence instead.
        with pytest.raises(sandbox.SandboxCeilingUnsealable, match="mount target"):
            sandbox._publish_notification_settings_mask_target()

    def test_the_spawn_path_actually_calls_it(self) -> None:
        """A materialiser nothing calls is a comment, not a mount target."""
        assert "_materialize_notification_settings_mask_target(" in inspect.getsource(
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


class TestTheRefusalNamesThisLeafNotTheLiveTargetPointer:
    """SecScope CONCERNS, concern 2 (misleading diagnostics): the refusal must name THIS
    leaf, not borrow the live-target pointer's wording, which names the wrong file and the
    wrong consequence to an operator about to inspect ``notification_settings.json``. The
    materialiser raises the leaf's OWN sentences."""

    @_POSIX_ONLY
    def test_the_multilink_refusal_names_the_notification_leaf(self, crew_home) -> None:
        target = crew_home / sandbox._NOTIFICATION_SETTINGS_LEAF
        target.write_text("{}\n", encoding="utf-8")
        os.link(target, crew_home / "backup-alias")  # a snapshot tool's second name

        with pytest.raises(sandbox.SandboxCeilingUnsealable) as exc:
            sandbox._publish_notification_settings_mask_target()

        msg = str(exc.value)
        # Names THIS leaf and its routing threat, not the live-target pointer's.
        assert "notification-settings leaf" in msg
        assert "deliver_to" in msg
        assert "live-target pointer" not in msg
        assert "execve" not in msg and "checkout" not in msg
        # Still carries the operator's remedy (the find invocation).
        assert "find " in msg and "-samefile" in msg

    @_POSIX_ONLY
    def test_the_symlink_refusal_names_the_notification_leaf(self, crew_home) -> None:
        real = crew_home / "elsewhere.json"
        real.write_text("{}\n", encoding="utf-8")
        os.symlink(real, crew_home / sandbox._NOTIFICATION_SETTINGS_LEAF)  # dotfile manager

        with pytest.raises(sandbox.SandboxCeilingUnsealable) as exc:
            sandbox._publish_notification_settings_mask_target()

        # A resolving link passes os.path.exists, then lstat sees S_ISLNK (not S_ISREG) and
        # raises the non-regular-file sentence -- which still names THIS leaf, not the
        # live-target pointer. (The SYMLINK sentence itself is the doctor classifier's
        # path, covered in TestTheDoctorClassifierSeesThisLeaf.)
        msg = str(exc.value)
        assert "notification-settings leaf" in msg
        assert "non-regular file" in msg
        assert "live-target pointer" not in msg

    @_POSIX_ONLY
    def test_an_irregular_file_refusal_names_the_notification_leaf(self, crew_home) -> None:
        fifo = crew_home / sandbox._NOTIFICATION_SETTINGS_LEAF
        os.mkfifo(fifo)  # a non-regular file the mask loops cannot classify

        with pytest.raises(sandbox.SandboxCeilingUnsealable) as exc:
            sandbox._publish_notification_settings_mask_target()

        msg = str(exc.value)
        assert "notification-settings leaf" in msg
        assert "non-regular file" in msg
        assert "live-target pointer" not in msg


class TestTheDoctorClassifierSeesThisLeaf:
    """SecScope CONCERNS, concern 2 (no advance notice): the leaf is NOT a credential
    leaf, so it is absent from ``_CREW_HARDLINK_REFUSED_LEAVES`` and neither
    ``masked_credential_leaf_aliases`` nor ``live_target_pointer_unfitness`` covers it.
    ``notification_settings_pointer_unfitness`` gives ``kirocrew doctor`` the pre-spawn
    read it was missing -- in the leaf's own words, which the spawn refusal shares."""

    def test_the_leaf_is_not_a_credential_leaf(self) -> None:
        """Why it needs its own classifier rather than riding the credential probe."""
        assert sandbox._NOTIFICATION_SETTINGS_LEAF not in sandbox._CREW_HARDLINK_REFUSED_LEAVES

    def test_an_absent_leaf_is_fit(self, crew_home) -> None:
        assert sandbox.notification_settings_pointer_unfitness() is None

    def test_a_healthy_leaf_is_fit(self, crew_home) -> None:
        (crew_home / sandbox._NOTIFICATION_SETTINGS_LEAF).write_text("{}\n", encoding="utf-8")
        assert sandbox.notification_settings_pointer_unfitness() is None

    def test_an_absent_data_home_is_fit(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(sandbox, "config_dir", lambda: tmp_path / "nope")
        assert sandbox.notification_settings_pointer_unfitness() is None

    @_POSIX_ONLY
    def test_a_symlinked_leaf_is_classified_unfit(self, crew_home) -> None:
        real = crew_home / "elsewhere.json"
        real.write_text("{}\n", encoding="utf-8")
        os.symlink(real, crew_home / sandbox._NOTIFICATION_SETTINGS_LEAF)

        unfit = sandbox.notification_settings_pointer_unfitness()
        assert unfit is not None
        assert unfit.path == str(crew_home / sandbox._NOTIFICATION_SETTINGS_LEAF)
        assert "SYMLINK" in unfit.detail
        assert "notification-settings leaf" in unfit.detail

    @_POSIX_ONLY
    def test_a_multilinked_leaf_is_classified_unfit(self, crew_home) -> None:
        target = crew_home / sandbox._NOTIFICATION_SETTINGS_LEAF
        target.write_text("{}\n", encoding="utf-8")
        os.link(target, crew_home / "backup-alias")

        unfit = sandbox.notification_settings_pointer_unfitness()
        assert unfit is not None
        assert "hard links" in unfit.detail
        assert "notification-settings leaf" in unfit.detail

    @_POSIX_ONLY
    def test_an_irregular_leaf_is_classified_unfit(self, crew_home) -> None:
        os.mkfifo(crew_home / sandbox._NOTIFICATION_SETTINGS_LEAF)

        unfit = sandbox.notification_settings_pointer_unfitness()
        assert unfit is not None
        assert "non-regular file" in unfit.detail

    def test_the_classifier_and_the_refusal_share_one_sentence(self, crew_home) -> None:
        """The whole reason the classifier exists: doctor's pre-spawn warning and the
        later refusal must not describe one file two different ways."""
        if sys.platform == "win32":
            pytest.skip("POSIX launcher only")
        target = crew_home / sandbox._NOTIFICATION_SETTINGS_LEAF
        target.write_text("{}\n", encoding="utf-8")
        os.link(target, crew_home / "backup-alias")

        unfit = sandbox.notification_settings_pointer_unfitness()
        with pytest.raises(sandbox.SandboxCeilingUnsealable) as exc:
            sandbox._publish_notification_settings_mask_target()
        assert unfit is not None
        # Same formatter builds both, so the strings are identical.
        assert unfit.detail == str(exc.value)

    def test_doctor_registers_and_prints_the_section(self, crew_home, capsys) -> None:
        """The classifier is wired into the doctor run and renders a section when unfit --
        naming routing as what stops, not agent spawns."""
        if sys.platform == "win32":
            pytest.skip("os.link fixture is POSIX-only here")
        from kiro_crew import cli_doctor
        from kiro_crew.doctor_checks import confinement

        # Registered in the doctor run sequence.
        assert "_doctor_notification_settings_pointer" in inspect.getsource(cli_doctor)

        target = crew_home / sandbox._NOTIFICATION_SETTINGS_LEAF
        target.write_text("{}\n", encoding="utf-8")
        os.link(target, crew_home / "backup-alias")

        issues: list[str] = []
        confinement._doctor_notification_settings_pointer(issues)
        out = capsys.readouterr().out
        assert "Notification Settings Leaf" in out
        assert "routing DISABLED" in out
        assert "agent spawns are unaffected" in out
        assert "REFUSED" not in out
        assert "notification-settings leaf" in issues


def _make_unfit(crew_home, shape: str) -> None:
    """Put the leaf into one of the ordinary-operation shapes that make it unmaskable."""
    target = crew_home / sandbox._NOTIFICATION_SETTINGS_LEAF
    if shape == "symlink":
        real = crew_home / "dotfiles-settings.json"
        real.write_text('{"system.approval": {"deliver_to": ["slack"]}}\n', encoding="utf-8")
        os.symlink(real, target)
    elif shape == "hardlink":
        target.write_text('{"system.approval": {"deliver_to": ["slack"]}}\n', encoding="utf-8")
        os.link(target, crew_home / "snapshot-alias")
    else:
        os.mkfifo(target)


_UNFIT_SHAPES = ("symlink", "hardlink", "fifo")


@_POSIX_ONLY
class TestAnUnfitLeafDisablesRoutingNotSpawning:
    """First Principles BLOCK, as ruled: the leaf's failure is scoped to the FEATURE. An
    unfit leaf turns notification routing off (the bridge delivers nothing, doctor names
    it) and agent spawns carry on, so ``namespace_argv`` never raises for this leaf."""

    @pytest.mark.parametrize("shape", _UNFIT_SHAPES)
    def test_the_materialiser_does_not_raise_and_establishes_nothing(
        self, crew_home, caplog, shape: str
    ) -> None:
        _make_unfit(crew_home, shape)
        established: list[str] = []
        with caplog.at_level("WARNING", logger=sandbox.logger.name):
            assert sandbox._materialize_notification_settings_mask_target(established) is None
        # Not REQUIRED of the launcher: a mask that cannot bind must not refuse the child.
        assert established == []
        assert any("notification routing is disabled" in r.getMessage() for r in caplog.records)

    @pytest.mark.skipif(not sys.platform.startswith("linux"), reason="namespace launcher")
    @pytest.mark.parametrize("shape", _UNFIT_SHAPES)
    def test_namespace_argv_still_builds_a_launch(self, crew_home, shape: str) -> None:
        _make_unfit(crew_home, shape)
        argv = sandbox.namespace_argv(["/bin/true"], "strict")
        assert "/bin/true" in argv

    @pytest.mark.parametrize("shape", _UNFIT_SHAPES)
    def test_the_bridge_gate_reads_the_leaf_as_unfit(self, crew_home, shape: str) -> None:
        from kiro_crew.dashboard import state as dashboard_state

        _make_unfit(crew_home, shape)
        assert dashboard_state._notification_routing_leaf_fit() is False

    def test_the_bridge_gate_reads_a_healthy_leaf_as_fit(self, crew_home) -> None:
        from kiro_crew.dashboard import state as dashboard_state

        (crew_home / sandbox._NOTIFICATION_SETTINGS_LEAF).write_text("{}\n", encoding="utf-8")
        assert dashboard_state._notification_routing_leaf_fit() is True

    def test_the_live_dispatcher_is_wired_to_the_gate(self) -> None:
        """A gate nothing passes to the dispatcher is a comment, not a refusal."""
        from kiro_crew.dashboard import state as dashboard_state

        source = inspect.getsource(dashboard_state.DashboardState.__init__)
        assert "routing_leaf_fit=_notification_routing_leaf_fit" in source

    @pytest.mark.parametrize("shape", _UNFIT_SHAPES)
    def test_an_armed_route_on_an_unfit_leaf_delivers_nothing(self, crew_home, shape: str) -> None:
        import asyncio
        from unittest import mock

        from kiro_crew.dashboard import state as dashboard_state
        from kiro_crew.notifications.bridge import BridgeDispatcher

        _make_unfit(crew_home, shape)
        sent: list[str] = []

        class _Sink:
            async def send(self, text: str, recheck) -> str:
                sent.append(text)
                return "m1"

        dispatcher = BridgeDispatcher(
            sink_resolver=lambda _t: _Sink(),
            settings_reader=lambda _c: {"deliver_to": ["slack"]},
            routing_leaf_fit=dashboard_state._notification_routing_leaf_fit,
        )
        sel = mock.Mock()
        with mock.patch("kiro_crew.sel.sel", return_value=sel):
            outcomes = asyncio.run(
                dispatcher.dispatch(
                    {"channel": "system.approval", "priority": "critical", "title": "t"}
                )
            )
        assert sent == []
        assert [(o.delivered, o.reason) for o in outcomes] == [(False, "routing_leaf_unfit")]
        assert sel.log_api_access.call_args.kwargs["outcome"] == "denied"


class TestOnlyAGatewayWrittenFileArmsARoute:
    """GPT 6.1 (v63): with an unmaskable settings NAME (a tolerated symlink), a sandboxed
    process can swap the link for its own single-link JSON arming ``deliver_to``; the
    shape check then reads it as fit, and the next start would load the forged route. The
    store honours routes only from bytes matching the write stamp it keeps in the masked
    staging directory, so a file the gateway did not write arms nothing."""

    def _store(self, tmp_path, monkeypatch):
        monkeypatch.setattr(notification_settings, "config_dir", lambda: tmp_path)
        return notification_settings.ChannelSettings

    def test_a_route_the_gateway_wrote_survives_a_restart(self, tmp_path, monkeypatch) -> None:
        store = self._store(tmp_path, monkeypatch)
        store().update("system.approval", deliver_to=["slack"])
        assert store().get("system.approval").get("deliver_to") == ["slack"]

    @_POSIX_ONLY
    def test_a_link_swapped_for_a_forged_file_arms_nothing(self, tmp_path, monkeypatch) -> None:
        """The exact attack: a symlinked leaf replaced by a regular single-link file."""
        store = self._store(tmp_path, monkeypatch)
        leaf = tmp_path / notification_settings._SETTINGS_FILENAME
        real = tmp_path / "dotfiles-settings.json"
        real.write_text('{"channel_settings": {}}\n', encoding="utf-8")
        os.symlink(real, leaf)
        # The sandboxed child: unlink the name, drop its own routing file there.
        leaf.unlink()
        leaf.write_text(
            json.dumps({"channel_settings": {"system.approval": {"deliver_to": ["slack"]}}}),
            encoding="utf-8",
        )
        assert os.stat(leaf).st_nlink == 1  # passes the bridge's shape check
        assert store().get("system.approval").get("deliver_to") is None

    def test_tampered_bytes_lose_their_routes_but_keep_display_state(
        self, tmp_path, monkeypatch
    ) -> None:
        store = self._store(tmp_path, monkeypatch)
        store().update("system.approval", deliver_to=["slack"])
        leaf = tmp_path / notification_settings._SETTINGS_FILENAME
        forged = {
            "channel_settings": {
                "system.approval": {"deliver_to": ["slack", "discord"]},
                "system.heartbeat": {"muted": True},
            }
        }
        leaf.write_text(json.dumps(forged), encoding="utf-8")
        reloaded = store()
        assert reloaded.get("system.approval").get("deliver_to") is None
        assert reloaded.get("system.heartbeat") == {"muted": True}

    def test_a_missing_stamp_honours_no_route(self, tmp_path, monkeypatch) -> None:
        store = self._store(tmp_path, monkeypatch)
        store().update("system.approval", deliver_to=["slack"])
        (tmp_path / notification_settings._STAGING_LEAF / "settings.sha256").unlink()
        assert store().get("system.approval").get("deliver_to") is None

    def test_an_owner_save_re_arms_after_a_forgery(self, tmp_path, monkeypatch) -> None:
        store = self._store(tmp_path, monkeypatch)
        (tmp_path / notification_settings._SETTINGS_FILENAME).write_text(
            json.dumps({"channel_settings": {"system.approval": {"deliver_to": ["slack"]}}}),
            encoding="utf-8",
        )
        s = store()
        assert s.get("system.approval").get("deliver_to") is None
        s.update("system.approval", deliver_to=["slack"])
        assert store().get("system.approval").get("deliver_to") == ["slack"]

    def test_the_stamp_lives_inside_the_masked_staging_directory(self) -> None:
        assert notification_settings._STAGING_LEAF in sandbox._CREW_HIDDEN_LEAVES
        assert notification_settings._STAGING_LEAF not in sandbox._CREW_ALIAS_TOLERATED_LEAVES

    def test_the_published_bytes_are_the_stamped_bytes(self, tmp_path, monkeypatch) -> None:
        """No newline translation between the write and the stamp: on Windows a text-mode
        write turned each LF into CRLF, so no gateway-written route ever matched."""
        store = self._store(tmp_path, monkeypatch)
        store().update("system.approval", deliver_to=["slack"])
        raw = (tmp_path / notification_settings._SETTINGS_FILENAME).read_bytes()
        assert b"\r" not in raw
        assert notification_settings._matches_write_stamp(raw)

    def test_an_import_stamps_the_validated_routes_not_the_live_file(
        self, tmp_path, monkeypatch
    ) -> None:
        """GPT 6.1 (v65): a sandbox that rewrites the swapped-in file before the stamp
        must not get its own routes authorized. The validated mapping is what lands."""
        store = self._store(tmp_path, monkeypatch)
        leaf = tmp_path / notification_settings._SETTINGS_FILENAME
        validated = {"system.approval": {"deliver_to": ["slack"]}}
        s = store()
        with s.replacing_file(validated):
            # The swap writes the archive's file...
            leaf.write_text(json.dumps({"channel_settings": validated}), encoding="utf-8")
            # ...and the sandboxed process replaces it before the lock is released.
            forged = {"system.agent": {"deliver_to": ["slack"]}}
            leaf.write_text(json.dumps({"channel_settings": forged}), encoding="utf-8")
        reloaded = store()
        assert reloaded.get("system.approval").get("deliver_to") == ["slack"]
        assert reloaded.get("system.agent").get("deliver_to") is None

    def test_a_swap_with_no_validated_mapping_stamps_nothing(self, tmp_path, monkeypatch) -> None:
        store = self._store(tmp_path, monkeypatch)
        leaf = tmp_path / notification_settings._SETTINGS_FILENAME
        s = store()
        with s.replacing_file():
            leaf.write_text(
                json.dumps({"channel_settings": {"system.agent": {"deliver_to": ["slack"]}}}),
                encoding="utf-8",
            )
        assert store().get("system.agent").get("deliver_to") is None

    def test_a_forge_between_publish_and_stamp_is_not_authorized(
        self, tmp_path, monkeypatch
    ) -> None:
        """The stamp hashes the payload held in memory, never the published name: a
        file swapped in right after the rename does not match it."""
        store = self._store(tmp_path, monkeypatch)
        leaf = tmp_path / notification_settings._SETTINGS_FILENAME
        real_replace = notification_settings.replace_with_retry
        forged = {"channel_settings": {"system.agent": {"deliver_to": ["slack"]}}}

        def _publish_then_forge(src, dst):
            real_replace(src, dst)
            if Path(dst) == leaf:
                leaf.write_text(json.dumps(forged), encoding="utf-8")

        monkeypatch.setattr(notification_settings, "replace_with_retry", _publish_then_forge)
        store().update("system.approval", deliver_to=["slack"])
        monkeypatch.setattr(notification_settings, "replace_with_retry", real_replace)
        assert store().get("system.agent").get("deliver_to") is None

    def test_every_stamp_hashes_bytes_held_in_memory(self) -> None:
        """Audit pin: the one stamp call site hashes the payload it just wrote, and no
        caller stamps a re-read of the settings file."""
        source = inspect.getsource(notification_settings)
        calls = [ln.strip() for ln in source.splitlines() if "_record_write_stamp(" in ln]
        calls = [c for c in calls if not c.startswith("def ")]
        assert calls == ['_record_write_stamp(payload.encode("utf-8"))']
