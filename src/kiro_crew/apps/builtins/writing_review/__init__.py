"""Writing Review — multi-scanner document review builtin app.

Public surface (imported by tests, backend routes, and the agent skill):

* :class:`Section` -- a heading/body pair extracted from a document.
* :class:`Finding` -- one issue flagged by a scanner, anchored to a section.
* :class:`ReviewContext` -- audience/type/tone/additional_context supplied by the user.
* :class:`ScanResult` -- the full output of :func:`run_scan`.
* :func:`parse_doc` -- turn a ``.md``/``.txt``/``.docx`` file into ``Section`` objects.

Additional orchestration (``run_scan``, prompt builders, artifact helpers)
is added in later slices of the spec; this module is intentionally the
single entry point for the app's data model.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from kiro_crew.apps.builtins.writing_review.pool import (
    TruncatedResponseError as _TruncatedResponseError,
)
from kiro_crew.apps.builtins.writing_review.pool import (
    get_pool,
)
from kiro_crew.security import is_sensitive_path

logger = logging.getLogger(__name__)


# ATX headings: one to six ``#`` at the start of a line, whitespace, then text.
# The trailing ``\n?`` swallows the newline so the body slice that follows
# does not carry an empty leading line.
_ATX_HEADING_PATTERN = re.compile(r"^(#{1,6})\s+(.+?)\n?$", re.MULTILINE)

# Scanners that always run, regardless of the user-supplied ``doc_type``.
# Order determines the reported ``scanners_run`` sequence but not execution
# order (execution is parallel via ``asyncio.gather``).
ALWAYS_ON_SCANNERS: tuple[str, ...] = (
    "clarity",
    "naturalness",
    "structure",
    "evidence",
    "consistency",
    "attribution",
    "audience",
    "readability",
)

# Doc-type substrings that trigger conditional scanners. Matches are
# case-insensitive; a doc_type of ``"Design Doc"`` picks up both
# ``"design"`` and ``"design document"``.
CONDITIONAL_SCANNERS: dict[str, str] = {
    "design": "design",
    "design document": "design",
    "email": "email",
}

# Alias for readability at call sites: the caller asking for "the default
# scanners" gets the always-on set; conditional scanners are layered on top
# by :func:`_resolve_scanners` when the ``doc_type`` warrants it.
DEFAULT_SCANNERS: tuple[str, ...] = ALWAYS_ON_SCANNERS

# Values accepted for :attr:`Finding.severity` and :attr:`Finding.cross_validation`.
# Kept as frozensets so a serialiser (Slice 5) can validate incoming JSON without
# hard-coding the list.
_SEVERITY_VALUES = frozenset({"high", "medium", "low", "advisory"})
_CROSS_VALIDATION_VALUES = frozenset({"clean", "conflicts", "redundant"})


@dataclass
class Section:
    """A heading and its body, extracted from a source document."""

    heading: str
    body: str


@dataclass
class RelatedLocation:
    """One other place where the same underlying issue appears.

    Attached to a primary :class:`Finding` after the cross-validation
    collation pass. When multiple scanners flag the same root cause
    across different sections/paragraphs, the highest-severity instance
    is kept as the primary and the rest are collapsed into
    ``related_locations`` on that primary. The user sees a single card
    per issue with an "Also appears in" list underneath, instead of N
    cards for the same underlying problem.

    Field values are pass-through from the source finding: ``scanner``
    is the scanner that flagged this instance, ``issue`` is that
    scanner's own wording (kept for the tooltip / discussion context).
    """

    section: str
    paragraph: int
    scanner: str
    issue: str


@dataclass
class Finding:
    """One issue flagged by a scanner, with location and proposed fix."""

    id: str
    scanner: str
    section: str
    paragraph: int
    issue: str
    rule: str
    severity: str  # one of "high", "medium", "low", "advisory"
    proposed_fix: str
    cross_validation: str = "clean"  # one of "clean", "conflicts", "redundant"
    conflicts: list[str] = field(default_factory=list)
    confidence: str = "medium"  # one of "high", "medium", "low"
    # Slice 1 (dedup collation): set by the cross-validation pass when
    # ``cross_validation == "redundant"`` -- points at the id of the
    # primary finding this is a duplicate of. Ignored in downstream
    # rendering once collation has run, kept on the record so a
    # discussion agent asking "why was this redundant?" can trace back.
    primary_id: str = ""
    # Populated by the collation step in ``run_scan``. When a primary
    # finding absorbs one or more redundant duplicates, each duplicate's
    # location is appended here so the FindingCard can render an "Also
    # appears in" list. Empty on non-primary findings.
    related_locations: list[RelatedLocation] = field(default_factory=list)


@dataclass
class FailedScanner:
    """Metadata for a scanner that failed during a review.

    Persisted on the ``ScanResult.failed_scanners`` list so the frontend can
    render per-scanner failure detail (name, reason, timing) and the discussion
    agent can quote the reason when asked "why didn't scanner X flag Y".
    """

    name: str
    reason_class: str
    # ``reason_class`` is one of:
    #   provider_timeout | invalid_json | truncated_response
    #   missing_brief    | rate_limited | worker_died | other
    message: str
    at: str  # ISO-8601 timestamp
    duration_ms: int


@dataclass
class ReviewContext:
    """Author-supplied context that steers every scanner's prompt."""

    audience: str = ""
    doc_type: str = ""
    tone: str = ""
    additional_context: list[str] = field(default_factory=list)
    # Free-form directive from the author: what decision they want the
    # reviewer to focus on. Renders as a standalone directive line in
    # every scanner prompt when non-empty. Empty ``ask`` is the default
    # and produces no prompt output -- unlike audience/doc_type/tone,
    # a missing ask must not become a "not specified" filler line
    # because that pushes the model to reason about the absence rather
    # than about the document itself. Also surfaced in the discussion
    # agent's context bundle so the writing-review-reviewer agent can weight
    # its conversation toward the author's actual concern.
    ask: str = ""


@dataclass
class ScanResult:
    """Full output of a document scan: sections, findings, and verdict."""

    doc_path: str
    doc_name: str
    doc_context: ReviewContext
    sections: list[Section]
    findings: list[Finding]
    verdict: str  # one of "red", "yellow", "green"
    scanners_run: list[str] = field(default_factory=list)
    partial_failure: bool = False
    failed_scanners: list[FailedScanner] = field(default_factory=list)
    log_reference: dict[str, str] = field(default_factory=dict)


# Storage-key uuid prefix produced by ``_stash_pasted_document`` in the
# backend routes module: 16 lowercase hex chars followed by ``_``. Stripped
# from ``ScanResult.doc_name`` so a browse-uploaded file shows the user's
# original filename ("hapi.md") in the sidebar / review record instead of
# the collision-safety storage key ("abc12345_hapi.md"). A ``doc_path``
# that doesn't start with this prefix (e.g. the user typed their own path)
# passes through untouched — the regex only fires against our own
# convention, never mangles a user filename.
_UUID_STORAGE_PREFIX = re.compile(r"^[0-9a-f]{16}_")


def _display_doc_name(raw_basename: str) -> str:
    """Return the human-facing filename with our storage uuid prefix stripped."""
    return _UUID_STORAGE_PREFIX.sub("", raw_basename)


def parse_doc(document_path: str | Path) -> list[Section]:
    """Parse a ``.md``, ``.txt``, or ``.docx`` file into :class:`Section` objects.

    Raises:
        FileNotFoundError: If the path does not resolve to an existing file.
        PermissionError: If the resolved path matches Kiro Crew's shared list
            of sensitive locations (credentials, tokens, key material). The
            check runs against the resolved path, so a symlink into
            ``~/.aws/`` is caught even when the symlink itself sits in a
            benign directory.
    """
    document_path = Path(document_path)
    # Reject the sensitive-path shape BEFORE probing filesystem state — the
    # security guard's purpose is to bar reads of credentials/tokens/keys
    # regardless of whether the resource exists on this host, so
    # ``FileNotFoundError`` must not preempt ``PermissionError`` when the
    # path resolves inside the shared sensitive list.
    resolved_path = document_path.resolve()
    if is_sensitive_path(str(resolved_path)):
        raise PermissionError(f"Reading sensitive path is not allowed: {document_path}")
    if not document_path.is_file():
        raise FileNotFoundError(f"Document not found: {document_path}")
    if document_path.suffix.lower() == ".docx":
        return _parse_docx(document_path)
    return _parse_markdown_or_text(document_path)


def _parse_docx(document_path: Path) -> list[Section]:
    """Extract heading-anchored sections from a ``.docx`` document.

    A "heading" is any paragraph whose style name starts with ``"heading"``
    (case-insensitive), which is how ``python-docx`` exposes the ``Heading 1``
    through ``Heading 9`` built-in styles. Body between headings is joined
    by newlines. A document with no heading-styled paragraphs still yields
    one :class:`Section` so callers can rely on a non-empty list.

    Content this extractor surfaces to the scanners:

    * **Paragraphs** -- text, in document order.
    * **Tables** -- rendered as GitHub-flavoured markdown pipe-tables,
      inline at the position where the table appears in the source.
      Without this, scanners cannot see fee schedules, signature blocks,
      or any other structured content the doc actually contains and
      will false-positive on rules like "table forward-reference has
      no content".
    * **Inline images / charts** -- emitted as ``[Image]`` / ``[Chart]``
      placeholders at the position where they appear in the paragraph.
      The text-only LLM cannot review image content, but knowing an
      image is PRESENT at paragraph N stops scanners from claiming
      "no diagram exists" when the doc has one.

    Content this extractor does NOT yet surface (tracked as follow-up):
    text boxes (``<w:txbxContent>``), headers/footers, footnotes,
    comments, embedded objects. Prose docs almost never rely on these
    for their reviewable content, but a future scanner might.
    """
    import docx as docx_module  # deferred: lets the module import without python-docx installed
    from docx.oxml.ns import qn
    from docx.table import Table as DocxTable
    from docx.text.paragraph import Paragraph as DocxParagraph

    docx_document = docx_module.Document(str(document_path))
    sections: list[Section] = []
    current_heading = ""
    current_body_lines: list[str] = []

    body_paragraph_tag = qn("w:p")
    body_table_tag = qn("w:tbl")

    for body_child_element in docx_document.element.body.iterchildren():
        if body_child_element.tag == body_paragraph_tag:
            paragraph = DocxParagraph(body_child_element, docx_document.part)
            # ``paragraph.style`` is ``ParagraphStyle | None`` — a paragraph
            # with no explicit style resolves to ``None`` rather than the
            # default. Guard the ``.name`` access so mypy narrows and a
            # style-less paragraph is treated as body text (empty style name).
            paragraph_style = paragraph.style
            style_name = ((paragraph_style.name if paragraph_style else "") or "").lower()
            paragraph_text = paragraph.text.strip()
            image_and_chart_markers = _extract_inline_object_placeholders(body_child_element)
            combined_paragraph_text = _join_paragraph_pieces(
                paragraph_text, image_and_chart_markers
            )
            if style_name.startswith("heading"):
                if current_heading or current_body_lines:
                    sections.append(
                        Section(
                            heading=current_heading,
                            body="\n".join(current_body_lines).strip(),
                        )
                    )
                current_heading = combined_paragraph_text
                current_body_lines = []
                continue
            if combined_paragraph_text:
                current_body_lines.append(combined_paragraph_text)
            continue
        if body_child_element.tag == body_table_tag:
            table_wrapper = DocxTable(body_child_element, docx_document.part)
            markdown_rendered_table = _render_docx_table_as_markdown(table_wrapper)
            if markdown_rendered_table:
                current_body_lines.append(markdown_rendered_table)
            continue

    if current_heading or current_body_lines:
        sections.append(
            Section(heading=current_heading, body="\n".join(current_body_lines).strip())
        )

    if not sections:
        return [Section(heading="", body="")]
    return sections


def _render_docx_table_as_markdown(table_wrapper: Any) -> str:
    """Format a python-docx ``Table`` as a GitHub-flavoured pipe-table.

    Rules:

    * Row 0 is treated as the header row -- Word tables do not carry a
      structural "is header" flag on cells (they use styling instead),
      so this is a convention. The LLM handles a "header row that is
      really a data row" case gracefully because pipe-tables are
      readable either way.
    * Cell newlines are collapsed to single spaces so a multi-line cell
      body does not break the pipe-table shape.
    * Merged cells surface as the same text in every merged column --
      that is how python-docx represents them and the LLM can read
      through the repetition.
    """
    row_cells_by_row: list[list[str]] = []
    for docx_row in table_wrapper.rows:
        row_cells_by_row.append(
            [table_cell.text.replace("\n", " ").strip() for table_cell in docx_row.cells]
        )
    if not row_cells_by_row:
        return ""
    column_count = max(len(row_cells) for row_cells in row_cells_by_row)
    padded_rows = [
        row_cells + [""] * (column_count - len(row_cells)) for row_cells in row_cells_by_row
    ]
    header_line = "| " + " | ".join(padded_rows[0]) + " |"
    divider_line = "| " + " | ".join(["---"] * column_count) + " |"
    body_lines = ["| " + " | ".join(row_cells) + " |" for row_cells in padded_rows[1:]]
    return "\n".join([header_line, divider_line, *body_lines])


def _extract_inline_object_placeholders(paragraph_element: Any) -> list[str]:
    """Return marker strings for inline images and charts in a paragraph.

    For each ``<w:drawing>`` element in the paragraph:

    1. Determine content type by scanning descendants -- ``<c:chart>``
       means a chart, ``<a:blip>`` means an image.
    2. Extract accessibility alt-text from the drawing's ``<wp:docPr>``
       (``descr`` attribute preferred, ``title`` as fallback). This is
       the author's own description of the visual and is the closest
       thing the text pipeline gets to "seeing" the image.
    3. Compose a placeholder that (a) tells the LLM a visual IS present
       so it does not emit "missing diagram" false positives, and (b)
       embeds the alt-text when available so the scanner reasons about
       what the visual depicts.

    Order follows the XML tree, which is document order within the
    paragraph. A drawing that contains neither a chart nor an image
    (rare -- text-box only, e.g.) is skipped.
    """
    from docx.oxml.ns import qn

    drawing_element_tag = qn("w:drawing")
    docpr_element_tag = qn("wp:docPr")
    blip_element_tag = qn("a:blip")
    chart_element_tag = qn("c:chart")

    inline_markers: list[str] = []
    for drawing_element in paragraph_element.iter(drawing_element_tag):
        alt_text = _extract_docpr_alt_text(drawing_element, docpr_element_tag)
        contains_chart = next(drawing_element.iter(chart_element_tag), None) is not None
        contains_image = next(drawing_element.iter(blip_element_tag), None) is not None
        if contains_chart:
            inline_markers.append(_build_visual_placeholder("Chart", alt_text))
        elif contains_image:
            inline_markers.append(_build_visual_placeholder("Image", alt_text))
    return inline_markers


def _extract_docpr_alt_text(drawing_element: Any, docpr_element_tag: str) -> str:
    """Return alt-text embedded on a drawing's ``<wp:docPr>``, or ``""``.

    Prefers the long-form ``descr`` attribute (author's full description
    of the visual). Falls back to ``title`` (author's short label) when
    ``descr`` is empty. Whitespace-only values are treated as absent.
    A drawing without a ``<wp:docPr>`` child yields the empty string.
    """
    docpr_element = next(drawing_element.iter(docpr_element_tag), None)
    if docpr_element is None:
        return ""
    descr_attribute = (docpr_element.get("descr") or "").strip()
    if descr_attribute:
        return descr_attribute
    title_attribute = (docpr_element.get("title") or "").strip()
    return title_attribute


def _build_visual_placeholder(kind: str, alt_text: str) -> str:
    """Compose a scanner-facing placeholder for an inline image or chart.

    Strong wording tells the LLM explicitly NOT to flag missing-visual
    findings on the surrounding prose -- one is already present. When
    the author supplied alt-text via ``<wp:docPr descr>`` we embed it
    verbatim so the scanner has actual content evidence instead of
    only knowing that some opaque visual exists.

    ``kind`` is user-facing ("Image" / "Chart"); lowercase is used
    inside the instruction sentence to read naturally.
    """
    kind_lower = kind.lower()
    # Pick the indefinite article correctly ("an image", "a chart") so
    # the placeholder reads as natural English rather than "A image".
    # The scanner would parse either shape fine, but the natural form
    # doesn't leak an amateur "why does this project write bad English"
    # signal into what the model sees.
    indefinite_article = "An" if kind_lower[:1] in "aeiou" else "A"
    if alt_text:
        return (
            f"[VISUAL: {alt_text}. {indefinite_article} {kind_lower} with "
            f"this description is embedded in the source document at this "
            f"location. The reviewer cannot see the raw pixels but the "
            f"reader can. Do NOT flag this section for missing "
            f"{kind_lower}s, diagrams, or figures -- one is already "
            f"present with the description above.]"
        )
    return (
        f"[VISUAL: {indefinite_article} {kind_lower} is embedded in the "
        f"source document at this location. The reviewer cannot see it "
        f"but the reader can. Do NOT flag this section for missing "
        f"{kind_lower}s, diagrams, or figures -- one is already present.]"
    )


def _join_paragraph_pieces(paragraph_text: str, inline_markers: list[str]) -> str:
    """Join paragraph text with any inline-object markers into one line.

    Markers appear AFTER the paragraph text so the LLM reads the
    surrounding prose first, then sees the placeholder. Empty paragraph
    text + markers = just markers; markers + no paragraph text yields
    the marker alone as a body line.
    """
    if paragraph_text and inline_markers:
        return paragraph_text + " " + " ".join(inline_markers)
    if paragraph_text:
        return paragraph_text
    if inline_markers:
        return " ".join(inline_markers)
    return ""


def _parse_markdown_or_text(document_path: Path) -> list[Section]:
    """Split a markdown/text document on ATX headings.

    A document with no headings collapses to a single :class:`Section` whose
    heading is empty and whose body is the full file. This mirrors how a
    document with a single H1 at the top would render -- callers can treat
    ``heading == ""`` as "no explicit section title given".
    """
    document_text = document_path.read_text(encoding="utf-8")

    sections: list[Section] = []
    last_body_end_offset = 0
    current_heading = ""

    for heading_match in _ATX_HEADING_PATTERN.finditer(document_text):
        body_before_heading = document_text[last_body_end_offset : heading_match.start()].strip()
        if current_heading or body_before_heading:
            sections.append(Section(heading=current_heading, body=body_before_heading))
        current_heading = heading_match.group(2).strip()
        last_body_end_offset = heading_match.end()

    trailing_body = document_text[last_body_end_offset:].strip()
    if current_heading or trailing_body:
        sections.append(Section(heading=current_heading, body=trailing_body))

    if not sections:
        return [Section(heading="", body=document_text.strip())]
    return sections


# --- Slice 2: scanner prompt building and parallel dispatch ------------------


def _app_root() -> Path:
    """Return the on-disk directory that holds this app's bundled files."""
    return Path(__file__).parent


def _load_scanner_brief(scanner_name: str) -> str:
    """Load a scanner brief markdown file from the app's ``scanners/`` dir.

    Missing briefs raise ``FileNotFoundError`` so the driver can flag the
    scanner as failed rather than silently produce zero findings.
    """
    scanner_brief_path = _app_root() / "scanners" / f"{scanner_name}.md"
    if not scanner_brief_path.is_file():
        raise FileNotFoundError(f"Scanner brief not found: {scanner_name}")
    return scanner_brief_path.read_text(encoding="utf-8")


_PROMPT_RESPONSE_FORMAT = """\
Respond with a JSON object containing a "findings" array. Each finding must have:
- section: the heading or section name where the issue appears
- paragraph: paragraph number within that section (1-indexed)
- issue: 2-3 sentences explaining what is wrong AND why it matters for the stated audience
- rule: which rule number from your brief this violates
- severity: "high", "medium", "low", or "advisory"
- proposed_fix: the complete rewritten text that should replace the flagged form (not a hint like "consider rephrasing")
- confidence: "high", "medium", or "low" -- how certain you are this is a genuine issue

Order your findings by severity, highest first: put every "high" finding at the top of
the array, then every "medium", then "low", then "advisory". Within the same severity,
order the findings so the most critical concern is listed first.

Return AT MOST 10 findings. If more than 10 issues exist in the document, drop the
lowest-severity ones so only the 10 most important remain in the response. Keep every
"high" and "medium" finding when they fit within the cap; drop "low" / "advisory"
first to make room.

If no issues are found, return {"findings": []}.
"""


def _scanner_prompt(
    *,
    scanner_name: str,
    scanner_brief: str,
    document_text: str,
    context: ReviewContext,
) -> str:
    """Assemble the full prompt for a single scanner.

    Layout is deterministic so the LLM sees the same structural cues on
    every call: context block first (audience/type/tone + additional_context),
    then the scanner brief, then the document, then the JSON response
    contract.
    """
    context_lines = [
        "Document context:",
        f"- Audience: {context.audience or 'not specified'}",
        f"- Document type: {context.doc_type or 'not specified'}",
        f"- Tone: {context.tone or 'not specified'}",
    ]
    if context.ask:
        # Emit the ask as a standalone directive line rather than another
        # bullet under "Document context:". This wording ("focused on
        # ...") gives the LLM a clear intent signal to weight findings
        # by relevance to the author's concern. Absent ask stays absent
        # from the prompt: a "not specified" filler would push the model
        # to reason about why the ask is missing.
        context_lines.append("")
        context_lines.append(f"The author is asking for review focused on: {context.ask}")
    if context.additional_context:
        context_lines.append("")
        # The user-facing label is "Additional context (one note per line)"
        # in ``NewReviewDialog``. Wording here matches: users may enter
        # both "don't-flag exceptions" (``FY2025 is correct``) AND
        # general framing notes (``written for the CFO``, ``follow-up
        # to last week's review``). The LLM infers per-line intent
        # from content; an old "do NOT flag these" instruction would
        # over-suppress on the framing-note case.
        context_lines.append("Author's additional context:")
        for context_line_text in context.additional_context:
            context_lines.append(f"- {context_line_text}")
    context_block = "\n".join(context_lines)

    return (
        f"{context_block}\n\n"
        f"---\n\n"
        f"{scanner_brief}\n\n"
        f"---\n\n"
        f"## Document to review\n\n"
        f"{document_text}\n\n"
        f"---\n\n"
        f"{_PROMPT_RESPONSE_FORMAT}"
    )


def _resolve_scanners(
    context: ReviewContext,
    scanner_toggles: dict[str, bool] | None = None,
) -> list[str]:
    """Choose which scanners to run based on doc type and user toggles.

    Always-on scanners come first, in declared order. Conditional scanners
    are appended only when a matching ``doc_type`` substring is present.
    User toggles apply last: an explicit ``False`` removes a scanner from
    the run even if it was otherwise selected.
    """
    resolved_scanner_names: list[str] = list(ALWAYS_ON_SCANNERS)
    doc_type_lower = (context.doc_type or "").lower()
    for trigger_substring, scanner_name in CONDITIONAL_SCANNERS.items():
        if trigger_substring in doc_type_lower and scanner_name not in resolved_scanner_names:
            resolved_scanner_names.append(scanner_name)
    if scanner_toggles:
        resolved_scanner_names = [
            scanner_name
            for scanner_name in resolved_scanner_names
            if scanner_toggles.get(scanner_name, True)
        ]
    return resolved_scanner_names


def finding_id(scanner: str, section: str, paragraph: int, rule: str) -> str:
    """Return a stable 12-char hex ID for a finding.

    Deterministic on scanner + location + rule so the same finding
    surfaced by two review runs shares an ID. Used by the "dismiss this
    finding" persistence path in Slice 5.
    """
    raw_key = f"{scanner}:{section}:{paragraph}:{rule}"
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()[:12]


def _coerce_finding(scanner_name: str, raw_finding: dict[str, Any]) -> Finding:
    """Turn one JSON finding object from the LLM into a :class:`Finding`.

    Missing fields default to safe values so a partially-formed response
    still yields a usable finding rather than raising. Severity and
    confidence are clamped to the allowed vocabulary.
    """
    section_name = str(raw_finding.get("section", ""))
    paragraph_raw = raw_finding.get("paragraph", 0)
    # LLMs sometimes emit non-numeric paragraph values ("N/A", "1-2",
    # "unknown"). Salvage the first integer if present, otherwise 0 --
    # a bad paragraph number should not fail the whole scanner.
    try:
        paragraph_number = int(paragraph_raw) if paragraph_raw else 0
    except (TypeError, ValueError):
        match = re.search(r"\d+", str(paragraph_raw))
        paragraph_number = int(match.group()) if match else 0
    rule_number = str(raw_finding.get("rule", ""))
    severity_value = str(raw_finding.get("severity", "medium")).lower()
    if severity_value not in _SEVERITY_VALUES:
        severity_value = "medium"
    confidence_value = str(raw_finding.get("confidence", "medium")).lower()
    if confidence_value not in ("high", "medium", "low"):
        confidence_value = "medium"
    return Finding(
        id=finding_id(scanner_name, section_name, paragraph_number, rule_number),
        scanner=scanner_name,
        section=section_name,
        paragraph=paragraph_number,
        issue=str(raw_finding.get("issue", "")),
        rule=rule_number,
        severity=severity_value,
        proposed_fix=str(raw_finding.get("proposed_fix", "")),
        confidence=confidence_value,
    )


# A stable marker embedded in the synthesis prompt so tests (and future
# audit logging) can distinguish scanner calls from the cross-validation
# call without having to guess from prompt content.
_SYNTHESIS_PROMPT_MARKER = "[WRITING-REVIEW SYNTHESIS PASS]"


def _finding_to_dict(finding: Finding) -> dict[str, Any]:
    """Serialise a :class:`Finding` for the synthesis prompt and JSON store."""
    return {
        "id": finding.id,
        "scanner": finding.scanner,
        "section": finding.section,
        "paragraph": finding.paragraph,
        "issue": finding.issue,
        "rule": finding.rule,
        "severity": finding.severity,
        "proposed_fix": finding.proposed_fix,
        "cross_validation": finding.cross_validation,
        "conflicts": list(finding.conflicts),
        "confidence": finding.confidence,
    }


def _synthesis_prompt(
    *,
    document_text: str,
    findings: list[Finding],
) -> str:
    """Build the cross-validation prompt that tags findings as redundant or clean.

    This is a focused tagging pass — not an editorial summary. The model
    receives all findings plus the source document and returns a JSON array
    tagging each finding as clean, conflicts, or redundant. Redundant findings
    are filtered out before the results reach the user.
    """
    findings_json = json.dumps(
        [_finding_to_dict(finding) for finding in findings],
        indent=2,
    )
    response_format = (
        "\nReturn a JSON object with a `results` array. Each entry must have:\n"
        "- id: the finding id from the input\n"
        '- cross_validation: one of "clean", "conflicts", "redundant"\n'
        "- conflicts: array of strings explaining any conflict (empty when clean/redundant)\n"
        '- primary_id: REQUIRED when cross_validation is "redundant"; the id of the\n'
        "  finding this one duplicates. Point at the finding you tagged clean --\n"
        "  the highest-severity, most actionable instance of the same underlying issue.\n"
        "  Downstream collation attaches this redundant finding's location under the\n"
        '  primary so the user sees ONE card with an "Also appears in" list, instead\n'
        "  of N cards for the same problem. Omit or leave empty for clean/conflicts.\n"
        "\n"
        "## Tagging rules\n"
        "\n"
        "The key heuristic: would fixing finding A automatically fix finding B?\n"
        "If yes, B is redundant. If no, both are clean.\n"
        "\n"
        'Tag "redundant" when ANY of these are true:\n'
        "1. Two findings target the same sentence or paragraph for related reasons — keep\n"
        "   the higher-severity one, tag the other redundant.\n"
        "2. Two findings describe the same underlying problem from different scanner angles —\n"
        "   e.g. attribution says 'wrong name' and consistency says 'inconsistent naming'\n"
        "   about the same entity. Keep the one whose fix addresses the root cause.\n"
        "3. Multiple findings trace to the same root cause — e.g. 5 'no quantification'\n"
        "   findings across different sections all exist because the document has zero\n"
        "   numbers. Keep the ONE highest-severity instance. Tag the rest redundant.\n"
        "4. A systematic gap that manifests across multiple sections — the same scanner\n"
        "   flags the same rule violation in 3+ different paragraphs because the author\n"
        "   made one editorial decision (e.g. 'never quantify anything'). The author\n"
        "   fixes these in one pass with one decision, not paragraph by paragraph.\n"
        "   Keep the single highest-severity instance. Tag the rest redundant.\n"
        "   Key signal: same scanner + same rule + different paragraphs = almost always\n"
        "   a systematic gap, not separate issues.\n"
        "\n"
        'Tag "conflicts" when two findings give opposing advice — applying one fix would\n'
        "violate the other scanner's rule. Both survive; the user decides.\n"
        "\n"
        'Tag "clean" for everything else.\n'
        "\n"
        "Be aggressive. Target: 25-35 findings per document. If your output exceeds 40\n"
        "findings with fewer than 5 tagged redundant, you have almost certainly under-tagged.\n"
        "Re-examine findings from the same scanner that target different paragraphs for the\n"
        "same rule — those are Pattern 4 (systematic gap). When in doubt, choose redundant.\n"
        "\n"
        "## Examples\n"
        "\n"
        "### Same root cause across sections → tag redundant\n"
        "\n"
        "Findings:\n"
        '  - id: "evidence_nonfunctional_4_2", scanner: "evidence", issue: "high latency with no threshold"\n'
        '  - id: "design_nonfunctional_3_2", scanner: "design", issue: "NFR section has no quantified targets"\n'
        "\n"
        "Both target the same section for the same reason. One fix (add the numbers) resolves both.\n"
        "Correct: design_nonfunctional_3_2 = clean, evidence_nonfunctional_4_2 = redundant.\n"
        "\n"
        "### Same concept from two scanners → tag redundant\n"
        "\n"
        "Findings:\n"
        '  - id: "consistency_crossdocument_0_1", scanner: "consistency", issue: "Uses MVC in glossary but MVP in body"\n'
        '  - id: "structure_architecture_4_5", scanner: "structure", issue: "MVP used before defined, inconsistent with glossary"\n'
        "\n"
        "Same terminology drift. One find-and-replace resolves both.\n"
        "Correct: consistency_crossdocument_0_1 = clean, structure_architecture_4_5 = redundant.\n"
        "\n"
        "### Multiple instances of one pattern → keep archetype, tag rest\n"
        "\n"
        "Findings:\n"
        '  - id: "attribution_architecture_3_1", issue: "conflates PaymentClient module with external Payment Gateway service"\n'
        '  - id: "attribution_errorhandling_1_1", issue: "503 error blames client module when failure is in downstream gateway"\n'
        '  - id: "attribution_implementation_3_1", issue: "mixes module name and service name interchangeably"\n'
        "\n"
        "All three are the same confusion in different paragraphs. Fix the mental model once.\n"
        "Correct: attribution_architecture_3_1 = clean, other two = redundant.\n"
        "\n"
        "### KEEP — looks similar but genuinely distinct\n"
        "\n"
        "Findings:\n"
        '  - id: "structure_problem_1_2", scanner: "structure", issue: "Problem statement has no cost-of-inaction"\n'
        '  - id: "evidence_problem_6_1", scanner: "evidence", issue: "No data on how many users affected"\n'
        "\n"
        "Different fixes: structure needs a consequences sentence, evidence needs metrics.\n"
        "Fixing one does NOT fix the other. Both = clean.\n"
        "\n"
        "### KEEP — same scanner, different fix\n"
        "\n"
        "Findings:\n"
        '  - id: "audience_glossary_1_4", issue: "Glossary over-explains terms the team uses daily"\n'
        '  - id: "audience_document_1_5", issue: "Document serves two audiences but never signposts sections"\n'
        "\n"
        "Different fixes: trim glossary vs add reader orientation note. Neither resolves the other.\n"
        "Both = clean.\n"
        "\n"
        "### Systematic gap — same scanner, same rule, multiple paragraphs → tag redundant\n"
        "\n"
        "Findings:\n"
        '  - id: "evidence_nfr_4_1", scanner: "evidence", rule: "1", section: "Non-Functional", issue: "latency alarm has no threshold"\n'
        '  - id: "evidence_stress_1_1", scanner: "evidence", rule: "1", section: "stress testing", issue: "no pass/fail criteria"\n'
        '  - id: "evidence_monitoring_5_1", scanner: "evidence", rule: "1", section: "Monitoring Plan", issue: "vague alarm windows"\n'
        '  - id: "evidence_problem_6_1", scanner: "evidence", rule: "1", section: "Problem statement", issue: "no scale data"\n'
        "\n"
        "All four are evidence rule 1 ('claims need data') in different sections. The author's\n"
        "single editorial decision is 'go through the document and add numbers.' They do not need\n"
        "to be told four times. Keep the highest-severity one; tag the rest redundant pointing at it.\n"
        "Correct: evidence_nfr_4_1 = clean (high severity, most actionable), other three = redundant.\n"
        "\n"
        'If nothing needs updating return {"results": []}.\n'
    )
    task_instruction = (
        "You are the cross-validation pass for a writing review tool. Your ONLY job is to\n"
        "tag each finding below as clean, conflicts, or redundant. You do NOT produce a\n"
        "summary, editorial, or rewrite. You return ONLY the tagging JSON.\n"
    )
    return (
        f"{_SYNTHESIS_PROMPT_MARKER}\n\n"
        f"{task_instruction}\n\n"
        f"---\n\n"
        f"## Source document\n\n{document_text}\n\n"
        f"---\n\n"
        f"## Scanner findings (JSON)\n\n{findings_json}\n\n"
        f"---\n\n"
        f"{response_format}"
    )


async def _cross_validate_findings(
    *,
    findings: list[Finding],
    document_text: str,
) -> list[Finding]:
    """Run the synthesis meta-scan and merge results back into findings.

    Dispatches through the module-level scanner pool. Synthesis is
    best-effort: a malformed or missing response is logged and left to
    fall through, because it makes the review LESS useful but never
    wrong. Only entries whose ``id`` matches a known finding are applied,
    so a hallucinated ID never invents metadata.
    """
    if not findings:
        return findings

    prompt = _synthesis_prompt(
        document_text=document_text,
        findings=findings,
    )
    scanner_pool = get_pool()
    try:
        raw_response = await scanner_pool.dispatch(prompt)
    except Exception:  # pragma: no cover - defensive; pool surfaces errors
        logger.exception("Synthesis pass raised; leaving findings unmerged")
        return findings

    if not isinstance(raw_response, dict):
        logger.warning("Synthesis returned non-object payload; skipping merge")
        return findings
    results_list = raw_response.get("results")
    if not isinstance(results_list, list):
        logger.warning("Synthesis payload missing results array; skipping merge")
        return findings

    findings_by_id: dict[str, Finding] = {finding.id: finding for finding in findings}
    for result_entry in results_list:
        if not isinstance(result_entry, dict):
            continue
        target_finding = findings_by_id.get(str(result_entry.get("id", "")))
        if target_finding is None:
            continue
        cross_validation_value = str(result_entry.get("cross_validation", "clean")).lower()
        if cross_validation_value in _CROSS_VALIDATION_VALUES:
            target_finding.cross_validation = cross_validation_value
        conflicts_list = result_entry.get("conflicts", [])
        if isinstance(conflicts_list, list):
            target_finding.conflicts = [str(entry) for entry in conflicts_list]
        # Slice 2: capture the primary id when the finding is tagged
        # redundant. Downstream collation walks this chain to resolve
        # each redundant finding to its terminal primary. Empty string
        # when the LLM did not name a primary -- caller treats as orphan.
        primary_id_value = result_entry.get("primary_id", "")
        if isinstance(primary_id_value, str):
            target_finding.primary_id = primary_id_value

    return findings


async def _run_one_scanner(
    *,
    scanner_name: str,
    document_text: str,
    context: ReviewContext,
) -> list[Finding]:
    """Run a single scanner and return its findings.

    Dispatches through the module-level scanner pool (isolated ACP session
    per call) rather than the caller's provider. Raises on any failure that
    should count against the ``partial_failure`` quorum: missing brief, pool
    error, or the response was not a JSON object with a ``findings`` list.
    """
    scanner_brief = _load_scanner_brief(scanner_name)
    prompt = _scanner_prompt(
        scanner_name=scanner_name,
        scanner_brief=scanner_brief,
        document_text=document_text,
        context=context,
    )
    scanner_pool = get_pool()
    first_attempt_partial: list[dict[str, Any]] = []
    try:
        raw_response = await scanner_pool.dispatch(prompt)
    except _TruncatedResponseError as first_truncation:
        # First attempt hit the model's output-token ceiling. Layer 1 has
        # already extracted every complete finding object from the raw
        # response into ``.partial_findings`` -- salvage them before the
        # retry so nothing the model already emitted is lost. Layer 4
        # merges these with whatever the retry produces (with a stricter
        # cap so the second response fits comfortably); ``id``-keyed
        # dedup drops any finding the retry re-emits verbatim.
        first_attempt_partial = list(first_truncation.partial_findings)
        logger.warning(
            "Scanner %s truncated on first attempt (salvaged %d); retrying with stricter cap",
            scanner_name,
            len(first_attempt_partial),
        )
        try:
            raw_response = await scanner_pool.dispatch(prompt + _TRUNCATION_RETRY_SUFFIX)
        except _TruncatedResponseError as second_truncation:
            # If the retry ALSO truncates, we still want to preserve every
            # finding either attempt salvaged. Merge both partial lists,
            # attach the union to a fresh ``TruncatedResponseError``, and
            # re-raise so the driver's failed_scanners recorder still fires
            # -- the caller sees ``truncated_response`` in the failure banner
            # but the salvage did not go to waste.
            merged_partial = _merge_findings_dicts_by_id(
                scanner_name,
                first_attempt_partial,
                list(second_truncation.partial_findings),
            )
            raise _TruncatedResponseError(
                str(second_truncation),
                partial_findings=merged_partial,
            ) from second_truncation
    if not isinstance(raw_response, dict):
        raise RuntimeError(f"Scanner {scanner_name} returned no parseable JSON")
    raw_findings = raw_response.get("findings")
    if not isinstance(raw_findings, list):
        raise RuntimeError(f"Scanner {scanner_name} response missing findings list")
    if first_attempt_partial:
        # Layer 4: merge first-attempt salvage with retry findings, keyed by
        # ``id`` so an overlap only counts once. The retry's fresh output is
        # authoritative for overlapping ids -- the retry ran under a stricter
        # prompt and its rendering is what the caller asked for.
        merged_raw_findings = _merge_findings_dicts_by_id(
            scanner_name,
            first_attempt_partial,
            raw_findings,
        )
        coerced_findings = [
            _coerce_finding(scanner_name, raw_finding) for raw_finding in merged_raw_findings
        ]
    else:
        coerced_findings = [
            _coerce_finding(scanner_name, raw_finding) for raw_finding in raw_findings
        ]
    return _cap_findings_by_severity(coerced_findings, MAX_FINDINGS_PER_SCANNER)


def _merge_findings_dicts_by_id(
    scanner_name: str,
    first_attempt_findings: list[dict[str, Any]],
    second_attempt_findings: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Merge two raw finding lists, keyed by the deterministic ``finding_id``.

    Order: first attempt's findings first (preserving the model's original
    ranking within that pass), then any second-attempt finding whose id was
    not already seen. Ids are computed from ``(scanner_name, section,
    paragraph, rule)`` exactly like :func:`_coerce_finding` does downstream,
    so an overlap deduplicates whether it appears in the first pass, the
    second pass, or both.

    Second-attempt findings for ids already seen are dropped, not
    replaced: the salvage pass captured what the model actually wrote
    (highest-severity ordering the base prompt asked for) before it hit
    the ceiling; the stricter retry re-emits a subset that would only
    reorder the merged list.
    """
    seen_ids: set[str] = set()
    merged: list[dict[str, Any]] = []
    for finding_dict in list(first_attempt_findings) + list(second_attempt_findings):
        section_name = str(finding_dict.get("section") or "")
        raw_paragraph_value = finding_dict.get("paragraph")
        try:
            paragraph_number = int(raw_paragraph_value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            paragraph_number = 0
        rule_number = str(finding_dict.get("rule") or "")
        candidate_id = finding_id(scanner_name, section_name, paragraph_number, rule_number)
        if candidate_id in seen_ids:
            continue
        seen_ids.add(candidate_id)
        merged.append(finding_dict)
    return merged


# Suffix appended to the scanner prompt on retry after a truncation. The
# cap here (5) is tighter than the base prompt's 10, chosen so the retry
# response comfortably fits within a Claude-default output-token ceiling
# on any doc size we support. Wording asks for the most severe findings
# rather than a random slice so users get signal, not noise, from what
# survives.
_TRUNCATION_RETRY_SUFFIX = (
    "\n\n---\n\n"
    "Your previous response was truncated (output too long). Retry with a"
    " STRICTER cap: return ONLY the top 5 findings, prioritising severity"
    " high → medium → low. Skip advisory findings entirely on this retry."
)


# Per-scanner ceiling on findings surfaced to the user. Model output-token
# limits regularly cut off ``design``-scale scanners on long docs; even if
# they did not, 20+ findings per scanner is more work than any reviewer can
# realistically act on. The cap here enforces the same ceiling regardless
# of how much the model actually emits — the prompt directive in
# ``_PROMPT_RESPONSE_FORMAT`` asks the model to self-limit, this filter
# is the guarantee.
MAX_FINDINGS_PER_SCANNER = 10

# Severity rank used by ``_cap_findings_by_severity`` when trimming. Higher
# rank = kept first when the cap bites. Unknown severities fall to the
# bottom so a corrupt entry cannot displace a real "high" finding.
_SEVERITY_RANK_FOR_CAP: dict[str, int] = {
    "high": 3,
    "medium": 2,
    "low": 1,
    "advisory": 0,
}


def _cap_findings_by_severity(findings: list[Finding], max_count: int) -> list[Finding]:
    """Return at most ``max_count`` findings, keeping the highest severities.

    Sort is stable and preserves the model's original ordering within each
    severity bucket, so a scanner that already ranked its findings by
    "most critical first" keeps that ordering after the trim. A cap of
    zero returns an empty list; a cap larger than the input passes the
    list through untouched (still sorted).
    """
    if max_count <= 0:
        return []
    ranked = sorted(
        findings,
        key=lambda finding: _SEVERITY_RANK_FOR_CAP.get(finding.severity, -1),
        reverse=True,
    )
    return ranked[:max_count]


PhaseCallback = Callable[[str, dict[str, Any]], None]


# reason_class vocabulary the frontend renders + the discussion agent quotes.
_FAILURE_REASON_PROVIDER_TIMEOUT = "provider_timeout"
_FAILURE_REASON_INVALID_JSON = "invalid_json"
_FAILURE_REASON_TRUNCATED_RESPONSE = "truncated_response"
_FAILURE_REASON_MISSING_BRIEF = "missing_brief"
_FAILURE_REASON_RATE_LIMITED = "rate_limited"
_FAILURE_REASON_WORKER_DIED = "worker_died"
_FAILURE_REASON_OTHER = "other"


def _classify_scanner_failure(exc: BaseException) -> str:
    """Map an exception raised during a scanner dispatch to a reason_class.

    Order matters: :class:`~pool.TruncatedResponseError` subclasses
    ``ValueError``, so we MUST check it before the ``(JSONDecodeError,
    ValueError)`` catch-all — otherwise a max-output-tokens truncation is
    mislabelled as generic ``invalid_json`` and the UI banner loses the
    "the model ran out of room" hint that tells an operator to retry with
    a smaller prompt or a shorter document.
    """
    if isinstance(exc, asyncio.TimeoutError):
        return _FAILURE_REASON_PROVIDER_TIMEOUT
    if isinstance(exc, _TruncatedResponseError):
        return _FAILURE_REASON_TRUNCATED_RESPONSE
    if isinstance(exc, (json.JSONDecodeError, ValueError)):
        return _FAILURE_REASON_INVALID_JSON
    if isinstance(exc, FileNotFoundError):
        return _FAILURE_REASON_MISSING_BRIEF
    error_message = str(exc).lower()
    if "rate" in error_message and "limit" in error_message:
        return _FAILURE_REASON_RATE_LIMITED
    if "worker" in error_message or "died" in error_message:
        return _FAILURE_REASON_WORKER_DIED
    return _FAILURE_REASON_OTHER


def _resolve_primary_chain(
    finding_id_lookup: str,
    findings_by_id: dict[str, Finding],
    seen_in_chain: set[str] | None = None,
) -> str:
    """Walk a redundant finding's ``primary_id`` chain to a terminal id.

    Returns the id of the first non-redundant finding reached, or
    ``""`` if the chain hits a missing id or a cycle. The empty return
    is the caller's signal that this finding is an orphan and should
    be preserved (with its ``cross_validation`` demoted to ``"clean"``)
    rather than collated.

    Cycles are protected by ``seen_in_chain``: if the walk revisits a
    finding it has already inspected, the chain is treated as broken.
    An LLM emitting an A -> B -> A cycle is misbehaving; we do not want
    to hang on it.
    """
    if seen_in_chain is None:
        seen_in_chain = set()
    if finding_id_lookup in seen_in_chain:
        return ""  # cycle -- treat as orphan
    seen_in_chain.add(finding_id_lookup)
    candidate_finding = findings_by_id.get(finding_id_lookup)
    if candidate_finding is None:
        return ""  # id not in scan -- orphan
    if candidate_finding.cross_validation != "redundant":
        return candidate_finding.id  # terminal primary
    next_primary_id = candidate_finding.primary_id or ""
    if not next_primary_id:
        return ""  # redundant but no primary named -- orphan
    return _resolve_primary_chain(next_primary_id, findings_by_id, seen_in_chain)


def _collate_redundant_findings(all_findings: list[Finding]) -> list[Finding]:
    """Collapse each redundant finding onto its primary's ``related_locations``.

    Two-pass:

    * Pass 1 builds a lookup and, for every redundant finding, resolves
      the terminal primary id via :func:`_resolve_primary_chain`. This
      flattens chains (A -> B -> C collates A onto C, not onto B).
    * Pass 2 iterates the original list in order: primary/conflicts/clean
      findings pass through untouched; redundant findings append a
      :class:`RelatedLocation` to their resolved primary and are dropped
      from the output list. Redundant findings whose chain does not
      terminate on a real primary (orphan) survive with
      ``cross_validation`` demoted to ``"clean"``.
    """
    findings_by_id: dict[str, Finding] = {finding.id: finding for finding in all_findings}
    # Pass 1: for each redundant finding, resolve its terminal primary id.
    terminal_primary_id_for: dict[str, str] = {}
    for finding_record in all_findings:
        if finding_record.cross_validation != "redundant":
            continue
        terminal_primary_id_for[finding_record.id] = _resolve_primary_chain(
            finding_record.primary_id or "",
            findings_by_id,
        )

    # Pass 2: build the collated output. Primary/conflicts/clean pass
    # through in original order (kept for the model's severity-ordered
    # ranking); redundants either collate onto their primary or survive
    # demoted-to-clean.
    collated_findings: list[Finding] = []
    collated_finding_ids: set[str] = set()
    number_of_redundants_absorbed = 0
    number_of_orphans_preserved = 0
    for finding_record in all_findings:
        if finding_record.cross_validation != "redundant":
            if finding_record.id not in collated_finding_ids:
                collated_findings.append(finding_record)
                collated_finding_ids.add(finding_record.id)
            continue
        terminal_primary_id = terminal_primary_id_for.get(finding_record.id, "")
        primary_finding = findings_by_id.get(terminal_primary_id) if terminal_primary_id else None
        if primary_finding is None:
            # Orphan: demote and keep. The demotion is what stops the UI
            # from showing a "redundant" tag with no counterpart.
            finding_record.cross_validation = "clean"
            if finding_record.id not in collated_finding_ids:
                collated_findings.append(finding_record)
                collated_finding_ids.add(finding_record.id)
            number_of_orphans_preserved += 1
            continue
        primary_finding.related_locations.append(
            RelatedLocation(
                section=finding_record.section,
                paragraph=finding_record.paragraph,
                scanner=finding_record.scanner,
                issue=finding_record.issue,
            )
        )
        number_of_redundants_absorbed += 1

    if number_of_redundants_absorbed or number_of_orphans_preserved:
        logger.info(
            "Collated %d redundant findings; preserved %d orphans (dedup collation)",
            number_of_redundants_absorbed,
            number_of_orphans_preserved,
        )
    return collated_findings


async def run_scan(
    *,
    doc_path: str | Path,
    context: ReviewContext,
    scanners: list[str] | None = None,
    scanner_toggles: dict[str, bool] | None = None,
    dismissed_ids: list[str] | None = None,
    on_phase: PhaseCallback | None = None,
    review_id: str = "",
) -> ScanResult:
    """Parse a document, dispatch every applicable scanner, collect findings.

    Every scanner call runs on its own isolated ACP session via the process-wide
    scanner pool, so the user's active chat context is never consumed by the
    review. Each dispatch is wrapped in timing; a failure builds a structured
    :class:`FailedScanner` record instead of a bare scanner name.
    """
    doc_path = Path(doc_path)
    sections = parse_doc(doc_path)
    document_text = "\n\n".join(
        f"## {section.heading}\n{section.body}" if section.heading else section.body
        for section in sections
    )

    if scanners is None:
        scanners = _resolve_scanners(context, scanner_toggles)

    if on_phase:
        on_phase("fetch", {"sections": len(sections), "chars": len(document_text)})

    # Pull the current concurrency setting so a settings PATCH takes effect on
    # the next scan without a gateway restart. Falls back to pool default when
    # the setting is missing or the store isn't reachable (e.g. under tests).
    scanner_pool = get_pool()
    try:
        from kiro_crew.apps.builtins.writing_review.backend import store as _store_module

        current_settings = _store_module.load_settings()
        setting_max_concurrent = int(current_settings.get("max_concurrent", 0) or 0)
        if setting_max_concurrent > 0:
            scanner_pool.resize(setting_max_concurrent)
    except Exception:  # noqa: BLE001 - best-effort; keep default on failure
        logger.debug("settings load failed; using default max_concurrent", exc_info=True)

    await scanner_pool.begin_batch()

    async def _dispatch_with_timing(
        scanner_name: str,
    ) -> tuple[str, list[Finding], FailedScanner | None]:
        start_time = asyncio.get_running_loop().time()
        try:
            findings_for_scanner = await _run_one_scanner(
                scanner_name=scanner_name,
                document_text=document_text,
                context=context,
            )
            if on_phase:
                on_phase("scanner", {"name": scanner_name, "found": len(findings_for_scanner)})
            return scanner_name, findings_for_scanner, None
        except asyncio.CancelledError:
            # Cancellation must propagate — swallowing it would strand the
            # subprocess and defeat task-cancellation semantics for the
            # whole gather() below. Timing on cancelled work is not
            # meaningful.
            raise
        except Exception as scanner_error:
            duration_ms = int((asyncio.get_running_loop().time() - start_time) * 1000)
            logger.warning("Scanner %s failed: %s", scanner_name, scanner_error)
            failed_record = FailedScanner(
                name=scanner_name,
                reason_class=_classify_scanner_failure(scanner_error),
                message=str(scanner_error),
                at=datetime.now(timezone.utc).isoformat(),
                duration_ms=duration_ms,
            )
            return scanner_name, [], failed_record

    try:
        dispatch_results = await asyncio.gather(
            *[_dispatch_with_timing(scanner_name) for scanner_name in scanners]
        )
    finally:
        # end_batch drops the runtime ref count -- last scan out kills the runtime.
        await scanner_pool.end_batch()

    all_findings: list[Finding] = []
    failed_scanners: list[FailedScanner] = []
    for _scanner_name, scanner_findings, failed_record in dispatch_results:
        if failed_record is not None:
            failed_scanners.append(failed_record)
            continue
        all_findings.extend(scanner_findings)

    # Slice 3 deterministic post-processing: collapse duplicates, drop
    # findings the author has already ruled on via additional_context, filter
    # anything the user dismissed in a prior review.
    from kiro_crew.apps.builtins.writing_review.deterministic import (
        compute_verdict,
        dedup_findings,
        match_additional_context,
    )

    all_findings = dedup_findings(all_findings)

    # Slice 4: cross-validate the deduped findings with a single synthesis
    # pool call. Best-effort: synthesis metadata is a bonus, not a gate.
    if all_findings:
        if on_phase:
            on_phase("cross_validate", {"findings": len(all_findings)})
        all_findings = await _cross_validate_findings(
            findings=all_findings,
            document_text=document_text,
        )
        # Collate ``redundant`` findings onto their primary instead of
        # dropping them (Slice 2 of the dedup collation spec). The user
        # sees ONE card per underlying issue with an "Also appears in"
        # list, rather than N cards for the same problem. ``conflicts``
        # findings survive untouched; those are genuine tensions and
        # both sides need to reach the user. ``clean`` findings pass
        # through unchanged.
        #
        # Two-pass resolution:
        # * Pass 1 walks each redundant finding's ``primary_id`` chain
        #   until it lands on a non-redundant finding. This flattens
        #   A -> B -> C chains so A collates directly onto C, not
        #   stranded pointing at B (which itself was redundant).
        # * Pass 2 applies the collation: each redundant finding is
        #   removed from the output and its location appended to the
        #   terminal primary's ``related_locations``.
        #
        # Orphan protection: if a redundant finding's ``primary_id`` is
        # missing, empty, or names an id that does not exist in this
        # scan, the finding survives -- but its ``cross_validation`` is
        # demoted from ``"redundant"`` to ``"clean"``. Leaving it as
        # redundant would leak an inert tag into the UI whose referent
        # nothing consumes. We never silently drop signal.
        all_findings = _collate_redundant_findings(all_findings)

    if context.additional_context:
        all_findings = match_additional_context(all_findings, context.additional_context)

    if dismissed_ids:
        dismissed_id_set = set(dismissed_ids)
        all_findings = [finding for finding in all_findings if finding.id not in dismissed_id_set]

    # ``partial_failure`` fires when ANY scanner failed. The field name signals
    # "the scan was partial" -- any failure means the user is seeing an
    # incomplete picture, not just a majority-fail case. The UI banner logic
    # already checks ``failed_scanners.length > 0`` too, so this stays
    # consistent with what the frontend renders.
    partial_failure = len(failed_scanners) > 0
    verdict = compute_verdict(all_findings)

    log_search_hint = f"review_id={review_id}" if review_id else f"doc={doc_path.name}"
    log_reference = {"path": "~/.kiro/crew/gateway.log", "search_hint": log_search_hint}

    # Terminal state is the caller's responsibility -- ``_run_scan_job`` writes
    # ``status="done"`` (with the review_id) directly onto the job record after
    # this function returns. Emitting ``on_phase("done", ...)`` here would
    # schedule a fire-and-forget ``status="running"`` write via the backend's
    # ``create_task`` in the phase callback, and that queued task can execute
    # AFTER the completion write, clobbering it. The monotonic guard in
    # ``_record_job_state`` catches this class of race defensively; not making
    # the call at all removes the specific offender so the guard never has to
    # fire in production.

    return ScanResult(
        doc_path=str(doc_path),
        doc_name=_display_doc_name(doc_path.name),
        doc_context=context,
        sections=sections,
        findings=all_findings,
        verdict=verdict,
        scanners_run=scanners,
        partial_failure=partial_failure,
        failed_scanners=failed_scanners,
        log_reference=log_reference,
    )


# --- Slice 6: artifact integration -----------------------------------------


# Text severity labels used inside artifact comment bodies. Emojis are
# forbidden in the Kiro Crew UI so severity is communicated with an
# unambiguous prefix instead; lucide-react icons render in the
# frontend, but the comment body is markdown so it stays text-only.
_SEVERITY_LABEL: dict[str, str] = {
    "high": "HIGH",
    "medium": "MEDIUM",
    "low": "LOW",
    "advisory": "ADVISORY",
}


def build_comment_body(finding: Finding) -> str:
    """Build the markdown body posted as an inline artifact comment.

    Layout:
        **[SEVERITY] Scanner - Rule N**

        Issue text.

        **Proposed fix:**
        > Suggested rewrite.

        **Cross-validation:**
        - Conflict note (only when synthesis flagged one).
    """
    severity_label = _SEVERITY_LABEL.get(finding.severity, finding.severity.upper())
    body_parts: list[str] = [
        f"**[{severity_label}] {finding.scanner.title()} - Rule {finding.rule}**",
        "",
        finding.issue,
    ]
    if finding.proposed_fix:
        body_parts.extend(["", "**Proposed fix:**", f"> {finding.proposed_fix}"])
    if finding.conflicts:
        body_parts.extend(["", "**Cross-validation:**"])
        for conflict_note in finding.conflicts:
            body_parts.append(f"- {conflict_note}")
    return "\n".join(body_parts)


# Anchor lengths tuned for the Kiro Crew ``CommentAnchor`` selector: a
# 20-160 char quote is precise enough to survive minor edits and short
# enough not to swallow whole paragraphs.
_ANCHOR_QUOTE_MAX_CHARS = 160
_ANCHOR_CONTEXT_CHARS = 40


def build_comment_anchor(finding: Finding, document_text: str) -> dict[str, Any]:
    """Build a ``CommentAnchor``-shaped dict for :func:`artifact_post_comment`.

    The returned dict has three keys: ``quote`` (the exact substring the
    comment anchors to), ``prefix`` (a short slice of preceding text used
    to disambiguate duplicates), and ``suffix`` (a short slice of
    following text, same purpose). When the finding's section cannot be
    located in the document we fall back to anchoring on the first
    non-empty line so the comment still lands somewhere reasonable.
    """
    anchor_target_text = _pick_anchor_target(finding, document_text)
    if not anchor_target_text:
        return {"quote": "", "prefix": "", "suffix": ""}

    quote_snippet = anchor_target_text[:_ANCHOR_QUOTE_MAX_CHARS]
    anchor_start_index = document_text.find(quote_snippet)
    if anchor_start_index < 0:
        return {"quote": quote_snippet, "prefix": "", "suffix": ""}

    prefix_start_index = max(0, anchor_start_index - _ANCHOR_CONTEXT_CHARS)
    suffix_end_index = min(
        len(document_text),
        anchor_start_index + len(quote_snippet) + _ANCHOR_CONTEXT_CHARS,
    )
    return {
        "quote": quote_snippet,
        "prefix": document_text[prefix_start_index:anchor_start_index],
        "suffix": document_text[anchor_start_index + len(quote_snippet) : suffix_end_index],
    }


def _pick_anchor_target(finding: Finding, document_text: str) -> str:
    """Find the paragraph the finding refers to within the document text."""
    if finding.section:
        section_header_positions = [
            document_text.find(f"# {finding.section}"),
            document_text.find(f"## {finding.section}"),
            document_text.find(f"### {finding.section}"),
        ]
        first_matching_position = next(
            (position for position in section_header_positions if position >= 0),
            -1,
        )
        if first_matching_position >= 0:
            after_header_text = document_text[first_matching_position:]
            paragraphs_in_section = [
                paragraph.strip()
                for paragraph in after_header_text.split("\n\n")
                if paragraph.strip()
            ]
            paragraph_index = max(0, finding.paragraph - 1)
            if paragraph_index < len(paragraphs_in_section):
                candidate_paragraph = paragraphs_in_section[paragraph_index]
                # Skip the heading line itself when returning body paragraphs.
                if candidate_paragraph.startswith("#"):
                    if paragraph_index + 1 < len(paragraphs_in_section):
                        return paragraphs_in_section[paragraph_index + 1]
                return candidate_paragraph
    # Fall back to the first non-empty paragraph in the document so the
    # comment still anchors somewhere.
    for paragraph in document_text.split("\n\n"):
        paragraph_stripped = paragraph.strip()
        if paragraph_stripped and not paragraph_stripped.startswith("#"):
            return paragraph_stripped
    return ""


# --- Package-level re-export -------------------------------------------------
# The gateway startup loop imports ``kiro_crew.apps.builtins.<name>`` and
# checks the package for a ``register_routes`` attribute
# (``dashboard/routes/system.py:145-149``). Without this line the routes for
# this app never mount and every /api/apps/writing-review/* request 404s
# silently. The import lives at the BOTTOM of __init__.py so ``backend.routes``
# can import ReviewContext / run_scan back out of this module without a
# circular-import error at package load.
from kiro_crew.apps.builtins.writing_review.backend.routes import (  # noqa: E402,F401
    register_routes,
)
