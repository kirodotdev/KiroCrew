import { useEffect, useState, type RefObject } from 'react'

/** Whether the element has come within `rootMargin` of the viewport, once.
 *
 * One-way false→true: the answer is used to START work (build a document, mint
 * a URL), and flapping it back to false as the element scrolls away would tear
 * down work that is already paid for.
 *
 * Environments without `IntersectionObserver` (SSR, older engines, jsdom without
 * a shim) answer `true` immediately rather than never — a missing optimization
 * must not become a blank page.
 */
export function useNearViewport(
  ref: RefObject<Element | null>,
  rootMargin = '400px 0px',
): boolean {
  const [near, setNear] = useState(false)
  useEffect(() => {
    const el = ref.current
    if (!el) return
    if (typeof IntersectionObserver === 'undefined') {
      setNear(true)
      return
    }
    const io = new IntersectionObserver(
      (entries) => {
        if (entries.some((e) => e.isIntersecting)) {
          setNear(true)
          io.disconnect()
        }
      },
      { rootMargin },
    )
    io.observe(el)
    return () => io.disconnect()
  }, [ref, rootMargin])
  return near
}
