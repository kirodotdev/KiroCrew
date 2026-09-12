"""Dev Fleet's backend gets the live-target pointer back out of the sandbox masks.

The pointer (``live_target.json``) names the checkout the gateway ``execve``s into at
startup, so it is masked from sandboxed processes: an agent that could write it would
choose the code the whole host runs next. But Dev Fleet's backend is itself a sandboxed
spawn (``apps/backend.py`` wraps it), and it is the pointer's ONLY legitimate writer
(make-live) as well as the reader that resolves which worktree is live — so the blanket
mask fenced the app from the one file it owns.

What that cost, measured on a real macOS host and the reason this file exists:

* ``live_target.snapshot()`` took ``[Errno 1] Operation not permitted`` from the Seatbelt
  deny rule, and make-live refused ITSELF — correctly, because ``restore(None)`` reads an
  absent prior pointer as "there was nothing here" and DELETES it, so continuing would let
  a failed restart destroy a live target the cutover merely could not read;
* ``read_target()`` maps the same EPERM to ``None`` ("nothing to honour"), so the fleet
  view reported no live worktree while a gateway was actively serving a pinned one.

The fix is the mechanism md-notebook already established: the leaf stays in
``_CREW_HIDDEN_LEAVES`` for every other sandboxed process and comes back to this ONE
provenance-checked builtin spawn through ``_APP_BACKEND_OWNED_LEAVES``. These tests pin
both halves — carved for dev-fleet, masked by default — plus the literal-drift guard, in
every mode and on both platform builders.
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
    """The mask, the carve-out and the module that owns the file must agree on the name.

    ``sandbox.py`` spells the leaf itself rather than importing ``service.live_target``,
    deliberately — this is a low-level module and that import drags in the config loader.
    A test-time import costs nothing, so the spelling is pinned here instead.
    """

    def test_the_leaf_matches_the_pointer_filename(self) -> None:
        assert sandbox._LIVE_TARGET_LEAF == live_target.pointer_path().name

    def test_the_app_name_matches_the_manifest(self) -> None:
        manifest = (
            os.path.dirname(inspect.getfile(sandbox)),
            "apps",
            "builtins",
            "dev_fleet",
            "app.json",
        )
        with open(os.path.join(*manifest), encoding="utf-8") as handle:
            assert json.load(handle)["name"] == sandbox.DEV_FLEET_APP_NAME


class TestTheDevFleetBackendGetsThePointerBack:
    """The owned-leaf helper resolves the pointer for dev-fleet, and both builders drop it."""

    def test_the_helper_resolves_both_home_spellings(self) -> None:
        targets = sandbox.app_backend_visible_targets(sandbox.DEV_FLEET_APP_NAME)

        for prefix in _CREW_PREFIXES:
            assert _crew_path(prefix, sandbox._LIVE_TARGET_LEAF) in targets

    def test_no_staging_leaf_is_carved_out(self) -> None:
        """Only the pointer — ``write_target`` stages a sibling temp in the unmasked crew
        home root, so nothing else needs unmasking and a wider carve-out would be a
        gratuitous hole."""
        assert set(sandbox._APP_BACKEND_OWNED_LEAVES[sandbox.DEV_FLEET_APP_NAME]) == {
            sandbox._LIVE_TARGET_LEAF
        }

    @_POSIX_ONLY
    def test_linux_unhides_the_pointer_for_this_spawn(self) -> None:
        hidden = _launcher_hidden(
            "standard",
            extra_visible_dirs=sandbox.app_backend_visible_targets(sandbox.DEV_FLEET_APP_NAME),
        )

        for prefix in _CREW_PREFIXES:
            target = _crew_path(prefix, sandbox._LIVE_TARGET_LEAF)
            assert target not in hidden, "the pointer is still bind-masked for its own backend"

    def test_macos_drops_every_rule_for_the_pointer(self) -> None:
        """Read AND write: the reported failure is the ``snapshot()`` READ, and the
        cutover's rename onto the literal needs the write side in the same spawn."""
        profile = sandbox._build_seatbelt_profile(
            "standard",
            extra_visible_dirs=sandbox.app_backend_visible_targets(sandbox.DEV_FLEET_APP_NAME),
        )

        for prefix in _CREW_PREFIXES:
            target = _crew_path(prefix, sandbox._LIVE_TARGET_LEAF)
            assert f'"{target}"' not in profile, "the pointer still carries a deny rule"


class TestEverythingElseKeepsTheMask:
    """The exemption is per-spawn: an agent subprocess must not gain the pointer."""

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

    def test_another_app_gets_nothing(self) -> None:
        """Keyed by app name, so a different backend's spawn is unchanged."""
        others = sandbox.app_backend_visible_targets(sandbox.MD_NOTEBOOK_APP_NAME)

        assert others, "the md-notebook carve-out disappeared"
        assert not [
            target for target in others if os.path.basename(target) == sandbox._LIVE_TARGET_LEAF
        ], "another app's backend was handed the live-target pointer"
        assert sandbox.app_backend_visible_targets("no-such-app") == ()


@pytest.fixture()
def crew_home(tmp_path, monkeypatch):
    """An isolated crew data home, so no test here touches the developer's own pointer."""
    home = tmp_path / ".kiro" / "crew"
    home.mkdir(parents=True)
    monkeypatch.setattr(sandbox, "config_dir", lambda: home)
    monkeypatch.setattr(sandbox.Path, "home", staticmethod(lambda: tmp_path))
    return home


class TestTheMaskGetsAMountTarget:
    """``mount(2)`` cannot mask an absent path, and the carve-out makes this leaf creatable.

    Before the carve-out the gap was vacuous: the pointer's only writer took EPERM from
    this mask, so on a sandboxed host the file never came into existence. Unmasking it for
    the dev-fleet spawn makes creation possible, so a namespace spawned while the pointer
    was absent would see — and be able to WRITE — the file Dev Fleet creates later, which
    selects the code the gateway ``execve``s into next.
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
        """``restore(None)`` DELETES the pointer, so make-live must see the stub as
        content to put back rather than as absence."""
        monkeypatch.setattr(live_target.loader, "config_dir", lambda: tmp_path)
        pointer = tmp_path / "live_target.json"
        pointer.write_text(live_target.NO_TARGET_DOCUMENT, encoding="utf-8")

        prior = live_target.snapshot()
        assert prior == live_target.NO_TARGET_DOCUMENT

        pointer.write_text('{"checkout": "/elsewhere"}\n', encoding="utf-8")
        assert live_target.restore(prior) is True
        assert live_target.read_target_reason() == (None, None)


class TestTheSpawnDoesNotInheritStartupHooks:
    """The pointer is WRITABLE in this namespace, so an agent-writable ``sitecustomize``
    or user-site ``.pth`` running here would choose the gateway's next image. ``-I`` is
    what shuts those off, and it must cover dev-fleet as well as md-notebook."""

    def test_dev_fleet_is_in_the_isolated_startup_set(self) -> None:
        source = inspect.getsource(sys.modules["kiro_crew.apps.backend"])

        assert "_isolated_startup_apps = (MD_NOTEBOOK_APP_NAME, DEV_FLEET_APP_NAME)" in source
        assert "_shipped_carveout_builtin" in source

    def test_every_carveout_app_is_in_that_set(self) -> None:
        """The drift gate: an app that gains a carve-out must gain the isolation with it."""
        import kiro_crew.apps.backend as backend

        source = inspect.getsource(backend)
        for app in sandbox._APP_BACKEND_OWNED_LEAVES:
            constant = {
                sandbox.MD_NOTEBOOK_APP_NAME: "MD_NOTEBOOK_APP_NAME",
                sandbox.DEV_FLEET_APP_NAME: "DEV_FLEET_APP_NAME",
            }.get(app)
            assert constant is not None, (
                f"{app} owns hidden leaves but this test does not know its name constant; "
                "decide explicitly whether its spawn needs -I and extend this map"
            )
            assert constant in source.split("_isolated_startup_apps = ", 1)[1].split(")", 1)[0]
