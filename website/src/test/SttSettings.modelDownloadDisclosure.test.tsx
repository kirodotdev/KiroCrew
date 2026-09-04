/**
 * The model picker must disclose what a model costs BEFORE the click, and it must
 * offer a way to pay that cost at a moment the user chose.
 *
 * Weights are fetched once per model, and the smallest is 78 MB while the largest
 * is 1.6 GB. Desktop releases bundle the recogniser and every runtime dependency,
 * so model download must be the user's only setup action. Three things pin that
 * contract: the per-option size, copy naming the one-click path, and the explicit
 * download control with the size on it.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react'
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
  sttStatus: ReturnType<typeof vi.fn>
  sttPrepare: ReturnType<typeof vi.fn>
}

/** 148 MB and 1.6 GB, the catalog's real `base` and `large-v3-turbo` sizes. */
const BASE_BYTES = 147_951_465
const TURBO_BYTES = 1_624_555_275

function mount(status: Record<string, unknown> = {}) {
  mockApi.sttConfig.mockResolvedValue({
    enabled: true,
    provider: 'local',
    model: 'base',
    streaming: false,
    providers: ['local', 'transcribe'],
    streaming_providers: ['local', 'transcribe'],
    language_codes: ['en-US'],
    prereqs: [],
  })
  mockApi.sttStatus.mockResolvedValue({
    available: true,
    code: '',
    detail: '',
    models: [
      { name: 'base', size_bytes: BASE_BYTES, present: false },
      { name: 'large-v3-turbo', size_bytes: TURBO_BYTES, present: false },
    ],
    download: { step: 'idle', model: '', downloaded_bytes: 0, total_bytes: 0, error: '' },
    ...status,
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <Provider store={store}>
      <QueryClientProvider client={qc}>
        <SttSettings />
      </QueryClientProvider>
    </Provider>,
  )
}

const modelSelect = () => screen.getByRole('combobox', { name: /model/i })

describe('SttSettings model download disclosure', () => {
  beforeEach(async () => {
    vi.clearAllMocks()
    await initI18n('en')
    Object.defineProperty(navigator, 'mediaDevices', {
      configurable: true,
      value: { enumerateDevices: async () => [] },
    })
  })
  afterEach(() => cleanup())

  it('says model download is the only desktop setup action', async () => {
    mount()
    await waitFor(() => expect(modelSelect()).toBeTruthy())
    const desc = screen.getByText(/models download on demand/i)
    expect(desc.textContent).toMatch(/click Download now/i)
    expect(desc.textContent).toMatch(/every other runtime dependency/i)
  })

  it('states each model size in its own option, from the served catalog', async () => {
    // The size is what makes the choice informed, and it has to be visible in the
    // list rather than after the commit. Sizes are formatted for the active locale
    // (SI, so 148 MB rather than a 1024-based mislabel).
    mount()
    await waitFor(() => expect(modelSelect()).toBeTruthy())
    fireEvent.click(modelSelect())
    await waitFor(() => expect(screen.getByRole('option', { name: /^base/ })).toBeTruthy())
    expect(screen.getByRole('option', { name: /base \(148MB\)/ })).toBeTruthy()
    expect(screen.getByRole('option', { name: /large-v3-turbo \(1\.6GB\)/ })).toBeTruthy()
  })

  it('offers a download control naming the one-time cost, and calls prepare', async () => {
    mount()
    await waitFor(() => expect(modelSelect()).toBeTruthy())
    // The cost, before the press.
    expect(screen.getByText(/one-time 148MB download/i)).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: /download now/i }))
    // The SELECTED model, explicitly: sending no id would race the config write.
    await waitFor(() => expect(mockApi.sttPrepare).toHaveBeenCalledWith('base'))
  })

  it('shows byte progress instead of the offer while the transfer runs', async () => {
    mount({
      download: {
        step: 'downloading',
        model: 'base',
        downloaded_bytes: 74_000_000,
        total_bytes: BASE_BYTES,
        error: '',
      },
    })
    await waitFor(() => expect(screen.getByText(/downloading the speech model/i)).toBeTruthy())
    // Percent AND absolute bytes: percent alone hides how much is left.
    expect(screen.getByText(/50%/)).toBeTruthy()
    expect(screen.getByText(/74MB of 148MB/)).toBeTruthy()
    expect(screen.queryByRole('button', { name: /download now/i })).toBeNull()
  })

  it('does not attribute another model transfer to the selected one', async () => {
    // The gateway runs one transfer at a time, so a switch mid-download leaves the
    // store reporting the PREVIOUS model. Claiming that progress here would show a
    // download the selected model never started.
    mount({
      download: {
        step: 'downloading',
        model: 'large-v3-turbo',
        downloaded_bytes: 800_000_000,
        total_bytes: TURBO_BYTES,
        error: '',
      },
    })
    await waitFor(() => expect(modelSelect()).toBeTruthy())
    expect(screen.queryByText(/downloading the speech model/i)).toBeNull()
    expect(screen.getByRole('button', { name: /download now/i })).toBeTruthy()
  })

  it('reports a model already on disk instead of offering it again', async () => {
    mount({
      models: [{ name: 'base', size_bytes: BASE_BYTES, present: true }],
    })
    await waitFor(() => expect(screen.getByText(/already on this machine/i)).toBeTruthy())
    expect(screen.queryByRole('button', { name: /download now/i })).toBeNull()
  })
})
