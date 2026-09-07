/**
 * The unavailable surface must name a remedy the user can actually carry out.
 *
 * Transcribe's availability is "boto3 + amazon-transcribe importable by the
 * gateway process", which nothing inside the dashboard can change: a package
 * becomes importable only in a fresh interpreter. So the page renders the
 * backend's prerequisite commands plus the restart that makes them take effect,
 * and it says so cause-neutrally when NO install channel exists at all (a frozen
 * build, a pip-less interpreter, an externally-managed python). These tests pin
 * that, plus the ffmpeg gap, which is deliberately reported even when the status
 * reads ready: the availability probe treats ffmpeg as optional, so a missing
 * one would otherwise surface only as a silent dictation failure.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup, fireEvent, waitFor } from '@testing-library/react'
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
    restartGateway: vi.fn(),
    sttStatus: vi.fn(),
    sttPrepare: vi.fn(),
  },
}))

const mockApi = api as unknown as {
  sttConfig: ReturnType<typeof vi.fn>
  saveSttConfig: ReturnType<typeof vi.fn>
  restartGateway: ReturnType<typeof vi.fn>
  sttStatus: ReturnType<typeof vi.fn>
}

function payload(over: Record<string, unknown> = {}) {
  return {
    enabled: true,
    provider: 'local',
    model: 'base',
    streaming: false,
    available: false,
    providers: ['local', 'transcribe'],
    streaming_providers: ['local', 'transcribe'],
    language_codes: ['en-US'],
    prereqs: [],
    ...over,
  }
}

function mount(over: Record<string, unknown> = {}) {
  const data = payload(over)
  mockApi.sttConfig.mockResolvedValue(data)
  mockApi.saveSttConfig.mockImplementation(async (p: Record<string, unknown>) => ({ ...data, ...p }))
  // The status endpoint answers the SAME verdict as the config fixture. The two
  // are served from one backend probe, so a fixture where they disagree would
  // exercise a state the gateway cannot produce. That includes the decoder: the
  // block is driven by status's `ffmpeg` object (which names WHICH decoder would
  // run and whether a fetch can fix the host), while the config's
  // `ffmpeg_missing` is only kept for compatibility.
  mockApi.sttStatus.mockResolvedValue({
    available: data.available !== false,
    code: data.available === false ? 'stt_extra_missing' : '',
    detail: '',
    models: [{ name: 'base', size_bytes: 147951465, present: true }],
    download: { step: 'idle', model: '', downloaded_bytes: 0, total_bytes: 0, error: '' },
    ffmpeg: {
      present: !data.ffmpeg_missing,
      source: data.ffmpeg_missing ? null : 'system',
      // These fixtures assert the MANUAL command route, which is the branch a
      // platform with no pinned executable takes.
      auto_fetch: data.bundled_interpreter ? 'bundled' : 'unsupported',
      os: 'Linux',
      arch: 'x86_64',
      download: {
        stage: 'idle',
        artifact: '',
        downloaded_bytes: 0,
        total_bytes: 0,
        error_code: '',
        error_detail: '',
      },
    },
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

/**
 * Wait for the loaded card (the Status row only renders post-fetch).
 *
 * Exact text, not a regex: the reason line beneath the badge also contains "not
 * installed", so a loose match finds two nodes and fails on the ambiguity.
 */
const loaded = () => screen.findByText('not installed')

describe('SttSettings provider-aware install surface', () => {
  beforeEach(async () => {
    await initI18n()
    vi.clearAllMocks()
  })
  afterEach(cleanup)

  it('hides the Install button and shows the restart hint for Transcribe', async () => {
    mount({
      provider: 'transcribe',
      prereqs: ["/opt/kirocrew/bin/python -m pip install 'boto3>=1.34,<2' 'amazon-transcribe>=0.6,<1'"],
    })
    await loaded()
    // No install affordance of any kind — the button installs a local Whisper
    // runtime, which cannot change Transcribe's availability.
    expect(screen.queryByRole('button', { name: /install/i })).toBeNull()
    // The prerequisite command from the backend is rendered verbatim…
    expect(screen.getByText(/amazon-transcribe>=0\.6,<1/)).toBeTruthy()
    // …with the next step that makes it take effect.
    expect(screen.getByText(/restart the gateway/i)).toBeTruthy()
    expect(screen.getByRole('button', { name: /restart gateway/i })).toBeTruthy()
  })

  it('confirms before restarting and disables the action in flight', async () => {
    let finish!: () => void
    mockApi.restartGateway.mockImplementation(() => new Promise<void>(resolve => { finish = resolve }))
    mount({
      provider: 'transcribe',
      prereqs: ["/opt/kirocrew/bin/python -m pip install 'boto3>=1.34,<2' 'amazon-transcribe>=0.6,<1'"],
    })
    await loaded()

    const restart = screen.getByTestId('stt-restart-gateway')
    fireEvent.click(restart)
    expect(mockApi.restartGateway).not.toHaveBeenCalled()
    expect(restart).toHaveTextContent(/click again to restart/i)

    fireEvent.click(screen.getByTestId('stt-restart-gateway'))
    await waitFor(() => expect(mockApi.restartGateway).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(screen.getByTestId('stt-restart-gateway')).toBeDisabled())
    finish()
  })

  it('offers the restart for the local provider too, which also needs the extra', async () => {
    // The restart follows the pip command rather than the provider: `local` needs
    // the same extra, and the in-dashboard installer that used to cover it is gone.
    mount({
      provider: 'local',
      prereqs: ["/opt/kirocrew/bin/python -m pip install 'boto3>=1.34,<2' 'amazon-transcribe>=0.6,<1'"],
    })
    await loaded()
    expect(screen.getByTestId('stt-restart-gateway')).toBeTruthy()
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

  it('renders no restart hint for an ffmpeg-only prereq list', async () => {
    // The list carries only the ffmpeg command. ffmpeg needs no restart: the PATH
    // probe re-runs on every settings read, so promising one would be busywork.
    mount({ provider: 'transcribe', prereqs: ['sudo apt-get install -y ffmpeg'] })
    await loaded()
    expect(screen.getByText('sudo apt-get install -y ffmpeg')).toBeTruthy()
    expect(screen.queryByText(/restart the gateway/i)).toBeNull()
  })

  it('surfaces the ffmpeg gap for the local provider too', async () => {
    // The availability checks skip ffmpeg for every provider, so the warning
    // is not Transcribe-gated.
    mount({
      provider: 'local',
      available: true,
      ffmpeg_missing: true,
      prereqs: ['sudo apt-get install -y ffmpeg'],
    })
    expect(await screen.findByText(/ffmpeg is missing/i)).toBeTruthy()
  })

  it('tells a packaged desktop user to reinstall instead of installing FFmpeg', async () => {
    mount({
      provider: 'local',
      available: true,
      ffmpeg_missing: true,
      bundled_interpreter: true,
      prereqs: [],
    })
    expect(await screen.findByText(/bundled audio decoder is missing or damaged/i)).toBeTruthy()
    expect(screen.getByText(/reinstall the Kiro Crew desktop app/i)).toBeTruthy()
    expect(screen.queryByText(/run these commands/i)).toBeNull()
  })

  it('shows no ffmpeg warning when ffmpeg is present', async () => {
    mount({ provider: 'transcribe', available: true, ffmpeg_missing: false, prereqs: [] })
    await screen.findByText(/ready/i)
    expect(screen.queryByText(/ffmpeg is missing/i)).toBeNull()
  })

  it('renders no Runtime row for any provider', async () => {
    mount({ provider: 'local' })
    await loaded()
    // The backend never serves `docker_mode`, so the row could only ever
    // read "Native" — it conveys nothing and is gone.
    expect(screen.queryByText(/^runtime$/i)).toBeNull()
  })
})
