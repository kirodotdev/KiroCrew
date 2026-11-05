/**
 * "Draft a new session from this prompt" (#25977) must leave the user where
 * they are when they open another session while it runs. The project path
 * creates with `activate: false` and then picks the agent, so the create
 * reducer's own `forkMayOpen` guard never runs there: `startFromInitialPrompt`
 * makes that check itself before it switches. Both paths are pinned, with the
 * real `createSlot` and `switchSlot` thunks against a mocked API, and the
 * un-moved case is pinned too: with the user still on the origin, the new
 * session opens.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import type { ChatSlot } from '../../types'

const mocks = vi.hoisted(() => ({
  createChatSlot: vi.fn(),
  chatSlotAgent: vi.fn(),
  chatSlotProject: vi.fn(),
  deleteChatSlot: vi.fn(),
  chatSlotDetail: vi.fn(),
}))
vi.mock('../../api/client', async () => {
  const { ApiError } = await import('../../api/apiError')
  return {
    SEARCH_MIN_CHARS: 2,
    ApiError,
    api: new Proxy(mocks as Record<string, unknown>, {
      get: (t, p: string) => (p in t ? t[p] : vi.fn().mockResolvedValue([])),
    }),
  }
})

import { store } from '../../store'
import { sseSlots } from '../../store/dashboardSlice'
import { setActiveSlot, stageToMainComposer } from '../../store/chatSlice'
import { PREFILL_STORAGE_KEY } from '../../utils/navIntent'
import { startFromInitialPrompt } from './startFromInitialPrompt'

const ORIGIN = 'chat-prompt-origin'
const OTHER = 'chat-prompt-other'
const NEW_KEY = 'chat-prompt-new'
const PROMPT = 'Summarise the open review comments'

function deferred() {
  let resolve!: (v: unknown) => void
  const promise = new Promise(r => { resolve = r })
  return { promise, resolve }
}

const activeSlot = () => store.getState().chat.activeSlot
const listed = (key: string) => store.getState().dashboard.slots.some(s => s.key === key)
const created = { key: NEW_KEY, title: NEW_KEY, messages: 0, running: false }
const start = (source: Parameters<typeof startFromInitialPrompt>[2]['source']) =>
  startFromInitialPrompt(store.dispatch, store.getState, { prompt: PROMPT, source })

beforeEach(() => {
  const slot = (key: string): ChatSlot => ({ key, title: key, messages: 2, running: false, folder_id: '' })
  store.dispatch(sseSlots([slot(ORIGIN), slot(OTHER)]))
  store.dispatch(setActiveSlot(ORIGIN))
  store.dispatch(stageToMainComposer(null))
  // A fresh object per create: the store freezes what it was handed.
  mocks.createChatSlot.mockImplementation(() => Promise.resolve({ ...created }))
  mocks.chatSlotProject.mockResolvedValue({ ok: true })
  mocks.chatSlotAgent.mockResolvedValue({ ok: true })
  mocks.deleteChatSlot.mockResolvedValue({ ok: true })
  mocks.chatSlotDetail.mockResolvedValue({ messages: [], running: false, has_more: false, total: 0, queue: [] })
})
afterEach(() => {
  vi.clearAllMocks()
  store.dispatch(setActiveSlot(null))
  store.dispatch(stageToMainComposer(null))
  store.dispatch(sseSlots([]))
})

describe('startFromInitialPrompt with a project to carry', () => {
  const source = { agent: 'reviewer', agent_kind: undefined, project: '/work/repo', folder_id: '' }

  it('leaves the user on the session they opened while the agent was picked', async () => {
    const agentPick = deferred()
    mocks.chatSlotAgent.mockReturnValue(agentPick.promise)
    const run = start(source)
    await vi.waitFor(() => expect(mocks.chatSlotAgent).toHaveBeenCalledWith(NEW_KEY, 'reviewer', undefined))
    store.dispatch(setActiveSlot(OTHER))
    agentPick.resolve({ ok: true })

    await expect(run).resolves.toBe(false)
    expect(activeSlot()).toBe(OTHER)
    expect(listed(NEW_KEY)).toBe(true)
    expect(mocks.chatSlotDetail).not.toHaveBeenCalledWith(NEW_KEY, expect.anything())
    expect(store.getState().chat.mainComposerAppend).toBeNull()
  })

  it('leaves the user on the origin when they went away and came back meanwhile', async () => {
    const agentPick = deferred()
    mocks.chatSlotAgent.mockReturnValue(agentPick.promise)
    const run = start(source)
    await vi.waitFor(() => expect(mocks.chatSlotAgent).toHaveBeenCalled())
    store.dispatch(setActiveSlot(OTHER))
    store.dispatch(setActiveSlot(ORIGIN))
    agentPick.resolve({ ok: true })

    await expect(run).resolves.toBe(false)
    expect(activeSlot()).toBe(ORIGIN)
    expect(listed(NEW_KEY)).toBe(true)
  })

  it('opens the new session when the user stayed on the origin', async () => {
    await expect(start(source)).resolves.toBe(true)
    expect(activeSlot()).toBe(NEW_KEY)
    expect(mocks.chatSlotProject).toHaveBeenLastCalledWith(NEW_KEY, '/work/repo')
  })

  // The request is numbered by its create's `pending`. Opening with that
  // number, not the one before it, orders it after a fork clicked earlier, so
  // that fork finishing later does not take the view back (`forkMayOpen`).
  it('opens with the request number its create was given', async () => {
    const before = store.getState().chat.requestSeq ?? 0
    await expect(start(source)).resolves.toBe(true)
    expect(store.getState().chat.activatedRequestSeq).toBe(before + 1)
  })

  // A staged prefill lives 30s, and the agent and project round-trips can
  // outlast it: the prompt is staged after them, not on the create.
  it('stages the prompt only after the session is set up', async () => {
    sessionStorage.removeItem(PREFILL_STORAGE_KEY)
    const agentPick = deferred()
    mocks.chatSlotAgent.mockReturnValue(agentPick.promise)
    const run = start(source)
    await vi.waitFor(() => expect(mocks.chatSlotAgent).toHaveBeenCalled())
    expect(sessionStorage.getItem(PREFILL_STORAGE_KEY)).toBeNull()
    agentPick.resolve({ ok: true })

    await expect(run).resolves.toBe(true)
    const staged = JSON.parse(sessionStorage.getItem(PREFILL_STORAGE_KEY) || 'null')
    expect(staged).toMatchObject({ slotKey: NEW_KEY, prompt: PROMPT })
  })
})

describe('startFromInitialPrompt with no project', () => {
  it('leaves the user on the session they opened while the create ran', async () => {
    const create = deferred()
    mocks.createChatSlot.mockReturnValue(create.promise)
    const run = start({ agent: 'reviewer', agent_kind: undefined, project: undefined, folder_id: '' })
    await vi.waitFor(() => expect(mocks.createChatSlot).toHaveBeenCalled())
    store.dispatch(setActiveSlot(OTHER))
    create.resolve({ ...created })

    await expect(run).resolves.toBe(false)
    expect(activeSlot()).toBe(OTHER)
    expect(store.getState().chat.mainComposerAppend).toBeNull()
  })
})
