import { describe, expect, it, vi } from 'vitest'
import { ThreadLiveStore, threadDrafts } from './threadLiveStore'

const frame = (over: Partial<Parameters<ThreadLiveStore['apply']>[0]> = {}) => ({
  slot: 'member-radar',
  mid: 'm-1',
  thread_slot: 'chat-77-1758524400',
  event: 'opened' as const,
  ...over,
})

describe('ThreadLiveStore', () => {
  it('holds the slot an opened announcement names', () => {
    const store = new ThreadLiveStore()
    const seen = vi.fn()
    store.subscribe('member-radar', 'm-1', seen)
    store.apply(frame({ title: 'The other eight' }))
    expect(store.get('member-radar', 'm-1')).toEqual({
      threadSlot: 'chat-77-1758524400',
      title: 'The other eight',
    })
    expect(seen).toHaveBeenCalledTimes(1)
  })

  it('marks a closed thread closed and keeps its slot', () => {
    const store = new ThreadLiveStore()
    store.apply(frame())
    store.apply(frame({ event: 'closed', summary_mid: 'm-9' }))
    const row = store.get('member-radar', 'm-1')
    // The slot survives the close: a closed thread is still readable, and the
    // back-link in the parent's summary card has to resolve.
    expect(row?.threadSlot).toBe('chat-77-1758524400')
    expect(row?.closedAt).toBeTruthy()
  })

  it('a stale close leaves the REPLACEMENT thread on that message alone', () => {
    // Closing releases the message, so a second thread can be opened on the same
    // mid before the first one's close frame arrives. Applied blind, that close
    // would overwrite the live replacement with the ended thread's slot: the
    // footer would read as ended and point at a conversation nobody is in.
    const store = new ThreadLiveStore()
    const seen = vi.fn()
    store.apply(frame())
    store.apply(frame({ event: 'closed', summary_mid: 'm-9' }))
    store.apply(frame({ thread_slot: 'chat-90-1758525000', title: 'the next one' }))
    store.subscribe('member-radar', 'm-1', seen)
    store.apply(frame({ event: 'closed', summary_mid: 'm-9' }))
    expect(store.get('member-radar', 'm-1')).toEqual({
      threadSlot: 'chat-90-1758525000',
      title: 'the next one',
    })
    expect(seen).not.toHaveBeenCalled()
  })

  it('a close still applies to the thread it names, held or not', () => {
    // The guard is about identity, not about refusing closes: the thread named by
    // the frame is closed, and a close arriving with no row held at all (a reload
    // between the open and the close) still records the one it names.
    const store = new ThreadLiveStore()
    store.apply(frame({ event: 'closed', summary_mid: 'm-9' }))
    expect(store.get('member-radar', 'm-1')?.closedAt).toBeTruthy()
  })

  it('drops a frame that names no slot rather than storing a blank row', () => {
    // A blank row would answer "this message has a thread" with nothing to open,
    // which is worse than answering nothing: the panel would mount an empty pane.
    const store = new ThreadLiveStore()
    const seen = vi.fn()
    store.subscribe('member-radar', 'm-1', seen)
    store.apply(frame({ thread_slot: '' }))
    expect(store.get('member-radar', 'm-1')).toBeUndefined()
    expect(seen).not.toHaveBeenCalled()
  })

  it('keeps threads apart', () => {
    const store = new ThreadLiveStore()
    store.apply(frame({ mid: 'm-2', thread_slot: 'chat-2-1' }))
    expect(store.get('member-radar', 'm-1')).toBeUndefined()
    expect(store.get('member-radar', 'm-2')?.threadSlot).toBe('chat-2-1')
    expect(store.get('member-other', 'm-2')).toBeUndefined()
  })

  it('reset drops every held row and notifies each thread', () => {
    // A reconnect: announcements made while the socket was down were never
    // delivered, so a held row may name a thread that has since closed. The
    // anchor index is refetched instead of trusted from memory.
    const store = new ThreadLiveStore()
    const a = vi.fn()
    const b = vi.fn()
    store.subscribe('member-radar', 'm-1', a)
    store.subscribe('member-radar', 'm-2', b)
    store.apply(frame())
    store.apply(frame({ mid: 'm-2', thread_slot: 'chat-2-1' }))
    a.mockClear(); b.mockClear()
    store.reset()
    expect(store.get('member-radar', 'm-1')).toBeUndefined()
    expect(store.get('member-radar', 'm-2')).toBeUndefined()
    expect(a).toHaveBeenCalledTimes(1)
    expect(b).toHaveBeenCalledTimes(1)
  })

  it('drafts are kept per thread and an empty draft is forgotten', () => {
    threadDrafts.set('member-radar', 'm-1', 'half a thought')
    threadDrafts.set('member-radar', 'm-2', 'another')
    expect(threadDrafts.get('member-radar', 'm-1')).toBe('half a thought')
    expect(threadDrafts.get('member-radar', 'm-2')).toBe('another')
    expect(threadDrafts.get('member-other', 'm-1')).toBe('')
    threadDrafts.set('member-radar', 'm-1', '')
    expect(threadDrafts.get('member-radar', 'm-1')).toBe('')
    threadDrafts.set('member-radar', 'm-2', '')
  })

  it('unsubscribe stops notifications', () => {
    const store = new ThreadLiveStore()
    const seen = vi.fn()
    const off = store.subscribe('member-radar', 'm-1', seen)
    off()
    store.apply(frame())
    expect(seen).not.toHaveBeenCalled()
  })
})
