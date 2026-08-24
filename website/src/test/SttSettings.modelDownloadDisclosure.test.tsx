/**
 * The Whisper model picker's description must disclose that a model downloads
 * on FIRST use, and what that looks like from the outside.
 *
 * The weight download happens inside the whisper CLI subprocess on the first
 * transcription with a given model, bounded only by `stt.timeout_secs` — so the
 * first dictation after switching models can appear to hang and then return no
 * transcript while the download continues unseen. The picker's description is
 * the one place a user scanning model sizes can learn this before hitting it,
 * so its presence is pinned here rather than left to copy drift.
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
    sttInstall: vi.fn(),
  },
}))

const mockApi = api as unknown as {
  sttConfig: ReturnType<typeof vi.fn>
}

function mount() {
  mockApi.sttConfig.mockResolvedValue({
    enabled: true,
    provider: 'whisper',
    streaming: false,
    providers: ['whisper', 'transcribe'],
    streaming_providers: ['transcribe'],
    models: { turbo: '~1.6 GB' },
    mlx_models: {},
    language_codes: ['en-US'],
    install_step: '',
    prereqs: [],
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

describe('SttSettings model picker first-use download disclosure', () => {
  beforeEach(async () => {
    vi.clearAllMocks()
    await initI18n('en')
    Object.defineProperty(navigator, 'mediaDevices', {
      configurable: true,
      value: { enumerateDevices: async () => [] },
    })
  })
  afterEach(() => cleanup())

  it('discloses the first-use download and its visible symptom on the Model row', async () => {
    mount()
    await waitFor(() =>
      expect(screen.getByRole('combobox', { name: /model/i })).toBeTruthy(),
    )
    // The fact: weights download on first use, not at install or save time.
    const desc = screen.getByText(/downloads on first use/i)
    expect(desc).toBeTruthy()
    // The symptom, in the same description: without it the fact reads as
    // harmless trivia instead of explaining a hung-then-empty first dictation.
    expect(desc.textContent).toMatch(/hang or time out/i)
    // It is the Model row's own description, not copy that drifted elsewhere.
    expect(desc.textContent).toMatch(/larger models are more accurate/i)
  })

  it('the MLX picker hint carries the same symptom, not the bare download fact', async () => {
    // Same runner, same stt.timeout_secs budget: the MLX model also downloads
    // inside the first transcription. A bare "Downloads on first use." reads
    // as harmless trivia (see the header comment), so the hint must carry the
    // hang/time-out symptom too. Pinned at the catalog level because the MLX
    // row only mounts on Apple Silicon.
    const en = (await import('../i18n/locales/en.json')) as Record<string, any>
    const hint: string =
      en.pages.settings.sttSettings.whisper_model_running_on_apple_mlx_metal_gpu_dow
    expect(hint).toMatch(/downloads on first use/i)
    expect(hint).toMatch(/hang or time out/i)
  })
})
