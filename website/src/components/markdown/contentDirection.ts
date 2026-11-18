import type { Element, Root } from 'hast'

const PROSE_TAGS = new Set(['p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'li', 'th', 'td', 'blockquote'])
const TECHNICAL_TAGS = new Set(['pre', 'code', 'kbd', 'samp', 'math'])

type Direction = 'auto' | 'ltr' | 'rtl'

export function authoredDirection(node?: Element): Direction | undefined {
  const value = node?.properties?.dir
  const dir = typeof value === 'string' ? value.toLowerCase() : undefined
  return dir === 'auto' || dir === 'ltr' || dir === 'rtl' ? dir : undefined
}

/** Keep language and direction when a styled override rebuilds sanitized HTML. */
export function directionProps(node?: Element) {
  return {
    dir: authoredDirection(node),
    lang: typeof node?.properties?.lang === 'string' ? node.properties.lang : undefined,
  }
}

/** Native first-strong resolution without attaching nodes or forcing layout. */
function nativeResolver(): ((text: string) => Direction) | null {
  if (typeof document === 'undefined') return null
  const probe = document.createElement('bdi')
  probe.dir = 'auto'
  try {
    if (!probe.matches(':dir(ltr)')) return null
  } catch {
    return null
  }
  return text => {
    probe.textContent = text
    return probe.matches(':dir(rtl)') ? 'rtl' : 'ltr'
  }
}

/** Prose resolves independently; an author's explicit container direction wins. */
export function rehypeContentDirection() {
  return (tree: Root) => {
    const resolve = nativeResolver()
    const visit = (parent: Root | Element, inherited: boolean): string => {
      const text: string[] = []
      for (const child of parent.children) {
        if (child.type === 'text') { text.push(child.value); continue }
        if (child.type !== 'element') continue
        const explicit = authoredDirection(child)
        const classes = child.properties.className
        const technical = TECHNICAL_TAGS.has(child.tagName)
          || (Array.isArray(classes) && (classes.includes('katex') || classes.includes('katex-display')))
        // Collect before adding descendant dir attributes: native auto skips
        // those descendants. Authored boundaries and technical islands stay
        // excluded, just as they are in HTML's directionality algorithm.
        const eligible = visit(child, inherited || explicit !== undefined || technical)
        const feedsParent = parent.type === 'element' && child.tagName === 'p'
          && (parent.tagName === 'li' || (parent.tagName === 'blockquote'
            && parent.children.find(n => n.type === 'element') === child))
        if (!explicit) {
          if (technical) child.properties.dir = 'ltr'
          else if (!inherited && (child.tagName === 'a' || PROSE_TAGS.has(child.tagName))) {
            if (resolve) child.properties.dir = resolve(eligible)
            else if (!feedsParent) child.properties.dir = 'auto'
          }
        }
        if (!technical && explicit === undefined && child.tagName !== 'bdi') text.push(eligible)
      }
      return text.join('')
    }
    visit(tree, false)
  }
}
