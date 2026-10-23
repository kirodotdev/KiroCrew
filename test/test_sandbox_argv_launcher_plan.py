"""What the Linux namespace plan seals, hides and carves out, and how the launcher applies it.

The launcher is tested at its two interfaces. What a spawn masks, seals and re-opens is
read off the :class:`~kiro_crew.sandbox_plan.ConfinementPlan` that
``sandbox._spawn_plan`` builds for it; what the launcher program then DOES with that
plan is driven through the stages of ``kiro_crew.sandbox_launcher_program`` with the
shared stand-in libc from ``test_sandbox_launcher_program``, on a tree under
``tmp_path``. The writable carve-outs, the sealed run-parent predicate, the rendered
launcher script, the agent env-passthrough contract and the pre-exec hardlink scan
budget are here too.
"""

from __future__ import annotations

import ast
import errno
import os
import stat
import sys
from pathlib import Path

import pytest
import sandbox_argv_helpers
from sandbox_argv_helpers import _POSIX_ONLY, _SECCOMP_ALLOW, _SECCOMP_EPERM, _seccomp_verdict
from test_sandbox_launcher_program import CoveringLibc, RecordingLibc, launch, payload, refusal

import kiro_crew.sandbox as sandbox_mod
from kiro_crew import sandbox_launcher_program as launcher_program
from kiro_crew import sandbox_plan
from kiro_crew.sandbox import (
    _SENSITIVE_ENV_PREFIXES,
    _build_launcher_script,
    _build_seatbelt_profile,
    _voice_runtime_parent_paths,
    _voice_runtime_sandbox_paths,
)
from kiro_crew.sandbox_launcher import launcher_program_source

# Several tests of this suite spawn real child interpreters (subprocess.run([sys.executable,
# ...])); every part of it pins itself to the same dedicated xdist worker so concurrent
# cold-starts under -n auto don't starve each other / blow the 30s timeout. Requires
# --dist loadgroup.
pytestmark = [
    pytest.mark.xdist_group(name="subprocess_spawn"),
    # test_sandbox_argv.py replaces ctypes.CDLL, find_library or a cached libc handle for a
    # test; every part of the suite puts the shared macOS handle caches back after each
    # test (see conftest.py).
    pytest.mark.usefixtures("fresh_darwin_libc_handles"),
]


# The shared fixtures, bound here by name so pytest registers them for this module.
clean_backend = sandbox_argv_helpers.clean_backend


# The launcher program's mount stages pin each target through ``O_PATH`` and address it
# as ``/proc/self/fd/<n>``, which only Linux has, so a test that drives them for real
# runs there; the plan they are driven from is checked on every POSIX host.
_LINUX_ONLY = pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="the launcher program's mount stages pin through O_PATH and /proc/self/fd",
)

#: The stages ``sandbox_launcher_program.place_masks`` runs.
_MASK_STAGES = (
    "stage_private_windows",
    "seal_readonly",
    "mask_sensitive",
    "apply_carveouts",
    "restore_exposed_files",
    "verify_fail_closed_aliases",
    "mask_sensitive_files",
    "mask_ssh_keys",
)

#: ``mount(2)`` flags, as the kernel defines them.
_MS_RDONLY = 1
_MS_NOSUID = 2
_MS_NODEV = 4
_MS_NOEXEC = 8
_MS_REMOUNT = 32
_MS_BIND = 4096


def _payload_under(plan: sandbox_plan.ConfinementPlan, root: Path) -> dict:
    """The launcher payload of *plan*, narrowed to the paths inside *root*.

    The planner also names this host's real credential homes under ``Path.home()``. A
    stage driven in-process here must never mount over those, so every path list keeps
    only the entries inside the test's own tree, in the plan's order, and ``~/.ssh``
    (the real one) is left alone.
    """
    data = sandbox_plan.namespace_payload(plan)
    prefix = str(root).rstrip("/") + "/"
    for key in (
        "sensitive_dirs",
        "private_dirs",
        "readonly_dirs",
        "writable_dirs",
        "sensitive_files",
        "required_mask_targets",
    ):
        data[key] = [path for path in data[key] if path.startswith(prefix)]
    for key in ("sensitive_dir_ids", "private_dir_ids", "mask_occupants"):
        data[key] = {path: ident for path, ident in data[key].items() if path.startswith(prefix)}
    for key in ("expose_files", "fail_closed_file_masks", "crew_home_aliases"):
        data[key] = [entry for entry in data[key] if entry[0].startswith(prefix)]
    data["hide_ssh"] = 0
    data["stand_in_roots"] = []
    return data


def _stage_order(monkeypatch: pytest.MonkeyPatch, run: launcher_program.Launch) -> list[str]:
    """The stages ``place_masks`` runs for *run*, in order, each recorded instead of run."""
    order: list[str] = []
    for name in _MASK_STAGES:
        monkeypatch.setattr(launcher_program, name, lambda _launch, name=name: order.append(name))
    launcher_program.place_masks(run)
    return order


def _bind_targets(libc: RecordingLibc) -> list[str]:
    """The resolved target of every bind a stage made, in order, remounts excluded."""
    return [
        os.path.realpath(call.target_path)
        for call in libc.calls
        if call.flags & _MS_BIND and not call.flags & _MS_REMOUNT
    ]


class TestWritableCarveouts:
    """A probe's private TMPDIR must be writable inside the sandbox.

    The MCP probe's TMPDIR lives at ``<data home>/run/mcp-tmp/<probe>``, inside
    the runtime parent both backends seal read-only, so the wrap must carve
    exactly that directory back out — a Bun-packaged server extracts its native
    module into TMPDIR before it can answer the handshake. These tests lock the
    carve-out's two properties: it OPENS the approved directory (emitted after
    the seal — Seatbelt is last-match-wins) and it opens NOTHING else (the
    validator refuses every candidate that would re-open another seal).
    """

    def _relocated_home(self, monkeypatch, tmp_path):
        custom_home = tmp_path / "crew-home"
        custom_home.mkdir()
        monkeypatch.setattr(sandbox_mod, "config_dir", lambda: custom_home)
        probe = custom_home / "run" / "mcp-tmp" / "probe-x"
        probe.mkdir(parents=True)
        return custom_home, probe

    @staticmethod
    def _spellings(path) -> list[str]:
        lexical = os.path.normpath(str(path))
        return list(dict.fromkeys((lexical, os.path.realpath(lexical))))

    def test_seatbelt_carveout_allow_lands_after_run_seal(self, monkeypatch, tmp_path):
        home, probe = self._relocated_home(monkeypatch, tmp_path)
        profile = _build_seatbelt_profile("standard", extra_writable_dirs=(str(probe),))
        deny = f'(deny file-write* (subpath "{home / "run"}"))'
        assert deny in profile
        for spelling in self._spellings(probe):
            allow = f'(allow file-write* (subpath "{spelling}"))'
            assert allow in profile
            # Seatbelt is last-match-wins: the allow must come AFTER the seal,
            # or the seal wins and the child's TMPDIR stays unwritable.
            assert profile.index(allow) > profile.index(deny)
        # The subtree's hardlink deny is deliberately NOT re-opened: a scratch
        # dir never needs to mint hardlinks, and the deny is what stops
        # aliasing a sealed inode into the writable window.
        assert "(allow file-link" not in profile
        assert "(allow file-read" not in profile

    @pytest.mark.parametrize(
        "candidate",
        [
            "run",  # the sealed parent itself: contains the voice runtime
            os.path.join("run", "voice-runtime"),  # the hidden runtime root
            "elsewhere",  # outside every carveable parent
            os.path.join("run", "absent"),  # does not exist
        ],
    )
    def test_seatbelt_refuses_unsafe_carveouts(self, monkeypatch, tmp_path, candidate):
        home, _probe = self._relocated_home(monkeypatch, tmp_path)
        (home / "elsewhere").mkdir()
        profile = _build_seatbelt_profile("standard", extra_writable_dirs=(str(home / candidate),))
        assert "(allow file-write*" not in profile

    def test_seatbelt_refuses_relative_carveout(self, monkeypatch, tmp_path):
        self._relocated_home(monkeypatch, tmp_path)
        profile = _build_seatbelt_profile("standard", extra_writable_dirs=("run/mcp-tmp/probe-x",))
        assert "(allow file-write*" not in profile

    @_POSIX_ONLY
    def test_launcher_embeds_validated_carveout(self, monkeypatch, tmp_path):
        home, probe = self._relocated_home(monkeypatch, tmp_path)
        plan = sandbox_mod._spawn_plan("namespace", "standard", extra_writable_dirs=(str(probe),))
        assert plan.writable == tuple(self._spellings(probe))
        assert sandbox_plan.namespace_payload(plan)["writable_dirs"] == self._spellings(probe)
        # The carve-out's remount must clear the seal -- a remount that re-passed
        # MS_RDONLY would silently keep the carve-out read-only -- and must re-assert
        # the bits the kernel locked on the mount the bind inherited, read off that
        # very target.
        statvfs_targets: list[object] = []

        class _Vfs:
            f_flag = (
                getattr(os, "ST_NOSUID", 0)
                | getattr(os, "ST_NODEV", 0)
                | getattr(os, "ST_NOEXEC", 0)
            )

        def _statvfs(target):
            statvfs_targets.append(target)
            return _Vfs()

        monkeypatch.setattr(launcher_program.os, "statvfs", _statvfs)
        locked = (
            (_MS_NOSUID if hasattr(os, "ST_NOSUID") else 0)
            | (_MS_NODEV if hasattr(os, "ST_NODEV") else 0)
            | (_MS_NOEXEC if hasattr(os, "ST_NOEXEC") else 0)
        )
        libc = RecordingLibc()
        launcher_program.apply_carveouts(
            launch(tmp_path, payload(writable_dirs=list(plan.writable)), libc=libc)
        )
        expected = []
        for spelling in plan.writable:
            target = spelling.encode()
            expected += [
                (target, target, _MS_BIND),
                (target, target, _MS_REMOUNT | _MS_BIND | locked),
            ]
        assert [(call.source, call.target, call.flags) for call in libc.calls] == expected
        assert not any(call.flags & _MS_RDONLY for call in libc.calls)
        assert statvfs_targets == [spelling.encode() for spelling in plan.writable]

    @_POSIX_ONLY
    def test_launcher_refuses_unsafe_carveout(self, monkeypatch, tmp_path):
        home, _probe = self._relocated_home(monkeypatch, tmp_path)
        run_dir = str(home / "run")
        plan = sandbox_mod._spawn_plan("namespace", "standard", extra_writable_dirs=(run_dir,))
        assert plan.writable == ()
        # The sealed parent itself holds the hidden voice runtime, which it would re-open.
        refused = [r for r in plan.refusals if r.kind == "carve-out"]
        assert [r.args[0] for r in refused] == [run_dir]
        assert "would be re-opened by it" in refused[0].args[1]

    @_POSIX_ONLY
    def test_launcher_seals_before_carveout_rebind(self, monkeypatch, tmp_path):
        """The READONLY seal must precede the carve-out re-bind.

        Load-bearing ordering: a non-recursive MS_BIND does not replicate
        submounts, so a parent self-bind established AFTER the carve-out would
        mask the carve-out mount entirely -- the writable window vanishes
        silently and the writable-TMPDIR failure returns with no error. The
        mounts themselves are recorded in that order by
        ``test_the_plans_mounts_land_seal_then_hide_then_carve_out``.
        """
        home, probe = self._relocated_home(monkeypatch, tmp_path)
        plan = sandbox_mod._spawn_plan("namespace", "standard", extra_writable_dirs=(str(probe),))
        assert str(home / "run") in plan.readonly
        assert str(probe) in plan.writable
        order = _stage_order(monkeypatch, launch(tmp_path, _payload_under(plan, tmp_path)))
        assert order.index("seal_readonly") < order.index("apply_carveouts")

    @_POSIX_ONLY
    @pytest.mark.parametrize("level", ["strict", "standard", "cc"])
    def test_launcher_seals_before_hiding(self, monkeypatch, tmp_path, level):
        """The READONLY seal must precede the sensitive_dirs hide.

        Same kernel property as the carve-out pin above, in the other
        direction: ``run/voice-runtime`` is hidden and its parent ``run`` is
        sealed, and a non-recursive self-bind of ``run`` issued AFTER the hide
        masks it -- the real leaf becomes readable through the new mount.
        Seal first, then hide ON the sealed parent. The carve-out stays last,
        so the three stages are seal < hide < carve-out. Every tier, since all
        three run the same launcher program.
        """
        home, probe = self._relocated_home(monkeypatch, tmp_path)
        plan = sandbox_mod._spawn_plan("namespace", level, extra_writable_dirs=(str(probe),))
        # The pair this pin exists for is actually planned: the leaf is hidden and its
        # parent is sealed.
        voice_roots = _voice_runtime_sandbox_paths()
        voice_parents = _voice_runtime_parent_paths()
        assert set(plan.sensitive_dirs) >= set(voice_roots)
        assert set(plan.readonly) >= set(voice_parents)
        order = _stage_order(monkeypatch, launch(tmp_path, _payload_under(plan, tmp_path)))
        seal = order.index("seal_readonly")
        hide = order.index("mask_sensitive")
        carve = order.index("apply_carveouts")
        assert seal < hide < carve

    @_LINUX_ONLY
    @pytest.mark.parametrize("level", ["strict", "standard", "cc"])
    def test_the_plans_mounts_land_seal_then_hide_then_carve_out(
        self, monkeypatch, tmp_path, level
    ):
        """The mounts the program places for a plan, in the order the kernel needs.

        The two ordering pins above, at the mount level: the launcher program
        places this tier's plan over this test's tree and the stand-in libc
        records each bind. The sealed ``run`` is bound before the hidden voice
        runtime under it, which is bound before the carve-out, and the leaf's
        contents are gone from its name.
        """
        home, probe = self._relocated_home(monkeypatch, tmp_path)
        plan = sandbox_mod._spawn_plan("namespace", level, extra_writable_dirs=(str(probe),))
        # The voice-runtime paths are cached per data-home spelling, so the directory
        # is created here rather than left to the cache's first fill.
        leaf = home / "run" / "voice-runtime"
        leaf.mkdir(exist_ok=True)
        (leaf / "decoder.bin").write_bytes(b"model")
        libc = CoveringLibc()
        launcher_program.place_masks(launch(tmp_path, _payload_under(plan, tmp_path), libc=libc))
        targets = _bind_targets(libc)
        seal = targets.index(os.path.realpath(home / "run"))
        hide = targets.index(os.path.realpath(leaf))
        carve = targets.index(os.path.realpath(probe))
        assert seal < hide < carve
        assert os.listdir(leaf) == []

    def test_launcher_carveout_mounts_fail_open(self, monkeypatch, tmp_path, capfd):
        """The two carve-out mounts WIDEN access, so they must not route
        through ``_mount_or_die``: a host refusing them keeps the seal
        (pre-carve-out behavior) instead of losing every sandboxed probe."""
        home, probe = self._relocated_home(monkeypatch, tmp_path)
        plan = sandbox_mod._spawn_plan("namespace", "standard", extra_writable_dirs=(str(probe),))
        carve = plan.writable[0]
        for fail_at, what in ((1, "bind"), (2, "remount")):
            libc = RecordingLibc(fail_at=fail_at)
            run = launch(tmp_path, payload(writable_dirs=[carve]), libc=libc)
            assert refusal(launcher_program.apply_carveouts, run) is None
            assert (
                f"sandbox: WARNING -- writable carve-out {what} for {carve} failed "
                f"(errno {errno.EPERM}); continuing with the path sealed"
            ) in capfd.readouterr().err
            # A bind that failed is not followed by its remount.
            assert len(libc.calls) == fail_at

    @_POSIX_ONLY
    def test_launcher_refuses_carveout_inside_unhidden_tree(self, monkeypatch, tmp_path):
        """A caller-re-exposed (``extra_visible_dirs``) tree must still refuse
        a writable window: exposure cancels the hide, not the write seal, so
        the validator's guard set must include the ``unhidden`` entries (the
        ``+ unhidden`` term in the planner is load-bearing)."""
        home, _probe = self._relocated_home(monkeypatch, tmp_path)
        exposed = home / "run" / "exposed-tree"
        inside = exposed / "scratch"
        inside.mkdir(parents=True)
        plan = sandbox_mod._spawn_plan(
            "namespace",
            "standard",
            extra_hidden_dirs=(str(exposed),),
            extra_visible_dirs=(str(exposed),),
            extra_writable_dirs=(str(inside),),
        )
        assert plan.writable == ()
        assert str(exposed) not in plan.sensitive_dirs
        assert [c.path for c in plan.cancellations] == [str(exposed)]
        assert [r.args for r in plan.refusals if r.kind == "carve-out"] == [
            (str(inside), f"it lies inside sealed subtree {str(exposed)!r}")
        ]

    def test_symlinked_data_home_emits_both_spellings(self, monkeypatch, tmp_path):
        """A symlinked data home (supported) must carve BOTH spellings:
        Seatbelt rules are path-based and see each spelling independently."""
        real_home = tmp_path / "real-home"
        real_home.mkdir()
        lexical_home = tmp_path / "linked-home"
        os.symlink(real_home, lexical_home)
        monkeypatch.setattr(sandbox_mod, "config_dir", lambda: lexical_home)
        probe = lexical_home / "run" / "mcp-tmp" / "probe-x"
        probe.mkdir(parents=True)
        lexical = os.path.normpath(str(probe))
        canonical = os.path.realpath(lexical)
        assert lexical != canonical  # the premise of the test
        profile = _build_seatbelt_profile("standard", extra_writable_dirs=(str(probe),))
        for spelling in (lexical, canonical):
            assert f'(allow file-write* (subpath "{spelling}"))' in profile

    def test_validator_refuses_dir_inside_hidden_tree(self, monkeypatch, tmp_path):
        """A carve-out under a read-hidden tree must be refused even when a
        (buggy or hostile) caller also names that tree as a carveable parent:
        write access without read access is still a tampering channel."""
        home, _probe = self._relocated_home(monkeypatch, tmp_path)
        hidden = home / "run" / "secrets"
        inside = hidden / "scratch"
        inside.mkdir(parents=True)
        approved, _refusals = sandbox_plan.writable_carveouts(
            (str(inside),),
            sandbox_mod._carveout_probes((str(inside),)),
            subtree_guards=[str(hidden)],
            literal_guards=[],
            carveable_parents=[str(home / "run")],
        )
        assert approved == []


class TestSealedRuntimeParentPredicate:
    """The question asked BEFORE a config-declared dir reaches a child.

    A spec-declared ``TMPDIR`` under ``<data home>/run`` is sealed read-only by
    both backends, and the write carve-out above is validated for self-derived
    scratch only — so the caller's only safe move is to stop honoring the path,
    which it can only do if ``classify_declared_temp_path`` answers honestly.
    Every test asserts the CLASSIFICATION (``None`` = honor; ``sealed`` or
    ``unclassifiable`` = refuse, and why), because the probe names that cause
    in its WARNING and a wrong cause misdirects the operator even when the
    refusal itself is right.
    """

    def _home(self, monkeypatch, tmp_path):
        home = tmp_path / "crew-home"
        (home / "run").mkdir(parents=True)
        monkeypatch.setattr(sandbox_mod, "config_dir", lambda: home)
        return home

    def test_declared_path_inside_the_run_parent_is_sealed(self, monkeypatch, tmp_path):
        home = self._home(monkeypatch, tmp_path)
        classify = sandbox_mod.classify_declared_temp_path
        assert classify(str(home / "run" / "custom-tmp")) == "sealed"
        # The parent itself and the managed root under it answer the same way:
        # containment, not a leaf allowlist.
        assert classify(str(home / "run")) == "sealed"
        assert classify(str(home / "run" / "mcp-tmp" / "probe-x")) == "sealed"

    def test_path_outside_the_data_home_is_not_sealed(self, monkeypatch, tmp_path):
        self._home(monkeypatch, tmp_path)
        chosen = tmp_path / "operator-volume" / "tmp"
        chosen.mkdir(parents=True)
        assert sandbox_mod.classify_declared_temp_path(str(chosen)) is None
        # An empty declaration is not a path and must not read as refused.
        assert sandbox_mod.classify_declared_temp_path("") is None

    def test_relative_declaration_is_unclassifiable(self, monkeypatch, tmp_path):
        self._home(monkeypatch, tmp_path)
        assert sandbox_mod.classify_declared_temp_path("run/x") == "unclassifiable"

    @_POSIX_ONLY
    def test_both_spellings_of_a_symlinked_data_home_are_sealed(self, monkeypatch, tmp_path):
        # config_dir() deliberately preserves a supported symlinked data home,
        # and path-based sandbox rules see each spelling independently — so a
        # declaration written either way must be recognised.
        real = tmp_path / "real-home"
        (real / "run").mkdir(parents=True)
        link = tmp_path / "linked-home"
        link.symlink_to(real)
        monkeypatch.setattr(sandbox_mod, "config_dir", lambda: link)
        classify = sandbox_mod.classify_declared_temp_path
        assert classify(str(link / "run" / "custom-tmp")) == "sealed"
        assert classify(str(real / "run" / "custom-tmp")) == "sealed"

    @_POSIX_ONLY
    def test_a_symlink_climbing_back_into_the_run_parent_is_sealed(self, monkeypatch, tmp_path):
        # Resolution ORDER is the whole test. `..` must be collapsed by realpath,
        # after symlinks, the way the child's libc will collapse it -- collapsing
        # it lexically first deletes the very symlink it was climbing out of, so
        # a declaration routed through a link back into the seal reads as outside.
        home = self._home(monkeypatch, tmp_path)
        (home / "run" / "inner").mkdir()
        link = tmp_path / "into-run"
        link.symlink_to(home / "run" / "inner")
        # Lexically `<link>/../tmp` collapses to `<tmp_path>/tmp`, which is
        # outside the seal; resolved symlink-first it is `<home>/run/tmp`.
        assert sandbox_mod.classify_declared_temp_path(str(link / ".." / "tmp")) == "sealed"

    @staticmethod
    def _install_case_insensitive_stat(monkeypatch, root: Path) -> None:
        """Model an APFS-style case-insensitive ``stat``/``lstat`` under *root* only.

        The bypass this guards needs a filesystem that answers for a
        differently-cased spelling, which Linux CI cannot create for real. What
        APFS actually does is narrow: the fold lives in NAME LOOKUP, so each
        component resolves case-insensitively for ``stat`` and ``lstat`` alike,
        while ``realpath`` keeps the caller's spelling (it walks with
        ``lstat``/``readlink``, neither of which rewrites a component's case) --
        so the alias never reaches a lexical comparison in canonical form.
        Reproduce exactly that, delegating every path outside *root* to the real
        call so nothing else in the process is disturbed.
        """
        real = {"stat": os.stat, "lstat": os.lstat}
        root_str = str(root)

        def _fold(path: str) -> str | None:
            resolved = os.path.dirname(root_str)
            for part in os.path.relpath(path, resolved).split(os.sep):
                try:
                    entries = os.listdir(resolved)
                except OSError:
                    return None
                match = next((e for e in entries if e.lower() == part.lower()), None)
                if match is None:
                    return None
                resolved = os.path.join(resolved, match)
            return resolved

        def _folding(name: str):
            def fake(path, *args, **kwargs):
                try:
                    return real[name](path, *args, **kwargs)
                except FileNotFoundError:
                    if not isinstance(path, (str, os.PathLike)):
                        raise
                    spelling = os.fspath(path)
                    if not isinstance(spelling, str) or not spelling.startswith(root_str + os.sep):
                        raise
                    folded = _fold(spelling)
                    if folded is None:
                        raise
                    return real[name](folded, *args, **kwargs)

            return fake

        monkeypatch.setattr(os, "stat", _folding("stat"))
        monkeypatch.setattr(os, "lstat", _folding("lstat"))

    def test_a_case_alias_of_the_run_parent_is_sealed(self, monkeypatch, tmp_path):
        # On case-insensitive APFS `<data home>/RUN` and `<data home>/run`
        # are ONE directory, and realpath does not fold the difference -- so a
        # lexical predicate answers "not sealed" for a path the backends seal,
        # which would let the probe honor the declaration and hand the child a
        # read-only TMPDIR.
        home = self._home(monkeypatch, tmp_path)
        (home / "run" / "custom-tmp").mkdir()
        self._install_case_insensitive_stat(monkeypatch, tmp_path)
        classify = sandbox_mod.classify_declared_temp_path
        assert classify(str(home / "RUN" / "custom-tmp")) == "sealed"
        # The parent's own differently-cased spelling answers the same way.
        assert classify(str(home / "Run")) == "sealed"

    def test_a_missing_leaf_under_a_case_aliased_parent_is_sealed(self, monkeypatch, tmp_path):
        # A declared temp normally does NOT exist yet, so the deepest EXISTING
        # ancestor is what carries the identity: `<home>/RUN` folds onto the
        # sealed parent, and nothing below a sealed directory can climb back out.
        home = self._home(monkeypatch, tmp_path)
        self._install_case_insensitive_stat(monkeypatch, tmp_path)
        assert (
            sandbox_mod.classify_declared_temp_path(str(home / "RUN" / "not-created-yet" / "tmp"))
            == "sealed"
        )

    def test_a_case_alias_outside_the_run_parent_is_still_not_sealed(self, monkeypatch, tmp_path):
        # Identity is the whole test: a differently-cased path that folds onto a
        # directory OUTSIDE the seal must stay honored, or the fix would refuse
        # every operator-chosen temp whose spelling merely resembles the seal.
        home = self._home(monkeypatch, tmp_path)
        (home / "runtime-cache").mkdir()
        self._install_case_insensitive_stat(monkeypatch, tmp_path)
        assert sandbox_mod.classify_declared_temp_path(str(home / "RUNTIME-cache" / "tmp")) is None

    def test_a_real_case_alias_is_sealed_where_the_filesystem_folds_case(
        self, monkeypatch, tmp_path
    ):
        # The same claim against the REAL filesystem, for the platforms that
        # actually fold (APFS, NTFS). Skipped on a case-sensitive CI filesystem,
        # where the aliased spelling is a different directory and nothing seals it.
        home = self._home(monkeypatch, tmp_path)
        if not (home / "RUN").is_dir():
            pytest.skip("test filesystem is case-sensitive; no real case alias to build")
        assert sandbox_mod.classify_declared_temp_path(str(home / "RUN" / "custom-tmp")) == "sealed"

    @_POSIX_ONLY
    def test_the_identity_walk_does_not_follow_a_final_symlink(self, tmp_path):
        # The identity probe runs on paths that came from config text, so it stats
        # NO-FOLLOW: `lstat` never traverses the final component, which is exactly
        # where a planted link could point at a remote or stalling target and turn
        # a local containment question into off-host I/O.
        sealed = tmp_path / "run"
        sealed.mkdir()
        link = tmp_path / "link-into-run"
        link.symlink_to(sealed)
        # The LINK's own identity is what the walk sees, never the sealed
        # directory it points at -- so this spelling answers False here.
        assert not sandbox_mod._identity_within_sealed_parent(str(link), str(sealed))

    @_POSIX_ONLY
    def test_a_symlink_to_the_run_parent_is_still_sealed(self, monkeypatch, tmp_path):
        # No coverage is lost by stating no-follow: symlink resolution lives in
        # the CANONICAL spelling, which `realpath` has already produced by the
        # time the identity walk runs -- so a link INTO the seal is still refused.
        home = self._home(monkeypatch, tmp_path)
        link = tmp_path / "link-into-run"
        link.symlink_to(home / "run")
        assert sandbox_mod.classify_declared_temp_path(str(link)) == "sealed"

    @_POSIX_ONLY
    def test_a_benign_local_symlink_chain_still_resolves(self, monkeypatch, tmp_path):
        # Containment is judged on `realpath`, so a multi-hop LOCAL chain ending
        # outside the seal stays honored, and the same chain ending inside it is
        # refused on the merits -- the chain itself is never a reason to refuse.
        home = self._home(monkeypatch, tmp_path)
        outside = tmp_path / "operator-volume" / "tmp"
        outside.mkdir(parents=True)
        (tmp_path / "b-out").symlink_to(outside)
        (tmp_path / "a-out").symlink_to(tmp_path / "b-out")
        assert sandbox_mod.classify_declared_temp_path(str(tmp_path / "a-out")) is None
        (tmp_path / "b-in").symlink_to(home / "run" / "custom-tmp")
        (tmp_path / "a-in").symlink_to(tmp_path / "b-in")
        assert sandbox_mod.classify_declared_temp_path(str(tmp_path / "a-in")) == "sealed"

    @_POSIX_ONLY
    def test_a_symlink_cycle_is_refused_instead_of_looping(self, monkeypatch, tmp_path):
        # ELOOP guard, owned by the kernel: `realpath(strict=True)` raises on a
        # cycle, and the lenient form would instead hand back the cycle's own
        # link with the remainder appended -- a spelling that says nothing about
        # where the temp really is, which a lexical or identity comparison would
        # then wave through. A path whose canonical form cannot be established is
        # refused, matching every other unverifiable declaration here. It is its
        # OWN cause, not "sealed": a diagnostic that called a cycle sealed would
        # send the operator looking under ``run``.
        self._home(monkeypatch, tmp_path)
        first = tmp_path / "loop-a"
        second = tmp_path / "loop-b"
        first.symlink_to(second)
        second.symlink_to(first)
        assert sandbox_mod.classify_declared_temp_path(str(first)) == "unclassifiable"
        # The cycle need not be the leaf: a declared temp usually does not exist
        # yet, and the component carrying the cycle is then an ANCESTOR of it.
        assert (
            sandbox_mod.classify_declared_temp_path(str(first / "not-created-yet" / "tmp"))
            == "unclassifiable"
        )

    @_POSIX_ONLY
    def test_a_missing_leaf_is_resolved_as_far_as_it_goes(self, monkeypatch, tmp_path):
        # The strict resolve must not turn the ORDINARY case into a refusal: a
        # declared temp that does not exist yet is judged by the deepest existing
        # ancestor, outside the seal honored and inside it sealed.
        home = self._home(monkeypatch, tmp_path)
        outside = tmp_path / "operator-volume"
        outside.mkdir()
        assert sandbox_mod.classify_declared_temp_path(str(outside / "not-yet" / "tmp")) is None
        assert (
            sandbox_mod.classify_declared_temp_path(str(home / "run" / "not-yet" / "tmp"))
            == "sealed"
        )

    @_POSIX_ONLY
    def test_a_posix_double_slash_declaration_is_judged_on_its_merits(self, monkeypatch, tmp_path):
        # `//mnt/data/tmp` is a legal POSIX spelling of `/mnt/data/tmp`, not a
        # UNC share, and no spelling is refused on its own here: outside the seal
        # it is honored; the same spelling INTO the seal is caught on the merits.
        home = self._home(monkeypatch, tmp_path)
        chosen = tmp_path / "operator-volume" / "tmp"
        chosen.mkdir(parents=True)
        assert sandbox_mod.classify_declared_temp_path("/" + str(chosen)) is None
        assert sandbox_mod.classify_declared_temp_path("/" + str(home / "run" / "tmp")) == "sealed"

    @staticmethod
    def _windows_path_semantics(monkeypatch) -> None:
        """Run the classification as Windows, on whichever OS runs the test."""
        monkeypatch.setattr("kiro_crew.sandbox.sys.platform", "win32")

    def test_on_windows_a_declared_temp_is_honored_because_nothing_seals_run(
        self, monkeypatch, tmp_path
    ):
        # Kiro Crew has no native Windows sandbox backend, so ``<data home>/run``
        # is never sealed for a probe child there and a declared temp under it is
        # writable. The classification therefore answers None for every path on
        # win32 -- the declaration is honored exactly as it was before the check
        # existed -- and nothing is resolved, since on Windows ``realpath`` and
        # ``stat`` OPEN the path they are handed.
        home = self._home(monkeypatch, tmp_path)
        self._windows_path_semantics(monkeypatch)
        resolutions: list[str] = []
        real_realpath = os.path.realpath

        def watched(path, *args, **kwargs):
            resolutions.append(os.fspath(path))
            return real_realpath(path, *args, **kwargs)

        monkeypatch.setattr(os.path, "realpath", watched)
        assert sandbox_mod.classify_declared_temp_path(str(home / "run" / "custom-tmp")) is None
        assert sandbox_mod.classify_declared_temp_path(str(home / "run")) is None
        assert resolutions == []


class TestBuildLauncherScript:
    @_POSIX_ONLY
    def test_strict_script_contains_dirs(self):
        plan = sandbox_mod._spawn_plan("namespace", "strict")
        home = str(Path.home())
        assert os.path.join(home, ".aws") in plan.sensitive_dirs
        assert os.path.join(home, ".gnupg") in plan.sensitive_dirs

    @_POSIX_ONLY
    def test_strict_script_denies_namespace_escape_not_hardlinks(self):
        """Linux seccomp deny list must contain the namespace-escape syscalls
        (mount/umount2/unshare/setns/pivot_root) and must NOT contain
        link/linkat -- hardlink containment is the bind-mask's job, and a
        blanket link ban broke hardlink-using build tools (npm cacache). Guards
        against an accidental re-add of link/linkat or drop of an escape
        syscall (pentest finding #9 remediation). Asserted on the filter the
        launcher installs, by running it on each syscall."""
        cases = (
            # mount=165 umount2=166 unshare=272 setns=308 pivot_root=155; link=86 linkat=265
            ("x86_64", (165, 166, 272, 308, 155), (86, 265)),
            # mount=40 umount2=39 unshare=97 setns=268 pivot_root=41; linkat=37
            ("aarch64", (40, 39, 97, 268, 41), (37,)),
        )
        for machine, escape, links in cases:
            denied, _kill_nr = launcher_program.seccomp_deny_table(machine)
            assert denied == escape
            for nr in escape:
                assert _seccomp_verdict(machine, nr, 0) == _SECCOMP_EPERM, (machine, nr)
            for nr in links:
                assert _seccomp_verdict(machine, nr, 0) == _SECCOMP_ALLOW, (machine, nr)

    @_POSIX_ONLY
    def test_launcher_refuses_when_seccomp_cannot_be_installed(self, monkeypatch, tmp_path):
        """An arch with no syscall table, or a libc without prctl(2), must make
        the launcher EXIT -- not skip the confinement steps and exec the agent anyway.

        Skipping leaves ``unshare`` permitted, so the child can enter a nested
        user namespace, hold CAP_SYS_ADMIN over a copy of this mount tree, and
        umount every credential mask -- the escape the filter exists to deny.
        Both ``_inside_kirocrew_sandbox`` and
        ``docs/system-specs/modules/security.md`` state that a sandboxed tree is
        confined "by the outer namespace + seccomp", so a silent skip makes that
        claim false while every caller still reads the spawn as isolated.
        """
        # The rendered launcher stays valid Python at every tier.
        for level in ("standard", "cc", "strict"):
            compile(_build_launcher_script(level), "<launcher-%s>" % level, "exec")

        supported = (
            ("x86_64", (165, 166, 272, 308, 155)),
            ("aarch64", (40, 39, 97, 268, 41)),
        )
        for machine, table in supported:
            monkeypatch.setattr(launcher_program._plat, "machine", lambda m=machine: m)
            libc = RecordingLibc()
            assert refusal(launcher_program.install_seccomp, launch(tmp_path, libc=libc)) is None
            assert launcher_program.seccomp_deny_table(machine)[0] == table
            # PR_SET_SECCOMP with SECCOMP_MODE_FILTER: the filter was installed.
            assert [p[:2] for p in libc.prctls] == [(22, 2)]

        for machine in ("riscv64", "armv7l", "ppc64le", "s390x"):
            monkeypatch.setattr(launcher_program._plat, "machine", lambda m=machine: m)
            libc = RecordingLibc()
            run = launch(tmp_path, libc=libc)
            message = refusal(launcher_program.run_child, run, ["/bin/agent"])
            assert message is not None and "sandbox: BLOCKED" in message
            assert repr(machine) in message
            # The refusal must name the explicit opt-out, or an operator on such
            # a host is left with no way forward.
            assert "agent.sandbox_allow_unsandboxed_exec" in message
            assert all(p[0] != 22 for p in libc.prctls), "a filter was installed"
            assert run.execs == []

        libc = RecordingLibc()
        libc.prctl = None  # type: ignore[assignment]
        run = launch(tmp_path, libc=libc)
        message = refusal(launcher_program.run_child, run, ["/bin/agent"])
        assert message is not None and "libc exposes no prctl(2)" in message
        assert run.execs == []

    @_POSIX_ONLY
    def test_standard_script_excludes_aws(self):
        plan = sandbox_mod._spawn_plan("namespace", "standard")
        # Standard dirs don't include .aws, and ~/.ssh stays unmasked.
        assert os.path.join(str(Path.home()), ".aws") not in plan.sensitive_dirs
        assert plan.hide_ssh is False
        assert sandbox_plan.namespace_payload(plan)["hide_ssh"] == 0

    @_POSIX_ONLY
    def test_auth_staging_is_hidden_except_for_trusted_auth_spawn(self):
        home = Path.home()
        staging = home / ".kiro" / "crew-auth-staging"
        workspace = staging / "auth-123"
        data_home = home / ".kiro" / "crew"

        regular_plan = sandbox_mod._spawn_plan("namespace", "standard")
        auth_plan = sandbox_mod._spawn_plan(
            "namespace",
            "standard",
            extra_hidden_dirs=(str(data_home),),
            extra_visible_dirs=(str(workspace),),
        )
        regular_profile = _build_seatbelt_profile("standard")
        auth_profile = _build_seatbelt_profile(
            "standard",
            extra_hidden_dirs=(str(data_home),),
            extra_visible_dirs=(str(workspace),),
        )

        assert str(staging) in regular_plan.sensitive_dirs
        assert str(staging) in regular_profile
        # The auth spawn's own workspace lifts the staging mask, and no field of the
        # launcher's data names the staging tree any more.
        assert (
            sandbox_plan.Cancellation(path=str(staging), by=(str(workspace),), read_only=False)
            in auth_plan.cancellations
        )
        assert str(staging) not in repr(sandbox_plan.namespace_payload(auth_plan))
        assert str(staging) not in auth_profile
        assert str(data_home) in auth_plan.sensitive_dirs
        assert str(data_home) in auth_profile

    @_POSIX_ONLY
    def test_a_file_valued_hidden_path_reaches_the_file_loop(self, tmp_path):
        """A hidden path that is a FILE must reach the launcher's file-masking stage.

        The launcher's two stages hide each kind differently — a directory gets an
        empty dir bind-mounted over it, a file gets an empty temp file — and each
        takes only the entries of its own kind. So a file entry offered to the
        directory stage alone would be SILENTLY SKIPPED: the caller asked for it to be
        hidden, got no error, and the file stayed readable.

        Not hypothetical: ``security.sensitive_home_dirs()`` is not all directories
        (``sel_hmac.key``, ``token_signing.key``, ``.kiro/crew/.env`` are files), and
        Papyrus passes that whole list as ``extra_hidden_dirs`` so a ``.tex`` cannot
        ``\\input`` the gateway's own secrets into a rendered PDF.

        Every path goes in BOTH lists and the CHILD classifies it — see the next test
        for why that, rather than deciding here.
        """
        secret = tmp_path / "token_signing.key"
        secret.write_text("s3cret", encoding="utf-8")
        real_dir = tmp_path / "creds"
        real_dir.mkdir()
        (real_dir / "id").write_text("key", encoding="utf-8")

        plan = sandbox_mod._spawn_plan(
            "namespace", "strict", extra_hidden_dirs=(str(secret), str(real_dir))
        )

        # The file reaches the stage that can actually hide it.
        assert str(secret) in plan.sensitive_files, "a file-valued hidden path cannot be hidden"
        # And the directory still reaches its own stage.
        assert str(real_dir) in plan.sensitive_dirs

    def test_the_builder_does_not_stat_the_hidden_paths(self, monkeypatch, tmp_path):
        """No filesystem probe of a hidden path while the launcher is built.

        Classifying each path here with ``os.path.isfile()`` would be 52 stats per
        async spawn on the gateway's single loop, and on a stalled NFS home each one
        blocks — freezing every session, cron and the liveness heartbeat. The child already re-checks with its own
        ``isdir``/``isfile`` per stage, so whichever stage matches does the work and
        the other skips; letting it decide keeps the syscalls where they were already
        happening and where blocking costs only that one spawn.

        Every ``stat`` and ``lstat`` the build makes is recorded, and so is every
        ``os.path`` predicate: on POSIX ``isfile``, ``isdir``, ``exists``, ``islink`` and
        ``realpath`` reach ``stat``/``lstat``, but on Windows ``isdir`` and ``exists``
        are ``nt._path_*`` builtins that never call ``os.stat``. None may name a hidden
        path or anything under one.
        """
        hidden = []
        for index in range(52):
            path = tmp_path / f"hidden-{index}"
            if index % 2:
                path.mkdir()
            else:
                path.write_text("s", encoding="utf-8")
            hidden.append(str(path))
        probed: list[str] = []

        def _recording(real):
            def _probe(path, *args, **kwargs):
                if not isinstance(path, int):
                    probed.append(os.fsdecode(os.fspath(path)))
                return real(path, *args, **kwargs)

            return _probe

        monkeypatch.setattr(os, "stat", _recording(os.stat))
        monkeypatch.setattr(os, "lstat", _recording(os.lstat))
        for predicate in ("isfile", "isdir", "exists", "lexists", "islink", "realpath"):
            monkeypatch.setattr(os.path, predicate, _recording(getattr(os.path, predicate)))
        # The namespace plan asks this process for its uid and gid, which Windows has
        # no call for; a stand-in there keeps this check as platform-neutral as the
        # property it states.
        monkeypatch.setattr(os, "getuid", getattr(os, "getuid", lambda: 0), raising=False)
        monkeypatch.setattr(os, "getgid", getattr(os, "getgid", lambda: 0), raising=False)

        _build_launcher_script("strict", extra_hidden_dirs=tuple(hidden))

        touched = sorted(
            {p for p in probed if any(p == h or p.startswith(h + os.sep) for h in hidden)}
        )
        assert touched == [], f"the launcher build stats hidden paths: {touched}"

    @_POSIX_ONLY
    def test_every_sensitive_path_reaches_a_loop_that_can_hide_it(self):
        """Whole-list check against the real sensitive-path list.

        Both stages self-guard, so a path present in both is hidden by whichever stage
        matches its actual type — and a future entry that happens to be a file cannot
        silently stop being hidden.
        """
        from kiro_crew import security

        home = os.path.expanduser("~")
        extra = tuple(os.path.join(home, rel) for rel in security.sensitive_home_dirs())
        plan = sandbox_mod._spawn_plan("namespace", "strict", extra_hidden_dirs=extra)

        for path in extra:
            assert path in plan.sensitive_dirs, f"{path} never reaches the directory loop"
            assert path in plan.sensitive_files, f"{path} never reaches the file loop"

    @_LINUX_ONLY
    def test_both_hiding_stages_hide_a_planned_path_by_its_kind(self, tmp_path):
        """Offered every hidden path, the two stages hide each by its own kind.

        The file and the directory a caller asked to hide are planned into both
        lists; the launcher's directory stage masks the directory and skips the
        file, its file stage masks the file and skips the directory's stand-in.
        """
        secret = tmp_path / "token_signing.key"
        secret.write_text("s3cret", encoding="utf-8")
        real_dir = tmp_path / "creds"
        real_dir.mkdir()
        (real_dir / "id").write_text("key", encoding="utf-8")
        plan = sandbox_mod._spawn_plan(
            "namespace", "strict", extra_hidden_dirs=(str(secret), str(real_dir))
        )
        run = launch(tmp_path, _payload_under(plan, tmp_path))
        launcher_program.mask_sensitive(run)
        launcher_program.mask_sensitive_files(run)
        assert secret.read_text(encoding="utf-8") == ""
        assert os.listdir(real_dir) == []

    @_POSIX_ONLY
    def test_cc_script_exposes_aws_config(self):
        plan = sandbox_mod._spawn_plan("namespace", "cc")
        aws = os.path.join(str(Path.home()), ".aws")
        # The tree is masked and its config is restored read-only into the mask.
        assert aws in plan.sensitive_dirs
        assert (os.path.join(aws, "config"), "config") in plan.expose

    @_POSIX_ONLY
    def test_script_scrubs_env_vars(self, tmp_path):
        plan = sandbox_mod._spawn_plan("namespace", "strict")
        assert set(_SENSITIVE_ENV_PREFIXES) <= set(plan.env_scrub_prefixes)
        planted = {prefix + "_PLANTED": "secret" for prefix in _SENSITIVE_ENV_PREFIXES}
        run = launch(
            tmp_path,
            payload(env_prefixes=list(plan.env_scrub_prefixes)),
            environ={**planted, "KEEP_ME": "1"},
        )
        launcher_program.scrub_env(run)
        assert sorted(set(planted) & set(run.environ)) == []
        assert run.environ["KEEP_ME"] == "1"

    @_POSIX_ONLY
    def test_launcher_has_no_unimportable_kiro_crew_refs(self, tmp_path):
        """The launcher runs as a standalone ~/.kirocrew/run script with the
        launcher dir scrubbed from sys.path, so it CANNOT import kiro_crew.
        Referencing a module-level helper like ``platform_compat`` NameErrors at
        runtime and crashed every command cron. Guard: the program imports only
        the standard library, there is no module-qualified RUNTIME reference to
        any host-only module the isolated launcher can't import, the rendered
        script stays syntactically valid at every tier, and the read-only copy
        an exposed file is restored as is made with the program's own chmod.

        AST-based over the program's source -- the rendered launcher is that
        source plus one data line -- so the program's own explanatory comment
        naming platform_compat (why the inline os.chmod must NOT use it) does
        not false-positive: only module-qualified attribute access counts.
        """
        tree = ast.parse(launcher_program_source())
        used_modules = {
            node.value.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
        }
        forbidden = used_modules & {"platform_compat", "kiro_crew", "logger", "logging"}
        assert not forbidden, f"launcher references un-importable module(s) {forbidden}"
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                assert node.level == 0 and node.module, "the launcher imports relative to nothing"
                imported.add(node.module.split(".")[0])
        assert imported <= set(sys.stdlib_module_names), sorted(
            imported - set(sys.stdlib_module_names)
        )
        for level in ("strict", "standard", "cc"):
            compile(_build_launcher_script(level), "<launcher>", "exec")

        config = tmp_path / "config"
        config.write_text("[profile x]\n", encoding="utf-8")
        run = launch(tmp_path, payload(expose_files=[[str(config), "config"]]))
        launcher_program.preread_exposed_files(run)
        config.unlink()  # what the empty mask over its parent leaves
        launcher_program.restore_exposed_files(run)
        assert config.read_text(encoding="utf-8") == "[profile x]\n"
        assert stat.S_IMODE(os.stat(config).st_mode) == 0o444


class TestAgentEnvPassthroughContract:
    """Pin the claude-code-provider env-passthrough contract.

    Inherited ``ANTHROPIC_*`` / ``CLAUDE_CODE_*`` variables must keep flowing
    to claude-harness children (docs/system-specs/modules/claude-code-provider.md,
    env-passthrough section); the custom-endpoint guide
    (docs/guides/custom-llm-backend.md) depends on them reaching the child. If
    either scrub list grows to cover these namespaces, this fails red — the
    failure a green-CI hardening pass would otherwise ship silently.
    """

    _PASSTHROUGH_KEYS = [
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_MODEL",
        "ANTHROPIC_AUTH_TOKEN",
        "CLAUDE_CODE_SUBAGENT_MODEL",
        "CLAUDE_CODE_API_KEY_HELPER_TTL_MS",
    ]

    def test_anthropic_and_claude_code_env_survive_agent_subprocess_scrub(self):
        # Both lists are prefix-matched (``startswith`` in ``scrub_env`` and
        # ``scrub_agent_denied_env``), so assert prefix semantics, not bare
        # membership — a prefix entry like "ANTHROPIC_" would never equal a key.
        for key in self._PASSTHROUGH_KEYS:
            assert not any(key.startswith(p) for p in _SENSITIVE_ENV_PREFIXES), key
            assert not any(key.startswith(p) for p in sandbox_mod._AGENT_DENIED_ENV_KEYS), key
        # And the ACP spawn path's own parent-side scrub passes them through.
        env = {key: "x" for key in self._PASSTHROUGH_KEYS}
        assert sandbox_mod.scrub_agent_subprocess_env(env) == env


@_POSIX_ONLY
class TestHardlinkScanBudget:
    """The pre-exec hardlink scan: per-root budgets, loud truncation, no pruning.

    Driven through ``refuse_hardlinked_credentials``, the launcher stage that runs
    last before exec, over a tree under ``tmp_path``. Each root has its own budget,
    because one shared across the CWD and /tmp walks lets a large worktree consume it
    all before /tmp (the world-writable root the check exists for) is scanned at all;
    and an exhausted budget warns, because a silent one makes a truncated scan
    indistinguishable from a clean one.
    """

    @staticmethod
    def _credential(tmp_path):
        """A masked credential directory and the regular file inside it."""
        creds = tmp_path / "creds"
        creds.mkdir()
        secret = creds / "key"
        secret.write_text("s", encoding="utf-8")
        return creds, secret

    @staticmethod
    def _scan(run, roots, *budget):
        """The scan stage over *roots*; its refusal message, or ``None``."""
        return refusal(
            launcher_program.refuse_hardlinked_credentials, run, [str(r) for r in roots], *budget
        )

    def test_only_aliased_credential_inodes_arm_the_walk(self, tmp_path, capfd):
        # An inode with st_nlink == 1 has no alias anywhere on the
        # filesystem, so it must not enter the match set: when every
        # credential has nlink == 1 the CWD + /tmp walk is skipped and the
        # common healthy-host spawn pays nothing (and emits no truncation
        # warning). BOTH collection loops carry the gate: the masked
        # directories (depth 1) and the masked files. The per-app credentials
        # one level below a mask root reach the child as inodes the PARENT
        # read -- it cannot stat them itself, because it masks that tree in
        # this same process -- and the parent applies the same gate before
        # sending one. Every other path list the launcher carries is filled
        # with directories too, which is how this notices a further
        # collection loop added without the gate.
        #
        # REGULAR FILES only, and that half is not cosmetic: every directory has
        # nlink >= 2, and the masked-file list carries directories on purpose, so a
        # bare nlink test armed the walk on every spawn. A FIFO with a second name
        # stands in for every other non-regular kind. More behaviour is covered in
        # test_sandbox_hardlink_scan.py.
        creds, secret = self._credential(tmp_path)
        (creds / "sub").mkdir()
        fifo = creds / "fifo"
        os.mkfifo(fifo)
        try:
            os.link(fifo, tmp_path / "fifo-link")
        except OSError:
            pass  # a filesystem that gives a FIFO no second name has nothing to gate
        lone = tmp_path / "lone"
        lone.write_text("s", encoding="utf-8")
        a_dir = tmp_path / "a-dir"
        (a_dir / "inner").mkdir(parents=True)
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "f").write_text("f", encoding="utf-8")
        run = launch(
            tmp_path,
            payload(
                sensitive_dirs=[str(creds), str(a_dir)],
                sensitive_files=[str(lone), str(a_dir), str(creds), str(fifo)],
                readonly_dirs=[str(a_dir)],
                writable_dirs=[str(a_dir)],
                private_dirs=[str(a_dir / "inner")],
                expose_files=[[str(lone), "lone"]],
            ),
        )
        # A budget of 0 makes an armed walk report itself at the first file it meets.
        assert self._scan(run, [workspace], 0) is None
        assert "hardlink scan truncated" not in capfd.readouterr().err

        # A second name for a credential in a masked directory arms it ...
        os.link(secret, tmp_path / "secret-link")
        assert self._scan(run, [workspace], 0) is None
        assert f"truncated at 0 files in {workspace}" in capfd.readouterr().err
        os.unlink(tmp_path / "secret-link")
        # ... and so does one for a masked file.
        os.link(lone, tmp_path / "lone-link")
        assert self._scan(run, [workspace], 0) is None
        assert f"truncated at 0 files in {workspace}" in capfd.readouterr().err

    def test_per_root_budget_covers_a_busy_tmp(self, tmp_path, capfd, monkeypatch):
        # The budget only applies once a credential inode is actually
        # aliased (see the nlink gate above), so it can afford to be
        # generous: 100k covers the busiest observed /tmp (~11.8k files)
        # with an order of magnitude to spare, making truncation genuinely
        # exceptional rather than a steady-state warning. The stage's own
        # default is what the launcher runs with; a root reporting one entry
        # more than that is where it stops.
        _creds, secret = self._credential(tmp_path)
        os.link(secret, tmp_path / "secret-link")
        busy = tmp_path / "busy-tmp"
        busy.mkdir()
        names = [f"entry-{index}" for index in range(100_001)]
        real_walk = os.walk

        def _walk(top, *args, **kwargs):
            if top == str(busy):
                yield top, [], names
                return
            yield from real_walk(top, *args, **kwargs)

        monkeypatch.setattr(launcher_program.os, "walk", _walk)
        run = launch(tmp_path, payload(sensitive_files=[str(secret)]))
        assert self._scan(run, [busy]) is None
        assert f"truncated at 100000 files in {busy}" in capfd.readouterr().err

    def test_per_root_budget_with_counter_reset_inside_root_loop(self, tmp_path, capfd):
        # Each root gets exactly one fresh budget, so a large CWD cannot
        # starve the /tmp scan -- and one budget per ROOT, not per directory:
        # a reset inside the walk would make the scan effectively unbounded.
        _creds, secret = self._credential(tmp_path)
        large = tmp_path / "large-cwd"
        for sub in ("d1", "d2"):
            (large / sub).mkdir(parents=True)
            for name in ("x", "y"):
                (large / sub / name).write_text(name, encoding="utf-8")
        small = tmp_path / "small-tmp"
        small.mkdir()
        os.link(secret, small / "alias")
        run = launch(tmp_path, payload(sensitive_files=[str(secret)]))
        message = self._scan(run, [large, small], 3)
        # The first root ran out across its directories ...
        assert f"truncated at 3 files in {large}" in capfd.readouterr().err
        # ... and the second still had a whole budget of its own.
        assert message is not None and str(small / "alias") in message

    def test_truncation_warns_on_stderr_without_exiting(self, tmp_path, capfd):
        _creds, secret = self._credential(tmp_path)
        os.link(secret, tmp_path / "secret-link")
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        for name in ("a", "b", "c"):
            (workspace / name).write_text(name, encoding="utf-8")
        run = launch(tmp_path, payload(sensitive_files=[str(secret)]))
        # Deliberate fail-open: the truncation path warns, it never exits.
        assert self._scan(run, [workspace], 2) is None
        # The diagnostic goes to stderr, which the parent already captures.
        assert (
            "sandbox: WARNING — pre-exec hardlink scan truncated at 2 files in "
            f"{workspace}; scan incomplete (control degrades open)"
        ) in capfd.readouterr().err

    def test_blocked_exit_path_for_found_hardlinks_still_present(self, tmp_path):
        _creds, secret = self._credential(tmp_path)
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        os.link(secret, workspace / "copy")
        run = launch(tmp_path, payload(sensitive_files=[str(secret)]))
        assert self._scan(run, [workspace]) == (
            "sandbox: BLOCKED — found hardlink(s) to protected credential inodes: "
            f"{[str(workspace / 'copy')]}. Remove them before running."
        )

    def test_no_directory_pruning_in_the_scan(self, tmp_path):
        # /tmp is world-writable and the sandboxed agent shares the uid, so
        # any name- or prefix-based prune list is a deterministic bypass: the
        # attacker just names their directory to match. The scan must visit
        # every directory the depth limit allows; noisy trees are handled by
        # the per-root budget + truncation warning, never by skipping. So an
        # alias under each of the names a noisy /tmp holds is found.
        _creds, secret = self._credential(tmp_path)
        tmp_root = tmp_path / "tmp"
        aliases = []
        for name in (
            "systemd-private-1234",
            ".X11-unix",
            "pip-build-env-x",
            "node-compile-cache",
            "pytest-of-agent",
        ):
            nested = tmp_root / name / "nested"
            nested.mkdir(parents=True)
            os.link(secret, nested / "alias")
            aliases.append(str(nested / "alias"))
        run = launch(tmp_path, payload(sensitive_files=[str(secret)]))
        message = self._scan(run, [tmp_root])
        assert message is not None
        assert [alias for alias in aliases if alias not in message] == []

    def test_generated_script_compiles_at_every_level(self):
        # Proves the rendered launcher is syntactically valid Python for every
        # sandbox level.
        for level in ("strict", "standard", "cc"):
            compile(_build_launcher_script(level), "<launcher>", "exec")
