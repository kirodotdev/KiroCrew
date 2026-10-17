import { describe, expect, it } from 'vitest'
import { embedReadPath, isEmbedCursor, mergeEmbedRead } from '../app-sdk/embedIncrementalRead'

const digest = 'f'.repeat(32)
const sent = { from: 4, since: 7, digest }

describe('embedIncrementalRead', () => {
  it('asks for a cursor until one is held, then echoes it', () => {
    expect(embedReadPath('s 1', 200, null)).toBe('/api/chat/slots/s%201?limit=200&cursor=1')
    expect(embedReadPath('s', 200, sent)).toBe(`/api/chat/slots/s?limit=200&from=4&since=7&digest=${digest}`)
  })

  it('only echoes a well-formed cursor', () => {
    expect(isEmbedCursor(sent)).toBe(true)
    expect(isEmbedCursor({ from: 4, since: 3, digest })).toBe(false)
    expect(isEmbedCursor({ from: 4, since: 7, digest: 'x&y=1' })).toBe(false)
    expect(isEmbedCursor({ from: -1, since: 7, digest })).toBe(false)
    expect(isEmbedCursor(undefined)).toBe(false)
  })

  it('keeps the held rows between the new window start and since', () => {
    const held = { messages: ['r4', 'r5', 'r6', 'r7'] }
    const res = { messages: ['r7b', 'r8'], next_before: 5, delta: { from: 4, since: 7 } }
    expect(mergeEmbedRead(held, sent, res)?.messages).toEqual(['r5', 'r6', 'r7b', 'r8'])
  })

  it('keeps nothing once the window moved past since', () => {
    const held = { messages: ['r4', 'r5', 'r6', 'r7'] }
    const res = { messages: ['r9'], next_before: 9, delta: { from: 4, since: 7 } }
    expect(mergeEmbedRead(held, sent, res)?.messages).toEqual(['r9'])
  })

  it('replaces the held page with a full one', () => {
    const res = { messages: ['x'], next_before: 0 }
    expect(mergeEmbedRead({ messages: ['a'] }, sent, res)).toBe(res)
  })

  it('refuses a delta it cannot place', () => {
    const held = { messages: ['r4', 'r5'] }
    const res = { messages: ['r7'], next_before: 4, delta: { from: 4, since: 7 } }
    expect(mergeEmbedRead(held, sent, res)).toBeNull()
    expect(mergeEmbedRead(undefined, sent, res)).toBeNull()
    expect(mergeEmbedRead({ messages: ['a', 'b', 'c'] }, null, res)).toBeNull()
    expect(mergeEmbedRead({ messages: ['a', 'b', 'c'] }, sent, { ...res, delta: { from: 3, since: 7 } })).toBeNull()
  })
})
