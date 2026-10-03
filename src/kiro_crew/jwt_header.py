"""The structural half of the shared JWT spelling: is the first segment a JSON object?

``credential_patterns.JWT_MULTI_SEGMENT`` matches on shape alone -- ``eyJ`` plus
two to four dot-separated base64url segments -- so a dotted name that merely
contains ``eyJ`` (``honeyJar.atlassian.net``, ``at eyJsonSerializer.deserialize.value``)
satisfies it and is cut in half by any consumer that trusts the match. A
left boundary is NOT the fix: it would miss a real token a renderer glued onto a
label (``compact=jwt<token>``), and a miss is a leak where a false positive is
mangled text. The durable narrowing is structural: every JWS and JWE header, and
the JSON payload of the signed non-JOSE tokens the scrubber already redacts
(itsdangerous, Flask session cookies), base64url-decodes to a JSON object; a
hostname label does not.

This module is that check, in ONE place. ``credential_patterns`` cannot hold it
because that module may import nothing at all (``test_credential_patterns.py``
pins the AST import-free), and ``security.redaction`` cannot be the home because
``log_redaction`` installs at CLI bootstrap, before the security package loads.
So both of them, and ``decisions.gate``, import from here. This module imports
only the stdlib and the import-free spelling home, so it keeps the log floor's
import-leaf shape.

The pattern text itself stays byte-identical: ``test_redaction_mirror_parity.py``
pins it to the frontend copy in ``website/src/utils/sanitize.ts``, which still
matches on shape alone by its own in-tree ruling (backend first, mirror later).

Two consumers scan with it in two ways, on purpose. The scrubber keeps the JWT
alternative inside its 23-branch alternation (``security.redaction``), because
branch ORDER decides what a span is called there (a Bearer header subsumes its
JWT; a link token retried at the same start); it imports only the verdict,
:func:`is_jwt_lookalike`. A consumer of the bare spelling -- the log floor, the
decisions gate -- scans with :func:`jwt_matches`, which is linear where a plain
``search`` is not (see its docstring).
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Iterator

from kiro_crew.credential_patterns import JWT_MULTI_SEGMENT

#: The base64url encoding of the ``{"`` every JSON-object header starts with.
JWT_PREFIX = "eyJ"

#: The shared spelling, compiled once. Same string object as the scrubber's branch.
JWT_RE = re.compile(JWT_MULTI_SEGMENT)

#: The segment class of the spelling, as a run: one linear step over a base64url run.
_SEGMENT_RUN_RE = re.compile(r"[A-Za-z0-9_-]*")


def is_json_object_segment(segment: str) -> bool:
    """Whether *segment* base64url-decodes to a JSON object: JOSE, itsdangerous, Flask session.

    Padding is restored before decoding because compact serialization strips it.
    Anything that is not valid base64url, not valid JSON, or JSON that is not an
    object (an array, a string, a number) is not a credential header.
    """
    try:
        header = json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))
    except (ValueError, RecursionError):
        return False
    return isinstance(header, dict)


def is_jwt_lookalike(match: re.Match[str]) -> bool:
    """Whether *match* is a JWS/JWE-shaped hit whose header is NOT a JSON object.

    Only a hit with at least two dots is on the multi-segment branch (the one-dot
    dashboard link token is a different alternative with its own tuned bounds). A
    header that holds a second ``eyJ`` is kept as a credential rather than rejected:
    rejecting it would make a consumer rescan that header once per ``eyJ`` it holds,
    so failing closed there is what keeps the rescan linear on dense input.
    """
    text = match.group()
    if not text.startswith(JWT_PREFIX) or text.count(".") < 2:
        return False
    header = text.split(".", 1)[0]
    if header.find(JWT_PREFIX, 1) != -1:
        return False
    return not is_json_object_segment(header)


def jwt_matches(text: str) -> Iterator[re.Match[str]]:
    """Every JWS/JWE in *text* by the shared spelling, minus the lookalikes, in linear time.

    ``JWT_RE.search`` is quadratic on a long base64url run holding many ``eyJ``
    (``eyJeyJeyJ...`` with no dot behind it): the engine tries each ``eyJ`` as a
    start and backtracks the greedy first segment over the whole run every time.
    But a match anchored at an ``eyJ`` is decided by what FOLLOWS the run -- the
    first segment cannot stop short of the run's end, because its class has no
    ``.`` -- so one failed attempt at the first ``eyJ`` of a run decides every
    later ``eyJ`` in that run. The scan therefore anchors ONE attempt per run
    and skips to the run's end when it fails. A rejected lookalike resumes one
    character on, not at its end: a real token can follow it with only a dot
    between them (``honeyJar.<jws>`` is one regex hit whose header is ``eyJar``).
    """
    pos = 0
    while (start := text.find(JWT_PREFIX, pos)) != -1:
        match = JWT_RE.match(text, start)
        if match is None:
            run = _SEGMENT_RUN_RE.match(text, start)
            pos = max(run.end() if run is not None else start, start + 1)
            continue
        if is_jwt_lookalike(match):
            pos = start + 1
            continue
        yield match
        pos = max(match.end(), start + 1)
