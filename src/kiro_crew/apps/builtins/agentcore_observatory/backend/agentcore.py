"""AgentCore Observatory — read-only control-plane reads, driven by the catalog.

One code path serves all 27 resource types: :func:`list_resource` looks the type
up in :mod:`.catalog` and builds the argv from that row. There is deliberately no
per-type function, because a hand-written function per type is 27 places for a
response key to be wrong and only one of them gets a test.

Every call goes through :func:`kiro_crew.cloud.aws.run_aws`, the ``cloud``
package's single subprocess chokepoint: credentials are resolved by the ``aws``
CLI's own provider chain from the profile name, and the argv is sandbox-wrapped.
Nothing here reads a credential file and nothing here mutates AWS state — the
verbs are ``list-*`` and ``get-*`` only.

**Agent sessions are refused, by design.** The chokepoint's
``assert_chokepoint_allowed`` admits only an explicit ``(service, operation)``
allowlist when ``KIROCREW_SESSION_KEY`` is set, and no AgentCore verb is on it.
The gateway process does not carry that variable, so the dashboard's own page
reads fine while an agent that imports this module gets
:class:`~kiro_crew.cloud.aws.CloudActionDenied`. Widening that allowlist is a
deliberate change to a core security surface and is NOT bundled here.

Two failure modes are silent in the underlying API and are therefore reported
explicitly rather than folded into a number:

* **A non-zero CLI exit is not an empty result.** An expired SSO session, a
  missing IAM permission, and an account with zero resources all produce
  "nothing to show" if only stdout is parsed. Each read carries its error text so
  the caller can distinguish "not authorized" from "none deployed" — the
  distinction that decides whether the user fixes credentials or deploys
  something.
* **A truncated page is not a total.** Callers that aggregate must check
  :attr:`ListResult.truncated`, set when the bounded pagination loop stopped with
  a ``nextToken`` still outstanding.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from kiro_crew.apps.builtins.agentcore_observatory.backend import catalog
from kiro_crew.apps.builtins.agentcore_observatory.backend.config import ObservatoryConfig
from kiro_crew.cloud.aws import CloudActionDenied, run_aws

logger = logging.getLogger(__name__)

#: The control-plane service name as the CLI spells it.
SERVICE = "bedrock-agentcore-control"

#: Per-call CLI timeout. A control-plane list is a single fast API call; a longer
#: wait means the CLI is stuck resolving credentials (an SSO prompt it can never
#: satisfy non-interactively), which should surface as an error rather than hold
#: the page open.
_TIMEOUT_SECS = 20

#: Bound on the pagination loop. The AWS CLI paginates on its own, so a
#: ``nextToken`` coming back at all is already unusual; this caps the damage of a
#: server that returns one forever instead of trusting it to terminate.
_MAX_PAGES = 20

#: Cap on one identifier taken from a request path or a parent row before it is
#: placed in an argv. Real AgentCore ids are far shorter.
_MAX_ID_LEN = 256


@dataclass
class ListResult:
    """One paginated read.

    ``ok`` is False only for a genuine failure. An authorized account with no
    resources is ``ok=True`` with an empty ``items`` — callers must not treat
    emptiness as an error, nor an error as emptiness.
    """

    ok: bool
    items: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""
    denied: bool = False
    truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "items": self.items,
            "error": self.error,
            "denied": self.denied,
            "truncated": self.truncated,
        }


@dataclass
class ObjectResult:
    """One ``get-*`` read: a single object rather than a list."""

    ok: bool
    item: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    denied: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "item": self.item, "error": self.error, "denied": self.denied}


def describe_denial(exc: CloudActionDenied) -> str:
    """Render a chokepoint refusal as guidance instead of a stack trace."""
    return (
        "AgentCore reads are not permitted from an agent session; open the "
        f"Observatory page in the dashboard instead. ({exc})"
    )


def safe_identifier(value: str) -> bool:
    """Whether ``value`` may be placed in an argv as a resource identifier.

    Deliberately narrow. Identifiers reach here from a URL path and from parent
    rows, so this refuses anything that could read as an option (a leading dash)
    or carry shell-significant text, even though the value is passed as its own
    argv entry and never interpolated into a shell string.
    """
    if not value or len(value) > _MAX_ID_LEN:
        return False
    if value.startswith("-"):
        return False
    return all(ch.isalnum() or ch in "._:/-" for ch in value)


def _call(cfg: ObservatoryConfig, args: list[str]) -> tuple[dict[str, Any] | None, str, bool]:
    """Run one CLI call. Returns ``(payload, error, denied)``.

    ``payload`` is None whenever ``error`` is set. A zero exit with unparseable
    stdout is an error, not an empty payload: silently substituting ``{}`` would
    report a broken CLI as an account with nothing in it.
    """
    try:
        rc, out, err = run_aws(args, cfg.profile, cfg.region, timeout=_TIMEOUT_SECS)
    except CloudActionDenied as exc:
        return None, describe_denial(exc), True
    except Exception as exc:  # noqa: BLE001 - a broken CLI must not 500 the page
        logger.debug("agentcore call failed: %s", args[:2], exc_info=True)
        return None, f"could not run the aws CLI: {exc}", False
    if rc != 0:
        detail = (err or out or "").strip()
        return None, detail or f"aws exited {rc}", False
    if not (out or "").strip():
        # A successful call with empty stdout: an empty payload, not an error,
        # since some verbs legitimately print nothing.
        return {}, "", False
    try:
        payload = json.loads(out)
    except json.JSONDecodeError:
        return None, "the aws CLI returned output that is not JSON", False
    if not isinstance(payload, dict):
        return None, "the aws CLI returned JSON that is not an object", False
    return payload, "", False


def _parent_args(rt: catalog.ResourceType, parent_ids: dict[str, str]) -> list[str] | str:
    """Build a child type's parent flags, or return an error string.

    ``parent_ids`` is keyed by the CLI flag so a two-parent type (only
    ``policy-generation-assets``) needs no positional agreement with the caller.
    """
    args: list[str] = []
    for param in rt.parent_params:
        value = parent_ids.get(param, "")
        if not value:
            return f"{rt.id} requires {param}"
        if not safe_identifier(value):
            return f"{param} is not a well-formed identifier"
        args += [param, value]
    return args


def list_resource(
    cfg: ObservatoryConfig,
    type_id: str,
    parent_ids: dict[str, str] | None = None,
) -> ListResult:
    """List one resource type, collecting pages up to :data:`_MAX_PAGES`.

    A child type needs its parent flags in ``parent_ids``; omitting them is a
    caller error reported as such rather than a call with a missing argument.
    """
    rt = catalog.by_id(type_id)
    if rt is None:
        return ListResult(ok=False, error=f"unknown resource type {type_id!r}")
    if not rt.listable:
        return ListResult(ok=False, error=f"{rt.id} has no list operation")
    if not cfg.configured:
        return ListResult(ok=False, error="no AWS region is configured")

    built = _parent_args(rt, parent_ids or {})
    if isinstance(built, str):
        return ListResult(ok=False, error=built)

    base = [SERVICE, rt.list_verb] + built
    items: list[dict[str, Any]] = []
    token = ""
    for _ in range(_MAX_PAGES):
        args = list(base) + (["--next-token", token] if token else [])
        payload, error, denied = _call(cfg, args)
        if payload is None:
            return ListResult(ok=False, error=error, denied=denied)
        chunk = payload.get(rt.list_key)
        if isinstance(chunk, list):
            items += [row for row in chunk if isinstance(row, dict)]
        token = str(payload.get("nextToken", "") or "")
        if not token:
            return ListResult(ok=True, items=items)
    # Fell out with a token still pending: report the partial set as partial. A
    # caller aggregating over `items` must not read this as a total.
    return ListResult(ok=True, items=items, truncated=True)


def get_resource(
    cfg: ObservatoryConfig,
    type_id: str,
    id_args: dict[str, str] | None = None,
) -> ObjectResult:
    """Fetch one object with the type's ``get-*`` verb.

    ``id_args`` is keyed by CLI flag. The singleton ``token-vault`` takes none,
    which is why an empty mapping is valid rather than a caller error.
    """
    rt = catalog.by_id(type_id)
    if rt is None:
        return ObjectResult(ok=False, error=f"unknown resource type {type_id!r}")
    if not rt.get_verb:
        return ObjectResult(ok=False, error=f"{rt.id} has no get operation")
    if not cfg.configured:
        return ObjectResult(ok=False, error="no AWS region is configured")

    args = [SERVICE, rt.get_verb]
    for param, value in sorted((id_args or {}).items()):
        if not param.startswith("--") or not safe_identifier(value):
            return ObjectResult(ok=False, error=f"{param} is not a well-formed identifier")
        args += [param, value]

    payload, error, denied = _call(cfg, args)
    if payload is None:
        return ObjectResult(ok=False, error=error, denied=denied)
    return ObjectResult(ok=True, item=payload)
