"""``x-crew-not-served-yet`` on ``POST /v1/responses`` against what the route answers.

The operation's ``x-crew-not-served-yet`` mark in ``crew-public.yaml`` names the parts
of the request and the response the spec describes and the gateway does not serve yet:

* ``request-body``: fields the route answers ``422 capability_unavailable``. A name is
  a path into the request body: ``background``, ``tool_choice=none``,
  ``input[].content[].type=input_image``. A name covers every path under it.
* ``request-headers``: request headers the route does not act on.
* ``response-headers``: response headers the route does not send.

Nothing here lists a field by hand. The request probes are built from the request
schema in the spec, one minimal valid request per field and per variant of a field,
and each is sent to the route. A probe the mark covers must be answered
``422 capability_unavailable`` and any other probe must not be, so a field the route
starts or stops serving fails here until the mark says so.

Changing what the route serves: ``docs/system-specs/modules/route-access.md``
("A Crew API route").

``GET /v1/models`` carries the mark for its response headers only, and one test here
holds it the same way.

The route runs in process over the stand-in turn of ``test_crew_api_responses.py``.
The schemas in the unit tests at the end are invented.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Iterator, Mapping
from typing import Any

import pytest
import yaml
from test_crew_api_responses import (  # noqa: F401 -- fixtures
    AS_OWNER,
    ASK,
    COMPONENTS,
    OPERATION,
    PATH,
    SPEC_DIR,
    _hermetic_home,
    client,
)

MARK: Mapping[str, list[str]] = OPERATION.get("x-crew-not-served-yet", {})
REQUEST_SCHEMA = OPERATION["requestBody"]["content"]["application/json"]["schema"]
PARTS = frozenset({"request-body", "request-headers", "response-headers"})
NOT_SERVED = (422, "capability_unavailable")

#: A request header whose effect no response shows, and why. Such a header can be in
#: neither state the checks below tell apart, so it is named here and not in the mark.
NO_VISIBLE_EFFECT = {
    "Crew-API-Version": "only a declaration: the contract answers every minor version alike",
}

#: A probe the route refuses for the probe's own shape, and how. The minimal request
#: for these fields is one message that is not from the user, and the route asks for
#: a user message with text.
#: shortcut: these three roles are not judged here, served or not; upgrade by probing
#: each as a user message plus one message of the role.
REFUSED_FOR_ITS_OWN_SHAPE = {
    "input[].role=assistant": "400 invalid_request",
    "input[].role=developer": "400 invalid_request",
    "input[].role=system": "400 invalid_request",
}

MODELS = yaml.safe_load((SPEC_DIR / "crew-public.yaml").read_text(encoding="utf-8"))["paths"][
    "/v1/models"
]["get"]

Variant = tuple[str, Any]


def _deref(schema: Any, components: Mapping[str, Any]) -> Any:
    while isinstance(schema, dict) and "$ref" in schema:
        node: Any = {"components": components}
        for part in schema["$ref"].split("#/", 1)[1].split("/"):
            node = node[part]
        schema = node
    return schema


def _text(schema: Mapping[str, Any]) -> str:
    """A string the schema's ``pattern`` or ``format`` accepts."""
    if schema.get("format") == "uri":
        return "https://example.invalid/x"
    pattern = schema.get("pattern", "")
    prefix = (
        pattern[1:] if pattern.startswith("^") and pattern[1:].replace("_", "").isalnum() else ""
    )
    return prefix + "x"


def _variants(schema: Any, components: Mapping[str, Any]) -> list[Variant]:
    """Minimal valid values of *schema*, one per variant, each with its path suffix.

    The suffix says which variant it is: ``=value`` for a constant or an enum value,
    ``[]`` for an array item, ``.name`` for a property of an object. The first variant
    of an object holds its required properties alone; each further one adds one
    optional property, or gives a required property another of its variants.
    """
    schema = _deref(schema, components)
    for key in ("oneOf", "anyOf"):
        if key in schema:
            return [found for option in schema[key] for found in _variants(option, components)]
    if "const" in schema:
        return [(f"={_word(schema['const'])}", schema["const"])]
    if "enum" in schema:
        return [(f"={_word(value)}", value) for value in schema["enum"]]
    kind = schema.get("type", "object" if "properties" in schema else None)
    if kind == "boolean":
        return [("=true", True)]
    if kind == "string":
        return [("", _text(schema))]
    if kind in ("number", "integer"):
        return [("", 1)]
    if kind == "array":
        return [
            (f"[]{suffix}", [value]) for suffix, value in _variants(schema["items"], components)
        ]
    if kind == "object":
        return _object_variants(schema, components)
    raise AssertionError(f"no sample value for the schema {schema!r}: teach _variants its type")


def _object_variants(schema: Mapping[str, Any], components: Mapping[str, Any]) -> list[Variant]:
    properties = schema.get("properties", {})
    required = [name for name in schema.get("required", ()) if name in properties]
    choices = {name: _variants(properties[name], components) for name in properties}
    base = {name: choices[name][0][1] for name in required}
    # The constants among the required properties say which object this is.
    tag = "".join(
        f".{name}{choices[name][0][0]}"
        for name in required
        if "const" in _deref(properties[name], components)
    )
    found: list[Variant] = [(tag, base)]
    for name, options in choices.items():
        for suffix, value in options[1:] if name in required else options:
            found.append((f"{tag}.{name}{suffix}", {**copy.deepcopy(base), name: value}))
    return found


def _word(value: Any) -> str:
    """A constant as the spec writes it: a string as it is, any other value as JSON."""
    return value if isinstance(value, str) else json.dumps(value)


def _probes(
    schema: Any, components: Mapping[str, Any], base: Mapping[str, Any]
) -> Iterator[Variant]:
    """One request per field and variant of the request *schema*: its path and its body."""
    for suffix, body in _variants(schema, components):
        path = suffix.lstrip(".")
        if path:  # the first variant is the base request itself
            field = re.split(r"[.\[=]", path, maxsplit=1)[0]
            yield path, {**base, field: body[field]}


def _covered(path: str, names: list[str]) -> bool:
    """Whether a name of the mark is *path* or a path above it."""
    return any(
        path == name or (path.startswith(name) and path[len(name)] in ".[=") for name in names
    )


def _drift(answers: Mapping[str, bool], names: list[str]) -> dict[str, list[str]]:
    """What disagrees, by kind. *answers* maps each probe's path to whether it was refused."""
    return {
        "refused 422 capability_unavailable, and not under a request-body name": sorted(
            path for path, refused in answers.items() if refused and not _covered(path, names)
        ),
        "under a request-body name, and not refused 422 capability_unavailable": sorted(
            path for path, refused in answers.items() if not refused and _covered(path, names)
        ),
        "a request-body name no field of the request schema is under": sorted(
            name for name in names if not any(_covered(path, [name]) for path in answers)
        ),
    }


async def _answer(
    api: Any, body: Any, headers: Mapping[str, str] | None = None
) -> tuple[int, str | None, Mapping[str, str]]:
    """The status, the error code when the answer is an error, and the headers."""
    response = await api.post(PATH, json=body, headers={**AS_OWNER, **(headers or {})})
    payload = await response.read()  # a stream is read to its end
    if response.status >= 400:
        return response.status, json.loads(payload)["error"]["code"], response.headers
    return response.status, None, response.headers


def test_the_mark_holds_only_the_parts_it_defines() -> None:
    assert set(MARK) <= PARTS
    assert all(isinstance(names, list) and names == sorted(set(names)) for names in MARK.values())


@pytest.mark.asyncio
async def test_the_request_fields_the_route_refuses_are_the_ones_the_mark_names(
    client,  # noqa: F811
) -> None:
    answers: dict[str, bool] = {}
    other_refusals: dict[str, str] = {}
    for path, body in _probes(REQUEST_SCHEMA, COMPONENTS["components"], ASK):
        status, code, _headers = await _answer(client, body)
        refused = (status, code) == NOT_SERVED
        # Two shapes of one variant share a path, and the route answers both alike.
        assert answers.setdefault(path, refused) == refused, f"{path} is refused in one shape only"
        if status >= 400 and not refused:
            other_refusals[path] = f"{status} {code}"
    # A probe refused for another reason proves nothing about its field, so each one is
    # named with its reason and none is counted as served by accident.
    assert other_refusals == REFUSED_FOR_ITS_OWN_SHAPE, (
        "a probe of POST /v1/responses is refused, and not 422 capability_unavailable: "
        "serve it, name it in x-crew-not-served-yet, or list it in REFUSED_FOR_ITS_OWN_SHAPE"
    )
    fields = _deref(REQUEST_SCHEMA, COMPONENTS["components"])["properties"]
    assert len(answers) > len(fields), "the request schema gave fewer probes than it has fields"

    drift = {
        kind: paths
        for kind, paths in _drift(answers, MARK.get("request-body", [])).items()
        if paths
    }
    assert not drift, (
        "x-crew-not-served-yet on POST /v1/responses and the route disagree "
        "(docs/system-specs/modules/route-access.md, 'A Crew API route'):\n"
        + "\n".join(f"{kind}:\n  " + "\n  ".join(paths) for kind, paths in drift.items())
    )


@pytest.mark.asyncio
async def test_the_response_headers_the_route_leaves_out_are_the_ones_the_mark_names(
    client,  # noqa: F811
) -> None:
    declared = set(OPERATION["responses"]["200"]["headers"])
    named = set(MARK.get("response-headers", []))
    assert named <= declared, "a response-headers name the 200 response does not declare"

    for body in (ASK, {**ASK, "model": ""}):  # an answer and a refusal
        status, _payload, headers = await _answer(client, body)
        absent = {name for name in declared if name not in headers}
        assert absent == named, (
            f"response-headers and a {status} from POST /v1/responses disagree. "
            f"Not sent, and not named: {sorted(absent - named)}. "
            f"Named, and sent: {sorted(named - absent)}."
        )


@pytest.mark.asyncio
async def test_the_response_headers_get_v1_models_leaves_out_are_the_ones_its_mark_names(
    client,  # noqa: F811
) -> None:
    mark = MODELS.get("x-crew-not-served-yet", {})
    assert set(mark) <= {"response-headers"}, "GET /v1/models has no request body to mark"
    declared = set(MODELS["responses"]["200"]["headers"])
    named = set(mark.get("response-headers", []))

    response = await client.get("/v1/models", headers=AS_OWNER)
    assert response.status == 200
    absent = {name for name in declared if name not in response.headers}
    assert absent == named, (
        "response-headers and a 200 from GET /v1/models disagree. "
        f"Not sent, and not named: {sorted(absent - named)}. "
        f"Named, and sent: {sorted(named - absent)}."
    )


async def _same_key_runs_once(api: Any) -> bool:
    """Whether a second request with the same ``Idempotency-Key`` is not run again."""
    before = len(api.turn.entered)
    for _ in range(2):
        status, _payload, _headers = await _answer(api, ASK, {"Idempotency-Key": "key-1"})
        assert status == 200
    return len(api.turn.entered) - before == 1


async def _request_id_is_kept(api: Any) -> bool:
    _status, _payload, headers = await _answer(api, ASK, {"X-Request-Id": "req_mine"})
    return headers["X-Request-Id"] == "req_mine"


#: How a response shows that the route acts on a request header.
HEADER_PROBES = {"Idempotency-Key": _same_key_runs_once, "X-Request-Id": _request_id_is_kept}


@pytest.mark.asyncio
async def test_the_request_headers_the_route_does_not_act_on_are_the_ones_the_mark_names(
    client,  # noqa: F811
) -> None:
    parameters = [
        _deref(parameter, COMPONENTS["components"]) for parameter in OPERATION["parameters"]
    ]
    declared = {parameter["name"] for parameter in parameters if parameter["in"] == "header"}
    assert declared == set(HEADER_PROBES) | set(NO_VISIBLE_EFFECT), (
        "a request header of POST /v1/responses has no probe here: add one to HEADER_PROBES, "
        "or name it in NO_VISIBLE_EFFECT with the reason no response can show it"
    )

    ignored = {name for name, acted_on in HEADER_PROBES.items() if not await acted_on(client)}
    named = set(MARK.get("request-headers", []))
    assert ignored == named, (
        "request-headers and POST /v1/responses disagree. "
        f"Not acted on, and not named: {sorted(ignored - named)}. "
        f"Named, and acted on: {sorted(named - ignored)}."
    )


# ── the walk and the comparison, on invented schemas ─────────────────────────


def test_a_schema_gives_one_probe_per_field_and_variant() -> None:
    components = {
        "schemas": {
            "Part": {
                "oneOf": [
                    {
                        "type": "object",
                        "required": ["type"],
                        "properties": {"type": {"const": "plain"}},
                    },
                    {
                        "type": "object",
                        "required": ["type", "url"],
                        "properties": {
                            "type": {"const": "picture"},
                            "url": {"type": "string", "format": "uri"},
                        },
                    },
                ]
            }
        }
    }
    schema = {
        "type": "object",
        "required": ["name"],
        "properties": {
            "name": {"type": "string"},
            "fast": {"type": "boolean"},
            "mode": {"enum": ["auto", "none"]},
            "ref": {"type": "string", "pattern": "^ref_"},
            "parts": {"type": "array", "items": {"$ref": "#/components/schemas/Part"}},
            "look": {"type": "object", "properties": {"size": {"type": "integer"}}},
        },
    }

    probes = dict(_probes(schema, components, {"name": "base"}))

    assert probes == {
        "fast=true": {"name": "base", "fast": True},
        "mode=auto": {"name": "base", "mode": "auto"},
        "mode=none": {"name": "base", "mode": "none"},
        "ref": {"name": "base", "ref": "ref_x"},
        "parts[].type=plain": {"name": "base", "parts": [{"type": "plain"}]},
        "parts[].type=picture": {
            "name": "base",
            "parts": [{"type": "picture", "url": "https://example.invalid/x"}],
        },
        "look": {"name": "base", "look": {}},
        "look.size": {"name": "base", "look": {"size": 1}},
    }


def test_a_name_covers_its_own_path_and_the_paths_under_it() -> None:
    names = ["mode=none", "parts", "look.size"]

    assert _covered("mode=none", names) and not _covered("mode=auto", names)
    assert _covered("parts[].type=plain", names) and not _covered("partsmore", names)
    assert _covered("look.size", names) and not _covered("look", names)


def test_each_kind_of_drift_is_reported() -> None:
    answers = {"fast=true": True, "mode=none": True, "mode=auto": False, "ref": False}

    assert _drift(answers, ["mode=none", "ref", "gone"]) == {
        "refused 422 capability_unavailable, and not under a request-body name": ["fast=true"],
        "under a request-body name, and not refused 422 capability_unavailable": ["ref"],
        "a request-body name no field of the request schema is under": ["gone"],
    }
    assert not any(_drift(answers, ["fast", "mode=none"]).values())
