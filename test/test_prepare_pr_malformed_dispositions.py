"""Which malformed disposition markers block PR Readiness, and which are notices.

A comment that starts with the disposition prefix but carries a marker that
does not parse still reaches the adjudication ledger. It blocks only while it
names the current head and no later well-formed record from the same writer
replaces it; otherwise the gate reports it as a notice.
"""

from __future__ import annotations

from pathlib import Path
from types import ModuleType

import pytest
from skill_script_helpers import load_skill_script

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "kiro_crew"
    / "builtin_skills"
    / "kirocrew-dev"
    / "kirocrew-prepare-pr"
    / "scripts"
    / "pr_findings.py"
)
_HEAD = "f" * 40
_OLD = "a" * 40


def _load_script() -> ModuleType:
    return load_skill_script("prepare_pr_findings", SCRIPT)


class TestMalformedMarkers:
    def _bindings(self, module: ModuleType) -> dict:
        return dict(module.DEFAULT_MARKER_BINDINGS)

    def _record(self, target: str, spans: list[str], head: str = _HEAD, **overrides) -> dict:
        record = {
            "author": "alice",
            "comment_id": 1,
            "target": target,
            "head": head,
            "spans": spans,
            "malformed": False,
        }
        record.update(overrides)
        return record

    def test_malformed_marker_keeps_the_target_and_head_it_names(self) -> None:
        """span= written inside the marker breaks the strict parse, but the lane
        and head it judged still read, so the gate can tell whether it is stale
        and still run the claim checks over it."""
        module = _load_script()
        span = module.span_hash("a.py", "gpt/BLOCKING")
        body = (
            f"<!-- ai-review-disposition target=gpt head={_OLD} span={span} -->\n"
            "- **fixed**\n> reason"
        )

        record = module.parse_disposition_record(
            {"id": 1, "user": {"login": "alice", "type": "User"}, "body": body}
        )

        assert record is not None
        assert record["malformed"] is True
        assert (record["target"], record["head"]) == ("gpt", _OLD)
        assert record["spans"] == [span]
        assert record["bullets"] == 1

    def _malformed(self, head: str, spans: list[str], **overrides) -> dict:
        return self._record("gpt", spans, head=head, malformed=True, bullets=1, **overrides)

    def test_malformed_marker_for_an_older_head_is_a_notice_not_a_block(self) -> None:
        """A malformed marker that judged an older head asks nothing of this
        one, so it must not leave deleting the comment as the only way out."""
        module = _load_script()
        notices: list = []

        violations = module.disposition_violations(
            [self._malformed(_OLD, ["0c70fb80f49f"], comment_id=9)],
            [],
            _HEAD,
            self._bindings(module),
            notices,
        )

        assert violations == []
        assert len(notices) == 1
        assert "comment 9 by alice" in notices[0]
        assert "older head" in notices[0]

    def test_malformed_marker_for_the_current_head_blocks_and_names_both_ways_out(self) -> None:
        module = _load_script()
        notices: list = []

        violations = module.disposition_violations(
            [self._malformed(_HEAD[:12], ["0c70fb80f49f"])],
            [],
            _HEAD,
            self._bindings(module),
            notices,
        )

        assert len(violations) == 1
        assert "malformed disposition marker" in violations[0]
        assert "post a corrected disposition" in violations[0]
        assert "delete that comment" in violations[0]
        assert notices == []

    def test_malformed_marker_replaced_by_a_later_record_does_not_block(self) -> None:
        """The same writer's later well-formed record for the same lane and span
        supersedes it, even on the current head."""
        module = _load_script()
        span = "0c70fb80f49f"
        notices: list = []

        violations = module.disposition_violations(
            [
                self._malformed(_HEAD, [span], comment_id=5),
                self._record("gpt", [span], comment_id=6),
            ],
            [],
            _HEAD,
            self._bindings(module),
            notices,
        )

        assert violations == []
        assert len(notices) == 1
        assert "superseded by comment 6" in notices[0]

    @pytest.mark.parametrize(
        "target, later_target, supersedes",
        [
            ("fable", "opus", True),
            ("opus", "gpt", False),
            ("first-principles", "first-principles", True),
        ],
        ids=["alias-same-lane", "different-lanes", "unbound-same-target"],
    )
    def test_superseding_resolves_lane_aliases(
        self, target: str, later_target: str, supersedes: bool
    ) -> None:
        module = _load_script()
        span = "0c70fb80f49f"
        notices: list = []

        violations = module.disposition_violations(
            [
                self._record(target, [span], malformed=True, comment_id=5),
                self._record(later_target, [span], comment_id=6),
            ],
            [],
            _HEAD,
            self._bindings(module),
            notices,
        )

        if supersedes:
            assert violations == []
            assert len(notices) == 1
            assert "superseded by comment 6" in notices[0]
        else:
            assert len(violations) == 1
            assert "malformed disposition marker" in violations[0]
            assert notices == []

    @pytest.mark.parametrize(
        "later",
        [
            {"comment_id": 4},
            {"comment_id": 6, "author": "bob"},
            {"comment_id": 6, "target": "opus"},
            {"comment_id": 6, "spans": ["111111111111"]},
        ],
        ids=["earlier", "other-author", "other-lane", "other-span"],
    )
    def test_only_a_later_record_for_the_same_finding_supersedes(self, later: dict) -> None:
        module = _load_script()
        replacement = {**self._record("gpt", ["0c70fb80f49f"]), **later}

        violations = module.disposition_violations(
            [self._malformed(_HEAD, ["0c70fb80f49f"], comment_id=5), replacement],
            [],
            _HEAD,
            self._bindings(module),
        )

        assert any("malformed disposition marker" in v for v in violations)

    def test_an_older_malformed_marker_still_gets_the_claim_checks(self) -> None:
        """It keeps ledger power, so skipping its shape must not skip the
        one-finding rule its target= and span= still answer to."""
        module = _load_script()

        violations = module.disposition_violations(
            [self._malformed(_OLD, ["0c70fb80f49f", "111111111111"])],
            [],
            _HEAD,
            self._bindings(module),
        )

        assert len(violations) == 1
        assert "claims 2 findings" in violations[0]
