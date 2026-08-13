/**
 * Tests for measureSidePanelReservedW — the live minimum space the activity
 * panel must leave to its left. The reserve is
 * max(static 560px, header cluster CONTENT + padding + 24px gap), so a wide
 * readout capsule (expanded metrics + usage) cannot slide under the panel before
 * the static reserve engages.
 *
 * Content, not box: the header is a three-track grid whose side groups are
 * `minmax(0,1fr)` remainders and whose items stretch, so a group's own box is
 * about half the window regardless of what it holds. Measuring boxes inflated
 * the reserve enough to halve a maximized panel, so each cluster is measured as
 * the extent of its children (first-child left to last-child right).
 */
import { describe, it, expect, afterEach } from 'vitest'
import { SIDE_PANEL_RESERVED_W, measureSidePanelReservedW } from '../pages/chat/SidePanel'

const rect = (left: number, width: number, height = 52) => ({
  width, height, top: 0, left, right: left + width, bottom: height, x: left, y: 0, toJSON: () => ({}),
}) as DOMRect

/** Mount a header whose clusters hold ONE child of the given content width.
 *  `boxWidth` is the stretched track the group itself occupies — deliberately
 *  much wider than its content, mirroring the grid, so a regression back to
 *  box measurement fails loudly instead of passing by coincidence. */
function mountHeader(contentWidths: number[], padLeft = 20, padRight = 12, boxWidth = 900) {
  const header = document.createElement('header')
  header.className = 'topbar-glass'
  header.style.paddingLeft = `${padLeft}px`
  header.style.paddingRight = `${padRight}px`
  for (const w of contentWidths) {
    const div = document.createElement('div')
    // jsdom's getBoundingClientRect always returns zeros; stub per-element.
    div.getBoundingClientRect = () => rect(0, boxWidth)
    const child = document.createElement('span')
    child.getBoundingClientRect = () => rect(0, w)
    div.appendChild(child)
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

  it('measures cluster content, not the stretched grid track it sits in', () => {
    // Both groups are 900px boxes holding 150px and 200px of content. Summing
    // boxes would give 1832; the content need is 406, below the static reserve.
    mountHeader([150, 200], 20, 12, 900)
    expect(measureSidePanelReservedW()).toBe(SIDE_PANEL_RESERVED_W)
  })

  it('spans a cluster from its first child to its last, gaps included', () => {
    const header = document.createElement('header')
    header.className = 'topbar-glass'
    header.style.paddingLeft = '20px'
    header.style.paddingRight = '12px'
    const group = document.createElement('div')
    group.getBoundingClientRect = () => rect(0, 900)
    // Two children 100px wide with a 40px gap between them: extent 240, not 200.
    const a = document.createElement('span')
    a.getBoundingClientRect = () => rect(600, 100)
    const b = document.createElement('span')
    b.getBoundingClientRect = () => rect(740, 100)
    group.append(a, b)
    header.appendChild(group)
    document.body.appendChild(header)
    // 240 + 20 + 12 + 24 = 296 -> below the static reserve, so assert the sum
    // through a second group that pushes it over.
    const wide = document.createElement('div')
    wide.getBoundingClientRect = () => rect(0, 900)
    const wideChild = document.createElement('span')
    wideChild.getBoundingClientRect = () => rect(0, 400)
    wide.appendChild(wideChild)
    header.appendChild(wide)
    expect(measureSidePanelReservedW()).toBe(240 + 400 + 20 + 12 + 24)
  })

  it('ignores a cluster with no rendered children', () => {
    const header = mountHeader([300, 400])
    const empty = document.createElement('div')
    empty.getBoundingClientRect = () => rect(0, 900)
    header.appendChild(empty)
    expect(measureSidePanelReservedW()).toBe(756)
  })

  it('excludes the skip-to-content anchor from the cluster sum', () => {
    const header = mountHeader([300, 400])
    const a = document.createElement('a')
    a.getBoundingClientRect = () => rect(0, 500)
    const inner = document.createElement('span')
    inner.getBoundingClientRect = () => rect(0, 500)
    a.appendChild(inner)
    header.appendChild(a)
    // Anchor's 500px must NOT inflate the reserve.
    expect(measureSidePanelReservedW()).toBe(756)
  })

  it('excludes absolute topbar overlays so panel width does not feed back through search width', () => {
    const header = mountHeader([300, 400])
    const search = document.createElement('button')
    search.setAttribute('data-topbar-overlay', '')
    search.getBoundingClientRect = () => rect(0, 500, 36)
    const label = document.createElement('span')
    label.getBoundingClientRect = () => rect(0, 500, 36)
    search.appendChild(label)
    header.appendChild(search)
    // The centre-track search must not inflate the reserve.
    expect(measureSidePanelReservedW()).toBe(756)
  })

  it('rounds up (Math.ceil) fractional cluster widths', () => {
    mountHeader([300.4, 400.3]) // 700.7+32+24 = 756.7 -> 757
    expect(measureSidePanelReservedW()).toBe(757)
  })
})
