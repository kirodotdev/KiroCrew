"""``HostBudget``: every backend the daemon is answerable for is charged before
it exists and released only once it is gone.

Pure counters on one event loop, so these are plain unit tests: no process, no
socket, no clock. What they pin is the contract the spawn path leans on --
reserve-before-spawn raises with nothing to reap, release is idempotent from
any path, ``release_all`` is the drain, and the automatic ceilings never sit
below the resident pool the operator already sized.
"""

from __future__ import annotations

import pytest

from kiro_crew.mcp_gateway import host_budget as hb


def _budget(**limits: int) -> hb.HostBudget:
    return hb.HostBudget(hb.HostBudgetLimits(**limits), rss_mb_per_backend=100, fds_per_backend=3)


class TestReserveAndRelease:
    def test_reserve_charges_every_dimension_with_the_backend_estimate(self) -> None:
        budget = _budget(max_procs=4, max_rss_mb=1000, max_fds=30)
        charge = budget.reserve(label="a", kind="pooled")
        assert (budget.procs_in_use, budget.rss_mb_in_use, budget.fds_in_use) == (1, 100, 3)
        assert charge.kind == "pooled" and not charge.released
        snap = budget.snapshot()
        assert snap["charges"] == 1 and snap["by_kind"] == {"pooled": 1}

    def test_release_is_idempotent_and_reaches_zero(self) -> None:
        budget = _budget(max_procs=2)
        a = budget.reserve(label="a")
        b = budget.reserve(label="b", kind="exclusive")
        a.release()
        a.release()
        assert budget.procs_in_use == 1 and a.released
        b.release()
        assert budget.procs_in_use == 0 and budget.snapshot()["by_kind"] == {}

    def test_pooled_exclusive_and_fallback_are_charged_identically(self) -> None:
        budget = _budget(max_procs=3)
        for kind in ("pooled", "exclusive", "fallback"):
            budget.reserve(label=kind, kind=kind)
        assert budget.procs_in_use == 3
        with pytest.raises(hb.HostBudgetExhausted):
            budget.reserve(label="one-more", kind="prewarm")

    def test_zero_ceiling_means_unbounded_in_that_dimension(self) -> None:
        budget = _budget(max_procs=0, max_rss_mb=0, max_fds=0)
        for i in range(50):
            budget.reserve(label=str(i))
        assert budget.procs_in_use == 50 and budget.snapshot()["rejections"] == 0

    def test_release_all_is_the_drain(self) -> None:
        budget = _budget(max_procs=5)
        charges = [budget.reserve(label=str(i)) for i in range(3)]
        assert budget.release_all() == 3
        assert budget.procs_in_use == 0 and all(c.released for c in charges)
        assert budget.release_all() == 0


class TestExhaustion:
    @pytest.mark.parametrize(
        "limits,dimension",
        [
            ({"max_procs": 1}, "procs"),
            ({"max_rss_mb": 150}, "rss_mb"),
            ({"max_fds": 5}, "fds"),
        ],
    )
    def test_the_dimension_that_overflows_is_named_and_nothing_is_charged(
        self, limits: dict[str, int], dimension: str
    ) -> None:
        budget = _budget(**limits)
        budget.reserve(label="first")
        before = budget.snapshot()
        with pytest.raises(hb.HostBudgetExhausted) as excinfo:
            budget.reserve(label="second")
        assert excinfo.value.dimension == dimension
        after = budget.snapshot()
        assert (after["procs"], after["rss_mb"], after["fds"]) == (
            before["procs"],
            before["rss_mb"],
            before["fds"],
        ), "a refused reservation must leave the counters untouched"
        assert after["rejections"] == before["rejections"] + 1

    def test_a_release_frees_room_for_the_next_reservation(self) -> None:
        budget = _budget(max_procs=1)
        first = budget.reserve(label="first")
        with pytest.raises(hb.HostBudgetExhausted):
            budget.reserve(label="second")
        first.release()
        budget.reserve(label="second")
        assert budget.procs_in_use == 1

    def test_limits_reject_negative_values(self) -> None:
        with pytest.raises(ValueError):
            hb.HostBudgetLimits(max_procs=-1)


class TestResolveLimits:
    def test_explicit_ceilings_pass_through(self) -> None:
        limits = hb.resolve_limits(
            max_procs=7, max_rss_mb=900, max_fds=42, available_mb=100000.0, max_backends=64
        )
        assert (limits.max_procs, limits.max_rss_mb, limits.max_fds) == (7, 900, 42)

    def test_auto_procs_derive_from_memory_but_never_below_the_pool(self) -> None:
        # 3000 MiB / 150 MiB per backend = 20 processes, below a 64-slot pool:
        # the budget must not refuse a spawn the pool would admit.
        limits = hb.resolve_limits(
            max_procs=0, max_rss_mb=0, max_fds=0, available_mb=3000.0, max_backends=64
        )
        assert limits.max_procs == 64
        # 30000 MiB gives 200, above the pool: memory wins.
        limits = hb.resolve_limits(
            max_procs=0, max_rss_mb=0, max_fds=0, available_mb=30000.0, max_backends=64
        )
        assert limits.max_procs == 200

    def test_auto_procs_without_a_memory_sample_use_the_floors(self) -> None:
        limits = hb.resolve_limits(
            max_procs=0, max_rss_mb=0, max_fds=0, available_mb=None, max_backends=4
        )
        assert limits.max_procs == hb._AUTO_PROCS_FLOOR
        assert limits.max_rss_mb == 0, "auto memory stays unbounded: procs already bound it"

    def test_auto_fds_take_a_share_of_the_soft_limit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(hb, "_nofile_soft_limit", lambda: 1000)
        limits = hb.resolve_limits(
            max_procs=0, max_rss_mb=0, max_fds=0, available_mb=None, max_backends=4
        )
        assert limits.max_fds == 600
        monkeypatch.setattr(hb, "_nofile_soft_limit", lambda: 0)
        limits = hb.resolve_limits(
            max_procs=0, max_rss_mb=0, max_fds=0, available_mb=None, max_backends=4
        )
        assert limits.max_fds == 0, "no readable limit leaves descriptors unbounded"
