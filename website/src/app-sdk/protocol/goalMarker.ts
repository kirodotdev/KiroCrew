/**
 * Dashboard-only autonomous goal suggestion marker.
 *
 * The agent emits one standalone `[GOAL: objective]` line before any final
 * `[OPTIONS: ...]` line. The backend teaches the marker only to slots full
 * chat can open (`_GOAL_RULE_DASHBOARD` in `context.py`); full chat renders it
 * as a review card, and hosts without goal review (split panes, Side Chat,
 * ChatEmbed) degrade it to an ordinary reply through
 * `goalSuggestionReplyFallback`. Keeping this parser in the shared protocol layer makes
 * transcript rendering and follow-up derivation consume the same marker.
 */

const GOAL_PREFIX = '[goal:'

function parseGoalLine(line: string): { matched: boolean; objective: string | null } {
  let start = 0
  while (line[start] === ' ' || line[start] === '\t') start += 1
  if (line.slice(start, start + GOAL_PREFIX.length).toLowerCase() !== GOAL_PREFIX) {
    return { matched: false, objective: null }
  }

  const bodyStart = start + GOAL_PREFIX.length
  // The closer is the line's last `]`; only spaces, tabs and a CRLF `\r` may
  // follow it, matching the backend scanner.
  let close = line.length - 1
  while (close >= bodyStart && (line[close] === ' ' || line[close] === '\t' || line[close] === '\r')) close -= 1
  if (close <= bodyStart || line[close] !== ']') return { matched: false, objective: null }
  // The objective's own brackets must balance, so `[GOAL: a] b]` stays prose.
  let depth = 0
  for (let index = bodyStart; index < close; index += 1) {
    const char = line[index]
    if (char === '\r') return { matched: false, objective: null }
    if (char === '[') depth += 1
    else if (char === ']' && --depth < 0) return { matched: false, objective: null }
  }
  if (depth !== 0) return { matched: false, objective: null }
  return { matched: true, objective: line.slice(bodyStart, close).trim() || null }
}

/** Remove every complete marker and return the last non-blank objective. */
export function parseGoalSuggestion(content: string): {
  text: string
  goalSuggestion: string | null
} {
  const output: string[] = []
  let goalSuggestion: string | null = null
  let start = 0
  while (start < content.length) {
    const newline = content.indexOf('\n', start)
    const lineEnd = newline < 0 ? content.length : newline
    const line = content.slice(start, lineEnd)
    const parsed = parseGoalLine(line)
    if (!parsed.matched) output.push(line)
    if (parsed.objective) goalSuggestion = parsed.objective
    if (newline < 0) break
    output.push('\n')
    start = newline + 1
  }
  return { text: output.join(''), goalSuggestion }
}

/** Whether a `[goal:` line has not reached its own closer yet: the first `]`
 *  outside the objective's balanced brackets, as `parseGoalLine` reads it. */
function goalMarkerStillOpen(tail: string): boolean {
  let depth = 0
  for (let index = GOAL_PREFIX.length; index < tail.length; index += 1) {
    const char = tail[index]
    if (char === '\r' || char === '\n') return false
    if (char === '[') depth += 1
    else if (char === ']') {
      if (depth === 0) return false
      depth -= 1
    }
  }
  return true
}

/** Hide a standalone marker while its closing bracket is still streaming. */
export function stripPartialGoalMarker(text: string): string {
  const newline = text.lastIndexOf('\n')
  const lineStart = newline < 0 ? 0 : newline + 1
  let markerStart = lineStart
  while (text[markerStart] === ' ' || text[markerStart] === '\t') markerStart += 1
  const tail = text.slice(markerStart).toLowerCase()
  const partialHead = ['[g', '[go', '[goa', '[goal'].includes(tail)
  const partialBody = tail.startsWith(GOAL_PREFIX) && goalMarkerStillOpen(tail)
  if (!partialHead && !partialBody) return text
  return text.slice(0, newline < 0 ? 0 : newline).trimEnd()
}
