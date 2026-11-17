"""Log redaction filter for secret values and Bearer tokens.

Intercepts log records at creation time via ``logging.setLogRecordFactory``,
ensuring ALL handlers (including those added after installation) see only
redacted output. This module performs ZERO vault I/O — the caller resolves
any literal secret values and passes them in via ``install_log_redaction``.

Currently the only caller (``cli._setup_cli_logging``) passes an empty list,
so the shape pass is what redacts in practice: the credential plan (the rule table,
:func:`kiro_crew.security.redaction.redact_credentials`) once this process has run
it, and before that, while the filter installs at bootstrap, a floor of Bearer
tokens (a token-shaped value: a digit, or mixed case), standalone signed JWTs and
``token`` URL parameter values. An identifier such as an AWS access key ID is
never masked: it names a key and cannot authenticate. Wiring resolved vault secret
values into the filter at gateway boot is a follow-up.

Records are mutated only when redaction changes the rendered message — for
``msg``/``args`` whose args are exact immutable scalars (str/int/float/bool/
None, no subclasses) with clean text: a scalar's entire exportable surface IS
the string the scan inspects. Any opaque object arg disqualifies preservation
— its attributes can carry credentials a structured handler would serialize
but no text scan can bound. ``exc_info`` is ALWAYS rendered, redacted into
``exc_text``, and cleared: a live traceback is an object graph whose frames
carry locals no text scan can bound.

The trade this makes explicit: redaction inspects the RENDERED
text, never the internals of the ``args`` objects themselves, so a structured
handler that serializes arg objects directly (attribute dumps, JSON) can emit
content the text scan never saw. That limitation is inherent — the scan does not
look inside objects — and the alternative is destroying such records wholesale as
collateral. Preservation also moves rendering from creation time
to emit time: an arg object whose ``str()`` reads live state, or one mutated
before a ``QueueHandler`` drains, can emit text the creation-time scan never
saw. Both halves of the trade are accepted deliberately in favor of keeping
structured fields intact on the overwhelmingly common secretless record.
"""

from __future__ import annotations

import logging
import re
import string
import sys
import threading
from typing import Callable, Optional

from kiro_crew import redaction_seams
from kiro_crew.credential_patterns import JWT_MULTI_SEGMENT, TOKEN_PARAMETER
from kiro_crew.jwt_header import is_jwt_header

logger = logging.getLogger(__name__)

# Bearer tokens: RFC 6750 token68 charset (case-insensitive to catch
# serializations that lowercase the scheme, e.g. "bearer <token>"). A match is
# masked only when its value is token-shaped (:func:`_mask_bearer`).
_BEARER_RE = re.compile(r"Bearer [A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE)

_DIGITS = frozenset(string.digits)
_UPPER = frozenset(string.ascii_uppercase)
_LOWER = frozenset(string.ascii_lowercase)
_BEARER_SCHEME_LEN = len("Bearer ")


def _token_shaped(value: str) -> bool:
    """Whether *value* reads as a token: it holds a digit, or mixes case past its first letter.

    Prose after the word ``bearer`` (``bearer authentication``, ``Bearer Credentials``)
    has neither; a random token of eight or more characters almost always has one.
    """
    chars = set(value)
    if chars & _DIGITS:
        return True
    return bool(set(value[1:]) & _UPPER) and bool(chars & _LOWER)


def _mask_bearer(m: re.Match[str]) -> str:
    """``Bearer [REDACTED]`` for a :data:`_BEARER_RE` match whose value is token-shaped."""
    if _token_shaped(m.group()[_BEARER_SCHEME_LEN:]):
        return "Bearer [REDACTED]"
    return m.group()


# Standalone JWTs: the shared ``eyJ`` header plus 2-4 further dot-separated
# base64url segments, covering 3-segment JWS AND 4-5-segment JWE (dir/ECDH-ES).
# Catches tokens logged OUTSIDE a ``Bearer `` scheme -- JSON-embedded, bare, or
# assignment-style. Same string object as the scrubber's JWS/JWE branch, and
# shape-only like it, so ``_redact_jwts`` applies the scrubber's header check.
_JWT_RE = re.compile(JWT_MULTI_SEGMENT)

# ``?token=`` / ``&token=`` values: the scrubber's own spelling. Only the value is
# replaced, so a redacted URL still reads as a token URL. None of the scrubber's
# skips or exemptions apply here: over-masking a log line is the safe direction.
_TOKEN_PARAM_RE = re.compile(TOKEN_PARAMETER)


def _mask_token_value(m: re.Match[str]) -> str:
    return m.group()[: m.start(1) - m.start()] + "[REDACTED]"


def _redact_jwts(text: str) -> str:
    """Replace each ``_JWT_RE`` hit whose header passes ``is_jwt_header``, as the scrubber does.

    ``honeyJar.atlassian.net`` is left intact. A rejected hit is rescanned from one
    character on, so a token glued after it (``honeyJar.<jws>``) still redacts.
    """
    parts: list[str] = []
    last = pos = 0
    while (m := _JWT_RE.search(text, pos)) is not None:
        if not is_jwt_header(m.group().split(".", 1)[0]):
            pos = m.start() + 1
            continue
        parts += (text[last : m.start()], "[REDACTED]")
        last = pos = m.end()
    return "".join(parts) + text[last:] if parts else text


class SecretRedactionFilter:
    """Redacts secret patterns and Bearer tokens from log records.

    Installed via ``logging.setLogRecordFactory`` so it intercepts EVERY
    record at creation — no handler can see plaintext regardless of when
    it was added. This class performs ZERO I/O at runtime — patterns are
    frozen at construction time.
    """

    def __init__(self, patterns: list[str]) -> None:
        """Initialize with literal secret values to redact.

        Parameters
        ----------
        patterns:
            List of literal strings (secret values) to redact. Each value
            is regex-escaped. Values shorter than 4 chars are skipped to
            avoid false positives.
        """
        escaped = [re.escape(p) for p in patterns if len(p) >= 4]
        self._secret_pattern: Optional[re.Pattern[str]] = (
            re.compile("|".join(escaped)) if escaped else None
        )

    def redact(self, text: str) -> str:
        """Replace secret values, Bearer tokens, JWTs and ``token`` values.

        Zero I/O — only applies pre-compiled patterns. With the owner's
        redaction setting off (``redaction_seams.published_off``) only the secret values are replaced.
        """
        # The literal vault-secret pass runs whatever the owner chose: these are
        # values the gateway resolved itself, never a shape guess.
        if self._secret_pattern is not None:
            text = self._secret_pattern.sub("[REDACTED]", text)
        # The shape passes below are the LOGS output seam.
        if redaction_seams.published_off():
            return text
        return redact_log_shapes(text)


#: Set while this thread runs the credential plan for a log line, so a record the plan
#: itself logs takes the floor instead of re-entering it.
_IN_PLAN = threading.local()


def _loaded_plan() -> Optional[Callable[[str], tuple[str, list[str]]]]:
    """The credential plan, once this process has loaded and run it; else None.

    Read off ``sys.modules`` and never imported here: this module installs before the
    ``security`` package loads, and a log record must not trigger that import.
    """
    seam = sys.modules.get("kiro_crew.security.redaction_structured.compat")
    redaction = sys.modules.get("kiro_crew.security.redaction")
    if seam is None or redaction is None or not hasattr(seam, "credential_plan"):
        return None
    return getattr(redaction, "redact_credentials", None)


def redact_log_shapes(text: str) -> str:
    """The shape pass over a log line: the credential plan, or the floor before it loads.

    Zero I/O. :class:`SecretRedactionFilter` skips it with the owner's redaction setting
    off; a sink that reaches anyone but the owner (the dashboard's live log stream)
    applies it itself then. A plan that cannot run answers with the floor.
    """
    plan = _loaded_plan()
    if plan is not None and not getattr(_IN_PLAN, "active", False):
        _IN_PLAN.active = True
        try:
            # The floor's Bearer pass runs after the plan too: a bare ``Bearer <token>``
            # with no header name before it is a credential in a log line.
            return _BEARER_RE.sub(_mask_bearer, plan(text)[0])
        except Exception:  # noqa: BLE001 -- the floor below still redacts the line
            pass
        finally:
            _IN_PLAN.active = False
    return _redact_floor(text)


def _redact_floor(text: str) -> str:
    """Bearer tokens, signed JWTs and ``token`` values: what a line gets before the plan loads."""
    text = _BEARER_RE.sub(_mask_bearer, text)
    text = _redact_jwts(text)
    return _TOKEN_PARAM_RE.sub(_mask_token_value, text)


#: Marks an installed factory as a redacting wrapper, and carries the two things it
#: needs: ``base`` (the factory it displaced, to call and to give back) and ``filter``
#: (the live :class:`SecretRedactionFilter`, or ``None`` to pass records through).
#:
#: Both live ON THE INSTALLED FUNCTION rather than in a module global, and that is the
#: invariant the whole module rests on. A module global is resolved at CALL time and can
#: be rewritten — or zeroed by a reload — while the wrapper stays in ``logging``'s slot,
#: so the two disagree and the wrapper ends up calling something other than what it
#: displaced. Held on the function, each wrapper's base is fixed at install:
#:
#: * A CYCLE IS UNREPRESENTABLE. Wrapping our own wrapper makes it the inner link of a
#:   finite chain whose base is still the real original, so the worst case is redacting
#:   twice. Resolved through a global, the same wrap made the wrapper its own base and
#:   every later record recursed until ``RecursionError`` — on a PROCESS-GLOBAL slot, so
#:   no record was emitted again, redacted or not.
#: * OWNERSHIP IS READ OFF THE SLOT. ``install``/``uninstall`` ask the installed object
#:   what it is instead of consulting a sentinel that anything replacing the slot (a test
#:   floor, a reload, another copy of this module) silently invalidates.
_FACTORY_BASE = "_kirocrew_redaction_base"
_FACTORY_FILTER = "_kirocrew_redaction_filter"


#: Exact scalar types whose exportable content IS their text: no attributes,
#: no interiors, nothing a structured handler can serialize beyond the string
#: form the scan inspects. Exact ``type()`` match — a *subclass* of ``str`` or
#: ``int`` can carry attributes holding credentials, so subclasses do not
#: qualify.
_BOUNDED_SCALAR_TYPES = (str, int, float, bool, type(None))


def _args_are_scan_bounded(args: object, filt: "SecretRedactionFilter") -> bool:
    """True only when every arg's exportable content was fully scanned clean.

    Args qualify only as a plain ``tuple`` whose every element is an exact
    immutable scalar (:data:`_BOUNDED_SCALAR_TYPES`) with clean text. The
    scalar restriction is what bounds the export surface: an OPAQUE object can
    have a benign ``str()`` while its attributes carry a credential that a
    structured handler serializing the object (attribute dump, JSON) would
    export — text scanning cannot bound that. A MAPPING is unbounded for the
    same reason: the dict object itself is exportable, and its KEYS (which
    ``%(name)s`` formatting never renders in full) can carry a credential — so
    mappings are never preserved, not key/value scanned. The text check on top
    closes the lossy-format hole (``%.100s`` rendering a clean prefix of a
    secret-bearing scalar).
    """
    # Only genuinely-absent args qualify as trivially bounded: ``None`` or an
    # exact empty tuple. A truthiness check would trust ANY falsy object —
    # e.g. a custom instance whose ``__bool__`` returns False while its
    # attributes carry a credential.
    if args is None or (type(args) is tuple and len(args) == 0):
        return True
    if type(args) is not tuple:
        # dict args (``%(name)s`` style), subclasses, or the odd bare arg:
        # all carry exportable content beyond their rendered text.
        return False
    try:
        for value in args:
            if type(value) not in _BOUNDED_SCALAR_TYPES:
                return False
            text = str(value)
            if filt.redact(text) != text:
                return False
    except Exception:
        return False
    return True


def _redacting_factory(base: Callable[..., logging.LogRecord]) -> Callable[..., logging.LogRecord]:
    """Build a record factory that redacts everything *base* produces.

    The wrapper runs at record CREATION, before any handler sees it, which is what
    covers handlers added after installation.

    Redaction stays process-wide — EVERY record is inspected. ``msg``/``args``
    are preserved only when every arg is an exact immutable scalar whose text
    scans clean (see :func:`_args_are_scan_bounded`), so a downstream JSON or
    observability handler still sees the original ``%s`` template and its
    ``args`` tuple on the common secretless-scalar record, while a record
    carrying any opaque object — whose attributes no text scan can bound — is
    materialized and cleared. ``exc_info`` is always rendered, redacted into
    ``exc_text``, and cleared — traceback frames carry locals no text scan can
    bound, so a live triple is never handed to a handler that might export
    them.
    """

    def factory(*args, **kwargs) -> logging.LogRecord:  # type: ignore[no-untyped-def]
        record = base(*args, **kwargs)
        filt: Optional[SecretRedactionFilter] = getattr(factory, _FACTORY_FILTER, None)
        if filt is None:
            return record

        # Materialize the full formatted message and redact it. Preserve the
        # record ONLY when nothing a handler could export carries a secret: the
        # rendered text AND every arg's own string form must scan clean.
        # Scanning args individually closes the lossy-format hole — a spec like
        # ``%.100s`` can render a clean prefix while the full secret stays in
        # ``record.args`` for a structured handler to export.
        try:
            rendered = record.getMessage()
        except Exception:
            # Render failure: args were never inspected, so "unchanged text ⇒
            # secretless" does not hold on this branch. Fall back to
            # unconditional destruction — scan the bare template and drop args
            # wholesale. Fidelity is already lost (the record cannot format),
            # and CPython's Handler.handleError prints ``Arguments: %s`` to
            # stderr on a later format failure, which would leak a
            # secret-bearing arg this scan never saw.
            record.msg = filt.redact(str(record.msg) if record.msg else "")
            record.args = None
            rendered = None
        if rendered is not None:
            redacted = filt.redact(rendered)
            # Preserve only when EVERY exportable component is bounded by the
            # scanned text: ``msg`` must be an exact ``str`` (an opaque msg
            # OBJECT has attributes a structured handler could export that its
            # rendered text never showed), and args must be a plain tuple of
            # exact scalars with clean text (see _args_are_scan_bounded).
            if (
                redacted != rendered
                or type(record.msg) is not str
                or not _args_are_scan_bounded(record.args, filt)
            ):
                record.msg = redacted
                record.args = None  # Already materialized — prevent double-format

        # exc_text: an already-rendered traceback string. Redacting in place is
        # idempotent, so assign unconditionally — the smaller form.
        if record.exc_text:
            record.exc_text = filt.redact(record.exc_text)

        # exc_info: ALWAYS render, redact, pin into exc_text, and clear. Unlike
        # msg/args — whose exportable surface is exactly the string forms the
        # scan inspects — a live exc_info triple is an object graph whose
        # traceback FRAMES carry locals (e.g. a client object holding a token)
        # that no text scan can bound: a locals-capturing handler would export
        # credentials the rendered-text check never saw. The preserve-on-clean
        # rule therefore applies only to msg/args; exc_info is destroyed
        # unconditionally, exactly as before this module preserved anything.
        if record.exc_info:
            import traceback

            rendered_tb = "".join(traceback.format_exception(*record.exc_info))
            record.exc_text = filt.redact(rendered_tb)
            record.exc_info = None  # Prevent handler re-render / locals export
        return record

    setattr(factory, _FACTORY_BASE, base)
    setattr(factory, _FACTORY_FILTER, None)
    return factory


def _installed_factory() -> Optional[Callable[..., logging.LogRecord]]:
    """The redacting wrapper currently in ``logging``'s slot, or ``None``.

    Only the TOP of the chain is inspected. A third party that wrapped ours answers
    ``None``, which is the right answer for both callers: the slot is not theirs to hand
    back, and installing a second wrapper over it is safe because chains terminate.

    Marker-based rather than identity-based so it survives this module being reloaded or
    imported a second time — either of which produces a new function object for a
    wrapper that is already installed and working.
    """
    factory = logging.getLogRecordFactory()
    return factory if hasattr(factory, _FACTORY_BASE) else None


def install_log_redaction(patterns: list[str]) -> SecretRedactionFilter:
    """Install the redaction filter via ``logging.setLogRecordFactory``.

    This intercepts records at creation time — a record-level chokepoint
    that covers ALL handlers, including those added later. The module
    performs ZERO vault I/O — the caller resolves secret values and passes
    them as *patterns*.

    Idempotent: when a redacting wrapper is already installed, its filter is
    re-pointed at *patterns* and the slot is left alone. Re-pointing the INSTALLED
    wrapper, rather than a global only this copy of the module can see, is what keeps
    the new patterns in force after a reload or a second import.

    Parameters
    ----------
    patterns:
        Literal secret values to redact from log output. Values shorter
        than 4 chars are ignored (too likely to cause false positives).
    """
    filt = SecretRedactionFilter(patterns)

    installed = _installed_factory()
    if installed is None:
        installed = _redacting_factory(logging.getLogRecordFactory())
        logging.setLogRecordFactory(installed)
    setattr(installed, _FACTORY_FILTER, filt)

    return filt


def uninstall_log_redaction() -> None:
    """Stop redacting, and give the record factory back.

    ``install_log_redaction`` needs a counterpart because the record factory is
    process-global: a caller that installs one and cannot remove it has changed
    every ``LogRecord`` for the life of the process, including dropping
    ``exc_info``, which the wrapper does on purpose so a handler cannot re-render
    an unredacted traceback.

    Idempotent, and it reverts only the wrapper at the TOP of the slot. If a third
    party wrapped ours, theirs is what the slot holds and ours is unreachable behind
    their closure, so nothing is touched and redaction CONTINUES until they remove
    their own wrapper. That is the safe direction for a control whose job is to keep
    secrets out of the log: the alternative is reaching into a chain this module does
    not own and unlinking whatever they added.
    """
    installed = _installed_factory()
    if installed is None:
        return
    setattr(installed, _FACTORY_FILTER, None)
    logging.setLogRecordFactory(getattr(installed, _FACTORY_BASE))
