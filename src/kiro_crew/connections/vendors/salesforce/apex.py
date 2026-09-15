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

from dataclasses import dataclass
from typing import Iterable

#: The HTTP method annotations a custom Apex REST method may carry.
_ALLOWED_HTTP_ANNOTATIONS = frozenset({"HttpGet", "HttpPost", "HttpPut", "HttpPatch", "HttpDelete"})


class ApexCapabilityError(PermissionError):
    """A disallowed Apex capability was requested (e.g. anonymous Apex)."""


@dataclass(frozen=True)
class ApexRestResource:
    """A single authorized custom Apex REST method.

    ``class_name`` and ``method_name`` name the specific method; ``url_mapping``
    is the class's ``@RestResource`` mapping; ``http_annotation`` is the one HTTP
    verb annotation the method carries. An instance exists ONLY for a method that
    passed :func:`register_authorized_rest_resource`, so holding one is proof of
    authorization.
    """

    class_name: str
    method_name: str
    url_mapping: str
    http_annotation: str


def register_authorized_rest_resource(
    *,
    class_name: str,
    method_name: str,
    url_mapping: str,
    http_annotation: str,
    authorized_methods: Iterable[str],
) -> ApexRestResource:
    """Register one authorized ``@RestResource`` method, or refuse.

    ``authorized_methods`` is the allow-list of ``"Class.method"`` identifiers a
    deployment has explicitly authorized. A method not in that list is refused
    with :class:`ApexCapabilityError` -- the core never registers an Apex method
    on the strength of the annotation alone; authorization is a separate,
    explicit fact the caller supplies.

    Validates that ``http_annotation`` is a real Apex REST verb annotation and
    that ``url_mapping`` is non-empty. Returns an :class:`ApexRestResource`
    token on success.
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
    identifier = f"{class_name}.{method_name}"
    if identifier not in set(authorized_methods):
        raise ApexCapabilityError(
            f"Apex REST method {identifier!r} is not in the deployment's authorized "
            "method allow-list; the @RestResource annotation alone does not authorize it"
        )
    return ApexRestResource(
        class_name=class_name,
        method_name=method_name,
        url_mapping=url_mapping,
        http_annotation=http_annotation,
    )


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
