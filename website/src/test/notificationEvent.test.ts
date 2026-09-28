import { describe, it, expect } from 'vitest'
import { shouldChimeOnTurnDone, TURN_DONE_KIND, shouldChimeOnPermissionRow } from '../hooks/notificationEvent'

// Only a terminal conversation or an explicit request for input warrants audio.

describe('shouldChimeOnTurnDone', () => {
  it('chimes when the conversation stops, whether active or background', () => {
    expect(shouldChimeOnTurnDone({ slot: 's1', reconnecting: false, continuing: false })).toBe(true)
  })

  it('stays silent at an intermediate turn boundary', () => {
    expect(shouldChimeOnTurnDone({ slot: 's1', reconnecting: false, continuing: true })).toBe(false)
  })

  it('an explicit input request outranks automated work', () => {
    expect(shouldChimeOnTurnDone({ slot: 's1', reconnecting: false, continuing: true, needsInput: true })).toBe(true)
  })

  it('does not replay input requests on reconnect', () => {
    expect(shouldChimeOnTurnDone({ slot: 's1', reconnecting: true, needsInput: true })).toBe(false)
  })

  it('never chimes during reconnect catch-up replay', () => {
    expect(shouldChimeOnTurnDone({ slot: 's1', reconnecting: true })).toBe(false)
  })

  it('never chimes for slot-less events', () => {
    expect(shouldChimeOnTurnDone({ slot: undefined, reconnecting: false })).toBe(false)
    expect(shouldChimeOnTurnDone({ slot: null, reconnecting: false })).toBe(false)
    expect(shouldChimeOnTurnDone({ slot: '', reconnecting: false })).toBe(false)
  })
})

describe('TURN_DONE_KIND', () => {
  it('is a valid sound category key', async () => {
    const { SOUND_CATEGORIES } = await import('../hooks/useNotificationSound')
    expect(SOUND_CATEGORIES).toContain(TURN_DONE_KIND)
  })
})

// The chat runner's `permission` row is the one live signal of a foreground
// tool prompt; its one delivery is the one sound.

describe('shouldChimeOnPermissionRow', () => {
  it('sounds for a new unresolved row', () => {
    expect(shouldChimeOnPermissionRow({ meta: { approval_id: '1' }, reconnecting: false })).toBe(true)
    expect(shouldChimeOnPermissionRow({ meta: null, reconnecting: false })).toBe(true)
    expect(shouldChimeOnPermissionRow({ reconnecting: false })).toBe(true)
  })

  it('stays silent for a row re-appended as resolved', () => {
    expect(shouldChimeOnPermissionRow({ meta: { resolved: 'rejected' }, reconnecting: false })).toBe(false)
  })

  it('never sounds during reconnect catch-up replay', () => {
    expect(shouldChimeOnPermissionRow({ meta: { approval_id: '1' }, reconnecting: true })).toBe(false)
  })
})
