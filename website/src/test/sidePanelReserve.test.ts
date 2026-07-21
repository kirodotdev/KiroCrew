/**
 * Tests for measureSidePanelReservedW — the live minimum space the activity
 * panel must leave to its left. Fixes the overlap miscalc where a wide
 * readout capsule (expanded metrics + usage) slid under the panel before the
 * static 560px reserve engaged: the reserve is now
 * max(static, header clusters + padding + 24px gap).
 */
import { describe, it, expect, afterEach } from 'vitest'
import { SIDE_PANEL_RESERVED_W, measureSidePanelReservedW } from '../pages/chat/SidePanel'

function mountHeader(clusterWidths: number[], padLeft = 20, padRight = 12) {
  const header = document.createElement('header')
  header.className = 'topbar-glass'
  header.style.paddingLeft = `${padLeft}px`
  header.style.paddingRight = `${padRight}px`
  for (const w of clusterWidths) {
    const div = document.createElement('div')
    // jsdom's getBoundingClientRect always returns zeros; stub per-cluster.
    div.getBoundingClientRect = () => ({ width: w, height: 52, top: 0, left: 0, right: w, bottom: 52, x: 0, y: 0, toJSON: () => ({}) } as DOMRect)
    header.appendChild(div)
  }
  document.body.appendChild(header)
  return header
}

afterEach(() => {
  document.querySelectorAll('header.topbar-glass').forEach(h => h.remove())
})

describe('measureSidePanelReservedW', () => {
  it('falls back to the static reserve when there is no header (embed/popout frames)', () => {
    expect(measureSidePanelReservedW()).toBe(SIDE_PANEL_RESERVED_W)
  })

  it('returns the static reserve when the header content need is smaller', () => {
    mountHeader([150, 200]) // 150+200+20+12+24 = 406 < 560
    expect(measureSidePanelReservedW()).toBe(SIDE_PANEL_RESERVED_W)
  })

  it('returns the header content need when it exceeds the static reserve (wide capsule)', () => {
    mountHeader([300, 400]) // 300+400+20+12+24 = 756 > 560
    expect(measureSidePanelReservedW()).toBe(756)
  })

  it('excludes the skip-to-content anchor from the cluster sum', () => {
    const header = mountHeader([300, 400])
    const a = document.createElement('a')
    a.getBoundingClientRect = () => ({ width: 500, height: 20, top: 0, left: 0, right: 500, bottom: 20, x: 0, y: 0, toJSON: () => ({}) } as DOMRect)
    header.appendChild(a)
    // Anchor's 500px must NOT inflate the reserve.
    expect(measureSidePanelReservedW()).toBe(756)
  })

  it('excludes absolute topbar overlays so panel width does not feed back through search width', () => {
    const header = mountHeader([300, 400])
    const search = document.createElement('button')
    search.setAttribute('data-topbar-overlay', '')
    search.getBoundingClientRect = () => ({ width: 500, height: 36, top: 0, left: 0, right: 500, bottom: 36, x: 0, y: 0, toJSON: () => ({}) } as DOMRect)
    header.appendChild(search)
    // The absolute search does not consume flow space and must not inflate the reserve.
    expect(measureSidePanelReservedW()).toBe(756)
  })

  it('rounds up (Math.ceil) fractional cluster widths', () => {
    mountHeader([300.4, 400.3]) // 700.7+32+24 = 756.7 -> 757
    expect(measureSidePanelReservedW()).toBe(757)
  })
})
