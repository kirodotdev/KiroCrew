/**
 * The install surface must be provider-aware.
 *
 * With `stt.provider = "transcribe"` the page used to offer "Install Whisper" —
 * a button that installs a different engine and can never change Transcribe's
 * availability (which is "boto3 + amazon-transcribe importable by the gateway
 * process"). Pressing it appeared to work and changed nothing, leaving no
 * in-app path to a working state. These tests pin that:
 *
 *  - Transcribe gets the prerequisite commands plus a restart hint, no button;
 *  - Whisper keeps its Install button (regression guard);
 *  - the Runtime row is gone — the backend never served `docker_mode`, so the
 *    row could only ever display "Native".
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
    available: false,
    providers: ['whisper', 'transcribe'],
    streaming_providers: ['transcribe'],
    models: { turbo: '1.5 GB' },
    mlx_models: {},
    language_codes: ['en-US'],
    install_step: '',
    prereqs: [],
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

/** Wait for the loaded card (the Status row only renders post-fetch). */
const loaded = () => screen.findByText(/not installed/i)

describe('SttSettings provider-aware install surface', () => {
  beforeEach(async () => {
    await initI18n()
    vi.clearAllMocks()
  })
  afterEach(cleanup)

  it('hides the Install button and shows the restart hint for Transcribe', async () => {
    mount({
      provider: 'transcribe',
      prereqs: ["/opt/kirocrew/bin/python -m pip install 'kirocrew[voice]'"],
    })
    await loaded()
    // No install affordance of any kind — the button installs a local Whisper
    // runtime, which cannot change Transcribe's availability.
    expect(screen.queryByRole('button', { name: /install/i })).toBeNull()
    // The prerequisite command from the backend is rendered verbatim…
    expect(screen.getByText(/kirocrew\[voice\]/)).toBeTruthy()
    // …with the transcribe-specific next step, not the button trailer.
    expect(screen.getByText(/restart the gateway/i)).toBeTruthy()
    expect(screen.queryByText(/then click install below/i)).toBeNull()
  })

  it('keeps the Install Whisper button for the whisper provider', async () => {
    mount({ provider: 'whisper' })
    await loaded()
    expect(screen.getByRole('button', { name: /install whisper/i })).toBeTruthy()
  })

  it('shows the unsupported notice when no install channel can get the voice extra', async () => {
    mount({ provider: 'transcribe', transcribe_unsupported: true, prereqs: [] })
    await loaded()
    // Frozen build, pip-less interpreter, or externally-managed python: no
    // button and no command can help — the page must say so, cause-neutrally.
    expect(screen.getByText(/can't install extra packages/i)).toBeTruthy()
    expect(screen.queryByRole('button', { name: /install/i })).toBeNull()
    expect(screen.queryByText(/run these commands/i)).toBeNull()
  })

  it('names the desktop app in the unsupported notice on the bundled interpreter', async () => {
    mount({ provider: 'transcribe', transcribe_unsupported: true, bundled_interpreter: true, prereqs: [] })
    await loaded()
    // "Run the gateway from a different Python environment" is not actionable
    // inside the app bundle — the copy must name the pip-install remedy.
    expect(screen.getByText(/desktop app can't add transcribe support/i)).toBeTruthy()
    expect(screen.queryByText(/this gateway's python can't install extra packages/i)).toBeNull()
  })

  it('surfaces the ffmpeg gap even when Status reads ready', async () => {
    mount({
      provider: 'transcribe',
      available: true,
      ffmpeg_missing: true,
      prereqs: ['sudo apt-get install -y ffmpeg'],
    })
    // `available: true` renders the Ready badge, so the not-installed anchor
    // never appears — wait on the warning itself.
    expect(await screen.findByText(/ffmpeg is missing/i)).toBeTruthy()
    expect(screen.getByText('sudo apt-get install -y ffmpeg')).toBeTruthy()
  })

  it('renders no trailer for a Transcribe ffmpeg-only prereq list', async () => {
    // Extra installed but STT disabled and ffmpeg missing: the list carries
    // only the ffmpeg command. Neither trailer fits — no install button
    // exists for Transcribe, and ffmpeg needs no restart.
    mount({ provider: 'transcribe', prereqs: ['sudo apt-get install -y ffmpeg'] })
    await loaded()
    expect(screen.getByText('sudo apt-get install -y ffmpeg')).toBeTruthy()
    expect(screen.queryByText(/then click install below/i)).toBeNull()
    expect(screen.queryByText(/restart the gateway/i)).toBeNull()
  })

  it('surfaces the ffmpeg gap for whisper too', async () => {
    // The availability checks skip ffmpeg for every provider, so the warning
    // is not Transcribe-gated.
    mount({
      provider: 'whisper',
      available: true,
      ffmpeg_missing: true,
      prereqs: ['sudo apt-get install -y ffmpeg'],
    })
    expect(await screen.findByText(/ffmpeg is missing/i)).toBeTruthy()
  })

  it('shows no ffmpeg warning when ffmpeg is present', async () => {
    mount({ provider: 'transcribe', available: true, ffmpeg_missing: false, prereqs: [] })
    await screen.findByText(/ready/i)
    expect(screen.queryByText(/ffmpeg is missing/i)).toBeNull()
  })

  it('renders no Runtime row for any provider', async () => {
    mount({ provider: 'whisper' })
    await loaded()
    // The backend never serves `docker_mode`, so the row could only ever
    // read "Native" — it conveys nothing and is gone.
    expect(screen.queryByText(/^runtime$/i)).toBeNull()
  })
})
