import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
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

const { updateDashboardConfigMock, patchConfigMock } = vi.hoisted(() => ({
  updateDashboardConfigMock: vi.fn(() => Promise.resolve({})),
  patchConfigMock: vi.fn(() => Promise.resolve({})),
}))

vi.mock('../api/client', () => ({
  api: {
    dashboardConfig: () => Promise.resolve({ ...BASE_DASH }),
    voiceConfig: () => Promise.resolve({ enabled: false, voice: 'Ruth', engine: 'neural', rate: '100%', autoSpeak: false, aws_profile: '', region: '' }),
    sttConfig: () => Promise.resolve({ enabled: false, provider: '', model: '', available: false, streaming: false, transcribe_region: '', transcribe_profile: '', language_code: 'en-US', models: {}, language_codes: [] }),
    kirocrewConfig: () => Promise.resolve({ agent: { completion_keep: 'head', completion_keep_chars: 3000, model: 'auto', reasoning_effort: '' } }),
    models: () => Promise.resolve([{ model_name: 'auto', description: 'Default' }]),
    patchConfig: patchConfigMock,
    updateDashboardConfig: updateDashboardConfigMock,
    updateVoiceConfig: () => Promise.resolve({}),
    updateSttConfig: () => Promise.resolve({}),
    tipsStatus: () => Promise.resolve({ enabled_config: true, opted_out: false }),
    tipsFeedback: () => Promise.resolve({ ok: true }),
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

import { Provider } from 'react-redux'

// ChatPanel reads the active slot from redux to name the session on its
// feature-video calls, so these renders need a store. A FRESH one per file,
// not the app singleton: a shared store would carry `activeSlot` across suites.
import { createTestStore } from './helpers'

function wrap(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<Provider store={createTestStore()}><QueryClientProvider client={qc}>{ui}</QueryClientProvider></Provider>)
}

describe('ChatPanel settings – Plain diffs toggle', () => {
  beforeEach(() => {
    updateDashboardConfigMock.mockClear()
    patchConfigMock.mockClear()
    localStorage.clear()
  })

  it('renders in the Messages section, off by default', async () => {
    wrap(<ChatPanel />)
    // Sits beside File change chips, the control it shares a surface with.
    expect(await screen.findByText('File Change Chips')).toBeInTheDocument()
    const toggle = await screen.findByRole('switch', { name: 'Plain diffs' })
    // Highlighted diffs are what a new install shows, so the switch starts off.
    expect(toggle).toHaveAttribute('aria-checked', 'false')
  })

  it('persists the choice to the key the diff surfaces read', async () => {
    wrap(<ChatPanel />)
    const toggle = await screen.findByRole('switch', { name: 'Plain diffs' })
    fireEvent.click(toggle)
    // The literal key matters: PierrePatch and DiffBlock read `mc-diff-plain`
    // through usePlainDiff, and nothing on the server mediates between them.
    await waitFor(() => expect(localStorage.getItem('mc-diff-plain')).toBe('1'))
    expect(toggle).toHaveAttribute('aria-checked', 'true')
  })

  it('seeds from the stored preference', async () => {
    localStorage.setItem('mc-diff-plain', '1')
    wrap(<ChatPanel />)
    expect(await screen.findByRole('switch', { name: 'Plain diffs' })).toHaveAttribute('aria-checked', 'true')
  })

  it('writes nothing to the server, unlike its neighbours in this section', async () => {
    // This is the ONLY browser-local row in Messages: the machine painting the
    // diff is the one spending the CPU, so the choice must not travel to the
    // instance config the way Link Previews or Widget Density do. A future
    // "make it a real setting" refactor would break this and nothing else.
    wrap(<ChatPanel />)
    const toggle = await screen.findByRole('switch', { name: 'Plain diffs' })
    fireEvent.click(toggle)
    await waitFor(() => expect(localStorage.getItem('mc-diff-plain')).toBe('1'))
    expect(updateDashboardConfigMock).not.toHaveBeenCalled()
    expect(patchConfigMock).not.toHaveBeenCalled()
  })
})
