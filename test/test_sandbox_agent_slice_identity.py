"""Agent scopes must name the instance that owns them.

Every gateway on a host placed its agent scopes in ONE slice whose name was a
module constant, so a scope's identity was "the agents slice, at time T" and
neither component named an owner. Anything reasoning about scopes as a
population therefore could not separate its own from a co-resident gateway's --
and a host routinely runs several at once, because ``kirocrew pod up`` creates
one per pod by design. These tests pin the owner being expressible, and pin the
two properties that made the shared constant safe (aggregate ceiling, cgroup
path matching) surviving the change.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest


def _slice_arg(argv: list[str]) -> str:
    """The ``--slice=`` value in a systemd-run argv."""
    for a in argv:
        if a.startswith("--slice="):
            return a.split("=", 1)[1]
    raise AssertionError(f"no --slice= in {argv!r}")


@pytest.fixture
def wrapped():
    """Call ``cgroup_scope_argv`` with the cgroup backend forced available.

    ``trusted_system_bin`` is stubbed so the assertions do not depend on the
    test host having systemd-run in a trusted directory, and the off-thread
    slice reconciliation is stubbed so no thread or subprocess is started.
    """
    import kiro_crew.sandbox as sb

    def _call(argv=None):
        sb._CGROUP_SCOPE_PROBE = None
        sb._CGROUP_WARNED = False
        try:
            with (
                patch("kiro_crew.sandbox._probe_cgroup_scope", return_value=(True, "ok")),
                patch(
                    "kiro_crew.platform_compat.trusted_system_bin",
                    return_value="/usr/bin/systemd-run",
                ),
                patch("kiro_crew.sandbox._reconcile_slice_memory_high_off_thread"),
                patch(
                    "kiro_crew.sandbox._cgroup_limits_from_config",
                    return_value=(8192, 8192, 50, 0),
                ),
                patch("kiro_crew.sandbox._cpu_controller_delegated", return_value=False),
            ):
                return sb.cgroup_scope_argv(list(argv or ["kiro-cli", "chat"]))
        finally:
            sb._CGROUP_SCOPE_PROBE = None
            sb._CGROUP_WARNED = False

    return _call


class TestAgentSliceIdentity:
    def test_two_instances_do_not_share_a_slice(self, wrapped, monkeypatch, tmp_path):
        """The load-bearing property: co-resident gateways get distinct slices.

        Without this, "every scope in the agents slice" reads as "my scopes" to
        any scope-level sweep, and the sweep reaches into a live session that
        belongs to another gateway on the same host.
        """
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "instance-a"))
        slice_a = _slice_arg(wrapped())
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "instance-b"))
        slice_b = _slice_arg(wrapped())

        assert slice_a != slice_b, (
            "two data homes produced the same slice, so a scope carries no " f"owner: {slice_a}"
        )

    def test_slice_is_stable_for_one_instance(self, wrapped, monkeypatch, tmp_path):
        """Recomputable: an owner check is only possible if the name is stable."""
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "instance-a"))
        assert _slice_arg(wrapped()) == _slice_arg(wrapped(["git", "status"]))

    def test_slice_nests_under_the_aggregate_parent(self, wrapped, monkeypatch, tmp_path):
        """The parent stays the aggregate enforcement boundary.

        systemd's dash-hierarchy makes ``kirocrew-agents-<tok>.slice`` a child
        of ``kirocrew-agents.slice``, so the MemoryHigh / MemoryMax applied to
        the parent keep bounding every instance's scopes (cgroup v2 bounds a
        descendant by the minimum effective limit along its ancestor chain), and
        the parent remains a path component of every scope's cgroup -- which is
        what callers matching the parent name in ``/proc/self/cgroup`` rely on.
        """
        import kiro_crew.sandbox as sb

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "instance-a"))
        name = _slice_arg(wrapped())
        parent_stem = sb._CGROUP_AGENTS_SLICE[: -len(".slice")]

        assert name.endswith(".slice")
        assert name.startswith(f"{parent_stem}-"), (
            f"{name} is not a dash-child of {sb._CGROUP_AGENTS_SLICE}, so it "
            "does not inherit the aggregate ceiling"
        )

    def test_instance_token_carries_no_dash(self, monkeypatch, tmp_path):
        """A dash in the token would silently add a systemd hierarchy level.

        systemd reads each dash as a separator, so ``...-ab-cd.slice`` would
        nest under ``...-ab.slice`` -- an extra level, and a prefix two tokens
        could share.
        """
        import kiro_crew.sandbox as sb

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "instance-a"))
        token = sb._instance_slice_token()

        assert token
        assert "-" not in token
        assert token.isalnum()

    def test_unresolvable_home_degrades_to_the_shared_slice(self, wrapped, caplog):
        """A missing owner must not fail the spawn.

        The scope still gets its ceilings; only the ownership component is lost,
        and the warning says so. Matches how an unavailable cgroup backend is
        handled rather than raising on the spawn path.
        """
        import logging

        import kiro_crew.sandbox as sb

        with patch(
            "kiro_crew.sandbox._instance_slice_token",
            side_effect=RuntimeError("no home"),
        ):
            with caplog.at_level(logging.WARNING):
                argv = wrapped()

        assert _slice_arg(argv) == sb._CGROUP_AGENTS_SLICE
        assert argv[argv.index("--") + 1 :] == ["kiro-cli", "chat"]
        assert "TasksMax=8192" in argv
        assert any("co-resident" in r.getMessage() for r in caplog.records)


class TestSlicePressureScanFindsNestedScopes:
    """The OOM-victim scan has to follow the scopes one level down.

    ``check_agents_slice_pressure`` names the scopes that recorded a kill. With
    scopes now under a per-instance child slice, a scan of direct children only
    would find none and report "(already reaped)" for every real victim.
    """

    def _slice_tree(self, tmp_path, *, nested: bool):
        """Build a fake agents-slice cgroup dir with one OOM-killed scope."""
        root = tmp_path / "kirocrew-agents.slice"
        holder = root / "kirocrew-agents-abc123.slice" if nested else root
        scope = holder / "run-rdeadbeef.scope"
        scope.mkdir(parents=True)
        (scope / "memory.events.local").write_text("max 3\noom_kill 2\n")
        (root / "memory.events").write_text("max 9\noom_kill 4\n")
        (root / "memory.events.local").write_text("max 9\noom_kill 0\n")
        (root / "memory.current").write_text("123\n")
        (root / "memory.max").write_text("456\n")
        return root, scope.name

    @pytest.mark.parametrize("nested", [True, False])
    def test_victim_scope_is_named(self, tmp_path, nested):
        """Both shapes are found.

        Nested is this instance's own layout. Flat still matters because the
        aggregate slice is shared host-wide: a co-resident gateway on an older
        build keeps putting scopes directly in it, and its kills are still
        worth naming.
        """
        import kiro_crew.sandbox as sb

        root, scope_name = self._slice_tree(tmp_path, nested=nested)
        prev = sb._SLICE_OOM_SEEN
        try:
            # Baseline below the tree's counters, so the scan sees new kills.
            sb._SLICE_OOM_SEEN = {"oom_kill": 0, "max": 0}
            with (
                patch("kiro_crew.sandbox._agents_slice_cgroup_dir", return_value=root),
                patch("kiro_crew.sandbox._SLICE_LIMITS_APPLIED", False),
            ):
                message = sb.check_agents_slice_pressure()
        finally:
            sb._SLICE_OOM_SEEN = prev

        assert message is not None
        assert scope_name in message
        assert "already reaped" not in message
