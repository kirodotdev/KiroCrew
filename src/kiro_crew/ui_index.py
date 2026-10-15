"""``find_ui``: search the packaged index of dashboard locations. Read-only.

The index (``docs/ui-index.generated.json``) is generated from the dashboard's
own source by ``website/scripts/gen-ui-index.mjs``: pages and tabs from the
registries the rail and Search Everywhere render, every Settings control from
the settings extraction, and registered controls proven at their render site.
This module only reads that packaged file and the optional build-time auto
tier (below). It never loads config, contacts
the gateway, inspects a browser or reads user state, so a result describes the
build's known locations and says ``availability: "not_observed"``.

Matching is deliberately literal: Unicode NFKC + casefold, whole phrases for
CJK scripts (no single-character scoring), token coverage for space-separated
scripts, over each location's labels and its curated search terms (the words a
newcomer uses, e.g. "old chats" for Older Sessions). A candidate must also
explain enough of the question once question words are dropped
(:data:`_MIN_COVERAGE`), so one shared word ("Chat" in "where are my old
chats") is never a confident answer. Words a location's ancestors explain
("Voice" in "voice model") leave the question before coverage is measured; they
never create a match on their own. When a location's own words and its path's
together explain the whole question ("import an artifact": Import from a file,
under Artifacts) it may answer, but only if nothing matches by its own words.
"everything"/"everywhere"/"anything"/"anywhere" are one word. A query that only
resembles a label is
``no_match``, which means "not in this build's index", never "the feature does
not exist".

A location whose on-screen label is runtime data (the model chip shows the
model's name) has ``label_kind: "description"``: its result carries
``description`` instead of ``label``, a static sentence saying what the control
is, never text to quote as what the screen shows. One element whose label flips
with a state (Switch to board view / Switch to list view) also carries
``label_by_state``: each label with the state it is on screen in, so the label
quoted is the one the person sees; ``label`` is the one the question named.

A question that is only an action verb ("add", "delete", "删除") names no
object, so no location answers it: the result is ``no_match`` with
``needs_object``, which tells the caller to ask what to act on rather than
guessing one of the many places that verb applies. Naming the kind of thing
("stop button", "停止按钮") supplies that object only when exactly one
registered control's own label starts with the verb (Stop generation): then
that control is the answer. A verb several controls start with ("delete
button") still asks.

An auto entry (``tier: "auto"``) is an unregistered control whose one static
label and one page the generator proved from source (its file is drawn by that
page's route and no other), or that only the app shell draws (then it is on
every page, like a registered shell control); where on the page it sits and what must be true
for it to show are unknown, so it carries ``conditions_unknown: true`` and no
search terms. It answers only when the whole label is in the question and
explains all of it (question words and its page's words aside), never on one
shared word or on a question that is only part of the label, and only when no
generated or curated location matches at all. It is never the sole-control
answer to "<verb> button", and never answers with a label that is one word in
the locale it matched in ("Back", "Name"; for CJK, a label of at most two
characters such as 返回): one word on one page is not specific enough to name
that control rather than any other.

The auto tier is not in the committed index. Any new static-label button moves
it, so it is generated at BUILD time (``npm run build``) into the dashboard
bundle as ``static/dist/ui-index.auto.json`` (:data:`AUTO_INDEX_PATH`) and read
beside the committed index when present. It names the committed index it was
built against (``base_input_digest``); a missing, unreadable, malformed or
mismatched auto file leaves the committed tiers fully usable and says
``auto_tier: "unavailable"`` in every answer (a source checkout with no
frontend build, or a build from another revision).

A one-content-word search term ("get an app" once question words go) answers
only when said whole, never by sharing that word ("app settings").

:func:`browse_ui` is the other mode of the same tool: it lists one AREA (see
:data:`AREAS`) whole, paged under the same byte cap, so a question the search
misses can still be answered by choosing the entry that means it.

Labels come in the requested locale; the prerequisite prose (``description``,
``only_if``, ``otherwise``) is English only. A non-English answer carries
``prose_locale: "en"`` and says so, and the caller translates that prose for
the person (it is one table, ``website/src/uiLocations/conditions.ts``, kept in
one language on purpose: it states facts for the agent, not on-screen copy).
"""

from __future__ import annotations

import importlib as _importlib
import json
import logging
import re
import sys as _sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kiro_crew import _facade_mirror
from kiro_crew.ui_index_load import (
    IndexUnavailable,
    load_index,
)
from kiro_crew.ui_index_match import (
    _AUTO,
    _AUTO_AVAILABLE,
    _AUTO_BASES,
    _CURATED,
    _DASHBOARD_PROSE_NOTE,
    _DASHBOARD_SOURCE,
    _DESCRIPTION,
    _FULL,
    _LANG_RE,
    _MIN_COVERAGE,
    _MIN_SCORE,
    _NEEDS_OBJECT,
    _PARENT_BONUS,
    _PATH_BASIS,
    _PICKER_KIND,
    _PROSE_NOTE,
    _SCORE_ID,
    _SURFACE_RE,
    _TOO_BIG,
    _VERB_CONTROL_BASIS,
    DEFAULT_LOCALE,
    MAX_RESPONSE_BYTES,
    MAX_RESULTS,
    QUERY_MAX_CHARS,
    SHELL_SURFACE,
    _bare_verb,
    _contains_run,
    _Index,
    _noun_verb,
    _one_word,
    _score,
    _sole_control,
    _Text,
    normalize,
)
from kiro_crew.ui_index_records import (
    _guide_ref_ok,
    _is_label_of,
    _label,
    _loc_label,
    _on_surface,
    _record,
    _resolve_locale,
    _resolve_locales,
    _state_key,
)

logger = logging.getLogger(__name__)


def authoritative_find(
    params: dict[str, Any], *, label_lang: str | None = None, path: Path | None = None
) -> dict[str, Any]:
    """A ``ui.find`` action's params with the label the index gives it.

    The label is what the guide panel names and what the reply should quote,
    so it is the index's, never one the agent made up. With a ``location_id``
    the location's own label (any of its keys, in any shipped language) is
    kept, and anything else becomes its label in *label_lang* (the dashboard's
    language). Without one, a label no indexed control carries is looked up
    with :func:`find_ui`; when the best result has a ``find_ref`` on the
    route the agent named (or the agent named none), its params replace the
    agent's (the agent's ``container`` is kept, and its route when the match
    is drawn on every page). Anything the
    index cannot place is returned unchanged, for the catalog to judge.

    The ``opener`` is never the agent's: it is replaced by the target's
    indexed opener (:func:`guide_catalog.ui_find_opener`), or dropped when the
    target has none or no ``location_id`` names it.
    """
    params = _indexed_opener(params)
    label = params.get("label")
    if not isinstance(label, str) or not label.strip():
        return params
    try:
        idx = load_index(path)
    except IndexUnavailable:
        return params
    lid = params.get("location_id")
    if isinstance(lid, str):
        loc = idx.by_id.get(lid)
        if loc is None or _is_label_of(idx, loc, label):
            return params
        locale, _source = _resolve_locale(label_lang, idx.locales)
        return {**params, "label": _label(idx, str(loc["label_key"]), locale)}
    owners = [loc for loc in idx.by_id.values() if _is_label_of(idx, loc, label)]
    if owners:
        return _bind_registered(idx, params, owners, label_lang)
    found = find_ui(label, label_lang, label_lang=label_lang, path=path)
    results = found.get("results") if isinstance(found, dict) else None
    best = results[0] if isinstance(results, list) and results else None
    ref = best.get("find_ref") if isinstance(best, dict) else None
    if not isinstance(ref, dict) or ref.get("action_id") != "ui.find":
        return params
    out = dict(ref.get("params") or {})
    # Only a match on the page the agent named: a match elsewhere is another
    # control, and the agent's params go to the catalog unchanged rather than
    # silently sending the guide (and the reply) to a different page.
    asked, matched = params.get("route"), out.get("route")
    if isinstance(asked, str) and asked:
        from kiro_crew.guide_catalog import _route_compatible

        if isinstance(matched, str) and matched:
            if not _route_compatible(asked, matched):
                return params
        else:
            out["route"] = asked
    if isinstance(params.get("container"), str):
        out["container"] = params["container"]
    return _indexed_opener(out)


def _bind_registered(
    idx: _Index, params: dict[str, Any], owners: list[dict[str, Any]], label_lang: str | None
) -> dict[str, Any]:
    """*params* bound to the one registered control a label names, when there is one.

    A label can be another name of a control (its tooltip, the other state of
    a flipping label, an older wording): searched for as written it can miss
    the control whose accessible name reads differently. When exactly one
    registered control on the named page (or anywhere, with no page named)
    carries the label, the find names that control by id and by the label the
    page shows in the dashboard's language. Anything less certain is left
    as the agent wrote it.
    """
    from kiro_crew.guide_catalog import _route_compatible, ui_find_location_ids

    ids = ui_find_location_ids()
    asked = params.get("route")

    def on_page(loc: dict[str, Any]) -> bool:
        if not isinstance(asked, str) or not asked:
            return True
        return any(
            p.get("surface_id") == SHELL_SURFACE
            or _route_compatible(asked, str(p.get("route") or ""))
            for p in loc.get("placements") or []
        )

    hits = [loc for loc in owners if loc.get("id") in ids and on_page(loc)]
    if len(hits) != 1 or len([loc for loc in owners if on_page(loc)]) != 1:
        return params
    loc = hits[0]
    locale, _source = _resolve_locale(label_lang, idx.locales)
    want = normalize(str(params.get("label")))
    # The state the agent named keeps its own label (Collapse sidebar, not
    # Expand); any other name of the control becomes the label it shows.
    state_key = next(
        (
            str(st.get("label_key"))
            for st in loc.get("state_labels") or []
            if any(normalize(_label(idx, str(st.get("label_key")), c)) == want for c in idx.labels)
        ),
        None,
    )
    shown = _label(idx, state_key or str(loc["label_key"]), locale)
    if not shown:
        return params
    out = {**params, "location_id": loc["id"]}
    if not _is_label_of(idx, loc, str(params.get("label"))) or normalize(shown) != normalize(
        str(params.get("label"))
    ):
        out["label"] = shown
    return _indexed_opener(out)


def planned_find(params: dict[str, Any], *, path: Path | None = None) -> dict[str, Any] | None:
    """The ``ui.show`` action that walks to a ``ui.find`` target, when the index plans one.

    A ``ui.find`` searches one page for a control. A registered control that
    sits on another page, behind a selection or inside an editor (Delete
    crewmate: open the crewmate's editor, then its Danger zone) is not on
    that page yet, so the search can only say it is not showing. When the
    index carries a ``ui.show`` plan for the control the find names (its
    ``location_id``, or the one registered control whose label it is), that
    plan is the guide: it opens the page, then points at each step on the
    way. Only a plan the gateway's catalog accepts, and only when the route
    the find names (if any) is a page that plan walks; otherwise ``None`` and
    the find is used as it is.
    """
    from kiro_crew.guide_catalog import ui_show_plans

    plans = ui_show_plans()
    lid = params.get("location_id")
    if not isinstance(lid, str):
        label = params.get("label")
        if not isinstance(label, str) or not label.strip():
            return None
        try:
            idx = load_index(path)
        except IndexUnavailable:
            return None
        hits = [
            loc_id
            for loc_id, loc in idx.by_id.items()
            if loc_id in plans and _is_label_of(idx, loc, label)
        ]
        asked_route = params.get("route")
        if len(hits) > 1 and isinstance(asked_route, str) and asked_route:
            # The page the agent named tells same-labelled controls apart.
            # A shell plan (no route) is on every page.
            hits = [h for h in hits if _plan_on_route(plans[h], asked_route)]
        if len(hits) > 1:
            # One label on several planned controls (New crewmate in the Add…
            # menu and in an open crewmate's switcher): the one whose walk
            # needs no prior pick is the general door; anything else is a tie.
            hits = [h for h in hits if not _plan_selects(plans[h])]
        if len(hits) != 1:
            return None
        lid = hits[0]
    plan = plans.get(lid)
    if plan is None:
        return None
    asked = params.get("route")
    if isinstance(asked, str) and asked and not _plan_on_route(plan, asked):
        return None
    action = {"id": "ui.show", "params": {"location_id": lid}}
    if not _guide_ref_ok({"action_id": "ui.show", "params": {"location_id": lid}}):
        return None
    return action


def _plan_routes(plan: dict[str, Any]) -> set[str]:
    """The page routes a ``ui.show`` plan's placements start on (none for shell chrome)."""
    return {
        str(pl.get("route"))
        for pl in plan.get("placements", [])
        if isinstance(pl, dict) and isinstance(pl.get("route"), str)
    }


def _plan_on_route(plan: dict[str, Any], asked: str) -> bool:
    """Whether a ``ui.show`` plan walks the page *asked* names (shell chrome is on every page)."""
    from kiro_crew.guide_catalog import _route_compatible

    routes = _plan_routes(plan)
    return not routes or any(_route_compatible(asked, r) for r in routes)


def _plan_selects(plan: dict[str, Any]) -> bool:
    """Whether any placement of a ``ui.show`` plan starts from a pick (a ``select`` step)."""
    return any(
        isinstance(st, dict) and st.get("kind") == "select"
        for pl in plan.get("placements", [])
        if isinstance(pl, dict)
        for st in pl.get("steps", [])
    )


def _indexed_opener(params: dict[str, Any]) -> dict[str, Any]:
    """*params* with ``opener`` set from the index for its ``location_id``."""
    from kiro_crew.guide_catalog import ui_find_opener

    lid = params.get("location_id")
    opener = ui_find_opener(lid) if isinstance(lid, str) else None
    out = {k: v for k, v in params.items() if k != "opener"}
    if opener is not None:
        out["opener"] = opener
    return out


def _envelope(idx: _Index | None, status: str, **extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"status": status}
    if idx is not None:
        out["schema_version"] = idx.raw.get("schema_version")
        out["input_digest"] = idx.raw.get("input_digest")
        cov = idx.raw.get("coverage")
        if isinstance(cov, dict) and isinstance(cov.get("scope"), str):
            out["coverage"] = cov["scope"]
            if idx.auto_status == _AUTO_AVAILABLE and idx.auto_scope:
                out["coverage"] += f"; plus {idx.auto_scope}"
        # Whether unregistered (auto-indexed) controls could answer at all: a
        # no_match with the tier unavailable says less than one with it.
        out["auto_tier"] = idx.auto_status
        if idx.auto_status != _AUTO_AVAILABLE and idx.auto_reason:
            out["auto_tier_reason"] = idx.auto_reason
    out.update(extra)
    return out


def _size(obj: dict[str, Any]) -> int:
    return len(json.dumps(obj, ensure_ascii=False, sort_keys=True).encode("utf-8"))


#: The packaged built-in apps, whose display names a question may carry
#: ("uninstall Command Bar"): the name is the item to act on, not words to
#: search the dashboard for (Command Bar is also the top bar's search box).
_BUILTIN_APPS_DIR = Path(__file__).resolve().parent / "apps" / "builtins"
#: What a person does to one app; only beside one of these is an app's name
#: taken out of the search, so "open Command Bar" still searches for it.
_APP_ACTION_RE = re.compile(
    r"\b(?:uninstall|remove|delete|disable|enable|install|update|reinstall)\b"
    r"|卸载|删除|移除|停用|禁用|启用|安装|更新|删掉",
    re.IGNORECASE,
)
_app_names_cache: tuple[str, ...] | None = None


def _builtin_app_names() -> tuple[str, ...]:
    """Display names of the packaged built-in apps, longest first."""
    global _app_names_cache
    if _app_names_cache is None:
        names: set[str] = set()
        for manifest in sorted(_BUILTIN_APPS_DIR.glob("*/app.json")):
            try:
                name = json.loads(manifest.read_text(encoding="utf-8")).get("displayName")
            except (OSError, ValueError, AttributeError):
                continue
            if isinstance(name, str) and len(name.strip()) >= 3:
                names.add(name.strip())
        _app_names_cache = tuple(sorted(names, key=lambda n: (-len(n), n)))
    return _app_names_cache


def _named_app(query: str) -> tuple[str, str] | None:
    """``(query without the app's name, the name)`` for a question that does
    something to one named built-in app; None for any other question."""
    if not _APP_ACTION_RE.search(query):
        return None
    for name in _builtin_app_names():
        m = re.search(
            r"(?<![A-Za-z0-9])[\"'“”‘’「」]?" + re.escape(name) + r"[\"'“”‘’「」]?(?![A-Za-z0-9])",
            query,
            re.IGNORECASE,
        )
        if m is None:
            continue
        rest = " ".join((query[: m.start()] + " " + query[m.end() :]).split())
        rest = re.sub(r"\b(?:the|app|application)\b|应用", " ", rest, flags=re.IGNORECASE)
        # A Chinese question points at the app it names ("Command Bar 这个应用"):
        # the demonstrative and the sentence punctuation are not search words.
        rest = re.sub(r"这个|那个|这款|那款|[？?！!。，,]", " ", rest)
        rest = " ".join(rest.split())
        if rest and _APP_ACTION_RE.search(rest):
            return rest + (" an app" if re.search(r"[A-Za-z]", rest) else "应用"), name
    return None


def find_ui(
    query: str,
    lang: str | None = None,
    surface: str | None = None,
    *,
    label_lang: str | None = None,
    path: Path | None = None,
    auto_path: Path | None = None,
) -> dict[str, Any]:
    """Search the packaged location index. See the module docstring for scope.

    *lang* is the language of the question; *label_lang*, when it names a
    shipped locale, is the language the user's dashboard tab shows, and every
    returned label is in it (``locale_source: "dashboard"``) so a quoted label
    matches the screen whatever language the conversation is in.
    """
    q_raw = (query or "").strip()
    if not q_raw:
        return {"error": "give a nonblank 'query'"}
    if len(q_raw) > QUERY_MAX_CHARS:
        return {"error": f"'query' is longer than {QUERY_MAX_CHARS} characters"}
    named = _named_app(q_raw)
    if named is not None:
        # The app is the item to act on: search for the action, and hand the
        # name back so the guide's choose-the-app step outlines that one app.
        d = find_ui(named[0], lang, surface, label_lang=label_lang, path=path, auto_path=auto_path)
        if d.get("status") == "ok":
            return {**d, "named_item": named[1]}
    if lang is not None and lang != "" and not _LANG_RE.match(lang):
        return {"error": "'lang' must be a language tag such as 'en' or 'zh-CN'"}
    if surface is not None and surface != "" and not _SURFACE_RE.match(surface):
        return {"error": "'surface' must be a surface id such as 'chat' or 'settings'"}
    try:
        idx = load_index(path, auto_path)
    except IndexUnavailable as exc:
        logger.warning("find_ui index unavailable: %s", exc)
        return _envelope(None, "unavailable", reason=str(exc), results=[])
    if surface and surface not in idx.surfaces:
        return {"error": f"unknown surface '{surface}'; known: {', '.join(sorted(idx.surfaces))}"}

    # ``qlocale`` reads the question; ``locale`` is every label's language.
    qlocale, locale, locale_source = _resolve_locales(
        lang or None, label_lang or None, tuple(idx.labels)
    )
    meta = {
        "requested_locale": lang or None,
        "resolved_locale": locale,
        "locale_source": locale_source,
    }
    queries = {code: _Text.of(q_raw, code) for code in idx.labels}
    if locale_source == _DASHBOARD_SOURCE:
        if locale != DEFAULT_LOCALE or qlocale != DEFAULT_LOCALE:
            meta["prose_locale"] = DEFAULT_LOCALE
            meta["prose_note"] = _DASHBOARD_PROSE_NOTE
    elif locale != DEFAULT_LOCALE:
        # Labels are localized; the prerequisite prose is not (see module doc).
        meta["prose_locale"] = DEFAULT_LOCALE
        meta["prose_note"] = _PROSE_NOTE
    verb = _bare_verb(queries[qlocale], qlocale) or _bare_verb(queries[DEFAULT_LOCALE], qlocale)
    for code in dict.fromkeys((qlocale, DEFAULT_LOCALE)):
        noun_verb = _noun_verb(queries[code])
        if noun_verb is None:
            continue
        sole = _sole_control(idx, noun_verb, code)
        if sole is not None:
            loc = idx.by_id[sole[0]]
            placements = [p for p in loc["placements"] if not surface or _on_surface(p, surface)]
            if placements:
                resp = _envelope(idx, "ok", **meta, ambiguous=False, truncated=False, results=[])
                resp["results"].append(
                    _record(idx, loc, placements, locale, _VERB_CONTROL_BASIS, sole[1])
                )
                return resp if _size(resp) <= MAX_RESPONSE_BYTES else {**_TOO_BIG, "results": []}
        verb = verb or noun_verb
        break
    if verb is not None:
        return _envelope(
            idx,
            "no_match",
            **meta,
            needs_object=_NEEDS_OBJECT.format(verb=verb),
            truncated=False,
            results=[],
        )
    # The question's language, then the screen's (the user may quote a label
    # they see), then English.
    searched = list(dict.fromkeys((qlocale, locale, DEFAULT_LOCALE)))
    # With no locale named, other shipped languages still count for whole-phrase
    # matches, so a localized question finds its control; labels stay English.
    phrase_locales = [code for code in idx.labels if code not in searched] if not lang else []

    scored: list[tuple[int, str, str]] = []
    matched: dict[str, str | None] = {}
    for loc_id, loc in idx.by_id.items():
        if loc.get("kind") == _PICKER_KIND:
            # A picker is where a guide's select step points; its own page
            # answers a where-is question about it, so it never outranks that.
            continue
        best, basis, best_key = 0, "", None
        auto = loc.get("tier") == _AUTO
        if q_raw.casefold() in (loc_id.casefold(), str(loc.get("setting_id") or "").casefold()):
            best, basis = _SCORE_ID, "id"
        else:
            forms = idx.texts.get(loc_id, {})
            ancestor_ids = list(
                dict.fromkeys(pid for p in loc["placements"] for pid in p["parent_ids"])
            )
            for code in searched + phrase_locales:
                code_forms = forms.get(code, [])
                if auto:
                    # A one-word label never answers (see _one_word). Then a
                    # cheap necessary condition for a full-coverage match, so
                    # the many auto entries cost a set test, not a score.
                    q = queries[code]
                    code_forms = [
                        t
                        for t in code_forms
                        if not _one_word(t)
                        and (
                            t.compact in q.compact
                            if (t.cjk or q.cjk)
                            else (t.content and t.content <= q.content)
                        )
                    ]
                if not code_forms:
                    continue
                # The ancestors' labels, in this locale or else English: words
                # the path explains ("Voice" in "voice model") count toward
                # coverage, never toward the score.
                context = idx.contexts.get((loc_id, code))
                if context is None:
                    context = idx.contexts[(loc_id, code)] = tuple(
                        t
                        for pid in ancestor_ids
                        for t in (
                            idx.texts.get(pid, {}).get(code)
                            or idx.texts.get(pid, {}).get(DEFAULT_LOCALE, [])
                        )
                        if not t.term
                    )
                for t in code_forms:
                    s, b, coverage = _score(
                        queries[code], t, phrase_only=code in phrase_locales, context=context
                    )
                    # A form that explains too little of the question (or the
                    # question too little of it) is no answer, however high it
                    # would rank: one shared word must not come back as "ok".
                    if coverage < _MIN_COVERAGE:
                        continue
                    # A search term that is ONE content word once question words
                    # are dropped ("get an app" is {app}) answers only when it is
                    # said whole: otherwise every question about that one word
                    # ("app settings": {app}) would be confidently this location.
                    if (
                        t.term
                        and not t.cjk
                        and len(t.content) == 1
                        and b not in ("exact_label", "label_phrase")
                    ):
                        continue
                    # An auto entry answers only when the question IS its label
                    # (question words and its page's words aside): a partial or
                    # one-word overlap could name any of many similar controls.
                    if auto and (
                        b not in _AUTO_BASES
                        or coverage < _FULL
                        or not (t.core if t.cjk else t.content)
                    ):
                        continue
                    if s > best:
                        best, basis, best_key = s, b, t.key
                        if b != _PATH_BASIS and t.term:
                            basis = "search_term"
                        elif b != _PATH_BASIS and loc.get("label_kind") == _DESCRIPTION:
                            basis = _DESCRIPTION
        if best < _MIN_SCORE:
            continue
        placements = loc["placements"]
        if surface:
            placements = [p for p in placements if _on_surface(p, surface)]
            if not placements:
                continue
        for pid in placements[0].get("parent_ids") or []:
            for code in searched:
                q = queries[code]
                if any(
                    (len(t.compact) >= 2 and _contains_run(q.tokens, t.tokens))
                    or (t.cjk and t.compact in q.compact)
                    for t in idx.texts.get(pid, {}).get(code, [])
                    if not t.term
                ):
                    best += _PARENT_BONUS
                    break
        scored.append((best, loc_id, basis))
        matched[loc_id] = best_key

    if not scored:
        return _envelope(idx, "no_match", **meta, truncated=False, results=[])
    # The auto tier is a fallback, like a path-completed match: any generated or
    # curated location that answers outranks every auto entry, so an auto entry
    # is returned only when nothing proven whole matches.
    if any(idx.by_id[i].get("tier") != _AUTO for _s, i, _b in scored):
        scored = [r for r in scored if idx.by_id[r[1]].get("tier") != _AUTO]
    # A path-completed match is a fallback: it answers only when no location
    # matches the question by its own words ("chat model" is Chat > Models,
    # not every model setting under Chat).
    if any(basis != _PATH_BASIS for _s, _i, basis in scored):
        scored = [r for r in scored if r[2] != _PATH_BASIS]
    scored.sort(key=lambda r: (-r[0], r[1]))
    top = scored[0][0]
    tied = sum(1 for s, _i, _b in scored if s == top)
    resp = _envelope(
        idx,
        "ok",
        **meta,
        ambiguous=tied > 1,
        truncated=False,
        results=[],
    )
    if tied > 1:
        # How many leading results share the top score, so a reader can tell
        # the tie apart from the lower-ranked rows after it.
        resp["tied"] = tied
    for n, (_score_v, loc_id, basis) in enumerate(scored):
        if n >= MAX_RESULTS:
            resp["truncated"] = True
            break
        loc = idx.by_id[loc_id]
        placements = [p for p in loc["placements"] if not surface or _on_surface(p, surface)]
        key = matched.get(loc_id)
        if loc.get("state_labels"):
            key = _state_key(idx, loc, [queries[c] for c in searched], key)
        rec = _record(idx, loc, placements, locale, basis, key)
        resp["results"].append(rec)
        if _size(resp) > MAX_RESPONSE_BYTES:
            resp["results"].pop()
            resp["truncated"] = True
            break
    if not resp["results"]:
        # Fixed and metadata-free, so it fits any cap of at least its own size.
        return {**_TOO_BIG, "results": []}
    return resp


# ---------------------------------------------------------------------- browse

#: ``find_ui``'s browse mode lists every location of one AREA, so a question
#: the search misses ("how do I tidy my chats into groups?") can still be
#: answered by reading the one area it must be in and picking the entry that
#: means it. Areas are a small, stable vocabulary over the index's own
#: structure: a location is in an area when it IS one of the area's anchor
#: locations, when one of its placements runs through an anchor (its path), or
#: when it matches the area's id prefixes or surface. They may overlap (a
#: Settings > Notifications control is in ``notifications`` and in
#: ``settings.notifications``). ``settings`` is split: its listing names one
#: ``settings.<tab>`` sub-area per Settings tab instead of 280 controls.


@dataclass(frozen=True)
class _Area:
    #: One English line saying what the area holds (prose, like ``only_if``).
    about: str
    #: The location whose localized label names the area, when there is one.
    title: str | None = None
    anchors: tuple[str, ...] = ()
    surfaces: tuple[str, ...] = ()
    ids: tuple[str, ...] = ()
    id_prefixes: tuple[str, ...] = ()
    #: Areas whose members are left out of this one.
    exclude: tuple[str, ...] = ()
    #: Neighbouring areas a question about this one often belongs to (returned
    #: as ``related``, so a miss here points at the next listing to read).
    related: tuple[str, ...] = ()


_COMPOSER_IDS = ("chat.model-picker", "chat.memory-mode")
AREAS: dict[str, _Area] = {
    "sessions": _Area(
        "the Sessions page: chat list, new chat, folders, older sessions, row and list menus",
        title="page.chat",
        anchors=("page.chat",),
        exclude=("composer",),
        related=("composer",),
    ),
    "composer": _Area(
        "the message box under a chat: attach files, model, memory mode, context usage, send and stop",
        ids=_COMPOSER_IDS,
        id_prefixes=("composer.",),
        related=("sessions",),
    ),
    "crewmates": _Area(
        "mates: the Mates page and the Mates and Custom agents tabs of Customize",
        title="page.members",
        anchors=("page.members", "tab.capabilities.crews", "tab.capabilities.templates"),
    ),
    "schedule": _Area(
        "the Schedule page: scheduled jobs, reminders, runs",
        title="page.schedule",
        anchors=("page.schedule",),
    ),
    "artifacts": _Area(
        "the Artifacts page: saved documents, folders, import, deploy",
        title="page.artifacts",
        anchors=("page.artifacts",),
    ),
    "apps": _Area(
        "apps: Discover (the app store) and Library (installed apps and their details)",
        title="page.apps",
        anchors=("page.apps", "page.apps-library"),
    ),
    "connections": _Area(
        "connections: MCP servers, messaging channels (Slack, Telegram, ...), OAuth apps",
        title="tab.capabilities.mcp",
        anchors=("tab.capabilities.mcp", "settings.tab.channels", "settings.tab.connections"),
        related=("settings",),
    ),
    "customize": _Area(
        "the Customize page: mates, connections, skills, hooks, knowledge, prompts, steering, workflows",
        title="page.capabilities",
        anchors=("page.capabilities",),
        related=("settings",),
    ),
    "notifications": _Area(
        "notifications: the bell in the top bar, the Notifications page, notification settings",
        title="page.notifications",
        anchors=("page.notifications", "shell.notifications", "settings.tab.notifications"),
    ),
    "shell": _Area(
        "controls on every page: navigation rail, top bar, search, terminal, phone menu "
        "(the built-in browser is a view of a chat's side panel, listed under sessions)",
        surfaces=(SHELL_SURFACE,),
        related=("sessions",),
    ),
    "settings": _Area(
        "the Settings page; its listing names one settings.<tab> sub-area per tab",
        title="page.settings",
        anchors=("page.settings",),
        related=("customize",),
    ),
    "developer": _Area(
        "the Developer, Logs, Webhooks and Task Runner pages",
        title="page.developer",
        anchors=("page.developer", "page.logs", "page.webhooks", "page.projects"),
    ),
}
#: The area split into sub-areas, and the tab-id prefix each sub-area is named from.
_SPLIT_AREA = "settings"
_SPLIT_TAB_PREFIX = "settings.tab."
_AREA_RE = re.compile(r"\A[a-z][a-z0-9-]{0,40}(?:\.[a-z0-9-]{1,40})?\Z")
_BROWSE_NOTE = (
    "every entry the index has for this area, in path order. tier auto is approximate: "
    "only its page is known, not where on it or what must be true for it to show; relay "
    "it hedged. needs lists short prerequisites (English ids) for the first way in; "
    "ways counts every way in, so when it is above 1 call find_ui with the entry's id "
    "as query for each way and its full prerequisites before answering"
)


def _area_table(idx: _Index) -> dict[str, _Area]:
    """The fixed areas plus one ``settings.<tab>`` sub-area per Settings tab in the index."""
    table = dict(AREAS)
    for loc_id in sorted(idx.by_id):
        if loc_id.startswith(_SPLIT_TAB_PREFIX) and idx.by_id[loc_id].get("tier") != _AUTO:
            sub = f"{_SPLIT_AREA}.{loc_id[len(_SPLIT_TAB_PREFIX):]}"
            table[sub] = _Area(f"one Settings tab: {loc_id}", title=loc_id, anchors=(loc_id,))
    return table


def area_ids(idx: _Index | None = None) -> list[str]:
    """Every browsable area id (the fixed vocabulary plus the Settings sub-areas)."""
    return list(_area_table(idx) if idx is not None else AREAS)


def _in_area(area: _Area, loc_id: str, loc: dict[str, Any]) -> bool:
    if loc_id in area.anchors or loc_id in area.ids:
        return True
    if any(loc_id.startswith(p) for p in area.id_prefixes):
        return True
    anchors = set(area.anchors)
    for p in loc["placements"]:
        if p.get("surface_id") in area.surfaces or anchors.intersection(p.get("parent_ids", [])):
            return True
    return False


def area_members(idx: _Index, area_id: str) -> list[str]:
    """Location ids in ``area_id`` (sub-areas included), in path order."""
    table = _area_table(idx)
    area = table[area_id]
    excluded = [table[x] for x in area.exclude]
    out = [
        loc_id
        for loc_id, loc in idx.by_id.items()
        if _in_area(area, loc_id, loc) and not any(_in_area(x, loc_id, loc) for x in excluded)
    ]

    def order(loc_id: str) -> tuple[bool, tuple[str, ...]]:
        # Proven entries first, the approximate auto tier after them; each in
        # path order, so a menu's items follow the menu and a tab's its tab.
        loc = idx.by_id[loc_id]
        return (loc.get("tier") == _AUTO, (*loc["placements"][0]["parent_ids"], loc_id))

    return sorted(out, key=order)


def _auto_one_word(idx: _Index, loc: dict[str, Any], locale: str) -> bool:
    if loc.get("tier") != _AUTO:
        return False
    return _one_word(_Text.of(_label(idx, loc["label_key"], locale), locale))


def _need(idx: _Index, req: dict[str, Any], locale: str) -> str:
    """One prerequisite as a short phrase: the browse listing's ``needs`` item."""
    kind = req.get("kind")
    if kind == "viewport":
        return f"{req.get('value')} only"
    if kind in ("shown_by", "preview_flag"):
        label = _loc_label(idx, str(req.get("location")), locale)
        if kind == "preview_flag":
            return f"preview: {label}"
        when = req.get("when")
        return f"after {label}" + (f" if {when}" if isinstance(when, str) else "")
    return str(req.get("id"))


def _entry(idx: _Index, loc: dict[str, Any], locale: str) -> dict[str, Any]:
    """One compact browse row: id, label (or description), path, needs, tier."""
    p = loc["placements"][0]
    text = _label(idx, loc["label_key"], locale)
    shown = {_DESCRIPTION: text} if loc.get("label_kind") == _DESCRIPTION else {"label": text}
    path_labels = [_loc_label(idx, pid, locale) for pid in p["parent_ids"]]
    out: dict[str, Any] = {
        "id": loc["id"],
        **shown,
        "path": " > ".join(path_labels + ([] if loc.get("label_kind") == _DESCRIPTION else [text])),
        "tier": loc.get("tier", _CURATED),
    }
    if p.get("surface_id") == SHELL_SURFACE:
        out["on_every_page"] = True
    states = loc.get("state_labels") or []
    if states:
        # The label each state shows, so the one the person sees can be quoted.
        out["states"] = {s["when"]: _label(idx, s["label_key"], locale) for s in states}
    needs = [_need(idx, r, locale) for r in p.get("requires", [])]
    if needs:
        out["needs"] = needs
    if len(loc["placements"]) > 1:
        out["ways"] = len(loc["placements"])
    return out


def browse_ui(
    area: str,
    lang: str | None = None,
    offset: int = 0,
    *,
    label_lang: str | None = None,
    path: Path | None = None,
    auto_path: Path | None = None,
) -> dict[str, Any]:
    """List every location of one area, a page at a time. Read-only, like :func:`find_ui`.

    Each page holds whole entries up to :data:`MAX_RESPONSE_BYTES`; when more
    remain the answer says ``truncated: true`` and ``next_offset``, so a long
    area is paged, never silently cut. ``settings`` lists its sub-areas
    (``sub_areas``) and only the entries no sub-area holds. Labels follow
    *label_lang* (the dashboard's language) when it names a shipped locale, as
    in :func:`find_ui`.
    """
    name = (area or "").strip()
    if not _AREA_RE.match(name):
        return {"error": "'area' must be an area id such as 'sessions' or 'settings.chat'"}
    if lang is not None and lang != "" and not _LANG_RE.match(lang):
        return {"error": "'lang' must be a language tag such as 'en' or 'zh-CN'"}
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        return {"error": "'offset' must be a nonnegative integer"}
    try:
        idx = load_index(path, auto_path)
    except IndexUnavailable as exc:
        logger.warning("find_ui index unavailable: %s", exc)
        return _envelope(None, "unavailable", reason=str(exc), entries=[])
    table = _area_table(idx)
    if name not in table:
        return {"error": f"unknown area '{name}'; known: {', '.join(table)}"}
    _qlocale, locale, locale_source = _resolve_locales(
        lang or None, label_lang or None, tuple(idx.labels)
    )
    spec = table[name]
    members = area_members(idx, name)
    # An auto entry whose label is one word in this locale ("Cancel", "Retry")
    # is left out, as find_ui never answers with one: on its own it cannot say
    # which of a page's look-alike controls it is. The count is reported.
    one_word = [m for m in members if _auto_one_word(idx, idx.by_id[m], locale)]
    members = [m for m in members if m not in set(one_word)]
    sub_areas: list[dict[str, Any]] = []
    if name == _SPLIT_AREA:
        held: set[str] = set()
        for sub_id in table:
            if not sub_id.startswith(f"{_SPLIT_AREA}."):
                continue
            ids = area_members(idx, sub_id)
            held.update(ids)
            sub_areas.append(
                {
                    "area": sub_id,
                    "label": _loc_label(idx, table[sub_id].title or sub_id, locale),
                    # What that sub-area's own listing totals (one-word auto left out).
                    "count": sum(not _auto_one_word(idx, idx.by_id[i], locale) for i in ids),
                }
            )
        members = [m for m in members if m not in held]
    resp = _envelope(
        idx,
        "ok",
        area=name,
        about=spec.about,
        requested_locale=lang or None,
        resolved_locale=locale,
        locale_source=locale_source,
        note=_BROWSE_NOTE,
        total=len(members),
        omitted_one_word_auto=len(one_word),
        offset=offset,
        truncated=False,
        entries=[],
    )
    if spec.title and spec.title in idx.by_id:
        resp["title"] = _loc_label(idx, spec.title, locale)
    if spec.related:
        resp["related"] = list(spec.related)
    if locale != DEFAULT_LOCALE:
        resp["prose_locale"] = DEFAULT_LOCALE
    if sub_areas and offset == 0:
        resp["sub_areas"] = sub_areas
    if _size(resp) > MAX_RESPONSE_BYTES:
        return {**_TOO_BIG, "entries": []}
    for n in range(offset, len(members)):
        resp["entries"].append(_entry(idx, idx.by_id[members[n]], locale))
        if _size(resp) > MAX_RESPONSE_BYTES:
            resp["entries"].pop()
            break
    shown = offset + len(resp["entries"])
    if shown < len(members):
        if not resp["entries"]:
            # Not even one entry fits: a fixed answer, never an endless empty page.
            return {**_TOO_BIG, "entries": []}
        # Keep room for the paging fields themselves.
        resp["truncated"] = True
        resp["next_offset"] = shown
        while _size(resp) > MAX_RESPONSE_BYTES and resp["entries"]:
            resp["entries"].pop()
            resp["next_offset"] = offset + len(resp["entries"])
    return resp


def setting_labels(
    setting_ids: list[str], label_lang: str | None, *, path: Path | None = None
) -> dict[str, dict[str, Any]]:
    """``{setting_id: {label, path}}`` as the user's dashboard shows them.

    For ``find_setting``, whose rows come from the English settings registry:
    a Settings-page setting with a location in this index gets its on-screen
    label and click path (parent first) in *label_lang* when that names a
    shipped locale, else English. A setting the index does not hold is left
    out, and an unreadable index answers ``{}``; never raises.
    """
    try:
        idx = load_index(path)
    except IndexUnavailable:
        return {}
    locale, _src = _resolve_locale(label_lang or None, tuple(idx.labels))
    by_setting = {
        str(loc["setting_id"]): loc
        for loc in idx.by_id.values()
        if isinstance(loc.get("setting_id"), str) and loc.get("placements")
    }
    out: dict[str, dict[str, Any]] = {}
    for sid in setting_ids:
        loc = by_setting.get(sid)
        if loc is None:
            continue
        parents = loc["placements"][0].get("parent_ids") or []
        out[sid] = {
            "label": _label(idx, loc["label_key"], locale),
            "path": [_loc_label(idx, p, locale) for p in parents],
        }
    return out


# This module is the facade of the index's owners and keeps the public surface: find_ui,
# browse_ui and its areas, the guide bindings (authoritative_find, planned_find) and
# setting_labels. Every other name is reached through it as ``kiro_crew.ui_index.<name>``:
# a read is served by the owner that binds the name, and a write -- ``monkeypatch.setattr``,
# ``mock.patch`` -- reaches every module holding it (:mod:`kiro_crew._facade_mirror`).
# Owners never import this module. New work goes to the owner of its responsibility:
#
#   ``ui_index_match`` -- the shared constants, the question vocabulary and the matcher.
#   ``ui_index_load`` -- reading, validating, merging and caching the packaged index.
#   ``ui_index_records`` -- one location's answer record: labels, requirements, refs.
#
#: Owner modules, lowest layer first: the first holder of a name answers its reads.
_OWNERS = (
    "kiro_crew.ui_index_match",
    "kiro_crew.ui_index_load",
    "kiro_crew.ui_index_records",
)
for _owner in _OWNERS:
    _importlib.import_module(_owner)
#: Name the facade serves but does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
_EXPORTS.update(_facade_mirror.install(_sys.modules[__name__], _OWNERS, excluded=()).served)
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule
#: What a star import binds: every public name of the unsplit module's namespace.
__all__ = sorted(n for n in set(globals()) | set(_EXPORTS) if not n.startswith("_"))

if TYPE_CHECKING:  # every owner-defined name in _EXPORTS, for type checkers and IDEs
    from kiro_crew.ui_index_load import (  # noqa: F401
        AutoTierUnavailable,
        _attach_auto,
        _cache,
        _check_location,
        _forms,
        _parse,
        _read_auto,
        _stat_key,
        _str_list,
        _str_map,
    )
    from kiro_crew.ui_index_match import (  # noqa: F401
        _ALL_WORDS,
        _AUTO_ARTIFACT,
        _AUTO_CJK_ONE_WORD_MAX,
        _AUTO_UNAVAILABLE,
        _BARE_CJK_VERBS,
        _BARE_VERBS,
        _CAUTION,
        _CJK_STOP_PHRASES,
        _CJK_UI_NOUNS,
        _CONTROL_KINDS,
        _GUIDABLE_POLICIES,
        _GUIDE_POLICIES,
        _LITERAL_PREFIX,
        _LOCALE_STOPWORDS,
        _POINT,
        _REQUIREMENT_KINDS,
        _SCORE_ALL_TOKENS,
        _SCORE_CJK_BIGRAMS,
        _SCORE_EXACT,
        _SCORE_LABEL_IN_QUERY,
        _SCORE_MOST_TOKENS,
        _SCORE_QUERY_IN_LABEL,
        _SPACE_RE,
        _STOPWORDS,
        _TIERS,
        _UI_NOUNS,
        AUTO_INDEX_PATH,
        INDEX_PATH,
        MAX_INDEX_BYTES,
        SCHEMA_VERSION,
        _bigrams,
        _cjk_rest,
        _is_cjk,
        _jaccard,
        _ratio,
        _stem,
        _stop_cache,
        _stopwords,
        _strip_cjk,
    )
    from kiro_crew.ui_index_records import (  # noqa: F401
        _DESCRIBED_FINDS,
        _FIND_ROLE_BY_KIND,
        _FIND_SKIP_KINDS,
        _find_ref,
        _guidable,
        _loc_label_keys,
        _page_opener_plan,
        _requirement,
        _state_text,
        _viewport,
        _where,
    )
