"""Value types for the credential rule table: tiers, detectors, hits and records.

A record says which rule removed how many characters and never carries the value
itself, so a caller can log, count and report redactions without handling a
secret.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum

from kiro_crew.security.redaction import REDACTED_CREDENTIAL_TAG

logger = logging.getLogger(__name__)

#: The text a redacted value is replaced with. It is the credential tag the
#: existing redactors write, so output from either reads the same and each one's
#: idempotence check recognises the other's placeholder.
PLACEHOLDER = REDACTED_CREDENTIAL_TAG


class Tier(str, Enum):
    """What a matched value can do, strongest first.

    ``secret`` and ``bearer_link`` values are replaced; ``identifier`` values are
    found and reported but always kept, because they cannot authenticate.
    """

    SECRET = "secret"
    BEARER_LINK = "bearer_link"
    IDENTIFIER = "identifier"

    @property
    def rank(self) -> int:
        """Position in strength order; lower wins an overlap."""
        return _TIER_ORDER.index(self)

    @property
    def redacts(self) -> bool:
        return self is not Tier.IDENTIFIER


class Detector(str, Enum):
    """How a rule finds a value, most certain first.

    The order is the overlap tie-breaker after tier, so it is part of the
    contract: a more certain detector wins a span a less certain one also claims.
    """

    CHECKSUM_PREFIX = "checksum_prefix"
    VENDOR_PREFIX = "vendor_prefix"
    STRUCTURAL = "structural"
    LABELLED = "labelled"
    KEY_ID_PAIR = "key_id_pair"
    URL_PARAM = "url_param"

    @property
    def rank(self) -> int:
        """Position in certainty order; lower is more certain."""
        return _DETECTOR_ORDER.index(self)


_TIER_ORDER: tuple[Tier, ...] = tuple(Tier)
_DETECTOR_ORDER: tuple[Detector, ...] = tuple(Detector)

#: Detectors that run a rule's own regex over the text.
PATTERN_DETECTORS = frozenset(
    {
        Detector.CHECKSUM_PREFIX,
        Detector.VENDOR_PREFIX,
        Detector.STRUCTURAL,
        Detector.KEY_ID_PAIR,
    }
)
#: Detectors that decide by a field or parameter name rather than a pattern.
NAME_DETECTORS = frozenset({Detector.LABELLED, Detector.URL_PARAM})


@dataclass(frozen=True)
class Hit:
    """A span of the original text claimed by one rule.

    ``start``/``end`` cover exactly what would be replaced: the value alone for a
    ``replace: value`` rule, the whole match for ``replace: match``.
    """

    rule_id: str
    tier: Tier
    detector: Detector
    start: int
    end: int

    @property
    def length(self) -> int:
        return self.end - self.start

    def overlaps(self, other: Hit) -> bool:
        return self.start < other.end and other.start < self.end


@dataclass(frozen=True)
class Record:
    """One replaced span, described without its value."""

    rule_id: str
    tier: Tier
    detector: Detector
    length: int

    def as_dict(self) -> dict[str, object]:
        return {
            "rule_id": self.rule_id,
            "tier": self.tier.value,
            "detector": self.detector.value,
            "length": self.length,
        }


@dataclass(frozen=True)
class RedactionResult:
    """Redacted text plus one record per placeholder, in text order."""

    text: str
    records: tuple[Record, ...]


class RedactionFailed(RuntimeError):
    """Redaction could not complete; the caller must not send or store the text.

    The message names only the internal error's type, never the text or a value
    from it.
    """
