import { afterEach, describe, expect, it } from 'vitest'
import { store } from '../../store'
import { sseSlots } from '../../store/dashboardSlice'
import type { ChatSlot } from '../../types'
import { isCrewWorkerSlot } from './attention'
import { crewModePreviewOn } from '../../test/crewModePreview'
import { PREVIEW_CREW_MODE } from '../../utils/previewFlags'

const row = (key: string, extra: Partial<ChatSlot> = {}): ChatSlot =>
  ({ key, title: key, messages: 1, running: false, ...extra }) as ChatSlot

/** A crew member's alerts are muted only while its conductor's approval card can
 *  show them, which is while that conductor is a live slot. With the conductor
 *  closed the crew member is a row of the person's lists again, so its approval
 *  must reach the person like any chat's. */
describe('isCrewWorkerSlot', () => {
  afterEach(() => { store.dispatch(sseSlots([])) })

  it('mutes a crew member whose conductor is open', () => {
    store.dispatch(sseSlots([row('root', { crew_mode_root: true }), row('w1', { crew_root: 'root' })]))
    expect(isCrewWorkerSlot('w1')).toBe(true)
  })

  it('lets a crew member whose conductor is closed alert like any chat', () => {
    store.dispatch(sseSlots([row('w1', { crew_root: 'closed-root' }), row('plain')]))
    expect(isCrewWorkerSlot('w1')).toBe(false)
  })

  it('never mutes a plain chat, a conductor, an unknown or an empty key', () => {
    store.dispatch(sseSlots([row('root', { crew_mode_root: true }), row('plain')]))
    expect(isCrewWorkerSlot('plain')).toBe(false)
    expect(isCrewWorkerSlot('root')).toBe(false)
    expect(isCrewWorkerSlot('nope')).toBe(false)
    expect(isCrewWorkerSlot('')).toBe(false)
  })

  it('with the Crew Mode preview off, lets a crew member alert like any chat', () => {
    localStorage.removeItem(PREVIEW_CREW_MODE)
    store.dispatch(sseSlots([row('root', { crew_mode_root: true }), row('w1', { crew_root: 'root' })]))
    expect(isCrewWorkerSlot('w1')).toBe(false)
  })
})

// Last, after the file's own hooks that clear localStorage: Crew Mode is a feature preview.
crewModePreviewOn()
