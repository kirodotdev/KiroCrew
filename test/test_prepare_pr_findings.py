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

import importlib.util
from pathlib import Path
from types import ModuleType

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
    spec = importlib.util.spec_from_file_location("prepare_pr_findings", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
    spec = importlib.util.spec_from_file_location("prepare_pr_status_parity", STATUS_SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
