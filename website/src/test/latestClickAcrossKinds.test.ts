/**
 * The session clicked last opens, whichever kinds of request the clicks made.
 * A create, fork or resume that opened its session switched the view, but
 * that switch was a request the user made before this one, not a move away
 * from it, so a request clicked after it still opens when it answers. A
 * request clicked earlier never undoes one clicked later.
 *
 * The real `createSlot` and `resumeFromHistory` thunks run against deferred
 * api answers.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import type { ChatSlot } from '../types'

const mocks = vi.hoisted(() => ({ resumeChatSlot: vi.fn(), createChatSlot: vi.fn() }))
vi.mock('../api/client', async () => {
  const { ApiError } = await import('../api/apiError')
  return {
    SEARCH_MIN_CHARS: 2,
    ApiError,
    api: new Proxy(mocks as Record<string, unknown>, {
      get: (t, p: string) => (p in t ? t[p] : vi.fn().mockResolvedValue([])),
    }),
  }
})

import { store } from '../store'
import { sseSlots } from '../store/dashboardSlice'
import { captureForkOrigin, createSlot, forkMayOpen, forkOpenSettled, forkOpenStarted, listForkedSlot, resumeFromHistory, setActiveSlot, switchSlot } from '../store/chatSlice'

const ORIGIN = 'chat-kinds-origin'
const OTHER = 'chat-kinds-other'
const R1 = 'chat-kinds-r1'
const NEW = 'chat-kinds-new'
const CHILD = 'chat-kinds-child'

let landCreate: (v: unknown) => void = () => {}
let landResume: (v: unknown) => void = () => {}
const activeSlot = () => store.getState().chat.activeSlot
const created = { key: NEW, title: 'New Session', messages: 0, running: false }
const resumed = { ok: true, key: R1, mode: 'orchestrator', messages: [] }

beforeEach(() => {
  mocks.createChatSlot.mockImplementation(() => new Promise(r => { landCreate = r }))
  mocks.resumeChatSlot.mockImplementation(() => new Promise(r => { landResume = r }))
  const slot = (key: string): ChatSlot => ({ key, title: key, messages: 2, running: false, folder_id: '' })
  store.dispatch(sseSlots([slot(ORIGIN), slot(OTHER)]))
  store.dispatch(setActiveSlot(ORIGIN))
  store.dispatch({ type: 'chat/fetchHistory/fulfilled', payload: { sessions: [{ key: R1, title: R1 }], hasMore: false, offset: 0, append: false } })
})
afterEach(() => {
  vi.clearAllMocks()
  store.dispatch(setActiveSlot(null))
  store.dispatch(sseSlots([]))
})

// Each returns its request wrapped: an async function returning the thunk's
// promise itself would wait for the request to settle.
async function startCreate() {
  const c = store.dispatch(createSlot({ memory_mode: 'persistent' }))
  await vi.waitFor(() => expect(mocks.createChatSlot).toHaveBeenCalled())
  return { done: c }
}
async function startResume() {
  const r = store.dispatch(resumeFromHistory({ key: R1, title: R1 }))
  await vi.waitFor(() => expect(mocks.resumeChatSlot).toHaveBeenCalled())
  return { done: r }
}

describe('latest click across create and resume', () => {
  it('opens the resume clicked after New when the create answers first', async () => {
    const c = await startCreate()
    const r = await startResume()
    landCreate(created)
    await c.done
    expect(activeSlot()).toBe(NEW)
    landResume(resumed)
    await r.done
    expect(activeSlot()).toBe(R1)
  })

  it('keeps the resume clicked after New when the create answers last', async () => {
    const c = await startCreate()
    const r = await startResume()
    landResume(resumed)
    await r.done
    expect(activeSlot()).toBe(R1)
    landCreate(created)
    await c.done
    expect(activeSlot()).toBe(R1)
  })

  it('opens New clicked after a resume when the resume answers first', async () => {
    const r = await startResume()
    const c = await startCreate()
    landResume(resumed)
    await r.done
    expect(activeSlot()).toBe(R1)
    landCreate(created)
    await c.done
    expect(activeSlot()).toBe(NEW)
  })

  it('still leaves the user where they moved while both were pending', async () => {
    const c = await startCreate()
    const r = await startResume()
    store.dispatch(setActiveSlot(OTHER))
    landCreate(created)
    await c.done
    landResume(resumed)
    await r.done
    expect(activeSlot()).toBe(OTHER)
  })
})

describe('two creates in a row', () => {
  // One request made twice: the first to open keeps the view, so text typed
  // while both ran stays in the chat it was carried into.
  it('keeps the first create that opened', async () => {
    const lands: Array<(v: unknown) => void> = []
    mocks.createChatSlot.mockImplementation(() => new Promise(r => { lands.push(r) }))
    const c1 = store.dispatch(createSlot({ memory_mode: 'persistent' }))
    const c2 = store.dispatch(createSlot({ memory_mode: 'persistent' }))
    await vi.waitFor(() => expect(lands).toHaveLength(2))
    lands[0]({ key: 'chat-kinds-new-1', title: 'New', messages: 0, running: false })
    await c1
    lands[1]({ key: 'chat-kinds-new-2', title: 'New', messages: 0, running: false })
    await c2
    expect(activeSlot()).toBe('chat-kinds-new-1')
  })
})

describe('a create, a resume, then another create', () => {
  // The resume opened between the two creates, so the second create is the
  // later click over it and opens.
  it('opens the second create when the resume opened after the first', async () => {
    const lands: Array<(v: unknown) => void> = []
    mocks.createChatSlot.mockImplementation(() => new Promise(r => { lands.push(r) }))
    const c1 = store.dispatch(createSlot({ memory_mode: 'persistent' }))
    await vi.waitFor(() => expect(lands).toHaveLength(1))
    const r = await startResume()
    const c2 = store.dispatch(createSlot({ memory_mode: 'persistent' }))
    await vi.waitFor(() => expect(lands).toHaveLength(2))
    lands[0]({ key: 'chat-kinds-new-1', title: 'New', messages: 0, running: false })
    await c1
    landResume(resumed)
    await r.done
    expect(activeSlot()).toBe(R1)
    lands[1]({ key: 'chat-kinds-new-2', title: 'New', messages: 0, running: false })
    await c2
    expect(activeSlot()).toBe('chat-kinds-new-2')
  })
})

describe('three creates in a row', () => {
  // One burst of New presses: the first to open keeps the view, however many
  // creates came after it, so the text typed into it stays on screen.
  it('keeps the first create that opened', async () => {
    const lands: Array<(v: unknown) => void> = []
    mocks.createChatSlot.mockImplementation(() => new Promise(r => { lands.push(r) }))
    const cs = [1, 2, 3].map(() => store.dispatch(createSlot({ memory_mode: 'persistent' })))
    await vi.waitFor(() => expect(lands).toHaveLength(3))
    for (const [i, land] of lands.entries()) {
      land({ key: `chat-kinds-new-${i + 1}`, title: 'New', messages: 0, running: false })
      await cs[i]
    }
    expect(activeSlot()).toBe('chat-kinds-new-1')
  })
})

describe('a create, a resume, then another create, answering create, create, resume', () => {
  // The resume was clicked between the creates, so the second create is not
  // a repeat of the first: it is the last click and keeps the view.
  it('ends on the second create', async () => {
    const lands: Array<(v: unknown) => void> = []
    mocks.createChatSlot.mockImplementation(() => new Promise(r => { lands.push(r) }))
    const c1 = store.dispatch(createSlot({ memory_mode: 'persistent' }))
    await vi.waitFor(() => expect(lands).toHaveLength(1))
    const r = await startResume()
    const c2 = store.dispatch(createSlot({ memory_mode: 'persistent' }))
    await vi.waitFor(() => expect(lands).toHaveLength(2))
    lands[0]({ key: 'chat-kinds-new-1', title: 'New', messages: 0, running: false })
    await c1
    lands[1]({ key: 'chat-kinds-new-2', title: 'New', messages: 0, running: false })
    await c2
    expect(activeSlot()).toBe('chat-kinds-new-2')
    landResume(resumed)
    await r.done
    expect(activeSlot()).toBe('chat-kinds-new-2')
  })
})

describe('a create that opens through a foreground tab', () => {
  // Cmd/Ctrl+Shift-click New: the create runs with `activate: false`, and its
  // success opens the tab with `switchSlot`, naming the create's number.
  it('still lets a resume clicked after it open', async () => {
    const c = store.dispatch(createSlot({ memory_mode: 'persistent', activate: false }))
    await vi.waitFor(() => expect(mocks.createChatSlot).toHaveBeenCalled())
    const r = await startResume()
    landCreate(created)
    const action = await c
    if (!createSlot.fulfilled.match(action)) throw new Error('create did not fulfil')
    await store.dispatch(switchSlot({ key: NEW, announceOnMissing: true, requestSeq: action.meta.originRequestSeq }))
    expect(activeSlot()).toBe(NEW)
    landResume(resumed)
    await r.done
    expect(activeSlot()).toBe(R1)
  })
})

describe('latest click across fork and create', () => {
  // The sidebar Fork's own path: capture, list the child, then switch with
  // the origin's request number.
  async function landFork(origin: ReturnType<typeof captureForkOrigin>) {
    store.dispatch(listForkedSlot({ key: CHILD, title: CHILD }))
    if (forkMayOpen(store.getState(), origin)) await store.dispatch(switchSlot({ key: CHILD, requestSeq: origin.seq }))
    store.dispatch(forkOpenSettled())
  }

  it('opens New clicked after a sidebar Fork when the fork answers first', async () => {
    store.dispatch(forkOpenStarted())
    const fork = captureForkOrigin(store.getState())
    const c = await startCreate()
    await landFork(fork)
    expect(activeSlot()).toBe(CHILD)
    landCreate(created)
    await c.done
    expect(activeSlot()).toBe(NEW)
  })

  it('keeps New clicked after a sidebar Fork when the fork answers last', async () => {
    store.dispatch(forkOpenStarted())
    const fork = captureForkOrigin(store.getState())
    const c = await startCreate()
    landCreate(created)
    await c.done
    expect(activeSlot()).toBe(NEW)
    await landFork(fork)
    expect(activeSlot()).toBe(NEW)
  })
})
