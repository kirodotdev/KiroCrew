/**
 * Tests for "Keep this chat", the confirm-gated ephemeral-session promotion:
 * the real `usePromoteSlot` hook (confirm, promote, open the copy, report a
 * failure) and the header overflow menu's entry point (`ChatHeaderMenu`).
 *
 * The hook runs through the REAL `useConfirm` dialog (rendered and clicked,
 * never `window.confirm`) and the REAL `promoteSlot` thunk, with only the
 * `api` module's network boundary mocked.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { Provider, useSelector } from 'react-redux'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import chatReducer, { createSlot, deleteSlot, setActiveSlot, switchSlotNoticeCopy } from '../store/chatSlice'
import dashboardReducer, { sseSlots } from '../store/dashboardSlice'
import ErrorNotice from '../components/ErrorNotice'
import { i18nT } from '../i18n/t'
import { api } from '../api/client'
import { ApiError } from '../api/apiError'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import { ChatHeaderMenu } from '../pages/chat/ChatHeaderMenu'
import { usePromoteSlot } from '../pages/chat/usePromoteSlot'
import { store as appStore, type AppDispatch, type AppStore, type RootState } from '../store'
import type { ChatSlot } from '../types'

vi.mock('../api/client', async importOriginal => {
  const actual = await importOriginal<typeof import('../api/client')>()
  return {
    ...actual,
    api: {
      promoteChatSlot: vi.fn(),
      chatSlotDetail: vi.fn(),
      deleteChatSlot: vi.fn().mockResolvedValue({}),
      // ChatHeaderMenu renders SessionActionsMenu (+ its LinkedSurfacesSection),
      // which queries these unconditionally on mount.
      unlinkSlack: vi.fn().mockResolvedValue({ ok: true, was_linked: true }),
      slackLink: vi.fn().mockResolvedValue({ ok: true }),
      pauseSlack: vi.fn().mockResolvedValue({ ok: true, was_paused: false }),
      pauseMirror: vi.fn().mockResolvedValue({ ok: true, was_paused: false }),
      unlinkMirror: vi.fn().mockResolvedValue({ ok: true, was_linked: true }),
      linkMirror: vi.fn().mockResolvedValue({ ok: true, conversation_id: 'dm-42' }),
      channelTargets: vi.fn().mockResolvedValue([]),
      slackChannels: vi.fn().mockResolvedValue([]),
      mcpActive: vi.fn().mockResolvedValue([]),
      setSlotColor: vi.fn().mockResolvedValue({}),
      chatFolders: vi.fn().mockResolvedValue([]),
      createChatSlot: vi.fn(),
    },
  }
})

const promoteChatSlotMock = api.promoteChatSlot as unknown as ReturnType<typeof vi.fn>
const chatSlotDetailMock = api.chatSlotDetail as unknown as ReturnType<typeof vi.fn>

function makeStore() {
  return configureStore({ reducer: { chat: chatReducer, dashboard: dashboardReducer } })
}

function PromoteSwitchNotice() {
  const notice = useSelector((state: RootState) => state.chat.switchSlotGone)
  if (!notice) return null
  return (
    <ErrorNotice
      testId="promote-switch-error"
      message={switchSlotNoticeCopy(notice.kind, notice.name)}
    />
  )
}

function Probe({
  store,
  activeSlot,
  memoryMode,
  showActionError,
}: {
  store: ReturnType<typeof makeStore> | AppStore
  activeSlot: string
  memoryMode: 'incognito' | 'temporary'
  showActionError: (message: string) => void
}) {
  const { promote, promoting, promoteConfirmDialog } = usePromoteSlot({
    activeSlot,
    memoryMode,
    dispatch: store.dispatch as AppDispatch,
    showActionError,
  })
  return (
    <Provider store={store}>
      <button onClick={() => { void promote() }} disabled={promoting}>promote</button>
      <PromoteSwitchNotice />
      {promoteConfirmDialog}
    </Provider>
  )
}

function renderProbe(memoryMode: 'incognito' | 'temporary' = 'incognito') {
  const showActionError = vi.fn()
  const utils = render(
    <Probe store={makeStore()} activeSlot="secret" memoryMode={memoryMode} showActionError={showActionError} />,
  )
  return { ...utils, showActionError }
}

beforeEach(() => {
  promoteChatSlotMock.mockReset()
  promoteChatSlotMock.mockResolvedValue({ ok: true, key: 'chat-1-kept', title: 'Kept', messages: 3 })
  chatSlotDetailMock.mockReset()
  chatSlotDetailMock.mockResolvedValue({ messages: [], has_more: false, total: 0 })
})

describe('usePromoteSlot confirm gating (#9694)', () => {
  it('shows a confirmation dialog stating what is retained before promoting anything', async () => {
    const user = userEvent.setup()
    renderProbe()
    await user.click(screen.getByText('promote'))
    expect(await screen.findByRole('dialog')).toBeInTheDocument()
    expect(screen.getByText('Keep this chat?')).toBeInTheDocument()
    expect(screen.getByText(/copied into a new, regular session/)).toBeInTheDocument()
    expect(screen.getByText(/The original stays private and unchanged/)).toBeInTheDocument()
    expect(promoteChatSlotMock).not.toHaveBeenCalled()
  })

  it('states the missing-memory-reads caveat only for a temporary source', async () => {
    const user = userEvent.setup()
    renderProbe('temporary')
    await user.click(screen.getByText('promote'))
    await screen.findByRole('dialog')
    expect(screen.getByText(/ran without your stored memory/)).toBeInTheDocument()
  })

  it('omits the temporary-mode caveat for an incognito source', async () => {
    const user = userEvent.setup()
    renderProbe('incognito')
    await user.click(screen.getByText('promote'))
    await screen.findByRole('dialog')
    expect(screen.queryByText(/without your stored memory/)).not.toBeInTheDocument()
  })

  it('never calls the promote API when the user cancels', async () => {
    const user = userEvent.setup()
    renderProbe()
    await user.click(screen.getByText('promote'))
    await screen.findByRole('dialog')
    await user.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(promoteChatSlotMock).not.toHaveBeenCalled()
  })

  it('never calls the promote API when the user dismisses via Escape', async () => {
    const user = userEvent.setup()
    renderProbe()
    await user.click(screen.getByText('promote'))
    await screen.findByRole('dialog')
    await user.keyboard('{Escape}')
    expect(promoteChatSlotMock).not.toHaveBeenCalled()
  })

  it('promotes exactly the active slot once, and only after the user confirms', async () => {
    const user = userEvent.setup()
    renderProbe()
    await user.click(screen.getByText('promote'))
    await user.click(await screen.findByRole('button', { name: 'Keep this chat' }))
    await waitFor(() => expect(promoteChatSlotMock).toHaveBeenCalledTimes(1))
    expect(promoteChatSlotMock).toHaveBeenCalledWith('secret')
  })

  it('uses the primary confirm style, not the danger one', async () => {
    const user = userEvent.setup()
    renderProbe()
    await user.click(screen.getByText('promote'))
    const confirmBtn = await screen.findByRole('button', { name: 'Keep this chat' })
    expect(confirmBtn.className).not.toContain('text-danger')
  })
})

describe('usePromoteSlot outcomes', () => {
  it('shows an error when the kept session cannot be opened', async () => {
    chatSlotDetailMock.mockRejectedValue(
      Object.assign(new Error('service unavailable'), { status: 503 }),
    )
    const user = userEvent.setup()
    renderProbe()
    await user.click(screen.getByText('promote'))
    await user.click(await screen.findByRole('button', { name: 'Keep this chat' }))

    const notice = await screen.findByTestId('promote-switch-error')
    expect(notice).toHaveAttribute('role', 'alert')
    expect(notice).toHaveTextContent(
      i18nT('store.chatSlice.session_open_error_named', { name: 'Kept' }),
    )
  })

  it('reports a generic failure without the backend text', async () => {
    promoteChatSlotMock.mockRejectedValue(
      new ApiError(500, 'internal boom 0xDEADBEEF', JSON.stringify({ error: 'internal boom 0xDEADBEEF' })),
    )
    const user = userEvent.setup()
    const { showActionError } = renderProbe()
    await user.click(screen.getByText('promote'))
    await user.click(await screen.findByRole('button', { name: 'Keep this chat' }))

    await waitFor(() => expect(showActionError).toHaveBeenCalledTimes(1))
    expect(showActionError).toHaveBeenCalledWith("Couldn't keep this chat. Try again.")
  })

  it('reports an over-capacity chat with its own notice, not the fork advice', async () => {
    promoteChatSlotMock.mockRejectedValue(
      new ApiError(400, 'conversation too large to fork', JSON.stringify({ code: 'fork_corpus_too_large' })),
    )
    const user = userEvent.setup()
    const { showActionError } = renderProbe()
    await user.click(screen.getByText('promote'))
    await user.click(await screen.findByRole('button', { name: 'Keep this chat' }))

    await waitFor(() => expect(showActionError).toHaveBeenCalledTimes(1))
    expect(showActionError).toHaveBeenCalledWith(
      'This chat is too long to keep. The original stays in history.',
    )
  })

  it('makes one copy when the action is triggered twice before the first finishes', async () => {
    let resolvePromote: (v: unknown) => void = () => {}
    promoteChatSlotMock.mockReturnValue(new Promise(r => { resolvePromote = r }))
    const user = userEvent.setup()
    renderProbe()
    await user.click(screen.getByText('promote'))
    await user.click(await screen.findByRole('button', { name: 'Keep this chat' }))
    await waitFor(() => expect(promoteChatSlotMock).toHaveBeenCalledTimes(1))
    // The trigger is disabled while the copy is in flight.
    expect(screen.getByText('promote')).toBeDisabled()
    resolvePromote({ ok: true, key: 'chat-1-kept', title: 'Kept', messages: 3 })
    await waitFor(() => expect(screen.getByText('promote')).not.toBeDisabled())
    expect(promoteChatSlotMock).toHaveBeenCalledTimes(1)
  })
})

/** The hook reads the app store to tell whether the user moved, as every fork
 *  caller does, so these cases run on that store. */
describe('usePromoteSlot when the keep resolves late', () => {
  const KEPT = 'chat-1-kept'
  const slot = (key: string): ChatSlot => ({ key, title: key, messages: 2, running: false, folder_id: '' })
  const listed = (key: string) => appStore.getState().dashboard.slots.some(s => s.key === key)

  function deferredPromote() {
    let resolve: (v: unknown) => void = () => {}
    promoteChatSlotMock.mockReturnValue(new Promise(r => { resolve = r }))
    return () => resolve({ ok: true, key: KEPT, title: 'Kept', messages: 3 })
  }

  async function startKeep() {
    const user = userEvent.setup()
    render(<Probe store={appStore} activeSlot="secret" memoryMode="incognito" showActionError={vi.fn()} />)
    await user.click(screen.getByText('promote'))
    await user.click(await screen.findByRole('button', { name: 'Keep this chat' }))
    await waitFor(() => expect(promoteChatSlotMock).toHaveBeenCalledTimes(1))
  }

  beforeEach(() => {
    appStore.dispatch(sseSlots([slot('secret'), slot('other')]))
    appStore.dispatch(setActiveSlot('secret'))
  })
  afterEach(() => {
    appStore.dispatch(setActiveSlot(null))
    appStore.dispatch(sseSlots([]))
  })

  it('leaves the user on the session they opened while the keep ran, and lists the copy', async () => {
    const land = deferredPromote()
    await startKeep()
    act(() => { appStore.dispatch(setActiveSlot('other')) })
    await act(async () => { land() })

    await waitFor(() => expect(screen.getByText('promote')).not.toBeDisabled())
    expect(listed(KEPT)).toBe(true)
    expect(appStore.getState().chat.activeSlot).toBe('other')
  })

  it('opens the copy when the user stayed on the source', async () => {
    const land = deferredPromote()
    await startKeep()
    await act(async () => { land() })

    await waitFor(() => expect(appStore.getState().chat.activeSlot).toBe(KEPT))
    expect(listed(KEPT)).toBe(true)
  })

  it('still opens a New chat clicked while the keep ran, when the keep answers first', async () => {
    const NEW = 'chat-1-new'
    let landCreate: (v: unknown) => void = () => {}
    ;(api.createChatSlot as unknown as ReturnType<typeof vi.fn>).mockImplementation(
      () => new Promise(r => { landCreate = r }),
    )
    const land = deferredPromote()
    await startKeep()
    const create = appStore.dispatch(createSlot({ memory_mode: 'persistent' }))
    await waitFor(() => expect(api.createChatSlot).toHaveBeenCalled())
    await act(async () => { land() })
    await waitFor(() => expect(appStore.getState().chat.activeSlot).toBe(KEPT))

    await act(async () => { landCreate({ key: NEW, title: 'New Session', messages: 0, running: false }); await create })
    expect(appStore.getState().chat.activeSlot).toBe(NEW)
  })

  it('holds the pending-open count from the confirmation until the copy is active', async () => {
    const land = deferredPromote()
    await startKeep()
    expect(appStore.getState().chat.pendingForkOpens).toBe(1)
    await act(async () => { land() })

    await waitFor(() => expect(appStore.getState().chat.activeSlot).toBe(KEPT))
    expect(appStore.getState().chat.pendingForkOpens).toBe(0)
  })

  it('releases the pending-open count when the keep fails', async () => {
    promoteChatSlotMock.mockRejectedValue(new ApiError(500, 'boom', JSON.stringify({ error: 'boom' })))
    await startKeep()

    await waitFor(() => expect(screen.getByText('promote')).not.toBeDisabled())
    expect(appStore.getState().chat.pendingForkOpens).toBe(0)
  })

  it("opens the copy's folder path when it opens the copy", async () => {
    let resolve: (v: unknown) => void = () => {}
    promoteChatSlotMock.mockReturnValue(new Promise(r => { resolve = r }))
    await startKeep()
    await act(async () => { resolve({ ok: true, key: KEPT, title: 'Kept', messages: 3, folder_id: 'folder-kept' }) })

    await waitFor(() => expect(appStore.getState().chat.activeSlot).toBe(KEPT))
    expect(appStore.getState().chat.revealRequest).toMatchObject({
      kind: 'session', target: KEPT, openFoldersOnly: true, folderId: 'folder-kept',
    })
  })

  it('still opens the copy when the source chat is closed while the keep runs', async () => {
    const land = deferredPromote()
    await startKeep()
    await act(async () => { await appStore.dispatch(deleteSlot('secret')) })
    expect(appStore.getState().chat.activeSlot).toBe('other')
    await act(async () => { land() })

    await waitFor(() => expect(appStore.getState().chat.activeSlot).toBe(KEPT))
  })
})

const dashboardState = {
  status: {}, connected: true, slots: [], approvalMode: 'normal',
  channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
  subagentRunning: {}, subagentDetails: {}, subagentText: {},
  sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
} as RootState['dashboard']

/** Renders the real `ChatHeaderMenu` with its trigger already opened. Opening via
 *  keyboard (Enter) sidesteps Radix's PointerEvent-driven mouse-open path, which
 *  jsdom does not implement. */
function renderHeaderMenu(memoryMode: string | undefined, onPromote: () => void, promoting = false) {
  const slot = { key: 'chat-1-100' } as ChatSlot
  const store = createTestStore({ dashboard: { ...dashboardState, slots: [slot] } })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const utils = render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatHeaderMenu activeSlot={slot.key} memoryMode={memoryMode} onPromote={onPromote} promoting={promoting} />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
  fireEvent.keyDown(utils.container.querySelector('button')!, { key: 'Enter' })
  return utils
}

describe('"Keep this chat" lives in the header overflow menu, not a peer title-row button', () => {
  it('offers the item for an incognito slot and fires onPromote on select', async () => {
    const onPromote = vi.fn()
    renderHeaderMenu('incognito', onPromote)
    fireEvent.click(await screen.findByText(i18nT('pages.chatPage.keep_this_chat')))
    expect(onPromote).toHaveBeenCalledTimes(1)
  })

  it('offers the item for a temporary slot', async () => {
    renderHeaderMenu('temporary', vi.fn())
    expect(await screen.findByText(i18nT('pages.chatPage.keep_this_chat'))).toBeInTheDocument()
  })

  it('omits the item for a persistent slot', async () => {
    const onPromote = vi.fn()
    renderHeaderMenu('persistent', onPromote)
    expect(await screen.findByText(i18nT('components.sessionActionsMenu.pin'))).toBeInTheDocument()
    expect(screen.queryByText(i18nT('pages.chatPage.keep_this_chat'))).not.toBeInTheDocument()
    expect(onPromote).not.toHaveBeenCalled()
  })

  it('disables the item while a keep is in flight', async () => {
    const onPromote = vi.fn()
    renderHeaderMenu('incognito', onPromote, true)
    const item = await screen.findByRole('menuitem', { name: i18nT('pages.chatPage.keep_this_chat') })
    expect(item).toHaveAttribute('aria-disabled', 'true')
    fireEvent.click(item)
    expect(onPromote).not.toHaveBeenCalled()
  })
})
