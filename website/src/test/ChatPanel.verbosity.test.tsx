import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
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
}

const { dashboardConfigMock, selectProps } = vi.hoisted(() => ({
  dashboardConfigMock: vi.fn(),
  selectProps: [] as { label: string; value: unknown }[],
}))

vi.mock('../api/client', () => ({
  api: {
    dashboardConfig: dashboardConfigMock,
    voiceConfig: () => Promise.resolve({ enabled: false, voice: 'Ruth', engine: 'neural', rate: '100%', autoSpeak: false, aws_profile: '', region: '' }),
    sttConfig: () => Promise.resolve({ enabled: false, provider: '', model: '', available: false, streaming: false, transcribe_region: '', transcribe_profile: '', language_code: 'en-US', models: {}, language_codes: [] }),
    kirocrewConfig: () => Promise.resolve({ agent: { completion_keep: 'head', completion_keep_chars: 3000, model: 'auto', reasoning_effort: '' } }),
    models: () => Promise.resolve([{ model_name: 'auto', description: 'Default' }]),
    patchConfig: () => Promise.resolve({}),
    updateDashboardConfig: () => Promise.resolve({}),
    updateVoiceConfig: () => Promise.resolve({}),
    updateSttConfig: () => Promise.resolve({}),
    tipsStatus: () => Promise.resolve({ enabled_config: true, opted_out: false }),
    tipsFeedback: () => Promise.resolve({ ok: true }),
  },
}))

/**
 * Wrap the real `SettingsSelect` so the test can see the exact `value` ChatPanel
 * hands it. Asserting on the rendered trigger text is NOT sufficient: Radix
 * silently degrades an out-of-domain value to the first option, so a malformed
 * value looks identical to `default` in the DOM and the guard's absence would go
 * undetected. Capturing the prop is what makes this a real regression test.
 */
vi.mock('../components/settings', async importOriginal => {
  const actual = await importOriginal<typeof import('../components/settings')>()
  return {
    ...actual,
    SettingsSelect: (props: Parameters<typeof actual.SettingsSelect>[0]) => {
      selectProps.push({ label: props.label, value: props.value })
      return actual.SettingsSelect(props)
    },
  }
})

import { ChatPanel } from '../pages/settings/ChatPanel'

function wrap(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>)
}

async function verbosityValueAfterLoad(persisted: unknown): Promise<unknown> {
  dashboardConfigMock.mockResolvedValue({ ...BASE_DASH, verbosity: persisted })
  wrap(<ChatPanel />)
  await screen.findByText('Response Verbosity')
  await waitFor(() => expect(selectProps.some(p => p.label === 'Response Verbosity')).toBe(true))
  const seen = selectProps.filter(p => p.label === 'Response Verbosity')
  return seen[seen.length - 1].value
}

/**
 * `dashboard.verbosity` is read from config.json with a plain `.get()` and never
 * type-checked, so `{"dashboard": {"verbosity": {}}}` reaches the UI as an object.
 * `?? 'default'` guards only null/undefined, so the object would be handed to a
 * prop typed `value: string` and on into Radix — relying on undefined behaviour
 * for an out-of-domain value. `asVerbosity` narrows it to a known level instead.
 */
describe('ChatPanel settings – Response Verbosity is narrowed before render', () => {
  beforeEach(() => {
    dashboardConfigMock.mockReset()
    selectProps.length = 0
  })

  it.each([
    ['default', 'default'],
    ['concise', 'concise'],
    ['ultra', 'ultra'],
  ])('passes through the known level %s', async (persisted, expected) => {
    expect(await verbosityValueAfterLoad(persisted)).toBe(expected)
  })

  // `an object` is the exact shape from the report: {"dashboard":{"verbosity":{}}}.
  it.each([
    ['an object', {}],
    ['a populated object', { level: 'ultra' }],
    ['an array', ['ultra']],
    ['a number', 7],
    ['a bool', true],
    ['an unknown string', 'chatty'],
    ['an empty string', ''],
    ['null', null],
    ['undefined', undefined],
  ])('narrows %s to "default"', async (_label, persisted) => {
    expect(await verbosityValueAfterLoad(persisted)).toBe('default')
  })

  it('keeps the settings page mounted on a malformed value', async () => {
    dashboardConfigMock.mockResolvedValue({ ...BASE_DASH, verbosity: {} })
    wrap(<ChatPanel />)
    expect(await screen.findByText('Response Verbosity')).toBeInTheDocument()
    const trigger = await screen.findByRole('combobox', { name: 'Response Verbosity' })
    expect(trigger).not.toHaveTextContent('[object Object]')
  })
})
