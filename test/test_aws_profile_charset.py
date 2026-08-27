"""Regression tests for the AWS-profile character class (#6055, #6063).

IAM Identity Center derives profile names shaped ``<account>+<permission-set>``
(e.g. ``AdminAccess+dev``), so ``+`` must be accepted. The SSM/instances pair
was fixed by #6051 and four more hand copies by #6055; #6063 consolidated the
shape into ``kiro_crew.constants.AWS_PROFILE_NAME_RE`` as the single source of
truth. These tests pin every consumer:

* ``kiro_crew.cloud.ec2._PROFILE_SPEC`` (EC2 wizard — aliases
  ``profiles.PROFILE_SPEC``)
* ``kiro_crew.deploy.profiles._PROFILE_RE`` (deploy profile registry + the
  ``aws configure list-profiles`` discovery filter)
* ``kiro_crew.deploy.handlers._PROFILE_SPEC`` (deploy-web HTTP boundary —
  aliases ``profiles.PROFILE_SPEC``)
* ``kiro_crew.validation._WM_PROFILE_RE`` (workspace-manager webapp_metadata)
* ``kiro_crew.instances.validation._AWS_PROFILE_RE`` (SSM tunnel inputs;
  ``instances.registry`` aliases it for its early record check)
* ``kiro_crew.aws_consent._PROFILE_RE`` (identity probe — a DELIBERATE
  near-sibling that derives its class from the shared fragments)
* the two standalone artifact-deploy scripts (``attach_backend.py`` /
  ``detach_backend.py``), which cannot import the package and embed the
  pattern verbatim under the byte-equality drift guard below

All these values only ever reach a subprocess as a discrete
``--profile <value>`` argv element (never a shell string), so the class must
still exclude whitespace and shell metacharacters, must reject option-shaped
(leading ``-``) values, and must anchor with ``\\Z`` so a trailing newline
cannot slip past ``$``'s end-of-line leniency.
"""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path

import pytest

from kiro_crew import aws_consent, constants
from kiro_crew import validation as validation_mod
from kiro_crew.cloud import aws as cloud_aws
from kiro_crew.cloud import ec2
from kiro_crew.deploy import engine as engine_mod
from kiro_crew.deploy import handlers
from kiro_crew.deploy import profiles as profiles_mod
from kiro_crew.instances import registry as instances_registry
from kiro_crew.instances import validation as instances_validation
from kiro_crew.validation import (
    ARTIFACT_SAVE_SCHEMA,
    ValidationError,
    validate_field,
    validate_tool_args,
)

_POSIX_ONLY = pytest.mark.skipif(
    os.name == "nt", reason="deploy profile discovery is POSIX-only by design"
)

_PATTERNS = {
    # ec2 and handlers alias profiles.PROFILE_SPEC (same idiom as handlers'
    # _REGION_SPEC); pin each boundary's ACTUAL pattern so a future divergent
    # local copy is still caught here. aws_consent keeps a deliberately
    # different shape (leading alnum only, '@'/'=' admitted) but shares every
    # property these tables assert.
    "cloud.ec2": ec2._PROFILE_SPEC.pattern,
    "deploy.profiles": profiles_mod._PROFILE_RE,
    "deploy.handlers": handlers._PROFILE_SPEC.pattern,
    "workspace-manager": validation_mod._WM_PROFILE_RE,
    "instances.validation": instances_validation._AWS_PROFILE_RE,
    "aws-consent": aws_consent._PROFILE_RE,
}

_ACCEPTED = [
    "AdminAccess+dev",  # the IAM Identity Center shape from the report
    "123456789012+PowerUserAccess",
    "p",  # single char (quantifier still means >= 1)
    "a.b_c-d",  # the full legacy charset keeps working
    "dev+test.2-x_9",
]

_REJECTED = [
    "-leading-dash",  # option-shaped: must never reach --profile argv
    "--profile",
    "+extra trailing junk",  # whitespace stays excluded even around '+'
    "has space",
    "semi;colon",
    "pipe|pipe",
    "dollar$(x)",
    "back`tick",
    "newline\ninside",
    "trailing\n",  # \Z regression: $ matched just before a trailing newline
    "tab\t",
    "",
]


class TestProfilePatternCharset:
    """The compiled patterns themselves, exercised via .match like production."""

    @pytest.mark.parametrize("site", sorted(_PATTERNS))
    @pytest.mark.parametrize("value", _ACCEPTED)
    def test_accepts_legal_profiles(self, site: str, value: str) -> None:
        assert _PATTERNS[site].match(value), f"{site} rejected legal profile {value!r}"

    @pytest.mark.parametrize("site", sorted(_PATTERNS))
    @pytest.mark.parametrize("value", _REJECTED)
    def test_rejects_unsafe_values(self, site: str, value: str) -> None:
        assert not _PATTERNS[site].match(value), f"{site} accepted unsafe value {value!r}"


class TestEc2ValidateProfile:
    def test_plus_profile_accepted(self) -> None:
        assert ec2.validate_profile("AdminAccess+dev") == "AdminAccess+dev"

    @pytest.mark.parametrize("bad", ["-foo", "evil;rm -rf", "a b"])
    def test_unsafe_profiles_rejected(self, bad: str) -> None:
        with pytest.raises(ValidationError):
            ec2.validate_profile(bad)

    def test_empty_profile_still_allowed(self) -> None:
        # Empty means "no --profile" downstream; the pattern is only enforced
        # on non-empty values (pre-existing semantics, must not change).
        assert ec2.validate_profile("") == ""

    def test_trailing_newline_normalized_by_sanitizer(self) -> None:
        # validate_field strips via sanitize_string BEFORE the pattern check,
        # so a trailing newline is normalized away rather than rejected here;
        # the \Z anchor is the defense for the raw-match call sites.
        assert ec2.validate_profile("AdminAccess+dev\n") == "AdminAccess+dev"


class TestDeployProfilesSpec:
    def test_plus_profile_accepted(self) -> None:
        assert validate_field("AdminAccess+dev", profiles_mod.PROFILE_SPEC) == "AdminAccess+dev"

    @pytest.mark.parametrize("bad", ["-foo", "evil;rm -rf"])
    def test_unsafe_profiles_rejected(self, bad: str) -> None:
        with pytest.raises(ValidationError):
            validate_field(bad, profiles_mod.PROFILE_SPEC)

    @_POSIX_ONLY
    def test_discovery_keeps_plus_profiles_and_drops_option_shaped(self, monkeypatch) -> None:
        # `aws configure list-profiles` output lines are stripped before the
        # filter, so the \Z anchor is behavior-neutral here; the widened class
        # is what lets SSO-derived names show up in discovery at all.
        out = "default\nAdminAccess+dev\n-cursed\nbad name!\nok.two\n"
        monkeypatch.setattr(profiles_mod.engine, "run_aws", lambda *a, **k: (0, out, ""))
        assert profiles_mod.discover_aws_profiles() == ["default", "AdminAccess+dev", "ok.two"]


class TestDeployHandlersSpec:
    def test_plus_profile_accepted(self) -> None:
        assert validate_field("AdminAccess+dev", handlers._PROFILE_SPEC) == "AdminAccess+dev"

    @pytest.mark.parametrize("bad", ["-foo", "evil;rm -rf"])
    def test_unsafe_profiles_rejected(self, bad: str) -> None:
        with pytest.raises(ValidationError):
            validate_field(bad, handlers._PROFILE_SPEC)

    def test_empty_profile_still_allowed(self) -> None:
        # "" clears the profile (falls back to default) — pre-existing
        # empty-value semantics that the charset change must not disturb.
        assert validate_field("", handlers._PROFILE_SPEC) == ""


class TestWorkspaceManagerProfile:
    @staticmethod
    def _args(profile: str) -> dict:
        return {
            "name": "t",
            "content": "x",
            "kind": "webapp",
            "webapp_metadata": {"deploy_target": {"profile": profile}},
        }

    def test_plus_profile_accepted(self) -> None:
        result = validate_tool_args(self._args("AdminAccess+dev"), ARTIFACT_SAVE_SCHEMA)
        assert result["webapp_metadata"]["deploy_target"]["profile"] == "AdminAccess+dev"

    @pytest.mark.parametrize("bad", ["-foo", "evil;rm -rf"])
    def test_unsafe_profiles_rejected(self, bad: str) -> None:
        with pytest.raises(ValidationError, match="invalid profile"):
            validate_tool_args(self._args(bad), ARTIFACT_SAVE_SCHEMA)

    def test_trailing_newline_rejected_raw(self) -> None:
        # This call site matches the raw stored value (no strip), so the old $
        # anchor let "p\n" through — \Z is a real tightening here.
        assert not validation_mod._WM_PROFILE_RE.match("AdminAccess+dev\n")

    def test_empty_profile_still_allowed(self) -> None:
        result = validate_tool_args(self._args(""), ARTIFACT_SAVE_SCHEMA)
        assert result["webapp_metadata"]["deploy_target"]["profile"] == ""


class TestPatternLengthBound:
    """The quantifier bounds length to 128 inside the pattern itself, matching
    the max_len=128 caps the FieldSpec sites enforce (and #6051's sibling)."""

    @pytest.mark.parametrize("site", sorted(_PATTERNS))
    def test_128_chars_accepted_129_rejected(self, site: str) -> None:
        assert _PATTERNS[site].match("a" * 128)
        assert not _PATTERNS[site].match("a" * 129)


class TestDiscreteArgvIntegration:
    """The safety argument for the widened charset rests on the profile only
    ever reaching the aws CLI as a discrete ``--profile <value>`` argv pair —
    pin that end-to-end for both subprocess chokepoints."""

    def test_cloud_build_argv_keeps_plus_profile_discrete(self) -> None:
        argv = cloud_aws._build_argv(["sts", "get-caller-identity"], "AdminAccess+dev", "")
        assert argv[-2:] == ["--profile", "AdminAccess+dev"]

    def test_deploy_engine_aws_keeps_plus_profile_discrete(self) -> None:
        argv = engine_mod._aws(["s3", "ls"], "AdminAccess+dev")
        assert argv[-2:] == ["--profile", "AdminAccess+dev"]


class TestSharedConstantAdoption:
    """#6063: one shared shape. Every in-package alias site must bind the SAME
    compiled object, so a re-spelled local copy fails here immediately."""

    def test_all_alias_sites_share_one_compiled_pattern(self) -> None:
        assert profiles_mod._PROFILE_RE is constants.AWS_PROFILE_NAME_RE
        assert validation_mod._WM_PROFILE_RE is constants.AWS_PROFILE_NAME_RE
        assert instances_validation._AWS_PROFILE_RE is constants.AWS_PROFILE_NAME_RE
        assert instances_registry._AWS_PROFILE_RE is constants.AWS_PROFILE_NAME_RE
        assert ec2._PROFILE_SPEC.pattern is constants.AWS_PROFILE_NAME_RE
        assert handlers._PROFILE_SPEC.pattern is constants.AWS_PROFILE_NAME_RE

    def test_pattern_composes_from_the_charset_fragments(self) -> None:
        # The fragments are the derivation surface for near-siblings
        # (aws_consent); the composed pattern is the verbatim-embed surface for
        # the standalone scripts. Pin both relationships.
        assert constants.AWS_PROFILE_NAME_PATTERN == (
            "^[" + constants.AWS_PROFILE_FIRST_CHARS + "]"
            "[" + constants.AWS_PROFILE_CHARS + "]{0,127}\\Z"
        )
        assert constants.AWS_PROFILE_NAME_RE.pattern == constants.AWS_PROFILE_NAME_PATTERN

    def test_dash_stays_last_in_the_continuation_class(self) -> None:
        # AWS_PROFILE_CHARS is TERMINAL-ONLY: its trailing literal '-' becomes
        # a RANGE if anything is appended after it. AWS_PROFILE_FIRST_CHARS is
        # the composable fragment (no literal '-'), which is what aws_consent
        # composes from. Pin the whole contract, not just the dash position.
        assert constants.AWS_PROFILE_CHARS.endswith("-")
        assert not constants.AWS_PROFILE_FIRST_CHARS.endswith("-")
        assert constants.AWS_PROFILE_CHARS == constants.AWS_PROFILE_FIRST_CHARS + "-"
        # The failure mode itself: appending after AWS_PROFILE_CHARS creates
        # the "+-@" range (0x2B-0x40) admitting '/', ':' and ';' — while the
        # sanctioned FIRST_CHARS composition admits none of them.
        misused = re.compile(f"^[{constants.AWS_PROFILE_CHARS}@=]+\\Z")
        assert misused.match("a/b:c;d"), "range hazard gone? update this contract test"
        sanctioned = re.compile(f"^[A-Za-z0-9][{constants.AWS_PROFILE_FIRST_CHARS}@=-]*\\Z")
        for ch in "/:;":
            assert not sanctioned.match(f"a{ch}b")
        assert sanctioned.match("a@b=c+d-e_f.g")


_SCRIPTS_DIR = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "kiro_crew"
    / "deploy"
    / "skills"
    / "artifact-deploy"
    / "scripts"
)
_SCRIPT_PROFILE_LITERAL_RE = re.compile(r'_PROFILE_RE = _?re\.compile\(r"([^"]+)"\)')


def _scripts_with_profile_pattern() -> list[str]:
    """Self-enrolling drift-guard roster: every standalone script that carries
    its own _PROFILE_RE hand copy is discovered, so the NEXT script to copy the
    pattern is guarded automatically rather than silently skipped."""
    return sorted(
        p.name for p in _SCRIPTS_DIR.glob("*.py") if "_PROFILE_RE" in p.read_text(encoding="utf-8")
    )


class TestStandaloneScriptDriftGuard:
    """The artifact-deploy scripts run standalone (no package import), so they
    embed AWS_PROFILE_NAME_PATTERN verbatim. Byte-equality against the shared
    source is what stops a new hand copy from diverging (#6063), mirroring the
    repo's other verbatim-copy guards."""

    def test_roster_discovers_the_known_copies(self) -> None:
        # The glob must never silently go empty (renamed dir, moved scripts):
        # an empty roster would green-light unguarded drift.
        assert set(_scripts_with_profile_pattern()) >= {
            "attach_backend.py",
            "detach_backend.py",
        }

    @pytest.mark.parametrize("script", _scripts_with_profile_pattern())
    def test_script_literal_is_byte_identical_to_shared_pattern(self, script: str) -> None:
        source = (_SCRIPTS_DIR / script).read_text(encoding="utf-8")
        found = _SCRIPT_PROFILE_LITERAL_RE.search(source)
        assert found, f"{script}: _PROFILE_RE literal not found (extractor drift?)"
        assert found.group(1) == constants.AWS_PROFILE_NAME_PATTERN, (
            f"{script}: embedded profile pattern diverged from "
            "constants.AWS_PROFILE_NAME_PATTERN — update the verbatim copy"
        )

    def test_shared_semantics_close_the_scripts_old_gaps(self) -> None:
        # The pre-#6063 script class was ^[a-zA-Z0-9._:/+-]+$ — unbounded,
        # option-shaped values admitted, ':' and '/' admitted, $-anchored.
        pat = re.compile(constants.AWS_PROFILE_NAME_PATTERN)
        assert pat.match("AdminAccess+dev")
        for bad in ("-leading-dash", "a:b", "a/b", "dev\n", "a" * 129, ""):
            assert not pat.match(bad), f"shared pattern accepted {bad!r}"


class TestAwsConsentProfileShape:
    """aws_consent keeps its DELIBERATE differences (leading alnum only,
    '@'/'=' admitted for existing configs) but derives its class from the
    shared fragments and anchors with \\Z. There is deliberately NO
    probe-local strip: the probe must judge the same raw value the paid
    consumers use (#6063)."""

    @pytest.fixture(autouse=True)
    def _clean_probe_cache(self):
        # probe_identity WRITES _probe_cache even with use_cache=False (the
        # flag only skips the read), and a leaked ok=True entry stays valid
        # for _PROBE_TTL_SECS — a later test on the same worker calling the
        # real probe would inherit a fabricated success. Mirrors
        # test_aws_consent.py's autouse clear.
        aws_consent._probe_cache.clear()
        yield
        aws_consent._probe_cache.clear()

    @pytest.mark.parametrize("value", ["user@site", "role=admin", "AdminAccess+dev"])
    def test_legacy_wider_charset_still_accepted(self, value: str) -> None:
        assert aws_consent._PROFILE_RE.match(value)

    @pytest.mark.parametrize("value", ["-leading", "_leading", ".leading", "+leading"])
    def test_first_char_stays_alnum_only(self, value: str) -> None:
        assert not aws_consent._PROFILE_RE.match(value)

    def test_trailing_newline_rejected_raw(self) -> None:
        # The old $ anchor matched just before a trailing newline.
        assert not aws_consent._PROFILE_RE.match("dev\n")

    def test_probe_refuses_padded_values_fail_closed(self) -> None:
        # No probe-local normalization: the paid consumers (boto3 sessions,
        # the --profile argv sites) use the same raw config value, so a probe
        # that validated a stripped COPY could record consent for a target the
        # real request never uses. A padded value must be REFUSED (the shape
        # gate runs before any CLI resolution, so no patching is needed),
        # never silently rewritten.
        for padded in (" dev ", "dev ", "dev\n", "\tdev"):
            identity = asyncio.run(aws_consent.probe_identity(padded, "us-east-1", use_cache=False))
            assert not identity.ok, f"padded profile {padded!r} was not refused"

    def test_probe_tolerates_none_profile_without_crashing(self, monkeypatch) -> None:
        # A JSON null in config reaches the probe as None; the falsy skip in
        # _inputs_are_safe has always tolerated it ("use the default
        # credential chain") and #6063 must not turn it into a crash.
        def _fake_run_aws(args: list, profile, region):
            return 0, '{"Account": "111122223333", "Arn": "arn:aws:iam::1:x"}', ""

        monkeypatch.setattr(aws_consent, "_run_aws", _fake_run_aws)
        monkeypatch.setattr(aws_consent, "_aws_cli_resolvable", lambda: True)
        identity = asyncio.run(
            aws_consent.probe_identity(None, "", use_cache=False)  # type: ignore[arg-type]
        )
        assert identity.ok

    def test_probe_identity_still_refuses_interior_whitespace(self) -> None:
        # Whitespace anywhere in the value is unsafe; the shape gate runs
        # before any CLI resolution, so no patching is needed.
        identity = asyncio.run(aws_consent.probe_identity("de v", "us-east-1", use_cache=False))
        assert not identity.ok
