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

const { dashboardConfigMock, updateDashboardConfigMock } = vi.hoisted(() => ({
  dashboardConfigMock: vi.fn(),
  updateDashboardConfigMock: vi.fn(() => Promise.resolve({})),
}))

vi.mock('../api/client', () => ({
  api: {
    dashboardConfig: dashboardConfigMock,
    voiceConfig: () => Promise.resolve({ enabled: false, voice: 'Ruth', engine: 'neural', rate: '100%', autoSpeak: false, aws_profile: '', region: '' }),
    sttConfig: () => Promise.resolve({ enabled: false, provider: '', model: '', available: false, streaming: false, transcribe_region: '', transcribe_profile: '', language_code: 'en-US', models: {}, language_codes: [] }),
    kirocrewConfig: () => Promise.resolve({ agent: { completion_keep: 'head', completion_keep_chars: 3000, model: 'auto', reasoning_effort: '' } }),
    models: () => Promise.resolve([{ model_name: 'auto', description: 'Default' }]),
    patchConfig: () => Promise.resolve({}),
    updateDashboardConfig: updateDashboardConfigMock,
    updateVoiceConfig: () => Promise.resolve({}),
    updateSttConfig: () => Promise.resolve({}),
    tipsStatus: () => Promise.resolve({ enabled_config: true, opted_out: false }),
    tipsFeedback: () => Promise.resolve({ ok: true }),
  },
}))

import { ChatPanel } from '../pages/settings/ChatPanel'

function wrap(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>)
}

describe('ChatPanel settings – Link Previews toggle', () => {
  beforeEach(() => {
    dashboardConfigMock.mockReset()
    updateDashboardConfigMock.mockClear()
    dashboardConfigMock.mockResolvedValue({ ...BASE_DASH })
  })

  it('renders the toggle in the Messages section, off by default', async () => {
    wrap(<ChatPanel />)
    expect(await screen.findByText('Link Previews')).toBeInTheDocument()
    const toggle = await screen.findByRole('switch', { name: 'Link Previews' })
    await waitFor(() => expect(toggle).toHaveAttribute('aria-checked', 'false'))
  })

  it('states the outbound-fetch tradeoff in the description', async () => {
    wrap(<ChatPanel />)
    expect(
      await screen.findByText(/fetches every link the model outputs.*request from your IP address/),
    ).toBeInTheDocument()
  })

  it('reflects link_previews: true from the server', async () => {
    dashboardConfigMock.mockResolvedValue({ ...BASE_DASH, link_previews: true })
    wrap(<ChatPanel />)
    const toggle = await screen.findByRole('switch', { name: 'Link Previews' })
    await waitFor(() => expect(toggle).toHaveAttribute('aria-checked', 'true'))
  })

  it('persists the change through updateDashboardConfig, preserving sibling fields', async () => {
    wrap(<ChatPanel />)
    const toggle = await screen.findByRole('switch', { name: 'Link Previews' })
    await waitFor(() => expect(toggle).toHaveAttribute('aria-checked', 'false'))
    fireEvent.click(toggle)
    await waitFor(() =>
      expect(updateDashboardConfigMock).toHaveBeenCalledWith({ ...BASE_DASH, link_previews: true }),
    )
  })

  it('turns the setting back off', async () => {
    dashboardConfigMock.mockResolvedValue({ ...BASE_DASH, link_previews: true })
    wrap(<ChatPanel />)
    const toggle = await screen.findByRole('switch', { name: 'Link Previews' })
    await waitFor(() => expect(toggle).toHaveAttribute('aria-checked', 'true'))
    fireEvent.click(toggle)
    await waitFor(() =>
      expect(updateDashboardConfigMock).toHaveBeenCalledWith({ ...BASE_DASH, link_previews: false }),
    )
  })

  it('is inert until the dashboard config has loaded', async () => {
    dashboardConfigMock.mockReturnValue(new Promise(() => {}))
    wrap(<ChatPanel />)
    const toggle = await screen.findByRole('switch', { name: 'Link Previews' })
    // Toggle is a role=switch div, so "disabled" is expressed as tabIndex -1 +
    // an ignored click, not a form-control disabled attribute.
    expect(toggle).toHaveAttribute('tabindex', '-1')
    fireEvent.click(toggle)
    expect(updateDashboardConfigMock).not.toHaveBeenCalled()
  })
})
