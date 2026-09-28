"""The AI cleanup pass on a finished transcript, and the guards that keep it safe.

Three properties carry the feature, and each has tests here:

* The switch IS the consent. ``stt.polish`` is off by default and the endpoint
  refuses while it is off, because the local provider's promise is that nothing
  leaves the machine and this is what sends the transcript off it.
* It can never lose or invent what the user said. A model reply that fails the
  length guard is discarded and the original is returned, so the worst outcome is
  the recogniser's own text rather than a rewrite.
* It never blocks dictation, which is why it is an endpoint rather than a frame on
  the speech websocket -- that socket closes shortly after the final, so a
  correction routed through it would be cancelled in the commonest case of all.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from kiro_crew.dashboard.handlers import core


@pytest.fixture
def polish(monkeypatch: pytest.MonkeyPatch):
    """The handler with its token gate, config and background model stubbed."""
    monkeypatch.setattr(core, "_deny_app_token", lambda request, scope: None)
    cfg = MagicMock()
    cfg.stt.polish = True
    monkeypatch.setattr(core.KiroCrewConfig, "load", staticmethod(lambda: cfg))
    sent: list[str] = []

    def set_reply(reply: str | Exception) -> None:
        async def _oneliner(sessions, prompt, **kwargs):
            sent.append(prompt)
            if isinstance(reply, Exception):
                raise reply
            return reply

        import kiro_crew.llm_helpers as helpers

        monkeypatch.setattr(helpers, "run_bg_oneliner", _oneliner)

    return cfg, sent, set_reply


def _request(text: object) -> MagicMock:
    request = MagicMock()

    async def _json():
        return {"text": text}

    request.json = _json
    request.app.get.return_value = MagicMock(sessions=MagicMock())
    return request


async def _call(request) -> dict:
    response = await core.api_stt_polish(request)
    return {"status": response.status, **json.loads(response.body)}


class TestTheSwitchIsTheConsent:
    @pytest.mark.asyncio
    async def test_it_refuses_while_the_setting_is_off(self, polish):
        """Off is the default, and off must mean the transcript goes nowhere.

        Honouring the request anyway would make the switch a decoration: a
        local-only install would be sending text to a model while its settings said
        otherwise.
        """
        cfg, sent, set_reply = polish
        cfg.stt.polish = False
        set_reply("corrected")
        result = await _call(_request("hello world"))
        assert result["status"] == 403
        assert result["code"] == "stt_polish_disabled"
        # The load-bearing assertion: no model call happened at all.
        assert sent == []

    @pytest.mark.asyncio
    async def test_the_default_configuration_has_it_off(self):
        """Read from the real dataclass, not the fixture's stub."""
        from kiro_crew.config.loader import _build_stt_config

        assert _build_stt_config({}).polish is False


class TestItCannotLoseOrInventContent:
    @pytest.mark.asyncio
    async def test_a_reply_that_dropped_content_is_discarded(self, polish):
        """Half the text back means the model summarised rather than corrected."""
        cfg, sent, set_reply = polish
        original = "deploy the service to production and then run the smoke tests"
        set_reply("deploy the service")
        result = await _call(_request(original))
        assert result["changed"] is False
        assert result["text"] == original

    @pytest.mark.asyncio
    async def test_a_reply_that_invented_content_is_discarded(self, polish):
        """Double the text back means the model answered the dictation."""
        cfg, sent, set_reply = polish
        original = "deploy the service"
        set_reply(
            "Deploy the service. Here is how you would do that: first, build the "
            "container image, then push it to the registry, then update the task "
            "definition and wait for the rollout to finish."
        )
        result = await _call(_request(original))
        assert result["changed"] is False
        assert result["text"] == original

    @pytest.mark.asyncio
    async def test_a_genuine_correction_is_returned_as_changed(self, polish):
        cfg, sent, set_reply = polish
        set_reply("Deploy the service, then run the smoke tests.")
        result = await _call(_request("deploy the service then run the smoke tests"))
        assert result["changed"] is True
        assert result["text"] == "Deploy the service, then run the smoke tests."

    @pytest.mark.asyncio
    async def test_a_same_length_word_substitution_is_discarded(self, polish):
        """The defect a length band cannot see, and the reason for the word check.

        "staging" and "production" make the reply a plausible length, so every
        ratio-based guard admits it -- and the one thing this endpoint must never do is
        change where a user just said to deploy. Dictation is also the one place they
        are not watching the text appear, so nothing downstream catches it either.
        """
        cfg, sent, set_reply = polish
        set_reply("Deploy to production.")
        result = await _call(_request("deploy to staging"))
        assert result["changed"] is False
        assert result["text"] == "deploy to staging"

    @pytest.mark.asyncio
    async def test_joining_two_words_is_discarded(self, polish):
        """The hole a letters-only comparison leaves, and it is not academic.

        "a part" and "apart" have identical letters and opposite meanings, so a guard
        that ignores where words divide accepts the swap. It is also why the prompt asks
        for no boundary fixes at all: a helpful re-split and a meaning change are the
        same edit seen from outside.
        """
        cfg, sent, set_reply = polish
        set_reply("Apart from the cluster.")
        result = await _call(_request("a part from the cluster"))
        assert result["changed"] is False
        assert result["text"] == "a part from the cluster"

    @pytest.mark.asyncio
    async def test_splitting_a_word_is_discarded_too(self, polish):
        """The same rule in the other direction: a division may not be invented
        inside a word either, because "nowhere" and "now here" differ the same way."""
        cfg, sent, set_reply = polish
        set_reply("Now here.")
        result = await _call(_request("nowhere"))
        assert result["changed"] is False

    @pytest.mark.asyncio
    async def test_a_mark_does_not_license_splitting_a_word(self, polish):
        """A comma in front of the split does not make the split punctuation.

        ``"nowhere"`` returned as ``"now, here"`` has identical letters and the
        opposite meaning, and a mark sits exactly where the run was cut -- so a rule
        that accepts any mark-justified division accepts this. The separator has to
        hold NOTHING but the mark: a script that spaces its words already divides
        them, so correct punctuation there lands on a division that exists.
        """
        cfg, sent, set_reply = polish
        set_reply("Now, here.")
        result = await _call(_request("nowhere"))
        assert result["changed"] is False
        assert result["text"] == "nowhere"

    @pytest.mark.asyncio
    async def test_a_sentence_break_at_an_existing_division_passes(self, polish):
        """And the same rule must not cost the feature its main English job.

        Restoring a full stop puts a mark on a division the transcript ALREADY has,
        which is why the check above can refuse mark-with-space divisions outright:
        English punctuation has no legitimate reason to invent one.
        """
        cfg, sent, set_reply = polish
        set_reply("The build failed. We should retry.")
        result = await _call(_request("the build failed we should retry"))
        assert result["changed"] is True
        assert result["text"] == "The build failed. We should retry."

    @pytest.mark.asyncio
    async def test_an_apostrophe_inside_a_word_is_discarded(self, polish):
        """The one capability this guard cannot safely offer, and why.

        A contraction apostrophe divides a run of Latin letters that had no division,
        and that division is a word being replaced: ``"shell continue"`` as
        ``"She'll continue."`` keeps every letter and changes who is continuing. So do
        ``"well"`` to ``"we'll"``, ``"wont"`` to ``"won't"`` and ``"were"`` to
        ``"we're"``. Restoring a contraction and rewriting a word are the same edit
        seen from outside -- the same reason this pass does not attempt mis-hearings --
        so a script that spaces its words gets no mark-created divisions at all, and
        the cost is that ``"dont"`` stays ``"dont"``.
        """
        cfg, sent, set_reply = polish
        set_reply("She'll continue.")
        result = await _call(_request("shell continue"))
        assert result["changed"] is False
        assert result["text"] == "shell continue"

    @pytest.mark.asyncio
    async def test_an_apostrophe_the_speaker_dictated_survives(self, polish):
        """Refusing to CREATE one is not refusing to keep one.

        The recogniser does produce contractions; the rule is only that the pass may
        not introduce a division, so an apostrophe already in the transcript is
        punctuation like any other and the rest of the correction lands.
        """
        cfg, sent, set_reply = polish
        set_reply("Don't stop the deploy.")
        result = await _call(_request("don't stop the deploy"))
        assert result["changed"] is True
        assert result["text"] == "Don't stop the deploy."

    @pytest.mark.asyncio
    async def test_an_accent_is_not_a_script_boundary(self, polish):
        """``isascii()`` is not a script test, and the difference corrupts a word.

        ``é`` is not ASCII and ``i`` is, so ``"caféine"`` returned as ``"café ine"``
        reads as a boundary between two scripts under that proxy when it is one Latin
        word cut in half -- and an accent next to a plain letter is ordinary input in
        every accented language, not an extreme case.
        """
        cfg, sent, set_reply = polish
        set_reply("Café ine.")
        result = await _call(_request("caféine"))
        assert result["changed"] is False
        assert result["text"] == "caféine"

    @pytest.mark.asyncio
    async def test_kanji_beside_kana_is_not_a_space_boundary(self, polish):
        """The allowance is about SPACING, not about the scripts differing.

        Japanese crosses kanji and kana inside a single word, so ``食べる`` returned as
        ``食 べる`` differs by script on either side of the gap and still has no place
        for a space. Asking whether each side's script spaces its words at all answers
        this and ``"caféine"`` with one test.
        """
        cfg, sent, set_reply = polish
        set_reply("食 べる")
        result = await _call(_request("食べる"))
        assert result["changed"] is False
        assert result["text"] == "食べる"

    @pytest.mark.asyncio
    async def test_an_added_emoji_is_discarded(self, polish):
        """A symbol is neither a word nor punctuation, and the guard must not guess.

        A check that normalises both sides and compares what is left has to decide what
        to drop, and anything it drops is something the model may change unobserved:
        ``"hello"`` as ``"hello 🚀"`` has the same letters and the same divisions, so
        only a comparison that keeps the rocket sees it at all.
        """
        cfg, sent, set_reply = polish
        set_reply("Hello 🚀")
        result = await _call(_request("hello"))
        assert result["changed"] is False
        assert result["text"] == "hello"

    @pytest.mark.asyncio
    async def test_a_symbol_the_speaker_dictated_still_passes(self, polish):
        """And a symbol already in the transcript must not block a punctuation fix.

        The rule is that symbols are IDENTICAL, not that they are absent: dictation
        that produced ``"1 + 2 equals 3"`` keeps its plus sign through the pass.
        """
        cfg, sent, set_reply = polish
        set_reply("1 + 2 equals 3.")
        result = await _call(_request("1 + 2 equals 3"))
        assert result["changed"] is True
        assert result["text"] == "1 + 2 equals 3."

    @pytest.mark.asyncio
    async def test_a_removed_symbol_is_discarded_too(self, polish):
        """Dropping one is the same defect in the other direction."""
        cfg, sent, set_reply = polish
        set_reply("1 2 equals 3.")
        result = await _call(_request("1 + 2 equals 3"))
        assert result["changed"] is False
        assert result["text"] == "1 + 2 equals 3"

    @pytest.mark.asyncio
    async def test_a_relocated_symbol_is_discarded(self, polish):
        """Moving one is the defect a presence check cannot see.

        ``"$100 fee"`` returned as ``"100$ fee"`` holds the same letters and the same
        currency sign, and divides in the same places -- every check built on
        normalising both sides and comparing what is left says yes. Aligning the two
        strings instead reports the sign deleted in one place and inserted in another,
        and neither span is punctuation.
        """
        cfg, sent, set_reply = polish
        set_reply("100$ fee.")
        result = await _call(_request("$100 fee"))
        assert result["changed"] is False
        assert result["text"] == "$100 fee"

    @pytest.mark.asyncio
    async def test_dropping_a_combining_mark_is_discarded(self, polish):
        """A vowel sign is part of the word, and Python does not think so.

        ``"ि".isalnum()`` is False -- combining marks are category Mn/Mc, not letters
        -- so a filter built on `isalnum` alone reduces Devanagari ``कि`` and ``क.``
        to the same ``क``, and deleting the mark becomes invisible to the one check
        that exists to catch it.
        """
        cfg, sent, set_reply = polish
        set_reply("क.")
        result = await _call(_request("कि"))
        assert result["changed"] is False
        assert result["text"] == "कि"

    @pytest.mark.asyncio
    async def test_hindi_punctuation_alone_still_passes(self, polish):
        """And the marks must not make a legitimate punctuation fix impossible."""
        cfg, sent, set_reply = polish
        set_reply("कितना समय लगेगा?")
        result = await _call(_request("कितना समय लगेगा"))
        assert result["changed"] is True
        assert result["text"] == "कितना समय लगेगा?"

    @pytest.mark.asyncio
    async def test_a_space_at_a_script_transition_is_allowed(self, polish):
        """The one bare space the guard must let through, and why it is safe.

        Putting a space between Chinese and Latin text is a typographic fix a
        punctuation pass is expected to make, so refusing it would reject the most
        common real correction on the worst-measured bucket (code-switched zh/en, CER
        0.436). It also cannot be the re-segmentation the rule exists to catch, which
        needs the same script on both sides.
        """
        cfg, sent, set_reply = polish
        set_reply("我们用 Kubernetes。")
        result = await _call(_request("我们用Kubernetes"))
        assert result["changed"] is True
        assert result["text"] == "我们用 Kubernetes。"

    @pytest.mark.asyncio
    async def test_punctuation_and_capitalisation_still_pass(self, polish):
        """The guard has to be blind to every change the feature is FOR.

        Marks added and a word capitalised, with every division left where it was.
        """
        cfg, sent, set_reply = polish
        set_reply("Deploy the service, then run the smoke tests.")
        result = await _call(_request("deploy the service then run the smoke tests"))
        assert result["changed"] is True
        assert result["text"] == "Deploy the service, then run the smoke tests."

    @pytest.mark.asyncio
    async def test_a_chinese_substitution_is_caught_without_spaces(self, polish):
        """Chinese has no word spaces, so a token-based check would see one token.

        The comparison is per CHARACTER for exactly this: here the model swapped
        "测试" for "生产" and left the length identical.
        """
        cfg, sent, set_reply = polish
        set_reply("部署到生产环境。")
        result = await _call(_request("部署到测试环境"))
        assert result["changed"] is False
        assert result["text"] == "部署到测试环境"

    @pytest.mark.asyncio
    async def test_chinese_punctuation_alone_still_passes(self, polish):
        """And the same text with only marks added must still be accepted."""
        cfg, sent, set_reply = polish
        set_reply("部署到测试环境。")
        result = await _call(_request("部署到测试环境"))
        assert result["changed"] is True
        assert result["text"] == "部署到测试环境。"

    @pytest.mark.asyncio
    async def test_a_reply_that_redaction_alters_is_discarded(self, polish):
        """Redaction runs AFTER the word check, so its output was never checked.

        The model may only re-punctuate, and re-punctuating a host the user dictated
        can turn it into something the exfiltration scrubber recognises. The scrubber
        then replaces that span with a marker -- words nobody said, arriving in the
        composer after the guard that exists to prevent exactly that had already run
        and passed. Ordering cannot fix it (redacting first would validate a string the
        user never dictated either), so the reply is declined.
        """
        cfg, sent, set_reply = polish
        # Constructed to REACH the branch rather than to look alarming: the words are
        # identical to the URL's letters, so `_polish_preserves_words` passes, every
        # original division survives as a mark (`://`, `.`, `/`, `?`, `=`), and the
        # ratio is 1.03. The scrubber then rewrites the whole span to a marker.
        raw = "send it to https evil test steal data AKIAIOSFODNN7EXAMPLE"
        set_reply("Send it to https://evil.test/steal?data=AKIAIOSFODNN7EXAMPLE")
        result = await _call(_request(raw))
        assert result["changed"] is False
        assert result["text"] == raw
        assert "REDACTED" not in result["text"]

    @pytest.mark.asyncio
    async def test_a_model_failure_keeps_the_original(self, polish):
        """The recogniser's own text is a correct outcome, not a degraded one."""
        cfg, sent, set_reply = polish
        set_reply(RuntimeError("model unavailable"))
        result = await _call(_request("deploy the service"))
        assert result["ok"] is True
        assert result["changed"] is False
        assert result["text"] == "deploy the service"

    @pytest.mark.asyncio
    async def test_an_unchanged_reply_reports_no_change(self, polish):
        cfg, sent, set_reply = polish
        set_reply("deploy the service")
        result = await _call(_request("deploy the service"))
        assert result["changed"] is False


class TestMixedLanguageDictationSurvives:
    @pytest.mark.asyncio
    async def test_a_zh_en_sentence_is_not_translated_away(self, polish):
        """The measured worst bucket is code-switched zh/en (CER 0.436).

        It is also the case a polish model is most likely to "fix" by translating one
        half, so the prompt forbids it and this pins that the corrected text is
        accepted with both languages intact.
        """
        cfg, sent, set_reply = polish
        original = "深圳啊或者是上海这种比较大的城市会有更多opportunity"
        set_reply("深圳啊，或者是上海这种比较大的城市会有更多 opportunity。")
        result = await _call(_request(original))
        assert result["changed"] is True
        assert "opportunity" in result["text"]
        assert "深圳" in result["text"]

    @pytest.mark.asyncio
    async def test_the_prompt_forbids_translating_and_answering(self, polish):
        """The prompt is the only thing standing between dictation and a chat reply.

        Asserted on the text actually sent, because a later edit that softens these
        rules would otherwise be invisible until a user's dictation came back
        answered instead of corrected.
        """
        cfg, sent, set_reply = polish
        set_reply("ok")
        await _call(_request("hello"))
        prompt = sent[0].lower()
        # The word rule, in the prompt as well as in the guard. Belt and braces on
        # purpose: the guard is what makes it safe, but a prompt that asked for word
        # changes would make every reply fail the guard and the feature useless.
        assert "never change, add, remove or reorder a word" in prompt
        assert "never join or split one" in prompt
        assert "only punctuation and capitalisation" in prompt
        assert "do not translate" in prompt
        assert "not a request addressed to you" in prompt


class TestInputLimits:
    @pytest.mark.asyncio
    async def test_an_overlong_transcript_is_refused_without_a_model_call(self, polish):
        cfg, sent, set_reply = polish
        set_reply("x")
        result = await _call(_request("a" * (core._POLISH_MAX_CHARS + 1)))
        assert result["status"] == 413
        assert sent == []

    @pytest.mark.asyncio
    async def test_empty_text_is_a_bad_request(self, polish):
        cfg, sent, set_reply = polish
        set_reply("x")
        result = await _call(_request("   "))
        assert result["status"] == 400
        assert sent == []


class TestRedaction:
    @pytest.mark.asyncio
    async def test_a_credential_is_not_what_gets_sent_to_the_model(self, polish):
        """The transcript leaves the machine, so it leaves it redacted.

        The streaming path already redacts before the browser sees a transcript, but
        a caller can hand this endpoint text from anywhere, so the endpoint does not
        rely on that.
        """
        cfg, sent, set_reply = polish
        set_reply("ok")
        await _call(_request("my key is AKIAIOSFODNN7EXAMPLE please note"))
        assert "AKIAIOSFODNN7EXAMPLE" not in sent[0]
