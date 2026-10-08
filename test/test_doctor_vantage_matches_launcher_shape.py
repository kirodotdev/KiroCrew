"""The doctor's confined-vantage predicate must recognise the identity map the
sandbox actually writes.

``_process_userns_vantage_confined`` in :mod:`kiro_crew.doctor_checks.confinement`
(exposed through the ``cli_doctor`` facade) decides whether the current shell is
Kiro Crew's own confined agent shell, so diagnostics keep their host-level verdict
instead of wrongly reporting it. It recognises that shell by the shape of
``/proc/self/uid_map`` (one identity entry of length one) plus seccomp filtering.

That same ``uid_map`` is produced in two places: the namespace launcher's parent
half writes it for a spawned child, and the backend probe writes it for its own
probe child. Those shapes and the doctor predicate are spelled out independently,
so a change to the written map could silently stop matching the predicate and
bring back the wrong host-level verdict for a confined shell -- with nothing going
red. These tests pin the written shapes to the predicate so any such drift fails
here.
"""

from __future__ import annotations

import sys

import pytest
from test_sandbox_launcher_program import RecordingLibc, payload

from kiro_crew import cli_doctor, platform_compat, sandbox, sandbox_launcher_program

program = sandbox_launcher_program

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="the namespace launcher and the vantage predicate are Linux-only",
)

#: Seccomp mode 2 (SECCOMP_MODE_FILTER). The confined agent shell always runs
#: with a seccomp filter installed; the predicate requires it alongside the map
#: shape, and the launcher-written map alone does not model it.
_SECCOMP_FILTER = "Seccomp:\t2\n"


class _ProcWrites:
    """A stand-in for ``open`` that records the text written to each path.

    Both the launcher parent and the probe write ``setgroups``/``uid_map``/
    ``gid_map`` under ``/proc/<pid>/``; capturing those lets a test read back the
    exact ``uid_map`` line each produces, without a real user namespace (which an
    agent sandbox's nested ``unshare`` is seccomp-denied from creating).
    """

    def __init__(self) -> None:
        self.written: list[tuple[str, str]] = []

    def __call__(self, path: str, mode: str = "r") -> "_ProcWrites":
        assert mode == "w", (path, mode)
        self.written.append((path, ""))
        return self

    def __enter__(self) -> "_ProcWrites":
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def write(self, text: str) -> int:
        path, before = self.written[-1]
        self.written[-1] = (path, before + text)
        return len(text)

    def uid_map(self) -> str:
        for path, text in self.written:
            if path.endswith("/uid_map"):
                return text
        raise AssertionError(f"no uid_map line was written: {self.written}")


def _doctor_classifies_confined(monkeypatch: pytest.MonkeyPatch, uid_map: str) -> bool | None:
    """Feed *uid_map* (with a seccomp filter present) to the doctor predicate."""
    monkeypatch.setattr(platform_compat, "IS_LINUX", True)
    files = {"uid_map": uid_map, "status": _SECCOMP_FILTER}
    monkeypatch.setattr(cli_doctor, "_read_linux_proc_self", lambda name: files[name])
    return cli_doctor._process_userns_vantage_confined()


def test_the_launcher_written_map_is_classified_confined(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The map the launcher writes for a spawned child is the confined-shell shape."""
    import os

    proc = _ProcWrites()
    real_pipe = os.pipe
    child_pid = 99_999_999_999
    child_ends: list[int] = []

    def _pipe() -> tuple[int, int]:
        read_end, write_end = real_pipe()
        if not child_ends:
            os.write(write_end, b"x")  # the child's "unshare done", already sent
        child_ends.append(os.dup(read_end))
        return read_end, write_end

    monkeypatch.setenv("KIROCREW_HOST_PID", "0")
    monkeypatch.setattr(program.os, "pipe", _pipe)
    monkeypatch.setattr(program.os, "fork", lambda: child_pid)
    monkeypatch.setattr(program.os, "waitpid", lambda _pid, _opts: (child_pid, 0))
    monkeypatch.setattr(program, "open", proc, raising=False)

    try:
        with pytest.raises(SystemExit):
            program.main(
                payload(real_uid=4321, real_gid=4321),
                libc=RecordingLibc(),
                argv=["/a"],
            )
    finally:
        for fd in child_ends:
            os.close(fd)

    assert _doctor_classifies_confined(monkeypatch, proc.uid_map()) is True


def test_the_probe_written_map_is_classified_confined(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The map the backend probe writes for its probe child is the same shape.

    ``_probe_write_identity_maps`` writes the three ``/proc/<pid>/`` files; capture
    its ``uid_map`` line the same way and assert the doctor agrees it is confined.
    """
    proc = _ProcWrites()
    monkeypatch.setattr(sandbox, "open", proc, raising=False)
    result = sandbox._probe_write_identity_maps(pid=99_999_999_999, uid=4321, gid=4321)
    assert result is None, result
    assert _doctor_classifies_confined(monkeypatch, proc.uid_map()) is True
