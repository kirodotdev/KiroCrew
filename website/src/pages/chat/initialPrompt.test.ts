import { describe, expect, it, vi } from 'vitest'

import type { ChatMessage } from '../../types'
import { INITIAL_PROMPT_MAX_PAGES, INITIAL_PROMPT_PAGE, firstPromptText, loadInitialPrompt, promptRowText } from './initialPrompt'

const row = (role: string, content: string, meta?: Record<string, unknown>): ChatMessage => ({ role, content, cls: role, ...(meta ? { meta } : {}) })

describe('promptRowText', () => {
  it('returns a user row as sent', () => {
    expect(promptRowText(row('user', 'Fix the draft\n\n- keep it small'))).toBe('Fix the draft\n\n- keep it small')
  })

  it('skips rows that are not prompts the person typed', () => {
    expect(promptRowText(row('assistant', 'hello'))).toBe('')
    expect(promptRowText(row('system', 'Session started'))).toBe('')
    expect(promptRowText(row('user', '   '))).toBe('')
    expect(promptRowText(row('user', 'taken back', { steerState: 'retracted' }))).toBe('')
  })
})

describe('firstPromptText', () => {
  it('picks the first prompt, not a later one', () => {
    const rows = [row('system', 'Session started'), row('user', 'first'), row('assistant', 'ok'), row('user', 'second')]
    expect(firstPromptText(rows)).toBe('first')
  })

  it('is empty when nothing was sent', () => {
    expect(firstPromptText([row('system', 'Session started')])).toBe('')
  })
})

describe('loadInitialPrompt', () => {
  it('reads the oldest page first', async () => {
    const fetchPage = vi.fn(async (_slot: string, _limit: number, _before: number) => ({ messages: [row('user', 'first')], total: 120 }))
    await expect(loadInitialPrompt('chat-1', fetchPage)).resolves.toBe('first')
    expect(fetchPage).toHaveBeenCalledTimes(1)
    expect(fetchPage).toHaveBeenCalledWith('chat-1', INITIAL_PROMPT_PAGE, INITIAL_PROMPT_PAGE)
  })

  it('stops at the end of a short transcript with no prompt', async () => {
    const fetchPage = vi.fn(async (_slot: string, _limit: number, _before: number) => ({ messages: [row('system', 'Session started')], total: 1 }))
    await expect(loadInitialPrompt('chat-1', fetchPage)).resolves.toBe('')
    expect(fetchPage).toHaveBeenCalledTimes(1)
  })

  it('pages forward through machine rows, at most the page cap', async () => {
    const fetchPage = vi.fn(async (_slot: string, _limit: number, _before: number) => ({ messages: [row('system', 'tick')], total: 10_000 }))
    await expect(loadInitialPrompt('chat-1', fetchPage)).resolves.toBe('')
    expect(fetchPage).toHaveBeenCalledTimes(INITIAL_PROMPT_MAX_PAGES)
    expect(fetchPage).toHaveBeenLastCalledWith('chat-1', INITIAL_PROMPT_PAGE, INITIAL_PROMPT_PAGE * INITIAL_PROMPT_MAX_PAGES)
  })

  /* The server cuts a page's oldest rows when the page passes its byte budget, and
   * reports it with `next_before`. The prompt lives in exactly those rows. */
  it('reads the prefix the server cut from a byte-trimmed page before judging it', async () => {
    const corpus = [row('user', 'the first prompt'), row('assistant', 'big 1'), row('assistant', 'big 2'), row('assistant', 'big 3'), row('user', 'a later prompt')]
    // A server whose byte budget leaves room for the newest two rows of any window.
    const fetchPage = vi.fn(async (_slot: string, limit: number, before: number) => {
      const end = Math.min(before, corpus.length)
      const start = Math.max(0, end - limit)
      const kept = Math.max(start, end - 2)
      return { messages: corpus.slice(kept, end), total: corpus.length, next_before: kept }
    })
    await expect(loadInitialPrompt('chat-1', fetchPage)).resolves.toBe('the first prompt')
    expect(fetchPage.mock.calls.map(c => [c[1], c[2]])).toEqual([[50, 50], [3, 3], [1, 1]])
  })

  it('answers with the retained row when the cut prefix holds no prompt', async () => {
    const corpus = [row('system', 'Session started'), row('assistant', 'big 1'), row('assistant', 'big 2'), row('user', 'a later prompt')]
    const fetchPage = async (_slot: string, limit: number, before: number) => {
      const end = Math.min(before, corpus.length)
      const start = Math.max(0, end - limit)
      const kept = Math.max(start, end - 1)
      return { messages: corpus.slice(kept, end), total: corpus.length, next_before: kept }
    }
    // The cut prefix holds no prompt, so the newest row IS the first prompt: the walk read the prefix and found nothing older.
    await expect(loadInitialPrompt('chat-1', fetchPage)).resolves.toBe('a later prompt')
  })

  it('stops when a response does not move the cursor older', async () => {
    const fetchPage = vi.fn(async (_slot: string, _limit: number, before: number) => ({ messages: [row('system', 'tick')], total: 10_000, next_before: before }))
    await expect(loadInitialPrompt('chat-1', fetchPage)).resolves.toBe('')
    expect(fetchPage).toHaveBeenCalledTimes(INITIAL_PROMPT_MAX_PAGES)
  })
})
