import { describe, expect, it } from 'vitest'
import { MOST_WAIT, pickCopy, type CopyWant } from './domCopies'

/**
 * A room of `frames` frames. Each page in `moving` is painted anew every frame; each page in `once` once, at frame 0.
 * Returns how many copies each page got, and the most copies in one frame (always one).
 */
function run(pages: readonly { id: string; pointer?: boolean; gazeDeg: number; moving?: boolean }[], frames: number) {
  const changed = new Map(pages.map(p => [p.id, true]))
  const waited = new Map(pages.map(p => [p.id, 0]))
  const copies = new Map(pages.map(p => [p.id, 0]))
  let last: string | null = null
  for (let f = 0; f < frames; f += 1) {
    const wants: CopyWant[] = pages.filter(p => changed.get(p.id)).map(p => ({ id: p.id, waited: waited.get(p.id) ?? 0, pointer: !!p.pointer, gazeDeg: p.gazeDeg }))
    const id = pickCopy(wants, last)
    for (const w of wants) if (w.id !== id) waited.set(w.id, (waited.get(w.id) ?? 0) + 1)
    if (id) { copies.set(id, (copies.get(id) ?? 0) + 1); changed.set(id, false); waited.set(id, 0); last = id }
    for (const p of pages) if (p.moving) changed.set(p.id, true)
  }
  return copies
}

describe('one copy a room frame', () => {
  it('copies nothing when no page changed: a still page costs nothing', () => {
    expect(pickCopy([], null)).toBeNull()
    const c = run([{ id: 'a', gazeDeg: 0 }, { id: 'b', gazeDeg: 30 }], 60)
    // Each was painted once, at the start, and is copied once.
    expect([c.get('a'), c.get('b')]).toEqual([1, 1])
  })
  it('gives a page that changes alone every frame', () => {
    const c = run([{ id: 'left', gazeDeg: 30 }, { id: 'mid', gazeDeg: 0, moving: true }, { id: 'right', gazeDeg: 30 }], 60)
    // The two still pages once each; the moving one every other frame.
    expect(c.get('left')).toBe(1)
    expect(c.get('right')).toBe(1)
    expect(c.get('mid')).toBe(58)
  })
  it('three pages in view, the middle one moving and looked at: it gets every frame but the two first copies of the still ones', () => {
    const c = run([{ id: 'left', gazeDeg: 28 }, { id: 'mid', gazeDeg: 1, moving: true }, { id: 'right', gazeDeg: 28 }], 600)
    expect((c.get('mid') ?? 0) / 600).toBeGreaterThan(0.99)
  })
  it('three moving pages at a room of 60: the pointer page and the looked-at page about 25 a second, the third about 9, none starves', () => {
    const c = run([{ id: 'pointer', pointer: true, gazeDeg: 20, moving: true }, { id: 'gaze', gazeDeg: 1, moving: true }, { id: 'third', gazeDeg: 30, moving: true }], 600)
    const perSecond = (id: string) => ((c.get(id) ?? 0) / 600) * 60
    expect(perSecond('pointer')).toBeGreaterThan(20)
    expect(perSecond('gaze')).toBeGreaterThan(20)
    expect(perSecond('third')).toBeGreaterThan(60 / (MOST_WAIT + 1) - 1)
    expect(perSecond('pointer') + perSecond('gaze') + perSecond('third')).toBeCloseTo(60, 6)
  })
  it('two moving pages: they share the frames; the one he looks at is never behind by more than a frame', () => {
    const c = run([{ id: 'gaze', gazeDeg: 2, moving: true }, { id: 'other', gazeDeg: 25, moving: true }], 600)
    expect(Math.abs((c.get('gaze') ?? 0) - (c.get('other') ?? 0))).toBeLessThanOrEqual(1)
  })
  it('a page under the pointer that is not the one he looks at goes first, then the looked-at one', () => {
    const wants = [{ id: 'pointer', waited: 0, pointer: true, gazeDeg: 30 }, { id: 'gaze', waited: 0, pointer: false, gazeDeg: 1 }]
    expect(pickCopy(wants, null)).toBe('pointer')
    expect(pickCopy(wants, 'pointer')).toBe('gaze')
    // A page that waited six frames goes before both.
    expect(pickCopy([...wants, { id: 'old', waited: MOST_WAIT, pointer: false, gazeDeg: 40 }], null)).toBe('old')
  })
})
