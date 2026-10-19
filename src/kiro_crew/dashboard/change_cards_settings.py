"""The settings a change card can target: lookup, search, diagnosis and value checks.

Reading a setting's current value, resolving a setting from the generated settings
registry, :func:`find_settings` and its localized rows, the settings diagnosis
(non-default values, recent dashboard changes, ranked findings), and the checks a
proposed value passes against its spec. A new setting lookup or check goes here.

Served through :mod:`kiro_crew.dashboard.change_cards`, which callers import and tests
patch; its mirror (:mod:`kiro_crew._facade_mirror`) carries such a patch to this module,
and this module never imports it.
"""

from __future__ import annotations

import functools
import json
import logging
import re
from pathlib import Path
from typing import Any

from kiro_crew import change_card_catalog as catalog
from kiro_crew.config.schema import requires_restart
from kiro_crew.dashboard.change_cards_records import _MESSAGE_MAX, _jsonable

logger = logging.getLogger("kiro_crew.dashboard.change_cards")


# ── current-state readers ──


def _config_value(path: str) -> Any:
    from kiro_crew.config.loader import KiroCrewConfig

    obj: Any = KiroCrewConfig.load()
    for segment in path.split("."):
        if isinstance(obj, dict):
            obj = obj.get(segment)
        else:
            obj = getattr(obj, segment, None)
        if obj is None:
            return None
    return _jsonable(obj)


_REGISTRY_PATH = Path(__file__).resolve().parents[1] / "docs" / "settings-registry.generated.json"
_FIND_LIMIT = 10
#: Most entries of a string-list setting find_setting echoes as its current value.
_FIND_LIST_ITEMS = 50
#: Id / config-key segments whose CURRENT VALUE is never echoed to the agent.
_SENSITIVE_SEGMENTS = frozenset(
    {"token", "secret", "password", "key", "credential", "credentials", "client", "id"}
)


@functools.lru_cache(maxsize=1)
def settings_registry() -> tuple[dict[str, Any], ...]:
    """The packaged Settings registry (id, label, tab, description, hint, configKey)."""
    try:
        data = json.loads(_REGISTRY_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("settings registry unreadable", exc_info=True)
        return ()
    entries = data.get("settings") if isinstance(data, dict) else None
    return tuple(e for e in entries or () if isinstance(e, dict) and isinstance(e.get("id"), str))


def _help_text(entry: dict[str, Any]) -> str:
    """All of a registry row's help, tip first: its ``hint`` (shown only behind
    the row's info tip) then its ``description`` (shown on the row). Settings
    search matches on both; every answer keeps them apart so a reader that
    quotes on-row copy can take ``description`` alone."""
    parts = (entry.get("hint"), entry.get("description"))
    return " ".join(str(p) for p in parts if isinstance(p, str) and p)


def _registry_entry(setting_id: str) -> dict[str, Any] | None:
    return next((e for e in settings_registry() if e["id"] == setting_id), None)


def _editable_config() -> dict[str, dict[str, Any]]:
    from kiro_crew.dashboard.handlers.core import _EDITABLE_CONFIG

    return _EDITABLE_CONFIG


def _dashboard_allowed(key: str) -> tuple[Any, ...]:
    if key == "default_memory_mode":
        from kiro_crew.dashboard.state import VALID_MEMORY_MODES

        return tuple(VALID_MEMORY_MODES)
    return catalog.DASHBOARD_KEYS[key]


def _dashboard_target(key: str, label: str) -> dict[str, Any]:
    return {
        "store": catalog.STORE_DASHBOARD,
        "key": key,
        "label": label,
        "allowed": list(_dashboard_allowed(key)),
    }


def spec_allowed_values(spec: dict[str, Any]) -> list[Any] | None:
    """The values a config-route setting accepts, when it is a closed set."""
    if spec.get("type") == "enum":
        return list(spec["values_fn"]()) if "values_fn" in spec else list(spec.get("values", []))
    if spec.get("type") == "bool":
        return [True, False]
    if spec.get("type") == "str" and "values" in spec:
        return list(spec["values"])
    return None


def _dashboard_list_target(key: str, label: str) -> dict[str, Any]:
    """A string-list dashboard setting, edited one item at a time."""
    return {"store": catalog.STORE_DASHBOARD, "key": key, "label": label, "list": True}


def _kirocrew_target(path: str, label: str, spec: dict[str, Any]) -> dict[str, Any]:
    target: dict[str, Any] = {"store": catalog.STORE_KIROCREW, "key": path, "label": label}
    allowed = spec_allowed_values(spec)
    if allowed is not None:
        target["allowed"] = allowed
    target["spec"] = spec
    return target


def resolve_setting(params: dict[str, Any]) -> dict[str, Any]:
    """Where a ``setting.change`` writes: ``{store, key, label, allowed?, spec?}``.

    A registry id resolves from data: a dashboard-config control the Settings page
    writes through ``PUT /api/dashboard/config``
    (``change_card_catalog.DASHBOARD_SETTINGS``), else its ``configKey`` when the
    config route accepts that key. Anything else has no write path a card may
    use. Raises :class:`kiro_crew.change_card_catalog.CardCatalogError`.
    """
    editable = _editable_config()
    err = catalog.CardCatalogError
    if "setting_id" in params:
        sid = params["setting_id"]
        entry = _registry_entry(sid)
        if entry is None:
            raise err("unknown_setting", f"no setting '{sid[:80]}'; search with find_setting")
        label = str(entry.get("label") or sid)
        if sid in catalog.DASHBOARD_SETTINGS:
            return _dashboard_target(catalog.DASHBOARD_SETTINGS[sid][0], label)
        if sid in catalog.DASHBOARD_LIST_SETTINGS:
            return _dashboard_list_target(catalog.DASHBOARD_LIST_SETTINGS[sid][0], label)
        key = entry.get("configKey")
        if isinstance(key, str) and key in editable:
            return _kirocrew_target(key, label, editable[key])
        if isinstance(key, str) and key.startswith("dashboard."):
            suffix = key.split(".", 1)[1]
            if suffix in catalog.DASHBOARD_KEYS:
                return _dashboard_target(suffix, label)
        raise err(
            "no_write_path",
            f"'{label}' cannot be changed by a card; offer the guide's settings.show "
            f"action ('{sid}') so the user changes it on the Settings page",
        )
    path = params["path"]
    if path in editable:
        return _kirocrew_target(path, path, editable[path])
    if path.startswith("dashboard.") and path.split(".", 1)[1] in catalog.DASHBOARD_KEYS:
        return _dashboard_target(path.split(".", 1)[1], path)
    if path.startswith("dashboard.") and path.split(".", 1)[1] in catalog.DASHBOARD_LIST_KEYS:
        return _dashboard_list_target(path.split(".", 1)[1], path)
    raise err("setting_not_editable", f"'{path}' is not a dashboard setting")


def read_setting_value(target: dict[str, Any]) -> Any:
    """The current value from the same store the card writes."""
    if target["store"] == catalog.STORE_DASHBOARD:
        return _config_value(f"dashboard.{target['key']}")
    return _config_value(target["key"])


def check_target_params(target: dict[str, Any], params: dict[str, Any]) -> None:
    """Refuse a ``value`` on a string-list setting and an ``op`` on any other."""
    err = catalog.CardCatalogError
    if target.get("list"):
        if "op" not in params:
            raise err(
                "list_setting",
                f"'{target['label']}' is a list; pass 'op' ('add' or 'remove') and one 'item'",
            )
        return
    if "op" in params:
        raise err("not_a_list", f"'{target['label']}' is not a list; pass 'value' instead")
    check_target_value(target, params["value"])


def check_target_value(target: dict[str, Any], value: Any) -> None:
    allowed = target.get("allowed")
    if allowed is not None and not catalog.value_allowed(value, allowed):
        raise catalog.CardCatalogError("invalid_value", f"value must be one of {allowed}")
    if "spec" in target:
        check_setting_value(target["spec"], value)


def _stem(word: str) -> str:
    for suffix, repl in (("ies", "y"), ("ing", ""), ("er", ""), ("ed", ""), ("es", ""), ("s", "")):
        if len(word) > len(suffix) + 2 and word.endswith(suffix):
            return word[: -len(suffix)] + repl
    return word


def _value_withheld(entry: dict[str, Any]) -> bool:
    text = f"{entry.get('id', '')}.{entry.get('configKey', '')}".lower()
    return bool(set(re.split(r"[-._%:]+", text)) & _SENSITIVE_SEGMENTS)


def find_settings(query: str) -> list[dict[str, Any]]:
    """Up to ten registry matches for *query*, best first. Never a credential value."""
    words = [_stem(w) for w in re.findall(r"[a-z0-9]+", query.lower()) if len(w) > 1]
    if not words:
        return []
    scored = []
    entries = settings_registry()
    hays = []
    for entry in entries:
        title = f"{entry['id']} {entry.get('label', '')}".lower()
        hay = f"{title} {_help_text(entry)} {entry.get('configKey', '')}".lower()
        hays.append((title, hay))
    # How many entries each word reaches: a tie goes to the entry whose words are
    # rarer, so "verbosity" outranks a "reply" that a dozen channel settings share.
    reach = {w: sum(1 for _t, hay in hays if w in hay) or 1 for w in words}
    for entry, (title, hay) in zip(entries, hays):
        hit = [w for w in words if w in hay]
        score = sum(3 if w in title else 1 for w in hit)
        if entry["id"] == query.strip():
            score += 100
        if score:
            scored.append((score, sum(1 / reach[w] for w in hit), entry))
    scored.sort(key=lambda row: (-row[0], -row[1], row[2]["id"]))
    out = []
    for _score, _rarity, entry in scored[:_FIND_LIMIT]:
        row: dict[str, Any] = {
            "setting_id": entry["id"],
            "label": entry.get("label", ""),
            # On-row text and tip text stay apart, as the registry keeps them:
            # `description` is what the row shows, `hint` sits behind its tip.
            "description": str(entry.get("description") or "")[:400],
            "tab": entry.get("tab", ""),
            "writable": False,
            "current_value": None,
        }
        try:
            target: dict[str, Any] | None = resolve_setting({"setting_id": entry["id"]})
        except catalog.CardCatalogError:
            target = None
        if isinstance(entry.get("hint"), str) and entry["hint"]:
            row["hint"] = entry["hint"][:400]
        if target is not None:
            row["writable"] = True
            if target.get("store") == catalog.STORE_KIROCREW and requires_restart(
                str(target.get("key") or "")
            ):
                # Most settings apply live; only a schema-marked one waits for a
                # restart, so the agent names a restart only when this says so.
                row["restart_required"] = True
            if target.get("allowed") is not None:
                row["allowed_values"] = target["allowed"]
            if target.get("list"):
                row["value_type"] = "string_list"
                row["ops"] = list(catalog._LIST_OPS)
            if not _value_withheld(entry):
                value = read_setting_value(target)
                if target.get("list"):
                    row["current_value"] = catalog.string_items(value)[:_FIND_LIST_ITEMS]
                else:
                    row["current_value"] = None if isinstance(value, (dict, list)) else value
        out.append(row)
    return out


def localize_setting_rows(rows: list[dict[str, Any]], ui_lang: str) -> list[dict[str, Any]]:
    """*rows* from :func:`find_settings` with labels as the user's screen shows them.

    The registry is English. Each row the dashboard location index holds gets
    its on-screen ``label`` and Settings ``path`` in *ui_lang* (English when
    that is blank or not shipped), plus ``label_locale``; a row the index does
    not hold keeps its registry label. Never raises.
    """
    from kiro_crew.ui_index import setting_labels

    try:
        shown = setting_labels([str(r.get("setting_id") or "") for r in rows], ui_lang or None)
    except Exception:  # a label lookup must never fail the search
        logger.debug("setting label lookup failed", exc_info=True)
        return rows
    locale = ui_lang or "en"
    out = []
    for row in rows:
        hit = shown.get(str(row.get("setting_id") or ""))
        if hit is None:
            out.append(row)
            continue
        out.append({**row, "label": hit["label"], "path": hit["path"], "label_locale": locale})
    return out


# ── settings diagnosis ──

#: Most ``Dashboard: ...`` history lines one diagnosis returns, newest first.
DIAGNOSE_RECENT_MAX = 50
#: How far back the diagnosis reads Global memory history for those lines.
_DIAGNOSE_HISTORY_DAYS = 90
#: A single value longer than this (as JSON) is summarized instead of echoed.
_DIAGNOSE_VALUE_MAX = 400
#: The whole diagnosis, as JSON, stays under this many characters.
DIAGNOSE_OUTPUT_MAX = 48_000
_DASHBOARD_LINE = "Dashboard: "
_KIROCREW_STORE = "kirocrew"
_DASHBOARD_PREFIX = "dashboard."


def _key_withheld(key: str, setting_id: str = "") -> bool:
    """The :func:`find_settings` credential test, applied to one config key path.

    A config path also splits on ``_`` (``slack.bot_token``), because a dotted
    path has no registry id whose hyphens would otherwise carry the split.
    """
    if _value_withheld({"id": setting_id, "configKey": key}):
        return True
    return _value_withheld({"id": "", "configKey": key.replace("_", ".")})


def _scrub(value: Any) -> Any:
    """*value* with every credential-like nested key reduced to ``{set: bool}``
    and every string run through the credential redactor."""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if _key_withheld(str(k)):
                out[str(k)] = {"set": v not in (None, "", [], {})}
            else:
                out[str(k)] = _scrub(v)
        return out
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    if isinstance(value, str) and value:
        from kiro_crew.platform import redact_via_context

        return redact_via_context(value)
    return value


def _bounded(value: Any) -> Any:
    """*value*, or a short summary of it when its JSON is longer than the cap."""
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    if len(text) <= _DIAGNOSE_VALUE_MAX:
        return value
    summary: dict[str, Any] = {"truncated": True, "type": type(value).__name__}
    if isinstance(value, dict):
        summary["size"] = len(value)
        summary["keys"] = sorted(str(k) for k in value)[:20]
    elif isinstance(value, list):
        summary["size"] = len(value)
    else:
        summary["preview"] = text[: _DIAGNOSE_VALUE_MAX // 2]
    return summary


def _flatten_like(default: Any, current: Any, prefix: str, out: dict[str, tuple[Any, Any]]) -> None:
    """Pair leaves of *current* with *default*, descending only where the default
    itself is a non-empty mapping; an empty default (a roster, a free-form map)
    stays one leaf so a whole collection compares as one setting."""
    if isinstance(default, dict) and default:
        cur = current if isinstance(current, dict) else {}
        for key in sorted(set(default) | set(cur), key=str):
            path = f"{prefix}.{key}" if prefix else str(key)
            _flatten_like(default.get(key), cur.get(key), path, out)
        return
    out[prefix] = (default, current)


def _registry_by_config_key() -> dict[str, dict[str, Any]]:
    by_key: dict[str, dict[str, Any]] = {}
    for entry in settings_registry():
        key = entry.get("configKey")
        if isinstance(key, str) and key:
            by_key.setdefault(key, entry)
    for sid, (dkey, _allowed) in catalog.DASHBOARD_SETTINGS.items():
        dash_entry = _registry_entry(sid)
        if dash_entry is not None:
            by_key.setdefault(f"{_DASHBOARD_PREFIX}{dkey}", dash_entry)
    return by_key


def non_default_settings() -> list[dict[str, Any]]:
    """Every config and dashboard-config leaf whose value differs from its default.

    The defaults are the dataclass defaults (``KiroCrewConfig()``), the same ones
    ``config.json`` falls back to, compared with the loaded configuration.
    """
    from kiro_crew.config.loader import KiroCrewConfig

    pairs: dict[str, tuple[Any, Any]] = {}
    _flatten_like(
        _jsonable(KiroCrewConfig().to_dict()), _jsonable(KiroCrewConfig.load().to_dict()), "", pairs
    )
    registry = _registry_by_config_key()
    rows = []
    for key, (default, current) in pairs.items():
        if default == current:
            continue
        entry = registry.get(key)
        row: dict[str, Any] = {
            "key": key,
            "store": (
                catalog.STORE_DASHBOARD if key.startswith(_DASHBOARD_PREFIX) else _KIROCREW_STORE
            ),
        }
        if entry is not None:
            row["setting_id"] = entry["id"]
            row["label"] = str(entry.get("label") or "")
        if _key_withheld(key, row.get("setting_id", "")):
            row["current"] = {"set": current not in (None, "", [], {})}
            row["default"] = {"set": default not in (None, "", [], {})}
        else:
            row["current"] = _bounded(_scrub(current))
            row["default"] = _bounded(_scrub(default))
        rows.append(row)
    return rows


def recent_dashboard_changes(limit: int = DIAGNOSE_RECENT_MAX) -> list[dict[str, Any]]:
    """The newest ``Dashboard: ...`` lines in Global memory history, newest first.

    Those are the lines :func:`kiro_crew.dashboard.handlers.change_cards._record_memory`
    writes for every owner mutation through a settings route, by hand or by card.
    """
    from datetime import date, timedelta

    from kiro_crew.context import ContextBuilder
    from kiro_crew.memory_stores import DEFAULT_MEMORY_STORE

    since = date.today() - timedelta(days=_DIAGNOSE_HISTORY_DAYS)
    # The operator's own Global history, deliberately: that is where the
    # "Dashboard: ..." lines are written, whichever member is asking.
    memory = ContextBuilder.get_memory_for(memory_store=DEFAULT_MEMORY_STORE)
    entries = memory.read_history_entries(since=since)
    found: list[dict[str, Any]] = []
    for entry in reversed(entries or []):
        day = str(entry.get("date") or "")
        lines: list[dict[str, Any]] = []
        time_label = ""
        for raw in str(entry.get("content") or "").splitlines():
            line = raw.strip()
            if line.startswith("#### "):
                time_label = line[5:].strip()
            elif line.startswith(_DASHBOARD_LINE):
                lines.append({"date": day, "time": time_label, "line": line[:_MESSAGE_MAX]})
        found.extend(reversed(lines))
        if len(found) >= limit:
            break
    return found[: max(0, min(limit, DIAGNOSE_RECENT_MAX))]


#: Probe statuses, most severe first (``diagnose_probes`` sorts the same way).
_FINDING_RANK = {"problem": 0, "warn": 1, "unknown": 2, "ok": 3}

#: Words a diagnosis topic carries that name no setting ("why did my ... change").
_TOPIC_STOPWORDS = frozenset(
    "the and for but not you your our are was were has have had did does doing done "
    "why what when where which who how this that these those with from into onto "
    "about after before again just still can cant could would should will wont "
    "any all some its it's mine there their them then than also very much more "
    "now new old get got set setting settings change changed changes changing "
    "option options value values thing things work working broken stop stopped "
    "anymore suddenly someone something happen happened".split()
)


def _topic_tokens(topic: str) -> list[str]:
    """The meaningful, stemmed words of *topic*: lower-cased, split on anything
    that is not a letter or digit, stopwords and words under three letters dropped."""
    words = re.findall(r"[a-z0-9]+", topic.lower())
    out: list[str] = []
    for word in words:
        if len(word) < 3 or word in _TOPIC_STOPWORDS:
            continue
        stem = _stem(word)
        if stem not in out:
            out.append(stem)
    return out


def _token_hits(tokens: list[str], *texts: Any) -> int:
    """How many of *tokens* occur in any of *texts*, case-insensitively."""
    hay = " ".join(str(t) for t in texts if t).lower()
    return sum(1 for tok in tokens if tok in hay)


def _row_hits(tokens: list[str], row: dict[str, Any]) -> int:
    entry = _registry_entry(row["setting_id"]) if row.get("setting_id") else None
    extra = (_help_text(entry), entry.get("tab")) if entry else ()
    return _token_hits(tokens, row["key"], row.get("setting_id"), row.get("label"), *extra)


def _finding_hits(tokens: list[str], item: dict[str, Any]) -> int:
    evidence = item.get("evidence")
    sid = evidence.get("setting_id") if isinstance(evidence, dict) else None
    entry = _registry_entry(sid) if isinstance(sid, str) else None
    label = entry.get("label") if entry else None
    return _token_hits(tokens, item.get("id"), item.get("summary"), sid, label)


def _ranked(items: list[dict[str, Any]], hits: Any) -> list[dict[str, Any]]:
    """The items with at least one token hit, most hits first (stable otherwise)."""
    scored = [(hits(item), i, item) for i, item in enumerate(items)]
    return [item for n, _i, item in sorted(scored, key=lambda s: (-s[0], s[1])) if n]


def diagnose_settings(
    topic: str = "", app: Any = None, *, include_history: bool = True
) -> dict[str, Any]:
    """``{findings, non_default, recent_changes, truncated[, topic_matched]}``.

    ``findings`` come from :mod:`kiro_crew.diagnose_probes`, problems first.
    *topic* keeps rows and ``ok`` findings that match any of its meaningful
    words; a non-``ok`` finding is always kept, and a topic that matches
    nothing returns the unfiltered result with ``topic_matched: false``. With
    ``include_history`` false Global memory history is not read. A
    credential-like setting reports only whether it is set; the JSON is cut to
    :data:`DIAGNOSE_OUTPUT_MAX` characters and ``truncated`` says so.
    """
    from kiro_crew.diagnose_probes import run_probes

    topic = (topic or "").strip()
    findings = run_probes(app, topic)
    rows = non_default_settings()
    changes = recent_dashboard_changes() if include_history else []
    result: dict[str, Any] = {
        "findings": findings,
        "non_default": rows,
        "recent_changes": changes,
        "truncated": False,
    }
    tokens = _topic_tokens(topic) if topic else []
    if topic:
        f_rows = _ranked(rows, lambda r: _row_hits(tokens, r))
        f_changes = _ranked(changes, lambda c: _token_hits(tokens, c["line"]))
        matched_findings = _ranked(findings, lambda f: _finding_hits(tokens, f))
        matched = bool(f_rows or f_changes or matched_findings)
        result["topic_matched"] = matched
        if matched:
            kept = {id(f) for f in matched_findings}
            others = [f for f in findings if id(f) not in kept and f.get("status") != "ok"]
            result["findings"] = sorted(
                matched_findings + others,
                key=lambda f: _FINDING_RANK.get(str(f.get("status")), 9),
            )
            result["non_default"] = f_rows
            result["recent_changes"] = f_changes
    while len(json.dumps(result, ensure_ascii=False, default=str)) > DIAGNOSE_OUTPUT_MAX:
        result["truncated"] = True
        if result["non_default"]:
            result["non_default"].pop()
        elif result["recent_changes"]:
            result["recent_changes"].pop()
        elif result["findings"]:
            result["findings"].pop()
        else:
            break
    return result


def setting_spec(path: str) -> dict[str, Any]:
    """The editable-config entry for *path*, as the settings route enforces it."""
    from kiro_crew.dashboard.handlers.core import _EDITABLE_CONFIG

    spec = _EDITABLE_CONFIG.get(path)
    if not spec:
        raise catalog.CardCatalogError(
            "setting_not_editable", f"'{path}' is not a dashboard setting"
        )
    return spec


def check_setting_value(spec: dict[str, Any], value: Any) -> None:
    """The same shape checks the settings route runs, so a card fails up front."""
    kind = spec.get("type")
    err = catalog.CardCatalogError
    if kind == "enum":
        allowed = list(spec["values_fn"]()) if "values_fn" in spec else spec.get("values", [])
        if value not in allowed:
            raise err("invalid_value", f"value must be one of {allowed}")
    elif kind == "bool":
        if not isinstance(value, bool):
            raise err("invalid_value", "value must be true or false")
    elif kind in ("int", "float"):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise err("invalid_value", "value must be a number")
        lo, hi = spec.get("min", 0), spec.get("max", 999999)
        if value < lo or value > hi:
            raise err("invalid_value", f"value must be between {lo} and {hi}")
    elif kind == "str":
        if not isinstance(value, str) or len(value) > spec.get("max_len", 256):
            raise err("invalid_value", "value must be a short string")
        if "values" in spec and value not in spec["values"]:
            raise err("invalid_value", f"value must be one of {spec['values']}")
        pattern = spec.get("pattern")
        if pattern and not re.fullmatch(pattern, value):
            raise err("invalid_value", "value has an invalid format")
    elif kind == "str_list":
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise err("invalid_value", "value must be a list of strings")
        if len(value) > spec["max_items"]:
            raise err("invalid_value", f"value must be at most {spec['max_items']} items")
        if any(len(item) > spec["max_item_len"] for item in value):
            raise err(
                "invalid_value", f"each item must be at most {spec['max_item_len']} characters"
            )
    elif kind == "dict":
        if not isinstance(value, dict) or set(value) != set(spec.get("keys", {})):
            raise err("invalid_value", "value must be a record with every declared key")
