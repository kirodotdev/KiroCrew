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
import { MemoryRouter } from 'react-router-dom'
import { SettingsSection, SettingsToggle } from '../components/settings'
import { useSettingHighlight } from '../hooks/useSettingHighlight'
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

/**
 * The same panel, but reached the way a deep link reaches it: under a router
 * carrying the url, with the REAL `useSettingHighlight` mounted above it.
 *
 * The Voice tab is the surface that makes ownership testable, because it holds TWO
 * collapsible groups and one is NESTED inside the other -- `PushToTalkConfig`'s
 * "Start dictation with a key" renders as the last child of "Fine-tuning". A reveal
 * that is not scoped to the group actually holding the target opens both.
 */
async function mountPanelAt(entry: string, over: Record<string, unknown> = {}) {
  // Seed BOTH reads and freeze them, so the panel commits its rows on the first
  // render rather than one round-trip later. That is not a convenience: the probe
  // strips its own parameter 100 ms after it mounts, so a panel that arrives after
  // that window makes every assertion here a statement about load latency instead
  // of about ownership. Seeding pins the ordering the defect lives in -- the group
  // opens, its children mount, and the signal is still up when they do.
  const view = mountPanel(over)
  view.unmount()
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity } },
  })
  qc.setQueryData(['sttConfig'], await mockApi.sttConfig())
  qc.setQueryData(['sttStatus'], await mockApi.sttStatus())
  function Probe() {
    useSettingHighlight()
    return <SttSettings />
  }
  return render(
    <Provider store={store}>
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={[entry]}><Probe /></MemoryRouter>
      </QueryClientProvider>
    </Provider>,
  )
}

describe('SettingsSection disclosure', () => {
  afterEach(() => cleanup())

  /* A collapsed group is not merely hidden -- it is ABSENT from the document that
   * a settings deep link searches. `useSettingHighlight` resolves
   * `/settings/<tab>?highlight=<id>` by querying the DOM for the row's
   * `data-setting-label` / `data-setting-key`, so a group that renders no rows
   * makes its settings unreachable from the command palette and from every
   * `SettingRef` chip. Six of the Voice tab's twenty-six registry entries sit
   * inside one. */
  function Probe({ children }: { children: React.ReactNode }) {
    useSettingHighlight()
    return <>{children}</>
  }

  /** The row the deep links below name, with the `data-setting-key` that makes it
   *  findable. `Streaming` is `stt.streaming` in the registry. */
  function TargetRow() {
    return <SettingsToggle label="Streaming" checked onChange={() => {}} configKey="stt.streaming" />
  }

  it('reveals the group holding the target, and only that group', async () => {
    // Two SIBLING groups, one holding the row the link names. A reveal driven by
    // "a link is pending" rather than by WHICH row it wants opens both, which
    // trades the hidden target for unrelated groups left standing open.
    render(
      <MemoryRouter initialEntries={['/settings/voice?highlight=voice.streaming']}>
        <Probe>
          <SettingsSection title="Owner" collapsible><TargetRow /></SettingsSection>
          <SettingsSection title="Bystander" collapsible><p>elsewhere</p></SettingsSection>
        </Probe>
      </MemoryRouter>,
    )

    await waitFor(() => expect(screen.getByText('Streaming')).toBeTruthy())
    expect(screen.getByRole('button', { name: 'Owner' }).getAttribute('aria-expanded')).toBe('true')
    expect(screen.queryByText('elsewhere')).toBeNull()
    expect(screen.getByRole('button', { name: 'Bystander' }).getAttribute('aria-expanded')).toBe('false')
  })

  it('reveals a NESTED group only when the target is inside it', async () => {
    // The shape the Voice tab actually has: one collapsible group as the last child
    // of another. The outer one must open either way -- the inner cannot exist
    // otherwise -- and the inner must answer for itself.
    render(
      <MemoryRouter initialEntries={['/settings/voice?highlight=voice.streaming']}>
        <Probe>
          <SettingsSection title="Outer" collapsible>
            <TargetRow />
            <SettingsSection title="Inner" collapsible><p>nested</p></SettingsSection>
          </SettingsSection>
        </Probe>
      </MemoryRouter>,
    )

    await waitFor(() => expect(screen.getByText('Streaming')).toBeTruthy())
    expect(screen.queryByText('nested')).toBeNull()
  })

  it('keeps the owning group open once the link has finished looking', async () => {
    // The probe strips its own parameter the moment it has rung the row, so a group
    // whose openness merely MIRRORED the signal would close on that same tick and
    // take the ringed row off screen. Latched for the owner; a group mounted after
    // the withdrawal gets nothing.
    render(
      <MemoryRouter initialEntries={['/settings/voice?highlight=voice.streaming']}>
        <Probe>
          <SettingsSection title="Owner" collapsible><TargetRow /></SettingsSection>
        </Probe>
      </MemoryRouter>,
    )
    await waitFor(() => expect(screen.getByText('Streaming')).toBeTruthy())

    // Past the probe's 100 ms tick. The first tree stays MOUNTED on purpose, so its
    // unmount cleanup cannot be what clears the signal: only the withdrawal can.
    await new Promise(r => setTimeout(r, 250))
    render(<SettingsSection title="Later" collapsible><p>later</p></SettingsSection>)

    expect(screen.queryByText('later')).toBeNull()
    expect(screen.getByText('Streaming')).toBeTruthy()
  })

  it('reveals nothing for an id the registry does not know', async () => {
    // The unknown-id branch strips the parameter and returns WITHOUT a cleanup, so
    // it is the one path where the withdrawal cannot ride the effect teardown. It
    // also resolves to no selector at all, so no group has anything to answer for.
    render(
      <MemoryRouter initialEntries={['/settings/voice?highlight=voice.no-such-row']}>
        <Probe>
          <SettingsSection title="Owner" collapsible><TargetRow /></SettingsSection>
        </Probe>
      </MemoryRouter>,
    )
    await new Promise(r => setTimeout(r, 250))

    expect(screen.queryByText('Streaming')).toBeNull()
    render(<SettingsSection title="Later" collapsible><p>later</p></SettingsSection>)
    expect(screen.queryByText('later')).toBeNull()
  })

  it.each([
    ['a registry id', '/settings/voice?highlight=voice.streaming'],
    ['a config key', '/settings/voice?highlight=key:stt.streaming'],
  ])('withdraws when the page is torn down mid-probe (%s)', async (_name, entry) => {
    // Leaving Settings before the probe finishes is the ordinary way out of it, and
    // the two url forms take DIFFERENT branches -- a config key waits on a mutation
    // observer, a registry id on a timer -- so each one has its own teardown.
    const view = render(
      <MemoryRouter initialEntries={[entry]}>
        <Probe>
          <SettingsSection title="Owner" collapsible><TargetRow /></SettingsSection>
        </Probe>
      </MemoryRouter>,
    )
    await waitFor(() => expect(screen.getByText('Streaming')).toBeTruthy())
    view.unmount()

    render(<SettingsSection title="Later" collapsible><p>later</p></SettingsSection>)
    expect(screen.queryByText('later')).toBeNull()
  })

  it('stays closed when the navigation carries no deep link', () => {
    // The other half, or the fix would just be "never collapse": an ordinary visit
    // to the tab must still cost the reader nothing.
    render(
      <MemoryRouter initialEntries={['/settings/voice']}>
        <Probe>
          <SettingsSection title="Group" collapsible><p>inside</p></SettingsSection>
        </Probe>
      </MemoryRouter>,
    )
    expect(screen.queryByText('inside')).toBeNull()
  })

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

describe('a settings deep link reveals the group that OWNS its target', () => {
  beforeEach(async () => {
    vi.clearAllMocks()
    await initI18n('en')
    Object.defineProperty(navigator, 'mediaDevices', {
      configurable: true,
      value: { enumerateDevices: async () => [] },
    })
  })
  afterEach(() => cleanup())

  it('opens Fine-tuning for Streaming and leaves Push-to-talk closed', async () => {
    // The real Voice tab, reached the way the command palette reaches it. `Streaming`
    // lives directly in "Fine-tuning"; "Start dictation with a key" is a SEPARATE
    // collapsible group nested inside it and owns none of this. Revealing it too
    // trades "the target stays hidden" for "unrelated groups open and stay open",
    // which is a different wrong answer rather than a fix.
    await mountPanelAt('/settings/voice?highlight=voice.streaming')

    // The owner opened and the target is on the page.
    await waitFor(() => expect(screen.getByText('Streaming')).toBeTruthy())

    // The unrelated group did not. Asserted by the LABEL of a row only it holds,
    // which is what a reader actually pays for, and on a row the registry lists
    // under its own id (`voice.shortcut-key`).
    expect(screen.queryByText('Shortcut key')).toBeNull()
    expect(screen.queryByText('How the key works')).toBeNull()
    expect(
      screen.getByRole('button', { name: /start dictation with a key/i }).getAttribute('aria-expanded'),
    ).toBe('false')
  })

  it('opens Push-to-talk when the target is the one IT owns', async () => {
    // The mirror image, so the first case cannot be satisfied by never revealing a
    // nested group: `Shortcut key` is inside "Start dictation with a key", which is
    // inside "Fine-tuning", and a deep link to it has to open BOTH.
    await mountPanelAt('/settings/voice?highlight=voice.shortcut-key')

    await waitFor(() => expect(screen.getByText('Shortcut key')).toBeTruthy())
    expect(screen.getByText('Streaming')).toBeTruthy()
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
