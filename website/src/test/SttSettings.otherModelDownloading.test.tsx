/**
 * While another model's transfer runs, the selected model's Download now waits
 * for it and says which model it is waiting for.
 *
 * The gateway runs one model transfer at a time, and a prepare request for a
 * different model starts nothing while one runs. An enabled button there would
 * accept a press that does nothing, with no progress and no reason on screen.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, cleanup } from '@testing-library/react'
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

const WAITS_LINE = /large-v3-turbo is downloading\. Download now becomes available when it finishes\./

/** The status body the gateway serves, with `download` laid over it. `base` is the
 *  selected model and is not on disk. */
function statusBody(download: Record<string, unknown>) {
  return structuredClone({
    available: true,
    code: '',
    detail: '',
    models: [
      { name: 'base', size_bytes: BASE_BYTES, present: false },
      { name: 'large-v3-turbo', size_bytes: TURBO_BYTES, present: false },
    ],
    download: { step: 'idle', model: '', downloaded_bytes: 0, total_bytes: 0, error: '', ...download },
  })
}

function mount(download: Record<string, unknown>) {
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
  mockApi.sttStatus.mockImplementation(async () => statusBody(download))
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <Provider store={store}>
      <QueryClientProvider client={qc}>
        <SttSettings />
      </QueryClientProvider>
    </Provider>,
  )
}

/** Resolves once the status body has rendered the model block. */
async function modelBlockReady() {
  await screen.findByRole('combobox', { name: /model/i })
  await waitFor(() => expect(mockApi.sttStatus).toHaveBeenCalled())
}

describe('SttSettings Download now while another model downloads', () => {
  beforeEach(async () => {
    mockApi.sttConfig.mockReset()
    mockApi.sttStatus.mockReset()
    mockApi.sttPrepare.mockReset()
    await initI18n('en')
    Object.defineProperty(navigator, 'mediaDevices', {
      configurable: true,
      value: { enumerateDevices: async () => [] },
    })
  })
  afterEach(() => cleanup())

  it('disables the selected model\'s Download now and names the model downloading', async () => {
    mount({ step: 'downloading', model: 'large-v3-turbo', downloaded_bytes: 800_000_000, total_bytes: TURBO_BYTES })
    expect(await screen.findByText(WAITS_LINE)).toBeTruthy()
    const button = screen.getByRole('button', { name: /download now/i }) as HTMLButtonElement
    expect(button.disabled).toBe(true)
    // The other model's bytes are not the selected model's progress.
    expect(screen.queryByText(/downloading the speech model/i)).toBeNull()
  })

  it('shows the selected model\'s own transfer as progress, with no waiting line', async () => {
    mount({ step: 'downloading', model: 'base', downloaded_bytes: 74_000_000, total_bytes: BASE_BYTES })
    expect(await screen.findByText(/downloading the speech model/i)).toBeTruthy()
    expect(screen.getByText(/74MB of 148MB/)).toBeTruthy()
    expect(screen.queryByText(WAITS_LINE)).toBeNull()
    expect(screen.queryByRole('button', { name: /download now/i })).toBeNull()
  })

  it('keeps Download now enabled when the other model\'s transfer failed', async () => {
    mount({ step: 'failed', model: 'large-v3-turbo', total_bytes: TURBO_BYTES, error: 'connection reset' })
    await modelBlockReady()
    const button = await screen.findByRole('button', { name: /download now/i }) as HTMLButtonElement
    expect(button.disabled).toBe(false)
    expect(screen.queryByText(/is downloading\. Download now becomes available/)).toBeNull()
  })
})
