import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import React from 'react'

const { patchConfigMock, tipsStatusMock, tipsFeedbackMock } = vi.hoisted(() => ({
  patchConfigMock: vi.fn(() => Promise.resolve({})),
  tipsStatusMock: vi.fn(() => Promise.resolve({ enabled_config: true, opted_out: false })),
  tipsFeedbackMock: vi.fn(() => Promise.resolve({ ok: true })),
}))

vi.mock('../api/client', () => ({
  api: {
    dashboardConfig: () => Promise.resolve({ restore_sessions: false, restore_window_minutes: 30, merge_queued_messages: false, widget_density: 'more' }),
    voiceConfig: () => Promise.resolve({ enabled: false, voice: 'Ruth', engine: 'neural', rate: '100%', autoSpeak: false, aws_profile: '', region: '' }),
    sttConfig: () => Promise.resolve({ enabled: false, provider: '', model: '', available: false, streaming: false, transcribe_region: '', transcribe_profile: '', language_code: 'en-US', models: {}, language_codes: [] }),
    kirocrewConfig: () => Promise.resolve({ agent: { completion_keep: 'head', completion_keep_chars: 3000 } }),
    patchConfig: patchConfigMock,
    updateDashboardConfig: () => Promise.resolve({}),
    updateVoiceConfig: () => Promise.resolve({}),
    updateSttConfig: () => Promise.resolve({}),
    tipsStatus: tipsStatusMock,
    tipsFeedback: tipsFeedbackMock,
  },
}))

import { ChatPanel } from '../pages/settings/ChatPanel'

function wrap(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>)
}

describe('ChatPanel settings – Feature Tips toggle', () => {
  beforeEach(() => {
    tipsStatusMock.mockClear()
    tipsFeedbackMock.mockClear()
    tipsStatusMock.mockImplementation(() => Promise.resolve({ enabled_config: true, opted_out: false }))
  })

  it('renders the toggle reflecting server state (enabled, not opted out)', async () => {
    wrap(<ChatPanel />)
    expect(await screen.findByText('Feature Tips')).toBeInTheDocument()
    await waitFor(() => expect(tipsStatusMock).toHaveBeenCalled())
  })

  it('fires optout when toggled off', async () => {
    wrap(<ChatPanel />)
    const label = await screen.findByText('Feature Tips')
    await waitFor(() => expect(tipsStatusMock).toHaveBeenCalled())
    fireEvent.click(label)
    await waitFor(() => expect(tipsFeedbackMock).toHaveBeenCalledWith('', 'optout'))
  })

  it('fires optin when toggled back on from opted-out state', async () => {
    tipsStatusMock.mockImplementation(() => Promise.resolve({ enabled_config: true, opted_out: true }))
    wrap(<ChatPanel />)
    const label = await screen.findByText('Feature Tips')
    await waitFor(() => expect(tipsStatusMock).toHaveBeenCalled())
    fireEvent.click(label)
    await waitFor(() => expect(tipsFeedbackMock).toHaveBeenCalledWith('', 'optin'))
  })

  it('renders disabled with config hint when tips_enabled=false at config level', async () => {
    tipsStatusMock.mockImplementation(() => Promise.resolve({ enabled_config: false, opted_out: false }))
    wrap(<ChatPanel />)
    expect(await screen.findByText(/Disabled by instance config/)).toBeInTheDocument()
    fireEvent.click(screen.getByText('Feature Tips'))
    // Disabled toggle must not fire feedback
    expect(tipsFeedbackMock).not.toHaveBeenCalled()
  })

  it('toggling the preference drops any cached tips-next query (Codex round-6)', async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    // Simulate a tip cached by a running Chat view before the user opts out
    qc.setQueryData(['tips-next'], { tip: { id: 'stale', title: 'Stale' }, glow: true })
    render(<QueryClientProvider client={qc}><ChatPanel /></QueryClientProvider>)
    const label = await screen.findByText('Feature Tips')
    await waitFor(() => expect(tipsStatusMock).toHaveBeenCalled())
    fireEvent.click(label)
    await waitFor(() => expect(tipsFeedbackMock).toHaveBeenCalledWith('', 'optout'))
    // onSettled must remove the cached tip so Chat can't display it
    await waitFor(() => expect(qc.getQueryData(['tips-next'])).toBeUndefined())
  })
})

describe('ChatPanel settings – Subagents section', () => {
  beforeEach(() => {
    patchConfigMock.mockClear()
  })

  it('renders the Subagents section with both completion_keep fields', () => {
    wrap(<ChatPanel />)
    expect(screen.getByText('Subagents')).toBeInTheDocument()
    expect(screen.getByText('Completion Event Truncation')).toBeInTheDocument()
    expect(screen.getByText('Completion Event Characters')).toBeInTheDocument()
  })

  it('seeds the completion-keep-chars input from the server config', async () => {
    wrap(<ChatPanel />)
    const input = await screen.findByLabelText('Completion event characters') as HTMLInputElement
    await waitFor(() => expect(input.value).toBe('3000'))
  })

  it('PATCHes agent.completion_keep_chars on blur with a valid integer', async () => {
    wrap(<ChatPanel />)
    const input = await screen.findByLabelText('Completion event characters') as HTMLInputElement
    await waitFor(() => expect(input.value).toBe('3000'))
    fireEvent.change(input, { target: { value: '5000' } })
    fireEvent.blur(input)
    await waitFor(() =>
      expect(patchConfigMock).toHaveBeenCalledWith('agent.completion_keep_chars', 5000)
    )
  })

  it('reverts and does NOT PATCH when the value is out of range', async () => {
    wrap(<ChatPanel />)
    const input = await screen.findByLabelText('Completion event characters') as HTMLInputElement
    await waitFor(() => expect(input.value).toBe('3000'))
    fireEvent.change(input, { target: { value: '999999999' } })
    fireEvent.blur(input)
    // Reverted to the last server value, no PATCH dispatched.
    expect(patchConfigMock).not.toHaveBeenCalled()
    expect(input.value).toBe('3000')
  })
})
