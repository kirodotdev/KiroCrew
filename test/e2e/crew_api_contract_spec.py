"""Contract cases for the Crew API, and what the spec says about each operation.

The spec is the oracle (``docs/request-for-change/assets/crew-api/`` and the error
table in ``docs/request-for-change/rfc-crew-api.md``). ``e2e/test_crew_api_contract.py``
boots one gateway, sends every case over HTTP and holds the answer to what the spec
says about that operation: the status, the headers, the body's schema and the error's
row in the table.

To add an operation: add ``e2e/crew_api_contract_cases/<operationId>.py`` with a
``CASES`` list. Every operation also gets :data:`COMMON_CASES`, so a file lists only
what is its own. ``test_crew_api_contract_coverage.py`` fails when an operation the
spec marks ``x-crew-status: available`` has no file there.

When the gateway and the spec disagree, the case fails. Do not write the gateway's
answer into the case: fix one of the two, or name the difference in
``crew_api_contract_differences.py`` with its reason, where it stays visible until it
is fixed.

The modules carry long names on purpose: the related-test selector matches a changed
module's bare stem against every test file (``scripts/run_scoped_tests.py``).
"""

from __future__ import annotations

import functools
import importlib.util
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

#: The machine owner: a session this gateway signed, sent as a Bearer.
OWNER = "owner"
#: An app the owner approved through local authorization, with its access token.
APP = "app"
#: No credential.
ANONYMOUS = "anonymous"
#: A Bearer this gateway never signed.
INVALID = "invalid"
CALLERS = (OWNER, APP, ANONYMOUS, INVALID)

#: ``Case.json`` when the request has no body.
NO_BODY: Any = object()


@dataclass(frozen=True)
class Case:
    """One call and the answer the spec gives for it.

    ``json`` and ``path`` may be a function of the booted gateway, for a value only a
    running gateway has (a model id, the id of something the case made first).
    ``before`` runs on the booted gateway ahead of the call, to make what the call needs.
    ``check`` receives the parsed body after every spec check passed.
    """

    name: str
    caller: str
    status: int
    code: str = ""
    json: Any = NO_BODY
    path: Mapping[str, str] | Callable[[Any], Mapping[str, str]] = field(default_factory=dict)
    check: Callable[[Any], None] | None = None
    before: Callable[[Any], None] | None = None


#: Sent to every operation: the two refusals a credential check gives.
COMMON_CASES = (
    Case("a call with no credential is refused", ANONYMOUS, 401, "invalid_token"),
    Case("a credential this gateway did not sign is refused", INVALID, 401, "invalid_token"),
)

CASES_DIR = Path(__file__).resolve().parent / "crew_api_contract_cases"
_DOCS = Path(__file__).resolve().parents[2] / "docs" / "request-for-change"
SPEC_DIR = _DOCS / "assets" / "crew-api"
RFC = _DOCS / "rfc-crew-api.md"
SPEC_FILES = ("crew-public.yaml", "crew-owner.yaml")
#: Where the spec files sit for reference resolution. Never fetched: ``_retrieve`` reads disk.
_SPEC_URI = "https://crew-api-spec.invalid/"
_METHODS = frozenset({"get", "put", "post", "delete", "patch"})
AVAILABLE = "available"
JSON = "application/json"
REQUEST_ID = "X-Request-Id"
VERSION = "Crew-API-Version"
CHALLENGE = "WWW-Authenticate"
# One row of the RFC's error table: | 403 | `permission_error` | `owner_only`, ... | When |
_ERROR_ROW = re.compile(r"^\| (\d{3}) \| `([a-z_]+)` \| ([^|]+) \|")


@dataclass(frozen=True)
class Operation:
    operation_id: str
    method: str
    path: str
    file: str
    status_mark: str
    responses: dict[str, Any]


@dataclass(frozen=True)
class Reply:
    """An HTTP answer: header names are lower-case, ``body`` is the raw bytes."""

    status: int
    headers: dict[str, str]
    body: bytes


@functools.cache
def _document(file: str) -> dict[str, Any]:
    return yaml.safe_load((SPEC_DIR / file).read_text(encoding="utf-8"))


def _resolved(node: Any, file: str) -> tuple[Any, str]:
    """*node* with a top-level ``$ref`` followed, and the file the result lives in."""
    while isinstance(node, dict) and "$ref" in node:
        target, _, pointer = node["$ref"].partition("#")
        file = target.removeprefix("./") or file
        node = _document(file)
        for token in pointer.strip("/").split("/"):
            node = node[token.replace("~1", "/").replace("~0", "~")]
    return node, file


@functools.cache
def operations() -> dict[str, Operation]:
    """Every operation in the spec, by ``operationId``."""
    found: dict[str, Operation] = {}
    for file in SPEC_FILES:
        for path, item in _document(file)["paths"].items():
            for method, raw in item.items():
                if method in _METHODS:
                    found[raw["operationId"]] = Operation(
                        operation_id=raw["operationId"],
                        method=method.upper(),
                        path=path,
                        file=file,
                        status_mark=str(raw.get("x-crew-status", "")),
                        responses=raw["responses"],
                    )
    return found


@functools.cache
def error_rows() -> frozenset[tuple[int, str, str]]:
    """The RFC's error table as ``(status, type, code)`` rows."""
    rows = set()
    for line in RFC.read_text(encoding="utf-8").splitlines():
        match = _ERROR_ROW.match(line)
        if match:
            status, error_type, codes = match.groups()
            rows.update(
                (int(status), error_type, code) for code in re.findall(r"`([a-z_]+)`", codes)
            )
    return frozenset(rows)


@functools.cache
def cases() -> dict[str, tuple[Case, ...]]:
    """Each operation's cases, its own first: one file in ``CASES_DIR`` per operation."""
    found = {}
    for path in sorted(CASES_DIR.glob("*.py")):
        # Loaded by path: the directory is not a package, so it needs no ``__init__.py``.
        module_spec = importlib.util.spec_from_file_location(f"_contract_cases_{path.stem}", path)
        assert module_spec is not None and module_spec.loader is not None, path
        module = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(module)
        found[path.stem] = (*module.CASES, *COMMON_CASES)
    return found


def available(found: Mapping[str, Operation]) -> set[str]:
    """The operations of *found* the spec marks ``x-crew-status: available``."""
    return {name for name, operation in found.items() if operation.status_mark == AVAILABLE}


def case_problems(operation_id: str, case: Case) -> list[str]:
    """What is wrong with a case before any gateway runs: it must ask for what the spec documents."""
    operation = operations().get(operation_id)
    if operation is None:
        return [f"{operation_id} is not an operationId in the spec"]
    problems = []
    if case.caller not in CALLERS:
        problems.append(f"unknown caller {case.caller!r}")
    if str(case.status) not in operation.responses:
        problems.append(f"the spec documents no {case.status} for {operation_id}")
    if (case.status >= 400) != bool(case.code):
        problems.append("an error case names its code, and no other case does")
    if case.code and (case.status, case.code) not in {(s, c) for s, _, c in error_rows()}:
        problems.append(f"the RFC's error table has no row {case.status} {case.code}")
    return problems


def _retrieve(uri: str) -> Resource:
    return Resource.from_contents(
        _document(uri.removeprefix(_SPEC_URI)), default_specification=DRAFT202012
    )


_REGISTRY: Registry = Registry(retrieve=_retrieve)


def _validator(schema: Any, file: str) -> Draft202012Validator:
    """A validator for *schema*, a node of spec file *file*.

    The root is that file with the schema hung on it, so a ``#/components/...`` reference
    resolves in the file the schema came from and a ``./components.yaml#/...`` one in
    its sibling.
    """
    root = {
        **_document(file),
        "$id": _SPEC_URI + file,
        "$defs": {"_": schema},
        "$ref": "#/$defs/_",
    }
    return Draft202012Validator(root, registry=_REGISTRY)


def _schema_problems(schema: Any, file: str, value: Any, what: str) -> list[str]:
    return [
        f"{what}: {'/'.join(str(part) for part in error.absolute_path) or '<root>'}: "
        f"{error.message[:200]}"
        for error in _validator(schema, file).iter_errors(value)
    ][:8]


def reply_problems(
    operation: Operation, case: Case, reply: Reply, sent_request_id: str
) -> list[str]:
    """Every way *reply* differs from the case and from what the spec says for its status."""
    problems: list[str] = []
    try:
        body = json.loads(reply.body)
    except ValueError:
        body = None
    code = (
        body.get("error", {}).get("code")
        if isinstance(body, dict) and isinstance(body.get("error"), dict)
        else None
    )
    if reply.status != case.status:
        problems.append(f"status {reply.status} (code {code!r}), the case expects {case.status}")
    documented = operation.responses.get(str(reply.status))
    if documented is None:
        return [*problems, f"the spec documents no {reply.status} for {operation.operation_id}"]
    response, file = _resolved(documented, operation.file)

    # Headers. Every answer carries the request id and the version; a 401 its challenge.
    required = [REQUEST_ID, VERSION] + ([CHALLENGE] if reply.status == 401 else [])
    for name in required:
        if name not in response.get("headers", {}):
            problems.append(f"the spec's {reply.status} response does not list the {name} header")
        elif name.lower() not in reply.headers:
            problems.append(f"no {name} header")
    for name, header in response.get("headers", {}).items():
        if name.lower() in reply.headers:
            described, header_file = _resolved(header, file)
            problems += _schema_problems(
                described.get("schema", {}),
                header_file,
                reply.headers[name.lower()],
                f"{name} header",
            )
    if reply.headers.get(REQUEST_ID.lower()) != sent_request_id:
        problems.append(f"{REQUEST_ID} is not the id the request sent")

    # Body.
    content = response.get("content", {})
    if JSON not in content:
        problems.append(f"the spec's {reply.status} response has no {JSON} body")
    elif not reply.headers.get("content-type", "").startswith(JSON):
        problems.append(f"Content-Type {reply.headers.get('content-type')!r}, not {JSON}")
    elif body is None:
        problems.append("the body is not JSON")
    else:
        problems += _schema_problems(content[JSON]["schema"], file, body, "body")

    # An error is a row of the RFC's table, and names the request.
    if reply.status >= 400 and isinstance(body, dict) and isinstance(body.get("error"), dict):
        error = body["error"]
        if error.get("code") != case.code:
            problems.append(f"code {error.get('code')!r}, the case expects {case.code!r}")
        if (reply.status, error.get("type"), error.get("code")) not in error_rows():
            problems.append(
                f"the RFC's error table has no row {reply.status} {error.get('type')} {error.get('code')}"
            )
        if error.get("request_id") != reply.headers.get(REQUEST_ID.lower()):
            problems.append(f"error.request_id is not the {REQUEST_ID} header")
    return problems
