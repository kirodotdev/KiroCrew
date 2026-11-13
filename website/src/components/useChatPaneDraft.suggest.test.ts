import { describe, it, expect, beforeEach } from 'vitest'
import { act, renderHook } from '@testing-library/react'
import { useChatPaneDraft } from './useChatPaneDraft'
import { suggestPaneDraft, appendPaneDraftReply, __resetPaneDraftsForTests } from '../utils/chatPaneDrafts'

/* A crewmate's suggestions page fills the LIVE composer. A second prompt
 * must replace the first while it is untouched, and must never wipe text the
 * user edited (UX review on #19866). */
describe('useChatPaneDraft + suggestPaneDraft', () => {
  beforeEach(() => {
    sessionStorage.clear()
    __resetPaneDraftsForTests()
  })

  it('a second suggested prompt replaces the first in the live composer', () => {
    const { result } = renderHook(() => useChatPaneDraft('member-a'))
    act(() => { suggestPaneDraft('member-a', 'first prompt') })
    expect(result.current.input).toBe('first prompt')
    act(() => { suggestPaneDraft('member-a', 'second prompt') })
    expect(result.current.input).toBe('second prompt')
  })

  it('keeps an edited prompt and appends the next one', () => {
    const { result } = renderHook(() => useChatPaneDraft('member-a'))
    act(() => { suggestPaneDraft('member-a', 'first prompt') })
    act(() => { result.current.setInput('first prompt, edited') })
    act(() => { suggestPaneDraft('member-a', 'second prompt') })
    expect(result.current.input).toContain('first prompt, edited')
    expect(result.current.input).toContain('second prompt')
  })
})

/* A dashboard page's replies to two cards both reach the LIVE composer. */
describe('useChatPaneDraft + appendPaneDraftReply', () => {
  beforeEach(() => {
    sessionStorage.clear()
    __resetPaneDraftsForTests()
  })

  it('adds a second card\'s reply after the first, and not a reply already there', () => {
    const { result } = renderHook(() => useChatPaneDraft('member-a'))
    act(() => { appendPaneDraftReply('member-a', '"Card A": Ship it') })
    act(() => { appendPaneDraftReply('member-a', '"Card B": Wait') })
    expect(result.current.input).toBe('"Card A": Ship it\n\n"Card B": Wait')
    act(() => { appendPaneDraftReply('member-a', '"Card A": Ship it') })
    expect(result.current.input).toBe('"Card A": Ship it\n\n"Card B": Wait')
  })

  it('keeps text the user typed and adds the reply after it', () => {
    const { result } = renderHook(() => useChatPaneDraft('member-a'))
    act(() => { result.current.setInput('my own words') })
    act(() => { appendPaneDraftReply('member-a', '"Card A": Ship it') })
    expect(result.current.input).toBe('my own words\n\n"Card A": Ship it')
  })
})
