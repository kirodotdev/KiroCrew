import { describe, expect, it } from 'vitest'
import { ACT_MESSAGE_TYPE, actText } from './CrewDynamicDashboard'

describe('actText: what a dashboard page may put in the chat box', () => {
  it('takes a trimmed reply of the act type', () => {
    expect(actText({ type: ACT_MESSAGE_TYPE, text: '  On "x": yes.  ' })).toBe('On "x": yes.')
  })
  it('refuses another type, a non-string, an empty or an over-long text', () => {
    expect(actText({ type: 'kirocrew-dashboard:ready', text: 'hi' })).toBeNull()
    expect(actText({ type: ACT_MESSAGE_TYPE, text: 42 })).toBeNull()
    expect(actText({ type: ACT_MESSAGE_TYPE, text: '   ' })).toBeNull()
    expect(actText({ type: ACT_MESSAGE_TYPE, text: 'a'.repeat(601) })).toBeNull()
    expect(actText(null)).toBeNull()
  })
  it('refuses control characters but keeps a newline', () => {
    expect(actText({ type: ACT_MESSAGE_TYPE, text: 'a\u0007b' })).toBeNull()
    expect(actText({ type: ACT_MESSAGE_TYPE, text: 'a\nb' })).toBe('a\nb')
  })
})
