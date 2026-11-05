import { describe, expect, it } from 'vitest'

import { dndDropY, railDropTarget } from './railPinDrop'

/** A rail DOM with each element's vertical span given explicitly (jsdom lays nothing out). */
function rail(groups: { id: string; top: number; bottom: number; rows: { id: string; top: number; bottom: number }[] }[]) {
  const root = document.createElement('div')
  const rect = (top: number, bottom: number) => () => ({ top, bottom, height: bottom - top, left: 0, right: 0, width: 0, x: 0, y: top, toJSON: () => ({}) }) as DOMRect
  for (const g of groups) {
    const el = document.createElement('div')
    el.dataset.railGroup = g.id
    el.getBoundingClientRect = rect(g.top, g.bottom)
    for (const r of g.rows) {
      const row = document.createElement('div')
      row.dataset.railItem = r.id
      row.getBoundingClientRect = rect(r.top, r.bottom)
      el.appendChild(row)
    }
    root.appendChild(el)
  }
  return root
}

const ROOT = () => rail([
  { id: 'workspace', top: 0, bottom: 100, rows: [{ id: 'chat', top: 20, bottom: 50 }, { id: 'members', top: 50, bottom: 80 }] },
  { id: 'apps', top: 100, bottom: 200, rows: [{ id: 'apps', top: 120, bottom: 150 }] },
])

describe('railDropTarget', () => {
  it('lands before the first row whose middle is below the point', () => {
    expect(railDropTarget(30, ROOT())).toEqual({ groupId: 'workspace', beforeId: 'chat' })
    expect(railDropTarget(40, ROOT())).toEqual({ groupId: 'workspace', beforeId: 'members' })
  })

  it("appends below a group's last row", () => {
    expect(railDropTarget(90, ROOT())).toEqual({ groupId: 'workspace', beforeId: null })
    expect(railDropTarget(180, ROOT())).toEqual({ groupId: 'apps', beforeId: null })
  })

  it('clamps points above and below the groups', () => {
    expect(railDropTarget(-50, ROOT())).toEqual({ groupId: 'workspace', beforeId: 'chat' })
    expect(railDropTarget(900, ROOT())).toEqual({ groupId: 'apps', beforeId: null })
  })

  it('returns null with no rail on screen', () => {
    expect(railDropTarget(10, document.createElement('div'))).toBeNull()
  })
})

describe('dndDropY', () => {
  it('adds the drag delta to the start point of a pointer or touch drag', () => {
    expect(dndDropY({ clientY: 40 } as unknown as Event, 25)).toBe(65)
    expect(dndDropY({ touches: [{ clientY: 10 }] } as unknown as Event, 5)).toBe(15)
    expect(dndDropY(null, 5)).toBeNull()
  })
})
