import { afterEach, describe, expect, it } from 'vitest'
import { store } from '../../store'
import { sseSlots } from '../../store/dashboardSlice'
import type { ChatSlot } from '../../types'
import { crewWorkerRoot } from './attention'

const row = (key: string, extra: Partial<ChatSlot> = {}): ChatSlot =>
  ({ key, title: key, messages: 1, running: false, ...extra }) as ChatSlot

/** A crew member's alerts point at its conductor only while that conductor's
 *  approval card can show them, which is while the conductor is a live slot. With
 *  the conductor closed the crew member is a row of the person's lists again, so
 *  its approval alerts in its own name and slot like any chat's. */
describe('crewWorkerRoot', () => {
  afterEach(() => { store.dispatch(sseSlots([])) })

  it('names the live conductor of a crew member', () => {
    store.dispatch(sseSlots([row('root', { crew_mode_root: true }), row('w1', { crew_root: 'root' })]))
    expect(crewWorkerRoot('w1')).toBe('root')
  })

  it('names nothing for a crew member whose conductor is closed', () => {
    store.dispatch(sseSlots([row('w1', { crew_root: 'closed-root' }), row('plain')]))
    expect(crewWorkerRoot('w1')).toBe('')
  })

  it('names nothing for a plain chat, a conductor, an unknown or an empty key', () => {
    store.dispatch(sseSlots([row('root', { crew_mode_root: true }), row('plain')]))
    expect(crewWorkerRoot('plain')).toBe('')
    expect(crewWorkerRoot('root')).toBe('')
    expect(crewWorkerRoot('nope')).toBe('')
    expect(crewWorkerRoot('')).toBe('')
  })
})
