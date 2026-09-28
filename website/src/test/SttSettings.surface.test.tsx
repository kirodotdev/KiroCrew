/**
 * The Voice panel's two-level shape, and the disclosure primitive under it.
 *
 * The panel used to render about nineteen equally-weighted rows, each with its own
 * one-to-three lines of explanation. Nothing was broken and nothing was missing --
 * that WAS the problem: a reader had no way to tell which rows carried a decision
 * from which ones carried a default that already worked.
 *
 * So the contract these tests pin is a shape, not a behaviour: the surface holds
 * only the decisions a user has to make, and everything else is reachable but not
 * spending their attention. A regression here does not throw or misread -- it just
 * quietly puts a knob back on the surface, which is exactly the kind of change a
 * diff review waves through.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'

import { store } from '../store'
import { initI18n } from '../i18n'
import { SettingsSection } from '../components/settings'
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
    endpointing: true,
    dictation_panel: true,
    language_code: 'en-US',
    providers: ['local', 'transcribe'],
    streaming_providers: ['local'],
    language_codes: ['auto', 'en-US'],
    prereqs: [],
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
    backend: {
      name: 'cpu',
      accelerated: false,
      encoder_only: false,
      detail: 'NEON',
      cpu_features: ['NEON'],
      requested: 'auto',
      honoured: true,
      threads: 8,
    },
    timings: { loads: 1, hashes: 1, last_load: null, last_final: null, partials: 0, partials_aborted: 0, decodes: [] },
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <Provider store={store}>
      <QueryClientProvider client={qc}><SttSettings /></QueryClientProvider>
    </Provider>,
  )
}

describe('SettingsSection disclosure', () => {
  afterEach(() => cleanup())

  it('renders its rows immediately when it is a plain heading', () => {
    // The regression guard for the other half of the prop: a section that never
    // asked to collapse must not start hidden, or every existing settings tab
    // loses its content at once.
    render(<SettingsSection title="Plain"><p>inside</p></SettingsSection>)
    expect(screen.getByText('inside')).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Plain' })).toBeNull()
  })

  it('starts closed and keeps its rows out of the DOM until asked', () => {
    render(<SettingsSection title="Group" collapsible><p>inside</p></SettingsSection>)
    const header = screen.getByRole('button', { name: 'Group' })
    expect(header.getAttribute('aria-expanded')).toBe('false')
    // Unmounted, not merely invisible: a hidden subtree still costs a
    // screen-reader user their place, which defeats the point of collapsing it.
    expect(screen.queryByText('inside')).toBeNull()

    fireEvent.click(header)
    expect(header.getAttribute('aria-expanded')).toBe('true')
    expect(screen.getByText('inside')).toBeTruthy()

    fireEvent.click(header)
    expect(screen.queryByText('inside')).toBeNull()
  })

  it('names the group as a heading, so the document outline is unchanged', () => {
    render(<SettingsSection title="Group" collapsible><p>inside</p></SettingsSection>)
    expect(screen.getByRole('heading', { name: 'Group' })).toBeTruthy()
  })
})

describe('Voice panel keeps only the necessary decisions on its surface', () => {
  beforeEach(async () => {
    vi.clearAllMocks()
    await initI18n('en')
    Object.defineProperty(navigator, 'mediaDevices', {
      configurable: true,
      value: { enumerateDevices: async () => [] },
    })
  })
  afterEach(() => cleanup())

  it('shows the six decisions and nothing else', async () => {
    mountPanel()
    await waitFor(() => expect(screen.getByRole('combobox', { name: /model/i })).toBeTruthy())

    // On the surface: what a user came here to decide.
    for (const name of [/microphone/i, /provider/i, /model/i, /language/i]) {
      expect(screen.getByRole('combobox', { name })).toBeTruthy()
    }
    expect(screen.getByText('Enabled')).toBeTruthy()
    expect(screen.getByText('Tidy up transcripts with AI')).toBeTruthy()

    // Behind the disclosure: real, adjustable, and defaulted. Asserted absent by
    // LABEL, because that is what a row costs a reader even when its explanation
    // has already moved into a tip.
    expect(screen.queryByText('Streaming')).toBeNull()
    expect(screen.queryByText('Dictation panel')).toBeNull()
    expect(screen.queryByText('Shortcut key')).toBeNull()
  })

  it('offers no millisecond dial anywhere, on the surface or under it', async () => {
    mountPanel()
    await waitFor(() => expect(screen.getByRole('button', { name: /fine-tuning/i })).toBeTruthy())
    fireEvent.click(screen.getByRole('button', { name: /fine-tuning/i }))

    // Both duration steppers are gone by measurement, not by taste: a decode costs
    // a large fixed amount plus a small term in the audio length, so the cadence a
    // user could dial was unreachable, and nobody can tell 700 ms from 750 ms by
    // feel. Both keys are still honoured from config.json.
    expect(screen.queryByText(/pause that ends a phrase/i)).toBeNull()
    expect(screen.queryByText(/live transcript refresh/i)).toBeNull()
  })

  it('states the acceleration beside Status rather than in a section of its own', async () => {
    mountPanel()
    // The one engine reading that changes a decision. The threads and the last
    // decode's cost answer "why is it slow", a question you go looking for, so they
    // are in the tip rather than on three labelled rows.
    // The MEANING, not the old label. A blind reader on "CPU only" said "I don't
    // really know what it means for me, whether it's good or bad", so the badge now
    // carries the explanation instead of deferring it to a hover target.
    await waitFor(() =>
      expect(screen.getByText('Runs on CPU — no GPU in this build')).toBeTruthy()
    )
    expect(screen.queryByText(/^Acceleration$/)).toBeNull()
    expect(screen.queryByText(/^Decode threads$/)).toBeNull()
  })

  it('reaches the key binding through its own group, two levels down', async () => {
    mountPanel()
    await waitFor(() => expect(screen.getByRole('button', { name: /fine-tuning/i })).toBeTruthy())
    expect(screen.queryByRole('button', { name: /start dictation with a key/i })).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: /fine-tuning/i }))
    const ptt = await screen.findByRole('button', { name: /start dictation with a key/i })
    expect(screen.queryByText('Shortcut key')).toBeNull()

    fireEvent.click(ptt)
    expect(await screen.findByText('Shortcut key')).toBeTruthy()
  })
})
