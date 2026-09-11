/**
 * Tests for "Keep this chat", the confirm-gated ephemeral-session promotion
 * flow: the header overflow menu's entry point (`ChatHeaderMenu`, below) and
 * `handlePromote`'s confirm-then-dispatch logic.
 *
 * SCOPE NOTE: `handlePromote` is a `useCallback` defined inline inside the
 * (very large) `ChatPage` component and is not exported standalone -- the
 * same constraint documented in `ChatPage.handleFork.test.tsx`. This file
 * reproduces `handlePromote`'s confirm-then-dispatch logic verbatim, driven
 * through the REAL `useConfirm` hook (rendered and clicked -- never
 * `window.confirm`) and the REAL `promoteSlot` thunk, with only the `api`
 * module's network boundary mocked. It does not assert the post-success
 * `switchSlot` dispatch, which is real dashboard-navigation plumbing out of
 * scope here -- the same boundary `ChatPage.handleFork.test.tsx` draws
 * around fork's own `switchSlot` call.
 *
 * What this covers that a bare thunk/api test could not: the confirmation
 * step must gate on an actual rendered dialog the user can read and cancel
 * -- "Keep this chat" is human-initiated, so a test that only asserted the
 * thunk dispatch would miss a regression that fired the API call before (or
 * without) showing the dialog. The menu-item tests further pin that the
 * entry point lives in the shared header overflow menu (`ChatHeaderMenu`),
 * gated on memory_mode, rather than a peer button in the title row.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { Provider } from 'react-redux'
import { fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import chatReducer, { promoteSlot } from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import { useConfirm } from '../components/ConfirmDialog'
import { i18nT } from '../i18n/t'
import { api } from '../api/client'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import { ChatHeaderMenu } from '../pages/chat/ChatPageMessageContent'
import type { RootState } from '../store'
import type { ChatSlot } from '../types'

vi.mock('../api/client', () => ({
  api: {
    promoteChatSlot: vi.fn(),
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
  },
}))

const promoteChatSlotMock = api.promoteChatSlot as unknown as ReturnType<typeof vi.fn>

function makeStore() {
  return configureStore({ reducer: { chat: chatReducer, dashboard: dashboardReducer } })
}

/**
 * Verbatim reproduction of ChatPage.tsx's `handlePromote`: the `confirm()`
 * call with its title/body/confirmLabel/confirmVariant selection (base vs.
 * temporary-mode caveat), then `dispatch(promoteSlot(...)).unwrap()` only
 * once the dialog resolves `true`. See file header for why this is
 * duplicated rather than imported.
 */
function Probe({
  store,
  activeSlot,
  memoryMode,
}: {
  store: ReturnType<typeof makeStore>
  activeSlot: string
  memoryMode: 'incognito' | 'temporary'
}) {
  const { confirm: confirmPromote, confirmDialog } = useConfirm()
  const handlePromote = async () => {
    const isTemporary = memoryMode === 'temporary'
    const confirmed = await confirmPromote({
      title: i18nT('pages.chatPage.keep_this_chat_confirm_title'),
      body: i18nT(
        isTemporary
          ? 'pages.chatPage.keep_this_chat_confirm_body_temporary'
          : 'pages.chatPage.keep_this_chat_confirm_body',
      ),
      confirmLabel: i18nT('pages.chatPage.keep_this_chat'),
      confirmVariant: 'primary',
    })
    if (!confirmed) return
    await store.dispatch(promoteSlot({ slot: activeSlot })).unwrap()
  }
  return (
    <Provider store={store}>
      <button onClick={handlePromote}>promote</button>
      {confirmDialog}
    </Provider>
  )
}

beforeEach(() => {
  promoteChatSlotMock.mockReset()
  promoteChatSlotMock.mockResolvedValue({ ok: true, key: 'chat-1-kept', title: 'Kept', messages: 3 })
})

describe('handlePromote confirm gating (#9694)', () => {
  it('shows a confirmation dialog stating what is retained before promoting anything', async () => {
    const user = userEvent.setup()
    render(<Probe store={makeStore()} activeSlot="secret" memoryMode="incognito" />)
    await user.click(screen.getByText('promote'))
    expect(await screen.findByRole('dialog')).toBeInTheDocument()
    expect(screen.getByText('Keep this chat?')).toBeInTheDocument()
    expect(screen.getByText(/copied into a new, regular session/)).toBeInTheDocument()
    expect(screen.getByText(/stays exactly as private as it is now/)).toBeInTheDocument()
    // Opening the dialog alone must not call the API.
    expect(promoteChatSlotMock).not.toHaveBeenCalled()
  })

  it('states the missing-memory-reads caveat only for a temporary source', async () => {
    const user = userEvent.setup()
    render(<Probe store={makeStore()} activeSlot="scratch" memoryMode="temporary" />)
    await user.click(screen.getByText('promote'))
    await screen.findByRole('dialog')
    expect(screen.getByText(/ran in temporary mode without reading stored memory/)).toBeInTheDocument()
  })

  it('omits the temporary-mode caveat for an incognito source', async () => {
    const user = userEvent.setup()
    render(<Probe store={makeStore()} activeSlot="secret" memoryMode="incognito" />)
    await user.click(screen.getByText('promote'))
    await screen.findByRole('dialog')
    expect(screen.queryByText(/temporary mode/)).not.toBeInTheDocument()
  })

  it('never calls the promote API when the user cancels', async () => {
    const user = userEvent.setup()
    render(<Probe store={makeStore()} activeSlot="secret" memoryMode="incognito" />)
    await user.click(screen.getByText('promote'))
    await screen.findByRole('dialog')
    await user.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(promoteChatSlotMock).not.toHaveBeenCalled()
  })

  it('never calls the promote API when the user dismisses via Escape', async () => {
    const user = userEvent.setup()
    render(<Probe store={makeStore()} activeSlot="secret" memoryMode="incognito" />)
    await user.click(screen.getByText('promote'))
    await screen.findByRole('dialog')
    await user.keyboard('{Escape}')
    expect(promoteChatSlotMock).not.toHaveBeenCalled()
  })

  it('promotes exactly the active slot, and only after the user confirms', async () => {
    const user = userEvent.setup()
    render(<Probe store={makeStore()} activeSlot="secret" memoryMode="incognito" />)
    await user.click(screen.getByText('promote'))
    await user.click(await screen.findByRole('button', { name: 'Keep this chat' }))
    expect(promoteChatSlotMock).toHaveBeenCalledTimes(1)
    expect(promoteChatSlotMock).toHaveBeenCalledWith('secret')
  })

  it('uses the primary (non-destructive) confirm button style, not the danger one', async () => {
    // Promoting is not destructive -- a red confirm button would misstate the
    // stakes of an action that keeps the ephemeral original untouched.
    const user = userEvent.setup()
    render(<Probe store={makeStore()} activeSlot="secret" memoryMode="incognito" />)
    await user.click(screen.getByText('promote'))
    const confirmBtn = await screen.findByRole('button', { name: 'Keep this chat' })
    expect(confirmBtn.className).toContain('bg-accent')
    expect(confirmBtn.className).not.toContain('text-danger')
  })
})

const dashboardState = {
  status: {}, connected: true, slots: [], approvalMode: 'normal',
  channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
  subagentRunning: {}, subagentDetails: {}, subagentText: {},
  sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
} as RootState['dashboard']

/** Renders the real `ChatHeaderMenu` (the shared header overflow menu) with
 *  its trigger already opened, mirroring `ChatHeaderMenu.slack.test.tsx`'s
 *  setup. Opening via keyboard (Enter) rather than a click sidesteps Radix's
 *  PointerEvent-driven mouse-open path, which jsdom does not implement. */
function renderHeaderMenu(memoryMode: string | undefined, onPromote: () => void) {
  const slot = { key: 'chat-1-100' } as ChatSlot
  const store = createTestStore({ dashboard: { ...dashboardState, slots: [slot] } })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const utils = render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatHeaderMenu activeSlot={slot.key} memoryMode={memoryMode} onPromote={onPromote} />
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
    const onPromote = vi.fn()
    renderHeaderMenu('temporary', onPromote)
    expect(await screen.findByText(i18nT('pages.chatPage.keep_this_chat'))).toBeInTheDocument()
  })

  it('omits the item for a persistent slot', async () => {
    const onPromote = vi.fn()
    renderHeaderMenu('persistent', onPromote)
    // Confirm the menu itself rendered, so an absent item below is a real
    // gate and not an empty/unopened menu.
    expect(await screen.findByText(i18nT('components.sessionActionsMenu.pin'))).toBeInTheDocument()
    expect(screen.queryByText(i18nT('pages.chatPage.keep_this_chat'))).not.toBeInTheDocument()
    expect(onPromote).not.toHaveBeenCalled()
  })
})
