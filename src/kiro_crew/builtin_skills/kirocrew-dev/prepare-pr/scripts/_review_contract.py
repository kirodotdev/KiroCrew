"""Shared reviewer-finding and disposition contract for prepare-pr scripts.

The prepare-pr skill is distributed as a directory, so its executable scripts can
share this stdlib-only module while remaining runnable by absolute path from any
working directory.
"""

from __future__ import annotations

import hashlib
import json
import re
import time

REVIEWED_STAMP_RE = re.compile(r"\[([A-Z][A-Z0-9_-]*)-REVIEWED\]\s+([0-9a-f]{7,40})\b")
BLOCK_MERGE_RE = re.compile(r"\[BLOCK-MERGE\]\s+([0-9a-f]{7,40})\b")
# The SANCTIONED downgrade, read from the one part of the comment a model cannot
# author. When adjudication clears a marker-writing lane's block, the workflow
# renders its heading from the PARSED decision the gate acts on --
# `codex-review.yml:1330` and `fork-gpt-review.yml:1296` both set
# verdict="... (all downgraded on adjudication)" -- and echoes it at
# `codex-review.yml:1364`, before any `<details>`. The rewritten
# `[BLOCK-MERGE-DOWNGRADED]` marker is NOT used for this, deliberately: that
# rewrite lands inside the body embedded from the model's own output file, so a
# review whose prose contains the marker would forge a clearance and suppress a
# real block. The workflow states this rule itself for a sibling marker at
# `codex-review.yml:1311-1314`: the refusal signal is the step's own output,
# "never a grep of the review body", because "a review merely QUOTING the refusal
# marker must not reclassify a completed verdict".
_DOWNGRADE_HEADING_RE = re.compile(r"^##[^\n]*\(all downgraded on adjudication\)", re.MULTILINE)
_PREFIX_SHA_RE = re.compile(r"\b([0-9a-f]{7,40})\b")


def sha_matches(stamp_sha, head_sha):
    """True when a stamped SHA identifies the current head.

    Two spellings count, because the stamp is not machine-written. The review
    workflows ask the MODEL to end its prose with `[<NAME>-REVIEWED] <sha>`
    (see the prompt in .github/workflows/design-review.yml) and then read that
    line back, so the 40 hex characters pass through a transcription step:

    * A >=7-hex PREFIX of the head is the ordinary form. Short-SHA references
      and the full 40 both land here, and a stamp naming an OLDER commit fails,
      which is the freshness guard the marker exists for.
    * An ELIDED head is the transcription artifact: a stamp that drops a
      CONTIGUOUS MIDDLE span and splices the head's own prefix to its own
      suffix. In one case the Design lane wrote 25 characters
      (the head's first 14 followed by its last 11) and every consumer read the
      PR as BLOCKED while PR Readiness was green.

    The elided form is verified, not merely tolerated: the token must be
    SHORTER than the head (a full-length token that is not a prefix names a
    different commit, and stays rejected), it must split into a >=7-hex prefix
    of the head plus a non-empty suffix of the head, and both halves must be
    the head's own. That keeps the guard the strict match was protecting -- a
    well-formed reference to another commit cannot pass, because it would have
    to begin with 7+ characters of THIS head and end with this head's tail --
    while a mangling of the current head does not fail closed.

    Lives here rather than once per entrypoint script: it arrived as a
    byte-identical pair pinned by a parity test, which is exactly the
    duplication this module exists to retire.
    """
    if not stamp_sha or not head_sha:
        return False
    if len(stamp_sha) >= 7 and head_sha.startswith(stamp_sha):
        return True
    if len(stamp_sha) >= len(head_sha):
        return False
    for cut in range(7, len(stamp_sha)):
        if head_sha.startswith(stamp_sha[:cut]) and head_sha.endswith(stamp_sha[cut:]):
            return True
    return False


# Bot type alone is spoofable. Marker authority comes from this allowlist and
# reviewer identity from the workflow-authored leading comment key below, never
# from a reviewer name that model-controlled body text happens to emit.
DEFAULT_MARKER_AUTHORS = ("github-actions[bot]",)
DEFAULT_MARKER_BINDINGS = (
    ("codex-ai-review", "GPT"),
    ("claude-ai-review", "OPUS"),
    ("design-review", "DESIGN"),
    ("ux-review", "UX"),
    ("first-principles-review", "FIRST-PRINCIPLES"),
)
_COMMENT_KEY_RE = re.compile(r"\A\s*<!--\s*([a-z0-9-]+)\s*-->")

# ---- Human override records ------------------------------------------------
# `ai-review-human-override.yml` records a repository writer's SHA-scoped
# decision to supersede an AI finding, as a bot-authored comment whose LEADING
# bytes are the marker below. The named lane then REPLACES its own keyed comment
# with a stampless "human override accepted" body, because the model was
# deliberately not re-run and no model verdict exists to stamp.
#
# So the two markers prove DIFFERENT things and neither substitutes for the
# other: `[<NAME>-REVIEWED] <sha>` is proof a MODEL produced a verdict for this
# commit, and this record is proof a HUMAN adjudicated it. A consumer that
# knows only the stamp reads an overridden head as an unreviewed one.
#
# Spelled to match the producer's `printf` byte for byte, which is also what
# every lane workflow selects on (`startswith("<!-- ai-review-human-override
# target=gpt head=$HEAD ")`, then an anchored read of `actor=`/`source=`).
# Every field is REQUIRED for the same reason it is there: a record missing
# attribution is not a record, and the lanes resolve such a comment to inactive
# rather than clearing on it. A future field added ahead of `actor=` stops
# matching here, which withholds the clearance -- the fail-closed direction.
OVERRIDE_MARKER_RE = re.compile(
    r"\A<!-- ai-review-human-override target=([a-z0-9-]+) head=([0-9a-fA-F]{7,40})"
    r" actor=(\S+) source=([0-9]+) -->"
)
OVERRIDE_TARGET_ALL = "all"
# The command's target spellings, mapped to each lane's WORKFLOW-AUTHORED
# comment key rather than straight to a reviewer name. Reviewer identity then
# still resolves through ``bindings`` -- the module's one source of truth for
# what a lane is called -- so a ``--marker-bindings`` override flows through and
# this table cannot drift into disagreeing with it. `fable` is the override
# spelling of the lane whose key is `claude-ai-review`, i.e. reviewer OPUS.
#
# One target the command accepts is deliberately ABSENT: `scope`, whose lane
# writes `<!-- security-scope-review -->` and has no entry in
# DEFAULT_MARKER_BINDINGS, so there is no reviewer for a row to resolve to and a
# row would clear no lane. The parity test derives this table from the lane
# workflows, so binding that lane fails a test until the row is added.
DEFAULT_OVERRIDE_TARGET_KEYS = (
    ("gpt", "codex-ai-review"),
    ("fable", "claude-ai-review"),
    ("design", "design-review"),
    ("ux", "ux-review"),
    ("first-principles", "first-principles-review"),
)
FINDING_RE = re.compile(
    r"^\s*(?:\*\*)?(BLOCKING|FINDING)(?:\*\*)?\s*(?:--|\u2014)\s*"
    r"(?:\*\*)?(\S+?):(\d+)(?:\*\*)?\s*(?:(?:--|\u2014)\s*)?(.*)$",
    re.MULTILINE,
)

DISPOSITION_PREFIX = "<!-- ai-review-disposition "
# The adjudication ledger selects by this leading byte prefix before parsing.
# A prefixed but malformed comment therefore retains downgrade power and must
# remain visible as a violation instead of being ignored.
DISPOSITION_MARKER_RE = re.compile(
    r"\A<!--\s*ai-review-disposition\s+target=([A-Za-z0-9_-]+)" r"\s+head=([0-9a-f]{7,40})\s*-->"
)
SPAN_CLAIM_RE = re.compile(r"\bspan=([0-9a-f]{12})\b")
# Span identity is deliberately coarse (path + reviewer/kind). Counting title
# bullets prevents two findings that share one span from sharing one rationale.
DISPOSITION_BULLET_RE = re.compile(r"^\s*[-*]\s*\*\*")
_MAX_COMMENT_PAGES = 50


def comment_key(body):
    """Return the workflow-authored leading comment key, if present."""
    match = _COMMENT_KEY_RE.match(body or "")
    return match.group(1) if match else ""


def parse_override_record(comment, authors=DEFAULT_MARKER_AUTHORS):
    """Return ``(target, head, actor)`` for a trusted override record, else None.

    Authority is the BOT AUTHORSHIP of the record, never the marker bytes.
    ai-review-human-override.yml reads the commenting human's collaborator
    permission and refuses to post unless it is write, maintain or admin -- and
    refuses equally when that read merely FAILS -- so a record existing under the
    workflow's own login already carries an authorization decision that was made
    before the bytes were written. The identical bytes from any other author are
    a forgery attempt and are ignored, which is the asymmetry the stamp
    allowlist already encodes: injection can deny a review, never forge one.

    The permission is deliberately NOT re-read here. It was checked at the
    moment of the decision, so re-checking would let a later access change
    rewrite a recorded historical judgment, and it would put a network call
    inside a pure parse.
    """
    user = comment.get("user") or {}
    if user.get("type") != "Bot":
        return None
    allowed = {a.lower() for a in authors or ()}
    if (user.get("login") or "").lower() not in allowed:
        return None
    match = OVERRIDE_MARKER_RE.match(comment.get("body") or "")
    if not match:
        return None
    target, head, actor, _source = match.groups()
    return target.lower(), head.lower(), actor


def override_reviewer_names(target, bindings, keys=DEFAULT_OVERRIDE_TARGET_KEYS):
    """Reviewer names one override target answers for, resolved via ``bindings``."""
    if target == OVERRIDE_TARGET_ALL:
        return {name for name in (bindings or {}).values() if name}
    key = dict(keys or ()).get(target)
    name = (bindings or {}).get(key) if key else ""
    return {name} if name else set()


def human_override_actors(comments, head_sha, bindings, authors=DEFAULT_MARKER_AUTHORS):
    """Return ``(named, blanket)`` -- the override actors valid for ``head_sha``.

    ``named`` maps reviewer name to actor for records naming ONE lane. Those
    ENROL their lane into the evaluation, because the record is independent
    proof that lane was answered for and has to keep standing on its own. A
    stamp is otherwise the only thing that puts a lane in the discovered set, so
    a lane whose only stamp sits in a DUPLICATE comment from an older head drops
    out of the evaluation the moment that comment is deleted, and the report
    reads clean having proved nothing. An enrolling record closes that exit.

    ``blanket`` is the actor of a ``target=all`` record, or "". It SATISFIES
    every lane already under evaluation but enrols none: in discovery mode a
    lane that never posted is not required, and inventing rows for it would
    claim a human adjudicated lanes that never ran.

    The head must match EXACTLY. ``sha_matches`` accepts >=7-hex prefixes and
    elided splices because a MODEL transcribes the stamp it was handed; this
    record is written by the workflow from ``.head.sha`` with no model anywhere
    in the path, so that tolerance would only widen what can satisfy the clause.
    """
    named: dict = {}
    blanket = ""
    head = (head_sha or "").lower()
    if not head:
        return named, blanket
    for comment in comments or []:
        parsed = parse_override_record(comment, authors)
        if not parsed:
            continue
        target, marked_head, actor = parsed
        if marked_head != head:
            continue
        if target == OVERRIDE_TARGET_ALL:
            blanket = actor
            continue
        for name in override_reviewer_names(target, bindings):
            named[name] = actor
    return named, blanket


def span_hash(path, rule_class):
    """Return a stable path-and-rule identity without reading the named path.

    Finding paths come from untrusted bot comments. Hashing their text avoids a
    file read of model-influenced input while keeping identities stable across
    rebases. The identity is deliberately path-scoped: two findings of one kind
    in the same file share an id, which errs toward earlier recurrence handling.
    """
    key = "{}|{}".format(path, rule_class)
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


# ---- Superseded verdicts ---------------------------------------------------
# A lane's marker comment is ONE slot, selected by the lane marker alone and
# never by the head, so publishing a verdict REPLACES whatever the slot held.
# Replacing a previous head's verdict is the lane doing its job. Replacing a
# verdict for the head under review is different: both bodies judged the same
# revision, the reader sees only the survivor, and nothing in the comment says a
# rival existed. Every body the slot ever held is kept by GraphQL
# ``userContentEdits`` -- in full, including the original publication -- so the
# record exists and wants reading rather than rebuilding.
#
# The two cases are told apart by ONE question, asked of each stored body that
# is not the current one: does it carry this lane's own stamp for the head under
# review? A body stamped for another head is an ordinary stale-head replacement
# and is not reported, because that is every normal publish. A body stamped for
# THIS head is a superseded sample and is.
_EDIT_PAGE_SIZE = 100
_MAX_EDIT_PAGES = 20
# The body field (`diff`) is the expensive half of this read, and the API throttles
# it specifically: the same query at the same page size succeeds without `diff` and
# is refused with it once a caller has spent its allowance. So ask the cheap
# question first -- HOW MANY bodies has this comment held? -- and pay for bodies
# only when the answer can matter. A comment holding at most one body has nothing
# that could have been superseded, which is the common case on any head whose lanes
# have each published once.
_EDIT_COUNT_QUERY = (
    "query($id:ID!){node(id:$id){... on IssueComment{" "userContentEdits(first:1){totalCount}}}}"
)
_EDIT_HISTORY_QUERY = (
    "query($id:ID!,$n:Int!,$c:String){node(id:$id){... on IssueComment{"
    "userContentEdits(first:$n,after:$c){totalCount "
    "pageInfo{hasNextPage endCursor} nodes{editedAt diff editor{login}}}}}}"
)


def count_comment_edits(node_id, run_command, notes=None):
    """How many bodies this comment has held; None when that cannot be read.

    Deliberately omits the body field, so it stays answerable when the expensive
    read is not. Fails closed like its sibling: an unreadable count is not zero.
    """
    if not node_id:
        _note(notes, "the comment carries no node id, so its history cannot be addressed")
        return None
    rc, out, err_text = run_command(
        ["gh", "api", "graphql", "-f", "query=" + _EDIT_COUNT_QUERY, "-F", "id=" + node_id]
    )
    if rc == 124:
        _note(notes, "the edit count did not answer inside its per-call bound")
        return None
    if rc != 0 or not out:
        _note(notes, _failure_reason(out, err_text, rc).replace("the read", "the edit count"))
        return None
    try:
        total = json.loads(out)["data"]["node"]["userContentEdits"]["totalCount"]
    except (ValueError, KeyError, TypeError):
        _note(notes, "the edit count returned no payload")
        return None
    if not isinstance(total, int) or total < 0:
        _note(notes, "the edit count was not a count")
        return None
    return total


def _failure_reason(out, err_text, rc):
    """Why a FAILED read failed, read only from places a payload cannot forge.

    The throttle signature is matched on stderr and on the GraphQL envelope's own
    `errors[].type` / `errors[].code`, never on the whole response text. A
    SUCCESSFUL history payload carries the review bodies themselves, and a review
    that discusses rate limits would otherwise turn a clean read into a refusal --
    a false `ok=false` that becomes a required status no recompute can clear. So
    this runs only after the caller has established the read did not succeed.
    """
    codes = ""
    try:
        errors = json.loads(out or "")["errors"]
        codes = " ".join(
            "{} {}".format(e.get("type") or "", e.get("code") or "")
            for e in errors
            if isinstance(e, dict)
        ).lower()
    except (ValueError, KeyError, TypeError):
        codes = ""
    stderr_text = (err_text or "").lower()
    if "rate limit" in stderr_text or "rate_limit" in codes or "rate limit" in codes:
        return "the API refused the read with a rate limit"
    return "the read failed (exit {})".format(rc)


def _note(notes, reason):
    """Record why a read failed, for a caller that must say more than "unreadable"."""
    if notes is not None and reason not in notes:
        notes.append(reason)


def fetch_comment_edit_history(node_id, lane, head_sha, run_command, deadline=None, notes=None):
    """What each body this comment has held CONCLUDED; None on error.

    Returns one entry per stored body as ``{at, editor, stamped, ...shape}``, never
    the body itself -- see the reduction in the loop.

    ``diff`` is GraphQL's name for the field, but what it returns is the whole
    body as of that edit, which is what makes a superseded verdict readable
    rather than merely detectable. The oldest entry is the original publication,
    so a comment created and then replaced once yields two entries.

    Returns None on ANY failure -- an unreadable history cannot be told from an
    unedited comment, and a caller that treats the two alike reports "no verdict
    was superseded" from a failed read. Every caller here fails closed on None.
    ``deadline`` is a ``time.monotonic()`` instant past which the read gives up
    the same way, so a slow page cannot spend a caller's whole budget.

    ``notes`` collects WHY a read failed. A rate limit and a genuine fault both
    fail closed, but they ask different things of whoever reads the result -- wait
    versus investigate -- and a bare "unreadable" cannot tell them apart. GitHub
    reports a GraphQL secondary limit in the envelope's ``errors[]`` while its own
    rate_limit endpoint can still show budget remaining, so the signature is read
    from stderr and from those error entries -- never from a response this read
    already SUCCEEDED in fetching, whose body is reviewer prose that may discuss
    rate limits itself. See ``_failure_reason``.
    """
    if not node_id:
        return None
    entries = []
    cursor = None
    for _page in range(_MAX_EDIT_PAGES):
        if deadline is not None and time.monotonic() >= deadline:
            _note(notes, "the time budget ran out mid-history")
            return None
        args = [
            "gh",
            "api",
            "graphql",
            "-f",
            "query=" + _EDIT_HISTORY_QUERY,
            "-F",
            "id=" + node_id,
            "-F",
            "n=" + str(_EDIT_PAGE_SIZE),
        ]
        if cursor:
            args += ["-F", "c=" + cursor]
        rc, out, err_text = run_command(args)
        if rc == 124:
            _note(notes, "the read did not answer inside its per-call bound")
            return None
        if rc != 0 or not out:
            _note(notes, _failure_reason(out, err_text, rc))
            return None
        try:
            edits = json.loads(out)["data"]["node"]["userContentEdits"]
        except (ValueError, KeyError, TypeError):
            _note(notes, "the read returned no history payload")
            return None
        for node in edits.get("nodes") or []:
            if not isinstance(node, dict):
                return None
            editor = node.get("editor") or {}
            # The body is REDUCED here and never retained. Each entry's body is an
            # externally-authored review comment that runs to tens of KB, and a page
            # asks for a hundred of them, so keeping them would bound the entry
            # COUNT while leaving the bytes unbounded. Only the shape and the two
            # scalars are ever consumed, so they are all that survives the loop.
            body = node.get("diff") or ""
            entries.append(
                dict(
                    _sample_shape(body, lane, head_sha),
                    at=node.get("editedAt") or "",
                    editor=(editor.get("login") or ""),
                    stamped=_stamped_for_head(body, lane, head_sha),
                )
            )
        page_info = edits.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            # The cap is a page cap, not a record cap: a history longer than the
            # cap is UNREAD, not empty, so report it as unreadable.
            total = edits.get("totalCount")
            if isinstance(total, int) and len(entries) < total:
                return None
            return entries
        cursor = page_info.get("endCursor") or ""
        if not cursor:
            return None
    return None


def _sample_shape(body, lane, head_sha):
    """What one stored body concluded, for a body already known to be this lane's.

    Two spellings of "this sample blocks", because the lanes do not share one.
    GPT and Opus write ``[BLOCK-MERGE] <head>`` into the body. The whole-design
    lanes never write that marker at all -- they end their body with
    ``<Lane>-Verdict: BLOCK`` -- so reading the marker alone makes ``blocking``
    structurally False for DESIGN, UX and FIRST-PRINCIPLES, and a same-head
    re-sample that replaces one of their BLOCK verdicts with a PASS would pass
    every gate silently. Whichever spelling a lane uses, a block is a block.
    """
    verdict = VERDICT_LINE_RE.search(body)
    label = verdict.group(1).upper() if verdict else ""
    marked = any(sha_matches(sha, head_sha) for sha in BLOCK_MERGE_RE.findall(body))
    # The verdict line is scoped to the lanes that own it, matching
    # design_lane_verdicts: a lane that does not write one cannot have a BLOCK
    # read out of model prose that merely quotes the shape.
    declared = label == "BLOCK" and (lane or "").upper() in WHOLE_DESIGN_LANES
    return {
        "blocking": marked or declared,
        "downgraded": _sanctioned_downgrade(body, head_sha),
        "verdict": label,
        "findings": len(FINDING_RE.findall(body)),
        "chars": len(body),
    }


def _sanctioned_downgrade(body, head_sha):
    """True when the WORKFLOW's own heading says adjudication downgraded this head.

    Read from the text BEFORE the first ``<details>``, which on a non-blocking
    body is entirely workflow-authored: the HTML key, the ``## <Lane> Review --
    <verdict>`` heading rendered from the parsed adjudication decision, and the
    sentence naming the head. The model's own output is embedded inside
    ``<details><summary>Review details</summary>`` on that path, so nothing it
    writes can reach this region -- which is the whole point, because the
    alternative signal (a ``[BLOCK-MERGE-DOWNGRADED]`` marker anywhere in the
    body) sits in the embedded model text and a review whose prose contains it
    would forge its own clearance.

    Requires the heading to BE a heading (``^##``) rather than the phrase
    appearing loose, and requires the head to be named in the same region, so a
    heading left over from another head does not clear this one.
    """
    prefix = (body or "").split("<details>", 1)[0]
    if not _DOWNGRADE_HEADING_RE.search(prefix):
        return False
    return any(sha_matches(sha, head_sha) for sha in _PREFIX_SHA_RE.findall(prefix))


def _stamped_for_head(body, name, head_sha):
    """True when ``body`` carries ``name``'s OWN stamp for ``head_sha``.

    Same rule as extract_findings: a stamp counts only under the lane whose
    workflow-authored key owns the comment, so a stamp name appearing inside
    model prose cannot make another lane's history look superseded.
    """
    return any(
        stamp_name == name and sha_matches(sha, head_sha)
        for stamp_name, sha in REVIEWED_STAMP_RE.findall(body)
    )


def superseded_verdicts(
    comments,
    head_sha,
    bindings,
    run_command,
    authors=DEFAULT_MARKER_AUTHORS,
    deadline=None,
):
    """Report verdicts for ``head_sha`` that a later sample at ``head_sha`` replaced.

    Returns ``{"ok", "lanes", "lanes_seen", "blocking_dropped", "error"}``.
    ``lanes`` carries one entry per bound lane whose history was read, each with
    the current body's shape and a newest-first list of superseded samples for
    this head. ``lanes_seen`` is how many lanes were actually examined, reported
    as its own field so a caller reads the population rather than inferring it
    from an array's length. ``blocking_dropped`` names the lanes where a
    superseded sample BLOCKED this head and the body now presented does not --
    the direction that turns a judged block into a clean board, which is the only
    direction that can manufacture a pass. A sample blocks in either of the two
    spellings the lanes use: ``[BLOCK-MERGE] <head>`` in the body, which GPT and
    Opus write, or a ``<Lane>-Verdict: BLOCK`` line from a whole-design lane,
    which never writes that marker at all. See ``_sample_shape``.

    A lane is NOT named when the presented body's WORKFLOW-AUTHORED heading says
    adjudication downgraded this head: that is the repository's own record that the
    block was cleared by decision, and the rewrite producing it necessarily leaves a
    superseded blocking body behind. ``current_downgraded`` reports that reading per
    lane. The heading is used rather than the rewritten marker because the marker
    lands inside the embedded model output -- see ``_sanctioned_downgrade``.

    ``ok`` False means the question could not be ANSWERED and must be read as
    unknown, never as "nothing was superseded". Two causes reach it and both fail
    closed. An unreadable history is one. EXAMINING NO LANE AT ALL is the other:
    a head whose lanes have not posted yet, or whose only comment wearing a lane
    key fails the author check, yields an empty lane set, and answering "nothing
    was superseded" there reports calm from having observed nothing -- the same
    shape as a clean scan of an empty population. A gate that has never seen a
    lane has not verified stability. So zero examined is UNKNOWN, stated in
    ``error``, rather than a pass a caller has to know to distrust.

    A caller that gates on this treats False as pending, the same convention
    disposition_gate uses: a transient API failure must not turn a required
    status red.
    """
    result = {
        "ok": False,
        "lanes": [],
        "lanes_seen": 0,
        "blocking_dropped": [],
        "error": "",
        # Which of the two causes of ok=False this is, as a value rather than as
        # prose a caller would have to pattern-match. "unreadable" is a read that
        # FAILED on a lane that exists; "no-lanes" is an empty population. Both
        # are UNKNOWN and neither is a pass, but a caller may treat them
        # differently: the required status maps both to pending, while the local
        # gate fails closed only on "unreadable", because an empty population is
        # already reported by the marker evaluation that runs before this.
        "cause": "",
    }
    if comments is None or not head_sha:
        result["error"] = "comments unavailable or no head sha"
        result["cause"] = "unreadable"
        return result
    allowed = {a.lower() for a in authors or ()}
    lanes = []
    dropped = set()
    for comment in comments:
        body = comment.get("body") or ""
        name = (bindings or {}).get(comment_key(body))
        if not name:
            continue
        user = comment.get("user") or {}
        if user.get("type") != "Bot" or (user.get("login") or "").lower() not in allowed:
            continue
        if deadline is not None and time.monotonic() >= deadline:
            # Out of budget with lanes still unread. Reporting the lanes already
            # examined would be a clean answer over a population cut short by the
            # clock, which is the same fault as answering over an empty one.
            result["error"] = (
                "the time budget ran out with lane {} and possibly others "
                "unexamined, so supersession is not evaluable for this head".format(name)
            )
            result["cause"] = "unreadable"
            return result
        notes: list = []
        node_id = comment.get("node_id") or ""
        stored = count_comment_edits(node_id, run_command, notes)
        if stored is None:
            result["error"] = "lane {} edit count not read: {}".format(
                name, notes[0] if notes else "no reason reported"
            )
            result["cause"] = "unreadable"
            return result
        if stored <= 1:
            # At most one stored body, so there is no earlier sample for this or
            # any other head. The current body is that body, and the bodies are
            # what the expensive read would have fetched.
            history = []
        else:
            history = fetch_comment_edit_history(
                node_id, name, head_sha, run_command, deadline, notes
            )
        if history is None:
            result["error"] = "lane {} history not read: {}".format(
                name, notes[0] if notes else "no reason reported"
            )
            result["cause"] = "unreadable"
            return result
        # Order WITHIN the head asked about, not within the comment's whole
        # history. Among the bodies stamped for this head, every one but the most
        # recent was replaced by a later sample for the SAME head, which is the
        # finding. The most recent one was replaced by a newer head's verdict or
        # is the body presented now -- an ordinary publish either way. Scoping the
        # sort this way makes the answer correct for a head the PR has since moved
        # past, where the overall-newest body belongs to another head entirely.
        stamped = sorted(
            (e for e in history if e.get("at") and e.get("stamped")),
            key=lambda e: e["at"],
            reverse=True,
        )
        samples = [{k: v for k, v in e.items() if k != "stamped"} for e in stamped[1:]]
        current = _sample_shape(body, name, head_sha)
        entry = {
            "lane": name,
            "comment_id": comment.get("id"),
            "current_stamped": _stamped_for_head(body, name, head_sha),
            "current_blocking": current["blocking"],
            "current_downgraded": current["downgraded"],
            "current_verdict": current["verdict"],
            "superseded": samples,
        }
        lanes.append(entry)
        # BLOCKING DROPPED needs the presented body to be a verdict for THIS
        # head. With the PR moved past the head asked about, the slot holds a
        # newer head's verdict, so `current_blocking` is False for a reason that
        # has nothing to do with a dropped block -- and staleness is already
        # reported by the marker evaluation. Requiring current_stamped keeps this
        # signal to the one case it names: both samples judged this head, the
        # earlier blocked, the survivor does not.
        #
        # And the presented body's own WORKFLOW-AUTHORED heading must not say
        # adjudication downgraded this head. That heading is the repository's
        # record that this exact block was cleared by decision, rendered from the
        # parsed decision the gate acts on, and the rewrite producing it replaces
        # the comment in place -- so it leaves a superseded blocking body behind as
        # a matter of course. Naming the lane there would redden the required
        # status on a head whose current verdict is a legitimate clear, and no
        # recompute could ever clear it, since the stored history keeps the old
        # body forever. Read from the heading and not from a marker in the body:
        # the marker sits in the embedded model output, so a review quoting it
        # would forge its own clearance. See _sanctioned_downgrade.
        if (
            entry["current_stamped"]
            and any(s["blocking"] for s in samples)
            and not current["blocking"]
            and not current["downgraded"]
        ):
            dropped.add(name)
    result["lanes"] = sorted(lanes, key=lambda e: e["lane"])
    result["lanes_seen"] = len(lanes)
    result["blocking_dropped"] = sorted(dropped)
    if not lanes:
        result["error"] = (
            "no bound lane marker comment was examined, so supersession is not "
            "evaluable for this head"
        )
        result["cause"] = "no-lanes"
        return result
    result["ok"] = True
    return result


def extract_findings(
    comments,
    head_sha,
    bindings,
):
    """Yield reviewer findings stamped for ``head_sha`` with stable span ids.

    A stamp counts only in the workflow-keyed lane that owns the comment. This
    keeps an injected reviewer name in model output from forging another lane's
    freshness.
    """
    for comment in comments or []:
        body = comment.get("body") or ""
        name = bindings.get(comment_key(body))
        if not name:
            continue
        fresh = any(
            stamp_name == name and sha_matches(sha, head_sha)
            for stamp_name, sha in REVIEWED_STAMP_RE.findall(body)
        )
        if not fresh:
            continue
        reviewer = name.lower()
        block_merge = any(sha_matches(sha, head_sha) for sha in BLOCK_MERGE_RE.findall(body))
        for kind, path, line, text in FINDING_RE.findall(body):
            try:
                line_no = int(line)
            except ValueError:
                line_no = 1
            rule_class = "{}/{}".format(reviewer, kind)
            yield {
                "reviewer": reviewer,
                "kind": kind,
                "path": path,
                "line": line_no,
                "text": text.strip(),
                "block_merge": block_merge,
                "span": span_hash(path, rule_class),
            }


# Whole-design lanes: Design, UX and First Principles review the SHAPE of the
# change rather than a line, so they emit a verdict line plus prose sections
# instead of the `BLOCKING -- path:line` shape FINDING_RE reads. SKILL.md ranks
# them above the line-level lanes in triage, which is only possible if their
# items reach the loop with span ids of their own.
WHOLE_DESIGN_LANES = ("DESIGN", "UX", "FIRST-PRINCIPLES")
# `Design-Verdict: CONCERNS`, `UX-Verdict: PASS`, `First-Principles-Verdict: BLOCK`
VERDICT_LINE_RE = re.compile(
    r"^[A-Za-z-]+-Verdict:\s*(PASS|CONCERNS|BLOCK)\b", re.MULTILINE | re.IGNORECASE
)
# The item-bearing sections of those lanes' output templates. Deliberately an
# allowlist rather than "every `###` heading": First Principles also emits a
# `### What this change ships` INVENTORY and UX a `### Evidence gaps` note, and
# neither is an item an author disposes of one by one.
DESIGN_ITEM_SECTIONS = (
    "Blockers",
    "Watch",
    "Subtractions",
    "Suggestions",
    "Not justified as shipped",
)
# A per-item trailing line naming what would retire the item. Optional: it is
# absent from every lane's older output, so parsing must not depend on it.
CLEARS_WHEN_RE = re.compile(r"clears\s+when\s*:\s*(.+?)\s*$", re.IGNORECASE)
# One synthetic path for every whole-design item. Their findings are about the
# change's shape, so no single file owns them, and a real path here would read
# as a line-level finding.
DESIGN_ITEM_PATH = "(design)"
_DESIGN_SPAN_TEXT_CHARS = 80
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$")
_BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.*)$")
_MD_NOISE_RE = re.compile(r"[`*_~]+")


def normalize_design_item_text(text):
    """Fold one item to the identity its span is hashed from.

    Markdown emphasis, list markers and line wrapping all change without the
    item changing, so they are stripped; the first
    ``_DESIGN_SPAN_TEXT_CHARS`` characters then keep the span stable when the
    lane re-words a trailing clause and change it when the item itself changes.
    """
    plain = _MD_NOISE_RE.sub("", text or "")
    return " ".join(plain.split()).lower()[:_DESIGN_SPAN_TEXT_CHARS]


def _collapse(lines):
    return " ".join(" ".join(lines).split())


def design_section_items(body):
    """Yield ``(section, item text)`` for each item in a whole-design body.

    Items are the section's bullets or numbered entries, each carrying its
    continuation lines (the consequence chain and the fix the templates ask
    for). A section whose lane wrote PROSE instead of a list still yields its
    paragraphs: the templates say "one or two lines each" without mandating a
    bullet, and silently yielding nothing there would hide exactly the CONCERNS
    items this extractor exists to surface.
    """
    wanted = {name.lower(): name for name in DESIGN_ITEM_SECTIONS}
    section = ""
    blocks: list = []

    def close():
        items = [b for b in blocks if b and b[0]]
        chosen = items or [b for b in blocks if b and not b[0]]
        out = []
        for _is_item, lines in chosen:
            text = _collapse(lines)
            if text:
                out.append((section, text))
        return out

    results: list = []
    for raw in (body or "").splitlines():
        heading = _HEADING_RE.match(raw)
        if heading:
            if section:
                results.extend(close())
            blocks = []
            section = wanted.get(heading.group(1).strip().lower(), "")
            continue
        if not section:
            continue
        # The stamp, the blocking marker and the verdict line are the body's
        # own trailers, never item text.
        if (
            REVIEWED_STAMP_RE.search(raw)
            or BLOCK_MERGE_RE.search(raw)
            or VERDICT_LINE_RE.search(raw)
        ):
            results.extend(close())
            blocks = []
            section = ""
            continue
        stripped = raw.strip()
        if not stripped:
            blocks.append(None)  # separator: the next line starts a new block
            continue
        bullet = _BULLET_RE.match(raw)
        if bullet:
            blocks.append([True, [bullet.group(1).strip()]])
        elif blocks and blocks[-1] is not None:
            blocks[-1][1].append(stripped)
        else:
            blocks.append([False, [stripped]])
    if section:
        results.extend(close())
    return results


def _fresh_design_bodies(comments, head_sha, bindings, lanes=WHOLE_DESIGN_LANES):
    """Yield ``(lane, body, verdict)`` per whole-design lane fresh for the head.

    Freshness follows extract_findings exactly: the lane's OWN stamp inside its
    own workflow-keyed comment, so a stamp name injected by model output cannot
    forge another lane's review.
    """
    wanted = {name.upper() for name in lanes or ()}
    for comment in comments or []:
        body = comment.get("body") or ""
        name = (bindings or {}).get(comment_key(body))
        if not name or name.upper() not in wanted:
            continue
        fresh = any(
            stamp_name == name and sha_matches(sha, head_sha)
            for stamp_name, sha in REVIEWED_STAMP_RE.findall(body)
        )
        if not fresh:
            continue
        match = VERDICT_LINE_RE.search(body)
        yield name, body, (match.group(1).upper() if match else "")


def design_lane_verdicts(comments, head_sha, bindings, lanes=WHOLE_DESIGN_LANES):
    """``{lane: verdict}`` for whole-design lanes stamped for ``head_sha``.

    A lane that reported PASS carries no items, so its verdict has to come from
    somewhere other than the item list for the reader to see the lane ran.
    """
    return {
        name: verdict
        for name, _body, verdict in _fresh_design_bodies(comments, head_sha, bindings, lanes)
    }


def extract_design_items(comments, head_sha, bindings, lanes=WHOLE_DESIGN_LANES):
    """Yield whole-design items stamped for ``head_sha`` with stable span ids.

    Deliberately SEPARATE from extract_findings, which stays the definition of
    "this lane has findings" for disposition_violations -- and therefore for
    pr-readiness.yml's server-side gate. Folding these items into that universe
    would silently reclassify a today-valid spanless ``target=design``
    disposition as a violation and fail the required status on PRs nobody
    touched, so the design items feed only local reporting (pr_findings.py) and
    the local unanswered-CONCERNS check (pr_status.py). A disposition may still
    CLAIM one of these spans: an unknown span for a lane with no
    extract_findings identities is not a violation.
    """
    for name, body, verdict in _fresh_design_bodies(comments, head_sha, bindings, lanes):
        for section, text in design_section_items(body):
            clears = CLEARS_WHEN_RE.search(text)
            yield {
                "reviewer": name.lower(),
                "lane": name,
                "verdict": verdict,
                "kind": section.upper(),
                "path": DESIGN_ITEM_PATH,
                "text": text,
                "clears_when": clears.group(1).strip() if clears else "",
                "block_merge": verdict == "BLOCK",
                "span": span_hash(name, normalize_design_item_text(text)),
            }


def unanswered_concern_lanes(verdicts, records, head_sha):
    """Whole-design lanes at CONCERNS for this head with no disposition yet.

    LOCAL ONLY -- the prepare-pr loop's own stop condition. SKILL.md says a
    green rollup with an unanswered CONCERNS is not converged, and nothing
    enforced it: the loop armed auto-merge straight past a fresh CONCERNS. The
    server-side required status is deliberately NOT changed, because CONCERNS
    is advisory by contract for every writer who never runs this loop.

    Answered means a repository writer's disposition record targets that lane
    and names this head (a short ``head=`` prefix counts, as everywhere else).
    """
    out = []
    for name, verdict in sorted((verdicts or {}).items()):
        if verdict != "CONCERNS" or name.upper() not in WHOLE_DESIGN_LANES:
            continue
        target = name.lower()
        answered = any(
            not record.get("malformed")
            and (record.get("target") or "").lower() == target
            and sha_matches(record.get("head") or "", head_sha)
            for record in records or []
        )
        if not answered:
            out.append(name)
    return out


def unanswered_concerns_reason(lane, head_sha):
    """The one blocking reason text for an unanswered whole-design CONCERNS."""
    return (
        "unanswered CONCERNS from {} on current head - answer each Watch item "
        "with an <!-- ai-review-disposition target={} head={} --> comment "
        "(fix, rebut, or accept-and-defer)".format(lane, lane.lower(), head_sha)
    )


def parse_disposition_record(comment):
    """Parse one disposition-marked comment into a record dict, else None.

    A body carrying the ledger-selected prefix remains visible as ``malformed``
    when its marker does not parse. Span claims preserve first-seen order and
    ignore quoted evidence, where a displayed span is not the writer's claim.
    """
    body = comment.get("body") or ""
    if not body.startswith(DISPOSITION_PREFIX):
        return None
    user = comment.get("user") or {}
    record = {
        "author": user.get("login") or "",
        "comment_id": comment.get("id"),
        "target": "",
        "head": "",
        "spans": [],
        "bullets": 0,
        "malformed": True,
    }
    match = DISPOSITION_MARKER_RE.match(body)
    if match:
        record["target"] = match.group(1).lower()
        record["head"] = match.group(2)
        record["malformed"] = False
        seen = set()
        spans = []
        bullets = 0
        for line in body.split("\n"):
            if line.lstrip().startswith(">"):
                continue
            if DISPOSITION_BULLET_RE.match(line):
                bullets += 1
            for span in SPAN_CLAIM_RE.findall(line):
                if span not in seen:
                    seen.add(span)
                    spans.append(span)
        record["spans"] = spans
        record["bullets"] = bullets
    return record


def fetch_disposition_comments(repo, number, run_command):
    """Return disposition-marked comments from any author, or None on error.

    Collection cannot filter to workflow bots because dispositions come from
    agents or humans; writer authority is checked separately before use.
    """
    if not repo:
        return None
    comments: list = []
    for page in range(1, _MAX_COMMENT_PAGES + 1):
        rc, out, _ = run_command(
            [
                "gh",
                "api",
                "repos/{}/issues/{}/comments?per_page=100&page={}".format(repo, number, page),
            ]
        )
        if rc != 0 or not out.strip():
            return None
        try:
            batch = json.loads(out)
        except ValueError:
            return None
        if not isinstance(batch, list):
            return None
        for comment in batch:
            if isinstance(comment, dict) and (comment.get("body") or "").startswith(
                DISPOSITION_PREFIX
            ):
                comments.append(comment)
        if len(batch) < 100:
            return comments
    return None


def author_write_verdict(repo, login, run_command):
    """ "writer" / "other" / "unknown" for ``login``'s permission on ``repo``.

    The marker prefix alone is forgeable -- anyone can comment on a
    public-repo PR -- so authority comes from the collaborators permission
    API, the same check codex-review.yml applies before a disposition enters
    the adjudication ledger.

    The three outcomes are NOT interchangeable, and collapsing them is how a
    dropped record silently produces a clean gate:

    * "writer" -- admin/maintain/write. The record counts.
    * "other" -- a DEFINITIVE answer that this author is not a writer: a
      permission below write, or HTTP 404 (not a collaborator at all), or
      HTTP 403 (this token cannot read the endpoint). The record is IGNORED,
      never gated on: a drive-by commenter must not be able to hold a PR
      hostage with a crafted marker. 403 is deliberately definitive rather
      than unknown -- for a workflow token it is a stable property of the
      token's permissions, not a blip, so calling it unknown would convert a
      configuration state into a permanent "cannot evaluate" on every pull
      request that carries any disposition comment. That trades a missing
      enforcement for a repository-wide merge block, which is the wrong
      direction for a required status. The ONE 403 that is not stable is a
      rate limit, carved out below.
    * "unknown" -- a TRANSIENT failure (5xx, 429, a rate-limit/abuse-detection
      403, network, empty or unparseable body). The caller must not treat this
      as "not a writer": the adjudication ledger may have admitted the same
      record when ITS lookup succeeded, so dropping it here would let a
      rule-violating record keep its downgrade power while the required status
      published success.
    """
    if not repo or not login:
        return "other"
    rc, out, err = run_command(
        ["gh", "api", "repos/{}/collaborators/{}/permission".format(repo, login)]
    )
    if rc == 0 and out.strip():
        try:
            permission = json.loads(out).get("permission") or ""
        except (ValueError, AttributeError):
            return "unknown"
        return "writer" if permission.lower() in ("admin", "maintain", "write") else "other"
    # GitHub's primary and secondary rate limits surface as HTTP 403 carrying
    # rate-limit text, which is transient exactly like a 429 -- the same
    # carve-out pr-readiness.yml's own gh_retry helper already makes for every
    # read-only call. Tested BEFORE the status classification, because that
    # 403 must read as unknown rather than as "this token has no access".
    if re.search(r"rate limit|abuse detection", err or "", re.IGNORECASE):
        return "unknown"
    # A definitive "no" is a 404 (not a collaborator) or a non-rate-limit 403
    # (this token cannot read the endpoint at all); everything else is transient.
    if re.search(r"HTTP (?:404|403)\b", err or ""):
        return "other"
    return "unknown"


def author_is_repo_writer(repo, login, run_command):
    """Return whether ``login`` has write, maintain, or admin permission.

    The boolean face of author_write_verdict, for callers that only need "does
    this record count": an unknown verdict reads as False here, so a record is
    never ACTED on without positive confirmation. A caller that must also
    distinguish "could not determine" -- because dropping a record the ledger
    admitted would publish a falsely clean verdict -- calls
    author_write_verdict directly.
    """
    return author_write_verdict(repo, login, run_command) == "writer"


def writer_disposition_records(repo, comments, run_command, verdict=None):
    """Parse disposition comments and retain only repository writers' records.

    Permission results are cached per author without a lookup cap: a flood of
    non-writer comments cannot push a real writer's record past an artificial
    boundary that the adjudication ledger itself does not apply.

    Returns None -- the same "could not establish the record set" signal an
    unreadable comment list produces -- when any author's permission is
    INDETERMINATE. Dropping such an author instead would be unsound in one
    specific, reachable way: the ledger makes the identical lookup at review
    time, so it can have admitted a record whose later verification here fails
    transiently, and the record would then keep full downgrade power while this
    gate reported nothing to answer for. An author DEFINITIVELY below write is
    dropped as before (see author_write_verdict for why 403 counts as
    definitive).

    ``verdict`` overrides the permission lookup with a ``(repo, login) ->
    verdict`` callable. Each entrypoint passes its OWN exported
    ``author_write_verdict``, so the verdict stays the entrypoint's substitution
    seam -- the same seam ``run_command`` is for the commands underneath it.
    """
    if comments is None:
        return None
    if verdict is None:

        def verdict(for_repo, login):
            return author_write_verdict(for_repo, login, run_command)

    verdicts: dict = {}
    records = []
    for comment in comments:
        record = parse_disposition_record(comment)
        if record is None:
            continue
        login = record["author"]
        if login not in verdicts:
            verdicts[login] = verdict(repo, login)
        if verdicts[login] == "unknown":
            return None
        if verdicts[login] == "writer":
            records.append(record)
    return records


def disposition_violations(
    records,
    comments,
    head_sha,
    bindings,
):
    """Return sorted violations of the one-lane, one-finding disposition rule.

    Records are checked against the reviewer findings on both the head they
    judged and the current head. A record keeps ledger power after a new push,
    so an older ``head=`` does not exempt malformed, multi-finding, or cross-lane
    claims. Lanes without parseable finding identities remain exempt from the
    requirement to claim one.
    """
    lanes = {name.lower() for name in (bindings or {}).values()}

    def lane_map(for_head):
        found: dict = {}
        if for_head:
            for finding in extract_findings(comments or [], for_head, bindings or {}):
                found.setdefault(finding["span"], finding["reviewer"])
        return found

    current_map = lane_map(head_sha)
    current_lanes = set(current_map.values())
    judged_cache: dict = {}
    out = set()
    for record in records or []:
        where = "comment {} by {}".format(
            record.get("comment_id") or "?", record.get("author") or "?"
        )
        if record.get("malformed"):
            out.add(
                "malformed disposition marker ({}) - the adjudication ledger "
                "selects it by prefix alone, so fix or delete that comment: "
                "expected '{}target=<lane> head=<sha> -->'".format(where, DISPOSITION_PREFIX)
            )
            continue
        target = record.get("target") or ""
        spans = record.get("spans") or []
        judged_head = record.get("head") or ""
        if judged_head and len(judged_head) < 40:
            for comment in comments or []:
                for _name, stamped in REVIEWED_STAMP_RE.findall(comment.get("body") or ""):
                    if len(stamped) == 40 and stamped.startswith(judged_head):
                        judged_head = stamped
                        break
                if len(judged_head) == 40:
                    break
        if judged_head not in judged_cache:
            judged_cache[judged_head] = lane_map(judged_head)
        judged_map = judged_cache[judged_head]
        judged_lanes = set(judged_map.values())
        if len(spans) > 1:
            out.add(
                "one disposition record claims {} findings ({}; target={}; "
                "spans: {}) - one rationale covers exactly one finding, so "
                "post one disposition comment per span".format(
                    len(spans), where, target, ", ".join(spans)
                )
            )
        bullets = record.get("bullets") or 0
        if bullets > 1:
            out.add(
                "one disposition record carries {} finding-title bullets "
                "({}; target={}) - one rationale covers exactly one finding, "
                "so post one comment per finding even when the findings "
                "share a span id".format(bullets, where, target)
            )
        if not spans and target in lanes and (target in judged_lanes or target in current_lanes):
            out.add(
                "disposition record claims no span= finding identity ({}; "
                "target={}) while that lane has findings on the head it "
                "judged or the current one - name exactly one span=<id> from "
                "pr_findings.py per comment".format(where, target)
            )
        for span in spans:
            lane = judged_map.get(span) or current_map.get(span)
            if lane is not None and lane != target:
                out.add(
                    "cross-lane disposition ({}; target={}) claims span {} "
                    "from lane {} - one comment covers exactly one lane, so "
                    "give that finding its own comment with target={}".format(
                        where, target, span, lane, lane
                    )
                )
            elif lane is None and target in lanes and target in judged_lanes:
                out.add(
                    "disposition record claims span {} that resolves to no "
                    "finding ({}; target={}) - claim the span=<id> exactly as "
                    "pr_findings.py printed it for the head the record "
                    "judged".format(span, where, target)
                )
    return sorted(out)
