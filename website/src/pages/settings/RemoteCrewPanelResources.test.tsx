/**
 * Settings -> Remote Crew: the warm-set cap stepper and the usage readout.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { createTestStore } from '../../test/helpers'
import { setWarm } from '../../store/instancesSlice'
import { i18next } from '../../i18n/index'
import '../../i18n/all'

const mocks = vi.hoisted(() => ({
  patchConfig: vi.fn(),
  kirocrewConfig: vi.fn(),
}))
vi.mock('../../api/client', () => ({
  api: mocks,
}))

import { RemoteCrewUsageRow, USAGE_POLL_MS, WarmCapSetting } from './RemoteCrewPanelResources'

function renderRow(ui: React.ReactElement, store = createTestStore()) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  const invalidate = vi.spyOn(qc, 'invalidateQueries')
  render(<Provider store={store}><QueryClientProvider client={qc}>{ui}</QueryClientProvider></Provider>)
  return { invalidate }
}

const input = () => screen.getByRole('spinbutton', { name: 'Crews kept loaded' }) as HTMLInputElement

describe('WarmCapSetting', () => {
  beforeEach(() => {
    mocks.patchConfig.mockReset().mockResolvedValue({ ok: true })
    mocks.kirocrewConfig.mockReset()
  })

  it('shows automatic and saves 6 on + from automatic, refreshing both reads', async () => {
    mocks.kirocrewConfig.mockResolvedValue({ instances: { warm_set_cap: 0 } })
    const { invalidate } = renderRow(<WarmCapSetting />)
    expect(await screen.findByText(/^Automatic \(5\) · /)).toBeInTheDocument()
    expect(input().value).toBe('5')
    expect(screen.queryByRole('button', { name: 'Use automatic' })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Increase' }))
    await waitFor(() => expect(mocks.patchConfig).toHaveBeenCalledWith('instances.warm_set_cap', 6))
    await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey: ['instances'] }))
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['kirocrewConfig'] })
  })

  it('clamps a typed 99 to 20', async () => {
    mocks.kirocrewConfig.mockResolvedValue({ instances: { warm_set_cap: 0 } })
    renderRow(<WarmCapSetting />)
    await screen.findByText(/^Automatic \(5\) · /)
    fireEvent.change(input(), { target: { value: '99' } })
    fireEvent.keyDown(input(), { key: 'Enter' })
    await waitFor(() => expect(mocks.patchConfig).toHaveBeenCalledWith('instances.warm_set_cap', 20))
  })

  it('keeps a stored value above 20: - saves 29, + and a typed 99 save nothing', async () => {
    mocks.kirocrewConfig.mockResolvedValue({ instances: { warm_set_cap: 30 } })
    renderRow(<WarmCapSetting />)
    expect(await screen.findByText(/^Set to 30 · /)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Decrease' }))
    await waitFor(() => expect(mocks.patchConfig).toHaveBeenCalledWith('instances.warm_set_cap', 29))
    // The refetch still reads 30, so the row is back at 30 once the save settles.
    await waitFor(() => expect(screen.getByRole('button', { name: 'Increase' })).toBeEnabled())
    expect(screen.getByText(/^Set to 30 · /)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Increase' }))
    fireEvent.change(input(), { target: { value: '99' } })
    fireEvent.keyDown(input(), { key: 'Enter' })
    // A second - proves the row was idle: a save from + or the typed 99 would
    // have been recorded (or kept the row pending) before this one lands.
    fireEvent.click(screen.getByRole('button', { name: 'Decrease' }))
    await waitFor(() => expect(mocks.patchConfig).toHaveBeenCalledTimes(2))
    expect(mocks.patchConfig.mock.calls).toEqual([
      ['instances.warm_set_cap', 29],
      ['instances.warm_set_cap', 29],
    ])
  })

  it('shows an explicit value and saves 0 on Use automatic', async () => {
    mocks.kirocrewConfig.mockResolvedValue({ instances: { warm_set_cap: 8 } })
    renderRow(<WarmCapSetting />)
    expect(await screen.findByText(/^Set to 8 · /)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Use automatic' }))
    await waitFor(() => expect(mocks.patchConfig).toHaveBeenCalledWith('instances.warm_set_cap', 0))
  })

  it('keeps the stepper off and says so when the config read fails', async () => {
    mocks.kirocrewConfig.mockRejectedValue(new Error('GET /api/config/kirocrew 500'))
    renderRow(<WarmCapSetting />)
    expect(await screen.findByTestId('warm-cap-load-error')).toBeInTheDocument()
    expect(screen.queryByText(/^Automatic \(5\) · /)).toBeNull()
    expect(screen.getByRole('button', { name: 'Increase' })).toBeDisabled()
    expect(input()).toBeDisabled()
  })

  it('shows a stale-value notice and keeps the stepper enabled when the post-save refetch fails', async () => {
    mocks.kirocrewConfig
      .mockResolvedValueOnce({ instances: { warm_set_cap: 0 } })
      .mockRejectedValue(new Error('GET /api/config/kirocrew 500'))
    renderRow(<WarmCapSetting />)
    await screen.findByText(/^Automatic \(5\) · /)
    fireEvent.click(screen.getByRole('button', { name: 'Increase' }))
    await waitFor(() => expect(mocks.patchConfig).toHaveBeenCalledWith('instances.warm_set_cap', 6))
    await waitFor(() => expect(mocks.kirocrewConfig).toHaveBeenCalledTimes(2))
    const notice = await screen.findByTestId('warm-cap-load-error')
    expect(notice).toHaveAttribute('role', 'status')
    expect(notice).toHaveTextContent("Couldn't read how many Crews stay loaded.")
    expect(notice).toHaveTextContent('The last known state may be out of date.')
    await waitFor(() => expect(screen.getByRole('button', { name: 'Increase' })).toBeEnabled())
    expect(screen.getByRole('button', { name: 'Decrease' })).toBeEnabled()
    expect(input()).toBeEnabled()
    expect(input().value).toBe('5')
    expect(screen.getByText(/^Automatic \(5\) · /)).toBeInTheDocument()
    expect(screen.queryByTestId('warm-cap-error')).toBeNull()
  })

  it('shows an inline error when the save fails', async () => {
    mocks.kirocrewConfig.mockResolvedValue({ instances: { warm_set_cap: 3 } })
    mocks.patchConfig.mockRejectedValue(new Error('PATCH /api/config/kirocrew 500'))
    renderRow(<WarmCapSetting />)
    await screen.findByText(/^Set to 3 · /)
    fireEvent.click(screen.getByRole('button', { name: 'Decrease' }))
    expect(await screen.findByTestId('warm-cap-error')).toBeInTheDocument()
  })
})

describe('RemoteCrewUsageRow', () => {
  afterEach(() => {
    delete (window as { electronAPI?: unknown }).electronAPI
    vi.useRealTimers()
  })

  it('renders nothing without the desktop bridge', () => {
    renderRow(<RemoteCrewUsageRow />)
    expect(screen.queryByTestId('remote-crew-usage')).toBeNull()
  })

  it('asks for the warm panes and shows their usage, then polls', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const remoteCrewUsage = vi.fn().mockResolvedValue({ panes: 2, sharedPanes: 1, memoryMb: 412, cpuPercent: 3.5 })
    ;(window as { electronAPI?: unknown }).electronAPI = { remoteCrewUsage }
    const store = createTestStore()
    store.dispatch(setWarm({ id: 'a', conn: { port: 7801, token: 't' } }))
    renderRow(<RemoteCrewUsageRow />, store)
    expect(await screen.findByText('Loaded 2 · Memory 412 MB · CPU 3.5%')).toBeInTheDocument()
    expect(screen.getByTestId('remote-crew-usage-shared')).toHaveTextContent("Sharing the main window's process: 1.")
    expect(remoteCrewUsage).toHaveBeenCalledWith([`http://${window.location.hostname}:7801`])
    await act(async () => { vi.advanceTimersByTime(USAGE_POLL_MS) })
    expect(remoteCrewUsage).toHaveBeenCalledTimes(2)
  })

  it('drops the old numbers and shows an error when a read fails', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const remoteCrewUsage = vi.fn()
      .mockResolvedValueOnce({ panes: 1, sharedPanes: 0, memoryMb: 200, cpuPercent: 1 })
      .mockRejectedValueOnce(new Error('remote-crew:usage failed'))
    ;(window as { electronAPI?: unknown }).electronAPI = { remoteCrewUsage }
    renderRow(<RemoteCrewUsageRow />)
    expect(await screen.findByText('Loaded 1 · Memory 200 MB · CPU 1%')).toBeInTheDocument()
    await act(async () => { vi.advanceTimersByTime(USAGE_POLL_MS) })
    expect(await screen.findByTestId('remote-crew-usage-error')).toBeInTheDocument()
    expect(screen.queryByText('Loaded 1 · Memory 200 MB · CPU 1%')).toBeNull()
  })

  it('writes the usage numbers in the interface language', async () => {
    ;(window as { electronAPI?: unknown }).electronAPI = {
      remoteCrewUsage: vi.fn().mockResolvedValue({ panes: 2, sharedPanes: 1, memoryMb: 1536, cpuPercent: 3.5 }),
    }
    await i18next.changeLanguage('de')
    try {
      renderRow(<RemoteCrewUsageRow />)
      const value = await screen.findByTestId('remote-crew-usage-value')
      await waitFor(() => expect(value).toHaveTextContent('3,5'))
      expect(value).toHaveTextContent('1.536')
      expect(value).not.toHaveTextContent('3.5')
    } finally {
      await i18next.changeLanguage('en')
    }
  })

  it('says so when no remote Crew is loaded', async () => {
    ;(window as { electronAPI?: unknown }).electronAPI = {
      remoteCrewUsage: vi.fn().mockResolvedValue({ panes: 0, sharedPanes: 0, memoryMb: 0, cpuPercent: 0 }),
    }
    renderRow(<RemoteCrewUsageRow />)
    expect(await screen.findByText('No remote Crew is loaded.')).toBeInTheDocument()
    expect(screen.queryByTestId('remote-crew-usage-shared')).toBeNull()
  })
})
