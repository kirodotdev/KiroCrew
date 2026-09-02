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
    sttStatus: vi.fn(),
    sttPrepare: vi.fn(),
  },
}))

const mockApi = api as unknown as {
  sttConfig: ReturnType<typeof vi.fn>
  saveSttConfig: ReturnType<typeof vi.fn>
  sttStatus: ReturnType<typeof vi.fn>
}

function payload(over: Record<string, unknown> = {}) {
  return {
    enabled: true,
    provider: 'local',
    model: 'base',
    streaming: false,
    providers: ['local', 'apple', 'transcribe'],
    // A DELIBERATELY partial list: the point of these specs is that the panel
    // reads the served capability rather than assuming, so the fixture withholds
    // the capability from a provider that really has it.
    streaming_providers: ['transcribe', 'apple'],
    language_codes: ['en-US'],
    prereqs: [],
    ...over,
  }
}

function mount(over: Record<string, unknown> = {}) {
  const data = payload(over)
  mockApi.sttConfig.mockResolvedValue(data)
  mockApi.saveSttConfig.mockImplementation(async (p: Record<string, unknown>) => ({ ...data, ...p }))
  mockApi.sttStatus.mockResolvedValue({
    available: true,
    code: '',
    detail: '',
    models: [{ name: 'base', size_bytes: 147951465, present: true }],
    download: { step: 'idle', model: '', downloaded_bytes: 0, total_bytes: 0, error: '' },
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

/** The Streaming row, identified by its description copy. */
const streamingRow = () => screen.queryByText(/show the transcript in the input box/i)

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
    mount({ provider: 'local' })
    await waitFor(() => expect(mockApi.sttConfig).toHaveBeenCalled())
    await waitFor(() => expect(providerSelect()).toBeTruthy())
    expect(streamingRow()).toBeNull()
  })

  it('turns streaming on when moving to a streaming-capable provider', async () => {
    mount({ provider: 'local', streaming: false })
    await waitFor(() => expect(providerSelect()).toBeTruthy())
    await pickProvider(/Apple Speech/i)
    await waitFor(() =>
      expect(mockApi.saveSttConfig).toHaveBeenCalledWith({ provider: 'apple', streaming: true }),
    )
  })

  it('turns streaming off when moving to a provider the served list excludes', async () => {
    // Leaving it on would advertise partials the gateway has told us this provider
    // does not produce.
    mount({ provider: 'apple', streaming: true })
    await waitFor(() => expect(providerSelect()).toBeTruthy())
    await pickProvider(/^Local/)
    await waitFor(() =>
      expect(mockApi.saveSttConfig).toHaveBeenCalledWith({ provider: 'local', streaming: false }),
    )
  })

  it('assumes every provider streams when the backend omits the capability list', async () => {
    // All three providers stream, so a gateway serving no `streaming_providers`
    // must not lose the toggle. The fallback used to be transcribe-only, which
    // would now hide the DEFAULT provider's own toggle.
    mount({ provider: 'local', streaming_providers: undefined })
    await waitFor(() => expect(streamingRow()).toBeTruthy())
  })
})
