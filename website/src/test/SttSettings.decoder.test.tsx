/**
 * The audio-decoder block on Settings > Voice, and the agent hand-off behind it.
 *
 * This surface replaced a dead end: a source install with no system ffmpeg was
 * shown shell commands, and on a distribution that packages no ffmpeg the command
 * was an `echo` of a URL. So what is pinned here is that each backend state gets
 * the ONE affordance that can actually resolve it — a fetch when the platform has
 * a pinned executable, the manual commands only when it does not, and a hand-off
 * to an agent session when the fetch itself failed — and that the hand-off carries
 * the facts an agent needs instead of the sentence on screen.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { store } from '../store'
import { initI18n } from '../i18n'
import SttSettings from '../pages/settings/SttSettings'
import { api } from '../api/client'
import { decoderRepairPrompt } from '../lib/sttProviders'
import { consumeChatHandoff, __resetNavSeamForTests } from '../utils/errorReport'

vi.mock('../api/client', () => ({
  api: {
    sttConfig: vi.fn(),
    saveSttConfig: vi.fn(),
    sttStatus: vi.fn(),
    sttPrepare: vi.fn(),
    sttFfmpegDownload: vi.fn(),
    restartGateway: vi.fn(),
  },
}))

const mockApi = api as unknown as {
  sttConfig: ReturnType<typeof vi.fn>
  sttStatus: ReturnType<typeof vi.fn>
  sttFfmpegDownload: ReturnType<typeof vi.fn>
}

/** The real linux-x86_64 wheel size, so the progress figures are the true ones. */
const WHEEL_BYTES = 29_498_237

const IDLE_DOWNLOAD = {
  stage: 'idle',
  artifact: '',
  downloaded_bytes: 0,
  total_bytes: 0,
  error_code: '',
  error_detail: '',
}

function mount(opts: {
  ffmpeg?: Record<string, unknown> | null
  prereqs?: string[]
  bundled?: boolean
} = {}) {
  mockApi.sttConfig.mockResolvedValue({
    enabled: true,
    provider: 'local',
    model: 'base',
    available: true,
    streaming: false,
    providers: ['local', 'transcribe'],
    streaming_providers: ['local', 'transcribe'],
    language_codes: ['en-US'],
    prereqs: opts.prereqs ?? [],
    bundled_interpreter: opts.bundled ?? false,
    ffmpeg_missing: !(opts.ffmpeg?.present ?? false),
  })
  mockApi.sttStatus.mockResolvedValue({
    available: true,
    code: '',
    detail: '',
    models: [{ name: 'base', size_bytes: 147_951_465, present: true }],
    download: { step: 'idle', model: '', downloaded_bytes: 0, total_bytes: 0, error: '' },
    ffmpeg: opts.ffmpeg === null ? undefined : {
      present: false,
      source: null,
      auto_fetch: 'available',
      os: 'Linux',
      arch: 'x86_64',
      download: IDLE_DOWNLOAD,
      ...(opts.ffmpeg ?? {}),
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

const missingNotice = () => screen.findByText(/ffmpeg is missing/i)

describe('SttSettings audio decoder', () => {
  beforeEach(async () => {
    vi.clearAllMocks()
    await initI18n('en')
    __resetNavSeamForTests()
    sessionStorage.clear()
    Object.defineProperty(navigator, 'mediaDevices', {
      configurable: true,
      value: { enumerateDevices: async () => [] },
    })
  })
  afterEach(() => cleanup())

  it('offers a one-click fetch when the platform has a pinned decoder', async () => {
    mount()
    await missingNotice()
    const button = screen.getByRole('button', { name: /download decoder/i })
    fireEvent.click(button)
    await waitFor(() => expect(mockApi.sttFfmpegDownload).toHaveBeenCalledTimes(1))
  })

  it('says nothing about the decoder once one resolves', async () => {
    mount({ ffmpeg: { present: true, source: 'store' } })
    // The model picker proves the panel rendered, so the absence below is a real
    // absence rather than a page that had not loaded yet.
    await waitFor(() => expect(screen.getByRole('combobox', { name: /model/i })).toBeTruthy())
    expect(screen.queryByText(/ffmpeg is missing/i)).toBeNull()
    expect(screen.queryByRole('button', { name: /download decoder/i })).toBeNull()
  })

  it('shows byte progress while the decoder is being fetched', async () => {
    mount({
      ffmpeg: {
        download: {
          ...IDLE_DOWNLOAD,
          stage: 'downloading',
          artifact: 'ffmpeg-linux-x86_64-v7.0.2',
          downloaded_bytes: WHEEL_BYTES / 2,
          total_bytes: WHEEL_BYTES,
        },
      },
    })
    await missingNotice()
    // Names the DECODER, not the speech model: both transfers can run at once, so
    // a bar that borrowed the model's sentence would attribute the wrong one.
    const bar = await screen.findByText(/downloading the audio decoder/i)
    expect(bar.textContent).toMatch(/50\s*%/)
    // A progress bar replaces the button; a second press would be a second task.
    expect(screen.queryByRole('button', { name: /download decoder/i })).toBeNull()
  })

  it('falls back to the manual commands only where nothing is pinned to fetch', async () => {
    mount({
      ffmpeg: { auto_fetch: 'unsupported' },
      prereqs: ['sudo apt-get install -y ffmpeg'],
    })
    await missingNotice()
    expect(screen.getByText('sudo apt-get install -y ffmpeg')).toBeTruthy()
    expect(screen.queryByRole('button', { name: /download decoder/i })).toBeNull()
  })

  it('shows no command at all when the platform has none to offer', async () => {
    // The `echo 'Build ffmpeg from source: …'` fallback is gone: a command whose
    // only effect is printing a URL is a dead end wearing the costume of an
    // instruction, and the backend now answers with an empty list instead.
    mount({ ffmpeg: { auto_fetch: 'unsupported' }, prereqs: [] })
    await missingNotice()
    expect(screen.queryByText(/Build ffmpeg from source/i)).toBeNull()
  })

  it('hands a failed fetch to an agent session instead of to the user', async () => {
    mount({
      ffmpeg: {
        download: {
          ...IDLE_DOWNLOAD,
          stage: 'failed',
          artifact: 'ffmpeg-linux-x86_64-v7.0.2',
          error_code: 'decoder_wheel_unverified',
          error_detail: 'imageio_ffmpeg-0.6.0-py3-none-manylinux2014_x86_64.whl: sha256 mismatch',
        },
      },
    })
    await missingNotice()
    expect(screen.getByText(/sha256 mismatch/i)).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: /let Kiro Crew fix it/i }))

    // Staged for the composer, not sent: the user reads it and presses send.
    const staged = consumeChatHandoff()
    expect(staged).not.toBeNull()
    const prompt = staged as string
    expect(prompt).toContain('decoder_wheel_unverified')
    expect(prompt).toContain('sha256 mismatch')
    // The GATEWAY's platform, which the browser cannot know on its own.
    expect(prompt).toContain('Linux x86_64')
  })

  it('never offers a fetch inside a desktop release', async () => {
    // A bundled app carries its own authenticated decoder and its resolver looks
    // nowhere else, so the only supported repair is reinstalling the app.
    mount({ bundled: true, ffmpeg: { auto_fetch: 'bundled' } })
    await waitFor(() =>
      expect(screen.getByText(/bundled audio decoder is missing or damaged/i)).toBeTruthy(),
    )
    expect(screen.queryByRole('button', { name: /download decoder/i })).toBeNull()
    expect(screen.queryByText(/ffmpeg is missing/i)).toBeNull()
  })

  it('renders nothing about the decoder before the status probe has answered', async () => {
    // Claiming a decoder is missing while the probe is still running would offer a
    // download on a host that already has one.
    mount({ ffmpeg: null })
    await waitFor(() => expect(screen.getByRole('combobox', { name: /model/i })).toBeTruthy())
    expect(screen.queryByText(/ffmpeg is missing/i)).toBeNull()
  })
})

describe('decoderRepairPrompt', () => {
  beforeEach(async () => {
    await initI18n('en')
  })

  it('carries the failure, the host, the trusted locations and the way to re-check', () => {
    const prompt = decoderRepairPrompt({
      code: 'decoder_member_missing',
      detail: 'the wheel carries no imageio_ffmpeg/binaries/ffmpeg-linux-x86_64-v7.0.2',
      os: 'Linux',
      arch: 'aarch64',
    })
    expect(prompt).toContain('decoder_member_missing')
    expect(prompt).toContain('the wheel carries no imageio_ffmpeg/binaries/ffmpeg-linux-x86_64-v7.0.2')
    expect(prompt).toContain('Linux aarch64')
    // The three trusted locations, and the one that is deliberately NOT trusted —
    // an agent that installs into ~/.local/bin will have done nothing at all.
    expect(prompt).toContain('/usr/local/bin')
    expect(prompt).toContain('/opt/homebrew/bin')
    expect(prompt).toContain('<data home>/models/ffmpeg/')
    expect(prompt).toContain('~/.local/bin')
    expect(prompt).toMatch(/NOT trusted/)
    // The digest is the anchor, not the path.
    expect(prompt).toContain('SHA-256')
    // How to know it worked.
    expect(prompt).toContain('GET /api/stt/status')
    // No sudo handed over as a silent instruction; the agent decides.
    expect(prompt).not.toMatch(/\bsudo\b/)
  })

  it('substitutes a readable phrase when the backend reported no code or detail', () => {
    const prompt = decoderRepairPrompt({ code: '', detail: '', os: 'Linux', arch: 'x86_64' })
    // An empty interpolation would leave a dangling "What failed:  — ".
    expect(prompt).toContain('not reported')
    expect(prompt).not.toMatch(/What failed: {2}/)
  })
})
