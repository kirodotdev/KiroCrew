/**
 * A create that resolves after the user opened another session must leave the
 * user where they are, and going to another session and back to the origin
 * counts as a choice too, so an A/B/A switch during a slow create leaves the
 * user on A. Creates and forks follow the same rule (`forkMayOpen`).
 *
 * A move the chat page makes on its own after losing the origin is not a
 * choice, and the new chat still opens.
 *
 * The real `createSlot` thunk runs against a deferred `api.createChatSlot`.
 * The un-switched case is pinned too: with the user still on the origin, the
 * new chat opens.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import type { ChatSlot } from '../types'

const mocks = vi.hoisted(() => ({ createChatSlot: vi.fn() }))
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
import { createSlot, dropActiveSlot, setActiveSlot, switchSlot } from '../store/chatSlice'

const ORIGIN = 'chat-create-origin'
const OTHER = 'chat-create-other'
const NEW_KEY = 'chat-create-new'

function deferredCreate() {
  let resolve!: (v: unknown) => void
  mocks.createChatSlot.mockReturnValue(new Promise(r => { resolve = r }))
  return () => resolve({ key: NEW_KEY, title: NEW_KEY, messages: 0, running: false })
}

const activeSlot = () => store.getState().chat.activeSlot
const listed = (key: string) => store.getState().dashboard.slots.some(s => s.key === key)

beforeEach(() => {
  const slot = (key: string): ChatSlot => ({ key, title: key, messages: 2, running: false, folder_id: '' })
  store.dispatch(sseSlots([slot(ORIGIN), slot(OTHER)]))
  store.dispatch(setActiveSlot(ORIGIN))
})
afterEach(() => {
  vi.clearAllMocks()
  store.dispatch(setActiveSlot(null))
  store.dispatch(sseSlots([]))
})

describe('createSlot resolving after the user moved', () => {
  it('leaves the user on the session they opened while the create ran', async () => {
    const land = deferredCreate()
    const created = store.dispatch(createSlot({ memory_mode: 'persistent' }))
    await vi.waitFor(() => expect(mocks.createChatSlot).toHaveBeenCalled())
    store.dispatch(setActiveSlot(OTHER))
    land()
    await created

    expect(listed(NEW_KEY)).toBe(true)
    expect(activeSlot()).toBe(OTHER)
  })

  it('leaves the user on the origin when they went away and came back while the create ran', async () => {
    const land = deferredCreate()
    const created = store.dispatch(createSlot({ memory_mode: 'persistent' }))
    await vi.waitFor(() => expect(mocks.createChatSlot).toHaveBeenCalled())
    store.dispatch(setActiveSlot(OTHER))
    store.dispatch(setActiveSlot(ORIGIN))
    land()
    await created

    expect(listed(NEW_KEY)).toBe(true)
    expect(activeSlot()).toBe(ORIGIN)
    expect(store.getState().chat.lastCreatedActivation).toBeNull()
  })

  // The chat page lost the origin from its list (closed in another tab, or a
  // frame dropped it) and moved the view itself: the user made no choice, so
  // the new chat still opens, as a fork's child does.
  it('opens the new chat when the page itself dropped the origin and auto-selected another session', async () => {
    const land = deferredCreate()
    const created = store.dispatch(createSlot({ memory_mode: 'persistent' }))
    await vi.waitFor(() => expect(mocks.createChatSlot).toHaveBeenCalled())
    store.dispatch(dropActiveSlot())
    void store.dispatch(switchSlot({ key: OTHER, automatic: true }))
    expect(activeSlot()).toBe(OTHER)
    land()
    await created

    expect(activeSlot()).toBe(NEW_KEY)
    expect(store.getState().chat.lastCreatedActivation?.slot).toBe(NEW_KEY)
  })

  it('opens the new chat when the page cleared the selection and auto-selected the origin again', async () => {
    const land = deferredCreate()
    const created = store.dispatch(createSlot({ memory_mode: 'persistent' }))
    await vi.waitFor(() => expect(mocks.createChatSlot).toHaveBeenCalled())
    store.dispatch(dropActiveSlot())
    void store.dispatch(switchSlot({ key: ORIGIN, automatic: true }))
    land()
    await created

    expect(activeSlot()).toBe(NEW_KEY)
  })

  it('leaves the user where they went after the page moved the view itself', async () => {
    const land = deferredCreate()
    const created = store.dispatch(createSlot({ memory_mode: 'persistent' }))
    await vi.waitFor(() => expect(mocks.createChatSlot).toHaveBeenCalled())
    store.dispatch(dropActiveSlot())
    void store.dispatch(switchSlot({ key: OTHER, automatic: true }))
    store.dispatch(setActiveSlot(ORIGIN))
    land()
    await created

    expect(listed(NEW_KEY)).toBe(true)
    expect(activeSlot()).toBe(ORIGIN)
    expect(store.getState().chat.lastCreatedActivation).toBeNull()
  })

  it('opens the new chat when the user stayed on the origin', async () => {
    const land = deferredCreate()
    const created = store.dispatch(createSlot({ memory_mode: 'persistent' }))
    await vi.waitFor(() => expect(mocks.createChatSlot).toHaveBeenCalled())
    // Re-selecting the session already open is no move.
    store.dispatch(setActiveSlot(ORIGIN))
    land()
    await created

    expect(activeSlot()).toBe(NEW_KEY)
    expect(store.getState().chat.lastCreatedActivation?.slot).toBe(NEW_KEY)
  })
})
