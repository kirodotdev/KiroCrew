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

    **The HOME-derived target cache in ``security`` is reset**, and that is what
    makes a pinned home actually reach the gate rather than only the environment.
    ``_home_targets_cache`` is TTL-bounded and keyed on the resolved roots, so it
    does pick up a new home -- and it is the cache the rest of
    ``test_security.py`` already clears by hand, which is the established seam this
    follows.

    Nothing in production depends on the reset -- the gateway's own HOME does not
    move under it, and ``_apply_pod_home_remap`` changes only a CHILD's environment
    -- so this is a test-pinning helper, not a gate fix.

    Also creates the directory, so ``resolve()`` is well defined on every platform,
    and returns the resolved value because the target set is anchored on
    ``Path.home().resolve()``.
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

    def test_the_gateways_own_home_is_what_the_resolving_gate_keys_on(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The pinned home reaches the gate that still fences paths.

        ``is_sensitive_bash_command`` fences no path spelling (see
        ``test_the_command_matcher_fences_no_path_after_9183``); what remains is
        the property that still has a mechanism behind it: the gateway's OWN home
        anchors the resolving fence, whatever ``HOME`` a spawned child was handed.

        Built with the running OS's separator: a hardcoded POSIX spelling matches
        nothing on Windows, where every candidate form the gate derives is
        backslash-separated.
        """
        real_home = _pin_home(monkeypatch, tmp_path / "real-home")
        monkeypatch.setenv("KIROCREW_POD", "1")

        for leaf in ((".aws", "credentials"), (".ssh", "id_ed25519")):
            target = str(real_home / Path(*leaf))
            assert security.is_sensitive_path(target) is True, target


class TestPodMintedGrantsAreFencedFromToolCalls:
    # The two-audience split, pinned.
    #
    # The sandbox mask deliberately CARVES OUT <os-home>/.aws so the pod's own
    # kiro-cli can read and WRITE its MCP OAuth grants there -- there is no env
    # lever that relocates them, and masking the tree empty discards every grant
    # the pod mints. That leaves one audience to fence: an agent TOOL call. It is
    # fenced HERE, in-band, because KIROCREW_OS_HOME is anchored as an alternate
    # $HOME and every fenced entry is re-anchored under it.
    #
    # Consequence worth stating: a pod's posture is strictly NARROWER than the
    # non-pod baseline, where the standard tier leaves the REAL ~/.aws visible to
    # tools (sandbox.py's _STANDARD_DIRS omits .aws so credential_process can
    # reach Bedrock auth). A pod denies the pod-local tree at the gate on top of
    # that. Red-first on the target set and on both separators.
    #
    # WHICH gate: `is_sensitive_path`, the layer that RESOLVES. The command
    # matcher fences NO path spelling at all -- the real $HOME's credential files
    # included, not merely the relocated roots. Pinned by
    # test_the_command_matcher_fences_no_path_after_9183 below.

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
        """The text layer is out of the path business -- universally, not per-root.

        ``is_sensitive_bash_command`` keeps only a size ceiling, an IMDS check
        and an environment-credential exfiltration check. It does not tokenize a
        command and route each token through ``is_sensitive_path``, so it fences
        no path spelling -- neither a relocated root nor the real ``$HOME``.

        The parity framing is therefore retired rather than restated: measured, the
        matcher allows EVERY path spelling, the real home's own credential files
        included, so the pod tree is not a special case there. Asserted in both
        directions -- the resolving gate still refuses each of these -- so losing
        the resolving fence fails loudly and reinstating a text-layer path matcher
        shows up here rather than silently.
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

        # The layer that does not -- for the override roots AND for the real home,
        # which is the half the retired parity claim got wrong.
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
        """The Windows fix, pinned at the BUILDER where it is platform-independent.

        Joining the os-home targets with the RUNNING OS's separator only is not
        enough on the bash surface: ``_shape_path_token`` normalises a token's
        backslashes to forward slashes before comparing, so on Windows a target
        of ``<os-home>\\.aws`` never compares equal to the candidate form
        ``<os-home>/.aws``. The re-anchor therefore emits BOTH joins.

        Asserted on the target set rather than through a hand-backslashed absolute
        path: the ROOT's own separators are whatever the platform produced, and
        rewriting those manufactures a spelling no platform emits (this suite's
        convention -- build targets with the running OS's separator). What the fix
        owns is the join between the root and the fenced entry, which is what this
        checks.
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
        """Widening must not disturb the form that already worked.

        The bash text layer covers no relocated home (see
        ``test_the_bash_text_layer_covers_no_relocated_home_after_9089``). What
        the both-separator widening owns is the TARGET SET, and this is its
        running-platform spelling reached through the caller the resolving fence
        serves.
        """
        os_home = tmp_path / "pod-os-home"
        monkeypatch.setenv("KIROCREW_POD", "1")
        monkeypatch.setenv("KIROCREW_OS_HOME", str(os_home))

        grant = self._grant(os_home)

        assert security.is_sensitive_path(str(grant)) is True
        assert security.is_sensitive_path(str(grant.parent)) is True


class TestTheStagedIdentityStoreIsFencedFromToolCalls:
    """The second two-audience tree, same split as the grant store.

    ``_seed_pod_os_home`` stages the agent runtime's identity store into the pod
    home so kiro-cli can sign in -- and that store is a BEARER-TOKEN DATABASE. The
    harness must keep reading it (the mount stays open; masking it empty is what
    broke sign-in two rounds ago), so the fence is gate-layer only: an agent TOOL
    call naming any path under it is refused in-band.

    Derived, not enumerated. ``identity_stores.fenced_home_dirs()`` -- the SAME
    table ``store_mappings`` seeds from -- is spliced into
    ``security._SENSITIVE_HOME_DIRS``, and ``_home_dir_targets_uncached``
    re-anchors EVERY ``home_dirs`` entry under ``KIROCREW_OS_HOME``. So a store row
    added to that table gains the real-home fence AND the pod-home fence with no
    second edit, which is the property these tests pin.
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
        """Every spelling a tool can OPEN, including the verbs that read a sqlite db.

        The bash text layer covers no relocated home (see the grant store's
        parity test), so the property is asserted where it lives: every path
        under the staged store resolves to a fenced target, whichever verb names
        it, so the refusal does not depend on a command spelling being
        recognised.
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
        """Gate-layer ONLY -- the launcher masks are unchanged.

        The repo already asserts this for the REAL home
        (``test_the_agent_runtime_auth_stores_stay_visible`` in the sandbox-mask
        suite): the runtime resolves its own access token from that store while
        running inside the sandbox, so a mask entry covering it breaks sign-in
        rather than protecting anything. This is the pod-home variant of the same
        assertion -- the fence added above must not have leaked into a mask tier.
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


# This file pins no Windows-native bash-tokenization pass. A path fenced only in
# command TEXT is still readable through an ``open()`` that never routes through
# the tool gate, so the bash-text normalizer helpers
# (``_windows_native_path_tokens`` / ``_check_sensitive_via_normalizer`` /
# ``_win_anchor_roots``) do not exist; ``test_security.py``'s
# ``TestTraversalSimulationIsGone`` asserts they are ABSENT by name.
#
# The pod grant store's fence is carried by two layers, both pinned above:
# ``is_sensitive_path`` on every
# resolved path (``KIROCREW_OS_HOME`` re-anchors every ``home_dirs`` entry in
# ``security._home_dir_targets_uncached``), and the OS mask
# (``sandbox._pod_os_home_targets``), covered by
# ``test_sandbox_pod_grant_corridor.py``.
