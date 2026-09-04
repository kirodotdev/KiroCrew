"""``acp.client._apply_pod_home_remap`` -- HOME remap for a pod-spawned kiro-cli
child, applied identically at both ACP spawn sites (``AcpClient._spawn`` and
``AcpRuntime._spawn_admitted``).

Regression context: ``pod.runtime.build_pod_env`` deliberately keeps the pod
GATEWAY's own ``HOME`` unchanged (isolating ``KIROCREW_HOME``/``KIRO_HOME``
only), so a pod-spawned kiro-cli child inherited that same real ``$HOME`` and
wrote its MCP OAuth grant artifacts there -- a REAL, durable, machine-level
credential that survived ``pod down``. This function is the fix's second half:
``mcp_grant.kiro_oauth_cache_dir()`` (pinned in ``test_mcp_grant.py``) makes
the pod's OWN reads resolve the pod's tree; this makes kiro-cli's OWN WRITES
land there too, by remapping the spawned child's ``HOME``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiro_crew.acp.client import _apply_pod_home_remap


def _base_pod_env(tmp_path: Path) -> dict[str, str]:
    """A KIROCREW_POD=1 env with KIROCREW_OS_HOME set, as build_pod_env emits."""
    return {
        "HOME": str(tmp_path / "real-home"),
        "PATH": "/usr/bin",
        "KIROCREW_POD": "1",
        "KIROCREW_OS_HOME": str(tmp_path / "pod-os-home"),
    }


class TestAppliesOnlyInsideAPodForKiroCli:
    def test_remaps_home_for_a_pod_kiro_cli_spawn(self, tmp_path: Path) -> None:
        env = _base_pod_env(tmp_path)
        out = _apply_pod_home_remap(env, pod_home_remap=True)
        assert out["HOME"] == str(tmp_path / "pod-os-home")
        assert out is env  # mutates in place, per the docstring's contract

    def test_userprofile_moves_with_home(self, tmp_path: Path) -> None:
        """Windows spelling of the same concept — the two must never disagree."""
        env = _base_pod_env(tmp_path)
        out = _apply_pod_home_remap(env, pod_home_remap=True)
        assert out["USERPROFILE"] == out["HOME"] == str(tmp_path / "pod-os-home")

    def test_noop_outside_a_pod(self, tmp_path: Path) -> None:
        """No KIROCREW_POD marker -- an ordinary, non-pod ACP spawn on the
        operator's own machine must never have its HOME touched."""
        env = _base_pod_env(tmp_path)
        del env["KIROCREW_POD"]
        real_home = env["HOME"]
        out = _apply_pod_home_remap(env, pod_home_remap=True)
        assert out["HOME"] == real_home
        assert "USERPROFILE" not in out

    def test_noop_for_a_non_kiro_harness_inside_a_pod(self, tmp_path: Path) -> None:
        """A Claude/KAS child inside a pod is a different harness with its own
        credential store; it must not be told a Kiro-specific env story."""
        env = _base_pod_env(tmp_path)
        real_home = env["HOME"]
        out = _apply_pod_home_remap(env, pod_home_remap=False)
        assert out["HOME"] == real_home
        assert "USERPROFILE" not in out

    def test_noop_when_the_pod_marker_is_set_with_no_os_home(self, tmp_path: Path) -> None:
        """A malformed pod env (marker without the directory to point at) must
        leave HOME as-is rather than remapping to an empty string -- breaking
        every filesystem-dependent tool in the spawned child would be strictly
        worse than the status quo it degrades to."""
        env = _base_pod_env(tmp_path)
        del env["KIROCREW_OS_HOME"]
        real_home = env["HOME"]
        out = _apply_pod_home_remap(env, pod_home_remap=True)
        assert out["HOME"] == real_home

    @pytest.mark.parametrize("marker", ["false", "0", "no", "off", "", "true", "2", " 1"])
    def test_only_the_exact_marker_value_one_remaps(self, tmp_path: Path, marker: str) -> None:
        """Every non-empty string is truthy in Python, so a truthiness test on
        KIROCREW_POD remapped HOME for a child whose marker explicitly said it
        was NOT in a pod. Only the value build_pod_env actually writes ("1")
        may move a credential store."""
        env = _base_pod_env(tmp_path)
        env["KIROCREW_POD"] = marker
        real_home = env["HOME"]
        out = _apply_pod_home_remap(env, pod_home_remap=True)
        assert out["HOME"] == real_home, f"marker {marker!r} must not remap HOME"
        assert "AWS_CONFIG_FILE" not in out


class TestTheCapabilitySetIsItsOwnDecision:
    """The remap is gated on ACP_BACKENDS_POD_HOME_REMAP, never on the
    internal-sandbox set: "carries its own OS sandbox" and "relocating HOME
    moves its credential store" are different questions, and conflating them
    would hand a harness added for sandbox reasons credential-relocation
    semantics it never opted into (harness-parity H6)."""

    def test_the_set_exists_and_is_a_subset_of_known_backends(self) -> None:
        from kiro_crew.acp_backends import (
            ACP_BACKENDS_KNOWN,
            ACP_BACKENDS_POD_HOME_REMAP,
        )

        assert ACP_BACKENDS_POD_HOME_REMAP <= ACP_BACKENDS_KNOWN

    def test_membership_is_positive_and_kiro_only(self) -> None:
        from kiro_crew.acp_backends import (
            ACP_BACKEND_CLAUDE,
            ACP_BACKEND_KAS,
            ACP_BACKEND_KIRO,
            ACP_BACKENDS_POD_HOME_REMAP,
        )

        assert ACP_BACKEND_KIRO in ACP_BACKENDS_POD_HOME_REMAP
        assert ACP_BACKEND_CLAUDE not in ACP_BACKENDS_POD_HOME_REMAP
        assert ACP_BACKEND_KAS not in ACP_BACKENDS_POD_HOME_REMAP

    def test_neither_spawn_site_gates_the_remap_on_the_sandbox_set(self) -> None:
        """The regression: reusing ACP_BACKENDS_INTERNAL_SANDBOX for this gate
        is the conflation, so neither call site may name it for the remap."""
        import inspect

        from kiro_crew.acp import client as client_mod
        from kiro_crew.acp import runtime as runtime_mod

        for source in (
            inspect.getsource(client_mod.AcpClient._spawn),
            inspect.getsource(runtime_mod.AcpRuntime._spawn_admitted),
        ):
            remap_call = source.split("_apply_pod_home_remap(")[1].split(")")[0]
            assert "ACP_BACKENDS_POD_HOME_REMAP" in remap_call
            assert "ACP_BACKENDS_INTERNAL_SANDBOX" not in remap_call


class TestAwsCredentialPointersAreNotExportedIntoThePod:
    """An earlier revision pinned ``AWS_CONFIG_FILE`` /
    ``AWS_SHARED_CREDENTIALS_FILE`` back at the real home so a pod agent turn
    could still reach the operator's file profiles after HOME moved. Naming
    those files in the child environment IS the leak: the deny matchers work on
    command text with no variable expansion, so the export is a working alias for
    a path the sensitive-path fence refuses by name, and the alias is retrievable
    through an unbounded set of spellings (``$VAR``, ``os.environ['VAR']``,
    ``$(printenv VAR)``, ``eval``, indirect expansion, a helper script). The
    alias is deleted at its source instead of matched spelling by spelling.

    Posture recorded here, corrected in round 9: an ACP agent turn inside a pod
    has NO inherited AWS credentials on any path. File credentials do not resolve
    (the pointer exports are gone), and environment credentials do not reach the
    turn either -- ``sandbox.scrub_agent_subprocess_env`` scrubs the
    ``AWS_SECRET`` and ``AWS_SESSION`` prefixes from every Kiro/ACP child. An
    earlier docstring here said env-var credentials were unaffected; that
    confused the pod GATEWAY's environment (where ``build_pod_env`` does keep
    ``AWS_*``) with the ACP child's, which is scrubbed after it."""

    def test_does_not_export_aws_config_file(self, tmp_path: Path) -> None:
        env = _base_pod_env(tmp_path)
        out = _apply_pod_home_remap(env, pod_home_remap=True)
        assert "AWS_CONFIG_FILE" not in out

    def test_does_not_export_aws_shared_credentials_file(self, tmp_path: Path) -> None:
        env = _base_pod_env(tmp_path)
        out = _apply_pod_home_remap(env, pod_home_remap=True)
        assert "AWS_SHARED_CREDENTIALS_FILE" not in out

    def test_no_real_home_path_leaks_into_the_child_env_at_all(self, tmp_path: Path) -> None:
        """The point of the removal: no value handed to the child may name the
        real home's tree. Catches a re-introduction under any variable name."""
        env = _base_pod_env(tmp_path)
        out = _apply_pod_home_remap(env, pod_home_remap=True)
        real_aws = str(tmp_path / "real-home" / ".aws")
        assert not [k for k, v in out.items() if isinstance(v, str) and real_aws in v]

    def test_env_var_credentials_still_survive_the_remap(self, tmp_path: Path) -> None:
        """``build_pod_env`` keeps ``AWS_*`` on purpose (its ``_TOKEN`` scrub
        excludes the ``AWS_`` prefix). The remap must not undo that -- this is
        the path that replaces file profiles inside a pod."""
        env = _base_pod_env(tmp_path)
        env["AWS_ACCESS_KEY_ID"] = "AKIAEXAMPLE"
        env["AWS_SESSION_TOKEN"] = "sts-temp"
        out = _apply_pod_home_remap(env, pod_home_remap=True)
        assert out["AWS_ACCESS_KEY_ID"] == "AKIAEXAMPLE"
        assert out["AWS_SESSION_TOKEN"] == "sts-temp"

    def test_an_operator_set_pointer_is_removed_too(self, tmp_path: Path) -> None:
        """The INHERITED pointer, which is the half the first round missed.

        This test previously asserted the opposite -- that an operator-set pointer
        is "neither created nor stripped here", on the reasoning that it names the
        operator's own file rather than an alias this function manufactured. That
        reasoning is wrong about WHO reads it: the value reaches the pod's AGENT,
        and the agent dereferences it. ``build_pod_env`` keeps ``AWS_*`` on purpose,
        so an absolute host pointer survives into a child whose ``HOME`` has moved
        and whose ``.aws/config`` / ``.aws/credentials`` / ``.aws/cli`` are
        empty-masked under the new home -- the pointer walks around the relocation
        and the operator's real credentials are disclosed. Whose file it is does not
        change what following it yields.

        Recorded because a test asserting a vulnerability is how this shipped past
        one review round: the first fix removed the manufactured export, this test
        pinned the inherited one in place, and the suite stayed green.
        """
        env = _base_pod_env(tmp_path)
        env["AWS_CONFIG_FILE"] = "/custom/aws-config"
        env["AWS_SHARED_CREDENTIALS_FILE"] = "/custom/aws-creds"
        out = _apply_pod_home_remap(env, pod_home_remap=True)
        assert "AWS_CONFIG_FILE" not in out
        assert "AWS_SHARED_CREDENTIALS_FILE" not in out

    def test_a_plain_text_container_bearer_does_not_reach_the_child(self, tmp_path: Path) -> None:
        """``AWS_CONTAINER_AUTHORIZATION_TOKEN`` -- the bearer IN the value.

        RED-FIRST for the twin the first pass missed. The set covered the ``_FILE``
        spelling and not the bare one, and the bare one is strictly worse: ``_FILE``
        names a file the agent still has to open, while this variable IS the token,
        in plain text, readable straight out of the child's environment. Per the SDK
        reference it is the documented alternative used whenever ``_FILE`` is unset,
        so an ECS/EKS/SnapStart host that sets it -- and ``build_pod_env`` keeps
        ``AWS_*`` on purpose -- handed the pod's agent a live bearer for the
        credential endpoint.

        A real-shaped value rather than a path, because the previous parametrized
        case fed every pointer a filesystem path and a value-carrying variable would
        have looked plausible under that fixture even while leaking.
        """
        env = _base_pod_env(tmp_path)
        env["AWS_CONTAINER_CREDENTIALS_FULL_URI"] = "http://localhost/get-credentials"
        env["AWS_CONTAINER_AUTHORIZATION_TOKEN"] = "Basic abcd"

        out = _apply_pod_home_remap(env, pod_home_remap=True)

        assert "AWS_CONTAINER_AUTHORIZATION_TOKEN" not in out
        assert "Basic abcd" not in out.values()
        # The endpoint goes too -- a bearer with no URL and a URL with no bearer are
        # both halves of one provider, and leaving either is a partial fix.
        assert "AWS_CONTAINER_CREDENTIALS_FULL_URI" not in out

    def test_the_whole_container_credential_family_is_covered(self) -> None:
        """The family is CLOSED and enumerable, so pin it by name.

        The SDK reference documents exactly four variables for the container
        credential provider, in two alternative pairs (relative/full URL, and
        token/token-file). The finding that produced the test above was one member
        of one pair being absent, which a per-variable test cannot catch on its own
        -- it only checks the names someone already thought to list. This asserts
        the set relation instead, so a fifth member added to the provider upstream
        fails here rather than silently going unscrubbed.
        """
        from kiro_crew.acp.client import CREDENTIAL_POINTER_ENV_VARS

        family = {
            "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
            "AWS_CONTAINER_CREDENTIALS_FULL_URI",
            "AWS_CONTAINER_AUTHORIZATION_TOKEN",
            "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE",
        }

        missing = family - set(CREDENTIAL_POINTER_ENV_VARS)
        assert not missing, f"container-credential vars not scrubbed: {sorted(missing)}"

    @pytest.mark.parametrize(
        "pointer",
        [
            "AWS_CONFIG_FILE",
            "AWS_SHARED_CREDENTIALS_FILE",
            "AWS_WEB_IDENTITY_TOKEN_FILE",
            "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
            "AWS_CONTAINER_CREDENTIALS_FULL_URI",
            "AWS_CONTAINER_AUTHORIZATION_TOKEN",
            "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE",
        ],
    )
    def test_every_pointer_in_the_family_is_removed(self, tmp_path: Path, pointer: str) -> None:
        """One case per variable, so a partial fix cannot pass.

        The family is the CLASS, not just the two names the finding cited: each of
        these is a location the SDK chain follows to obtain credentials, and the
        two container ones are URLs, which no filesystem mask or ``HOME`` remap can
        reach at all -- removal from the env is the only control that touches them.
        """
        env = _base_pod_env(tmp_path)
        env[pointer] = str(tmp_path / "real-home" / ".aws" / "host-pointer")
        out = _apply_pod_home_remap(env, pod_home_remap=True)
        assert pointer not in out

    def test_the_pointer_set_and_the_store_roots_stay_disjoint(self) -> None:
        """Two sets, two reasons, no overlap -- the drift check on the split.

        Store roots are DERIVED from ``identity_stores.IDENTITY_STORE_ROOTS``;
        pointers are explicit because no table enumerates them. If a name ever
        appears in both, one of the two rationales has stopped being true and the
        scrub needs re-deriving rather than a third list.
        """
        from kiro_crew.acp.client import (
            CREDENTIAL_POINTER_ENV_VARS,
            IDENTITY_STORE_ROOT_ENV_VARS,
        )

        assert not (CREDENTIAL_POINTER_ENV_VARS & IDENTITY_STORE_ROOT_ENV_VARS)

    def test_the_global_secret_scrub_still_owns_the_secret_carrying_vars(self) -> None:
        """One philosophy, two scopes -- pinned so the lists cannot converge.

        ``sandbox.scrub_agent_subprocess_env`` drops the variables that CARRY a
        secret, on EVERY agent spawn. This set drops the ones that POINT at
        credentials, and only where the pointed-at tree has been relocated. Adding
        pointers to the global scrub would break the non-pod path the standard tier
        deliberately supports (the AWS CLI and ``credential_process`` reading the
        real ``~/.aws``), so the boundary is asserted rather than assumed.
        """
        from kiro_crew.acp.client import CREDENTIAL_POINTER_ENV_VARS
        from kiro_crew.sandbox import scrub_agent_subprocess_env

        probe = {name: "/host/pointer" for name in CREDENTIAL_POINTER_ENV_VARS}
        probe["AWS_SECRET_ACCESS_KEY"] = "shhh"
        probe["AWS_SESSION_TOKEN"] = "shhh"
        scrubbed = scrub_agent_subprocess_env(dict(probe))

        # The global scrub owns the secrets ...
        assert "AWS_SECRET_ACCESS_KEY" not in scrubbed
        assert "AWS_SESSION_TOKEN" not in scrubbed
        # ... and deliberately does NOT own the pointers, which is why the pod
        # remap has to remove them itself.
        assert set(CREDENTIAL_POINTER_ENV_VARS) <= set(scrubbed)

    def test_a_non_pod_env_is_byte_identical_through_the_remap(self, tmp_path: Path) -> None:
        """No pod marker, no change -- every pointer survives untouched.

        The non-pod baseline is a supported configuration, not an oversight: the
        standard sandbox tier leaves the real ``~/.aws`` visible so file profiles
        and ``credential_process`` keep working. This asserts the WHOLE mapping is
        unchanged, not merely that a couple of keys survive.
        """
        env = _base_pod_env(tmp_path)
        del env["KIROCREW_POD"]
        for pointer in (
            "AWS_CONFIG_FILE",
            "AWS_SHARED_CREDENTIALS_FILE",
            "AWS_WEB_IDENTITY_TOKEN_FILE",
            "AWS_CONTAINER_CREDENTIALS_FULL_URI",
        ):
            env[pointer] = f"/host/{pointer}"
        before = dict(env)

        out = _apply_pod_home_remap(env, pod_home_remap=True)

        assert out == before

    def test_home_and_userprofile_still_move_together(self, tmp_path: Path) -> None:
        """Removing the pointers must not disturb the remap's actual job."""
        env = _base_pod_env(tmp_path)
        out = _apply_pod_home_remap(env, pod_home_remap=True)
        assert out["HOME"] == str(tmp_path / "pod-os-home")
        assert out["USERPROFILE"] == str(tmp_path / "pod-os-home")


class TestBothSpawnTransportsApplyItIdentically:
    """AcpClient and AcpRuntime must reach the same function with the same
    harness-parity-correct classification -- never a hand-rolled copy that
    could drift between the two transports."""

    def test_acp_client_spawn_calls_the_shared_helper(self) -> None:
        import inspect

        from kiro_crew.acp import client as client_mod

        source = inspect.getsource(client_mod.AcpClient._spawn)
        assert "_apply_pod_home_remap(" in source
        assert "ACP_BACKENDS_POD_HOME_REMAP" in source

    def test_acp_runtime_spawn_calls_the_same_shared_helper(self) -> None:
        import inspect

        from kiro_crew.acp import runtime as runtime_mod

        source = inspect.getsource(runtime_mod.AcpRuntime._spawn_admitted)
        assert "_apply_pod_home_remap(" in source
        assert "ACP_BACKENDS_POD_HOME_REMAP" in source

    def test_runtime_imports_the_client_defined_function_rather_than_a_copy(self) -> None:
        """Import identity, not merely name equality — a copy-pasted function
        of the same name would pass a naive check while drifting silently."""
        from kiro_crew.acp import client as client_mod
        from kiro_crew.acp import runtime as runtime_mod

        assert runtime_mod._apply_pod_home_remap is client_mod._apply_pod_home_remap


class TestIdentityStoreRootOverridesAreScrubbed:
    """A remapped HOME is not enough: the identity store honours a ROOT OVERRIDE.

    ``identity_stores`` resolves each store from ``StoreRoot.env_var`` before
    falling back to ``$HOME``, so an inherited ``XDG_DATA_HOME`` (or the Windows
    ``LOCALAPPDATA`` / ``APPDATA``) pointing at a host path made a pod's kiro-cli
    read AND WRITE the HOST identity store -- sign-in state that survives
    ``pod down``, which is the escape the remap exists to prevent.
    """

    def test_a_hostile_data_root_override_is_removed(self) -> None:
        """Red-first: the override must be GONE, so the default resolves in-pod."""
        from kiro_crew.acp.client import IDENTITY_STORE_ROOT_ENV_VARS, _apply_pod_home_remap

        env = {"KIROCREW_POD": "1", "KIROCREW_OS_HOME": "/pods/p1/os-home"}
        for var in IDENTITY_STORE_ROOT_ENV_VARS:
            env[var] = "/home/user/.local/share"

        out = _apply_pod_home_remap(dict(env), pod_home_remap=True)

        assert out["HOME"] == "/pods/p1/os-home"
        for var in IDENTITY_STORE_ROOT_ENV_VARS:
            assert var not in out, f"{var} still points at the host identity store"

    def test_the_scrubbed_set_is_derived_from_the_store_table(self) -> None:
        """Pinned to its source so a new platform/product row cannot be missed."""
        from kiro_crew.acp.client import IDENTITY_STORE_ROOT_ENV_VARS
        from kiro_crew.identity_stores import IDENTITY_STORE_ROOTS

        assert IDENTITY_STORE_ROOT_ENV_VARS == {
            root.env_var for root in IDENTITY_STORE_ROOTS if root.env_var
        }
        assert "XDG_DATA_HOME" in IDENTITY_STORE_ROOT_ENV_VARS

    def test_a_non_pod_spawn_env_is_byte_identical(self) -> None:
        """Outside a pod nothing is scrubbed -- the override is the operator's own."""
        from kiro_crew.acp.client import IDENTITY_STORE_ROOT_ENV_VARS, _apply_pod_home_remap

        env = {"HOME": "/home/user", "XDG_DATA_HOME": "/home/user/.local/share"}
        assert _apply_pod_home_remap(dict(env), pod_home_remap=True) == env
        assert _apply_pod_home_remap(dict(env), pod_home_remap=False) == env
        env_marked = {**env, "KIROCREW_POD": "1"}  # marker but no os-home: no remap
        assert _apply_pod_home_remap(dict(env_marked), pod_home_remap=True) == env_marked
        assert IDENTITY_STORE_ROOT_ENV_VARS  # the scrub set is non-empty on every platform
