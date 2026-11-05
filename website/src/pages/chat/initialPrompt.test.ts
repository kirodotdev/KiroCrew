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
})
