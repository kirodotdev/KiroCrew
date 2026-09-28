"""The operator-facing Fargate hand-edit guide exists and its fields track the code.

The `fargate` block in `cloud.json` is the only supported way to configure the
Fargate remote-crew lane: there is no wizard step, no dashboard form, and no
`cloud config` CLI verb that writes it, and the file is sealed against agent
edits. `docs/guides/remote-crew-on-fargate.md` is the operator-facing surface that
says so, lists the required fields, and gives the reason — the place an operator
reads before launch.

This test pins two things so the guide tracks the code: that it is DISCOVERABLE
(linked from the guides index), and that the fields it tells an operator to write
are the real `FargateConfig` fields — so a field renamed, added, or dropped in the
code fails here instead of leaving the guide out of step.

Prose is matched against the whitespace-collapsed text so re-wrapping a paragraph
is never a failure — the claim is the words, not the column the line breaks at.
"""

import dataclasses
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GUIDE = ROOT / "docs" / "guides" / "remote-crew-on-fargate.md"
GUIDES_INDEX = ROOT / "docs" / "guides" / "README.md"


@pytest.fixture(scope="module")
def guide_text() -> str:
    return GUIDE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def guide_flat(guide_text: str) -> str:
    return re.sub(r"\s+", " ", guide_text)


def test_the_guide_is_linked_from_the_guides_index() -> None:
    """An operator finds the guide from the guides README, not by knowing its path."""
    index = GUIDES_INDEX.read_text(encoding="utf-8")
    assert "remote-crew-on-fargate.md" in index, (
        "the Fargate guide is not linked from docs/guides/README.md, so an operator "
        "looking before launch cannot discover it"
    )


def test_the_guide_states_the_hand_edit_path_and_its_reason(guide_flat: str) -> None:
    """The finding is a MISSING statement; assert the statement is present.

    The guide must say (a) hand-editing cloud.json is the path, (b) there is no
    wizard/form/CLI yet, and (c) the file is operator-owned / sealed against agent
    edits — the reason the product does not write it for you.
    """
    assert "cloud.json" in guide_flat
    assert "no wizard step" in guide_flat and "no dashboard form" in guide_flat
    assert "sealed against agent" in guide_flat


def test_every_field_the_guide_documents_is_a_real_fargate_config_field(
    guide_text: str,
) -> None:
    """Each key the guide tells an operator to write exists on FargateConfig.

    Read off the dataclass rather than a second copy of the field list, so a
    renamed or dropped field reddens this test instead of leaving the guide
    pointing at a key the reader's edit would then be ignored for.
    """
    from kiro_crew.cloud.config import FargateConfig

    real = {f.name for f in dataclasses.fields(FargateConfig)}
    documented = {
        "cluster",
        "subnets",
        "security_groups",
        "image",
        "secrets",
        "cpu_architecture",
        "assign_public_ip",
        "task_ttl_seconds",
        "internal_only",
    }
    assert (
        documented <= real
    ), f"the guide documents fargate keys not on FargateConfig: {sorted(documented - real)}"
    for key in documented:
        assert f"`{key}`" in guide_text, f"the guide is missing the {key} field"


def test_the_guide_lists_exactly_the_required_fields_completeness_judges(
    guide_flat: str,
) -> None:
    """The Required column marks yes for exactly the fields is_complete() gates.

    `is_complete` gates cluster, subnets, security_groups, image, secrets and
    cpu_architecture (which has a default but must be in the accepted set);
    internal_only is deliberately NOT part of the judgement and task_ttl_seconds
    is optional. The guide's table must not tell an operator a field is required
    when the lane registers without it, or optional when it does not.
    """
    # The four placement/image/secret fields with no usable default are marked required.
    for key in ("cluster", "subnets", "security_groups", "image", "secrets"):
        assert (
            f"| `{key}` | yes |" in guide_flat
        ), f"the guide's field table does not mark {key} as required"
    # internal_only is explicitly documented as optional and not part of completeness.
    assert "| `internal_only` | no |" in guide_flat


def test_the_guide_carries_the_internal_only_security_note(guide_flat: str) -> None:
    """internal_only loosens the sandbox; the guide must carry the exposure it accepts.

    It writes SMC_INTERNAL_ONLY and starts the model subprocess unsandboxed, and
    the guide must NOT let a reader read it as "no untrusted input reaches this
    task" — the exact misreading the code's own docstring warns against.
    """
    assert "SMC_INTERNAL_ONLY" in guide_flat
    assert "unsandboxed" in guide_flat
    assert 'does **not** mean "no untrusted input reaches this task."' in guide_flat
