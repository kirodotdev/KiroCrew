"""The curation plan (review file): deny-by-default, signature, content pin.

Ported from ``crew_export/plan.py`` -- JSON instead of YAML (no PyYAML here). The template the
``plan`` verb writes, the guarded read of an ``--allow`` file, the merge of several, the
verification of the merge against live candidates, and the decision set both verbs print and
the report records.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import destination as _destination
from . import pinned as _pinned
from . import sensitive as _sensitive
from .candidates import Candidate
from .contract import PLAN_VERSION, ExportRefused

_KINDS = ("skills", "mcp")

_PLAN_INSTRUCTIONS = (
    "Everything below starts include:false. Flip include:true on the skills and "
    "MCP servers a customer may reach, fill in reviewed_by and reviewed_at, then "
    "pass this file to the build with --allow. Leaving it untouched is valid: you "
    "get a working crew with its persona and no private content. Do not hand-edit "
    "sha256 -- it pins each entry to the content you reviewed; if a SELECTED entry "
    "changes afterwards the build refuses and names it. A 'blocked' entry cannot "
    "be included at all."
)


@dataclass
class Plan:
    crew: str
    reviewed_by: str
    reviewed_at: str
    selections: dict[str, dict[str, bool]]
    pins: dict[str, dict[str, str]]

    def included(self, kind: str) -> set[str]:
        return {cid for cid, on in self.selections.get(kind, {}).items() if on}

    def is_signed(self) -> bool:
        return bool(self.reviewed_by.strip()) and bool(self.reviewed_at.strip())

    def selects_anything(self) -> bool:
        return any(self.included(kind) for kind in _KINDS)


@dataclass
class Drift:
    appeared: int = 0
    vanished: int = 0

    def describe(self) -> str:
        parts = []
        if self.appeared:
            parts.append(f"{self.appeared} new candidate(s) appeared (all excluded)")
        if self.vanished:
            parts.append(f"{self.vanished} candidate(s) no longer exist")
        return "; ".join(parts)


def write_plan(path: Path, crew: str, candidates: dict[str, list[Candidate]]) -> bool:
    """Write a fresh deny-by-default review template, claiming the name atomically.

    Returns ``True`` when this call created the plan and ``False`` when a plan was already
    there. The two outcomes are decided by the ``O_EXCL`` open itself, not by an ``is_file()``
    check before it: re-running ``plan`` on an already-planned crew is normal, and a check-
    then-write let a racer's plan be truncated between the two. A symlink or a directory at the
    path is still refused rather than treated as "already planned".
    """
    body: dict[str, object] = {
        "plan_version": PLAN_VERSION,
        "crew": crew,
        "instructions": _PLAN_INSTRUCTIONS,
        "reviewed_by": "",
        "reviewed_at": "",
    }
    for kind in _KINDS:
        entries = []
        for c in candidates.get(kind, []):
            entry: dict[str, object] = {"id": c.id, "include": False, "sha256": c.content_hash}
            if c.note:
                entry["note"] = c.note
            if c.blocked:
                entry["blocked"] = c.blocked
            entries.append(entry)
        body[kind] = entries
    _destination._mkdir_guarded(path, what="the plan")
    # newline="" here is uniformity, not correctness: the plan is written before the
    # digest is taken and is carried into the bundle afterwards, so bundle_digest never
    # covers it, and read_plan goes through json.loads, which does not care. It is pinned
    # anyway so that "every write_text in this module pins newline" is a rule with no
    # exceptions -- one a reader can apply from the call site without first working out
    # whether these particular bytes end up hashed. The call that DOES depend on it is
    # _write_guarded; see the note there.
    # Written through ``_write_nofollow`` rather than ``write_text``, which follows a link at
    # the destination. A dangling symlink at the plan path is the worst case: ``write_text``
    # CREATES the link's target, so a plan written to a path an earlier run left linked
    # elsewhere lands wherever it points, with this build's own file mode.
    #
    # ``newline=""`` comes with that writer, and the rule it belongs to is unchanged: every
    # text write in this module pins newline, so a reader can apply it from the call site
    # without first working out whether these particular bytes end up hashed. They do not --
    # the digest is taken before the carried plan is written in -- and the call that DOES
    # depend on it is _write_guarded; see the note there.
    return _destination._write_nofollow(
        path, json.dumps(body, indent=2, ensure_ascii=False) + "\n", exclusive=True, exists_ok=True
    )


def _require_plan_include(kind: str, cid: str, raw: object) -> bool:
    """A plan entry's ``include`` must be a real JSON boolean.

    ``bool("false")`` is ``True``, so a plan that says ``"include": "false"`` --
    a string, the shape a hand-edited or template-rendered plan easily produces --
    would SELECT the item and ship it in a published bundle, defeating the
    deny-by-default seam this producer exists to enforce. Coercing silently is the
    wrong direction here twice over: it is the OVER-sharing direction the module
    warns against, and it hides that the reviewer's plan does not say what they
    meant. So require a genuine boolean and refuse anything else, in the voice of
    the other ``ExportRefused`` guards. Absent defaults to ``False`` (excluded),
    which is the deny-by-default posture.
    """
    if isinstance(raw, bool):
        return raw
    raise ExportRefused(
        f"curation plan entry {cid!r} in section {kind!r} has a non-boolean "
        f"'include': {raw!r}. It is not coerced because the string \"false\" is "
        f"truthy, so a coercion would SELECT an item the reviewer meant to "
        f"exclude and ship it in the bundle. Write true or false, not a string."
    )


def read_plan(path: Path) -> Plan:
    _pinned._refuse_without_nofollow_primitive()
    # The ``--allow`` path is an operator-typed CLI argument, so it can name a UNC share, a
    # sensitive location, or a redirect just like ``--source`` can. It goes through the same
    # three gates the agent-spec read uses, in the same order, so a check-then-read window and
    # an unfenced read cannot let a redirected or sensitive plan path through. UNC first
    # on Windows, before any stat: resolving a UNC path IS the outbound SMB probe and a Windows
    # SMB touch carries an NTLM exchange, so a name fence cannot help once the stat has gone out.
    if os.name == "nt":
        try:
            from kiro_crew.hooks import is_unc_shape, unc_probe_allowed
        except ImportError as exc:
            raise ExportRefused(
                f"cannot judge whether the curation plan path {path} names a UNC path, "
                f"because kiro_crew.hooks is not importable here ({exc}). Reading it could "
                f"reach a host over SMB before any check runs, so it is refused rather than "
                f"read unchecked. Pass --allow a local path."
            ) from exc
        _raw = str(path)
        if is_unc_shape(_raw) and not unc_probe_allowed(_raw):
            raise ExportRefused(
                f"the curation plan path {path} is a UNC path outside the trusted roots. "
                f"Reading it would reach that host over SMB before this build could check "
                f"anything about it. Pass --allow a local path."
            )
    try:
        from kiro_crew import security as _sec

        _fence: Callable[[str], bool] | None = _sec.is_sensitive_path
    except Exception:  # pragma: no cover - exercised by whichever branch the environment allows
        _fence = None
    _posix = path.as_posix()
    if (_fence is not None and _fence(_posix)) or _sensitive._looks_sensitive_standalone(_posix):
        raise ExportRefused(
            f"the curation plan path {path} is inside a credential/sensitive location. "
            f"Refusing to read it. Pass --allow a plan written by the plan command."
        )
    # ``_read_text_openat`` walks the path one component at a time from the filesystem root,
    # opening each with ``O_NOFOLLOW | O_DIRECTORY`` via ``dir_fd``, so a redirect at ANY
    # component fails its own open -- not only the final one. ``_read_text_nofollow`` guards
    # ONLY the last component, so an intermediate directory swapped for a symlink (``--allow
    # /tmp/alias/auth.json`` with ``alias -> ~/.codex``) is followed into a credential file
    # before the leaf open runs, and the literal-component standalone fence above cannot catch
    # it because the resolved location is not spelled in the path. The spec read anchors every
    # component this same way; the plan read must match it. ``path.absolute()`` makes a relative
    # ``--allow`` absolute WITHOUT resolving links (unlike ``resolve()``), so the walk starts at
    # the real root and every component -- including the redirecting one -- is opened no-follow.
    # ``None`` covers a missing file, a link at any component, a special file, or a non-UTF-8
    # body; the two branches keep "no plan" distinct from "unreadable".
    abs_path = path if path.is_absolute() else path.absolute()
    text = _pinned._read_text_openat(
        Path(abs_path.anchor), abs_path.relative_to(abs_path.anchor), refuse_hard_link=True
    )
    if text is None:
        try:
            present = os.lstat(path)
        except OSError:
            present = None
        if present is None:
            raise ExportRefused(f"no curation plan at {path}. Run the plan command first.")
        raise ExportRefused(
            f"the curation plan at {path} could not be read as UTF-8 (it may be a link, a "
            f"special file, or not decodable); refusing rather than following it."
        )
    try:
        raw = json.loads(text)
    except (ValueError, OSError) as exc:
        # ``ValueError`` rather than ``json.JSONDecodeError``, because the read happens
        # before the parse and can fail on its own terms: a plan file that is not valid
        # UTF-8 raises ``UnicodeDecodeError``, which is a ``ValueError`` and neither a
        # ``JSONDecodeError`` nor an ``OSError``. It therefore escaped this handler and left
        # ``main`` printing a traceback where this module's contract is to refuse cleanly.
        # ``JSONDecodeError`` is itself a ``ValueError``, so the wider tuple still covers
        # what the narrower one did.
        raise ExportRefused(f"curation plan {path} is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ExportRefused(f"curation plan {path} is not an object")
    if raw.get("plan_version") != PLAN_VERSION:
        raise ExportRefused(
            f"curation plan version {raw.get('plan_version')!r} is not {PLAN_VERSION}; "
            f"regenerate it"
        )
    selections: dict[str, dict[str, bool]] = {}
    pins: dict[str, dict[str, str]] = {}
    for kind in _KINDS:
        entries = raw.get(kind) or []
        if not isinstance(entries, list):
            raise ExportRefused(f"curation plan section {kind!r} is not a list")
        sel: dict[str, bool] = {}
        pin: dict[str, str] = {}
        for entry in entries:
            if not isinstance(entry, dict) or "id" not in entry:
                raise ExportRefused(f"malformed entry in {kind!r}: {entry!r}")
            raw_id = entry["id"]
            if not isinstance(raw_id, str):
                raise ExportRefused(
                    f"entry id in {kind!r} is {type(raw_id).__name__} ({raw_id!r}), not a "
                    f"string; it names what the plan selects and cannot be coerced. Fix the "
                    f"plan."
                )
            cid = raw_id
            sel[cid] = _require_plan_include(kind, cid, entry.get("include", False))
            # A non-string ``sha256`` is REFUSED, not ``str()``-coerced: the pin decides whether
            # a skill's bytes match what was reviewed, so a fabricated pin is a fabricated
            # integrity claim in a signed plan. Absent (None/missing) is legitimate -- it means
            # no pin -- and stays the empty string.
            raw_sha = entry.get("sha256")
            if raw_sha is not None and not isinstance(raw_sha, str):
                raise ExportRefused(
                    f"'sha256' for {cid!r} in {kind!r} is {type(raw_sha).__name__} "
                    f"({raw_sha!r}), not a string; a content pin cannot be coerced. Fix the plan."
                )
            pin[cid] = raw_sha or ""
        selections[kind] = sel
        pins[kind] = pin
    # The plan's identity/provenance fields feed the signed-plan guard, so a non-string is
    # REFUSED rather than ``str()``-coerced, the same rule the ``sha256`` pin above states: a
    # coerced ``reviewed_by`` or ``reviewed_at`` fabricates provenance the signature is taken
    # over, and a coerced ``crew`` fabricates which crew the plan claims to be for. Absent
    # (None/missing) stays the empty string, which is a legitimate "unsigned/unstated" plan.
    for _field in ("crew", "reviewed_by", "reviewed_at"):
        _val = raw.get(_field)
        if _val is not None and not isinstance(_val, str):
            raise ExportRefused(
                f"plan field {_field!r} is {type(_val).__name__} ({_val!r}), not a string; "
                f"it is provenance the signed-plan guard reads and cannot be coerced. Fix "
                f"the plan."
            )
    return Plan(
        crew=str(raw.get("crew") or ""),
        reviewed_by=str(raw.get("reviewed_by") or ""),
        reviewed_at=str(raw.get("reviewed_at") or ""),
        selections=selections,
        pins=pins,
    )


def verify(plan: Plan, crew: str, candidates: dict[str, list[Candidate]]) -> Drift:
    """Refuse unless signed and every selected item is byte-for-byte as reviewed.

    Ported from ``crew_export/plan.py:verify``. Drift outside the selection is
    reported, never refused on: a file the operator did not choose cannot reach
    the bundle, so blocking on it is a false alarm.
    """
    if plan.crew != crew:
        raise ExportRefused(f"plan was written for crew {plan.crew!r}, not {crew!r}")
    if not plan.is_signed():
        raise ExportRefused(
            "curation plan is unreviewed: reviewed_by and reviewed_at are blank. "
            "Read the plan, choose what customers may reach, sign it, then build. "
            "There is deliberately no flag to skip this."
        )
    by_kind = {kind: {c.id: c for c in candidates.get(kind, [])} for kind in _KINDS}
    drift = Drift()
    for kind in _KINDS:
        live = set(by_kind[kind])
        planned = set(plan.selections.get(kind, {}))
        drift.appeared += len(live - planned)
        drift.vanished += len(planned - live)
        for cid in plan.included(kind):
            candidate = by_kind[kind].get(cid)
            if candidate is None:
                raise ExportRefused(f"plan selects {kind}/{cid!r}, which does not exist")
            if candidate.blocked:
                raise ExportRefused(
                    f"plan selects {kind}/{cid!r}, which cannot be included: {candidate.blocked}"
                )
            pinned = plan.pins.get(kind, {}).get(cid, "")
            if not pinned:
                raise ExportRefused(
                    f"plan selects {kind}/{cid!r} with no recorded content hash, so "
                    f"what was approved cannot be established. Re-run the plan."
                )
            if pinned != candidate.content_hash:
                raise ExportRefused(
                    f"{kind}/{cid} changed after it was approved, so the approval no "
                    f"longer covers it.\n  reviewed: {pinned}\n  current:  "
                    f"{candidate.content_hash}\nRe-run the plan command and look again."
                )
    return drift


def merge_plans(paths: list[Path], crew: str) -> Plan | None:
    """Union the selections of one or more signed review files.

    Each file must match the crew and, if it selects anything, be signed;
    otherwise its selections are refused rather than silently ignored. Returns
    ``None`` when no ``--allow`` was given (pure deny-by-default: an empty
    bundle).
    """
    if not paths:
        return None
    merged_sel: dict[str, dict[str, bool]] = {k: {} for k in _KINDS}
    merged_pins: dict[str, dict[str, str]] = {k: {} for k in _KINDS}
    reviewers: list[str] = []
    reviewed_ats: list[str] = []
    for p in paths:
        plan = read_plan(p)
        if plan.crew != crew:
            raise ExportRefused(f"--allow {p} was written for crew {plan.crew!r}, not {crew!r}")
        if plan.selects_anything() and not plan.is_signed():
            raise ExportRefused(
                f"--allow {p} selects items but is unreviewed (reviewed_by / "
                f"reviewed_at are blank). Sign it or its selections are refused."
            )
        if plan.is_signed():
            reviewers.append(plan.reviewed_by)
            reviewed_ats.append(plan.reviewed_at)
        for kind in _KINDS:
            for cid, on in plan.selections.get(kind, {}).items():
                merged_sel[kind][cid] = merged_sel[kind].get(cid, False) or on
                pin = plan.pins.get(kind, {}).get(cid, "")
                if not pin:
                    continue
                # A pin is only meaningful from a plan that SELECTS the item. The
                # signature check above lets a plan selecting nothing through
                # unsigned, which is correct on its own terms, but the old merge
                # took that plan's pins anyway and the last writer won. So an
                # unsigned plan that selected nothing could replace the content
                # hash a SIGNED plan was reviewed against, and verification would
                # then accept content no reviewer ever saw. Selection is what an
                # approval is about, so it is also what licenses a pin.
                if not on:
                    continue
                prev = merged_pins[kind].get(cid)
                if prev is not None and prev != pin:
                    # Two selecting plans disagreeing about the content is not
                    # something to resolve by ordering. Whichever we picked, one
                    # reviewer approved something else.
                    raise ExportRefused(
                        f"two --allow plans select {kind} {cid!r} but pin different "
                        f"content ({prev} and {pin}). One of the two reviewers "
                        f"approved content this build would not ship, so neither "
                        f"pin is used. Re-review against a single revision."
                    )
                merged_pins[kind][cid] = pin
    return Plan(
        crew=crew,
        reviewed_by="; ".join(sorted(set(r for r in reviewers if r))),
        reviewed_at="; ".join(sorted(set(a for a in reviewed_ats if a))),
        selections=merged_sel,
        pins=merged_pins,
    )


def _denied_list(candidates: dict[str, list[Candidate]], plan: Plan | None) -> list[dict]:
    """What did not ship and why, so the owner can see it (SMC_BUNDLE_JSON.denied)."""
    out: list[dict] = []
    for kind in _KINDS:
        included = plan.included(kind) if plan else set()
        for c in candidates.get(kind, []):
            if c.id in included:
                continue
            if c.blocked:
                reason = c.blocked
            elif plan is None:
                reason = "no curation plan supplied (deny-by-default)"
            else:
                reason = "not marked reviewed in the plan (deny-by-default)"
            out.append({"kind": kind, "id": c.id, "reason": reason})
    return out


def _decision_set(candidates: dict[str, list[Candidate]], plan: Plan | None) -> dict:
    included = {kind: sorted(plan.included(kind)) if plan else [] for kind in _KINDS}
    return {"included": included, "denied": _denied_list(candidates, plan)}
