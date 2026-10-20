"""Turning one indexed location into an answer record.

Locale resolution, the localized label of a location and of its path, what must be
true for it to show (``_requirement``), the label a state shows it under, the
``ui.find`` reference a guide can bind to it (``_find_ref``) and the page opener a
planned guide starts from -- assembled per location by ``_record``.

:mod:`kiro_crew.ui_index` stays the import path and the patch surface for every name
here. This module imports only :mod:`kiro_crew.ui_index_match`, never the facade.
"""

from __future__ import annotations

from typing import Any

from kiro_crew.ui_index_match import (
    _AUTO,
    _CAUTION,
    _CURATED,
    _DASHBOARD_SOURCE,
    _DESCRIPTION,
    _GUIDABLE_POLICIES,
    _LITERAL_PREFIX,
    _POINT,
    DEFAULT_LOCALE,
    SHELL_SURFACE,
    _bigrams,
    _Index,
    _Text,
    normalize,
)


def _resolve_locale(lang: str | None, available: tuple[str, ...]) -> tuple[str, str]:
    """(resolved locale, source): ``requested`` when shipped, else English ``fallback``."""
    if not lang:
        return DEFAULT_LOCALE, "fallback"
    want = lang.replace("_", "-").casefold()
    exact = [code for code in available if code.casefold() == want]
    if exact:
        return exact[0], "requested"
    base = want.split("-")[0]
    prefixed = [code for code in available if code.casefold().split("-")[0] == base]
    if len(prefixed) == 1:
        return prefixed[0], "requested"
    return DEFAULT_LOCALE, "fallback"


def _resolve_locales(
    lang: str | None, label_lang: str | None, available: tuple[str, ...]
) -> tuple[str, str, str]:
    """(question locale, label locale, label source).

    The question is read in *lang* (the user's words), but a label must be the
    one their screen shows: *label_lang*, the language their dashboard tab
    renders, wins for every label when it names a shipped locale, with source
    ``dashboard``. Without it the labels follow *lang*, as before.
    """
    question, source = _resolve_locale(lang or None, available)
    if label_lang:
        shown, shown_source = _resolve_locale(label_lang, available)
        if shown_source == "requested":
            return question, shown, _DASHBOARD_SOURCE
    return question, question, source


def _label(idx: _Index, key: str, locale: str) -> str:
    if key.startswith(_LITERAL_PREFIX):
        return key[len(_LITERAL_PREFIX) :]
    return idx.labels.get(locale, {}).get(key) or idx.labels[DEFAULT_LOCALE].get(key) or key


def _loc_label(idx: _Index, loc_id: str, locale: str) -> str:
    loc = idx.by_id.get(loc_id)
    return _label(idx, loc["label_key"], locale) if loc else loc_id


def _guidable(setting_id: str) -> bool:
    from kiro_crew.guide_catalog import guidable_settings

    return setting_id in guidable_settings()


def _guide_ref_ok(ref: dict[str, Any]) -> bool:
    """Whether the gateway's guide catalog accepts this binding, params included."""
    from kiro_crew.guide_catalog import GuideCatalogError, validate_actions

    params = ref.get("params", {})
    try:
        validate_actions([{"id": ref.get("action_id"), "params": params}])
    except (GuideCatalogError, TypeError, ValueError):
        return False
    return True


def _viewport(requires: list[dict[str, Any]]) -> Any:
    return next((r.get("value") for r in requires if r.get("kind") == "viewport"), None)


def _where(placement: dict[str, Any]) -> dict[str, Any]:
    """The placement's route, or ``on_every_page`` for the shell (no route there)."""
    if placement.get("surface_id") == SHELL_SURFACE:
        return {"on_every_page": True}
    return {"route": placement["route"]}


def _on_surface(placement: dict[str, Any], surface: str) -> bool:
    """Whether a placement is drawn on ``surface``: its own, or the shell's (everywhere)."""
    return placement.get("surface_id") in (surface, SHELL_SURFACE)


def _requirement(
    idx: _Index, req: dict[str, Any], locale: str, viewport: Any = None, depth: int = 0
) -> dict[str, Any]:
    kind = req.get("kind")
    if kind in ("shown_by", "preview_flag"):
        target = idx.by_id.get(str(req.get("location")))
        out: dict[str, Any] = {"kind": kind}
        if target:
            placements = target["placements"]
            # The revealing control's placement for the same viewport, so its own
            # qualifications ("desktop only", "with open sessions") travel along.
            placement = next(
                (p for p in placements if _viewport(p["requires"]) in (None, viewport)),
                placements[0],
            )
            out["label"] = _label(idx, target["label_key"], locale)
            out["path"] = [_loc_label(idx, p, locale) for p in placement["parent_ids"]] + [
                out["label"]
            ]
            out.update(_where(placement))
            if target.get("setting_id"):
                out["setting_id"] = target["setting_id"]
            if kind == "shown_by" and depth < 2:
                own = [
                    _requirement(idx, r, locale, viewport, depth + 1)
                    for r in placement["requires"]
                    if r.get("kind") != "viewport"
                ]
                if own:
                    out["requires"] = own
        when = req.get("when")
        if kind == "shown_by" and isinstance(when, str):
            # A conditional step: only while this state holds; otherwise the
            # target is already on screen and the step is skipped.
            out["when"] = when
            out["only_if"] = idx.reveal_states.get(when, when)
            out["otherwise"] = "already visible; skip this step"
        return out
    if kind == "viewport":
        return {"kind": kind, "value": req.get("value")}
    cid = req.get("id")
    out = {"kind": kind, "id": cid}
    if isinstance(cid, str) and cid in idx.conditions:
        out["description"] = idx.conditions[cid]
    return out


def _state_text(idx: _Index, when: str) -> str:
    return idx.conditions.get(when) or idx.reveal_states.get(when) or when


def _state_key(
    idx: _Index, loc: dict[str, Any], queries: list[_Text], matched_key: str | None
) -> str | None:
    """Which of a flipping label's keys the question is about.

    The label sharing the most words (CJK: two-character runs) with the question
    wins: "collapse the navigation" is asked while the rail shows Collapse, "切换
    看板" while the list shows 切换到看板视图. A tie keeps the label the question
    matched, else the primary one.
    """
    best: tuple[int, str | None] = (0, None)
    for s in loc.get("state_labels") or []:
        overlap = 0
        for q in queries:
            for code in dict.fromkeys((DEFAULT_LOCALE, *idx.labels)):
                text = idx.labels.get(code, {}).get(s["label_key"])
                if not text:
                    continue
                t = _Text.of(text, code)
                n = (
                    len(_bigrams(t.core) & _bigrams(q.core))
                    if (t.cjk or q.cjk)
                    else len(t.content & q.content)
                )
                overlap = max(overlap, n)
        if overlap > best[0]:
            best = (overlap, s["label_key"])
        elif overlap == best[0] and overlap and s["label_key"] == matched_key:
            best = (overlap, matched_key)
    return best[1] or matched_key


def _record(
    idx: _Index,
    loc: dict[str, Any],
    placements: list[dict[str, Any]],
    locale: str,
    basis: str,
    matched_key: str | None = None,
) -> dict[str, Any]:
    states = loc.get("state_labels") or []
    # A flipping label answers with the state the question named ("switch to
    # list view" -> the label shown in board view); otherwise the primary one.
    shown_key = (
        matched_key
        if matched_key and any(s["label_key"] == matched_key for s in states)
        else loc["label_key"]
    )
    text = _label(idx, shown_key, locale)
    # A description says what the control is; it is never its on-screen label.
    shown = {_DESCRIPTION: text} if loc.get("label_kind") == _DESCRIPTION else {"label": text}
    rec: dict[str, Any] = {
        "id": loc["id"],
        "kind": loc.get("kind"),
        **shown,
        "placements": [
            {
                "path": [
                    {
                        "role": "navigation",
                        "label": _loc_label(idx, pid, locale),
                        "id": pid,
                    }
                    for pid in p["parent_ids"]
                ]
                + [{"role": "target", **shown, "id": loc["id"]}],
                **_where(p),
                "entry": p.get("entry_kind"),
                "requires": [
                    _requirement(idx, r, locale, _viewport(p["requires"])) for r in p["requires"]
                ],
            }
            for p in placements
        ],
        "availability": "not_observed",
        "match": basis,
    }
    if loc.get("tier") == _AUTO:
        # A control with this label is drawn on this page; where on it, and
        # what must be true for it to show, is not known (see find_ui's doc).
        rec["tier"] = _AUTO
        rec["conditions_unknown"] = True
    if states:
        rec["label_by_state"] = [
            {
                "label": _label(idx, s["label_key"], locale),
                "when": s["when"],
                "only_if": _state_text(idx, s["when"]),
            }
            for s in states
        ]
    sid = loc.get("setting_id")
    if sid:
        rec["setting_id"] = sid
        if _guidable(sid):
            rec["guide_ref"] = {"action_id": "settings.show", "params": {"setting_id": sid}}
    elif isinstance(loc.get("guide_ref"), dict) and _guide_ref_ok(loc["guide_ref"]):
        # Checked here as well as at generation: a binding the catalog refuses
        # (an unknown action, or a sensitive setting) is never handed out.
        rec["guide_ref"] = loc["guide_ref"]
    elif (
        "guide_ref" not in loc
        and isinstance(loc.get("guide_plan"), dict)
        and (
            (
                loc.get("tier", _CURATED) == _CURATED
                and loc.get("guide_policy", _POINT) in _GUIDABLE_POLICIES
            )
            or (loc.get("tier") == _AUTO and loc.get("guide_policy") in _GUIDABLE_POLICIES)
        )
    ):
        # The generic pointer: the generator planned this location for ui.show
        # (never beside a binding of its own, which wins even when refused), and
        # the gateway's catalog (the packaged plans) must accept it too.
        ref = {"action_id": "ui.show", "params": {"location_id": loc["id"]}}
        if _guide_ref_ok(ref):
            rec["guide_ref"] = ref
    if loc.get("guide_policy") == _CAUTION:
        # A destructive control: offered only when the user asked for that
        # action, saying plainly what it removes (see find_ui's doc).
        rec["caution"] = True
        if isinstance(loc.get("caution_key"), str):
            # What it removes and keeps, in the page's own words: the only
            # account of its consequences the agent may give.
            rec["caution_text"] = _label(idx, loc["caution_key"], locale)
    if "guide_ref" not in rec and loc.get("kind") == "page":
        opener = _page_opener_plan(idx, loc)
        if opener is not None:
            # A page is reached by the control that opens it: when that is a
            # registered control the index plans a walk to (the top bar's
            # Developer entry), the guide ends on it, never on a search for a
            # control named like the page on the page itself.
            rec["guide_ref"] = {"action_id": "ui.show", "params": {"location_id": opener}}
    if "guide_ref" not in rec:
        # A described control (its on-screen text is live data, a model name)
        # is searched by its id; the name the guide panel quotes is its alias,
        # the plain word for what it is ("Model"), never the description.
        find_label = text
        if loc.get("label_kind") == _DESCRIPTION:
            aliases = loc.get("alias_keys") or []
            find_label = (
                _label(idx, str(aliases[0]), locale)
                if aliases and loc.get("id") in _DESCRIBED_FINDS
                else ""
            )
        find = _find_ref(idx, loc, placements, find_label)
        if find is not None:
            rec["find_ref"] = find
    return rec


#: The accessible role a ``ui.find`` narrows to, per location kind; a kind
#: missing here (a list, a field group) searches every role.
_FIND_ROLE_BY_KIND = {
    "button": "button",
    "disclosure": "button",
    "menu-item": "menuitem",
    "link": "link",
    "toggle": "switch",
    "tab": "tab",
    "field": "textbox",
}
#: Location kinds a ``ui.find`` never names: a list is not one control, and a
#: setting is reached through ``settings.show`` or not at all. A page is named
#: by the control that opens it (see :func:`_find_ref`).
_FIND_SKIP_KINDS = frozenset({"page", "setting", "list"})


#: Described controls a ``ui.find`` may name by their alias (see :func:`_record`).
#: Opt-in, never every described control: the approval-mode picker is also
#: described and aliased, and it is the agent's own ceiling, answered in words.
_DESCRIBED_FINDS = frozenset({"chat.model-picker", "composer.automation"})


def _find_ref(
    idx: _Index, loc: dict[str, Any], placements: list[dict[str, Any]], label: str
) -> dict[str, Any] | None:
    """A ``ui.find`` binding for a control the index has no planned guide for.

    The control's own label (the one the record shows, in the dashboard's
    language), the route of its first placement (none for a top-bar control)
    and its role, and for a registered control its location id, which the tab
    matches before any name, plus, when it is drawn in a menu, the registered
    control that opens it (``opener``); a page the rail opens is named by its rail entry,
    with no route. Never for a setting, a deny-listed location, a description
    (its on-screen text is live data), or anything the gateway's catalog
    refuses (a trust-root page, an unknown route, a name that widens what the
    agent may do). ``caution`` when the English label removes something.
    """
    # A page is pointed at by the control that opens it. The navigation rail
    # draws its entry on every page, so a rail page carries no route and the
    # guide stays where the person is; a page the rail has no entry for gets
    # no find (see below).
    page = loc.get("kind") == "page"
    rail_page = (
        page
        and bool(placements)
        and "rail" in (placements[0].get("entry"), placements[0].get("entry_kind"))
    )
    if (
        loc.get("setting_id")
        or loc.get("guide_policy") == "deny"
        or (loc.get("label_kind") == _DESCRIPTION and loc.get("id") not in _DESCRIBED_FINDS)
        or (loc.get("kind") in _FIND_SKIP_KINDS and not page)
        # A page with no entry in the rail (Logs, Webhooks) has no control to
        # point at, and the guide never opens a page itself: its path is
        # given in words (the record's route and requirements).
        or (page and not rail_page)
        or not label
        or not placements
    ):
        return None
    from kiro_crew.guide_catalog import (
        REMOVAL_LABEL_RE,
        ui_find_location_ids,
        ui_find_opener,
        ui_find_params_ok,
    )

    params: dict[str, Any] = {"label": label}
    where = _where(placements[0])
    if "route" in where and not rail_page:
        params["route"] = where["route"]
    role = _FIND_ROLE_BY_KIND.get(str(loc.get("kind")))
    if role:
        params["role"] = role
    ids = ui_find_location_ids()
    if not page and loc.get("id") in ids:
        # The registered control by its id: its label can read differently in
        # another state of the page (a button an app relabels), and a row a
        # shared primitive draws carries the id at run time.
        params["location_id"] = loc["id"]
        opener = ui_find_opener(loc["id"])
        if opener is not None:
            # Drawn inside a menu: the control that opens it, which the guide
            # points at first while the menu is closed. Static, from the
            # index; the tab never opens it.
            params["opener"] = opener
    english = idx.labels.get(DEFAULT_LOCALE, {}).get(str(loc.get("label_key")), "")
    if loc.get("guide_policy") == _CAUTION or REMOVAL_LABEL_RE.search(english):
        params["caution"] = True
    return {"action_id": "ui.find", "params": params} if ui_find_params_ok(params) else None


def _loc_label_keys(loc: dict[str, Any]) -> list[str]:
    """Every catalog key a location's on-screen name can come from."""
    keys = [str(loc.get("label_key") or "")]
    keys += [str(k) for k in loc.get("alias_keys") or []]
    keys += [str(st.get("label_key") or "") for st in loc.get("state_labels") or []]
    return [k for k in keys if k]


def _is_label_of(idx: _Index, loc: dict[str, Any], label: str) -> bool:
    want = normalize(label)
    return any(
        normalize(_label(idx, key, code)) == want
        for key in _loc_label_keys(loc)
        for code in idx.labels
    )


def _page_opener_plan(idx: _Index, page: dict[str, Any]) -> str | None:
    """The one planned registered control that opens *page*: a button or link named like it."""
    from kiro_crew.guide_catalog import ui_show_plans

    plans = ui_show_plans()
    name = _label(idx, str(page.get("label_key") or ""), DEFAULT_LOCALE)
    if not name:
        return None
    hits = [
        lid
        for lid, loc in idx.by_id.items()
        if lid in plans
        and loc.get("kind") in ("button", "link", "menu-item")
        and _is_label_of(idx, loc, name)
    ]
    return (
        hits[0]
        if len(hits) == 1
        and _guide_ref_ok({"action_id": "ui.show", "params": {"location_id": hits[0]}})
        else None
    )
