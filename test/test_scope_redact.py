"""The outbound scrub must not corrupt the document it protects.

``scripts/scope_redact.py`` is the last thing that touches the scope-review lanes'
text before that text becomes public. It therefore has two duties that pull in
opposite directions, and the tests below pin both.

*It must remove the credential shape.* Every surface those lanes write -- the job
log, the step summary, the uploaded artifact, the pull request comment -- is
world-readable on a public repository, and the text is model-written out of a
diff. So an access-key id, an ARN, an account id and a credential-bearing
assignment must not survive.

*It must leave the document usable.* The machine-readable report is read back with
``json.loads``: the verdict folder answers exit 2 on a file that does not parse,
which publishes a HARD BLOCK naming no rows. A scrub that breaks the JSON turns a
real reportable verdict into an over-refusal -- the exact failure class this lane
exists to prevent, caused by the lane's own scrub. So the round trip is pinned
here on the shape that broke it: a credential at the very END of a string value,
where a whitespace-terminated match reaches past the closing quote.

The report fixture is a real :class:`deny_diff.Report` rendered through
``render_json``, the way ``test_scope_candidates.py`` builds its legs, rather than
a hand-written imitation that drifts from the module it describes.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
REDACT = ROOT / "scripts" / "scope_redact.py"
CANDIDATES = ROOT / "scripts" / "scope_candidates.py"
DENY_DIFF = ROOT / "scripts" / "deny_diff.py"

ARN = "arn:aws:iam::" + "9" * 12 + ":role/Deployer"
KEY_ID = "AKIAIOSFODNN7EXAMPLE"
#: A 12-digit account id, ASSEMBLED rather than written out. The literal form is an
#: internal-content marker, so a test that spells it would fail the public-repo scan
#: it exists to keep honest.
ACCT = "9" * 12
#: A stand-in for a session token, ASSEMBLED and deliberately low-entropy. Written
#: out, a realistic token trips the secrets scanners on this repo's own diff -- the
#: test would red the very gate it exists to defend. The redactor's value pattern is
#: ``[^\s"',;)\]}]+``, so what the value CONTAINS is not what these tests measure;
#: only that it is a value and that the delimiter after it survives.
SESSION_TOKEN = "session-token-" + "x" * 12


def _load(name: str, path: Path):
    """Import a script by path, registered in ``sys.modules`` before exec.

    A script's dataclasses resolve their own annotations through
    ``sys.modules[cls.__module__]``, so a module executed without a registration
    raises at class-creation time rather than at use.
    """
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


redact = _load("scope_redact", REDACT)
scope = _load("scope_candidates_for_redact", CANDIDATES)
deny_diff = _load("deny_diff_for_redact", DENY_DIFF)


def _report_json(*, command: str, reason: str, total_rows: int = 1) -> str:
    """One differential leg in ``deny_diff``'s OWN rendering.

    Built through ``Report`` and ``render_json`` so every field carries the type
    the differential actually emits, and so a schema change here breaks a test
    rather than silently leaving the fixture describing a shape nothing writes.
    """
    row = deny_diff.Row(index=0, kind="shell", command=command, platform="any", reason=reason)
    verdict = deny_diff.Verdict(denied=True, reason="tier-2 refusal", tier="denied_commands")
    report = deny_diff.Report(
        base="base",
        head="head",
        base_sha="a" * 7,
        head_sha="b" * 7,
        platform="linux",
        source="corpus.json",
        total_rows=total_rows,
        skipped_kind=0,
        skipped_platform=0,
        base_absent_tiers=[],
        regressions=[(row, verdict)],
        unchanged_allowed=0,
    )
    return deny_diff.render_json(report)


class TestJsonModeKeepsTheDocumentParseable:
    def test_an_arn_ending_a_string_value_leaves_valid_json(self, tmp_path: Path) -> None:
        """The shape that breaks a raw-bytes substitution: nothing follows the ARN.

        A match bounded by "up to the next whitespace" runs past the closing quote
        and the comma, so the value never terminates and the document stops
        parsing -- and the folder reads an unparseable report as exit 2, a hard
        block with no rows.
        """
        path = tmp_path / "report-linux.json"
        path.write_text(
            _report_json(command="aws sts get-caller-identity", reason=f"the deploy role is {ARN}"),
            encoding="utf-8",
        )

        assert redact.main(["--mode", "json", str(path)]) == 0

        text = path.read_text(encoding="utf-8")
        document = json.loads(text)
        assert document["regressions"][0]["why_legitimate"] == "the deploy role is [REDACTED-ARN]"
        assert ARN not in text

    def test_a_twelve_digit_json_number_is_not_rewritten(self, tmp_path: Path) -> None:
        """A count is not an account id, and a bare token is not a JSON value."""
        path = tmp_path / "counts.json"
        path.write_text(json.dumps({"counts": {"total_rows": int(ACCT)}}), encoding="utf-8")

        assert redact.main(["--mode", "json", str(path)]) == 0

        assert json.loads(path.read_text(encoding="utf-8")) == {"counts": {"total_rows": int(ACCT)}}

    def test_the_same_digits_inside_a_string_are_redacted(self, tmp_path: Path) -> None:
        """The number rule is not dropped, only confined to where text lives."""
        path = tmp_path / "row.json"
        path.write_text(json.dumps({"why": f"account {ACCT} owns it"}), encoding="utf-8")

        assert redact.main(["--mode", "json", str(path)]) == 0

        assert json.loads(path.read_text(encoding="utf-8")) == {
            "why": "account [REDACTED-ACCT] owns it"
        }

    def test_a_credential_nested_in_a_list_of_dicts_is_found(self, tmp_path: Path) -> None:
        """A finding lives under a list under a dict, so the walk must be recursive."""
        path = tmp_path / "rows.json"
        path.write_text(
            json.dumps({"golden_paths": [{"a": "ok"}, {"command_or_flow": f"aws --key {KEY_ID}"}]}),
            encoding="utf-8",
        )

        assert redact.main(["--mode", "json", str(path)]) == 0

        document = json.loads(path.read_text(encoding="utf-8"))
        assert document["golden_paths"][1]["command_or_flow"] == "aws --key [REDACTED-AWS-KEY-ID]"
        assert document["golden_paths"][0]["a"] == "ok"

    def test_a_secret_named_by_its_key_is_redacted(self, tmp_path: Path) -> None:
        """A key and its value are two separate strings, so the pair needs its own rule.

        Without it the assignment rule -- which needs the name and the value on
        one side of the separator -- never sees the pair, and the secret is
        published intact.
        """
        path = tmp_path / "env.json"
        path.write_text(json.dumps({"aws_session_token": SESSION_TOKEN}), encoding="utf-8")

        assert redact.main(["--mode", "json", str(path)]) == 0

        assert json.loads(path.read_text(encoding="utf-8")) == {"aws_session_token": "[REDACTED]"}

    def test_a_non_json_input_is_an_error_not_a_text_fallback(self, tmp_path: Path) -> None:
        """The caller asked for the parse guarantee; only a parse can give it.

        Falling back to a raw substitution would answer 0 while producing exactly
        the unparseable output the JSON mode exists to prevent.
        """
        path = tmp_path / "broken.json"
        path.write_text("{not json at all", encoding="utf-8")

        assert redact.main(["--mode", "json", str(path)]) == 1
        assert path.read_text(encoding="utf-8") == "{not json at all"

    def test_a_credential_shape_in_a_KEY_is_refused(self, tmp_path: Path) -> None:
        """Redacting a key would change the schema, and two keys can collapse into one."""
        path = tmp_path / "weird.json"
        path.write_text(json.dumps({ARN: "value"}), encoding="utf-8")

        assert redact.main(["--mode", "json", str(path)]) == 1


class TestTextMode:
    def test_a_plain_log_line_is_redacted(self, tmp_path: Path) -> None:
        path = tmp_path / "error.log"
        path.write_text(
            f"refused: aws sts assume-role --role-arn {ARN} --key {KEY_ID}\n", encoding="utf-8"
        )

        assert redact.main(["--mode", "text", str(path)]) == 0

        assert path.read_text(encoding="utf-8") == (
            "refused: aws sts assume-role --role-arn [REDACTED-ARN] "
            "--key [REDACTED-AWS-KEY-ID]\n"
        )

    def test_an_arn_keeps_the_prose_punctuation_that_follows_it(self, tmp_path: Path) -> None:
        """The bound is the fix in text mode too: a comment body is prose, not tokens."""
        path = tmp_path / "body.md"
        path.write_text(f"The role ({ARN}), which is legitimate, was refused.\n", encoding="utf-8")

        assert redact.main(["--mode", "text", str(path)]) == 0

        assert path.read_text(encoding="utf-8") == (
            "The role ([REDACTED-ARN]), which is legitimate, was refused.\n"
        )

    def test_a_credential_assignment_is_redacted_without_eating_the_delimiter(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "log.txt"
        path.write_text(f"env: AWS_SESSION_TOKEN={SESSION_TOKEN}; next\n", encoding="utf-8")

        assert redact.main(["--mode", "text", str(path)]) == 0

        assert path.read_text(encoding="utf-8") == "env: AWS_SESSION_TOKEN=[REDACTED]; next\n"


class TestTheChangedSignal:
    def test_an_untouched_file_reports_unchanged(self, tmp_path: Path, capsys) -> None:
        path = tmp_path / "clean.json"
        path.write_text(json.dumps({"why": "gh pr view 1"}), encoding="utf-8")

        assert redact.main(["--mode", "json", str(path)]) == 0
        assert "changed=no" in capsys.readouterr().out

    def test_a_redacted_file_reports_changed(self, tmp_path: Path, capsys) -> None:
        path = tmp_path / "dirty.json"
        path.write_text(json.dumps({"why": ARN}), encoding="utf-8")

        assert redact.main(["--mode", "json", str(path)]) == 0
        assert "changed=yes" in capsys.readouterr().out

    def test_fail_if_changed_is_a_distinct_code_from_a_failure_to_redact(
        self, tmp_path: Path
    ) -> None:
        """A caller must be able to tell "scrubbed, so withhold it" from "unscrubbed".

        The two demand opposite things: a redacted file was protected and the
        caller decides whether it is still worth publishing, while an unredacted
        one must not be published at all.
        """
        dirty = tmp_path / "dirty.json"
        dirty.write_text(json.dumps({"why": ARN}), encoding="utf-8")
        clean = tmp_path / "clean.json"
        clean.write_text(json.dumps({"why": "ls -la"}), encoding="utf-8")
        broken = tmp_path / "broken.json"
        broken.write_text("{", encoding="utf-8")

        assert redact.main(["--mode", "json", "--fail-if-changed", str(clean)]) == 0
        assert redact.main(["--mode", "json", "--fail-if-changed", str(dirty)]) == 10
        assert redact.main(["--mode", "json", "--fail-if-changed", str(broken)]) == 1

    def test_one_changed_file_in_a_batch_reports_changed(self, tmp_path: Path) -> None:
        clean = tmp_path / "clean.log"
        clean.write_text("nothing here\n", encoding="utf-8")
        dirty = tmp_path / "dirty.log"
        dirty.write_text(f"role {ARN}\n", encoding="utf-8")

        assert redact.main(["--mode", "text", "--fail-if-changed", str(clean), str(dirty)]) == 10


class TestTheMarkerSpellings:
    """The markers are a contract: both lanes' seed paths grep for ``[REDACTED-``.

    A renamed marker stops matching, and a row nobody can read then travels into
    the next run's candidate set instead of being refused on the way in.
    """

    @pytest.mark.parametrize(
        ("text", "marker"),
        [
            (KEY_ID, "[REDACTED-AWS-KEY-ID]"),
            (ARN, "[REDACTED-ARN]"),
            (ACCT, "[REDACTED-ACCT]"),
            ("aws_secret_access_key=abcdef", "[REDACTED]"),
        ],
    )
    def test_the_marker_is_spelled_exactly_as_the_seed_path_greps_for_it(
        self, text: str, marker: str
    ) -> None:
        assert marker in redact.redact_text(text)

    def test_every_prefixed_marker_matches_the_seed_path_pattern(self) -> None:
        redacted = redact.redact_text(f"{KEY_ID} {ARN} {ACCT}")
        assert redacted.count("[REDACTED-") == 3


class TestTheReportStillFolds:
    def test_a_redacted_report_folds_to_a_real_verdict_rather_than_exit_2(
        self, tmp_path: Path
    ) -> None:
        """The end-to-end property, on the case that produced the over-refusal.

        A report carrying an ARN at the end of a string value must still fold to
        the verdict the differential actually measured -- exit 1, a confirmed
        regression with its row -- rather than to exit 2, which publishes a block
        that names nothing.
        """
        path = tmp_path / "report-ubuntu-latest.json"
        path.write_text(
            _report_json(command="aws sts get-caller-identity", reason=f"the deploy role is {ARN}"),
            encoding="utf-8",
        )

        assert redact.main(["--mode", "json", str(path)]) == 0

        code = scope.main(
            [
                "verdict",
                "--report",
                f"ubuntu-latest={path}",
                "--out-md",
                str(tmp_path / "body.md"),
                "--out-rows",
                str(tmp_path / "rows.json"),
            ]
        )

        assert code == 1
        body = (tmp_path / "body.md").read_text(encoding="utf-8")
        assert "aws sts get-caller-identity" in body
        rows = json.loads((tmp_path / "rows.json").read_text(encoding="utf-8"))
        assert rows["golden_paths"][0]["command_or_flow"] == "aws sts get-caller-identity"
