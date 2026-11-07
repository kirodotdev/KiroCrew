import { describe, it, expect } from 'vitest'

import { resumeKeyFor } from './resumeKey'

describe('resumeKeyFor', () => {
  it.each([
    ['a dashboard chat', 'dashboard:chat-2-1789049480', 'dashboard_chat-2-1789049480'],
    ['a channel session', 'slack:1700000000.000001', 'slack_1700000000.000001'],
    ['a key already in its stem form', 'dashboard_chat-2-1789049480', 'dashboard_chat-2-1789049480'],
  ])('names %s by its transcript stem', (_kind, sessionKey, expected) => {
    expect(resumeKeyFor(sessionKey)).toBe(expected)
  })

  it('names a crew member thread by its session key', () => {
    expect(resumeKeyFor('dashboard:member-radar')).toBe('dashboard:member-radar')
  })
})
