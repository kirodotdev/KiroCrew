/**
 * usePaletteActions — the live half of the §2 Enter matrix (the pure
 * `resolveInvokableEnter` primitive is covered in paletteActions.test.ts).
 *
 * What matters here is the wiring: a token only ever reaches chat through
 * `setPendingInput` (never a second FE-side resolver), a new session is created
 * BEFORE navigating so ChatPage's auto-create cannot race it into a duplicate,
 * and a failed create is reported instead of silently seeding nothing.
 */
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
import { act, renderHook, waitFor } from '@testing-library/react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import type { ReactNode } from 'react'

const navigateSpy = vi.hoisted(() => vi.fn())
vi.mock('react-router-dom', async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  useNavigate: () => navigateSpy,
}))

const createSlotOutcome = vi.hoisted(() => ({ fail: false }))
/** The real thunk's `fulfilled` creator, so a mocked create opens its session
 *  through the real reducer and records `lastCreatedActivation`. */
const realCreateSlot = vi.hoisted(() => ({ fulfilled: null as null | ((...a: unknown[]) => unknown) }))
const createSlot = vi.hoisted(() =>
  vi.fn(() => (dispatch: (a: unknown) => unknown, getState: () => { chat: { activeSlot: string | null; activeSlotMoves?: number; activeSlotAutoMoves?: number } }) => {
    const requestId = 'zzq-create'
    const { chat } = getState()
    const meta = { originActiveSlot: chat.activeSlot, originActiveSlotMoves: chat.activeSlotMoves ?? 0, originActiveSlotAutoMoves: chat.activeSlotAutoMoves ?? 0, activate: true }
    const slot = { key: 'zzq-new-slot', title: '', messages: 0, running: false }
    const p = createSlotOutcome.fail
      ? Promise.reject(new Error('zzq create failed'))
      : Promise.resolve(slot).then((s) => { dispatch(realCreateSlot.fulfilled!(s, requestId, undefined, meta)); return s })
    // Mimic createAsyncThunk's dispatch return: a promise carrying unwrap().
    return Object.assign(p.catch(() => undefined), { unwrap: () => p, requestId })
  }),
)
vi.mock('../../store/chatSlice', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../store/chatSlice')>()
  realCreateSlot.fulfilled = actual.createSlot.fulfilled as unknown as (...a: unknown[]) => unknown
  return { ...actual, createSlot }
})

import { createTestStore } from '../../test/helpers'
import { setActiveSlot } from '../../store/chatSlice'
import { usePaletteActions } from './paletteActions'

function harness(activeSlot: string | null) {
  const store = createTestStore()
  if (activeSlot) store.dispatch(setActiveSlot(activeSlot))
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>
      <Provider store={store}>
        <MemoryRouter>{children}</MemoryRouter>
      </Provider>
    </QueryClientProvider>
  )
  return { store, ...renderHook(() => usePaletteActions(), { wrapper }) }
}

beforeEach(() => {
  navigateSpy.mockClear()
  createSlot.mockClear()
  createSlotOutcome.fail = false
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe('usePaletteActions — hasActiveChat', () => {
  it('is false with no active slot', () => {
    expect(harness(null).result.current.hasActiveChat).toBe(false)
  })

  it('is true once a slot is active', () => {
    expect(harness('zzq-slot-1').result.current.hasActiveChat).toBe(true)
  })
})

describe('insertToken', () => {
  it('seeds the active composer verbatim and creates no session', () => {
    const { result, store } = harness('zzq-slot-1')
    act(() => result.current.insertToken('$zzq-skill'))
    expect(store.getState().chat.pendingInput).toBe('$zzq-skill')
    expect(createSlot).not.toHaveBeenCalled()
  })
})

describe('newSessionWithToken', () => {
  it('creates the session first, then seeds it and navigates to chat', async () => {
    // Asked from a page that is not a chat surface, so the create navigates.
    window.history.replaceState(null, '', '/settings')
    try {
      const { result, store } = harness(null)
      act(() => result.current.newSessionWithToken('@zzq/prompt'))
      // Nothing is seeded or navigated until the create resolves.
      expect(store.getState().chat.pendingInput).toBeNull()
      expect(navigateSpy).not.toHaveBeenCalled()

      await waitFor(() => expect(createSlot).toHaveBeenCalledTimes(1))
      await waitFor(() => expect(store.getState().chat.pendingInput).toBe('@zzq/prompt'))
      expect(navigateSpy).toHaveBeenCalledWith('/chat')
    } finally {
      window.history.replaceState(null, '', '/')
    }
  })

  it('reports a failed create and seeds nothing', async () => {
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {})
    createSlotOutcome.fail = true
    const { result, store } = harness(null)
    act(() => result.current.newSessionWithToken('@zzq/prompt'))

    await waitFor(() => expect(consoleError).toHaveBeenCalled())
    expect(consoleError.mock.calls[0][0]).toContain('[palette]')
    expect(store.getState().chat.pendingInput).toBeNull()
    expect(navigateSpy).not.toHaveBeenCalled()
  })
})

describe('enterInsertOrNewSession', () => {
  it('inserts into the active chat', () => {
    const { result, store } = harness('zzq-slot-1')
    act(() => result.current.enterInsertOrNewSession('$zzq-skill'))
    expect(store.getState().chat.pendingInput).toBe('$zzq-skill')
    expect(createSlot).not.toHaveBeenCalled()
  })

  it('opens a new session when there is none', async () => {
    const { result, store } = harness(null)
    act(() => result.current.enterInsertOrNewSession('$zzq-skill'))
    await waitFor(() => expect(createSlot).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(store.getState().chat.pendingInput).toBe('$zzq-skill'))
  })
})

describe('navigate', () => {
  it('forwards an in-app route to the router', () => {
    const { result } = harness('zzq-slot-1')
    act(() => result.current.navigate('/zzq-route'))
    expect(navigateSpy).toHaveBeenCalledWith('/zzq-route', { state: { leaveGranted: true } })
  })
})
