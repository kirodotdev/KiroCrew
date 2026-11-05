import { describe, it, expect } from 'vitest'

import { isUntitledSession, sessionDisplayTitle } from '../utils/sessionDisplayTitle'

// An open untitled slot arrives from the backend titled "New Session…"
// (NEW_SESSION_TITLE). Every surface that names a session goes through this
// helper, so the sidebar, reference chip and notification read the same
// "Untitled session" label the command bar shows, and the command bar keeps
// the key findable for it (#29748).
describe('sessionDisplayTitle — the backend placeholder title (#29748)', () => {
  it.each(['New Session…', 'New Session...'])('names a slot titled %s "Untitled session"', (title) => {
    const slot = { key: 'chat-1792085678', title }
    expect(isUntitledSession(slot)).toBe(true)
    expect(sessionDisplayTitle(slot)).toBe('Untitled session')
  })

  it('names a missing or key-equal title the same way', () => {
    expect(sessionDisplayTitle({ key: 'chat-1', title: null })).toBe('Untitled session')
    expect(sessionDisplayTitle({ key: 'chat-1', title: '' })).toBe('Untitled session')
    expect(sessionDisplayTitle({ key: 'chat-1', title: 'chat-1' })).toBe('Untitled session')
  })

  it('keeps a real title, including one that only starts like the placeholder', () => {
    for (const title of ['New Session Planning', 'New Session', 'Fix the sidebar']) {
      const slot = { key: 'chat-1', title }
      expect(isUntitledSession(slot)).toBe(false)
      expect(sessionDisplayTitle(slot)).toBe(title)
    }
  })
})
