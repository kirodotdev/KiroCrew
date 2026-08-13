/**
 * The streaming controls must be gated on a CAPABILITY, not on a provider name.
 *
 * When the on-device `apple` provider was added, this panel still read
 * `provider === 'transcribe'`, so selecting `apple` hid the Streaming toggle
 * entirely — the provider's whole reason for existing became unreachable from the
 * UI and could only be enabled by hand-editing config.json. The backend owns the
 * capability list (`stt_stream._STREAMING_PROVIDERS`) and serves it as
 * `streaming_providers`; these tests pin that the panel honours it.
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
    sttInstall: vi.fn(),
  },
}))

const mockApi = api as unknown as {
  sttConfig: ReturnType<typeof vi.fn>
  saveSttConfig: ReturnType<typeof vi.fn>
}

function payload(over: Record<string, unknown> = {}) {
  return {
    enabled: true,
    provider: 'whisper',
    streaming: false,
    providers: ['whisper', 'mlx', 'apple', 'transcribe'],
    streaming_providers: ['transcribe', 'apple'],
    models: { turbo: '1.5 GB' },
    mlx_models: {},
    language_codes: ['en-US'],
    install_step: '',
    prereqs: {},
    ...over,
  }
}

function mount(over: Record<string, unknown> = {}) {
  const data = payload(over)
  mockApi.sttConfig.mockResolvedValue(data)
  mockApi.saveSttConfig.mockImplementation(async (p: Record<string, unknown>) => ({ ...data, ...p }))
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <Provider store={store}>
      <QueryClientProvider client={qc}>
        <SttSettings />
      </QueryClientProvider>
    </Provider>,
  )
}

/** The Streaming row, identified by its description copy. */
const streamingRow = () => screen.queryByText(/stream live partial transcripts/i)

/**
 * The provider `<select>`, located by its accessible name rather than by index —
 * the mic-device select is rendered first, so `getAllByRole('combobox')[0]` picks
 * the wrong control.
 */
const providerSelect = () => screen.getByRole('combobox', { name: /provider/i })

/**
 * Pick *label* from the provider dropdown. `SimpleSelect` wraps a Radix Select, so
 * a `change` event on the trigger does nothing — open it, then click the option
 * (the pattern used by `ArtifactDeployPage.test.tsx`).
 */
async function pickProvider(label: RegExp) {
  fireEvent.click(providerSelect())
  await waitFor(() => expect(screen.getByRole('option', { name: label })).toBeTruthy())
  fireEvent.click(screen.getByRole('option', { name: label }))
}

describe('SttSettings streaming gate', () => {
  beforeEach(async () => {
    vi.clearAllMocks()
    await initI18n('en')
    Object.defineProperty(navigator, 'mediaDevices', {
      configurable: true,
      value: { enumerateDevices: async () => [] },
    })
  })
  afterEach(() => cleanup())

  it('offers the streaming toggle for the on-device apple provider', async () => {
    mount({ provider: 'apple' })
    await waitFor(() => expect(streamingRow()).toBeTruthy())
  })

  it('hides the streaming toggle for a provider that cannot stream', async () => {
    mount({ provider: 'whisper' })
    await waitFor(() => expect(mockApi.sttConfig).toHaveBeenCalled())
    await waitFor(() => expect(providerSelect()).toBeTruthy())
    expect(streamingRow()).toBeNull()
  })

  it('turns streaming on when moving to a streaming-capable provider', async () => {
    mount({ provider: 'whisper', streaming: false })
    await waitFor(() => expect(providerSelect()).toBeTruthy())
    await pickProvider(/Apple Speech/i)
    await waitFor(() =>
      expect(mockApi.saveSttConfig).toHaveBeenCalledWith({ provider: 'apple', streaming: true }),
    )
  })

  it('turns streaming off when moving to a provider that cannot stream', async () => {
    // Leaving it on would be a lie: the whisper/mlx CLIs have no partial channel.
    mount({ provider: 'apple', streaming: true })
    await waitFor(() => expect(providerSelect()).toBeTruthy())
    await pickProvider(/^Whisper \(local\)$/)
    await waitFor(() =>
      expect(mockApi.saveSttConfig).toHaveBeenCalledWith({ provider: 'whisper', streaming: false }),
    )
  })

  it('falls back to transcribe-only when the backend omits the capability list', async () => {
    // An older gateway serving no `streaming_providers` must not lose the toggle it
    // already had.
    mount({ provider: 'transcribe', streaming_providers: undefined })
    await waitFor(() => expect(streamingRow()).toBeTruthy())
  })
})
