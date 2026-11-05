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

    Rows may carry duplicate orders, gaps or no ``order`` key; all must be tolerated.
    Only a real JSON number is accepted, the same set the frontend accepts, so the
    value sorts identically on both sides (``int("1e3")`` raises where ``Number``
    reads 1000). A float truncates like ``Math.trunc``; ``bool`` is excluded because
    ``typeof true`` is not ``'number'``. The clamp to ``2**53 - 1`` keeps rows that
    ``JSON.parse`` would collapse to one value from comparing as ordered here.

    This is a SORT KEY: an exception would abort every folder tool, so nothing raises.
    """
    value = folder.get("order")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    if value != value or value in (_POS_INF, _NEG_INF):  # NaN, ±Infinity
        return 0
    return max(-_CHAT_FOLDER_ORDER_LIMIT, min(_CHAT_FOLDER_ORDER_LIMIT, int(value)))


# A-Z to a-z only, written out: every Unicode version maps this range the same,
# so both sides fold it without a table.
_ASCII_FOLD = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


def _chat_folder_name_key(folder: dict) -> bytes:
    """A folder name as the frontend's ``<`` would compare it.

    An agent picks ``before``/``after`` anchors from ``chat_folder_tree``, so this key
    must match ``folderTree.bySidebarOrder`` exactly or the anchor hits the wrong gap.

    Only ``A``-``Z`` fold (the frontend folds them by arithmetic): any wider fold reads
    each runtime's own Unicode tables, which can differ. The fold still matters because
    stores from before ``order`` tie every sibling at 0, so the name decides the order.

    ``utf-16-be`` bytes give JavaScript's code-unit order, which differs from Python's
    code-point order above U+FFFF. ``surrogatepass`` because a stored name can hold a
    lone surrogate, which the strict codec raises on (and a sort key must not raise).

    A non-``str`` name reads as empty, as on the frontend, because the two languages
    stringify objects and booleans differently.
    """
    name = folder.get("name")
    text = name if isinstance(name, str) else ""
    return text.translate(_ASCII_FOLD).encode("utf-16-be", "surrogatepass")


def _chat_folder_created(folder: dict) -> float | None:
    """A folder's creation stamp as the sidebar reads it, or ``None`` when it has none.

    ``created_at`` is epoch seconds; older rows lack it. It uses
    ``_chat_folder_order``'s acceptance rule to match the sidebar's ``folderCreated``.
    An int is clamped BEFORE ``float`` because ``float(10**400)`` raises, and a sort
    key must not raise.
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

    The folded name is cut into runs of ASCII digits and of other text. A digit run
    compares by length after stripping leading zeros, then by digits, so a long run
    never overflows on either side; a text run compares as ``_chat_folder_name_key``.
    A digit run sorts before a text run; on equal runs the shorter name sorts first.

    Only ``0``-``9`` is a digit: ``str.isdigit`` reads Unicode tables that the
    sidebar's ``/[0-9]/`` does not, and this key must match
    ``folderTree.naturalNameCompare``. Ties (``01`` against ``1``) fall to ``custom``.
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

    ``custom`` is stored ``order`` then folded name; placements are computed in it.
    ``name`` and ``created`` end in the ``custom`` key so ties keep the person's
    arrangement. ``created`` is newest first; an unstamped folder sorts oldest.
    An unknown mode reads as ``custom`` rather than raising.
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

    The comparator mirrors ``folderTree.folderComparator``, because "put this after
    that" means after what the person sees, and siblings may share an ``order``.

    ``mode`` defaults to ``custom`` because a ``before``/``after`` gap only exists in
    the stored order; the tree listing passes the person's mode to match the sidebar.
    """
    kids = [f for f in folders if f.get("id") and str(f.get("parent_id") or "") == parent_id]
    return sorted(kids, key=_chat_folder_sort_key(mode))


# ``chat-sidebar/folderBlocks.tsx``'s ``renderFolderBlock`` draws nothing past depth 10.
# Nesting is not capped, so only the reported depth is clamped, never the row:
# dropping a folder would hide its live sessions from the agent.
_SIDEBAR_MAX_DRAWN_DEPTH = 10


def _chat_folder_render_order(
    folders: list[dict], mode: str = FOLDER_SORT_DEFAULT
) -> list[tuple[str, int]]:
    """``(folder_id, depth)`` in sidebar render order — pre-order, siblings by ``mode``.

    As the sidebar does, a row whose ``parent_id`` is missing renders at the top
    level, and folders caught in a parent cycle are appended at the end. Depth is
    clamped to ``_SIDEBAR_MAX_DRAWN_DEPTH``, but every row is still listed so no
    folder's live sessions are hidden from the agent.
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

    A free slot makes the move ONE write, which cannot land half-applied the way a
    multi-row renumber can. ``siblings`` excludes the folder being placed, in render
    order. ``None`` (adjacent neighbours, or an edge at ``±_CHAT_FOLDER_ORDER_LIMIT``,
    where the clamp would tie the new order with the anchor) means renumber.
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
