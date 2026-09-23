/**
 * Dashboard-only autonomous goal suggestion marker.
 *
 * The agent emits one standalone `[GOAL: objective]` line before any final
 * `[OPTIONS: ...]` line. Keeping this parser in the shared protocol layer makes
 * transcript rendering and follow-up derivation consume the same marker.
 */

const goalMarkerPattern = /^[ \t]*\[GOAL:[ \t]*([^\]\r\n]+?)\][ \t]*$/gim

/** Remove every complete marker and return the last non-blank objective. */
export function parseGoalSuggestion(content: string): {
  text: string
  goalSuggestion: string | null
} {
  const matches = [...content.matchAll(new RegExp(goalMarkerPattern))]
  const goalSuggestion = matches
    .map(match => match[1].trim())
    .filter(Boolean)
    .at(-1) ?? null
  if (!matches.length) return { text: content, goalSuggestion: null }
  const text = content.replace(new RegExp(goalMarkerPattern), '')
  return { text, goalSuggestion }
}

/** Hide a standalone marker while its closing bracket is still streaming. */
export function stripPartialGoalMarker(text: string): string {
  const match = /(?:^|\n)[ \t]*\[(?:G|GO|GOA|GOAL|GOAL:[^\]\r\n]*)$/i.exec(text)
  if (!match || match.index === undefined) return text
  return text.slice(0, match.index).trimEnd()
}
