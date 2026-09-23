"""The overload-resilience state the gateway keeps under the
crew data home is fenced from agents.

One new crew-home entry, on BOTH surfaces the repo already ratchets: the tool
gate (``security._CREW_SECRET_LEAVES`` -> ``is_sensitive_path``) and the OS
sandbox (``sandbox._CREW_HIDDEN_LEAVES``):

* ``tasks/`` -- the durable task queue (``taskq/store.py``), other sessions'
  accepted work and the scheduler's authority. Hidden + fenced.

Plus the per-process scratch root (``scratch/``): every session spawn gets its
own ``<home>/scratch/<label>-<rand>``; the sandbox masks the root as a whole
and re-exposes only the spawn's own directory as a private window
(``extra_private_dirs``), so one session's scratch is never readable from a
sibling session's sandbox.

The paths asserted are the ones the module REALLY uses
(``TaskStore.default_path``), so a moved file cannot silently leave the fence
behind.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from kiro_crew import sandbox, security
from kiro_crew.taskq.store import TaskStore

_HOME = os.path.expanduser("~")
_CREW = os.path.join(_HOME, ".kiro", "crew")


def _real_paths(scratch: Path | None = None) -> dict[str, str]:
    """The modules' own layout, computed against a scratch home (so the test
    never opens anything under the operator's real crew home) and rebased onto
    the crew-home spelling the gate matches."""
    probe = scratch if scratch is not None else Path(_HOME) / ".kiro" / "crew-fence-probe"

    def _rebase(path: str | Path) -> str:
        return os.path.join(_CREW, os.path.relpath(str(path), str(probe)))

    return {
        "tasks_db": _rebase(TaskStore.default_path(probe)),
        "tasks_wal": _rebase(str(TaskStore.default_path(probe)) + "-wal"),
    }


class TestToolGateFence:
    @pytest.mark.parametrize("key", ["tasks_db", "tasks_wal"])
    def test_the_real_path_is_sensitive(self, key: str, tmp_path: Path) -> None:
        assert security.is_sensitive_path(_real_paths(tmp_path)[key]), key

    def test_the_leaves_are_declared_on_the_gate(self) -> None:
        assert "tasks" in set(security.paths._CREW_SECRET_LEAVES)


class TestSandboxDisposition:
    def test_tasks_is_hidden(self) -> None:
        assert "tasks" in set(sandbox._CREW_HIDDEN_LEAVES)
        for prefix in (".kiro/crew", ".kirocrew"):
            assert f"{prefix}/tasks" in sandbox._CREW_HIDDEN_DIRS

    @pytest.mark.skipif(os.name == "nt", reason="POSIX launcher only")
    @pytest.mark.parametrize("mode", ("standard", "strict"))
    def test_the_launcher_masks_the_new_dirs(self, mode: str) -> None:
        import json
        import re

        script = sandbox._build_launcher_script(mode)
        match = re.search(r"SENSITIVE_DIRS = (\[.*?\])\n", script, re.S)
        assert match
        masked = set(json.loads(match.group(1)))
        assert os.path.join(_CREW, "tasks") in masked


class TestScratchConfidentiality:
    """A session's private scratch never sits where another session's sandbox can read it."""

    def test_scratch_root_is_hidden_from_every_sandboxed_process(self) -> None:
        assert "scratch" in sandbox._CREW_HIDDEN_LEAVES
        for prefix in (".kiro/crew", ".kirocrew"):
            assert f"{prefix}/scratch" in sandbox._CREW_HIDDEN_DIRS

    @staticmethod
    def _readable(path: str, hidden: list[str], windows: list[str]) -> bool:
        """The launcher's mask semantics: a path under a hidden dir is absent
        unless it sits inside one of THIS spawn's private windows."""
        under = lambda p, d: p.startswith(d.rstrip("/") + "/")  # noqa: E731
        if any(under(path, w) for w in windows):
            return True
        return not any(under(path, d) for d in hidden)

    @pytest.mark.skipif(os.name == "nt", reason="POSIX launcher only")
    def test_a_second_session_view_cannot_read_the_first_session_log(self) -> None:
        import json
        import re

        root = os.path.join(_CREW, "scratch")
        a_dir, b_dir = os.path.join(root, "session-aaaa"), os.path.join(root, "session-bbbb")
        a_log = os.path.join(a_dir, "build.log")

        def view(own: str) -> tuple[list[str], list[str]]:
            script = sandbox._build_launcher_script("standard", extra_private_dirs=(own,))
            hidden = json.loads(re.search(r"SENSITIVE_DIRS = (\[.*?\])\n", script, re.S).group(1))
            windows = json.loads(re.search(r"PRIVATE_DIRS = (\[.*?\])\n", script, re.S).group(1))
            return hidden, windows

        a_hidden, a_windows = view(a_dir)
        b_hidden, b_windows = view(b_dir)
        assert root in a_hidden and a_windows == [a_dir]
        assert root in b_hidden and b_windows == [b_dir]
        assert self._readable(a_log, a_hidden, a_windows)  # the owner keeps its own file
        assert not self._readable(a_log, b_hidden, b_windows)  # a sibling session does not
        assert not self._readable(os.path.join(b_dir, "x"), a_hidden, a_windows)

    @pytest.mark.skipif(os.name == "nt", reason="Seatbelt profile only")
    def test_seatbelt_denies_the_tree_except_the_own_window(self) -> None:
        root = os.path.join(_CREW, "scratch")
        own = os.path.join(root, "session-aaaa")
        profile = sandbox._build_seatbelt_profile("standard", extra_private_dirs=(own,))
        rules = [line for line in profile.splitlines() if root in line]
        for op in ("file-read*", "file-write*", "file-link"):
            assert any(
                op in line
                and f'(subpath "{root}")' in line
                and f'(require-not (subpath "{own}"))' in line
                for line in rules
            ), op
        # Nothing re-allows a sibling; the root is re-opened for stat only, so
        # ``realpath`` of the own window resolves.
        allows = [line for line in rules if line.startswith("(allow")]
        assert allows == [f'(allow file-read-metadata (literal "{root}"))'], allows

    def test_the_spawn_sites_hand_their_scratch_back_as_a_private_window(self) -> None:
        import inspect

        from kiro_crew.acp import client, runtime

        for module in (client, runtime):
            src = inspect.getsource(module)
            assert "extra_private_dirs=scratch_window" in src, module.__name__


class TestPrivateWindowDoesNotCostDelegation:
    def test_windows_kiro_spawn_with_only_a_private_window_still_delegates(
        self, monkeypatch
    ) -> None:
        """Every session spawn now carries its scratch window. A window relaxes
        a mask owned by Kiro Crew the delegated sandbox never applies, so it must
        not be read as an extra path policy (which sends Windows to the
        fail-closed no-backend path)."""
        from unittest.mock import MagicMock, patch

        monkeypatch.setattr("kiro_crew.sandbox.sys.platform", "win32")
        launch = r"C:\Program Files\Kiro\kiro-cli.exe"
        with (
            patch("kiro_crew.sel.sel", return_value=MagicMock()),
            patch("kiro_crew.sandbox.detect_backend") as mock_detect,
        ):
            argv, cleanup = sandbox.wrap_argv(
                [launch, "acp"],
                mode="auto",
                strip_python_env=True,
                is_kiro_cli=True,
                extra_private_dirs=(r"C:\Users\me\.kiro\crew\scratch\session-aaaa",),
            )
        assert argv == [launch, "acp"]
        assert cleanup is None
        mock_detect.assert_not_called()


def _outside_any_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the routing as a gateway does: not already inside a Kiro Crew sandbox.

    ``wrap_argv`` passes argv through untouched -- no launcher, no profile, no
    backend probe -- when the process carries the launcher-only
    ``KIROCREW_SANDBOX_ACTIVE`` marker, because nested OS sandboxing is impossible
    by design. A test process spawned by a sandboxed agent inherits that marker,
    so the routing these tests pin would never be reached. The marker is the one
    seam that decision reads.
    """
    monkeypatch.delenv("KIROCREW_SANDBOX_ACTIVE", raising=False)


class TestThePrivateWindowSurvivesTheRoutingItDependsOn:
    """Two lines carry the scratch window onto a real Linux spawn, and both read
    ZERO tests when deleted.

    The tests above build the launcher script and the Seatbelt profile DIRECTLY,
    so they prove the window's CONTENT and never the route to it. Measured:
    dropping `or extra_private_dirs` from `wrap_argv`'s namespace-backend
    condition passes 1874 tests with `PRIVATE_DIRS` silently `[]` on the default
    Linux kiro spawn, and dropping the launcher's placeholder `os.makedirs` passes
    1047 with the window's bind then failing ENOENT and refusing the spawn.
    """

    @pytest.mark.skipif(os.name == "nt", reason="the namespace backend is Linux")
    def test_a_window_alone_still_routes_through_the_extra_paths_launcher(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A spawn whose ONLY extra path is its own scratch window must still get
        the launcher that carries it. The window is the one restriction that
        arrives on EVERY session spawn, so a condition that ignores it silently
        withdraws the fence from the default path rather than an unusual one."""
        import json
        import re
        from unittest.mock import MagicMock, patch

        _outside_any_sandbox(monkeypatch)
        own = os.path.join(_CREW, "scratch", "session-aaaa")
        with (
            patch("kiro_crew.sel.sel", return_value=MagicMock()),
            patch("kiro_crew.sandbox.detect_backend", return_value="namespace"),
            # A kiro-cli spawn on macOS is handed to kiro-cli's OWN sandbox when the
            # internal edition's settings ask for it, and then Kiro Crew writes no
            # launcher at all -- so on a machine carrying that setting this test
            # read `cleanup is None` and failed, while every CI runner passed. The
            # decision is the host's, not this test's subject, so it is pinned:
            # ``is_kiro_cli=True`` is what reaches it, which is why the sibling
            # seatbelt case (``is_kiro_cli=False``) never saw it.
            patch("kiro_crew.sandbox.kiro_internal_sandbox_enabled", return_value=False),
        ):
            argv, cleanup = sandbox.wrap_argv(
                ["/usr/bin/kiro-cli", "acp"],
                mode="standard",
                strip_python_env=True,
                is_kiro_cli=True,
                extra_private_dirs=(own,),
            )
        try:
            assert cleanup is not None, "no launcher was written, so no window can be carried"
            script = open(cleanup, encoding="utf-8").read()
            windows = json.loads(re.search(r"PRIVATE_DIRS = (\[.*?\])\n", script, re.S).group(1))
            assert windows == [own], f"the window did not reach the launcher: {windows}"
            hidden = json.loads(re.search(r"SENSITIVE_DIRS = (\[.*?\])\n", script, re.S).group(1))
            assert os.path.join(_CREW, "scratch") in hidden, "the root is not masked at all"
        finally:
            if cleanup is not None:
                os.unlink(cleanup)

    def test_a_window_alone_still_reaches_the_seatbelt_profile(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The macOS half of the same routing, so the fix is not one-of-two. The
        `sandbox-exec` branch has the identical condition, and its else-arm calls
        `sandbox_exec_argv` WITHOUT `extra_private_dirs` -- so dropping the window
        from the test there loses it just as silently, on the platform where the
        profile is the only fence."""
        from unittest.mock import MagicMock, patch

        _outside_any_sandbox(monkeypatch)
        own = os.path.join(_CREW, "scratch", "session-aaaa")
        seen: dict[str, object] = {}

        def _spy(argv, level, **kw):
            seen.update(kw)
            return list(argv), None

        with (
            patch("kiro_crew.sel.sel", return_value=MagicMock()),
            patch("kiro_crew.sandbox.detect_backend", return_value="sandbox-exec"),
            patch("kiro_crew.sandbox.sandbox_exec_argv", side_effect=_spy),
        ):
            sandbox.wrap_argv(
                ["/usr/bin/kiro-cli", "acp"],
                mode="standard",
                strip_python_env=True,
                is_kiro_cli=False,
                extra_private_dirs=(own,),
            )
        assert seen.get("extra_private_dirs") == (own,), (
            "the window never reached sandbox_exec_argv, so the Seatbelt profile "
            f"denies the whole scratch root with no window: {seen!r}"
        )

    @pytest.mark.skipif(os.name == "nt", reason="POSIX launcher only")
    def test_the_window_placeholder_is_created_before_its_parent_is_masked(self) -> None:
        """The window binds onto a path INSIDE the parent's empty stand-in, so the
        stand-in has to carry that path before the parent is masked. Without the
        placeholder the bind gets ENOENT and `_mount_or_die` refuses the spawn --
        fail-closed, but a fence that refuses every session is not the fence this
        is. Ordering is the assertion, because the placeholder is only correct
        where it is."""
        script = sandbox._build_launcher_script(
            "standard", extra_private_dirs=(os.path.join(_CREW, "scratch", "session-aaaa"),)
        )
        makedirs = script.index("os.makedirs(os.path.join(per_dir_empty.decode()")
        mask = script.index('"hiding credential directory %s"')
        window = script.index('"opening private window %s"')
        assert makedirs < mask < window, (
            "the placeholder must be staged BEFORE the parent's mask (the mask "
            "shadows the real path) and the window bound AFTER it"
        )
