"""Tests for kiro_crew.security: pass 4 of ``redact_credentials``, the ``?token=`` URL parameter.

The value of a ``token`` query parameter is redacted by the parameter's name rather than the
value's shape (template placeholders excepted), in batch and across ``StreamRedactor`` chunks,
and the companion URL prefixes whose token pass 4 leaves alone.
"""

from __future__ import annotations

import random
import re

import pytest

from kiro_crew.security import redact_credentials


class TestTokenParamValueRedaction:
    """Pass 4: the VALUE of a `?token=` / `&token=` URL parameter.

    Keyed on the parameter NAME, not the value's shape, so an OPAQUE bearer
    value -- one that looks nothing like a JWT -- is redacted where every
    shape-based pass sees ordinary text. `token=` stays visible so a redacted
    URL still reads as a token URL. The pass is deliberately absent from
    `_contains_fixed_credential`, so the request-blocking surface in
    `exfil.py` is unchanged.
    """

    # Opaque: mixed-case alphanumerics, no `eyJ` prefix, far under the 40-char
    # bare-secret floor -- invisible to every shape-based pass. An `eyJ`-shaped
    # value here would make this pass look tested when it is not.
    _OPAQUE = "Xk9fQ2mP4nR7sT1v"

    def test_opaque_value_is_redacted(self) -> None:
        """An opaque non-`eyJ` value in a token URL must not survive."""
        text = f"open https://host.example.com/?token={self._OPAQUE} to continue"
        result, warnings = redact_credentials(text)
        assert self._OPAQUE not in result
        assert result == ("open https://host.example.com/?token=[REDACTED: credential] to continue")
        assert warnings == ["Redacted token parameter value (16 chars)"]

    def test_ampersand_form_and_neighbour_params_survive(self) -> None:
        text = f"https://h.example/x?a=1&token={self._OPAQUE}&b=2"
        result, _ = redact_credentials(text)
        assert result == "https://h.example/x?a=1&token=[REDACTED: credential]&b=2"

    def test_template_placeholder_dollar_is_not_redacted(self) -> None:
        """A JS/TS `${...}` placeholder after `token=` is code, not a value."""
        for text in (
            "const url = `${base}&token=${encodeURIComponent(jwt)}`;",
            "https://h.example/x?token=${x}",
        ):
            result, warnings = redact_credentials(text)
            assert result == text, text
            assert warnings == [], text

    def test_dollar_values_without_placeholder_still_redact(self) -> None:
        """Only `${` is exempt: a `$`-led value or a lone trailing `$` is redacted."""
        for value in ("$abc", "$"):
            for tail in ("", "&b=2"):
                result, warnings = redact_credentials(f"https://h/x?token={value}{tail}")
                assert result == f"https://h/x?token=[REDACTED: credential]{tail}", (value, tail)
                assert warnings == [f"Redacted token parameter value ({len(value)} chars)"]

    def test_value_before_brace_still_redacts(self) -> None:
        result, warnings = redact_credentials("https://h/x?token=abc${x}")
        assert result == "https://h/x?token=[REDACTED: credential]{x}"
        assert warnings == ["Redacted token parameter value (4 chars)"]

    def test_stream_floor_cut_keeps_a_placeholder_whole(self) -> None:
        """A forced 512-char floor cut never splits `$` from `{`: stream equals batch."""
        from kiro_crew.security import StreamRedactor

        for size in range(480, 501):
            text = " &token=${" + "a" * size + " BEGIN RSA PRIVATE"
            redactor = StreamRedactor()
            streamed = redactor.feed(text) + redactor.flush()
            assert streamed == redact_credentials(text)[0], size

    def test_stream_matches_batch_at_every_chunk_cut_near_a_placeholder(self) -> None:
        from kiro_crew.security import StreamRedactor

        for text in (
            "x &token=${jwt} y",
            "x ?token=abc${x} y",
            " &token=${" + "a" * 490 + " BEGIN RSA PRIVATE",
        ):
            expected = redact_credentials(text)[0]
            at = text.index("${")
            for cut in range(max(0, at - 12), min(len(text), at + 12) + 1):
                redactor = StreamRedactor()
                streamed = redactor.feed(text[:cut]) + redactor.feed(text[cut:]) + redactor.flush()
                assert streamed == expected, (text[:20], cut)

    def test_entity_equals_does_not_capture_its_own_semicolon(self) -> None:
        """A present `;` is the reference's terminator, never the value.

        Red before the fix: the greedy EQ gave up its `;` to the value class,
        so pass 4 spliced the tag over the semicolon -- corrupting text
        `chat_runner.py` redacts IN PLACE with no recovery path.
        """
        for anchor in ("?token&#61;", "?token&#x3D;", "?token&#0000061;", "&token&#61;"):
            for tail in ("", "&x=1", "#frag", " tail"):
                text = f"https://h/p{anchor}{tail}"
                result, warnings = redact_credentials(text)
                assert result == text, (anchor, tail)  # corruption assertion
                assert ";" not in result or result.count(";") == text.count(";")
                assert warnings == [], (anchor, tail)

    def test_empty_entity_equals_value_matches_the_plain_equals_baseline(self) -> None:
        """`?token&#61;` with no value behaves exactly like `?token=` with no value."""
        for entity in ("&#61;", "&#x3D;", "&#0000061;", "&equals;"):
            for tail in ("", "&x=1"):
                assert redact_credentials(f"?token{entity}{tail}")[0] == f"?token{entity}{tail}"
                assert redact_credentials(f"?token={tail}")[0] == f"?token={tail}"

    def test_semicolon_led_value_is_still_redacted(self) -> None:
        """Still-green positive: a `;` AFTER the terminator is value, not structure."""
        for anchor in ("?token&#61;", "?token&#x3D;", "?token=", "?token&#0000061;"):
            result, warnings = redact_credentials(f"{anchor};{self._OPAQUE}")
            assert result == f"{anchor}[REDACTED: credential]"
            assert warnings == [f"Redacted token parameter value ({len(';' + self._OPAQUE)} chars)"]

    def test_terminators_do_not_swallow_following_text(self) -> None:
        """The value class stops at whitespace, `&`, quotes and `#`.

        Wider than the `[^\\s&]+` precedent in `instances/token_mint._TOKEN_RE`
        on purpose: a quoted URL or a fragment must keep its closing quote and
        its fragment text.
        """
        cases = [
            (f"?token={self._OPAQUE} trailing prose", " trailing prose"),
            (f"?token={self._OPAQUE}#fragment", "#fragment"),
            (f'link "?token={self._OPAQUE}" quoted', '" quoted'),
            (f"link '?token={self._OPAQUE}' quoted", "' quoted"),
            (f"?token={self._OPAQUE}&next=1", "&next=1"),
            # RFC 3986-excluded bytes also terminate: a value glued to prose
            # or markup by one of them keeps everything past the stop.
            (f"?token={self._OPAQUE}<br>", "<br>"),
            (f"?token={self._OPAQUE}}}末尾", "}末尾"),
            (f"?token={self._OPAQUE}`code`", "`code`"),
        ]
        for text, kept in cases:
            result, _ = redact_credentials(text)
            assert self._OPAQUE not in result, text
            assert kept in result, text

    def test_bare_token_assignment_outside_a_query_not_matched(self) -> None:
        """The anchor is `[?&]token=`: a prose or config `token=` is not a URL
        parameter, and matching it would false-positive on ordinary text."""
        text = f"config line token={self._OPAQUE} stays"
        result, warnings = redact_credentials(text)
        assert result == text
        assert warnings == []

    def test_near_miss_parameter_names_not_matched(self) -> None:
        """Anchor-widening control: names that merely CONTAIN `token` stay.

        Each of these reds if the anchor loosens (`token[^=]*=`, a missing `=`
        or a dropped `[?&]`), which is the widening a shape-based pass invites
        and this name-keyed pass must not."""
        for text in (
            "https://h.example/?tokens=abc123 listing",
            "https://h.example/?token_id=abc123 lookup",
            "https://h.example/?mytoken=abc123 legacy",
            "https://h.example/?tokenizer=bpe model",
        ):
            result, warnings = redact_credentials(text)
            assert result == text, text
            assert warnings == [], text

    def test_parameter_name_folds_ascii_case_value_does_not(self) -> None:
        """ASCII-cased `token` names carry the same bearer value."""
        for name in ("Token", "TOKEN", "toKen"):
            text = f"https://h.example/?{name}={self._OPAQUE}"
            result, _ = redact_credentials(text)
            assert self._OPAQUE not in result, name
            assert f"{name}=[REDACTED: credential]" in result, name

    def test_named_percent_entity_is_case_sensitive(self) -> None:
        """Only the exact WHATWG named reference decodes to a percent sign."""
        from kiro_crew.security.redaction import _TOKEN_PARAM_PARTIAL_RE

        for name in ("&PERCNT;74oken", "&PerCnt;74oken"):
            text = f"?{name}={self._OPAQUE}"
            assert redact_credentials(text) == (text, []), name
            assert _TOKEN_PARAM_PARTIAL_RE.search(f"?{name}=") is None, name

        lowercase = f"?&percnt;74oken={self._OPAQUE}"
        assert redact_credentials(lowercase)[0] == ("?&percnt;74oken=[REDACTED: credential]")
        assert _TOKEN_PARAM_PARTIAL_RE.search("?&percnt;74oken=") is not None

    def test_numeric_percent_reference_keeps_ascii_hex_case_fold(self) -> None:
        """HTML numeric references accept either x case; named refs do not."""
        for name in ("&#x25;74oken", "&#X25;74oken", "&#X025;74oken"):
            text = f"?{name}={self._OPAQUE}"
            result, warnings = redact_credentials(text)
            assert result == f"?{name}=[REDACTED: credential]", name
            assert warnings == ["Redacted token parameter value (16 chars)"], name

    def test_parameter_name_rejects_unicode_casefold_homoglyphs(self) -> None:
        """Unicode case-fold lookalikes are not parser-equivalent to `token`."""
        for name in ("to\u212aen", "TO\u212aEN"):
            text = f"?{name}=somevalue"
            result, warnings = redact_credentials(text)
            assert result == text, name
            assert warnings == [], name

    def test_stream_partial_rejects_unicode_casefold_homoglyphs(self) -> None:
        """A trailing Kelvin-spelled name is not a streaming token anchor."""
        from kiro_crew.security.redaction import _TOKEN_PARAM_PARTIAL_RE

        for name in ("to\u212aen", "TO\u212aEN"):
            assert _TOKEN_PARAM_PARTIAL_RE.search(f"?{name}=") is None, name

    def test_percent_encoded_parameter_name_is_redacted(self) -> None:
        """A parser decodes `to%6ben` to `token`; the scrubber must agree."""
        text = f"https://h.example/?to%6ben={self._OPAQUE}"
        result, warnings = redact_credentials(text)
        assert result == "https://h.example/?to%6ben=[REDACTED: credential]"
        assert warnings == ["Redacted token parameter value (16 chars)"]

    def test_fully_and_mixed_encoded_parameter_names_are_redacted(self) -> None:
        for name in ("%74%6F%6B%65%6E", "to%6Ben"):
            text = f"https://h.example/?{name}={self._OPAQUE}"
            result, _ = redact_credentials(text)
            assert self._OPAQUE not in result, name
            assert result == f"https://h.example/?{name}=[REDACTED: credential]"

    def test_percent_encoded_near_miss_names_not_matched(self) -> None:
        """Wrong and invalid escapes do not decode to the `token` name."""
        for name in ("to%6Aen", "to%6gen"):
            text = f"https://h.example/?{name}=x"
            result, warnings = redact_credentials(text)
            assert result == text, name
            assert warnings == [], name

    def test_percent_encoded_name_split_mid_escape_not_leaked(self) -> None:
        """A chunk ending mid-escape holds the whole parameter until redaction."""
        from kiro_crew.security import StreamRedactor

        secret_head = self._OPAQUE[:8]
        secret_tail = self._OPAQUE[8:]
        redactor = StreamRedactor()
        emitted = [
            redactor.feed("?to%6"),
            redactor.feed("ben=" + secret_head),
            redactor.feed(secret_tail),
            redactor.flush(),
        ]
        assert all(secret_head not in piece and secret_tail not in piece for piece in emitted)
        assert "".join(emitted) == "?to%6ben=[REDACTED: credential]"

    def test_stream_partial_matches_every_encoded_name_prefix(self) -> None:
        """Every valid separator/name prefix pair stays buffered."""
        from itertools import product

        from kiro_crew.security import _CRED_CLASS
        from kiro_crew.security.redaction import _TOKEN_PARAM_PARTIAL_RE

        letter_spellings = (
            ("t", "%74", "%54", "&#116;", "&#84;", "&#x74;", "&#x54;", "&#37;74", "&percnt;74"),
            ("o", "%6F", "%4F", "&#111;", "&#79;", "&#x6f;", "&#x4f;", "&#37;6f", "&percnt;6f"),
            ("k", "%6B", "%4B", "&#107;", "&#75;", "&#x6b;", "&#x4b;", "&#37;6b", "&percnt;6b"),
            ("e", "%65", "%45", "&#101;", "&#69;", "&#x65;", "&#x45;", "&#37;65", "&percnt;65"),
            ("n", "%6E", "%4E", "&#110;", "&#78;", "&#x6e;", "&#x4e;", "&#37;6e", "&percnt;6e"),
        )
        separator_spellings = (
            "?",
            "&",
            "&amp;",
            "&AMP;",
            "&#38;",
            "&#38",
            "&#0000000038;",
            "&#x26;",
            "&#X26",
            "&#x0000000026;",
            "&quest;",
            "&#63;",
            "&#63",
            "&#0000000063;",
            "&#x3F;",
            "&#X3f",
            "&#x000000003F;",
        )
        for parts in product(*letter_spellings):
            name = "".join(parts)
            separators = separator_spellings
            if name[0] in "%&":
                separators += ("&amp", "&AMP")
            for separator in separators:
                for end in range(1, len(name) + 1):
                    prefix = name[:end]
                    assert prefix[-1] in _CRED_CLASS or _TOKEN_PARAM_PARTIAL_RE.search(
                        f"{separator}{prefix}"
                    ), f"{separator}{prefix}"

    def test_stream_holds_complete_entity_name_parts_across_chunks(self) -> None:
        """A completed entity must not release an in-progress token name."""
        from kiro_crew.security import StreamRedactor

        for first_chunk, second_chunk in (
            ("?t&#111;", "ken=" + self._OPAQUE),
            ("?&percnt;", "74oken=" + self._OPAQUE),
            ("?&#37;&#55;", "&#52;oken=" + self._OPAQUE),
        ):
            redactor = StreamRedactor()
            emitted = [redactor.feed(first_chunk)]
            assert emitted[0] == "", first_chunk
            emitted.extend((redactor.feed(second_chunk), redactor.flush()))
            joined = "".join(emitted)
            assert self._OPAQUE not in joined, first_chunk
            assert "[REDACTED: credential]" in joined, first_chunk

    def test_fully_composed_anchor_fits_stream_holdback_bound(self) -> None:
        """The longest separator plus five composed letters remains bounded."""
        from kiro_crew.security import _STREAM_HOLDBACK_MAX

        def html_hex_byte(byte: str) -> str:
            return f"&#x{'0' * 8}{ord(byte):X};"

        separator = "&#x000000003F;"
        name = "".join(
            "".join(html_hex_byte(byte) for byte in f"%{ord(letter):x}") for letter in "token"
        )
        anchor = separator + name
        assert len(anchor) <= 224
        assert len(anchor) < _STREAM_HOLDBACK_MAX

    def test_html_entity_separators_and_equals_are_redacted(self) -> None:
        """Every bounded HTML5 spelling decoded before query parsing is structure."""
        cases = (
            ("?", "token", "="),
            ("?", "t&#111;ken", "="),
            ("&", "token", "="),
            ("&amp;", "token", "="),
            ("&amp;", "t&#111;ken", "&equals;"),
            ("&AMP;", "token", "="),
            ("&amp", "%74oken", "="),
            ("&AMP", "%74oken", "="),
            ("&amp", "&#116;oken", "="),
            ("&AMP", "&#x74;oken", "="),
            ("&#38;", "token", "="),
            ("&#38", "token", "="),
            ("&#0000000038;", "token", "="),
            ("&#x26;", "token", "="),
            ("&#X26", "token", "="),
            ("&#x0000000026;", "token", "="),
            ("&quest;", "token", "="),
            ("&#63;", "token", "="),
            ("&#63", "token", "="),
            ("&#0000000063;", "token", "="),
            ("&#x3F;", "token", "="),
            ("&#X3f", "token", "="),
            ("&#x000000003F;", "token", "="),
            ("?", "&#x74;oken", "="),
            ("?", "&#84;oken", "="),
            ("?", "&#37;74oken", "="),
            ("?", "&percnt;74oken", "="),
            ("?", "%&#55;&#52;oken", "="),
            ("?", "&#x25;&#x37;&#x34;oken", "="),
            ("?", "t%6&#70;ken", "="),
            ("?", "t%6&#102;ken", "="),
            ("?", "t&#111ken", "="),
            ("?", "token", "&equals;"),
            ("?", "token", "&#61;"),
            ("?", "token", "&#61"),
            ("?", "token", "&#0000000061;"),
            ("?", "token", "&#x3D;"),
            ("?", "token", "&#X3d"),
            ("?", "token", "&#x000000003D;"),
        )
        for separator, name, equals in cases:
            anchor = f"{separator}{name}{equals}"
            result, warnings = redact_credentials(f"{anchor}{self._OPAQUE}")
            assert result == f"{anchor}[REDACTED: credential]", anchor
            assert warnings == ["Redacted token parameter value (16 chars)"], anchor

    def test_non_decoding_separator_and_equals_spellings_are_unchanged(self) -> None:
        """Near entities, double encodings, and later-stage escapes stay data."""
        texts = (
            f"&amptoken={self._OPAQUE}",
            f"&Amp;token={self._OPAQUE}",
            f"&questtoken={self._OPAQUE}",
            f"?token&equals{self._OPAQUE}",
            f"&amp;amp;token={self._OPAQUE}",
            f"%26token={self._OPAQUE}",
            f"?token%3D{self._OPAQUE}",
            f"&#382token={self._OPAQUE}",
            f"?token&#61123abc{self._OPAQUE}",
            f"&#{'0' * 9}38;token={self._OPAQUE}",
            f"?&percnt74oken={self._OPAQUE}",
            f"?&#{'0' * 9}116;oken={self._OPAQUE}",
            f"&ampToken={self._OPAQUE}",
        )
        for text in texts:
            result, warnings = redact_credentials(text)
            assert result == text, text
            assert warnings == [], text

    def test_source_and_doc_templates_not_matched(self) -> None:
        """A token-URL TEMPLATE in source or docs must survive.

        The value class excludes the RFC 3986-forbidden bytes, so a value
        beginning with one -- an f-string placeholder, a markdown backtick, an
        angle-bracket placeholder -- is no match at all. This is the chip-diff
        surface: `chat_runner.py` redacts file snapshots IN PLACE, so a false
        positive here rewrites a snapshot of `dashboard/urls.py` with no
        recovery path."""
        for text in (
            'url = f"{base}?token={token}"',
            "open `http://host:7777?token=` in a browser",
            "http://localhost:7777?token=<your-token-here>",
            "curl {base}?token={quote(tok)} --silent",
        ):
            result, warnings = redact_credentials(text)
            assert result == text, text
            assert warnings == [], text

    def test_value_already_caught_by_pass_1_is_byte_identical(self) -> None:
        """A pass-1-shaped value keeps pass 1's tag and single warning.

        Pass 4 ranks last and claims only uncovered gaps, so outputs existing
        callers assert on do not change for values the scrubber already caught.
        """
        result, warnings = redact_credentials("?token=AKIAIOSFODNN7EXAMPLE")
        assert result == "?token=[REDACTED: credential]"
        assert warnings == ["Redacted credential pattern (20 chars)"]

    def test_value_already_caught_by_pass_3_is_single_tagged(self) -> None:
        """A bare 40-char secret AS the token value is claimed once, by pass 3.

        This pins the `taken = sorted(taken + pass3)` fold ahead of pass 4:
        without it, pass 4 re-claims the span pass 3 took, `_splice` receives
        overlapping spans, the tag doubles, and the dashboard's redaction
        notice counts two credentials where one was removed.
        """
        secret = "wJalrXUtnFEMI/K7MDENG/" "bPxRfiCYEXAMPLEKEY"
        result, warnings = redact_credentials(f"?token={secret}")
        assert result == "?token=[REDACTED: credential]"
        assert warnings == [f"Redacted bare secret key ({len(secret)} chars)"]

    def test_redacted_output_is_a_fixed_point(self) -> None:
        """Canonical credential tags are fixed points; an exfil tag collapses once."""
        for text in (
            f"?token={self._OPAQUE}",
            f"https://h.example/?token=AKIAIOSFODNN7EXAMPLE&x=1 and ?token={self._OPAQUE}",
        ):
            once, _ = redact_credentials(text)
            twice, warnings = redact_credentials(once)
            assert twice == once, text
            assert warnings == [], text

        exfil_tagged = "?token=[REDACTED: suspicious URL to collect.example]"
        once, _ = redact_credentials(exfil_tagged)
        twice, warnings = redact_credentials(once)
        assert once != exfil_tagged
        assert twice == once
        assert warnings == []

    def test_attacker_authored_redaction_prefix_does_not_bypass(self) -> None:
        """A redaction-shaped prefix is untrusted unless it is a fixed literal."""
        secret = "sk_live_" "51H8xY2abcdefghijklmnop"
        result, _ = redact_credentials(f"?token=[REDACTED{secret}")
        assert result.startswith("?token=[REDACTED: credential]")
        assert "[REDACTED: credential]" in result
        assert secret not in result

        from kiro_crew.security import StreamRedactor

        redactor = StreamRedactor()
        emitted = [
            redactor.feed("?token=[REDACTED"),
            redactor.feed(secret),
            redactor.flush(),
        ]
        joined = "".join(emitted)
        assert all(secret not in piece for piece in emitted)
        assert joined.startswith("?token=[REDACTED: credential]")
        assert secret not in joined

    def test_fake_exfil_tags_do_not_bypass(self) -> None:
        """Pass 4 claims only group(1), so the post-space residual matches the
        tag-free witness ``?token=a Zk8q.co`` while the value head is redacted.
        """
        contiguous = "?token=[REDACTED:suspicious-URL-to-Zk8q.co]"
        result, _ = redact_credentials(contiguous)
        assert result == "?token=[REDACTED: credential]"
        assert "Zk8q.co" not in result

        spaced = "?token=[REDACTED: suspicious URL to Zk8q.co]"
        result, _ = redact_credentials(spaced)
        assert result == "?token=[REDACTED: credential] suspicious URL to Zk8q.co]"

    def test_registered_credential_tags_are_fixed_points(self) -> None:
        """Every module-owned credential-tag literal is trusted by construction."""
        from kiro_crew.security import CREDENTIAL_REDACTION_TAGS

        for tag in CREDENTIAL_REDACTION_TAGS:
            text = f"?token={tag}"
            result, warnings = redact_credentials(text)
            assert result == text
            assert warnings == []

    def test_blocking_surface_unchanged(self) -> None:
        """Redaction-only: `_contains_fixed_credential` gates request-BLOCKING
        decisions in `exfil.py` and must not learn the parameter name -- a
        `?token=` URL is maskable output, never itself grounds to block."""
        from kiro_crew.security.redaction import _contains_fixed_credential

        assert not _contains_fixed_credential(f"https://h.example/?token={self._OPAQUE}")

    def test_exfil_first_composition_still_replaces_the_whole_url(self) -> None:
        """A long opaque token must not defeat the whole-URL exfil redaction.

        `redact_exfiltration_urls` classifies partly by query LENGTH and
        replaces the ENTIRE url. Pass 4 shortening `?token=<210 chars>` to its
        tag ahead of that scan would drop the query below the threshold, so
        the attacker host and every OTHER parameter -- the actual payload,
        which no name-keyed pass matches -- would render verbatim. The
        canonical composition (`redact` / `redact_with_findings`) runs the
        exfil pass FIRST, and this pins that pass 4 does not break it.
        """
        from kiro_crew.security import redact

        url = (
            "fetching https://collect.attacker.example/?token="
            + "aB3" * 70
            + "&host=corp-laptop&path=/home/alice/.aws/credentials"
        )
        out = redact(url)
        # The whole URL is replaced by the exfil tag, which NAMES the domain by
        # design ("suspicious URL to <domain>") -- what must be gone is the
        # fetchable URL and the payload parameters, not the domain word.
        assert out == "fetching [REDACTED: suspicious URL to collect.attacker.example]"
        assert "corp-laptop" not in out
        assert "/home/alice/.aws/credentials" not in out
        assert "?token=" not in out


class TestTokenParamExemptUrlPrefixes:
    """Companion URL prefixes whose ``?token=`` value pass 4 leaves alone.

    NEUTRAL PLACEHOLDER PREFIX ONLY: a companion's real approval host never
    appears in the public repo.
    """

    _PREFIX = "https://approve.example.com/auth"
    _TOKEN = "eyJ3b3JrZmxvd0lkIjoiMDAwMDAwMDAtMDAwMC00MDAwLTgwMDAtMDAwMDAwMDAwMDAwIn0%3D"

    class _StubCredentialPolicy:
        def __init__(self, prefixes: object):
            self._prefixes = prefixes

        def redact(self, text: str) -> str:
            from kiro_crew.security import redact

            return redact(text)

        def exempt_exact_hosts(self) -> "frozenset[str]":
            # The display battery also runs the exfil pass; exempt its heuristics
            # so the cache test isolates pass 4.
            return frozenset({"approve.example.com"})

        def token_param_exempt_url_prefixes(self) -> object:
            if isinstance(self._prefixes, Exception):
                raise self._prefixes
            return self._prefixes

    def _install(self, prefixes: object) -> None:
        import dataclasses

        from kiro_crew.config import KiroCrewConfig
        from kiro_crew.platform.bootstrap import build_default_context
        from kiro_crew.platform.context import set_context

        base = build_default_context(KiroCrewConfig())
        set_context(dataclasses.replace(base, credentials=self._StubCredentialPolicy(prefixes)))

    @property
    def _url(self) -> str:
        return f"{self._PREFIX}?token={self._TOKEN}"

    def test_default_redacts(self) -> None:
        out, warnings = redact_credentials(self._url)
        assert self._TOKEN not in out
        assert warnings == [f"Redacted token parameter value ({len(self._TOKEN)} chars)"]

    @pytest.mark.parametrize(
        "text",
        [
            "{u}",
            "Approve {u}",
            "[link]({u})",
            "<{u}>",
            '"{u}"',
            "'{u}'",
            "`{u}`",
            "*{u}*",
        ],
    )
    def test_exempt_prefix_keeps_value(self, text: str) -> None:
        self._install(frozenset({self._PREFIX}))
        rendered = text.format(u=self._url)
        assert redact_credentials(rendered) == (rendered, [])

    @pytest.mark.parametrize(
        ("origin", "text"),
        [
            *(
                ("test_prefix_nested_in_another_url_is_not_exempt", text)
                for text in (
                    "[Approve](https://evil.example/?next=({u}))",
                    "[Approve](//evil.example/?next=[x]({u}))",
                    "[Approve](/redirect?next=[x]({u}))",
                    "https://evil.example/?next=({u})",
                    "https://evil.example/?next=[{u}]",
                    "https://evil.example/?next=<{u}>",
                    'https://evil.example/?next="{u}"',
                    "https://evil.example/?next='{u}'",
                    "https://evil.example/?next=`{u}`",
                    "https://evil.example/?next=*{u}*",
                )
            ),
            *(
                ("test_browser_destination_embedding_is_not_exempt", text)
                for text in (
                    '<a href="https://evil.example/?next=\t{u}">Approve</a>',
                    '<a href="https://evil.example/?next=\n{u}">Approve</a>',
                    '<a href="https://evil.example/?next=\r{u}">Approve</a>',
                    '<a href="https://evil.example/?next= {u}">Approve</a>',
                    "<https://evil.example/?next= {u}>",
                    'href="//evil.example/?next=\t{u}"',
                    'href="https:\\evil.example/?next=\t{u}"',
                    'href="https:evil.example/?next=\t{u}"',
                )
            ),
        ],
    )
    def test_prefix_embedded_in_another_destination_is_not_exempt(
        self, origin: str, text: str
    ) -> None:
        self._install(frozenset({self._PREFIX}))
        assert self._TOKEN not in redact_credentials(text.format(u=self._url))[0]

    @pytest.mark.parametrize(
        "text",
        [
            "[Approve](<&sol;&sol;evil.example/?next= {u}>)",
            "[Approve](<&#104;ttps&#58;&#47;&#47;evil.example/?next= {u}>)",
            '<a href="&#x2F;&#x2F;evil.example/?next=\t{u}">Approve</a>',
            "&amp; {u}",
            "<x> {u}",
        ],
    )
    def test_encoded_or_tagged_destination_is_not_exempt(self, text: str) -> None:
        """A renderer decodes character references inside ``<...>`` and attributes, so
        the raw-text guard treats any ``&`` or ``<`` before the prefix as a
        destination signal instead of decoding it (double-encoding and renderer
        differences make decoding a moving target). Doubt means MORE redaction.
        """
        self._install(frozenset({self._PREFIX}))
        assert self._TOKEN not in redact_credentials(text.format(u=self._url))[0]

    def test_multiple_standalone_exempt_links_keep_both_values(self) -> None:
        self._install(frozenset({self._PREFIX}))
        text = f"- [First]({self._url})\n- [Second]({self._url})"
        assert redact_credentials(text) == (text, [])

    def test_multiple_autolinked_exempt_links_keep_both_values(self) -> None:
        """An exempt URL's own ``<`` opener is blanked with it, so it is not a tag signal."""
        self._install(frozenset({self._PREFIX}))
        text = f"- <{self._url}>\n- <{self._url}>"
        assert redact_credentials(text) == (text, [])

    def test_prose_label_before_exempt_link_keeps_value(self) -> None:
        self._install(frozenset({self._PREFIX}))
        text = f"Approve: {self._url}"
        assert redact_credentials(text) == (text, [])

    @pytest.mark.parametrize(
        "url",
        [
            "https://approve.example.com.evil.example/auth?token={t}",
            "https://x.approve.example.com/auth?token={t}",
            "https://user@approve.example.com/auth?token={t}",
            "http://approve.example.com/auth?token={t}",
            "https://approve.example.com/other?token={t}",
            "https://approve.example.com/auth/x?token={t}",
            "https://approve.example.com/auth?a=1&token={t}",
            "https://approve.example.com/auth?TOKEN={t}",
            "https://approve.example.com/auth&#63;token={t}",
            "xhttps://approve.example.com/auth?token={t}",
            "https://redirect.example/?next=https://approve.example.com/auth?token={t}",
            "https://redirect.example/https://approve.example.com/auth?token={t}",
            "https://redirect.example/#https://approve.example.com/auth?token={t}",
            "x:https://approve.example.com/auth?token={t}",
            "x@https://approve.example.com/auth?token={t}",
        ],
    )
    def test_other_shapes_still_redacted(self, url: str) -> None:
        self._install(frozenset({self._PREFIX}))
        assert self._TOKEN not in redact_credentials(url.format(t=self._TOKEN))[0]

    def test_earlier_pass_claim_keeps_whole_value_redacted(self) -> None:
        self._install(frozenset({self._PREFIX}))
        out = redact_credentials(f"{self._PREFIX}?token={self._TOKEN}AKIAIOSFODNN7EXAMPLE")[0]
        assert self._TOKEN not in out
        assert "AKIAIOSFODNN7EXAMPLE" not in out

    @pytest.mark.parametrize(
        "text",
        [
            "[A]({u}),[B](https://svc.example/cb?token=SECRET)",
            "[A]({u})[B](https://svc.example/cb?token=SECRET)",
            "{u}?token=SECRET",
            "{u}/cb?token=SECRET",
            "{u}:SECRET",
            "{u}(SECRET)",
            "{u}[SECRET]",
        ],
    )
    def test_value_run_past_the_token_is_not_exempt(self, text: str) -> None:
        """`_TOKEN_PARAM_VALUE_CLASS` admits `)`, `,`, `(`, `[`, `:` and `?`, so one
        pass-4 match can swallow a second URL after the exempt token. The exemption
        covers one unreserved/base64/percent-encoded run plus trailing closers only;
        anything else redacts the WHOLE value, which is also its re-redaction fixed point.
        """
        self._install(frozenset({self._PREFIX}))
        out, warnings = redact_credentials(text.format(u=self._url))
        assert "SECRET" not in out
        assert self._TOKEN not in out
        assert len(warnings) == 1 and warnings[0].startswith("Redacted token parameter value (")
        assert redact_credentials(out) == (out, [])

    @pytest.mark.parametrize("trail", [")", ").", "),", "]", "]:", "?", "!", ";"])
    def test_trailing_closers_after_the_token_stay_exempt(self, trail: str) -> None:
        self._install(frozenset({self._PREFIX}))
        text = f"Open {self._url}{trail} then wait"
        assert redact_credentials(text) == (text, [])

    @pytest.mark.parametrize(
        "prefixes",
        [
            frozenset({"https://approve.example.com"}),
            frozenset({"approve.example.com/auth"}),
            frozenset({"http://approve.example.com/auth"}),
            frozenset({"https://approve.example.com/auth?"}),
            frozenset({b"https://approve.example.com/auth"}),
            None,
            RuntimeError("adapter bug"),
        ],
    )
    def test_malformed_or_failing_seam_degrades_to_redaction(self, prefixes: object) -> None:
        self._install(prefixes)
        assert self._TOKEN not in redact_credentials(self._url)[0]

    def test_pre_method_adapter_degrades_to_redaction(self) -> None:
        import dataclasses

        from kiro_crew.config import KiroCrewConfig
        from kiro_crew.platform.bootstrap import build_default_context
        from kiro_crew.platform.context import set_context

        class _Old:
            def redact(self, text: str) -> str:
                return text

            def exempt_exact_hosts(self) -> "frozenset[str]":
                return frozenset()

        base = build_default_context(KiroCrewConfig())
        set_context(dataclasses.replace(base, credentials=_Old()))
        assert self._TOKEN not in redact_credentials(self._url)[0]

    def test_stream_windows_never_apply_the_exemption(self) -> None:
        from kiro_crew.security import REDACTED_CREDENTIAL_TAG, StreamRedactor

        self._install(frozenset({self._PREFIX}))
        split_messages = (
            ("[Approve](https://evil.example/?next=(", f"{self._url})) "),
            ('<a href="https://evil.example/?next= ', f'{self._url}">Approve</a>'),
        )
        for chunks in split_messages:
            redactor = StreamRedactor()
            streamed = "".join(redactor.feed(chunk) for chunk in chunks) + redactor.flush()
            assert self._TOKEN not in streamed
            assert REDACTED_CREDENTIAL_TAG in streamed

    def test_plain_approval_link_is_redacted_only_while_streaming(self) -> None:
        from kiro_crew.security import REDACTED_CREDENTIAL_TAG, StreamRedactor

        self._install(frozenset({self._PREFIX}))
        redactor = StreamRedactor()
        streamed = redactor.feed(f"{self._url} ") + redactor.flush()
        assert self._TOKEN not in streamed
        assert REDACTED_CREDENTIAL_TAG in streamed
        assert redact_credentials(self._url) == (self._url, [])

    def test_stream_suspension_resets_after_custom_redactor_exception(self) -> None:
        from kiro_crew.security import StreamRedactor

        self._install(frozenset({self._PREFIX}))

        def raising_redactor(text: str) -> str:
            assert self._TOKEN not in redact_credentials(self._url)[0]
            raise RuntimeError("redactor failed")

        redactor = StreamRedactor(raising_redactor)
        with pytest.raises(RuntimeError, match="redactor failed"):
            redactor.feed("safe text ")
        assert redact_credentials(self._url) == (self._url, [])

    def test_display_cache_key_covers_the_prefix_set(self) -> None:
        from kiro_crew.dashboard import chat_utils

        chat_utils._clear_display_redaction_cache()
        assert self._TOKEN not in chat_utils._redact_for_display(self._url)
        self._install(frozenset({self._PREFIX}))
        assert chat_utils._redact_for_display(self._url) == self._url
        chat_utils._clear_display_redaction_cache()

    def test_repeated_exempt_links_stay_linear(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Every scanner operation examines only a constant multiple of the input."""
        from kiro_crew.security import redaction as redaction_module

        work = {"boundary": 0, "destination": 0, "scheme": 0}
        calls = {"boundary": 0, "destination": 0}
        real_boundary = redaction_module._TokenParamExemptScan.boundary
        real_destination = redaction_module._token_param_destination_signal
        real_scheme = redaction_module._token_param_has_scheme_before

        def counting_boundary(scan: object, sep: int, start: int) -> bool:
            ws_cursor = scan.ws_cursor
            result = real_boundary(scan, sep, start)
            calls["boundary"] += 1
            work["boundary"] += 4 * max(0, sep - ws_cursor)
            if scan.last_ws + 1 >= scan.left_floor:
                work["boundary"] += max(0, start - scan.last_ws - 1)
            return result

        def counting_destination(text: str, start: int, end: int) -> bool:
            calls["destination"] += 1
            work["destination"] += end - start
            return real_destination(text, start, end)

        def counting_scheme(text: str, start: int, colon: int) -> bool:
            i = colon - 1
            while i >= start and text[i] in redaction_module._TOKEN_PARAM_SCHEME_CHARS:
                work["scheme"] += 1
                i -= 1
            return real_scheme(text, start, colon)

        monkeypatch.setattr(redaction_module._TokenParamExemptScan, "boundary", counting_boundary)
        monkeypatch.setattr(
            redaction_module, "_token_param_destination_signal", counting_destination
        )
        monkeypatch.setattr(redaction_module, "_token_param_has_scheme_before", counting_scheme)

        self._install(frozenset({self._PREFIX}))
        link = f"{self._PREFIX}?token=x"
        cases = (
            (f"{link} " * 20_000, True),
            ("& " + f"{link} " * 20_000, False),
            ("a" * 200_000 + " " + link, True),
            ("a1" * 100_000 + " " + link, True),
            ("a: " * 70_000 + link, True),
            ("a:" * 100_000 + link, False),
        )
        assert redaction_module._TOKEN_PARAM_DESTINATION_SIGNAL_RE.pattern == r"//|\\|&|<|:(?=\S)"
        for text, kept in cases:
            before = sum(work.values())
            out, _warnings = redact_credentials(text)
            assert (out == text) is kept
            assert sum(work.values()) - before <= 8 * len(text)
        assert calls["boundary"] > 0
        assert calls["destination"] > 0

    @staticmethod
    def _reference_token_param_exempt(
        text: str,
        m: "re.Match[str]",
        prefixes: tuple[str, ...],
        earlier_exempt_urls: list[tuple[int, int]],
    ) -> tuple[int, int] | None:
        """The seam's guards as first written: each candidate re-reads its whole prefix."""
        from kiro_crew.security.redaction import (
            _TOKEN_PARAM_DESTINATION_SIGNAL_RE,
            _TOKEN_PARAM_EXEMPT_OPENERS,
            _TOKEN_PARAM_EXEMPT_TRAIL,
            _TOKEN_PARAM_EXEMPT_VALUE_RE,
        )

        def boundary(start: int) -> bool:
            left = text[max(text.rfind(c, 0, start) for c in " \t\r\n") + 1 : start]
            if "://" in left:
                return False
            markdown_opener = re.fullmatch(r"\[[^][]*\]\(", left) is not None
            return not left or left in _TOKEN_PARAM_EXEMPT_OPENERS or markdown_opener

        def preceding_context(start: int) -> bool:
            pieces: list[str] = []
            cursor = 0
            for exempt_start, exempt_end in (*earlier_exempt_urls, (start, start)):
                if exempt_start > 0 and text[exempt_start - 1] == "<":
                    exempt_start -= 1
                pieces.extend((text[cursor:exempt_start], " " * (exempt_end - exempt_start)))
                cursor = exempt_end
            return _TOKEN_PARAM_DESTINATION_SIGNAL_RE.search("".join(pieces)) is None

        sep = m.start()
        if not text.startswith("?token=", sep):
            return None
        run = _TOKEN_PARAM_EXEMPT_VALUE_RE.match(text, m.start(1))
        if run is None or not set(text[run.end() : m.end(1)]) <= _TOKEN_PARAM_EXEMPT_TRAIL:
            return None
        for prefix in prefixes:
            start = sep - len(prefix)
            if (
                start >= 0
                and text.startswith(prefix, start)
                and boundary(start)
                and preceding_context(start)
            ):
                return start, run.end()
        return None

    def test_incremental_guards_match_the_whole_prefix_reference(self) -> None:
        """The cursor-based guards answer exactly as the whole-prefix statements do.

        Every pass-4 match of each text is put to both, in order, and also every
        other match, since the plan skips matches an earlier pass already claimed.
        """
        from kiro_crew.security.redaction import (
            _TOKEN_PARAM_RE,
            _token_param_exempt,
            _TokenParamExemptScan,
        )

        long_prefix = f"https://go.example/{self._PREFIX}"
        prefixes = tuple(sorted((self._PREFIX, long_prefix)))
        link = f"{self._PREFIX}?token=x"
        pieces = [
            link,
            f"<{link}>",
            f"[Approve]({link})",
            f"[Approve]({link}),",
            f"({link})",
            f"'{link}'",
            f"{long_prefix}?token=x",
            f"<{long_prefix}?token=x>",
            f"{link}?token=SECRET",
            "https://svc.example/cb?token=SECRET",
            f"https://evil.example/?next=({link})",
            f"[Approve](/redirect?next=[x]({link}))",
            f'<a href="https://evil.example/?next= {link}">Approve</a>',
            "&amp;",
            "&sol;&sol;evil.example",
            "<x>",
            "x:",
            "x:y",
            "foo:",
            "1abc:d",
            "a1" * 5 + ":x",
            ": y",
            "+.-a:b",
            "\\",
            "//",
            "Approve:",
            "plain words",
            "",
        ]
        separators = [" ", "\n", "\t", "", "<", ">", ")", "](", "/", ":"]
        rng = random.Random(20261008)
        texts = [
            sep.join([a, b, c]) for a in pieces for b in pieces for c in pieces for sep in " \n"
        ]
        texts.extend(
            rng.choice(separators).join(rng.choice(pieces) for _ in range(rng.randint(1, 9)))
            for _ in range(3000)
        )
        texts.extend(sep.join([link, link, link]) for sep in separators)
        for text in texts:
            for stride in (1, 2):
                scan = _TokenParamExemptScan(text)
                earlier: list[tuple[int, int]] = []
                for i, m in enumerate(_TOKEN_PARAM_RE.finditer(text)):
                    if i % stride:
                        continue
                    expected = self._reference_token_param_exempt(text, m, prefixes, earlier)
                    assert _token_param_exempt(text, m, prefixes, scan) == expected, (text, m)
                    if expected is not None:
                        earlier.append(expected)
                        scan.accept(expected)
