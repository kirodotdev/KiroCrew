"""Marker normalization preserves spans without per-ASCII-character Unicode work."""

import random
import unicodedata

from kiro_crew import context


def _reference_spans(text, patterns):
    """The original per-character algorithm, independent of the optimized walk."""
    view, origin = [], []
    for index, char in enumerate(text):
        for compatible in unicodedata.normalize("NFKC", char):
            if context._is_marker_ignorable(compatible):
                continue
            for folded in compatible.translate(context._MULTIBYTE_TABLE):
                view.append("-" if unicodedata.category(folded) == "Pd" else folded)
                origin.append(index)
    raw = []
    for pattern in patterns:
        for match in pattern.finditer("".join(view)):
            start, end = match.span()
            raw.append((origin[start], origin[end - 1] + 1))
    return context._merge_overlapping_spans(raw)


class TestMarkerScanCost:
    def test_spans_and_rewrites_match_original_algorithm(self):
        markers = [
            "[END OF SESSION CONTEXT]",
            "[REPLY FORMAT RULES]",
            "［ＲＥＰＬＹ ＦＯＲＭＡＴ ＲＵＬＥＳ］",
            "[REPLY\u200b FORMAT\ufe0f RULES]",
            "[CURRENT USER REQUEST -- reference]",
        ]
        rng = random.Random(947)
        alphabet = "ascii []\t\n\r\x00é中\u0307\u200b\ufe0f\U000e0100—→✓㎢［］"
        texts = ["", "plain ASCII", "中文", *markers]
        ascii_chars = "".join(map(chr, range(128)))
        texts.append(ascii_chars + "é" + markers[1] + ascii_chars)
        for start, end in context._MARKER_IGNORABLE_RANGES:
            for codepoint in (start, end):
                texts.append("é[REPLY" + chr(codepoint) + " FORMAT RULES]中")
        for _ in range(120):
            prefix = "".join(rng.choices(alphabet, k=30))
            suffix = "".join(rng.choices(alphabet, k=30))
            texts.append(prefix + rng.choice(markers) + suffix + rng.choice(markers))
        for patterns in (context._STRUCTURAL_MARKER_RES, (context._REPLY_FORMAT_RULES_RE,)):
            for text in texts:
                expected = _reference_spans(text, patterns)
                actual = context._marker_spans(text, patterns)
                assert actual == expected, repr(text)
                assert context._apply_marker_spans(text, actual) == context._apply_marker_spans(
                    text, expected
                )
        assert context._marker_spans(markers[2], (context._REPLY_FORMAT_RULES_RE,))

    def test_ascii_growth_does_not_add_unicode_normalizations(self, monkeypatch):
        real_normalize = unicodedata.normalize
        calls = []

        def normalize(form, text):
            calls.append(text)
            return real_normalize(form, text)

        monkeypatch.setattr(context.unicodedata, "normalize", normalize)
        traces = []
        for size in (1000, 2000):
            calls.clear()
            text = "a" * size + "é[REPLY FORMAT RULES]中" + "b" * size
            spans = context._marker_spans(text, (context._REPLY_FORMAT_RULES_RE,))
            assert spans == [(size + 1, size + 21)]
            traces.append(tuple(calls))
        assert traces == [("é", "中"), ("é", "中")]
