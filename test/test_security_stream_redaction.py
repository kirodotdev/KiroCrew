"""Tests for kiro_crew.security: redaction on output sinks and across stream chunks.

``redact_and_truncate``, the SEL emitters that redact before truncating, ``scan_history``,
``StreamRedactor`` (cross-chunk streaming redaction) and the ``scan_memory`` import guard. The
JOSE samples ``TestStreamRedactor`` reads live in ``security_helpers``.
"""

from __future__ import annotations

import base64
import json

import pytest
import security_helpers

from kiro_crew import security
from kiro_crew.security import REDACTED_CREDENTIAL_TAG, redact_and_truncate, scan_history


def _jose_header(min_len: int) -> str:
    """A base64url JOSE header segment at least *min_len* characters long."""
    raw = json.dumps({"alg": "HS256", "pad": "A" * min_len}).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


class TestRedactAndTruncate:
    """Tests for redact_and_truncate()."""

    def test_truncates_long_text(self) -> None:
        text = "x" * 10000
        result = redact_and_truncate(text, max_chars=100)
        assert len(result) <= 100

    def test_redacts_credentials_in_truncated(self) -> None:
        text = "Key: AKIAIOSFODNN7EXAMPLE in output"
        result = redact_and_truncate(text)
        assert "AKIAIOSFODNN7EXAMPLE" not in result

    def test_handles_none(self) -> None:
        assert redact_and_truncate(None) == ""

    def test_credential_straddling_boundary_not_leaked(self) -> None:
        """A secret spanning the max_chars cut must not leak a partial.

        Redaction runs over the full text before truncation. Truncating first
        would slice AKIA...EXAMPLE in half, leaving an unredactable prefix that
        does not match the credential regex and would leak on the wire.
        """
        prefix = "prefix "  # 7 chars
        secret = "AKIAIOSFODNN7EXAMPLE"  # 20-char AWS access key ID
        text = prefix + secret + " trailing"
        # Boundary lands 8 chars into the 20-char key.
        max_chars = len(prefix) + 8
        result = redact_and_truncate(text, max_chars=max_chars)
        assert len(result) <= max_chars
        # No fragment of the access key ID (which starts with "AKIA") survives.
        assert "AKIA" not in result


class TestSELEmittersRedactBeforeTruncate:
    """SEL metadata emitters must redact BEFORE truncating.

    Slicing to 200 chars before redacting writes a credential straddling the
    200-char boundary to the durable audit event with its tail cut off, in the
    shape the credential regex cannot match. Each test plants the 20-char
    AWS access key ID 'AKIAIOSFODNN7EXAMPLE' straddling index 200 and asserts no
    fragment of it (its 'AKIA' prefix) survives in the emitted event's metadata.

    These call the emitters DIRECTLY, so they pin the emitter's own ordering and
    nothing about what a caller feeds it. Redaction here is case-sensitive by
    design, so a caller that hands over a case-folded view defeats it while these
    still pass; that half is pinned in test_push_branch_gate.py
    (``test_allow_audit_records_the_raw_command_not_the_matching_view``).
    """

    SECRET = "AKIAIOSFODNN7EXAMPLE"  # 20-char AWS access key ID

    def test_push_allow_event_redacts_straddling_secret(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew import security

        logged: list = []

        class _RecorderLog:
            def log(self, event: object) -> None:
                logged.append(event)

        monkeypatch.setattr(security, "SecurityEventLog", lambda: _RecorderLog())

        # Build a push command whose token starts a few chars before index 200
        # so the 20-char key straddles the 200-char cut, and the total length
        # exceeds 200 chars.
        prefix = "git push https://x:"
        pad = "a" * (200 - len(prefix) - 4)
        command = prefix + pad + self.SECRET + "@github.com/o/r " + "y" * 300
        assert len(command) > 200
        assert 200 - len(prefix + pad) < len(self.SECRET)  # key straddles the cut

        security._emit_push_allow_event(command)

        assert len(logged) == 1
        event = logged[0]
        assert event.event_type == "push_allowed"
        assert not any("AKIA" in str(value) for value in event.metadata.values()), event.metadata

    def test_injection_dropped_event_redacts_straddling_secret(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew import security

        logged: list = []

        class _RecorderLog:
            def log(self, event: object) -> None:
                logged.append(event)

        monkeypatch.setattr(security, "SecurityEventLog", lambda: _RecorderLog())

        pad = "p" * (200 - 4)
        sample = pad + self.SECRET + " " + "z" * 300
        assert len(sample) > 200
        assert 200 - len(pad) < len(self.SECRET)  # key straddles the cut

        security.audit_injection_dropped(
            surface="slack",
            session_key="k",
            channel_id="C",
            thread_ts="1",
            sample=sample,
        )

        assert len(logged) == 1
        event = logged[0]
        assert event.event_type == "prompt_injection_dropped"
        assert not any("AKIA" in str(value) for value in event.metadata.values()), event.metadata


class TestScanHistory:
    """Tests for scan_history()."""

    def test_detects_suspicious_command_in_history(self, tmp_path) -> None:
        history_file = tmp_path / "session1.jsonl"
        entries = [
            json.dumps({"role": "assistant", "content": "rm -rf /"}),
            json.dumps({"role": "assistant", "content": "echo hello"}),
        ]
        history_file.write_text("\n".join(entries))
        findings = scan_history(tmp_path)
        assert len(findings) == 1
        assert "rm -rf /" in findings[0]["snippet"]

    def test_ignores_user_messages(self, tmp_path) -> None:
        history_file = tmp_path / "session1.jsonl"
        entries = [
            json.dumps({"role": "user", "content": "rm -rf /"}),
        ]
        history_file.write_text("\n".join(entries))
        findings = scan_history(tmp_path)
        assert len(findings) == 0

    def test_empty_dir(self, tmp_path) -> None:
        assert scan_history(tmp_path) == []

    def test_nonexistent_dir(self, tmp_path) -> None:
        assert scan_history(tmp_path / "nope") == []

    def test_respects_last_n(self, tmp_path) -> None:
        history_file = tmp_path / "session1.jsonl"
        entries = [json.dumps({"role": "assistant", "content": "rm -rf /"}) for _ in range(200)]
        history_file.write_text("\n".join(entries))
        findings = scan_history(tmp_path, last_n=5)
        assert len(findings) == 5


class TestStreamRedactor:
    """Tests for StreamRedactor (cross-chunk streaming redaction, issue 3)."""

    @staticmethod
    def _run(chunks):
        from kiro_crew.security import StreamRedactor

        r = StreamRedactor()
        emits = [r.feed(c) for c in chunks]
        emits.append(r.flush())
        return emits

    def test_credential_split_across_chunks(self) -> None:
        emits = self._run(["The access key is AKIA", "IOSFODNN7", "EXAMPLE"])
        # No single emit leaks a raw fragment
        for e in emits:
            assert "AKIAIOSFODNN7EXAMPLE" not in e
            assert not ("AKIA" in e and "REDACTED" not in e)
        joined = "".join(emits)
        assert joined == "The access key is [REDACTED: credential]"

    def test_char_by_char_stream(self) -> None:
        from kiro_crew.security import StreamRedactor

        r = StreamRedactor()
        out = "".join(r.feed(c) for c in "x AKIAIOSFODNN7EXAMPLE y") + r.flush()
        assert "AKIAIOSFODNN7EXAMPLE" not in out
        assert "[REDACTED: credential]" in out

    def test_no_data_loss_benign(self) -> None:
        joined = "".join(self._run(["Hello ", "world, ", "this is ", "fine."]))
        assert joined == "Hello world, this is fine."

    def test_single_chunk_credential(self) -> None:
        joined = "".join(self._run(["key=AKIAIOSFODNN7EXAMPLE done"]))
        assert "AKIAIOSFODNN7EXAMPLE" not in joined
        assert "REDACTED" in joined

    def test_github_token_split(self) -> None:
        joined = "".join(self._run(["use ghp_ABCDEFGHIJ", "KLMNOPQRSTUVWXYZ", "abcdef1234567890"]))
        assert "ghp_" "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef" not in joined
        assert "REDACTED" in joined

    def test_reset_discards_buffer(self) -> None:
        from kiro_crew.security import StreamRedactor

        r = StreamRedactor()
        assert r.feed("AKIA") == ""  # held
        r.reset()
        assert r.flush() == ""  # nothing left after reset

    def test_flush_empty(self) -> None:
        from kiro_crew.security import StreamRedactor

        assert StreamRedactor().flush() == ""

    def test_long_unbroken_run_is_capped_no_data_loss(self) -> None:
        """A pathologically long unbroken credential-class run does not grow the
        held buffer without bound: the excess beyond the cap is committed, and
        no content is lost across feed+flush."""
        from kiro_crew.security import _STREAM_HOLDBACK_MAX, StreamRedactor

        r = StreamRedactor()
        blob = "a" * (_STREAM_HOLDBACK_MAX + 300)  # no terminator, all cred-class
        emitted = r.feed(blob)
        # Some of the run was committed (not held forever) — held tail is capped.
        assert emitted, "cap did not release any of the oversized run"
        emitted += r.flush()
        assert emitted == blob, "content lost/altered across cap+flush"

    # ── Split `Authorization: Bearer <token>` holdback ──
    # The Bearer credential pattern spans the whitespace after `:` and after
    # `Bearer`; whitespace is not in _CRED_CLASS, so without the partial-anchor
    # the header + spaces commit and the token leaks on the next chunk.

    def test_bearer_split_at_spaces_not_leaked(self) -> None:
        emits = self._run(["Authorization: Bearer ", "opaque-token-value", " trailing text"])
        for e in emits:
            assert "opaque-token-value" not in e
        joined = "".join(emits)
        assert "opaque-token-value" not in joined
        assert "[REDACTED: credential]" in joined
        assert joined.endswith(" trailing text")

    def test_bearer_split_mid_word_not_leaked(self) -> None:
        emits = self._run(["Authorization: Bea", "rer sup3r-secret", " done"])
        for e in emits:
            assert "sup3r-secret" not in e
        joined = "".join(emits)
        assert "sup3r-secret" not in joined
        assert "[REDACTED: credential]" in joined
        assert joined.endswith(" done")

    @pytest.mark.parametrize(
        "chunks",
        (
            ("open &amp", ";token=SECRET", " done"),
            ("open &amp;", "token=SECRET", " done"),
        ),
    )
    def test_html_entity_token_separator_split_not_leaked(self, chunks) -> None:
        joined = "".join(self._run(chunks))
        assert joined == "open &amp;token=[REDACTED: credential] done"

    def test_html_entity_token_equals_split_not_leaked(self) -> None:
        joined = "".join(self._run(("open ?token&#", "61;SECRET", " done")))
        assert joined == "open ?token&#61;[REDACTED: credential] done"

    def test_canonical_tag_split_inside_token_value_is_fixed_point(self) -> None:
        tag = REDACTED_CREDENTIAL_TAG
        split = tag.index("credential") + 2
        joined = "".join(self._run((f"?token={tag[:split]}", f"{tag[split:]} and more text")))
        assert joined == f"?token={tag} and more text"

    def test_canonical_tag_split_in_prose_is_byte_identical(self) -> None:
        tag = REDACTED_CREDENTIAL_TAG
        split = tag.index("credential") + 2
        joined = "".join(self._run((f"prefix {tag[:split]}", f"{tag[split:]} suffix")))
        assert joined == f"prefix {tag} suffix"

    @pytest.mark.parametrize(
        "tag,split",
        tuple(
            (tag, split)
            for tag in security.CREDENTIAL_REDACTION_TAGS
            for split in (1, tag.index(":") + 1, len(tag) - 1)
        ),
    )
    def test_canonical_tag_is_fixed_point_at_internal_chunk_boundaries(self, tag, split) -> None:
        joined = "".join(self._run((f"?token={tag[:split]}", tag[split:])))
        assert joined == f"?token={tag}"

    @pytest.mark.parametrize("tag", security.CREDENTIAL_REDACTION_TAGS)
    def test_complete_canonical_tag_commits_byte_identically(self, tag) -> None:
        assert "".join(self._run((f"?token={tag}",))) == f"?token={tag}"

    def test_non_tag_bracket_value_is_still_redacted_normally(self) -> None:
        joined = "".join(self._run(("?token=[NOTATAG: cr", "edential]")))
        assert joined == f"?token={REDACTED_CREDENTIAL_TAG} credential]"

    def test_flush_preserves_an_incomplete_canonical_tag_prefix(self) -> None:
        tag = REDACTED_CREDENTIAL_TAG
        prefix = tag[: tag.index("credential") + 2]
        assert "".join(self._run((f"?token={prefix}",))) == f"?token={prefix}"

    def test_canonical_tag_holdback_never_authorizes_a_drop(self, monkeypatch) -> None:
        monkeypatch.setattr(security, "_STREAM_HOLDBACK_MAX", 4)
        monkeypatch.setattr(security, "_STREAM_HOLDBACK_JWT_MAX", 8)
        tag = REDACTED_CREDENTIAL_TAG
        text = f"?token={tag[: tag.index('credential') + 2]}"
        redactor = security.StreamRedactor(redactor=lambda value: value)
        assert redactor.feed(text) + redactor.flush() == text

    def test_entity_equals_empty_value_not_corrupted_across_chunks(self) -> None:
        joined = "".join(self._run(("open ?token&#", "61;", "&x=1 done")))
        assert joined == "open ?token&#61;&x=1 done"

    def test_html_entity_equals_makes_token_anchor_strong(self) -> None:
        from kiro_crew.security import (
            _STREAM_HOLDBACK_JWT_MAX,
            _STREAM_HOLDBACK_MAX,
            StreamRedactor,
        )

        redactor = StreamRedactor()
        assert redactor.feed("?token&#61;" + "x" * 600) == ""
        assert _STREAM_HOLDBACK_MAX < len(redactor._buf) <= _STREAM_HOLDBACK_JWT_MAX

    def test_authorization_in_prose_not_over_held(self) -> None:
        text = "Authorization: granted to all users."
        joined = "".join(self._run(["Authorization: ", "granted to all", " users."]))
        assert joined == text

    def test_bearer_anchor_respects_holdback_cap_no_unbounded_buffer(self) -> None:
        """A long unbroken `Authorization: Bearer <token>` must not pin the buffer.

        The partial-Bearer anchor pulls the commit point back to the
        `Authorization` start; without re-clamping to the holdback ceiling a token
        of all-Bearer-class chars would keep the anchor matching to end-of-buffer
        on every feed, growing the buffer without bound (WS/SSE/Slack DoS) and
        re-scanning O(n^2). The cap (escalated to the JWT ceiling for a credential
        anchor) must stay authoritative: once the withheld tail exceeds it the
        redactor stops accumulating, so the retained buffer stays bounded.
        """
        from kiro_crew.security import _STREAM_HOLDBACK_JWT_MAX, StreamRedactor

        r = StreamRedactor()
        r.feed("Authorization: Bearer ")
        # Feed a long unbroken Bearer-class token in chunks. The security property
        # under test is the memory bound: the retained buffer must never exceed the
        # ceiling, no matter how long the anchored token runs (that is what prevents
        # the unbounded-growth / O(n^2) DoS).
        for _ in range(60):
            r.feed("a" * 200)  # 12000 chars total, far exceeding the 4096 ceiling
            assert len(r._buf) <= _STREAM_HOLDBACK_JWT_MAX
        r.flush()
        assert len(r._buf) == 0

    # ── Terminal long-token un-bisect + fail-closed ceiling ──

    def test_terminal_long_jwt_not_bisected(self) -> None:
        """A terminal JWT longer than the 512-char DoS floor stays fully redacted.

        Without the JWT-aware cap the
        default 512-char holdback would bisect a long terminal token, emitting the
        first (len-512) chars raw before flush() redacted only the held tail.
        """
        from kiro_crew.security import _STREAM_HOLDBACK_MAX, StreamRedactor

        payload = _jose_header(_STREAM_HOLDBACK_MAX + 800)
        jwt = f"{payload}.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6"
        assert len(jwt) > _STREAM_HOLDBACK_MAX
        r = StreamRedactor()
        emitted = r.feed("Authorization header token ") + r.feed(jwt) + r.flush()
        assert jwt not in emitted
        assert "eyJ" not in emitted  # no raw prefix leaked ahead of the flush
        assert "[REDACTED: credential]" in emitted

    def test_terminal_long_jwe_not_bisected(self) -> None:
        """A 5-segment compact JWE longer than the 512 floor stays fully redacted.

        `_PARTIAL_JWT_TAIL_RE`'s
        trailing-segment quantifier must admit 5 segments (a compact JWE
        header.key.iv.ciphertext.tag) so it escalates the cap instead of bisecting
        the >512-char JWE at the 512 floor and leaking its raw head.
        """
        from kiro_crew.security import _STREAM_HOLDBACK_MAX, StreamRedactor

        seg = _jose_header(_STREAM_HOLDBACK_MAX + 400)
        jwe = f"{seg}.QW5rZXk.aXY.Y2lwaGVydGV4dA.dGFn"  # 5 compact JWE segments
        assert len(jwe) > _STREAM_HOLDBACK_MAX
        r = StreamRedactor()
        emitted = r.feed("token ") + r.feed(jwe) + r.flush()
        assert jwe not in emitted
        assert "eyJ" not in emitted  # no raw head leaked ahead of the flush
        assert "[REDACTED: credential]" in emitted

    def test_jose_validated_jwt_split_across_chunks(self) -> None:
        from kiro_crew.security import StreamRedactor

        jws = security_helpers._JWT
        jwe = security_helpers._JWE_DIR
        text = f"host honeyJar.example.com then {jws} and {jwe} done"
        for size in (1, 5, 13):
            r = StreamRedactor()
            chunks = [text[i : i + size] for i in range(0, len(text), size)]
            emitted = "".join(r.feed(c) for c in chunks) + r.flush()
            assert emitted == (
                "host honeyJar.example.com then [REDACTED: credential]"
                " and [REDACTED: credential] done"
            ), size

    def test_holdback_anchor_needs_a_json_header(self) -> None:
        from kiro_crew.security import _partial_jwt_tail

        assert _partial_jwt_tail("see honeyJar.example.com") is None
        assert _partial_jwt_tail("eyJ0.honeyJar.example") is None
        jws = security_helpers._JWT
        assert _partial_jwt_tail(f"x {jws[:40]}") is not None
        assert _partial_jwt_tail("x eyJhbGciOi") is not None  # header still arriving
        assert _partial_jwt_tail(f"eyJfoo.{jws[:40]}").start() == 7

    def test_terminal_long_opaque_bearer_not_bisected(self) -> None:
        """A >512-char opaque (non-JWT) Bearer token stays fully redacted.

        Opaque OAuth/refresh/SSO bearer
        tokens carry no `eyJ` header, so only the JWT anchor escalated the cap —
        an opaque bearer tail longer than 512 chars was bisected, streaming its
        head raw. `_BEARER_ANCHOR_PARTIAL_RE` now holds the whole anchor together
        and also escalates the cap.
        """
        from kiro_crew.security import _STREAM_HOLDBACK_MAX, StreamRedactor

        token = "A1b2C3d4" * ((_STREAM_HOLDBACK_MAX + 400) // 8)  # opaque, no eyJ
        assert len(token) > _STREAM_HOLDBACK_MAX
        r = StreamRedactor()
        emitted = r.feed("Authorization: Bearer ") + r.feed(token) + r.flush()
        assert token not in emitted
        assert token[:_STREAM_HOLDBACK_MAX] not in emitted

    def test_terminal_long_opaque_token_param_not_bisected(self) -> None:
        """A >512-char opaque `?token=` value stays fully redacted.

        `?` `&` `=` are in `_CRED_CLASS`, so a token URL is one withheld run —
        but with no recognised credential anchor a run past the 512 floor is
        BISECTED, and for a >=512-char opaque value the cut lands inside the
        value: the committed prefix carries the `token=` anchor (pass 4 redacts
        it there), while the tail reaches `flush()` anchor-less and would
        stream raw. `_TOKEN_PARAM_PARTIAL_RE` recognises the trailing partial
        and escalates the cap, the same remedy the opaque Bearer case got.
        """
        from kiro_crew.security import _STREAM_HOLDBACK_MAX, StreamRedactor

        value = "Xk9fQ2mP" * ((_STREAM_HOLDBACK_MAX + 400) // 8)  # opaque, no eyJ
        assert len(value) > _STREAM_HOLDBACK_MAX
        r = StreamRedactor()
        emitted = (
            r.feed("open http://localhost:7777?token=") + r.feed(value) + r.feed(" now\n")
        ) + r.flush()
        assert value not in emitted
        assert value[-_STREAM_HOLDBACK_MAX:] not in emitted  # the tail must not leak
        assert "token=[REDACTED: credential]" in emitted
        assert "[REDACTED: credential]" in emitted

    def test_terminal_token_param_punctuation_not_split(self) -> None:
        """A legal non-credential-class byte cannot split a token value."""
        from kiro_crew.security import StreamRedactor

        value = "abc!Zk8qWm3v"
        r = StreamRedactor()
        emitted = r.feed(f"open http://h.example/?token={value}") + r.flush()
        assert "Zk8qWm3v" not in emitted
        assert value not in emitted
        assert emitted == "open http://h.example/?token=[REDACTED: credential]"

    def test_token_param_punctuation_at_chunk_boundary_not_split(self) -> None:
        """A chunk boundary after punctuation cannot detach a token tail."""
        from kiro_crew.security import StreamRedactor

        value = "abc!Zk8qWm3v"
        r = StreamRedactor()
        emitted = r.feed("open http://h.example/?token=abc!")
        emitted += r.feed("Zk8qWm3v")
        emitted += r.flush()
        assert "Zk8qWm3v" not in emitted
        assert value not in emitted
        assert emitted == "open http://h.example/?token=[REDACTED: credential]"

    def test_token_param_punctuation_before_terminator_not_split(self) -> None:
        """A completed parameter spanning the commit point stays intact."""
        from kiro_crew.security import StreamRedactor

        r = StreamRedactor()
        emitted = r.feed("open http://h.example/?token=abc!Zk8qWm3v&next=1 done\n")
        emitted += r.flush()
        assert "Zk8qWm3v" not in emitted
        assert "token=[REDACTED: credential]" in emitted
        assert "&next=1 done" in emitted

    def test_token_param_terminator_at_chunk_boundary_not_split(self) -> None:
        """A trailing terminator cannot detach a token tail across feeds."""
        from kiro_crew.security import StreamRedactor

        r = StreamRedactor()
        emitted = r.feed("open http://h.example/?token=abc!Zk8qWm3v&")
        emitted += r.feed("next=1 done\n")
        emitted += r.flush()
        assert "Zk8qWm3v" not in emitted
        assert "token=[REDACTED: credential]" in emitted
        assert "&next=1 done" in emitted

    def test_weak_token_name_prefix_past_ceiling_preserves_data(self) -> None:
        """A bare token-name prefix is bounded lookahead, not a credential."""
        from kiro_crew.security import _STREAM_HOLDBACK_JWT_MAX, StreamRedactor

        text = "a" * (_STREAM_HOLDBACK_JWT_MAX + 200) + "?t"
        assert len(text) > 4200
        r = StreamRedactor()
        joined = r.feed(text) + r.flush()
        assert joined == text
        assert REDACTED_CREDENTIAL_TAG not in joined

    def test_complete_token_anchor_past_ceiling_still_fails_closed(self) -> None:
        """A token parameter with ``=`` keeps the existing strong-anchor drop."""
        from kiro_crew.security import _STREAM_HOLDBACK_JWT_MAX, StreamRedactor

        text = "a" * (_STREAM_HOLDBACK_JWT_MAX + 200) + "?token=" + "b" * 600
        r = StreamRedactor()
        assert r.feed(text) == REDACTED_CREDENTIAL_TAG
        assert r.flush() == ""

    def test_forced_floor_cut_cannot_bisect_complete_token_param(self) -> None:
        """The 512-byte floor is repaired when it bisects a token value."""
        from kiro_crew.security import _STREAM_HOLDBACK_MAX, StreamRedactor

        marker = "rawtokensuffix"
        value = "A" * 500 + marker + "B" * 6
        tail = " BEGIN PRIVATE KEY"
        text = "open ?token=" + value + tail
        # The natural trailing run is only ``KEY`` (3 bytes); the partial-PEM
        # rule lowers the Phase-A cut to zero. The 550-byte buffer then forces a
        # 512-byte floor cut at byte 38, which is 26 bytes into the token value.
        assert len(text) == 550
        assert len(text) - _STREAM_HOLDBACK_MAX == len("open ?token=") + 26
        r = StreamRedactor()
        pieces = [r.feed(text), r.flush()]
        assert all(marker not in piece for piece in pieces)
        assert "?token=[REDACTED: credential]" in "".join(pieces)

    def test_forced_cut_inside_weak_anchor_clamps_before_question_mark(self, monkeypatch) -> None:
        """A forced cut retains a bounded incomplete token-name anchor."""
        monkeypatch.setattr(security, "_STREAM_HOLDBACK_MAX", 4)
        r = security.StreamRedactor()
        # ``?to%6`` is a five-byte weak anchor. A four-byte cap proposes a cut
        # one byte into it; the repair must clamp back before ``?``.
        emitted = r.feed("safe ?to%6")
        assert emitted == "safe "
        assert r._buf == "?to%6"

        joined = emitted + r.feed("ben=weak-secret-value") + r.flush()
        assert "weak-secret-value" not in joined
        assert "?to%6ben=[REDACTED: credential]" in joined

    def test_credential_anchored_tail_past_ceiling_fails_closed(self) -> None:
        """A credential-anchored tail past the 4096 ceiling fails closed.

        A JWT/JWE/Bearer tail exceeding
        `_STREAM_HOLDBACK_JWT_MAX` must NOT be bisected (which would emit the
        token's head raw). feed() redacts+emits the safe prefix, appends the tag,
        and DROPS the oversized tail.
        """
        from kiro_crew.security import _STREAM_HOLDBACK_JWT_MAX, StreamRedactor

        jwt = _jose_header(_STREAM_HOLDBACK_JWT_MAX + 500) + ".eyJz.SflK"
        r = StreamRedactor()
        emitted = r.feed("prefix ") + r.feed(jwt)
        emitted += r.flush()
        assert jwt not in emitted
        assert "eyJ" not in emitted  # oversized head dropped, not streamed raw
        assert "[REDACTED: credential]" in emitted
        assert emitted.startswith("prefix ")

    @staticmethod
    def _assert_oversized_continuation_is_hidden(head: str) -> None:
        from kiro_crew.security import REDACTED_CREDENTIAL_TAG, StreamRedactor

        continuation = "tail_u7-Qp9_" * 24
        redactor = StreamRedactor()
        pieces = [
            redactor.feed(head),
            redactor.feed(continuation + " done"),
            redactor.flush(),
        ]
        joined = "".join(pieces)
        assert all(
            continuation[start : start + 32] not in joined
            for start in range(len(continuation) - 31)
        )
        assert joined.count(REDACTED_CREDENTIAL_TAG) == 1
        assert joined.endswith(" done")

    def test_oversized_jwt_continuation_is_discarded(self) -> None:
        from kiro_crew.security import _STREAM_HOLDBACK_JWT_MAX

        self._assert_oversized_continuation_is_hidden(
            "eyJ" + "A" * (_STREAM_HOLDBACK_JWT_MAX + 500)
        )

    def test_oversized_token_param_continuation_is_discarded(self) -> None:
        from kiro_crew.security import _STREAM_HOLDBACK_JWT_MAX

        self._assert_oversized_continuation_is_hidden(
            "?token=" + "T" * (_STREAM_HOLDBACK_JWT_MAX + 500)
        )

    def test_oversized_bearer_continuation_is_discarded(self) -> None:
        from kiro_crew.security import _STREAM_HOLDBACK_JWT_MAX

        self._assert_oversized_continuation_is_hidden(
            "Authorization: Bearer " + "B" * (_STREAM_HOLDBACK_JWT_MAX + 500)
        )

    def test_jwt_discard_preserves_non_jwt_continuation(self) -> None:
        from kiro_crew.security import (
            _STREAM_HOLDBACK_JWT_MAX,
            REDACTED_CREDENTIAL_TAG,
            StreamRedactor,
        )

        redactor = StreamRedactor()
        jwt = _jose_header(_STREAM_HOLDBACK_JWT_MAX + 500) + ".eyJz.SflK"
        assert redactor.feed(jwt) == REDACTED_CREDENTIAL_TAG
        assert redactor._discarding
        assert redactor.feed("!Important") + redactor.flush() == "!Important"

    def test_bearer_discard_preserves_non_bearer_continuation(self) -> None:
        from kiro_crew.security import (
            _STREAM_HOLDBACK_JWT_MAX,
            REDACTED_CREDENTIAL_TAG,
            StreamRedactor,
        )

        redactor = StreamRedactor()
        bearer = "Authorization: Bearer " + "B" * (_STREAM_HOLDBACK_JWT_MAX + 500)
        assert redactor.feed(bearer) == REDACTED_CREDENTIAL_TAG
        assert redactor._discarding
        assert redactor.feed("!Important") + redactor.flush() == "!Important"

    def test_token_param_discard_keeps_own_class_until_terminator(self) -> None:
        from kiro_crew.security import (
            _STREAM_HOLDBACK_JWT_MAX,
            REDACTED_CREDENTIAL_TAG,
            StreamRedactor,
        )

        redactor = StreamRedactor()
        token_param = "?token=" + "T" * (_STREAM_HOLDBACK_JWT_MAX + 500)
        assert redactor.feed(token_param) == REDACTED_CREDENTIAL_TAG
        assert redactor._discarding
        assert redactor.feed("!SecretTail done") + redactor.flush() == " done"

    def test_jwt_discard_keeps_base64url_continuation_until_terminator(self) -> None:
        from kiro_crew.security import (
            _STREAM_HOLDBACK_JWT_MAX,
            REDACTED_CREDENTIAL_TAG,
            StreamRedactor,
        )

        redactor = StreamRedactor()
        jwt = _jose_header(_STREAM_HOLDBACK_JWT_MAX + 500) + ".eyJz.SflK"
        assert redactor.feed(jwt) == REDACTED_CREDENTIAL_TAG
        assert redactor._discarding
        assert redactor.feed("More.JWT_- done") + redactor.flush() == " done"

    def test_flush_while_discarding_emits_no_continuation(self) -> None:
        from kiro_crew.security import _STREAM_HOLDBACK_JWT_MAX, StreamRedactor

        redactor = StreamRedactor()
        redactor.feed("?token=" + "T" * (_STREAM_HOLDBACK_JWT_MAX + 500))
        assert redactor._discarding
        assert redactor.flush() == ""
        assert not redactor._discarding

    def test_reset_clears_discard_state(self) -> None:
        from kiro_crew.security import _STREAM_HOLDBACK_JWT_MAX, StreamRedactor

        redactor = StreamRedactor()
        redactor.feed("?token=" + "T" * (_STREAM_HOLDBACK_JWT_MAX + 500))
        assert redactor._discarding
        redactor.reset()
        assert not redactor._discarding
        assert redactor.feed("visible ") + redactor.flush() == "visible "

    def test_complete_over_ceiling_value_with_in_buffer_terminator_commits(self) -> None:
        from kiro_crew.security import StreamRedactor

        redactor = StreamRedactor()
        first = redactor.feed("?token=" + "T" * 5000 + " done")
        assert first == "?token=[REDACTED: credential] "
        assert not redactor._discarding
        assert redactor.flush() == "done"

    def test_discard_stops_at_the_next_query_parameter(self) -> None:
        from kiro_crew.security import (
            _STREAM_HOLDBACK_JWT_MAX,
            REDACTED_CREDENTIAL_TAG,
            StreamRedactor,
        )

        redactor = StreamRedactor()
        first = redactor.feed("?token=" + "T" * (_STREAM_HOLDBACK_JWT_MAX + 500))
        assert first == REDACTED_CREDENTIAL_TAG
        assert redactor._discarding
        remainder = redactor.feed("TTTT&next=visible done") + redactor.flush()
        assert remainder == "&next=visible done"
        assert "TTTT" not in remainder

    def test_discard_stops_at_a_fragment(self) -> None:
        from kiro_crew.security import (
            _STREAM_HOLDBACK_JWT_MAX,
            REDACTED_CREDENTIAL_TAG,
            StreamRedactor,
        )

        redactor = StreamRedactor()
        first = redactor.feed("?token=" + "T" * (_STREAM_HOLDBACK_JWT_MAX + 500))
        assert first == REDACTED_CREDENTIAL_TAG
        assert redactor._discarding
        remainder = redactor.feed("TTTT#anchor visible") + redactor.flush()
        assert remainder == "#anchor visible"
        assert "TTTT" not in remainder

    def test_drop_preserves_suffix_when_the_value_ended_inside_the_buffer(self) -> None:
        """Fail-closed drops only the token region, never its benign suffix."""
        from kiro_crew.security import REDACTED_CREDENTIAL_TAG, StreamRedactor

        value = "A" * 5000 + "!" + "B" * 100
        redactor = StreamRedactor()
        first = redactor.feed(f"?token={value}&next=visible")
        assert first == REDACTED_CREDENTIAL_TAG
        assert not redactor._discarding
        assert redactor._buf == "&next=visible"
        assert first + redactor.flush() == REDACTED_CREDENTIAL_TAG + "&next=visible"

    def test_drop_arms_when_the_same_value_reaches_the_buffer_end(self) -> None:
        """The same oversized value remains sticky when its continuation is unknown."""
        from kiro_crew.security import REDACTED_CREDENTIAL_TAG, StreamRedactor

        value = "A" * 5000 + "!" + "B" * 100
        redactor = StreamRedactor()
        first = redactor.feed(f"?token={value}")
        assert first == REDACTED_CREDENTIAL_TAG
        assert redactor._discarding
        assert redactor.feed("&next=visible") + redactor.flush() == "&next=visible"
        assert not redactor._discarding

    @staticmethod
    def _over_ceiling_crossing_with_weak_tail() -> str:
        """A complete over-ceiling ``?token=`` value followed by a WEAK ``&tok`` tail.

        The ``;`` is outside ``_CRED_CLASS`` but inside the token value class,
        so Phase A's natural cut lands INSIDE the value: the complete match
        crosses the cut (STRONG), the buffer exceeds the 4096 ceiling with the
        cut at zero, and the only thing at the buffer end is a bare name prefix
        with no ``=`` (WEAK). Nothing STRONG reaches the buffer end.
        """
        from kiro_crew import security

        value = "A" * 2000 + ";" + "A" * (security._STREAM_HOLDBACK_JWT_MAX - 1500)
        text = f"?token={value}&tok"
        assert ";" not in security._CRED_CLASS
        assert all(char in security._CRED_CLASS for char in "A&tok")
        assert len(text) > security._STREAM_HOLDBACK_JWT_MAX
        return text

    def test_weak_tail_on_over_ceiling_crossing_does_not_crash(self) -> None:
        """A WEAK ``&tok`` tail past an over-ceiling crossing must not raise.

        The drop covers the completed value only; the WEAK prefix is HELD (the
        existing holdback), and its completion follows the weak-completion rules.
        """
        from kiro_crew.security import REDACTED_CREDENTIAL_TAG, StreamRedactor

        redactor = StreamRedactor()
        first = redactor.feed(self._over_ceiling_crossing_with_weak_tail())
        assert first == REDACTED_CREDENTIAL_TAG
        assert "A" * 32 not in first
        assert redactor._buf == "&tok"
        joined = first + redactor.feed("en=secret&x=1") + redactor.flush()
        assert "secret" not in joined
        assert joined == REDACTED_CREDENTIAL_TAG + "&token=[REDACTED: credential]&x=1"

    def test_weak_tail_never_arms_the_sticky_discard(self) -> None:
        """WEAK evidence holds bytes but never arms a drop or names a discard kind."""
        from kiro_crew.security import REDACTED_CREDENTIAL_TAG, StreamRedactor

        redactor = StreamRedactor()
        first = redactor.feed(self._over_ceiling_crossing_with_weak_tail())
        assert first == REDACTED_CREDENTIAL_TAG
        assert not redactor._discarding
        assert redactor._discard_kind is None
        # Breaking the prefix releases it verbatim: nothing was dropped off it.
        assert redactor.feed(" visible") + redactor.flush() == "&tok visible"

    def test_drop_ending_at_terminator_does_not_suppress_next_chunk(self) -> None:
        from kiro_crew.security import _STREAM_HOLDBACK_JWT_MAX, StreamRedactor

        redactor = StreamRedactor()
        first = redactor.feed("?token=" + "T" * (_STREAM_HOLDBACK_JWT_MAX + 500) + " ")
        assert first == "?token=[REDACTED: credential] "
        assert redactor._buf == ""
        assert not redactor._discarding
        assert first + redactor.feed("next ") + redactor.flush() == (
            "?token=[REDACTED: credential] next "
        )

    def test_post_bound_continuation_never_resumes_raw(self) -> None:
        """Crossing the 1 MiB discard bound must not turn fail-closed into fail-open.

        With no terminator in sight, the credential's continuation past the
        bound is STILL the credential. It must never re-enter Phase A as an
        anchorless run (where the 512 floor would bisect it and stream it raw):
        every byte after the bound stays dropped, in ``feed()`` and ``flush()``.
        """
        from kiro_crew.security import (
            _STREAM_DISCARD_MAX,
            _STREAM_HOLDBACK_JWT_MAX,
            REDACTED_CREDENTIAL_TAG,
            StreamRedactor,
        )

        redactor = StreamRedactor()
        armed = redactor.feed("?token=" + "T" * (_STREAM_HOLDBACK_JWT_MAX + 500))
        assert armed == REDACTED_CREDENTIAL_TAG
        assert redactor._discarding

        chunk = "Q" * (64 * 1024)
        pieces: list[str] = []
        fed = 0
        while fed <= _STREAM_DISCARD_MAX:  # cross the real production bound
            pieces.append(redactor.feed(chunk))
            fed += len(chunk)

        post_bound = "post_bound_leak_" + "Z" * 40
        pieces.append(redactor.feed(post_bound))
        pieces.append(redactor.flush())
        joined = "".join(pieces)
        assert "post_bound_leak" not in joined
        assert "Q" * 32 not in joined
        # Nothing but the tag itself ever leaves the discard.
        assert joined.replace(REDACTED_CREDENTIAL_TAG, "") == ""

    def test_hard_bound_retags_and_keeps_discarding_until_a_terminator(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The bound caps the bytes dropped BETWEEN tags; only a terminator exits.

        Reaching the bound re-emits the tag once per bound-worth of silent
        continuation (so a reader sees the drop is ongoing and the counter stays
        O(1)) and keeps discarding; there is no permanent wedge because the next
        terminator byte still exits the discard and releases the trailing text.
        """
        from kiro_crew import security

        monkeypatch.setattr(security, "_STREAM_DISCARD_MAX", 64)
        tag = security.REDACTED_CREDENTIAL_TAG
        redactor = security.StreamRedactor()
        assert redactor.feed("?token=" + "T" * (security._STREAM_HOLDBACK_JWT_MAX + 500)) == tag
        assert redactor._discarding

        # Under the bound: silent.
        assert redactor.feed("C" * 63) == ""
        assert redactor._discarding
        # The byte that reaches the bound: one tag, counter reset, still discarding.
        assert redactor.feed("C") == tag
        assert redactor._discarding
        assert redactor._discarded == 0
        # A second bound-worth of continuation re-tags exactly once more.
        assert redactor.feed("C" * 64) == tag
        assert redactor._discarding
        assert redactor.feed("C" * 63) == ""
        # A terminator exits the discard -- even when the same chunk crosses the
        # bound -- and the trailing text is emitted with no extra tag.
        assert redactor.feed("CC&next=1 trailing") + redactor.flush() == "&next=1 trailing"
        assert not redactor._discarding

    def test_plain_cred_run_past_ceiling_still_committed(self) -> None:
        """A plain cred-class run with NO credential anchor is not dropped.

        No-data-loss guard: the fail-closed drop
        fires ONLY for a credential-anchored tail. A benign long alphanumeric run
        past the ceiling is still committed verbatim (bisected, no data loss),
        keeping the DoS bound intact without corrupting non-secret output.
        """
        from kiro_crew.security import _STREAM_HOLDBACK_JWT_MAX, StreamRedactor

        blob = "a" * (_STREAM_HOLDBACK_JWT_MAX + 600)  # no eyJ / Bearer anchor
        r = StreamRedactor()
        emitted = r.feed(blob) + r.flush()
        assert emitted == blob  # committed in full, nothing dropped


class TestScanMemoryImportGuard:
    """scan_memory()'s optional vector_memory import must degrade gracefully on
    ANY import-time failure — not only ImportError. A C-extension can raise
    OSError (or another Exception) at import; the old ``except ImportError``
    let that crash the caller instead of skipping the scan."""

    def test_non_importerror_degrades_to_empty(self, monkeypatch) -> None:
        import builtins

        from kiro_crew.security import scan_memory

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "kiro_crew.vector_memory" or name.endswith(".vector_memory"):
                raise OSError("simulated C-extension load failure")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        # Must return cleanly (empty findings), not raise.
        assert scan_memory() == []
