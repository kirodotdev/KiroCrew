"""Which cgroup's stall judges memory while the memory cgroup is out of view.

``out_of_view_memory_pressure_file`` names the topmost visible cgroup's
``memory.pressure`` only when that cgroup is a child cgroup without the memory
controller, on a kernel not booted with that controller off: the free figure is then
the host's, and any limit sits above the mount. ``out_of_view_memory_stall_us`` reads that
file's total ``full`` stall. Every kernel input is synthetic: the readers go through
``kiro_crew.subagent``'s ``open``, which these tests replace, so the host's own
cgroup layout decides nothing.
"""

from __future__ import annotations

from io import StringIO
from typing import Any

import pytest

import kiro_crew.subagent as subagent

_V2_MOUNT = "31 20 0:28 / /sys/fs/cgroup rw - cgroup2 cgroup2 rw\n"
_EVENTS = "populated 1\nfrozen 0\n"
_PRESSURE = "/sys/fs/cgroup/memory.pressure"


def _proc_cgroups(memory_enabled: str = "1") -> str:
    return (
        "#subsys_name\thierarchy\tnum_cgroups\tenabled\n"
        "cpuset\t0\t57\t1\n"
        f"memory\t0\t57\t{memory_enabled}\n"
        "pids\t0\t57\t1\n"
    )


_ROWS_WITHOUT_MEMORY = "#subsys_name\thierarchy\tnum_cgroups\tenabled\ncpuset\t0\t57\t1\n"


@pytest.fixture
def cgroup_files(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Path -> text, or an exception to raise; every other path is absent."""
    files: dict[str, Any] = {}

    def read(path: object, **_kwargs: object) -> StringIO:
        value = files.get(str(path))
        if value is None:
            raise FileNotFoundError(path)
        if isinstance(value, BaseException):
            raise value
        return StringIO(str(value))

    monkeypatch.setattr(subagent, "open", read, raising=False)
    monkeypatch.setattr(subagent.platform_compat, "IS_LINUX", True)
    return files


def _namespaced(files: dict[str, Any], controllers: str) -> None:
    """A cgroup namespace rooted at a child cgroup, the way a container sees itself."""
    files.update(
        {
            "/proc/self/cgroup": "0::/\n",
            "/proc/self/mountinfo": _V2_MOUNT,
            "/proc/cgroups": _proc_cgroups(),
            "/sys/fs/cgroup/cgroup.controllers": controllers,
            "/sys/fs/cgroup/cgroup.events": _EVENTS,
        }
    )


class TestOutOfViewPressureFile:
    @pytest.mark.parametrize("controllers", ["cpuset cpu pids\n", "\n"])
    def test_a_child_cgroup_without_the_memory_controller_is_out_of_view(
        self, cgroup_files: dict[str, Any], controllers: str
    ) -> None:
        _namespaced(cgroup_files, controllers)
        assert subagent.out_of_view_memory_pressure_file() == _PRESSURE

    def test_a_visible_memory_controller_is_in_view(self, cgroup_files: dict[str, Any]) -> None:
        _namespaced(cgroup_files, "cpuset cpu io memory pids\n")
        assert subagent.out_of_view_memory_pressure_file() is None

    def test_the_real_root_is_in_view(self, cgroup_files: dict[str, Any]) -> None:
        # Only a child cgroup has cgroup.events: above the real root there is nothing.
        _namespaced(cgroup_files, "cpuset cpu pids\n")
        del cgroup_files["/sys/fs/cgroup/cgroup.events"]
        assert subagent.out_of_view_memory_pressure_file() is None

    def test_a_bind_mounted_child_cgroup_is_judged_at_its_mount(
        self, cgroup_files: dict[str, Any]
    ) -> None:
        cgroup_files.update(
            {
                "/proc/self/cgroup": "0::/pod/ctr/app\n",
                "/proc/self/mountinfo": (
                    "31 20 0:28 /pod/ctr /sys/fs/cgroup rw - cgroup2 cgroup2 rw\n"
                ),
                "/proc/cgroups": _proc_cgroups(),
                "/sys/fs/cgroup/cgroup.controllers": "cpu pids\n",
                "/sys/fs/cgroup/cgroup.events": _EVENTS,
                "/sys/fs/cgroup/app/cgroup.controllers": "\n",
                "/sys/fs/cgroup/app/cgroup.events": _EVENTS,
            }
        )
        assert subagent.out_of_view_memory_pressure_file() == _PRESSURE

    def test_a_second_mount_at_the_real_root_keeps_it_in_view(
        self, cgroup_files: dict[str, Any]
    ) -> None:
        cgroup_files.update(
            {
                "/proc/self/cgroup": "0::/pod/ctr\n",
                "/proc/self/mountinfo": (
                    "31 20 0:28 /pod/ctr /sys/fs/cgroup rw - cgroup2 cgroup2 rw\n"
                    "32 20 0:28 / /host/cgroup rw - cgroup2 cgroup2 rw\n"
                ),
                "/proc/cgroups": _proc_cgroups(),
                "/sys/fs/cgroup/cgroup.controllers": "cpu pids\n",
                "/sys/fs/cgroup/cgroup.events": _EVENTS,
                "/host/cgroup/cgroup.controllers": "cpu io memory pids\n",
            }
        )
        assert subagent.out_of_view_memory_pressure_file() is None

    def test_a_v1_memory_hierarchy_is_in_view(self, cgroup_files: dict[str, Any]) -> None:
        # systemd's hybrid layout: the controllers on v1, an empty v2 tree beside them.
        # The v1 hierarchy holds the memory controller, readable or not, so it is in
        # view whatever cgroup2-style files its mount shows.
        cgroup_files.update(
            {
                "/proc/self/cgroup": "0::/\n4:memory:/user.slice\n",
                "/proc/self/mountinfo": (
                    "31 20 0:28 / /sys/fs/cgroup/unified rw - cgroup2 cgroup2 rw\n"
                    "32 20 0:29 / /sys/fs/cgroup/memory rw - cgroup cgroup rw,memory\n"
                ),
                "/proc/cgroups": _proc_cgroups(),
                "/sys/fs/cgroup/unified/cgroup.controllers": "\n",
                "/sys/fs/cgroup/unified/cgroup.events": _EVENTS,
                "/sys/fs/cgroup/memory/cgroup.controllers": "\n",
                "/sys/fs/cgroup/memory/cgroup.events": _EVENTS,
            }
        )
        assert subagent.out_of_view_memory_pressure_file() is None

    @pytest.mark.parametrize("name", ["cgroup.controllers", "cgroup.events"])
    @pytest.mark.parametrize("error", [PermissionError, FileNotFoundError])
    def test_an_unreadable_flag_file_is_no_evidence(
        self, cgroup_files: dict[str, Any], name: str, error: type[OSError]
    ) -> None:
        _namespaced(cgroup_files, "cpuset cpu pids\n")
        cgroup_files[f"/sys/fs/cgroup/{name}"] = error(name)
        assert subagent.out_of_view_memory_pressure_file() is None

    @pytest.mark.parametrize(
        ("proc_cgroups", "cmdline"),
        [
            (_proc_cgroups(memory_enabled="0"), "ro cgroup_disable=memory quiet\n"),
            (_proc_cgroups(memory_enabled="0"), None),
            # From Linux 6.12 a kernel without CONFIG_MEMCG_V1 lists no memory row, so
            # only the command line shows the controller off.
            (_ROWS_WITHOUT_MEMORY, "ro cgroup_disable=cpuset,memory quiet\n"),
        ],
    )
    def test_a_kernel_booted_with_the_memory_controller_off_is_nothing_out_of_view(
        self, cgroup_files: dict[str, Any], proc_cgroups: str, cmdline: str | None
    ) -> None:
        # With the controller off, no cgroup anywhere carries a memory limit.
        _namespaced(cgroup_files, "cpuset cpu pids\n")
        cgroup_files["/proc/cgroups"] = proc_cgroups
        cgroup_files["/proc/cmdline"] = cmdline
        assert subagent.out_of_view_memory_pressure_file() is None

    @pytest.mark.parametrize(
        "proc_cgroups",
        [
            _ROWS_WITHOUT_MEMORY,  # Linux 6.12+ without CONFIG_MEMCG_V1
            "memory 0\n",  # malformed
            PermissionError("/proc/cgroups"),
            None,  # absent
        ],
    )
    def test_a_missing_memory_row_is_no_evidence_the_controller_is_off(
        self, cgroup_files: dict[str, Any], proc_cgroups: Any
    ) -> None:
        _namespaced(cgroup_files, "cpuset cpu pids\n")
        cgroup_files["/proc/cgroups"] = proc_cgroups
        cgroup_files["/proc/cmdline"] = "ro cgroup_disable=cpuset quiet\n"
        assert subagent.out_of_view_memory_pressure_file() == _PRESSURE

    def test_of_two_mounts_in_view_the_topmost_names_the_file(
        self, cgroup_files: dict[str, Any]
    ) -> None:
        # A deeper bind mount listed first must not narrow what is measured.
        cgroup_files.update(
            {
                "/proc/self/cgroup": "0::/app/x\n",
                "/proc/self/mountinfo": (
                    "32 20 0:28 /app /run/app-cgroup rw - cgroup2 cgroup2 rw\n"
                    "31 20 0:28 / /sys/fs/cgroup rw - cgroup2 cgroup2 rw\n"
                ),
                "/proc/cgroups": _proc_cgroups(),
                "/sys/fs/cgroup/cgroup.controllers": "cpu pids\n",
                "/sys/fs/cgroup/cgroup.events": _EVENTS,
                "/run/app-cgroup/cgroup.controllers": "\n",
                "/run/app-cgroup/cgroup.events": _EVENTS,
            }
        )
        assert subagent.out_of_view_memory_pressure_file() == _PRESSURE

    @pytest.mark.parametrize("platform_flag", ["IS_MACOS", "IS_WINDOWS"])
    def test_off_linux_nothing_is_out_of_view(
        self,
        cgroup_files: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
        platform_flag: str,
    ) -> None:
        _namespaced(cgroup_files, "cpuset cpu pids\n")
        monkeypatch.setattr(subagent.platform_compat, "IS_LINUX", False)
        monkeypatch.setattr(subagent.platform_compat, platform_flag, True)
        assert subagent.out_of_view_memory_pressure_file() is None


class TestOutOfViewStall:
    def test_reads_the_full_total_in_microseconds(self, cgroup_files: dict[str, Any]) -> None:
        _namespaced(cgroup_files, "cpuset cpu pids\n")
        cgroup_files[_PRESSURE] = (
            "some avg10=50.00 avg60=20.00 avg300=5.00 total=987654321\n"
            "full avg10=37.50 avg60=15.00 avg300=4.00 total=123456789\n"
        )
        assert subagent.out_of_view_memory_stall_us() == 123456789

    def test_in_view_there_is_no_reading(self, cgroup_files: dict[str, Any]) -> None:
        _namespaced(cgroup_files, "cpuset cpu io memory pids\n")
        cgroup_files[_PRESSURE] = "full avg10=37.50 avg60=15.00 avg300=4.00 total=123\n"
        assert subagent.out_of_view_memory_stall_us() is None

    @pytest.mark.parametrize(
        "pressure",
        [
            None,  # absent
            PermissionError(_PRESSURE),
            "some avg10=50.00 avg60=20.00 avg300=5.00 total=987654321\n",  # no full line
            "full avg10=37.50 avg60=15.00 avg300=4.00\n",  # no total
            "full avg10=37.50 avg60=15.00 avg300=4.00 total=-5\n",
            "full avg10=37.50 avg60=15.00 avg300=4.00 total=12x\n",
        ],
    )
    def test_a_missing_or_malformed_total_is_no_reading(
        self, cgroup_files: dict[str, Any], pressure: Any
    ) -> None:
        _namespaced(cgroup_files, "cpuset cpu pids\n")
        cgroup_files[_PRESSURE] = pressure
        assert subagent.out_of_view_memory_stall_us() is None
