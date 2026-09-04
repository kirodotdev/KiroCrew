"""Shared reviewer-finding and disposition contract for prepare-pr scripts.

The prepare-pr skill is distributed as a directory, so its executable scripts can
share this stdlib-only module while remaining runnable by absolute path from any
working directory.
"""

from __future__ import annotations

import hashlib
import json
import re

REVIEWED_STAMP_RE = re.compile(r"\[([A-Z][A-Z0-9_-]*)-REVIEWED\]\s+([0-9a-f]{7,40})\b")
BLOCK_MERGE_RE = re.compile(r"\[BLOCK-MERGE\]\s+([0-9a-f]{7,40})\b")


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
      suffix. Observed on PR 4107, where the Design lane wrote 25 characters
      (the head's first 14 followed by its last 11) and every consumer read the
      PR as BLOCKED while PR Readiness was green.

    The elided form is verified, not merely tolerated: the token must be
    SHORTER than the head (a full-length token that is not a prefix names a
    different commit, and stays rejected), it must split into a >=7-hex prefix
    of the head plus a non-empty suffix of the head, and both halves must be
    the head's own. That keeps the guard the strict match was protecting -- a
    well-formed reference to another commit cannot pass, because it would have
    to begin with 7+ characters of THIS head and end with this head's tail --
    while a mangling of the current head no longer fails closed.

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


def span_hash(path, rule_class):
    """Return a stable path-and-rule identity without reading the named path.

    Finding paths come from untrusted bot comments. Hashing their text avoids a
    file read of model-influenced input while keeping identities stable across
    rebases. The identity is deliberately path-scoped: two findings of one kind
    in the same file share an id, which errs toward earlier recurrence handling.
    """
    key = "{}|{}".format(path, rule_class)
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


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
