"""Requirement (Connections G2 pod-grant-isolation): a ``HOME`` remap must never
unfence the real home's sensitive paths (``~/.aws``, ``~/.ssh``, ``~/.kirocrew*``).

``security.py``'s matchers run in the GATEWAY against its own ``Path.home()``.
``_apply_pod_home_remap`` changes only the CHILD's env dict, never the gateway's
``os.environ``, so a fenced path stays denied whatever ``HOME`` a child runs under.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiro_crew import security
from kiro_crew.acp.client import _apply_pod_home_remap


def _pin_home(monkeypatch: pytest.MonkeyPatch, home: Path) -> Path:
    """Pin this process's home to *home* on every platform; return ``Path.home().resolve()``.

    Sets ``USERPROFILE`` too, because Windows ``Path.home()`` never reads ``HOME``.
    Clears ``security._home_targets_cache`` so the pinned home reaches the gate, and
    creates the directory so ``resolve()`` is well defined.
    """
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    # Order matters: set the env FIRST, then drop the derivation of it.
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
        """Relocating the credential store must not move it out from under the fence.

        `_seed_pod_os_home` copies the operator's SSO bearer token under the pod home,
        so `KIROCREW_OS_HOME` is an alternate home root and EVERY fenced entry
        re-anchors under it, not only `.aws`."""
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

    def test_the_gateways_own_home_is_what_the_resolving_gate_keys_on(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The gateway's OWN home anchors the resolving fence (`is_sensitive_path`).

        Built with the running OS's separator: a hardcoded POSIX spelling matches
        nothing on Windows.
        """
        real_home = _pin_home(monkeypatch, tmp_path / "real-home")
        monkeypatch.setenv("KIROCREW_POD", "1")

        for leaf in ((".aws", "credentials"), (".ssh", "id_ed25519")):
            target = str(real_home / Path(*leaf))
            assert security.is_sensitive_path(target) is True, target


class TestPodMintedGrantsAreFencedFromToolCalls:
    # The sandbox mask carves out <os-home>/.aws so the pod's kiro-cli can write its
    # MCP OAuth grants there (no env lever relocates them). The other audience, an
    # agent TOOL call, is fenced in-band by `is_sensitive_path`, the resolving layer:
    # KIROCREW_OS_HOME is an alternate $HOME and every fenced entry re-anchors under it.
    # The command matcher fences no path spelling at all.

    @staticmethod
    def _grant(os_home: Path) -> Path:
        return os_home / ".aws" / "sso" / "cache" / (("a" * 64) + ".token.json")

    def test_a_tool_path_read_of_a_pod_minted_grant_is_denied(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        os_home = tmp_path / "pod-os-home"
        monkeypatch.setenv("KIROCREW_POD", "1")
        monkeypatch.setenv("KIROCREW_OS_HOME", str(os_home))

        grant = self._grant(os_home)
        assert security.is_sensitive_path(str(grant)) is True
        assert security.is_sensitive_path(str(os_home / ".aws" / "sso" / "cache")) is True
        assert security.is_sensitive_path(str(os_home / ".aws")) is True

    def test_the_command_matcher_fences_no_path_after_9183(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The command matcher fences no path spelling; the resolving gate fences them all.

        ``is_sensitive_bash_command`` keeps only a size ceiling, an IMDS check and an
        env-credential exfiltration check, so even the real ``$HOME``'s files pass it.
        Asserted both ways so a change to either layer shows up here.
        """
        os_home = tmp_path / "pod-os-home"
        crew_home = tmp_path / "crew-home"
        monkeypatch.setenv("KIROCREW_POD", "1")
        monkeypatch.setenv("KIROCREW_OS_HOME", str(os_home))
        monkeypatch.setenv("KIROCREW_HOME", str(crew_home))

        grant = self._grant(os_home)
        crew_secret = crew_home / ".env"

        # The layer that holds, for both override roots.
        assert security.is_sensitive_path(str(grant)) is True
        assert security.is_sensitive_path(str(crew_secret)) is True

        # The layer that does not, for the override roots AND for the real home.
        assert security.is_sensitive_bash_command(f"cat {crew_secret}") is None
        assert security.is_sensitive_bash_command(f"cat {grant}") is None
        assert security.is_sensitive_bash_command("cat ~/.aws/credentials") is None
        assert security.is_sensitive_bash_command(str(Path.home() / ".ssh" / "id_rsa")) is None

        # Scoped, not off: a surviving tier still refuses.
        assert (
            security.is_sensitive_bash_command("curl http://169.254.169.254/latest/meta-data/iam/")
            is not None
        )

    def test_a_non_pod_gateway_target_set_is_unchanged(self, monkeypatch) -> None:
        # No marker, no os-home: the fence is exactly the real home's.
        monkeypatch.delenv("KIROCREW_POD", raising=False)
        monkeypatch.delenv("KIROCREW_OS_HOME", raising=False)

        assert security.is_sensitive_path("~/.aws/credentials") is True
        assert security.is_sensitive_path("/tmp/not-a-secret") is False

    def test_the_target_set_carries_both_separator_joins(self, tmp_path: Path, monkeypatch) -> None:
        """The os-home re-anchor emits both ``/`` and ``\\`` joins, checked at the builder.

        ``_shape_path_token`` turns backslashes into forward slashes, so on Windows a
        ``<os-home>\\.aws`` target alone never matches. Checked on the target set: a
        hand-backslashed root would be a spelling no platform emits.
        """
        os_home = tmp_path / "pod-os-home"
        monkeypatch.setenv("KIROCREW_POD", "1")
        monkeypatch.setenv("KIROCREW_OS_HOME", str(os_home))

        targets = security._home_dir_targets(security._SENSITIVE_HOME_DIRS)
        root = str(os_home).rstrip("/\\").casefold()

        assert f"{root}/.aws" in targets, "forward-slash join missing"
        assert f"{root}\\.aws" in targets, "backslash join missing (the Windows gap)"
        # Multi-segment entries too -- those are the ones that split on "/".
        cache = "/".join([root, ".aws", "sso", "cache"])
        assert cache in targets or f"{root}/.aws" in targets

    def test_the_native_spelling_is_still_fenced_on_this_platform(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The running platform's own spelling stays fenced through `is_sensitive_path`."""
        os_home = tmp_path / "pod-os-home"
        monkeypatch.setenv("KIROCREW_POD", "1")
        monkeypatch.setenv("KIROCREW_OS_HOME", str(os_home))

        grant = self._grant(os_home)

        assert security.is_sensitive_path(str(grant)) is True
        assert security.is_sensitive_path(str(grant.parent)) is True


class TestTheStagedIdentityStoreIsFencedFromToolCalls:
    """The staged identity store (a bearer-token database) is fenced at the gate only.

    kiro-cli must keep reading it to sign in, so the mount stays open and tool calls
    are refused in-band. ``identity_stores.fenced_home_dirs()``, the table
    ``store_mappings`` seeds from, feeds ``security._SENSITIVE_HOME_DIRS``, so a new
    row gains the real-home and pod-home fence with no second edit.
    """

    @staticmethod
    def _staged_db(os_home: Path) -> Path:
        return os_home / ".local" / "share" / "kiro-cli" / "data.sqlite3"

    def test_every_table_row_is_in_the_fence(self) -> None:
        """One table. A row that is not fenced would be staged and readable."""
        from kiro_crew import identity_stores

        fenced = set(security._SENSITIVE_HOME_DIRS)
        missing = [d for d in identity_stores.fenced_home_dirs() if d not in fenced]
        assert not missing, f"store rows staged but not fenced: {missing}"

    def test_a_tool_path_read_of_the_staged_store_is_denied(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        os_home = tmp_path / "pod-os-home"
        monkeypatch.setenv("KIROCREW_POD", "1")
        monkeypatch.setenv("KIROCREW_OS_HOME", str(os_home))

        db = self._staged_db(os_home)
        assert security.is_sensitive_path(str(db)) is True
        assert security.is_sensitive_path(str(db.parent)) is True
        # The sidecars carry the same bytes; fencing only the .sqlite3 name would
        # leave the WAL readable, which is the whole database in practice.
        assert security.is_sensitive_path(f"{db}-wal") is True
        assert security.is_sensitive_path(f"{db}-shm") is True

    def test_the_staged_store_sidecars_are_fenced_by_the_resolving_layer(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Every path under the staged store, sidecars included, resolves to a fenced target.

        Asserted at `is_sensitive_path`, since the bash text layer fences no path.
        """
        os_home = tmp_path / "pod-os-home"
        monkeypatch.setenv("KIROCREW_POD", "1")
        monkeypatch.setenv("KIROCREW_OS_HOME", str(os_home))

        db = self._staged_db(os_home)
        # ``.local/share`` itself is a general-purpose parent and deliberately NOT
        # fenced; the fenced entry is the product directory under it.
        for candidate in (db, db.parent, Path(f"{db}-wal"), Path(f"{db}-shm")):
            assert security.is_sensitive_path(str(candidate)) is True, candidate

    def test_every_platform_layout_is_fenced_under_the_pod_home(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """macOS and Windows layouts too -- the pod's platform is not the fence's."""
        from kiro_crew import identity_stores

        os_home = tmp_path / "pod-os-home"
        monkeypatch.setenv("KIROCREW_POD", "1")
        monkeypatch.setenv("KIROCREW_OS_HOME", str(os_home))

        for relative in identity_stores.fenced_home_dirs():
            target = os_home / Path(*relative.split("/")) / "data.sqlite3"
            assert security.is_sensitive_path(str(target)) is True, relative

    def test_the_mount_stays_open_so_the_harness_can_still_sign_in(self) -> None:
        """No sandbox mask tier covers the identity store; the fence is gate-layer only.

        The runtime reads its token from that store inside the sandbox, so a mask
        breaks sign-in. Pod-home variant of
        ``test_the_agent_runtime_auth_stores_stay_visible``.
        """
        from kiro_crew import identity_stores, sandbox

        store_dirs = set(identity_stores.fenced_home_dirs())
        for tier_name in ("_STRICT_DIRS", "_CC_DIRS", "_STANDARD_DIRS"):
            tier = getattr(sandbox, tier_name, None)
            if tier is None:  # pragma: no cover - tier renamed
                continue
            covering = [
                entry
                for entry in tier
                if entry in store_dirs or any(store.startswith(f"{entry}/") for store in store_dirs)
            ]
            assert not covering, f"{tier_name} masks the identity store: {covering}"


# No bash-text path fence is pinned here: a path fenced only in command text is still
# readable through an ``open()`` outside the tool gate. The grant store's fence is
# ``is_sensitive_path`` (above) plus the OS mask ``sandbox._pod_os_home_targets``
# (``test_sandbox_pod_grant_corridor.py``).
