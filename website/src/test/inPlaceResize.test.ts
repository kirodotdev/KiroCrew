import { afterEach, describe, expect, it, vi } from 'vitest'
import { inPlaceDeltaAbove, noteInPlaceResize, resizedInPlaceBelow } from '../hooks/virtualizer/inPlaceResize'

describe('inPlaceResize', () => {
  afterEach(() => { vi.useRealTimers() })

  it('reports a note inside the row at or below the fold', () => {
    const row = document.createElement('div')
    const anchor = document.createElement('div')
    row.appendChild(anchor)
    noteInPlaceResize(anchor, 300)
    expect(resizedInPlaceBelow(row, 100)).toBe(true)
    expect(resizedInPlaceBelow(row, 300)).toBe(true)
  })

  it('ignores a note above the fold or in another row', () => {
    const row = document.createElement('div')
    const other = document.createElement('div')
    const anchor = document.createElement('div')
    row.appendChild(anchor)
    noteInPlaceResize(anchor, 50)
    expect(resizedInPlaceBelow(row, 100)).toBe(false)
    noteInPlaceResize(anchor, 400)
    expect(resizedInPlaceBelow(other, 100)).toBe(false)
  })

  it('sums only the changes noted above the fold in the row', () => {
    const row = document.createElement('div')
    const above = document.createElement('div')
    const below = document.createElement('div')
    row.append(above, below)
    noteInPlaceResize(above, -200, 0, -480)
    noteInPlaceResize(below, 300, 0, 350)
    expect(resizedInPlaceBelow(row, 0)).toBe(true)
    expect(inPlaceDeltaAbove(row, 0)).toBe(-480)
    expect(inPlaceDeltaAbove(document.createElement('div'), 0)).toBe(0)
  })

  it('forgets a note after two frames', async () => {
    const row = document.createElement('div')
    const anchor = document.createElement('div')
    row.appendChild(anchor)
    noteInPlaceResize(anchor, 300)
    await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(() => setTimeout(r, 0))))
    expect(resizedInPlaceBelow(row, 100)).toBe(false)
  })
})
