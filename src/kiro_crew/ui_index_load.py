"""Reading the packaged dashboard-location index: validation, the auto-tier merge and the cache.

``load_index`` reads the committed index and the optional build-time auto tier,
validates every location (``_check_location``, ``_parse``), merges the auto tier
only after checking all of it (``_attach_auto``), and keeps one parsed index per
(file, mtime, size) of both files in ``_cache``. A malformed committed index raises
``IndexUnavailable``; an unusable auto tier raises ``AutoTierUnavailable`` and
leaves the committed tiers answering.

:mod:`kiro_crew.ui_index` stays the import path and the patch surface for every name
here, and this module logs under that name. It imports only
:mod:`kiro_crew.ui_index_match`, never the facade.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from kiro_crew.ui_index_match import (
    _AUTO,
    _AUTO_ARTIFACT,
    _AUTO_AVAILABLE,
    _CAUTION,
    _CURATED,
    _DESCRIPTION,
    _GUIDABLE_POLICIES,
    _GUIDE_POLICIES,
    _LITERAL_PREFIX,
    _REQUIREMENT_KINDS,
    _TIERS,
    AUTO_INDEX_PATH,
    DEFAULT_LOCALE,
    INDEX_PATH,
    MAX_INDEX_BYTES,
    SCHEMA_VERSION,
    SHELL_SURFACE,
    _Index,
    _Text,
)

# Logs under the facade's name: a record names the import path callers use.
logger = logging.getLogger("kiro_crew.ui_index")


_cache: dict[str, Any] = {"key": None, "index": None}


class IndexUnavailable(Exception):
    """The packaged index is missing, unreadable, malformed or of an unknown schema."""


# Tracebacks and logs name it by the import path its callers use.
IndexUnavailable.__module__ = "kiro_crew.ui_index"


class AutoTierUnavailable(Exception):
    """The auto-tier artifact cannot be used; the committed index still answers."""


# Tracebacks and logs name it by the import path its callers use.
AutoTierUnavailable.__module__ = "kiro_crew.ui_index"


def _str_map(value: Any, what: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in value.items()
    ):
        raise IndexUnavailable(f"malformed {what}")
    return value


def _str_list(value: Any, what: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        raise IndexUnavailable(f"malformed {what}")
    return value


def _check_location(
    loc: dict[str, Any],
    ids: set[str],
    english: dict[str, str],
    reveal_states: dict[str, str],
    shell_ids: set[str],
    described: set[str],
    conditions: dict[str, str],
) -> None:
    """Every field the search and the records read, with every reference resolved.

    Raises :class:`IndexUnavailable` on the first problem: a structurally broken
    index is reported as unavailable, never answered from partially.
    """
    key = loc.get("label_key")
    if not isinstance(key, str) or not (key.startswith(_LITERAL_PREFIX) or key in english):
        raise IndexUnavailable("location label is missing")
    if not isinstance(loc.get("kind"), str):
        raise IndexUnavailable("malformed location")
    tier = loc.get("tier", _CURATED)
    if tier not in _TIERS:
        raise IndexUnavailable("unknown location tier")
    if "conditions_unknown" in loc and loc["conditions_unknown"] is not True:
        raise IndexUnavailable("malformed location")
    if tier == _AUTO and (loc.get("conditions_unknown") is not True or "terms" in loc):
        # An auto entry is searchable by its label alone and never claims to
        # know its prerequisites: a curated term or a missing flag is a broken index.
        raise IndexUnavailable("malformed auto location")
    if loc.get("label_kind", _DESCRIPTION) != _DESCRIPTION:
        raise IndexUnavailable("unknown label kind")
    for k in _str_list(loc.get("alias_keys", []), "alias keys"):
        if not (k.startswith(_LITERAL_PREFIX) or k in english):
            raise IndexUnavailable("location alias label is missing")
    if "caution_key" in loc:
        ck = loc["caution_key"]
        if not isinstance(ck, str) or ck not in english or loc.get("guide_policy") != _CAUTION:
            # The page's own words for what a destructive control removes:
            # only on a caution location, and only a key the catalog carries.
            raise IndexUnavailable("malformed caution text")
    if "setting_id" in loc and not isinstance(loc["setting_id"], str):
        raise IndexUnavailable("malformed location")
    if "guide_ref" in loc and not isinstance(loc["guide_ref"], dict):
        raise IndexUnavailable("malformed guide binding")
    if "guide_plan" in loc and not isinstance(loc["guide_plan"], dict):
        raise IndexUnavailable("malformed guide plan")
    policy = loc.get("guide_policy")
    if policy is not None and policy not in _GUIDE_POLICIES:
        raise IndexUnavailable("unknown guide policy")
    if tier == _AUTO and "guide_plan" in loc and policy not in _GUIDABLE_POLICIES:
        # Only a guidable location is ever planned: a plan on a search-only
        # or denied auto entry is a broken artifact, not a guide to offer.
        raise IndexUnavailable("malformed auto location")
    if "state_labels" in loc:
        states = loc["state_labels"]
        own = {key, *loc.get("alias_keys", [])}
        if (
            not isinstance(states, list)
            or len(states) < 2
            or not all(
                isinstance(s, dict)
                and s.get("label_key") in own
                and isinstance(s.get("when"), str)
                and (s["when"] in conditions or s["when"] in reveal_states)
                for s in states
            )
        ):
            raise IndexUnavailable("malformed state labels")
    placements = loc.get("placements")
    if not isinstance(placements, list) or not placements:
        raise IndexUnavailable("location has no placements")
    for p in placements:
        if not isinstance(p, dict):
            raise IndexUnavailable("malformed placement")
        route = p.get("route")
        if not isinstance(p.get("surface_id"), str) or not isinstance(p.get("entry_kind"), str):
            raise IndexUnavailable("malformed placement")
        parent_ids = _str_list(p.get("parent_ids"), "placement path")
        if not all(pid in ids for pid in parent_ids):
            raise IndexUnavailable("placement path names an unknown location")
        if any(pid in described for pid in parent_ids):
            raise IndexUnavailable("placement path names a location with no label")
        if p["surface_id"] == SHELL_SURFACE:
            # Shell chrome is drawn on every page: no route, and a path only
            # through other shell chrome (the phone menu button), never a page.
            if route != "" or not all(pid in shell_ids for pid in parent_ids):
                raise IndexUnavailable("malformed shell placement")
        elif not isinstance(route, str) or not route.startswith("/"):
            raise IndexUnavailable("placement has no route")
        requires = p.get("requires")
        if not isinstance(requires, list):
            raise IndexUnavailable("malformed requirements")
        for r in requires:
            if not isinstance(r, dict) or r.get("kind") not in _REQUIREMENT_KINDS:
                raise IndexUnavailable("malformed requirement")
            if r["kind"] in ("shown_by", "preview_flag") and r.get("location") not in ids:
                raise IndexUnavailable("requirement names an unknown location")
            if r["kind"] == "shown_by" and r["location"] in described:
                raise IndexUnavailable("requirement names a location with no label")
            if "when" in r and r["when"] not in reveal_states:
                raise IndexUnavailable("requirement names an unknown reveal state")
            if r["kind"] == "condition" and not isinstance(r.get("id"), str):
                raise IndexUnavailable("malformed requirement")


def _parse(raw: Any) -> _Index:
    if not isinstance(raw, dict) or raw.get("schema_version") != SCHEMA_VERSION:
        raise IndexUnavailable("unsupported index schema")
    locations, labels, locales = raw.get("locations"), raw.get("labels"), raw.get("locales")
    if (
        not isinstance(locations, list)
        or not isinstance(labels, dict)
        or not isinstance(locales, list)
    ):
        raise IndexUnavailable("malformed index")
    if DEFAULT_LOCALE not in labels:
        raise IndexUnavailable("index has no English labels")
    for name, table in labels.items():
        _str_map(table, f"labels for {name!r:.20}")
    _str_list(locales, "locales")
    _str_list(raw.get("surfaces") or [], "surfaces")
    conditions = _str_map(raw.get("conditions"), "conditions")
    reveal_states = _str_map(raw.get("reveal_states"), "reveal states")
    by_id: dict[str, dict[str, Any]] = {}
    for loc in locations:
        if not isinstance(loc, dict) or not isinstance(loc.get("id"), str) or not loc["id"]:
            raise IndexUnavailable("malformed location")
        if loc["id"] in by_id:
            raise IndexUnavailable("duplicate location id")
        by_id[loc["id"]] = loc
    ids = set(by_id)
    shell_ids = {
        loc_id
        for loc_id, loc in by_id.items()
        if isinstance(loc.get("placements"), list)
        and loc["placements"]
        and all(
            isinstance(p, dict) and p.get("surface_id") == SHELL_SURFACE for p in loc["placements"]
        )
    }
    described = {loc_id for loc_id, loc in by_id.items() if loc.get("label_kind") == _DESCRIPTION}
    for loc in locations:
        if loc.get("tier") == _AUTO:
            # The auto tier is a build-time artifact (AUTO_INDEX_PATH); the
            # committed index carrying it would mean two sources for one tier.
            raise IndexUnavailable("the committed index carries auto entries")
        _check_location(
            loc, ids, labels[DEFAULT_LOCALE], reveal_states, shell_ids, described, conditions
        )
    idx = _Index(
        raw=raw,
        by_id=by_id,
        locales=tuple(locales),
        surfaces=frozenset(raw.get("surfaces") or []),
        labels={str(k): v for k, v in labels.items()},
        conditions=conditions,
        reveal_states=reveal_states,
    )
    for loc_id, loc in by_id.items():
        idx.texts[loc_id] = _forms(idx.labels, loc)
    return idx


def _forms(labels: dict[str, dict[str, str]], loc: dict[str, Any]) -> dict[str, list[_Text]]:
    """A location's searchable forms per locale: label, aliases, then search terms."""
    keys = [loc["label_key"], *[k for k in loc.get("alias_keys") or [] if isinstance(k, str)]]
    terms = loc.get("terms") or {}
    if not isinstance(terms, dict) or not all(
        isinstance(v, list) and all(isinstance(x, str) for x in v) for v in terms.values()
    ):
        raise IndexUnavailable("malformed search terms")
    per_locale: dict[str, list[_Text]] = {}
    for locale in labels:
        forms = [
            _Text.of(t, locale, key=k) for k, t in ((k, labels[locale].get(k)) for k in keys) if t
        ]
        forms += [_Text.of(t, locale, term=True) for t in terms.get(locale, []) if t.strip()]
        if forms:
            per_locale[locale] = forms
    return per_locale


def _attach_auto(idx: _Index, raw: Any) -> None:
    """Merge a build-time auto-tier artifact into ``idx``, or raise without touching it.

    Every check runs before anything is merged, so a refused artifact leaves
    the committed index exactly as parsed. The artifact must name this
    committed index (``base_input_digest``), ship the same locales, add only
    ``tier: auto`` entries (same per-entry validation as any location) whose
    paths run through committed pages and tabs, and add labels only for keys
    the committed index does not already define differently.
    """
    if (
        not isinstance(raw, dict)
        or raw.get("schema_version") != SCHEMA_VERSION
        or raw.get("artifact") != _AUTO_ARTIFACT
    ):
        raise AutoTierUnavailable("unsupported auto tier schema")
    if raw.get("base_input_digest") != idx.raw.get("input_digest"):
        raise AutoTierUnavailable("auto tier was built against a different index")
    locations, labels, locales = raw.get("locations"), raw.get("labels"), raw.get("locales")
    if (
        not isinstance(locations, list)
        or not isinstance(labels, dict)
        or locales != list(idx.locales)
    ):
        raise AutoTierUnavailable("malformed auto tier")
    try:
        merged: dict[str, dict[str, str]] = {}
        for locale, table in idx.labels.items():
            extra = _str_map(labels.get(locale), "auto labels")
            for k, v in extra.items():
                if table.get(k, v) != v:
                    raise AutoTierUnavailable("auto tier relabels a committed key")
            merged[locale] = {**table, **extra}
        if set(labels) - set(idx.labels):
            raise AutoTierUnavailable("auto tier has labels for an unknown locale")
        new: dict[str, dict[str, Any]] = {}
        for loc in locations:
            if not isinstance(loc, dict) or not isinstance(loc.get("id"), str) or not loc["id"]:
                raise AutoTierUnavailable("malformed auto location")
            if loc["id"] in idx.by_id or loc["id"] in new:
                raise AutoTierUnavailable("duplicate location id")
            if loc.get("tier") != _AUTO:
                raise AutoTierUnavailable("auto tier carries a non-auto location")
            new[loc["id"]] = loc
        committed = set(idx.by_id)
        described = {i for i, loc in idx.by_id.items() if loc.get("label_kind") == _DESCRIPTION}
        # An auto entry may be ON the shell (a control only the app shell draws:
        # no route, no path) but never hangs under shell chrome.
        shell_ids: set[str] = set()
        for loc in new.values():
            _check_location(
                loc,
                committed,
                merged[DEFAULT_LOCALE],
                idx.reveal_states,
                shell_ids,
                described,
                idx.conditions,
            )
        texts = {loc_id: _forms(merged, loc) for loc_id, loc in new.items()}
    except IndexUnavailable as exc:
        raise AutoTierUnavailable(str(exc)) from exc
    idx.labels = merged
    idx.by_id.update(new)
    idx.texts.update(texts)
    idx.contexts.clear()
    cov = raw.get("coverage")
    idx.auto_scope = cov.get("scope") if isinstance(cov, dict) else None
    idx.auto_status, idx.auto_reason = _AUTO_AVAILABLE, None


def _stat_key(target: Path) -> tuple[str, int, int] | None:
    try:
        st = target.stat()
    except OSError:
        return None
    return (str(target), st.st_mtime_ns, st.st_size)


def _read_auto(idx: _Index, target: Path | None) -> None:
    """Merge the auto tier from ``target`` when it is there and sound; else say why not."""
    if target is None:
        idx.auto_reason = "no auto tier file was read"
        return
    try:
        st = target.stat()
    except OSError:
        idx.auto_reason = "no auto tier in this install (the dashboard bundle was not built)"
        return
    try:
        if st.st_size > MAX_INDEX_BYTES:
            raise AutoTierUnavailable("auto tier file is too large")
        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise AutoTierUnavailable("auto tier file is unreadable") from exc
        _attach_auto(idx, raw)
    except AutoTierUnavailable as exc:
        # The committed tiers still answer; only the auto entries are missing.
        logger.warning("find_ui auto tier unavailable: %s", exc)
        idx.auto_reason = str(exc)


def load_index(path: Path | None = None, auto_path: Path | None = None) -> _Index:
    """The parsed index, cached per (file, mtime, size) of both files; one entry, process-wide.

    ``auto_path`` defaults to :data:`AUTO_INDEX_PATH` only when ``path`` does
    too: an explicit committed index (a test, a scratch build) is read with
    the auto tier the caller names, or none. The cache holds packaged build
    data only, never anything about a caller.
    """
    target = path or INDEX_PATH
    auto_target = auto_path
    if auto_target is None and path is None:
        auto_target = AUTO_INDEX_PATH
    key = _stat_key(target)
    if key is None:
        raise IndexUnavailable("index file is missing")
    full_key = (key, _stat_key(auto_target) if auto_target is not None else None)
    if _cache["key"] == full_key and _cache["index"] is not None:
        return _cache["index"]  # type: ignore[no-any-return]
    if key[2] > MAX_INDEX_BYTES:
        raise IndexUnavailable("index file is too large")
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise IndexUnavailable("index file is unreadable") from exc
    idx = _parse(raw)
    _read_auto(idx, auto_target)
    _cache["key"], _cache["index"] = full_key, idx
    return idx
