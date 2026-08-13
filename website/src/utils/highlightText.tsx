import React from 'react'

export function escapeRegex(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

/**
 * Split text on search term matches and wrap in <mark> elements.
 * @param currentOcc — 0-based index of the occurrence that gets `search-current`.
 *                     -1 means all occurrences get `search-match`.
 */
export function highlightText(
  text: string,
  term: string,
  caseSensitive: boolean,
  currentOcc: number,
): React.ReactNode {
  if (!term) return text
  const regex = new RegExp(`(${escapeRegex(term)})`, caseSensitive ? 'g' : 'gi')
  const parts = text.split(regex)
  if (parts.length === 1) return text
  // split(regex) with capture group alternates: even = non-match, odd = match
  let occIdx = 0
  return parts.map((part, i) => {
    if (i % 2 === 1) {
      const cls = occIdx === currentOcc ? 'search-current' : 'search-match'
      occIdx++
      return <mark key={i} className={cls}>{part}</mark>
    }
    return part
  })
}
