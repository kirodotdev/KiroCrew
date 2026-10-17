/**
 * The geometry rule behind keeping a pinned folder header in place on collapse:
 * the lane scrolls up by exactly the distance the header is painted below its
 * block's top, measured, so it holds for every depth and for a header the
 * block's end is already pushing off. The sidebar wiring is pinned in
 * ChatSidebar.stickyFolderCollapse.test.tsx.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { act, renderHook } from '@testing-library/react'
import { holdPinnedHeaderThroughCollapse, renderRowsRisingInto, useHoldPinnedHeaderOnCollapse, HOLD_ARM_TTL_MS } from '../pages/chat-sidebar/stickyCollapse'
import { FOLDER_BODY_COLLAPSE_MS } from '../components/FolderBody'
import type { ChatFolder } from '../types'

function world(laneScrollTop: number, blockTop: number, headerTop: number) {
  const lane = document.createElement('div')
  let top = laneScrollTop
  Object.defineProperty(lane, 'scrollTop', { configurable: true, get: () => top, set: (v: number) => { top = Math.max(0, v) } })
  const block = document.createElement('div')
  const header = document.createElement('div')
  header.setAttribute('data-folder-row', 'f')
  block.appendChild(header)
  lane.appendChild(block)
  const r = (y: number) => ({ top: y, bottom: y + 32, left: 0, right: 300, width: 300, height: 32, x: 0, y, toJSON: () => ({}) }) as DOMRect
  block.getBoundingClientRect = () => r(blockTop)
  header.getBoundingClientRect = () => r(headerTop)
  return { lane, block }
}

describe('holdPinnedHeaderThroughCollapse', () => {
  it('a nested header pinned one row below its parent keeps that painted row', () => {
    // Depth 1 pins at lane top + 32; its block's top is 250px above that.
    const { lane, block } = world(900, 132 - 250, 132)
    expect(holdPinnedHeaderThroughCollapse(lane, block)).toBe(250)
    expect(lane.scrollTop).toBe(650)
  })

  it('a header the block end is pushing off uses its pushed, painted offset', () => {
    // Pushed 20px above its pin: painted at 80, block top 400 above that.
    const { lane, block } = world(700, 80 - 400, 80)
    holdPinnedHeaderThroughCollapse(lane, block)
    expect(lane.scrollTop).toBe(300)
  })

  it('ignores sub-pixel offsets and a missing lane, block or header', () => {
    const { lane, block } = world(300, 100.3, 100.6)
    expect(holdPinnedHeaderThroughCollapse(lane, block)).toBe(0)
    expect(lane.scrollTop).toBe(300)
    expect(holdPinnedHeaderThroughCollapse(null, block)).toBe(0)
    expect(holdPinnedHeaderThroughCollapse(lane, null)).toBe(0)
    expect(holdPinnedHeaderThroughCollapse(lane, document.createElement('div'))).toBe(0)
  })

  it('reads only the block\'s OWN header, never a nested folder\'s', () => {
    const { lane, block } = world(500, 0, 0)
    const nested = document.createElement('div')
    const nestedHeader = document.createElement('div')
    nestedHeader.setAttribute('data-folder-row', 'child')
    nestedHeader.getBoundingClientRect = () => ({ top: 400 }) as DOMRect
    nested.appendChild(nestedHeader)
    block.appendChild(nested)
    expect(holdPinnedHeaderThroughCollapse(lane, block)).toBe(0)
  })
})

/** A `.session-row` whose box is `top`..`top + 48`, appended to `parent`. */
function sessionRow(parent: HTMLElement, top: number): HTMLElement {
  const row = document.createElement('div')
  row.className = 'session-row'
  parent.appendChild(row)
  row.getBoundingClientRect = () => ({ top, bottom: top + 48, left: 0, right: 300, width: 300, height: 48, x: 0, y: top, toJSON: () => ({}) }) as DOMRect
  return row
}

const forcedVisible = (row: HTMLElement) => row.style.getPropertyValue('content-visibility') === 'visible'

describe('renderRowsRisingInto', () => {
  // The lane is 0..800 on screen. The collapsing block is open at 100..1700
  // with a 32px header, so its body is 1568px; everything after it rises by
  // that much when the body closes.
  function scene() {
    const lane = document.createElement('div')
    lane.getBoundingClientRect = () => ({ top: 0, bottom: 800, left: 0, right: 300, width: 300, height: 800, x: 0, y: 0, toJSON: () => ({}) }) as DOMRect
    const before = sessionRow(lane, 20)
    const block = document.createElement('div')
    const header = document.createElement('div')
    header.setAttribute('data-folder-row', 'f')
    block.appendChild(header)
    lane.appendChild(block)
    block.getBoundingClientRect = () => ({ top: 100, bottom: 1700, left: 0, right: 300, width: 300, height: 1600, x: 0, y: 100, toJSON: () => ({}) }) as DOMRect
    header.getBoundingClientRect = () => ({ top: 100, bottom: 132, left: 0, right: 300, width: 300, height: 32, x: 0, y: 100, toJSON: () => ({}) }) as DOMRect
    const inside = sessionRow(block, 132)
    // After the block: rows at 1700, 1748, ... The lane's bottom is 800, so a
    // row rises into it while top - 1568 < 800, i.e. top < 2368.
    const after = Array.from({ length: 20 }, (_, i) => sessionRow(lane, 1700 + i * 48))
    return { lane, block, before, inside, after }
  }

  it('renders only the rows after the block that can reach the lane, and undoes it', () => {
    const { lane, block, before, inside, after } = scene()
    const undo = renderRowsRisingInto(lane, block)
    expect(forcedVisible(before)).toBe(false)
    expect(forcedVisible(inside)).toBe(false)
    // tops 1700..2324 reach the lane (14 rows); 2372 and on do not.
    expect(after.map(forcedVisible)).toEqual(after.map((_, i) => 1700 + i * 48 < 2368))
    undo()
    expect(after.some(forcedVisible)).toBe(false)
    // Nothing left behind: an emptied style attribute, or none at all.
    expect(after.every(r => !r.getAttribute('style'))).toBe(true)
  })

  it('passes over the rows of a closed folder in between without measuring them', () => {
    const { lane, block, after } = scene()
    // A collapsed folder between the block and the rows after it: FolderBody
    // marks its body inert. Its rows would report tops far below the lane if
    // measured, which must neither force them visible nor end the walk.
    const closed = document.createElement('div')
    closed.setAttribute('inert', '')
    lane.insertBefore(closed, after[0])
    const hidden = Array.from({ length: 12 }, (_, i) => sessionRow(closed, 1700 + i * 48))
    const measured: HTMLElement[] = []
    for (const row of hidden) { const real = row.getBoundingClientRect; row.getBoundingClientRect = () => { measured.push(row); return real() } }
    renderRowsRisingInto(lane, block)
    expect(measured).toEqual([])
    expect(hidden.some(forcedVisible)).toBe(false)
    expect(after.map(forcedVisible)).toEqual(after.map((_, i) => 1700 + i * 48 < 2368))
  })

  it('a body that is already closed rises by nothing: only rows already level with the lane', () => {
    const { lane, block, after } = scene()
    block.getBoundingClientRect = () => ({ top: 100, bottom: 132, left: 0, right: 300, width: 300, height: 32, x: 0, y: 100, toJSON: () => ({}) }) as DOMRect
    // With no transition the rows have moved up in the same layout.
    after.forEach((row, i) => { const top = 132 + i * 48; row.getBoundingClientRect = () => ({ top, bottom: top + 48, height: 48 } as DOMRect) })
    renderRowsRisingInto(lane, block)
    expect(after.map(forcedVisible)).toEqual(after.map((_, i) => 132 + i * 48 < 800))
  })

  it('leaves the lane alone without a block or a lane', () => {
    const { lane, block, after } = scene()
    renderRowsRisingInto(null, block)()
    renderRowsRisingInto(lane, null)()
    expect(after.some(forcedVisible)).toBe(false)
  })
})

describe('useHoldPinnedHeaderOnCollapse', () => {
  afterEach(() => { vi.restoreAllMocks() })

  const folder = (collapsed: boolean): ChatFolder[] => [{ id: 'f', name: 'f', order: 0, collapsed } as ChatFolder]

  function setup() {
    // Header pinned 300px below its block's top, lane scrolled 500px.
    const { lane, block } = world(500, -200, 100)
    document.body.appendChild(lane)
    const laneRef = { current: lane }
    const hook = renderHook(({ folders }) => useHoldPinnedHeaderOnCollapse(laneRef, folders), { initialProps: { folders: folder(false) } })
    return { lane, block, hook }
  }

  it('holds on the first commit that shows the armed folder collapsed, and only once', () => {
    const { lane, block, hook } = setup()
    act(() => hook.result.current.armHold('f', block))
    hook.rerender({ folders: folder(false) })
    expect(lane.scrollTop).toBe(500)
    hook.rerender({ folders: folder(true) })
    expect(lane.scrollTop).toBe(200)
    lane.scrollTop = 500
    hook.rerender({ folders: folder(true).map(f => ({ ...f })) })
    expect(lane.scrollTop).toBe(500)
    lane.remove()
  })

  it('drops an arm whose collapse never rendered within the window', () => {
    const now = vi.spyOn(performance, 'now').mockReturnValue(1000)
    const { lane, block, hook } = setup()
    act(() => hook.result.current.armHold('f', block))
    now.mockReturnValue(1000 + HOLD_ARM_TTL_MS + 1)
    hook.rerender({ folders: folder(true) })
    expect(lane.scrollTop).toBe(500)
    lane.remove()
  })

  it('uses where the header was painted at the arm, so a body that is already closed still holds', () => {
    // Reduced motion: by the collapsed commit the header has fallen to the
    // block's top, so only the arm-time reading still has the 300px offset.
    const { lane, block, hook } = setup()
    act(() => hook.result.current.armHold('f', block))
    const header = block.querySelector<HTMLElement>('[data-folder-row]')!
    header.getBoundingClientRect = block.getBoundingClientRect
    hook.rerender({ folders: folder(true) })
    expect(lane.scrollTop).toBe(200)
    lane.remove()
  })

  it('an expand before the collapse renders disarms it, so a later collapse does not scroll', () => {
    const { lane, block, hook } = setup()
    act(() => hook.result.current.armHold('f', block))
    hook.rerender({ folders: folder(false) })
    act(() => hook.result.current.disarm())
    hook.rerender({ folders: folder(true) })
    expect(lane.scrollTop).toBe(500)
    lane.remove()
  })

  it('disarm cancels a pending hold', () => {
    const { lane, block, hook } = setup()
    act(() => hook.result.current.armHold('f', block))
    act(() => hook.result.current.disarm())
    hook.rerender({ folders: folder(true) })
    expect(lane.scrollTop).toBe(500)
    lane.remove()
  })

  it('names the folder for an instant close only when its header was pinned at the arm', () => {
    const { lane, block, hook } = setup()
    act(() => hook.result.current.armHold('f', block))
    expect(hook.result.current.instantCloseId).toBe('f')
    hook.rerender({ folders: folder(true) })
    expect(hook.result.current.instantCloseId).toBe('f')
    // Seen open again, by any control: the next collapse decides afresh.
    hook.rerender({ folders: folder(false) })
    expect(hook.result.current.instantCloseId).toBeNull()
    // A header at its natural place animates.
    block.getBoundingClientRect = block.querySelector<HTMLElement>('[data-folder-row]')!.getBoundingClientRect
    act(() => hook.result.current.armHold('f', block))
    expect(hook.result.current.instantCloseId).toBeNull()
    lane.remove()
  })

  it('disarm and a dropped arm both clear the instant close', () => {
    const now = vi.spyOn(performance, 'now').mockReturnValue(1000)
    const { lane, block, hook } = setup()
    act(() => hook.result.current.armHold('f', block))
    act(() => hook.result.current.disarm())
    expect(hook.result.current.instantCloseId).toBeNull()
    act(() => hook.result.current.armHold('f', block))
    now.mockReturnValue(1000 + HOLD_ARM_TTL_MS + 1)
    hook.rerender({ folders: folder(true) })
    expect(hook.result.current.instantCloseId).toBeNull()
    lane.remove()
  })

  it('releases the instant close two frames after the collapse commit, so later reorders animate', () => {
    const frames: FrameRequestCallback[] = []
    vi.spyOn(window, 'requestAnimationFrame').mockImplementation(cb => { frames.push(cb); return frames.length })
    vi.spyOn(window, 'cancelAnimationFrame').mockImplementation(() => {})
    const { lane, block, hook } = setup()
    act(() => hook.result.current.armHold('f', block))
    hook.rerender({ folders: folder(true) })
    expect(hook.result.current.instantCloseId).toBe('f')
    // The first frame is the one the collapse paints in: still instant.
    act(() => frames.shift()!(0))
    expect(hook.result.current.instantCloseId).toBe('f')
    act(() => frames.shift()!(16))
    expect(hook.result.current.instantCloseId).toBeNull()
    lane.remove()
  })

  it('renders the rising rows in the collapsed commit and lets them go once the close has finished', () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    try {
      const frames: FrameRequestCallback[] = []
      vi.spyOn(window, 'requestAnimationFrame').mockImplementation(cb => { frames.push(cb); return frames.length })
      vi.spyOn(window, 'cancelAnimationFrame').mockImplementation(() => {})
      const { lane, block, hook } = setup()
      lane.getBoundingClientRect = () => ({ top: 0, bottom: 800, height: 800 } as DOMRect)
      const rising = sessionRow(lane, 400)
      act(() => hook.result.current.armHold('f', block))
      expect(forcedVisible(rising)).toBe(false)
      hook.rerender({ folders: folder(true) })
      expect(forcedVisible(rising)).toBe(true)
      // The instant-close release frames (two) come first; the rows outlast them.
      act(() => { frames.shift()!(0); frames.shift()!(16) })
      expect(forcedVisible(rising)).toBe(true)
      act(() => { vi.advanceTimersByTime(FOLDER_BODY_COLLAPSE_MS) })
      expect(forcedVisible(rising)).toBe(true)
      act(() => frames.shift()!(166))
      expect(forcedVisible(rising)).toBe(true)
      act(() => frames.shift()!(182))
      expect(forcedVisible(rising)).toBe(false)
      lane.remove()
    } finally {
      vi.useRealTimers()
    }
  })

  it('a new arm and a disarm let the rising rows go at once', () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    try {
      const { lane, block, hook } = setup()
      lane.getBoundingClientRect = () => ({ top: 0, bottom: 800, height: 800 } as DOMRect)
      const rising = sessionRow(lane, 400)
      act(() => hook.result.current.armHold('f', block))
      hook.rerender({ folders: folder(true) })
      expect(forcedVisible(rising)).toBe(true)
      hook.rerender({ folders: folder(false) })
      act(() => hook.result.current.armHold('f', block))
      expect(forcedVisible(rising)).toBe(false)
      hook.rerender({ folders: folder(true) })
      expect(forcedVisible(rising)).toBe(true)
      act(() => hook.result.current.disarm())
      expect(forcedVisible(rising)).toBe(false)
      lane.remove()
    } finally {
      vi.useRealTimers()
    }
  })

  it('releases a pinned arm whose collapse never renders at the TTL, with no folders change', () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    try {
      const { lane, block, hook } = setup()
      act(() => hook.result.current.armHold('f', block))
      expect(hook.result.current.instantCloseId).toBe('f')
      act(() => { vi.advanceTimersByTime(HOLD_ARM_TTL_MS - 1) })
      expect(hook.result.current.instantCloseId).toBe('f')
      act(() => { vi.advanceTimersByTime(1) })
      expect(hook.result.current.instantCloseId).toBeNull()
      // The arm is gone too: a later collapse of the same folder does not scroll.
      hook.rerender({ folders: folder(true) })
      expect(lane.scrollTop).toBe(500)
      lane.remove()
    } finally {
      vi.useRealTimers()
    }
  })

  it('a pinned collapse that renders is not cut short by the TTL timer', () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    const frames: FrameRequestCallback[] = []
    vi.spyOn(window, 'requestAnimationFrame').mockImplementation(cb => { frames.push(cb); return frames.length })
    vi.spyOn(window, 'cancelAnimationFrame').mockImplementation(() => {})
    try {
      const { lane, block, hook } = setup()
      act(() => hook.result.current.armHold('f', block))
      hook.rerender({ folders: folder(true) })
      expect(lane.scrollTop).toBe(200)
      // Re-armed on a later collapse: the first arm's timer must not clear it.
      act(() => { vi.advanceTimersByTime(HOLD_ARM_TTL_MS / 2) })
      hook.rerender({ folders: folder(false) })
      act(() => hook.result.current.armHold('f', block))
      act(() => { vi.advanceTimersByTime(HOLD_ARM_TTL_MS / 2 + 1) })
      expect(hook.result.current.instantCloseId).toBe('f')
      lane.remove()
    } finally {
      vi.useRealTimers()
    }
  })
})
