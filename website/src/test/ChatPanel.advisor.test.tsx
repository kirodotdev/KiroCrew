import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import React from 'react'

const { patchConfigMock, kirocrewConfigMock } = vi.hoisted(() => ({
  patchConfigMock: vi.fn(() => Promise.resolve({})),
  kirocrewConfigMock: vi.fn(() => Promise.resolve({
    agent: { model: 'auto', reasoning_effort: '' },
    advisor: {
      enabled: false,
    },
  })),
}))

vi.mock('../api/client', () => ({
  api: {
    dashboardConfig: () => Promise.resolve({
      restore_sessions: false,
      restore_window_minutes: 30,
      merge_queued_messages: false,
      widget_density: 'more',
    }),
    kirocrewConfig: kirocrewConfigMock,
    models: () => Promise.resolve([{ model_name: 'auto', description: 'Default' }]),
    patchConfig: patchConfigMock,
    updateDashboardConfig: () => Promise.resolve({}),
    tipsStatus: () => Promise.resolve({ enabled_config: true, opted_out: false }),
    tipsFeedback: () => Promise.resolve({ ok: true }),
  },
}))

import { ChatPanel } from '../pages/settings/ChatPanel'

import { Provider } from 'react-redux'

// ChatPanel reads the active slot from redux for its feature-video calls, so
// this render needs a store -- a FRESH one per file, like the sibling suites.
import { createTestStore } from './helpers'

function renderPanel() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <Provider store={createTestStore()}>
      <QueryClientProvider client={qc}><ChatPanel /></QueryClientProvider>
    </Provider>,
  )
}

beforeEach(() => {
  patchConfigMock.mockReset().mockResolvedValue({})
  kirocrewConfigMock.mockReset().mockResolvedValue({
    agent: { model: 'auto', reasoning_effort: '' },
    advisor: {
      enabled: false,
    },
  })
})

describe('ChatPanel — Advisor settings', () => {
  it('renders every Advisor control with the configured values and bounds', async () => {
    renderPanel()

    expect(await screen.findByRole('heading', { name: 'Advisor' })).toBeInTheDocument()
    expect(screen.getByRole('switch', { name: 'Enable Advisor' })).not.toBeChecked()

    // Round-72 (First Principles): the reviewer model is the `advisor` ROLE
    // pin (agent.role_models.advisor), rendered with the same picker as the
    // other roles -- not a free-text advisor.model field.
    const model = screen.getByRole('combobox', { name: 'Advisor model' })
    expect(model).toHaveTextContent('Auto (provider picks)')
    expect(screen.queryByLabelText('Advisor model')).not.toBeInstanceOf(HTMLInputElement)

    // The note budget and interruption cooldown are backend constants, not
    // settings: no tuning knobs render.
    expect(screen.queryByLabelText('Advice limit per review')).toBeNull()
    expect(screen.queryByLabelText('Interruption cooldown (seconds)')).toBeNull()
  })

  it('PATCHes advisor.enabled and shows the optimistic value immediately', async () => {
    let resolve!: (value: object) => void
    patchConfigMock.mockImplementationOnce(() => new Promise(r => { resolve = r }))
    renderPanel()

    const toggle = await screen.findByRole('switch', { name: 'Enable Advisor' })
    await waitFor(() => expect(toggle).not.toHaveAttribute('aria-disabled'))
    fireEvent.click(toggle)

    await waitFor(() => expect(toggle).toBeChecked())
    expect(patchConfigMock).toHaveBeenCalledWith('advisor.enabled', true)
    resolve({})
  })

  it('rolls back a rejected Advisor toggle and reports the save failure', async () => {
    // The backend's deny reason (e.g. the kiro-cli-only reviewer) must reach
    // the user, or they retry a toggle that can never stick.
    patchConfigMock.mockRejectedValueOnce(new Error('reviewer runs on the kiro-cli agent backend only'))
    renderPanel()

    const toggle = await screen.findByRole('switch', { name: 'Enable Advisor' })
    await waitFor(() => expect(toggle).not.toHaveAttribute('aria-disabled'))
    fireEvent.click(toggle)

    await waitFor(() => expect(patchConfigMock).toHaveBeenCalledWith('advisor.enabled', true))
    await waitFor(() => expect(toggle).not.toBeChecked())
    // The panel renders other role="alert" icons (upstream feature-video
    // readout), so look for the save-failure notice among them.
    await waitFor(() =>
      expect(
        screen
          .getAllByRole('alert')
          .some(el =>
            el.textContent?.includes('Failed to save Advisor setting: reviewer runs on the kiro-cli agent backend only'),
          ),
      ).toBe(true),
    )
  })
})
