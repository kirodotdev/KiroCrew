"""``resource_limits.slice_memory_high_pct``: the agents slice ``MemoryHigh`` knob.

Each test drives the real ``_ensure_agent_slice_memory_high`` against a real
``config.json`` in a temporary data home. Only the ``systemctl`` call is faked
(``subprocess.run``, whose argv is captured, and the trusted-binary lookup), and
``os.sysconf`` stands in for the host's RAM so the expected numbers are exact.
Nothing here reaches the live user manager.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import kiro_crew.sandbox as sb
from kiro_crew.config.loader import _WARNED_RESOURCE_LIMIT_KEYS, ResourceLimitsConfig

_MIB = 1024 * 1024
_GIB = 1024 * _MIB
_PAGE = 4096


def _ram(total_bytes: int):
    """An ``os.sysconf`` stand-in reporting *total_bytes* of physical RAM."""

    def fake(name: str) -> int:
        return total_bytes // _PAGE if "PHYS" in name else _PAGE

    return fake


def _pct_mb(total_bytes: int, pct: int) -> int:
    return (total_bytes * pct // 100) // _MIB


# What the faked ``systemctl --user show -p MemoryHigh`` answers: a property
# value, or "fail" (non-zero exit) / "raise" (the call itself errors).
_UNIT = {"show": "infinity"}


def _unit(show: str) -> None:
    """Set what the fake ``systemctl show`` answers for MemoryHigh."""
    _UNIT["show"] = show


def _fake_systemctl(argv: list[str], **_kw: object) -> MagicMock:
    if "show" in argv:
        mode = _UNIT["show"]
        if mode == "raise":
            raise OSError("bus unreachable")
        if mode == "fail":
            return MagicMock(returncode=1, stderr="Failed", stdout="")
        return MagicMock(returncode=0, stderr="", stdout=f"MemoryHigh={mode}\n")
    return MagicMock(returncode=0, stderr="", stdout="")


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fresh data home, the reconciler re-armed, and no pod marker."""
    d = tmp_path / "home"
    d.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(d))
    monkeypatch.delenv("KIROCREW_POD", raising=False)
    monkeypatch.setattr(sb, "_SLICE_MEMHIGH_DISABLED", False)
    monkeypatch.setattr(sb, "_SLICE_MEMHIGH_APPLIED", None)
    monkeypatch.setattr(sb, "_SLICE_MEMHIGH_DESIRED", None)
    monkeypatch.setattr(sb, "_SLICE_MEMHIGH_CONFIGURED", False)
    monkeypatch.setattr(sb, "_SLICE_MEMHIGH_DEFERRED", False)
    monkeypatch.setattr(sb, "_SLICE_UNIT_PROPS", None)
    # The unit reports no MemoryHigh, and there is no slice cgroup dir at all
    # (a fresh login): the decision must not depend on the cgroup file.
    _unit("infinity")
    monkeypatch.setattr(sb, "_agents_slice_cgroup_dir", lambda: None)
    _WARNED_RESOURCE_LIMIT_KEYS.clear()
    yield d
    _WARNED_RESOURCE_LIMIT_KEYS.clear()


def _write_limits(home: Path, limits: dict | None) -> None:
    body = {} if limits is None else {"resource_limits": limits}
    (home / "config.json").write_text(json.dumps(body), encoding="utf-8")


def _apply(ram=_ram(16 * _GIB)) -> list[list[str]]:
    """Run the real reconcile once; return the argv of every set-property call."""
    with (
        patch("os.sysconf", side_effect=ram, create=True),
        patch(
            "kiro_crew.sandbox.platform_compat.trusted_system_bin",
            return_value="/usr/bin/systemctl",
        ),
        patch("kiro_crew.sandbox.subprocess.run", side_effect=_fake_systemctl) as run,
    ):
        sb._ensure_agent_slice_memory_high()
    return [c.args[0] for c in run.call_args_list if "set-property" in c.args[0]]


def _memory_high(calls: list[list[str]]) -> str:
    assert len(calls) == 1, calls
    argv = calls[0]
    assert argv[:5] == [
        "/usr/bin/systemctl",
        "--user",
        "set-property",
        "--runtime",
        "kirocrew-agents.slice",
    ]
    assert len(argv) == 6
    return argv[5]


def _rearm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Forget the resolved value, as a fresh gateway process would."""
    monkeypatch.setattr(sb, "_SLICE_MEMHIGH_APPLIED", None)
    monkeypatch.setattr(sb, "_SLICE_MEMHIGH_DESIRED", None)
    monkeypatch.setattr(sb, "_SLICE_MEMHIGH_CONFIGURED", False)
    monkeypatch.setattr(sb, "_SLICE_MEMHIGH_DEFERRED", False)
    monkeypatch.setattr(sb, "_SLICE_UNIT_PROPS", None)


SIXTEEN = 16 * _GIB
DEFAULT_MB = _pct_mb(SIXTEEN, 75)  # 12288
MAX_TOTAL_DEFAULT_MB = _pct_mb(SIXTEEN, 80)  # 13107
CAP_DEFAULT_MB = MAX_TOTAL_DEFAULT_MB * 95 // 100  # 12451, below MemoryMax


def test_unset_is_todays_75_percent(home: Path) -> None:
    _write_limits(home, {})
    assert _memory_high(_apply()) == f"MemoryHigh={DEFAULT_MB}M"


def test_a_pct_in_the_local_overlay_is_honoured(home: Path) -> None:
    """`kirocrew config set --local` writes config.local.json, whose keys win
    over config.json."""
    _write_limits(home, {"slice_memory_high_pct": 90})
    (home / "config.local.json").write_text(
        json.dumps({"resource_limits": {"slice_memory_high_pct": 60}}), encoding="utf-8"
    )
    assert _memory_high(_apply()) == f"MemoryHigh={_pct_mb(SIXTEEN, 60)}M"


def test_a_pct_only_in_the_local_overlay_is_honoured(home: Path) -> None:
    (home / "config.local.json").write_text(
        json.dumps({"resource_limits": {"slice_memory_high_pct": 55}}), encoding="utf-8"
    )
    assert _memory_high(_apply()) == f"MemoryHigh={_pct_mb(SIXTEEN, 55)}M"


def test_a_configured_pct_reaches_the_systemctl_argv(home: Path) -> None:
    _write_limits(home, {"slice_memory_high_pct": 60})
    assert _memory_high(_apply()) == f"MemoryHigh={_pct_mb(SIXTEEN, 60)}M"


@pytest.mark.parametrize("pct", [50, 70, 76])
def test_each_in_range_value_is_honoured(home: Path, pct: int) -> None:
    _write_limits(home, {"slice_memory_high_pct": pct})
    assert _memory_high(_apply()) == f"MemoryHigh={_pct_mb(SIXTEEN, pct)}M"


@pytest.mark.parametrize("junk", [49, 96, 0, -75, 100, "80", True, None, [80], 0.5])
def test_junk_or_out_of_range_uses_the_default(
    home: Path, junk: object, caplog: pytest.LogCaptureFixture
) -> None:
    _write_limits(home, {"slice_memory_high_pct": junk})
    with caplog.at_level(logging.WARNING):
        assert _memory_high(_apply()) == f"MemoryHigh={DEFAULT_MB}M"
    warned = [r for r in caplog.records if "slice_memory_high_pct" in r.getMessage()]
    # null is "unset", not junk, so it alone stays silent.
    assert len(warned) == (0 if junk is None else 1)


def test_a_non_finite_value_uses_the_default(home: Path) -> None:
    (home / "config.json").write_text(
        '{"resource_limits": {"slice_memory_high_pct": NaN}}', encoding="utf-8"
    )
    assert _memory_high(_apply()) == f"MemoryHigh={DEFAULT_MB}M"


def test_a_pct_above_the_aggregate_memory_max_is_capped_below_it(
    home: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """95% of RAM is above the slice's default 80% MemoryMax. The value lands
    at 95% of MemoryMax, strictly below it, so a throttle window remains. The
    value is resolved once per process, so later reconciles log nothing more."""
    _write_limits(home, {"slice_memory_high_pct": 95})
    with caplog.at_level(logging.INFO, logger="kiro_crew.sandbox"):
        assert _memory_high(_apply()) == f"MemoryHigh={CAP_DEFAULT_MB}M"
        assert _apply() == []  # steady state: no re-resolve, no second line
    assert CAP_DEFAULT_MB < MAX_TOTAL_DEFAULT_MB
    clamped = [r for r in caplog.records if "is above its cap" in r.getMessage()]
    assert len(clamped) == 1
    assert clamped[0].levelno == logging.WARNING


def test_a_pct_just_under_the_cap_is_not_touched(home: Path) -> None:
    # 76% of 16 GiB = 12451.84 MB, floor 12451 = the cap: not above it.
    _write_limits(home, {"slice_memory_high_pct": 76})
    assert _memory_high(_apply()) == f"MemoryHigh={_pct_mb(SIXTEEN, 76)}M"


def test_a_low_max_total_memory_caps_the_default_below_it(
    home: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Unset pct gets the same cap: a memory.high at or above memory.max never
    throttles. With the default pct the line is informational."""
    _write_limits(home, {"max_total_memory_mb": 4096})
    with caplog.at_level(logging.INFO, logger="kiro_crew.sandbox"):
        assert _memory_high(_apply()) == f"MemoryHigh={4096 * 95 // 100}M"
    clamped = [r for r in caplog.records if "is above its cap" in r.getMessage()]
    assert [r.levelno for r in clamped] == [logging.INFO]


def test_an_explicit_75_is_a_configured_value_and_gets_the_cap(home: Path) -> None:
    """Writing the default number is still a choice made in config, so it gets
    the headroom cap that unset does not."""
    _write_limits(home, {"slice_memory_high_pct": 75, "max_total_memory_mb": 4096})
    assert _memory_high(_apply()) == f"MemoryHigh={4096 * 95 // 100}M"


def test_an_explicit_75_on_a_default_host_matches_unset(home: Path) -> None:
    _write_limits(home, {"slice_memory_high_pct": 75})
    assert _memory_high(_apply()) == f"MemoryHigh={DEFAULT_MB}M"


def test_a_low_max_total_memory_caps_a_configured_pct_below_it(home: Path) -> None:
    _write_limits(home, {"slice_memory_high_pct": 60, "max_total_memory_mb": 4096})
    assert _memory_high(_apply()) == f"MemoryHigh={4096 * 95 // 100}M"


@pytest.mark.parametrize("limits", [{}, {"slice_memory_high_pct": 60, "max_total_memory_mb": 4096}])
def test_a_pod_writes_the_default_when_the_unit_has_no_memory_high(
    home: Path, monkeypatch: pytest.MonkeyPatch, limits: dict
) -> None:
    """Nobody has set a ceiling: a pod applies today's default, key ignored."""
    monkeypatch.setenv("KIROCREW_POD", "1")
    _write_limits(home, limits)
    assert _memory_high(_apply()) == f"MemoryHigh={DEFAULT_MB}M"


@pytest.mark.parametrize("show", ["fail", "raise"])
@pytest.mark.parametrize("limits", [{}, {"slice_memory_high_pct": 60}])
def test_a_pod_writes_nothing_when_the_unit_cannot_be_read(
    home: Path, monkeypatch: pytest.MonkeyPatch, limits: dict, show: str
) -> None:
    monkeypatch.setenv("KIROCREW_POD", "1")
    _unit(show)
    _write_limits(home, limits)
    assert _apply() == []


@pytest.mark.parametrize("limits", [{}, {"slice_memory_high_pct": 90}])
def test_a_pod_never_overwrites_a_ceiling_already_on_the_slice(
    home: Path, monkeypatch: pytest.MonkeyPatch, limits: dict
) -> None:
    """The live gateway applied 60%; a pod starting an agent must leave it,
    whether or not its own config sets the key."""
    monkeypatch.setenv("KIROCREW_POD", "1")
    _unit(str(_pct_mb(SIXTEEN, 60) * _MIB))
    _write_limits(home, limits)
    assert _apply() == []
    assert _apply() == []
    assert sb._SLICE_MEMHIGH_DISABLED is False
    assert sb._SLICE_MEMHIGH_APPLIED is None


def test_an_unset_gateway_leaves_an_existing_finite_value_alone(home: Path) -> None:
    """The live gateway applied 60%; a second non-pod gateway (a dev backend)
    with the key unset must not replace it with the default."""
    _unit(str(_pct_mb(SIXTEEN, 60) * _MIB))
    _write_limits(home, {})
    assert _apply() == []
    assert _apply() == []
    assert sb._SLICE_MEMHIGH_APPLIED is None


@pytest.mark.parametrize(
    ("held_mb", "writes"),
    [
        (CAP_DEFAULT_MB, False),
        (MAX_TOTAL_DEFAULT_MB - 1, False),
        (MAX_TOTAL_DEFAULT_MB, True),
        (_pct_mb(SIXTEEN, 95), True),
    ],
)
def test_an_unset_gateway_replaces_a_held_value_only_at_or_above_memory_max(
    home: Path, held_mb: int, writes: bool
) -> None:
    """Below MemoryMax a held value still throttles and is kept. An earlier run
    with pct 95 and a raised max_total_memory_mb can leave MemoryHigh at 95% of
    RAM, above the default MemoryMax, where it never throttles: replaced."""
    _unit(str(held_mb * _MIB))
    _write_limits(home, {})
    if writes:
        assert _memory_high(_apply()) == f"MemoryHigh={DEFAULT_MB}M"
    else:
        assert _apply() == []


def test_a_held_value_above_a_lowered_memory_max_is_replaced_with_the_cap(home: Path) -> None:
    """The held 75% default is above a now-lower MemoryMax."""
    _unit(str(DEFAULT_MB * _MIB))
    _write_limits(home, {"max_total_memory_mb": 4096})
    assert _memory_high(_apply()) == f"MemoryHigh={4096 * 95 // 100}M"


def _apply_counting(n: int) -> tuple[int, int]:
    """Run *n* reconciles; return (show calls, set-property calls)."""
    shows = sets = 0
    with (
        patch("os.sysconf", side_effect=_ram(SIXTEEN), create=True),
        patch(
            "kiro_crew.sandbox.platform_compat.trusted_system_bin",
            return_value="/usr/bin/systemctl",
        ),
        patch("kiro_crew.sandbox.subprocess.run", side_effect=_fake_systemctl) as run,
    ):
        for _ in range(n):
            sb._ensure_agent_slice_memory_high()
    for c in run.call_args_list:
        shows += "show" in c.args[0]
        sets += "set-property" in c.args[0]
    return shows, sets


def test_a_configured_pct_95_cap_is_kept_by_unset_gateways_and_pods(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The live gateway at pct 95 writes the cap (95% of MemoryMax, below it);
    a later unset gateway and a pod each leave it alone."""
    _write_limits(home, {"slice_memory_high_pct": 95})
    assert _memory_high(_apply()) == f"MemoryHigh={CAP_DEFAULT_MB}M"
    _unit(str(CAP_DEFAULT_MB * _MIB))
    _write_limits(home, {})
    _rearm(monkeypatch)
    assert _apply() == []
    monkeypatch.setenv("KIROCREW_POD", "1")
    _rearm(monkeypatch)
    assert _apply() == []


@pytest.mark.parametrize("show", ["fail", "raise"])
def test_a_failed_show_falls_back_to_the_config_cap(home: Path, show: str) -> None:
    _unit(show)
    _write_limits(home, {"slice_memory_high_pct": 90})
    assert _memory_high(_apply()) == f"MemoryHigh={CAP_DEFAULT_MB}M"


def test_a_configured_gateway_never_asks_systemctl_show(home: Path) -> None:
    _write_limits(home, {"slice_memory_high_pct": 60})
    shows, sets = _apply_counting(20)
    assert (shows, sets) == (0, 1)


@pytest.mark.parametrize("show", ["fail", "raise"])
def test_a_failed_show_is_asked_once_by_an_unset_gateway(home: Path, show: str) -> None:
    _unit(show)
    _write_limits(home, {})
    shows, sets = _apply_counting(5)
    assert (shows, sets) == (1, 1)


def test_a_replacement_is_written_once(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The replacement becomes this process's value, so the next spawns
    compare equal to it and do not write again."""
    monkeypatch.setenv("KIROCREW_POD", "1")
    _unit(str(DEFAULT_MB * _MIB))
    _write_limits(home, {"max_total_memory_mb": 4096})
    _, sets = _apply_counting(10)
    assert sets == 1


def test_a_deferring_instance_asks_systemctl_once(home: Path) -> None:
    _unit(str((CAP_DEFAULT_MB - 1) * _MIB))
    _write_limits(home, {})
    shows, sets = _apply_counting(20)
    assert (shows, sets) == (1, 0)


def test_a_held_value_that_is_not_a_byte_count_is_left_alone(home: Path) -> None:
    _unit("12G")
    _write_limits(home, {})
    assert _apply() == []


def test_a_pod_replaces_a_held_value_above_the_cap(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KIROCREW_POD", "1")
    _unit(str(_pct_mb(SIXTEEN, 95) * _MIB))
    _write_limits(home, {"slice_memory_high_pct": 60})
    assert _memory_high(_apply()) == f"MemoryHigh={DEFAULT_MB}M"


@pytest.mark.parametrize("show", ["infinity", ""])
def test_an_unset_gateway_writes_the_default_with_no_slice_cgroup_dir(
    home: Path, show: str
) -> None:
    """A fresh login: no slice cgroup yet (the fixture's lookup answers None),
    the unit has no MemoryHigh, so the default lands on the first spawn."""
    _unit(show)
    _write_limits(home, {})
    assert _memory_high(_apply()) == f"MemoryHigh={DEFAULT_MB}M"


@pytest.mark.parametrize("show", ["fail", "raise"])
def test_an_unset_gateway_writes_the_default_when_the_unit_cannot_be_read(
    home: Path, show: str
) -> None:
    _unit(show)
    _write_limits(home, {})
    assert _memory_high(_apply()) == f"MemoryHigh={DEFAULT_MB}M"


def test_a_deferring_gateway_fills_the_slot_after_a_restart(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deferring holds for the process; a restarted gateway reads the unit
    again and fills a slot that is now unset."""
    _unit(str(_pct_mb(SIXTEEN, 60) * _MIB))
    _write_limits(home, {})
    assert _apply() == []
    _unit("infinity")  # e.g. after a new login session
    assert _apply() == []  # still deferring until restart
    _rearm(monkeypatch)
    assert _memory_high(_apply()) == f"MemoryHigh={DEFAULT_MB}M"


@pytest.mark.parametrize("pct", [60, 75, 90])
def test_an_explicit_key_overwrites_an_existing_finite_value(home: Path, pct: int) -> None:
    """A set key always writes, an explicit 75 included: two configured
    instances are last-writer-wins."""
    _unit(str(_pct_mb(SIXTEEN, 50) * _MIB))
    _write_limits(home, {"slice_memory_high_pct": pct})
    expected = min(_pct_mb(SIXTEEN, pct), CAP_DEFAULT_MB)
    assert _memory_high(_apply()) == f"MemoryHigh={expected}M"


def test_unreadable_ram_uses_the_fallback_mb(home: Path) -> None:
    def no_sysconf(name: str) -> int:
        raise OSError("no sysconf")

    _write_limits(home, {"slice_memory_high_pct": 60})
    # Both the configured pct and the aggregate MemoryMax fall back to 12288 MB,
    # so the configured value lands on the cap below that MemoryMax.
    assert (
        _memory_high(_apply(no_sysconf))
        == f"MemoryHigh={sb._SLICE_FALLBACK_MEMORY_HIGH_MB * 95 // 100}M"
    )


def test_unreadable_ram_with_the_default_pct_uses_the_fallback_mb(home: Path) -> None:
    def no_sysconf(name: str) -> int:
        raise OSError("no sysconf")

    _write_limits(home, {})
    # The 12288 MB fallback equals the aggregate fallback, so the cap applies.
    assert (
        _memory_high(_apply(no_sysconf))
        == f"MemoryHigh={sb._SLICE_FALLBACK_MEMORY_HIGH_MB * 95 // 100}M"
    )


def test_a_tiny_host_scales_down(home: Path) -> None:
    one_g = 1 * _GIB
    _write_limits(home, {"slice_memory_high_pct": 60})
    assert _memory_high(_apply(_ram(one_g))) == f"MemoryHigh={_pct_mb(one_g, 60)}M"


def test_a_missing_config_file_uses_the_default(home: Path) -> None:
    assert not (home / "config.json").exists()
    assert _memory_high(_apply()) == f"MemoryHigh={DEFAULT_MB}M"


@pytest.mark.parametrize("text", ["{not json", "[]", '{"resource_limits": "x"}'])
def test_an_unparseable_config_uses_the_default(home: Path, text: str) -> None:
    (home / "config.json").write_text(text, encoding="utf-8")
    assert _memory_high(_apply()) == f"MemoryHigh={DEFAULT_MB}M"


def test_the_value_is_process_constant_until_restart(home: Path) -> None:
    _write_limits(home, {"slice_memory_high_pct": 60})
    assert _memory_high(_apply()) == f"MemoryHigh={_pct_mb(SIXTEEN, 60)}M"
    _write_limits(home, {"slice_memory_high_pct": 90})
    assert _apply() == []  # resolved once; the edit waits for a restart
    assert sb._SLICE_MEMHIGH_APPLIED == f"{_pct_mb(SIXTEEN, 60)}M"


def test_the_kill_switch_still_disarms_after_a_failed_apply(home: Path) -> None:
    _write_limits(home, {"slice_memory_high_pct": 60})
    with (
        patch("os.sysconf", side_effect=_ram(SIXTEEN), create=True),
        patch(
            "kiro_crew.sandbox.platform_compat.trusted_system_bin",
            return_value="/usr/bin/systemctl",
        ),
        patch("kiro_crew.sandbox.subprocess.run") as run,
    ):
        run.return_value = MagicMock(returncode=1, stderr="Failed", stdout="")
        sb._ensure_agent_slice_memory_high()
        sb._ensure_agent_slice_memory_high()
    assert sum("set-property" in c.args[0] for c in run.call_args_list) == 1
    assert sb._SLICE_MEMHIGH_DISABLED is True
    assert sb._SLICE_MEMHIGH_APPLIED is None


def test_no_cgroup_v2_means_no_systemctl_call(home: Path) -> None:
    """Where the cgroup probe fails (non-Linux, no cgroup v2, no delegation) the
    scope wrapper never reaches the reconcile, configured or not."""
    _write_limits(home, {"slice_memory_high_pct": 60})
    sb._CGROUP_SCOPE_PROBE = None
    sb._CGROUP_WARNED = False
    try:
        with (
            patch("kiro_crew.sandbox._probe_cgroup_scope", return_value=(False, "no cgroup v2")),
            patch("kiro_crew.sandbox.subprocess.run") as run,
        ):
            out = sb.cgroup_scope_argv(["git", "status"])
        assert out == ["git", "status"]
        run.assert_not_called()
        assert sb._SLICE_MEMHIGH_APPLIED is None
    finally:
        sb._CGROUP_SCOPE_PROBE = None
        sb._CGROUP_WARNED = False


class TestParse:
    @pytest.mark.parametrize("value", [50, 75, 95, 80.0])
    def test_the_range_is_inclusive(self, value: object) -> None:
        _WARNED_RESOURCE_LIMIT_KEYS.clear()
        rl = ResourceLimitsConfig.from_raw({"slice_memory_high_pct": value})
        assert rl.slice_memory_high_pct == int(value)  # type: ignore[call-overload]

    @pytest.mark.parametrize("value", [49, 96, 49.9, 95.5, "75", False])
    def test_outside_the_range_is_unset(self, value: object) -> None:
        _WARNED_RESOURCE_LIMIT_KEYS.clear()
        rl = ResourceLimitsConfig.from_raw({"slice_memory_high_pct": value})
        assert rl.slice_memory_high_pct is None
