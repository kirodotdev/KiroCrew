/**
 * `/fork <prompt>` off its plain path: a session the user opened while the
 * fork ran keeps the view and the prompt waits unsent in the fork's stored
 * draft, a second Enter makes no second fork, and a refused fork opens nothing. The plain
 * path (fork, open, send) runs through ChatPage in
 * ChatPageAutoSendSidRewrite.test.tsx.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { createTestStore } from './helpers'
import type { ChatSlot } from '../types'

const mocks = vi.hoisted(() => ({ forkChatSlot: vi.fn() }))
vi.mock('../api/client', async () => {
  const { ApiError } = await import('../api/apiError')
  return {
    SEARCH_MIN_CHARS: 2,
    ApiError,
    api: new Proxy(mocks as Record<string, unknown>, {
      get: (t, p: string) => (p in t ? t[p] : vi.fn().mockResolvedValue({})),
    }),
  }
})

import { interceptSlashCommand, isInterceptedSlashCommand } from '../pages/chat/ChatInput'
import { ApiError } from '../api/apiError'
import { sseSlots } from '../store/dashboardSlice'
import { setActiveSlot, switchSlot } from '../store/chatSlice'
import { i18nT } from '../i18n/t'
import { loadDrafts } from '../utils/chatDrafts'

const ORIGIN = 'chat-fork-origin'
const OTHER = 'chat-fork-other'
const FORK = 'chat-fork-child'

let store: ReturnType<typeof createTestStore>

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  store = createTestStore()
  const slot = (key: string): ChatSlot => ({ key, title: key, messages: 2, running: false, folder_id: '' })
  store.dispatch(sseSlots([slot(ORIGIN), slot(OTHER)]))
  store.dispatch(setActiveSlot(ORIGIN))
})

describe('/fork slash command', () => {
  it('refuses on a host that hides Fork, without forking or moving the active session', async () => {
    const sendToFork = vi.fn().mockReturnValue(true)
    const res = await interceptSlashCommand('/fork hello', ORIGIN, store.dispatch, { sendToFork, forkUnavailable: true })
    expect(res).toEqual({
      intercepted: true,
      failed: true,
      stage: 'fork',
      error: i18nT('pages.chatPage.command_not_available_here', { command: '/fork' }),
    })
    expect(mocks.forkChatSlot).not.toHaveBeenCalled()
    expect(sendToFork).not.toHaveBeenCalled()
    expect(store.getState().chat.activeSlot).toBe(ORIGIN)
  })

  it('leaves a session opened meanwhile on screen and keeps the prompt unsent for the fork', async () => {
    let land!: () => void
    mocks.forkChatSlot.mockReturnValue(new Promise(r => { land = () => r({ ok: true, key: FORK, title: FORK, messages: 2 }) }))
    const sendToFork = vi.fn().mockReturnValue(true)
    const running = interceptSlashCommand('/fork hello', ORIGIN, store.dispatch, { sendToFork })
    await store.dispatch(switchSlot(OTHER))
    land()
    expect(await running).toEqual({ intercepted: true })
    expect(store.getState().chat.activeSlot).toBe(OTHER)
    expect(loadDrafts()[FORK]).toBe('hello')
    expect(sendToFork).not.toHaveBeenCalled()
  })

  it('keeps each prompt when two forks from two sessions both land off screen', async () => {
    const lands: Array<() => void> = []
    mocks.forkChatSlot
      .mockReturnValueOnce(new Promise(r => { lands.push(() => r({ ok: true, key: FORK, title: FORK, messages: 2 })) }))
      .mockReturnValueOnce(new Promise(r => { lands.push(() => r({ ok: true, key: 'chat-fork-child-2', title: 'chat-fork-child-2', messages: 2 })) }))
    const first = interceptSlashCommand('/fork alpha', ORIGIN, store.dispatch, {})
    await store.dispatch(switchSlot(OTHER))
    const second = interceptSlashCommand('/fork beta', OTHER, store.dispatch, {})
    await store.dispatch(switchSlot(ORIGIN))
    lands.forEach(land => land())
    await Promise.all([first, second])
    expect(loadDrafts()).toMatchObject({ [FORK]: 'alpha', 'chat-fork-child-2': 'beta' })
  })

  it('a second /fork while the first is still being made forks nothing more', async () => {
    let land!: () => void
    mocks.forkChatSlot.mockReturnValue(new Promise(r => { land = () => r({ ok: true, key: FORK, title: FORK, messages: 2 }) }))
    const sendToFork = vi.fn().mockReturnValue(true)
    const first = interceptSlashCommand('/fork hello', ORIGIN, store.dispatch, { sendToFork })
    expect(await interceptSlashCommand('/fork hello', ORIGIN, store.dispatch, { sendToFork })).toEqual({ intercepted: true, failed: true, unconfirmed: true })
    land()
    expect(await first).toEqual({ intercepted: true })
    expect(mocks.forkChatSlot).toHaveBeenCalledTimes(1)
    expect(sendToFork).toHaveBeenCalledExactlyOnceWith(FORK, 'hello')
  })

  it('reports a refused fork and opens nothing', async () => {
    mocks.forkChatSlot.mockRejectedValue(new ApiError(409, 'slot cap reached (20)', '{"error":"slot cap reached (20)","code":"slot_cap_reached"}'))
    const sendToFork = vi.fn()
    const result = await interceptSlashCommand('/fork hello', ORIGIN, store.dispatch, { sendToFork })
    expect(result).toEqual({
      intercepted: true, failed: true, stage: 'fork',
      error: i18nT('pages.chatPage.fork_failed_error', { error: 'slot cap reached (20)' }),
    })
    expect(store.getState().chat.activeSlot).toBe(ORIGIN)
    expect(sendToFork).not.toHaveBeenCalled()
  })

  it('words an over-capacity refusal without a fork direction', async () => {
    store.dispatch(sseSlots([{ key: ORIGIN, title: 'Quarterly report', messages: 2, running: false, folder_id: '' }]))
    mocks.forkChatSlot.mockRejectedValue(new ApiError(413, 'too large', '{"error":"too large","code":"fork_corpus_too_large"}'))
    const result = await interceptSlashCommand('/fork hello', ORIGIN, store.dispatch, {})
    expect(result).toMatchObject({
      failed: true, stage: 'fork',
      error: i18nT('pages.chatPage.fork_too_large_offsite_unknown', { title: 'Quarterly report', control: i18nT('pages.chat.assistantMessage.fork_conversation_from_here') }),
    })
  })

  // `/fork` is a whole word: a message that only starts with those letters is
  // ordinary text for the agent, not a fork.
  it.each(['/forkify', '/fork-x', '/forkify the plan', '/forks'])('sends %j to the agent instead of forking', async raw => {
    expect(isInterceptedSlashCommand(raw)).toBe(false)
    const sendToFork = vi.fn().mockReturnValue(true)
    expect(await interceptSlashCommand(raw, ORIGIN, store.dispatch, { sendToFork })).toEqual({ intercepted: false })
    expect(mocks.forkChatSlot).not.toHaveBeenCalled()
    expect(sendToFork).not.toHaveBeenCalled()
  })

  it.each(['/fork', '/fork hello', '/fork\nhello'])('still treats %j as a fork', raw => {
    expect(isInterceptedSlashCommand(raw)).toBe(true)
  })
})
