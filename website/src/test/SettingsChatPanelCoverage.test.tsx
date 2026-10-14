/**
 * Coverage pass over Settings ▸ Chat (`pages/settings/ChatPanel.tsx`).
 *
 * The existing ChatPanel specs pin a handful of rows in depth (default model,
 * About You, link previews, verbosity). What they leave cold
 * is the long tail: every OTHER row's `onChange`, the per-role model/effort
 * block, the two query-failure banners with their Retry buttons, the save-error
 * banner and its dismiss, and — most importantly — the `onError` arm of each
 * mutation, which is where an optimistic write gets rolled back.
 *
 * This file drives those. It deliberately does NOT re-assert what the sibling
 * specs already own.
 */

// SettingsSelect wraps Radix Select, whose portalled listbox jsdom cannot open;
// the repo's double (also used by SettingsSelect.test.tsx) makes every picker
// here driveable as real role="option" nodes.
vi.mock('@radix-ui/react-select', async () => await import('./__mocks__/@radix-ui/react-select'))

import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, render, screen, fireEvent, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import React from 'react'
import { SETTINGS_CARD_STAGGER_MS } from '../components/settings'
import { resumeWindowLabel, resumeWindowProse } from '../pages/settings/chatPanelOptions'

const BASE_DASH = {
  restore_sessions: false,
  restore_window_minutes: 30,
  resume_interrupted_turns: false,
  resume_retry_limit: 5,
  resume_retry_window_minutes: 60,
  merge_queued_messages: false,
  default_memory_mode: 'persistent' as const,
  widget_density: 'more' as const,
  verbosity: 'default' as const,
  quick_send: false,
  session_grid: false,
  tail_fork_enabled: false,
  link_previews: false,
  mcp_app_panel: false,
  folder_suggestions_enabled: true,
}

const BASE_MC = {
  session: { autocompact_pct: 90 },
  agent: {
    model: 'auto',
    reasoning_effort: '',
    completion_keep: 'head',
    completion_keep_chars: 3000,
    soft_stop_budget_secs: 10,
  },
  dashboard: { user_role: '', user_role_other: '', user_technical_level: '', prevent_sleep: false },
  knowledge: { embed_rate_limit: 120 },
}

const {
  dashboardConfigMock,
  updateDashboardConfigMock,
  kirocrewConfigMock,
  patchConfigMock,
  modelsMock,
  tipsStatusMock,
  tipsFeedbackMock,
} = vi.hoisted(() => ({
  dashboardConfigMock: vi.fn(),
  updateDashboardConfigMock: vi.fn(() => Promise.resolve({})),
  kirocrewConfigMock: vi.fn(),
  patchConfigMock: vi.fn(() => Promise.resolve({})),
  modelsMock: vi.fn(() =>
    Promise.resolve([
      { model_name: 'auto', description: 'Default' },
      { model_name: 'claude-opus-4.8', description: 'Opus' },
      { model_name: 'claude-haiku-4.5', description: 'Haiku' },
    ])
  ),
  tipsStatusMock: vi.fn(() => Promise.resolve({ enabled_config: true, opted_out: false })),
  tipsFeedbackMock: vi.fn(() => Promise.resolve({ ok: true })),
}))

vi.mock('../api/client', () => ({
  api: {
    dashboardConfig: dashboardConfigMock,
    updateDashboardConfig: updateDashboardConfigMock,
    kirocrewConfig: kirocrewConfigMock,
    kirocrewAgents: () => Promise.resolve({ agents: [], default_agent: 'default' }),
    agentResolvedModel: () => Promise.resolve({ model: '', pinned: false }),
    patchConfig: patchConfigMock,
    models: modelsMock,
    voiceConfig: () => Promise.resolve({ enabled: false, voice: 'Ruth', engine: 'neural', rate: '100%', autoSpeak: false, aws_profile: '', region: '' }),
    sttConfig: () => Promise.resolve({ enabled: false, provider: '', model: '', available: false, streaming: false, transcribe_region: '', transcribe_profile: '', language_code: 'en-US', models: {}, language_codes: [] }),
    updateVoiceConfig: () => Promise.resolve({}),
    updateSttConfig: () => Promise.resolve({}),
    tipsStatus: tipsStatusMock,
    tipsFeedback: tipsFeedbackMock,
    // The panel reads the feature-video cache on mount. Downloads OFF here, so
    // the readout renders its policy line and no button -- these files measure
    // other settings, and a live control would put a stray button in their reach.
    featureVideoStatus: () => Promise.resolve({
      enabled: true, download_enabled: false, release: 'r1',
      cached: 0, total: 0, downloading: null,
    }),
    featureVideoFetchAll: () => Promise.resolve({ ok: true }),
  },
}))

import { ChatPanel } from '../pages/settings/ChatPanel'
import { ApiError } from '../api/apiError'
import { markModelsAuthRequired, markModelsDegraded } from '../providers/modelListHealth'
import { resolveDefaultMemoryMode } from '../api/queryClient'
import { MAX_MESSAGE_FONT_SIZE } from '../pages/chat/ChatSettings'
import en from '../i18n/locales/en.json'

import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'

// ChatPanel reads the active slot from redux to name the session on its
// feature-video calls, so these renders need a store. A FRESH one per file,
// not the app singleton: a shared store would carry `activeSlot` across suites.
import { createTestStore } from './helpers'

const LS_KEY = 'mc-chat-config'

function wrap(sub = 'transcript') {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const view = render(
    <MemoryRouter initialEntries={[`/settings?tab=chat&sub=${sub}`]}>
    <Provider store={createTestStore()}>
    <QueryClientProvider client={qc}>
      <ChatPanel />
    </QueryClientProvider>
    </Provider>
    </MemoryRouter>
  )
  return Object.assign(view, { qc })
}

/** The localStorage-backed chat config as the panel last wrote it. */
function storedChat(): Record<string, unknown> {
  return JSON.parse(localStorage.getItem(LS_KEY) || '{}')
}

/** Seed the Kiro Crew config query with a deep-merged override of BASE_MC. */
function seedMc(over: {
  session?: Record<string, unknown>
  agent?: Record<string, unknown>
  dashboard?: Record<string, unknown>
  knowledge?: Record<string, unknown>
} = {}) {
  kirocrewConfigMock.mockImplementation(() =>
    Promise.resolve({
      session: { ...BASE_MC.session, ...over.session },
      agent: { ...BASE_MC.agent, ...over.agent },
      dashboard: { ...BASE_MC.dashboard, ...over.dashboard },
      knowledge: { ...BASE_MC.knowledge, ...over.knowledge },
    }) as never
  )
}

/** A switch that has left its loading-disabled state. */
async function settledSwitch(name: string) {
  const sw = await screen.findByRole('switch', { name })
  await waitFor(() => expect(sw).not.toHaveAttribute('aria-disabled'))
  return sw
}

/** Open a SettingsSelect by label once it is interactive; returns its options. */
async function openSelect(label: string) {
  const trigger = await screen.findByRole('combobox', { name: label })
  await waitFor(() => expect(trigger).not.toHaveAttribute('data-disabled'))
  fireEvent.click(trigger)
  // The settings rail is also a listbox; pick the dropdown's.
  return within(screen.getAllByRole('listbox').find(l => !l.closest('nav'))!).getAllByRole('option')
}

/** Open a select and click the option at `index`. */
async function pickOption(label: string, index: number) {
  const opts = await openSelect(label)
  fireEvent.click(opts[index])
}

/** A number input that has left its loading-disabled state. */
async function settledInput(name: string) {
  const input = (await screen.findByLabelText(name)) as HTMLInputElement
  await waitFor(() => expect(input).not.toBeDisabled())
  return input
}

const rejectOnce = (mock: { mockImplementationOnce: (fn: () => unknown) => unknown }) =>
  mock.mockImplementationOnce(() => Promise.reject(new Error('boom')))

beforeEach(() => {
  localStorage.clear()
  dashboardConfigMock.mockReset()
  updateDashboardConfigMock.mockReset()
  kirocrewConfigMock.mockReset()
  patchConfigMock.mockReset()
  tipsStatusMock.mockReset()
  tipsFeedbackMock.mockReset()
  // The model list health store and the models mock are module-global; a test
  // that drives a degraded / auth-required fetch must not leak that state into
  // the next. Reset both to the healthy default before each test.
  modelsMock.mockReset()
  modelsMock.mockImplementation(() =>
    Promise.resolve([
      { model_name: 'auto', description: 'Default' },
      { model_name: 'claude-opus-4.8', description: 'Opus' },
      { model_name: 'claude-haiku-4.5', description: 'Haiku' },
    ]) as never
  )
  markModelsAuthRequired('acp', false)
  markModelsDegraded('acp', false)
  dashboardConfigMock.mockImplementation(() => Promise.resolve({ ...BASE_DASH }) as never)
  updateDashboardConfigMock.mockImplementation(() => Promise.resolve({}) as never)
  patchConfigMock.mockImplementation(() => Promise.resolve({}) as never)
  tipsStatusMock.mockImplementation(() =>
    Promise.resolve({ enabled_config: true, opted_out: false }) as never
  )
  tipsFeedbackMock.mockImplementation(() => Promise.resolve({ ok: true }) as never)
  seedMc()
})

describe('ChatPanel — load failures', () => {
  it('surfaces a dashboard-config load failure and refetches on Retry', async () => {
    rejectOnce(dashboardConfigMock)
    wrap()
    expect(await screen.findByText('Failed to load dashboard config.')).toBeInTheDocument()
    expect(dashboardConfigMock).toHaveBeenCalledTimes(1)

    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(dashboardConfigMock).toHaveBeenCalledTimes(2))
    await waitFor(() =>
      expect(screen.queryByText('Failed to load dashboard config.')).not.toBeInTheDocument()
    )
  })

  it('surfaces a config load failure and refetches on Retry', async () => {
    rejectOnce(kirocrewConfigMock)
    wrap()
    expect(await screen.findByText('Failed to load config.')).toBeInTheDocument()
    expect(kirocrewConfigMock).toHaveBeenCalledTimes(1)

    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(kirocrewConfigMock).toHaveBeenCalledTimes(2))
    await waitFor(() =>
      expect(screen.queryByText('Failed to load config.')).not.toBeInTheDocument()
    )
  })

  it('shows one Retry per failed query when both fail', async () => {
    rejectOnce(dashboardConfigMock)
    rejectOnce(kirocrewConfigMock)
    wrap()
    await screen.findByText('Failed to load dashboard config.')
    await screen.findByText('Failed to load config.')
    expect(screen.getAllByRole('button', { name: 'Retry' })).toHaveLength(2)
  })

  it('says to run kiro-cli login when the model list is degraded by expired auth', async () => {
    // /api/models answers 503 with the sign-in code (expired kiro-cli login).
    // The acp adapter reads that code where the fetch error forms and flags the
    // provider auth-required; the banner must then show the one actionable
    // sign-in message, NOT the generic "Failed to load config."
    const authErr = new ApiError(
      503,
      'model list unavailable',
      JSON.stringify({ error: 'kiro-cli login', code: 'kiro_prerequisite_required' }),
    )
    modelsMock.mockImplementation(() => Promise.reject(authErr) as never)
    wrap()
    expect(
      await screen.findByText(/Run 'kiro-cli login' in your terminal, then click Retry\./i),
    ).toBeInTheDocument()
    expect(screen.queryByText('Failed to load config.')).not.toBeInTheDocument()
  })

  it('keeps the generic config banner when /api/models is degraded for a non-auth reason', async () => {
    // A generic degraded 503 (code model_list_unavailable) is a transient blip,
    // not an auth wall: the banner stays the generic "Failed to load config." and
    // never claims a sign-in is needed (fail-safe).
    const blip = new ApiError(503, 'model list unavailable', JSON.stringify({ error: 'model list timed out', code: 'model_list_unavailable' }))
    modelsMock.mockImplementation(() => Promise.reject(blip) as never)
    wrap()
    expect(await screen.findByText('Failed to load config.')).toBeInTheDocument()
    expect(screen.queryByText(/kiro-cli login/i)).not.toBeInTheDocument()
  })
})

describe('ChatPanel — save-error banner', () => {
  it('rolls the optimistic write back and explains the failure', async () => {
    rejectOnce(updateDashboardConfigMock)
    wrap('composer')
    const sw = await settledSwitch('Quick Send')
    fireEvent.click(sw)
    expect(await screen.findByText(/Failed to save dashboard config/)).toBeInTheDocument()
    // onError restores the pre-mutation cache entry, so the switch goes back off.
    await waitFor(() => expect(sw).toHaveAttribute('aria-checked', 'false'))
  })

  it('clears the banner when dismissed', async () => {
    rejectOnce(updateDashboardConfigMock)
    wrap('composer')
    fireEvent.click(await settledSwitch('Quick Send'))
    const banner = await screen.findByText(/Failed to save dashboard config/)
    expect(banner).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }))
    await waitFor(() =>
      expect(screen.queryByText(/Failed to save dashboard config/)).not.toBeInTheDocument()
    )
  })
})

describe('ChatPanel — Composer', () => {
  it('stores the picked send shortcut locally', async () => {
    wrap('composer')
    await pickOption('Send shortcut', 1)
    await waitFor(() => expect(storedChat().sendOnEnter).toBe('ctrl-enter'))
  })

  it('stores the follow-up bar layout from the button group', async () => {
    wrap('composer')
    const group = await screen.findByRole('group', { name: 'Follow-Up Bar Layout' })
    fireEvent.click(within(group).getByRole('button', { name: 'Multiline' }))
    await waitFor(() => expect(storedChat().followUpLayout).toBe('multiline'))
  })

  it('persists Quick Send through the dashboard config, sending only that key', async () => {
    wrap('composer')
    fireEvent.click(await settledSwitch('Quick Send'))
    // ONLY the changed key. A full-object body rebuilt from this panel's cached
    // config would write every other setting back at its cached value, clobbering
    // one a second tab changed after we cached it. Siblings are preserved by the
    // handler, which applies only the keys present in the body -- see
    // test/test_session_card_source_links_knob.py::TestConfigEndpoint.
    await waitFor(() =>
      expect(updateDashboardConfigMock).toHaveBeenCalledWith({ quick_send: true })
    )
  })

  it('persists Merge Queued Messages', async () => {
    wrap('composer')
    fireEvent.click(await settledSwitch('Merge Queued Messages'))
    await waitFor(() =>
      expect(updateDashboardConfigMock).toHaveBeenCalledWith({
        merge_queued_messages: true,
      })
    )
  })

  it('PATCHes the soft-stop budget on blur with an in-range value', async () => {
    wrap('composer')
    const input = await settledInput('Soft-stop budget (seconds)')
    await waitFor(() => expect(input.value).toBe('10'))
    fireEvent.change(input, { target: { value: '20.5' } })
    fireEvent.blur(input)
    await waitFor(() =>
      expect(patchConfigMock).toHaveBeenCalledWith('agent.soft_stop_budget_secs', 20.5)
    )
  })

  it.each([
    ['above the ceiling', '600'],
    ['below the floor', '0.1'],
    ['not a number', 'abc'],
  ])('reverts the soft-stop budget and writes nothing when %s', async (_case, typed) => {
    wrap('composer')
    const input = await settledInput('Soft-stop budget (seconds)')
    await waitFor(() => expect(input.value).toBe('10'))
    fireEvent.change(input, { target: { value: typed } })
    fireEvent.blur(input)
    expect(patchConfigMock).not.toHaveBeenCalled()
    expect(input.value).toBe('10')
  })

  it('reverts the soft-stop budget to the server value when the write fails', async () => {
    rejectOnce(patchConfigMock)
    wrap('composer')
    const input = await settledInput('Soft-stop budget (seconds)')
    await waitFor(() => expect(input.value).toBe('10'))
    fireEvent.change(input, { target: { value: '30' } })
    fireEvent.blur(input)
    expect(await screen.findByText(/Failed to save soft-stop budget/)).toBeInTheDocument()
    await waitFor(() => expect(input.value).toBe('10'))
  })
})

describe('ChatPanel — Messages', () => {
  it('stores the text streaming style from the button group', async () => {
    wrap()
    const group = await screen.findByRole('group', { name: 'Text Streaming Style' })
    fireEvent.click(within(group).getByRole('button', { name: 'Immediate' }))
    await waitFor(() => expect(storedChat().streamMode).toBe('immediate'))
  })

  it('stores the content width from the button group', async () => {
    wrap()
    const group = await screen.findByRole('group', { name: 'Content Width' })
    fireEvent.click(within(group).getByRole('button', { name: 'Full' }))
    await waitFor(() => expect(storedChat().contentWidth).toBe('full'))
  })

  it('steps the message font size up and down from the default', async () => {
    wrap()
    await screen.findByText('Message Font Size')
    fireEvent.click(screen.getByRole('button', { name: 'Increase' }))
    await waitFor(() => expect(storedChat().messageFontSize).toBe(15))
    fireEvent.click(screen.getByRole('button', { name: 'Decrease' }))
    fireEvent.click(screen.getByRole('button', { name: 'Decrease' }))
    await waitFor(() => expect(storedChat().messageFontSize).toBe(13))
  })

  it('clamps the message font size at MAX_MESSAGE_FONT_SIZE and does not exceed it', async () => {
    wrap()
    const increase = await screen.findByRole('button', { name: 'Increase' })
    for (let i = 0; i < 20; i++) fireEvent.click(increase)
    await waitFor(() => expect(storedChat().messageFontSize).toBe(MAX_MESSAGE_FONT_SIZE))
  })

  it.each([
    ['Show Timestamps', 'showTimestamps', false],
    ['Double-click to edit your messages', 'doubleClickToEdit', true],
    ['Pin the latest turn', 'pinLastPrompt', false],
    ['Simplified Tool Call Names', 'simplifiedToolNames', false],
    ['Tool calls start expanded', 'toolCallsStartExpanded', true],
    ['Show Context Percentage', 'showContextPct', true],
  ])('stores %s locally when flipped', async (label, key, expected) => {
    wrap()
    fireEvent.click(await screen.findByRole('switch', { name: label }))
    await waitFor(() => expect(storedChat()[key]).toBe(expected))
  })

  it('inverts the stored collapse flag behind Show Thinking Inline', async () => {
    wrap()
    // The row shows the INVERSE of collapseAllSteps, so turning it on must
    // write `false` — a straight passthrough would flip the meaning.
    const sw = await screen.findByRole('switch', { name: 'Show Thinking Inline' })
    expect(sw).toHaveAttribute('aria-checked', 'false')
    fireEvent.click(sw)
    await waitFor(() => expect(storedChat().collapseAllSteps).toBe(false))
  })

  it('stores the file-change chip style', async () => {
    wrap()
    await pickOption('File Change Chips', 1)
    await waitFor(() => expect(storedChat().fileChipStyle).toBe('minimal'))
  })

  it('persists the widget density', async () => {
    wrap()
    await pickOption('Widget Density', 1)
    await waitFor(() =>
      expect(updateDashboardConfigMock).toHaveBeenCalledWith({ widget_density: 'less' })
    )
  })

  it('persists the response verbosity', async () => {
    wrap()
    await pickOption('Response Verbosity', 1)
    await waitFor(() =>
      expect(updateDashboardConfigMock).toHaveBeenCalledWith({ verbosity: 'concise' })
    )
  })

  it('persists the MCP app side-panel toggle', async () => {
    wrap('sidepanel')
    fireEvent.click(await settledSwitch('MCP Apps in Side Panel'))
    await waitFor(() =>
      expect(updateDashboardConfigMock).toHaveBeenCalledWith({ mcp_app_panel: true })
    )
  })

  it.each([
    ['Keep Side Panel Open', 'side_panel_always_open'],
    ['Pin Summary in Side Panel', 'side_panel_pin_summary'],
  ])('shows %s off by default and persists turning it on', async (label, key) => {
    wrap('sidepanel')
    const sw = await settledSwitch(label)
    expect(sw).toHaveAttribute('aria-checked', 'false')
    fireEvent.click(sw)
    await waitFor(() =>
      expect(updateDashboardConfigMock).toHaveBeenCalledWith({ [key]: true })
    )
  })

  it('names Session summaries on the Pin Summary row while they are off, and drops it once they are on', async () => {
    const { qc } = wrap('sidepanel')
    const row = () => document.querySelector('[data-setting-key="dashboard.side_panel_pin_summary"]') as HTMLElement
    const needs = 'Session summaries are off. Turn them on in Settings → Chat → Sessions to pin Summary.'
    // BASE_MC carries no session_summary section: summaries are off.
    await waitFor(() => expect(within(row()).getByText(needs)).toBeInTheDocument())
    // The toggle stays usable while they are off.
    fireEvent.click(await settledSwitch('Pin Summary in Side Panel'))
    await waitFor(() =>
      expect(updateDashboardConfigMock).toHaveBeenCalledWith({ side_panel_pin_summary: true })
    )
    act(() => { qc.setQueryData(['kirocrewConfig'], { ...BASE_MC, session_summary: { enabled: true } }) })
    await waitFor(() => expect(within(row()).queryByText(needs)).toBeNull())
  })

  it('refreshes every chat\'s cached summary state when Session summaries saves', async () => {
    const { qc } = wrap('sessions')
    // The side panel's + menu row and Pin Summary read this per-chat query,
    // which never goes stale on its own.
    qc.setQueryData(['session-summary', 'chat-1'], { enabled: false })
    fireEvent.click(await settledSwitch('Session summaries'))
    await waitFor(() =>
      expect(patchConfigMock).toHaveBeenCalledWith('session_summary.enabled', true)
    )
    await waitFor(() =>
      expect(qc.getQueryState(['session-summary', 'chat-1'])?.isInvalidated).toBe(true)
    )
  })

  it('persists the folder-suggestions toggle', async () => {
    wrap('sessions')
    fireEvent.click(await settledSwitch('Folder suggestions'))
    await waitFor(() =>
      expect(updateDashboardConfigMock).toHaveBeenCalledWith({
        folder_suggestions_enabled: false,
      })
    )
  })

  it('shows topic tags off by default and persists turning them on', async () => {
    wrap('sessions')
    const sw = await settledSwitch('Topic tags')
    expect(sw).toHaveAttribute('aria-checked', 'false')
    fireEvent.click(sw)
    await waitFor(() =>
      expect(updateDashboardConfigMock).toHaveBeenCalledWith({
        topic_tags_enabled: true,
      })
    )
  })

  it('rolls the Feature Tips preference back when the write fails', async () => {
    rejectOnce(tipsFeedbackMock)
    wrap('discovery')
    const sw = await settledSwitch('Feature Tips')
    await waitFor(() => expect(sw).toHaveAttribute('aria-checked', 'true'))
    fireEvent.click(sw)
    expect(await screen.findByText(/Failed to save tips preference/)).toBeInTheDocument()
    await waitFor(() => expect(sw).toHaveAttribute('aria-checked', 'true'))
  })
})

describe('ChatPanel — Sessions', () => {
  it('makes an in-flight default mode authoritative for new chats', async () => {
    let settle!: (value: unknown) => void
    updateDashboardConfigMock.mockImplementationOnce(
      () => new Promise(resolve => { settle = resolve }) as never,
    )
    const view = wrap('sessions')
    await pickOption('Default Memory Mode', 2)
    await waitFor(() => expect(updateDashboardConfigMock).toHaveBeenCalled())
    expect(screen.getByRole('combobox', { name: 'Default Memory Mode' }))
      .toHaveAttribute('data-disabled')
    view.unmount()

    const staleRead = vi.fn(() => Promise.resolve({ default_memory_mode: 'persistent' }))
    await expect(resolveDefaultMemoryMode(staleRead)).resolves.toBe('temporary')
    expect(staleRead).not.toHaveBeenCalled()

    settle({})
    await waitFor(async () => {
      const settledRead = vi.fn(() => Promise.resolve({ default_memory_mode: 'persistent' }))
      await expect(resolveDefaultMemoryMode(settledRead)).resolves.toBe('persistent')
      expect(settledRead).toHaveBeenCalled()
    })
  })

  it('stays locked when an unrelated dashboard mutation settles first', async () => {
    let settleMode!: (value: unknown) => void
    let settleQuickSend!: (value: unknown) => void
    updateDashboardConfigMock
      .mockImplementationOnce(() => new Promise(resolve => { settleMode = resolve }) as never)
      .mockImplementationOnce(() => new Promise(resolve => { settleQuickSend = resolve }) as never)
    wrap('sessions')

    await pickOption('Default Memory Mode', 2)
    // Quick Send lives on the Composer page; panel state survives the switch.
    fireEvent.click(screen.getByRole('option', { name: 'Composer' }))
    fireEvent.click(await settledSwitch('Quick Send'))
    await waitFor(() => expect(updateDashboardConfigMock).toHaveBeenCalledTimes(2))
    settleQuickSend({})
    await waitFor(() => expect(dashboardConfigMock.mock.calls.length).toBeGreaterThan(1))
    fireEvent.click(screen.getByRole('option', { name: 'Sessions' }))
    expect(screen.getByRole('combobox', { name: 'Default Memory Mode' }))
      .toHaveAttribute('data-disabled')

    settleMode({})
    await waitFor(() =>
      expect(screen.getByRole('combobox', { name: 'Default Memory Mode' }))
        .not.toHaveAttribute('data-disabled')
    )
  })

  it('reports a failed mode save after an unrelated dashboard save settles', async () => {
    let rejectMode!: (reason?: unknown) => void
    let settleQuickSend!: (value: unknown) => void
    updateDashboardConfigMock
      .mockImplementationOnce(
        () => new Promise((_resolve, reject) => { rejectMode = reject }) as never,
      )
      .mockImplementationOnce(() => new Promise(resolve => { settleQuickSend = resolve }) as never)
    wrap('sessions')

    await pickOption('Default Memory Mode', 2)
    fireEvent.click(screen.getByRole('option', { name: 'Composer' }))
    fireEvent.click(await settledSwitch('Quick Send'))
    await waitFor(() => expect(updateDashboardConfigMock).toHaveBeenCalledTimes(2))
    settleQuickSend({})
    rejectMode(new Error('mode save failed'))

    expect(await screen.findByText(/Failed to save dashboard config/)).toBeInTheDocument()
  })

  it('persists the default memory mode', async () => {
    wrap('sessions')
    await pickOption('Default Memory Mode', 1)
    await waitFor(() =>
      expect(updateDashboardConfigMock).toHaveBeenCalledWith({
        default_memory_mode: 'incognito',
      })
    )
  })

  it.each([
    ['Split View (Session Grid)', 'session_grid', true],
    ['Tail-only Fork', 'tail_fork_enabled', true],
    ['Restore Sessions', 'restore_sessions', true],
    ['Resume interrupted turns automatically', 'resume_interrupted_turns', true],
  ])('persists %s through the dashboard config', async (label, key, expected) => {
    wrap('sessions')
    fireEvent.click(await settledSwitch(label))
    await waitFor(() =>
      expect(updateDashboardConfigMock).toHaveBeenCalledWith({ [key]: expected })
    )
  })

  it.each([
    ['History Expanded', 'historyExpanded', false],
    ['Confirm Before Closing Session', 'confirmCloseSession', true],
    ['Open Sidebar Sessions in a Session Tab', 'sidebarClickOpensTab', true],
  ])('stores %s locally when flipped', async (label, key, expected) => {
    wrap('sessions')
    fireEvent.click(await screen.findByRole('switch', { name: label }))
    await waitFor(() => expect(storedChat()[key]).toBe(expected))
  })

  it('greys out Dim Inactive Panes until split view is on', async () => {
    wrap('sessions')
    await settledSwitch('Split View (Session Grid)')
    expect(screen.getByRole('switch', { name: 'Dim Inactive Panes' })).toHaveAttribute('aria-disabled', 'true')
  })

  it('stores Dim Inactive Panes locally once split view is on', async () => {
    dashboardConfigMock.mockImplementation(
      () => Promise.resolve({ ...BASE_DASH, session_grid: true }) as never
    )
    wrap('sessions')
    fireEvent.click(await settledSwitch('Dim Inactive Panes'))
    await waitFor(() => expect(storedChat().dimInactivePanes).toBe(false))
  })

  it('hides the restore window until session restore is on', async () => {
    wrap('sessions')
    await settledSwitch('Restore Sessions')
    expect(screen.queryByRole('combobox', { name: 'Restore Window' })).not.toBeInTheDocument()
  })

  it('offers a no-limit window and persists the picked one', async () => {
    dashboardConfigMock.mockImplementation(
      () => Promise.resolve({ ...BASE_DASH, restore_sessions: true }) as never
    )
    wrap('sessions')
    const opts = await openSelect('Restore Window')
    expect(opts.map(o => o.textContent)).toEqual([
      '15m',
      '30m',
      '1h',
      '2h',
      '6h',
      '12h',
      '24h',
      'No limit',
    ])
    fireEvent.click(opts[2])
    await waitFor(() =>
      expect(updateDashboardConfigMock).toHaveBeenCalledWith({
        restore_window_minutes: 60,
      })
    )
  })

  it('hides the automatic-resume limit and window until the switch is on', async () => {
    wrap('sessions')
    await settledSwitch('Resume interrupted turns automatically')
    expect(document.querySelector('[data-setting-key="dashboard.resume_retry_limit"]')).toBeNull()
    expect(document.querySelector('[data-setting-key="dashboard.resume_retry_window_minutes"]')).toBeNull()
  })

  it('states both automatic-resume consequences in the warning the switch is described by, and what the switch does as its help', async () => {
    wrap('sessions')
    const sw = await settledSwitch('Resume interrupted turns automatically')

    const consequenceId = sw.getAttribute('aria-describedby')
    expect(consequenceId).toBeTruthy()
    const warning = document.getElementById(consequenceId as string) as HTMLElement
    // The warn hue as TEXT: plain in dark palettes, darkened in light ones so the
    // three sentences clear the 4.5:1 floor (the plain hue is 3.4:1 on kiro-light).
    expect(warning).toHaveClass('text-warn-text')
    expect(warning).not.toHaveClass('text-warn')
    expect(warning).not.toHaveClass('text-muted')
    // The three consequences share the warning, so none reads as optional help -- one
    // bullet each, the risk first, so none hides inside a paragraph: the irreversible one
    // (repeated work) on its own, the routine spend on its own, then the Trust kept.
    const bullets = within(warning).getAllByRole('listitem').map(li => li.textContent ?? '')
    expect(bullets).toHaveLength(3)
    expect(bullets[0]).toMatch(/^May repeat work the cut-off turn had begun/)
    expect(bullets[0]).not.toMatch(/model turns/)
    expect(bullets[1]).toMatch(/^Uses model turns and tool calls while you are away/)
    // The control is named as the composer labels it -- the Trust segment of Approval mode --
    // so the reader is not sent looking for a "Trust mode" control that does not exist.
    expect(bullets[2]).toMatch(/^After an error, the resumed turn keeps the chat's Trust approval mode, so its tools run without asking/)
    expect(bullets[2]).not.toMatch(/Trust mode/)
    // The approval rule is read in one place: the restart case (a turn that needs an
    // approval waits) is the second sentence of the Trust bullet, the boundary of that
    // risk -- not a bullet of its own (a reassurance among the risks dilutes them) and not
    // a grey sentence a skimmer could take for every automatic resume.
    expect(bullets[2]).toMatch(/without asking\. After a restart, a resumed turn that needs a tool approval waits for you\.$/)
    expect(bullets.filter(b => /waits for you/.test(b))).toHaveLength(1)
    // The row's grey help is the plain statement of the feature, always visible, not a tooltip.
    const help = screen.getByText(/presses Resume for you, up to the per-chat limit below\.$/)
    expect(help).toHaveClass('text-muted')
    expect(help.textContent).not.toContain('Trust')
    expect(help.textContent).not.toContain('repeat work')
    expect(help.textContent).not.toMatch(/waits for you|approval/)
    const row = sw.closest('[data-setting-key="dashboard.resume_interrupted_turns"]') as HTMLElement
    expect(row.querySelector('[data-settings-hint]')).toBeNull()
  })

  it('keeps the automatic-resume feature statement, the two costs and the Trust consequence in separate English catalog keys', () => {
    const chatPanel = (en as { pages: { settings: { chatPanel: Record<string, string> } } }).pages.settings.chatPanel

    expect(chatPanel.resume_interrupted_turns_description).toContain('presses Resume for you')
    expect(chatPanel.resume_interrupted_turns_description).not.toContain('Trust')
    // The help says what the switch does and stops; the approval rule -- both halves --
    // is the Trust bullet's, so a skimmed help cannot read as "every resume waits".
    expect(chatPanel.resume_interrupted_turns_description).toMatch(/up to the per-chat limit below\.$/)
    expect(chatPanel.resume_interrupted_turns_description).not.toMatch(/waits for you|approval/)
    expect(chatPanel.resume_interrupted_turns_consequence).toMatch(/without asking\. After a restart, a resumed turn that needs a tool approval waits for you\.$/)
    // Each consequence opens with its risk, so a reader skimming the bullets meets the
    // hazard first; "Trust" alone would assume they know it is a mode picked in the composer.
    expect(chatPanel.resume_interrupted_turns_cost).toMatch(/^May repeat work the cut-off turn had begun/)
    // The irreversible risk and the routine spend are separate bullets: one sentence holding
    // both let the half-applied edit hide behind the cost of model turns.
    expect(chatPanel.resume_interrupted_turns_cost).not.toMatch(/model turns/)
    expect(chatPanel.resume_interrupted_turns_spend).toMatch(/^Uses model turns and tool calls while you are away\.$/)
    expect(chatPanel.resume_interrupted_turns_consequence).toMatch(/^After an error, the resumed turn keeps the chat's Trust approval mode, so its tools run without asking/)
    expect(chatPanel.resume_interrupted_turns_consequence).not.toMatch(/Trust mode/)
    expect(chatPanel.resume_interrupted_turns_hint).toBeUndefined()
    // The window row's description states the rule; a tip restating it would say it twice.
    expect(chatPanel.resume_retry_window_hint).toBeUndefined()
    // The limit is a maximum, not a count of resumes so far.
    expect(chatPanel.resume_retry_limit).toBe('Max automatic resumes per chat')
  })

  /** The second sentence of the window row's rule, moved out of the limit stepper's tip. */
  const RESET_RULE = ' At the limit the chat pauses and asks you to press Resume, which resets the count.'

  it('states the limit and the window as one rule, with both live values, on the window row', async () => {
    dashboardConfigMock.mockImplementation(
      () => Promise.resolve({ ...BASE_DASH, resume_interrupted_turns: true, resume_retry_limit: 5, resume_retry_window_minutes: 60 }) as never
    )
    wrap('sessions')
    await settledSwitch('Resume interrupted turns automatically')
    const windowRow = document.querySelector('[data-setting-key="dashboard.resume_retry_window_minutes"]') as HTMLElement

    // The pair is on the row itself, always visible: not a tooltip, not the value alone --
    // and the row has no tip at all, since a tip would only restate the sentence. It opens
    // with the rule, not with a pointer back to the stepper, and spells the window out
    // ("1 hour"): inside a sentence the select's "1h" reads like a typo. The second
    // sentence is what the limit decides -- the chat pauses at the limit and a Resume
    // press resets the count -- in the open, not behind the stepper's glyph: a person
    // reads it before choosing a number.
    // The quantity carries fmtUnit's non-breaking space, which the default matcher folds into a
    // plain one on the query side only -- so match the raw text.
    const raw = { normalizer: (t: string) => t }
    const sentence = within(windowRow).getByText(`At most 5 automatic resumes per chat in any 1 hour.${RESET_RULE}`, raw)
    expect(within(windowRow).getByRole('combobox')).toHaveTextContent('1h')
    expect(sentence.textContent).not.toMatch(/^Together with/)
    expect(sentence).toHaveClass('text-muted')
    expect(within(windowRow).queryByRole('button', { name: 'More information' })).toBeNull()
    // The limit row keeps its tip for the one detail the sentence does not carry -- that
    // restart and error resumes share the count -- and no longer for the reset rule, which
    // would only restate the sentence beneath it. (The hidden tip text sits on `title`.)
    const limitRow = document.querySelector('[data-setting-key="dashboard.resume_retry_limit"]') as HTMLElement
    expect(within(limitRow).getByRole('button', { name: 'More information' })).toHaveAttribute(
      'title', 'Restart and error resumes counted together.'
    )
  })

  it('abbreviates the window in the select and spells it out in the rule', () => {
    // The select offers seven windows; a hand-edited config can hold any 5..1440.
    expect(resumeWindowLabel(60)).toBe('1h')
    expect(resumeWindowLabel(5)).toBe('5m')
    expect(resumeWindowLabel(1440)).toBe('24h')
    // Not in the list: a localized quantity of minutes, never an empty sentence.
    expect(resumeWindowLabel(45)).toMatch(/45/)
    expect(resumeWindowLabel(45)).not.toBe('')
    // The prose form: whole hours as hours, anything else as minutes, unit as a word
    // (localized by Intl, so each locale gets its own word and plural). The space is
    // fmtUnit's non-breaking one, so the quantity never wraps between number and unit.
    expect(resumeWindowProse(60)).toBe('1 hour')
    expect(resumeWindowProse(120)).toBe('2 hours')
    expect(resumeWindowProse(1440)).toBe('24 hours')
    expect(resumeWindowProse(5)).toBe('5 minutes')
    expect(resumeWindowProse(45)).toBe('45 minutes')
    expect(resumeWindowProse(60)).not.toMatch(/\d+h\b/)
  })

  it('re-states the rule as the limit steps and as the window is picked', async () => {
    // A gateway that keeps what it was sent: the re-read after each save returns
    // the written values, as the real one does, so the row converges on them.
    let current = { ...BASE_DASH, resume_interrupted_turns: true, resume_retry_limit: 1, resume_retry_window_minutes: 60 }
    dashboardConfigMock.mockImplementation(() => Promise.resolve({ ...current }) as never)
    updateDashboardConfigMock.mockImplementation((patch: Record<string, unknown>) => {
      current = { ...current, ...patch }
      return Promise.resolve({ ...current }) as never
    })
    wrap('sessions')
    await settledSwitch('Resume interrupted turns automatically')
    // A limit of one reads in the singular.
    const raw = { normalizer: (t: string) => t }
    expect(screen.getByText(`At most 1 automatic resume per chat in any 1 hour.${RESET_RULE}`, raw)).toBeInTheDocument()

    const limitRow = document.querySelector('[data-setting-key="dashboard.resume_retry_limit"]') as HTMLElement
    fireEvent.click(within(limitRow).getByRole('button', { name: 'Increase' }))
    await waitFor(() =>
      expect(screen.getByText(`At most 2 automatic resumes per chat in any 1 hour.${RESET_RULE}`, raw)).toBeInTheDocument()
    )

    const opts = await openSelect('Resume limit window')
    fireEvent.click(opts[4])
    await waitFor(() =>
      expect(screen.getByText(`At most 2 automatic resumes per chat in any 2 hours.${RESET_RULE}`, raw)).toBeInTheDocument()
    )
  })

  /** Mount Sessions with the switch on and the limit at `limit`; returns the stepper's row. */
  async function resumeLimitRow(limit: number) {
    dashboardConfigMock.mockImplementation(
      () => Promise.resolve({ ...BASE_DASH, resume_interrupted_turns: true, resume_retry_limit: limit }) as never
    )
    wrap('sessions')
    await settledSwitch('Resume interrupted turns automatically')
    return document.querySelector('[data-setting-key="dashboard.resume_retry_limit"]') as HTMLElement
  }

  it('steps the automatic-resume limit up and persists the new value', async () => {
    const row = await resumeLimitRow(5)
    fireEvent.click(within(row).getByRole('button', { name: 'Increase' }))
    await waitFor(() =>
      expect(updateDashboardConfigMock).toHaveBeenCalledWith({ resume_retry_limit: 6 })
    )
  })

  it('steps the automatic-resume limit down and persists the new value', async () => {
    const row = await resumeLimitRow(5)
    fireEvent.click(within(row).getByRole('button', { name: 'Decrease' }))
    await waitFor(() =>
      expect(updateDashboardConfigMock).toHaveBeenCalledWith({ resume_retry_limit: 4 })
    )
  })

  it('never writes past the ceiling of the automatic-resume limit', async () => {
    const row = await resumeLimitRow(20)
    fireEvent.click(within(row).getByRole('button', { name: 'Increase' }))
    await waitFor(() => expect(updateDashboardConfigMock).toHaveBeenCalledTimes(1))
    expect(updateDashboardConfigMock).toHaveBeenCalledWith({ resume_retry_limit: 20 })
    expect(updateDashboardConfigMock).not.toHaveBeenCalledWith({ resume_retry_limit: 21 })
  })

  it('disables the decrement at the floor of the automatic-resume limit', async () => {
    const row = await resumeLimitRow(1)
    expect(within(row).getByRole('button', { name: 'Decrease' })).toBeDisabled()
    expect(within(row).getByRole('button', { name: 'Increase' })).not.toBeDisabled()
  })

  it('offers the automatic-resume windows and persists the picked one', async () => {
    dashboardConfigMock.mockImplementation(
      () => Promise.resolve({ ...BASE_DASH, resume_interrupted_turns: true }) as never
    )
    wrap('sessions')
    const opts = await openSelect('Resume limit window')
    expect(opts.map(o => o.textContent)).toEqual(['5m', '15m', '30m', '1h', '2h', '4h', '24h'])
    fireEvent.click(opts[4])
    await waitFor(() =>
      expect(updateDashboardConfigMock).toHaveBeenCalledWith({ resume_retry_window_minutes: 120 })
    )
  })
})

describe('ChatPanel — Context', () => {
  it('PATCHes the auto-compact threshold as a number', async () => {
    wrap('advanced')
    await pickOption('Auto-Compact Threshold', 1)
    await waitFor(() =>
      expect(patchConfigMock).toHaveBeenCalledWith('session.autocompact_pct', 40)
    )
  })

  it('offers the shipped default as an option, labelled and bound', async () => {
    // The control renders a value from config against a fixed option list, so a
    // default with no matching option yields a select bound to nothing. Asserts
    // the full list rather than membership so the '(default)' marker cannot sit
    // on two options at once, or drift onto one that is no longer the default.
    wrap('advanced')
    const opts = await openSelect('Auto-Compact Threshold')
    expect(opts.map(o => o.textContent)).toEqual([
      '20%',
      '40%',
      '60%',
      '70% (Default)',
      '80%',
      '90%',
    ])
  })

  it('shows a stored 90 without calling it the default', async () => {
    // An install predating the default change keeps 90; this is not migrated,
    // so the control must display it and must not mark it as the default.
    wrap('advanced')
    const trigger = await screen.findByRole('combobox', { name: 'Auto-Compact Threshold' })
    await waitFor(() => expect(trigger).toHaveTextContent('90%'))
    expect(trigger).not.toHaveTextContent('default')
  })

  it('surfaces a failed auto-compact write', async () => {
    rejectOnce(patchConfigMock)
    wrap('advanced')
    await pickOption('Auto-Compact Threshold', 0)
    expect(await screen.findByText(/Failed to save auto-compact threshold/)).toBeInTheDocument()
  })
})

describe('ChatPanel — Compaction Wait Budget', () => {
  const field = () => screen.findByRole('spinbutton', { name: 'Compaction Time Limit (seconds)' })

  it('shows the stored value, including one off the presets', async () => {
    seedMc({ session: { compact_wait_secs: 450 } })
    wrap('advanced')
    const input = await field()
    await waitFor(() => expect(input).toHaveValue(450))
  })

  it('PATCHes a typed value on blur and shows no notice when it is kept', async () => {
    seedMc({ session: { compact_wait_secs: 0 } })
    patchConfigMock.mockImplementationOnce(() => Promise.resolve({}))
    wrap('advanced')
    const input = await field()
    await waitFor(() => expect(input).toHaveValue(0))
    fireEvent.change(input, { target: { value: '900' } })
    fireEvent.blur(input)
    await waitFor(() =>
      expect(patchConfigMock).toHaveBeenCalledWith('session.compact_wait_secs', 900)
    )
    expect(screen.queryByText(/outside the allowed range/)).not.toBeInTheDocument()
  })

  it('shows the value the server clamped to, with a notice', async () => {
    seedMc({ session: { compact_wait_secs: 0 } })
    patchConfigMock.mockImplementationOnce(() =>
      Promise.resolve({
        clamp_notice: { path: 'session.compact_wait_secs', requested: 20, stored: 60, min: 60, max: 3600 },
      }) as never
    )
    wrap('advanced')
    const input = await field()
    await waitFor(() => expect(input).toHaveValue(0))
    fireEvent.change(input, { target: { value: '20' } })
    fireEvent.blur(input)
    expect(await screen.findByRole('status')).toHaveTextContent(/outside the allowed range/)
    expect(input).toHaveValue(60)
  })

  it('says the unit and what 0 means without a hover', async () => {
    wrap('advanced')
    await field()
    expect(screen.getByText('Longest a compaction may run. 0 = default (300 s). Otherwise 60 to 3,600 s.')).toBeInTheDocument()
  })

  it('locks the field while a save is in flight, so its result cannot erase a newer draft', async () => {
    seedMc({ session: { compact_wait_secs: 0 } })
    let settle: (v: unknown) => void = () => {}
    patchConfigMock.mockImplementationOnce(() => new Promise(r => { settle = r }) as never)
    wrap('advanced')
    const input = await field()
    await waitFor(() => expect(input).toHaveValue(0))
    fireEvent.change(input, { target: { value: '20' } })
    fireEvent.blur(input)
    await waitFor(() => expect(input).toBeDisabled())
    settle({ clamp_notice: { path: 'session.compact_wait_secs', requested: 20, stored: 60, min: 60, max: 3600 } })
    await waitFor(() => expect(input).not.toBeDisabled())
    expect(input).toHaveValue(60)
  })

  it('never writes an untouched field back over a value saved elsewhere', async () => {
    seedMc({ session: { compact_wait_secs: 0 } })
    wrap('advanced')
    const input = await field()
    await waitFor(() => expect(input).toHaveValue(0))
    // Another client saves 900; this panel's config query refetches.
    seedMc({ session: { compact_wait_secs: 900 } })
    await pickOption('Auto-Compact Threshold', 1)
    await waitFor(() => expect(input).toHaveValue(900))
    patchConfigMock.mockClear()
    fireEvent.focus(input)
    fireEvent.blur(input)
    expect(patchConfigMock).not.toHaveBeenCalledWith('session.compact_wait_secs', expect.anything())
  })

  it('surfaces a failed write and restores the saved value', async () => {
    seedMc({ session: { compact_wait_secs: 300 } })
    rejectOnce(patchConfigMock)
    wrap('advanced')
    const input = await field()
    await waitFor(() => expect(input).toHaveValue(300))
    fireEvent.change(input, { target: { value: '600' } })
    fireEvent.blur(input)
    expect(await screen.findByText(/Failed to save compaction wait budget/)).toBeInTheDocument()
    await waitFor(() => expect(input).toHaveValue(300))
  })
})

describe('ChatPanel — Subagents', () => {
  it('PATCHes the completion-keep mode on selection', async () => {
    wrap('advanced')
    const opts = await openSelect('Completion Event Truncation')
    expect(opts.map(o => o.textContent)).toEqual([
      'Head (preserve start of stream)',
      'Tail (preserve end / final summary)',
      'Both (head + tail with truncation marker)',
    ])
    fireEvent.click(opts[1])
    await waitFor(() => expect(patchConfigMock).toHaveBeenCalledWith('agent.completion_keep', 'tail'))
  })

  it('surfaces a failed completion-keep-mode write', async () => {
    rejectOnce(patchConfigMock)
    wrap('advanced')
    await pickOption('Completion Event Truncation', 2)
    expect(await screen.findByText(/Failed to save completion-keep mode/)).toBeInTheDocument()
  })

  it('reverts the completion-keep characters when the write fails', async () => {
    rejectOnce(patchConfigMock)
    wrap('advanced')
    const input = await settledInput('Completion event characters')
    await waitFor(() => expect(input.value).toBe('3000'))
    fireEvent.change(input, { target: { value: '8000' } })
    fireEvent.blur(input)
    expect(await screen.findByText(/Failed to save completion-keep characters/)).toBeInTheDocument()
    await waitFor(() => expect(input.value).toBe('3000'))
  })
})

describe('ChatPanel — per-role models', () => {
  it.each([
    ['Background Model', 'agent.role_models.background'],
    ['Subagent Model', 'agent.role_models.subagent'],
  ])('%s PATCHes its own config path', async (label, path) => {
    wrap('models')
    await waitFor(() => expect(modelsMock).toHaveBeenCalled())
    await openSelect(label)
    fireEvent.click(screen.getByRole('option', { name: 'claude-opus-4.8' }))
    await waitFor(() => expect(patchConfigMock).toHaveBeenCalledWith(path, 'claude-opus-4.8'))
  })

  it.each([['Background Model'], ['Subagent Model']])(
    '%s surfaces a failed write',
    async label => {
      rejectOnce(patchConfigMock)
      wrap('models')
      await waitFor(() => expect(modelsMock).toHaveBeenCalled())
      await openSelect(label)
      fireEvent.click(screen.getByRole('option', { name: 'claude-haiku-4.5' }))
      expect(await screen.findByText(/Failed to save role model/)).toBeInTheDocument()
    }
  )

  it('labels the unset role model as the provider default', async () => {
    // Deliberately NOT the chat row's 'Default (auto)': a role on auto lets the
    // provider pick and never inherits agent.model (RoleModels.resolve_model),
    // so sharing that label claimed an inheritance that does not exist.
    wrap('models')
    const opts = await openSelect('Background Model')
    expect(opts.map(o => o.textContent)).toEqual([
      'Auto (provider picks)',
      'claude-opus-4.8',
      'claude-haiku-4.5',
    ])
  })

  it('keeps the chat row on its own "Default (auto)" spelling', async () => {
    // The two spellings are the whole point of the split — if the role label
    // ever leaks into the chat picker, the panel is back to implying that
    // per-role work inherits the global default.
    wrap('models')
    const opts = await openSelect('Default Model')
    expect(opts.map(o => o.textContent)).toContain('Default (auto)')
    expect(opts.map(o => o.textContent)).not.toContain('Auto (provider picks)')
  })

  it('keeps a pinned role model selectable when the backend stops listing it', async () => {
    // Dropping it would move the select to a foreign value, and the resulting
    // change event would overwrite the operator's pin.
    seedMc({ agent: { role_models: { background: 'claude-opus-4.7-retired' } } })
    wrap('models')
    await waitFor(() => expect(modelsMock).toHaveBeenCalled())
    const opts = await openSelect('Background Model')
    expect(opts.map(o => o.textContent)).toContain('claude-opus-4.7-retired')
    expect(patchConfigMock).not.toHaveBeenCalled()
  })
})

describe('ChatPanel — per-role reasoning effort', () => {
  it.each([
    ['Background Effort', 'background', 'agent.role_efforts.background'],
    ['Subagent Effort', 'subagent', 'agent.role_efforts.subagent'],
  ])('%s PATCHes its own config path once the role model can reason', async (label, role, path) => {
    seedMc({ agent: { role_models: { [role]: 'claude-opus-4.8' } } })
    wrap('models')
    await openSelect(label)
    fireEvent.click(screen.getByRole('option', { name: 'High' }))
    await waitFor(() => expect(patchConfigMock).toHaveBeenCalledWith(path, 'high'))
  })

  it.each([
    ['Background Effort', 'background'],
    ['Subagent Effort', 'subagent'],
  ])('%s surfaces a failed write', async (label, role) => {
    seedMc({ agent: { role_models: { [role]: 'claude-opus-4.8' } } })
    rejectOnce(patchConfigMock)
    wrap('models')
    await openSelect(label)
    fireEvent.click(screen.getByRole('option', { name: 'Max' }))
    expect(await screen.findByText(/Failed to save role effort/)).toBeInTheDocument()
  })

  it.each([['Background Effort'], ['Subagent Effort']])(
    '%s is inert while the role inherits a non-reasoning chat default',
    async label => {
      // Role model 'auto' resolves to the chat default, which is 'auto' here —
      // not reasoning-capable, so the row stays visible but cannot be opened.
      wrap('models')
      const trigger = await screen.findByRole('combobox', { name: label })
      await waitFor(() => expect(trigger).toHaveAttribute('data-disabled'))
      fireEvent.click(trigger)
      expect(screen.queryAllByRole('listbox').filter(l => !l.closest('nav'))).toHaveLength(0)
      expect(patchConfigMock).not.toHaveBeenCalled()
    }
  )

  it('enables the role effort row from the chat default when the role is on auto', async () => {
    // The gate reads the RESOLVED model: no pin, so the chat default decides.
    seedMc({ agent: { model: 'claude-opus-4.8' } })
    wrap('models')
    await openSelect('Background Effort')
    fireEvent.click(screen.getByRole('option', { name: 'Low' }))
    await waitFor(() =>
      expect(patchConfigMock).toHaveBeenCalledWith('agent.role_efforts.background', 'low')
    )
  })
})

describe('ChatPanel — About You and Power', () => {
  it('PATCHes the technical comfort level', async () => {
    wrap('aboutyou')
    await pickOption('Technical Comfort', 2)
    await waitFor(() =>
      expect(patchConfigMock).toHaveBeenCalledWith(
        'dashboard.user_technical_level',
        'somewhat-technical'
      )
    )
  })

  it('surfaces a failed profile write', async () => {
    rejectOnce(patchConfigMock)
    wrap('aboutyou')
    await pickOption('Technical Comfort', 1)
    expect(await screen.findByText(/Failed to save profile/)).toBeInTheDocument()
  })

  it('PATCHes the prevent-sleep flag', async () => {
    wrap('advanced')
    fireEvent.click(await settledSwitch('Prevent sleep while running'))
    await waitFor(() =>
      expect(patchConfigMock).toHaveBeenCalledWith('dashboard.prevent_sleep', true)
    )
  })

  it('reflects a stored prevent-sleep flag and turns it back off', async () => {
    seedMc({ dashboard: { prevent_sleep: true } })
    wrap('advanced')
    const sw = await settledSwitch('Prevent sleep while running')
    await waitFor(() => expect(sw).toHaveAttribute('aria-checked', 'true'))
    fireEvent.click(sw)
    await waitFor(() =>
      expect(patchConfigMock).toHaveBeenCalledWith('dashboard.prevent_sleep', false)
    )
  })

  it('surfaces a failed prevent-sleep write', async () => {
    rejectOnce(patchConfigMock)
    wrap('advanced')
    fireEvent.click(await settledSwitch('Prevent sleep while running'))
    expect(await screen.findByText(/Failed to save dashboard config/)).toBeInTheDocument()
  })
})

describe('ChatPanel — optimistic model selection (#6848)', () => {
  /** A patchConfig that stays pending until `release()` is called. */
  function pendingPatch() {
    let release!: () => void
    patchConfigMock.mockImplementationOnce(
      () => new Promise(res => { release = () => res({}) }) as never
    )
    return () => release()
  }

  it.each([
    ['Default Model', 'claude-opus-4.8'],
    ['Background Model', 'claude-opus-4.8'],
    ['Subagent Model', 'claude-opus-4.8'],
    ['Fallback model', 'claude-opus-4.8'],
  ])('%s shows the picked value immediately, before the PATCH settles', async (label, model) => {
    const release = pendingPatch()
    wrap('models')
    await waitFor(() => expect(modelsMock).toHaveBeenCalled())
    await openSelect(label)
    fireEvent.click(screen.getByRole('option', { name: model }))
    // The PATCH is still in flight — the trigger must already show the choice.
    const trigger = screen.getByRole('combobox', { name: label })
    await waitFor(() => expect(trigger).toHaveTextContent(model))
    expect(patchConfigMock).toHaveBeenCalledTimes(1)
    release()
  })

  it('shows a picked reasoning effort immediately, before the PATCH settles', async () => {
    seedMc({ agent: { model: 'claude-opus-4.8' } })
    const release = pendingPatch()
    wrap('models')
    await waitFor(() => expect(modelsMock).toHaveBeenCalled())
    await openSelect('Default Reasoning Effort')
    fireEvent.click(screen.getByRole('option', { name: 'High' }))
    const trigger = screen.getByRole('combobox', { name: 'Default Reasoning Effort' })
    await waitFor(() => expect(trigger).toHaveTextContent('High'))
    expect(patchConfigMock).toHaveBeenCalledTimes(1)
    release()
  })

  it('rolls the selector back to the server value when the PATCH fails', async () => {
    rejectOnce(patchConfigMock)
    wrap('models')
    await waitFor(() => expect(modelsMock).toHaveBeenCalled())
    await openSelect('Default Model')
    fireEvent.click(screen.getByRole('option', { name: 'claude-haiku-4.5' }))
    expect(await screen.findByText(/Failed to save default model/)).toBeInTheDocument()
    const trigger = screen.getByRole('combobox', { name: 'Default Model' })
    await waitFor(() => expect(trigger).toHaveTextContent('Default (auto)'))
    expect(trigger).not.toHaveTextContent('claude-haiku-4.5')
  })

  it('rolls a role model back when the PATCH fails', async () => {
    seedMc({ agent: { role_models: { background: 'claude-opus-4.8' } } })
    rejectOnce(patchConfigMock)
    wrap('models')
    await waitFor(() => expect(modelsMock).toHaveBeenCalled())
    await openSelect('Background Model')
    fireEvent.click(screen.getByRole('option', { name: 'claude-haiku-4.5' }))
    expect(await screen.findByText(/Failed to save role model/)).toBeInTheDocument()
    const trigger = screen.getByRole('combobox', { name: 'Background Model' })
    await waitFor(() => expect(trigger).toHaveTextContent('claude-opus-4.8'))
  })
})

describe('ChatPanel — page structure', () => {
  it.each([
    ['models', ['Background', 'Subagents', 'Rate-limit fallback', 'Content-filter fallback']],
    ['advanced', ['Power', 'Context', 'Subagents']],
  ])('%s exposes its group titles as headings', async (sub, titles) => {
    wrap(sub)
    for (const title of titles) {
      expect(await screen.findByRole('heading', { name: title })).toBeInTheDocument()
    }
  })

  // Each rail page is its own scroll, so its first card must rise at once and
  // the rest follow in page order. Ordinals carried over from the pre-rail
  // single scroll left a page blank for index*60ms after a rail click.
  it.each(['transcript', 'composer', 'sessions', 'sidepanel', 'models', 'aboutyou', 'advanced'])(
    '%s staggers its cards from zero in page order',
    async sub => {
      const { container } = wrap(sub)
      await waitFor(() =>
        expect(container.querySelectorAll('.animate-rise').length).toBeGreaterThan(0)
      )
      const delays = [...container.querySelectorAll<HTMLElement>('.animate-rise')].map(
        el => el.style.animationDelay
      )
      expect(delays).toEqual(delays.map((_, i) => (i === 0 ? '' : `${i * SETTINGS_CARD_STAGGER_MS}ms`)))
    }
  )
})
