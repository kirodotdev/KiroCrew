import { describe, expect, it } from 'vitest'
import { unfilledRegions } from '../apps/pptx-maker/lib'

const REGION = { name: 'metric-1', x: 96, y: 280, w: 552, h: 640 }

describe('unfilledRegions', () => {
  it('keeps a region no content component has reached', () => {
    const out = unfilledRegions([REGION], [{ svg: '<rect/>', class: 'com.sun.star.drawing.CustomShape' }], 1920)
    expect(out).toEqual([REGION])
  })

  it('drops a region once text lands mostly inside it', () => {
    const text = { svg: '<text/>', text: 'Up to 30%', bbox: { x: 120, y: 300, w: 400, h: 200 } }
    expect(unfilledRegions([REGION], [text], 1920)).toEqual([])
  })

  it('ignores decoration that merely touches the region edge', () => {
    const bar = { svg: '<text/>', text: 'Title', bbox: { x: 0, y: 260, w: 1920, h: 30 } }
    expect(unfilledRegions([REGION], [bar], 1920)).toHaveLength(1)
  })

  it('scales the 1920px canvas into the viewBox units', () => {
    const [scaled] = unfilledRegions([REGION], [], 28000)
    expect(scaled.x).toBeCloseTo((96 * 28000) / 1920)
    expect(scaled.w).toBeCloseTo((552 * 28000) / 1920)
  })

  it('drops malformed agent-authored entries instead of trusting them', () => {
    const bad = [null, 'x', { name: 'a', x: '1', y: 0, w: 10, h: 10 }, { name: 'b', x: 0, y: 0, w: -5, h: 10 }, { ...REGION, x: Number.NaN }]
    expect(unfilledRegions(bad, [], 1920)).toEqual([])
    expect(unfilledRegions('not-a-list', [], 1920)).toEqual([])
  })
})
