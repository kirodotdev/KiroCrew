"""Sibling order for sidebar chat folders: fractional rank keys.

A folder's place among its siblings is a short ``rank`` string, and siblings
sort by comparing those strings. To put a folder between two neighbours the
server generates a string that sorts strictly between theirs, so the common
move writes only the moved folder. A section that first needs normalization is
re-ranked in place in its current order before the move lands. This is the
standard fractional-indexing technique: a rank is read as the digits of a
base-62 fraction ``0.d1d2d3...``.

Folders written before ranks existed carry only the legacy integer ``order``.
They sort AFTER every ranked sibling, by that ``order`` then by name, which is
exactly the order older builds drew. There is no migration step: the first
positioning in a section that holds an unranked sibling, a corrupt rank or two
siblings sharing a rank (see :func:`plan_position`) gives every sibling in that
section a fresh, evenly spaced rank in the order they already render in, then
places the moved folder. That spread never changes where any other folder is
drawn; it only makes the section rankable. Every later move there writes a
single new rank, until a gap is exhausted and the section is re-spread.

Everything here is pure and never raises on stored data: the store is read with
a bare ``json.loads`` and a rank or order can hold any JSON value, so a value
this module does not accept is treated as absent rather than as an error.

The sidebar mirrors the comparator and the key generator in
``website/src/utils/folderRank.ts``; ``test/fixtures/chat_folder_rank.json``
and ``test/fixtures/chat_folder_sibling_order.json`` hold the cases both
suites must agree on.
"""

from __future__ import annotations

from typing import Any

#: Rank digits in ascending code-unit order, so plain string comparison is
#: numeric comparison of the fractions. ASCII only: Python compares ``str`` by
#: code point and JavaScript by UTF-16 code unit, and the two agree on ASCII.
RANK_DIGITS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
_BASE = len(RANK_DIGITS)
_DIGIT_VALUE = {c: i for i, c in enumerate(RANK_DIGITS)}

#: Longest rank accepted from the store or generated for a move. Inserting into
#: the same gap over and over grows the key by about one character every five
#: or six moves; when a positioned move would pass this bound the section is
#: re-spread. An unpositioned append at an exhausted tail stays unranked, sorts
#: last by its ``order``, and the person's next move re-spreads the section.
MAX_RANK_LEN = 48

# The largest integer JavaScript represents exactly (``Number.MAX_SAFE_INTEGER``).
# The sidebar reads folder rows through ``JSON.parse``, so an ``order`` past this
# is not the number the store holds; both sides clamp to it so their comparisons
# agree on rows no sane writer produces but a hand-edited store can.
ORDER_LIMIT = 2**53 - 1

_POS_INF = float("inf")
_NEG_INF = float("-inf")

# A-Z to a-z and nothing else, written out rather than looked up. Every Unicode
# version ever published maps this range identically, so both sides can fold it
# without consulting a table.
_ASCII_FOLD = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


def valid_rank(value: object) -> str | None:
    """``value`` when it is a usable rank, else ``None``.

    Usable means a non-empty ``str`` of at most :data:`MAX_RANK_LEN` rank digits
    that does not end in ``0``. The trailing-zero rule keeps one spelling per
    fraction (``"1"`` and ``"10"`` are the same number), which is what makes
    string order equal numeric order and guarantees a key exists between any
    two distinct ranks.
    """
    if not isinstance(value, str) or not value or len(value) > MAX_RANK_LEN:
        return None
    if value[-1] == "0" or any(c not in _DIGIT_VALUE for c in value):
        return None
    return value


def rank_between(lo: str | None, hi: str | None) -> str | None:
    """A rank strictly between ``lo`` and ``hi``, or ``None`` when none fits.

    ``None`` for ``lo`` means "before everything", for ``hi`` "after
    everything". The result is deterministic, so two callers asking for the
    same gap get the same key; siblings that tie on rank are separated by id.

    ``None`` comes back when either bound is not a valid rank, when ``lo`` is
    not below ``hi``, or when the only key that fits would be longer than
    :data:`MAX_RANK_LEN`. The caller answers all three the same way: re-spread
    the section.
    """
    if lo is not None and valid_rank(lo) is None:
        return None
    if hi is not None and valid_rank(hi) is None:
        return None
    low = lo or ""
    if hi is not None and not low < hi:
        return None
    upper: str | None = hi
    out: list[str] = []
    # Walk digit by digit. While both bounds share a digit it is copied. Where
    # they differ by two or more, the midpoint digit ends the key. Where they
    # differ by exactly one, the lower digit is kept and the upper bound is
    # dropped, so the rest only has to exceed what remains of ``low``. The walk
    # is bounded by the two lengths: past the end of ``low`` its digit reads as
    # 0 and the next step always has room.
    for i in range(len(low) + len(hi or "") + 2):
        a = _DIGIT_VALUE[low[i]] if i < len(low) else 0
        if upper is None:
            b = _BASE
        else:
            b = _DIGIT_VALUE[upper[i]] if i < len(upper) else 0
        if a == b:
            out.append(RANK_DIGITS[a])
            continue
        if b - a > 1:
            out.append(RANK_DIGITS[(a + b) // 2])
            key = "".join(out)
            return key if len(key) <= MAX_RANK_LEN else None
        out.append(RANK_DIGITS[a])
        upper = None
    return None  # unreachable for valid bounds; refusing beats looping


def spread_ranks(count: int) -> list[str]:
    """``count`` ascending ranks, evenly spaced, with room in every gap.

    Width is one digit more than ``count`` strictly needs, so each gap holds
    dozens of later inserts before any key grows past three characters.
    """
    if count <= 0:
        return []
    width = 1
    while _BASE**width < count + 1:
        width += 1
    width += 1
    step = _BASE**width // (count + 1)
    out: list[str] = []
    for i in range(1, count + 1):
        value = i * step
        digits: list[str] = []
        for _ in range(width):
            value, d = divmod(value, _BASE)
            digits.append(RANK_DIGITS[d])
        out.append("".join(reversed(digits)).rstrip("0"))
    return out


def legacy_order(folder: dict) -> int:
    """A folder's legacy integer ``order``. Anything that is not a finite number is 0.

    The store can hold a duplicate order, a gap, or no ``order`` key at all, and
    every reader tolerates all three. Only a real JSON number is accepted, and
    the sidebar's counterpart accepts exactly the same set: the two languages'
    conversions of everything else disagree (``int("0x10")`` raises where
    ``Number`` reads 16). A float truncates toward zero, matching ``Math.trunc``;
    ``bool`` is excluded because ``typeof true`` is not ``'number'``; the result
    is clamped to the range JavaScript represents exactly.

    This is a sort key, so nothing here may raise.
    """
    value = folder.get("order")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    if value != value or value in (_POS_INF, _NEG_INF):  # NaN, ±Infinity
        return 0
    return max(-ORDER_LIMIT, min(ORDER_LIMIT, int(value)))


def name_key(folder: dict) -> bytes:
    """A folder name as the sidebar's ``<`` compares it.

    Only ``A``-``Z`` fold, through a literal table, because a wider fold reads
    each runtime's own Unicode tables and the sidebar's runtime is not this
    one. The UTF-16-BE encoding makes byte order equal JavaScript's code-unit
    order, which differs from Python's code-point order above U+FFFF;
    ``surrogatepass`` keeps a lone surrogate in a persisted name from raising.
    A name that is not a ``str`` reads as empty on both sides.
    """
    name = folder.get("name")
    text = name if isinstance(name, str) else ""
    return text.translate(_ASCII_FOLD).encode("utf-16-be", "surrogatepass")


def custom_sort_key(folder: dict) -> tuple[Any, ...]:
    """The ``custom`` sibling order: ranked folders by (rank, id), then the rest.

    A folder without a valid rank sorts after every ranked sibling, by its
    legacy ``order`` and then its name, so a section nobody has repositioned
    since ranks existed draws exactly as it did before. The id tie-break on
    ranked rows compares UTF-16 code units so it matches the browser, meaning
    two rows that end up with the same rank (a hand edit, or a store merged from
    two copies) still sort the same way on every client.
    """
    rank = valid_rank(folder.get("rank"))
    if rank is not None:
        fid = folder.get("id")
        id_key = fid.encode("utf-16-be", "surrogatepass") if isinstance(fid, str) else b""
        return (0, rank, id_key)
    return (1, legacy_order(folder), name_key(folder))


def plan_position(siblings: list[dict], index: int) -> tuple[str, dict[str, str]]:
    """Where to put a folder at ``index`` among ``siblings``.

    ``siblings`` is the destination's children WITHOUT the folder being placed,
    sorted by :func:`custom_sort_key`. Returns the moved folder's new rank and
    a map of sibling id to replacement rank, which is empty on the common path.

    The common path is available only when every sibling carries a valid rank
    and all those ranks are distinct. The new rank then goes between the two
    neighbours. Checking the whole section prevents a duplicate or unranked
    row elsewhere from surviving a move that appears locally rankable.

    Anything else (any unranked or corrupt sibling, any duplicate rank, or a
    gap too deep for :data:`MAX_RANK_LEN`) re-spreads the section: every
    sibling gets a fresh rank in its CURRENT order and the moved folder takes
    the slot at ``index``. No sibling changes position relative to any other,
    so the spread is a normalization of the section rather than a move of the
    folders it touches.
    """
    index = max(0, min(index, len(siblings)))
    ranks = [valid_rank(sibling.get("rank")) for sibling in siblings]
    section_ranked = all(rank is not None for rank in ranks)
    section_distinct = section_ranked and len(set(ranks)) == len(ranks)
    if section_distinct:
        lo = ranks[index - 1] if index > 0 else None
        hi = ranks[index] if index < len(ranks) else None
        rank = rank_between(lo, hi)
        if rank is not None:
            return rank, {}
    fresh = spread_ranks(len(siblings) + 1)
    own = fresh.pop(index)
    respread: dict[str, str] = {}
    for sibling, new_rank in zip(siblings, fresh, strict=True):
        sid = sibling.get("id")
        if isinstance(sid, str) and sibling.get("rank") != new_rank:
            respread[sid] = new_rank
    return own, respread


def section_siblings(folders: list[dict], parent_id: str, exclude_id: str = "") -> list[dict]:
    """The children of ``parent_id`` in ``custom`` order, without ``exclude_id``.

    A row whose ``parent_id`` names no folder in ``folders`` is drawn in the top
    level by the sidebar, so it counts as a top-level sibling here too.
    """
    known = {f.get("id") for f in folders if isinstance(f.get("id"), str)}

    def container(f: dict) -> str:
        pid = f.get("parent_id")
        return pid if isinstance(pid, str) and pid in known else ""

    kids = [
        f
        for f in folders
        if isinstance(f.get("id"), str) and f.get("id") != exclude_id and container(f) == parent_id
    ]
    return sorted(kids, key=custom_sort_key)


def assign_append_rank(folder: dict, folders: list[dict], *, clear_unranked: bool = False) -> None:
    """Assign the rank for appending ``folder`` to its sibling section."""
    parent_id = str(folder.get("parent_id", "") or "")
    rank = append_rank(section_siblings(folders, parent_id))
    if rank is not None:
        folder["rank"] = rank
    elif clear_unranked:
        folder.pop("rank", None)


def append_rank(siblings: list[dict]) -> str | None:
    """A rank after the last sibling when every sibling is ranked, else ``None``.

    Used when a folder joins a section without a chosen position (created, or
    reparented with no anchor). In an empty or fully ranked section this normally
    keeps the section fully ranked, so the next move there is a single write. If
    the tail key would exceed :data:`MAX_RANK_LEN`, however, the newcomer stays
    unranked and sorts last by its ``order``; the person's next move re-spreads
    the section. A section with an unranked row also leaves the newcomer
    unranked, which sorts it after the ranked rows by its ``order``: the end,
    where a new folder has always appeared.
    """
    if not siblings:
        return rank_between(None, None)
    ranks = [valid_rank(f.get("rank")) for f in siblings]
    if any(r is None for r in ranks):
        return None
    return rank_between(max(r for r in ranks if r is not None), None)
