"""Tests for voice_reply module — text preparation and provider resolution.

Markdown stripping, SSML, rate and pitch validation and sentence splitting, then
which TTS provider and engine a host resolves (system, Polly), the system
engine's rate mapping and SAPI command, and how the system voices are listed. Each provider's synthesis is covered in
``test_voice_reply_synthesis.py``; the pipeline around it (dispatch, upload,
streaming, stitching) in ``test_voice_reply_pipeline.py``.
"""

from __future__ import annotations

import asyncio
import base64
import os
import re
import threading
import time
from unittest.mock import AsyncMock, patch

import pytest
from voice_reply_helpers import _no_argv_prefixers  # noqa: F401
from voice_reply_helpers import _mock_subprocess

from kiro_crew.voice_reply import (
    _SAY_VOICE_RE,
    DEFAULT_PITCH,
    DEFAULT_PROVIDER,
    DEFAULT_RATE,
    PROVIDER_POLLY,
    PROVIDER_SYSTEM,
    SYSTEM_ENGINE_ESPEAK,
    SYSTEM_ENGINE_SAPI,
    SYSTEM_ENGINE_SAY,
    VALID_ENGINES,
    VALID_PROVIDERS,
    SystemVoiceProbeError,
    _parse_system_voices,
    _sapi_encoded_command,
    _sapi_rate,
    _system_wpm,
    _validate_pitch,
    _validate_rate,
    is_available,
    list_system_voices,
    resolve_configured_provider,
    resolve_system_tts,
    resolve_system_tts_async,
    split_sentences,
    strip_markdown,
    synthesis_settings,
    text_to_ssml,
)


class TestStripMarkdown:
    def test_removes_code_blocks(self) -> None:
        assert strip_markdown("before ```code``` after") == "before (code block) after"

    def test_removes_inline_code(self) -> None:
        assert strip_markdown("use `foo` here") == "use foo here"

    def test_removes_slack_links(self) -> None:
        assert strip_markdown("<https://example.com|Example>") == "Example"
        assert strip_markdown("<https://example.com>") == "(link)"

    def test_removes_markdown_links(self) -> None:
        assert strip_markdown("[click](https://example.com)") == "click"

    def test_a_balanced_pair_in_a_link_url_leaves_only_the_label(self) -> None:
        url = "https://en.wikipedia.org/wiki/Python_(programming_language)"
        assert strip_markdown(f"see [Python]({url}) now") == "see Python now"
        assert strip_markdown("see [docs](https://example.com/a_(b)_c) now") == "see docs now"

    def test_a_web_address_in_an_unbalanced_link_is_never_spoken(self) -> None:
        """An unbalanced ``(`` makes this text, not a link, so it stays as written
        and the bare-URL pass replaces the web address."""
        assert strip_markdown("see [a](https://example.com/(b) now") == "see [a]( (link) now"

    def test_a_relative_destination_in_an_unbalanced_link_is_spoken_as_written(self) -> None:
        """The bare-URL pass only knows web addresses, so nothing shortens this one."""
        text = "see [guide](docs/setup(old) now"
        assert strip_markdown(text) == text

    def test_removes_bold_italic(self) -> None:
        assert strip_markdown("**bold** and *italic*") == "bold and italic"

    def test_removes_emoji_shortcodes(self) -> None:
        assert strip_markdown("hello :wave: world") == "hello world"

    def test_preserves_plain_text(self) -> None:
        assert strip_markdown("hello world") == "hello world"

    def test_removes_control_tag_comments(self) -> None:
        # Trailing control-tag LINES are stripped — the tail-anchored grammar
        # shared with the frontend recognizer. Stacked tags all go.
        assert strip_markdown("report body\n<!-- keep-visible -->") == "report body"
        assert strip_markdown("done\n<!-- deliver:dashboard -->") == "done"
        assert (
            strip_markdown("report\n<!-- keep-visible -->\n<!-- deliver:slack -->") == "report"
        )

    def test_mid_body_and_same_line_tags_are_rendered_content(self) -> None:
        # Only tail LINES are control tags: producers emit "as its final
        # line" (prompt contract; heartbeat/task-planner both append). A tag
        # mid-body or trailing on a prose line is content, never stripped —
        # this is what makes a tag quoted in ANY code dialect untouchable.
        assert "deliver" in strip_markdown("done <!-- deliver:dashboard --> ok")
        assert "keep-visible" in strip_markdown("prose tail <!-- keep-visible -->")

    def test_ordinary_html_comments_are_preserved(self) -> None:
        # Scoped to known control tags, never all comments: an ordinary
        # comment quoted in inline code is visible content the user asked
        # about — a generic strip deleted it from speech.
        assert strip_markdown("use `<!-- ordinary -->` here") == "use <!-- ordinary --> here"

    def test_recognized_tag_quoted_in_inline_code_is_preserved(self) -> None:
        # A RECOGNIZED tag in inline code renders literally — quoted visible
        # content, so speech keeps it (round-5 grammar unification).
        assert strip_markdown("the `<!-- keep-visible -->` tag") == (
            "the <!-- keep-visible --> tag"
        )

    def test_plan_task_id_tag_is_not_spoken(self) -> None:
        # Third control-tag family; task_planner appends "\n<!-- plan_task_id:… -->",
        # so the tag arrives as its own trailing line.
        assert strip_markdown("plan ready\n<!-- plan_task_id:abc123 -->") == "plan ready"

    def test_html_comment_inside_fence_is_already_placeholdered(self) -> None:
        # Fences are replaced before the comment strip runs, so a comment
        # inside code cannot swallow surrounding prose.
        assert strip_markdown("a ```<!-- not a tag -->``` b") == "a (code block) b"

    def test_unterminated_control_tag_no_longer_swallows_text(self) -> None:
        # A truncated control tag leaks literally instead of silently eating
        # the rest of the message (visible garbage beats silent data loss).
        assert strip_markdown("safe part <!-- broken") == "safe part <!-- broken"

    def test_credential_rejoined_by_comment_strip_is_redacted(self) -> None:
        # A control tag interposed inside a key id splits it, so a redaction
        # scan on the RAW text misses it; the strip rejoins the halves. The
        # post-strip redaction pass must catch the reconstructed secret
        # before it reaches TTS.
        out = strip_markdown("key AKIAIOSF<!-- keep-visible -->ODNN7EXAMPLE end")
        assert "AKIAIOSFODNN7EXAMPLE" not in out

    def test_credential_rejoined_by_emphasis_strip_is_redacted(self) -> None:
        # Same class, different strip: `**` emphasis markers inside a key id
        # are removed by the [*_~]+ pass. The invariant covers every strip,
        # not just the control-tag one.
        out = strip_markdown("key AKIAIOSF**ODNN7EXAMPLE** end")
        assert "AKIAIOSFODNN7EXAMPLE" not in out

    def test_contiguous_credential_still_redacted_after_strip(self) -> None:
        # Idempotence: a credential the pre-strip scan would catch is also
        # caught by the post-strip pass when strip_markdown is used alone
        # (split_sentences path has no pre-strip redaction).
        out = strip_markdown("key AKIAIOSFODNN7EXAMPLE end")
        assert "AKIAIOSFODNN7EXAMPLE" not in out

    def test_control_tag_regex_linear_on_adversarial_input(self) -> None:
        # CodeQL py/polynomial-redos, two vectors: (a) "<!--deliver:" + many
        # tabs (adjacent-quantifier ambiguity); (b) the
        # repeated prefix "<!--deliver:" * n, where an UNBOUNDED body lets
        # each of n start positions rescan an O(n) tail = quadratic.
        # Bounding every quantifier keeps a failed attempt constant work.
        # This times the shared helper; strip_markdown's other passes are
        # not under test.
        # Polynomial time at this size hangs for minutes; linear completes
        # in milliseconds. Generous bound for slow CI.
        import time

        from kiro_crew.constants import strip_control_comments

        start = time.monotonic()
        out_tabs = strip_control_comments("<!--deliver:" + "\t" * 50_000)
        out_reps = strip_control_comments("<!--deliver:" * 20_000)
        assert time.monotonic() - start < 5.0
        # Unterminated tags are preserved (no swallow), not stripped.
        assert out_tabs.startswith("<!--deliver:")
        assert out_reps.startswith("<!--deliver:")

    def test_unterminated_tag_preserved_through_full_strip(self) -> None:
        # Same no-swallow contract through the full TTS pipeline.
        assert strip_markdown("safe <!--deliver:oops").startswith("safe <!--deliver:oops")

    def test_oversized_tag_body_is_not_treated_as_control_tag(self) -> None:
        # The body bound (256) is what makes matching linear; a "tag" larger
        # than any real emission is left visible rather than stripped.
        big = "<!-- deliver:" + "x" * 300 + " -->"
        assert strip_markdown(f"before {big} after").strip() != "before after"

    def test_collapses_whitespace(self) -> None:
        assert strip_markdown("a\n\n\n\nb") == "a\n\nb"

    def test_preserves_bullet_lists(self) -> None:
        result = strip_markdown("- item one\n- item two")
        assert "item one" in result
        assert "item two" in result


class TestTextToSsml:
    def test_empty_input(self) -> None:
        assert text_to_ssml("") == ""

    def test_basic_ssml(self) -> None:
        result = text_to_ssml("Hello world")
        assert result.startswith("<speak>")
        assert result.endswith("</speak>")
        assert "Hello world" in result

    def test_includes_rate(self) -> None:
        result = text_to_ssml("test", rate="110%")
        assert 'rate="110%"' in result

    def test_returns_ssml_without_prosody_for_neural(self) -> None:
        result = text_to_ssml("test", pitch="+10%", engine="neural")
        assert result.startswith("<speak>")
        assert "</speak>" in result
        assert "<prosody" not in result
        assert "pitch" not in result

    def test_neural_escapes_xml_entities(self) -> None:
        result = text_to_ssml("a & b < c > d", engine="neural")
        assert "&amp;" in result
        assert "&lt;" in result
        assert "&gt;" in result
        assert "<prosody" not in result

    def test_neural_adds_break_tags(self) -> None:
        result = text_to_ssml("para one\n\npara two", engine="neural")
        assert 'break time="600ms"' in result
        assert "<prosody" not in result

    def test_neural_truncates_long_text(self) -> None:
        long_text = "word. " * 1000
        result = text_to_ssml(long_text, engine="neural")
        assert result.startswith("<speak>")
        assert "</speak>" in result
        assert "<prosody" not in result
        # Should be truncated
        assert len(result) < len(long_text) + 100

    def test_excludes_pitch_for_generative(self) -> None:
        result = text_to_ssml("test", pitch="+10%", engine="generative")
        assert "pitch" not in result

    def test_excludes_pitch_for_long_form(self) -> None:
        result = text_to_ssml("test", pitch="+10%", engine="long-form")
        assert "pitch" not in result

    def test_escapes_xml_entities(self) -> None:
        result = text_to_ssml("a & b < c > d")
        assert "&amp;" in result
        assert "&lt;" in result
        assert "&gt;" in result

    def test_truncates_long_text(self) -> None:
        long_text = "word. " * 1000
        result = text_to_ssml(long_text)
        # Should end with a period (sentence boundary truncation)
        assert result.endswith("</prosody></speak>")

    def test_adds_paragraph_breaks(self) -> None:
        result = text_to_ssml("para one\n\npara two")
        assert 'break time="600ms"' in result

    def test_adds_line_breaks(self) -> None:
        result = text_to_ssml("line one\nline two")
        assert 'break time="300ms"' in result


class TestValidation:
    def test_valid_rate(self) -> None:
        assert _validate_rate("95%") == "95%"
        assert _validate_rate("110%") == "110%"
        assert _validate_rate("50%") == "50%"

    def test_invalid_rate_returns_default(self) -> None:
        assert _validate_rate("banana") == DEFAULT_RATE
        assert _validate_rate("") == DEFAULT_RATE
        assert _validate_rate("1000%") == DEFAULT_RATE

    def test_valid_pitch(self) -> None:
        assert _validate_pitch("+10%") == "+10%"
        assert _validate_pitch("-5%") == "-5%"
        assert _validate_pitch("+0%") == "+0%"

    def test_invalid_pitch_returns_default(self) -> None:
        assert _validate_pitch("banana") == DEFAULT_PITCH
        assert _validate_pitch("10%") == DEFAULT_PITCH  # missing +/-
        assert _validate_pitch("") == DEFAULT_PITCH

    def test_valid_engines(self) -> None:
        assert "neural" in VALID_ENGINES
        assert "generative" in VALID_ENGINES
        assert "long-form" in VALID_ENGINES
        assert "standard" in VALID_ENGINES
        assert "invalid" not in VALID_ENGINES


class TestSplitSentences:
    def test_multi_sentence(self) -> None:
        assert split_sentences("Hello world. How are you?") == [
            "Hello world.",
            "How are you?",
        ]

    def test_single_sentence(self) -> None:
        assert split_sentences("Hello world.") == ["Hello world."]

    def test_empty_input(self) -> None:
        assert split_sentences("") == []

    def test_strips_markdown_before_splitting(self) -> None:
        assert split_sentences("**Bold sentence.** Another one.") == [
            "Bold sentence.",
            "Another one.",
        ]


# ── Provider constants ──────────────────────────────────────────────────


class TestProviderConstants:
    def test_constants_defined(self) -> None:
        assert PROVIDER_POLLY == "polly"
        assert PROVIDER_SYSTEM == "system"
        # The host's built-in engine is the default because it is the only
        # provider that needs nothing installed, so auto-speak works on a fresh
        # machine. The paired assertion is the load-bearing one: whatever the
        # default becomes, it must never be the paid cloud provider.
        assert DEFAULT_PROVIDER == PROVIDER_SYSTEM
        assert DEFAULT_PROVIDER != PROVIDER_POLLY
        assert PROVIDER_POLLY in VALID_PROVIDERS
        assert "piper" not in VALID_PROVIDERS
        assert PROVIDER_SYSTEM in VALID_PROVIDERS


# ── is_available() ──────────────────────────────────────────────────────


class TestIsAvailable:
    def test_polly_available_when_aws_on_path(self) -> None:
        with patch("kiro_crew.voice_reply.shutil.which", return_value="/usr/bin/aws"):
            assert is_available(PROVIDER_POLLY) is True

    def test_polly_unavailable_when_aws_missing(self) -> None:
        with patch("kiro_crew.voice_reply.shutil.which", return_value=None):
            assert is_available(PROVIDER_POLLY) is False

    def test_unknown_provider_returns_false(self, caplog) -> None:
        assert is_available("bogus") is False

    def test_system_available_when_engine_resolves(self) -> None:
        with patch(
            "kiro_crew.voice_reply.resolve_system_tts",
            return_value=(SYSTEM_ENGINE_SAY, "/usr/bin/say"),
        ):
            assert is_available(PROVIDER_SYSTEM) is True

    def test_system_unavailable_without_engine(self) -> None:
        with patch("kiro_crew.voice_reply.resolve_system_tts", return_value=None):
            assert is_available(PROVIDER_SYSTEM) is False


# ── resolve_system_tts() ─────────────────────────────────────────────────


class TestResolveSystemTts:
    def test_macos_uses_say(self) -> None:
        with patch("kiro_crew.voice_reply.IS_MACOS", True), patch(
            "kiro_crew.voice_reply.IS_WINDOWS", False
        ), patch(
            "kiro_crew.voice_reply.trusted_system_bin", return_value="/usr/bin/say"
        ) as probe:
            assert resolve_system_tts() == (SYSTEM_ENGINE_SAY, "/usr/bin/say")
        probe.assert_called_once_with("say")

    def test_windows_uses_powershell_five(self) -> None:
        # Windows PowerShell specifically: System.Speech is .NET-Framework-only
        # and throws in pwsh 7, so resolving anything else would report a
        # provider as available that fails on every call.
        ps = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
        with patch("kiro_crew.voice_reply.IS_MACOS", False), patch(
            "kiro_crew.voice_reply.IS_WINDOWS", True
        ), patch("kiro_crew.voice_reply.trusted_system_bin", return_value=ps) as probe:
            assert resolve_system_tts() == (SYSTEM_ENGINE_SAPI, ps)
        probe.assert_called_once_with("powershell")

    def test_linux_prefers_espeak_ng_then_espeak(self) -> None:
        calls: list[str] = []

        def probe(name: str) -> str | None:
            calls.append(name)
            return "/usr/bin/espeak" if name == "espeak" else None

        with patch("kiro_crew.voice_reply.IS_MACOS", False), patch(
            "kiro_crew.voice_reply.IS_WINDOWS", False
        ), patch("kiro_crew.voice_reply.trusted_system_bin", side_effect=probe):
            assert resolve_system_tts() == (SYSTEM_ENGINE_ESPEAK, "/usr/bin/espeak")
        assert calls == ["espeak-ng", "espeak"]

    def test_linux_without_engine_returns_none(self) -> None:
        # The normal answer on a stock server image, not a broken host.
        with patch("kiro_crew.voice_reply.IS_MACOS", False), patch(
            "kiro_crew.voice_reply.IS_WINDOWS", False
        ), patch("kiro_crew.voice_reply.trusted_system_bin", return_value=None):
            assert resolve_system_tts() is None

    def test_macos_without_say_returns_none(self) -> None:
        with patch("kiro_crew.voice_reply.IS_MACOS", True), patch(
            "kiro_crew.voice_reply.IS_WINDOWS", False
        ), patch("kiro_crew.voice_reply.trusted_system_bin", return_value=None):
            assert resolve_system_tts() is None


# ── system speed mapping ─────────────────────────────────────────────────


class TestResolveSystemTtsAsync:
    @pytest.mark.asyncio
    async def test_resolution_runs_off_the_event_loop(self) -> None:
        """Directory stats are not bounded, and one loop serves every session.

        A fixed directory on a stalled network or fuse mount would freeze the
        gateway and its heartbeats, so resolution is handed to a worker thread.
        """
        loop_thread = threading.get_ident()
        seen: list[int] = []

        def slow_resolve() -> tuple[str, str] | None:
            seen.append(threading.get_ident())
            return (SYSTEM_ENGINE_SAY, "/usr/bin/say")

        with patch("kiro_crew.voice_reply.resolve_system_tts", side_effect=slow_resolve):
            assert await resolve_system_tts_async() == (SYSTEM_ENGINE_SAY, "/usr/bin/say")
        assert seen and seen[0] != loop_thread


# ── system speed mapping ─────────────────────────────────────────────────


class TestSystemRateMapping:
    def test_wpm_scales_from_the_engine_default(self) -> None:
        assert _system_wpm("100%") == 175
        assert _system_wpm("200%") == 350
        assert _system_wpm("50%") == 88

    def test_wpm_is_clamped_to_a_speakable_band(self) -> None:
        # A rate the UI cannot produce but config.json can.
        assert _system_wpm("999%") == 500
        assert _system_wpm("1%") == 80

    def test_wpm_falls_back_on_an_invalid_rate(self) -> None:
        assert _system_wpm("fast") == 175

    def test_a_non_string_rate_does_not_crash(self) -> None:
        # config.json is JSON, so a hand-edited `"rate": 100` arrives as an int
        # and can reach `_RATE_RE.match` as a non-string. The built-in engine
        # is the default, so that typo would drop the audio of every reply.
        assert _system_wpm(100) == 175  # type: ignore[arg-type]
        assert _sapi_rate(100) == 0  # type: ignore[arg-type]
        assert _system_wpm(None) == 175  # type: ignore[arg-type]
        assert _sapi_rate(None) == 0  # type: ignore[arg-type]

    def test_the_validators_themselves_absorb_a_non_string(self) -> None:
        """Pinned on the validators, not their callers.

        The coercion lives inside ``_validate_rate`` / ``_validate_pitch`` so
        every reader is covered at once. Guarding only the built-in engine's two
        call sites left ``text_to_ssml`` — reached from both Polly paths — with
        the same TypeError, and left pitch unguarded entirely.
        """
        assert _validate_rate(100) == "100%"  # type: ignore[arg-type]
        assert _validate_rate(None) == "100%"  # type: ignore[arg-type]
        assert _validate_rate([]) == "100%"  # type: ignore[arg-type]
        assert _validate_pitch(0) == "+0%"  # type: ignore[arg-type]
        assert _validate_pitch(None) == "+0%"  # type: ignore[arg-type]
        assert _validate_pitch({}) == "+0%"  # type: ignore[arg-type]
        # The SSML builder is the third caller; a non-string must not reach it
        # as one. A pitch that lands on the default is omitted from the prosody
        # by design, so only the rate is asserted here.
        ssml = text_to_ssml("hi", rate=100, pitch=0)  # type: ignore[arg-type]
        assert 'rate="100%"' in ssml and "pitch=" not in ssml
        assert 'rate="100%"' in text_to_ssml("hi", rate=100, pitch=-25)  # type: ignore[arg-type]

    def test_sapi_rate_centres_on_zero(self) -> None:
        assert _sapi_rate("100%") == 0
        assert _sapi_rate("150%") == 5
        assert _sapi_rate("80%") == -2

    def test_sapi_rate_is_clamped_to_the_api_range(self) -> None:
        assert _sapi_rate("999%") == 10
        assert _sapi_rate("1%") == -10


# ── _sapi_encoded_command() ──────────────────────────────────────────────


class TestSapiEncodedCommand:
    def _decode(self, payload: str) -> str:
        return base64.b64decode(payload).decode("utf-16-le")

    def test_payload_is_utf16le_base64(self) -> None:
        script = self._decode(_sapi_encoded_command("out.wav", "in.txt", "", 0))
        assert "System.Speech" in script
        assert "SetOutputToWaveFile" in script

    def test_spoken_text_is_never_in_the_script(self) -> None:
        # The whole reason the text goes to a file: nothing a model produced may
        # reach a command line, where quoting decides how it is parsed.
        script = self._decode(_sapi_encoded_command("out.wav", "in.txt", "", 0))
        assert "ReadAllText('in.txt'" in script
        assert "Speak($t)" in script

    def test_quotes_in_paths_are_escaped(self) -> None:
        script = self._decode(_sapi_encoded_command("o'ut.wav", "in.txt", "", 0))
        assert "'o''ut.wav'" in script

    def test_the_voice_name_is_read_from_a_file_never_the_script(self) -> None:
        """The configured voice must not reach argv, only its temp path.

        This is what makes the Windows argv derived entirely inside the package,
        which is the claim ``first_party_fixed_argv`` rests on at the sandbox
        chokepoint. A name interpolated into the script would be user config
        travelling on the command line.
        """
        without = self._decode(_sapi_encoded_command("o.wav", "i.txt", "", 0))
        assert "SelectVoice" not in without

        script = self._decode(_sapi_encoded_command("o.wav", "i.txt", "/tmp/v.txt", 0))
        assert "ReadAllText('/tmp/v.txt'" in script
        assert "SelectVoice($v)" in script
        # The name itself appears nowhere — only the path it is read from.
        assert "Zira" not in script

    def test_rate_is_interpolated_as_an_integer(self) -> None:
        assert "$s.Rate=-3;" in self._decode(
            _sapi_encoded_command("o.wav", "i.txt", "", -3)
        )


# ── _parse_system_voices() ───────────────────────────────────────────────


class TestParseSystemVoices:
    def test_say_listing(self) -> None:
        out = (
            "Alex                en_US    # Most people recognize me by my voice.\n"
            "Grandma (Deutsch)   de_DE    # Ich bin die Oma.\n"
            "Tingting            zh_CN    # 你好。\n"
        )
        assert _parse_system_voices(SYSTEM_ENGINE_SAY, out) == [
            {"id": "Grandma (Deutsch)", "name": "Grandma (Deutsch)", "language": "de-DE"},
            {"id": "Alex", "name": "Alex", "language": "en-US"},
            {"id": "Tingting", "name": "Tingting", "language": "zh-CN"},
        ]

    def test_say_name_containing_spaces_is_not_split(self) -> None:
        # Two-space padding is the only field separator, so a name with an
        # internal space would otherwise be truncated to its first word.
        rows = _parse_system_voices(
            SYSTEM_ENGINE_SAY, "Eddy (English (UK))  en_GB    # Hello.\n"
        )
        assert rows[0]["name"] == "Eddy (English (UK))"

    def test_espeak_listing_skips_header(self) -> None:
        out = (
            "Pty Language       Age/Gender VoiceName          File\n"
            " 5  af              --/M      Afrikaans          gmw/af\n"
            " 5  en-gb           --/M      English_(GB)       gmw/en\n"
        )
        assert _parse_system_voices(SYSTEM_ENGINE_ESPEAK, out) == [
            {"id": "af", "name": "Afrikaans", "language": "af"},
            {"id": "en-gb", "name": "English_(GB)", "language": "en-gb"},
        ]

    def test_sapi_listing(self) -> None:
        out = "Microsoft Zira Desktop|en-US\nMicrosoft Hanhan Desktop|zh-CN\n"
        assert _parse_system_voices(SYSTEM_ENGINE_SAPI, out) == [
            {
                "id": "Microsoft Zira Desktop",
                "name": "Microsoft Zira Desktop",
                "language": "en-US",
            },
            {
                "id": "Microsoft Hanhan Desktop",
                "name": "Microsoft Hanhan Desktop",
                "language": "zh-CN",
            },
        ]

    def test_duplicates_are_collapsed(self) -> None:
        out = "Alex                en_US    # a\nAlex                en_US    # a\n"
        assert len(_parse_system_voices(SYSTEM_ENGINE_SAY, out)) == 1

    def test_blank_and_unparseable_lines_are_skipped(self) -> None:
        assert _parse_system_voices(SYSTEM_ENGINE_SAY, "\n   \ngarbage\n") == []

    def test_the_locale_group_cannot_backtrack_exponentially(self) -> None:
        """The locale tail must exclude the separators that delimit it.

        With ``\\w`` the tail could also match ``-``/``_``, so a run like
        ``_0_0_0`` has many valid partitions and a non-matching line makes the
        engine try them all — measured at 34ms for 20 repetitions and growing
        ~3.7x per further two, i.e. hours by 40. A voice listing is engine
        output, but a third-party installed voice supplies its own name.

        Asserted as SHAPE first, because that is deterministic: no separator may
        appear in the repeated group's character class. The timing check is a
        generously-bounded backstop for a future rewrite that reintroduces the
        ambiguity some other way.
        """
        # The repeated group is `(?:<sep class><tail class>+)`. Ambiguity exists
        # exactly when the tail class can also match a separator, so compile it
        # and ask, instead of pattern-matching the source text.
        pattern = _SAY_VOICE_RE.pattern
        group = pattern[pattern.index("(?:") : pattern.index(")*")]
        classes = re.findall(r"\[[^\]]*\]", group)
        assert len(classes) == 2, f"unexpected locale group shape: {group}"
        tail = re.compile(classes[1])
        for sep in ("-", "_"):
            assert not tail.match(sep), f"locale tail also matches {sep!r}: {classes[1]}"

        adversarial = "Voice  en" + "_0" * 5_000 + "!\n"
        start = time.perf_counter()
        assert _parse_system_voices(SYSTEM_ENGINE_SAY, adversarial) == []
        # Linear parsing lands near a millisecond; the ambiguous form would not
        # finish. The ceiling is loose on purpose — this is not a benchmark.
        assert time.perf_counter() - start < 2.0


# ── list_system_voices() ─────────────────────────────────────────────────


class TestListSystemVoices:
    """A failed probe must be distinguishable from a host with no engine.

    If both answer with an empty list, the endpoint reports
    ``available: true`` and the panel renders a picker holding only the OS
    default — which reads as "this host has one voice", not as a failure the
    user can retry.
    """

    @pytest.mark.asyncio
    async def test_no_engine_is_an_empty_list_not_an_error(self) -> None:
        with patch("kiro_crew.voice_reply.resolve_system_tts", return_value=None):
            assert await list_system_voices() == []

    @pytest.mark.asyncio
    async def test_a_spawn_failure_raises(self) -> None:
        with patch(
            "kiro_crew.voice_reply.resolve_system_tts",
            return_value=(SYSTEM_ENGINE_SAY, "/usr/bin/say"),
        ), patch("asyncio.create_subprocess_exec", side_effect=OSError("no exec")):
            with pytest.raises(SystemVoiceProbeError):
                await list_system_voices()

    @pytest.mark.asyncio
    async def test_a_nonzero_exit_raises(self) -> None:
        async def fake_exec(*_cmd, **_kw):
            return _mock_subprocess(returncode=3, stderr=b"bad flag")

        with patch(
            "kiro_crew.voice_reply.resolve_system_tts",
            return_value=(SYSTEM_ENGINE_SAY, "/usr/bin/say"),
        ), patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            with pytest.raises(SystemVoiceProbeError):
                await list_system_voices()

    @pytest.mark.asyncio
    async def test_a_timeout_raises_and_reaps(self) -> None:
        proc = _mock_subprocess(returncode=0)
        proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError)

        async def fake_exec(*_cmd, **_kw):
            return proc

        with patch(
            "kiro_crew.voice_reply.resolve_system_tts",
            return_value=(SYSTEM_ENGINE_SAY, "/usr/bin/say"),
        ), patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            with pytest.raises(SystemVoiceProbeError):
                await list_system_voices()
        # A probe left running would hold the pipe open for the process's life.
        proc.kill.assert_called()

    @pytest.mark.asyncio
    async def test_cancellation_still_propagates_as_cancellation(self) -> None:
        proc = _mock_subprocess(returncode=0)
        proc.communicate = AsyncMock(side_effect=asyncio.CancelledError)

        async def fake_exec(*_cmd, **_kw):
            return proc

        with patch(
            "kiro_crew.voice_reply.resolve_system_tts",
            return_value=(SYSTEM_ENGINE_SAY, "/usr/bin/say"),
        ), patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            with pytest.raises(asyncio.CancelledError):
                await list_system_voices()


# ── resolve_polly_cli() ─────────────────────────────────────────


class TestResolvePollyCli:
    @pytest.mark.skipif(
        os.name == "nt",
        reason="fallback install dirs are POSIX literals; dead on Windows by design",
    )
    def test_resolved_absolutely_under_minimal_path(self, monkeypatch, tmp_path) -> None:
        """A GUI-launched gateway's minimal PATH must still resolve the CLI
        absolutely via the deploy engine's well-known-dirs resolver instead of
        silently skipping TTS."""
        from kiro_crew import github_runner, voice_reply
        from kiro_crew.deploy import engine

        fake_aws = tmp_path / "aws"
        fake_aws.write_text("#!/bin/sh\n")
        fake_aws.chmod(0o755)
        empty_bin = tmp_path / "emptybin"
        empty_bin.mkdir()
        monkeypatch.setenv("PATH", str(empty_bin))
        monkeypatch.setattr(engine, "_AWS_BIN_DIRS", (str(tmp_path),))
        monkeypatch.setattr(github_runner, "validate_provider_executable", lambda c: c)

        assert voice_reply.resolve_polly_cli() == str(fake_aws)
        # The converted is_available() probe site sees the same resolution.
        assert is_available(PROVIDER_POLLY) is True

    def test_none_when_cli_absent_everywhere(self, monkeypatch, tmp_path) -> None:
        """Bare-name fallback that is not invocable maps to None — the value
        every probe site already treats as 'unavailable'."""
        from kiro_crew import voice_reply
        from kiro_crew.deploy import engine

        empty_bin = tmp_path / "emptybin"
        empty_bin.mkdir()
        monkeypatch.setenv("PATH", str(empty_bin))
        monkeypatch.setattr(engine, "_AWS_BIN_DIRS", ())

        assert voice_reply.resolve_polly_cli() is None
        assert is_available(PROVIDER_POLLY) is False


# ── resolve_configured_provider() ────────────────────────────────────────


@pytest.fixture
def fresh_removed_warning(monkeypatch):
    """The removed-provider notice fires once per process; give each test its own."""
    monkeypatch.setattr("kiro_crew.voice_reply._warned_removed_providers", set())


class TestResolveConfiguredProvider:
    def test_named_provider_is_kept(self) -> None:
        for name in (PROVIDER_SYSTEM, PROVIDER_POLLY):
            assert resolve_configured_provider({"provider": name}) == name

    def test_absent_section_resolves_to_the_default(self) -> None:
        assert resolve_configured_provider(None) == DEFAULT_PROVIDER
        assert resolve_configured_provider({}) == DEFAULT_PROVIDER

    def test_invalid_provider_warns_and_never_reaches_polly(self, caplog) -> None:
        import logging

        with caplog.at_level(logging.WARNING, logger="kiro_crew.voice_reply"):
            assert resolve_configured_provider({"provider": "ploly"}) == DEFAULT_PROVIDER
        assert DEFAULT_PROVIDER != PROVIDER_POLLY
        assert any("ploly" in rec.message for rec in caplog.records)

    def test_unhashable_provider_does_not_raise(self) -> None:
        # config.json can hold a list or dict where a string belongs; `in
        # VALID_PROVIDERS` would raise TypeError on those.
        assert resolve_configured_provider({"provider": ["polly"]}) == DEFAULT_PROVIDER
        assert resolve_configured_provider({"provider": {"a": 1}}) == DEFAULT_PROVIDER


class TestRemovedPiperProvider:
    """A saved Piper config keeps speaking, through the local default, and says why."""

    def test_a_named_piper_provider_resolves_to_the_local_default(
        self, caplog, fresh_removed_warning
    ) -> None:
        import logging

        with caplog.at_level(logging.WARNING, logger="kiro_crew.voice_reply"):
            got = resolve_configured_provider({"provider": "piper", "piper_model": "~/v.onnx"})
        assert got == DEFAULT_PROVIDER == PROVIDER_SYSTEM
        notices = [r.message for r in caplog.records if "was removed" in r.message]
        assert len(notices) == 1
        assert "'piper'" in notices[0] and "23154" in notices[0]
        # The remedy must be one the operator can act on: Settings shows the
        # resolved `system` already selected, so re-picking it saves nothing.
        assert "config.json" in notices[0]

    def test_an_unnamed_provider_with_a_piper_model_resolves_to_the_default(
        self, caplog, fresh_removed_warning
    ) -> None:
        # The pre-system-default upgrade shape: no provider, only a model path.
        import logging

        with caplog.at_level(logging.WARNING, logger="kiro_crew.voice_reply"):
            assert resolve_configured_provider({"piper_model": "~/v.onnx"}) == DEFAULT_PROVIDER
            assert resolve_configured_provider({"provider": "", "piper_model": "~/v.onnx"}) == (
                DEFAULT_PROVIDER
            )
        assert sum("was removed" in r.message for r in caplog.records) == 1

    @pytest.mark.parametrize("malformed", [{}, [], 0, 3, None, "", "   "])
    def test_a_malformed_piper_model_resolves_quietly(
        self, malformed, caplog, fresh_removed_warning
    ) -> None:
        import logging

        with caplog.at_level(logging.WARNING, logger="kiro_crew.voice_reply"):
            assert resolve_configured_provider({"piper_model": malformed}) == DEFAULT_PROVIDER
        assert not any("was removed" in r.message for r in caplog.records)

    def test_the_notice_is_logged_once_per_process(self, caplog, fresh_removed_warning) -> None:
        import logging

        with caplog.at_level(logging.WARNING, logger="kiro_crew.voice_reply"):
            for _ in range(3):
                resolve_configured_provider({"provider": "piper"})
        assert sum("was removed" in r.message for r in caplog.records) == 1

    def test_an_explicit_provider_outranks_a_leftover_model(
        self, caplog, fresh_removed_warning
    ) -> None:
        import logging

        with caplog.at_level(logging.WARNING, logger="kiro_crew.voice_reply"):
            got = resolve_configured_provider(
                {"provider": PROVIDER_POLLY, "piper_model": "~/voices/en.onnx"}
            )
        assert got == PROVIDER_POLLY
        assert not any("was removed" in r.message for r in caplog.records)

    def test_leftover_piper_keys_are_not_synthesis_settings(self, fresh_removed_warning) -> None:
        settings = synthesis_settings(
            {
                "provider": "piper",
                "piper_binary": "/opt/piper",
                "piper_model": "~/v.onnx",
                "piper_length_scale": 0.8,
            }
        )
        assert settings["provider"] == PROVIDER_SYSTEM
        assert not any("piper" in key or "length_scale" in key for key in settings)
