import { MemoryRouter } from 'react-router-dom'
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

const LABEL = 'Preferred Reply Language'

const { dashboardConfigMock, selectProps } = vi.hoisted(() => ({
  dashboardConfigMock: vi.fn(),
  selectProps: [] as { label: string; value: unknown; options?: readonly string[]; optionLabels?: readonly string[] }[],
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
    featureVideoStatus: () => Promise.resolve({
      enabled: true, download_enabled: false, release: 'r1',
      cached: 0, total: 0, downloading: null,
    }),
    featureVideoFetchAll: () => Promise.resolve({ ok: true }),
  },
}))

/** Capture the exact `value` and options ChatPanel hands the real Select: Radix
 *  degrades an out-of-domain value silently, so the DOM alone cannot show it. */
vi.mock('../components/settings', async importOriginal => {
  const actual = await importOriginal<typeof import('../components/settings')>()
  return {
    ...actual,
    SettingsSelect: (props: Parameters<typeof actual.SettingsSelect>[0]) => {
      selectProps.push({ label: props.label, value: props.value, options: props.options, optionLabels: props.optionLabels })
      return actual.SettingsSelect(props)
    },
  }
})

import { ChatPanel } from '../pages/settings/ChatPanel'
import { Provider } from 'react-redux'
import { createTestStore } from './helpers'

function wrap(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<MemoryRouter initialEntries={['/settings?tab=chat&sub=transcript']}><Provider store={createTestStore()}><QueryClientProvider client={qc}>{ui}</QueryClientProvider></Provider></MemoryRouter>)
}

async function rowAfterLoad(persisted: unknown) {
  dashboardConfigMock.mockResolvedValue({ ...BASE_DASH, preferred_language: persisted })
  wrap(<ChatPanel />)
  await screen.findByText(LABEL)
  await waitFor(() => expect(selectProps.some(p => p.label === LABEL)).toBe(true))
  const seen = selectProps.filter(p => p.label === LABEL)
  return seen[seen.length - 1]
}

describe('ChatPanel settings – Preferred Reply Language', () => {
  beforeEach(() => {
    dashboardConfigMock.mockReset()
    selectProps.length = 0
  })

  // The backend accepts exactly '' plus these codes (config.sections.PREFERRED_LANGUAGES).
  it('offers auto first, then every shipped UI language by its own name', async () => {
    const row = await rowAfterLoad('')
    expect(row.options).toEqual(['', 'en', 'zh-CN', 'hi', 'es', 'fr', 'bn', 'pt', 'ru', 'de', 'ja', 'ko', 'it'])
    expect(row.optionLabels?.[0]).toBe('Auto (match my message)')
    expect(row.optionLabels?.[2]).toBe('简体中文')
    expect(row.optionLabels).toHaveLength(row.options!.length)
  })

  it('passes a stored code through', async () => {
    expect((await rowAfterLoad('zh-CN')).value).toBe('zh-CN')
  })

  it.each([
    ['the pseudolocale', 'en-XA'],
    ['an unknown string', 'klingon'],
    ['an object', {}],
    ['a number', 7],
    ['null', null],
    ['undefined', undefined],
  ])('narrows %s to auto', async (_label, persisted) => {
    expect((await rowAfterLoad(persisted)).value).toBe('')
  })
})
