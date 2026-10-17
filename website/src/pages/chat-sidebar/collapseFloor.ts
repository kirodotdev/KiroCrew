/**
 * Holds a scroll floor under the session list so a folder collapsed near the
 * end of the list stays where `holdPinnedHeaderThroughCollapse` put it.
 *
 * That hold scrolls the lane back so the collapsed folder's header stays where
 * it was painted. Near the end of the list that is not enough on its own: once
 * the body has closed there may be too little content left to stay scrolled
 * that far, so the browser clamps `scrollTop` and the whole list slides down,
 * putting the collapsed folder mid-lane with the folders above it back in
 * view.
 *
 * So the tree lane carries a zero-height anchor (`COLLAPSE_FLOOR_ATTR`) as its
 * first child, where nothing above it can move it, holding one absolutely
 * positioned 1px marker. Before the hold scrolls, the marker is placed so the
 * lane's scroll height cannot drop below what the held position needs. Being
 * out of flow, it never changes the lane's flex layout; it only extends the
 * scrollable area. The extra room shows as blank space under the list and is
 * given back as the person scrolls up: the floor always sits at the highest
 * scroll position reached since the collapse, and is removed once the
 * content alone reaches it.
 */

/** Marks the lane's floor anchor: rendered by ChatSidebar as the tree lane's
 *  first child, with one hidden absolutely positioned child (the marker). */
export const COLLAPSE_FLOOR_ATTR = 'data-collapse-floor'

/**
 * The body keeps shrinking for the length of its close transition after the
 * hold scrolls. Until this has passed the floor is only lowered by the person
 * scrolling up, never dropped because the content still looks tall enough.
 */
export const FLOOR_SETTLE_MS = 1000

/** Removes the floor currently held on a lane, if any. */
const activeFloors = new WeakMap<HTMLElement, () => void>()

/**
 * Keep `anchorTop` a reachable scroll position on `lane` until the content
 * alone reaches it. Call immediately before setting `lane.scrollTop` to
 * `anchorTop`. A lane without a floor anchor is left alone.
 */
export function holdScrollFloor(lane: HTMLElement, anchorTop: number): void {
  activeFloors.get(lane)?.()
  const floorAnchor = lane.querySelector<HTMLElement>(`:scope > [${COLLAPSE_FLOOR_ATTR}]`)
  const marker = floorAnchor?.firstElementChild as HTMLElement | null | undefined
  if (!floorAnchor || !marker) return

  let lowestTop = anchorTop
  let settled = false
  // The marker's bottom edge in the lane's scroll coordinates, as last placed.
  let markerBottom = 0

  // Where the floor's bottom lands in the lane's scroll coordinates.
  const floorBottom = () => lowestTop + lane.clientHeight

  const place = () => {
    // The anchor's own offset inside the lane's scrollable area.
    const anchorAt = floorAnchor.getBoundingClientRect().top - lane.getBoundingClientRect().top + lane.scrollTop
    const top = Math.ceil(floorBottom() - anchorAt) - 1
    marker.style.top = `${top}px`
    marker.style.display = 'block'
    markerBottom = anchorAt + top + 1
  }

  const release = () => {
    window.clearTimeout(timer)
    lane.removeEventListener('scroll', onScroll)
    marker.style.display = ''
    marker.style.top = ''
    floorAnchor.setAttribute(COLLAPSE_FLOOR_ATTR, '')
    if (activeFloors.get(lane) === release) activeFloors.delete(lane)
  }

  // Whether the lane's own content reaches past the floor, measured with the
  // marker left in place. The lane's scroll height is the larger of the
  // content's extent and the marker's bottom, so a scroll height beyond the
  // marker can only come from content. Taking the marker out of layout to
  // measure would shrink the scroll range below the held position, and the
  // browser clamps scrollTop during that forced layout; restoring the marker
  // does not undo the clamp. A rect on a child cannot stand in either: the
  // lane's content sits in nested flex-1 boxes clamped to the lane while
  // their rows overflow them. The 1px allows for subpixel rounding in
  // scrollHeight; content within it of the floor just keeps the floor until
  // the next upward scroll.
  const contentPastFloor = () => lane.scrollHeight - 1 > markerBottom

  // Once the content alone reaches the floor, the floor is doing nothing.
  const releaseIfUnneeded = () => {
    if (lowestTop <= 0 || contentPastFloor()) release()
  }

  // The hold's own scroll and every later scroll land here. Only an upward
  // scroll lowers the position the floor has to hold.
  function onScroll() {
    if (lane.scrollTop >= lowestTop) return
    lowestTop = lane.scrollTop
    place()
    if (settled) releaseIfUnneeded()
  }

  // The anchor reads `settled` from here until the floor is released, so a
  // browser test can wait for the settle check instead of sleeping past it.
  const timer = window.setTimeout(() => {
    settled = true
    floorAnchor.setAttribute(COLLAPSE_FLOOR_ATTR, 'settled')
    releaseIfUnneeded()
  }, FLOOR_SETTLE_MS)
  activeFloors.set(lane, release)
  place()
  lane.addEventListener('scroll', onScroll, { passive: true })
}
