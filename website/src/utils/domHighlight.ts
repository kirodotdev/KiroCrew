import { escapeRegex } from './highlightText'

/**
 * Walk text nodes inside `el` and wrap search term matches in <mark> elements.
 * @param currentOcc — 0-based index of the occurrence that gets `search-current`.
 *                     -1 means all occurrences get `search-match`.
 */
export function applySearchHighlights(
  el: HTMLElement,
  term: string,
  caseSensitive: boolean,
  currentOcc: number,
): void {
  clearSearchHighlights(el)
  if (!term) return

  const flags = caseSensitive ? 'g' : 'gi'
  const escaped = escapeRegex(term)

  const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT)
  const textNodes: Text[] = []
  while (walker.nextNode()) textNodes.push(walker.currentNode as Text)

  let occIdx = 0
  for (const node of textNodes) {
    const text = node.textContent || ''
    const check = caseSensitive ? text.includes(term) : text.toLowerCase().includes(term.toLowerCase())
    if (!check) continue

    const frag = document.createDocumentFragment()
    let last = 0
    for (const match of text.matchAll(new RegExp(`(${escaped})`, flags))) {
      if (match.index! > last) frag.appendChild(document.createTextNode(text.slice(last, match.index!)))
      const mark = document.createElement('mark')
      mark.className = occIdx === currentOcc ? 'search-current' : 'search-match'
      mark.textContent = match[0]
      frag.appendChild(mark)
      occIdx++
      last = match.index! + match[0].length
    }
    if (last < text.length) frag.appendChild(document.createTextNode(text.slice(last)))
    node.parentNode!.replaceChild(frag, node)
  }
}

export function clearSearchHighlights(el: HTMLElement): void {
  const marks = el.querySelectorAll('mark.search-match, mark.search-current')
  // Bail when there's nothing to clear. Skipping the normalize() below is
  // load-bearing: AssistantMessage runs this on every streaming token, and
  // el.normalize() merges adjacent text nodes that React is still tracking,
  // which causes "Failed to execute 'removeChild' on 'Node': The node to be
  // removed is not a child of this node" on the next React commit. Diff
  // streaming hits this most because DiffBlock produces many small text-bearing
  // <span>s per token.
  if (marks.length === 0) return
  marks.forEach(m => m.replaceWith(...m.childNodes))
  // Merge adjacent text nodes left behind by mark unwrapping.
  // Without this, repeated highlight→clear cycles fragment text nodes,
  // causing the next TreeWalker pass to see tiny fragments where
  // matches may not span correctly.
  el.normalize()
}
