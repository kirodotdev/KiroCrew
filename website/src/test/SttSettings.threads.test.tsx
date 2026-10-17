/**
 * The decode-thread picker in Settings -> Voice -> Fine-tuning.
 *
 * Its choices are the gateway's to serve (`thread_options`, sized to the host's
 * cores), so these tests pin that the panel renders exactly those, saves the
 * chosen count as a number, stays off the surface, and says when the environment
 * variable overrides whatever is picked here.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'

import { store } from '../store'
import { initI18n } from '../i18n'
import SttSettings from '../pages/settings/SttSettings'
import { api } from '../api/client'

vi.mock('../api/client', () => ({
  api: {
    sttConfig: vi.fn(),
    saveSttConfig: vi.fn(),
    sttStatus: vi.fn(),
    sttPrepare: vi.fn(),
  },
}))

const mockApi = api as unknown as {
  sttConfig: ReturnType<typeof vi.fn>
  saveSttConfig: ReturnType<typeof vi.fn>
  sttStatus: ReturnType<typeof vi.fn>
}

function mountPanel(over: Record<string, unknown> = {}) {
  const cfg = {
    enabled: true,
    provider: 'local',
    model: 'base',
    streaming: true,
    endpointing: false,
    dictation_panel: true,
    language_code: 'en-US',
    providers: ['local', 'transcribe'],
    streaming_providers: ['local'],
    language_codes: ['auto', 'en-US'],
    prereqs: [],
    threads: 0,
    thread_options: [0, 1, 2, 4, 8, 16, 32, 64],
    threads_env: null,
    ...over,
  }
  mockApi.sttConfig.mockResolvedValue(cfg)
  mockApi.saveSttConfig.mockImplementation(async (p: Record<string, unknown>) => ({ ...cfg, ...p }))
  mockApi.sttStatus.mockResolvedValue({
    available: true,
    code: '',
    detail: '',
    provider: 'local',
    model: 'base',
    models: [{ name: 'base', size_bytes: 147951465, present: true }],
    download: { step: 'idle', model: '', downloaded_bytes: 0, total_bytes: 0, error: '' },
    timings: { last_load: null, last_final: null },
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <Provider store={store}>
      <QueryClientProvider client={qc}><SttSettings /></QueryClientProvider>
    </Provider>,
  )
}

async function openFineTuning() {
  await waitFor(() => expect(screen.getByRole('button', { name: /fine-tuning/i })).toBeTruthy())
  fireEvent.click(screen.getByRole('button', { name: /fine-tuning/i }))
}

describe('decode thread picker', () => {
  beforeEach(async () => {
    vi.clearAllMocks()
    await initI18n('en')
    Object.defineProperty(navigator, 'mediaDevices', {
      configurable: true,
      value: { enumerateDevices: async () => [] },
    })
  })
  afterEach(() => cleanup())

  it('stays behind the Fine-tuning disclosure', async () => {
    mountPanel()
    await waitFor(() => expect(screen.getByRole('combobox', { name: /model/i })).toBeTruthy())
    expect(screen.queryByRole('combobox', { name: /decode threads/i })).toBeNull()
  })

  it('offers the served choices, with 0 labelled Auto, and saves a number', async () => {
    mountPanel()
    await openFineTuning()
    const picker = await screen.findByRole('combobox', { name: /decode threads/i })
    expect(picker.textContent).toContain('Auto')

    fireEvent.click(picker)
    const listbox = await screen.findByRole('listbox')
    const labels = within(listbox).getAllByRole('option').map(o => o.textContent)
    expect(labels).toEqual(['Auto', '1', '2', '4', '8', '16', '32', '64'])

    fireEvent.click(within(listbox).getByRole('option', { name: '64' }))
    await waitFor(() => expect(mockApi.saveSttConfig).toHaveBeenCalled())
    expect(mockApi.saveSttConfig.mock.calls[0][0]).toEqual({ threads: 64 })
  })

  it('names the environment override when the gateway has one', async () => {
    mountPanel({ threads_env: '48' })
    await openFineTuning()
    await screen.findByRole('combobox', { name: /decode threads/i })
    expect(screen.getByText(/Ignored while KIROCREW_STT_THREADS sets 48 threads/)).toBeTruthy()
  })

  it('is absent for a provider that does not run the local model', async () => {
    mountPanel({ provider: 'transcribe' })
    await openFineTuning()
    expect(screen.queryByRole('combobox', { name: /decode threads/i })).toBeNull()
  })
})
