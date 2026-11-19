import { useLayoutEffect, useRef, useState } from 'react'

/** What a move takes from a kept host's content, recorded as it changes. A
 *  browser puts a box that moves to a new parent back at its top and sends the
 *  focus to the page, and on a fold the column holding the host leaves the page
 *  before its slot lets go, so neither can be read at the move. `atEnd` keeps a
 *  box that follows its newest line (an activity log) at its end when the new
 *  surface's width wraps it to another height. */
interface KeptView {
  scrolls: WeakMap<Element, { top: number; left: number; atEnd: boolean }>
  focused: HTMLElement | null
}
const keptViews = new WeakMap<HTMLElement, KeptView>()

function recordView(host: HTMLElement): KeptView {
  const view: KeptView = { scrolls: new WeakMap(), focused: null }
  // A scroll event does not bubble, but its capture phase passes the host. One
  // that fires while the host is off the page (a scroll queued in the frame
  // before a fold) reads the 0 a box off the page has, so it is not recorded.
  host.addEventListener('scroll', e => {
    const box = e.target
    if (!(box instanceof Element) || !box.isConnected) return
    view.scrolls.set(box, {
      top: box.scrollTop,
      left: box.scrollLeft,
      atEnd: box.scrollTop > 0 && box.scrollTop + box.clientHeight >= box.scrollHeight - 1,
    })
  }, { capture: true, passive: true })
  host.addEventListener('focusin', e => { if (e.target instanceof HTMLElement) view.focused = e.target })
  // A field the focus left for another place on the page is forgotten; one
  // that lost it by leaving the page with the host is kept, to focus again.
  host.addEventListener('focusout', e => {
    const field = e.target
    queueMicrotask(() => {
      const kept = view.focused
      if (kept && kept === field && kept.isConnected && document.activeElement !== kept) view.focused = null
    })
  })
  return view
}

/** Right after a move: every recorded offset back, and the focus too when
 *  nothing else has taken it since. A field that does not get it back here is
 *  forgotten, so a later move cannot hand it a focus the person left. */
function putBack(host: HTMLElement, view: KeptView) {
  for (const box of host.querySelectorAll('*')) {
    const at = view.scrolls.get(box)
    if (!at) continue
    box.scrollTop = at.atEnd ? box.scrollHeight : at.top
    box.scrollLeft = at.left
  }
  const field = view.focused
  view.focused = null
  if (!field || !host.contains(field)) return
  const active = document.activeElement
  // Its `focusin` records it again.
  if (!active || active === document.body) field.focus({ preventScroll: true })
}

/** A kept host for content that must survive a move between surfaces (the
 *  sessions list's, the side panel's): a detached `display: contents` element
 *  the owner portals into and each surface adopts with `AdoptPaneHost`, so the
 *  content moves with it instead of mounting again. Null outside the browser.
 *
 *  `keepScrollAndFocus` has each adoption put back the scroll offsets and the
 *  focus the move took. The sessions list leaves it off: its lane anchors its
 *  offset to a row (`useLaneScrollMemory`), and a pixel offset put back first
 *  would be recorded over that anchor. */
export function useKeptPaneHost({ keepScrollAndFocus = false }: { keepScrollAndFocus?: boolean } = {}): HTMLElement | null {
  const [host] = useState(() => {
    if (typeof document === 'undefined') return null
    const el = document.createElement('div')
    el.style.display = 'contents'
    if (keepScrollAndFocus) keptViews.set(el, recordView(el))
    return el
  })
  return host
}

/** Puts a kept host element (the sessions list's, the side panel's) inside the
 *  surface showing it, and takes it out again when that surface leaves.
 *  `onMountChange` hears this slot come and go, so the host's owner can mount
 *  the content once a surface holds the host, and keep it mounted while a
 *  leaving surface still animates out with it. */
export function AdoptPaneHost({ host, onMountChange }: { host: HTMLElement | null; onMountChange?: (mounted: boolean) => void }) {
  const ref = useRef<HTMLDivElement>(null)
  useLayoutEffect(() => {
    const slot = ref.current
    if (!slot || !host) return
    slot.appendChild(host)
    const view = keptViews.get(host)
    if (view) putBack(host, view)
    // Release the host only if this slot still holds it: when a surface is
    // swapped (a phone/desktop switch while open), the new slot adopts the
    // host before the old one unmounts.
    return () => { if (host.parentNode === slot) host.remove() }
  }, [host])
  useLayoutEffect(() => {
    onMountChange?.(true)
    return () => onMountChange?.(false)
  }, [onMountChange])
  return <div ref={ref} style={{ display: 'contents' }} />
}
