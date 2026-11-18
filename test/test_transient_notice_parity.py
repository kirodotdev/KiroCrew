"""Guard: the gateway status-row notice tokens must match the frontend's table.

Every status row the chat gateway appends (the transient-5xx ladder, connection
lost, session busy, stalled turn or tool, session stuck, the empty-response and
model-fallback notices) carries a ``meta["notice"]`` wire token. The tokens are
``TRANSIENT_NOTICE_*`` in ``kiro_crew.dashboard.chat_utils`` and
``GATEWAY_NOTICE_*`` in ``kiro_crew.dashboard.gateway_notice``.
``NOTICE_FAMILIES`` in ``website/src/pages/chat/gatewayNotice.ts`` is the
frontend's copy of that list, keyed to pick which localized card a row renders
as, and ``COPY_KEY`` beside it names each token's catalog key. Nothing links the
two sides but the token string -- the same shape as the ``*_RECOVERY_PREFIX``
markers guarded by ``test_recovery_marker_parity.py`` -- so an edit on one side
silently drops the row back to raw English on the other. Discovered by regex
rather than listed, so a new token is caught automatically.

Rows persisted before their token existed carry only their English wording, so
the frontend's ``LEGACY`` table must still recognise every fallback text the
gateway writes today; that is pinned here too, by running the frontend's own
patterns against the Python constants.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import json
import pkgutil
import re
import types
from pathlib import Path

import pytest

from kiro_crew.acp import kas_route
from kiro_crew.acp.kas_route import (
    REASON_CONFIGURED,
    REASON_KIRO_CLI_LAUNCHER,
    REASON_KIRO_CLI_SIGNED_OUT,
    REASON_NO_KIRO_CLI,
    KasRoute,
)
from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS
from kiro_crew.messaging import empty_turn_copy

_REPO = Path(__file__).resolve().parents[1]
_CHAT_UTILS = _REPO / "src" / "kiro_crew" / "dashboard" / "chat_utils.py"
_GATEWAY = _REPO / "src" / "kiro_crew" / "dashboard" / "gateway_notice.py"
_FRONTEND = _REPO / "website" / "src" / "pages" / "chat" / "gatewayNotice.ts"
_EN_CATALOG = _REPO / "website" / "src" / "i18n" / "locales" / "en.json"

# `TRANSIENT_NOTICE_X = "..."` / `GATEWAY_NOTICE_X = "..."` at column 0, excluding
# the META_KEY / ARGS_KEY entries (which name meta fields, not tokens).
_BACKEND_RE = re.compile(
    r'^(?P<name>(?:TRANSIENT|GATEWAY)_NOTICE_(?!META_KEY|ARGS_KEY)[A-Z_]+)\s*=\s*"(?P<token>[^"]+)"',
    re.MULTILINE,
)
_META_KEY_RE = re.compile(
    r'^(?:TRANSIENT|GATEWAY)_NOTICE_META_KEY\s*=\s*"(?P<key>[^"]+)"', re.MULTILINE
)
# English fallback content: `TRANSIENT_X_TEXT = "..."` / `GATEWAY_X_TEXT = (...)`.
_TEXT_RE = re.compile(
    r'^(?P<name>(?!GATEWAY_NOTICE_)(?:TRANSIENT|GATEWAY)_[A-Z_]+_TEXT)\s*=\s*\(?\s*"(?P<text>[^"]+)"',
    re.MULTILINE,
)
# Every column-0 assignment of the same names, whatever its right-hand side. The
# strict patterns above must parse each one; a constant written in a shape they
# miss (an annotation, single quotes) would otherwise drop out of every check.
_BACKEND_NAME_RE = re.compile(
    r"^((?:TRANSIENT|GATEWAY)_NOTICE_(?!META_KEY|ARGS_KEY)[A-Z_]+)\b\s*[:=]", re.MULTILINE
)
_TEXT_NAME_RE = re.compile(
    r"^((?!GATEWAY_NOTICE_)(?:TRANSIENT|GATEWAY)_[A-Z_]+_TEXT)\b\s*[:=]", re.MULTILINE
)


def _assigned_names(pattern: re.Pattern[str]) -> set[str]:
    return {
        name
        for path in (_CHAT_UTILS, _GATEWAY)
        for name in pattern.findall(path.read_text(encoding="utf-8"))
    }


def _table(name: str) -> str:
    match = re.search(
        rf"^const {name}\b[^=]*=\s*(?P<open>[\[{{])(?P<body>.*?)^(?P<close>[\]}}])",
        _FRONTEND.read_text(encoding="utf-8"),
        re.DOTALL | re.MULTILINE,
    )
    assert match, f"could not find the {name} table in gatewayNotice.ts"
    return match.group("body")


def _backend_tokens() -> dict[str, str]:
    found: dict[str, str] = {}
    for path in (_CHAT_UTILS, _GATEWAY):
        found.update(_BACKEND_RE.findall(path.read_text(encoding="utf-8")))
    assert found, "no TRANSIENT_NOTICE_* / GATEWAY_NOTICE_* token constants found"
    return found


def _frontend_families() -> dict[str, str]:
    entries = re.findall(
        r"^\s*(?P<token>[a-z_]+):\s*\{\s*role:\s*'(?P<role>error|notice)'",
        _table("NOTICE_FAMILIES"),
        re.MULTILINE,
    )
    assert entries, "NOTICE_FAMILIES parsed as empty"
    return dict(entries)


def _frontend_copy_keys() -> dict[str, str]:
    entries = re.findall(
        r"^\s*(?P<token>[a-z_]+):\s*'(?P<key>[a-zA-Z_.]+)'", _table("COPY_KEY"), re.MULTILINE
    )
    assert entries, "COPY_KEY parsed as empty"
    return dict(entries)


def _legacy_patterns() -> list[re.Pattern[str]]:
    # `re: /^...$/u` -> a Python pattern: JS named groups `(?<n>` become `(?P<n>`.
    sources = re.findall(r"re:\s*/(?P<src>\^.*?\$)/u", _table("LEGACY"))
    assert sources, "LEGACY parsed as empty"
    return [re.compile(src.replace("(?<", "(?P<")) for src in sources]


def _fallback_texts() -> dict[str, str]:
    texts: dict[str, str] = {}
    for path in (_CHAT_UTILS, _GATEWAY):
        texts.update(_TEXT_RE.findall(path.read_text(encoding="utf-8")))
    return texts


def test_backend_tokens_match_the_frontend_table() -> None:
    backend = set(_backend_tokens().values())
    frontend = set(_frontend_families())
    missing_in_frontend = backend - frontend
    missing_in_backend = frontend - backend
    assert not missing_in_frontend, (
        "backend token(s) the frontend cannot resolve, so their rows render as raw "
        f"English on every locale: {sorted(missing_in_frontend)}. Add them to "
        "NOTICE_FAMILIES in gatewayNotice.ts."
    )
    assert not missing_in_backend, (
        "frontend NOTICE_FAMILIES entr(ies) with no backend constant, so nothing emits "
        f"them: {sorted(missing_in_backend)}. Remove them, or add the constant to "
        "gateway_notice.py."
    )


def test_backend_token_values_are_unique() -> None:
    tokens = _backend_tokens()
    assert len(set(tokens.values())) == len(tokens), tokens


def test_every_token_names_catalog_copy() -> None:
    copy_keys = _frontend_copy_keys()
    assert set(copy_keys) == set(_frontend_families())
    catalog = json.loads(_EN_CATALOG.read_text(encoding="utf-8"))
    plural_bases = set(
        json.loads((_REPO / "website" / "src" / "i18n" / "pluralKeys.json").read_text("utf-8"))
    )

    def _leaf(key: str) -> object:
        node: object = catalog
        for part in key.split("."):
            if not (isinstance(node, dict) and part in node):
                return None
            node = node[part]
        return node

    for token, key in copy_keys.items():
        leaf = _leaf(key)
        if leaf is None and key in plural_bases:
            # A plural base has no flat key; i18next resolves `_one`/`_other`
            # from it at runtime. Require the English forms both languages have.
            one, other = _leaf(f"{key}_one"), _leaf(f"{key}_other")
            assert isinstance(one, str) and one, f"{token}: {key}_one not in en.json"
            assert isinstance(other, str) and other, f"{token}: {key}_other not in en.json"
            continue
        assert isinstance(leaf, str) and leaf, f"{token}: {key} is not a string"


def test_frontend_reads_the_meta_field_the_backend_writes() -> None:
    keys = {
        m.group("key")
        for path in (_CHAT_UTILS, _GATEWAY)
        for m in _META_KEY_RE.finditer(path.read_text(encoding="utf-8"))
    }
    # One field for every family; the frontend reads `meta.notice` by property
    # name, so a renamed field on either side would make every token invisible.
    assert keys == {"notice"}
    assert "notice?: unknown" in _FRONTEND.read_text(encoding="utf-8")


def test_legacy_table_recognises_every_pre_token_wording() -> None:
    # The gateway texts below were written without a token before the token
    # existed, so history on disk carries them bare. The transient-5xx texts are
    # excluded: that wording has only ever been written with its token.
    patterns = _legacy_patterns()
    texts = {n: t for n, t in _fallback_texts().items() if n.startswith("GATEWAY_")}
    for name in (
        "EMPTY_TURN_NOTICE",
        "EMPTY_TURN_NOTICE_AFTER_RECOVERY",
        "EMPTY_TURN_NOTICE_AFTER_WORK",
    ):
        texts[name] = getattr(empty_turn_copy, name)
    unmatched = [n for n, t in texts.items() if not any(p.match(t) for p in patterns)]
    assert (
        not unmatched
    ), f"fallback text(s) no LEGACY pattern in gatewayNotice.ts recognises: {unmatched}"


def test_fallback_texts_avoid_internal_vocabulary() -> None:
    # A person reading the English fallback is not told about "backends" or
    # "hiccups". Pins the wording class, not the copy.
    texts = _fallback_texts()
    assert texts, "no TRANSIENT_*_TEXT / GATEWAY_*_TEXT fallback constants found"
    for name, text in texts.items():
        lowered = text.lower()
        assert "hiccup" not in lowered and "backend" not in lowered, (name, text)


def _legacy_entries() -> list[tuple[re.Pattern[str], str]]:
    pairs = re.findall(r"re:\s*/(\^.*?\$)/u,\s*token:\s*'([a-z_]+)'", _table("LEGACY"))
    assert pairs, "LEGACY parsed as empty"
    return [(re.compile(src.replace("(?<", "(?P<")), token) for src, token in pairs]


def _legacy_token(text: str) -> str | None:
    for pattern, token in _legacy_entries():
        if pattern.match(text):
            return token
    return None


def test_every_constant_and_legacy_row_is_parsed() -> None:
    # The scans above are what every other check iterates, so a constant or row
    # they fail to parse would be checked by nothing. Each is compared with a
    # looser scan of the same source rather than with a stored total: a total is
    # one line every PR that adds a family edits, and two such PRs merge into a
    # tree neither tested (testing-conventions.md § Merge-safe ratchets and counts).
    assert set(_backend_tokens()) == _assigned_names(_BACKEND_NAME_RE)
    assert set(_fallback_texts()) == _assigned_names(_TEXT_NAME_RE)
    legacy = _legacy_entries()
    assert len(legacy) == len(
        re.findall(r"\bre:\s*/", _table("LEGACY"))
    ), "a LEGACY row whose pattern or token the parser cannot read"
    assert {token for _, token in legacy} <= set(_frontend_families())


def _content_texts(content: ast.expr, module: str) -> list[str] | None:
    """Every English text *content* can render, or ``None`` when it is chosen at run time.

    A literal, an f-string (each interpolation rendered as ``7``, which every
    captured value pattern accepts), a module constant, a ``gn.`` or other
    module-attribute constant, or a conditional between any of those.
    """
    from kiro_crew.dashboard import gateway_notice as gn

    if isinstance(content, ast.Constant) and isinstance(content.value, str):
        return [content.value]
    if isinstance(content, ast.JoinedStr):
        return ["".join(v.value if isinstance(v, ast.Constant) else "7" for v in content.values)]
    if isinstance(content, ast.Name):
        text = getattr(importlib.import_module(module), content.id, None)
        return [text] if isinstance(text, str) else None
    if isinstance(content, ast.Attribute) and isinstance(content.value, ast.Name):
        owner = (
            gn
            if content.value.id == "gn"
            else getattr(importlib.import_module(module), content.value.id, None)
        )
        text = getattr(owner, content.attr, None)
        return [text] if isinstance(text, str) else None
    if isinstance(content, ast.IfExp):
        body = _content_texts(content.body, module)
        orelse = _content_texts(content.orelse, module)
        return body + orelse if body is not None and orelse is not None else None
    return None


def _tokenized_sites() -> list[tuple[str, int, str, list[str] | None]]:
    """``(path, line, token, texts)`` for every append whose meta names one token.

    ``texts`` is ``None`` for a site whose text is chosen at run time; its wording
    is pinned by :func:`_run_time_wording` instead.
    """
    from kiro_crew.dashboard import gateway_notice as gn

    token_re = re.compile(r"\b(?:gn\.)?(GATEWAY_NOTICE_[A-Z_]+)")
    src_root = _REPO / "src"
    sites = []
    for path in sorted((src_root / "kiro_crew").rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        if "GATEWAY_NOTICE_" not in source:
            continue
        module = ".".join(path.relative_to(src_root).with_suffix("").parts)
        for node in ast.walk(ast.parse(source)):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "append"
                and len(node.args) >= 2
            ):
                continue
            meta = [k.value for k in node.keywords if k.arg == "meta"]
            names = (
                set(token_re.findall(ast.get_source_segment(source, meta[0]) or ""))
                if meta
                else set()
            )
            if len(names) != 1:
                continue
            sites.append(
                (
                    path.relative_to(_REPO).as_posix(),
                    node.lineno,
                    getattr(gn, names.pop()),
                    _content_texts(node.args[1], module),
                )
            )
    return sites


def test_every_tokenized_site_is_recognised_by_its_legacy_pattern() -> None:
    # Rows written before a site carried its token are on disk with only this
    # English, so the frontend's LEGACY table must map each wording to the very
    # token the site writes today.
    sites = _tokenized_sites()
    resolved = [(p, line, tok, texts) for p, line, tok, texts in sites if texts is not None]
    assert resolved, "the source scan resolved no tokenized append site"
    wrong = [
        (p, line, tok, text, _legacy_token(text))
        for p, line, tok, texts in resolved
        for text in texts
        if _legacy_token(text) != tok
    ]
    assert not wrong, f"site(s) whose English no LEGACY pattern maps to their token: {wrong}"


def test_every_unresolved_site_has_its_wording_pinned() -> None:
    # A site whose text the scan cannot resolve is checked by nothing above, so
    # its token's wording must be pinned by the run-time check below.
    pinned = set(_run_time_wording())
    unchecked = [(p, line, tok) for p, line, tok, texts in _tokenized_sites() if texts is None]
    assert all(tok in pinned for _p, _line, tok in unchecked), (
        "tokenized append site(s) whose text the scan cannot resolve and whose token "
        "_run_time_wording does not pin, so their English is checked against LEGACY "
        f"by nothing: {[s for s in unchecked if s[2] not in pinned]}"
    )


def _run_time_wording() -> dict[str, list[str]]:
    """The wording of each token whose site picks its text at run time."""
    from kiro_crew.dashboard.chat_runner import (
        SESSION_NOT_FOUND_GIVE_UP_TEXT,
        SESSION_NOT_FOUND_RETRY_TEXT,
    )
    from kiro_crew.dashboard.session_control_parts import delivery_containment as dc
    from kiro_crew.llm_helpers import CREDENTIAL_RELOAD_RETRY_NOTICE

    # The acp_recovery module reads the give-up text through chat_runner at run
    # time, so the source scan above cannot resolve it.
    return {
        "session_reconnect_give_up": [SESSION_NOT_FOUND_GIVE_UP_TEXT],
        "session_reconnecting": [SESSION_NOT_FOUND_RETRY_TEXT],
        "credentials_reloading": [CREDENTIAL_RELOAD_RETRY_NOTICE],
        # The sender's discard notice joins its target and reason at run time; only
        # a cause with a framed reason carries the token.
        "send_discarded": [
            "⚠ Message you sent to crew:x " + dc._SEND_DISCARD_REASONS[cause]
            for cause in dc._SEND_DISCARD_FRAMED_REASONS
        ],
        "capacity_retry_cancelled_superseded": [
            "ℹ The capacity retry was cancelled — your message takes over."
        ],
        "capacity_retry_cancelled_stopped": [
            "ℹ The capacity retry was cancelled — the turn was stopped, the call was not retried."
        ],
    }


def test_run_time_chosen_tokens_match_their_wording() -> None:
    wrong = [
        (token, text, _legacy_token(text))
        for token, texts in _run_time_wording().items()
        for text in texts
        if _legacy_token(text) != token
    ]
    assert not wrong, f"run-time wording no LEGACY pattern maps to its token: {wrong}"


def test_every_cancelled_replay_wording_has_its_own_token() -> None:
    from kiro_crew.dashboard.recovery_replays import (
        ReplayFamily,
        ReplayRevocation,
        cancel_notice,
        cancel_notice_meta,
        cancel_notice_token,
    )

    revocations = (
        ReplayRevocation(),
        ReplayRevocation(superseded=True),
        ReplayRevocation(rebound=True),
        ReplayRevocation(rebound=True, superseded=True),
        ReplayRevocation(stop_moved=True),
    )
    seen: dict[str, set[str]] = {}
    for family in ReplayFamily:
        for revocation in revocations:
            token = cancel_notice_token(family, revocation)
            text = cancel_notice(family, revocation)
            assert _legacy_token(text) == token, (family, revocation, text)
            assert cancel_notice_meta(family, revocation) == {"notice": token}
            seen.setdefault(token, set()).add(text)
    # One wording per token, and every wording of every family covered.
    assert len(seen) == 12
    assert all(len(texts) == 1 for texts in seen.values()), seen


def test_localized_copy_avoids_internal_vocabulary() -> None:
    catalog = json.loads(_EN_CATALOG.read_text(encoding="utf-8"))
    copy = catalog["pages"]["chat"]["gatewayNotice"]
    assert copy, "pages.chat.gatewayNotice is empty in en.json"
    for key, text in copy.items():
        lowered = text.lower()
        assert "hiccup" not in lowered and "backend" not in lowered, (key, text)


def _code_objects(module: types.ModuleType) -> list[types.CodeType]:
    # By code filename, not ``__module__``: the facade's compose re-creates each
    # owner function on its own globals, which renames the function's module but
    # keeps the code object.
    origin = module.__spec__.origin if module.__spec__ else None
    found: list[types.CodeType] = []
    pending: list[object] = list(vars(module).values())
    while pending:
        obj = pending.pop()
        if isinstance(obj, type) and obj.__module__ == module.__name__:
            pending.extend(vars(obj).values())
        elif isinstance(obj, (staticmethod, classmethod, property)):
            pending.extend(
                f for f in (getattr(obj, n, None) for n in ("__func__", "fget", "fset")) if f
            )
        elif inspect.isfunction(obj) and obj.__code__.co_filename == origin:
            code = [obj.__code__]
            while code:
                current = code.pop()
                found.append(current)
                code.extend(c for c in current.co_consts if isinstance(c, types.CodeType))
    return found


def test_composed_owner_modules_read_the_notice_module_locally() -> None:
    # chat_turn and chat_api functions run on their facade's globals, and the
    # facade binds no module objects from its owners, so a global ``gn`` lookup
    # would be a NameError at the append. Each reader imports it as a local.
    import kiro_crew.dashboard.chat_api as chat_api
    import kiro_crew.dashboard.chat_turn as chat_turn

    readers = 0
    for package in (chat_turn, chat_api):
        for info in pkgutil.iter_modules(package.__path__, package.__name__ + "."):
            module = importlib.import_module(info.name)
            codes = _code_objects(module)
            global_reads = sorted({c.co_qualname for c in codes if "gn" in c.co_names})
            assert not global_reads, (info.name, global_reads)
            if any("gn" in c.co_varnames or "gn" in c.co_freevars for c in codes):
                readers += 1
    assert readers == 15


def test_gateway_notice_meta_shape() -> None:
    from kiro_crew.dashboard import gateway_notice as gn

    # No args: only the token field, so a row's meta stays as small as before.
    assert gn.gateway_notice_meta(gn.GATEWAY_NOTICE_SESSION_STUCK) == {"notice": "session_stuck"}
    # Args ride beside it, under the field the frontend interpolates from.
    meta = gn.gateway_notice_meta(gn.GATEWAY_NOTICE_CONNECTION_LOST_GIVE_UP, exit_code=-9)
    assert meta == {"notice": "connection_lost_give_up", "notice_args": {"exit_code": -9}}


@pytest.mark.parametrize(
    ("route", "named"),
    [
        (None, None),
        (KasRoute(bundle=False), "relay"),
        (KasRoute(bundle=True, reason=REASON_NO_KIRO_CLI), "bundle"),
        (KasRoute(bundle=True, reason=REASON_KIRO_CLI_SIGNED_OUT), "bundle"),
        (KasRoute(bundle=True, reason=REASON_CONFIGURED), "bundle"),
    ],
)
def test_kas_signed_out_row_carries_the_token_and_its_route(
    monkeypatch: pytest.MonkeyPatch, route: KasRoute | None, named: str | None
) -> None:
    from kiro_crew.acp.transport_errors import AcpAuthRequired
    from kiro_crew.agent_sdk.host_auth import signed_out_message
    from kiro_crew.dashboard.chat_turn.recovery import _terminal_error_meta

    monkeypatch.setattr(kas_route, "_LAST_SPAWN_ROUTE", route)
    text = signed_out_message(ACP_BACKEND_KAS)
    meta = _terminal_error_meta(AcpAuthRequired(text, backend=ACP_BACKEND_KAS))
    # The route rides along so the translated copy names it: every KAS prompt
    # names the real route (the recorded decision on choosing KAS's engine).
    expected: dict[str, object] = {"kind": "auth_required", "notice": "kas_signed_out"}
    if named:
        expected["notice_args"] = {"route": named}
    assert meta == expected


def test_other_signed_out_wording_keeps_its_english(monkeypatch: pytest.MonkeyPatch) -> None:
    from kiro_crew.acp.transport_errors import AcpAuthRequired
    from kiro_crew.agent_sdk.host_auth import signed_out_message
    from kiro_crew.dashboard.chat_turn.recovery import _terminal_error_meta

    # The launcher relay names `kiro-cli login`, which the translated copy does not.
    launcher = KasRoute(bundle=False, reason=REASON_KIRO_CLI_LAUNCHER)
    monkeypatch.setattr(kas_route, "_LAST_SPAWN_ROUTE", launcher)
    text = signed_out_message(ACP_BACKEND_KAS)
    assert "kiro-cli login" in text
    meta = _terminal_error_meta(AcpAuthRequired(text, backend=ACP_BACKEND_KAS))
    assert meta == {"kind": "auth_required"}
    # A sentence written under another route than the live one is not re-labelled.
    monkeypatch.setattr(kas_route, "_LAST_SPAWN_ROUTE", KasRoute(bundle=False))
    bundle_text = f"{KasRoute(bundle=True, reason=REASON_CONFIGURED).describe()}. Not signed"
    meta = _terminal_error_meta(AcpAuthRequired(bundle_text, backend=ACP_BACKEND_KAS))
    assert meta == {"kind": "auth_required"}


@pytest.mark.parametrize(
    ("route", "named"),
    [
        (None, None),
        (KasRoute(bundle=False), "relay"),
        (KasRoute(bundle=True, reason=REASON_CONFIGURED), "bundle"),
    ],
)
@pytest.mark.parametrize("request_id", ["", "4f2a-77"])
def test_kas_signed_out_error_frame_row_carries_the_token(
    monkeypatch: pytest.MonkeyPatch, route: KasRoute | None, named: str | None, request_id: str
) -> None:
    from kiro_crew.acp.client import AcpError, _raise_acp_error
    from kiro_crew.agent_sdk.host_auth import kas_signed_out_route
    from kiro_crew.dashboard.chat_turn.recovery import _terminal_error_meta

    monkeypatch.setattr(kas_route, "_LAST_SPAWN_ROUTE", route)
    # kiro-cli's own refusal on a signed-out relay. The formatter's sign-in arm
    # writes the signed-out sentence, then its model-switch note, then the
    # request id when the frame carries one.
    data = "Not logged in. Run `kiro-cli login` first."
    if request_id:
        data += f" request_id: {request_id}"
    frame = {"code": -32000, "message": "Authentication required.", "data": data}
    with pytest.raises(AcpError) as caught:
        _raise_acp_error(frame, None, backend=ACP_BACKEND_KAS)
    text = str(caught.value)
    assert caught.value.auth_required is True
    # The row's own text keeps the whole English answer; only its token is new.
    assert "Switching models will not help" in text
    assert (f"(request_id: {request_id})" in text) is bool(request_id)
    expected: dict[str, object] = {"kind": "auth_required", "notice": "kas_signed_out"}
    if named:
        expected["notice_args"] = {"route": named}
    assert _terminal_error_meta(caught.value) == expected
    # The sentence must lead the text: one quoted inside other prose is not this row.
    assert kas_signed_out_route(f"Earlier: {text}") is None
