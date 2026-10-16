"""The instances registry and the injection-safe validators that guard it."""

from __future__ import annotations

from pathlib import Path

import pytest


class TestValidatorsRejectEmbeddedNewlines:
    """Every anchored validator in this package must use ``\\Z``, not ``$``.

    Python's ``$`` also matches just BEFORE a trailing newline, so a value like
    ``"20h\\n"`` passes a ``$``-anchored check and then reaches an ssh/ssm
    argument list carrying an embedded newline. Every regex here guards a value
    that ends up on such a command line, so this is one bug class rather than one
    regex — a ratchet is the only thing that keeps a future edit from
    reintroducing it.
    """

    def test_no_anchored_pattern_uses_a_dollar_anchor(self):
        import re as _re

        offenders = []
        for mod in ("registry", "validation", "constants"):
            path = (
                Path(__file__).resolve().parents[1]
                / "src"
                / "kiro_crew"
                / "instances"
                / f"{mod}.py"
            )
            for line in path.read_text().splitlines():
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                # An anchored pattern literal that still ends a branch with `$`.
                if _re.search(r'r"\^[^"]*\$(?:\|\^\$)?"', stripped):
                    offenders.append(f"{mod}.py: {stripped}")
        assert not offenders, (
            "anchored with `$`, which also matches before a trailing newline; "
            "use `\\Z`:\n  " + "\n  ".join(offenders)
        )

    def test_a_trailing_newline_never_survives_validation(self, tmp_path):
        """Two safe answers, and every guard must give one of them.

        ``validation.py`` SANITIZES — it strips before matching and returns the
        cleaned value, so a newline cannot reach the argument list it guards. The
        registry and the ttl check REJECT, because they persist what they are
        given. What must not happen is a guard accepting the value and passing the
        newline through unchanged.
        """
        from kiro_crew.instances import validation
        from kiro_crew.instances.registry import (
            InstancesRegistry,
            InvalidInstanceError,
            validate_ttl,
        )

        for fn, dirty in (
            (validation.validate_ssh_host, "host\n"),
            (validation.validate_remote_bin, "/usr/bin/kirocrew\n"),
            (validation.validate_ssm_target, "i-0123456789abcdef0\n"),
            (validation.validate_ssm_run_as, "ec2-user\n"),
            (validation.validate_aws_profile, "Admin\n"),
            (validation.validate_aws_region, "us-west-2\n"),
        ):
            cleaned = fn(dirty)
            assert "\n" not in cleaned, f"{fn.__name__} passed a newline through"

        # The persisting layers refuse outright rather than silently rewriting.
        with pytest.raises(InvalidInstanceError):
            validate_ttl("20h\n")
        reg = InstancesRegistry(tmp_path / "instances.json")
        with pytest.raises(InvalidInstanceError):
            reg.add(name="CD", ssh_host="cd-1-alias\n", instance_id="cd-1")
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        with pytest.raises(InvalidInstanceError):
            reg.update("cd-1", ttl="20h\n")


class TestSlugifyHashFallback:
    def test_non_ascii_names_derive_distinct_stable_ids(self) -> None:
        from kiro_crew.instances.registry import _ID_RE, _slugify

        chinese = _slugify("\u5f00\u53d1\u673a")
        arabic = _slugify("\u062e\u0627\u062f\u0645 \u0627\u0644\u062a\u0637\u0648\u064a\u0631")
        assert chinese.startswith("instance-")
        assert chinese != arabic
        assert chinese == _slugify("\u5f00\u53d1\u673a")
        assert _ID_RE.match(chinese)

    def test_ascii_names_are_unchanged(self) -> None:
        from kiro_crew.instances.registry import _slugify

        assert _slugify("Dev Box 2") == "dev-box-2"
