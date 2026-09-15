"""Corpus constraints for the admission benchmark's committed 50-topic corpus.

:func:`validate_corpus` refuses a corpus whose fragments ``write_episodic`` would
silently drop. A dropped fragment removes a row from the haystack without removing
it from the denominator the report is computed over, so the reported precision
would describe a store that never held what the report says it did.
:mod:`.member_v2` runs it before every measurement, and
``test_episodic_admission_bench.py`` pins the committed corpus against it in the
default suite. The gate measurement itself lives in ``member_v2`` (the CI-run
harness); nothing here loads a model or fabricates a vector.
"""

from __future__ import annotations

from typing import Sequence

from kiro_crew.vector_memory import _EPISODIC_LONG_TEXT_CHARS

from .admission_corpus import ADMISSION_TOPICS, AdmissionTopic
from .errors import BenchRefusal

#: Fragment lengths the corpus carries, in the order they are reported. These are
#: the two branches of the gate, named rather than derived from a boolean, so a
#: refusal says which branch a fragment belongs to.
SHORT = "short"
LONG = "long"
KINDS = (SHORT, LONG)


class AdmissionRefusal(BenchRefusal):
    """Raised rather than reporting a number computed on a compromised sample."""


def validate_corpus(topics: Sequence[AdmissionTopic] = ADMISSION_TOPICS) -> None:
    """Refuse a corpus whose fragments ``write_episodic`` would silently drop.

    Every constraint here is one the store enforces by returning ``False`` from
    the write. A dropped fragment removes a row from the haystack without removing
    it from the corpus the denominator is computed over, so the reported precision
    would describe a store that never held what the report says it did. Checked up
    front so the failure names the offending topic instead of surfacing as a
    missing pair much later.
    """
    from kiro_crew.vector_memory_constants import _contains_injection

    if len(topics) < 2:
        raise AdmissionRefusal(
            f"the corpus carries {len(topics)} topic(s); with fewer than two there is "
            "no irrelevant pair to score against."
        )
    seen_ids: set[str] = set()
    seen_prefixes: dict[str, str] = {}
    for topic in topics:
        if topic.topic_id in seen_ids:
            raise AdmissionRefusal(f"duplicate topic_id in the corpus: {topic.topic_id!r}")
        seen_ids.add(topic.topic_id)
        for kind in KINDS:
            text = getattr(topic, kind)
            label = f"{topic.topic_id}.{kind}"
            # The store's own bounds. Outside them write_episodic returns False.
            if not 10 <= len(text) <= 2000:
                raise AdmissionRefusal(
                    f"{label} is {len(text)} chars; write_episodic accepts 10-2000."
                )
            is_long = len(text) > _EPISODIC_LONG_TEXT_CHARS
            if is_long != (kind == LONG):
                raise AdmissionRefusal(
                    f"{label} is {len(text)} chars, which puts it on the wrong side of the "
                    f"{_EPISODIC_LONG_TEXT_CHARS}-char long-text cutoff for a {kind!r} "
                    "fragment — the two length branches would not be measured separately."
                )
            if _contains_injection(text):
                raise AdmissionRefusal(
                    f"{label} matches an injection pattern, so write_episodic would drop it."
                )
            # Text-hash dedup: the store rejects a second row whose lowercased
            # first 80 characters match an existing one.
            prefix = text[:80].lower()
            if prefix in seen_prefixes:
                raise AdmissionRefusal(
                    f"{label} shares its first 80 characters with {seen_prefixes[prefix]}; "
                    "the store's text-hash dedup would reject the second one."
                )
            seen_prefixes[prefix] = label
