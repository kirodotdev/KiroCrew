"""Self-contained pieces of the personal drive that make no AWS call.

Split out of :mod:`kiro_crew.publish.personal_drive` to keep that module under the
repository's line cap. Nothing here changed in the move. ``personal_drive`` re-exports
every name, so ``personal_drive.<name>`` keeps working for callers and tests:

* the publish handle (``external_id``) format and its single parser;
* the opaque-origin sandbox response-headers policy config;
* the per-artifact mutation locks;
* the "no profile registered" messages.
"""

from __future__ import annotations

import re
import secrets
import threading
from typing import Any, Callable, TypeVar

_T = TypeVar("_T")

#: ``external_id`` is ``<random>~<profile>``. The RANDOM half comes first so a profile
#: name containing the separator cannot corrupt the object key, and the key/URL use only
#: that half -- an account label never reaches a public link.
_ID_SEP = "~"
_EXTERNAL_ID_BYTES = 16

#: Account-global name of the response-headers policy that opaque-origins published
#: artifacts. Account-global because a policy name is unique per account, which is
#: exactly why the find-or-create below must VERIFY a same-named policy before reusing it
#: (see :meth:`_sandbox_headers_policy`).
_SANDBOX_POLICY_NAME = "kirocrew-publish-drive-sandbox"

#: THE sandbox CSP directive -- one source of truth. It is spelled ONCE here so the create
#: path and the reuse-verification path cannot drift: if a second literal existed in the
#: verify path, a future edit to one copy would let the verifier accept (or a create
#: emit) a policy whose CSP does not match, silently defeating the isolation. Delivered
#: as a HEADER (a ``<meta>`` CSP ignores ``sandbox`` by spec), it gives each served
#: document an OPAQUE origin so origin-keyed storage (localStorage/IndexedDB/cookies) is
#: simply unreachable and cannot be shared between mutually-untrusted published documents.
#:
#: ``frame-ancestors 'none'`` rides along in the SAME constant deliberately, rather than
#: living only in the ``FrameOptions`` header below, because this constant is the one thing
#: the reuse path VERIFIES: a policy whose CSP does not match exactly fails closed. Framing
#: protection expressed only as a header would be reused unverified, which is the same
#: fail-open the CSP comparison exists to prevent. It is ``'none'`` and not ``'self'``
#: because every artifact on a drive shares ONE domain -- ``'self'`` would let one published
#: document frame another, which is precisely the cross-document relationship the opaque
#: origin above exists to deny. Nothing legitimately frames a published copy: the
#: dashboard's viewer renders artifact HTML from its own storage, never the public URL.
_SANDBOX_CSP = "sandbox allow-scripts allow-popups; frame-ancestors 'none'"


def _sandbox_policy_config() -> dict[str, Any]:
    """The response-headers-policy config this module installs -- one source of truth.

    Both the create path and the reuse-verification path derive the required CSP from the
    SAME :data:`_SANDBOX_CSP` constant via this builder, so there is no second literal to
    drift out of sync with the one the create path writes.
    """
    return {
        "Name": _SANDBOX_POLICY_NAME,
        "Comment": "Opaque-origin sandbox for pooled artifact publishing",
        "SecurityHeadersConfig": {
            "ContentSecurityPolicy": {
                "ContentSecurityPolicy": _SANDBOX_CSP,
                "Override": True,
            },
            "ContentTypeOptions": {"Override": True},
            # Legacy companion to the CSP's `frame-ancestors`: a browser too old for CSP3
            # honours only this header, and a published artifact is a public page whose
            # framing a third-party origin would otherwise control. DENY rather than
            # SAMEORIGIN for the reason given on `_SANDBOX_CSP` -- one drive serves every
            # artifact from a single domain, so "same origin" is another published
            # document, not a trusted host.
            "FrameOptions": {"FrameOption": "DENY", "Override": True},
            "StrictTransportSecurity": {
                "AccessControlMaxAgeSec": 31536000,
                "IncludeSubdomains": True,
                "Override": True,
            },
            "ReferrerPolicy": {
                "ReferrerPolicy": "same-origin",
                "Override": True,
            },
        },
    }


# One lock per published artifact, so two mutations of the SAME object cannot interleave.
# Every mutating path is read-then-write against S3 (head to decide, then copy/put/delete),
# and the seam does not serialize per artifact -- its only lock covers cloning -- so without
# it a push landing between a move's copy and its delete has its bytes deleted with the
# source, leaving the record naming a version the drive does not hold and, since the
# stored digest then matches nothing, every later push in permanent conflict.
#
# Keyed on the artifact's own id at this destination, so unrelated artifacts never contend.
# Entries are never evicted: one small lock per artifact touched in this process is cheaper
# than the bugs that come from freeing a lock somebody may be about to take.
_KEY_LOCKS: dict[str, threading.Lock] = {}
_KEY_LOCKS_GUARD = threading.Lock()


def _key_lock(key_part: str) -> threading.Lock:
    """The lock guarding one artifact's remote state."""
    with _KEY_LOCKS_GUARD:
        return _KEY_LOCKS.setdefault(key_part, threading.Lock())


def _serialized(key_part: str, work: Callable[[], _T]) -> _T:
    """Run one artifact's remote mutation under that artifact's own lock.

    Called on the worker thread, never on the event loop, so a blocking lock is the right
    primitive here and cannot stall the gateway.
    """
    with _key_lock(key_part):
        return work()


def _no_profile_message() -> str:
    return (
        "No AWS account is registered yet, so there is nowhere to put the drive. "
        "Register a profile on the Artifact Deploy page first; the drive is then "
        "created in that account on the first publish."
    )


def _unknown_profile_message(requested: str, known: list[str]) -> str:
    if known:
        return (
            f"AWS profile {requested!r} is not registered. "
            f"Registered profiles: {', '.join(sorted(known))}."
        )
    return _no_profile_message()


#: An AWS account id is exactly twelve digits and cannot contain :data:`_ID_SEP`. That is
#: what makes the three-field handle unambiguous even though a PROFILE NAME may contain the
#: separator: the middle field is read as an account ONLY if it has this shape, so a
#: two-field handle minted before binding existed cannot have its profile name mistaken for
#: an account.
_ACCOUNT_RE = re.compile(r"^\d{12}$")


def make_external_id(profile: str, account: str) -> str:
    """A publish handle binding an unguessable key to the ACCOUNT that holds the copy.

    Shape: ``<key>~<account>~<profile>``. The account is what the withdrawal paths verify;
    the profile name is retained only so the AWS calls have a credential source to resolve.

    Binding the NAME alone was the earlier shape and it was a real exposure, not a nicety.
    A profile name is a local alias the owner can repoint at another account by editing
    their AWS config. Do that, then delete the artifact, and the withdrawal resolved the
    name to the NEW account, found no drive there, reported a removal that removed nothing,
    and cleared the record -- leaving the original copy public in the old account with its
    handle erased. That needed no race and no failure, just a config edit and an ordinary
    delete, which is why it was fixed here rather than deferred with the timing-dependent
    windows.
    """
    key = secrets.token_hex(_EXTERNAL_ID_BYTES)
    return f"{key}{_ID_SEP}{account}{_ID_SEP}{profile}"


def _parse_external_id(external_id: str) -> tuple[str, str, str]:
    """Return ``(key_part, bound_account, bound_profile)``; unknown fields are ``''``.

    The single parser for this format, so the three readers below cannot drift apart.
    Three shapes are accepted, and the middle one is the reason for :data:`_ACCOUNT_RE`:

    * ``key~account~profile`` -- current. Account and profile both known.
    * ``key~profile`` -- minted before binding, or hand-written by a caller. The middle
      field fails the account shape, so it is read as the PROFILE and the account is
      unknown. Reading it as an account would be the worst possible error: the withdrawal
      paths would then compare a profile name against a live account id, mismatch every
      time, and refuse every operation on every pre-existing publication.
    * ``key`` -- no separator at all. Both unknown; the caller falls back to the registry
      default, which is the pre-binding behaviour.
    """
    key_part, _, rest = (external_id or "").partition(_ID_SEP)
    if not rest:
        return key_part, "", ""
    head, sep, tail = rest.partition(_ID_SEP)
    if sep and _ACCOUNT_RE.match(head):
        return key_part, head, tail
    return key_part, "", rest


def split_external_id(external_id: str) -> tuple[str, str]:
    """Return ``(key_part, bound_profile)``; the profile is ``''`` when unbound.

    Kept at two fields because that is all its callers need -- they want the object key,
    or the credential source. The account is read through :func:`bound_account` by the one
    place that verifies it.
    """
    key_part, _account, bound = _parse_external_id(external_id)
    return key_part, bound


def bound_account(external_id: str) -> str:
    """The account id pinned into the handle, or ``''`` when the handle predates binding."""
    _key, account, _profile = _parse_external_id(external_id)
    return account
