/**
 * usePanelLayout → the collapsed sidebar's flyout New surfaces a READABLE
 * notice when the create fails (errors-use-error-notice).
 *
 * `createSlot.rejected` only clears the pending flag and `focusComposerAfter`
 * swallows the rejection (it only governs focus), so without flyoutNew's own
 * `.catch` the flyout New would fail with no visible notice. flyoutNew maps the
 * error so only user-facing copy is shown: an ApiError's message (already built
 * by friendlyErrText) is shown as-is, while a raw thrown exception falls back to
 * a readable sentence instead of leaking developer text. The real `createSlot`
 * thunk runs against a mocked `api.createChatSlot`, so the assertions read the
 * thunk's ACTUAL effect through showActionError.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { renderHook, waitFor, act } from '@testing-library/react'
import { Provider } from 'react-redux'
import { type ReactNode } from 'react'
import { createTestStore } from './helpers'
import { usePanelLayout } from '../pages/chat/page/paneLayout'
import type { RootState } from '../store'
import type { ChatSlot } from '../types'

const mocks = vi.hoisted(() => ({ createChatSlot: vi.fn() }))
vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy(mocks as Record<string, unknown>, {
    get: (target, prop: string) => (prop in target ? target[prop] : vi.fn().mockResolvedValue(undefined)),
  }),
}))
// The default memory mode resolves through the dashboard-config query; keep it
// synchronous and off the network.
vi.mock('../api/queryClient', () => ({ resolveDefaultMemoryMode: vi.fn().mockResolvedValue(undefined) }))

const ORIGIN = 's1'
const NEW_KEY = 'chat-new-1'
const SLOT = { key: ORIGIN, title: 'Session 1', messages: 1, running: false, mode: '', created: '', last_ts: '2026-01-01T00:00:00Z' } as ChatSlot

function setup() {
  const store = createTestStore({
    dashboard: { connected: true, slots: [SLOT], slotsLoaded: true } as unknown as RootState['dashboard'],
    chat: {
      activeSlot: ORIGIN, messages: [], slotRunning: false, slotStopping: false, slotState: 'idle',
      slotStatusDetail: {}, slotHasMore: false, slotOldestIndex: 0, loadingOlder: false,
      history: [], historyHasMore: false, historyOffset: 0, pendingInput: null, slotContextPct: {},
      voicePlaying: false, voiceAudio: null, subagents: {}, toolLog: [], activityOpen: false, activityTab: 'tools',
      slotActivity: {}, slotHistory: [], slotMessages: {}, slotLoading: false,
    } as unknown as RootState['chat'],
  })
  const flyoutClose = vi.fn()
  const showActionError = vi.fn()
  const wrapper = ({ children }: { children: ReactNode }) => <Provider store={store}>{children}</Provider>
  const { result } = renderHook(() => usePanelLayout({
    mode: '',
    dispatch: store.dispatch,
    isMobile: false,
    filteredSlots: [SLOT],
    activeSlot: ORIGIN,
    isWelcomeState: false,
    showActionError,
    currentSlot: SLOT,
    chatFolders: [],
    chatFoldersError: null,
    chatFoldersLoaded: true,
    mobileNewSessionBusy: false,
    mobileNewSessionInFlightRef: { current: false },
    setMobileNewSessionBusy: vi.fn(),
    setSidebarPinned: vi.fn(),
    sidebarPinned: true,
    railWidth: 48,
    winW: 1200,
    effectiveSidebarWidth: 280,
    mobileSessions: false,
    openSidebar: vi.fn(),
    closeSidebar: vi.fn(),
    previewExpanded: false,
    sidebarOpen: true,
    // Only `.close` is read by flyoutNew.
    flyout: { close: flyoutClose } as never,
    memorySwitch: {
      store,
      composerMountedRef: { current: true },
      lockRef: { current: false },
    } as never,
  }), { wrapper })
  return { store, flyoutClose, showActionError, result }
}

describe('usePanelLayout – flyout New surfaces create errors (#21959)', () => {
  beforeEach(() => {
    mocks.createChatSlot.mockReset().mockResolvedValue({ key: NEW_KEY })
  })

  it('a flyout New closes the flyout and creates the session', async () => {
    const { store, flyoutClose, result } = setup()
    act(() => { result.current.flyoutNew() })
    expect(flyoutClose).toHaveBeenCalled()
    await waitFor(() => expect(mocks.createChatSlot).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe(NEW_KEY))
  })

  it('a failed flyout New surfaces the backend message when it is user-facing copy', async () => {
    // errors-use-error-notice: createSlot.rejected only clears the pending flag
    // and focusComposerAfter swallows the rejection, so flyoutNew must surface
    // the failure itself. An ApiError's message is already user copy
    // (apiFailure → friendlyErrText), and it survives the thunk boundary as a
    // serialized { name: 'ApiError', message }, so it is shown as-is.
    mocks.createChatSlot.mockReset().mockRejectedValue({ name: 'ApiError', message: 'The agent runtime is unavailable. Try again.' })
    const { showActionError, result } = setup()
    act(() => { result.current.flyoutNew() })
    await waitFor(() => expect(showActionError).toHaveBeenCalledWith('The agent runtime is unavailable. Try again.'))
  })

  it('a flyout New does NOT show raw exception text; it falls back to readable copy', async () => {
    // A raw thrown exception (here a fetch TypeError) is developer text, so it
    // must NOT reach the notice. The create thunk throws rather than
    // rejectWithValue, so this arrives as a serialized plain object whose
    // `name` is not 'ApiError'; flyoutNew shows the i18n fallback instead.
    const RAW = 'Cannot read properties of undefined (reading \'key\')'
    mocks.createChatSlot.mockReset().mockRejectedValue({ name: 'TypeError', message: RAW })
    const { showActionError, result } = setup()
    act(() => { result.current.flyoutNew() })
    await waitFor(() => expect(showActionError).toHaveBeenCalledWith('Could not start a new session'))
    expect(showActionError).not.toHaveBeenCalledWith(RAW)
  })
})
