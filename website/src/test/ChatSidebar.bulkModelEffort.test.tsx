/**
 * Switch All Sessions panel: the effort picker that rides along with the model.
 *
 * The pick is sent only when the target model can use effort ("Keep" and a
 * non-effort model both omit it, so each session keeps its own), the Switch
 * count includes sessions whose only difference is their effort and is split
 * above the button into the sessions that reset and the sessions that keep
 * their conversation -- each group headed by its count and listing the
 * sessions by name, so the reader can tell WHICH conversation a reset would
 * wipe (mid-turn and remote-bound effort-only sessions excluded, as the
 * backend skips them) -- the Default row names where its default comes from
 * (and says when that could not be read), and a partial outcome keeps the
 * panel open with every applicable notice, each naming its sessions.
 * Harness shared with ChatSidebar.bulkModelRosterError.test.tsx.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import type { RootState } from '../store'

// Render framer-motion elements as plain DOM because jsdom cannot run projection.
vi.mock('framer-motion', async () => {
  const React = await import('react')
  const FRAMER_PROPS = new Set([
    'layout', 'layoutId', 'layoutScroll', 'initial', 'animate', 'exit',
    'transition', 'variants', 'whileHover', 'whileTap', 'whileInView',
    'drag', 'dragConstraints', 'dragElastic', 'onAnimationComplete',
  ])
  const make = (tag: string) =>
    React.forwardRef((props: Record<string, unknown>, ref: React.Ref<unknown>) => {
      const clean: Record<string, unknown> = {}
      for (const k of Object.keys(props)) {
        if (k === 'children') continue
        if (k === 'layoutId') { clean['data-layout-id'] = props[k]; continue }
        if (FRAMER_PROPS.has(k)) continue
        clean[k] = props[k]
      }
      return React.createElement(tag, { ...clean, ref }, props.children as React.ReactNode)
    })
  const motion = new Proxy({}, { get: (_t, tag: string) => make(tag) })
  return {
    motion,
    AnimatePresence: ({ children }: { children?: React.ReactNode }) => React.createElement(React.Fragment, null, children),
    LayoutGroup: ({ children }: { children?: React.ReactNode }) => React.createElement(React.Fragment, null, children),
  }
})

vi.mock('../components/ProjectPicker', () => ({ default: () => null }))

vi.mock('../pages/chat/ChatSettings', () => ({
  loadChatConfig: () => ({ tagColumnsEnabled: false, confirmCloseSession: false, defaultAutopilot: false }),
  saveChatConfig: vi.fn(),
}))

const mocks = vi.hoisted(() => ({
  models: vi.fn(),
  chatSlotsModel: vi.fn(),
  chatFolders: vi.fn(),
  chatTags: vi.fn(),
  tagColumns: vi.fn(),
  kirocrewConfig: vi.fn(),
  sessions: vi.fn(),
}))
vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy(mocks as unknown as Record<string, unknown>, {
    get: (target, prop: string) => (prop in target ? target[prop] : vi.fn().mockResolvedValue([])),
  }),
}))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockImplementation((q: string) => ({
    matches: false, media: q, onchange: null,
    addListener: vi.fn(), removeListener: vi.fn(),
    addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn(),
  })),
})

import ChatSidebar from '../pages/ChatSidebar'

type TestSlot = { key: string; title: string; running: boolean; messages: number; model?: string; reasoning_effort?: string; executor?: 'local' | 'remote' }

const LIVE_ROSTER = [
  { model_name: 'auto', description: 'Default' },
  { model_name: 'opus-4.8', description: 'Opus' },
  { model_name: 'sonnet-4.7', description: 'Sonnet' },
]

function renderSidebar(slots: TestSlot[]) {
  const defaults = createTestStore().getState()
  const store = createTestStore({
    dashboard: {
      ...defaults.dashboard,
      status: {}, connected: true, slots, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      slotsLoaded: true,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as unknown as RootState['dashboard'],
    chat: {
      ...defaults.chat,
      activeSlot: null, slotStatusDetail: {}, subagents: {}, slotActivity: {},
      automations: {}, workflowRuns: {}, subagentQueued: {}, slotHistory: [],
      revealRequest: null, revealNonce: 0,
    } as unknown as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  qc.setQueryData(['chat-folders'], [])
  qc.setQueryData(['tag-columns'], [])
  render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={slots} activeSlot={null} unreadSlots={[]}
              history={[]} historyHasMore={false}
              defaultAgent="" installedAgents={[{ name: 'builder', source: 'builtin' }]}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
}

async function openSwitchAllPanel() {
  fireEvent.keyDown(screen.getAllByLabelText('More options')[0], { key: 'Enter' })
  fireEvent.click(await screen.findByText('Switch all to model…'))
  expect(screen.getByText('Switch All Sessions')).toBeTruthy()
}

const effortTrigger = () => screen.getByRole('combobox', { name: 'Effort' })
const switchBtn = () => screen.getByRole('button', { name: /^Switch \d+ sessions?$/ })
/** The session names listed under one block of the reset/keep split, in order. */
const listedNames = (blockTestId: string) =>
  within(screen.getByTestId(blockTestId)).getAllByRole('listitem').map(li => li.textContent)

/** Pick a row in the model listbox by the model id it sends. */
async function pickModel(id: string) {
  const listbox = screen.getByRole('listbox', { name: 'Model list' })
  await waitFor(() => expect(within(listbox).getAllByRole('option').length).toBe(LIVE_ROSTER.length))
  const row = within(listbox)
    .getAllByRole('option')
    .find(o => o.querySelector('[data-model-id]')?.getAttribute('data-model-id') === id)
  expect(row).toBeTruthy()
  fireEvent.click(row!)
}

async function pickEffort(label: string) {
  fireEvent.click(effortTrigger())
  fireEvent.click(await screen.findByRole('option', { name: label }))
}

const IDLE_ON_OPUS: TestSlot[] = [
  { key: 'k-a', title: 'Idle A', running: false, messages: 1, model: 'opus-4.8', reasoning_effort: 'low' },
  { key: 'k-b', title: 'Idle B', running: false, messages: 1, model: 'opus-4.8', reasoning_effort: 'high' },
]
const IDLE_ON_SONNET: TestSlot[] = [
  { key: 'k-a', title: 'Idle A', running: false, messages: 1, model: 'sonnet-4.7' },
]

beforeEach(() => {
  localStorage.clear()
  mocks.models.mockResolvedValue(LIVE_ROSTER)
  mocks.chatSlotsModel.mockResolvedValue({ ok: true, switched: [], skipped_running: [], unchanged: [], failed: [], effort_not_applied: [] })
  mocks.chatFolders.mockResolvedValue([])
  mocks.chatTags.mockResolvedValue([])
  mocks.tagColumns.mockResolvedValue([])
  mocks.sessions.mockResolvedValue({ sessions: [], has_more: false })
  mocks.kirocrewConfig.mockResolvedValue({ agent: { reasoning_effort: 'medium' } })
})
afterEach(() => {
  vi.clearAllMocks()
})

describe('ChatSidebar — Switch All Sessions effort', () => {
  it('is disabled until a model that takes effort is picked, and shows no choice while disabled', async () => {
    renderSidebar(IDLE_ON_OPUS)
    await openSwitchAllPanel()
    expect(effortTrigger()).toBeDisabled()
    // An em dash, not "Keep current effort": there is no pick yet, and a value
    // in a disabled control would read as one being remembered.
    expect(effortTrigger()).toHaveTextContent('—')
    expect(effortTrigger()).not.toHaveTextContent('Keep')
    const hint = screen.getByTestId('bulk-effort-unsupported')
    expect(hint).toHaveTextContent('Pick a model to choose its effort level.')
    expect(effortTrigger().getAttribute('aria-describedby')).toBe(hint.id)
    await pickModel('opus-4.8')
    expect(effortTrigger()).not.toBeDisabled()
    expect(effortTrigger()).toHaveTextContent('Keep current effort')
  })

  it('sends the picked level with the model', async () => {
    renderSidebar(IDLE_ON_SONNET)
    await openSwitchAllPanel()
    await pickModel('opus-4.8')
    await pickEffort('Max')
    fireEvent.click(switchBtn())
    await waitFor(() => expect(mocks.chatSlotsModel).toHaveBeenCalledWith('opus-4.8', true, 'max'))
  })

  it('omits the level under Keep, so each session keeps its own', async () => {
    renderSidebar(IDLE_ON_SONNET)
    await openSwitchAllPanel()
    await pickModel('opus-4.8')
    fireEvent.click(switchBtn())
    await waitFor(() => expect(mocks.chatSlotsModel).toHaveBeenCalledTimes(1))
    expect(mocks.chatSlotsModel.mock.calls[0][2]).toBeUndefined()
  })

  it('shows no reset/keep split before a model is picked, since nothing would change yet', async () => {
    renderSidebar(IDLE_ON_SONNET)
    await openSwitchAllPanel()
    expect(switchBtn()).toBeDisabled()
    expect(screen.queryByTestId('bulk-switch-split')).toBeNull()
    await pickModel('opus-4.8')
    expect(screen.getByTestId('bulk-switch-split')).toBeInTheDocument()
  })

  it('counts sessions whose only difference is their effort, as ones that keep their conversation', async () => {
    // Both sessions already run opus-4.8, so under Keep there is nothing to do;
    // picking High makes the one at Low a switch, and the one at High stays put.
    renderSidebar(IDLE_ON_OPUS)
    await openSwitchAllPanel()
    await pickModel('opus-4.8')
    expect(switchBtn()).toBeDisabled()
    expect(screen.queryByTestId('bulk-switch-split')).toBeNull()  // nothing to split at 0
    await pickEffort('High')
    expect(switchBtn()).toHaveTextContent('Switch 1 session')
    expect(switchBtn()).not.toBeDisabled()
    expect(screen.getByTestId('bulk-switch-keep')).toHaveTextContent('1 keeps its conversation')
    expect(listedNames('bulk-switch-keep')).toEqual(['Idle A'])
    expect(screen.queryByTestId('bulk-switch-reset')).toBeNull()
    fireEvent.click(switchBtn())
    await waitFor(() => expect(mocks.chatSlotsModel).toHaveBeenCalledWith('opus-4.8', true, 'high'))
  })

  it('splits the button count into the sessions that reset and the sessions that keep their conversation, by name', async () => {
    // Two on sonnet change model (they reset); one on opus at Low changes only
    // its effort (it keeps); one on opus at High is unchanged. The button says
    // 3, the blocks above it say which are which -- each count with the
    // sessions behind it listed by name, so the reader knows exactly which
    // conversation a reset would wipe -- and the reset heading carries the
    // same warning colour as the description's "resets its conversation".
    renderSidebar([
      { key: 'k-a', title: 'A', running: false, messages: 1, model: 'sonnet-4.7' },
      { key: 'k-b', title: 'B', running: false, messages: 1, model: 'sonnet-4.7', reasoning_effort: 'high' },
      { key: 'k-c', title: 'C', running: false, messages: 1, model: 'opus-4.8', reasoning_effort: 'low' },
      { key: 'k-d', title: 'D', running: false, messages: 1, model: 'opus-4.8', reasoning_effort: 'high' },
    ])
    await openSwitchAllPanel()
    await pickModel('opus-4.8')
    // Under Keep only the model changes count, and all of them reset.
    expect(switchBtn()).toHaveTextContent('Switch 2 sessions')
    expect(screen.getByTestId('bulk-switch-reset')).toHaveTextContent('2 reset their conversations')
    expect(listedNames('bulk-switch-reset')).toEqual(['A', 'B'])
    expect(screen.queryByTestId('bulk-switch-keep')).toBeNull()
    await pickEffort('High')
    expect(switchBtn()).toHaveTextContent('Switch 3 sessions')
    expect(screen.getByTestId('bulk-switch-reset')).toHaveTextContent('2 reset their conversations')
    expect(listedNames('bulk-switch-reset')).toEqual(['A', 'B'])
    expect(screen.getByTestId('bulk-switch-keep')).toHaveTextContent('1 keeps its conversation')
    expect(listedNames('bulk-switch-keep')).toEqual(['C'])
    expect(screen.getByTestId('bulk-switch-split')).not.toHaveTextContent('D')  // unchanged, so not on the button
    expect(screen.getByText('2 reset their conversations')).toHaveClass('text-danger')
    expect(screen.getByText('1 keeps its conversation')).not.toHaveClass('text-danger')
    // Plain text, not a control: the names are already on screen, so there is
    // nothing to drill into and nothing here should look clickable.
    expect(within(screen.getByTestId('bulk-switch-split')).queryByRole('button')).toBeNull()
    expect(within(screen.getByTestId('bulk-switch-split')).queryByRole('link')).toBeNull()
  })

  it('does not count a mid-turn effort-only session, whatever the running checkbox says', async () => {
    // The backend leaves an effort-only session alone while it is mid-turn (no
    // turn to tear down, so it waits for a retry) even under skip_running=false,
    // while a model change on a running session IS forced through by that box.
    renderSidebar([
      { key: 'k-a', title: 'Busy effort-only', running: true, messages: 1, model: 'opus-4.8', reasoning_effort: 'low' },
      { key: 'k-b', title: 'Busy model change', running: true, messages: 1, model: 'sonnet-4.7' },
      { key: 'k-c', title: 'Idle effort-only', running: false, messages: 1, model: 'opus-4.8', reasoning_effort: 'low' },
    ])
    await openSwitchAllPanel()
    await pickModel('opus-4.8')
    await pickEffort('High')
    // Skipping running sessions (the default): only the idle effort-only one.
    expect(switchBtn()).toHaveTextContent('Switch 1 session')
    expect(listedNames('bulk-switch-keep')).toEqual(['Idle effort-only'])
    expect(screen.queryByTestId('bulk-switch-reset')).toBeNull()
    // Untick "skip running": the running model change joins; the running
    // effort-only session still does not.
    fireEvent.click(screen.getByRole('checkbox'))
    expect(switchBtn()).toHaveTextContent('Switch 2 sessions')
    expect(screen.getByTestId('bulk-switch-reset')).toHaveTextContent('1 resets its conversation')
    expect(listedNames('bulk-switch-reset')).toEqual(['Busy model change'])
    expect(screen.getByTestId('bulk-switch-keep')).toHaveTextContent('1 keeps its conversation')
    expect(listedNames('bulk-switch-keep')).toEqual(['Idle effort-only'])
  })

  it('does not count a remote-bound effort-only session, but still counts its model change', async () => {
    // A remote-bound session's effort is its peer's to run, so the backend
    // never applies an effort-only pick to it; its model still moves.
    renderSidebar([
      { key: 'k-a', title: 'Remote effort-only', running: false, messages: 1, model: 'opus-4.8', reasoning_effort: 'low', executor: 'remote' },
      { key: 'k-b', title: 'Remote model change', running: false, messages: 1, model: 'sonnet-4.7', reasoning_effort: 'low', executor: 'remote' },
      { key: 'k-c', title: 'Local effort-only', running: false, messages: 1, model: 'opus-4.8', reasoning_effort: 'low', executor: 'local' },
    ])
    await openSwitchAllPanel()
    await pickModel('opus-4.8')
    await pickEffort('High')
    expect(switchBtn()).toHaveTextContent('Switch 2 sessions')
    expect(listedNames('bulk-switch-reset')).toEqual(['Remote model change'])
    expect(listedNames('bulk-switch-keep')).toEqual(['Local effort-only'])
  })

  it('keeps the panel open with a status line naming sessions that were skipped or manage their own effort', async () => {
    // A 200 with skipped or effort-not-applied entries and nothing failed is a
    // partial outcome, not a success: the panel stays open, its picks intact,
    // and says what did not happen, to WHICH sessions, and whether a retry can
    // help. Names come from the rows on screen, so the reader can find them.
    mocks.chatSlotsModel.mockResolvedValue({
      ok: true, switched: ['k-a'], skipped_running: ['k-b', 'k-c'], unchanged: [], failed: [], effort_not_applied: ['k-a'],
    })
    renderSidebar([
      { key: 'k-a', title: 'Idle A', running: false, messages: 1, model: 'sonnet-4.7' },
      { key: 'k-b', title: 'Busy B', running: true, messages: 1, model: 'sonnet-4.7' },
      { key: 'k-c', title: 'Busy C', running: true, messages: 1, model: 'sonnet-4.7' },
    ])
    await openSwitchAllPanel()
    await pickModel('opus-4.8')
    await pickEffort('High')
    fireEvent.click(switchBtn())
    const notice = await screen.findByTestId('bulk-model-notice')
    expect(notice).toHaveTextContent(
      "2 sessions were still replying, so nothing changed for them: Busy B and Busy C. Press Switch again when their replies finish to include them. 1 remote session manages its own effort level, so it wasn't changed: Idle A.",
    )
    expect(notice).toHaveAttribute('role', 'status')
    expect(screen.getByText('Switch All Sessions')).toBeTruthy()  // still open
    expect(effortTrigger()).toHaveTextContent('High')              // pick kept for the retry
    expect(screen.queryByTestId('bulk-model-error')).toBeNull()   // not an error
  })

  it('says only what applies: a lone busy skip, singular, naming the session', async () => {
    mocks.chatSlotsModel.mockResolvedValue({
      ok: true, switched: [], skipped_running: ['k-a'], unchanged: [], failed: [], effort_not_applied: [],
    })
    renderSidebar(IDLE_ON_SONNET)
    await openSwitchAllPanel()
    await pickModel('opus-4.8')
    fireEvent.click(switchBtn())
    const notice = await screen.findByTestId('bulk-model-notice')
    expect(notice).toHaveTextContent('1 session was still replying, so nothing changed for it: Idle A. Press Switch again when its reply finishes to include it.')
    expect(notice).not.toHaveTextContent('remote')
  })

  it('renders both the failure and the remote-managed notice when one response carries both', async () => {
    // The backend classifies each slot independently in one loop, so one
    // response can carry a failed reset AND a remote-bound session whose
    // effort the peer kept. The failure must not hide the remote notice: it is
    // the one outcome no retry reaches, so the reader has to see it now.
    mocks.chatSlotsModel.mockResolvedValue({
      ok: true, switched: ['k-b'], skipped_running: [], unchanged: [], failed: ['k-a'], effort_not_applied: ['k-b'],
    })
    renderSidebar([
      { key: 'k-a', title: 'Local A', running: false, messages: 1, model: 'sonnet-4.7' },
      { key: 'k-b', title: 'Remote B', running: false, messages: 1, model: 'sonnet-4.7', executor: 'remote' },
    ])
    await openSwitchAllPanel()
    await pickModel('opus-4.8')
    await pickEffort('High')
    fireEvent.click(switchBtn())
    const error = await screen.findByTestId('bulk-model-error')
    expect(error).toHaveTextContent('1 session failed to switch: Local A.')
    const notice = screen.getByTestId('bulk-model-notice')
    expect(notice).toHaveTextContent("1 remote session manages its own effort level, so it wasn't changed: Remote B.")
    expect(notice).not.toHaveTextContent('busy')
    expect(screen.getByText('Switch All Sessions')).toBeTruthy()  // still open
    expect(effortTrigger()).toHaveTextContent('High')              // picks intact
  })

  it('renders a named notice for sessions whose effort is deferred to their next start, and keeps the panel open', async () => {
    // effort_deferred: the switch recorded the new default effort, but the live
    // session keeps its current level until it next starts. Nothing to retry,
    // yet the reader is told which sessions and what will happen. Like the other
    // partial outcomes it keeps the panel open with the picks intact.
    mocks.chatSlotsModel.mockResolvedValue({
      ok: true, switched: ['k-a'], skipped_running: [], unchanged: [], failed: [], effort_not_applied: [], effort_deferred: ['k-a'],
    })
    renderSidebar(IDLE_ON_SONNET)
    await openSwitchAllPanel()
    await pickModel('opus-4.8')
    await pickEffort('High')
    fireEvent.click(switchBtn())
    const notice = await screen.findByTestId('bulk-model-notice')
    expect(notice).toHaveTextContent('1 session keeps its current effort until it next starts, then uses the default: Idle A.')
    expect(notice).toHaveAttribute('role', 'status')
    expect(screen.getByText('Switch All Sessions')).toBeTruthy()  // still open
    expect(effortTrigger()).toHaveTextContent('High')              // pick kept for the retry
    expect(screen.queryByTestId('bulk-model-error')).toBeNull()   // not an error
  })

  it('closes the panel on a clean success and clears the picks', async () => {
    renderSidebar(IDLE_ON_SONNET)
    await openSwitchAllPanel()
    await pickModel('opus-4.8')
    await pickEffort('High')
    fireEvent.click(switchBtn())
    await waitFor(() => expect(screen.queryByText('Switch All Sessions')).toBeNull())
    expect(screen.queryByTestId('bulk-model-notice')).toBeNull()
  })

  it('names where the Default row\'s default comes from, apart from the model list\'s own Default', async () => {
    renderSidebar(IDLE_ON_OPUS)
    await openSwitchAllPanel()
    await pickModel('opus-4.8')
    fireEvent.click(effortTrigger())
    expect(await screen.findByRole('option', { name: 'Default from Settings · Medium' })).toBeTruthy()
    expect(screen.queryByRole('option', { name: 'Default · Medium' })).toBeNull()
  })

  it('anchors the Default row to Settings even when no default is configured', async () => {
    mocks.kirocrewConfig.mockResolvedValue({ agent: { reasoning_effort: '' } })
    renderSidebar(IDLE_ON_OPUS)
    await openSwitchAllPanel()
    await pickModel('opus-4.8')
    fireEvent.click(effortTrigger())
    // Nothing configured, so the row names the Settings default as unset rather
    // than reading a bare "Model default" that drops the Settings anchor.
    expect(await screen.findByRole('option', { name: 'Default from Settings · not set' })).toBeTruthy()
    expect(screen.queryByRole('option', { name: 'Model default' })).toBeNull()
  })

  it('says when the Settings default could not be read, and still offers the Default row', async () => {
    // A failed config read is an error the panel must show, not swallow: the
    // Default row would otherwise say "Model default" as if none were
    // configured. The row keeps working -- it still clears each session to the
    // configured default -- it just cannot name the level, and the notice says
    // so. No hand-off on the notice: the panel's picks are unsaved.
    mocks.kirocrewConfig.mockRejectedValue(new Error('config unavailable'))
    renderSidebar(IDLE_ON_OPUS)
    await openSwitchAllPanel()
    const notice = await screen.findByTestId('bulk-effort-config-error')
    expect(notice).toHaveTextContent("Couldn't read the default effort from Settings. The “Default from Settings” row still applies it, but can't show the level.")
    expect(within(notice).queryByRole('button', { name: /ask the agent/i })).toBeNull()
    await pickModel('opus-4.8')
    fireEvent.click(effortTrigger())
    expect(await screen.findByRole('option', { name: 'Default from Settings' })).toBeTruthy()
    expect(screen.queryByRole('option', { name: 'Model default' })).toBeNull()
    expect(screen.queryByRole('option', { name: /Default from Settings ·/ })).toBeNull()
    fireEvent.click(screen.getByRole('option', { name: 'Default from Settings' }))
    // Both sessions run at an explicit level, so clearing to the default counts them.
    expect(switchBtn()).toHaveTextContent('Switch 2 sessions')
    fireEvent.click(switchBtn())
    await waitFor(() => expect(mocks.chatSlotsModel).toHaveBeenCalledWith('opus-4.8', true, ''))
  })

  it('says which change resets a conversation and which keeps it', async () => {
    renderSidebar(IDLE_ON_OPUS)
    await openSwitchAllPanel()
    const intro = screen.getByText(/Pick a model for every session, and an effort level/)
    expect(intro).toHaveTextContent(
      "Pick a model for every session, and an effort level if that model uses one. Changing a session's model resets its conversation. Changing only its effort keeps the conversation.",
    )
    // The red emphasis stays on the reset wording, and only on it.
    expect(screen.getByText('resets its conversation')).toHaveClass('text-danger')
  })

  it('cues the direction of the scale while the control is live', async () => {
    renderSidebar(IDLE_ON_OPUS)
    await openSwitchAllPanel()
    expect(screen.queryByTestId('bulk-effort-scale')).toBeNull()  // nothing before a model is picked
    await pickModel('opus-4.8')
    const hint = screen.getByTestId('bulk-effort-scale')
    expect(hint).toHaveTextContent('Lower levels are faster, higher levels are smarter.')
    expect(effortTrigger().getAttribute('aria-describedby')).toBe(hint.id)
  })

  it('drops the pick and says why when the model does not take effort', async () => {
    renderSidebar(IDLE_ON_SONNET)
    await openSwitchAllPanel()
    await pickModel('opus-4.8')
    await pickEffort('High')
    await pickModel('auto')
    expect(effortTrigger()).toBeDisabled()
    const hint = screen.getByTestId('bulk-effort-unsupported')
    expect(hint).toHaveTextContent("This model doesn't use effort levels, so each session keeps its own.")
    expect(screen.queryByTestId('bulk-effort-scale')).toBeNull()  // one hint at a time
    expect(effortTrigger().getAttribute('aria-describedby')).toBe(hint.id)
    fireEvent.click(switchBtn())
    await waitFor(() => expect(mocks.chatSlotsModel).toHaveBeenCalledTimes(1))
    expect(mocks.chatSlotsModel.mock.calls[0][0]).toBe('auto')
    expect(mocks.chatSlotsModel.mock.calls[0][2]).toBeUndefined()
  })
})
