r"""Contract: client-facing request-path validators reject trailing newlines.

Python's ``$`` regex anchor matches immediately BEFORE a trailing newline, so a
``$``-anchored pattern used with ``.match`` accepts its own valid input with a
``"\n"`` suffix. For a validator on the request path -- a client-supplied value
that reaches a subprocess argv or a filesystem path -- that admits a value the
pattern's author never meant to accept. The fix for the class is anchoring at
``\Z``, which matches only at the true end of the string.

This module guards the class two ways:

* **Behaviorally** -- every registered request-path validator accepts a known
  valid input and rejects the same input with ``"\n"`` appended.
* **Structurally** -- no module-level compiled pattern in any module of the
  registered request-path packages carries an unescaped ``$``
  (``re.MULTILINE`` patterns are exempt: per-line anchoring is the point of
  that flag, and such patterns parse trusted tool output, not client input).
  Modules are discovered by walking the package, so a validator added to a new
  or existing module inside a registered package is covered without editing
  this file.

When a NEW package gains client-facing validators, add it to
``_REQUEST_PATH_PACKAGES`` and its canonical patterns to ``_VALIDATORS``.

Not every registered pattern is reachable with a trailing newline at its
boundary: one routed through ``validation.validate_field`` (a ``FieldSpec``
``pattern``) sits behind ``sanitize_string()``, which strips before matching,
so for those entries ``\Z`` is defense-in-depth against a future raw call
site rather than a measured boundary change. Patterns matched raw
(``.match``/``.fullmatch`` on the unsanitized value) are the ones this
contract tightens directly.
"""

from __future__ import annotations

import importlib
import pkgutil
import re
from types import ModuleType

import pytest

from kiro_crew import artifacts, cloud, crew_log, skill_providers, validation
from kiro_crew.apps.builtins.papyrus import backend as papyrus_backend
from kiro_crew.apps.builtins.papyrus.backend import gitops, store
from kiro_crew.cloud import config as cloud_config
from kiro_crew.cloud import ec2, fargate_engine, login_target, ssm
from kiro_crew.cloud.fargate import identity, runtask, taskdef
from kiro_crew.crew_log import schema as crew_log_schema
from kiro_crew.skill_providers import github as skill_provider_github

#: Packages whose modules validate client-supplied input on a request path.
#: The structural walk imports every module in each -- recursively, so a
#: sub-package (``cloud/fargate/``) and any package added under a registered
#: root later are covered without editing this file.
_REQUEST_PATH_PACKAGES = (papyrus_backend, cloud, skill_providers, crew_log)

#: (pattern, canonical valid input) for every request-path validator. The
#: behavioral half of the contract runs each through accept and reject cases.
#: GIT_URL_RE gets one entry per regex ALTERNATION (the URL form and the
#: scp-like form each carry their own anchor), not one per transport.
_VALIDATORS = (
    pytest.param(gitops.GIT_URL_RE, "https://example.com/group/paper.git", id="git-url-url-form"),
    pytest.param(gitops.GIT_URL_RE, "git@example.com:group/paper.git", id="git-url-scp-form"),
    pytest.param(store.PROJECT_NAME_RE, "paper", id="project-name"),
    pytest.param(artifacts._TAG_RE, "artifact-tag", id="artifact-store-tag"),
    pytest.param(validation._ARTIFACT_TAG_RE, "artifact-tag", id="artifact-mcp-tag"),
    pytest.param(cloud_config._TAG_RE, "my-crew-1", id="cloud-config-tag"),
    pytest.param(ec2._TAG_RE, "my-crew-1", id="cloud-ec2-tag"),
    pytest.param(ec2._REGION_RE, "us-east-1", id="cloud-ec2-region"),
    pytest.param(ec2._CIDR_RE, "10.0.0.0/16", id="cloud-ec2-cidr"),
    pytest.param(ec2._REPO_RE, "https://github.com/kirodotdev/KiroCrew.git", id="cloud-ec2-repo"),
    pytest.param(ec2._REF_RE, "release/1.2", id="cloud-ec2-ref"),
    pytest.param(ec2._PROFILE_SPEC.pattern, "default", id="cloud-ec2-profile"),
    pytest.param(ec2._SUBNET_ID_RE, "subnet-0123456789abcdef0", id="cloud-ec2-subnet-id"),
    pytest.param(fargate_engine._TAG_VALUE_RE, "kirocrew_launch-1", id="cloud-engine-tag-value"),
    pytest.param(login_target._REGION_RE, "ap-southeast-2", id="cloud-login-region"),
    pytest.param(login_target._HOSTNAME_RE, "example.awsapps.com", id="cloud-login-hostname"),
    pytest.param(ssm._USERNAME_RE, "ec2-user", id="cloud-ssm-username"),
    pytest.param(identity._CREW_RE, "my-crew", id="cloud-identity-crew"),
    pytest.param(identity._PARTITION_RE, "aws-us-gov", id="cloud-identity-partition"),
    pytest.param(identity._ACCOUNT_RE, "123456789012", id="cloud-identity-account"),
    pytest.param(identity._REGION_RE, "us-east-1", id="cloud-identity-region"),
    pytest.param(
        identity._SECRET_NAME_RE,
        "kirocrew/crew/my-crew/gateway_token",
        id="cloud-identity-secret-name",
    ),
    pytest.param(identity._SECRET_SUFFIX_RE, "Ab12Cd", id="cloud-identity-secret-suffix"),
    pytest.param(identity._SECRET_TAIL_RE, "gateway_token-Ab12Cd", id="cloud-identity-secret-tail"),
    pytest.param(runtask._TAG_VALUE_RE, "kirocrew_launch-1", id="cloud-runtask-tag-value"),
    pytest.param(
        taskdef._DIGEST_REF_RE,
        "public.ecr.aws/kirocrew/crew@sha256:" + "0" * 64,
        id="cloud-taskdef-digest-ref",
    ),
    pytest.param(skill_provider_github._OWNER_RE, "acme", id="skill-provider-owner"),
    pytest.param(skill_provider_github._REPO_RE, "widgets", id="skill-provider-repo"),
    pytest.param(skill_provider_github._FULL_SHA_RE, "0" * 40, id="skill-provider-commit"),
    pytest.param(skill_provider_github._SEGMENT_RE, "SKILL.md", id="skill-provider-path-segment"),
    pytest.param(skill_provider_github._REF_RE, "release/1.2", id="skill-provider-ref"),
    pytest.param(crew_log_schema._SEGMENT_RE, "activity", id="crew-log-name-segment"),
)

#: Registered for the STRUCTURAL half only. ``skill_providers.github._ADDRESS_RE``
#: splits ``owner/repo`` out of an address, and its ``[^/@:]`` classes admit a
#: newline, so the whole address plus ``"\n"`` matches it even anchored at ``\Z``
#: -- the newline is refused one step later, when ``_OWNER_RE`` and ``_REPO_RE``
#: judge the parts it captured. Its anchor still has to be ``\Z`` (the structural
#: half enforces that), and ``test_skill_provider_github.py`` owns the boundary.
_SPLITTERS_WITH_NO_BOUNDARY_CASE = (skill_provider_github._ADDRESS_RE,)

#: A ``$`` that is a real anchor: preceded by an EVEN number of backslashes
#: (``\$`` is a literal dollar; ``\\$`` is an escaped backslash then an
#: anchor). Deliberately a heuristic: a literal ``$`` inside a character class
#: is flagged too -- that fails loud with a clear message. One walked pattern
#: carries such a literal: ``cloud/login_target.py``'s ``_CONTROL_OR_SHELL_RE``
#: spells it ``\$`` (a semantic no-op inside ``[...]``) precisely so this
#: heuristic does not flag it; do not "simplify" that escape away.
_UNESCAPED_DOLLAR = re.compile(r"(?<!\\)(?:\\\\)*\$")


def _package_modules(package: ModuleType) -> list[ModuleType]:
    """Every module inside *package*, recursively, imported."""
    return [
        importlib.import_module(info.name)
        for info in pkgutil.walk_packages(package.__path__, prefix=f"{package.__name__}.")
    ]


class TestTrailingNewlineContract:
    @pytest.mark.parametrize("pattern,valid", _VALIDATORS)
    def test_valid_input_is_accepted(self, pattern: re.Pattern[str], valid: str) -> None:
        """Tightening the anchor must not break the accept case."""
        assert pattern.match(valid) is not None

    @pytest.mark.parametrize("pattern,valid", _VALIDATORS)
    def test_valid_input_plus_newline_is_rejected(
        self, pattern: re.Pattern[str], valid: str
    ) -> None:
        assert (
            pattern.match(valid + "\n") is None
        ), f"{pattern.pattern!r} accepts a trailing newline -- anchor with \\Z, not $"

    def test_no_request_path_pattern_is_dollar_anchored(self) -> None:
        """The structural half: a ``$``-anchored validator anywhere in a
        request-path package fails here even before it has a behavioral entry."""
        offenders = [
            f"{module.__name__}.{name}"
            for package in _REQUEST_PATH_PACKAGES
            for module in _package_modules(package)
            for name, value in sorted(vars(module).items())
            if isinstance(value, re.Pattern)
            and isinstance(value.pattern, str)
            and not value.flags & re.MULTILINE
            and _UNESCAPED_DOLLAR.search(value.pattern)
        ]
        assert not offenders, (
            "request-path validators must anchor with \\Z, not $ (Python's $ "
            f"matches before a trailing newline): {offenders}"
        )

    def test_walk_sees_the_known_validators(self) -> None:
        """Guards the walk itself: if package discovery silently breaks (an
        import error path, a renamed package), the structural test would pass
        vacuously -- so assert the walk still reaches the known validator
        modules."""
        walked = {m.__name__ for p in _REQUEST_PATH_PACKAGES for m in _package_modules(p)}
        assert gitops.__name__ in walked
        assert store.__name__ in walked
        assert ec2.__name__ in walked
        assert identity.__name__ in walked
        assert skill_provider_github.__name__ in walked
        assert crew_log_schema.__name__ in walked

    def test_a_splitter_with_no_boundary_case_is_still_z_anchored(self) -> None:
        """A pattern exempt from the behavioral half is not exempt from the rule.

        Its own character classes admit the newline, so only the structural half
        can speak for it -- state that here rather than leaving a reader to infer
        the omission from the registry.
        """
        for pattern in _SPLITTERS_WITH_NO_BOUNDARY_CASE:
            assert not _UNESCAPED_DOLLAR.search(pattern.pattern)
            assert pattern.pattern.endswith(r"\Z")
