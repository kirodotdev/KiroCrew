/**
 * Regression test: "Auto" must be selectable as the session model.
 *
 * Collapsing the Auto pick to '' before sending it is wrong:
 *   const val = modelName === 'auto' ? '' : modelName
 * '' is ALSO the "no model chosen yet" state, and every reader of an empty
 * model re-resolves it to the agent template's model — ChatPage's
 * `resolvedModel` query for existing slots, its `_initResolvedModel` effect for
 * the not-yet-created slot, and the backend's `slot.model` backfill in
 * chat_runner. That would snap Auto straight back to the agent's model (e.g.
 * claude-opus-5) so Auto could never be selected.
 *
 * Instead the picker sends 'auto' verbatim — the id kiro-cli advertises (and
 * reports as its `default_model`), and the value the ChatPane and Alt+Shift
 * model-cycle paths already use.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, act, waitFor, fireEvent } from '@testing-library/react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import chatReducer from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'
import { ThemeProvider } from '../hooks/useTheme'
import type { RootState } from '../store'

// The agent template pins a concrete model — this is what an empty slot.model
// resolves to, and what would clobber an explicit Auto pick. Repeated as a
// literal inside the vi.mock factory below, which is hoisted above this const.
const AGENT_MODEL = 'claude-opus-5'

interface VirtuosoMockProps {
  data?: unknown[]
  itemContent: (index: number, item: unknown) => ReactNode
}
vi.mock('react-virtuoso', () => ({ Virtuoso: ({ data, itemContent }: VirtuosoMockProps) => <div data-testid="virtuoso">{data?.map((d: unknown, i: number) => <div key={i}>{itemContent(i, d)}</div>)}</div> }))
vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [], running: false, has_more: false, total: 0 }),
    chatHistory: vi.fn().mockResolvedValue({ sessions: [] }),
    models: vi.fn().mockResolvedValue([
      { model_name: 'auto', description: 'Models chosen by task' },
      { model_name: 'claude-opus-5', description: 'Claude Opus 5' },
      { model_name: 'claude-sonnet-5', description: 'Claude Sonnet 5' },
    ]),
    agents: vi.fn().mockResolvedValue([]),
    agentDetail: vi.fn().mockResolvedValue({ model: 'claude-opus-5' }),
    // The backend resolver owns the default-model precedence; the composer
    // asks it what an un-pinned slot would run on.
    agentResolvedModel: vi.fn().mockResolvedValue({ model: 'claude-opus-5' }),
    chatSlotModel: vi.fn().mockResolvedValue({ ok: true }),
    workspaces: vi.fn().mockResolvedValue({ workspaces: [] }),
    slackChannels: vi.fn().mockResolvedValue([]),
    spawnList: vi.fn().mockResolvedValue({ agents: [] }),
  },
  SEARCH_MIN_CHARS: 2,
}))
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: false }))
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Test', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [{ name: 'kirocrew' }], defaultAgent: 'kirocrew' }) }))
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

/** `model` is deliberately optional so a test can render the legacy "never
 *  chosen" slot (no model key at all) as well as an explicit pick. */
function makeStore(model?: string) {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null,
        slots: [{ key: 'slot-a', messages: 0, running: false, mode: '', agent: 'kirocrew', model, pending_approval: false, waiting_for_input: false, last_activity_ts: undefined }],
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
      chat: {
        activeSlot: 'slot-a', messages: [],
        slotRunning: false, slotStopping: false, slotState: 'idle',
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

async function renderChat(model?: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  await act(async () => {
    render(
      <QueryClientProvider client={qc}>
        <Provider store={makeStore(model)}>
          <ThemeProvider>
            <MemoryRouter><ChatPage /></MemoryRouter>
          </ThemeProvider>
        </Provider>
      </QueryClientProvider>,
    )
  })
  await waitFor(() => expect(screen.getByLabelText('Message input')).toBeTruthy())
}

/** Open the model picker from the composer's model chip. */
async function openModelPicker() {
  const chip = await waitFor(() => screen.getByTitle(/^Model: /))
  await act(async () => { fireEvent.click(chip) })
  return chip
}

beforeEach(() => {
  sessionStorage.clear()
  localStorage.clear()
  vi.clearAllMocks()
})

describe('ChatPage — Auto model selection', { timeout: 15_000 }, () => {
  it('sends the literal "auto" (not "") when Auto is picked', async () => {
    const { api } = await import('../api/client')
    await renderChat(AGENT_MODEL)
    await openModelPicker()

    const autoOption = await waitFor(() => screen.getByRole('option', { name: /auto/ }))
    await act(async () => { fireEvent.click(autoOption) })

    expect(api.chatSlotModel).toHaveBeenCalledWith('slot-a', 'auto')
    // '' is the "never chosen" state; sending it un-sticks the pick.
    expect(api.chatSlotModel).not.toHaveBeenCalledWith('slot-a', '')
  })

  it('shows Auto as the active model and does not re-resolve it to the agent model', async () => {
    await renderChat('auto')
    expect(await waitFor(() => screen.getByTitle('Model: auto'))).toBeTruthy()

    await openModelPicker()
    const autoOption = await waitFor(() => screen.getByRole('option', { name: /auto/ }))
    expect(autoOption.getAttribute('aria-selected')).toBe('true')
  })

  it('still inherits the resolved default when no model was ever chosen', async () => {
    // Legacy/never-chosen slot: an absent model shows what the backend resolver
    // reports for the agent rather than Auto.
    await renderChat(undefined)
    expect(await waitFor(() => screen.getByTitle(`Model: ${AGENT_MODEL}`))).toBeTruthy()
  })
})
