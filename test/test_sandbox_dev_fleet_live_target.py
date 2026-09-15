"""The live-target pointer stays masked from EVERY sandbox — Dev Fleet's backend included.

The pointer (``live_target.json``) names the checkout the gateway ``execve``s into at
startup, so it is bind-masked from sandboxed processes: a process that could write it
would choose the code the whole host runs next. Dev Fleet's backend is one of those
processes, and it is the process that asks for a cutover (make-live) — so the first version of
this fix carved the leaf back out for that ONE spawn, the way md-notebook's Notes state
is carved out.

That carve-out was a hole, and this file exists to keep it closed: the backend spawns
``npm ci`` / build steps for arbitrary worktrees, a nested sandbox is denied by design,
so those children run inside the backend's namespace — a leaf the backend could write,
a worktree's lifecycle script could write. The cutover therefore moved into the GATEWAY
process (``dev_fleet/gateway_routes.py``, pinned by ``test_dev_fleet_gateway_routes``),
and what the sandbox keeps is:

* the mask, in every mode, on both platform builders, with NO owned-leaf exemption
  for dev-fleet (``_APP_BACKEND_OWNED_LEAVES`` must never list the pointer);
* a MOUNT TARGET for it, materialised before every namespace spawn, because an absent
  file cannot be masked and the crew home root is writable in-sandbox;
* a document that every reader treats exactly as absence, so that stub never surfaces
  as a corrupt pointer.
"""

from __future__ import annotations

import inspect
import json
import os
import re
import sys

import pytest

from kiro_crew import sandbox
from kiro_crew.service import live_target

_POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="POSIX launcher only")
_MODES = ("standard", "cc", "strict")
_CREW_PREFIXES = (".kiro/crew", ".kirocrew")


def _crew_path(prefix: str, leaf: str) -> str:
    """Spell a crew-home target the way the production builders do (single relative join)."""
    return os.path.join(os.path.expanduser("~"), f"{prefix}/{leaf}")


def _launcher_hidden(mode: str, *, extra_visible_dirs: tuple[str, ...] = ()) -> set[str]:
    script = sandbox._build_launcher_script(mode, extra_visible_dirs=extra_visible_dirs)
    match = re.search(r"SENSITIVE_DIRS = (\[.*?\])\n", script, re.S)
    assert match, "SENSITIVE_DIRS missing from the launcher"
    return set(json.loads(match.group(1)))


class TestTheLiteralsCannotDrift:
    """The mask and the module that owns the file must agree on the name.

    ``sandbox.py`` spells the leaf itself rather than importing ``service.live_target``,
    deliberately — this is a low-level module and that import drags in the config loader.
    A test-time import costs nothing, so the spelling is pinned here instead.
    """

    def test_the_leaf_matches_the_pointer_filename(self) -> None:
        assert sandbox._LIVE_TARGET_LEAF == live_target.pointer_path().name


class TestNoBackendGetsThePointerBack:
    """The inverse of the md-notebook carve-out tests: dev-fleet is NOT in the table.

    Not because its backend has no use for the file — it did — but because every
    descendant of that long-lived spawn shares its namespace (nested sandboxing is denied
    on both platforms, so ``wrap_argv`` passes build children through), and a worktree's
    ``npm ci`` lifecycle script is one of those descendants.
    """

    def test_dev_fleet_owns_no_hidden_leaves(self) -> None:
        assert "dev-fleet" not in sandbox._APP_BACKEND_OWNED_LEAVES
        assert sandbox.app_backend_visible_targets("dev-fleet") == ()

    def test_no_app_is_handed_the_pointer(self) -> None:
        for app, leaves in sandbox._APP_BACKEND_OWNED_LEAVES.items():
            assert sandbox._LIVE_TARGET_LEAF not in leaves, (
                f"{app}'s backend spawn was handed the live-target pointer; its build "
                "children would inherit the write"
            )
            for target in sandbox.app_backend_visible_targets(app):
                assert os.path.basename(target) != sandbox._LIVE_TARGET_LEAF

    @_POSIX_ONLY
    def test_the_md_notebook_carveout_leaves_the_pointer_masked(self) -> None:
        """The one carve-out that DOES exist must not lift this leaf as a side effect."""
        hidden = _launcher_hidden(
            "standard",
            extra_visible_dirs=sandbox.app_backend_visible_targets(sandbox.MD_NOTEBOOK_APP_NAME),
        )
        for prefix in _CREW_PREFIXES:
            assert _crew_path(prefix, sandbox._LIVE_TARGET_LEAF) in hidden

    def test_the_isolated_startup_stays_scoped_to_md_notebook(self) -> None:
        """The ``-I`` rider the carve-out needed is gone with it: dev-fleet owns no
        hidden leaf, and the isolated start is keyed on md-notebook alone. (The
        ``DEV_FLEET_APP_NAME`` constant in ``apps.backend`` names the bound-port
        deferral, not a carve-out.)"""
        import kiro_crew.apps.backend as backend_mod

        source = inspect.getsource(sys.modules["kiro_crew.apps.backend"])
        assert backend_mod.DEV_FLEET_APP_NAME not in sandbox._APP_BACKEND_OWNED_LEAVES
        assert "_shipped_md_notebook" in source
        assert "_shipped_dev_fleet" not in source


class TestEveryProcessKeepsTheMask:
    """No exemption exists, so every builder hides the pointer in every mode."""

    @_POSIX_ONLY
    @pytest.mark.parametrize("mode", _MODES)
    @pytest.mark.parametrize("prefix", _CREW_PREFIXES)
    def test_a_default_build_keeps_the_mask(self, mode: str, prefix: str) -> None:
        assert _crew_path(prefix, sandbox._LIVE_TARGET_LEAF) in _launcher_hidden(mode)

    @pytest.mark.parametrize("mode", _MODES)
    def test_a_default_seatbelt_profile_keeps_the_deny(self, mode: str) -> None:
        profile = sandbox._build_seatbelt_profile(mode)
        target = _crew_path(".kiro/crew", sandbox._LIVE_TARGET_LEAF)

        assert f'"{target}"' in profile

    def test_the_hidden_table_names_the_leaf(self) -> None:
        assert sandbox._LIVE_TARGET_LEAF in sandbox._CREW_HIDDEN_LEAVES


@pytest.fixture()
def crew_home(tmp_path, monkeypatch):
    """An isolated crew data home, so no test here touches the developer's own pointer."""
    home = tmp_path / ".kiro" / "crew"
    home.mkdir(parents=True)
    monkeypatch.setattr(sandbox, "config_dir", lambda: home)
    monkeypatch.setattr(sandbox.Path, "home", staticmethod(lambda: tmp_path))
    return home


class TestSeatbeltDeniesTheAbsentPointer:
    """macOS needs no stub: a Seatbelt deny is a path rule, so it is emitted -- and
    holds -- for a pointer that does not exist yet. Pinned against the profile
    builder itself: the isolated crew home has NO pointer, and the literal
    ``file-write*`` deny (which covers ``file-write-create``) is still present for
    every crew-home spelling."""

    @pytest.mark.parametrize("mode", _MODES)
    def test_the_write_deny_is_emitted_for_a_pointer_that_does_not_exist(
        self, crew_home, mode: str
    ) -> None:
        assert not (crew_home / sandbox._LIVE_TARGET_LEAF).exists()
        profile = sandbox._build_seatbelt_profile(mode)
        denied = re.findall(
            r'\(deny file-write\* \(literal "([^"]*/'
            + re.escape(sandbox._LIVE_TARGET_LEAF)
            + r')"\)\)',
            profile,
        )
        # The builder joins the home with a "/"-spelled crew prefix, so on Windows the
        # literal carries mixed separators while ``Path`` spells backslashes throughout.
        # The rule is macOS text either way; what is pinned is WHICH file it names.
        assert os.path.normpath(str(crew_home / sandbox._LIVE_TARGET_LEAF)) in {
            os.path.normpath(path) for path in denied
        }
        assert denied and not any(os.path.exists(path) for path in denied)


class TestTheMaskGetsAMountTarget:
    """``mount(2)`` cannot mask an absent path, and the crew home root is writable.

    So with the pointer absent, a namespace spawned before the gateway ever wrote one
    holds the data-home root writable at that name and can CREATE the file that selects
    the code the gateway ``execve``s into next. Publishing the absent-equivalent document
    before every spawn gives the mask a mount target from the outset.
    """

    def test_the_fixture_really_isolates_the_real_home(self, crew_home, tmp_path) -> None:
        """Guard the guard: an unpatched home would publish into the developer's own tree."""
        assert sandbox.Path.home() == tmp_path
        assert sandbox.config_dir() == crew_home

    def test_an_absent_pointer_is_published(self, crew_home) -> None:
        created = sandbox._materialize_live_target_mask_target()

        pointer = crew_home / sandbox._LIVE_TARGET_LEAF
        assert created == str(pointer)
        assert pointer.read_bytes() == sandbox._LIVE_TARGET_PRECREATE_CONTENT

    @_POSIX_ONLY
    def test_the_published_pointer_is_owner_only(self, crew_home) -> None:
        """The pointer is a code-execution input: owner-only from birth, never for
        the width of a write window. POSIX-only because the assertion is about
        POSIX mode bits — Windows carries this as a DACL, which ``mkstemp`` sets and
        ``st_mode`` cannot express (it reports 0o666 there for every temp file)."""
        sandbox._materialize_live_target_mask_target()

        assert os.stat(crew_home / sandbox._LIVE_TARGET_LEAF).st_mode & 0o077 == 0

    def test_a_real_pin_is_left_byte_for_byte_alone(self, crew_home) -> None:
        pointer = crew_home / sandbox._LIVE_TARGET_LEAF
        pinned = '{"checkout": "/somewhere/real"}\n'
        pointer.write_text(pinned, encoding="utf-8")

        assert sandbox._materialize_live_target_mask_target() is None
        assert pointer.read_text(encoding="utf-8") == pinned

    def test_an_absent_data_home_is_left_absent(self, tmp_path, monkeypatch) -> None:
        """A host with no install is not scaffolded — mirrors the other materialisers."""
        monkeypatch.setattr(sandbox, "config_dir", lambda: tmp_path / "nope")

        assert sandbox._materialize_live_target_mask_target() is None
        assert not (tmp_path / "nope").exists()

    @_POSIX_ONLY
    def test_a_special_file_refuses_the_spawn(self, crew_home) -> None:
        """A FIFO matches neither isdir nor isfile, so its mask would be silently skipped.
        POSIX-only: ``os.mkfifo`` does not exist on Windows, and the launcher this
        protects is the Linux namespace path."""
        os.mkfifo(crew_home / sandbox._LIVE_TARGET_LEAF)

        with pytest.raises(sandbox.SandboxCeilingUnsealable, match="non-regular file"):
            sandbox._materialize_live_target_mask_target()

    @_POSIX_ONLY
    def test_a_resolving_link_refuses_the_spawn(self, crew_home, tmp_path) -> None:
        """A mount follows its target, so the mask would bind the referent while the
        lexical name stayed an agent-replaceable link. POSIX-only: creating a symlink
        on Windows needs a privilege the runner may not hold, which would make this a
        flake rather than a check."""
        elsewhere = tmp_path / "elsewhere.json"
        elsewhere.write_text("{}\n", encoding="utf-8")
        (crew_home / sandbox._LIVE_TARGET_LEAF).symlink_to(elsewhere)

        with pytest.raises(sandbox.SandboxCeilingUnsealable):
            sandbox._materialize_live_target_mask_target()

    def test_the_temp_is_staged_in_a_masked_directory_not_beside_the_target(
        self, crew_home, monkeypatch
    ) -> None:
        """A temp beside the target sits in the data-home root, which every sandbox can
        see and ``link(2)``; a bind mask covers a path, not the inode behind it. The
        stub is therefore staged inside a hidden directory that is masked in every mode
        and precreated so the mask has a mount target."""
        seen: dict = {}
        real = sandbox._publish_empty_ceiling

        def _spy(target, parent, content=sandbox._EMPTY_CEILING_DOCUMENT):
            seen["parent"] = parent
            return real(target, parent, content=content)

        monkeypatch.setattr(sandbox, "_publish_empty_ceiling", _spy)
        assert sandbox._materialize_live_target_mask_target() is not None
        assert seen["parent"] == str(crew_home / sandbox._LIVE_TARGET_STAGING_LEAF)
        assert seen["parent"] != str(crew_home), "the temp must never be staged in the root"
        assert sandbox._LIVE_TARGET_STAGING_LEAF in sandbox._CREW_HIDDEN_LEAVES
        assert sandbox._LIVE_TARGET_STAGING_LEAF in sandbox._CREW_PRECREATE_HIDDEN_DIR_LEAVES
        # Nothing is left behind in the staging directory after the publish.
        assert list((crew_home / sandbox._LIVE_TARGET_STAGING_LEAF).iterdir()) == []

    @pytest.mark.parametrize("mode", _MODES)
    @pytest.mark.parametrize("prefix", _CREW_PREFIXES)
    @_POSIX_ONLY
    def test_the_staging_directory_is_masked_in_every_mode(self, mode, prefix) -> None:
        assert _crew_path(prefix, sandbox._LIVE_TARGET_STAGING_LEAF) in _launcher_hidden(mode)

    def test_the_published_stub_has_exactly_one_link(self, crew_home) -> None:
        target = sandbox._materialize_live_target_mask_target()
        assert target is not None
        assert os.stat(target).st_nlink == 1

    def test_a_pre_existing_pointer_with_a_second_hard_link_refuses_the_spawn(
        self, crew_home
    ) -> None:
        """A second name on the pointer's inode is an unmasked path to the bytes the
        gateway executes, wherever it came from."""
        pointer = crew_home / sandbox._LIVE_TARGET_LEAF
        pointer.write_text('{"checkout": "/real"}\n', encoding="utf-8")
        os.link(pointer, crew_home / "sneaky-alias")
        with pytest.raises(sandbox.SandboxCeilingUnsealable, match="hard links"):
            sandbox._materialize_live_target_mask_target()

    def test_a_link_planted_during_publish_refuses_the_spawn(self, crew_home, monkeypatch):
        """Simulate the race the masked staging dir exists to prevent: someone links the
        just-published inode before the materialiser's post-publish check."""
        real = sandbox._publish_empty_ceiling

        def _link_after_publish(target, parent, content=sandbox._EMPTY_CEILING_DOCUMENT):
            ok = real(target, parent, content=content)
            if ok:
                os.link(target, crew_home / "racer-alias")
            return ok

        monkeypatch.setattr(sandbox, "_publish_empty_ceiling", _link_after_publish)
        with pytest.raises(sandbox.SandboxCeilingUnsealable, match="hard links"):
            sandbox._materialize_live_target_mask_target()

    def test_the_linux_spawn_path_calls_it(self) -> None:
        """Pinned by source: the mask is built from the live host, which CI cannot dirty."""
        assert "_materialize_live_target_mask_target()" in inspect.getsource(
            sandbox.namespace_argv
        ), "the namespace spawn path does not materialise the live-target mask target"


class TestThePublishedDocumentReadsAsAbsent:
    """The stub must be indistinguishable from "no pointer" to every reader.

    Otherwise materialising it would make every Linux host log an ignored live target at
    boot and show an unusable pointer in the fleet view.
    """

    def test_the_sandbox_document_matches_the_module_that_owns_it(self) -> None:
        assert sandbox._LIVE_TARGET_PRECREATE_CONTENT == live_target.NO_TARGET_DOCUMENT.encode()

    def test_the_staging_leaf_is_spelled_the_same_in_both_modules(self) -> None:
        """The pointer's writer stages in the directory the launcher masks."""
        assert live_target._STAGING_LEAF == sandbox._LIVE_TARGET_STAGING_LEAF

    def test_the_reader_reports_no_target_and_no_complaint(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(live_target.loader, "config_dir", lambda: tmp_path)
        (tmp_path / "live_target.json").write_text(live_target.NO_TARGET_DOCUMENT, encoding="utf-8")

        assert live_target.read_target_reason() == (None, None)

    def test_a_missing_key_still_complains(self, tmp_path, monkeypatch) -> None:
        """Only an EXPLICIT null is the sentinel: a hand-edit or a typo'd key keeps its
        diagnostic, so this does not blunt the reader."""
        monkeypatch.setattr(live_target.loader, "config_dir", lambda: tmp_path)
        (tmp_path / "live_target.json").write_text('{"chekout": "/x"}\n', encoding="utf-8")

        target, reason = live_target.read_target_reason()

        assert target is None
        assert reason is not None and "no 'checkout' string" in reason

    def test_a_snapshot_of_the_stub_is_restorable(self, tmp_path, monkeypatch) -> None:
        """``restore(None)`` UNPINS (publishes the stub itself), so a snapshot of
        the stub round-trips either way; make-live must still see the stub as
        content to put back rather than as absence, so the two paths agree."""
        monkeypatch.setattr(live_target.loader, "config_dir", lambda: tmp_path)
        pointer = tmp_path / "live_target.json"
        pointer.write_text(live_target.NO_TARGET_DOCUMENT, encoding="utf-8")

        prior = live_target.snapshot()
        assert prior == live_target.NO_TARGET_DOCUMENT

        pointer.write_text('{"checkout": "/elsewhere"}\n', encoding="utf-8")
        assert live_target.restore(prior) is True
        assert live_target.read_target_reason() == (None, None)
