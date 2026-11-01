import type { CSSProperties } from 'react'

/** Where an overlay item or answer sits relative to the box it is pinned to, in screen space. */
export function placeAround(place: string, r: { x: number; y: number; w: number; h: number }): CSSProperties {
  switch (place) {
    case 'right': return { left: r.x + r.w + 10, top: r.y }
    case 'bottom': return { left: r.x + r.w / 2, top: r.y + r.h + 10, transform: 'translateX(-50%)' }
    case 'over': return { left: r.x + r.w / 2, top: r.y + r.h / 2, transform: 'translate(-50%, -50%)' }
    // A badge sitting on the box's top-right corner: a job's status on its part.
    case 'corner': return { left: r.x + r.w - 6, top: r.y - 6, transform: 'translate(-100%, -40%)' }
    default: return { left: r.x + r.w / 2, top: r.y - 10, transform: 'translate(-50%, -100%)' }
  }
}

/**
 * Overlay items never sit on each other: each one in *layer* that overlaps an
 * item already placed steps out of its way (up for items above, down for the rest).
 */
export function nudgeApart(layer: HTMLElement | null, selector: string): void {
  if (!layer) return
  const els = [...layer.querySelectorAll<HTMLElement>(selector)]
  els.forEach(el => { el.style.marginTop = '' })
  const placed: DOMRect[] = []
  for (const el of els.sort((a, b) => a.getBoundingClientRect().top - b.getBoundingClientRect().top)) {
    let r = el.getBoundingClientRect()
    const up = el.dataset.place === 'top'
    let shift = 0
    for (let pass = 0; pass < 12; pass++) {
      const hit = placed.find(p => r.left < p.right && r.right > p.left && r.top < p.bottom + 4 && r.bottom + 4 > p.top)
      if (!hit) break
      const step = up ? -(r.bottom - hit.top + 6) : hit.bottom - r.top + 6
      shift += step
      r = new DOMRect(r.left, r.top + step, r.width, r.height)
    }
    if (shift) el.style.marginTop = `${shift}px`
    placed.push(r)
  }
}
