/**
 * Text and selections inside Pierre's code blocks, read from outside them.
 *
 * Pierre draws a code block into an OPEN shadow root on a `diffs-container`
 * host. Two tools that work on the document tree stop at that host: a
 * `TreeWalker` never enters the root, and a selection made wholly inside it is
 * reported with both ends moved onto the host's parent — collapsed — while
 * `toString()` still returns the text. This module reads both from the root
 * itself.
 *
 * One source line is one `[data-line]` element holding a text node per token
 * (`mycli`, ` rm`, ` NAME`), so a phrase crosses node boundaries. The gutter's
 * line numbers and the root's `<style>` carry text too, outside `[data-line]`,
 * and are never searched.
 *
 * A block is not final when it first appears. `WarmSwap` parks the highlighted
 * copy in an `aria-hidden` box beside the plain copy, fills it, then removes the
 * plain copy and shows the box. A parked block is not searched (its text would
 * count twice), and `watchText` tells a find when the swap has left it holding
 * ranges on nodes that are gone.
 */

const CODE_HOST = 'diffs-container'
const CODE_LINE = '[data-line]'

/**
 * Whether `host` sits in an `aria-hidden` box below `root`: a block parked until it
 * paints. Only boxes inside `root` count, so an app hidden behind a dialog is not parked.
 */
function isParked(host: Element, root: Element): boolean {
  for (let el: Element | null = host; el && el !== root; el = el.parentElement) if (el.getAttribute('aria-hidden') === 'true') return true
  return false
}

/** The open shadow roots of the shown code blocks under `root`, in document order. */
export function codeShadowRoots(root: Element): ShadowRoot[] {
  const roots: ShadowRoot[] = []
  for (const host of root.querySelectorAll(CODE_HOST)) if (host.shadowRoot && !isParked(host, root)) roots.push(host.shadowRoot)
  return roots
}

/** The text nodes of `el`, whitespace-only ones included: inside a code line they are the spaces between tokens. */
function textNodesOf(el: Element): Text[] {
  const nodes: Text[] = []
  const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT)
  for (let n = walker.nextNode(); n; n = walker.nextNode()) nodes.push(n as Text)
  return nodes
}

/**
 * The runs of text a find searches, in document order: every non-blank text
 * node of the light DOM on its own, and each source line of a shown code block as
 * one run, so a phrase can span the line's tokens.
 */
export function searchRuns(root: Element): Text[][] {
  const runs: Text[][] = []
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT, {
    acceptNode: (n) => {
      if (n.nodeType === Node.TEXT_NODE) return n.nodeValue?.trim() ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT
      if ((n as Element).localName !== CODE_HOST) return NodeFilter.FILTER_SKIP
      return isParked(n as Element, root) ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT
    },
  })
  for (let n = walker.nextNode(); n; n = walker.nextNode()) {
    if (n.nodeType === Node.TEXT_NODE) runs.push([n as Text])
    else for (const line of (n as Element).shadowRoot?.querySelectorAll(CODE_LINE) ?? []) runs.push(textNodesOf(line))
  }
  return runs
}

/**
 * The text node and offset that character `offset` of the joined run falls on. A
 * start sits on the node it begins in, an end on the node it finishes in.
 */
function boundary(nodes: Text[], offset: number, isEnd: boolean): [Text, number] {
  let from = 0
  for (const node of nodes) {
    const to = from + node.data.length
    if (isEnd ? offset <= to : offset < to) return [node, offset - from]
    from = to
  }
  const last = nodes[nodes.length - 1]
  return [last, last.data.length]
}

/**
 * A Range for every match of `re` in each run. `re` is global and matches a non-empty
 * literal: an empty match would never advance. A match may span the run's text nodes.
 */
export function matchRanges(runs: Text[][], re: RegExp): Range[] {
  const ranges: Range[] = []
  for (const nodes of runs) {
    const text = nodes.map((n) => n.data).join('')
    for (let m = re.exec(text); m; m = re.exec(text)) {
      const range = document.createRange()
      range.setStart(...boundary(nodes, m.index, false))
      range.setEnd(...boundary(nodes, m.index + m[0].length, true))
      ranges.push(range)
    }
  }
  return ranges
}

/**
 * The range a selection really covers when it lies wholly inside a code block
 * hosted under `container`, or null. For such a selection `getRangeAt` and
 * `isCollapsed` describe the host's parent, so only `getComposedRanges` — absent
 * in older engines, which then behave as before — names the text.
 */
export function shadowSelectionRange(sel: Selection, container: Element): Range | null {
  if (typeof sel.getComposedRanges !== 'function') return null
  const roots = codeShadowRoots(container)
  if (!roots.length) return null
  try {
    const [composed] = sel.getComposedRanges({ shadowRoots: roots })
    const root = composed?.startContainer.getRootNode()
    if (!(root instanceof ShadowRoot) || !roots.includes(root)) return null
    const range = document.createRange()
    range.setStart(composed.startContainer, composed.startOffset)
    range.setEnd(composed.endContainer, composed.endOffset) // into another root, this collapses the range
    return range.collapsed ? null : range
  } catch {
    return null // an engine that implements the older call shape rejects this one
  }
}

/**
 * Calls `onChange`, at most once a frame, after the text under `root` may have
 * changed, until the returned function stops it. A tree observer sees a block
 * swapped in but not the fill of a shadow root, so every shown root is watched
 * too, and the set is taken again after each change: a new block brings a new root.
 */
export function watchText(root: Element, onChange: () => void): () => void {
  const observers: MutationObserver[] = []
  let frame = 0
  function attach() {
    for (const target of [root, ...codeShadowRoots(root)]) {
      const observer = new MutationObserver(schedule)
      observer.observe(target, { childList: true, subtree: true })
      observers.push(observer)
    }
  }
  function detach() {
    for (const observer of observers.splice(0)) observer.disconnect()
  }
  function schedule() {
    if (frame) return
    frame = requestAnimationFrame(() => { frame = 0; detach(); attach(); onChange() })
  }
  attach()
  return () => { cancelAnimationFrame(frame); detach() }
}
