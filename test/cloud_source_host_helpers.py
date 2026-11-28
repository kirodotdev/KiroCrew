"""The host above a test's temp root, presented as a well-kept one.

``cloud.source`` refuses to stage the source tarball under a directory that a
third account owns, and when the data home sits outside the operator's own home
it checks every ancestor up to ``/``. A test's data home lives under pytest's
temp root, so those ancestors are the HOST's directories: ``/``, ``/var``,
``/var/tmp``. Their owners are an input the test did not choose. Inside a user
namespace (an agent sandbox, a rootless container) ``/`` reads as the overflow
uid 65534, and every tarball build refuses before the code under test runs.

The directories pytest creates belong to this account, so only the host part is
rewritten: an ancestor of the base temp directory whose owner is neither this
account nor root is reported as owned by root. Everything at or below the base
temp directory is reported as it is, which keeps the refusals the staging tests
plant under ``tmp_path`` meaningful.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest


def present_a_well_kept_host_above(monkeypatch: pytest.MonkeyPatch, base: Path) -> None:
    """Report root as the owner of any foreign-owned ancestor of ``base``."""
    if os.name != "posix":
        return
    resolved = base.resolve()
    host = {str(node) for node in resolved.parents}
    mine = os.geteuid()
    real_stat = Path.stat

    def _stat(self: Path, *args, **kwargs) -> os.stat_result:
        info = real_stat(self, *args, **kwargs)
        if str(self) in host and info.st_uid not in (mine, 0):
            fields = list(info)
            fields[4] = 0  # st_uid
            return os.stat_result(tuple(fields))
        return info

    monkeypatch.setattr(Path, "stat", _stat)
