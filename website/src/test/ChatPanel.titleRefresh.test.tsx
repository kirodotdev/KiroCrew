import { MemoryRouter } from 'react-router-dom'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import React from 'react'

const LABEL = 'Auto-title refresh interval (turns)'
const DEFAULT = 'Default'
const PATH = 'dashboard.title_refresh_every_turns'

// A stateful stand-in for the gateway: a save changes what the next GET returns,
// so the refetch after a save shows what the real server would.
const { server, patchConfigMock, kirocrewConfigMock } = vi.hoisted(() => {
  const server = { value: undefined as number | undefined }
  return {
    server,
    patchConfigMock: vi.fn((_path: string, value: unknown) => {
      server.value = value as number
      return Promise.resolve({})
    }),
    kirocrewConfigMock: vi.fn(() => Promise.resolve(
      server.value === undefined ? {} : { dashboard: { title_refresh_every_turns: server.value } },
    )),
  }
})

vi.mock('../api/client', () => ({
  api: {
    dashboardConfig: () => Promise.resolve({ restore_sessions: false, restore_window_minutes: 30, merge_queued_messages: false, widget_density: 'more' }),
    voiceConfig: () => Promise.resolve({ enabled: false, voice: 'Ruth', engine: 'neural', rate: '100%', autoSpeak: false, aws_profile: '', region: '' }),
    sttConfig: () => Promise.resolve({ enabled: false, provider: '', model: '', available: false, streaming: false, transcribe_region: '', transcribe_profile: '', language_code: 'en-US', models: {}, language_codes: [] }),
    kirocrewConfig: kirocrewConfigMock,
    models: () => Promise.resolve([{ model_name: 'auto', description: 'Default' }]),
    patchConfig: patchConfigMock,
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

import { ChatPanel } from '../pages/settings/ChatPanel'

import { Provider } from 'react-redux'
import { createTestStore } from './helpers'

function renderSessions(stored?: number) {
  server.value = stored
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<MemoryRouter initialEntries={['/settings?tab=chat&sub=sessions']}><Provider store={createTestStore()}><QueryClientProvider client={qc}><ChatPanel /></QueryClientProvider></Provider></MemoryRouter>)
}

/** The stepper row, once the config query has resolved and enabled its buttons. */
async function row(): Promise<HTMLElement> {
  const inc = await screen.findByRole('button', { name: 'Increase' })
  await waitFor(() => expect(inc).not.toBeDisabled())
  return inc.closest(`[data-setting-key="${PATH}"]`) as HTMLElement
}

/** The row's text readout, like Message Font Size: a plain number, no input box.
 *  The box also holds invisible, aria-hidden copies of the widest values it must
 *  fit (`reserveWidthFor`), so the shown value is read with those stripped out. */
function readout(r: HTMLElement): string {
  const box = (r.querySelector('span.cursor-default') as HTMLElement).cloneNode(true) as HTMLElement
  box.querySelectorAll('[aria-hidden="true"]').forEach(el => el.remove())
  return box.textContent ?? ''
}

describe('ChatPanel settings -- auto-title refresh cadence', () => {
  beforeEach(() => {
    patchConfigMock.mockClear()
    kirocrewConfigMock.mockClear()
    localStorage.clear()
  })

  it('shows the stored cadence in the Sessions card, under a label with no placeholder letter', async () => {
    renderSessions(5)
    const r = await row()
    expect(readout(r)).toBe('5')
    expect(r.getAttribute('data-setting-label')).toBe(LABEL)
  })

  it('reads "Default", not 0, when nothing is stored: 0 is the built-in schedule, not "never"', async () => {
    renderSessions()
    expect(readout(await row())).toBe(DEFAULT)
  })

  it('reads "Default" for a stored 0 too', async () => {
    renderSessions(0)
    expect(readout(await row())).toBe(DEFAULT)
  })

  it('disables − at Default, where there is nothing below, and keeps + live', async () => {
    renderSessions(0)
    const r = await row()
    expect(within(r).getByRole('button', { name: 'Decrease' })).toBeDisabled()
    expect(within(r).getByRole('button', { name: 'Increase' })).not.toBeDisabled()
    expect(patchConfigMock).not.toHaveBeenCalled()
  })

  it('enables − at the minimum cadence, whose − returns to Default', async () => {
    renderSessions(4)
    const r = await row()
    expect(within(r).getByRole('button', { name: 'Decrease' })).not.toBeDisabled()
  })

  it('renders a plain +/- stepper with no typed input box, like Message Font Size', async () => {
    renderSessions(5)
    await row()
    expect(screen.queryByRole('spinbutton', { name: LABEL })).toBeNull()
  })

  it('steps up from Default straight to the minimum cadence', async () => {
    renderSessions(0)
    const r = await row()
    fireEvent.click(within(r).getByRole('button', { name: 'Increase' }))
    await waitFor(() => expect(patchConfigMock).toHaveBeenCalledWith(PATH, 4))
    await waitFor(() => expect(readout(r)).toBe('4'))
  })

  it('steps up by one above the minimum cadence', async () => {
    renderSessions(4)
    const r = await row()
    fireEvent.click(within(r).getByRole('button', { name: 'Increase' }))
    await waitFor(() => expect(patchConfigMock).toHaveBeenCalledWith(PATH, 5))
  })

  it('steps down from the minimum cadence back to Default, storing 0, and − goes dead', async () => {
    renderSessions(4)
    const r = await row()
    const dec = within(r).getByRole('button', { name: 'Decrease' })
    fireEvent.click(dec)
    await waitFor(() => expect(patchConfigMock).toHaveBeenCalledWith(PATH, 0))
    await waitFor(() => expect(readout(r)).toBe(DEFAULT))
    expect(dec).toBeDisabled()
  })

  it('runs rapid clicks in click order, so the last click is the value stored', async () => {
    renderSessions(4)
    const r = await row()
    // Hold the first PATCH open; without a shared scope the second would start
    // at once and could land first, leaving the earlier click as the stored value.
    let releaseFirst: () => void = () => {}
    patchConfigMock.mockImplementationOnce((_path: string, value: unknown) => new Promise(resolve => {
      releaseFirst = () => { server.value = value as number; resolve({}) }
    }))
    const inc = within(r).getByRole('button', { name: 'Increase' })
    fireEvent.click(inc)
    await waitFor(() => expect(readout(r)).toBe('5'))
    fireEvent.click(inc)
    // The readout moves at click time, but the second PATCH waits its turn.
    await waitFor(() => expect(readout(r)).toBe('6'))
    expect(patchConfigMock).toHaveBeenCalledTimes(1)
    releaseFirst()
    await waitFor(() => expect(patchConfigMock).toHaveBeenCalledTimes(2))
    expect(patchConfigMock.mock.calls.map(c => c[1])).toEqual([5, 6])
    await waitFor(() => expect(server.value).toBe(6))
  })

  it('reports a failed save and shows the stored cadence again', async () => {
    patchConfigMock.mockImplementationOnce(() => Promise.reject(new Error('boom')))
    renderSessions(8)
    const r = await row()
    fireEvent.click(within(r).getByRole('button', { name: 'Increase' }))
    expect(await screen.findByText('Failed to save title refresh setting')).toBeInTheDocument()
    await waitFor(() => expect(readout(r)).toBe('8'))
  })
})
