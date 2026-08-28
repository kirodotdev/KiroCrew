"""Regression tests for the `pr_findings.py` credential redactor.

`pr_findings.py` prints UNTRUSTED CI-log and review-comment text, so it redacts
credentials first. It carries its OWN stdlib-only copy of the patterns because
the script is documented as portable and cannot import `kiro_crew.security`.
That copy required THREE `.`-separated segments, so the two-segment dashboard
link token (`base64url(payload).base64url(hmac_sig)`) never matched it.

Every case below uses the token in BARE PROSE, not as `token=<value>`. The
labelled form was already covered by `_KV_RE`, so a `?token=` case would pass
before the fix and prove nothing.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from types import ModuleType

from skill_script_helpers import load_skill_script

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (
    ROOT / "src" / "kiro_crew" / "builtin_skills" / "kirocrew-dev" / "prepare-pr" / "scripts" / "pr_findings.py"
)

# Same token shape the backend tests pin (`test_security.py`), so all three
# copies of the pattern are locked to one generator.
_LINK_PAYLOAD = (
    "eyJzdWIiOiJsb2NhbC1hcHAiLCJleHAiOjE3ODU0MTc2MDYsInNlc3Npb25fZXhwIjoxNzg1NDg5MzA2"
    "LCJpYXQiOjE3ODU0MTczMDYsIm5vbmNlIjoiOTM5YzE3MGQ5ZjBiNmEyMiIsImdlbiI6MH0"
)
_SIG = "gVhM4aKLA8dyFH-oZlQx6SpYSNPkXA07kpDhWd6UhZI"  # 43 chars, base64url


def _load_script() -> ModuleType:
    return load_skill_script("prepare_pr_findings", SCRIPT)


class TestCredentialRedaction:
    def test_redacts_bare_two_segment_link_token(self) -> None:
        """A link token in prose must be replaced whole, payload included."""
        module = _load_script()
        token = f"{_LINK_PAYLOAD}.{_SIG}"

        result = module.redact(f"open the dashboard with {token} before it expires")

        assert token not in result
        assert "eyJzdWIi" not in result, "the payload carries sub/exp/nonce claims"
        assert _SIG not in result

    def test_redacts_freshly_minted_link_token(self) -> None:
        """Tie the pattern to the real generator, not to a pasted sample.

        A hard-coded token cannot notice that `generate_token` changed shape.
        This mints one and fails if the copied pattern stops covering it.
        """
        module = _load_script()
        from kiro_crew.dashboard.token_auth import generate_token

        token = generate_token("local-app", 300, register_nonce=False)

        result = module.redact(f"link: {token}")

        assert token not in result
        assert token.split(".")[0] not in result

    def test_redacts_three_segment_jwt_whole(self) -> None:
        """A signed JWT must not be left with a dangling signature."""
        module = _load_script()
        jwt = (
            "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
            ".eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIn0"
            ".dQw4w9WgXcQdQw4w9WgXcQdQw4w9WgXcQdQw4w9WgXc"
        )

        result = module.redact(f"leaked in the log: {jwt}")

        assert jwt not in result
        for segment in jwt.split("."):
            assert segment not in result

    def test_keeps_signature_of_a_jws_matching_the_link_token_shape(self) -> None:
        """The one case where alternative ORDER is load-bearing.

        A conventional JWS header is 33 chars past `eyJ`, far below the
        link-token alternative's first-segment floor, so it cannot match a real
        JWS at all and the test above passes in either order. Order matters only
        when the header clears that floor AND the payload is exactly 43 chars,
        because the right boundary is satisfied by a `.`, so running the
        link-token alternative first leaves `.signature` in the printed log.
        """
        module = _load_script()
        sig = "C" * 43
        crafted = f"eyJ{'A' * 100}.{'B' * 43}.{sig}"

        result = module.redact(f"log: {crafted}")

        assert sig not in result
        assert crafted not in result

    def test_eyj_identifiers_not_redacted(self) -> None:
        """Ordinary code containing `eyJ` must survive verbatim.

        A left boundary alone cannot help at offset 0, so the corpus includes
        statement-initial identifiers as well as attribute access.
        """
        module = _load_script()
        for text in (
            "eyJsonSerializer.deserializeFromStringValue(x)",
            "eyJsonSerializerConfigurationFactoryBuilder.deserializeFromStringValue(x)",
            "obj.eyJsonReader.readValueFromInputStream(x)",
            "keyJson.get(raw)",
            "surveyJson.title",
            "eyJargonized.intercontinentalization",
        ):
            assert module.redact(text) == text, text


# ---------------------------------------------------------------------------
# Issue #2550: stable span_hash per reviewer finding + marker-regex parity.
# ---------------------------------------------------------------------------

STATUS_SCRIPT = SCRIPT.with_name("pr_status.py")

_HEAD = "f" * 40
_OLD = "a" * 40


def _load_status() -> ModuleType:
    return load_skill_script("prepare_pr_status_parity", STATUS_SCRIPT)


class TestMarkerRegexParity:
    """Both scripts carry a copy of the reviewer-marker contract; neither can
    import the other (each is standalone-copyable by design), so parity is
    pinned here -- drift would make pr_status.py gate on markers that
    pr_findings.py cannot see, or vice versa."""

    def test_stamp_and_block_patterns_are_byte_identical(self) -> None:
        findings = _load_script()
        status = _load_status()
        assert findings.REVIEWED_STAMP_RE.pattern == status.REVIEWED_STAMP_RE.pattern
        assert findings.BLOCK_MERGE_RE.pattern == status.BLOCK_MERGE_RE.pattern
        assert findings._CTRL_RE.pattern == status._CTRL_RE.pattern
        assert findings.DEFAULT_MARKER_AUTHORS == status.DEFAULT_MARKER_AUTHORS
        assert findings.DEFAULT_MARKER_BINDINGS == status.DEFAULT_MARKER_BINDINGS
        assert findings._COMMENT_KEY_RE.pattern == status._COMMENT_KEY_RE.pattern
        assert findings.FINDING_RE.pattern == status.FINDING_RE.pattern
        assert findings.DISPOSITION_PREFIX == status.DISPOSITION_PREFIX
        assert findings.DISPOSITION_MARKER_RE.pattern == status.DISPOSITION_MARKER_RE.pattern
        assert findings.SPAN_CLAIM_RE.pattern == status.SPAN_CLAIM_RE.pattern

    def test_disposition_and_finding_helpers_are_byte_identical(self) -> None:
        """The disposition-rule computation is defined once and copied: a
        drift would make pr_findings.py report violations the pr_status.py
        gate does not enforce, or vice versa -- the same failure mode the
        marker-regex parity above exists to stop."""
        findings = _load_script()
        status = _load_status()
        for helper in (
            "span_hash",
            "extract_findings",
            "parse_disposition_record",
            "fetch_disposition_comments",
            "author_is_repo_writer",
            "writer_disposition_records",
            "disposition_violations",
        ):
            assert inspect.getsource(getattr(findings, helper)) == inspect.getsource(
                getattr(status, helper)
            ), helper

    def test_c1_controls_are_stripped(self) -> None:
        """U+009B is the single-byte CSI (equivalent to ESC-[): a bot finding
        carrying C1 controls must not reach the terminal through sanitize()."""
        module = _load_script()
        laced = "safe\x9b31mred\x9d]0;title\x07also\x85line"
        cleaned = module.sanitize(laced)
        assert "\x9b" not in cleaned
        assert "\x9d" not in cleaned
        assert "\x85" not in cleaned
        assert "safe" in cleaned and "also" in cleaned

    def test_emitting_workflows_still_carry_the_marker_grammar(self) -> None:
        """Pin the EMITTERS to the consumers, not just the two consumer copies
        to each other: a review-workflow prompt tweak that drops or renames a
        stamp would silently orphan the parsers -- the freshness gate would see
        no stamps and stop gating. This drift is exactly what the marker-
        grammar spec (docs/ci/prepare-pr-portability.md §5.9) exists to stop."""
        workflows = {
            ".github/workflows/codex-review.yml": (
                "[GPT-REVIEWED]",
                "[BLOCK-MERGE]",
                "<!-- codex-ai-review -->",
            ),
            ".github/workflows/claude-review.yml": (
                "[OPUS-REVIEWED]",
                "[BLOCK-MERGE]",
                "<!-- claude-ai-review -->",
            ),
            ".github/workflows/design-review.yml": (
                "[DESIGN-REVIEWED]",
                "<!-- design-review -->",
            ),
            ".github/workflows/ux-review.yml": (
                "[UX-REVIEWED]",
                "<!-- ux-review -->",
            ),
        }
        for rel, markers in workflows.items():
            text = (ROOT / rel).read_text(encoding="utf-8")
            for marker in markers:
                assert marker in text, (
                    f"{rel} no longer emits {marker}; update the parsers in "
                    "pr_status.py/pr_findings.py and §5.9 of "
                    "docs/ci/prepare-pr-portability.md together"
                )


class TestSpanHash:
    def test_deterministic_and_line_number_independent(self) -> None:
        """The same finding after a rebase must keep its identity, or
        recurrence detection resets on every push. The hash takes no line
        number and reads no file, so it is stable by construction."""
        module = _load_script()
        a = module.span_hash("src/mod.py", "gpt/BLOCKING")
        b = module.span_hash("src/mod.py", "gpt/BLOCKING")
        assert a == b
        assert len(a) == 12

    def test_different_rule_class_separates_findings_in_one_path(self) -> None:
        module = _load_script()
        assert module.span_hash("a.py", "gpt/BLOCKING") != module.span_hash(
            "a.py", "opus/BLOCKING"
        )

    def test_no_file_is_ever_opened_for_untrusted_paths(self) -> None:
        """Finding paths come from UNTRUSTED bot-comment text. Reading any
        file a comment names -- even inside the working tree, which can be a
        dotfiles checkout holding credentials -- is a file read of
        LLM-influenced input that this standalone script cannot route through
        the repo's sensitive-path gate. Ratchet: the module must contain no
        open() call at all outside the redaction-safe stdlib imports."""
        source = SCRIPT.read_text(encoding="utf-8")
        assert "open(" not in source.replace("subprocess.run", ""), (
            "pr_findings.py must never open() a file: finding paths are "
            "untrusted comment text and cannot be routed through hooks.py"
        )


class TestExtractFindings:
    def test_scoped_to_current_head_and_bound_lane_comments(self) -> None:
        module = _load_script()
        bindings = dict(_load_script().DEFAULT_MARKER_BINDINGS)
        comments = [
            # Stale comment: findings for a diff that no longer exists.
            {
                "user": {"type": "Bot"},
                "body": (
                    "<!-- codex-ai-review -->\n"
                    f"BLOCKING -- src/old.py:5 -- gone\n[GPT-REVIEWED] {_OLD}"
                ),
            },
            # Fresh comment: one blocking + one advisory.
            {
                "user": {"type": "Bot"},
                "body": (
                    "<!-- codex-ai-review -->\n"
                    "BLOCKING -- src/x.py:10 -- broken guard\n"
                    "FINDING -- src/y.py:20 -- could be tighter -> Fix: tighten\n"
                    f"[GPT-REVIEWED] {_HEAD}\n[BLOCK-MERGE] {_HEAD}"
                ),
            },
            # Un-keyed comment: no bound lane, contributes nothing.
            {
                "user": {"type": "Bot"},
                "body": f"BLOCKING -- src/z.py:1 -- fake\n[GPT-REVIEWED] {_HEAD}",
            },
        ]

        found = list(module.extract_findings(comments, _HEAD, bindings))

        assert [(f["kind"], f["path"], f["line"]) for f in found] == [
            ("BLOCKING", "src/x.py", 10),
            ("FINDING", "src/y.py", 20),
        ]
        assert all(f["reviewer"] == "gpt" for f in found)
        assert all(f["block_merge"] for f in found)
        assert all(len(f["span"]) == 12 for f in found)


class TestFindingLineFormats:
    def test_bold_opus_format_is_parsed(self) -> None:
        """Opus emits `**BLOCKING — file:line — title**` with detail on
        following lines; omitting it from the listing hides real blockers."""
        module = _load_script()
        bindings = dict(module.DEFAULT_MARKER_BINDINGS)
        comments = [
            {
                "user": {"type": "Bot"},
                "body": (
                    "<!-- claude-ai-review -->\n"
                    "**BLOCKING \u2014 src/a.py:12 \u2014 guard removed**\n"
                    "detail line\n"
                    f"[OPUS-REVIEWED] {_HEAD}\n[BLOCK-MERGE] {_HEAD}"
                ),
            },
        ]

        found = list(module.extract_findings(comments, _HEAD, bindings))

        assert [(f["kind"], f["path"], f["line"]) for f in found] == [
            ("BLOCKING", "src/a.py", 12)
        ]

    def test_plain_gpt_format_still_parses(self) -> None:
        module = _load_script()
        bindings = dict(module.DEFAULT_MARKER_BINDINGS)
        comments = [
            {
                "user": {"type": "Bot"},
                "body": (
                    "<!-- codex-ai-review -->\n"
                    f"FINDING -- src/b.py:3 -- tighten -> Fix: x\n[GPT-REVIEWED] {_HEAD}"
                ),
            },
        ]
        found = list(module.extract_findings(comments, _HEAD, bindings))
        assert [(f["kind"], f["path"], f["line"]) for f in found] == [
            ("FINDING", "src/b.py", 3)
        ]


class TestRollupHelperParity:
    """Both scripts carry a copy of fetch_check_rollup and its notice; neither
    can import the other (standalone-copyable by design), so parity is pinned
    here -- drift would make the two reports describe the same degraded token
    state in different words, or degrade under different conditions."""

    def test_rollup_fetch_helpers_are_byte_identical(self) -> None:
        findings = _load_script()
        status = _load_status()
        assert findings.ROLLUP_UNAVAILABLE_NOTICE == status.ROLLUP_UNAVAILABLE_NOTICE
        assert findings.ROLLUP_HEAD_MOVED_NOTICE == status.ROLLUP_HEAD_MOVED_NOTICE
        assert inspect.getsource(findings.fetch_check_rollup) == inspect.getsource(
            status.fetch_check_rollup
        )


class TestDegradedRollup:
    """gh resolves a --json field set atomically, so a Checks-blind token (any
    fine-grained PAT) fails EVERY request naming statusCheckRollup. The
    findings collector must keep working on the core fields it was authorised
    to read and say why its CI section is empty."""

    def test_rollup_fetch_failure_completes_with_a_visible_notice(self, capsys) -> None:
        module = _load_script()
        payload = json.dumps(
            {
                "number": 42,
                "url": "https://github.com/example/repo/pull/42",
                "headRefOid": _HEAD,
            }
        )

        def fake_run(args: list[str]) -> tuple[int, str, str]:
            if args[:3] == ["gh", "auth", "status"]:
                return 0, "", ""
            if args[:3] == ["gh", "pr", "view"]:
                fields = args[args.index("--json") + 1] if "--json" in args else ""
                if "statusCheckRollup" in fields:
                    return 1, "", "Resource not accessible by personal access token"
                return 0, payload, ""
            raise AssertionError("unexpected command: {}".format(args))

        module.run = fake_run
        module.iter_unresolved_threads = lambda *_a: iter(())
        module.fetch_bot_comments = lambda *_a: []
        module.fetch_disposition_comments = lambda *_a: []

        code = module.main(["pr_findings.py", "42"])
        captured = capsys.readouterr()

        assert code == 0
        assert "NOTICE: " + module.ROLLUP_UNAVAILABLE_NOTICE in captured.out
        assert "could not read PR" not in captured.err

    def test_head_moved_between_reads_discards_the_rollup(self, capsys) -> None:
        """A push landing between the core read and the rollup read must not
        pair the old head's identity with the new head's failing checks: the
        rollup is discarded with its own notice and no check is drilled into."""
        module = _load_script()
        payload = json.dumps(
            {
                "number": 42,
                "url": "https://github.com/example/repo/pull/42",
                "headRefOid": _OLD,
            }
        )
        moved_rollup = json.dumps(
            {
                "headRefOid": _HEAD,
                "statusCheckRollup": [
                    {
                        "name": "CI",
                        "status": "COMPLETED",
                        "conclusion": "FAILURE",
                        "detailsUrl": "https://github.com/example/repo/actions/runs/1",
                    }
                ],
            }
        )

        def fake_run(args: list[str]) -> tuple[int, str, str]:
            if args[:3] == ["gh", "auth", "status"]:
                return 0, "", ""
            if args[:3] == ["gh", "pr", "view"]:
                fields = args[args.index("--json") + 1] if "--json" in args else ""
                if "statusCheckRollup" in fields:
                    return 0, moved_rollup, ""
                return 0, payload, ""
            raise AssertionError("unexpected command: {}".format(args))

        module.run = fake_run
        module.iter_unresolved_threads = lambda *_a: iter(())
        module.fetch_bot_comments = lambda *_a: []
        module.fetch_disposition_comments = lambda *_a: []

        code = module.main(["pr_findings.py", "42"])
        captured = capsys.readouterr()

        assert code == 0
        assert "NOTICE: " + module.ROLLUP_HEAD_MOVED_NOTICE in captured.out
        # The stale-paired failing check must not be drilled into.
        assert "--- CI" not in captured.out


# ---------------------------------------------------------------------------
# Issue #4187: the one-lane / one-rationale-per-finding disposition rule is
# mechanical, not prose. A writer's disposition record claims the finding it
# rules on by span= identity; the computation lives here (non-gating) and the
# byte-identical copy in pr_status.py gates on it.
# ---------------------------------------------------------------------------


def _disposition_comment(author: str, body: str, comment_id: int = 1) -> dict:
    return {"id": comment_id, "user": {"login": author, "type": "User"}, "body": body}


class TestDispositionRecordParsing:
    def test_valid_marker_parses_target_head_and_ordered_unique_spans(self) -> None:
        """target= is case-normalized and span claims keep first-seen order
        with duplicates dropped, so re-quoting a span in the rationale is one
        claim, not a false multi-span violation."""
        module = _load_script()
        span_a = module.span_hash("a.py", "gpt/BLOCKING")
        span_b = module.span_hash("b.py", "gpt/FINDING")
        body = (
            f"<!-- ai-review-disposition target=GPT head={_HEAD} -->\n"
            f"- **fixed** span={span_a}\n> reason\nspan={span_b} and again span={span_a}"
        )

        record = module.parse_disposition_record(_disposition_comment("alice", body, 7))

        assert record == {
            "author": "alice",
            "comment_id": 7,
            "target": "gpt",
            "head": _HEAD,
            "spans": [span_a, span_b],
            "bullets": 1,
            "malformed": False,
        }

    def test_quoted_evidence_lines_are_not_claims(self) -> None:
        """Quoting the pr_findings.py listing as a ruling's evidence is
        natural; span ids and title bullets inside ``> `` quoted lines must
        not read as claims, or every well-evidenced record becomes a false
        multi-span or multi-bullet violation."""
        module = _load_script()
        span_a = module.span_hash("a.py", "gpt/BLOCKING")
        span_b = module.span_hash("b.py", "gpt/BLOCKING")
        span_c = module.span_hash("c.py", "gpt/BLOCKING")
        body = (
            f"<!-- ai-review-disposition target=gpt head={_HEAD} -->\n"
            f"- **rebutted** span={span_a}\n"
            f"> the listing said: span={span_b} [BLOCKING] b.py:1\n"
            f"> - **fixed** a prior record's bullet, quoted\n"
            f"  > indented quote: span={span_c}"
        )

        record = module.parse_disposition_record(_disposition_comment("alice", body))

        assert record is not None
        assert record["spans"] == [span_a]
        assert record["bullets"] == 1

    def test_prefix_with_unparseable_marker_is_malformed_not_invisible(self) -> None:
        """codex-review.yml's ledger selects records by the byte prefix alone,
        so a comment whose marker does not parse still has downgrade power and
        must stay visible to the check rather than silently escaping it."""
        module = _load_script()
        body = "<!-- ai-review-disposition targets=gpt,design -->\nblanket ruling"

        record = module.parse_disposition_record(_disposition_comment("alice", body))

        assert record is not None
        assert record["malformed"] is True

    def test_non_disposition_comment_is_not_a_record(self) -> None:
        module = _load_script()
        assert module.parse_disposition_record(_disposition_comment("a", "plain text")) is None

    def test_leading_whitespace_is_not_a_record(self) -> None:
        """Selection is byte-prefix parity with the ledger's jq startswith():
        a comment the ledger would not consume must not be gated either."""
        module = _load_script()
        body = f" <!-- ai-review-disposition target=gpt head={_HEAD} -->"
        assert module.parse_disposition_record(_disposition_comment("a", body)) is None


class TestWriterDispositionRecords:
    def test_non_writers_are_dropped_and_permission_lookups_are_cached(self) -> None:
        module = _load_script()
        body = f"<!-- ai-review-disposition target=gpt head={_HEAD} -->"
        lookups: list[str] = []

        def fake_run(args: list[str]) -> tuple[int, str, str]:
            assert args[:2] == ["gh", "api"] and "/collaborators/" in args[2]
            login = args[2].split("/")[4]
            lookups.append(login)
            perm = {"alice": "write", "mallory": "read"}.get(login, "none")
            return 0, json.dumps({"permission": perm}), ""

        module.run = fake_run
        comments = [
            _disposition_comment("alice", body, 1),
            _disposition_comment("mallory", body, 2),
            _disposition_comment("alice", body, 3),
        ]

        records = module.writer_disposition_records("o/r", comments)

        assert [r["comment_id"] for r in records] == [1, 3]
        assert sorted(lookups) == ["alice", "mallory"], "one lookup per login, cached"

    def test_permission_api_error_drops_the_record_not_the_gate(self) -> None:
        """Fail-soft per author: the gate can only ADD blocking, so an
        unverifiable author degrades to pre-existing behavior while a drive-by
        commenter can never hold the PR hostage with a crafted marker."""
        module = _load_script()
        module.run = lambda _args: (1, "", "boom")
        body = f"<!-- ai-review-disposition target=gpt head={_HEAD} -->"

        records = module.writer_disposition_records("o/r", [_disposition_comment("a", body)])

        assert records == []

    def test_every_distinct_author_is_permission_checked(self) -> None:
        """No author cap: the adjudication ledger's own author loop is
        uncapped, so a flood of non-writer comments must not be able to push
        a real writer's record past a cap this check has but the ledger does
        not -- the two consumers must degrade together."""
        module = _load_script()
        body = f"<!-- ai-review-disposition target=gpt head={_HEAD} -->"
        lookups: list[str] = []

        def fake_run(args: list[str]) -> tuple[int, str, str]:
            login = args[2].split("/")[4]
            lookups.append(login)
            perm = "write" if login == "writer26" else "read"
            return 0, json.dumps({"permission": perm}), ""

        module.run = fake_run
        comments = [
            _disposition_comment("flood{:02d}".format(i), body, i) for i in range(25)
        ] + [_disposition_comment("writer26", body, 26)]

        records = module.writer_disposition_records("o/r", comments)

        assert [r["comment_id"] for r in records] == [26]
        assert len(lookups) == 26

    def test_unreadable_comment_list_propagates_none(self) -> None:
        module = _load_script()
        assert module.writer_disposition_records("o/r", None) is None


class TestDispositionViolations:
    """The computation reads the trusted bot comments directly: a record is
    validated against the findings stamped for the head its ``head=`` says it
    judged (in the ordinary fix-then-push round that is the PRIOR head) and
    against the current head's -- a record is immutable and keeps its
    adjudication-ledger downgrade power on every later head, so "an older
    head" is not an exemption."""

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

    def _gpt_comment(self, head: str, lines: list[str]) -> dict:
        return {
            "user": {"type": "Bot", "login": "github-actions[bot]"},
            "body": "<!-- codex-ai-review -->\n" + "\n".join(lines) + f"\n[GPT-REVIEWED] {head}",
        }

    def _opus_comment(self, head: str, lines: list[str]) -> dict:
        return {
            "user": {"type": "Bot", "login": "github-actions[bot]"},
            "body": "<!-- claude-ai-review -->\n" + "\n".join(lines) + f"\n[OPUS-REVIEWED] {head}",
        }

    def _current_head_comments(self, module: ModuleType) -> tuple[list[dict], str, str]:
        span_gpt = module.span_hash("src/x.py", "gpt/BLOCKING")
        span_opus = module.span_hash("src/y.py", "opus/BLOCKING")
        comments = [
            self._gpt_comment(_HEAD, ["BLOCKING -- src/x.py:10 -- broken"]),
            self._opus_comment(_HEAD, ["**BLOCKING \u2014 src/y.py:5 \u2014 title**"]),
        ]
        return comments, span_gpt, span_opus

    def test_cross_lane_claim_is_a_violation(self) -> None:
        """The observed defect: a rationale answering another lane's finding.
        target= must equal the reviewer of every span the record claims."""
        module = _load_script()
        comments, _span_gpt, span_opus = self._current_head_comments(module)

        violations = module.disposition_violations(
            [self._record("gpt", [span_opus])], comments, _HEAD, self._bindings(module)
        )

        assert len(violations) == 1
        assert "cross-lane" in violations[0]
        assert "target=gpt" in violations[0] and "lane opus" in violations[0]

    def test_one_record_claiming_two_spans_is_a_violation(self) -> None:
        """One rationale covers exactly one finding, and the record is the only
        unit the adjudication ledger can scope a rationale by."""
        module = _load_script()
        span_a = module.span_hash("a.py", "gpt/BLOCKING")
        span_b = module.span_hash("b.py", "gpt/BLOCKING")
        comments = [
            self._gpt_comment(
                _HEAD, ["BLOCKING -- a.py:1 -- one", "BLOCKING -- b.py:2 -- two"]
            )
        ]

        violations = module.disposition_violations(
            [self._record("gpt", [span_a, span_b])], comments, _HEAD, self._bindings(module)
        )

        assert len(violations) == 1
        assert "one rationale covers exactly one finding" in violations[0]

    def test_two_title_bullets_sharing_one_span_are_a_violation(self) -> None:
        """span_hash is deliberately coarse: two findings of one kind in one
        file share a span id, so span dedup alone would let one record carry
        both titles under one rationale. The bullet count closes that shape."""
        module = _load_script()
        span = module.span_hash("a.py", "gpt/BLOCKING")
        comments = [
            self._gpt_comment(
                _HEAD, ["BLOCKING -- a.py:1 -- one", "BLOCKING -- a.py:99 -- two"]
            )
        ]
        record = self._record("gpt", [span], bullets=2)

        violations = module.disposition_violations(
            [record], comments, _HEAD, self._bindings(module)
        )

        assert len(violations) == 1
        assert "finding-title bullets" in violations[0]

    def test_multi_span_gates_even_for_an_unbound_target_and_an_old_head(self) -> None:
        """Claims are explicit whatever the lane, and a record keeps ledger
        power on every later head -- so neither an unbound target= nor an
        older head= exempts a multi-span record."""
        module = _load_script()
        comments, span_gpt, span_opus = self._current_head_comments(module)

        violations = module.disposition_violations(
            [self._record("first-principles", [span_gpt, span_opus], head=_OLD)],
            comments,
            _HEAD,
            self._bindings(module),
        )

        assert any("one rationale covers exactly one finding" in v for v in violations)

    def test_spanless_record_for_a_lane_with_findings_is_a_violation(self) -> None:
        """The #3963 shape: a blanket comment with no finding identity at all.
        Without this class the rule stays prose -- a record simply omits span=
        tokens and claims everything its rationale fits."""
        module = _load_script()
        comments, _span_gpt, _span_opus = self._current_head_comments(module)

        violations = module.disposition_violations(
            [self._record("gpt", [])], comments, _HEAD, self._bindings(module)
        )

        assert len(violations) == 1
        assert "claims no span=" in violations[0]

    def test_prior_head_record_is_validated_against_the_head_it_judged(self) -> None:
        """The ordinary flow: the writer stamps head=<prior-reviewed-sha> and
        pushes, so the live head has already moved when the gate polls. The
        record must be judged against ITS head's findings, not skipped as
        history -- skipping is exactly how the blanket ruling shipped green."""
        module = _load_script()
        span_opus = module.span_hash("src/y.py", "opus/BLOCKING")
        comments = [
            self._gpt_comment(_OLD, ["BLOCKING -- src/x.py:10 -- broken"]),
            self._opus_comment(_OLD, ["**BLOCKING \u2014 src/y.py:5 \u2014 title**"]),
        ]

        violations = module.disposition_violations(
            [self._record("gpt", [span_opus], head=_OLD)],
            comments,
            _HEAD,
            self._bindings(module),
        )

        assert len(violations) == 1
        assert "cross-lane" in violations[0]

    def test_fabricated_span_is_a_violation(self) -> None:
        """A claim resolving to no finding on the judged head is a fabricated
        or stale identity: without this class any 12 hex characters convert a
        blanket record into a compliant one."""
        module = _load_script()
        comments, _span_gpt, _span_opus = self._current_head_comments(module)

        violations = module.disposition_violations(
            [self._record("gpt", ["0" * 12])], comments, _HEAD, self._bindings(module)
        )

        assert len(violations) == 1
        assert "resolves to no finding" in violations[0]

    def test_short_prefix_judged_head_is_expanded_to_the_stamped_sha(self) -> None:
        """head= may be a 7-40 hex prefix while stamps carry the full 40; the
        lookup must still find the judged head's findings."""
        module = _load_script()
        comments = [self._gpt_comment(_OLD, ["BLOCKING -- src/x.py:10 -- broken"])]

        violations = module.disposition_violations(
            [self._record("gpt", ["0" * 12], head=_OLD[:12])],
            comments,
            _HEAD,
            self._bindings(module),
        )

        assert len(violations) == 1
        assert "resolves to no finding" in violations[0]

    def test_superseded_record_with_gone_stamps_is_not_relitigated(self) -> None:
        """Once the reviewer re-adjudicated on a new head (stamps rewritten in
        place), a historical record whose claim can no longer be resolved is
        left alone -- flagging it would permanently block legitimate history."""
        module = _load_script()
        span_gone = module.span_hash("gone.py", "gpt/BLOCKING")
        comments = [self._gpt_comment(_HEAD, ["BLOCKING -- src/x.py:10 -- still here"])]

        violations = module.disposition_violations(
            [self._record("gpt", [span_gone], head=_OLD)],
            comments,
            _HEAD,
            self._bindings(module),
        )

        assert violations == []

    def test_blanket_old_record_still_gates_while_its_lane_has_live_findings(self) -> None:
        """A span-less record keeps ledger downgrade power exactly as long as
        its lane still has findings for it to cover; superseded stamps do not
        launder it."""
        module = _load_script()
        comments = [self._gpt_comment(_HEAD, ["BLOCKING -- src/x.py:10 -- live"])]

        violations = module.disposition_violations(
            [self._record("gpt", [], head=_OLD)], comments, _HEAD, self._bindings(module)
        )

        assert len(violations) == 1
        assert "claims no span=" in violations[0]

    def test_spanless_record_for_a_lane_without_parseable_findings_is_exempt(self) -> None:
        """An advisory lane whose concerns never parse into FINDING lines has
        no span identity to demand -- requiring one would block dispositioning
        a Design/UX prose concern at all."""
        module = _load_script()
        comments, _span_gpt, _span_opus = self._current_head_comments(module)

        violations = module.disposition_violations(
            [self._record("design", [])], comments, _HEAD, self._bindings(module)
        )

        assert violations == []

    def test_unbound_target_is_exempt_from_the_claim_requirement(self) -> None:
        """A lane outside the bindings (e.g. first-principles) has no finding
        identity the parsers can verify a claim against."""
        module = _load_script()
        comments, _span_gpt, _span_opus = self._current_head_comments(module)

        violations = module.disposition_violations(
            [self._record("first-principles", [])], comments, _HEAD, self._bindings(module)
        )

        assert violations == []

    def test_malformed_record_is_flagged_on_any_head(self) -> None:
        """A malformed marker cannot be head-scoped (its head= is unreadable)
        and keeps its ledger downgrade power until the comment is fixed."""
        module = _load_script()

        violations = module.disposition_violations(
            [
                {
                    "author": "alice",
                    "comment_id": 9,
                    "target": "",
                    "head": "",
                    "spans": [],
                    "malformed": True,
                }
            ],
            [],
            _HEAD,
            self._bindings(module),
        )

        assert len(violations) == 1
        assert "malformed disposition marker" in violations[0]

    def test_one_record_per_finding_in_its_own_lane_is_clean(self) -> None:
        module = _load_script()
        comments, span_gpt, span_opus = self._current_head_comments(module)
        records = [
            self._record("gpt", [span_gpt]),
            self._record("opus", [span_opus], comment_id=2),
        ]

        assert module.disposition_violations(records, comments, _HEAD, self._bindings(module)) == []

    def test_output_is_sorted_and_deduplicated(self) -> None:
        """The gate folds these into ``progress_key.status``, which a polling
        loop compares byte-for-byte -- so the list must be deterministic."""
        module = _load_script()
        comments, _span_gpt, span_opus = self._current_head_comments(module)
        record = self._record("gpt", [span_opus])

        violations = module.disposition_violations(
            [record, dict(record)], comments, _HEAD, self._bindings(module)
        )

        assert violations == sorted(violations)
        assert len(violations) == 1
