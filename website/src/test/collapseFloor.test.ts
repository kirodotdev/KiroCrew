/**
 * The scroll floor that keeps a folder collapsed near the end of the list from
 * sliding down (collapseFloor.ts), driven through the hold that calls it.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { COLLAPSE_FLOOR_ATTR } from '../pages/chat-sidebar/collapseFloor'
import { holdPinnedHeaderThroughCollapse } from '../pages/chat-sidebar/stickyCollapse'

function rect(top: number): DOMRect {
  return { top, bottom: top + 32, left: 0, right: 200, width: 200, height: 32, x: 0, y: top, toJSON: () => ({}) } as DOMRect
}

/**
 * A 600px lane scrolled to 1200, with a folder block whose top is 500px above
 * the pinned header, and the floor anchor as the lane's first child. The
 * lane's scroll height is `contentHeight` of in-flow content, or further down
 * to the marker's bottom while the marker is shown, as in real layout.
 */
function mount(contentHeight: number, { withFloor = true } = {}) {
  const lane = document.createElement('div')
  Object.defineProperty(lane, 'clientHeight', { value: 600 })
  lane.getBoundingClientRect = () => rect(0)
  lane.scrollTop = 1200
  const block = document.createElement('div')
  const header = document.createElement('div')
  header.setAttribute('data-folder-row', 'f1')
  block.appendChild(header)
  block.getBoundingClientRect = () => rect(-500)
  header.getBoundingClientRect = () => rect(0)
  const marker = document.createElement('div')
  if (withFloor) {
    const anchor = document.createElement('div')
    anchor.setAttribute(COLLAPSE_FLOOR_ATTR, '')
    anchor.appendChild(marker)
    lane.appendChild(anchor)
    anchor.getBoundingClientRect = () => rect(-lane.scrollTop)
  }
  lane.appendChild(block)
  document.body.appendChild(lane)
  const reads: string[] = []
  Object.defineProperty(lane, 'scrollHeight', {
    get: () => {
      reads.push(marker.style.display)
      if (marker.style.display !== 'block') return contentHeight
      return Math.max(contentHeight, (parseInt(marker.style.top, 10) || 0) + 1)
    },
  })
  return { lane, block, marker, reads }
}

function scrollLane(lane: HTMLElement, top: number) {
  lane.scrollTop = top
  lane.dispatchEvent(new Event('scroll'))
}

afterEach(() => {
  document.body.replaceChildren()
  vi.useRealTimers()
})

describe('holdScrollFloor via holdPinnedHeaderThroughCollapse', () => {
  it('holds a floor near the end of the list, then lowers it on scroll-up', async () => {
    vi.useFakeTimers()
    // 1000px of content once collapsed; holding scrollTop 700 needs 1300.
    const { lane, block, marker } = mount(1000)
    expect(holdPinnedHeaderThroughCollapse(lane, block)).toBe(500)
    expect(lane.scrollTop).toBe(700)
    expect(marker.style.display).toBe('block')
    expect(marker.style.top).toBe('1299px')
    await vi.advanceTimersByTimeAsync(1500)
    expect(marker.style.display).toBe('block')
    // Scrolling up lowers the floor with it.
    scrollLane(lane, 600)
    expect(marker.style.top).toBe('1199px')
    // Scrolling back down never raises it.
    scrollLane(lane, 650)
    expect(marker.style.top).toBe('1199px')
    // Once the content alone reaches the floor, the floor is removed.
    scrollLane(lane, 300)
    expect(marker.style.display).toBe('')
  })

  it('drops the floor once settled when content below fills the lane', async () => {
    vi.useFakeTimers()
    const { lane, block, marker } = mount(4000)
    holdPinnedHeaderThroughCollapse(lane, block)
    expect(marker.style.display).toBe('block')
    await vi.advanceTimersByTimeAsync(1500)
    expect(marker.style.display).toBe('')
    expect(lane.scrollTop).toBe(700)
  })

  it('never takes the marker out of layout to measure the content', async () => {
    // Hiding the marker for a read forces a layout whose scroll range is
    // below the held position, and the browser clamps scrollTop there.
    vi.useFakeTimers()
    const { lane, block, reads } = mount(1000)
    holdPinnedHeaderThroughCollapse(lane, block)
    await vi.advanceTimersByTimeAsync(1500)
    scrollLane(lane, 600)
    expect(reads.length).toBeGreaterThan(0)
    expect(reads.every(d => d === 'block')).toBe(true)
  })

  it('still holds the header on a lane with no floor anchor', () => {
    const { lane, block } = mount(4000, { withFloor: false })
    expect(holdPinnedHeaderThroughCollapse(lane, block)).toBe(500)
    expect(lane.scrollTop).toBe(700)
  })
})
