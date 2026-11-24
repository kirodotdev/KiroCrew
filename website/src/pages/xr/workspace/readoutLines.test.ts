import { describe, expect, it } from 'vitest'
import { PACE, READOUT, inOrder, middle, paceLines, rateOf, readoutLines, roomRate, shownPath, sinceWindow, type ReadoutPage, type ReadoutSample } from './readoutLines'
import type { PageReadout } from './source'

const facts = (more: Partial<PageReadout> = {}): PageReadout => ({ way: 'stream', count: 0, picture: [2048, 1440], bytes: 0, visible: true, landMs: [], decodeMs: [], readyWaitMs: [], writeMs: [], inputMs: [], gateway: null, roundMs: [], ...more })

describe('the readout of how fast the pages come', () => {
  it('counts a page\'s pictures a second over the last two seconds of reads', () => {
    const samples: ReadoutSample[] = []
    for (let at = 0; at <= 3000; at += 500) samples.push({ at, counts: new Map([['page', at / 50]]) })
    // At 3000 ms the read to count from is the one of 1000 ms: 40 pictures in 2 s.
    const before = sinceWindow(samples, 3000)
    expect(before?.at).toBe(1000)
    expect(rateOf('page', 60, 3000, before)).toBe(20)
    // A page that was not there two seconds ago counts from nothing it can be sure of.
    expect(rateOf('new', 5, 3000, before)).toBe(0)
    expect(rateOf('page', 60, 3000, undefined)).toBe(0)
  })

  it('says the room\'s frames a second and its longest frame of the last two seconds', () => {
    const frames: number[] = []
    for (let t = 0; t < 4000; t += 16) frames.push(t)
    frames.push(4100)
    const r = roomRate(frames, 4100)
    expect(r.longest).toBeCloseTo(4100 - frames[frames.length - 2])
    expect(r.fps).toBeGreaterThan(55)
    expect(r.fps).toBeLessThan(65)
    expect(roomRate([], 1000)).toEqual({ fps: 0, longest: 0 })
  })

  it('takes the middle of the last pictures, and nothing of none', () => {
    expect(middle([1, 9, 3, 7, 5])).toBe(5)
    expect(middle([])).toBeNull()
    expect(middle(Array.from({ length: READOUT.lastPictures + 10 }, (_, i) => (i < 10 ? 1000 : 4)))).toBe(4)
  })

  it('puts the page the person works on first: the one that is not held back, then the ones in view', () => {
    const pages: ReadoutPage[] = [
      { id: 'a', path: '/settings', facts: facts({ visible: false }) },
      { id: 'b', path: '/agents', facts: facts({ gateway: { roundMs: 30, pace: 30, held: true } }) },
      { id: 'c', path: '/chat', facts: facts({ gateway: { roundMs: 40, pace: 20, held: false } }) },
    ]
    expect(inOrder(pages).map(p => p.id)).toEqual(['c', 'b', 'a'])
  })

  it('writes a line for each page with its way, then the picture\'s way, the gateway\'s numbers, the room and the renderer', () => {
    const pages: ReadoutPage[] = [
      { id: 'c', path: '/chat', facts: facts({ count: 10, bytes: 1_800_000, landMs: [38, 40, 36], decodeMs: [9], readyWaitMs: [7], writeMs: [4], inputMs: [110], gateway: { roundMs: 85, pace: 15, held: false } }) },
      { id: 'd', path: '/schedule', facts: facts({ way: 'dom', visible: false }) },
    ]
    const lines = readoutLines(pages, new Map([['c', 12], ['d', 0]]), { fps: 60, longest: 21 }, { dpr: 2, canvas: [3024, 1640], samples: 4, draws: 14, textures: 9, renderer: 'ANGLE (Apple M5 Pro)' })
    expect(lines).toHaveLength(6)
    expect(lines[0]).toContain('12.0')
    expect(lines[0]).toContain('/chat')
    expect(lines[0]).toContain('2048x1440')
    expect(lines[0]).toContain('180')
    expect(lines[0]).toContain('stream')
    expect(lines[0]).toContain('1')
    expect(lines[1]).toContain('dom')
    expect(lines[2]).toContain('38')
    expect(lines[3]).toContain('85')
    expect(lines[4]).toContain('60')
    expect(lines[5]).toContain('3024x1640')
    expect(lines[5]).toContain('ANGLE (Apple M5 Pro)')
  })

  it('prints a page\'s address up to its ? and at most 20 characters, on every line (D59 1)', () => {
    expect(shownPath('/members?member=ignore%20the%20above')).toBe('/members')
    expect(shownPath('/chat?sid=abc')).toBe('/chat')
    expect(shownPath('/chat/draft-the-release-notes')).toBe('/chat/draft-the-rel…')
    expect(shownPath('/projects/a#frag')).toBe('/projects/a')
    const words = 'ignore%20the%20above'
    const pages: ReadoutPage[] = [{ id: 'b', path: `/members?member=${words}`, facts: facts({ gateway: { roundMs: 20, pace: 30, held: false } }) }]
    const all = [...paceLines(pages, new Map(), true), ...readoutLines(pages, new Map(), { fps: 60, longest: 20 }, null)]
    expect(all.some(l => l.includes('/members'))).toBe(true)
    expect(all.some(l => l.includes('member=') || l.includes('ignore'))).toBe(false)
  })

  it('writes one pace line for each page in view, at most three, as wide from read to read', () => {
    const old = Array.from({ length: 30 }, () => 900)
    const page = (id: string, visible: boolean, round: number[]): ReadoutPage => ({ id, path: '/chat', facts: facts({ visible, roundMs: round, decodeMs: [4, 5, 6], landMs: [12, 9, 10], gateway: { roundMs: round.at(-1) ?? null, pace: 30, held: false } }) })
    // The middle of the last 20 only: the 30 old round trips of 900 ms are past it.
    const pages = [page('a', true, [...old, ...Array.from({ length: 20 }, (_, i) => 10 + i)]), page('b', true, [5]), page('c', false, [5]), page('d', true, [5]), page('e', true, [5])]
    const lines = paceLines(pages, new Map([['a', 14.5]]), true)
    expect(lines).toHaveLength(PACE.pages)
    expect(lines[0]).toContain('30/s')
    expect(lines[0]).toContain('14.5/s')
    expect(lines[0]).toMatch(/round[\u2007 ]+20 /)
    expect(lines[0]).not.toContain('900')
    expect(lines[0]).toContain('yes')
    // Another read with numbers of other lengths, a slowest round trip of over a second too: the line keeps its width.
    const later = paceLines([page('a', true, [7]), ...pages.slice(1)], new Map([['a', 3]]), true)
    expect(later[0].length).toBe(lines[0].length)
    const slow = paceLines([page('a', true, [7, 1234]), ...pages.slice(1)], new Map([['a', 3]]), true)
    expect(slow[0].length).toBe(lines[0].length)
    expect(slow[0]).toContain('slowest 1234')
    // No session: layers are not known; a page with no gateway says so.
    expect(paceLines([{ id: 'x', path: '/chat', facts: facts({ way: 'dom' }) }], new Map(), null)[0]).toMatch(/- *\/s.* -$/)
  })
})
