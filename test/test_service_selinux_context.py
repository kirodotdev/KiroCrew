"""The system unit runs the gateway in the installer's SELinux context.

Without ``SELinuxContext=`` a system unit's gateway runs as ``system_u``, and
every cache it writes under the user's home (``~/.npm/_cacache``) is labelled
``system_u``; the user's own shell is then refused hardlinks inside it. The line
is written only when the loaded policy says the unit can still start with it.

These tests point the module at a tmp_path selinuxfs, a fake
``/proc/self/attr/current`` and a fake policy answer (``_compute_av``, the one
call that reaches the kernel), so the verdict never depends on the host.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew.service import linux as svc_linux
from kiro_crew.service import selinux as sel

USER_CTX = "unconfined_u:unconfined_r:unconfined_t:s0-s0:c0.c1023"
INIT_T = "system_u:system_r:init_t:s0"
BIN_T = "system_u:object_r:bin_t:s0"
EXE = "/home/tester/.local/bin/kirocrew"

# 1-based bit numbers as selinuxfs reports them.
_BITS = {("process", "transition"): 2, ("file", "entrypoint"): 18}
_INDEX = {"process": 2, "file": 6}


@pytest.fixture
def host(tmp_path, monkeypatch):
    """A host whose selinuxfs, installer context and policy each test sets."""
    root = tmp_path / "selinux"
    for (cls, perm), bit in _BITS.items():
        (root / "class" / cls / "perms").mkdir(parents=True, exist_ok=True)
        (root / "class" / cls / "perms" / perm).write_text(str(bit))
        (root / "class" / cls / "index").write_text(str(_INDEX[cls]))
    attr = tmp_path / "attr-current"
    monkeypatch.setattr(sel, "_ENFORCE_PATH", root / "enforce")
    monkeypatch.setattr(sel, "_CLASS_DIR", root / "class")
    monkeypatch.setattr(sel, "_INSTALLER_ATTR", attr, raising=False)
    monkeypatch.setattr(sel, "_system_manager_context", lambda: INIT_T)
    monkeypatch.setattr(sel, "_file_context", lambda _p: BIN_T)
    monkeypatch.setattr(sel, "_interpreter_of", lambda _p: None)
    asked: list[tuple[str, str, str]] = []

    def configure(
        *,
        enforce_value: str | None = "1",
        context: str | None = USER_CTX,
        denied: frozenset[tuple[str, str]] = frozenset(),
        answer: bool = True,
    ) -> list[tuple[str, str, str]]:
        if enforce_value is not None:
            (root / "enforce").write_text(enforce_value)
        if context is not None:
            # The kernel NUL-terminates the attribute.
            attr.write_text(context + "\x00")

        def compute_av(source, target, cls):
            asked.append((source, target, cls))
            if not answer:
                return None
            allowed = 0
            for (c, perm), bit in _BITS.items():
                if c == cls and (cls, perm) not in denied:
                    allowed |= 1 << (bit - 1)
            return allowed, 0

        monkeypatch.setattr(sel, "_compute_av", compute_av)
        return asked

    return configure


def _render(*, user_scope: bool = False) -> str:
    gid = MagicMock(returncode=0, stdout="tester\n", stderr="")
    with (
        patch("kiro_crew.service.common.shutil.which", return_value=EXE),
        patch("kiro_crew.service.linux.subprocess.run", return_value=gid),
        patch("kiro_crew.service.linux.trusted_system_bin", return_value="/usr/bin/id"),
    ):
        return svc_linux.render_unit(user_scope=user_scope)


class TestInstallerContext:
    @pytest.mark.parametrize("enforce_value", ["1", "0"])
    def test_enforcing_and_permissive_hosts_both_get_the_context(self, host, enforce_value):
        host(enforce_value=enforce_value)
        assert sel.installer_context(EXE) == USER_CTX

    def test_the_policy_is_asked_both_questions(self, host):
        asked = host()
        sel.installer_context(EXE)
        assert (INIT_T, USER_CTX, "process") in asked
        assert (USER_CTX, BIN_T, "file") in asked

    @pytest.mark.parametrize(
        "denied", [("process", "transition"), ("file", "entrypoint")], ids=lambda d: d[1]
    )
    def test_a_denied_switch_omits_the_context(self, host, denied):
        host(denied=frozenset({denied}))
        assert sel.installer_context(EXE) is None

    def test_an_unanswered_policy_question_omits_the_context(self, host):
        host(answer=False)
        assert sel.installer_context(EXE) is None

    def test_no_policy_interface_omits_the_context(self, host, monkeypatch, tmp_path):
        """The real query path, with ``/sys/fs/selinux/access`` absent."""
        host()
        monkeypatch.setattr(sel, "_compute_av", self._real_compute_av)
        monkeypatch.setattr(sel, "_ACCESS_PATH", tmp_path / "no-access-node")
        assert sel.installer_context(EXE) is None

    _real_compute_av = staticmethod(sel._compute_av)

    def test_an_omitted_context_says_why_once(self, host, caplog):
        host(denied=frozenset({("file", "entrypoint")}))
        with caplog.at_level(logging.INFO, logger=sel.log.name):
            sel.installer_context(EXE)
        lines = [r.getMessage() for r in caplog.records if "SELinuxContext=" in r.getMessage()]
        assert len(lines) == 1
        assert "may not start" in lines[0] and BIN_T in lines[0]

    def test_an_interpreter_without_entrypoint_omits_the_context(self, host, monkeypatch):
        host()
        labels = {EXE: BIN_T, "/usr/bin/python3": "system_u:object_r:home_bin_t:s0"}
        monkeypatch.setattr(sel, "_interpreter_of", lambda _p: "/usr/bin/python3")
        monkeypatch.setattr(sel.os.path, "realpath", lambda p: p)
        monkeypatch.setattr(sel, "_file_context", labels.get)

        def compute_av(source, target, cls):
            entry = (
                0 if target.endswith(":home_bin_t:s0") else 1 << (_BITS[("file", "entrypoint")] - 1)
            )
            return (1 << (_BITS[("process", "transition")] - 1)) | entry, 0

        monkeypatch.setattr(sel, "_compute_av", compute_av)
        assert sel.installer_context(EXE) is None

    def test_no_selinux_is_none(self, host):
        host(enforce_value=None)
        assert sel.installer_context(EXE) is None

    def test_unreadable_context_is_none(self, host):
        host(context=None)
        assert sel.installer_context(EXE) is None

    def test_a_system_u_installer_is_none(self, host):
        host(context="system_u:system_r:initrc_t:s0")
        assert sel.installer_context(EXE) is None

    @pytest.mark.parametrize(
        "bad",
        ["", "kernel", "unconfined_u::unconfined_t", "a:b:c\nExecStartPre=/bin/sh", "a:b:c d"],
    )
    def test_a_malformed_context_is_none(self, host, bad):
        host(context=bad)
        assert sel.installer_context(EXE) is None


class TestRenderedUnit:
    @pytest.fixture(autouse=True)
    def _user(self, _floor_monkeypatch):
        _floor_monkeypatch.setenv("USER", "tester")
        _floor_monkeypatch.delenv("SUDO_USER", raising=False)

    def test_selinux_host_unit_runs_in_the_installer_context(self, host):
        host()
        unit = _render()
        service = unit.split("[Service]\n", 1)[1].split("\n[Install]", 1)[0]
        assert f"SELinuxContext={USER_CTX}" in service.splitlines()

    def test_a_policy_that_refuses_the_switch_leaves_the_unit_unchanged(self, host):
        host(denied=frozenset({("file", "entrypoint")}))
        assert "SELinuxContext=" not in _render()

    def test_non_selinux_host_unit_has_no_context_line(self, host):
        host(enforce_value=None)
        assert "SELinuxContext=" not in _render()

    def test_user_scope_unit_never_carries_it(self, host):
        host()
        assert "SELinuxContext=" not in _render(user_scope=True)
