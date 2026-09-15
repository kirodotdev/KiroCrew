import { describe, it, expect } from 'vitest'
import { extractPromptFromToken, extractSlackContextFromToken } from '../utils/tokenPrompt'

function fakeToken(payload: object): string {
  const encoded = btoa(JSON.stringify(payload)).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
  return `${encoded}.fakesig`
}

describe('extractPromptFromToken', () => {
  it('extracts prompt from valid token', () => {
    const token = fakeToken({ sub: 'user1', prompt: 'hello world' })
    expect(extractPromptFromToken(token)).toBe('hello world')
  })

  it('returns null when token has no prompt field', () => {
    const token = fakeToken({ sub: 'user1', exp: 9999999999 })
    expect(extractPromptFromToken(token)).toBeNull()
  })

  it('returns null for empty prompt', () => {
    const token = fakeToken({ sub: 'user1', prompt: '' })
    expect(extractPromptFromToken(token)).toBeNull()
  })

  it('returns null for malformed token (not base64)', () => {
    expect(extractPromptFromToken('not-a-valid-token')).toBeNull()
  })

  it('returns null for empty string', () => {
    expect(extractPromptFromToken('')).toBeNull()
  })

  it('handles special characters in prompt', () => {
    const prompt = "What's the status of @user? <script>alert(1)</script>"
    const token = fakeToken({ sub: 'user1', prompt })
    expect(extractPromptFromToken(token)).toBe(prompt)
  })

  it('handles base64url padding correctly', () => {
    // Prompt that produces padding characters in base64
    const token = fakeToken({ sub: 'u', prompt: 'a' })
    expect(extractPromptFromToken(token)).toBe('a')
  })
})

describe('extractSlackContextFromToken', () => {
  it('extracts session_key, channel, thread_ts when present', () => {
    const token = fakeToken({
      sub: 'u', prompt: 'hi',
      session_key: 'dashboard:chat-2-9', channel: 'C9', thread_ts: '1700.5',
    })
    expect(extractSlackContextFromToken(token)).toEqual({
      sessionKey: 'dashboard:chat-2-9', channel: 'C9', threadTs: '1700.5',
    })
  })

  it('returns nulls when claims absent', () => {
    const token = fakeToken({ sub: 'u', prompt: 'hi' })
    expect(extractSlackContextFromToken(token)).toEqual({
      sessionKey: null, channel: null, threadTs: null,
    })
  })

  it('returns only the claims that are present (fresh thread, no session)', () => {
    const token = fakeToken({ sub: 'u', prompt: 'hi', channel: 'C9', thread_ts: '1800.9' })
    expect(extractSlackContextFromToken(token)).toEqual({
      sessionKey: null, channel: 'C9', threadTs: '1800.9',
    })
  })

  it('rejects non-string / oversized claims', () => {
    const token = fakeToken({ sub: 'u', channel: 123, thread_ts: 'x'.repeat(500) })
    expect(extractSlackContextFromToken(token)).toEqual({
      sessionKey: null, channel: null, threadTs: null,
    })
  })

  it('returns nulls for malformed token', () => {
    expect(extractSlackContextFromToken('not-a-token')).toEqual({
      sessionKey: null, channel: null, threadTs: null,
    })
  })
})
