// Settings ▸ Chat — tips + dashboard-config optimistic writes through the
// per-path overlay (#6890). The model pickers' contract is pinned in
// ChatPanel.optimisticPickers.test.tsx; this file covers the two remaining
// mutations this panel converted: the Feature-Tips toggle (['tipsStatus'])
// and the whole-object dashboard config (['dashboardConfig']).
//
// SettingsSelect wraps Radix Select, which needs pointer APIs jsdom lacks —
// same lightweight mock the sibling ChatPanel suites use.
vi.mock('@radix-ui/react-select', async () => await import('./__mocks__/@radix-ui/react-select'))

import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import React from 'react'

const { tipsFeedbackMock, tipsStatusMock, dashboardConfigMock, updateDashboardConfigMock } = vi.hoisted(() => ({
  tipsFeedbackMock: vi.fn(() => Promise.resolve({ ok: true })),
  tipsStatusMock: vi.fn(() => Promise.resolve({ enabled_config: true, opted_out: false })),
  dashboardConfigMock: vi.fn(() =>
    Promise.resolve({ restore_sessions: false, restore_window_minutes: 30, merge_queued_messages: false, widget_density: 'more', quick_send: false })
  ),
  updateDashboardConfigMock: vi.fn(() => Promise.resolve({})),
}))

vi.mock('../api/client', () => ({
  api: {
    dashboardConfig: dashboardConfigMock,
    kirocrewConfig: () => Promise.resolve({ agent: { model: 'auto', reasoning_effort: '' } }),
    models: () => Promise.resolve([{ model_name: 'auto', description: 'Default' }]),
    patchConfig: () => Promise.resolve({}),
    updateDashboardConfig: updateDashboardConfigMock,
    tipsStatus: tipsStatusMock,
    tipsFeedback: tipsFeedbackMock,
  },
}))

import { ChatPanel } from '../pages/settings/ChatPanel'

function wrap(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>)
}

function defer(mock: ReturnType<typeof vi.fn>) {
  let resolve!: (v: unknown) => void
  let reject!: (e: unknown) => void
  const promise = new Promise((res, rej) => { resolve = res; reject = rej })
  mock.mockImplementationOnce(() => promise as never)
  return { resolve, reject }
}

const tipsToggle = () => screen.getByRole('switch', { name: 'Feature Tips' })
const quickSendToggle = () => screen.getByRole('switch', { name: 'Quick Send' })

beforeEach(() => {
  tipsFeedbackMock.mockReset().mockImplementation(() => Promise.resolve({ ok: true }))
  tipsStatusMock.mockReset().mockImplementation(() =>
    Promise.resolve({ enabled_config: true, opted_out: false })
  )
  dashboardConfigMock.mockReset().mockImplementation(() =>
    Promise.resolve({ restore_sessions: false, restore_window_minutes: 30, merge_queued_messages: false, widget_density: 'more', quick_send: false })
  )
  updateDashboardConfigMock.mockReset().mockImplementation(() => Promise.resolve({}))
})

describe('ChatPanel — Feature Tips toggle through the overlay', () => {
  it('shows the flip immediately, and a failure rolls back only the tips path', async () => {
    const dTips = defer(tipsFeedbackMock)
    const dDash = defer(updateDashboardConfigMock)
    wrap(<ChatPanel />)
    await waitFor(() => expect(tipsToggle()).toBeChecked())
    await waitFor(() => expect(quickSendToggle()).not.toHaveAttribute('aria-disabled'))
    expect(quickSendToggle()).not.toBeChecked()

    // A dashboard save is in flight at the same time — the cross-mutation
    // pairing the whole-snapshot writes could interleave on.
    fireEvent.click(quickSendToggle())
    await waitFor(() => expect(quickSendToggle()).toBeChecked())

    fireEvent.click(tipsToggle())
    // Pre-settle: the toggle reads OFF while only the initial status fetch
    // has run — display comes from the overlay, not a cache write.
    await waitFor(() => expect(tipsToggle()).not.toBeChecked())
    expect(tipsFeedbackMock).toHaveBeenCalledWith('', 'optout')
    expect(tipsStatusMock).toHaveBeenCalledTimes(1)

    // The tips save FAILS: its own display rolls back, the in-flight
    // dashboard toggle keeps its optimistic value.
    dTips.reject(new Error('boom'))
    await waitFor(() => expect(tipsToggle()).toBeChecked())
    expect(await screen.findByText(/Failed to save tips preference/)).toBeInTheDocument()
    expect(quickSendToggle()).toBeChecked()

    dashboardConfigMock.mockImplementation(() =>
      Promise.resolve({ restore_sessions: false, restore_window_minutes: 30, merge_queued_messages: false, widget_density: 'more', quick_send: true })
    )
    dDash.resolve({})
    await waitFor(() => expect(quickSendToggle()).toBeChecked())
  })

  it('keeps the refetched opt-out after a successful settle', async () => {
    const dTips = defer(tipsFeedbackMock)
    wrap(<ChatPanel />)
    await waitFor(() => expect(tipsToggle()).toBeChecked())
    fireEvent.click(tipsToggle())
    await waitFor(() => expect(tipsToggle()).not.toBeChecked())

    tipsStatusMock.mockImplementation(() =>
      Promise.resolve({ enabled_config: true, opted_out: true })
    )
    dTips.resolve({ ok: true })
    await waitFor(() => expect(tipsStatusMock).toHaveBeenCalledTimes(2))
    expect(tipsToggle()).not.toBeChecked()
  })
})

describe('ChatPanel — dashboard config through the overlay', () => {
  it("a slow earlier save's success does not clobber a newer toggle's display", async () => {
    // Two dashboard toggles in quick succession. Each save now carries only its
    // OWN key on the wire (the handler applies whichever keys a body has, so
    // sending the whole cached config would clobber a key another tab changed),
    // while the DISPLAY still composes on the shown config -- which is the
    // property this test exists to pin: the monotonic token keeps the first
    // save's settle from overwriting the second's display or cache write.
    const d1 = defer(updateDashboardConfigMock)
    const d2 = defer(updateDashboardConfigMock)
    wrap(<ChatPanel />)
    await waitFor(() => expect(quickSendToggle()).not.toHaveAttribute('aria-disabled'))
    expect(quickSendToggle()).not.toBeChecked()
    const mergeToggle = screen.getByRole('switch', { name: 'Merge Queued Messages' })

    fireEvent.click(quickSendToggle())
    await waitFor(() => expect(quickSendToggle()).toBeChecked())
    fireEvent.click(mergeToggle)
    await waitFor(() => expect(mergeToggle).toBeChecked())
    // The second body names only its own key; the two saves compose on the
    // SERVER, and on screen through the shown config (asserted above and below).
    expect(updateDashboardConfigMock).toHaveBeenLastCalledWith({ merge_queued_messages: true })

    // The OLDER save settles first; the server still reports the pre-save
    // config, so a wrongful cache write or pending clear would be visible as
    // either toggle dropping back.
    d1.resolve({})
    await waitFor(() => expect(dashboardConfigMock.mock.calls.length).toBeGreaterThan(1))
    expect(quickSendToggle()).toBeChecked()
    expect(mergeToggle).toBeChecked()

    dashboardConfigMock.mockImplementation(() =>
      Promise.resolve({ restore_sessions: false, restore_window_minutes: 30, merge_queued_messages: true, widget_density: 'more', quick_send: true })
    )
    d2.resolve({})
    await waitFor(() => expect(quickSendToggle()).toBeChecked())
    await waitFor(() => expect(mergeToggle).toBeChecked())
  })

  it('a failed dashboard save rolls back its display and reports the failure', async () => {
    const d1 = defer(updateDashboardConfigMock)
    wrap(<ChatPanel />)
    await waitFor(() => expect(quickSendToggle()).not.toHaveAttribute('aria-disabled'))
    expect(quickSendToggle()).not.toBeChecked()
    fireEvent.click(quickSendToggle())
    await waitFor(() => expect(quickSendToggle()).toBeChecked())

    d1.reject(new Error('boom'))
    await waitFor(() => expect(quickSendToggle()).not.toBeChecked())
    expect(await screen.findByText(/Failed to save dashboard config/)).toBeInTheDocument()
  })
})
