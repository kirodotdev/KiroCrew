// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, screen, waitFor } from '@testing-library/react'
import { useLocation } from 'react-router-dom'
import { api } from '../api/client'
import { ApiError } from '../api/apiError'
import { NavigationLeaveGuardProvider, useRegisterNavigationLeaveGuard } from '../components/NavigationLeaveGuard'
import { createTestStore, renderHookWithProviders as renderHookSync, renderWithProviders } from '../test/helpers'
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
    await renderHookWithProviders(() => useFrameOpenSession({ current: frame }, SESSION, { open, gesture: () => true, tied: () => true }))
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
    await renderHookWithProviders(() => useFrameOpenSession({ current: frame }, member, { open: vi.fn(), gesture: () => true, tied: () => true }))
    post({ type: OPEN_SESSION_MESSAGE_TYPE, item_id: ITEM }, frame.contentWindow)
    await waitFor(() => expect(ask).toHaveBeenCalledWith(member, ITEM))
  })

  it('takes the dashboard from the host, never from the message', async () => {
    const frame = mountFrame()
    const ask = vi.spyOn(api, 'crewBoardOpen').mockResolvedValue({ ok: true, action: 'open', item_id: ITEM, slot: WORKER_SLOT })
    await renderHookWithProviders(() => useFrameOpenSession({ current: frame }, SESSION, { open: vi.fn(), gesture: () => true, tied: () => true }))
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
        useFrameOpenSession({ current: frame }, SESSION, { gesture: () => true, tied: () => true })
        return useLocation()
      },
      { store, route: '/members' },
    )
    expect(result.current.pathname).toBe('/members')
    post({ type: OPEN_SESSION_MESSAGE_TYPE, item_id: ITEM }, frame.contentWindow)
    await waitFor(() => expect(result.current.pathname).toBe('/chat'))
    expect(dispatch).toHaveBeenCalledWith({
      type: 'test/switchSlot',
      payload: { key: WORKER_SLOT, announceOnMissing: true, gesture: true },
    })
  })

  it('ignores a valid message from a window that is not its frame', async () => {
    const frame = mountFrame()
    const ask = vi.spyOn(api, 'crewBoardOpen')
    await renderHookWithProviders(() => useFrameOpenSession({ current: frame }, SESSION, { open: vi.fn(), gesture: () => true, tied: () => true }))
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

  it('answers open_untied and opens nothing when the live click cannot be tied to the asking frame', async () => {
    // A page that focuses itself after the owner typed in the host passes the gesture test alone.
    const frame = mountFrame()
    const posted = vi.spyOn(frame.contentWindow as Window, 'postMessage')
    const ask = vi.spyOn(api, 'crewBoardOpen')
    const open = vi.fn()
    await renderHookWithProviders(() => useFrameOpenSession({ current: frame }, SESSION, { open, gesture: () => true, tied: () => false }))
    post({ type: OPEN_SESSION_MESSAGE_TYPE, item_id: ITEM }, frame.contentWindow)
    expect(ask).not.toHaveBeenCalled()
    expect(open).not.toHaveBeenCalled()
    expect(posted).toHaveBeenCalledWith({ type: OPEN_SESSION_RESULT_TYPE, item_id: ITEM, code: 'open_untied' }, '*')
  })

  it('does not answer a message that had no gesture, tied or not', async () => {
    const frame = mountFrame()
    const posted = vi.spyOn(frame.contentWindow as Window, 'postMessage')
    await renderHookWithProviders(() => useFrameOpenSession({ current: frame }, SESSION, { open: vi.fn(), gesture: () => false, tied: () => false }))
    post({ type: OPEN_SESSION_MESSAGE_TYPE, item_id: ITEM }, frame.contentWindow)
    expect(posted).not.toHaveBeenCalled()
  })

  it('ignores a message whose id is not a non-empty string', async () => {
    const frame = mountFrame()
    const ask = vi.spyOn(api, 'crewBoardOpen')
    await renderHookWithProviders(() => useFrameOpenSession({ current: frame }, SESSION, { open: vi.fn(), gesture: () => true, tied: () => true }))
    for (const item_id of ['', undefined, 7, null]) {
      post({ type: OPEN_SESSION_MESSAGE_TYPE, item_id }, frame.contentWindow)
    }
    expect(ask).not.toHaveBeenCalled()
  })

  it('hands a malformed id to the gateway and tells the page its refusal', async () => {
    const frame = mountFrame()
    const open = vi.fn()
    const ask = vi.spyOn(api, 'crewBoardOpen').mockRejectedValue(refusal(400, 'invalid_item_id'))
    await renderHookWithProviders(() => useFrameOpenSession({ current: frame }, SESSION, { open, gesture: () => true, tied: () => true }))
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

  it.each(['/popout/chat/a-session', '/embed/chat'])(
    'asks nothing and tells the page it cannot open when the window is %s',
    async (route) => {
      const frame = mountFrame()
      const open = vi.fn()
      const ask = vi.spyOn(api, 'crewBoardOpen').mockResolvedValue({ ok: true, action: 'open', item_id: ITEM, slot: WORKER_SLOT })
      await renderHookWithProviders(
        () => useFrameOpenSession({ current: frame }, SESSION, { open, gesture: () => true, tied: () => true }),
        { route },
      )
      post({ type: OPEN_SESSION_MESSAGE_TYPE, item_id: ITEM }, frame.contentWindow)
      await waitFor(() =>
        expect(frame.contentWindow.postMessage).toHaveBeenCalledWith(
          { type: OPEN_SESSION_RESULT_TYPE, item_id: ITEM, code: 'open_unavailable' },
          '*',
        ),
      )
      // The router of a popout or embed window re-pins any other route, so a /chat navigation
      // would land back on the session already shown while the page believed the open worked.
      expect(ask).not.toHaveBeenCalled()
      expect(open).not.toHaveBeenCalled()
    },
  )

  it('does nothing when the frame has no dashboard to name', async () => {
    const frame = mountFrame()
    const ask = vi.spyOn(api, 'crewBoardOpen')
    await renderHookWithProviders(() => useFrameOpenSession({ current: frame }, null, { open: vi.fn(), gesture: () => true, tied: () => true }))
    post({ type: OPEN_SESSION_MESSAGE_TYPE, item_id: ITEM }, frame.contentWindow)
    expect(ask).not.toHaveBeenCalled()
  })

  it('tells the page the gateway\'s refusal code, opens nothing and never names a session', async () => {
    const frame = mountFrame()
    const open = vi.fn()
    vi.spyOn(api, 'crewBoardOpen').mockRejectedValue(refusal(409, 'worker_closed'))
    await renderHookWithProviders(() => useFrameOpenSession({ current: frame }, SESSION, { open, gesture: () => true, tied: () => true }))
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
    await renderHookWithProviders(() => useFrameOpenSession({ current: frame }, SESSION, { open: vi.fn(), gesture: () => true, tied: () => true }))
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
    await renderHookWithProviders(() => useFrameOpenSession({ current: frame }, SESSION, { open, gesture: () => true, tied: () => true }))
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
    await renderHookWithProviders(() => useFrameOpenSession(holder, SESSION, { open: vi.fn(), gesture: () => true, tied: () => true }))
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
    await renderHookWithProviders(() => useFrameOpenSession({ current: frame }, SESSION, { open, gesture: () => true, tied: () => true }))
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

  it('tells another card to wait while a lookup is out, and still sends one request', async () => {
    const frame = mountFrame()
    const OTHER = 'it_3f9a01c3'
    const open = vi.fn()
    let settle: (v: { ok: boolean; action: 'open'; item_id: string; slot: string }) => void = () => {}
    const ask = vi.spyOn(api, 'crewBoardOpen').mockImplementation(
      () => new Promise((resolve) => { settle = resolve }),
    )
    await renderHookWithProviders(() => useFrameOpenSession({ current: frame }, SESSION, { open, gesture: () => true, tied: () => true }))
    post({ type: OPEN_SESSION_MESSAGE_TYPE, item_id: ITEM }, frame.contentWindow)
    post({ type: OPEN_SESSION_MESSAGE_TYPE, item_id: ITEM }, frame.contentWindow)
    post({ type: OPEN_SESSION_MESSAGE_TYPE, item_id: OTHER }, frame.contentWindow)
    expect(ask).toHaveBeenCalledTimes(1)
    // The same card again is dropped without a word; the other card is told to wait.
    expect(frame.contentWindow.postMessage).toHaveBeenCalledTimes(1)
    expect(frame.contentWindow.postMessage).toHaveBeenCalledWith(
      { type: OPEN_SESSION_RESULT_TYPE, item_id: OTHER, code: 'open_busy' },
      '*',
    )
    await act(async () => {
      settle({ ok: true, action: 'open', item_id: ITEM, slot: WORKER_SLOT })
    })
    await waitFor(() => expect(open).toHaveBeenCalledTimes(1))
  })

  describe('through the app shell\'s leave guard, like every other exit', () => {
    /** The real hook with its default navigation, under a provider and one registered guard. */
    function Probe({ frame, verdict }: { frame: HTMLIFrameElement; verdict: () => boolean }) {
      useRegisterNavigationLeaveGuard(verdict)
      useFrameOpenSession({ current: frame }, SESSION, { gesture: () => true, tied: () => true })
      return <span data-testid="path">{useLocation().pathname}</span>
    }
    function mountProbe(frame: HTMLIFrameElement, verdict: () => boolean) {
      const store = createTestStore()
      const dispatch = vi.spyOn(store, 'dispatch')
      renderWithProviders(
        <NavigationLeaveGuardProvider><Probe frame={frame} verdict={verdict} /></NavigationLeaveGuardProvider>,
        { store, route: '/members' },
      )
      return dispatch
    }

    it('does not switch when the guard vetoes, e.g. an unsent Questions answer the person keeps', async () => {
      const frame = mountFrame()
      vi.spyOn(window, 'focus').mockImplementation(() => {})
      const ask = vi.spyOn(api, 'crewBoardOpen').mockResolvedValue({ ok: true, action: 'open', item_id: ITEM, slot: WORKER_SLOT })
      const verdict = vi.fn(() => false)
      const dispatch = mountProbe(frame, verdict)
      post({ type: OPEN_SESSION_MESSAGE_TYPE, item_id: ITEM }, frame.contentWindow)
      await waitFor(() => expect(verdict).toHaveBeenCalledTimes(1))
      expect(ask).toHaveBeenCalledTimes(1)
      // The page is told it was NOT opened, never that it worked: the guard decides before anything is posted.
      await waitFor(() =>
        expect(frame.contentWindow.postMessage).toHaveBeenCalledWith(
          { type: OPEN_SESSION_RESULT_TYPE, item_id: ITEM, code: 'open_declined' },
          '*',
        ),
      )
      expect(frame.contentWindow.postMessage).not.toHaveBeenCalledWith(
        { type: OPEN_SESSION_RESULT_TYPE, item_id: ITEM, code: null },
        '*',
      )
      expect(screen.getByTestId('path')).toHaveTextContent('/members')
      expect(dispatch).not.toHaveBeenCalledWith(expect.objectContaining({ type: 'test/switchSlot' }))
    })

    it('switches when the guard allows, or when no answer is waiting', async () => {
      const frame = mountFrame()
      vi.spyOn(window, 'focus').mockImplementation(() => {})
      vi.spyOn(api, 'crewBoardOpen').mockResolvedValue({ ok: true, action: 'open', item_id: ITEM, slot: WORKER_SLOT })
      const verdict = vi.fn(() => true)
      const dispatch = mountProbe(frame, verdict)
      post({ type: OPEN_SESSION_MESSAGE_TYPE, item_id: ITEM }, frame.contentWindow)
      await waitFor(() => expect(screen.getByTestId('path')).toHaveTextContent('/chat'))
      // Asked once, not twice, and the page is told it worked.
      expect(verdict).toHaveBeenCalledTimes(1)
      expect(frame.contentWindow.postMessage).toHaveBeenCalledWith(
        { type: OPEN_SESSION_RESULT_TYPE, item_id: ITEM, code: null },
        '*',
      )
      expect(dispatch).toHaveBeenCalledWith({
        type: 'test/switchSlot',
        payload: { key: WORKER_SLOT, announceOnMissing: true, gesture: true },
      })
    })
  })

  it('stops listening when the host unmounts', async () => {
    const frame = mountFrame()
    const ask = vi.spyOn(api, 'crewBoardOpen')
    const { unmount } = await renderHookWithProviders(() => useFrameOpenSession({ current: frame }, SESSION, { open: vi.fn(), gesture: () => true, tied: () => true }))
    unmount()
    post({ type: OPEN_SESSION_MESSAGE_TYPE, item_id: ITEM }, frame.contentWindow)
    expect(ask).not.toHaveBeenCalled()
  })
})
