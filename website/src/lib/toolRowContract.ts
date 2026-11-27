/**
 * Wire contract for the lead word of a `tool` row's content.
 *
 * The gateway writes one `tool` row per call (`Tool: <title>`) and, for some
 * calls, a hidden sibling sharing its `tool_call_id`: `Approved: <title>` when
 * the person approved it, `Refused: <title> — <reason>` when it did not run.
 * Readers key on the lead to tell the drawn pill from its hidden siblings.
 *
 * Older gateways led the same rows with a glyph (U+1F527 wrench, U+2705 check,
 * U+1F6AB no-entry), and saved history keeps those forever, so every reader
 * accepts both spellings. The backend twin is `kiro_crew/tool_row_prefix.py`;
 * the two lists must change together.
 */
export type ToolRowKind = 'call' | 'approved' | 'refused'

// Held as a regex, not string constants: these are wire values matched
// byte-for-byte against the backend, never copy, so they stay out of the
// i18n catalog (the same reason `denyReason.ts` keeps its marker a regex).
// Group 1 is a call lead, 2 an approved lead, 3 a refused lead.
const LEAD_RE = /^(?:(Tool:|\u{1F527})|(Approved:|\u2705)|(Refused:|\u{1F6AB}))/u

function leadOf(content: string | null | undefined): readonly [string, ToolRowKind] | null {
  if (!content) return null
  const m = LEAD_RE.exec(content)
  if (!m) return null
  const kind: ToolRowKind = m[1] ? 'call' : m[2] ? 'approved' : 'refused'
  return [m[0], kind]
}

/** Which tool row this content is, from its lead, or null for none. */
export function toolRowKind(content: string | null | undefined): ToolRowKind | null {
  return leadOf(content)?.[1] ?? null
}

/** True for the drawn call row (`Tool: …`, or the old wrench lead). */
export function isToolCallRow(content: string | null | undefined): boolean {
  return toolRowKind(content) === 'call'
}

/** True for the hidden refusal sibling (`Refused: …`, or the old no-entry lead). */
export function isToolRefusedRow(content: string | null | undefined): boolean {
  return toolRowKind(content) === 'refused'
}

/** The content with its lead and the whitespace after it removed. */
export function stripToolRowLead(content: string | null | undefined): string {
  const text = content ?? ''
  const lead = leadOf(text)
  return lead ? text.slice(lead[0].length).replace(/^\s+/, '') : text
}

/**
 * Only the call lead removed: the form the label readers want, which leave an
 * `Approved:` or `Refused:` row's text as it is (they only ever read call rows,
 * and the old code stripped only the wrench).
 */
export function stripToolCallLead(content: string | null | undefined): string {
  const text = content ?? ''
  return isToolCallRow(text) ? stripToolRowLead(text) : text
}
