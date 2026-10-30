/**
 * The Status badge for an installed recogniser that does not load.
 *
 * `stt_import_failed`, `stt_unsupported_cpu`, `stt_load_crashed` and
 * `stt_native_probe_crashed` all mean the recogniser IS installed and it failed to
 * load or the preflight would not load it. The badge must say
 * "not working", not "not installed": the reason line right under it says otherwise,
 * and an install is not the fix for any of them. A plain missing extra keeps
 * "not installed".
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup } from '@testing-library/react'
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

function mountRefused(code: string) {
  const cfg = {
    enabled: true,
    provider: 'local',
    model: 'base',
    streaming: true,
    endpointing: true,
    dictation_panel: true,
    language_code: 'en-US',
    providers: ['local', 'transcribe'],
    streaming_providers: ['local'],
    language_codes: ['auto', 'en-US'],
    prereqs: [],
  }
  mockApi.sttConfig.mockResolvedValue(cfg)
  mockApi.saveSttConfig.mockImplementation(async (p: Record<string, unknown>) => ({ ...cfg, ...p }))
  mockApi.sttStatus.mockResolvedValue({
    available: false,
    code,
    detail: '',
    provider: 'local',
    model: 'base',
    models: [{ name: 'base', size_bytes: 147951465, present: true }],
    download: { step: 'idle', model: '', downloaded_bytes: 0, total_bytes: 0, error: '' },
    timings: { loads: 0, hashes: 0, last_load: null, last_final: null, partials: 0, partials_aborted: 0, decodes: [] },
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <Provider store={store}>
      <QueryClientProvider client={qc}><SttSettings /></QueryClientProvider>
    </Provider>,
  )
}

describe('Status badge for an installed-but-refused recogniser', () => {
  beforeEach(async () => {
    vi.clearAllMocks()
    await initI18n('en')
    Object.defineProperty(navigator, 'mediaDevices', {
      configurable: true,
      value: { enumerateDevices: async () => [] },
    })
  })
  afterEach(() => cleanup())

  it.each(['stt_import_failed', 'stt_unsupported_cpu', 'stt_load_crashed', 'stt_native_probe_crashed'])(
    '%s reads "not working", never "not installed"',
    async (code) => {
      mountRefused(code)
      expect(await screen.findByText('not working')).toBeTruthy()
      expect(screen.queryByText('not installed')).toBeNull()
    },
  )

  it('a missing extra still reads "not installed"', async () => {
    mountRefused('stt_extra_missing')
    expect(await screen.findByText('not installed')).toBeTruthy()
    expect(screen.queryByText('not working')).toBeNull()
  })

  it.each(['stt_import_failed', 'stt_unsupported_cpu', 'stt_load_crashed', 'stt_native_probe_crashed'])(
    '%s shows its reason as an error notice, not muted text',
    async (code) => {
      mountRefused(code)
      const notice = await screen.findByTestId('stt-unavailable-error')
      expect(notice.getAttribute('role')).toBe('alert')
    },
  )

  it('a missing extra keeps its reason as status text', async () => {
    mountRefused('stt_extra_missing')
    await screen.findByText('not installed')
    expect(screen.queryByTestId('stt-unavailable-error')).toBeNull()
  })

  it('the load-crashed reason names the product and the real remedy', async () => {
    mountRefused('stt_load_crashed')
    const line = await screen.findByText(/last attempt to load the speech model crashed/)
    expect(line.textContent).toContain('until the voice packages are reinstalled')
    expect(line.textContent).toContain('Provider menu below')
    expect(line.textContent).not.toContain('gateway')
    expect(line.textContent).not.toContain('{{productName}}')
  })
})
