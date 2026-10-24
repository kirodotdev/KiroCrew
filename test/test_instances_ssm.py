"""The SSM connection method: validation, registry, tunnel argv, mint, diagnosis."""

from __future__ import annotations

import asyncio
import os

import pytest
from instances_helpers import _FakeTunnel, _patch_port_probe


class TestSsmValidation:
    """Injection-safe validation of the SSM-transport inputs."""

    def test_valid_targets(self):
        from kiro_crew.instances.validation import validate_ssm_target

        assert validate_ssm_target("i-0123456789abcdef0") == "i-0123456789abcdef0"
        assert validate_ssm_target("mi-0123456789abcdef0") == "mi-0123456789abcdef0"
        assert validate_ssm_target("i-abcdef12") == "i-abcdef12"  # legacy 8-char id
        assert validate_ssm_target("  i-0123456789abcdef0  ") == "i-0123456789abcdef0"

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "i-",
            "x-0123456789abcdef0",  # wrong prefix
            "i-0123456789ABCDEF0",  # uppercase hex not used by AWS ids
            "i-0123456789abcdef0; rm -rf /",  # shell metacharacters
            "-i-0123456789abcdef0",  # option injection
            "i-0123456789abcdef0 --region evil",  # argv smuggling
            "$(whoami)",
        ],
    )
    def test_rejects_bad_targets(self, bad):
        from kiro_crew.instances.validation import SsmValidationError, validate_ssm_target

        with pytest.raises(SsmValidationError):
            validate_ssm_target(bad)

    def test_profile_and_region(self):

        from kiro_crew.instances.validation import (
            SsmValidationError,
            validate_aws_profile,
            validate_aws_region,
        )

        # Empty is allowed: "use the default chain / default region".
        assert validate_aws_profile("") == ""
        assert validate_aws_region("") == ""
        assert validate_aws_profile("my-profile_1.x") == "my-profile_1.x"
        # '+' is legal in profile names: IAM entity names permit it, and SSO
        # tooling derives "<account>+<permission-set>" shaped profiles.
        assert validate_aws_profile("AdminAccess+dev") == "AdminAccess+dev"
        assert validate_aws_region("us-east-1") == "us-east-1"
        assert validate_aws_region("us-gov-west-1") == "us-gov-west-1"
        # Option injection + metacharacters + bogus region shapes are refused.
        for bad in ("-oProxyCommand=x", "-dev", "a b", "a;b", "a$(b)", "a$b"):
            with pytest.raises(SsmValidationError):
                validate_aws_profile(bad)
        for bad in ("useast1", "US-EAST-1", "us-east-1; rm -rf /", "-us-east-1"):
            with pytest.raises(SsmValidationError):
                validate_aws_region(bad)

    def test_ssm_run_as_accepts_unix_usernames_and_defaults_when_empty(self):
        """Empty means "the default user", never an empty ``sudo -u``."""
        from kiro_crew.instances.validation import validate_ssm_run_as

        assert validate_ssm_run_as("ubuntu") == "ubuntu"
        assert validate_ssm_run_as("ec2-user") == "ec2-user"
        assert validate_ssm_run_as("_svc_01") == "_svc_01"
        assert validate_ssm_run_as("  ubuntu  ") == "ubuntu"
        # Empty / None fall back to the default rather than producing `sudo -u ''`.
        assert validate_ssm_run_as("") == "ec2-user"
        assert validate_ssm_run_as(None) == "ec2-user"  # type: ignore[arg-type]

    def test_ssm_run_as_rejects_injection_and_bad_usernames(self):
        """It is interpolated into `sudo -u <user> -i` on the remote box."""
        from kiro_crew.instances.validation import SsmValidationError, validate_ssm_run_as

        for bad in (
            "root; rm -rf /",
            "user name",
            "-oProxyCommand=x",
            "Ubuntu",  # uppercase is not a valid Unix username here
            "1user",  # must not start with a digit
            "us$er",
            "a" * 33,  # over the length cap
        ):
            with pytest.raises(SsmValidationError):
                validate_ssm_run_as(bad)


# An ECS task target is ``ecs:<cluster>_<taskId>_<runtimeId>``. Built from parts
# here so each attack vector below differs from a VALID value in exactly one way.
_ECS_TASK_ID = "0123456789abcdef0123456789abcdef"
_ECS_RUNTIME_ID = f"{_ECS_TASK_ID}-1234567890"
_ECS_OK = f"ecs:mycluster_{_ECS_TASK_ID}_{_ECS_RUNTIME_ID}"


class TestEcsTargetValidation:
    """The Fargate lane's ECS task target, at the same guard as the EC2 id.

    The ECS shape has to be widened into ``validate_ssm_target`` for a Fargate
    crew to be reachable at all, and that validator is a shell-injection and
    argv-smuggling boundary. So the accept set is pinned narrowly and the reject
    set is pinned vector by vector: a future widening that loosens the charset
    fails these tests rather than quietly enlarging the boundary.
    """

    @pytest.mark.parametrize(
        "good",
        [
            _ECS_OK,
            f"ecs:my_cluster_with_underscores_{_ECS_TASK_ID}_{_ECS_RUNTIME_ID}",
            f"ecs:A-b_9_{_ECS_TASK_ID}_{_ECS_RUNTIME_ID}",
            # AWS bounds a cluster name at 255 chars; that is the longest value
            # the pattern accepts, at 336 bytes total.
            f"ecs:{'c' * 255}_{_ECS_TASK_ID}_{_ECS_RUNTIME_ID}",
        ],
    )
    def test_accepts_well_formed_ecs_targets(self, good):
        from kiro_crew.instances.validation import validate_ssm_target

        assert validate_ssm_target(good) == good

    def test_strips_then_validates_and_returns_the_stripped_value(self):
        """Surrounding whitespace is removed, and the STRIPPED value is returned.

        Load-bearing ordering: the raw value fails the anchored pattern while its
        stripped form passes, so returning the caller's original instead of the
        stripped one would hand back the newline the pattern just refused.
        """
        from kiro_crew.instances.validation import validate_ssm_target

        assert validate_ssm_target(f"  {_ECS_OK}\n") == _ECS_OK

    def test_ec2_ids_still_accepted_after_widening(self):
        """Widening for Fargate must not disturb the EC2/SSM-managed lane."""
        from kiro_crew.instances.validation import validate_ssm_target

        assert validate_ssm_target("i-0123456789abcdef0") == "i-0123456789abcdef0"
        assert validate_ssm_target("mi-0123456789abcdef0") == "mi-0123456789abcdef0"
        assert validate_ssm_target("i-abcdef12") == "i-abcdef12"

    def test_ecs_target_rejects_unicode_digits(self):
        """``[0-9]`` not ``\\d`` for the runtime suffix.

        Python's ``\\d`` matches any Unicode decimal digit, so a ``\\d{1,20}``
        suffix ACCEPTS Arabic-Indic digits. This exact vector was accepted by the
        first proposed pattern and is the reason the shipped one spells the class
        out. Pinned as its own named test so a future edit back to ``\\d`` fails
        here with an explanation rather than in a security review.
        """
        from kiro_crew.instances.validation import SsmValidationError, validate_ssm_target

        vector = f"ecs:c_{_ECS_TASK_ID}_{_ECS_TASK_ID}-\u0661234567890"
        with pytest.raises(SsmValidationError):
            validate_ssm_target(vector)

    def test_ecs_target_rejects_trailing_newline_via_z_anchor(self):
        """``\\Z`` not ``$``: ``$`` also matches just before a trailing newline."""
        from kiro_crew.instances.validation import (
            _ECS_TARGET_RE,
            SsmValidationError,
            validate_ssm_target,
        )

        # A trailing newline cannot reach validate_ssm_target (it strips first),
        # so the anchor property is asserted on the pattern itself.
        assert _ECS_TARGET_RE.match(f"{_ECS_OK}\n") is None
        with pytest.raises(SsmValidationError):
            validate_ssm_target(f"{_ECS_OK}\nwhoami")

    def test_rejects_values_over_the_length_bound(self):
        from kiro_crew.instances.validation import (
            _MAX_SSM_TARGET_LEN,
            SsmValidationError,
            validate_ssm_target,
        )

        longest = f"ecs:{'c' * 255}_{_ECS_TASK_ID}_{_ECS_RUNTIME_ID}"
        assert len(longest) <= _MAX_SSM_TARGET_LEN
        with pytest.raises(SsmValidationError):
            validate_ssm_target(f"ecs:{'c' * 256}_{_ECS_TASK_ID}_{_ECS_RUNTIME_ID}")

    @pytest.mark.parametrize("suffix_digits", [1, 10, 11, 20])
    def test_the_length_bound_never_rejects_what_the_pattern_accepts(self, suffix_digits):
        """The bound must be derived from the pattern, not stated beside it.

        It was stated, as 336, computed from a 10-digit runtime suffix while the
        pattern accepts 20 -- so every legal target with an 11-to-20 digit suffix
        was refused by the length check before the pattern could accept it. Failing
        closed made that a false reject rather than a hole, but the check silently
        overrode the shape it was supposed to be guarding.

        Parametrised across the suffix lengths that straddle the old boundary, so a
        future hardcoded number fails here instead of quietly shrinking the accept
        set.
        """
        from kiro_crew.instances.validation import (
            _ECS_TARGET_RE,
            ssm_target_matches,
            validate_ssm_target,
        )

        target = f"ecs:{'c' * 255}_{_ECS_TASK_ID}_{_ECS_TASK_ID}-{'9' * suffix_digits}"
        # The pattern accepts it, so every layer above the pattern must too.
        assert _ECS_TARGET_RE.match(target), "fixture no longer matches the pattern"
        assert ssm_target_matches(target), f"length bound false-rejects {len(target)} chars"
        assert validate_ssm_target(target) == target

    def test_the_bound_is_the_longest_value_the_pattern_accepts(self):
        """Derivation check: the constant equals the longest legal target's length."""
        from kiro_crew.instances.validation import (
            _ECS_TARGET_RE,
            _MAX_SSM_TARGET_LEN,
            ssm_target_matches,
        )

        longest = f"ecs:{'c' * 255}_{'0' * 32}_{'0' * 32}-{'9' * 20}"
        assert _ECS_TARGET_RE.match(longest)
        assert ssm_target_matches(longest)
        assert len(longest) == _MAX_SSM_TARGET_LEN == 346
        # One character more than the longest legal value is refused.
        assert not ssm_target_matches(longest + "9")

    def test_does_not_unicode_normalise_the_target(self):
        """A fullwidth cluster char must stay rejected, NOT be folded to ASCII.

        NFKC-normalising first would fold fullwidth ``ｃ`` to ``c`` and turn a
        rejected value into an accepted one, so the validator deliberately does
        no normalisation. Asserted because "normalise before validating" is a
        plausible-sounding change that would silently open the charset.
        """
        from kiro_crew.instances.validation import SsmValidationError, validate_ssm_target

        fullwidth = f"ecs:\uff43luster_{_ECS_TASK_ID}_{_ECS_RUNTIME_ID}"
        with pytest.raises(SsmValidationError):
            validate_ssm_target(fullwidth)
        # Proof the rejection is the raw form, not the folded one: NFKC of this
        # value IS an otherwise-valid target, so a normalising validator passes it.
        import unicodedata

        assert validate_ssm_target(unicodedata.normalize("NFKC", fullwidth))

    @pytest.mark.parametrize(
        "vector",
        [
            pytest.param(f"{_ECS_OK}\nwhoami", id="embedded-newline"),
            pytest.param(f"ecs:c_$(id)_{_ECS_TASK_ID}_{_ECS_RUNTIME_ID}", id="dollar-paren"),
            pytest.param(f"ecs:c_`id`_{_ECS_TASK_ID}_{_ECS_RUNTIME_ID}", id="backtick"),
            pytest.param(f"{_ECS_OK};id", id="semicolon"),
            pytest.param(f"{_ECS_OK}|id", id="pipe"),
            pytest.param(f"{_ECS_OK}&", id="ampersand"),
            pytest.param(f"ecs:c'_{_ECS_TASK_ID}_{_ECS_RUNTIME_ID}", id="single-quote"),
            pytest.param(f'ecs:c"_{_ECS_TASK_ID}_{_ECS_RUNTIME_ID}', id="double-quote"),
            pytest.param(
                f"ecs:-oProxyCommand_{_ECS_TASK_ID}_{_ECS_RUNTIME_ID}", id="leading-dash-cluster"
            ),
            pytest.param(f"{_ECS_OK} --region us-east-1", id="argv-smuggle-region"),
            pytest.param(f"{_ECS_OK} --profile admin", id="argv-smuggle-profile"),
            pytest.param(
                f"ecs:{'c' * 256}_{_ECS_TASK_ID}_{_ECS_RUNTIME_ID}", id="overlong-cluster"
            ),
            pytest.param(f"ecs:c_{_ECS_TASK_ID}_{_ECS_TASK_ID}-{'9' * 21}", id="overlong-runtime"),
            pytest.param(f"ecs\uff1ac_{_ECS_TASK_ID}_{_ECS_RUNTIME_ID}", id="fullwidth-colon"),
            pytest.param(f"ecs:\u0441_{_ECS_TASK_ID}_{_ECS_RUNTIME_ID}", id="cyrillic-es"),
            pytest.param(
                f"ecs:c_{_ECS_TASK_ID}_{_ECS_TASK_ID}-\u0661234567890", id="unicode-digit"
            ),
            pytest.param(f"{_ECS_OK}\x00", id="nul-byte"),
            pytest.param(f"ecs:c\t_{_ECS_TASK_ID}_{_ECS_RUNTIME_ID}", id="tab"),
            pytest.param(
                f"ecs:c_{_ECS_TASK_ID.upper()}_{_ECS_RUNTIME_ID}", id="uppercase-hex-task-id"
            ),
            pytest.param(f"ecs:c_{_ECS_TASK_ID[:31]}_{_ECS_RUNTIME_ID}", id="task-id-31-hex"),
            pytest.param(f"ecs:c_{_ECS_TASK_ID}0_{_ECS_RUNTIME_ID}", id="task-id-33-hex"),
            pytest.param(f"ecs:c_{_ECS_TASK_ID}_{_ECS_TASK_ID}", id="no-runtime-suffix"),
            pytest.param(f"ecs:../../c_{_ECS_TASK_ID}_{_ECS_RUNTIME_ID}", id="path-traversal"),
            pytest.param(f"-{_ECS_OK}", id="leading-dash-whole-arg"),
            pytest.param(f"ecs:_{_ECS_TASK_ID}_{_ECS_RUNTIME_ID}", id="empty-cluster"),
            pytest.param(f"ec2:c_{_ECS_TASK_ID}_{_ECS_RUNTIME_ID}", id="wrong-scheme"),
            pytest.param(f"ecs:c*_{_ECS_TASK_ID}_{_ECS_RUNTIME_ID}", id="glob-in-cluster"),
        ],
    )
    def test_rejects_injection_and_malformed_ecs_targets(self, vector):
        """Every vector differs from a valid target in exactly one way.

        A naive widening -- ``^ecs:.+_.+_.+$`` -- accepts most of these, including
        command substitution, argv smuggling and a NUL byte (pinned vector by
        vector in :meth:`test_naive_widening_accepts_the_dangerous_classes`).
        That is what this corpus exists to prevent, so do not relax the pattern to
        make a new case pass: add the case and keep the pattern anchored.

        Vectors differing from a valid target ONLY by surrounding whitespace are
        deliberately absent here -- the validator strips before matching and
        returns the stripped value, so it accepts them by design. The anchor that
        makes that safe is asserted in
        :meth:`test_pattern_rejects_surrounding_whitespace` instead.
        """
        from kiro_crew.instances.validation import SsmValidationError, validate_ssm_target

        with pytest.raises(SsmValidationError):
            validate_ssm_target(vector)

    @pytest.mark.parametrize(
        "whitespace_variant",
        [f"{_ECS_OK}\n", f"{_ECS_OK}\r", f"{_ECS_OK} ", f"  {_ECS_OK}", f"\t{_ECS_OK}\n"],
    )
    def test_pattern_rejects_surrounding_whitespace(self, whitespace_variant):
        """The PATTERN refuses whitespace; the validator strips it first.

        Both halves matter and they are different claims. ``validate_ssm_target``
        accepts these because it strips and then returns the stripped value, which
        is safe. The pattern must still refuse them, because that anchoring is
        what makes the strip sufficient -- under a ``$`` anchor a trailing newline
        would match, and any caller reaching the pattern without the validator's
        strip would pass a newline through.
        """
        from kiro_crew.instances.validation import ssm_target_matches, validate_ssm_target

        assert not ssm_target_matches(whitespace_variant)
        assert validate_ssm_target(whitespace_variant) == _ECS_OK

    def test_naive_widening_accepts_the_dangerous_classes(self):
        """What a loose widening would let through, pinned by class not by count.

        Measured against THIS corpus, the naive pattern accepts 24 of its 27
        vectors. The count is incidental -- it moves whenever a vector is added --
        so the assertions that carry the meaning are the per-class ones below:
        each is a value the naive pattern accepts and the shipped one refuses.
        """
        import re

        from kiro_crew.instances.validation import ssm_target_matches

        naive = re.compile(r"^ecs:.+_.+_.+$")
        dangerous = {
            "command substitution": f"ecs:c_$(id)_{_ECS_TASK_ID}_{_ECS_RUNTIME_ID}",
            "backtick substitution": f"ecs:c_`id`_{_ECS_TASK_ID}_{_ECS_RUNTIME_ID}",
            "argv smuggling": f"{_ECS_OK} --profile admin",
            "option injection": f"ecs:-oProxyCommand_{_ECS_TASK_ID}_{_ECS_RUNTIME_ID}",
            "NUL byte": f"{_ECS_OK}\x00",
            "trailing newline": f"{_ECS_OK}\n",
            "path traversal": f"ecs:../../c_{_ECS_TASK_ID}_{_ECS_RUNTIME_ID}",
            "glob": f"ecs:c*_{_ECS_TASK_ID}_{_ECS_RUNTIME_ID}",
            "unicode digit suffix": f"ecs:c_{_ECS_TASK_ID}_{_ECS_TASK_ID}-\u0661234567890",
        }
        for label, vector in dangerous.items():
            assert naive.match(vector), f"corpus stale: naive no longer accepts {label}"
            assert not ssm_target_matches(vector), f"shipped pattern accepts {label}"


class TestSsmTargetShapeHasOneDefinition:
    """The target charset is a security boundary and must exist ONCE.

    Two copies of one charset is a charset that drifts: widening the authoritative
    validator while a second copy in ``registry.py`` still refuses the value
    produces a lane that validates and then rejects its own accepted input. So the
    charset is spelled in ``validation.py`` alone and imported.
    """

    def test_registry_does_not_redefine_the_target_pattern(self):
        """Source-level ratchet: assert the duplicated CHARSET is absent.

        This asserted the identifier -- ``"_SSM_TARGET_RE = re.compile" not in
        source`` -- and that was a ratchet that could not fail. Measured against
        four realistic ways of reintroducing the duplication, it caught only one:
        restoring the exact deleted line. A renamed constant, the same line without
        spaces around ``=``, and an inline ``re.compile`` with no constant at all
        each sailed through while putting the second copy of the charset back. A
        check that passes for three of four evasions is worse than none, because it
        manufactures confidence.

        So it asserts the CHARSETS instead. Those are what the seam protects, and
        unlike an identifier they cannot be renamed around: any re-spelling of
        either shape has to contain them to match the same values.
        """
        from pathlib import Path

        from kiro_crew.instances import registry

        source = Path(registry.__file__).read_text(encoding="utf-8")
        # The EC2/SSM-managed shape's hex class, and the ECS shape's cluster bound.
        assert "a-f0-9" not in source, "registry re-spells the EC2 target charset"
        assert "{0,254}" not in source, "registry re-spells the ECS cluster bound"
        assert "^ecs:" not in source, "registry re-spells the ECS target shape"
        # It imports the shared decision instead of restating any shape.
        assert "ssm_target_matches" in source

    def test_registry_and_validator_agree_on_every_shape(self):
        from kiro_crew.instances.validation import (
            SsmValidationError,
            ssm_target_matches,
            validate_ssm_target,
        )

        for value in (_ECS_OK, "i-0123456789abcdef0", "mi-0123456789abcdef0", "i-abcdef12"):
            assert ssm_target_matches(value)
            assert validate_ssm_target(value) == value
        # Rejected by BOTH. Deliberately not a whitespace-only variant: the
        # validator strips first, so those two layers legitimately disagree there
        # (see TestEcsTargetValidation.test_pattern_rejects_surrounding_whitespace).
        for value in (f"{_ECS_OK} --profile admin", "x-0123456789abcdef0", f"{_ECS_OK}\nwhoami"):
            assert not ssm_target_matches(value)
            with pytest.raises(SsmValidationError):
                validate_ssm_target(value)

    def test_registry_accepts_an_ecs_target_for_a_fargate_record(self, tmp_path):
        """End of the seam: a record carrying an ECS target must persist."""
        from kiro_crew.instances.registry import InstancesRegistry

        reg = InstancesRegistry(path=tmp_path / "instances.json")
        inst = reg.add(name="Fargate crew", connection_method="fargate", ssm_target=_ECS_OK)
        assert inst.ssm_target == _ECS_OK
        # Round-trips through disk rather than only passing the in-memory check.
        reloaded = InstancesRegistry(path=tmp_path / "instances.json").get(inst.id)
        assert reloaded is not None and reloaded.ssm_target == _ECS_OK

    def test_registry_still_refuses_a_malformed_target(self, tmp_path):
        from kiro_crew.instances.registry import InstancesRegistry, InvalidInstanceError

        reg = InstancesRegistry(path=tmp_path / "instances.json")
        with pytest.raises(InvalidInstanceError):
            reg.add(
                name="Bad crew",
                connection_method="ssm",
                ssm_target=f"{_ECS_OK} --profile admin",
            )


class TestSsmRegistry:
    """Registry support for connection_method + the SSM coordinate fields."""

    def _reg(self, tmp_path):
        from kiro_crew.instances.registry import InstancesRegistry

        return InstancesRegistry(path=tmp_path / "instances.json")

    def test_defaults_to_ssh_for_backcompat(self, tmp_path):
        reg = self._reg(tmp_path)
        inst = reg.add(name="Dev", ssh_host="dev-1")
        assert inst.connection_method == "ssh"
        assert inst.ssm_target == "" and inst.aws_profile == "" and inst.aws_region == ""

    def test_legacy_record_without_connection_method_loads_as_ssh(self, tmp_path):
        """A pre-SSM instances.json must keep working (defaults to ssh)."""
        import json

        path = tmp_path / "instances.json"
        path.write_text(
            json.dumps(
                {
                    "instances": [
                        {"id": "old", "name": "Old", "ssh_host": "old-host", "remote_port": 7777}
                    ],
                    "last_active_id": "old",
                }
            ),
            encoding="utf-8",
        )
        inst = self._reg(tmp_path).get("old")
        assert inst is not None
        assert inst.connection_method == "ssh"
        assert inst.ssh_host == "old-host"

    def test_add_ssm_instance(self, tmp_path):
        reg = self._reg(tmp_path)
        inst = reg.add(
            name="EC2 Box",
            connection_method="ssm",
            ssm_target="i-0123456789abcdef0",
            aws_profile="dev",
            aws_region="eu-west-2",
            remote_port=7777,
            provisioner_id="aws_ec2",
        )
        assert inst.connection_method == "ssm"
        assert inst.ssm_target == "i-0123456789abcdef0"
        assert inst.aws_profile == "dev" and inst.aws_region == "eu-west-2"
        # Round-trips through disk.
        reloaded = self._reg(tmp_path).get(inst.id)
        assert reloaded.connection_method == "ssm"
        assert reloaded.ssm_target == "i-0123456789abcdef0"
        assert reloaded.provisioner_id == "aws_ec2"

    def test_ssm_requires_target_and_ssh_requires_host(self, tmp_path):
        from kiro_crew.instances.registry import InvalidInstanceError

        reg = self._reg(tmp_path)
        # ssm without a target is invalid...
        with pytest.raises(InvalidInstanceError):
            reg.add(name="No target", connection_method="ssm")
        # ...and ssh without a host is still invalid.
        with pytest.raises(InvalidInstanceError):
            reg.add(name="No host", connection_method="ssh")
        # An unknown method is refused rather than silently treated as ssh.
        with pytest.raises(InvalidInstanceError):
            reg.add(name="Bogus", connection_method="telnet", ssh_host="h")

    def test_update_can_switch_method(self, tmp_path):
        reg = self._reg(tmp_path)
        reg.add(name="Dev", ssh_host="dev-1", instance_id="dev")
        u = reg.update("dev", connection_method="ssm", ssm_target="i-0123456789abcdef0")
        assert u.connection_method == "ssm"

    def test_no_aws_credentials_persisted(self, tmp_path):
        """Only the profile NAME may be stored — never a key/secret."""
        reg = self._reg(tmp_path)
        reg.add(
            name="EC2",
            connection_method="ssm",
            ssm_target="i-0123456789abcdef0",
            aws_profile="dev",
            instance_id="ec2",
        )
        raw = (tmp_path / "instances.json").read_text(encoding="utf-8")
        for marker in ("AKIA", "ASIA", "aws_secret_access_key", "aws_session_token"):
            assert marker not in raw

    def test_aws_profile_allows_plus_and_rejects_metacharacters(self, tmp_path):
        """The record check accepts '+' (SSO-derived profile names) but still
        refuses whitespace and shell metacharacters, mirroring validation.py."""
        from kiro_crew.instances.registry import InvalidInstanceError

        reg = self._reg(tmp_path)
        inst = reg.add(
            name="SSO box",
            connection_method="ssm",
            ssm_target="i-0123456789abcdef0",
            aws_profile="AdminAccess+dev",
            instance_id="sso",
        )
        assert inst.aws_profile == "AdminAccess+dev"
        assert reg.list()[0].aws_profile == "AdminAccess+dev"
        for i, bad in enumerate(("a b", "a;b", "a$(b)", "a$b")):
            with pytest.raises(InvalidInstanceError):
                reg.add(
                    name="bad profile",
                    connection_method="ssm",
                    ssm_target="i-0123456789abcdef0",
                    aws_profile=bad,
                    instance_id=f"bad-{i}",
                )

    def test_aws_profile_regex_is_single_sourced(self):
        """The registry's early record check aliases validation.py's pattern.

        There is exactly one AWS-profile charset: registry._AWS_PROFILE_RE is
        the SAME compiled object as validation._AWS_PROFILE_RE, so the two
        check sites cannot drift. If someone re-introduces a second copy this
        identity check fails even when the copies happen to be textually
        equal. Note the pattern accepts a leading '-' by design: option
        injection is blocked by the separate startswith('-') guard in
        validation.validate_aws_profile, not by the character class, and the
        empty "default chain" value is handled by the `if self.aws_profile`
        guard at the registry check site.
        """
        from kiro_crew.instances import registry, validation

        assert registry._AWS_PROFILE_RE is validation._AWS_PROFILE_RE

    def test_ssm_run_as_defaults_and_round_trips(self, tmp_path):
        """A record written before ssm_run_as existed must load as the default.

        Regression guard for the design-review finding: the remote user was
        hard-coded to ``ec2-user`` inside ``cloud.ssm.run_command``, so an Ubuntu
        AMI's mint failed. It is now a per-instance field — but an older registry
        file has no key at all, and a file with an explicit empty string would
        fail validation, so BOTH must resolve to the default.
        """
        from kiro_crew.instances.registry import Instance

        assert Instance.from_dict({"id": "a", "name": "A"}).ssm_run_as == "ec2-user"
        assert Instance.from_dict({"id": "a", "ssm_run_as": ""}).ssm_run_as == "ec2-user"
        assert Instance.from_dict({"id": "a", "ssm_run_as": "ubuntu"}).ssm_run_as == "ubuntu"

        reg = self._reg(tmp_path)
        inst = reg.add(
            name="Ubuntu box",
            connection_method="ssm",
            ssm_target="i-0123456789abcdef0",
            ssm_run_as="ubuntu",
            instance_id="ubu",
        )
        assert inst.ssm_run_as == "ubuntu"
        assert reg.list()[0].ssm_run_as == "ubuntu"
        assert "ssm_run_as" in inst.to_dict()

    def test_ssm_run_as_is_validated_on_add(self, tmp_path):
        """User input reaches `sudo -u` on the remote box, so it is validated."""
        from kiro_crew.instances.registry import InvalidInstanceError

        reg = self._reg(tmp_path)
        with pytest.raises(InvalidInstanceError):
            reg.add(
                name="bad",
                connection_method="ssm",
                ssm_target="i-0123456789abcdef0",
                ssm_run_as="root; rm -rf /",
                instance_id="bad",
            )

    def test_error_messages_carry_no_raw_regex(self, tmp_path):
        """Form errors must read in plain English, not as a regex.

        UX-review finding: the pattern flowed verbatim into the Settings form.
        """
        from kiro_crew.instances.registry import InvalidInstanceError

        reg = self._reg(tmp_path)
        with pytest.raises(InvalidInstanceError) as e:
            reg.add(
                name="bad",
                connection_method="ssm",
                ssm_target="not-an-id",
                instance_id="bad2",
            )
        msg = str(e.value)
        assert "^" not in msg and "[a-f0-9]" not in msg and "{8,17}" not in msg
        assert "hex digits" in msg


class TestSsmTunnelArgv:
    """The SSM port-forward argv (loopback-bound, no shell, no injected opts)."""

    @pytest.fixture(autouse=True)
    # flake-ok: pre-existing autouse aws-CLI resolver pin on the shared monkeypatch, moved verbatim; no test here calls undo()
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing class-local autouse stub, moved verbatim; de-flake pass tracked
    def _bare_resolver(self, monkeypatch):
        """Pin the shared aws-CLI resolver to the bare name so the
        argv-shape assertions stay deterministic across hosts."""
        from kiro_crew.cloud import ssm

        monkeypatch.setattr(ssm, "resolve_aws_bin", lambda: "aws")

    def test_start_builds_argv_off_the_event_loop(self, monkeypatch):
        """The SSM branch's argv build resolves the aws CLI, which
        probes the filesystem — start() must run it in a worker thread, never
        on the gateway event loop (a stalled network mount on PATH would
        otherwise freeze every request)."""
        from kiro_crew.instances import ssh_tunnel_manager as stm
        from kiro_crew.instances.ssh_tunnel_manager import _SshTunnel

        seen: dict = {}

        def probe_builder(*a, **k):
            try:
                asyncio.get_running_loop()
                seen["on_loop"] = True
            except RuntimeError:
                seen["on_loop"] = False
            return ["aws", "ssm", "start-session"]

        monkeypatch.setattr(stm, "_build_ssm_tunnel_argv", probe_builder)

        class FakeProc:
            returncode = None
            # A real asyncio Process always exposes both stream attributes.
            stdout = None
            stderr = None
            pid = 4242

            def terminate(self):
                self.returncode = -15

            def kill(self):
                self.returncode = -9

            async def wait(self):
                return self.returncode

        async def fake_exec(*a, **k):
            return FakeProc()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        # stop() on the SSM transport reaps the child's whole process group by
        # pid. FakeProc.pid is a made-up number, so the real signal would land
        # on whatever unrelated process holds that pid on the host (on CI, an
        # xdist worker). Keep the signal out of the OS; the fake's terminate()
        # is the fallback path and settles returncode.
        monkeypatch.setattr(stm._SshTunnel, "_signal_group", staticmethod(lambda pid, sig: False))

        async def _undelivered(_pid, _sig):
            return False

        monkeypatch.setattr(stm._SshTunnel, "_signal_group_async", staticmethod(_undelivered))

        async def main():
            t = _SshTunnel(
                "cd-1",
                "",
                7778,
                7777,
                transport="ssm",
                ssm_target="i-0123456789abcdef0",
            )

            async def _reachable():
                return True

            t._port_reachable = _reachable
            ok = await t.start()
            assert ok
            await t.stop()

        asyncio.run(main())
        assert seen["on_loop"] is False  # built in a worker thread, not on the loop

    def test_argv_shape(self):
        from kiro_crew.instances.ssh_tunnel_manager import _build_ssm_tunnel_argv

        argv = _build_ssm_tunnel_argv(
            "i-0123456789abcdef0", 7777, 7777, profile="dev", region="eu-west-2"
        )
        assert argv[:3] == ["aws", "ssm", "start-session"]
        assert "--target" in argv and "i-0123456789abcdef0" in argv
        assert "AWS-StartPortForwardingSession" in argv
        assert "portNumber=7777,localPortNumber=7777" in argv
        assert argv[argv.index("--region") + 1] == "eu-west-2"
        assert argv[argv.index("--profile") + 1] == "dev"
        # argv list => no shell; nothing is a single concatenated string.
        assert all(isinstance(a, str) for a in argv)

    def test_omits_empty_profile_and_region(self):
        from kiro_crew.instances.ssh_tunnel_manager import _build_ssm_tunnel_argv

        argv = _build_ssm_tunnel_argv("i-0123456789abcdef0", 7777, 7777)
        assert "--profile" not in argv and "--region" not in argv

    def test_ssh_argv_unchanged_for_ssh_instances(self):
        """Regression guard: the SSH argv must not gain SSM flags."""
        from kiro_crew.instances.ssh_tunnel_manager import _build_ssh_tunnel_argv

        argv = _build_ssh_tunnel_argv("dev-1", 7777, 7777)
        assert argv[0] == "ssh" and "-N" in argv
        assert argv[-1] == "dev-1"
        assert "-L" in argv
        assert argv[argv.index("-L") + 1] == "127.0.0.1:7777:127.0.0.1:7777"
        assert "ssm" not in argv


class TestSsmTunnelProcessGroup:
    """Cross-platform teardown of the aws wrapper + session-manager-plugin.

    The plugin grandchild is what holds the forwarded port, so a teardown that
    signals only the ``aws`` wrapper wedges the port. GPT/design review found the
    original code used bare ``start_new_session=True`` and raw
    ``os.killpg``/``os.getpgid`` — both POSIX-only — so on native Windows (a
    supported platform) the plugin orphaned. Everything must route through
    ``platform_compat``.
    """

    def _tunnel(self, transport):
        from kiro_crew.instances.ssh_tunnel_manager import _SshTunnel

        return _SshTunnel(
            instance_id="i1",
            ssh_host="dev-1",
            local_port=7777,
            remote_port=7777,
            transport=transport,
            ssm_target="i-0123456789abcdef0" if transport == "ssm" else "",
        )

    @pytest.mark.asyncio
    async def test_ssm_spawn_passes_both_isolation_kwargs(self, monkeypatch):
        """POSIX gets setsid; Windows gets CREATE_NEW_PROCESS_GROUP."""
        import kiro_crew.instances.ssh_tunnel_manager as mod

        seen = {}

        async def fake_exec(*argv, **kw):
            seen.update(kw)
            raise OSError("stop here — we only care about the spawn kwargs")

        monkeypatch.setattr(mod.asyncio, "create_subprocess_exec", fake_exec)
        monkeypatch.setattr(mod.platform_compat, "IS_POSIX", True)
        monkeypatch.setattr(mod.platform_compat, "CREATE_NEW_PROCESS_GROUP", 0x200)
        await self._tunnel("ssm").start()
        assert seen["start_new_session"] is True
        assert seen["creationflags"] == 0x200

        # Same call on Windows: no setsid (it is silently ignored there), but the
        # creation flag is what makes the tree taskkill /T-reapable.
        seen.clear()
        monkeypatch.setattr(mod.platform_compat, "IS_POSIX", False)
        await self._tunnel("ssm").start()
        assert seen["start_new_session"] is False
        assert seen["creationflags"] == 0x200

    @pytest.mark.asyncio
    async def test_ssh_spawn_gets_no_process_group(self, monkeypatch):
        """Regression guard: the SSH transport's spawn is unchanged."""
        import kiro_crew.instances.ssh_tunnel_manager as mod

        seen = {}

        async def fake_exec(*argv, **kw):
            seen.update(kw)
            raise OSError("stop")

        monkeypatch.setattr(mod.asyncio, "create_subprocess_exec", fake_exec)
        monkeypatch.setattr(mod.platform_compat, "IS_POSIX", True)
        await self._tunnel("ssh").start()
        assert seen["start_new_session"] is False
        assert seen["creationflags"] == 0

    @pytest.mark.asyncio
    async def test_ssm_and_ssh_children_get_the_same_plugin_search_path(
        self, monkeypatch, tmp_path
    ):
        """Both tunnel children need a PATH that can find session-manager-plugin.

        The argv head is resolved absolutely, but the aws CLI then looks the
        plugin up BY NAME on this child's own PATH — under a GUI-launched gateway
        the minimal launchd one — so the tunnel died inside a correctly resolved
        ``aws``. The ssh child has the same gap through the user's
        ``ProxyCommand`` (an SSM connect helper looks the plugin up by name), so
        it gets the same appended dirs — once its own head resolves absolutely.
        """
        import kiro_crew.instances.ssh_tunnel_manager as mod
        from kiro_crew.deploy import engine

        seen = {}

        async def fake_exec(*argv, **kw):
            seen.update(kw)
            raise OSError("stop here — we only care about the spawn kwargs")

        # tmp_path stand-ins: a host path literal would flake and is unrunnable
        # on Windows, which this class deliberately also exercises. `aws` sits on
        # the inherited PATH so the head resolves absolutely (a PATH hit needs no
        # provenance check), which is what makes the widening applicable. Windows
        # resolves executables by PATHEXT rather than the exec bit, so the planted
        # file differs there — otherwise the head falls back to the bare name and
        # the widening is (correctly) withheld.
        inherited = tmp_path / "sysbin"
        inherited.mkdir()
        if os.name == "nt":
            fake_aws = inherited / "aws.cmd"
            fake_aws.write_text("@echo off\n")
            monkeypatch.setenv("PATHEXT", ".cmd")
        else:
            fake_aws = inherited / "aws"
            fake_aws.write_text("#!/bin/sh\n")
            fake_aws.chmod(0o755)
        install_dir = tmp_path / "install"
        install_dir.mkdir()
        # The PATH wiring is under test here, not the directory check, which has
        # its own tests in test_deploy_web_engine.py and refuses the Windows CI
        # runner's temp directory.
        from kiro_crew import github_runner

        monkeypatch.setattr(
            github_runner, "validate_provider_directory", lambda candidate: candidate
        )
        monkeypatch.setenv("PATH", str(inherited))
        monkeypatch.setattr(engine, "_AWS_BIN_DIRS", (str(install_dir),))
        monkeypatch.setattr(mod.asyncio, "create_subprocess_exec", fake_exec)
        monkeypatch.setattr(mod.platform_compat, "IS_POSIX", True)

        await self._tunnel("ssm").start()
        child_path = seen["env"]["PATH"].split(os.pathsep)
        assert str(install_dir) in child_path
        # Appended: the inherited PATH still wins every name it can resolve.
        assert child_path.index(str(inherited)) < child_path.index(str(install_dir))

        # ssh: same widening, same order, once its head resolves on the
        # inherited PATH (planted the same way as the aws stand-in above).
        fake_ssh = inherited / ("ssh.cmd" if os.name == "nt" else "ssh")
        fake_ssh.write_text("@echo off\n" if os.name == "nt" else "#!/bin/sh\n")
        fake_ssh.chmod(0o755)
        seen.clear()
        await self._tunnel("ssh").start()
        child_path = seen["env"]["PATH"].split(os.pathsep)
        assert child_path.index(str(inherited)) < child_path.index(str(install_dir))

    def test_teardown_routes_through_the_platform_shim(self, monkeypatch):
        """Not raw os.killpg — that leaves the plugin alive on Windows."""
        import kiro_crew.instances.ssh_tunnel_manager as mod

        calls = []
        monkeypatch.setattr(
            mod.platform_compat,
            "kill_process_tree",
            lambda pid, sig: (calls.append((pid, sig)), True)[1],
        )
        assert self._tunnel("ssm")._signal_group(4321, 15) is True
        assert calls == [(4321, 15)]

    def test_teardown_reports_undelivered_instead_of_raising(self, monkeypatch):
        """The shim propagates; a failure must degrade to the single-proc kill."""
        import kiro_crew.instances.ssh_tunnel_manager as mod

        for exc in (ProcessLookupError, PermissionError, OSError, ValueError):

            def boom(pid, sig, _e=exc):
                raise _e("nope")

            monkeypatch.setattr(mod.platform_compat, "kill_process_tree", boom)
            assert self._tunnel("ssm")._signal_group(4321, 15) is False


class TestSsmTransportSelection:
    """The manager must drive the transport each instance is configured for."""

    @pytest.fixture(autouse=True)
    # flake-ok: pre-existing autouse stub of both port probes on the shared monkeypatch; moved verbatim, no test calls undo()
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing class-local autouse stub, moved verbatim; de-flake pass tracked
    def _free_ports(self, monkeypatch):
        # These tests assert which TRANSPORT the manager selects (ssh vs ssm) via
        # _FakeTunnel; they are not about real local-port availability. connect()
        # probes the real _is_port_free (CSE SEC-016 mirror-conflict check), and
        # on a busy CI shard the fixed ports below (53510-53513) can already be
        # bound -- connect() then returns an error status WITHOUT registering the
        # tunnel, so `mgr._tunnels[id]` raises KeyError and the test flakes. Stub
        # the probe to always-free, exactly as the other SshTunnelManager test
        # classes do, so transport selection is tested deterministically.
        _patch_port_probe(monkeypatch)

    def _mgr(self, tmp_path, *, mint=None, connect_timeout_secs=None):
        from kiro_crew.instances.registry import InstancesRegistry
        from kiro_crew.instances.ssh_tunnel_manager import SshTunnelManager

        async def ok_mint(
            host,
            *,
            remote_bin="",
            ttl="20h",
            remote_port=None,
            embed_parent_port=None,
            timeout_secs=None,
        ):
            return "SSH_TOKEN"

        reg = InstancesRegistry(path=tmp_path / "instances.json")
        manager_kwargs = {}
        if connect_timeout_secs is not None:
            manager_kwargs["connect_timeout_secs"] = connect_timeout_secs
        return reg, SshTunnelManager(
            reg,
            base_port=53500,
            mint_token=mint or ok_mint,
            tunnel_factory=_FakeTunnel,
            **manager_kwargs,
        )

    @pytest.mark.parametrize(
        ("configured", "ssh_expected", "ssm_expected"),
        [
            (None, 15.0, 25.0),
            (15.0, 15.0, 15.0),
            (45.0, 45.0, 45.0),
            (0.0, 15.0, 25.0),
            (200.0, 120.0, 120.0),
        ],
    )
    def test_connect_timeout_matrix(self, tmp_path, configured, ssh_expected, ssm_expected):
        from kiro_crew.config.loader import InstancesConfig
        from kiro_crew.instances.constants import (
            CONNECT_TIMEOUT_CEILING_SECS,
            DEFAULT_CONNECT_TIMEOUT_SECS,
            DEFAULT_SSM_CONNECT_TIMEOUT_SECS,
        )

        assert DEFAULT_CONNECT_TIMEOUT_SECS == 15.0
        assert DEFAULT_SSM_CONNECT_TIMEOUT_SECS == 25.0
        assert CONNECT_TIMEOUT_CEILING_SECS == 120.0

        config = InstancesConfig(connect_timeout_secs=configured)
        _, mgr = self._mgr(
            tmp_path,
            connect_timeout_secs=config.connect_timeout_secs,
        )
        assert mgr._connect_timeout_for("ssh") == ssh_expected
        assert mgr._connect_timeout_for("ssm") == ssm_expected

    @pytest.mark.asyncio
    async def test_ssh_instance_uses_ssh_transport(self, tmp_path):
        reg, mgr = self._mgr(tmp_path)
        reg.add(name="Dev", ssh_host="dev-1", instance_id="dev", remote_port=53510)
        await mgr.connect("dev")
        tunnel = mgr._tunnels["dev"]
        assert tunnel.transport == "ssh"
        assert tunnel.ssh_host == "dev-1"
        assert mgr.get_token("dev") == "SSH_TOKEN"
        await mgr.shutdown()

    @pytest.mark.asyncio
    async def test_ssm_instance_uses_ssm_transport_and_ssm_mint(self, tmp_path, monkeypatch):
        import kiro_crew.instances.ssh_tunnel_manager as mod
        from kiro_crew.instances.constants import DEFAULT_SSM_CONNECT_TIMEOUT_SECS

        seen = {}

        async def fake_ssm_mint(target, **kwargs):
            seen["target"] = target
            seen.update(kwargs)
            return "SSM_TOKEN"

        monkeypatch.setattr(mod, "mint_remote_token_ssm", fake_ssm_mint)
        # The plugin presence check must not gate the unit test.
        monkeypatch.setattr("kiro_crew.cloud.ssm.session_manager_plugin_installed", lambda: True)

        reg, mgr = self._mgr(tmp_path)
        reg.add(
            name="EC2",
            connection_method="ssm",
            ssm_target="i-0123456789abcdef0",
            aws_profile="dev",
            aws_region="eu-west-2",
            instance_id="ec2",
            remote_port=53511,
        )
        await mgr.connect("ec2")

        tunnel = mgr._tunnels["ec2"]
        assert tunnel.transport == "ssm"
        assert tunnel.ssm_target == "i-0123456789abcdef0"
        assert tunnel.aws_profile == "dev" and tunnel.aws_region == "eu-west-2"
        assert tunnel.connect_timeout_secs == DEFAULT_SSM_CONNECT_TIMEOUT_SECS == 25.0
        # Token came from the SSM mint (NOT the ssh mint seam).
        assert mgr.get_token("ec2") == "SSM_TOKEN"
        assert seen["target"] == "i-0123456789abcdef0"
        assert seen["aws_profile"] == "dev" and seen["aws_region"] == "eu-west-2"
        await mgr.shutdown()

    @pytest.mark.asyncio
    async def test_mint_timeout_threads_to_ssh_mint(self, tmp_path):
        """A configured instances.mint_timeout_secs reaches the ssh mint call."""
        from kiro_crew.instances.registry import InstancesRegistry
        from kiro_crew.instances.ssh_tunnel_manager import SshTunnelManager

        seen = {}

        async def capturing_mint(
            host,
            *,
            remote_bin="",
            ttl="20h",
            remote_port=None,
            embed_parent_port=None,
            timeout_secs=None,
        ):
            seen["timeout_secs"] = timeout_secs
            return "TOK"

        reg = InstancesRegistry(path=tmp_path / "instances.json")
        mgr = SshTunnelManager(
            reg,
            base_port=53520,
            mint_timeout_secs=77.0,
            mint_token=capturing_mint,
            tunnel_factory=_FakeTunnel,
        )
        reg.add(name="Dev", ssh_host="dev-1", instance_id="dev", remote_port=53521)
        await mgr.connect("dev")
        assert seen["timeout_secs"] == 77.0
        await mgr.shutdown()

    @pytest.mark.asyncio
    async def test_mint_timeout_ssm_default_and_override(self, tmp_path, monkeypatch):
        """SSM mint keeps its higher default; an explicit override wins for it too."""
        import kiro_crew.instances.ssh_tunnel_manager as mod
        from kiro_crew.instances.constants import DEFAULT_SSM_MINT_TIMEOUT_SECS
        from kiro_crew.instances.registry import InstancesRegistry
        from kiro_crew.instances.ssh_tunnel_manager import SshTunnelManager

        seen = {}

        async def fake_ssm_mint(target, **kwargs):
            seen["timeout_secs"] = kwargs.get("timeout_secs")
            return "SSM_TOKEN"

        monkeypatch.setattr(mod, "mint_remote_token_ssm", fake_ssm_mint)
        monkeypatch.setattr("kiro_crew.cloud.ssm.session_manager_plugin_installed", lambda: True)

        def add_ssm(reg, iid, port):
            reg.add(
                name=iid,
                connection_method="ssm",
                ssm_target="i-0123456789abcdef0",
                aws_profile="dev",
                aws_region="eu-west-2",
                instance_id=iid,
                remote_port=port,
            )

        # Default manager -> SSM mint gets the higher SSM default (90s).
        reg = InstancesRegistry(path=tmp_path / "a.json")
        mgr = SshTunnelManager(reg, base_port=53530, tunnel_factory=_FakeTunnel)
        add_ssm(reg, "ec2a", 53531)
        await mgr.connect("ec2a")
        assert seen["timeout_secs"] == DEFAULT_SSM_MINT_TIMEOUT_SECS == 90.0
        await mgr.shutdown()

        # Explicit override wins for the SSM transport too.
        reg2 = InstancesRegistry(path=tmp_path / "b.json")
        mgr2 = SshTunnelManager(
            reg2, base_port=53540, mint_timeout_secs=45.0, tunnel_factory=_FakeTunnel
        )
        add_ssm(reg2, "ec2b", 53541)
        await mgr2.connect("ec2b")
        assert seen["timeout_secs"] == 45.0
        await mgr2.shutdown()

    @pytest.mark.asyncio
    async def test_plugin_probe_runs_off_the_event_loop(self, tmp_path, monkeypatch):
        """The prerequisite probe must not block the gateway event loop.

        The probe resolves the plugin through the deploy engine's shared resolver
        — PATH scan, then the well-known install dirs, then executable-provenance
        validation — so a stalled network mount on any of those would freeze every
        request and heartbeat. Pinned by the THREAD it actually runs on rather
        than by source inspection, so an edit that drops the offload fails here
        even if it keeps the wording.
        """
        import threading

        from kiro_crew.instances.ssh_tunnel_manager import TunnelState

        loop_thread = threading.get_ident()
        ran_on: dict = {}

        def _probe():
            ran_on["thread"] = threading.get_ident()
            return False  # short-circuit: no tunnel spawn, error status asserted

        monkeypatch.setattr("kiro_crew.cloud.ssm.session_manager_plugin_installed", _probe)
        reg, mgr = self._mgr(tmp_path)
        reg.add(
            name="EC2",
            connection_method="ssm",
            ssm_target="i-0123456789abcdef0",
            instance_id="ec2",
            remote_port=53514,
        )

        st = await mgr.connect("ec2")

        assert st.state == TunnelState.ERROR
        assert ran_on["thread"] != loop_thread
        await mgr.shutdown()

    @pytest.mark.asyncio
    async def test_ssm_connect_fails_clean_without_plugin(self, tmp_path, monkeypatch):
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState

        monkeypatch.setattr("kiro_crew.cloud.ssm.session_manager_plugin_installed", lambda: False)
        reg, mgr = self._mgr(tmp_path)
        reg.add(
            name="EC2",
            connection_method="ssm",
            ssm_target="i-0123456789abcdef0",
            instance_id="ec2",
            remote_port=53512,
        )
        st = await mgr.connect("ec2")
        assert st.state == TunnelState.ERROR
        assert "session-manager-plugin" in st.error
        # No tunnel was spawned for a missing prerequisite.
        assert mgr.status("ec2") is None
        await mgr.shutdown()

    @pytest.mark.asyncio
    async def test_invalid_ssm_target_surfaces_error_without_spawn(self, tmp_path, monkeypatch):
        """A registry record hand-edited to a bad target must not reach argv."""
        import dataclasses

        from kiro_crew.instances.ssh_tunnel_manager import TunnelState

        reg, mgr = self._mgr(tmp_path)
        reg.add(
            name="EC2",
            connection_method="ssm",
            ssm_target="i-0123456789abcdef0",
            instance_id="ec2",
            remote_port=53513,
        )
        # Simulate a hand-edited instances.json that bypassed registry validation:
        # the authoritative guard is the tunnel manager's pre-argv validation.
        tampered = dataclasses.replace(reg.get("ec2"), ssm_target="-oProxyCommand=evil")
        monkeypatch.setattr(mgr._registry, "get", lambda iid: tampered if iid == "ec2" else None)
        st = await mgr.connect("ec2")
        assert st.state == TunnelState.ERROR
        assert "invalid" in st.error.lower()
        await mgr.shutdown()


class TestSsmMintOutputRedaction:
    """The SSM mint's error tail must be no weaker than the SSH mint's.

    Both transports raise ``TokenMintError`` from a partially-successful mint --
    the remote printed its success URL and then exited non-zero -- so both build
    an error message out of a stream that can be holding a live token. The SSH
    transport routes that through ``_redacted_output_tail``, which adds a
    ``?token=`` URL-param pass and a token-shape pass on top of the generic
    ``redact()``. A second, weaker copy of that helper on the SSM side leaks the
    shapes those two extra passes exist to catch, which is what these tests pin.
    """

    # SYNTHETIC, never a real token: the two-segment `payload.signature` shape
    # `dashboard/token_auth.generate_token` mints (one dot), with a deliberately
    # short payload and signature so it clears neither of the bounds
    # `security`'s own two-segment link-token pattern keys on. A real token is
    # already redacted by that pattern; this value stands in for the shapes that
    # are not -- a remote of a different Kiro Crew vintage, or a truncated write.
    _SYNTHETIC_TOKEN = "eyJzdWIiOiJzeW50aGV0aWMiLCJleHAiOjF9.c3ludGhldGljLXNpZ25hdHVyZQ"

    # SYNTHETIC opaque bearer value: no `eyJ` prefix and no dot, so no token-SHAPE
    # pattern can recognise it. Only the ``?token=`` URL-param pass catches this,
    # which is what makes it a separate vector rather than a restatement.
    _OPAQUE_TOKEN = "9f3c1ab77d2e4f508c6b1e0a4d7c2f91b5e8a03c"

    # The vectors are named because they fail for DIFFERENT reasons, which the
    # mutation matrix confirms: dropping the token-shape pass kills the first two,
    # dropping the ``?token=`` URL-param pass kills only the third. An `eyJ`-shaped
    # value in a URL is caught by the shape pass on its own, so without an opaque
    # third vector the URL-param pass would be untested here.
    _VECTORS = (
        ("bare_two_segment_token", "{token}", _SYNTHETIC_TOKEN),
        (
            "two_segment_token_in_success_url",
            "http://localhost:5476?token={token}",
            _SYNTHETIC_TOKEN,
        ),
        ("opaque_token_in_success_url", "http://localhost:5476?token={token}", _OPAQUE_TOKEN),
    )

    @staticmethod
    def _patch_failing_mint(monkeypatch, stdout: str):
        """Make the send-command chokepoint report a partially-successful mint."""
        from kiro_crew.cloud import ssm as cloud_ssm

        def fake_run_command(target, command, profile, region, **kwargs):
            return cloud_ssm.CommandResult(status="Failed", stdout=stdout, stderr="", exit_code=1)

        monkeypatch.setattr(cloud_ssm, "run_command", fake_run_command)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("vector_name, stdout_template, secret", _VECTORS)
    async def test_failed_ssm_mint_never_carries_a_token_into_its_error(
        self, monkeypatch, vector_name, stdout_template, secret
    ):
        """A partially-successful SSM mint must not put a token in its exception.

        The raised message travels straight into the operator's log, so the
        assertion is on the real boundary (the exception text), not on the helper
        in isolation.
        """
        from kiro_crew.instances import ssm_token_mint as sm

        stdout = f"starting mint\n{stdout_template.format(token=secret)}\n"
        self._patch_failing_mint(monkeypatch, stdout)

        with pytest.raises(sm.TokenMintError) as excinfo:
            await sm.mint_remote_token_ssm("i-0123456789abcdef0", ttl="20h")

        message = str(excinfo.value)
        assert (
            secret not in message
        ), f"{vector_name}: token reached the raised TokenMintError message"
        # The tail must still carry a reason -- a fix that redacts everything
        # would pass the leak assertion while destroying the error's usefulness.
        assert "starting mint" in message

    @pytest.mark.parametrize("vector_name, stdout_template, secret", _VECTORS)
    def test_ssm_tail_is_no_weaker_than_the_ssh_tail(self, vector_name, stdout_template, secret):
        """Pin the INVARIANT, not just today's three vectors.

        ``ssm_token_mint``'s tail helper claims to mirror ``token_mint``'s
        intent. Asserting the mirror directly means the day the SSH side learns a
        new token shape, the SSM side cannot silently stay behind: this fails
        instead of a leak going unnoticed.
        """
        from kiro_crew.instances import ssm_token_mint as sm
        from kiro_crew.instances import token_mint as tm

        text = f"starting mint\n{stdout_template.format(token=secret)}\n"
        assert secret not in tm._redacted_output_tail(
            text
        ), f"{vector_name}: the SSH-side reference itself leaked -- fix that first"
        assert secret not in sm._redacted_tail(
            text
        ), f"{vector_name}: SSM tail is weaker than the SSH tail it mirrors"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("budget, names_setting", [(90.0, True), (120.0, False)])
    async def test_ssm_mint_timeout_names_the_mint_budget_setting(
        self, monkeypatch, budget, names_setting
    ):
        """An SSM mint timeout names ``instances.mint_timeout_secs`` and the SSM agent.

        At the ceiling the setting cannot go higher, so the message points at the
        target's reachability instead of telling the user to raise it.
        """
        from kiro_crew.instances import ssm_token_mint as sm

        async def _times_out(*a, **k):
            raise asyncio.TimeoutError

        monkeypatch.setattr(sm, "_send_over_ssm", _times_out)
        with pytest.raises(sm.TokenMintError) as excinfo:
            await sm.mint_remote_token_ssm("i-0123456789abcdef0", ttl="20h", timeout_secs=budget)
        msg = str(excinfo.value)
        assert msg.startswith("timed out minting token on i-0123456789abcdef0 over SSM")
        assert f"polling budget {budget}s" in msg
        assert "SSM agent is online" in msg
        assert ("instances.mint_timeout_secs" in msg) is names_setting
        assert ("already at its maximum" in msg) is not names_setting


class TestSsmDiagnostics:
    """The SSM diagnosis ladder reports the first broken link, SSM-worded."""

    @pytest.mark.asyncio
    async def test_unreachable_node_is_first_rung(self, monkeypatch):
        import kiro_crew.instances.diagnostics as diag

        async def no_node(*a, **k):
            return False

        monkeypatch.setattr(diag, "_probe_ssm_managed", no_node)
        res = await diag.diagnose_instance_ssm("i-0123456789abcdef0", 7777, 7777)
        assert res.code == diag.SSM_UNREACHABLE
        assert res.ok is False
        # Must NOT tell an SSM user to check SSH access.
        assert "ssh" not in res.reason.lower()

    @pytest.mark.asyncio
    async def test_remote_down_then_not_connected_then_ok(self, monkeypatch):
        import kiro_crew.instances.diagnostics as diag

        async def node_ok(*a, **k):
            return True

        monkeypatch.setattr(diag, "_probe_ssm_managed", node_ok)

        async def dash_down(*a, **k):
            return False

        monkeypatch.setattr(diag, "_probe_remote_dashboard_ssm", dash_down)
        res = await diag.diagnose_instance_ssm("i-0123456789abcdef0", 7777, 7777)
        assert res.code == diag.REMOTE_DOWN

        async def dash_ok(*a, **k):
            return True

        monkeypatch.setattr(diag, "_probe_remote_dashboard_ssm", dash_ok)
        # local_port == 0 -> never connected (not a broken tunnel)
        res = await diag.diagnose_instance_ssm("i-0123456789abcdef0", 7777, 0)
        assert res.code == diag.NOT_CONNECTED

        async def fwd_ok(_lp):
            return True

        monkeypatch.setattr(diag, "_probe_local_forward", fwd_ok)
        res = await diag.diagnose_instance_ssm("i-0123456789abcdef0", 7777, 7777)
        assert res.code == diag.OK and res.ok is True

    @pytest.mark.asyncio
    async def test_invalid_target_short_circuits(self):
        import kiro_crew.instances.diagnostics as diag

        res = await diag.diagnose_instance_ssm("-oProxyCommand=evil", 7777, 7777)
        assert res.code == diag.UNKNOWN
        assert res.probes == []


class TestSsmExitErrorClassification:
    """SSM stderr must be classified with SSM vocabulary, not ssh's."""

    def _tunnel(self, stderr):
        from kiro_crew.instances.ssh_tunnel_manager import _SshTunnel

        t = _SshTunnel("ec2", "", 7777, 7777, transport="ssm", ssm_target="i-0123456789abcdef0")
        t._stderr_buf = stderr
        return t

    @pytest.mark.parametrize(
        "stderr,expected",
        [
            ("An error occurred (AccessDeniedException) ...", "IAM denied ssm:StartSession"),
            ("The security token included in the request is expired", "credentials missing"),
            ("SessionManagerPlugin is not found", "session-manager-plugin is not installed"),
            ("TargetNotConnected: i-0 is not connected", "not a connected managed node"),
        ],
    )
    def test_classification(self, stderr, expected):
        t = self._tunnel(stderr)
        msg = t._exit_error(255)
        assert expected.lower() in msg.lower()
        # Never mislabels an SSM failure as an ssh auth problem.
        assert "ssh auth failed" not in msg

    def test_probe_failure_message_shared(self):
        t = self._tunnel("")
        t._probe_failed = True
        assert "health probe failed" in t._exit_error(0)

    def test_ssh_classification_unchanged(self):
        """Regression guard: the ssh classifier still owns ssh instances."""
        from kiro_crew.instances.ssh_tunnel_manager import _SshTunnel

        t = _SshTunnel("dev", "dev-1", 7777, 7777)
        t._stderr_buf = "Permission denied (publickey)."
        assert "ssh auth failed" in t._exit_error(255)
