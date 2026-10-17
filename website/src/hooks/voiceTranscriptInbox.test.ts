import { describe, it, expect, vi, beforeEach } from 'vitest'
import { subscribeTranscripts, beginTranscription, settleTranscription, redeliverPending, failedCapture, discardFailedCapture, takeFailedCapture, _subscriberCount, _heldCount, type TranscriptSink, type TranscriptResult } from './voiceTranscriptInbox'

/* chat-core P3-b: the inbox used to hold ONE subscriber (the later mount
 * silently detached the earlier). Composers now co-mount — ChatPage's hook stays
 * alive under N split panes, the Members page mounts a pane per DM — so it is a
 * set: `begin` fans out (every mic reads the same in-flight fact), `settle`
 * releases busy everywhere but delivers the TEXT once: to the subscriber whose
 * composer owns the session; else to subscribers that can take unowned text
 * (ChatPage's draft store); else it is HELD until an owner appears, never handed
 * to a subscriber that would drop it. */

type S = TranscriptSink & { begins: number[]; settles: Array<[number, boolean]> }
function sink(owns?: (id: string | null) => boolean, acceptsUnowned = false): S {
  const s: S = {
    begins: [], settles: [],
    begin: vi.fn((r: { id: number }) => { s.begins.push(r.id) }),
    settle: vi.fn((r: { id: number }, deliver: boolean) => { s.settles.push([r.id, deliver]) }),
    owns, acceptsUnowned,
  }
  return s
}

const unsubs: Array<() => void> = []
beforeEach(() => {
  while (unsubs.length) unsubs.pop()!()
  expect(_subscriberCount()).toBe(0)
  discardFailedCapture()
  // Drain anything a previous test left held.
  const drain = sink(() => true)
  const u = subscribeTranscripts(drain)
  return new Promise<void>(r => queueMicrotask(() => { u(); r() }))
})

describe('voiceTranscriptInbox — many subscribers, one delivery', () => {
  it('a later subscriber no longer detaches an earlier one', () => {
    const a = sink(); const b = sink()
    unsubs.push(subscribeTranscripts(a), subscribeTranscripts(b))
    expect(_subscriberCount()).toBe(2)
    const req = beginTranscription('slot-a')
    expect(a.begins).toEqual([req.id])
    expect(b.begins).toEqual([req.id])
  })

  it('delivers the text to the owner only and releases busy on every subscriber', () => {
    const page = sink(id => id === 'slot-a', true)  // ChatPage showing slot-a
    const paneA = sink(id => id === 'slot-a')       // a split pane of the same slot
    const paneB = sink(id => id === 'slot-b')
    unsubs.push(subscribeTranscripts(page), subscribeTranscripts(paneA), subscribeTranscripts(paneB))
    const req = beginTranscription('slot-a')
    settleTranscription({ id: req.id, text: 'hello', sessionId: 'slot-a' })
    // Two claimants: the most recently subscribed (the pane the user is looking
    // at) wins; the page releases busy but must not also splice the text.
    expect(paneA.settles).toEqual([[req.id, true]])
    expect(page.settles).toEqual([[req.id, false]])
    expect(paneB.settles).toEqual([[req.id, false]])
  })

  it('unowned text goes to subscribers that accept it (a draft store), not to panes', () => {
    const page = sink(id => id === 'slot-x', true)
    const pane = sink(id => id === 'slot-y')
    unsubs.push(subscribeTranscripts(page), subscribeTranscripts(pane))
    const req = beginTranscription('slot-gone')
    settleTranscription({ id: req.id, text: 'late', sessionId: 'slot-gone' })
    expect(page.settles).toEqual([[req.id, true]])
    expect(pane.settles).toEqual([[req.id, false]])
  })

  it('unowned text lands in at most ONE taker — two draft-store hosts do not both append it', () => {
    // Two co-mounted ChatPage instances (the ArtifactChatPanel case) both keep a
    // draft store; a transcript delivered to both would be appended twice to the
    // same persisted draft. Same tie-break as the owner path: last subscribed.
    const first = sink(id => id === 'slot-x', true)
    const second = sink(id => id === 'slot-y', true)
    unsubs.push(subscribeTranscripts(first), subscribeTranscripts(second))
    const req = beginTranscription('slot-gone')
    settleTranscription({ id: req.id, text: 'late', sessionId: 'slot-gone' })
    expect(second.settles).toEqual([[req.id, true]])
    expect(first.settles).toEqual([[req.id, false]])
  })

  it('holds unowned text nobody can take, and delivers it when its owner appears (GPT F2)', () => {
    // Pane A recorded; before /api/stt settled the pane switched to slot B.
    const pane = sink(id => id === 'slot-b')
    unsubs.push(subscribeTranscripts(pane))
    const req = beginTranscription('slot-a')
    settleTranscription({ id: req.id, text: 'kept', sessionId: 'slot-a' })
    // Busy released, text NOT delivered — the pane would have dropped it.
    expect(pane.settles).toEqual([[req.id, false]])
    // The pane switches back to slot A: the hook calls redeliverPending.
    pane.owns = id => id === 'slot-a'
    redeliverPending()
    expect(pane.settles).toEqual([[req.id, false], [req.id, true]])
    // Delivered once: a second redeliver finds nothing.
    redeliverPending()
    expect(pane.settles).toHaveLength(2)
  })

  it('holds SEVERAL unowned transcripts, each landing when its own session returns (GPT round 4)', () => {
    // Pane dictates in A, switches to B before STT answers, dictates in B,
    // switches to C before that answers: two results are waiting, for two
    // sessions. Neither may overwrite the other.
    const pane = sink(id => id === 'slot-c')
    unsubs.push(subscribeTranscripts(pane))
    const ra = beginTranscription('slot-a')
    settleTranscription({ id: ra.id, text: 'first', sessionId: 'slot-a' })
    const rb = beginTranscription('slot-b')
    settleTranscription({ id: rb.id, text: 'second', sessionId: 'slot-b' })
    expect(_heldCount()).toBe(2)
    // Back to B: only B's text lands; A's stays held.
    pane.owns = id => id === 'slot-b'
    redeliverPending()
    expect(pane.settles.filter(([, d]) => d)).toEqual([[rb.id, true]])
    expect(_heldCount()).toBe(1)
    // Back to A: the first dictation is still there.
    pane.owns = id => id === 'slot-a'
    redeliverPending()
    expect(pane.settles.filter(([, d]) => d)).toEqual([[rb.id, true], [ra.id, true]])
    expect(_heldCount()).toBe(0)
  })

  it('an error is surfaced to every subscriber — there is no composer for it to land in', () => {
    const a = sink(id => id === 'slot-a'); const b = sink()
    unsubs.push(subscribeTranscripts(a), subscribeTranscripts(b))
    const req = beginTranscription('slot-a')
    settleTranscription({ id: req.id, error: 'boom', sessionId: 'slot-a' })
    expect(a.settles).toEqual([[req.id, true]])
    expect(b.settles).toEqual([[req.id, true]])
  })

  it('holds a result that settles with nobody mounted and hands it to the next owner', async () => {
    const req = beginTranscription('slot-a')
    settleTranscription({ id: req.id, text: 'kept', sessionId: 'slot-a' })
    const a = sink(id => id === 'slot-a')
    unsubs.push(subscribeTranscripts(a))
    await new Promise<void>(r => queueMicrotask(r))
    expect(a.settles).toEqual([[req.id, true]])
  })

  it('unsubscribing removes only that subscriber', () => {
    const a = sink(); const b = sink()
    const ua = subscribeTranscripts(a)
    unsubs.push(subscribeTranscripts(b))
    ua()
    expect(_subscriberCount()).toBe(1)
    const req = beginTranscription('slot-a')
    expect(a.begins).toEqual([])
    expect(b.begins).toEqual([req.id])
  })
})

/* A failed batch transcription keeps its audio in the tab, one capture at a
 * time, so the notice can offer it back: Retry posts the same bytes under a new
 * request, Discard and a new recording let it go. */
describe('voiceTranscriptInbox — the audio of a failed request', () => {
  const capture = (retryable = true) => ({ blob: new Blob(['x'.repeat(200)], { type: 'audio/webm' }), ext: 'webm', retryable })
  type R = S & { released: number[]; results: TranscriptResult[] }
  function keeper(): R {
    const s = sink(() => true) as R
    s.released = []
    s.results = []
    s.settle = vi.fn((r: TranscriptResult, deliver: boolean) => { s.settles.push([r.id, deliver]); s.results.push(r) })
    s.release = vi.fn((id: number) => { s.released.push(id) })
    return s
  }

  it('a failed result carries its blob, and the inbox holds it', () => {
    const s = keeper()
    unsubs.push(subscribeTranscripts(s))
    const req = beginTranscription('slot-a')
    const c = capture()
    settleTranscription({ id: req.id, error: 'failed', sessionId: 'slot-a', capture: c })
    expect(s.results[0].capture?.blob).toBe(c.blob)
    expect(failedCapture(req.id)).toBe(c)
  })

  it('a retry takes the same blob back, releases it, and posts under a new request id', () => {
    const s = keeper()
    unsubs.push(subscribeTranscripts(s))
    const req = beginTranscription('slot-a')
    const c = capture()
    settleTranscription({ id: req.id, error: 'failed', sessionId: 'slot-a', capture: c })

    const taken = takeFailedCapture(req.id)
    expect(taken?.capture.blob).toBe(c.blob)
    expect(taken?.sessionId).toBe('slot-a')
    expect(failedCapture(req.id)).toBeNull()
    expect(s.released).toEqual([req.id])

    const retry = beginTranscription(taken!.sessionId)
    expect(retry.id).not.toBe(req.id)
    // A retry that succeeds leaves nothing behind.
    settleTranscription({ id: retry.id, text: 'hello', sessionId: 'slot-a' })
    expect(failedCapture(retry.id)).toBeNull()
  })

  it('refuses a retry while another transcription is in flight', () => {
    const req = beginTranscription('slot-a')
    settleTranscription({ id: req.id, error: 'failed', sessionId: 'slot-a', capture: capture() })
    const other = beginTranscription('slot-b')
    expect(takeFailedCapture(req.id)).toBeNull()
    expect(failedCapture(req.id)).not.toBeNull()
    settleTranscription({ id: other.id, text: 'x', sessionId: 'slot-b' })
  })

  it('Discard releases it; a stale id leaves a newer failure alone', () => {
    const s = keeper()
    unsubs.push(subscribeTranscripts(s))
    const first = beginTranscription('slot-a')
    settleTranscription({ id: first.id, error: 'failed', sessionId: 'slot-a', capture: capture() })
    discardFailedCapture(first.id + 1000)
    expect(failedCapture(first.id)).not.toBeNull()
    discardFailedCapture(first.id)
    expect(failedCapture(first.id)).toBeNull()
    expect(s.released).toEqual([first.id])
  })

  it('a new recording (discard with no id) releases it', () => {
    const req = beginTranscription('slot-a')
    settleTranscription({ id: req.id, error: 'failed', sessionId: 'slot-a', capture: capture() })
    discardFailedCapture()
    expect(failedCapture(req.id)).toBeNull()
  })

  it('holds only one: a newer failure replaces and releases the older', () => {
    const s = keeper()
    unsubs.push(subscribeTranscripts(s))
    const first = beginTranscription('slot-a')
    settleTranscription({ id: first.id, error: 'failed', sessionId: 'slot-a', capture: capture() })
    const second = beginTranscription('slot-a')
    const c2 = capture(false)
    settleTranscription({ id: second.id, error: 'failed', sessionId: 'slot-a', capture: c2 })
    expect(failedCapture(first.id)).toBeNull()
    expect(failedCapture(second.id)).toBe(c2)
    expect(s.released).toEqual([first.id])
  })

  it('a failure waiting for a composer arrives without audio that was let go meanwhile', async () => {
    const req = beginTranscription('slot-a')
    settleTranscription({ id: req.id, error: 'failed', sessionId: 'slot-a', capture: capture() })
    expect(_heldCount()).toBe(1)
    discardFailedCapture()
    const s = keeper()
    unsubs.push(subscribeTranscripts(s))
    await new Promise<void>(r => queueMicrotask(r))
    expect(s.results).toHaveLength(1)
    expect(s.results[0].error).toBe('failed')
    expect(s.results[0].capture).toBeUndefined()
  })

  it('shows a delivered failure again to a subscriber that mounts while its audio is held', async () => {
    const a = keeper()
    const unsubA = subscribeTranscripts(a)
    const req = beginTranscription('slot-a')
    settleTranscription({ id: req.id, error: 'failed', sessionId: 'slot-a', capture: capture() })
    expect(a.results).toHaveLength(1)
    expect(_heldCount()).toBe(0)
    unsubA()

    const b = keeper()
    unsubs.push(subscribeTranscripts(b))
    expect(b.results).toHaveLength(0)
    await new Promise<void>(r => queueMicrotask(r))
    expect(b.results.map(r => [r.id, r.error])).toEqual([[req.id, 'failed']])
    expect(b.settles).toEqual([[req.id, true]])

    // Once the audio is gone there is nothing to show again.
    discardFailedCapture(req.id)
    const c = keeper()
    unsubs.push(subscribeTranscripts(c))
    await new Promise<void>(r => queueMicrotask(r))
    expect(c.results).toHaveLength(0)
  })
})
