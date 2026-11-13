// Canonical keep-visible marker — the invisible intent tag an agent appends to a
// SUBSTANTIVE MID-TURN message (a report, synthesis, or results table that is not
// the turn's final message) so collapse-all mode does not fold it into the
// "Worked through N steps" pane (#7948).
//
// The marker is an HTML comment, so the markdown renderer (rehypeRaw parses it
// into a hast comment node, which the react renderer skips) shows nothing: the
// exemption costs no visible ink. HTML-comment control tags are an established
// convention in this codebase (the heartbeat's `<!-- deliver:... -->` routing
// tags), and gating visibility on an explicit intent marker rather than a size
// heuristic follows the [OPTIONS:] hand-back precedent documented in
// TurnBlock.tsx — "hide intermediate reasoning" is a user preference, so only a
// direct signal of agent intent may override it.
//
// g-flagged for the same reason as OPTION_MARKER_RE: `String#replace` (which
// resets lastIndex) is the one safe direct use. Probe for presence via
// hasKeepVisibleMarker below — never call .test()/.exec() on this shared const,
// both leave lastIndex advanced and the next reader scans from the wrong offset.
//
// TAIL-ANCHORED: matches only a standalone marker line at the END of the
// message (the emission contract — agents append it as the message's final
// line). A literal `<!-- keep-visible -->` quoted inside a code block or
// mid-message prose is VISIBLE rendered content, not a control tag: it must
// not trigger the exemption, and stripping it from search/copy text would
// corrupt what the user sees. The leading `(?:^|\n)[ \t]*` also consumes the
// preceding newline so a copy-strip leaves no trailing blank line.
//
// BOUNDED QUANTIFIERS: leading indent is capped at 3 (CommonMark: 4+ spaces
// renders as an indented code block — visible content, never a control tag),
// other whitespace runs at 16 and tag bodies at 256,
// the backend grammar's exact bounds (constants._TRAILING_CONTROL_LINES_RE) --
// parity is pinned by the shared corpus in test/fixtures/control_tag_corpus.json,
// asserted by BOTH test suites, so a bound edited on one side goes red on the other.
// STACKED SIBLINGS: the marker line may be followed by other recognized
// control-tag lines (`deliver:`, `plan_task_id:` — the backend grammar's
// families — or `option-action:`, via isStringTagLine) without voiding the exemption; the whole trailing tag block
// matches, so copy/search strip all of it and the exemption still fires.
const KEEP_VISIBLE_MARKER_RE =
  /(?:^|\n)[ \t]{0,3}<!--\s{0,16}keep-visible\s{0,16}-->[ \t]{0,16}(?:\n[ \t]{0,3}<!--\s{0,16}(?:deliver|plan_task_id):[^>\n]{0,256}-->[ \t]{0,16})*\s{0,16}$/gi

const STRING_TAG_FAMILIES = new Set(['option-action'])
// The fallback peel also takes the regex's own sibling families, so a mixed tail stays one block.
const PEEL_FAMILIES = new Set([...STRING_TAG_FAMILIES, 'deliver', 'plan_task_id'])
const STRING_TAG_MAX_LINES = 8

/** Mirrors constants._is_string_tag_line: indent ≤3, ≤16 blanks before the
 *  family name, ≤256 characters after its colon, no `>`, trailing blanks ≤16,
 *  family name compared case-insensitively. */
function isStringTagLine(line: string): boolean {
  let lead = 0
  while (lead < line.length && (line[lead] === ' ' || line[lead] === '\t')) lead++
  let stop = line.length
  while (stop > lead && (line[stop - 1] === ' ' || line[stop - 1] === '\t')) stop--
  if (lead > 3 || line.length - stop > 16) return false
  const s = line.slice(lead, stop)
  if (s.length < 7 || !s.startsWith('<!--') || !s.endsWith('-->')) return false
  const body = s.slice(4, -3)
  const inner = body.trimStart()
  if (body.includes('>') || body.length - inner.length > 16) return false
  const colon = inner.indexOf(':')
  return (
    colon !== -1 &&
    PEEL_FAMILIES.has(inner.slice(0, colon).toLowerCase()) &&
    inner.length - colon - 1 <= 256
  )
}

/** Mirrors constants._string_tag_line_start: where a complete string-checked
 *  tag line ending `text` starts (at its leading newline), else null. */
function stringTagLineStart(text: string, atEnd: boolean): number | null {
  const end = atEnd ? text.trimEnd().length : text.length
  if (text.length - end > 32) return null
  const ls = text.lastIndexOf('\n', end - 1) + 1
  if (!isStringTagLine(text.slice(ls, end))) return null
  return Math.max(ls - 1, 0)
}

// Fence-delimiter lines (CommonMark: 3+ backticks or tildes, ≤3 leading
// spaces) for the open-fence parity guard.
const FENCE_DELIM_LINE_RE = /^[ \t]{0,3}(`{3,}|~{3,})/

// Over-approximate fence-open CANDIDATES the exact walker cannot classify: a
// fence run preceded only by whitespace and CommonMark container-marker
// characters (list bullets, ordered-list digits/punctuation, blockquote
// markers) or by 4+ spaces (indented code at top level, a REAL fence inside a
// list continuation). Mirrors constants._AMBIGUOUS_FENCE_LINE_RE — see the
// backend comment for the full rationale. Instead of tracking CommonMark
// containers, a candidate seen while outside any tracked fence VETOES the
// whole decision (strip nothing, no exemption): the failure modes are
// asymmetric, so over-matching costs at most a feature-miss, never content.
// Deliberately NOT g-flagged: .test() is called per line and must not carry
// lastIndex between calls. Bounded class then a literal run — linear.
const AMBIGUOUS_FENCE_LINE_RE = /^[ \t>+*\-\d.)]{0,40}(`{3,}|~{3,})/

/** True when position `idx` falls inside an UNTERMINATED code fence — or
 *  when the fence structure before `idx` is AMBIGUOUS. Inside an open fence
 *  the renderer shows every line as literal code — including one that lexes
 *  like the marker — so it is visible content: neither the exemption nor a
 *  strip may fire on it. Close rule per CommonMark: same character, run at
 *  least as long as the opener. STRIP ONLY WHEN PROVABLY OUTSIDE: a
 *  container-prefixed or over-indented fence candidate seen while outside
 *  any tracked fence may be a real opener this grammar cannot see, so the
 *  walk answers true (do nothing) rather than risk deleting fence-interior
 *  content; inside a tracked fence the same shape is literal code under
 *  every interpretation and does not veto. */
function inOpenFence(text: string, idx: number): boolean {
  let openRun: string | null = null
  for (const line of text.slice(0, idx).split('\n')) {
    const m = FENCE_DELIM_LINE_RE.exec(line)
    if (!m) {
      if (openRun === null && AMBIGUOUS_FENCE_LINE_RE.test(line)) return true
      continue
    }
    if (openRun === null) openRun = m[1]
    // CommonMark 4.5: a CLOSING fence may not carry an info string — only
    // whitespace may follow the run; a fence-lookalike with trailing text
    // inside an open fence is literal code, not a closer.
    else if (
      m[1][0] === openRun[0] &&
      m[1].length >= openRun.length &&
      line.slice(m.index + m[0].length).trim() === ''
    )
      openRun = null
  }
  return openRun !== null
}

/** Strip the trailing marker block — fence-guarded: a tail inside an
 *  unterminated fence is visible code and is left intact. The one safe
 *  direct use of the shared g-flagged RE is `String#replace` (resets
 *  lastIndex); the offset callback carries the fence check. */
export function stripKeepVisibleMarker(text: string): string {
  const strip = (t: string) =>
    t.replace(KEEP_VISIBLE_MARKER_RE, (match, offset: number) =>
      inOpenFence(t, offset) ? match : '',
    )
  const direct = strip(text)
  if (direct !== text) return direct
  // Peel trailing option-action lines, then the marker block must end what is left.
  let head = text
  for (let i = 0; i < STRING_TAG_MAX_LINES; i++) {
    const start = stringTagLineStart(head, i === 0)
    if (start === null || inOpenFence(head, start)) return text
    head = head.slice(0, start)
    if (head.endsWith('\n')) return text
    const stripped = strip(head)
    if (stripped !== head) return stripped
  }
  return text
}

/** True when *text* carries a LIVE keep-visible marker (tail-anchored and
 *  not swallowed by an unterminated fence). Probes via the guarded strip,
 *  mirroring the hasOptionsMarker convention in TurnBlock.tsx. */
export function hasKeepVisibleMarker(text: string): boolean {
  return stripKeepVisibleMarker(text) !== text
}
