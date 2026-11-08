"""An inherited ``ENV`` does not decline a name grant; ``BASH_ENV`` still does.

``name_grant.environment_refusal()`` declines every name grant while the
inherited environment can redefine a program name as a shell function before
the command runs. ``ENV`` names a start-up file that only an INTERACTIVE POSIX
shell reads, and the shell that runs an agent command is started with ``-c``
and no ``-i``. A container image that exports ``ENV`` for its interactive logins
must therefore not cost every name grant on that host its auto-approve.

The first class pins the decision. The second is the premise it rests on,
measured on the real shells: ``bash -c`` and ``sh -c`` never source ``ENV``,
while ``bash -c`` does source ``BASH_ENV``, which is why that one stays refused.
"""

from __future__ import annotations

import os
import shlex
import shutil

import pytest
from installer_test_helpers import run_bounded

from kiro_crew import name_grant, platform_compat

pytestmark = pytest.mark.skipif(
    platform_compat.IS_WINDOWS,
    reason="the inherited-preload refusal and the shells it models are POSIX-only",
)

#: A start-up file path of the shape a container image exports for its
#: interactive logins. Its content never matters to the decision under test.
_CONTAINER_ENV_VALUE = "/etc/container-interactive-rc"


@pytest.fixture(autouse=True)
def _clear_pins():
    """Each test starts and ends with no pinned program identities."""

    name_grant._PINS.clear()
    yield
    name_grant._PINS.clear()


@pytest.fixture
def system_dir(tmp_path, monkeypatch):
    """A hermetic search path whose only directory stands in for the system bins."""

    system = tmp_path / "usr" / "bin"
    system.mkdir(parents=True)
    head = system / "head"
    head.write_bytes(b"\x7fELF\x02\x01\x01\x00 not a real binary\n")
    head.chmod(0o700)

    monkeypatch.setattr(name_grant, "_agent_search_path", lambda: str(system))

    def fake_system_bin(name: str) -> str | None:
        candidate = system / name
        return str(candidate) if candidate.is_file() else None

    monkeypatch.setattr(platform_compat, "trusted_system_bin", fake_system_bin)
    monkeypatch.setattr(name_grant, "_agent_writable_roots", lambda: ())
    monkeypatch.setattr(name_grant, "_path_is_ambiguous", lambda: False)
    return system


class TestInheritedEnvDecision:
    """What ``environment_refusal`` answers for an inherited ``ENV``."""

    def test_an_inherited_env_does_not_decline_a_name_grant(self, system_dir, monkeypatch):
        assert name_grant.name_grant_refusal("head file") is None
        monkeypatch.setenv("ENV", _CONTAINER_ENV_VALUE)
        assert name_grant.environment_refusal() is None
        assert name_grant.name_grant_refusal("head file") is None

    def test_an_inherited_bash_env_still_declines(self, system_dir, monkeypatch):
        # The variable a NON-interactive bash does source stays refused, so the
        # decision above is about which shell reads the file, not a looser gate.
        monkeypatch.setenv("BASH_ENV", _CONTAINER_ENV_VALUE)
        refusal = name_grant.environment_refusal()
        assert refusal is not None
        assert refusal.code == name_grant.AMBIGUOUS_ENV

    def test_env_written_in_the_command_line_still_refuses(self, system_dir):
        # A command-position prefix is a different subject: the command itself
        # chooses the start-up file, so it is refused rather than reasoned about.
        assert name_grant.program_names(f"ENV={_CONTAINER_ENV_VALUE} head file") is None


def _child_env(**overrides: str) -> dict[str, str]:
    """The test process's environment with no shell preload, plus *overrides*."""

    env = {
        key: value
        for key, value in os.environ.items()
        if key not in ("BASH_ENV", "ENV", "SHELLOPTS", "BASHOPTS")
        and not key.startswith(name_grant._BASH_FUNC_KEY_PREFIX)
        and not value.startswith(name_grant._BASH_FUNC_VALUE_PREFIX)
    }
    env.update(overrides)
    return env


class TestNonInteractiveShellPremise:
    """The shells an agent command runs under, started the way the harness starts them."""

    @pytest.fixture
    def shadowing_rc(self, tmp_path):
        """A start-up file that shadows ``head``, and a file ``head`` would read."""

        rc = tmp_path / "rc"
        rc.write_text("head() { echo SHADOWED; }\n", encoding="utf-8")
        data = tmp_path / "data.txt"
        data.write_text("real line\nsecond line\n", encoding="utf-8")
        return rc, data

    @staticmethod
    def _run_head(shell: str, data, tmp_path, env: dict[str, str]) -> str:
        # run_bounded's default bound sits under pytest-timeout's ceiling, so a
        # wedged shell is reaped by the helper rather than the test runner.
        proc = run_bounded(
            [shell, "-c", f"head -n 1 {shlex.quote(str(data))}"],
            env,
            cwd=str(tmp_path),
        )
        assert proc.returncode == 0, proc.stderr
        return proc.stdout.strip()

    @pytest.mark.parametrize("shell_name", ["bash", "sh"])
    def test_a_dash_c_shell_does_not_source_env(self, shell_name, shadowing_rc, tmp_path):
        shell = shutil.which(shell_name)
        if shell is None:
            pytest.skip(f"{shell_name} is not installed on this host")
        rc, data = shadowing_rc
        out = self._run_head(shell, data, tmp_path, _child_env(ENV=str(rc)))
        assert out == "real line"

    def test_the_probe_sees_a_shadow_when_bash_sources_bash_env(self, shadowing_rc, tmp_path):
        # Positive control: the same start-up file DOES shadow `head` once a
        # non-interactive shell reads it, so the negative case above is a real
        # observation and not a probe that cannot see a function.
        shell = shutil.which("bash")
        if shell is None:
            pytest.skip("bash is not installed on this host")
        rc, data = shadowing_rc
        out = self._run_head(shell, data, tmp_path, _child_env(BASH_ENV=str(rc)))
        assert out == "SHADOWED"
