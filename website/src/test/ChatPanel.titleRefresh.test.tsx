import { MemoryRouter } from 'react-router-dom'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import React from 'react'

const LABEL = 'Auto-title refresh interval (turns)'
const TOGGLE_LABEL = 'Refresh auto titles'
const DEFAULT = 'Default'
const PATH = 'dashboard.title_refresh_every_turns'
const ENABLED_PATH = 'dashboard.title_refresh_enabled'

// A stateful stand-in for the gateway: a save changes what the next GET returns,
// so the refetch after a save shows what the real server would.
const { server, patchConfigMock, kirocrewConfigMock } = vi.hoisted(() => {
  const server = { value: undefined as number | undefined, enabled: undefined as boolean | undefined }
  return {
    server,
    patchConfigMock: vi.fn((path: string, value: unknown) => {
      if (path === 'dashboard.title_refresh_enabled') server.enabled = value as boolean
      else server.value = value as number
      return Promise.resolve({})
    }),
    kirocrewConfigMock: vi.fn(() => {
      const dashboard: Record<string, unknown> = {}
      if (server.value !== undefined) dashboard.title_refresh_every_turns = server.value
      if (server.enabled !== undefined) dashboard.title_refresh_enabled = server.enabled
      return Promise.resolve(Object.keys(dashboard).length ? { dashboard } : {})
    }),
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

function renderSessions(stored?: number, enabled?: boolean) {
  server.value = stored
  server.enabled = enabled
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<MemoryRouter initialEntries={['/settings?tab=chat&sub=sessions']}><Provider store={createTestStore()}><QueryClientProvider client={qc}><ChatPanel /></QueryClientProvider></Provider></MemoryRouter>)
}

/** The stepper row, once the config query has resolved and enabled its buttons. */
async function row(): Promise<HTMLElement> {
  const inc = await screen.findByRole('button', { name: 'Increase' })
  await waitFor(() => expect(inc).not.toBeDisabled())
  return inc.closest(`[data-setting-key="${PATH}"]`) as HTMLElement
}

function box(r: HTMLElement): HTMLInputElement {
  return within(r).getByRole('spinbutton', { name: LABEL }) as HTMLInputElement
}

/** What the row's box shows: the typed value, or its placeholder when empty. */
function readout(r: HTMLElement): string {
  const input = box(r)
  return input.value === '' ? input.placeholder : input.value
}

function typeAndCommit(r: HTMLElement, text: string) {
  const input = box(r)
  fireEvent.change(input, { target: { value: text } })
  fireEvent.keyDown(input, { key: 'Enter' })
}

async function toggle(): Promise<HTMLElement> {
  const t = await screen.findByRole('switch', { name: TOGGLE_LABEL })
  await waitFor(() => expect(t).not.toBeDisabled())
  return t
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

  it('saves a typed cadence on Enter', async () => {
    renderSessions(0)
    const r = await row()
    typeAndCommit(r, '50')
    await waitFor(() => expect(patchConfigMock).toHaveBeenCalledWith(PATH, 50))
    await waitFor(() => expect(readout(r)).toBe('50'))
  })

  it('saves a typed cadence on blur', async () => {
    renderSessions(5)
    const r = await row()
    const input = box(r)
    fireEvent.change(input, { target: { value: '12' } })
    fireEvent.blur(input)
    await waitFor(() => expect(patchConfigMock).toHaveBeenCalledWith(PATH, 12))
  })

  it.each([
    ['2', 4],
    ['5000', 1000],
    ['0', 0],
  ])('clamps a typed %s the way the loader reads it, storing %d', async (typed, stored) => {
    renderSessions(8)
    const r = await row()
    typeAndCommit(r, typed)
    await waitFor(() => expect(patchConfigMock).toHaveBeenCalledWith(PATH, stored))
  })

  it('returns to Default when the box is emptied', async () => {
    renderSessions(8)
    const r = await row()
    typeAndCommit(r, '')
    await waitFor(() => expect(patchConfigMock).toHaveBeenCalledWith(PATH, 0))
    await waitFor(() => expect(readout(r)).toBe(DEFAULT))
  })

  it('does not save an emptied box that already shows Default', async () => {
    renderSessions(0)
    const r = await row()
    typeAndCommit(r, '')
    await waitFor(() => expect(readout(r)).toBe(DEFAULT))
    expect(patchConfigMock).not.toHaveBeenCalled()
  })

  it('reverts a half-typed entry such as "-" instead of treating it as an emptied box', async () => {
    renderSessions(8)
    const r = await row()
    const input = box(r)
    // jsdom never flags badInput; a browser does for "-", and reports its value as "".
    Object.defineProperty(input, 'validity', { configurable: true, get: () => ({ badInput: true }) })
    fireEvent.change(input, { target: { value: '' } })
    fireEvent.blur(input)
    expect(readout(r)).toBe('8')
    expect(patchConfigMock).not.toHaveBeenCalled()
  })

  it('reverts a typed value on Escape without saving', async () => {
    renderSessions(8)
    const r = await row()
    const input = box(r)
    fireEvent.change(input, { target: { value: '40' } })
    fireEvent.keyDown(input, { key: 'Escape' })
    expect(readout(r)).toBe('8')
    fireEvent.blur(input)
    expect(patchConfigMock).not.toHaveBeenCalled()
  })
})

describe('ChatPanel settings -- turning auto-title refresh off', () => {
  beforeEach(() => {
    patchConfigMock.mockClear()
    kirocrewConfigMock.mockClear()
    localStorage.clear()
  })

  it('is on by default and shows the cadence row', async () => {
    renderSessions()
    expect(await toggle()).toBeChecked()
    await row()
  })

  it('hides the cadence row while refresh is off', async () => {
    renderSessions(10, false)
    expect(await toggle()).not.toBeChecked()
    expect(screen.queryByRole('spinbutton', { name: LABEL })).toBeNull()
  })

  it('stores false and hides the cadence row when switched off', async () => {
    renderSessions(10)
    await row()
    fireEvent.click(await toggle())
    await waitFor(() => expect(patchConfigMock).toHaveBeenCalledWith(ENABLED_PATH, false))
    await waitFor(() => expect(screen.queryByRole('spinbutton', { name: LABEL })).toBeNull())
    expect(server.value).toBe(10)
  })

  it('brings the stored cadence back when switched on again', async () => {
    renderSessions(10, false)
    fireEvent.click(await toggle())
    await waitFor(() => expect(patchConfigMock).toHaveBeenCalledWith(ENABLED_PATH, true))
    expect(readout(await row())).toBe('10')
  })
})
