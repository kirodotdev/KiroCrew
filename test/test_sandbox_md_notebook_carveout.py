"""The hardening that rides with the md-notebook backend's mask carve-out.

The carve-out ITSELF is not retested here: ``_APP_BACKEND_OWNED_LEAVES`` names the hidden
leaves the Notes backend owns, ``app_backend_visible_targets`` resolves them, and
``apps/backend.py`` passes them as ``extra_visible_dirs`` for exactly that spawn; a spelling
beneath a foreign mask is refused by ``carveout_shadowed_by_foreign_mask``. Both are pinned
in ``test_sandbox_governance_mask.py``.

What this file pins is what that carve-out OPENS, and the guards that close it. Making the
three state files writable on a sandboxed host for the first time exposed two credential
paths:

* an ABSENT leaf gets no mask at all — ``mount(2)`` cannot target a missing path and the
  launcher's hiding loops guard on existence — so an agent namespace spawned before the
  first vault attach reads the PAT saved after it;
* the state writers staged their temp BESIDE the target, so a file holding the real PAT
  bytes sat at a name none of the three leaf masks covers, and a SIGKILL between write and
  rename left it readable by a same-uid agent forever.

The answers are :func:`sandbox._materialize_md_notebook_mask_targets` and the masked
top-level ``md-notebook-staging`` directory every state writer now publishes through.
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest

import kiro_crew.sandbox as sb

_MODES = ("standard", "cc", "strict")
_CREW_PREFIXES = (".kiro/crew", ".kirocrew")


@pytest.fixture(autouse=True)
def _pin_ssh_accept_new(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin ``_ssh_supports_accept_new`` at the seam the namespace plan reads.

    The real probe runs the host's ``ssh -V``. It is ``lru_cache``d, but any test
    that clears the cache (``TestSshSupportsAcceptNew`` does) hands the next
    plan-building test in the process a real spawn -- a host program none of these
    tests is about (test-hygiene class 7). ``True`` is what a modern host answers.
    """
    monkeypatch.setattr("kiro_crew.sandbox._ssh_supports_accept_new", lambda: True)


def _crew_path(prefix: str, leaf: str) -> str:
    from pathlib import Path

    return os.path.join(str(Path.home()), *prefix.split("/"), *leaf.split("/"))


def _hidden_dirs(mode: str, **kwargs: object) -> set[str]:
    """The directories the Linux launcher bind-masks for one spawn: the plan's masks."""
    return set(sb._spawn_plan("namespace", mode, **kwargs).sensitive_dirs)  # type: ignore[arg-type]


@pytest.fixture()
def crew_home(tmp_path, monkeypatch):
    """An isolated crew data home, shared by every class in this module.

    Module-level so the mask classes and the sweep classes cannot drift onto different
    isolation: the ``Path.home`` patch below is what keeps this suite from deleting
    ``*.tmp`` files in the developer's real data home, and a class that quietly lacked it
    would do exactly that.
    """
    home = tmp_path / ".kiro" / "crew"
    home.mkdir(parents=True)
    monkeypatch.setattr(sb, "config_dir", lambda: home)
    # The sweep resolves the LEGACY home spelling from ``Path.home()`` as well, which no
    # ``KIROCREW_HOME`` pin covers.
    monkeypatch.setattr(sb.Path, "home", staticmethod(lambda: tmp_path))
    return home


class TestALinkedChainMasksTheWholeStateDirectory:
    """A linked ``workspace/md-notebook`` chain hides that whole directory; a healthy one
    does NOT.

    Skipping the sweep on a linked chain leaves one thing behind that the legacy-name masks
    cannot cover: a legacy staging orphan holding real PAT bytes, whose name is neither a
    bare state file nor ``.state``. A directory mask covers every name inside it.

    The negative half matters just as much. That directory also holds the vault clone data,
    which agents are MEANT to read, so masking it wholesale on a healthy host would hide
    the user's notes and defeat the app. The mask therefore appears only where the chain is
    linked, which is where the sweep cannot run.
    """

    def test_a_healthy_host_does_not_mask_the_state_directory(self, crew_home) -> None:
        (crew_home / "workspace" / "md-notebook" / "vaults").mkdir(parents=True)

        assert sb._md_notebook_degraded_mask_dirs() == [], (
            "a healthy host masked the whole app directory, which would hide the vault "
            "clone data agents are meant to read"
        )

    def test_a_planted_link_masks_the_state_directory(self, crew_home, tmp_path) -> None:
        elsewhere = tmp_path / "elsewhere-mask-dir"
        elsewhere.mkdir()
        (crew_home / "workspace").mkdir(parents=True)
        (crew_home / "workspace" / "md-notebook").symlink_to(elsewhere, target_is_directory=True)

        masked = sb._md_notebook_degraded_mask_dirs()

        # The degraded fallback masks the whole ``md-notebook/`` directory (reached through
        # the planted link): it holds the legacy bare names, the retired ``.state/`` AND any
        # orphan beside them, so masking it fences every legacy exposure at once.
        assert str(crew_home / "workspace" / "md-notebook") in masked

    def test_the_orphan_the_sweep_cannot_delete_is_masked_instead(self, crew_home, tmp_path):
        """The two halves meet here: the sweep leaves the orphan, the mask hides it.

        A directory the sweep refuses to descend keeps its orphan, so the mask must name
        the bare ``md-notebook/`` directory — the orphan sits as its DIRECT child, at a name
        no legacy-name mask covers, so it would otherwise stay readable in every sandbox.
        """
        victim = tmp_path / "victim-orphan"
        (victim / "md-notebook").mkdir(parents=True)
        orphan = victim / "md-notebook" / "pat.tmp"
        orphan.write_text("ghp_realtoken")
        (crew_home / "workspace").symlink_to(victim, target_is_directory=True)

        removed = sb._sweep_legacy_md_notebook_temps()

        assert orphan.exists(), "the sweep deleted through a planted link"
        assert removed == []
        parent_dir = str(crew_home / "workspace" / "md-notebook")
        assert parent_dir in sb._md_notebook_degraded_mask_dirs(), (
            "the sweep could not delete the orphan AND the directory holding it is "
            "unmasked, so the PAT bytes in it stay readable inside the sandbox"
        )

    def test_both_launch_paths_carry_the_degraded_mask(self, monkeypatch, tmp_path) -> None:
        """Linux and macOS each render their own launch artifact, so a fix that reached
        one would leave the other exposed. The live host is healthy in CI, so the degraded
        set is supplied at the host seam both plans read, and both must mask it in every
        mode: the namespace plan the launcher renders, and the Seatbelt profile itself."""
        state_dir = str(tmp_path / ".kiro" / "crew" / "workspace" / "md-notebook")
        monkeypatch.setattr(sb, "_md_notebook_degraded_mask_dirs", lambda: [state_dir])

        for mode in _MODES:
            plan = sb._spawn_plan("namespace", mode)
            assert (
                state_dir in plan.sensitive_dirs
            ), f"the {mode} Linux launcher does not carry the degraded state-directory mask"
            profile = sb._build_seatbelt_profile(mode)
            assert (
                f"(deny file-read* (subpath {json.dumps(state_dir)}))" in profile
            ), f"the {mode} Seatbelt profile does not carry the degraded state-directory mask"


class TestTheStateDirectoryIsMaskedAndCarvedBack:
    """The top-level state directory is fenced from agents and carved back to the backend.

    Every state file, the migration marker and every in-flight temp live inside it, so the
    mask stays in every mode for every OTHER sandboxed process, while the Notes backend's
    own spawn gets the directory back so it can read and publish its state.
    """

    @pytest.mark.parametrize("mode", _MODES)
    @pytest.mark.parametrize("prefix", _CREW_PREFIXES)
    def test_the_state_dir_is_masked_in_every_mode(self, mode: str, prefix: str) -> None:
        assert _crew_path(prefix, sb._MD_NOTEBOOK_STATE_DIR) in _hidden_dirs(mode)

    def test_the_state_dir_is_the_top_level_staging_leaf(self) -> None:
        # One directory, one name: the mask, the carve-out, the precreate list and the
        # sensitive-path fence all name the same top-level leaf.
        assert sb._MD_NOTEBOOK_STATE_DIR == sb._MD_NOTEBOOK_STAGING_LEAF == "md-notebook-staging"

    def test_the_backend_gets_the_state_dir_back(self) -> None:
        targets = sb.app_backend_visible_targets(sb.MD_NOTEBOOK_APP_NAME)
        # Under whichever home spelling resolves on this host — the carve-out refuses a
        # spelling whose parent chain holds a symlink.
        assert any(
            t.endswith(os.sep + sb._MD_NOTEBOOK_STATE_DIR) for t in targets
        ), "the state directory must be carved back to the backend"
        # Nothing under ``workspace/md-notebook`` is carved back: the legacy spellings are
        # read only by the gateway-side migration.
        assert not any(f"workspace{os.sep}md-notebook" in t for t in targets), targets

    def test_the_launcher_lifts_the_state_mask_for_the_backend_spawn_only(self) -> None:
        carved = sb.app_backend_visible_targets(sb.MD_NOTEBOOK_APP_NAME)
        assert carved, "no carve-out resolved, so this test would pass vacuously"
        hidden_for_backend = _hidden_dirs("standard", extra_visible_dirs=carved)
        hidden_for_agents = _hidden_dirs("standard")
        for target in carved:
            assert target not in hidden_for_backend, f"{target} still masked for the backend"
            assert target in hidden_for_agents, f"{target} unmasked for an ordinary spawn"

    def test_the_precreate_table_covers_exactly_the_state_leaves(self) -> None:
        """Materialising a leaf needs its own absent-equivalence argument, so the two
        tables must not drift: a leaf added to the mask without one would be created
        with no proof that empty means absent to its reader."""
        assert set(sb._MD_NOTEBOOK_PRECREATE_CONTENT) == set(sb._MD_NOTEBOOK_STATE_LEAVES)
        for leaf in sb._MD_NOTEBOOK_PRECREATE_CONTENT:
            assert os.path.dirname(leaf) == sb._MD_NOTEBOOK_STATE_DIR


class TestANewOwnedLeavesAppCannotSilentlySkipMaterialization:
    """A drift gate: `_APP_BACKEND_OWNED_LEAVES` is generic, this hardening is not.

    The materialiser, the precreate table, and the `-I` startup isolation are all keyed to
    md-notebook by name, while the owned-leaves table any app can join is a plain dict. An
    app added there would get its leaves unmasked for its own spawn — and would silently
    reopen BOTH holes this file exists to close: no mount target for an absent leaf, and
    interpreter startup hooks running in a namespace holding its secret. Neither failure
    is visible at runtime, so the omission has to be loud HERE instead.
    """

    def test_every_owned_leaves_app_is_covered_by_materialization(self) -> None:
        # md-notebook owns the whole top-level state DIRECTORY. Its mount target is
        # precreated by ``_materialize_maskable_dirs`` (it is in
        # ``_CREW_PRECREATE_HIDDEN_DIR_LEAVES``), and the per-file materialiser gives
        # ``pat``/``vaults.json``/``settings.json`` inside it absent-equivalent documents
        # before launch.
        covered = {
            sb.MD_NOTEBOOK_APP_NAME: {sb._MD_NOTEBOOK_STATE_DIR},
            # NOT dev-fleet: its live-target pointer stays masked from its own backend
            # (build children share that namespace — see
            # test_sandbox_dev_fleet_live_target.py); the pointer is still materialised
            # before every spawn, but as a mask TARGET, not as an owned leaf.
        }
        assert set(sb._APP_BACKEND_OWNED_LEAVES) == set(covered), (
            "an app gained entries in _APP_BACKEND_OWNED_LEAVES without materialisation "
            "coverage. Its own spawn now sees those leaves, so before shipping it you must "
            "(a) give every FILE leaf an absent-equivalent document with a written "
            "per-leaf argument, the way _MD_NOTEBOOK_PRECREATE_CONTENT does, and "
            "materialise it before launch — otherwise an absent leaf gets NO mask and a "
            "namespace spawned earlier reads whatever the backend writes later; and "
            "(b) decide explicitly whether that spawn needs the -I isolated startup "
            "md-notebook uses, since a bare `python -m` runs agent-writable "
            "sitecustomize/usercustomize inside the one namespace where its secret is "
            "unmasked. Then extend this table."
        )
        for app, leaves in sb._APP_BACKEND_OWNED_LEAVES.items():
            assert set(leaves) == covered[app], (
                f"{app}'s owned leaves and its materialisation coverage have drifted: "
                f"{set(leaves) ^ covered[app]}"
            )
        # The state directory's mount target is precreated, and every state FILE inside it
        # still carries its own absent-equivalence argument, so the precreate tables must
        # cover exactly the directory and the three state leaves.
        assert sb._MD_NOTEBOOK_STATE_DIR in sb._CREW_PRECREATE_HIDDEN_DIR_LEAVES
        assert set(sb._MD_NOTEBOOK_PRECREATE_CONTENT) == set(sb._MD_NOTEBOOK_STATE_LEAVES)
        assert {f"{sb._MD_NOTEBOOK_STATE_DIR}/{n}" for n in sb._MD_NOTEBOOK_STATE_FILES} == set(
            sb._MD_NOTEBOOK_STATE_LEAVES
        )


class TestAbsentStateFilesAreMaterializedSoTheMaskCanMount:
    """``mount(2)`` cannot target an absent path and the launcher's hiding loops
    guard on existence, so an absent leaf gets NO mask in the spawned namespace.
    The carve-out makes these three files creatable on a sandboxed host for the
    first time, so an agent namespace spawned before the first vault attach
    would read the PAT saved after it — unless the absent-equivalent documents
    are materialised before launch."""

    def test_the_fixture_really_isolates_the_real_home(self, crew_home, tmp_path):
        """Guard the guard: if ``Path.home`` ever stops being patched here, this suite
        would sweep the developer's own crew home. Fail loudly instead."""
        assert sb.Path.home() == tmp_path

    def test_every_leaf_is_created_with_its_absent_equivalent_document(self, crew_home):
        created = sb._materialize_md_notebook_mask_targets()
        state = crew_home / sb._MD_NOTEBOOK_STATE_DIR
        assert set(created) == {str(state / n) for n in ("pat", "vaults.json", "settings.json")}
        assert (state / "vaults.json").read_bytes() == b"[]\n"
        assert (state / "settings.json").read_bytes() == b"{}\n"
        assert (state / "pat").read_bytes() == b""
        # The PAT mount target is a credential path: owner-only from birth.
        assert os.stat(state / "pat").st_mode & 0o077 == 0

    def test_the_state_dir_is_materialized_by_the_shared_direct_child_path(self) -> None:
        """The state directory is a DIRECT child of the data home, so it belongs to
        ``_materialize_maskable_dirs`` — whose plain ``mkdir`` is only sound for direct
        children — rather than to the per-file md-notebook materialiser."""
        assert sb._MD_NOTEBOOK_STATE_DIR in sb._CREW_PRECREATE_HIDDEN_DIR_LEAVES
        assert "/" not in sb._MD_NOTEBOOK_STATE_DIR

    def test_the_state_dir_has_no_agent_writable_ancestor(self) -> None:
        """A mask covers the leaf, NOT its ancestors. Under ``workspace/md-notebook`` — a
        tree the agent can write at OS level — the state dir could be renamed out from
        under its own mask, and a later PAT write would publish through the replacement,
        unmasked, into a live agent's view. Top-level, like ``aws-control-staging``: every
        state file's parent is the state directory, and the state directory's parent is the
        data-home root itself."""
        assert not sb._MD_NOTEBOOK_STATE_DIR.startswith("workspace/")
        assert os.path.dirname(sb._MD_NOTEBOOK_STATE_DIR) == ""
        for leaf in sb._MD_NOTEBOOK_STATE_LEAVES:
            assert os.path.dirname(leaf) == sb._MD_NOTEBOOK_STATE_DIR

    def test_the_backend_and_the_mask_name_the_same_state_dir(self) -> None:
        """The backend spells its state dir and file names itself rather than importing
        the sandbox module into the app backend's process, so the two spellings are pinned
        here: a mismatch would stage PAT bytes at a name nothing masks."""
        from kiro_crew.apps.builtins.md_notebook import server

        assert server._STATE_DIR_LEAF == sb._MD_NOTEBOOK_STATE_DIR
        assert server._STATE_FILE_NAMES == sb._MD_NOTEBOOK_STATE_FILES
        assert server._RETIRED_STATE_SUBDIR == os.path.basename(sb._MD_NOTEBOOK_RETIRED_STATE_DIR)

    def test_the_backend_resolves_its_state_dir_directly_under_the_data_home(
        self, crew_home, monkeypatch
    ):
        """End to end on the backend's own resolver: the live state directory is a direct
        child of the crew data-home root, not of ``workspace/md-notebook``."""
        from kiro_crew.apps.builtins.md_notebook import server

        monkeypatch.setattr(server, "_HOME", None)
        monkeypatch.setattr(server, "config_dir", lambda: crew_home)
        assert server._state_dir() == crew_home / sb._MD_NOTEBOOK_STATE_DIR
        assert server._state_dir().parent == crew_home
        assert server._pat_file().parent == server._state_dir()
        assert server._migration_marker_path().parent == server._state_dir()

    def test_the_sweep_runs_on_the_macos_launch_path_too(self) -> None:
        """Materialising is Linux-only for a real reason — a Seatbelt deny is a path rule
        that holds for a name that does not exist yet — but that does NOT transfer to an
        orphan already on disk at a name no rule names. The profile denies the leaves and
        the staging dir, never an arbitrary ``*.tmp`` sibling, so skipping macOS would
        leave a pre-upgrade token readable there forever."""
        import inspect

        for fn in (sb.namespace_argv, sb.sandbox_exec_argv):
            assert "_sweep_legacy_md_notebook_temps()" in inspect.getsource(
                fn
            ), f"{fn.__name__} does not sweep legacy md-notebook staging temps"

    def test_the_documents_read_as_absent_to_the_backend(self, crew_home, monkeypatch):
        """The whole materialisation argument: an empty document must mean what
        an absent file means TO THE READER. Pin it against the backend's own
        read functions rather than restating their behavior here."""
        from kiro_crew.apps.builtins.md_notebook import server

        sb._materialize_md_notebook_mask_targets()
        monkeypatch.setattr(server, "_HOME", None)
        monkeypatch.setattr(server, "config_dir", lambda: crew_home)
        assert (server._state_dir() / "pat").is_file(), "the stub this test reads is absent"
        assert server._read_vaults_sync() == []
        assert server._read_settings_sync() == server._default_settings()
        assert server._read_pat_sync() is None

    def test_existing_state_is_left_byte_for_byte_alone(self, crew_home):
        state = crew_home / sb._MD_NOTEBOOK_STATE_DIR
        state.mkdir(parents=True)
        (state / "vaults.json").write_text('[{"id": "real"}]')
        assert sb._materialize_md_notebook_mask_targets()  # creates only the other two
        assert (state / "vaults.json").read_text() == '[{"id": "real"}]'

    def test_a_legacy_sibling_temp_is_swept(self, crew_home):
        """A pre-upgrade writer staged BESIDE the target, so a SIGKILL in that window
        left real PAT bytes at a name no mask covers — not the three leaves, not
        the staging directory. Materialising forward cannot help an artefact on disk, so
        the orphan is removed."""
        state = crew_home / "workspace" / "md-notebook"
        state.mkdir(parents=True)
        orphans = [
            state / "tmpab12cd34.tmp",  # atomic_write's mkstemp(dir=parent)
            state / "vaults.json.deadbeef.tmp",  # git_ops.staged_temp_name
            state / "settings.json.cafe1234.tmp",
        ]
        for o in orphans:
            o.write_text("ghp_leaked_token")

        removed = sb._sweep_legacy_md_notebook_temps()

        for o in orphans:
            assert not o.exists(), f"legacy PAT-bearing temp survived: {o}"
        # The returned list is the caller-visible record of WHICH credentials were exposed,
        # which is what the operator needs in order to know what to rotate — assert it
        # rather than leaving it as bookkeeping nothing reads.
        assert set(removed) == {str(o) for o in orphans}

    def test_the_sweep_keeps_real_state_and_clone_data(self, crew_home):
        """Only ``*.tmp`` DIRECT children are orphans by construction. The state files,
        the staging dir, and the vault clones must all survive."""
        state = crew_home / "workspace" / "md-notebook"
        (state / "vaults").mkdir(parents=True)
        (state / "vaults" / "v1").mkdir()
        (state / "vaults" / "v1" / "note.md.abcd.tmp").write_text("a note temp, not ours")
        (state / "pat").write_text("ghp_real")
        (state / "vaults.json").write_text("[]")

        sb._sweep_legacy_md_notebook_temps()
        sb._materialize_md_notebook_mask_targets()

        assert (state / "pat").read_text() == "ghp_real"
        assert (state / "vaults.json").read_text() == "[]"
        assert (
            state / "vaults" / "v1" / "note.md.abcd.tmp"
        ).exists(), "the sweep reached into a vault clone; it must only touch direct children"

    def test_a_legacy_home_orphan_is_swept_too(self, crew_home, tmp_path):
        """The mask covers BOTH crew-home spellings, so an orphan under an un-migrated or
        rolled-back ``~/.kirocrew`` is exposed exactly like one under the live home. This
        is the opposite requirement from materialising, which is live-home-only because a
        stub in a home nothing reads would be a file nobody opens — a token already written
        to the legacy home stays readable whichever home is live now."""
        legacy = tmp_path / ".kirocrew" / "workspace" / "md-notebook"
        legacy.mkdir(parents=True)
        orphan = legacy / "tmplegacy1.tmp"
        orphan.write_text("ghp_leaked_token")

        sb._sweep_legacy_md_notebook_temps()

        assert not orphan.exists(), "a legacy-home PAT temp survived the sweep"

    def test_a_relocated_home_orphan_is_swept_too(self, tmp_path, monkeypatch):
        """The live home is resolved through ``config_dir()``, which ``KIROCREW_HOME`` can
        move OUT from under ``$HOME`` entirely — so the two roots are not interchangeable
        and neither alone is sufficient. Pins the live-home root independently of the
        ``$HOME``-prefix roots, which the other sweep tests happen to share."""
        relocated = tmp_path / "srv" / "crew"
        state = relocated / "workspace" / "md-notebook"
        state.mkdir(parents=True)
        monkeypatch.setattr(sb, "config_dir", lambda: relocated)
        monkeypatch.setattr(sb.Path, "home", staticmethod(lambda: tmp_path / "elsewhere"))
        orphan = state / "tmprelocated.tmp"
        orphan.write_text("ghp_leaked_token")

        sb._sweep_legacy_md_notebook_temps()

        assert not orphan.exists(), "a relocated-home PAT temp survived the sweep"

    def test_the_sweep_refuses_to_delete_through_a_planted_parent_link(self, crew_home, tmp_path):
        """The sweep UNLINKS, so following a planted link is irreversible deletion in a
        tree the agent chose — strictly worse than the materialiser's create-only version
        of the same hazard. The intermediate components are agent-writable, so the chain
        gets the same planted-link refusal, and the root is skipped rather than swept.

        The planted path resolves COMPLETELY — the victim really does contain an
        ``md-notebook`` directory holding a ``*.tmp`` file — so the open succeeds and the
        chain refusal is the only thing standing between the sweep and the deletion.
        ``O_NOFOLLOW`` cannot help here: it only judges the final component, which is a
        real directory."""
        victim_state = tmp_path / "victim" / "md-notebook"
        victim_state.mkdir(parents=True)
        bystander = victim_state / "unrelated.tmp"
        bystander.write_text("someone else's file")
        # ``workspace`` itself is the planted link, so the state dir resolves inside it.
        (crew_home / "workspace").symlink_to(tmp_path / "victim", target_is_directory=True)

        sb._sweep_legacy_md_notebook_temps()

        assert bystander.exists(), "the sweep deleted through a planted parent link"

    def test_the_sweep_refuses_to_delete_through_a_linked_state_dir(self, crew_home, tmp_path):
        """Same hazard one component deeper, where the LEAF is the link. The
        ``O_NOFOLLOW`` open is what refuses it, and it also pins the directory so a swap
        between the check and the unlink cannot redirect either syscall."""
        victim = tmp_path / "victim2"
        victim.mkdir()
        bystander = victim / "unrelated.tmp"
        bystander.write_text("someone else's file")
        (crew_home / "workspace").mkdir()
        (crew_home / "workspace" / "md-notebook").symlink_to(victim, target_is_directory=True)

        sb._sweep_legacy_md_notebook_temps()

        assert bystander.exists(), "the sweep deleted through a linked state directory"

    def test_the_descent_refuses_a_link_at_every_component_not_just_the_last(
        self, crew_home, tmp_path
    ):
        """``O_NOFOLLOW`` judges only the FINAL component, so opening the joined path is
        unsound however carefully the chain was pre-checked: an intermediate directory
        swapped between the check and the open redirects the whole descent, and the sweep
        then unlinks inside a tree the agent chose. The descent is therefore per-component
        from the trusted anchor, which the kernel enforces at each step instead of at one.

        Exercised directly on the helper because the interesting property is per-component
        refusal, and a chain-level test cannot tell which component did the refusing.
        """
        elsewhere = tmp_path / "elsewhere-descent"
        elsewhere.mkdir()

        # A clean chain opens and is usable.
        (crew_home / "workspace" / "md-notebook").mkdir(parents=True)
        fd = sb._open_dir_anchored(str(crew_home), sb._MD_NOTEBOOK_STATE_COMPONENTS)
        assert fd is not None
        os.close(fd)

        # A link at the LAST component is refused.
        (crew_home / "workspace" / "md-notebook").rmdir()
        (crew_home / "workspace" / "md-notebook").symlink_to(elsewhere, target_is_directory=True)
        assert sb._open_dir_anchored(str(crew_home), sb._MD_NOTEBOOK_STATE_COMPONENTS) is None

        # And so is a link at an INTERMEDIATE component, which is the case a joined-path
        # open with O_NOFOLLOW would have accepted.
        (crew_home / "workspace" / "md-notebook").unlink()
        (crew_home / "workspace").rmdir()
        victim = tmp_path / "victim-descent"
        (victim / "md-notebook").mkdir(parents=True)
        (crew_home / "workspace").symlink_to(victim, target_is_directory=True)
        assert sb._open_dir_anchored(str(crew_home), sb._MD_NOTEBOOK_STATE_COMPONENTS) is None

    def test_the_sweep_spares_an_inflight_ceiling_temp(self, crew_home):
        """``_publish_empty_ceiling`` stages ITS temp in the target's parent — the very
        directory this sweep scans — and publishes with ``os.link``. Two concurrent spawns
        would otherwise let one's sweep unlink the other's temp between the ``mkstemp`` and
        the ``link``, failing that spawn for no reason. Gateway-owned, not an orphan."""
        state = crew_home / "workspace" / "md-notebook"
        state.mkdir(parents=True)
        inflight = state / f"{sb._CEILING_TEMP_PREFIX}abcd1234.tmp"
        inflight.write_bytes(b"")

        sb._sweep_legacy_md_notebook_temps()

        assert inflight.exists(), "the sweep unlinked another spawn's in-flight temp"

    def test_a_non_regular_tmp_is_left_alone_by_the_sweep(self, crew_home):
        """``lstat`` decides, so a link is never followed and a directory is not removed:
        the sweep deletes files, and anything else at such a name is the operator's."""
        state = crew_home / "workspace" / "md-notebook"
        state.mkdir(parents=True)
        (state / "a-dir.tmp").mkdir()

        sb._sweep_legacy_md_notebook_temps()

        assert (state / "a-dir.tmp").is_dir()

    def test_an_unremovable_legacy_temp_refuses_the_spawn(self, crew_home, monkeypatch):
        """Fail-closed like every other branch here: if the orphan cannot be removed,
        launching would hand the agent the PAT this mechanism exists to hide."""
        state = crew_home / "workspace" / "md-notebook"
        state.mkdir(parents=True)
        (state / "tmpstuck.tmp").write_text("ghp_leaked_token")

        def _refuse(path, *a, **k):
            raise OSError(13, "Permission denied")

        monkeypatch.setattr(sb.os, "unlink", _refuse)
        with pytest.raises(sb.SandboxCeilingUnsealable, match="tmpstuck.tmp"):
            sb._sweep_legacy_md_notebook_temps()

    def test_an_absent_data_home_is_not_created(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sb, "config_dir", lambda: tmp_path / "never-made")
        assert sb._materialize_md_notebook_mask_targets() == []
        assert not (tmp_path / "never-made").exists()

    def test_a_failed_publish_refuses_the_spawn(self, crew_home, monkeypatch):
        """Fail-closed: launching anyway would run the agent with a mask the
        launcher silently skips — the exact hole this materialiser closes."""
        monkeypatch.setattr(sb, "_publish_empty_ceiling", lambda *a, **k: False)
        with pytest.raises(sb.SandboxCeilingUnsealable):
            sb._materialize_md_notebook_mask_targets()

    def test_a_linked_app_dir_does_not_reach_the_state_stubs(self, crew_home, tmp_path):
        """A link at ``workspace/md-notebook`` (an operator who symlinks ``workspace/`` to
        another disk) leaves the state stubs untouched: they live in the top-level state
        directory, so the materialiser creates them normally and never writes through the
        link."""
        elsewhere = tmp_path / "elsewhere-mask"
        elsewhere.mkdir()
        (crew_home / "workspace").mkdir(parents=True)
        (crew_home / "workspace" / "md-notebook").symlink_to(elsewhere, target_is_directory=True)

        created = sb._materialize_md_notebook_mask_targets()

        state = crew_home / sb._MD_NOTEBOOK_STATE_DIR
        assert set(created) == {str(state / n) for n in sb._MD_NOTEBOOK_STATE_FILES}
        assert list(elsewhere.iterdir()) == [], (
            "the materialiser followed the planted link and created state at "
            f"its target: {list(elsewhere.iterdir())!r}"
        )

    def test_a_linked_state_dir_is_never_written_through_and_refuses_the_spawn(
        self, crew_home, tmp_path
    ):
        """A link AT the state directory: the per-file materialiser skips (it cannot tell
        where a write would land) and writes nothing through it, and the shared
        direct-child materialiser — which runs first on the spawn path — refuses the spawn,
        so no namespace launches with the mask bound over a referent."""
        elsewhere = tmp_path / "elsewhere-state"
        elsewhere.mkdir()
        (crew_home / sb._MD_NOTEBOOK_STATE_DIR).symlink_to(elsewhere, target_is_directory=True)

        assert sb._materialize_md_notebook_mask_targets() == []
        assert list(elsewhere.iterdir()) == [], "the materialiser wrote through the link"
        with pytest.raises(sb.SandboxCeilingUnsealable):
            sb._materialize_maskable_dirs()

    def test_a_skipped_materialisation_never_launches_with_the_carveout(
        self, crew_home, tmp_path, monkeypatch
    ):
        """THE safety coupling, pinned in one place because separating the two reopens the
        hole this file exists to close.

        The per-file materialiser skips a state file whose chain holds a link, while the
        carve-out filter judges the state DIRECTORY's own chain — so for a link AT the state
        directory the materialiser skips and the carve-out is still granted. That pairing is
        safe only because the same link refuses the spawn before anything launches: in the
        direct-child materialiser and again in the hidden-leaf alias pass. If a future change
        let either refusal lapse, the backend would be handed a carve-out over a linked
        directory with no stubs in it — this test fails first.
        """
        elsewhere = tmp_path / "elsewhere-coupling"
        elsewhere.mkdir()
        (crew_home / sb._MD_NOTEBOOK_STATE_DIR).symlink_to(elsewhere, target_is_directory=True)

        assert (
            sb._materialize_md_notebook_mask_targets() == []
        ), "materialisation did not skip, so this coupling is not under test"
        with pytest.raises(sb.SandboxCeilingUnsealable):
            sb._materialize_maskable_dirs()
        with pytest.raises(sb.SandboxCeilingUnsealable):
            sb._refuse_aliased_masked_leaves()
        assert list(elsewhere.iterdir()) == []

    def test_a_resolving_leaf_symlink_refuses_the_spawn(self, crew_home, tmp_path):
        """A RESOLVING link at the leaf is refused too, not only a dangling one: the
        backend reads ``pat`` through it, so a link there would hand it whatever the link
        names — refuse it before launch rather than trust a referent."""
        real = tmp_path / "somewhere-else-pat"
        real.write_bytes(b"")
        state = crew_home / sb._MD_NOTEBOOK_STATE_DIR
        state.mkdir(parents=True)
        (state / "pat").symlink_to(real)
        with pytest.raises(sb.SandboxCeilingUnsealable):
            sb._materialize_md_notebook_mask_targets()

    def test_a_dangling_leaf_symlink_refuses_the_spawn(self, crew_home, tmp_path):
        """A DANGLING leaf link takes the other route to the same refusal: ``exists()``
        is False for one, so it reaches the publish, where ``os.link`` fails EEXIST on
        the link's own name and the race re-check lstats it as a link."""
        state = crew_home / sb._MD_NOTEBOOK_STATE_DIR
        state.mkdir(parents=True)
        (state / "pat").symlink_to(tmp_path / "nothing-here")
        with pytest.raises(sb.SandboxCeilingUnsealable):
            sb._materialize_md_notebook_mask_targets()

    def test_a_special_file_at_a_leaf_refuses_the_spawn(self, crew_home):
        """An EXISTING target is acceptable only as a regular file: a FIFO at ``pat``
        would block or mislead the backend's reader."""
        state = crew_home / sb._MD_NOTEBOOK_STATE_DIR
        state.mkdir(parents=True)
        os.mkfifo(state / "pat")
        with pytest.raises(sb.SandboxCeilingUnsealable):
            sb._materialize_md_notebook_mask_targets()

    def test_a_raced_special_file_refuses_the_spawn(self, crew_home, monkeypatch):
        """A LOST publish race is benign only when the winner clears the same
        regular-file bar the pre-check enforces: an agent racing mkfifo between
        validation and publish must not have its non-file accepted on bare
        existence."""

        real_publish = sb._publish_empty_ceiling

        def _raced(target, parent, content=b"{}\n"):
            if target.endswith("pat") and not os.path.exists(target):
                os.mkfifo(target)  # the racing writer wins with a FIFO
                return False  # our publish loses (os.link EEXIST -> False)
            return real_publish(target, parent, content=content)

        monkeypatch.setattr(sb, "_publish_empty_ceiling", _raced)
        with pytest.raises(sb.SandboxCeilingUnsealable):
            sb._materialize_md_notebook_mask_targets()

    def test_namespace_argv_materializes_before_the_launcher_runs(self, crew_home):
        """The call site: every Linux spawn gets mount targets before its child
        mounts, so no agent namespace can predate the mask."""
        sb.namespace_argv(["/bin/true"])
        state = crew_home / sb._MD_NOTEBOOK_STATE_DIR
        assert state.is_dir(), "the state directory's mount target is absent"
        for name in ("pat", "vaults.json", "settings.json"):
            assert (state / name).is_file(), f"{name} absent after namespace_argv"


class TestStateWritersStageInsideTheMask:
    """A temp staged BESIDE the target holds the same bytes (PAT included) at a name no
    mask covers, and a SIGKILL between write and rename leaves it there forever. Every state
    writer stages its temp INSIDE the whole-directory state mask instead, so the in-flight
    bytes and any crash orphan both stay behind that mask."""

    @pytest.fixture()
    def server(self, tmp_path, monkeypatch):
        from kiro_crew.apps.builtins.md_notebook import server

        monkeypatch.setattr(server, "_HOME", tmp_path / "state")
        return server

    def test_a_crashed_publish_leaves_the_temp_inside_the_mask(self, server, monkeypatch):
        def _boom(tmp, target):
            raise AssertionError("simulated crash at publish time")

        # The by-name floor funnels its publish through ``replace_with_retry``; crash there.
        monkeypatch.setattr(server, "replace_with_retry", _boom)
        # Suppress the failure-path unlink so the orphan the crash WOULD leave is observable
        # — this models SIGKILL, which runs no cleanup at all.
        monkeypatch.setattr(server.Path, "unlink", lambda self, *a, **k: None)
        with pytest.raises(AssertionError):
            server._write_pat_sync("ghp_secret")

        state = server._state_dir()
        entries = list(state.iterdir()) if state.exists() else []
        # The orphan temp is INSIDE the masked state directory, beside where the target
        # would be — not at a sibling name outside the mask.
        assert entries, "the temp was not staged inside the masked state dir at all"
        temps = [p for p in entries if p.name.endswith(".tmp")]
        assert temps, f"no staged temp inside the state dir; found {[p.name for p in entries]!r}"
        assert temps[0].read_text() == "ghp_secret"
        if os.name == "posix":
            assert os.stat(temps[0]).st_mode & 0o077 == 0

    def test_each_writer_publishes_to_its_target_with_no_residue(self, server):
        server._write_pat_sync("ghp_token")
        server._write_vaults_sync([{"id": "v1"}])
        server._write_settings_sync({"autoSync": True})
        state = server._state_dir()
        assert (state / "pat").read_text() == "ghp_token"
        assert "v1" in (state / "vaults.json").read_text()
        assert "autoSync" in (state / "settings.json").read_text()
        assert {p.name for p in state.iterdir()} == {
            "pat",
            "vaults.json",
            "settings.json",
        }, "a writer left a temp inside the state dir after a successful publish"

    def test_a_planted_parent_link_refuses_the_write(self, server, tmp_path):
        """The planted-link guard atomic_write enforces, which staging here must keep: a
        secret write whose parent chain passes through a planted link is refused, because
        mkdir and the staging create would follow it and the token lands outside the
        sensitive-path fence."""
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        # Plant the state dir itself as a link to a foreign directory, so the target's
        # parent chain passes through it.
        server._state_dir().symlink_to(elsewhere, target_is_directory=True)
        with pytest.raises(OSError):
            server._write_pat_sync("ghp_secret")
        assert list(elsewhere.iterdir()) == [], (
            "the PAT write followed a pre-planted parent link and published "
            f"the token outside the fence: {list(elsewhere.iterdir())!r}"
        )

    def test_clearing_the_pat_keeps_the_mask_mount_target(self, server):
        """Clearing must atomically empty the file, never unlink it: the inode
        is the sandbox mask's mount target, and a clear landing between the
        launcher's materialize and mount steps would leave that namespace
        maskless for a later PAT save."""
        server._write_pat_sync("ghp_token")
        assert server._read_pat_sync() == "ghp_token"
        server._write_pat_sync("")  # what api_pat's clear branch calls
        pat_file = server._state_dir() / "pat"
        assert pat_file.is_file(), "the PAT clear removed the mask's mount target"
        assert pat_file.read_bytes() == b""
        assert server._read_pat_sync() is None, "empty must read as absent"

    def test_the_clear_ROUTE_keeps_the_mask_mount_target(self, server, monkeypatch):
        """Pins the ROUTE, not just the writer. The test above proves an empty write is
        absent-equivalent; this one proves ``api_pat``'s clear branch actually takes it.
        An ``os.unlink`` there would delete the mask's mount target while every
        writer-level assertion above still passed."""
        server._write_pat_sync("ghp_token")
        pat_file = server._state_dir() / "pat"
        assert pat_file.is_file()

        async def _fake_body(_request):
            return {"pat": ""}

        async def _no_gh():
            return None

        monkeypatch.setattr(server, "json_body", _fake_body)
        monkeypatch.setattr(server, "gh_token", _no_gh)

        response = asyncio.run(server.api_pat(object()))

        assert pat_file.is_file(), "the clear route removed the mask's mount target"
        assert pat_file.read_bytes() == b""
        assert json.loads(response.text)["hasPat"] is False


class TestADirectoryMaskSurvivesAnInDirPublish:
    """The three state files are masked as ONE whole directory, not three leaves.

    If each state file were its own leaf mask, publishing (a rename onto ``pat``) would
    replace the directory entry that ``pat`` bind mount sits on, detaching the mount in a
    live agent's namespace — the fresh real PAT would then be readable by a same-uid
    sandboxed agent. Masking the whole state directory puts the
    bind-mount point AT the directory: a rename of a name INSIDE it never replaces the
    directory's own mount point, so the agent keeps seeing the masked (empty tmpfs)
    directory whatever the gateway publishes underneath. These tests pin that geometry
    structurally — a real namespace cannot be spawned under CI's unprivileged runner —
    which is what the fix rests on: the mask names the DIRECTORY, and every publish target
    is a child NAME inside it.
    """

    @pytest.mark.parametrize("mode", _MODES)
    @pytest.mark.parametrize("prefix", _CREW_PREFIXES)
    def test_the_whole_state_directory_is_masked_not_its_files(self, mode, prefix):
        hidden = _hidden_dirs(mode)
        # The DIRECTORY is masked...
        assert _crew_path(prefix, sb._MD_NOTEBOOK_STATE_DIR) in hidden, (
            "the state directory is not masked as a whole, so a publish rename of a file "
            "inside it would be unprotected"
        )
        # ...and the three state FILES are NOT individually masked leaves any more. If they
        # were, a rename onto one would detach that leaf's mount — the exposure this closes.
        for leaf in sb._MD_NOTEBOOK_STATE_LEAVES:
            assert _crew_path(prefix, leaf) not in hidden, (
                f"{leaf} is still masked as an individual leaf; a publish rename onto it "
                "would detach the bind mount and expose the file to a sandboxed agent"
            )

    def test_every_publish_target_is_a_name_inside_the_masked_directory(
        self, tmp_path, monkeypatch
    ):
        """The backend's publish geometry: each state writer renames onto a child NAME of
        the masked state directory, never onto the directory (the mount point) itself.
        A rename of a child inside a wholly-masked directory cannot detach the directory's
        bind mount, so the published bytes stay behind the mask in a live agent's view."""
        from kiro_crew.apps.builtins.md_notebook import server

        monkeypatch.setattr(server, "_HOME", tmp_path / "state")
        state_dir = server._state_dir()
        for target in (server._pat_file(), server._vaults_json(), server._settings_json()):
            # The target's PARENT is exactly the masked state directory...
            assert target.parent == state_dir, (
                f"{target} is not inside the masked state directory; publishing it would "
                "cross the directory's mount point"
            )
            # ...and the target is NOT the directory itself (which is the bind-mount point).
            assert target != state_dir

    def test_the_legacy_names_stay_masked_for_stale_copies(self):
        """A state file can sit at a legacy spelling — directly under
        ``workspace/md-notebook/`` or inside the retired ``workspace/md-notebook/.state/`` —
        written by an older build or left by a migration that could not move it. Such a copy
        can still hold the real PAT, so those spellings must stay masked (and uncarved) on an
        upgraded host."""
        legacy = (*sb._MD_NOTEBOOK_RETIRED_STATE_LEAVES, sb._MD_NOTEBOOK_RETIRED_STATE_DIR)
        for mode in _MODES:
            hidden = _hidden_dirs(mode)
            for prefix in _CREW_PREFIXES:
                for retired in legacy:
                    assert _crew_path(prefix, retired) in hidden, (
                        f"the legacy spelling {retired} is unmasked; a stale PAT copy would "
                        "be readable by a sandboxed agent"
                    )
        # And the legacy spellings are NOT carved back to the backend — it reads only the
        # state directory, so nothing legitimately needs them unmasked.
        carved = sb.app_backend_visible_targets(sb.MD_NOTEBOOK_APP_NAME)
        for prefix in _CREW_PREFIXES:
            for retired in legacy:
                assert _crew_path(prefix, retired) not in carved, (
                    f"the legacy spelling {retired} was carved back to the backend; it must "
                    "stay masked so a stale PAT copy cannot be read"
                )

    def test_renaming_the_app_dir_aside_cannot_redirect_a_pat_write(self, tmp_path, monkeypatch):
        """The state directory cannot be renamed out from under its own mask.

        A same-uid agent can write ``workspace/md-notebook`` — it holds the clone data — so
        it can rename that tree aside and recreate it. Were the state directory nested there,
        the rename would carry it out from under its mask and a later PAT write would land in
        a directory a subsequent namespace does not mask. The state directory is a direct
        child of the data-home root instead, so the rename leaves it where it was: the next
        write publishes into the same masked directory, and nothing reaches either app tree.
        """
        from kiro_crew.apps.builtins.md_notebook import server

        crew_home = tmp_path / "crew"
        app_dir = crew_home / "workspace" / "md-notebook"
        app_dir.mkdir(parents=True)
        monkeypatch.setattr(server, "_HOME", None)
        monkeypatch.setattr(server, "config_dir", lambda: crew_home)

        server._write_pat_sync("ghp_before")
        state_dir = crew_home / sb._MD_NOTEBOOK_STATE_DIR
        assert (state_dir / "pat").read_text() == "ghp_before"

        # The agent renames the app tree aside and recreates it.
        moved = crew_home / "workspace" / "md-notebook-moved"
        app_dir.rename(moved)
        app_dir.mkdir()

        server._write_pat_sync("ghp_after")

        assert server._pat_file() == state_dir / "pat"
        assert (state_dir / "pat").read_text() == "ghp_after"
        for tree in (moved, app_dir):
            leaked = [p for p in tree.rglob("*") if p.is_file()]
            assert leaked == [], f"a PAT write reached the agent-writable tree {tree}: {leaked}"


class TestLegacyStateMigratesIntoTheStateDir:
    """The gateway moves a legacy state file (``workspace/md-notebook/{pat,…}`` or the
    retired ``.state/``) into the top-level state dir — real filesystem, idempotent, fail-soft —
    so an existing user's live PAT is not stranded at an uncarved legacy name."""

    @pytest.fixture()
    def server(self, tmp_path, monkeypatch):
        from kiro_crew.apps.builtins.md_notebook import server

        monkeypatch.setattr(server, "_HOME", tmp_path / "md-notebook")
        return server

    def test_a_legacy_pat_moves_into_state(self, server):
        legacy = server._legacy_state_dir()
        legacy.mkdir(parents=True)
        (legacy / "pat").write_text("ghp_legacy")
        (legacy / "vaults.json").write_text('[{"id": "v1"}]')
        (legacy / "settings.json").write_text('{"autoSync": true}')

        moved = server.migrate_legacy_state_into_state_dir()

        state = server._state_dir()
        assert (state / "pat").read_text() == "ghp_legacy"
        assert (state / "vaults.json").read_text() == '[{"id": "v1"}]'
        assert (state / "settings.json").read_text() == '{"autoSync": true}'
        assert set(moved) == {str(state / n) for n in ("pat", "vaults.json", "settings.json")}
        # The PAT lands owner-only.
        if os.name == "posix":
            assert os.stat(state / "pat").st_mode & 0o077 == 0

    def test_migration_is_idempotent_and_never_clobbers_new_state(self, server):
        legacy = server._legacy_state_dir()
        legacy.mkdir(parents=True)
        (legacy / "pat").write_text("ghp_stale_legacy")
        # A newer PAT already written under the new layout must WIN over the stale legacy one.
        state = server._state_dir()
        state.mkdir(parents=True)
        (state / "pat").write_text("ghp_current")

        moved = server.migrate_legacy_state_into_state_dir()

        assert (
            state / "pat"
        ).read_text() == "ghp_current", (
            "the migration clobbered the current PAT with a stale legacy copy"
        )
        assert str(state / "pat") not in moved
        # A second run is a no-op.
        assert server.migrate_legacy_state_into_state_dir() == []

    def test_migration_is_a_noop_with_no_legacy_state(self, server):
        assert server.migrate_legacy_state_into_state_dir() == []

    def test_a_bare_file_written_after_the_first_migration_is_never_laundered(self, server):
        """One-shot: a bare file that appears AFTER the first migration is not promoted.

        The materialiser plants only the state-dir targets, and the launcher's
        ``isfile``-guarded mask does not cover an ABSENT bare leaf, so once the first pass
        empties the bare names a sandboxed agent could write its own bare ``settings.json``
        choosing the unattended ``git push`` target. Re-running the migration must NOT move
        that agent-written file into the fenced state dir. The completion marker makes the
        promotion loop one-shot.
        """
        state = server._state_dir()
        # First boot: no legacy state at all, so the pass moves nothing but seals the marker.
        assert server.migrate_legacy_state_into_state_dir() == []
        marker = server._migration_marker_path()
        assert marker.exists(), "the first migration did not write the completion marker"
        # The state-dir settings target reads as the absent-equivalent stub.
        assert (
            server._is_absent_equivalent_state("settings.json", state / "settings.json")
            or not (state / "settings.json").exists()
        )

        # An agent now writes a REAL bare settings.json choosing autoSync + a push target.
        legacy = server._legacy_state_dir()
        legacy.mkdir(parents=True, exist_ok=True)
        (legacy / "settings.json").write_text('{"autoSync": true}')

        moved = server.migrate_legacy_state_into_state_dir()

        assert moved == [], "a bare file written after the first migration was laundered"
        # The state-dir target is untouched — still the stub / absent, never the agent's file.
        if (state / "settings.json").exists():
            assert server._is_absent_equivalent_state(
                "settings.json", state / "settings.json"
            ), "the agent-written bare settings.json was promoted into the fenced state dir"

    def test_a_failed_move_is_recorded_pending_and_retried_not_stranded(self, server, monkeypatch):
        """A genuine legacy file that fails to move is retried, not sealed away.

        A per-file rename failure (e.g. a Windows sharing violation) must not strand the
        real credential at the bare name forever: the first pass records it in the marker's
        ``pending`` map by content hash, and a later run retries THAT file and completes
        the move.
        """
        legacy = server._legacy_state_dir()
        legacy.mkdir(parents=True, exist_ok=True)
        (legacy / "vaults.json").write_text('[{"id": "v1", "remoteUrl": "real"}]')
        state = server._state_dir()

        # First pass: force the vaults.json move to fail.
        real_replace = server.replace_with_retry

        def _flaky(src, dst, *a, **k):
            if str(src).endswith("vaults.json"):
                raise OSError("simulated sharing violation")
            return real_replace(src, dst, *a, **k)

        monkeypatch.setattr(server, "replace_with_retry", _flaky)
        assert server.migrate_legacy_state_into_state_dir() == []

        marker = server._migration_marker_path()
        pending = server._read_migration_pending(marker)
        assert "vaults.json" in pending, "a failed move was not recorded for retry"
        assert pending["vaults.json"] == server._file_identity(legacy / "vaults.json")

        # Later run with the lock cleared: the same content is retried and the move completes.
        monkeypatch.setattr(server, "replace_with_retry", real_replace)
        moved = server.migrate_legacy_state_into_state_dir()
        assert moved == [str(state / "vaults.json")]
        assert (state / "vaults.json").read_text().startswith('[{"id": "v1"')
        assert server._read_migration_pending(marker) == {}, "pending did not clear after retry"

    def test_a_pending_retry_ignores_an_agent_replacement_at_the_same_name(
        self, server, monkeypatch
    ):
        """The scoped retry promotes only the recorded content, never an agent's replacement.

        If, between the failed first pass and the retry, an agent unlinks the pending
        legacy file and drops its own at the same bare name with DIFFERENT content (its own
        push remote), its content hash differs from the recorded one. The retry must NOT
        promote the agent's file — it is not the recorded legacy credential.
        """
        legacy = server._legacy_state_dir()
        legacy.mkdir(parents=True, exist_ok=True)
        (legacy / "vaults.json").write_text('[{"id": "legit"}]')
        state = server._state_dir()

        real_replace = server.replace_with_retry

        def _flaky(src, dst, *a, **k):
            if str(src).endswith("vaults.json"):
                raise OSError("simulated sharing violation")
            return real_replace(src, dst, *a, **k)

        monkeypatch.setattr(server, "replace_with_retry", _flaky)
        server.migrate_legacy_state_into_state_dir()

        # Agent replaces the bare file with its own, different content (attacker remote).
        (legacy / "vaults.json").unlink()
        (legacy / "vaults.json").write_text('[{"id": "attacker", "remoteUrl": "evil"}]')

        monkeypatch.setattr(server, "replace_with_retry", real_replace)
        moved = server.migrate_legacy_state_into_state_dir()
        assert moved == [], "an agent replacement at a pending name was promoted"
        if (state / "vaults.json").exists():
            assert server._is_absent_equivalent_state(
                "vaults.json", state / "vaults.json"
            ), "the agent's replacement vaults.json was laundered into the state dir"

    def test_a_live_write_retires_a_pending_retry_so_a_cleared_pat_is_not_restored(
        self, server, monkeypatch
    ):
        """A user clear must stick: a pending retry does not move the old PAT back over it.

        If the legacy PAT move fails and is recorded pending, then the user clears the PAT
        (an empty write, which reads as the absent-equivalent stub), a later migration must
        NOT see that stub and restore the old token. The live write retires the pending
        entry, so the clear is final.
        """
        legacy = server._legacy_state_dir()
        legacy.mkdir(parents=True, exist_ok=True)
        (legacy / "pat").write_text("ghp_old_real")
        state = server._state_dir()

        real_replace = server.replace_with_retry

        def _flaky(src, dst, *a, **k):
            if str(src).endswith("/pat"):
                raise OSError("simulated sharing violation")
            return real_replace(src, dst, *a, **k)

        monkeypatch.setattr(server, "replace_with_retry", _flaky)
        server.migrate_legacy_state_into_state_dir()
        assert "pat" in server._read_migration_pending(server._migration_marker_path())

        # The user clears the PAT: an empty (absent-equivalent) live write to the state dir.
        monkeypatch.setattr(server, "replace_with_retry", real_replace)
        server._write_state_staged_sync(state / "pat", "")
        assert "pat" not in server._read_migration_pending(
            server._migration_marker_path()
        ), "the live clear did not retire the pending retry"

        # Next migration must NOT restore the old token over the user's clear.
        server.migrate_legacy_state_into_state_dir()
        pat_bytes = (state / "pat").read_bytes() if (state / "pat").exists() else b""
        assert pat_bytes == b"", "a pending retry restored a PAT the user had cleared"

    def test_the_completion_marker_lives_outside_the_legacy_subtree(self, server):
        """The one-shot marker must survive a rename of the agent-writable app subtree.

        If the marker lived under the agent-renamable ``workspace/md-notebook``, an agent
        could rename that subtree aside and recreate it with forged legacy files; the next
        spawn would see no marker and reopen promotion. The marker therefore lives inside
        the top-level state directory, outside the legacy subtree.
        """
        server.migrate_legacy_state_into_state_dir()
        marker = server._migration_marker_path()
        assert marker.exists(), "the first migration did not write the completion marker"
        legacy = server._legacy_state_dir()
        assert marker.parent == server._state_dir()
        assert legacy not in marker.parents and legacy != marker.parent, (
            "the completion marker lives inside the agent-renamable app subtree; renaming "
            "that subtree would erase the one-shot and reopen promotion"
        )

    def test_a_retired_state_dir_copy_migrates(self, server):
        """The retired ``workspace/md-notebook/.state/`` layout is a migration source too."""
        retired = server._retired_state_dir()
        retired.mkdir(parents=True)
        (retired / "pat").write_text("ghp_from_retired_state")
        (retired / "settings.json").write_text('{"autoSync": true}')

        moved = server.migrate_legacy_state_into_state_dir()

        state = server._state_dir()
        assert set(moved) == {str(state / "pat"), str(state / "settings.json")}
        assert (state / "pat").read_text() == "ghp_from_retired_state"
        assert not (retired / "pat").exists(), "the retired copy was not moved"
        assert server._read_pat_sync() == "ghp_from_retired_state"

    def test_the_retired_state_dir_wins_over_a_bare_copy(self, server):
        """Where both legacy layouts hold real content, the newer ``.state/`` copy lands and
        the bare copy is left alone, still fenced by its own mask."""
        legacy = server._legacy_state_dir()
        retired = server._retired_state_dir()
        retired.mkdir(parents=True)
        (legacy / "pat").write_text("ghp_older_bare")
        (retired / "pat").write_text("ghp_newer_state")

        moved = server.migrate_legacy_state_into_state_dir()

        state = server._state_dir()
        assert moved == [str(state / "pat")]
        assert (state / "pat").read_text() == "ghp_newer_state"
        assert (legacy / "pat").read_text() == "ghp_older_bare"

    def test_a_retired_state_dir_file_after_the_first_pass_is_never_laundered(self, server):
        """The one-shot covers the retired ``.state/`` source as well as the bare names: a
        file an agent drops there after the first pass is never promoted."""
        assert server.migrate_legacy_state_into_state_dir() == []
        retired = server._retired_state_dir()
        retired.mkdir(parents=True)
        (retired / "vaults.json").write_text('[{"id": "attacker", "remoteUrl": "evil"}]')

        assert server.migrate_legacy_state_into_state_dir() == []
        assert not (server._state_dir() / "vaults.json").exists()

    def test_a_legacy_symlink_is_never_moved_into_the_state_dir(self, server, tmp_path):
        """Renaming a legacy LINK would plant it inside the fenced directory, where the
        backend would read whatever its referent holds. Only a regular file migrates."""
        referent = tmp_path / "attacker-settings.json"
        referent.write_text('{"autoSync": true}')
        retired = server._retired_state_dir()
        retired.mkdir(parents=True)
        (retired / "settings.json").symlink_to(referent)

        assert server.migrate_legacy_state_into_state_dir() == []
        dst = server._state_dir() / "settings.json"
        assert not dst.is_symlink() and not dst.exists()

    def test_a_pending_retired_state_copy_is_retried_and_retired_by_a_live_write(
        self, server, monkeypatch
    ):
        """A failed move from the retired ``.state/`` source is recorded under its own key,
        and a live write retires it, so a user's clear is not undone by a later retry."""
        retired = server._retired_state_dir()
        retired.mkdir(parents=True)
        (retired / "pat").write_text("ghp_old_real")
        real_replace = server.replace_with_retry

        def _flaky(src, dst, *a, **k):
            if os.path.basename(os.path.dirname(str(src))) == ".state":
                raise OSError("simulated sharing violation")
            return real_replace(src, dst, *a, **k)

        monkeypatch.setattr(server, "replace_with_retry", _flaky)
        server.migrate_legacy_state_into_state_dir()
        marker = server._migration_marker_path()
        assert set(server._read_migration_pending(marker)) == {".state/pat"}

        monkeypatch.setattr(server, "replace_with_retry", real_replace)
        server._write_pat_sync("")
        assert server._read_migration_pending(marker) == {}, "the live clear left it pending"
        server.migrate_legacy_state_into_state_dir()
        assert (server._state_dir() / "pat").read_bytes() == b""
        assert (retired / "pat").read_text() == "ghp_old_real", "the stale copy was promoted"

    def test_a_marker_cannot_name_a_source_the_migration_does_not_own(self, server):
        """``pending`` keys outside the known source spellings are dropped on read, so a
        forged or corrupted marker cannot steer the retry at an arbitrary path."""
        marker = server._migration_marker_path()
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({"pending": {"../../evil": "x", ".state/pat": "y"}}))
        assert server._read_migration_pending(marker) == {".state/pat": "y"}

    def test_a_clear_fails_closed_when_pending_retirement_cannot_persist(self, server, monkeypatch):
        """A clear is not acknowledged if the pending retirement cannot be made durable.

        If retirement could log-and-continue, api_pat would report a clear as succeeded
        while a stale pending entry survived — and the next migration would restore the old
        credential over the acknowledged clear. The write must RAISE instead, so the caller
        never acknowledges a clear it could not make final.
        """
        legacy = server._legacy_state_dir()
        legacy.mkdir(parents=True, exist_ok=True)
        (legacy / "pat").write_text("ghp_old_real")
        state = server._state_dir()
        real_replace = server.replace_with_retry

        def _flaky(src, dst, *a, **k):
            if str(src).endswith("/pat"):
                raise OSError("simulated sharing violation")
            return real_replace(src, dst, *a, **k)

        monkeypatch.setattr(server, "replace_with_retry", _flaky)
        server.migrate_legacy_state_into_state_dir()
        assert "pat" in server._read_migration_pending(server._migration_marker_path())

        # The marker write (retirement) now fails; the clear must propagate, not ack.
        monkeypatch.setattr(server, "replace_with_retry", real_replace)

        def _boom_marker(*_a, **_k):
            raise OSError("simulated marker persistence failure")

        monkeypatch.setattr(server, "_write_migration_marker", _boom_marker)
        with pytest.raises(OSError):
            server._write_state_staged_sync(state / "pat", "")
