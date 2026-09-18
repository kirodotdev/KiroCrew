"""The over-capacity fork refusal sends a ceiling on the wire and states none in copy.

A row count is not an affordance in a transcript that numbers nothing, so the banner
names the ACTION that shrinks the slice and no number at all. The bound still travels
on the wire for programmatic callers. This pins both halves: the wire field against the
constant the backend enforces, and every catalog against carrying no count.
"""

import json
import pathlib
import re

from kiro_crew.dashboard.chat_fork import _MAX_SLOT_MESSAGES

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_LOCALES = _REPO_ROOT / "website" / "src" / "i18n" / "locales"
_FORK = _REPO_ROOT / "src" / "kiro_crew" / "dashboard" / "chat_fork.py"
_KEYS = ("fork_too_large_error_head", "fork_too_large_error_tail")
_DIGIT = re.compile(r"[0-9]")


class TestForkCeilingIsEnforcedNotReported:
    def test_the_refusal_ships_no_field_nothing_reads(self) -> None:
        """The copy states no count, so the wire field had no reader left either."""
        source = _FORK.read_text(encoding="utf-8")
        assert '"max_messages"' not in source, (
            "the refusal carries a bound again; every consumer was removed with the "
            "count, so a reinstated field ships with nobody to read it"
        )
        assert (
            '"code": "fork_corpus_too_large"' in source
        ), "the code IS read -- it selects the localized copy -- so it must survive"

    def test_no_catalog_states_a_count_the_transcript_cannot_show(self) -> None:
        catalogs = sorted(p for p in _LOCALES.glob("*.json"))
        assert catalogs, f"no catalogs found under {_LOCALES}"
        checked: list[str] = []
        offenders: list[str] = []
        for path in catalogs:
            page = json.loads(path.read_text(encoding="utf-8")).get("pages", {}).get("chatPage", {})
            if _KEYS[0] not in page:
                continue
            checked.append(path.name)
            for key in _KEYS:
                text = page[key]
                if "{{max}}" in text:
                    offenders.append(f"{path.name}/{key}: interpolates a count: {text!r}")
                elif _DIGIT.search(text):
                    offenders.append(f"{path.name}/{key}: states a count: {text!r}")
        assert len(checked) >= 13, f"expected every catalog to carry the key, got {checked}"
        assert not offenders, f"a count the user cannot act on: {offenders}"

    def test_the_bound_dependent_fallback_is_gone_from_every_catalog(self) -> None:
        """Its only consumer needed a payload the one refusal site never sends."""
        present = [
            path.name
            for path in sorted(_LOCALES.glob("*.json"))
            if "fork_too_large_error_generic" in path.read_text(encoding="utf-8")
        ]
        assert not present, f"unreachable fallback copy still shipped in {present}"

    def test_the_enforced_ceiling_is_a_positive_count(self) -> None:
        assert isinstance(_MAX_SLOT_MESSAGES, int) and _MAX_SLOT_MESSAGES > 0
