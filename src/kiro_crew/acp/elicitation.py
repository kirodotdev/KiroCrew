"""MCP elicitation as it reaches the ACP client, on both wires.

An MCP server that needs the person to confirm or fill something in sends its
agent an ``elicitation/create``. The agent cannot answer it -- only a client with
a human can -- so the harness turns it around as a server->client request:

* Standard ACP: the client method ``elicitation/create``
  (``METHOD_ELICITATION_CREATE``), with the elicitation fields FLAT on
  ``params`` next to ``sessionId`` / ``toolCallId``.
* KAS: the extension method ``_kiro/mcp/elicitation``
  (``METHOD_KAS_MCP_ELICITATION``), with the same fields NESTED under
  ``params.elicitation``.

Both expect the same answer: ``{"action": "accept" | "decline" | "cancel"}``,
plus ``"content"`` (the person's answers) for an accepted form. This module owns
the shape: it reads either wire into one :class:`ElicitationRequest`, bounds and
checks what the server asked for, checks the answer a person gives against the
requested schema, and builds the response. The two drivers (``AcpClient`` and
``AcpSessionHandle``) and the dashboard reach the protocol only through it, so a
shape change lands once.

What is refused here and why. A request is PARSED fail-closed: a shape this
module cannot render (an unknown property type, a nested object, a URL that is
not http(s), an oversized message) raises :class:`ElicitationParseError` and the
driver answers ``decline`` -- never ``accept`` on a guess, and never a JSON-RPC
error, which the KAS engine turns into a cancel of the whole tool call. An
answer is VALIDATED the same way: a value that does not fit the requested schema
raises :class:`ElicitationContentError` and nothing is sent, so a server never
receives content its own schema would reject.

An elicitation is a human-in-the-loop requirement the SERVER declared. It is
never auto-accepted by a trust grant: trust answers Crew's own tool gate, and
this is not that gate. With no person reachable the answer is ``decline``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import Any
from urllib.parse import urlsplit

from kiro_crew.acp.types import (
    METHOD_ELICITATION_CREATE,
    METHOD_KAS_MCP_ELICITATION,
    JsonRpcMessage,
)

#: The person accepted; form content rides along.
ELICITATION_ACCEPT = "accept"
#: The person saw the ask and said no.
ELICITATION_DECLINE = "decline"
#: Nobody answered: the turn ended, the wait timed out, or the card went away.
ELICITATION_CANCEL = "cancel"
ELICITATION_ACTIONS = frozenset({ELICITATION_ACCEPT, ELICITATION_DECLINE, ELICITATION_CANCEL})

#: The client renders a form from ``requestedSchema``.
ELICITATION_MODE_FORM = "form"
#: The client sends the person to ``url`` and reports whether they went.
ELICITATION_MODE_URL = "url"
ELICITATION_MODES = frozenset({ELICITATION_MODE_FORM, ELICITATION_MODE_URL})

#: The methods a driver recognises as an elicitation request. Both carry an id
#: and MUST be answered; an unanswered one blocks the tool call that raised it.
ELICITATION_METHODS = frozenset({METHOD_ELICITATION_CREATE, METHOD_KAS_MCP_ELICITATION})

# Bounds on what a server may ask. The server is a third party and the message
# and field labels are rendered to a person, so each is capped like any other
# display text a backend authors; the counts keep one request from building an
# unbounded form.
MESSAGE_MAX_CHARS = 4000
LABEL_MAX_CHARS = 500
FIELD_NAME_MAX_CHARS = 128
PROPERTIES_MAX = 32
ENUM_OPTIONS_MAX = 64
#: A string answer a person types; also bounds every enum value.
STRING_VALUE_MAX_CHARS = 10_000
URL_MAX_CHARS = 2048

#: The primitive property types MCP's elicitation schema allows. Anything else
#: (``object``, ``null``, a missing type) cannot be rendered as a form field.
_PRIMITIVE_TYPES = frozenset({"string", "number", "integer", "boolean"})
#: Field kinds the dashboard form knows how to draw.
FIELD_KIND_STRING = "string"
FIELD_KIND_NUMBER = "number"
FIELD_KIND_INTEGER = "integer"
FIELD_KIND_BOOLEAN = "boolean"
FIELD_KIND_ENUM = "enum"
FIELD_KIND_MULTI_ENUM = "multi_enum"

#: The string formats the MCP elicitation schema names; any other value is dropped.
STRING_FORMATS: frozenset[str] = frozenset({"email", "uri", "date", "date-time"})


class ElicitationParseError(ValueError):
    """The request is not one this client can put to a person."""


class ElicitationContentError(ValueError):
    """The answer does not fit the schema the server asked for."""


@dataclass(frozen=True)
class ElicitationOption:
    """One choice of an enum field: the wire value and the label shown for it."""

    value: str
    label: str


@dataclass(frozen=True)
class ElicitationField:
    """One form field, normalised from an ``ElicitationPropertySchema``.

    ``kind`` is one of the ``FIELD_KIND_*`` constants. ``options`` is set for the
    two enum kinds. ``default`` is the server's suggested value, already checked
    against the field, or ``None``.
    """

    name: str
    kind: str
    title: str = ""
    description: str = ""
    required: bool = False
    options: tuple[ElicitationOption, ...] = ()
    default: Any = None
    minimum: float | None = None
    maximum: float | None = None
    min_length: int | None = None
    max_length: int | None = None
    #: Multi-select bounds on how many choices the answer may carry.
    min_items: int | None = None
    max_items: int | None = None
    #: The schema's ``format`` for a string (``email``, ``uri``, ``date``,
    #: ``date-time``): a rendering hint only, never enforced here.
    format: str = ""

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ElicitationField:
        """The inverse of :meth:`as_dict`; an unknown value is left at its default."""
        options = tuple(
            ElicitationOption(str(o.get("value", "")), str(o.get("label", o.get("value", ""))))
            for o in payload.get("options") or []
            if isinstance(o, dict)
        )
        return cls(
            name=str(payload.get("name") or ""),
            kind=str(payload.get("kind") or FIELD_KIND_STRING),
            title=str(payload.get("title") or ""),
            description=str(payload.get("description") or ""),
            required=bool(payload.get("required")),
            options=options,
            default=payload.get("default"),
            minimum=_number(payload, "minimum"),
            maximum=_number(payload, "maximum"),
            min_length=_int(payload, "min_length"),
            max_length=_int(payload, "max_length"),
            min_items=_int(payload, "min_items"),
            max_items=_int(payload, "max_items"),
            format=_string_format(payload),
        )

    def as_dict(self) -> dict[str, Any]:
        """The field as the dashboard card receives it."""
        out: dict[str, Any] = {"name": self.name, "kind": self.kind, "required": self.required}
        if self.title:
            out["title"] = self.title
        if self.description:
            out["description"] = self.description
        if self.options:
            out["options"] = [{"value": o.value, "label": o.label} for o in self.options]
        if self.default is not None:
            out["default"] = self.default
        if self.minimum is not None:
            out["minimum"] = self.minimum
        if self.maximum is not None:
            out["maximum"] = self.maximum
        if self.min_length is not None:
            out["min_length"] = self.min_length
        if self.max_length is not None:
            out["max_length"] = self.max_length
        if self.min_items is not None:
            out["min_items"] = self.min_items
        if self.max_items is not None:
            out["max_items"] = self.max_items
        if self.format:
            out["format"] = self.format
        return out


@dataclass(frozen=True)
class ElicitationRequest:
    """One elicitation, the same whichever wire carried it.

    ``request_id`` is the JSON-RPC id the answer must carry. ``tool_call_id`` is
    the tool call the elicitation interrupts (the KAS engine always names it;
    standard ACP may leave it empty for a session-scoped ask). ``url_mode`` and
    the fields are mutually exclusive by mode.
    """

    request_id: str | int
    session_id: str
    mode: str
    message: str
    tool_call_id: str = ""
    #: Form mode: the fields in schema order.
    fields: tuple[ElicitationField, ...] = ()
    #: URL mode: where to send the person, and the server's id for the ask.
    url: str = ""
    elicitation_id: str = ""
    #: The method the request arrived on, for the log line and the audit.
    method: str = field(default="", compare=False)

    @property
    def is_form(self) -> bool:
        return self.mode == ELICITATION_MODE_FORM

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ElicitationRequest:
        """Rebuild a request from its :meth:`as_dict` shape.

        The dashboard stores that shape on the card's row and reads it back to
        validate the person's answer; the fields come back through
        :meth:`ElicitationField.from_dict`, so the validation the API runs is the
        one the driver runs again on send.
        """
        mode = str(payload.get("mode") or ELICITATION_MODE_FORM)
        raw_fields = payload.get("fields") or []
        return cls(
            request_id=str(payload.get("request_id") or ""),
            session_id=str(payload.get("session_id") or ""),
            mode=mode,
            message=str(payload.get("message") or ""),
            tool_call_id=str(payload.get("tool_call_id") or ""),
            fields=tuple(ElicitationField.from_dict(f) for f in raw_fields if isinstance(f, dict)),
            url=str(payload.get("url") or ""),
            elicitation_id=str(payload.get("elicitation_id") or ""),
        )

    def as_dict(self) -> dict[str, Any]:
        """The payload the dashboard card renders; no wire detail leaks through."""
        out: dict[str, Any] = {
            "request_id": str(self.request_id),
            "tool_call_id": self.tool_call_id,
            "mode": self.mode,
            "message": self.message,
        }
        if self.is_form:
            out["fields"] = [f.as_dict() for f in self.fields]
        else:
            out["url"] = self.url
            out["elicitation_id"] = self.elicitation_id
        return out


def is_elicitation_request(msg: JsonRpcMessage) -> bool:
    """True for a server->client REQUEST on either elicitation method.

    A notification on these methods (no id) is not one: there is nothing to
    answer, and the classifier leaves it to its notification handling.
    """
    return msg.id is not None and isinstance(msg.method, str) and msg.method in ELICITATION_METHODS


def parse_elicitation_request(msg: JsonRpcMessage) -> ElicitationRequest:
    """Read either wire into one :class:`ElicitationRequest`.

    Raises :class:`ElicitationParseError` for a request this client cannot put to
    a person. The reason names the first thing wrong, in words fit for a log.
    """
    if not is_elicitation_request(msg):
        raise ElicitationParseError("not an elicitation request")
    assert msg.id is not None
    params = msg.params if isinstance(msg.params, dict) else {}
    if msg.method == METHOD_KAS_MCP_ELICITATION:
        inner = params.get("elicitation")
        if not isinstance(inner, dict):
            raise ElicitationParseError("KAS elicitation carries no `elicitation` object")
        body: dict[str, Any] = dict(inner)
    else:
        body = dict(params)
    session_id = params.get("sessionId")
    if not isinstance(session_id, str) or not session_id:
        # Standard ACP also allows a request-scoped ask (``requestId``) outside
        # any session, during auth or configuration. Nothing in Crew runs those
        # phases interactively, so there is no person to route it to.
        raise ElicitationParseError("elicitation names no session")
    tool_call_id = params.get("toolCallId")
    tool_call_id = tool_call_id if isinstance(tool_call_id, str) else ""

    message = body.get("message")
    if not isinstance(message, str) or not message.strip():
        raise ElicitationParseError("elicitation carries no message")
    message = message[:MESSAGE_MAX_CHARS]

    # KAS fills ``mode`` in even when the MCP server omitted it; standard ACP
    # requires it. An absent mode with a ``requestedSchema`` is the MCP default
    # (form), so the one shape the spec leaves implicit still renders.
    mode = body.get("mode")
    if mode is None and isinstance(body.get("requestedSchema"), dict):
        mode = ELICITATION_MODE_FORM
    # ``isinstance`` before the membership test: a list or dict here would raise
    # TypeError out of the parser instead of the refusal every caller catches.
    if not isinstance(mode, str) or mode not in ELICITATION_MODES:
        raise ElicitationParseError(f"unsupported elicitation mode {mode!r}")

    if mode == ELICITATION_MODE_URL:
        url = body.get("url")
        if not isinstance(url, str) or len(url) > URL_MAX_CHARS:
            raise ElicitationParseError("URL elicitation carries no usable url")
        try:
            parts = urlsplit(url)
        except ValueError as exc:  # a malformed bracketed host, for one
            raise ElicitationParseError("URL elicitation url does not parse") from exc
        if parts.scheme not in ("http", "https") or not parts.netloc:
            # The person is sent to this address in their browser. Anything
            # but a web URL (a javascript:, file: or custom scheme) is not a
            # place a confirmation can happen and is refused unrendered.
            raise ElicitationParseError("URL elicitation url is not http(s)")
        elicitation_id = body.get("elicitationId")
        if not isinstance(elicitation_id, str) or not elicitation_id:
            raise ElicitationParseError("URL elicitation carries no elicitationId")
        return ElicitationRequest(
            request_id=msg.id,
            session_id=session_id,
            mode=mode,
            message=message,
            tool_call_id=tool_call_id,
            url=url,
            elicitation_id=elicitation_id[:FIELD_NAME_MAX_CHARS],
            method=msg.method or "",
        )

    schema = body.get("requestedSchema")
    if not isinstance(schema, dict):
        raise ElicitationParseError("form elicitation carries no requestedSchema")
    return ElicitationRequest(
        request_id=msg.id,
        session_id=session_id,
        mode=mode,
        message=message,
        tool_call_id=tool_call_id,
        fields=_parse_schema(schema),
        method=msg.method or "",
    )


def _parse_schema(schema: dict[str, Any]) -> tuple[ElicitationField, ...]:
    """Normalise an ``ElicitationSchema`` into renderable fields, or refuse it."""
    if schema.get("type", "object") != "object":
        raise ElicitationParseError("requestedSchema is not an object schema")
    props = schema.get("properties")
    if props is None:
        props = {}
    if not isinstance(props, dict):
        raise ElicitationParseError("requestedSchema.properties is not an object")
    if len(props) > PROPERTIES_MAX:
        raise ElicitationParseError(f"requestedSchema has more than {PROPERTIES_MAX} properties")
    required_raw = schema.get("required") or []
    if not isinstance(required_raw, list) or not all(isinstance(r, str) for r in required_raw):
        raise ElicitationParseError("requestedSchema.required is not a list of names")
    required = set(required_raw)
    fields: list[ElicitationField] = []
    for name, prop in props.items():
        if not isinstance(name, str) or not name or len(name) > FIELD_NAME_MAX_CHARS:
            raise ElicitationParseError("requestedSchema has an unusable property name")
        if not isinstance(prop, dict):
            raise ElicitationParseError(f"property {name!r} is not a schema object")
        fields.append(_parse_field(name, prop, name in required))
    missing = required - set(props)
    if missing:
        raise ElicitationParseError("requestedSchema requires a property it does not define")
    return tuple(fields)


def _label(prop: dict[str, Any], key: str) -> str:
    value = prop.get(key)
    return value[:LABEL_MAX_CHARS] if isinstance(value, str) else ""


def _string_format(prop: dict[str, Any]) -> str:
    """The string's ``format`` when it is one the MCP spec names, else ``""``.

    A closed set, not free text: the value reaches the dashboard row unredacted
    (it is a type hint, not display text), so a server-written string there
    would be a channel around the display redaction.
    """
    value = prop.get("format")
    return value if isinstance(value, str) and value in STRING_FORMATS else ""


def _number(prop: dict[str, Any], key: str) -> float | None:
    value = prop.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        out = float(value)
    except OverflowError as exc:
        # An integer past the float range is a bound this card cannot check an
        # answer against; refuse the request rather than let the conversion
        # escape past the parse-error handler and end the turn.
        raise ElicitationParseError(f"{key} is too large to represent") from exc
    if out != out or out in (float("inf"), float("-inf")):
        raise ElicitationParseError(f"{key} is not a finite number")
    return out


def _int(prop: dict[str, Any], key: str) -> int | None:
    value = prop.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _enum_options(prop: dict[str, Any], name: str) -> tuple[ElicitationOption, ...] | None:
    """The choices of a single-select string, or None when it is a free string.

    Four spellings carry choices: MCP's titled list (``[{const, title}]``) under
    ``oneOf`` for a single-select or ``anyOf`` for a multi-select's ``items``, a
    plain ``enum`` list, and the legacy ``enum`` + ``enumNames`` pair. Values
    must be strings: the response schema allows no other enum value type.
    """
    titled_key = "oneOf" if "oneOf" in prop else "anyOf"
    titled = prop.get(titled_key)
    if isinstance(titled, list):
        options = []
        for entry in titled:
            if not isinstance(entry, dict) or not isinstance(entry.get("const"), str):
                raise ElicitationParseError(
                    f"property {name!r} has a non-string {titled_key} choice"
                )
            value = _option_value(entry["const"], name)
            options.append(ElicitationOption(value, _label(entry, "title") or value))
        return _bounded_options(options, name)
    values = prop.get("enum")
    if values is None:
        return None
    if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
        raise ElicitationParseError(f"property {name!r} has a non-string enum")
    names = prop.get("enumNames")
    labels = names if isinstance(names, list) and len(names) == len(values) else None
    options = []
    for i, value in enumerate(values):
        value = _option_value(value, name)
        label = labels[i] if labels and isinstance(labels[i], str) else value
        options.append(ElicitationOption(value, label[:LABEL_MAX_CHARS]))
    return _bounded_options(options, name)


def _option_value(value: str, name: str) -> str:
    """An offered choice, verbatim: it is sent back as the answer, so it is never
    cut down -- a truncated value is one the server never offered, and two
    choices sharing a prefix would collapse into one. Too long is refused."""
    if len(value) > STRING_VALUE_MAX_CHARS:
        raise ElicitationParseError(f"property {name!r} offers a choice longer than the limit")
    return value


def _bounded_options(options: list[ElicitationOption], name: str) -> tuple[ElicitationOption, ...]:
    if not options:
        raise ElicitationParseError(f"property {name!r} offers no choices")
    if len(options) > ENUM_OPTIONS_MAX:
        raise ElicitationParseError(
            f"property {name!r} offers more than {ENUM_OPTIONS_MAX} choices"
        )
    return tuple(options)


def _parse_field(name: str, prop: dict[str, Any], required: bool) -> ElicitationField:
    ptype = prop.get("type")
    title = _label(prop, "title")
    description = _label(prop, "description")
    if ptype == "array":
        # Multi-select: an array whose items are a string enum. Any other array
        # (free strings, numbers) has no form control and is refused.
        items = prop.get("items")
        if not isinstance(items, dict) or items.get("type", "string") != "string":
            raise ElicitationParseError(f"property {name!r} is an array of a non-enum type")
        options = _enum_options(items, name)
        if options is None:
            raise ElicitationParseError(f"property {name!r} is an array without choices")
        field_ = ElicitationField(
            kind=FIELD_KIND_MULTI_ENUM,
            options=options,
            min_items=_int(prop, "minItems"),
            max_items=_int(prop, "maxItems"),
            name=name,
            title=title,
            description=description,
            required=required,
        )
        return _with_default(field_, prop.get("default"))
    if not isinstance(ptype, str) or ptype not in _PRIMITIVE_TYPES:
        raise ElicitationParseError(f"property {name!r} has unsupported type {ptype!r}")
    if ptype == "string":
        options = _enum_options(prop, name)
        if options is not None:
            field_ = ElicitationField(
                kind=FIELD_KIND_ENUM,
                options=options,
                name=name,
                title=title,
                description=description,
                required=required,
            )
        else:
            field_ = ElicitationField(
                kind=FIELD_KIND_STRING,
                min_length=_int(prop, "minLength"),
                max_length=_int(prop, "maxLength"),
                format=_string_format(prop),
                name=name,
                title=title,
                description=description,
                required=required,
            )
    elif ptype == "boolean":
        field_ = ElicitationField(
            kind=FIELD_KIND_BOOLEAN,
            name=name,
            title=title,
            description=description,
            required=required,
        )
    else:
        field_ = ElicitationField(
            kind=FIELD_KIND_INTEGER if ptype == "integer" else FIELD_KIND_NUMBER,
            minimum=_number(prop, "minimum"),
            maximum=_number(prop, "maximum"),
            name=name,
            title=title,
            description=description,
            required=required,
        )
    return _with_default(field_, prop.get("default"))


def _with_default(field_: ElicitationField, default: Any) -> ElicitationField:
    """Attach a server default only when it fits the field; otherwise drop it."""
    if default is None:
        return field_
    try:
        checked = _check_value(field_, default)
    except ElicitationContentError:
        return field_
    return replace(field_, default=checked)


def validate_form_content(
    request: ElicitationRequest, content: dict[str, Any] | None
) -> dict[str, Any]:
    """The person's answers, checked against the requested fields.

    Returns the content to send: only the fields the schema names, each value
    checked and shaped for the wire. A missing optional field is omitted; a
    missing required one, an unknown field, or a value of the wrong shape raises
    :class:`ElicitationContentError` and nothing is sent.
    """
    if not request.is_form:
        raise ElicitationContentError("a URL elicitation takes no content")
    given = content if isinstance(content, dict) else {}
    by_name = {f.name: f for f in request.fields}
    unknown = set(given) - set(by_name)
    if unknown:
        raise ElicitationContentError(
            f"content names a field the schema does not: {sorted(unknown)[0]!r}"
        )
    out: dict[str, Any] = {}
    for field_ in request.fields:
        if field_.name not in given or given[field_.name] is None:
            if field_.required:
                raise ElicitationContentError(f"required field {field_.name!r} has no value")
            continue
        out[field_.name] = _check_value(field_, given[field_.name])
    return out


def _check_value(field_: ElicitationField, value: Any) -> Any:
    kind = field_.kind
    if kind == FIELD_KIND_BOOLEAN:
        if not isinstance(value, bool):
            raise ElicitationContentError(f"{field_.name!r} must be true or false")
        return value
    if kind in (FIELD_KIND_INTEGER, FIELD_KIND_NUMBER):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ElicitationContentError(f"{field_.name!r} must be a number")
        # ``1e400`` decodes to infinity. Kept, it would be written as the JSON
        # extension ``Infinity``, which a browser's ``JSON.parse`` and the engine
        # refuse whole: a default would make the card's frame unreadable, an
        # answer the response.
        if isinstance(value, float) and not math.isfinite(value):
            raise ElicitationContentError(f"{field_.name!r} must be a finite number")
        if kind == FIELD_KIND_INTEGER:
            if isinstance(value, float) and not value.is_integer():
                raise ElicitationContentError(f"{field_.name!r} must be a whole number")
            value = int(value)
        if field_.minimum is not None and value < field_.minimum:
            raise ElicitationContentError(f"{field_.name!r} is below its minimum")
        if field_.maximum is not None and value > field_.maximum:
            raise ElicitationContentError(f"{field_.name!r} is above its maximum")
        return value
    if kind == FIELD_KIND_STRING:
        if not isinstance(value, str):
            raise ElicitationContentError(f"{field_.name!r} must be text")
        if len(value) > STRING_VALUE_MAX_CHARS:
            raise ElicitationContentError(f"{field_.name!r} is too long")
        if field_.min_length is not None and len(value) < field_.min_length:
            raise ElicitationContentError(f"{field_.name!r} is too short")
        if field_.max_length is not None and len(value) > field_.max_length:
            raise ElicitationContentError(f"{field_.name!r} is too long")
        return value
    allowed = {o.value for o in field_.options}
    if kind == FIELD_KIND_ENUM:
        if not isinstance(value, str) or value not in allowed:
            raise ElicitationContentError(f"{field_.name!r} is not one of the offered choices")
        return value
    # multi_enum
    if not isinstance(value, list) or not all(isinstance(v, str) and v in allowed for v in value):
        raise ElicitationContentError(f"{field_.name!r} must be a list of the offered choices")
    chosen = list(dict.fromkeys(value))
    if field_.min_items is not None and len(chosen) < field_.min_items:
        raise ElicitationContentError(
            f"{field_.name!r} needs at least {field_.min_items} choice(s)"
        )
    if field_.max_items is not None and len(chosen) > field_.max_items:
        raise ElicitationContentError(
            f"{field_.name!r} allows at most {field_.max_items} choice(s)"
        )
    return chosen


def elicitation_response(action: str, content: dict[str, Any] | None = None) -> dict[str, Any]:
    """The JSON-RPC ``result`` for an elicitation, identical on both wires.

    ``content`` is carried only with ``accept``; the other two actions say
    nothing about the form, and a server reading content next to a decline
    would be reading an answer the person did not give.
    """
    if action not in ELICITATION_ACTIONS:
        raise ValueError(f"unknown elicitation action {action!r}")
    out: dict[str, Any] = {"action": action}
    if action == ELICITATION_ACCEPT and content is not None:
        out["content"] = content
    return out
