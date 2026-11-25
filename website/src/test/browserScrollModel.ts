/**
 * A scroll box that keeps its offsets the way a browser does, for tests that
 * move one between parents.
 *
 * happy-dom keeps a box's offsets wherever the box goes and fires no `scroll`
 * when one is set. A browser (WebKit 26 and Chromium 145, checked by moving a
 * scrolled box between two parents) differs on both: a box that is off the
 * page reads 0, one that moves to a new parent comes back at its top, and an
 * offset that changes fires `scroll` at the box. This gives one box that
 * behaviour. `placeOf` names where the box is, such as the surface holding it;
 * when that changes, the offsets go back to 0. `size` can be changed by the
 * test, as when a new surface's width wraps the content to another height.
 */
export function modelBrowserScroll(
  box: HTMLElement,
  placeOf: () => unknown,
  size: { height: number; client: number } = { height: 2000, client: 100 },
) {
  let top = 0
  let left = 0
  let place = placeOf()
  const follow = () => {
    const now = placeOf()
    if (now !== place) { top = 0; left = 0; place = now }
  }
  const write = (next: number, current: number, store: (v: number) => void) => {
    follow()
    if (!box.isConnected || next === current) return
    store(next)
    box.dispatchEvent(new Event('scroll'))
  }
  Object.defineProperties(box, {
    scrollHeight: { configurable: true, get: () => size.height },
    clientHeight: { configurable: true, get: () => size.client },
    scrollTop: {
      configurable: true,
      get: () => { follow(); return box.isConnected ? top : 0 },
      set: (v: number) => {
        follow()
        const next = Math.min(Math.max(0, v), Math.max(0, size.height - size.client))
        write(next, top, n => { top = n })
      },
    },
    scrollLeft: {
      configurable: true,
      get: () => { follow(); return box.isConnected ? left : 0 },
      set: (v: number) => { follow(); write(Math.max(0, v), left, n => { left = n }) },
    },
  })
  return size
}
