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

import importlib
import inspect
import json
import pkgutil
import re
import types
from pathlib import Path

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
    for token, key in copy_keys.items():
        node: object = catalog
        for part in key.split("."):
            assert isinstance(node, dict) and part in node, f"{token}: {key} not in en.json"
            node = node[part]
        assert isinstance(node, str) and node, f"{token}: {key} is not a string"


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
    assert len(texts) == 13
    unmatched = [n for n, t in texts.items() if not any(p.match(t) for p in patterns)]
    assert (
        not unmatched
    ), f"fallback text(s) no LEGACY pattern in gatewayNotice.ts recognises: {unmatched}"


def test_fallback_texts_avoid_internal_vocabulary() -> None:
    # A person reading the English fallback is not told about "backends" or
    # "hiccups". Pins the wording class, not the copy.
    texts = _fallback_texts()
    assert len(texts) == 13
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


def test_token_tables_have_the_pinned_size() -> None:
    # 3 transient-5xx tokens and 58 gateway tokens; a new row family moves both.
    assert len(_backend_tokens()) == 61
    assert len(_frontend_families()) == 61
    assert len(_legacy_entries()) == 63


def _tokenized_literal_sites() -> list[tuple[str, int, str, str]]:
    """``(path, line, token, english)`` for every append whose meta names one token.

    The content must be a literal, an f-string (each interpolation rendered as
    ``7``, which every captured value pattern accepts) or a module constant;
    a site that picks its token or its text at run time is pinned below instead.
    """
    import ast
    import importlib

    from kiro_crew.dashboard import gateway_notice as gn

    token_re = re.compile(r"\bgn\.(GATEWAY_NOTICE_[A-Z_]+)")
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
            content = node.args[1]
            if isinstance(content, ast.Constant) and isinstance(content.value, str):
                text = content.value
            elif isinstance(content, ast.JoinedStr):
                text = "".join(
                    v.value if isinstance(v, ast.Constant) else "7" for v in content.values
                )
            elif isinstance(content, ast.Name):
                text = getattr(importlib.import_module(module), content.id, None)
                if not isinstance(text, str):
                    continue
            else:
                continue
            sites.append(
                (str(path.relative_to(_REPO)), node.lineno, getattr(gn, names.pop()), text)
            )
    return sites


def test_every_tokenized_literal_site_is_recognised_by_its_legacy_pattern() -> None:
    # Rows written before a site carried its token are on disk with only this
    # English, so the frontend's LEGACY table must map each wording to the very
    # token the site writes today.
    sites = _tokenized_literal_sites()
    assert len(sites) == 27
    wrong = [
        (p, line, tok, _legacy_token(text))
        for p, line, tok, text in sites
        if _legacy_token(text) != tok
    ]
    assert not wrong, f"site(s) whose English no LEGACY pattern maps to their token: {wrong}"


def test_run_time_chosen_tokens_match_their_wording() -> None:
    from kiro_crew.dashboard.chat_runner import (
        SESSION_NOT_FOUND_GIVE_UP_TEXT,
        SESSION_NOT_FOUND_RETRY_TEXT,
    )
    from kiro_crew.llm_helpers import CREDENTIAL_RELOAD_RETRY_NOTICE

    # The acp_recovery module reads the give-up text through chat_runner at run
    # time, so the source scan above cannot resolve it.
    assert _legacy_token(SESSION_NOT_FOUND_GIVE_UP_TEXT) == "session_reconnect_give_up"
    assert _legacy_token(SESSION_NOT_FOUND_RETRY_TEXT) == "session_reconnecting"
    assert _legacy_token(CREDENTIAL_RELOAD_RETRY_NOTICE) == "credentials_reloading"
    assert (
        _legacy_token("ℹ The capacity retry was cancelled — your message takes over.")
        == "capacity_retry_cancelled_superseded"
    )
    assert (
        _legacy_token(
            "ℹ The capacity retry was cancelled — the turn was stopped, the call was not retried."
        )
        == "capacity_retry_cancelled_stopped"
    )


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
    assert len(copy) == 60
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
    assert readers == 12


def test_gateway_notice_meta_shape() -> None:
    from kiro_crew.dashboard import gateway_notice as gn

    # No args: only the token field, so a row's meta stays as small as before.
    assert gn.gateway_notice_meta(gn.GATEWAY_NOTICE_SESSION_STUCK) == {"notice": "session_stuck"}
    # Args ride beside it, under the field the frontend interpolates from.
    meta = gn.gateway_notice_meta(gn.GATEWAY_NOTICE_CONNECTION_LOST_GIVE_UP, exit_code=-9)
    assert meta == {"notice": "connection_lost_give_up", "notice_args": {"exit_code": -9}}
