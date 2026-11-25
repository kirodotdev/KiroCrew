import { useEffect } from 'react'
import { GUIDE_KEEP_CLEAR_ATTR } from '../guide/trustRoot'
import { decorClipPath, type DecorHole } from './decorClipPath'

/**
 * Theme decoration never paints over the chat composer.
 *
 * A theme's decorative overlays (a pack's overlay iframes, a registered theme's
 * `branding.overlays`) sit in the shell's decor slot, above the chat. A mascot
 * pinned to a corner then lands on the composer's right edge, over the end of
 * the line being typed and the send button. The composer, and the notice rows
 * each host draws beside it, are marked `data-guide-keep-clear` for the guide
 * panel; the decor slot honours the same
 * mark by clipping a hole for each marked box, so the composer shows through
 * whatever a theme draws, at every width, and the overlay looks tucked behind it.
 *
 * The clip lives on the slot, not on each overlay, so a pack or edition cannot
 * opt out of it, and nothing else about an overlay's placement changes.
 */

export { decorClipPath, type DecorHole }

/**
 * The boxes of *els*, as drawn now.
 * shortcut: a hole is the box's rectangle, so an overlay behind a rounded
 * corner shows a square notch there; trace the radius if a theme makes it show.
 */
function holesOf(els: readonly HTMLElement[]): DecorHole[] {
  const out: DecorHole[] = []
  for (const el of els) {
    const r = el.getBoundingClientRect()
    if (r.width > 0 && r.height > 0) out.push({ top: r.top, left: r.left, bottom: r.bottom, right: r.right })
  }
  return out
}

const MARKED = `[${GUIDE_KEEP_CLEAR_ATTR}]`

/** Whether *node* is, or holds, a marked box. */
function holdsMarkedBox(node: Node): boolean {
  return node instanceof Element && (node.matches(MARKED) || node.querySelector(MARKED) !== null)
}

/**
 * The marked boxes plus every element between each one and *body*. A box can
 * move without changing size (a taller bottom terminal pushes the composer
 * up; a sidebar toggle shifts a width-capped composer sideways), and when it
 * does, one of its containers changes size. A reply streaming into the
 * transcript resizes none of them, since the transcript scrolls beside the
 * composer, not around it.
 * shortcut: a box moved by a sibling of an unresized container goes unseen;
 * observe that sibling too if a layout ever does this.
 */
function boxesAndContainers(marked: readonly HTMLElement[], body: HTMLElement): Set<Element> {
  const out = new Set<Element>()
  for (const box of marked) {
    for (let el: Element | null = box; el && el !== body; el = el.parentElement) {
      if (out.has(el)) break
      out.add(el)
    }
  }
  return out
}

/** Whether one mutation added, removed, marked or unmarked a box. */
function touchesMarkedBox(record: MutationRecord): boolean {
  if (record.type === 'attributes') return true
  return Array.from(record.addedNodes).some(holdsMarkedBox) || Array.from(record.removedNodes).some(holdsMarkedBox)
}

/**
 * Keep *slot*'s `clip-path` cut around the keep-clear boxes. It measures only
 * while the slot holds a decoration, coalesced to one read per frame: on a
 * window resize, a marked box or one of its containers resizing (which is how
 * a box that moves is seen), or a marked box being added, removed, marked or
 * unmarked (a composer mounting, a notice appearing). Other DOM changes, such
 * as a reply streaming in, do not trigger a read.
 */
export function useThemeDecorKeepClear(slot: HTMLElement | null): void {
  useEffect(() => {
    if (!slot) return
    const doc = slot.ownerDocument
    const win = doc.defaultView
    if (!win) return
    let frame = 0
    let watching = false
    const sized = typeof ResizeObserver === 'function' ? new ResizeObserver(() => schedule()) : null
    // Each box and container is observed once. `observe()` reports a size right
    // away, so re-observing on every read would schedule the next read forever.
    const observed = new Set<Element>()
    // Only a marked box arriving, leaving or losing its mark calls for a new
    // read. A streaming reply rewrites the transcript many times a second, and
    // none of that moves the composer.
    const tree = new MutationObserver(records => {
      if (records.some(touchesMarkedBox)) schedule()
    })
    const apply = () => {
      frame = 0
      const marked = Array.from(doc.querySelectorAll<HTMLElement>(MARKED))
      if (sized) {
        const now = boxesAndContainers(marked, doc.body)
        for (const el of observed) if (!now.has(el)) { sized.unobserve(el); observed.delete(el) }
        for (const el of now) if (!observed.has(el)) { sized.observe(el); observed.add(el) }
      }
      const clip = decorClipPath(holesOf(marked), win.innerWidth, win.innerHeight)
      if (clip) slot.style.clipPath = clip
      else slot.style.removeProperty('clip-path')
    }
    function schedule() {
      if (!frame) frame = win!.requestAnimationFrame(apply)
    }
    const start = () => {
      if (watching) return
      watching = true
      win.addEventListener('resize', schedule)
      tree.observe(doc.body, { childList: true, subtree: true, attributes: true, attributeFilter: [GUIDE_KEEP_CLEAR_ATTR] })
      schedule()
    }
    const stop = () => {
      if (!watching) return
      watching = false
      win.removeEventListener('resize', schedule)
      tree.disconnect()
      sized?.disconnect()
      observed.clear()
      if (frame) win.cancelAnimationFrame(frame)
      frame = 0
      slot.style.removeProperty('clip-path')
    }
    const sync = () => (slot.childElementCount > 0 ? start() : stop())
    const contents = new MutationObserver(sync)
    contents.observe(slot, { childList: true })
    sync()
    return () => {
      contents.disconnect()
      stop()
    }
  }, [slot])
}
