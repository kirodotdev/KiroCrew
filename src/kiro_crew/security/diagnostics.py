"""What a refusal says about ITSELF: rule id, component, span offsets and shape.

A refusal that names only a matcher cannot be acted on. The agent reading it
cannot tell WHICH tier decided, cannot tell WHERE in its own command the decision
landed, and therefore cannot tell a true positive from a false one -- so it either
retries blind or gives up on work the policy does not actually forbid. Naming the
tier and the offsets turns that into a diagnosis the reader can check against the
command it just wrote.

Three properties make this safe to put in front of a model and in an audit record.

*It carries no payload bytes.* The matched region is reported as offsets plus a
character-class census, never as text. A refusal is the one message that is
guaranteed to be about content the policy judged sensitive, so echoing the match
would make the explanation the leak. :class:`RefusalSpanShape` is what a reader
gets instead: enough to recognise the token in a command they already hold,
nothing to reconstruct one they do not.

*Its identifiers are closed.* ``rule`` and ``component`` are screened by
:data:`_DIAGNOSTIC_ID_RE`, so a value that is not an identifier is replaced by
:data:`_UNNAMED` rather than rendered. That is what stops agent-controlled text
from reaching the line through a caller that passes the wrong argument: the format
is structurally incapable of quoting a command, not merely careful not to.

*Its cost is bounded.* The census reads at most :data:`_MAX_CENSUS_CHARS` of the
span while ``length`` still reports the span's true size, because the one tier that
refuses without scanning refuses precisely because its subject is too large to
walk on the event loop, and a diagnostic that walked it anyway would reintroduce
the cost that refusal exists to avoid.

It imports nothing from this package, which is what lets both the keystone and the
catalog tier reach it without an import-time cycle.
"""

from __future__ import annotations

import re
import string
from dataclasses import asdict, dataclass

#: Marker for the diagnostic line. Deliberately NOT the deny prefix: the refusal's
#: first line is a parsed micro-format (a per-line, end-anchored regex in the
#: dashboard's recovery card, and a partition on the colon-space separator in the
#: test helper), so a second occurrence of that prefix on a later line would be
#: read as a second, fabricated pattern. This marker shares no prefix with it.
REFUSAL_DIAGNOSTIC_PREFIX = "Refusal diagnostic: "

#: Stand-in for a ``rule`` or ``component`` that is not an identifier. Present so
#: the line renders with the shape it always has, and the caller's bug shows up as
#: a missing name rather than as unscreened text on a security message.
_UNNAMED = "unnamed"

#: Identifier screen for ``rule`` and ``component``: lowercase, dot/dash/underscore
#: separated, bounded. Every catalog rule id and every component name below fits it,
#: and no command, path or pattern body does.
_DIAGNOSTIC_ID_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}\Z")

#: Ceiling on how much of a span the census reads. See the module docstring: the
#: over-ceiling refusal's subject is unbounded by definition.
_MAX_CENSUS_CHARS = 4096

#: Path separators are counted apart from other punctuation because the fence this
#: package guards is a PATH fence: separator count is what distinguishes a
#: multi-segment path token from a word of prose of the same length.
_PATH_SEPARATORS = "/\\"


@dataclass(frozen=True)
class RefusalSpanShape:
    """Character-class census of a matched span, with no character of it.

    ``length`` is the span's true length; ``censused`` is how much of it the counts
    below actually cover, which is less than ``length`` only when the span exceeds
    :data:`_MAX_CENSUS_CHARS`. Keeping both means a bounded census never has to
    misreport the size of what was refused.
    """

    length: int
    censused: int
    ascii_uppercase: int
    ascii_lowercase: int
    digits: int
    path_separators: int
    whitespace: int
    symbols: int
    other: int

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class RefusalDiagnostic:
    """Why a refusal fired, in terms a reader can check without the matched bytes.

    ``rule`` is the identity an operator toggles or an auditor greps for; a catalog
    denial's rule id, or the name of a floor for a tier that owns no catalog row.
    ``component`` is the pass inside that tier, which is what separates "the fence
    matched your text" from "the fence matched a normalized copy of your text" --
    two refusals that read identically today and need different responses.
    """

    rule: str
    component: str
    start: int
    end: int
    shape: RefusalSpanShape

    def as_dict(self) -> dict[str, object]:
        return asdict(self)

    def as_line(self) -> str:
        """The diagnostic as ONE line, prefixed and free of payload bytes."""
        shape = (
            f"len={self.shape.length}"
            f",seen={self.shape.censused}"
            f",upper={self.shape.ascii_uppercase}"
            f",lower={self.shape.ascii_lowercase}"
            f",digit={self.shape.digits}"
            f",sep={self.shape.path_separators}"
            f",space={self.shape.whitespace}"
            f",symbol={self.shape.symbols}"
            f",other={self.shape.other}"
        )
        return (
            f"{REFUSAL_DIAGNOSTIC_PREFIX}rule={self.rule} component={self.component} "
            f"span={self.start}..{self.end} shape={shape}"
        )


def _diagnostic_id(value: str) -> str:
    """*value* if it is an identifier, else :data:`_UNNAMED`."""
    return value if _DIAGNOSTIC_ID_RE.fullmatch(value) else _UNNAMED


def _char_class(char: str) -> str:
    if char in string.ascii_uppercase:
        return "ascii_uppercase"
    if char in string.ascii_lowercase:
        return "ascii_lowercase"
    if char in string.digits:
        return "digits"
    if char in _PATH_SEPARATORS:
        return "path_separators"
    if char.isspace():
        return "whitespace"
    if char.isascii() and char.isprintable():
        return "symbols"
    return "other"


def refusal_span_shape(value: str) -> RefusalSpanShape:
    """Census *value* by character class, reading at most the census ceiling.

    A plain loop rather than a ``Counter`` over a generator: the counted slice is
    already bounded, and the eight buckets are fixed, so a dict of the same eight
    keys buys nothing and this runs on the event loop inside a gate.
    """
    censused = value[:_MAX_CENSUS_CHARS]
    counts = {
        "ascii_uppercase": 0,
        "ascii_lowercase": 0,
        "digits": 0,
        "path_separators": 0,
        "whitespace": 0,
        "symbols": 0,
        "other": 0,
    }
    for char in censused:
        counts[_char_class(char)] += 1
    return RefusalSpanShape(
        length=len(value),
        censused=len(censused),
        ascii_uppercase=counts["ascii_uppercase"],
        ascii_lowercase=counts["ascii_lowercase"],
        digits=counts["digits"],
        path_separators=counts["path_separators"],
        whitespace=counts["whitespace"],
        symbols=counts["symbols"],
        other=counts["other"],
    )


def refusal_diagnostic(
    rule: str,
    component: str,
    subject: str,
    span: "tuple[int, int] | None" = None,
) -> RefusalDiagnostic:
    """Build the diagnostic for a refusal of *subject* by *rule* at *span*.

    ``span`` omitted means the whole subject, which is the honest reading for a
    tier that decides on the argv as a whole rather than at an offset: an
    argv-structural floor matches a command's SHAPE, so pointing at one region of
    it would name a cause the reader could disprove. Out-of-range or inverted
    offsets are clamped rather than rejected, because a diagnostic must never be
    the thing that raises inside a gate that has already decided to refuse.
    """
    if span is None:
        start, end = 0, len(subject)
    else:
        start = max(0, min(span[0], len(subject)))
        end = max(start, min(span[1], len(subject)))
    return RefusalDiagnostic(
        rule=_diagnostic_id(rule),
        component=_diagnostic_id(component),
        start=start,
        end=end,
        shape=refusal_span_shape(subject[start:end]),
    )


def annotate_refusal(reason: str, diagnostic: RefusalDiagnostic) -> str:
    """*reason* with the diagnostic appended on its OWN line.

    Appending rather than inserting is what keeps every existing reader intact:
    the first line stays whatever the refusal already said, and an operator note
    keeps the second line it has always had, so the diagnostic is always last and
    a reader that stops before it sees exactly what it saw before.

    An empty *reason* is returned unchanged. A gate signals "allowed" with a
    falsey reason, so annotating one would turn an allow into a refusal.
    """
    if not reason:
        return reason
    return f"{reason}\n{diagnostic.as_line()}"
