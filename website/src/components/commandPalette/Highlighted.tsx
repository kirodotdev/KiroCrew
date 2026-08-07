import type { ReactNode } from 'react'

/**
 * Render `text` with the characters at `indices` emphasised.
 *
 * Emits one node per CONSECUTIVE RUN of matched / unmatched characters, not one
 * per character. This is a load-bearing property, not a micro-optimisation: the
 * palette's result list re-renders on every selection change — including plain
 * mouse movement, since hovering a row selects it — and it renders both a title
 * and a match-centered snippet per row. At one node per character a full
 * Sessions tab was several thousand elements rebuilt per hovered row, which is
 * long enough to drop frames. A fuzzy match is a handful of short runs, so the
 * run form is bounded by the number of match groups instead of by text length.
 *
 * Unmatched runs are emitted as bare strings rather than wrapped in an element:
 * React does not require keys for string children, and the element saved per run
 * is the entire point.
 *
 * Highlighting is expressed as React nodes and never
 * `dangerouslySetInnerHTML` — no HTML string is ever built from the matched text
 * (`frontend-security` lint rule).
 *
 * @param text - The full string to render.
 * @param indices - Character offsets into `text` to emphasise. Offsets outside
 *   the string are ignored; order and duplicates do not matter.
 */
export function Highlighted({ text, indices }: { text: string; indices: number[] }): ReactNode {
  if (indices.length === 0) return text
  const hit = new Set(indices)
  const nodes: ReactNode[] = []
  let start = 0
  while (start < text.length) {
    const matched = hit.has(start)
    let end = start + 1
    while (end < text.length && hit.has(end) === matched) end++
    const run = text.slice(start, end)
    nodes.push(
      matched ? (
        <strong key={start} className="text-text-strong font-semibold">
          {run}
        </strong>
      ) : (
        run
      ),
    )
    start = end
  }
  return <>{nodes}</>
}

export default Highlighted
