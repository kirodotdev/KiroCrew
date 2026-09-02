"""Pure validation and projection helpers for Spec Builder."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
from pathlib import Path
from typing import Any

try:
    from kiro_crew.security import (
        is_sensitive_path,
        redact_and_truncate,
        redact_credentials,
        redact_exfiltration_urls,
    )

    _HAS_SECURITY = True
except Exception:  # pragma: no cover - security module always present in prod
    _HAS_SECURITY = False

    def is_sensitive_path(path: str) -> bool:  # type: ignore[misc]
        """Fail closed when path sensitivity cannot be determined."""
        return True


logger = logging.getLogger("kirocrew.app.spec-builder")
_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")
_VALID_TYPES = ("feature", "bug", "quick")

#: Every status this app can be in. index.json is agent-writable, so the stored
#: value is untrusted: an unrecognised one is reported as "planning" rather than
#: echoed, which both closes a credential-egress path and is the truth (a spec with
#: no live loop IS planning). Allowlisting beats redacting here because the set is
#: small and closed, so there is nothing to sanitise -- only to recognise.
_VALID_STATUSES = ("planning", "executing")


def _known_status(value: object) -> str:
    """The stored status if this app recognises it, else "planning"."""
    text = str(value or "")
    return text if text in _VALID_STATUSES else "planning"


# ── redaction ──────────────────────────────────────────────────────────────


#: Served in place of any text this app cannot scrub. Everything that flows
#: through _redact is agent- or user-authored (spec documents, transcripts,
#: agent-written state), so it can contain credentials by construction.
_UNSCRUBBABLE = "[unavailable: redaction is not available]"


def _redact(text: str) -> str:
    """Scrub credentials + exfiltration URLs from agent/user text before it
    leaves this backend (transcript, file contents, spec metadata).

    Fails CLOSED. If the security module could not be imported there is no way
    to scrub, and every caller feeds this untrusted content on its way to the
    browser -- so withhold the text rather than serving it raw. The same
    reasoning as the fail-closed ``is_sensitive_path`` fallback above: when the
    judgement cannot be made, refuse instead of waving it through.
    """
    if not isinstance(text, str) or not text:
        return text or ""
    if not _HAS_SECURITY:
        return _UNSCRUBBABLE
    text, _ = redact_exfiltration_urls(text)
    text, _ = redact_credentials(text)
    return text


def _redact_and_truncate(text: str, max_chars: int) -> str:
    """Scrub like ``_redact``, then truncate — never ``_redact(x[:n])``.

    Truncating first can cut a credential at the boundary, leaving a fragment
    the redaction regexes no longer match, so the raw remainder would leak.
    Fails CLOSED exactly like ``_redact``: with no security module there is no
    way to scrub, so withhold the text rather than serving a bounded raw slice.
    """
    if not isinstance(text, str) or not text:
        return text or ""
    if not _HAS_SECURITY:
        return _UNSCRUBBABLE
    return redact_and_truncate(text, max_chars)


def _usable_name(name: str) -> bool:
    """True when this index KEY can be served as a spec name.

    Two reasons an entry is dropped rather than repaired. The key must satisfy the
    same grammar `create` enforces, because it becomes a slot key and a session
    filename downstream. And it must survive `_redact` unchanged: index.json is
    agent-writable, so a credential can be parked in the KEY, and `GET /specs`
    returns the key as `"name"`. Scrubbing it would produce a name that no longer
    matches the directory the entry points at, so the entry goes instead.
    """
    return _valid_name(name) and _redact(name) == name


def _entry_is_usable(meta: dict) -> bool:
    """True when an index entry carries the one field handlers dereference.

    ``spec_dir`` only. Handlers index it directly (``meta["spec_dir"]``), which is
    what turned a shapeless entry into a 500. ``working_dir`` is deliberately NOT
    required here: it is re-validated through ``_safe_dir`` at the slot chokepoint,
    which refuses a missing one outright rather than running the spec unscoped. So
    an entry without it still lists and reads -- it just cannot be given a worker.
    """
    spec_dir = meta.get("spec_dir")
    return isinstance(spec_dir, str) and bool(spec_dir.strip()) and "\x00" not in spec_dir


#: A slot key is a history-file identity: it becomes a session filename and flows
#: into core's session-key parsing, so a persisted one is validated before use.
_SLOT_KEY_RE = re.compile(r"^spec-builder-[A-Za-z0-9_-]{1,96}$")

#: A per-creation suffix: eight lowercase hex, as minted by _new_slot_key.
_SLOT_SUFFIX_RE = re.compile(r"^[0-9a-f]{8}$")


def _owns_slot_key(name: str, key: str) -> bool:
    """True when *key* is a slot key THIS spec may claim.

    The grammar alone was not enough. index.json is agent-writable, so an entry
    could carry another spec's perfectly valid key -- and `_ensure_worker_slot`
    would then adopt that spec's live session, delivering this spec's messages and
    approval cards into the other conversation. Ownership is therefore structural:
    the key must encode the indexed name, either as the per-creation
    ``spec-builder-<name>-<8hex>`` or the legacy name-derived
    ``spec-builder-<name>`` (kept so specs created before per-creation keys keep
    the transcript they already have).
    """
    if not _valid_name(name) or not _SLOT_KEY_RE.match(key):
        return False
    legacy = f"spec-builder-{name}"
    if key == legacy:
        return True
    prefix = legacy + "-"
    return key.startswith(prefix) and bool(_SLOT_SUFFIX_RE.match(key[len(prefix) :]))


_PHASE_FILES = [("tasks", "tasks.md"), ("design", "design.md"), ("requirements", "requirements.md")]

#: ONE task line in ``tasks.md``: a bullet or ordered marker, then a checkbox,
#: then the task text. Group 1 is the box body (empty/blank = open, ``x``/``X`` =
#: done) and group 2 is the text. Accepts the ``-``/``*``/``+`` and ``1.``/``1)``
#: markers Markdown allows, and a bare ``[]`` alongside ``[ ]``, because the list
#: is model-written and its marker style varies between runs.
#:
#: Deliberately the ONLY task-line pattern in this module. The handoff gate needs
#: "is there an open task", the detail endpoint needs the enumerated list, and the
#: per-task endpoint needs to address one of them; expressing those as separate
#: regexes would let the gate and the list disagree about what a task even is,
#: and the per-task run would then target a line the gate never counted.
_TASK_LINE_RE = re.compile(
    r"^[ \t]*(?:[-*+]|\d+[.)])[ \t]+\[([ \t]?|[xX])\][ \t]*(.*)$", re.MULTILINE
)

#: Documents the user may edit through the app. The spec directory also holds
#: ``.spec-state.json`` (agent-authored) and the STOP sentinel (a control), and
#: neither is a document a person should be able to PUT arbitrary text into.
_EDITABLE_DOCS = frozenset(f for _phase, f in _PHASE_FILES)

#: Phases whose approval the app records. Matches ``ADVANCE`` in the SPA: there is
#: no "approve tasks" step, because approving the task list IS the handoff.
_APPROVABLE_PHASES = ("requirements", "design")

#: Cap on tasks enumerated for one spec. A model-written list is normally tens of
#: lines; the bound stops a pathological file from inflating every detail poll.
_MAX_TASKS = 300


def _sha256_text(text: str) -> str:
    """Content hash used as an edit/approval fingerprint.

    Hex-encoded SHA-256 of the UTF-8 bytes. Two uses, both about a document
    changing under someone: an editor sends back the hash it loaded so a save
    that would overwrite an agent's newer write is refused, and an approval
    records the hash it approved so the UI can say the document has moved since.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _parse_tasks(text: str) -> list[dict]:
    """Enumerate ``tasks.md``'s checklist as addressable tasks.

    Each task carries its ``index`` (position among task lines, which is what the
    UI renders and what the run endpoint addresses) and a ``hash`` of its text.
    BOTH are required to act on one: an index alone is a moving target because the
    agent rewrites this file between polls, so a click on "task 3" could dispatch
    whatever ended up third. The hash pins the identity the user actually saw, and
    a mismatch is refused rather than guessed at.

    The hash is derived from the RAW task body while only ``text`` is redacted for
    egress. Hashing the redacted rendering would collapse different credentials to
    the same identity, allowing an agent edit hidden by redaction to survive the
    stale-click check.

    ``tasks.md`` stays the source of truth -- there is no sidecar task store. That
    file is the interop contract with the Kiro IDE and CLI, which read and write
    the same three documents, so a spec built here has to remain a spec they can
    open. Progress is therefore DERIVED by re-parsing checkboxes rather than
    tracked separately, and an agent (or a person) checking a box by hand shows up
    without anything having to be told.
    """
    tasks: list[dict] = []
    for match in _TASK_LINE_RE.finditer(text or ""):
        body = (match.group(2) or "").strip()
        if not body:
            # A checkbox with no text is not something a user can be asked to run.
            continue
        tasks.append(
            {
                "index": len(tasks),
                "text": _redact(body)[:_MAX_FIELD],
                "done": match.group(1).strip().lower() == "x",
                "hash": _sha256_text(body),
            }
        )
        if len(tasks) >= _MAX_TASKS:
            break
    return tasks


def _has_open_task(text: str) -> bool:
    """True when ``tasks.md`` holds at least one UNCHECKED task.

    The predicate behind the handoff gate. Existence is not a plan: the prompt the
    gate arms tells the agent to work through each unchecked task in order, so a
    zero-byte, prose-only or fully-checked file gave the autonomous loop nothing to
    act on while still reading as a finished Tasks phase.
    """
    return any(not t["done"] for t in _parse_tasks(text))


def _numeric(value: object) -> float:
    """An index timestamp as a JSON-representable float, or 0.0.

    index.json is agent-writable, so a timestamp is untrusted input like every other
    field: returning it verbatim let a credential parked in `created_at` reach the
    dashboard, and mixing types broke the list sort. One coercion serves both.

    NaN and the infinities have to go the same way as a non-number. `float()` accepts
    them, and `json.dumps` then writes them as bare `NaN` / `Infinity`, which is not
    JSON -- `JSON.parse` throws on the whole document, so one poisoned timestamp
    takes out the entire spec list rather than the one spec that carries it.
    """
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) else 0.0


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _normalize_approvals(raw: Any, docs: dict) -> dict:
    """Project the stored approval record onto its schema and mark what has moved.

    Returns ``{phase: {"hash", "at", "user", "stale"}}`` for the phases in
    ``_APPROVABLE_PHASES`` only.

    ``stale`` is DERIVED here, never stored: it compares the hash that was approved
    against the document's hash right now, so a document the agent rewrote after
    sign-off reports itself as changed instead of continuing to look approved. A
    phase whose document has since disappeared is also stale -- there is nothing
    left that the approval describes.

    Normalized on read because this record lives in the app's index, and the index
    is reachable by the agent (it runs shell commands as the user), exactly like
    every other index field this module scrubs on the way out. Which is also the
    honest limit of what this is: a record of a human review, not an attestation
    that cannot be forged. It earns its place against the previous behaviour --
    where approval was a chat message and left no trace at all -- not against a
    threat model where the agent is hostile.
    """
    out: dict[str, dict] = {}
    if not isinstance(raw, dict):
        return out
    for phase in _APPROVABLE_PHASES:
        entry = raw.get(phase)
        if not isinstance(entry, dict):
            continue
        approved_hash = str(entry.get("hash", ""))
        if not _SHA256_RE.match(approved_hash):
            continue
        current = str((docs.get(phase + ".md") or {}).get("hash", ""))
        out[phase] = {
            "hash": approved_hash,
            "at": _numeric(entry.get("at")),
            "user": _clean_str(entry.get("user")),
            "stale": current != approved_hash,
        }
    return out


# Bounds for the agent-authored state file. It is LLM output, so every field is
# treated as hostile: unknown keys dropped, types enforced, lists capped.
_MAX_DECISIONS = 50
_MAX_OPTIONS = 20
_MAX_FIELD = 2000
_DECISION_PROMPT_PREFIX = "Decision - "
_DECISION_PROMPT_SEPARATOR = ": "
_MAX_DECISION_PROMPT = (
    len(_DECISION_PROMPT_PREFIX) + _MAX_FIELD + len(_DECISION_PROMPT_SEPARATOR) + _MAX_FIELD
)


def _clean_str(v: Any) -> str:
    """Redact and length-cap a value that must be a string. Non-strings -> ''."""
    return _redact(v)[:_MAX_FIELD] if isinstance(v, str) else ""


def _decision_answer_prompt(decision: dict[str, Any], option: str) -> str:
    """Build the bounded agent prompt from fields validated by this backend.

    The bound includes both independently capped fields. Truncating their composed
    sentence to ``_MAX_FIELD`` can remove the option when a title fills that budget,
    leaving crash replay to deliver a prompt that does not contain the immutable answer.
    """
    title = _clean_str(decision.get("title"))
    selected = _clean_str(option)
    separator = _DECISION_PROMPT_SEPARATOR if title else ""
    return f"{_DECISION_PROMPT_PREFIX}{title}{separator}{selected}"


def _decision_fingerprint(decision: dict[str, Any]) -> str:
    """Stable identity for the rendered question, independent of its reused id.

    Recommended is presentation guidance rather than question identity. The fields that
    define what is being asked are the normalized id, title and set of offered options;
    reordering those choices is presentation-only and cannot reopen a settled question.
    """
    payload = json.dumps(
        {
            "id": str(decision.get("id", "")),
            "title": str(decision.get("title", "")),
            "options": sorted(str(option) for option in decision.get("options") or []),
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _normalize_spec_state(raw: Any) -> dict | None:
    """Project agent-authored ``.spec-state.json`` onto the documented schema.

    Returns ``None`` unless the payload is a dict. Every value is redacted and
    capped, and **keys are redacted too** — a credential placed in an object
    *key* would otherwise be served verbatim, since the previous recursive
    scrub only walked values. Malformed entries (e.g. ``decisions: [null]``,
    which crashed SpecStatePanel) are dropped rather than forwarded.
    """
    if not isinstance(raw, dict):
        return None
    out: dict[str, Any] = {}

    decisions: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for item in (
        (raw.get("decisions") or [])[:_MAX_DECISIONS]
        if isinstance(raw.get("decisions"), list)
        else []
    ):
        if not isinstance(item, dict):
            continue
        did = _clean_str(item.get("id")) or _clean_str(item.get("title"))
        title = _clean_str(item.get("title"))
        if not did or not title:
            continue
        # The id IS the identity: the ledger is keyed on it and the overlay matches on
        # it, so two entries claiming one id would both settle when either is answered
        # -- and the second card would display an answer chosen for the first. A
        # duplicate is malformed agent output; the FIRST occurrence wins.
        if did in seen_ids:
            continue
        seen_ids.add(did)
        opts_raw = item.get("options")
        options = [
            _clean_str(o)
            for o in (opts_raw[:_MAX_OPTIONS] if isinstance(opts_raw, list) else [])
            if isinstance(o, str)
        ]
        decisions.append(
            {
                "id": did,
                "title": title,
                "options": [o for o in options if o],
                "recommended": _clean_str(item.get("recommended")),
                "answer": _clean_str(item.get("answer")),
                # Overlaid from this backend's own ledger; the agent does not get
                # to declare a decision re-openable. See _apply_recorded_answers.
                "locked": False,
            }
        )
    out["decisions"] = decisions
    out["blocking"] = _clean_str(raw.get("blocking"))
    ctx = raw.get("context")
    out["context"] = {"template": _clean_str(ctx.get("template")) if isinstance(ctx, dict) else ""}
    return out


def _same_spec_dir(left: str, right: str) -> bool:
    """Whether two persisted paths currently name the same directory.

    BLOCKING -- callers run on a worker thread; index mutations also hold ``_INDEX_LOCK``.
    The lexical fast path handles a directory that disappeared during delete;
    ``samefile`` supplies the filesystem's own case and alias semantics while both paths
    exist.
    """
    if _decision_key(left) == _decision_key(right):
        return True
    try:
        return os.path.samefile(left, right)
    except (OSError, ValueError):
        return False


def _decision_key(spec_dir: str) -> str:
    """The ledger key for a spec: its directory, normalized LEXICALLY.

    The DIRECTORY is the identity, not the name. Both live in the index, but only the
    name is a label the agent can mint more of -- adding a second entry pointing at the
    same files gave the alias its own (empty) record, so its cards rendered answerable
    and a click dispatched a conflicting answer over the same documents. Keying on the
    directory collapses every name for one spec onto one record.

    PURE: no ``resolve()``, no ``realpath``, no filesystem access of any kind. It used
    to resolve, so that a symlinked SPELLING of one directory could not mint a second
    record. That defence was real but it bought a worse hole, because the spec directory
    belongs to the agent: swap the directory for a symlink and ``resolve()`` returns a
    DIFFERENT key while the index identity still matches, so the settled record went
    missing, the card re-opened, and a conflicting answer could be dispatched. A key
    derived from mutable filesystem state is a key the agent can move.

    Lexical normalization keeps both properties instead of trading one for the other:
    - the key cannot move, because nothing outside this string decides it, so a
      directory swap leaves a settled decision settled;
    - the alias-by-spelling hole stays closed at the WRITE end instead, where
      ``_claim_decision_locked`` refuses a spec_dir that does not verify as itself
      (``_verified_spec_dir``). An alias spelled through a symlink cannot record an
      answer at all, so it cannot reverse one.

    ``normcase`` as well as ``normpath`` because on Windows the same directory can be
    spelled with different case or separators without being a different directory.

    The read side deliberately does NOT refuse an unverifiable directory: a read that
    returns "no record" UNLOCKS a card, which is the reversal direction. Reads answer
    from the lexical key and stay locked; only the write side refuses.
    """
    return os.path.normcase(os.path.normpath(spec_dir))


def _valid_name(name: str) -> bool:
    return bool(_NAME_RE.match(name))


# ── seed / execution prompts ─────────────────────────────────────────────────


#: Per-type deliverables. ``quick`` deliberately skips design.md.
_TYPE_PLAN: dict[str, tuple[str, ...]] = {
    "feature": ("requirements.md", "design.md", "tasks.md"),
    "bug": ("requirements.md", "design.md", "tasks.md"),
    "quick": ("requirements.md", "tasks.md"),
}

_TYPE_GUIDANCE: dict[str, str] = {
    "feature": (
        "FEATURE spec: full Requirements -> Design -> Tasks. requirements.md states "
        "user-visible behaviour with acceptance criteria; design.md states the technical "
        "approach; tasks.md is an ordered, checkable task list."
    ),
    "bug": (
        "BUG spec: requirements.md is the investigation -- symptoms, reproduction, root "
        "cause, expected behaviour. design.md is the fix approach. tasks.md is the "
        "ordered fix plus the regression test that would have caught it."
    ),
    "quick": (
        "QUICK spec: keep it light. requirements.md is a short goal plus acceptance "
        "bullets, then tasks.md is the ordered task list. Do NOT write design.md unless "
        "the user asks for it."
    ),
}


def _seed_prompt(
    spec_type: str, name: str, spec_dir: Path, working_dir: str, description: str
) -> str:
    """The opening turn for a new spec.

    SELF-CONTAINED by necessity: builtin apps do not pass through
    ``bridges.register_app``, so the manifest's ``spec-workflow`` skill is not on
    the agent's skill path. The prompt therefore carries the workflow and the
    selected type's deliverables itself.
    """
    desc = (
        f"\n\nThe user's initial description:\n{description.strip()}" if description.strip() else ""
    )
    files = _TYPE_PLAN.get(spec_type, _TYPE_PLAN["feature"])
    guidance = _TYPE_GUIDANCE.get(spec_type, _TYPE_GUIDANCE["feature"])
    paths = "\n".join(f"  - {spec_dir / f}" for f in files)
    return (
        f"You are the Kiro Spec agent for spec **{name}** (type: **{spec_type}**).\n\n"
        f"{guidance}\n\n"
        f"Write ONLY to these EXACT absolute paths (never invent another location):\n"
        f"{paths}\n"
        f"WORKING_DIR (the codebase this spec is for): {working_dir}\n\n"
        f"How to work:\n"
        f"- ONE phase at a time. After writing a file, STOP and ask the user to review; do "
        f"not start the next phase until they approve.\n"
        f"- Ask focused clarifying questions in chat (1-3 at a time, with your recommended "
        f"answer) only when the answer would materially change the output. Never ask what "
        f"you can find by reading {working_dir} yourself.\n"
        f"- Keep every document self-contained and concrete: no placeholders, no TODOs.\n\n"
        f"Also maintain {spec_dir / '.spec-state.json'} -- the app renders it as UI, so it "
        f"is plumbing: never mention it in chat and never list it as a deliverable. Shape:\n"
        f'  {{"decisions": [{{"id": "<stable-id>", "title": "<question>", '
        f'"options": ["A", "B"], "recommended": "A", "answer": null}}], '
        f'"blocking": "<one sentence: what you are waiting on, or null>", '
        f'"context": {{"template": "<the module you are modelling this on>"}}}}\n'
        f"Update it every time you ask a decision, receive an answer, or change phase; set "
        f"a decision's `answer` when the user picks one and keep the entry.\n\n"
        f"Begin with {files[0]}: draft it, then STOP and ask the user to review before "
        f"moving on.{desc}"
    )


def _task_prompt(
    name: str, spec_dir: Path, working_dir: str, task_text: str, task_index: int
) -> str:
    """Instruction for running ONE task from the list.

    Deliberately scoped and deliberately NOT an autonudge loop: the whole-list
    handoff arms a loop that keeps going, while this dispatches a single turn and
    stops. Running one task is how a user takes a plan for a walk without handing
    over the whole thing, so it must end where the user expects it to.

    Names the task by both its text and its validated checklist occurrence. Text
    alone is ambiguous when a plan repeats a label, while the occurrence alone is
    hard for the model to recognize. The handler revalidates both against the
    latest tasks.md snapshot immediately before dispatch.
    """
    return (
        f"SINGLE TASK from spec '{name}'. Work ONLY on this one task from "
        f"{spec_dir / 'tasks.md'}, operating inside {working_dir} (your shell already "
        f"starts there — no cd needed). This is checklist item {task_index + 1}, "
        f"counting non-empty checklist items from top to bottom:\n\n{task_text}\n\n"
        f"Mark its checkbox [x] in tasks.md when it is genuinely done, run the "
        f"relevant build/tests to verify, then STOP and summarize. Do NOT continue "
        f"to the following tasks — I am running these one at a time."
    )


def _duplicate_prompt(name: str, source: str, spec_dir: Path) -> str:
    """Orientation for a duplicated spec's fresh conversation.

    A duplicate copies the documents but NOT the transcript -- the new spec gets
    its own slot key, so it cannot inherit the original's history. Without a first
    turn the agent would come to the conversation knowing nothing about documents
    that are already on disk, so this tells it what it is looking at and, notably,
    tells it not to start rewriting them.
    """
    return (
        f"Spec '{name}' is a copy of '{source}'. Its documents are already written "
        f"at {spec_dir} — read them before doing anything else. Do NOT rewrite or "
        f"regenerate them; wait for me to say what should change in this copy."
    )


def _opted_in(body: dict, field: str) -> bool:
    """True only when *field* is the JSON boolean ``true``.

    Truthy coercion would also accept strings such as ``"false"``. These flags
    create a worktree or adopt existing documents, so opt-in must be exact.
    """
    return body.get(field) is True
