import type { ReactNode } from 'react'

/** The `[start, end)` ranges of *text* that hold one of *query*'s words, merged.
 *
 *  Case-insensitive, words of two or more characters (a one-letter word would
 *  mark half the excerpt), quotes and a leading `-` (an excluded word) dropped. */
export function matchRanges(text: string, query: string): Array<[number, number]> {
  const words = query
    .split(/\s+/)
    .filter(w => !w.startsWith('-'))
    .map(w => w.replace(/^"+|"+$/g, ''))
    .filter(w => w.length >= 2)
  const lower = text.toLowerCase()
  const ranges: Array<[number, number]> = []
  for (const word of words) {
    const needle = word.toLowerCase()
    for (let at = lower.indexOf(needle); at >= 0; at = lower.indexOf(needle, at + needle.length)) {
      ranges.push([at, at + needle.length])
    }
  }
  ranges.sort((a, b) => a[0] - b[0])
  const merged: Array<[number, number]> = []
  for (const r of ranges) {
    const last = merged[merged.length - 1]
    if (last && r[0] <= last[1]) last[1] = Math.max(last[1], r[1])
    else merged.push([r[0], r[1]])
  }
  return merged
}

/** *text* with the parts that match *query* wrapped in `<mark>`, as React nodes. */
export function highlightRanges(text: string, query: string): ReactNode[] {
  const out: ReactNode[] = []
  let pos = 0
  for (const [start, end] of matchRanges(text, query)) {
    if (start > pos) out.push(text.slice(pos, start))
    out.push(<mark key={start} className="bg-accent-subtle text-text rounded-sm">{text.slice(start, end)}</mark>)
    pos = end
  }
  if (pos < text.length) out.push(text.slice(pos))
  return out
}
