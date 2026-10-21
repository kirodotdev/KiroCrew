// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, waitFor } from '@testing-library/react'
import { useLocation } from 'react-router-dom'
import { api } from '../api/client'
import { ApiError } from '../api/apiError'
import { createTestStore, renderHookWithProviders as renderHookSync } from '../test/helpers'
import {
  OPEN_SESSION_MESSAGE_TYPE,
  OPEN_SESSION_RESULT_TYPE,
  openSessionItemOf,
  useFrameOpenSession,
} from './useFrameOpenSession'

// The real `switchSlot` is a thunk that reads the gateway. What is under test here is that the
// hook dispatches it for the slot the gateway named, so it becomes a plain action.
vi.mock('../store/chatSlice', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../store/chatSlice')>()),
  switchSlot: (arg: unknown) => ({ type: 'test/switchSlot', payload: arg }),
}))

const ITEM = 'it_3f9a01c2'
const WORKER_SLOT = 'chat-4-worker'
const SESSION = { kind: 'session', slot: 'chat-1-root' } as const

/** A frame stand-in whose window is a unique object: the hook compares identity only. */
function mountFrame() {
  const win = { postMessage: vi.fn() } as unknown as Window
  return { contentWindow: win } as HTMLIFrameElement
}

function post(data: unknown, source: unknown) {
  const event = new MessageEvent('message', { data })
  Object.defineProperty(event, 'source', { value: source })
  window.dispatchEvent(event)
}

/** Render, then let the providers' own mount effects settle so no update lands after the test's last barrier. */
async function renderHookWithProviders<T>(hook: () => T, options?: Parameters<typeof renderHookSync<T>>[1]) {
  const rendered = renderHookSync(hook, options)
  await act(async () => {})
  return rendered
}

function refusal(status: number, code: string): ApiError {
  return new ApiError(status, 'refused', JSON.stringify({ ok: false, error: 'refused', code }))
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('openSessionItemOf', () => {
  it('takes a non-empty string id and leaves its shape to the gateway', async () => {
    expect(openSessionItemOf({ type: OPEN_SESSION_MESSAGE_TYPE, item_id: ITEM })).toBe(ITEM)
    for (const bad of ['', 5, null, undefined, {}]) {
      expect(openSessionItemOf({ type: OPEN_SESSION_MESSAGE_TYPE, item_id: bad })).toBeNull()
    }
    expect(openSessionItemOf({ type: 'kirocrew-dashboard:open', item_id: ITEM })).toBeNull()
    expect(openSessionItemOf(null)).toBeNull()
  })
})

describe('useFrameOpenSession', () => {
  it('asks the gateway for the card\'s worker and opens it, from its own frame after a gesture', async () => {
    const frame = mountFrame()
    const open = vi.fn()
    const ask = vi.spyOn(api, 'crewBoardOpen').mockResolvedValue({ ok: true, action: 'open', item_id: ITEM, slot: WORKER_SLOT })
    await renderHookWithProviders(() => useFrameOpenSession({ current: frame }, SESSION, { open, gesture: () => true }))
    post({ type: OPEN_SESSION_MESSAGE_TYPE, item_id: ITEM }, frame.contentWindow)
    await waitFor(() => expect(open).toHaveBeenCalledWith(WORKER_SLOT))
    expect(ask).toHaveBeenCalledTimes(1)
    expect(ask).toHaveBeenCalledWith(SESSION, ITEM)
    // The page is told it worked, with an item id and no code. The key stays in the host.
    expect(frame.contentWindow.postMessage).toHaveBeenCalledTimes(1)
    expect(frame.contentWindow.postMessage).toHaveBeenCalledWith(
      { type: OPEN_SESSION_RESULT_TYPE, item_id: ITEM, code: null },
      '*',
    )
    expect(JSON.stringify((frame.contentWindow.postMessage as ReturnType<typeof vi.fn>).mock.calls)).not.toContain(WORKER_SLOT)
  })

  it('names the member dashboard when the frame shows a crewmate\'s page', async () => {
    const frame = mountFrame()
    const member = { kind: 'member', slug: 'atlas', member: 'Atlas' } as const
    const ask = vi.spyOn(api, 'crewBoardOpen').mockResolvedValue({ ok: true, action: 'open', item_id: ITEM, slot: WORKER_SLOT })
    await renderHookWithProviders(() => useFrameOpenSession({ current: frame }, member, { open: vi.fn(), gesture: () => true }))
    post({ type: OPEN_SESSION_MESSAGE_TYPE, item_id: ITEM }, frame.contentWindow)
    await waitFor(() => expect(ask).toHaveBeenCalledWith(member, ITEM))
  })

  it('takes the dashboard from the host, never from the message', async () => {
    const frame = mountFrame()
    const ask = vi.spyOn(api, 'crewBoardOpen').mockResolvedValue({ ok: true, action: 'open', item_id: ITEM, slot: WORKER_SLOT })
    await renderHookWithProviders(() => useFrameOpenSession({ current: frame }, SESSION, { open: vi.fn(), gesture: () => true }))
    post(
      { type: OPEN_SESSION_MESSAGE_TYPE, item_id: ITEM, dashboard: { kind: 'session', slot: 'chat-other' }, slot: WORKER_SLOT, conductor: 'x' },
      frame.contentWindow,
    )
    await waitFor(() => expect(ask).toHaveBeenCalledTimes(1))
    expect(ask).toHaveBeenCalledWith(SESSION, ITEM)
  })

  it('navigates to /chat and switches to the slot the gateway named, the way a session link does', async () => {
    const frame = mountFrame()
    vi.spyOn(api, 'crewBoardOpen').mockResolvedValue({ ok: true, action: 'open', item_id: ITEM, slot: WORKER_SLOT })
    // The in-app session-link path raises the window first; jsdom has no window.focus.
    vi.spyOn(window, 'focus').mockImplementation(() => {})
    const store = createTestStore()
    const dispatch = vi.spyOn(store, 'dispatch')
    const { result } = await renderHookWithProviders(
      () => {
        useFrameOpenSession({ current: frame }, SESSION, { gesture: () => true })
        return useLocation()
      },
      { store, route: '/members' },
    )
    expect(result.current.pathname).toBe('/members')
    post({ type: OPEN_SESSION_MESSAGE_TYPE, item_id: ITEM }, frame.contentWindow)
    await waitFor(() => expect(result.current.pathname).toBe('/chat'))
    expect(dispatch).toHaveBeenCalledWith({
      type: 'test/switchSlot',
      payload: { key: WORKER_SLOT, announceOnMissing: true },
    })
  })

  it('ignores a valid message from a window that is not its frame', async () => {
    const frame = mountFrame()
    const ask = vi.spyOn(api, 'crewBoardOpen')
    await renderHookWithProviders(() => useFrameOpenSession({ current: frame }, SESSION, { open: vi.fn(), gesture: () => true }))
    post({ type: OPEN_SESSION_MESSAGE_TYPE, item_id: ITEM }, window)
    post({ type: OPEN_SESSION_MESSAGE_TYPE, item_id: ITEM }, mountFrame().contentWindow)
    expect(ask).not.toHaveBeenCalled()
  })

  it('ignores a message when the person made no gesture in the frame', async () => {
    const frame = mountFrame()
    const ask = vi.spyOn(api, 'crewBoardOpen')
    await renderHookWithProviders(() => useFrameOpenSession({ current: frame }, SESSION, { open: vi.fn(), gesture: () => false }))
    post({ type: OPEN_SESSION_MESSAGE_TYPE, item_id: ITEM }, frame.contentWindow)
    expect(ask).not.toHaveBeenCalled()
  })

  it('ignores a message whose id is not a non-empty string', async () => {
    const frame = mountFrame()
    const ask = vi.spyOn(api, 'crewBoardOpen')
    await renderHookWithProviders(() => useFrameOpenSession({ current: frame }, SESSION, { open: vi.fn(), gesture: () => true }))
    for (const item_id of ['', undefined, 7, null]) {
      post({ type: OPEN_SESSION_MESSAGE_TYPE, item_id }, frame.contentWindow)
    }
    expect(ask).not.toHaveBeenCalled()
  })

  it('hands a malformed id to the gateway and tells the page its refusal', async () => {
    const frame = mountFrame()
    const open = vi.fn()
    const ask = vi.spyOn(api, 'crewBoardOpen').mockRejectedValue(refusal(400, 'invalid_item_id'))
    await renderHookWithProviders(() => useFrameOpenSession({ current: frame }, SESSION, { open, gesture: () => true }))
    post({ type: OPEN_SESSION_MESSAGE_TYPE, item_id: '../it_3f9a01c2' }, frame.contentWindow)
    await waitFor(() =>
      expect(frame.contentWindow.postMessage).toHaveBeenCalledWith(
        { type: OPEN_SESSION_RESULT_TYPE, item_id: '../it_3f9a01c2', code: 'invalid_item_id' },
        '*',
      ),
    )
    expect(ask).toHaveBeenCalledWith(SESSION, '../it_3f9a01c2')
    expect(open).not.toHaveBeenCalled()
  })

  it('does nothing when the frame has no dashboard to name', async () => {
    const frame = mountFrame()
    const ask = vi.spyOn(api, 'crewBoardOpen')
    await renderHookWithProviders(() => useFrameOpenSession({ current: frame }, null, { open: vi.fn(), gesture: () => true }))
    post({ type: OPEN_SESSION_MESSAGE_TYPE, item_id: ITEM }, frame.contentWindow)
    expect(ask).not.toHaveBeenCalled()
  })

  it('tells the page the gateway\'s refusal code, opens nothing and never names a session', async () => {
    const frame = mountFrame()
    const open = vi.fn()
    vi.spyOn(api, 'crewBoardOpen').mockRejectedValue(refusal(409, 'worker_closed'))
    await renderHookWithProviders(() => useFrameOpenSession({ current: frame }, SESSION, { open, gesture: () => true }))
    post({ type: OPEN_SESSION_MESSAGE_TYPE, item_id: ITEM }, frame.contentWindow)
    await waitFor(() =>
      expect(frame.contentWindow.postMessage).toHaveBeenCalledWith(
        { type: OPEN_SESSION_RESULT_TYPE, item_id: ITEM, code: 'worker_closed' },
        '*',
      ),
    )
    expect(open).not.toHaveBeenCalled()
  })

  it('tells the page a generic failure when the request itself fails', async () => {
    const frame = mountFrame()
    vi.spyOn(api, 'crewBoardOpen').mockRejectedValue(new Error('offline'))
    await renderHookWithProviders(() => useFrameOpenSession({ current: frame }, SESSION, { open: vi.fn(), gesture: () => true }))
    post({ type: OPEN_SESSION_MESSAGE_TYPE, item_id: ITEM }, frame.contentWindow)
    await waitFor(() =>
      expect(frame.contentWindow.postMessage).toHaveBeenCalledWith(
        { type: OPEN_SESSION_RESULT_TYPE, item_id: ITEM, code: 'open_failed' },
        '*',
      ),
    )
  })

  it('treats an answer with no slot as a failure rather than navigating', async () => {
    const frame = mountFrame()
    const open = vi.fn()
    vi.spyOn(api, 'crewBoardOpen').mockResolvedValue({ ok: true, action: 'open', item_id: ITEM, slot: '' })
    await renderHookWithProviders(() => useFrameOpenSession({ current: frame }, SESSION, { open, gesture: () => true }))
    post({ type: OPEN_SESSION_MESSAGE_TYPE, item_id: ITEM }, frame.contentWindow)
    await waitFor(() =>
      expect(frame.contentWindow.postMessage).toHaveBeenCalledWith(
        { type: OPEN_SESSION_RESULT_TYPE, item_id: ITEM, code: 'open_failed' },
        '*',
      ),
    )
    expect(open).not.toHaveBeenCalled()
  })

  it('does not answer a frame that was replaced while the request was out', async () => {
    const first = mountFrame()
    const holder = { current: first as HTMLIFrameElement | null }
    let settle: (v: { ok: boolean; action: 'open'; item_id: string; slot: string }) => void = () => {}
    vi.spyOn(api, 'crewBoardOpen').mockImplementation(() => new Promise((resolve) => { settle = resolve }))
    await renderHookWithProviders(() => useFrameOpenSession(holder, SESSION, { open: vi.fn(), gesture: () => true }))
    post({ type: OPEN_SESSION_MESSAGE_TYPE, item_id: ITEM }, first.contentWindow)
    const second = mountFrame()
    holder.current = second
    await act(async () => {
      settle({ ok: true, action: 'open', item_id: ITEM, slot: WORKER_SLOT })
    })
    expect(first.contentWindow.postMessage).not.toHaveBeenCalled()
    expect(second.contentWindow.postMessage).not.toHaveBeenCalled()
  })

  it('sends one request for a double click, and accepts a new click once the first settles', async () => {
    const frame = mountFrame()
    const open = vi.fn()
    let settle: (v: { ok: boolean; action: 'open'; item_id: string; slot: string }) => void = () => {}
    const ask = vi.spyOn(api, 'crewBoardOpen').mockImplementation(
      () => new Promise((resolve) => { settle = resolve }),
    )
    await renderHookWithProviders(() => useFrameOpenSession({ current: frame }, SESSION, { open, gesture: () => true }))
    post({ type: OPEN_SESSION_MESSAGE_TYPE, item_id: ITEM }, frame.contentWindow)
    post({ type: OPEN_SESSION_MESSAGE_TYPE, item_id: ITEM }, frame.contentWindow)
    expect(ask).toHaveBeenCalledTimes(1)
    await act(async () => {
      settle({ ok: true, action: 'open', item_id: ITEM, slot: WORKER_SLOT })
    })
    await waitFor(() => expect(open).toHaveBeenCalledTimes(1))
    post({ type: OPEN_SESSION_MESSAGE_TYPE, item_id: ITEM }, frame.contentWindow)
    expect(ask).toHaveBeenCalledTimes(2)
  })

  it('stops listening when the host unmounts', async () => {
    const frame = mountFrame()
    const ask = vi.spyOn(api, 'crewBoardOpen')
    const { unmount } = await renderHookWithProviders(() => useFrameOpenSession({ current: frame }, SESSION, { open: vi.fn(), gesture: () => true }))
    unmount()
    post({ type: OPEN_SESSION_MESSAGE_TYPE, item_id: ITEM }, frame.contentWindow)
    expect(ask).not.toHaveBeenCalled()
  })
})
