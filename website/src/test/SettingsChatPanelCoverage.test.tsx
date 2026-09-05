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
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import React from 'react'

const BASE_DASH = {
  restore_sessions: false,
  restore_window_minutes: 30,
  merge_queued_messages: false,
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
    patchConfig: patchConfigMock,
    models: modelsMock,
    voiceConfig: () => Promise.resolve({ enabled: false, voice: 'Ruth', engine: 'neural', rate: '100%', autoSpeak: false, aws_profile: '', region: '' }),
    sttConfig: () => Promise.resolve({ enabled: false, provider: '', model: '', available: false, streaming: false, transcribe_region: '', transcribe_profile: '', language_code: 'en-US', models: {}, language_codes: [] }),
    updateVoiceConfig: () => Promise.resolve({}),
    updateSttConfig: () => Promise.resolve({}),
    tipsStatus: tipsStatusMock,
    tipsFeedback: tipsFeedbackMock,
  },
}))

import { ChatPanel } from '../pages/settings/ChatPanel'

const LS_KEY = 'mc-chat-config'

function wrap() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <ChatPanel />
    </QueryClientProvider>
  )
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
  return within(screen.getByRole('listbox')).getAllByRole('option')
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
})

describe('ChatPanel — save-error banner', () => {
  it('rolls the optimistic write back and explains the failure', async () => {
    rejectOnce(updateDashboardConfigMock)
    wrap()
    const sw = await settledSwitch('Quick Send')
    fireEvent.click(sw)
    expect(await screen.findByText(/Failed to save dashboard config/)).toBeInTheDocument()
    // onError restores the pre-mutation cache entry, so the switch goes back off.
    await waitFor(() => expect(sw).toHaveAttribute('aria-checked', 'false'))
  })

  it('clears the banner when dismissed', async () => {
    rejectOnce(updateDashboardConfigMock)
    wrap()
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
    wrap()
    await pickOption('Send shortcut', 1)
    await waitFor(() => expect(storedChat().sendOnEnter).toBe('ctrl-enter'))
  })

  it('stores the follow-up bar layout from the button group', async () => {
    wrap()
    const group = await screen.findByRole('group', { name: 'Follow-Up Bar Layout' })
    fireEvent.click(within(group).getByRole('button', { name: 'Multiline' }))
    await waitFor(() => expect(storedChat().followUpLayout).toBe('multiline'))
  })

  it('persists Quick Send through the dashboard config, sending only that key', async () => {
    wrap()
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
    wrap()
    fireEvent.click(await settledSwitch('Merge Queued Messages'))
    await waitFor(() =>
      expect(updateDashboardConfigMock).toHaveBeenCalledWith({
        merge_queued_messages: true,
      })
    )
  })

  it('PATCHes the soft-stop budget on blur with an in-range value', async () => {
    wrap()
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
    wrap()
    const input = await settledInput('Soft-stop budget (seconds)')
    await waitFor(() => expect(input.value).toBe('10'))
    fireEvent.change(input, { target: { value: typed } })
    fireEvent.blur(input)
    expect(patchConfigMock).not.toHaveBeenCalled()
    expect(input.value).toBe('10')
  })

  it('reverts the soft-stop budget to the server value when the write fails', async () => {
    rejectOnce(patchConfigMock)
    wrap()
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

  it.each([
    ['Show Timestamps', 'showTimestamps', false],
    ['Pin the latest turn', 'pinLastPrompt', false],
    ['Simplified Tool Call Names', 'simplifiedToolNames', false],
    ['Show Context Percentage', 'showContextPct', true],
  ])('stores %s locally when flipped', async (label, key, expected) => {
    wrap()
    fireEvent.click(await screen.findByRole('switch', { name: label }))
    await waitFor(() => expect(storedChat()[key]).toBe(expected))
  })

  it('clears a stored minimized flag when the pin toggle is switched on', async () => {
    localStorage.setItem(LS_KEY, JSON.stringify({ pinLastPrompt: false, pinPromptMinimized: true }))
    wrap()
    // The row promises a sticky banner, so a flag stored in an earlier session
    // must not survive the flip and hand back the chip instead.
    fireEvent.click(await screen.findByRole('switch', { name: 'Pin the latest turn' }))
    await waitFor(() => expect(storedChat().pinLastPrompt).toBe(true))
    expect(storedChat().pinPromptMinimized).toBe(false)
  })

  it('leaves the minimized flag intact when the pin toggle is switched off', async () => {
    localStorage.setItem(LS_KEY, JSON.stringify({ pinLastPrompt: true, pinPromptMinimized: true }))
    wrap()
    // Turning the banner off must not clear the preference, or an unconditional
    // reset would pass the test above while discarding the choice either way.
    fireEvent.click(await screen.findByRole('switch', { name: 'Pin the latest turn' }))
    await waitFor(() => expect(storedChat().pinLastPrompt).toBe(false))
    expect(storedChat().pinPromptMinimized).toBe(true)
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
    wrap()
    fireEvent.click(await settledSwitch('MCP Apps in Side Panel'))
    await waitFor(() =>
      expect(updateDashboardConfigMock).toHaveBeenCalledWith({ mcp_app_panel: true })
    )
  })

  it('persists the folder-suggestions toggle', async () => {
    wrap()
    fireEvent.click(await settledSwitch('Folder suggestions'))
    await waitFor(() =>
      expect(updateDashboardConfigMock).toHaveBeenCalledWith({
        folder_suggestions_enabled: false,
      })
    )
  })

  it('rolls the Feature Tips preference back when the write fails', async () => {
    rejectOnce(tipsFeedbackMock)
    wrap()
    const sw = await settledSwitch('Feature Tips')
    await waitFor(() => expect(sw).toHaveAttribute('aria-checked', 'true'))
    fireEvent.click(sw)
    expect(await screen.findByText(/Failed to save tips preference/)).toBeInTheDocument()
    await waitFor(() => expect(sw).toHaveAttribute('aria-checked', 'true'))
  })
})

describe('ChatPanel — Sessions', () => {
  it.each([
    ['Split View (Session Grid)', 'session_grid', true],
    ['Tail-only Fork', 'tail_fork_enabled', true],
    ['Restore Sessions', 'restore_sessions', true],
  ])('persists %s through the dashboard config', async (label, key, expected) => {
    wrap()
    fireEvent.click(await settledSwitch(label))
    await waitFor(() =>
      expect(updateDashboardConfigMock).toHaveBeenCalledWith({ [key]: expected })
    )
  })

  it.each([
    ['History Expanded', 'historyExpanded', false],
    ['Confirm Before Closing Session', 'confirmCloseSession', true],
    ['Default to Autopilot Mode', 'defaultAutopilot', true],
  ])('stores %s locally when flipped', async (label, key, expected) => {
    wrap()
    fireEvent.click(await screen.findByRole('switch', { name: label }))
    await waitFor(() => expect(storedChat()[key]).toBe(expected))
  })

  it('hides the restore window until session restore is on', async () => {
    wrap()
    await settledSwitch('Restore Sessions')
    expect(screen.queryByRole('combobox', { name: 'Restore Window' })).not.toBeInTheDocument()
  })

  it('offers a no-limit window and persists the picked one', async () => {
    dashboardConfigMock.mockImplementation(
      () => Promise.resolve({ ...BASE_DASH, restore_sessions: true }) as never
    )
    wrap()
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
})

describe('ChatPanel — Context', () => {
  it('PATCHes the auto-compact threshold as a number', async () => {
    wrap()
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
    wrap()
    const opts = await openSelect('Auto-Compact Threshold')
    expect(opts.map(o => o.textContent)).toEqual([
      '20% (aggressive)',
      '40%',
      '60%',
      '70% (default)',
      '80%',
      '90%',
    ])
  })

  it('shows a stored 90 without calling it the default', async () => {
    // An install predating the default change keeps 90; this is not migrated,
    // so the control must display it and must not mark it as the default.
    wrap()
    const trigger = await screen.findByRole('combobox', { name: 'Auto-Compact Threshold' })
    await waitFor(() => expect(trigger).toHaveTextContent('90%'))
    expect(trigger).not.toHaveTextContent('default')
  })

  it('surfaces a failed auto-compact write', async () => {
    rejectOnce(patchConfigMock)
    wrap()
    await pickOption('Auto-Compact Threshold', 0)
    expect(await screen.findByText(/Failed to save auto-compact threshold/)).toBeInTheDocument()
  })
})

describe('ChatPanel — Subagents', () => {
  it('PATCHes the completion-keep mode on selection', async () => {
    wrap()
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
    wrap()
    await pickOption('Completion Event Truncation', 2)
    expect(await screen.findByText(/Failed to save completion-keep mode/)).toBeInTheDocument()
  })

  it('reverts the completion-keep characters when the write fails', async () => {
    rejectOnce(patchConfigMock)
    wrap()
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
    wrap()
    await waitFor(() => expect(modelsMock).toHaveBeenCalled())
    await openSelect(label)
    fireEvent.click(screen.getByRole('option', { name: 'claude-opus-4.8' }))
    await waitFor(() => expect(patchConfigMock).toHaveBeenCalledWith(path, 'claude-opus-4.8'))
  })

  it.each([['Background Model'], ['Subagent Model']])(
    '%s surfaces a failed write',
    async label => {
      rejectOnce(patchConfigMock)
      wrap()
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
    wrap()
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
    wrap()
    const opts = await openSelect('Default Model')
    expect(opts.map(o => o.textContent)).toContain('Default (auto)')
    expect(opts.map(o => o.textContent)).not.toContain('Auto (provider picks)')
  })

  it('keeps a pinned role model selectable when the backend stops listing it', async () => {
    // Dropping it would move the select to a foreign value, and the resulting
    // change event would overwrite the operator's pin.
    seedMc({ agent: { role_models: { background: 'claude-opus-4.7-retired' } } })
    wrap()
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
    wrap()
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
    wrap()
    await openSelect(label)
    fireEvent.click(screen.getByRole('option', { name: 'Max' }))
    expect(await screen.findByText(/Failed to save role effort/)).toBeInTheDocument()
  })

  it.each([['Background Effort'], ['Subagent Effort']])(
    '%s is inert while the role inherits a non-reasoning chat default',
    async label => {
      // Role model 'auto' resolves to the chat default, which is 'auto' here —
      // not reasoning-capable, so the row stays visible but cannot be opened.
      wrap()
      const trigger = await screen.findByRole('combobox', { name: label })
      await waitFor(() => expect(trigger).toHaveAttribute('data-disabled'))
      fireEvent.click(trigger)
      expect(screen.queryByRole('listbox')).not.toBeInTheDocument()
      expect(patchConfigMock).not.toHaveBeenCalled()
    }
  )

  it('enables the role effort row from the chat default when the role is on auto', async () => {
    // The gate reads the RESOLVED model: no pin, so the chat default decides.
    seedMc({ agent: { model: 'claude-opus-4.8' } })
    wrap()
    await openSelect('Background Effort')
    fireEvent.click(screen.getByRole('option', { name: 'Low' }))
    await waitFor(() =>
      expect(patchConfigMock).toHaveBeenCalledWith('agent.role_efforts.background', 'low')
    )
  })
})

describe('ChatPanel — About You and Power', () => {
  it('PATCHes the technical comfort level', async () => {
    wrap()
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
    wrap()
    await pickOption('Technical Comfort', 1)
    expect(await screen.findByText(/Failed to save profile/)).toBeInTheDocument()
  })

  it('PATCHes the prevent-sleep flag', async () => {
    wrap()
    fireEvent.click(await settledSwitch('Prevent sleep while running'))
    await waitFor(() =>
      expect(patchConfigMock).toHaveBeenCalledWith('dashboard.prevent_sleep', true)
    )
  })

  it('reflects a stored prevent-sleep flag and turns it back off', async () => {
    seedMc({ dashboard: { prevent_sleep: true } })
    wrap()
    const sw = await settledSwitch('Prevent sleep while running')
    await waitFor(() => expect(sw).toHaveAttribute('aria-checked', 'true'))
    fireEvent.click(sw)
    await waitFor(() =>
      expect(patchConfigMock).toHaveBeenCalledWith('dashboard.prevent_sleep', false)
    )
  })

  it('surfaces a failed prevent-sleep write', async () => {
    rejectOnce(patchConfigMock)
    wrap()
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
    wrap()
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
    wrap()
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
    wrap()
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
    wrap()
    await waitFor(() => expect(modelsMock).toHaveBeenCalled())
    await openSelect('Background Model')
    fireEvent.click(screen.getByRole('option', { name: 'claude-haiku-4.5' }))
    expect(await screen.findByText(/Failed to save role model/)).toBeInTheDocument()
    const trigger = screen.getByRole('combobox', { name: 'Background Model' })
    await waitFor(() => expect(trigger).toHaveTextContent('claude-opus-4.8'))
  })
})
