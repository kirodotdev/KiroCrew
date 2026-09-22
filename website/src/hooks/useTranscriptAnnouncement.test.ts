/**
 * WHEN the transcript's live region speaks, which is the whole point of the hook.
 *
 * Each case here is a way the region used to say the wrong thing: on every
 * streaming flush, on a session switch, on first load, or forever after one
 * reply.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { renderHook, act } from '@testing-library/react'

import { ANNOUNCEMENT_HOLD_MS, useTranscriptAnnouncement } from './useTranscriptAnnouncement'

const streaming = { role: 'streaming', content: 'partial' }
const settled = (content: string) => ({ role: 'assistant', content })

beforeEach(() => { vi.useFakeTimers() })
afterEach(() => { vi.useRealTimers() })

describe('useTranscriptAnnouncement', () => {
  it('stays silent while a turn is streaming, however many flushes arrive', () => {
    const { result, rerender } = renderHook(
      ({ msg }) => useTranscriptAnnouncement(msg, msg.role === 'streaming').announcement,
      { initialProps: { msg: streaming } },
    )
    expect(result.current).toBe('')
    rerender({ msg: { role: 'streaming', content: 'partial and more' } })
    expect(result.current).toBe('')
  })

  it('speaks the settled reply as text once the turn lands', () => {
    const { result, rerender } = renderHook(
      ({ msg }) => useTranscriptAnnouncement(msg, msg.role === 'streaming').announcement,
      { initialProps: { msg: streaming } },
    )
    rerender({ msg: settled('## Done\n\nRan `npm ci` — see [the log](https://x/y).') })
    // Named like the transcript's heading for the reply, and the markdown
    // punctuation the bubble never shows is absent.
    expect(result.current).toBe('Kiro Crew: Done\nRan npm ci — see the log.')
  })

  it('says nothing on first load or a session switch, only on a landing', () => {
    // No streaming state preceded these, so the transcript merely ENDS in a
    // reply — reading it back would announce a conversation the user opened,
    // not one that just answered them.
    const { result, rerender } = renderHook(
      ({ msg }) => useTranscriptAnnouncement(msg, msg.role === 'streaming').announcement,
      { initialProps: { msg: settled('an old reply') } },
    )
    expect(result.current).toBe('')
    rerender({ msg: settled('a different old reply') })
    expect(result.current).toBe('')
  })

  it('releases the text so it stops being a second copy of the reply', () => {
    const { result, rerender } = renderHook(
      ({ msg }) => useTranscriptAnnouncement(msg, msg.role === 'streaming').announcement,
      { initialProps: { msg: streaming } },
    )
    rerender({ msg: settled('all done') })
    expect(result.current).toBe('Kiro Crew: all done')
    act(() => { vi.advanceTimersByTime(ANNOUNCEMENT_HOLD_MS - 1) })
    expect(result.current).toBe('Kiro Crew: all done')
    act(() => { vi.advanceTimersByTime(1) })
    expect(result.current).toBe('')
  })

  it('speaks a second identical reply, because the region was cleared between', () => {
    const { result, rerender } = renderHook(
      ({ msg }) => useTranscriptAnnouncement(msg, msg.role === 'streaming').announcement,
      { initialProps: { msg: streaming } },
    )
    rerender({ msg: settled('same answer') })
    expect(result.current).toBe('Kiro Crew: same answer')
    rerender({ msg: streaming })
    expect(result.current).toBe('')
    rerender({ msg: settled('same answer') })
    expect(result.current).toBe('Kiro Crew: same answer')
  })

  it('stays silent when a turn ends without a reply', () => {
    const { result, rerender } = renderHook(
      ({ msg }) => useTranscriptAnnouncement(msg, msg.role === 'streaming').announcement,
      { initialProps: { msg: streaming } },
    )
    rerender({ msg: { role: 'stop_event', content: '' } })
    expect(result.current).toBe('')
  })

  it('speaks the sent message with the user label, and keeps it when the reply starts streaming', () => {
    const { result, rerender } = renderHook(
      ({ msg }) => useTranscriptAnnouncement(msg, msg.role === 'streaming'),
      { initialProps: { msg: settled('an old reply') as { role: string, content: string } } },
    )
    act(() => { result.current.announceSent('Run the **tests**') })
    expect(result.current.announcement).toBe('You: Run the tests')
    // The reply begins almost at once; clearing here would cut the sent message off.
    rerender({ msg: streaming })
    expect(result.current.announcement).toBe('You: Run the tests')
    rerender({ msg: settled('They pass.') })
    expect(result.current.announcement).toBe('Kiro Crew: They pass.')
  })

  it("speaks a reply under the host's speaker name when one is given", () => {
    // A crewmate's chat: the reply is the crewmate's, not the product's.
    const { result, rerender } = renderHook(
      ({ msg }) => useTranscriptAnnouncement(msg, msg.role === 'streaming', 'Radar').announcement,
      { initialProps: { msg: streaming as { role: string, content: string } } },
    )
    rerender({ msg: settled('Done.') })
    expect(result.current).toBe('Radar: Done.')
  })

  it('releases the sent message after the hold, and ignores an empty send', () => {
    const { result } = renderHook(() => useTranscriptAnnouncement(undefined, false))
    act(() => { result.current.announceSent('   ') })
    expect(result.current.announcement).toBe('')
    act(() => { result.current.announceSent('hello') })
    expect(result.current.announcement).toBe('You: hello')
    act(() => { vi.advanceTimersByTime(ANNOUNCEMENT_HOLD_MS) })
    expect(result.current.announcement).toBe('')
  })
})
