import { describe, it, expect } from 'vitest'
import {
  activeCompareKey, compareChoices, compareFileName, compareSnapshot, isComparableKind, orderStates, stateKey,
} from './artifactCompare'

describe('artifact comparison states', () => {
  it('keys Live and a snapshot so one select can carry both', () => {
    expect(stateKey(null)).toBe('live')
    expect(stateKey(3)).toBe('3')
  })

  it('offers Live first, then snapshots newest first, without the state on screen', () => {
    expect(compareChoices(null, [1, 2, 3])).toEqual(['3', '2', '1'])
    expect(compareChoices(2, [1, 2, 3])).toEqual(['live', '3', '1'])
    expect(compareChoices(null, [])).toEqual([])
  })

  it('draws a comparison only against another state the artifact still has', () => {
    expect(activeCompareKey('2', null, [1, 2, 3])).toBe('2')
    expect(activeCompareKey('live', 3, [1, 2, 3])).toBe('live')
    expect(activeCompareKey('', null, [1, 2])).toBeNull()
    expect(activeCompareKey('none', null, [1, 2])).toBeNull()
    expect(activeCompareKey('2', 2, [1, 2])).toBeNull()
    expect(activeCompareKey('live', null, [1, 2])).toBeNull()
    expect(activeCompareKey('9', null, [1, 2])).toBeNull()
    expect(activeCompareKey('2.5', null, [1, 2, 3])).toBeNull()
    expect(activeCompareKey('abc', null, [1, 2])).toBeNull()
  })

  it('puts the older state first and Live last', () => {
    expect(orderStates('3', '1')).toEqual(['1', '3'])
    expect(orderStates('1', '3')).toEqual(['1', '3'])
    expect(orderStates('live', '4')).toEqual(['4', 'live'])
    expect(orderStates('2', 'live')).toEqual(['2', 'live'])
  })

  it('names the snapshot to fetch, and none for Live', () => {
    expect(compareSnapshot('4')).toBe(4)
    expect(compareSnapshot('live')).toBeNull()
    expect(compareSnapshot(null)).toBeNull()
  })

  it('compares text kinds only', () => {
    for (const kind of ['markdown', 'text', 'html', 'widget', 'svg', 'json'] as const) {
      expect(isComparableKind(kind)).toBe(true)
    }
    expect(isComparableKind('image')).toBe(false)
    expect(isComparableKind('webapp')).toBe(false)
  })

  it('gives the viewer a file name whose extension matches the kind', () => {
    expect(compareFileName({ slug: 'plan', kind: 'markdown' })).toBe('plan.md')
    expect(compareFileName({ slug: 'plan', kind: 'widget' })).toBe('plan.html')
    expect(compareFileName({ slug: 'plan', kind: 'text' })).toBe('plan.txt')
  })
})
