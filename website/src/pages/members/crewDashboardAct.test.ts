// @vitest-environment jsdom
import { describe, expect, it } from 'vitest'
import { ACT_MESSAGE_TYPE, SEND_MESSAGE_TYPE, actText, sendText } from './CrewDynamicDashboard'

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

describe('sendText: what a dashboard page may send into the thread', () => {
  it('takes a trimmed reply of the send type and no other type', () => {
    expect(sendText({ type: SEND_MESSAGE_TYPE, text: '  Approve  ' })).toBe('Approve')
    expect(sendText({ type: ACT_MESSAGE_TYPE, text: 'Approve' })).toBeNull()
    expect(actText({ type: SEND_MESSAGE_TYPE, text: 'Approve' })).toBeNull()
  })
  it('has the same text rules as act', () => {
    expect(sendText({ type: SEND_MESSAGE_TYPE, text: 42 })).toBeNull()
    expect(sendText({ type: SEND_MESSAGE_TYPE, text: '   ' })).toBeNull()
    expect(sendText({ type: SEND_MESSAGE_TYPE, text: 'a'.repeat(601) })).toBeNull()
    expect(sendText({ type: SEND_MESSAGE_TYPE, text: 'a'.repeat(600) })).toBe('a'.repeat(600))
    expect(sendText({ type: SEND_MESSAGE_TYPE, text: 'a\u0007b' })).toBeNull()
    expect(sendText({ type: SEND_MESSAGE_TYPE, text: 'a\nb' })).toBe('a\nb')
    expect(sendText(null)).toBeNull()
  })
  it('names a target nowhere: only the text is read', () => {
    expect(sendText({ type: SEND_MESSAGE_TYPE, text: 'x', slot: 'chat-9', session: 'other' })).toBe('x')
  })
})
