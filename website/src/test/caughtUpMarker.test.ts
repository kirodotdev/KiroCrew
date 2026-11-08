// Feature: the caught-up marker and the unread run it resolves to.
//
// The marker is the newest message a reader saw at the bottom of a crewmate's
// chat. `findUnreadRun` turns it into the boundary the pane lands on: the first
// message after the marker, and how many follow. Every "no run" answer is
// deliberate: a first visit, a marker whose row is gone, or a reader who was
// already caught up must all keep the default bottom placement.
import { describe, it, expect, beforeEach } from 'vitest'
import {
  SEEN_TAIL_KEY_PREFIX,
  durableMessageId,
  findUnreadRun,
  isProvisionalRow,
  loadSeenTail,
  newestDurableMessage,
  saveSeenTail,
} from '../chat-core/transcript/caughtUpMarker'
import type { ChatMessage } from '../types'

const msg = (ts: string, role = 'assistant', meta?: Record<string, unknown>): ChatMessage =>
  ({ role, content: ts, cls: '', ts, ...(meta ? { meta } : {}) })

const T = ['2026-10-23T09:00:00Z', '2026-10-23T09:07:00Z', '2026-10-23T09:14:00Z', '2026-10-23T09:21:00Z']

describe('durableMessageId', () => {
  it('is the server mid alone when the row carries one, else the ts', () => {
    expect(durableMessageId(msg(T[0]))).toBe(T[0])
    expect(durableMessageId(msg(T[0], 'assistant', { mid: 'm1' }))).toBe('m:m1')
  })
  it('matches a confirmed send to its hydrated copy across a ts change', () => {
    // The sent bubble keeps its client ts after confirmation stamps the mid;
    // the next visit's hydrated row has the server ts. Same mid, same id.
    const sentThenConfirmed = msg('2026-10-23T09:00:00.123Z', 'user', { mid: 'm9', sendId: 's1' })
    const hydrated = msg('2026-10-23T09:00:00.456Z', 'user', { mid: 'm9' })
    expect(durableMessageId(sentThenConfirmed)).toBe(durableMessageId(hydrated))
    expect(findUnreadRun([msg(T[0]), hydrated, msg(T[2])], durableMessageId(sentThenConfirmed))).toEqual({ startId: T[2], count: 1 })
  })
  it('ignores the client-side clientTs: a reload does not round-trip it', () => {
    expect(durableMessageId(msg(T[0], 'user', { clientTs: 'c-1' }))).toBe(T[0])
  })
  it('is null for a row with no ts yet', () => {
    expect(durableMessageId({ role: 'assistant', content: 'x', cls: '' } as ChatMessage)).toBeNull()
  })
})

describe('saveSeenTail / loadSeenTail', () => {
  beforeEach(() => { localStorage.clear() })

  it('round-trips per slot under the swept prefix', () => {
    saveSeenTail('member-radar', T[1])
    expect(loadSeenTail('member-radar')).toBe(T[1])
    expect(localStorage.getItem(`${SEEN_TAIL_KEY_PREFIX}member-radar`)).toBe(T[1])
    expect(loadSeenTail('member-scout')).toBeNull()
  })
  it('treats an empty slot or id as nothing to do', () => {
    saveSeenTail('', T[1])
    saveSeenTail('member-radar', '')
    expect(localStorage.length).toBe(0)
    expect(loadSeenTail('')).toBeNull()
  })
})

describe('findUnreadRun', () => {
  const messages = [msg(T[0], 'user'), msg(T[1]), msg(T[2]), msg(T[3])]

  it('starts at the message after the marker and counts the rest', () => {
    expect(findUnreadRun(messages, T[1])).toEqual({ startId: T[2], count: 2 })
  })
  it('is null with no marker (a first visit)', () => {
    expect(findUnreadRun(messages, null)).toBeNull()
  })
  it('is null when the marker is the newest row (caught up)', () => {
    expect(findUnreadRun(messages, T[3])).toBeNull()
  })
  it('is null when the marker row is gone: a boundary is never guessed', () => {
    expect(findUnreadRun(messages, '2020-01-01T00:00:00Z')).toBeNull()
  })
  it('is null on an empty transcript', () => {
    expect(findUnreadRun([], T[1])).toBeNull()
  })
  it('matches the newest occurrence when ids repeat', () => {
    // Two rows with one ts (a coarse clock): the marker names the later one, so
    // the run starts after IT, not after the first row that happens to match.
    const dup = [msg(T[0]), msg(T[1]), msg(T[1]), msg(T[2])]
    expect(findUnreadRun(dup, T[1])).toEqual({ startId: T[2], count: 1 })
  })
  it("leaves the reader's own provisional sends out of the run", () => {
    // A queued send drawn as a bubble after the marker is theirs, not unseen,
    // and its id is renamed when the server's copy lands.
    const queued = msg(T[2], 'user', { queuedBubble: true, queueId: 'q1' })
    expect(findUnreadRun([msg(T[0]), msg(T[1]), queued], T[1])).toBeNull()
    expect(findUnreadRun([msg(T[0]), msg(T[1]), queued, msg(T[3])], T[1])).toEqual({ startId: T[3], count: 1 })
  })

  it('needs a durable id on the first unseen message', () => {
    const noTs = [msg(T[0]), { role: 'assistant', content: 'streaming', cls: '' } as ChatMessage]
    expect(findUnreadRun(noTs, T[0])).toBeNull()
  })
})

describe('newestDurableMessage', () => {
  it('skips rows the client still holds provisionally', () => {
    const server = msg(T[1])
    const optimistic = msg(T[2], 'user', { optimistic: true, sendId: 's1' })
    const queued = msg(T[3], 'user', { queuedBubble: true, queueId: 'q1' })
    const streaming = { role: 'streaming', content: '...', cls: '', ts: T[3] } as ChatMessage
    expect(isProvisionalRow(optimistic)).toBe(true)
    expect(isProvisionalRow(queued)).toBe(true)
    expect(isProvisionalRow(streaming)).toBe(true)
    expect(isProvisionalRow(server)).toBe(false)
    expect(newestDurableMessage([msg(T[0], 'user'), server, optimistic, queued, streaming])).toBe(server)
  })
  it('skips a client-only row with no ts, such as a local send-failure notice', () => {
    const server = msg(T[1])
    const localError = { role: 'error', content: 'offline', cls: '' } as ChatMessage
    expect(newestDurableMessage([server, localError])).toBe(server)
  })
  it('is null when nothing durable is on hand', () => {
    expect(newestDurableMessage([])).toBeNull()
    expect(newestDurableMessage([msg(T[2], 'user', { optimistic: true })])).toBeNull()
  })
})
