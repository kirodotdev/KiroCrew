"""Requirement (Connections G2 pod-grant-isolation): if ``HOME`` is remapped
for ANY process, the real passwd home's sensitive paths (``~/.aws``,
``~/.ssh``, ``~/.kirocrew*``) must STILL be denied by ``security.py``'s
matchers inside that process. A remap that unfences the real home is a
rejected design.

Why this matters for the pod ``HOME`` remap
(``acp.client._apply_pod_home_remap``): ``security.py``'s sensitive-path
matchers run inside the GATEWAY process, evaluating a tool call's ARGUMENTS
against the gateway's own ``Path.home()`` -- the spawned kiro-cli child's
remapped ``HOME`` never reaches this code, because the gateway process's
``os.environ["HOME"]`` is never touched by ``build_pod_env`` or by
``_apply_pod_home_remap`` (which mutates only the CHILD's env dict handed to
``create_subprocess_exec``/``create_subprocess_limited``, not the gateway's
own ``os.environ``).

These tests pin that property directly: a fenced path resolved against the
gateway's OWN home stays denied regardless of what ``HOME`` a spawned
child happens to run under.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiro_crew import security
from kiro_crew.acp.client import _apply_pod_home_remap


def _pin_home(monkeypatch: pytest.MonkeyPatch, home: Path) -> Path:
    """Pin this process's home to *home* on every platform, and return it as
    ``Path.home()`` resolves it.

    ``USERPROFILE`` is set alongside ``HOME`` because Windows ``Path.home()``
    reads that one and never ``HOME`` -- pinning only ``HOME`` leaves the
    matcher anchored on the real runner profile there, which is what made these
    tests fail on the Windows shard while passing on Linux. Mirrors the autouse
    fixture in ``test_pod.py``, which pins both for the same reason.

    **Both HOME-derived caches in ``security`` are reset**, and that is what makes
    a pinned home actually reach the matcher rather than only the environment.
    ``is_sensitive_bash_command`` runs several passes over different derivations of
    ``Path.home()``:

    * ``_SENSITIVE_RE`` (pass 1, the fast-path regex) is a PROCESS GLOBAL built
      once -- ``if _SENSITIVE_RE is None`` -- with no TTL and no invalidation. It
      captures ``Path.home()`` at first use, so once ANY earlier test in the same
      xdist worker has called a matcher, a later ``monkeypatch.setenv("HOME", ...)``
      is invisible to that pass forever.
    * ``_home_targets_cache`` (the later passes) is TTL-bounded and keyed on the
      resolved roots, so it does pick up a new home -- and it is the cache the rest
      of ``test_security.py`` already clears by hand, which is the established seam
      this follows.

    That asymmetry is exactly why this failed ONLY on Windows: on Linux the
    target-set passes rescued the stale pass-1 regex (verified -- priming the
    global before moving HOME does not change the Linux verdict), while on the
    Windows runner they did not. The Windows pattern itself is NOT the gap: built
    with a Windows-shaped home and cleared caches, it matches
    ``C:\\...\\real-home\\.aws\\credentials`` (the builder has a full
    ``win_sep``/``win_gsep`` branch). Nothing in production depends on the reset --
    the gateway's own HOME does not move under it, and ``_apply_pod_home_remap``
    changes only a CHILD's environment -- so this is a test-pinning fix, not a
    matcher fix.

    Also creates the directory, so ``resolve()`` is well defined on every platform,
    and returns the resolved value because the target set is anchored on
    ``Path.home().resolve()``.
    """
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    # Order matters: set the env FIRST, then drop both derivations of it.
    monkeypatch.setattr(security, "_SENSITIVE_RE", None, raising=False)
    security._home_targets_cache.clear()
    return Path.home().resolve()


class TestGatewayHomeIsIndependentOfAChildsRemappedHome:
    def test_apply_pod_home_remap_never_touches_process_environ(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The remap operates on a plain dict handed to the child spawn call
        -- never on os.environ, which is what security.py's Path.home() calls
        resolve against inside the gateway's own process."""
        real_home = _pin_home(monkeypatch, tmp_path / "real-home")
        gateway_home_before = Path.home()

        child_env = {
            "HOME": str(real_home),
            "KIROCREW_POD": "1",
            "KIROCREW_OS_HOME": str(tmp_path / "pod-os-home"),
        }
        _apply_pod_home_remap(child_env, pod_home_remap=True)

        assert child_env["HOME"] == str(tmp_path / "pod-os-home")
        # The gateway's OWN Path.home() -- what security.py's matchers read --
        # is completely unaffected by mutating the child's env dict.
        assert Path.home() == gateway_home_before == real_home

    def test_sensitive_paths_stay_denied_against_the_gateways_own_home(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Simulates the pod boot ordering: a KIROCREW_POD/KIROCREW_OS_HOME
        pair is present in the GATEWAY's own os.environ too (build_pod_env
        sets both on the whole pod gateway process), yet a tool call naming
        the real ~/.aws/credentials must still be denied by is_sensitive_path
        -- the pod anchor ADDS a fenced root, it never removes the real one."""
        real_home = _pin_home(monkeypatch, tmp_path / "real-home")
        monkeypatch.setenv("KIROCREW_POD", "1")
        monkeypatch.setenv("KIROCREW_OS_HOME", str(tmp_path / "pod-os-home"))

        assert security.is_sensitive_path(str(real_home / ".aws" / "credentials")) is True
        assert security.is_sensitive_path(str(real_home / ".ssh" / "id_rsa")) is True
        assert security.is_sensitive_path("~/.aws/credentials") is True

    def test_the_relocated_pod_home_is_fenced_too(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The regression all three review lanes converged on: relocating the
        credential store must not move it OUT from under the fence.

        `_seed_pod_os_home` copies the operator's real SSO bearer token into
        `<pod home>/os-home/.aws/sso/cache`, and a pod-spawned child's `$HOME`
        is that tree -- so if `is_sensitive_path` anchored `.aws` only under the
        real home, an agent inside a pod could read a verbatim copy of the
        operator's identity token at the pod-path spelling while the identical
        bytes at `~/.aws` were refused. `KIROCREW_OS_HOME` is therefore anchored
        as an alternate home root, and EVERY fenced entry re-anchors under it,
        not merely `.aws`."""
        pod_os_home = tmp_path / "pod-os-home"
        _pin_home(monkeypatch, tmp_path / "real-home")
        monkeypatch.setenv("KIROCREW_POD", "1")
        monkeypatch.setenv("KIROCREW_OS_HOME", str(pod_os_home))

        # The seeded host SSO token, at the pod-path spelling.
        assert (
            security.is_sensitive_path(
                str(pod_os_home / ".aws" / "sso" / "cache" / "kiro-auth-token.json")
            )
            is True
        )
        # A pod-minted MCP grant pair lands in the same directory.
        assert security.is_sensitive_path(str(pod_os_home / ".aws" / "credentials")) is True
        # The relocation moves the WHOLE home, so every other fenced entry
        # follows it -- not just the one subtree the token happens to live in.
        assert security.is_sensitive_path(str(pod_os_home / ".ssh" / "id_ed25519")) is True

    def test_a_remapped_child_env_home_does_not_leak_into_the_matcher(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The child's env dict is not an input to the matcher: `is_sensitive_path`
        takes no environment, so the real home stays fenced regardless of what
        HOME the child was handed. The pod tree is fenced too, but by the
        os.environ-level anchor rather than by this dict."""
        real_home = _pin_home(monkeypatch, tmp_path / "real-home")

        child_env = {
            "HOME": str(real_home),
            "KIROCREW_POD": "1",
            "KIROCREW_OS_HOME": str(tmp_path / "pod-os-home"),
        }
        _apply_pod_home_remap(child_env, pod_home_remap=True)
        assert child_env["HOME"] == str(tmp_path / "pod-os-home")

        assert security.is_sensitive_path(str(real_home / ".ssh" / "id_ed25519")) is True
        # No KIROCREW_OS_HOME in THIS process's environ, so the pod spelling is
        # not anchored here -- which is why build_pod_env sets it on the pod
        # gateway itself, covered by the test above.
        assert (
            security.is_sensitive_path(str((tmp_path / "pod-os-home") / ".ssh" / "id_ed25519"))
            is False
        )

    def test_is_sensitive_bash_command_also_keys_on_the_gateways_home(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        real_home = _pin_home(monkeypatch, tmp_path / "real-home")
        monkeypatch.setenv("KIROCREW_POD", "1")

        # Built with the running OS's separator: a hardcoded POSIX spelling
        # matches nothing on Windows, where every candidate form the matcher
        # derives is backslash-separated.
        target = str(real_home / ".aws" / "credentials")

        # The path gate first. It shares the home-derived target set with the bash
        # matcher's later passes, so if THIS holds the pinned home did reach the
        # matcher -- which makes the next assertion a statement about the bash
        # surface alone rather than about whether the pin took.
        assert security.is_sensitive_path(target) is True
        assert security.is_sensitive_bash_command(f"cat {target}") is not None

    def test_the_pinned_home_reaches_the_fast_path_regex_too(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """`_SENSITIVE_RE` is a process global with no TTL and no invalidation, so
        a matcher call from ANY earlier test in this worker freezes it on the real
        runner home and a later `setenv("HOME", ...)` never reaches pass 1. On
        Linux the target-set passes cover for that; on the Windows shard they did
        not, which is why only Windows failed. Pins that `_pin_home` drops the
        global: prime it first, exactly as a neighbouring test would."""
        security.is_sensitive_bash_command("cat /etc/hostname")  # prime pass 1
        assert security._SENSITIVE_RE is not None

        real_home = _pin_home(monkeypatch, tmp_path / "real-home")

        assert security._SENSITIVE_RE is None, "_pin_home must drop the primed regex"
        target = str(real_home / ".aws" / "credentials")
        assert security.is_sensitive_bash_command(f"cat {target}") is not None
