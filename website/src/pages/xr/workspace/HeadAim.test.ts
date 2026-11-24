import { Group, Mesh } from 'three'
import { describe, expect, it } from 'vitest'
import { carriedBy } from './HeadAim'

/** A panel as `Panel.tsx` draws it, for what a down on each of its parts takes (X10). */
function panel() {
  const g = new Group(); g.userData.xrPanel = 'p1'
  const pageMesh = new Mesh(); pageMesh.userData.xrPage = 'p1'; pageMesh.scale.set(1.56, 1.09, 1)
  const edge = new Mesh()
  const close = new Mesh(); close.userData.xrPress = 'close'
  g.add(pageMesh, edge, close)
  return { pageMesh, edge, close }
}

describe('which down takes the carry (D65, X10)', () => {
  it('a down on an edge or a corner carries, with the hit\'s distance and the page\'s width', () => {
    const { edge } = panel()
    expect(carriedBy({ object: edge, distance: 1.8 })).toEqual({ holdM: 1.8, widthM: 1.56 })
  })
  it('a down on the page\'s own picture, on the close mark, or on nothing does not', () => {
    const { pageMesh, close } = panel()
    expect(carriedBy({ object: pageMesh, distance: 1.75 })).toBeNull()
    expect(carriedBy({ object: close, distance: 1.75 })).toBeNull()
    expect(carriedBy(undefined)).toBeNull()
    expect(carriedBy({ object: new Mesh(), distance: 1 })).toBeNull()
  })
})
