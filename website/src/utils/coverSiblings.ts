/** Make every sibling of `cover` `inert`, so Tab/Enter or a scripted `.focus()`
 *  cannot reach what it covers. Siblings mounted later are covered too; only the
 *  `inert` this call added is removed on cleanup. */
export function coverSiblings(cover: Element): () => void {
  const pane = cover.parentElement
  if (!pane) return () => {}
  const mine = new Set<Element>()
  const apply = () => {
    for (const el of Array.from(pane.children)) {
      if (el === cover || el.hasAttribute('inert')) continue
      el.setAttribute('inert', '')
      mine.add(el)
    }
  }
  apply()
  const mo = new MutationObserver(apply)
  mo.observe(pane, { childList: true })
  return () => {
    mo.disconnect()
    mine.forEach(el => el.removeAttribute('inert'))
  }
}
