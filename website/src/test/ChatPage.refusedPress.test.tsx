/**
 * Regression test: a server-refused regenerate or switch-variant press renders
 * the refusal above the composer instead of dying in console.warn.
 *
 * Why this is pinned at the ChatPage layer: the two presses live in different
 * shapes — `handleRegenerate` is a memoized callback, the switch-variant handler
 * is built inline inside the message renderer — but both must land their
 * rejection on the ONE shared refused-press surface. A unit test of either
 * handler passes whether or not the surface exists; only the mounted page
 * proves the refusal becomes pixels.
 *
 * The behaviour being defended: these endpoints re-check under the slot lock
 * and refuse for ordinary, user-actionable reasons (a turn already running, a
 * stop in progress, a pending approval, a readiness probe that timed out). The
 * old catch logged to the console, so the button flicked to disabled and
 * straight back with nothing on screen — the user's only theory was "the click
 * did nothing", and the next move was pressing it again.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, act, waitFor, fireEvent } from '@testing-library/react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import chatReducer, { setSlotRunning, setActiveSlot } from '../store/chatSlice'
import { ApiError } from '../api/apiError'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'
import { ThemeProvider } from '../hooks/useTheme'
import type { RootState } from '../store'

interface VirtuosoMockProps {
  data?: unknown[]
  itemContent: (index: number, item: unknown) => ReactNode
}
vi.mock('react-virtuoso', () => ({ Virtuoso: ({ data, itemContent }: VirtuosoMockProps) => <div data-testid="virtuoso">{data?.map((d: unknown, i: number) => <div key={i}>{itemContent(i, d)}</div>)}</div> }))

/**
 * ChatPage refetches the active slot on mount and the response REPLACES
 * `chat.messages`, so the fetch and the preloaded store must tell the same
 * story or the transcript this test clicks on would be silently blanked.
 */
type Msg = { role: string; content: string; variants?: { content: string; ts?: string }[]; variant_idx?: number; meta?: Record<string, unknown> }
const detail = vi.hoisted(() => ({ messages: [] as Msg[], running: false }))
const regenerateSlot = vi.hoisted(() => vi.fn())
const switchVariant = vi.hoisted(() => vi.fn())
const continueSlot = vi.hoisted(() => vi.fn())
const steerChat = vi.hoisted(() => vi.fn())
vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    chatSlotDetail: vi.fn(async () => ({ messages: detail.messages, running: detail.running, has_more: false, total: detail.messages.length })),
    chatHistory: vi.fn().mockResolvedValue({ sessions: [] }),
    models: vi.fn().mockResolvedValue([]),
    agents: vi.fn().mockResolvedValue([]),
    agentDetail: vi.fn().mockResolvedValue({}),
    workspaces: vi.fn().mockResolvedValue({ workspaces: [] }),
    slackChannels: vi.fn().mockResolvedValue([]),
    spawnList: vi.fn().mockResolvedValue({ agents: [] }),
    regenerateSlot: (...a: unknown[]) => regenerateSlot(...a),
    switchVariant: (...a: unknown[]) => switchVariant(...a),
    continueSlot: (...a: unknown[]) => continueSlot(...a),
    steerChat: (...a: unknown[]) => steerChat(...a),
  },
  SEARCH_MIN_CHARS: 2,
}))
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: false }))
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Test', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [], defaultAgent: 'default' }) }))
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <span>{content}</span> }))
vi.mock('../components/WelcomeView', () => ({ default: () => null }))
vi.mock('../components/MarkdownPanel', () => ({ default: () => null }))
vi.mock('../pages/chat/ActivityViewer', () => ({ default: () => null }))
vi.mock('../components/DetailPanel', () => ({ default: () => null }))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
})

import ChatPage from '../pages/ChatPage'

function makeStore(messages: Msg[], running = false) {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null,
        slots: [
          { key: 'slot-a', messages: messages.length, running, mode: '', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined },
          // A second slot so tests can genuinely switch away from slot-a —
          // ChatPage validates activeSlot against this roster and snaps back
          // to the first slot when the target does not exist.
          { key: 'slot-b', messages: 0, running: false, mode: '', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined },
        ],
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
      chat: {
        activeSlot: 'slot-a', messages,
        slotRunning: running, slotStopping: false, slotState: 'idle',
        history: [], historyHasMore: false, pendingInput: null,
        subagents: {}, toolLog: [], activityOpen: false, activityTab: 'tools',
        slotHasMore: false, slotOldestIndex: 0, loadingOlder: false,
        slotStatusDetail: {}, slotContextPct: {}, slotActivity: {}, slotHistory: [],
        historyOffset: 0, _wsChunkedDuringFetch: false,
        slotMessages: {}, slotLoading: false,
      } as unknown as RootState['chat'],
      notifications: { items: [] } as unknown as RootState['notifications'],
    },
  })
}

async function renderWith(messages: Msg[], running = false) {
  detail.messages = messages
  detail.running = running
  const store = makeStore(messages, running)
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  await act(async () => {
    render(
      <QueryClientProvider client={qc}>
        <Provider store={store}>
          <ThemeProvider>
            <MemoryRouter><ChatPage /></MemoryRouter>
          </ThemeProvider>
        </Provider>
      </QueryClientProvider>,
    )
  })
  return store
}

const plainTurn: Msg[] = [
  { role: 'user', content: 'hello' },
  { role: 'assistant', content: 'first answer' },
]

const variantTurn: Msg[] = [
  { role: 'user', content: 'hello' },
  {
    role: 'assistant', content: 'take two',
    variants: [{ content: 'take one' }, { content: 'take two' }],
    variant_idx: 1,
  },
]

/** Nothing came back at all — the shape that offers Continue in the composer. */
const interruptedTurn: Msg[] = [{ role: 'user', content: 'do the thing' }]

beforeEach(() => {
  regenerateSlot.mockReset()
  switchVariant.mockReset()
  continueSlot.mockReset()
  steerChat.mockReset()
})

describe('refused presses render the notice above the composer', () => {
  it('a refused regenerate shows the server reason with the per-action title', async () => {
    regenerateSlot.mockRejectedValue(new Error('A turn is already running'))
    await renderWith(plainTurn)

    fireEvent.click(screen.getByRole('button', { name: 'Regenerate response' }))

    const notice = await screen.findByTestId('refused-press-error')
    expect(notice.textContent).toContain("Couldn't regenerate")
    expect(notice.textContent).toContain('A turn is already running')
  })

  it('a refused switch-variant shows the server reason, and dismiss clears it', async () => {
    switchVariant.mockRejectedValue(new Error('stop in progress'))
    await renderWith(variantTurn)

    fireEvent.click(screen.getByRole('button', { name: 'Previous version' }))

    const notice = await screen.findByTestId('refused-press-error')
    expect(notice.textContent).toContain("Couldn't switch version")
    expect(notice.textContent).toContain('stop in progress')
    expect(switchVariant).toHaveBeenCalledWith('slot-a', 0)

    fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }))
    await waitFor(() => expect(screen.queryByTestId('refused-press-error')).toBeNull())
  })

  it('a refused continue shows the server reason with its own title', async () => {
    // Continue is the third press on this surface. It is refused by the same
    // slot-lock re-check as the other two (sub-agents still delivering, a queued
    // message, a pending approval), and before this it was the loudest silent
    // failure of the three: the button sits on the error card of a turn that
    // already failed, so "nothing happened" reads as the recovery itself being
    // broken.
    continueSlot.mockRejectedValue(new Error('sub-agents are running'))
    await renderWith(interruptedTurn)

    fireEvent.click(screen.getByTestId('composer-continue'))

    const notice = await screen.findByTestId('refused-press-error')
    expect(notice.textContent).toContain("Couldn't continue")
    expect(notice.textContent).toContain('sub-agents are running')
  })

  it('a rejected steer shows the server reason above the composer (#8625)', async () => {
    // Steer is the fourth press on the surface, and it was the most silent of
    // all: steer() clears the composer before the POST settles, so a rejection
    // left the user with no text, no bubble follow-up, and nothing on screen —
    // console.error was the only trace. Mid-turn (slotRunning) Enter routes to
    // steer, which is exactly the state this drives. A 4xx ApiError is a
    // DEFINITIVE refusal: the server answered and refused, so the rollback
    // (bubble splice + text restore) applies.
    steerChat.mockRejectedValue(new ApiError(409, 'no live turn to steer into'))
    const store = await renderWith(plainTurn, true)

    const input = await waitFor(() => screen.getByLabelText('Message input') as HTMLTextAreaElement)
    fireEvent.change(input, { target: { value: 'change course now' } })
    await act(async () => {
      fireEvent.keyDown(input, { key: 'Enter' })
      await Promise.resolve()
    })
    await waitFor(() => expect(steerChat).toHaveBeenCalled())

    const notice = await screen.findByTestId('refused-press-error')
    expect(notice.textContent).toContain("Couldn't steer")
    expect(notice.textContent).toContain('no live turn to steer into')
    // The notice carries the agent hand-off: the hand-off stages a prompt for
    // a fresh session and per-slot drafts persist across the switch, so it
    // destroys nothing here (AGENTS.md errors-use-error-notice).
    expect(screen.getByRole('button', { name: /Ask the agent/ })).toBeInTheDocument()
    // The optimistic bubble is gone from the transcript: nothing reached the
    // turn, so a row with a steer badge would assert delivery right above the
    // refusal notice. Asserted on store state (the composer's sizing mirror
    // also carries the text, so a DOM text query is ambiguous). The text
    // itself is NOT lost — it is restored into the composer (steer() cleared
    // it at send time, and with the bubble gone the restore is the user's
    // only remaining copy).
    await waitFor(() => {
      expect((screen.getByLabelText('Message input') as HTMLTextAreaElement).value).toContain('change course now')
    })
    const rows = (store.getState().chat.messages as { role: string; content: string }[])
      .filter(m => m.role === 'user' && m.content === 'change course now')
    expect(rows).toHaveLength(0)
  })

  it('a transport failure reports without rolling back the steer (#8625)', async () => {
    // A dropped connection proves nothing about delivery: the steer may have
    // executed with only the receipt lost. The notice must still appear (a
    // silent maybe-failure is the defect class), but the optimistic bubble
    // stays for WS/history reconciliation and the composer is NOT restored —
    // restoring would invite a resend that executes the steer twice.
    steerChat.mockRejectedValue(new TypeError('Failed to fetch'))
    const store = await renderWith(plainTurn, true)

    const input = await waitFor(() => screen.getByLabelText('Message input') as HTMLTextAreaElement)
    fireEvent.change(input, { target: { value: 'change course now' } })
    await act(async () => {
      fireEvent.keyDown(input, { key: 'Enter' })
      await Promise.resolve()
    })
    await waitFor(() => expect(steerChat).toHaveBeenCalled())

    const notice = await screen.findByTestId('refused-press-error')
    // Delivery is UNKNOWN here, so the title must not assert refusal — the
    // optimistic bubble is still on screen and a flat "Couldn't steer" beside
    // it would contradict the transcript and invite a double-send.
    expect(notice.textContent).toContain('Steer may not have arrived')
    expect(notice.textContent).toContain('Failed to fetch')
    // Bubble retained, composer left alone.
    const rows = (store.getState().chat.messages as { role: string; content: string; meta?: Record<string, unknown> }[])
      .filter(m => m.role === 'user' && m.content === 'change course now')
    expect(rows).toHaveLength(1)
    expect(rows[0].meta?.optimistic).toBe(true)
    expect((screen.getByLabelText('Message input') as HTMLTextAreaElement).value).toBe('')
  })

  it('a background-slot steer rejection is retained and surfaces on return (#8625)', async () => {
    // The rejection lands while the user is on another slot: it must not be
    // discarded (adjudicated errors-use-error-notice hit on an earlier
    // revision) — it is retained per origin slot and shown when the user
    // returns.
    let rejectSteer!: (e: unknown) => void
    steerChat.mockImplementationOnce(() => new Promise((_res, rej) => { rejectSteer = rej }))
    const store = await renderWith(plainTurn, true)

    const input = await waitFor(() => screen.getByLabelText('Message input') as HTMLTextAreaElement)
    fireEvent.change(input, { target: { value: 'change course now' } })
    await act(async () => { fireEvent.keyDown(input, { key: 'Enter' }); await Promise.resolve() })
    await waitFor(() => expect(steerChat).toHaveBeenCalled())

    // Switch away, then let the steer reject in the background.
    await act(async () => { store.dispatch(setActiveSlot('slot-b')) })
    await act(async () => { rejectSteer(new ApiError(409, 'no live turn to steer into')); await Promise.resolve(); await Promise.resolve() })
    expect(screen.queryByTestId('refused-press-error')).toBeNull()

    // Returning to the origin slot surfaces the retained refusal.
    await act(async () => { store.dispatch(setActiveSlot('slot-a')) })
    const notice = await screen.findByTestId('refused-press-error')
    expect(notice.textContent).toContain("Couldn't steer")
    expect(notice.textContent).toContain('no live turn to steer into')
  })

  it('a stale steer success does not clear a newer steer refusal (#8625)', async () => {
    // Steers are independent messages, not retries: when an older steer
    // settles successfully AFTER a newer one already failed, the failure
    // notice must survive — the old success proves nothing about the new
    // message. (Ordering is per-slot attempt counters; see steerMutation.)
    let resolveFirst!: (v: unknown) => void
    steerChat
      .mockImplementationOnce(() => new Promise(res => { resolveFirst = res }))
      .mockImplementationOnce(() => Promise.reject(new Error('steer channel closed')))
    await renderWith(plainTurn, true)

    const input = await waitFor(() => screen.getByLabelText('Message input') as HTMLTextAreaElement)
    // Steer 1: stays pending.
    fireEvent.change(input, { target: { value: 'first steer' } })
    await act(async () => { fireEvent.keyDown(input, { key: 'Enter' }); await Promise.resolve() })
    // Steer 2: rejects and raises the notice.
    fireEvent.change(input, { target: { value: 'second steer' } })
    await act(async () => { fireEvent.keyDown(input, { key: 'Enter' }); await Promise.resolve() })
    await screen.findByTestId('refused-press-error')

    // Steer 1 now settles successfully — the notice must stay.
    await act(async () => { resolveFirst({ ok: true, steered: true }); await Promise.resolve(); await Promise.resolve() })
    expect(screen.getByTestId('refused-press-error').textContent).toContain('steer channel closed')
  })

  it('a turn taking over retires the refusal', async () => {
    regenerateSlot.mockRejectedValue(new Error('pending approval'))
    const store = await renderWith(plainTurn)

    fireEvent.click(screen.getByRole('button', { name: 'Regenerate response' }))
    await screen.findByTestId('refused-press-error')

    // The turn starting is the success signal for whatever the slot was busy
    // with — the old reason would now describe a state that passed.
    await act(async () => { store.dispatch(setSlotRunning(true)) })
    await waitFor(() => expect(screen.queryByTestId('refused-press-error')).toBeNull())
  })
})
