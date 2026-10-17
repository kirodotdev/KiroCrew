"""The chat folder tree as the sidebar draws it, for the ``kirocrew-dashboard`` tools.

The folder tools list folders in the order the person sees them and place a folder in the
order the sidebar stores, so every key here mirrors ``website/src/utils/folderTree.ts``
operation for operation: the sort-mode read, a folder's stored ``order``, its name key, the
``name`` and ``created`` view orders, sibling and render order, and the free ``order`` slot
a placement takes. Each folder's human path, its same-named children, and the order a
group's sessions draw in (pinned first, by their stored rank) are derived here too.
:mod:`kiro_crew.mcp_dashboard` binds every name defined here, so each one reads, imports
and patches as ``kiro_crew.mcp_dashboard.<name>``.
"""

from __future__ import annotations

import re as _re
from collections.abc import Callable
from typing import Any

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.config.sections import FOLDER_SORT_DEFAULT, FOLDER_SORT_MODES


def _read_folder_sort_setting() -> object:
    """The raw ``dashboard.folder_sort`` value from the gateway's config on disk.

    Read the way the other MCP tools read a dashboard setting (the probe timeout
    in ``mcp_discovery``, the quarantine threshold in ``mcp_quarantine``):
    through the loader from ``config.json``, NOT through ``GET
    /api/config/kirocrew``. That route is cookie-authenticated only -- it is in
    neither internal-secret allowlist -- and admitting it to read one enum would
    open the whole config surface, PATCH included, to every secret-bearing
    caller. The sidebar menu's PATCH persists to the same file before it
    answers, so this read sees the menu's last choice; the loader has reduced
    the stored value to the known set, and the local overlay file is merged the
    same way for both readers.
    """
    return KiroCrewConfig.load().dashboard.folder_sort


def _chat_folder_sort_mode(
    read: Callable[[], object] = _read_folder_sort_setting,
) -> tuple[str, str | None]:
    """The person's sidebar folder sort mode, as ``read`` reports it.

    ``dashboard.folder_sort`` is the ONE stored copy of the preference: the
    sidebar menu writes it through the config PATCH allowlist, the sidebar reads
    it back through its config query, and the tree tool reads the same file, so
    the two cannot draw the tree in different orders. Returns ``(mode, None)``,
    or ``("custom", error)`` when the read itself failed -- ``custom`` being the
    stored-order listing every earlier build produced. A value outside the known
    set also reads as ``custom``, without an error: the loader has already
    reduced the stored value to a known one, so that can only be a build skew,
    not a broken read. The caller decides what to say about an error: the tree
    is still worth listing when only its ordering is in doubt.

    ``read`` is the config read; its default is the one the tree tool uses.
    """
    try:
        raw = read()
    except Exception as exc:  # noqa: BLE001 -- said in the header, not raised
        return FOLDER_SORT_DEFAULT, f"{type(exc).__name__}: {exc}"
    if isinstance(raw, str) and raw in FOLDER_SORT_MODES:
        return raw, None
    return FOLDER_SORT_DEFAULT, None


def _chat_folder_paths(folders: list[dict]) -> dict[str, str]:
    """Map each sidebar folder id to its ``parent/child`` human path.

    Chat folders persist only ``parent_id`` (unlike artifact folders, whose
    store computes ``path`` server-side), so the path is derived here. Walks
    parents with a visited-set guard so a pre-existing cycle in
    ``folders.json`` — which ``_is_descendant`` in ``chat_folders.py`` also
    defends against — cannot hang the tool.
    """
    by_id = {str(f.get("id") or ""): f for f in folders if f.get("id")}
    paths: dict[str, str] = {}
    for fid in by_id:
        segments: list[str] = []
        seen: set[str] = set()
        cur = fid
        while cur and cur in by_id and cur not in seen:
            seen.add(cur)
            segments.append(str(by_id[cur].get("name") or "?"))
            cur = str(by_id[cur].get("parent_id") or "")
        paths[fid] = "/".join(reversed(segments))
    return paths


def _chat_folder_children(folders: list[dict], parent_id: str, name: str) -> list[dict]:
    """Every DIRECT child of ``parent_id`` whose name equals ``name`` (case-insensitive).

    Chat folder names are not unique within a parent — the sidebar happily holds
    two folders called ``0811`` under the same parent — so a path segment can be
    genuinely ambiguous. Returning the whole match list is what keeps the two
    callers honest: taking the first match would patch an arbitrary sibling, or
    file a session into whichever duplicate happened to be created first.
    """
    target = str(name).strip().lower()
    return [
        f
        for f in folders
        if str(f.get("parent_id") or "") == parent_id
        and str(f.get("name", "")).strip().lower() == target
    ]


# The largest integer JavaScript represents exactly (``Number.MAX_SAFE_INTEGER``).
# The sidebar reads folder rows through ``JSON.parse``, so an ``order`` past this
# is not the number the store holds; both sides clamp to it so their comparisons
# agree on rows no sane writer produces but a hand-edited store can.
_CHAT_FOLDER_ORDER_LIMIT = 2**53 - 1

# A bare ``json.loads`` yields these for ``1e999`` / ``-1e999``; neither is a
# position, and neither survives the arithmetic a placement does with one.
_POS_INF = float("inf")
_NEG_INF = float("-inf")


def _chat_folder_order(folder: dict) -> int:
    """A folder's sidebar sort position. Anything that is not a finite number is 0.

    The endpoint stores whatever int a PATCH hands it and never renumbers, so a
    row can carry a duplicate order, a gap, or (from an older store) no ``order``
    key at all. The sidebar tolerates all three; so must every reader here.

    Only a real JSON number is accepted, and the frontend's counterpart accepts
    exactly the same set. That is deliberate: this value has to sort IDENTICALLY
    here and in the sidebar, and the two languages' conversions of everything else
    do not agree — ``int("0x10")`` and ``int("1e3")`` raise where ``Number`` reads
    16 and 1000, ``int([5])`` raises where ``Number`` reads 5. Rejecting the whole
    class costs nothing (the endpoint only ever writes an int) and removes the
    parity question instead of answering it per type.

    A float truncates toward zero, matching ``Math.trunc``, and ``bool`` is
    excluded even though it is an ``int`` subclass, because ``typeof true`` is not
    ``'number'`` on the other side. The result is clamped to the range JavaScript
    represents exactly: Python ints are unbounded, but the sidebar reads the same
    row through ``JSON.parse``, where ``2**53 + 1`` and ``2**53`` collapse to one
    value — so without the clamp those two rows compare as ordered here and as
    EQUAL there, and there the name tie-break decides the pair instead.

    This is a SORT KEY: an escaping exception would abort the whole sort and take
    down every folder tool rather than one row, so nothing here may raise.
    """
    value = folder.get("order")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    if value != value or value in (_POS_INF, _NEG_INF):  # NaN, ±Infinity
        return 0
    return max(-_CHAT_FOLDER_ORDER_LIMIT, min(_CHAT_FOLDER_ORDER_LIMIT, int(value)))


# A-Z to a-z and nothing else, written out rather than looked up. Every Unicode
# version ever published maps this range identically, so both sides can fold it
# without consulting a table — which is the whole reason the fold stops here.
_ASCII_FOLD = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


def _chat_folder_name_key(folder: dict) -> bytes:
    """A folder name as the frontend's ``<`` would compare it.

    ``chat_folder_tree`` is where an agent picks a ``before``/``after`` anchor, so a
    sequence that differs from the rendered one aims the anchor at the wrong gap.
    That makes this key part of a contract with ``folderTree.bySidebarOrder``, and
    every operation in it has to mean the same thing in both languages.

    No Unicode-table fold. ``str.lower()`` reads the interpreter's tables and
    ``String.prototype.toLowerCase()`` reads the browser's, so a character whose case
    mapping was added or changed between the interpreter's version and the browser's
    folds differently on the two sides — a skew no code here can close, because
    neither side owns both tables.

    ``A``-``Z`` fold anyway, through the literal 26-entry table above. That range's
    case mapping is fixed for every Unicode version ever published and the frontend
    does the same fold by ARITHMETIC on the code unit, so it carries no version
    dependency while still ordering ``alpha`` before ``Beta``. The distinction is
    worth drawing because a store written before ``order`` existed has every sibling
    tied at 0 — the whole tie-break decides those sidebars, so a fold that only
    ordered by raw code unit would show them uppercase-first until the first drag.

    ``utf-16-be`` rather than the str itself: Python orders str by CODE POINT while
    JavaScript orders by UTF-16 CODE UNIT, and the two disagree above U+FFFF — an
    astral character's surrogates start at 0xD800, so Python sorts U+1F600 after
    U+FF21 and JavaScript sorts it before. Comparing the big-endian UTF-16 bytes is
    code-unit order. This is a fixed encoding, not a table lookup, so it carries no
    version dependency either.

    ``surrogatepass`` because a folder name is persisted JSON and can hold a LONE
    surrogate, which the strict codec refuses outright — and a raised encoder
    inside a sort key takes down every folder tool, not one row. Passing it through
    also keeps the byte-for-byte match: a JavaScript string holds that same lone
    unit and compares it as 0xD800, which is exactly what these bytes carry.

    A name that is not a ``str`` reads as empty rather than being stringified, and
    the frontend's counterpart does the same. Stringifying is where the two
    languages part company: ``str({"a": 1})`` is ``"{'a': 1}"`` where
    ``String({a: 1})`` is ``"[object Object]"``, and ``str(True)`` is ``"True"``
    where ``String(true)`` is ``"true"``. Reading the whole class as empty makes
    both sides agree by construction instead of per type.
    """
    name = folder.get("name")
    text = name if isinstance(name, str) else ""
    return text.translate(_ASCII_FOLD).encode("utf-16-be", "surrogatepass")


def _chat_folder_created(folder: dict) -> float | None:
    """A folder's creation stamp as the sidebar reads it, or ``None`` when it has none.

    ``created_at`` is epoch seconds written by every folder creator since the
    sidebar's ``created`` sort existed; a row from before that carries no key. The
    acceptance rule is ``_chat_folder_order``'s, for the same reason: this value
    orders the ``created`` mode on both sides, so only a real, finite JSON number
    counts and everything else reads as "no stamp" identically here and in the
    sidebar's ``folderCreated``. Both sides clamp to the range JavaScript holds
    exactly, and an unbounded int is clamped BEFORE the ``float`` call because
    ``float(10**400)`` raises -- and this is a sort key, so nothing here may.
    """
    value = folder.get("created_at")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, int):
        value = max(-_CHAT_FOLDER_ORDER_LIMIT, min(_CHAT_FOLDER_ORDER_LIMIT, value))
    stamp = float(value)
    if stamp != stamp or stamp in (_POS_INF, _NEG_INF):  # NaN, ±Infinity
        return None
    return max(-float(_CHAT_FOLDER_ORDER_LIMIT), min(float(_CHAT_FOLDER_ORDER_LIMIT), stamp))


_DIGIT_RUNS = _re.compile(r"[0-9]+|[^0-9]+")


def _chat_folder_natural_key(folder: dict) -> tuple[tuple[Any, ...], ...]:
    """The ``name`` sort mode's key: case-folded natural order, ``01.`` < ``02.`` < ``10.``.

    The folded name is cut into maximal runs of ASCII digits and of everything
    else, and the runs are compared position by position. A digit run compares by
    its integer value -- leading zeros stripped, then length, then the digits, so
    the value is compared without ever being converted to a number and an
    arbitrarily long run cannot overflow on either side. A text run compares as
    ``_chat_folder_name_key`` does (UTF-16 code units of the ``A``-``Z``-folded
    text). Where a digit run meets a text run, the digit run sorts first; where
    every compared run is equal, the shorter name sorts first.

    Only ``0``-``9`` is a digit, deliberately: ``str.isdigit`` reads the
    interpreter's Unicode tables where the sidebar's ``/[0-9]/`` does not, and a
    key that agrees with ``folderTree.naturalNameCompare`` by construction is the
    whole point -- ``chat_folder_tree`` in this mode must list what the sidebar
    draws. Ties (``01`` against ``1``) fall through to the ``custom`` key.
    """
    name = folder.get("name")
    text = (name if isinstance(name, str) else "").translate(_ASCII_FOLD)
    key: list[tuple[Any, ...]] = []
    for run in _DIGIT_RUNS.findall(text):
        if "0" <= run[0] <= "9":
            digits = run.lstrip("0")
            key.append((0, len(digits), digits.encode("ascii")))
        else:
            key.append((1, run.encode("utf-16-be", "surrogatepass")))
    return tuple(key)


def _chat_folder_sort_key(mode: str) -> Callable[[dict], tuple[Any, ...]]:
    """The sibling sort key for one folder sort mode.

    ``custom`` is the stored ``order`` then the folded name -- the only order that
    existed before the mode did, and the one every placement (``before``/``after``)
    is computed in. ``name`` and ``created`` are VIEW orders layered on top: each
    ends in the ``custom`` key so two folders the mode cannot separate keep the
    order the person arranged, and so the whole key stays total on both sides.
    ``created`` is newest first, the direction the sidebar's session list already
    reads in; a folder with no stamp sorts as older than every stamped one.

    An unknown mode reads as ``custom`` rather than raising, because this is
    called from a listing an agent depends on and the loader has already reduced
    the stored value to a known one.
    """

    def custom(folder: dict) -> tuple[Any, ...]:
        return (_chat_folder_order(folder), _chat_folder_name_key(folder))

    if mode == "name":
        return lambda f: (_chat_folder_natural_key(f), *custom(f))
    if mode == "created":

        def created(folder: dict) -> tuple[Any, ...]:
            stamp = _chat_folder_created(folder)
            return (1, 0.0, *custom(folder)) if stamp is None else (0, -stamp, *custom(folder))

        return created
    return custom


def _chat_folder_siblings(
    folders: list[dict], parent_id: str, mode: str = FOLDER_SORT_DEFAULT
) -> list[dict]:
    """Direct children of ``parent_id``, in the order the sidebar renders them.

    The comparator mirrors the sidebar's own (``folderTree.folderComparator``
    sorts each parent's children by the person's folder sort mode -- stored
    ``order`` then name in ``custom``), because a caller saying "put this after
    that" means after what the PERSON SEES. Sorting by ``order`` alone would
    disagree with the rendered list wherever two siblings share a number, which
    the store permits.

    ``mode`` defaults to ``custom`` because that is the order a POSITION lives in:
    the placement helpers call this to find the gap a ``before``/``after`` anchor
    names, and a gap only exists in the stored order. The tree LISTING passes the
    person's mode instead, so it shows what the sidebar shows.

    The name key is ``_chat_folder_name_key``, which folds ``A``-``Z`` and compares
    the UTF-16 encoding, so ``Apple`` sorts between ``alpha`` and ``apricot``.
    Nothing outside that range folds: a wider fold would read each runtime's own
    Unicode tables, and the sidebar's runtime is not this one — see that function
    for which mappings are left alone and why.
    """
    kids = [f for f in folders if f.get("id") and str(f.get("parent_id") or "") == parent_id]
    return sorted(kids, key=_chat_folder_sort_key(mode))


# ``chat-sidebar/folderBlocks.tsx``'s ``renderFolderBlock`` returns nothing for ``depth > 10``, so a
# row nested deeper is not drawn. The store caps folder COUNT but not nesting, so
# such a tree is legal rather than corrupt. Only the reported DEPTH is clamped
# here, never the row: this listing is an agent's one view of the tree and it
# carries each folder's live sessions, so dropping a folder would hide those
# sessions to buy indentation parity on a row nobody can see anyway.
_SIDEBAR_MAX_DRAWN_DEPTH = 10


def _chat_folder_render_order(
    folders: list[dict], mode: str = FOLDER_SORT_DEFAULT
) -> list[tuple[str, int]]:
    """``(folder_id, depth)`` in sidebar render order — pre-order, siblings by ``mode``.

    Depth-first from the top level, so a child always follows its parent, which
    is the shape the sidebar draws. Siblings sort with ``_chat_folder_sort_key``
    for the person's folder sort mode, at every depth, because the sidebar applies
    its comparator to every parent's children alike. Two defences the sidebar also
    carries: a row whose ``parent_id`` names a folder absent from this list renders
    at the top level rather than being dropped, and a parent cycle can neither
    loop the walk nor swallow the folders caught in it (those are appended at the
    end).

    Depth is clamped to ``_SIDEBAR_MAX_DRAWN_DEPTH`` so the indentation cannot claim
    a level the sidebar does not draw. Every row is still listed at that depth: an
    anchor beside a row the person cannot see writes an ``order`` nobody reads,
    which is a smaller cost than hiding the folder and the live sessions filed in
    it from the only tree view an agent gets.
    """
    known = {str(f.get("id") or "") for f in folders if f.get("id")}
    by_parent: dict[str, list[dict]] = {}
    for folder in folders:
        if not folder.get("id"):
            continue
        parent = str(folder.get("parent_id") or "")
        by_parent.setdefault(parent if parent in known else "", []).append(folder)
    sort_key = _chat_folder_sort_key(mode)
    for kids in by_parent.values():
        kids.sort(key=sort_key)

    out: list[tuple[str, int]] = []
    seen: set[str] = set()
    # Iterative walk: the store caps folder COUNT but not nesting depth, and a
    # recursive descent would answer a deep chain with a RecursionError.
    stack: list[tuple[str, int]] = [
        (str(f.get("id") or ""), 0) for f in reversed(by_parent.get("", []))
    ]
    while stack:
        fid, depth = stack.pop()
        if not fid or fid in seen:
            continue
        seen.add(fid)
        out.append((fid, min(depth, _SIDEBAR_MAX_DRAWN_DEPTH)))
        stack.extend((str(f.get("id") or ""), depth + 1) for f in reversed(by_parent.get(fid, [])))
    for folder in folders:
        fid = str(folder.get("id") or "")
        if fid and fid not in seen:
            seen.add(fid)
            out.append((fid, 0))
    return out


def _free_slot_order(siblings: list[dict], index: int) -> int | None:
    """An unused ``order`` that lands a folder at ``index``, or ``None`` if none fits.

    Positioning a folder means writing several rows when the siblings have to be
    renumbered, and several writes cannot be made atomic from here — so the cheap
    case is worth taking whenever the store already has room: before the first
    sibling, after the last, or in a gap between two adjacent ones. Then the whole
    reposition is ONE write and cannot land half-applied.

    ``siblings`` is the destination's children WITHOUT the folder being placed, in
    render order. ``None`` means the neighbours are adjacent integers, which is
    what a sidebar drag leaves behind, and the caller must renumber instead.

    An edge slot at ``±_CHAT_FOLDER_ORDER_LIMIT`` is also not free. The neighbour's
    order is read back through the same clamp, so a value one past the bound
    returns as the bound itself — equal to the anchor rather than outside it, which
    hands the pair to the name tie-break and can place the folder on the wrong
    side. There is no representable slot there, so the caller renumbers.
    """
    if not siblings:
        return 0
    if index <= 0:
        first = _chat_folder_order(siblings[0])
        return None if first <= -_CHAT_FOLDER_ORDER_LIMIT else first - 1
    if index >= len(siblings):
        last = _chat_folder_order(siblings[-1])
        return None if last >= _CHAT_FOLDER_ORDER_LIMIT else last + 1
    low = _chat_folder_order(siblings[index - 1])
    high = _chat_folder_order(siblings[index])
    # Equal or inverted neighbours leave no room either: the pair is already
    # separated only by the name tie-break, which no order value can get between.
    if high - low >= 2:
        return low + (high - low) // 2
    return None


def _slot_group(row: dict, known_folder_ids: set[str]) -> str:
    """The sidebar group a session row renders in: its folder id, or ``""``.

    A folder id with no folder row renders at the top level, the same fallback
    ``chat_folder_tree`` uses.
    """
    fid = str(row.get("folder_id") or "")
    return fid if fid in known_folder_ids else ""


def _pinned_sidebar_order(rows: list[dict]) -> list[dict]:
    """The pinned rows of one group in the order the sidebar draws them.

    Rows with a stored ``pin_rank`` come first, by rank. Pinned rows without
    one (the person never reordered, or pinned before the order was in use)
    follow newest activity first, which is the sidebar's default sort.
    """
    pinned = [r for r in rows if r.get("pinned")]
    ranked = sorted(
        (r for r in pinned if isinstance(r.get("pin_rank"), int)), key=lambda r: r["pin_rank"]
    )
    unranked = sorted(
        (r for r in pinned if not isinstance(r.get("pin_rank"), int)),
        key=lambda r: str(r.get("last_turn_ts") or r.get("last_ts") or r.get("created") or ""),
        reverse=True,
    )
    return [*ranked, *unranked]


def _sidebar_group_order(rows: list[dict]) -> list[dict]:
    """*rows* of one group with the pinned ones first, in sidebar order."""
    pinned = _pinned_sidebar_order(rows)
    return [*pinned, *(r for r in rows if not r.get("pinned"))]
