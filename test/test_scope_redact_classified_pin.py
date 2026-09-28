"""Pin ``scope_redact``'s classified/prose split against ``deny_diff``'s row loader.

``scope_redact.drop_changed_fields`` removes a row field whose value a redaction
would change, and keeps every field the classifier reads verbatim. Which fields
those are lives in ``_CLASSIFIED_ROW_FIELDS``, hand-written in a different program
from the loader that actually reads them (``deny_diff._row``).

Hand-written agreement drifts in one direction that matters. If ``_row`` later
reads a FOURTH field, a credential shape in it is pruned as prose, and the
differential then classifies a row nobody proposed -- a false green, on input the
author of the diff under review influences.

This cannot be caught at runtime inside the prune. An unrecognized field carrying
a live secret must still be scrubbed from what becomes a world-readable artifact,
and refusing the run over it is the very failure the prune exists to prevent. So the
check belongs here, in CI, before a fourth classified field ever ships.

The read-set is recovered from ``_row``'s SOURCE rather than by calling it: calling
it only reveals the fields the caller happened to supply, while the AST names every
``entry.get(...)`` the loader performs, including one added tomorrow.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import scope_redact as redact  # noqa: E402

DENY_DIFF = SCRIPTS / "deny_diff.py"


def _reads_off(source: str, *, func: str, subject: str) -> list[str | None]:
    """Every read off *subject* inside *func*, as its literal field name or ``None``.

    One walk answers both questions below: a literal name enumerates the read-set,
    and a ``None`` marks a read the literal scan cannot name, which would make that
    enumeration incomplete. Covers ``subject.get("name")`` and ``subject["name"]``.
    """
    tree = ast.parse(source)
    target = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == func
        ),
        None,
    )
    assert target is not None, f"{DENY_DIFF} no longer defines {func}()"

    reads: list[str | None] = []
    for node in ast.walk(target):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == subject
        ):
            key: ast.expr | None = node.args[0] if node.args else None
        elif (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Name)
            and node.value.id == subject
        ):
            key = node.slice
        else:
            continue
        literal = isinstance(key, ast.Constant) and isinstance(key.value, str)
        reads.append(key.value if literal else None)  # type: ignore[union-attr]
    return reads


def _row_loader_fields(source: str, *, func: str = "_row", subject: str = "entry") -> set[str]:
    """The literal field names ``func`` reads off *subject*."""
    return {name for name in _reads_off(source, func=func, subject=subject) if name is not None}


def _row_loader_dynamic_reads(source: str, *, func: str = "_row", subject: str = "entry") -> int:
    """How many reads off *subject* carry a field name the literal scan cannot see."""
    return sum(1 for name in _reads_off(source, func=func, subject=subject) if name is None)


@pytest.fixture(scope="module")
def deny_diff_source() -> str:
    assert DENY_DIFF.is_file(), f"{DENY_DIFF} is missing, so the split cannot be pinned"
    return DENY_DIFF.read_text(encoding="utf-8")


class TestTheClassifiedSplitAgreesWithTheLoader:
    def test_the_loader_reads_only_fields_the_split_accounts_for(
        self, deny_diff_source: str
    ) -> None:
        """A field the loader reads and the split never names is the false green.

        Redden here and the fix is a decision, not a guess: name the new field
        classified (a shape in it refuses, exit 11) or prose (a shape in it is
        removed). Leaving it unnamed silently chooses prose.
        """
        read = _row_loader_fields(deny_diff_source)

        unaccounted = read - set(redact._DENY_DIFF_ROW_FIELDS)

        assert not unaccounted, (
            f"deny_diff._row now reads {sorted(unaccounted)}, which scope_redact's split "
            "does not account for. Add each to _CLASSIFIED_ROW_FIELDS if a verdict depends "
            "on it, or to _DENY_DIFF_ROW_FIELDS alone if it is only rendered."
        )

    def test_the_split_claims_no_field_the_loader_stopped_reading(
        self, deny_diff_source: str
    ) -> None:
        """The other direction: a stale name here would over-protect a dead field.

        Harmless to the artifact, but it makes the docstring wrong about what the
        classifier reads, which is the thing a reader of this prune relies on.
        """
        read = _row_loader_fields(deny_diff_source)

        stale = set(redact._DENY_DIFF_ROW_FIELDS) - read

        assert not stale, (
            f"deny_diff._row no longer reads {sorted(stale)}; drop it from "
            "_DENY_DIFF_ROW_FIELDS (and from _CLASSIFIED_ROW_FIELDS if it is there)."
        )

    def test_every_classified_field_is_one_the_loader_actually_reads(
        self, deny_diff_source: str
    ) -> None:
        """Refusing a run over a field nobody classifies would be a scope regression."""
        read = _row_loader_fields(deny_diff_source)

        assert set(redact._CLASSIFIED_ROW_FIELDS) <= read, (
            f"_CLASSIFIED_ROW_FIELDS names {sorted(set(redact._CLASSIFIED_ROW_FIELDS) - read)}, "
            "which deny_diff._row does not read. Exit 11 would then refuse a run over text "
            "the classifier never sees."
        )

    def test_the_classified_set_is_a_subset_of_the_accounted_read_set(self) -> None:
        """Both tuples describe one loader, so the narrower must sit inside the wider."""
        assert set(redact._CLASSIFIED_ROW_FIELDS) <= set(redact._DENY_DIFF_ROW_FIELDS)

    def test_the_scan_sees_the_fields_the_loader_is_known_to_read(
        self, deny_diff_source: str
    ) -> None:
        """Guards the SCAN, not the split.

        An AST walk that matched nothing would make every assertion above vacuous,
        so the four fields the loader reads today are named here directly.
        """
        read = _row_loader_fields(deny_diff_source)

        assert {"kind", "command_or_flow", "platform", "reason"} <= read, (
            f"the source scan found only {sorted(read)}; it has stopped matching "
            "deny_diff._row's reads, so the pins above prove nothing."
        )

    def test_the_loader_reads_no_computed_field_name(self, deny_diff_source: str) -> None:
        """A computed field name would put reads outside the literal scan's reach."""
        dynamic = _row_loader_dynamic_reads(deny_diff_source)

        assert dynamic == 0, (
            "deny_diff._row now performs "
            f"{dynamic} read(s) whose field name is not a string literal, so the source "
            "scan can no longer enumerate its read-set and the pins above prove nothing."
        )


def _prune_case_arm(workflow: str, code: str) -> str:
    """Return one ``case`` arm of a lane's ``--drop-changed-fields`` prune.

    Read off the text rather than a YAML parse: the arm is shell inside a
    ``run:`` block, so the thing being pinned is the shell, and a parse would
    hand back the whole script anyway.
    """
    text = (REPO_ROOT / ".github" / "workflows" / workflow).read_text(encoding="utf-8")
    start = text.index("--drop-changed-fields --fail-if-changed")
    arm = text.index(f"{code})", start)
    return text[arm : text.index(";;", arm)]


class TestAClassifiedShapeRefusesOnlyWhereTheBaseRefAlreadyDid:
    """Exit 11 means a classified field carries a credential shape.

    The two lanes answer it differently, and the difference is not a
    preference. The same-repo lane probed the candidate set BEFORE
    ``validate`` at the base ref already, so refusing there refuses a run
    base refused. The fork lane probed only AFTER ``validate``, so a shape
    in a row ``validate`` drops during normalization was admitted at base --
    refusing it pre-``validate`` would red a case base passed. Both keep
    fail-closed: exit 11 writes nothing, so the shape survives into the
    probe on the normalized corpus, which refuses any row the legs classify.
    """

    def test_the_same_repo_lane_refuses(self) -> None:
        assert "exit 1" in _prune_case_arm("security-scope-review.yml", "11")

    def test_the_fork_lane_hands_it_to_validate(self) -> None:
        arm = _prune_case_arm("fork-security-scope-review.yml", "11")

        assert "exit 1" not in arm, (
            "the fork lane refuses pre-validate on exit 11 again. A credential shape in "
            "a row validate drops during normalization was admitted at the base ref, so "
            "this reds a case base passed; the probe on the normalized corpus is the "
            "backstop for any row that survives."
        )

    def test_the_fork_lane_still_names_the_field_it_found(self) -> None:
        """Handing on must not cost the author the only attribution available."""
        arm = _prune_case_arm("fork-security-scope-review.yml", "11")

        assert "::warning::" in arm and "the classifier reads verbatim" in arm

    def test_the_fork_lane_post_validate_probe_still_refuses(self) -> None:
        """The backstop the arm above defers to must actually be a refusal."""
        text = (REPO_ROOT / ".github" / "workflows" / "fork-security-scope-review.yml").read_text(
            encoding="utf-8"
        )
        probe = text.index('"$REDACTOR" --mode json --fail-if-changed scrub-probe.json')

        assert "exit 1" in text[probe : probe + 2000]
