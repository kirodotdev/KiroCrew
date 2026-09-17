"""Apex capability gate: authorized ``@RestResource`` only.

Salesforce exposes two very different ways to run Apex from outside the org, and
this core treats them as different capabilities, not two points on one scale:

* **Custom Apex REST** -- a developer annotates a class with
  ``@RestResource(urlMapping='/...')`` and specific methods with
  ``@HttpGet`` / ``@HttpPost`` / etc., exposing a FIXED, named set of operations
  at a fixed URL. An external caller can invoke ONLY those pre-declared methods.
  This is the capability the L1 core admits, one authorized method at a time.
* **Anonymous Apex** -- ``executeAnonymous`` (Tooling API / SOAP) compiles and
  runs an ARBITRARY Apex code block. It requires the high-privilege "Author
  Apex" permission and runs in system mode, bypassing the FLS and object
  permissions the rest of this campaign is careful to honor. It is a fundamentally
  higher-privilege, unbounded capability. (All facts search-snippet corroborated;
  ``developer.salesforce.com`` rejects automated fetches with HTTP 403.)

The gate: :func:`register_authorized_rest_resource` admits a specific, named
``@RestResource`` method; :func:`reject_execute_anonymous` ALWAYS raises. There
is no code path in this core that registers, wraps, or dispatches anonymous
Apex -- the rejection is unconditional and total, and the negative test proves
the reject path actually fires.
"""

from __future__ import annotations

from dataclasses import astuple, dataclass
from typing import Iterable, Union

#: The HTTP method annotations a custom Apex REST method may carry.
_ALLOWED_HTTP_ANNOTATIONS = frozenset({"HttpGet", "HttpPost", "HttpPut", "HttpPatch", "HttpDelete"})


class ApexCapabilityError(PermissionError):
    """A disallowed Apex capability was requested (e.g. anonymous Apex)."""


@dataclass(frozen=True)
class ApexRestResource:
    """A single authorized custom Apex REST method, as a COMPLETE descriptor.

    All four fields together identify the capability: ``class_name`` and
    ``method_name`` name the method, ``url_mapping`` is the class's
    ``@RestResource`` mapping, and ``http_annotation`` is the one HTTP verb
    annotation the method carries. The authorized unit is this whole tuple --
    not the ``Class.method`` identifier alone -- so re-pointing an authorized
    method at a different verb or path is a DIFFERENT (unauthorized) descriptor,
    not the same authorized one wearing a new hat.

    An instance exists ONLY for a descriptor that matched the deployment's
    authorized set exactly in :func:`register_authorized_rest_resource`, so
    holding one is proof that this exact ``(class, method, urlMapping,
    annotation)`` capability was authorized.
    """

    class_name: str
    method_name: str
    url_mapping: str
    http_annotation: str


#: What the allow-list may carry: a full :class:`ApexRestResource`, or a
#: 4-tuple / 4-field mapping spelling the same ``(class, method, url_mapping,
#: http_annotation)``. Every form is normalized to the same 4-tuple key so the
#: comparison is over the complete descriptor, never a partial identifier.
AuthorizedDescriptor = Union[
    ApexRestResource,
    "tuple[str, str, str, str]",
    "dict[str, str]",
]

_DESCRIPTOR_FIELDS = ("class_name", "method_name", "url_mapping", "http_annotation")


def _descriptor_key(descriptor: AuthorizedDescriptor) -> tuple[str, str, str, str]:
    """Normalize any accepted allow-list entry to its 4-tuple identity.

    Raises :class:`ApexCapabilityError` for a malformed entry so a truncated or
    partial allow-list entry cannot silently widen what is considered
    authorized (e.g. a 2-tuple that would only pin class+method).
    """

    if isinstance(descriptor, ApexRestResource):
        return astuple(descriptor)
    if isinstance(descriptor, dict):
        missing = [f for f in _DESCRIPTOR_FIELDS if not descriptor.get(f)]
        if missing:
            raise ApexCapabilityError(
                "an authorized Apex descriptor needs all four of "
                f"{list(_DESCRIPTOR_FIELDS)}; entry is missing/empty {missing}"
            )
        return tuple(str(descriptor[f]) for f in _DESCRIPTOR_FIELDS)  # type: ignore[return-value]
    if isinstance(descriptor, tuple):
        if len(descriptor) != 4 or not all(descriptor):
            raise ApexCapabilityError(
                "an authorized Apex descriptor tuple must be a full, non-empty "
                "(class_name, method_name, url_mapping, http_annotation)"
            )
        return tuple(str(part) for part in descriptor)  # type: ignore[return-value]
    raise ApexCapabilityError(
        f"unrecognized authorized-descriptor entry {descriptor!r}; expected an "
        "ApexRestResource, a 4-tuple, or a 4-field mapping"
    )


def register_authorized_rest_resource(
    *,
    class_name: str,
    method_name: str,
    url_mapping: str,
    http_annotation: str,
    authorized_methods: Iterable[AuthorizedDescriptor],
) -> ApexRestResource:
    """Register one authorized ``@RestResource`` method, or refuse.

    ``authorized_methods`` is the allow-list of COMPLETE descriptors a
    deployment has explicitly authorized -- each an :class:`ApexRestResource`
    (or an equivalent 4-tuple / 4-field mapping) spelling out ``(class_name,
    method_name, url_mapping, http_annotation)``. The candidate is authorized
    ONLY when its whole 4-tuple matches an approved descriptor exactly. This is
    the fix for the earlier gap where only the ``Class.method`` identifier was
    checked while ``url_mapping`` / ``http_annotation`` were merely shape-
    validated: an authorized method re-pointed at a different verb or path is a
    different descriptor and is now refused, so the returned token can never
    stand for an unapproved capability.

    Shape validation (a real Apex verb annotation, a non-empty ``/`` path) runs
    first as a precondition, but it is NOT the authorization -- the exact
    descriptor match is. Returns an :class:`ApexRestResource` token on success.
    """

    if not class_name or not method_name:
        raise ApexCapabilityError("an Apex REST method needs a class and method name")
    if not url_mapping or not url_mapping.startswith("/"):
        raise ApexCapabilityError(
            f"@RestResource urlMapping must be a non-empty path, got {url_mapping!r}"
        )
    if http_annotation not in _ALLOWED_HTTP_ANNOTATIONS:
        raise ApexCapabilityError(
            f"{http_annotation!r} is not an Apex REST HTTP annotation; expected one "
            f"of {sorted(_ALLOWED_HTTP_ANNOTATIONS)}"
        )
    candidate = ApexRestResource(
        class_name=class_name,
        method_name=method_name,
        url_mapping=url_mapping,
        http_annotation=http_annotation,
    )
    approved = {_descriptor_key(entry) for entry in authorized_methods}
    if astuple(candidate) not in approved:
        raise ApexCapabilityError(
            f"Apex REST capability {astuple(candidate)!r} is not in the deployment's "
            "authorized descriptor allow-list; the full (class, method, urlMapping, "
            "annotation) descriptor must match exactly -- the @RestResource annotation "
            "alone, or a matching class.method with a different verb/path, does not "
            "authorize it"
        )
    return candidate


def reject_execute_anonymous(*_args: object, **_kwargs: object) -> None:
    """Always refuse anonymous Apex execution.

    ``executeAnonymous`` runs arbitrary, unbounded Apex in system mode and is a
    categorically higher-privilege capability than a named ``@RestResource``
    method. The L1 core does not implement it under any argument, and this
    function exists precisely so the refusal is an explicit, testable code path
    rather than an absence a caller might mistake for "not yet implemented".
    """

    raise ApexCapabilityError(
        "anonymous Apex (executeAnonymous) is not a supported capability: it runs "
        "arbitrary Apex in system mode, bypassing FLS/object permissions, and is "
        "categorically distinct from an authorized @RestResource method. Register a "
        "specific @RestResource method via register_authorized_rest_resource instead."
    )
