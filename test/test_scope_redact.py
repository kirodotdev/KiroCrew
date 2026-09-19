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


def _corpus(*rows: dict) -> str:
    return json.dumps({"golden_paths": list(rows)}, indent=2)


def _row(command: str, *, reason: str = "the owner runs this daily") -> dict:
    return {
        "kind": "shell",
        "command_or_flow": command,
        "platform": "posix",
        "reason": reason,
    }


class TestDropChangedFieldsSplitsTheCorpusByWhatIsClassified:
    """A corpus cannot be redacted where the classifier reads it, and only there.

    ``deny_diff`` selects rows by ``kind`` and ``platform`` and hands
    ``command_or_flow`` to the deny composite verbatim at the base ref and at the
    head ref, so a placeholder in any of the three measures an operation nobody
    proposed -- and a placeholder where a command belongs is a command nobody ever
    refused, which classifies as ALLOWED.

    ``reason`` is prose. The classifier takes it with ``entry.get("reason", "")`` and
    classifies nothing from it, so removing it costs that row's EXPLANATION and
    leaves the measurement untouched.

    NO ROW IS EVER REMOVED, and that is a security property rather than tidiness.
    The rows are model-authored out of the diff under review, so a removed row is a
    boundary the lane silently stopped probing, on input the author of that diff
    influences.
    """

    def test_a_shape_in_the_prose_removes_the_field_and_keeps_the_row(self, tmp_path: Path) -> None:
        clean = _row("cat ~/.kirocrew/cloud.json")
        shaped = _row("grep -c . ~/.kirocrew/cloud.json", reason=f"the role reads {ARN} first")
        path = tmp_path / "candidates.json"
        path.write_text(_corpus(clean, shaped), encoding="utf-8")

        code = redact.main(
            ["--mode", "json", "--drop-changed-fields", "--fail-if-changed", str(path)]
        )

        assert code == 10
        body = path.read_text(encoding="utf-8")
        rows = json.loads(body)["golden_paths"]
        assert len(rows) == 2, "no row may be removed"
        assert rows[0] == clean
        assert rows[1] == {
            "kind": "shell",
            "command_or_flow": "grep -c . ~/.kirocrew/cloud.json",
            "platform": "posix",
        }, "the classified fields stay verbatim and only the prose field goes"
        assert "[REDACTED" not in body, "a corpus must carry no redaction marker"

    def test_a_shape_in_a_classified_field_refuses_and_writes_nothing(self, tmp_path: Path) -> None:
        """It can be neither rewritten nor removed, so the run is refused.

        Rewriting measures an operation nobody proposed. Removing the row drops the
        boundary it probes, which is a coverage hole rather than a scrub. Exit 11 says
        so, and the file is left exactly as it arrived.
        """
        original = _corpus(
            _row("cat ~/.kirocrew/cloud.json"),
            _row(f"aws secretsmanager describe-secret --secret-id {ARN}"),
        )
        path = tmp_path / "candidates.json"
        path.write_text(original, encoding="utf-8")

        code = redact.main(["--mode", "json", "--drop-changed-fields", str(path)])

        assert code == 11
        assert path.read_text(encoding="utf-8") == original

    def test_one_classified_shape_refuses_even_beside_prunable_prose(self, tmp_path: Path) -> None:
        """Fail closed on the strictest row, not on the average of them."""
        path = tmp_path / "candidates.json"
        original = _corpus(
            _row("ls ~/.kirocrew", reason=f"after reading {ARN}"),
            _row(f"aws iam get-role --role-name {ARN}"),
        )
        path.write_text(original, encoding="utf-8")

        assert redact.main(["--mode", "json", "--drop-changed-fields", str(path)]) == 11
        assert path.read_text(encoding="utf-8") == original

    def test_each_classified_field_refuses_on_its_own(self, tmp_path: Path) -> None:
        """All three are read by the classifier, so all three refuse.

        ``kind`` and ``platform`` come from closed sets, so a shape in one is already
        a row the validator would reject -- but this flag must not be the thing that
        decides that, and a shape there is no more rewritable than one in the command.
        """
        for field in ("kind", "command_or_flow", "platform"):
            path = tmp_path / f"candidates-{field}.json"
            original = _corpus({**_row("ls ~/.kirocrew"), field: ARN})
            path.write_text(original, encoding="utf-8")

            assert redact.main(["--mode", "json", "--drop-changed-fields", str(path)]) == 11, field
            assert path.read_text(encoding="utf-8") == original, field

    def test_a_clean_corpus_is_untouched_and_reports_unchanged(self, tmp_path: Path) -> None:
        rows = [_row("cat ~/.kirocrew/cloud.json"), _row("ls ~/.kirocrew")]
        path = tmp_path / "candidates.json"
        path.write_text(_corpus(*rows), encoding="utf-8")

        code = redact.main(
            ["--mode", "json", "--drop-changed-fields", "--fail-if-changed", str(path)]
        )

        assert code == 0
        assert json.loads(path.read_text(encoding="utf-8"))["golden_paths"] == rows

    def test_each_credential_class_prunes_the_prose_field(self, tmp_path: Path) -> None:
        """The flag borrows the redaction vocabulary rather than restating it.

        A rule added to ``redact_text`` must reach this decision with no second edit,
        so the classes are pinned through the flag as well as through the scrub.
        """
        for shaped_reason in (
            f"the role is {ARN}",
            f"the key id is {KEY_ID}",
            # The account id is bounded by `\b`, so it needs a non-word character
            # beside it. `p{ACCT}` does NOT match, so a fixture spelled that way
            # asserts nothing about the account-id rule.
            f"the account is /{ACCT}/",
            f"the env holds aws_session_token={SESSION_TOKEN}",
        ):
            path = tmp_path / "candidates.json"
            path.write_text(_corpus(_row("ls ~/.kirocrew", reason=shaped_reason)), encoding="utf-8")

            assert (
                redact.main(
                    ["--mode", "json", "--drop-changed-fields", "--fail-if-changed", str(path)]
                )
                == 10
            ), shaped_reason
            row = json.loads(path.read_text(encoding="utf-8"))["golden_paths"][0]
            assert "reason" not in row, shaped_reason
            assert row["command_or_flow"] == "ls ~/.kirocrew", shaped_reason

    def test_a_secret_named_by_an_extra_field_removes_that_field(self, tmp_path: Path) -> None:
        """In JSON a key and its value are two separate strings.

        The assignment rule cannot see the pair, so the anchored key rule is what
        catches ``{"aws_session_token": "<secret>"}``. It reaches this decision through
        the same walk, so the field goes and the row stays.
        """
        path = tmp_path / "candidates.json"
        path.write_text(
            _corpus(dict(_row("ls ~/.kirocrew"), aws_session_token=SESSION_TOKEN)),
            encoding="utf-8",
        )

        assert (
            redact.main(["--mode", "json", "--drop-changed-fields", "--fail-if-changed", str(path)])
            == 10
        )
        row = json.loads(path.read_text(encoding="utf-8"))["golden_paths"][0]
        assert "aws_session_token" not in row
        assert row["command_or_flow"] == "ls ~/.kirocrew"

    def test_a_credential_shape_in_an_extra_field_KEY_removes_that_field(
        self, tmp_path: Path
    ) -> None:
        """A key is a field name, and an unclassified one is removable.

        ``--mode json`` refuses the whole document on a shaped key, because rewriting
        one changes the schema its consumer reads. Removing the field is the narrower
        answer and does not touch what is measured. This is the path through the
        exception, which only a key the redaction itself would rewrite reaches.
        """
        path = tmp_path / "candidates.json"
        path.write_text(
            _corpus({**_row("ls ~/.kirocrew"), ARN: "the role this row assumes"}),
            encoding="utf-8",
        )

        assert (
            redact.main(["--mode", "json", "--drop-changed-fields", "--fail-if-changed", str(path)])
            == 10
        )
        row = json.loads(path.read_text(encoding="utf-8"))["golden_paths"][0]
        assert ARN not in row
        assert row["command_or_flow"] == "ls ~/.kirocrew"

    def test_a_shape_outside_the_rows_is_refused(self, tmp_path: Path) -> None:
        """Nothing outside a row says whether the classifier reads it.

        Rewriting it would put a marker into a file whose contract is that the
        measured bytes are untouched, so the answer is to refuse: the document is not
        the one the flag describes.
        """
        path = tmp_path / "candidates.json"
        path.write_text(
            json.dumps({"golden_paths": [_row("ls ~/.kirocrew")], "note": f"for {ARN}"}, indent=2),
            encoding="utf-8",
        )

        assert redact.main(["--mode", "json", "--drop-changed-fields", str(path)]) == 1

    def test_a_document_with_no_rows_key_is_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "candidates.json"
        path.write_text(json.dumps({"rows": [_row("ls ~/.kirocrew")]}), encoding="utf-8")

        assert redact.main(["--mode", "json", "--drop-changed-fields", str(path)]) == 1

    def test_an_unparseable_corpus_is_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "candidates.json"
        path.write_text('{"golden_paths": [', encoding="utf-8")

        assert redact.main(["--mode", "json", "--drop-changed-fields", str(path)]) == 1

    def test_a_bare_list_is_accepted_like_the_committed_corpus(self, tmp_path: Path) -> None:
        """``deny_diff.load_corpus`` accepts both shapes, so this must too."""
        path = tmp_path / "candidates.json"
        path.write_text(
            json.dumps([_row("ls ~/.kirocrew", reason=f"the role is {ARN}")]), encoding="utf-8"
        )

        assert (
            redact.main(["--mode", "json", "--drop-changed-fields", "--fail-if-changed", str(path)])
            == 10
        )
        assert json.loads(path.read_text(encoding="utf-8"))[0] == {
            "kind": "shell",
            "command_or_flow": "ls ~/.kirocrew",
            "platform": "posix",
        }

    def test_the_flag_is_refused_with_text_mode(self, tmp_path: Path) -> None:
        """A corpus is a parsed document, and text mode has no fields to tell apart.

        Accepting the pair would line-redact the very commands the flag exists to keep
        byte-faithful.
        """
        path = tmp_path / "candidates.json"
        path.write_text(_corpus(_row("ls ~/.kirocrew")), encoding="utf-8")

        with pytest.raises(SystemExit) as caught:
            redact.main(["--mode", "text", "--drop-changed-fields", str(path)])

        assert caught.value.code == 2

    def test_the_removed_count_is_printed_for_the_lane_to_report(
        self, tmp_path: Path, capsys
    ) -> None:
        """The removal costs an explanation, so the number reaches the job log."""
        path = tmp_path / "candidates.json"
        path.write_text(
            _corpus(
                _row("ls ~/.kirocrew"),
                _row("cat ~/.kirocrew/cloud.json", reason=f"the role is {ARN}"),
                _row("jq . ~/.kirocrew/config.json", reason=f"the account is /{ACCT}/"),
            ),
            encoding="utf-8",
        )

        redact.main(["--mode", "json", "--drop-changed-fields", str(path)])

        assert "removed=2" in capsys.readouterr().out

    def test_the_pruned_rows_are_all_still_adjudicated_by_validate(self, tmp_path: Path) -> None:
        """END TO END, through the harness the lane actually runs.

        The units above prove the file; this proves the consequence. Every row the
        reviewer proposed reaches ``validate`` and the corpus it writes for the
        differential, with each command byte-identical -- so a credential shape in
        prose costs no measurement at all.
        """
        commands = [
            "cat ~/.kirocrew/cloud.json",
            "ls ~/.kirocrew",
            "jq . ~/.kirocrew/config.json",
        ]
        path = tmp_path / "candidates.json"
        path.write_text(
            _corpus(
                _row(commands[0]),
                _row(commands[1], reason=f"the execution role reads {ARN}"),
                _row(commands[2]),
            ),
            encoding="utf-8",
        )
        out = tmp_path / "normalized.json"

        assert (
            redact.main(["--mode", "json", "--drop-changed-fields", "--fail-if-changed", str(path)])
            == 10
        )
        assert scope.main(["validate", "--candidates", str(path), "--out", str(out)]) == 0

        normalized = json.loads(out.read_text(encoding="utf-8"))["golden_paths"]
        assert [row["command_or_flow"] for row in normalized] == commands
        assert normalized[1].get("reason", "") == ""

    def test_a_pruned_corpus_still_loads_through_the_classifier_schema(
        self, tmp_path: Path
    ) -> None:
        """``reason`` is optional in the row schema, so its absence is not a defect.

        Pinned against ``deny_diff``'s own loader rather than asserted, because that
        loader is what a removed field has to survive.
        """
        path = tmp_path / "candidates.json"
        path.write_text(
            _corpus(_row("ls ~/.kirocrew", reason=f"the role is {ARN}")), encoding="utf-8"
        )
        redact.main(["--mode", "json", "--drop-changed-fields", str(path)])

        rows = deny_diff.load_corpus(path)

        assert len(rows) == 1
        assert rows[0].command == "ls ~/.kirocrew"
        assert rows[0].reason == ""


class TestTheFeatureDetectionContract:
    """Both lanes stage this script from the BASE ref and grep its help for the flag.

    That is what lets a workflow which knows the flag run against a base copy that
    does not: an unknown argument is an argparse exit 2, which says nothing about
    credentials, so the lanes ask first and fall back to refusing the run when the
    answer is no.

    The grepped STRING is therefore a contract between this script's argparse surface
    and two ``run:`` bodies. A rename would leave the grep matching nothing, the
    fallback engaged permanently, and no test failing -- so the string is pinned here,
    and against the workflows that grep it.
    """

    FLAG = "--drop-changed-fields"

    def test_the_flag_appears_in_the_help_the_lanes_grep(self, capsys) -> None:
        with pytest.raises(SystemExit) as caught:
            redact.main(["--help"])

        assert caught.value.code == 0
        assert self.FLAG in capsys.readouterr().out

    @pytest.mark.parametrize(
        "workflow",
        ("security-scope-review.yml", "fork-security-scope-review.yml"),
    )
    def test_each_lane_greps_for_exactly_this_spelling(self, workflow: str) -> None:
        text = (ROOT / ".github" / "workflows" / workflow).read_text(encoding="utf-8")

        assert f'grep -q -- "{self.FLAG}"' in text, workflow
        # And the fallback exists: a lane that detects the flag and then does nothing
        # different when it is absent has no fail-closed path at all.
        assert "predates --drop-changed-fields" in text, workflow
