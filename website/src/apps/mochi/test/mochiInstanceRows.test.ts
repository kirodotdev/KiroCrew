/**
 * The Settings switcher lists only instances you could actually attach to.
 *
 * Only CONNECTED instances are offered, because Mochi never opens a tunnel itself
 * — listing a disconnected one would offer something that cannot work. The single
 * exception is the currently-SAVED choice: `petInstance` is stored opaquely and
 * survives an absent instance on purpose, so hiding it would make a remembered
 * choice look lost (its row would vanish AND nothing would be highlighted, since
 * the saved value is not 'self').
 */
import { describe, it, expect } from 'vitest'

import { visibleRows, isUsable } from '../panel/MochiInstances'
import type { CoreInstance } from '../panel/panelBridge'

const live = (id: string, port = 7778): CoreInstance =>
  ({ id, name: id, local_port: port, status: { state: 'connected' } }) as CoreInstance
const down = (id: string, state: string = 'disconnected'): CoreInstance =>
  ({ id, name: id, local_port: 0, status: { state } }) as unknown as CoreInstance

describe('visibleRows', () => {
  it('lists connected instances', () => {
    expect(visibleRows([live('a'), live('b')], 'self').map(i => i.id)).toEqual(['a', 'b'])
  })

  it('hides every instance that is not connected', () => {
    for (const state of ['disconnected', 'connecting', 'error']) {
      expect(visibleRows([down('a', state)], 'self')).toEqual([])
    }
  })

  it('hides a connected instance with no allocated port', () => {
    const noPort = { id: 'a', name: 'a', local_port: 0, status: { state: 'connected' } }
    expect(visibleRows([noPort as CoreInstance], 'self')).toEqual([])
  })

  it('KEEPS the saved choice even when it has gone away', () => {
    // Otherwise the row vanishes and nothing is highlighted — the user's
    // remembered choice would look lost.
    expect(visibleRows([down('a'), live('b')], 'a').map(i => i.id)).toEqual(['a', 'b'])
  })

  it('does not duplicate the saved choice when it is also connected', () => {
    expect(visibleRows([live('a'), live('b')], 'a').map(i => i.id)).toEqual(['a', 'b'])
  })

  it("does not resurrect OTHER down instances just because one is saved", () => {
    expect(visibleRows([down('a'), down('b'), live('c')], 'a').map(i => i.id)).toEqual(['a', 'c'])
  })

  it('an empty list stays empty (self is rendered separately)', () => {
    expect(visibleRows([], 'self')).toEqual([])
  })
})

describe('isUsable', () => {
  it('requires BOTH a connected tunnel and a real port', () => {
    expect(isUsable(live('a'))).toBe(true)
    expect(isUsable(down('a'))).toBe(false)
    expect(isUsable({ id: 'a', local_port: 7778 } as CoreInstance)).toBe(false)
    expect(isUsable({ id: 'a', status: { state: 'connected' } } as CoreInstance)).toBe(false)
  })
})
