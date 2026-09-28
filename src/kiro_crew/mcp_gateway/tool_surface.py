"""The tool set a session was already told about, projected so only a real
change shows.

A client freezes its MCP tool set when the session is created and never
re-reads it. When the PROCESS behind that session is replaced — the daemon's
transparent respawn after a shared backend dies — the session keeps building
calls against the schema the OLD process published. This module is the one
place that answers "would a call built against what we already served still be
the same call here?", so every adoption point asks that question the same way
instead of inventing its own policy.

What is compared, and why only this
-----------------------------------
Each tool's NAME mapped to a canonical form of its ``inputSchema``. That is
exactly the part of a listing a pending call is constructed from, and it is
what moves in each concrete regression: a renamed tool, an argument whose type
changed, a newly required field.

``description``, ``annotations`` and ``outputSchema`` are deliberately left
out. None of them can invalidate the arguments a client has already built, so
folding them in would only cost transparent recoveries at servers that reword a
sentence between builds.

Model visibility is part of the answer, as a FILTER
---------------------------------------------------
Only tools whose ``_meta.ui.visibility`` admits the model are projected, read
through the same ``visibility_allows`` the agent-facing listing filter uses so
the two cannot drift. Visibility has to be in scope because it is enforced at
LIST time only: nothing re-checks it when a model-originated ``tools/call``
arrives, so a tool a replacement withdraws from the model is still listed by a
frozen client and its call would be forwarded and executed. Same name, same
schema, and the call is one the server no longer offers the model.

It is a filter and not a compared field because the two directions are not
symmetric. A tool the client HELD and the replacement withdraws leaves the
projected set, so it reads as ``gone`` — the refusal that case needs. A tool
that was app-only and becomes model-visible ENTERS the set, so it reads as an
addition, which is agreement for the same reason any addition is: the client's
tool set is frozen, it never held that tool, and no call it built names it.
Comparing a visibility flag per tool instead would refuse that second case for
nothing.

Ordering inside a schema is NOT normalised. ``required: ["a", "b"]`` and
``required: ["b", "a"]`` therefore read as a change even though JSON Schema
gives them the same meaning. That is the deliberate answer rather than an
oversight: distinguishing a reorder from a rewrite needs a JSON-Schema
semantic model, and the cost of the conservative reading is one lost
transparent recovery — the session reconnects — whereas the cost of the
permissive reading is a call issued against a schema that no longer exists.
Tool ORDER across the list is not compared at all: the projection is a mapping,
so a server free to enumerate its tools differently each spawn is not thereby
reported as changed.

Not a cache
-----------
A projection is never read as an authorization input. ``app_call`` fetches
``tools/list`` FRESH on every call precisely because a server may revoke a
tool's visibility at any moment, and nothing here weakens that stance: this is
a record of what a client was ALREADY told, consulted only to notice that the
record and a replacement disagree. It authorizes nothing and it is never
served to anybody.

Unmeasurable is its own answer
------------------------------
A listing this module cannot project reduces to ``None`` rather than to an
empty surface, so "the server named no tools" stays distinguishable from "we
could not read what it named". Each caller decides what to do with that;
collapsing the two here would let a malformed listing read as agreement.

Bounded by construction, not by the server
------------------------------------------
A surface is retained per stub for that stub's lifetime, and its size must
therefore not be a function of what a server chooses to send. Two things follow:
schema VALUES are stored as fixed-width digests rather than as canonical text
(one ``tools/list`` frame may be as large as the transport allows), and a
listing over the tool-count or name-length budget is refused as unmeasurable
rather than truncated to fit. Truncating would silently drop tools from the
comparison, which is the failure this module exists to prevent.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Optional

from kiro_crew.mcp_gateway.apps import AUDIENCE_MODEL, visibility_allows

#: Tool name -> a fixed-width digest of the canonical ``inputSchema``. A mapping,
#: so tool order is out of the comparison by construction.
#:
#: A DIGEST, not the schema text. A surface is retained per stub for the life of
#: that stub, and one ``tools/list`` frame may be as large as the transport
#: allows (64 MiB by default), so keeping the canonical text would let a big
#: listing pin megabytes per session. The comparison only ever asks whether two
#: schemas are equal, which a digest answers in 64 bytes.
ToolSurface = Dict[str, str]

#: How many tool names a change description names before it summarises the
#: rest. A server may publish hundreds of tools, and this string is logged and
#: audited — an unbounded one would put a whole listing in a log line.
_MAX_NAMED = 5

#: How many characters of ONE name survive into that description. A tool name is
#: server-chosen and unbounded; capping the count alone would still let a single
#: pathological name dominate a log line.
_MAX_NAME_CHARS = 80

#: Retention budget for a projection: at most this many MODEL-VISIBLE tools,
#: each named in at most this many characters. Beyond either bound the listing is
#: unmeasurable rather than truncated — dropping tools to fit would make a
#: dropped tool's schema change read as agreement, which is the silent adoption
#: this module exists to prevent. Counted over the visible set only, and checked
#: as it accumulates, so a tool the model cannot see can neither spend the budget
#: nor use it to make the listing unmeasurable. Sized well above any real server
#: (the largest MCP servers publish tens of tools), so the bound is a ceiling on
#: a pathological or hostile listing rather than a limit a genuine one meets.
_MAX_TOOLS = 2048
_MAX_NAME_LEN = 512

#: Line terminators that live OUTSIDE the C0/C1 ranges, so a predicate built
#: from those ranges alone would let them through. See :func:`_is_control`.
_EXTRA_LINE_BREAKS = frozenset("\u2028\u2029")


def project_tool_surface(result: Any) -> Optional[ToolSurface]:
    """Project a ``tools/list`` *result* into a comparable surface.

    Returns ``None`` — "unmeasurable" — when the listing is not shaped like one
    this module can compare. Every case below is scoped to the MODEL-VISIBLE set,
    because that is the only set this projection describes: an invisible tool is
    skipped before any of these rules can reject it, so it can never make the
    listing unmeasurable and thereby switch validation off for the tools the
    client actually holds.

    * *result* is not an object, or its ``tools`` is not an array. There is
      nothing to project.
    * an entry is not an object. Visibility cannot be evaluated, so whether it
      belongs to the visible set is unknowable.
    * a VISIBLE entry's ``name`` is not a string. A server that cannot name a
      tool is not one a comparison can conclude anything about, the same answer
      :mod:`kiro_crew.mcp_gateway.preflight` already gives for a non-string tool
      name.
    * a VISIBLE name appears twice. Two entries cannot both survive into a
      mapping, and which one a client kept is not knowable on this side — so the
      honest answer is that this listing is not projectable, not a silent drop.
    * a VISIBLE tool's schema does not survive ``json.dumps``. Nothing off the
      wire should, but a synthesised listing could, and a crash here would
      propagate into an adoption path whose whole job is to be the safe one.
    * the VISIBLE tools exceed the retention budget — more than ``_MAX_TOOLS``
      of them, or one named longer than ``_MAX_NAME_LEN``. A surface is held per
      stub for that stub's lifetime, and the answer must be bounded by something
      other than what a server chooses to send.

    There is deliberately no cap on the RAW entry count. Bounding retention is
    the budget's job, and a raw cap would be exactly the off switch described
    above. The per-entry work is bounded by the transport's own frame limit —
    the same bound ``apps.strip_model_hidden_tools`` already lives with while
    iterating this identical listing.
    """
    if not isinstance(result, dict):
        return None
    tools = result.get("tools")
    if not isinstance(tools, list):
        return None
    surface: ToolSurface = {}
    for entry in tools:
        if not isinstance(entry, dict):
            # Visibility cannot be evaluated at all, so whether this entry
            # belongs to the model-visible set is unknowable. Everything below
            # is scoped to that set, so an entry that cannot be placed in or
            # out of it makes the listing unmeasurable rather than partially
            # read.
            return None
        if not visibility_allows(entry, AUDIENCE_MODEL).allowed:
            # Not the client's to hold, so not part of what a replacement can
            # contradict. Skipped rather than recorded and compared — see the
            # module docstring for why visibility is a filter here, not a
            # compared field.
            #
            # Checked FIRST, ahead of every other per-entry rule, and that
            # ordering is load-bearing: each rule below answers with
            # "unmeasurable", which clears the anchor and leaves a respawn
            # unvalidated. Judged before the visibility filter, a tool the model
            # cannot even see — one absurd name, one entry past the budget —
            # would switch the guard off for every tool it CAN see. A hidden
            # tool must not be able to do that.
            continue
        name = entry.get("name")
        if not isinstance(name, str) or len(name) > _MAX_NAME_LEN:
            return None
        if name in surface:
            return None
        try:
            canonical = json.dumps(entry.get("inputSchema"), sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError):
            return None
        surface[name] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        if len(surface) > _MAX_TOOLS:
            # The budget bounds what is RETAINED, so it counts model-visible
            # tools only and is checked as they accumulate — an invisible tool
            # cannot spend it, and the loop stops the moment it is spent rather
            # than reading a hostile listing to the end.
            return None
    return surface


def describe_surface_change(old: Optional[ToolSurface], new: Optional[ToolSurface]) -> str:
    """One line naming what moved between two surfaces, or ``""`` for agreement.

    Truthiness IS the verdict, so a caller reads one value for both the
    decision and the reason it reports.

    A tool the replacement ADDS is agreement, not a change. The whole inclusion
    criterion here is "can this invalidate arguments a client already built?",
    and a name the client does not know cannot: no held call names it. Reporting
    an addition would turn the most common upgrade shape — a server that grew a
    tool — into a refused recovery, buying nothing, since the client's tool set
    is frozen and it could not call the new tool either way. A tool that is GONE
    or whose schema MOVED is the opposite: a held call names it.

    The two ``None`` cases are deliberately asymmetric, because they are not the
    same situation:

    * ``old is None`` — nothing was ever served that this could contradict, so
      there is no claim to break and the answer is agreement. A caller must not
      refuse an adoption over a comparison it never had an anchor for.
    * ``new is None`` while ``old`` is a real surface — the same server answered
      projectably before and does not now. That IS the change, reported as one.
    """
    if old is None:
        return ""
    if new is None:
        return "the replacement's tools/list could not be read as a tool set"
    gone = sorted(set(old) - set(new))
    changed = sorted(n for n in set(old) & set(new) if old[n] != new[n])
    parts = [
        f"{label}={_named(names)}"
        for label, names in (("gone", gone), ("schema-changed", changed))
        if names
    ]
    return "; ".join(parts)


def _named(names: list[str]) -> str:
    """Comma-joined names, sanitised and bounded, with a count for the rest."""
    shown = ",".join(_sanitise(n) for n in names[:_MAX_NAMED])
    extra = len(names) - _MAX_NAMED
    return f"{shown} (+{extra} more)" if extra > 0 else shown


def _sanitise(name: str) -> str:
    """A tool name safe to put in one line of a log or an audit record.

    A tool name is the SERVER's string, and the description this module builds
    is written to a ``logger.warning`` and to an SEL audit field. A name
    carrying a newline or a carriage return would end the record early and open
    a second one the server controls entirely — a forged entry in an
    append-only trail, from data the server chose. Every C0 control, DEL and C1
    control becomes a visible ``\\xNN`` escape instead.

    Length is bounded for the same reason the NAME COUNT is: a server may call
    a tool anything, and one pathological name should not put kilobytes into a
    log line. Truncation is marked so a reader never mistakes the shortened
    form for the whole name.
    """
    escaped = "".join(_escape(ch) if _is_control(ch) else ch for ch in name)
    if len(escaped) <= _MAX_NAME_CHARS:
        return escaped
    return escaped[:_MAX_NAME_CHARS] + "..."


def _escape(ch: str) -> str:
    """One control rendered visibly, in the width its code point needs.

    A fixed two-digit form would render U+2028 as ``\\x2028``, which reads as a
    space followed by the digits 28 — so the escape would hide exactly the
    character it exists to expose.
    """
    point = ord(ch)
    return f"\\x{point:02x}" if point <= 0xFF else f"\\u{point:04x}"


def _is_control(ch: str) -> bool:
    """Anything that can open a second line: C0 controls, DEL, C1 controls, and
    the Unicode line and paragraph separators.

    "Control" is defined here by CONSEQUENCE, not by Unicode category. The only
    question this predicate answers is whether a consumer reading one line of
    log or one audit field would see a second one, and the code-point RANGES
    alone do not answer it: U+2028 and U+2029 sit far outside them and Python's
    own ``str.splitlines`` breaks on both, as do a JavaScript parser and several
    log viewers. ``str.splitlines`` is therefore the reference for the set, and
    a ratchet test holds this function to it rather than to an enumeration
    somebody has to keep correct by hand.
    """
    point = ord(ch)
    return point < 0x20 or point == 0x7F or 0x80 <= point <= 0x9F or ch in _EXTRA_LINE_BREAKS
