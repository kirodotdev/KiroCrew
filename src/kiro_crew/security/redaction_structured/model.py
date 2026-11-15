"""Shared types for the structured credential detector: hits, tiers and ranks."""

from __future__ import annotations

from dataclasses import dataclass

TAG = "[REDACTED: credential]"

SECRET = "secret"
IDENTIFIER = "identifier"
TIERS = frozenset({SECRET, IDENTIFIER})
TIER_RANK = {SECRET: 2, IDENTIFIER: 1}

# Resolve step 3: a more certain detector names a merged span.
CERTAINTY_RANK = {
    "checksum": 5,
    "fixed_format": 4,
    "armour": 3,
    "jose": 3,
    "known_key": 2,
    "key_id_pair": 1,
    "structure": 0,
}

# Detectors whose Secret hits a structural Identifier that fully contains them overrides.
CONTAINABLE_DETECTORS = frozenset({"known_key", "key_id_pair"})

# Structural Identifier rows that do NOT override an exact-label (known_key) Secret hit
# (owner ruling R4): the label is exact evidence, the object-name row only an alphabet
# and a length. They still override key-ID pairing.
LABEL_BEATS_RULES = frozenset({"git.object_name"})

# A hit whose unit is still running when a stream window ends, decided as a Secret:
# ``all`` covers everything to the end of the text, ``b64run`` the rest of a base64 run.
TAIL_ALL = "all"
TAIL_B64RUN = "b64run"


@dataclass(frozen=True, slots=True)
class Hit:
    """One detection over ``text[start:end]``. Never holds the value itself."""

    start: int
    end: int
    rule_id: str
    tier: str
    detector: str
    certainty: str
    structural: bool = False
    tail: str = ""
    # Where the text this hit was decided from begins, when that is before ``start``
    # (a URL parameter is decided from its URL's start); -1 when it is ``start``. A
    # stream keeps this position uncommitted until the hit itself is committed.
    anchor: int = -1

    def rank_key(self) -> tuple[int, int, int, str]:
        """Ascending sort key: higher tier, more certain detector, longer span, lower id."""
        return (
            -TIER_RANK[self.tier],
            -CERTAINTY_RANK[self.certainty],
            -(self.end - self.start),
            self.rule_id,
        )


Span = tuple[int, int, str, str]
