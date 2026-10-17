/**
 * Meetings SettingsView — the whole surface, which no other suite touches.
 *
 * Two things here are behaviour rather than rendering, and they are the reason
 * the file is worth more than a smoke test:
 *
 *   1. The backend's PUT is a full validated REPLACE, so every change has to send
 *      the complete config. The payload shape is asserted field-by-field, because
 *      a patch that dropped an unrelated field would silently reset it.
 *   2. Saves are CHAINED and each payload is derived from the CACHE at send time,
 *      not from the render-time snapshot. Two rapid toggles of different agents
 *      therefore both survive; the test drives exactly that race by holding the
 *      first response open while the second toggle is queued.
 *
 * Harness follows MeetingsPage.test.tsx: hoisted `meetingsApi` doubles over the
 * real module, one QueryClientProvider per render. No Redux — this view reads
 * nothing from the store.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const apiMocks = vi.hoisted(() => ({
  config: vi.fn(),
  saveConfig: vi.fn(),
  dictionary: vi.fn(),
  addTerm: vi.fn(),
  removeTerm: vi.fn(),
}))

vi.mock('../apps/meetings/api', async importOriginal => {
  const actual = await importOriginal<typeof import('../apps/meetings/api')>()
  return { ...actual, meetingsApi: apiMocks }
})

import SettingsView from '../apps/meetings/SettingsView'
import type { ConfigResponse, DictionaryTerm, MeetingsConfig } from '../apps/meetings/api'

const BASE_CONFIG: MeetingsConfig = {
  meeting_agents: [
    { id: 'note-taker', name: 'Note Taker', widget_type: 'markdown', enabled_by_default: true },
    { id: 'sketch-artist', name: 'Sketch Artist', widget_type: 'html', enabled_by_default: true },
  ],
  stt_provider: 'local',
  task_provider: 'taskei',
  calendar: { provider: 'ics', source: '/tmp/team.ics' },
  presets: { standup: { enabled_agents: ['note-taker'] } },
  default_preset: 'standup',
  poll_interval_active: 3,
  poll_interval_idle: 30,
}

const REGISTRIES = {
  task_providers: [
    { id: 'taskei', label: 'Taskei' },
    { id: 'github', label: 'GitHub' },
  ],
  calendar_providers: [
    { id: 'none', label: 'No calendar' },
    { id: 'ics', label: 'ICS file', requires_source: true },
  ],
  stt_providers: [{ id: 'local', label: 'Local' }],
}

function configResponse(overrides: Partial<MeetingsConfig> = {}): ConfigResponse {
  return { ...REGISTRIES, config: { ...BASE_CONFIG, ...overrides } }
}

const TERM: DictionaryTerm = { correct: 'Kiro Crew', aliases: ['kiro cru', 'kero crew'] }

function renderView() {
  const notify = vi.fn()
  const onBack = vi.fn()
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  const utils = render(
    <QueryClientProvider client={queryClient}>
      <SettingsView onBack={onBack} notify={notify} />
    </QueryClientProvider>,
  )
  return { ...utils, notify, onBack, queryClient }
}

/** Opens a SimpleSelect by its accessible name and picks an option by label. */
async function pickOption(selectName: string, optionLabel: string) {
  fireEvent.click(await screen.findByRole('combobox', { name: selectName }))
  fireEvent.click(await screen.findByRole('option', { name: optionLabel }))
}

/** The nth payload handed to the save endpoint, typed for field assertions. */
function savedPayload(index: number): MeetingsConfig {
  return apiMocks.saveConfig.mock.calls[index][0] as MeetingsConfig
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.useFakeTimers({ shouldAdvanceTime: true })
  apiMocks.config.mockResolvedValue(configResponse())
  apiMocks.dictionary.mockResolvedValue({ terms: [] })
  apiMocks.saveConfig.mockImplementation((config: MeetingsConfig) => Promise.resolve({ config }))
  apiMocks.addTerm.mockResolvedValue({ terms: [TERM] })
  apiMocks.removeTerm.mockResolvedValue({ terms: [] })
})

afterEach(() => {
  vi.clearAllTimers()
  vi.useRealTimers()
  vi.restoreAllMocks()
})

describe('Meetings SettingsView — rendering', () => {
  it('populates both provider pickers from the backend registries', async () => {
    renderView()

    expect(await screen.findByRole('combobox', { name: 'Task provider' })).toHaveTextContent(
      'Taskei',
    )
    expect(screen.getByRole('combobox', { name: 'Calendar provider' })).toHaveTextContent(
      'ICS file',
    )
    // The labels come from the registry rows, not from the id, so an
    // out-of-repo provider shows its own name with no frontend change.
    fireEvent.click(screen.getByRole('combobox', { name: 'Task provider' }))
    expect(await screen.findByRole('option', { name: 'GitHub' })).toBeInTheDocument()
  })

  it('lists every configured agent with its own default-enabled switch', async () => {
    renderView()

    expect(await screen.findByText('note-taker')).toBeInTheDocument()
    expect(screen.getByText('sketch-artist')).toBeInTheDocument()
    expect(
      screen.getByRole('switch', { name: 'Enable Note Taker by default' }),
    ).toHaveAttribute('aria-checked', 'true')
  })

  it('treats a missing enabled_by_default as enabled and an explicit false as off', async () => {
    apiMocks.config.mockResolvedValue(
      configResponse({
        meeting_agents: [
          { id: 'note-taker', name: 'Note Taker', widget_type: 'markdown' },
          {
            id: 'sketch-artist',
            name: 'Sketch Artist',
            widget_type: 'html',
            enabled_by_default: false,
          },
        ],
      }),
    )
    renderView()

    expect(
      await screen.findByRole('switch', { name: 'Enable Note Taker by default' }),
    ).toHaveAttribute('aria-checked', 'true')
    expect(
      screen.getByRole('switch', { name: 'Enable Sketch Artist by default' }),
    ).toHaveAttribute('aria-checked', 'false')
  })

  it('shows the source field only for a provider that declares requires_source', async () => {
    apiMocks.config.mockResolvedValue(configResponse({ calendar: { provider: 'none', source: '' } }))
    renderView()

    await screen.findByRole('combobox', { name: 'Calendar provider' })
    expect(screen.queryByLabelText('Calendar source')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Save' })).not.toBeInTheDocument()
    expect(
      screen.queryByText('A path to a local .ics file, or an https URL to one.'),
    ).not.toBeInTheDocument()
  })

  it('returns to the meetings list from the header action', async () => {
    const { onBack } = renderView()

    fireEvent.click(await screen.findByRole('button', { name: 'Back' }))
    expect(onBack).toHaveBeenCalledTimes(1)
  })
})

describe('Meetings SettingsView — saving config', () => {
  it('sends the whole config with only the task provider replaced', async () => {
    const { notify } = renderView()

    await pickOption('Task provider', 'GitHub')

    await waitFor(() => expect(apiMocks.saveConfig).toHaveBeenCalledTimes(1))
    expect(savedPayload(0)).toEqual({ ...BASE_CONFIG, task_provider: 'github' })
    await waitFor(() => expect(notify).toHaveBeenCalledWith('Saved.', { type: 'success' }))
  })

  it('keeps the existing calendar source when only the provider changes', async () => {
    renderView()

    await pickOption('Calendar provider', 'No calendar')

    await waitFor(() => expect(apiMocks.saveConfig).toHaveBeenCalledTimes(1))
    // `calendar` is a nested object: the field not being changed has to survive.
    expect(savedPayload(0).calendar).toEqual({ provider: 'none', source: '/tmp/team.ics' })
  })

  it('trims the typed source, saves it, and drops the local draft', async () => {
    renderView()

    const field = await screen.findByLabelText('Calendar source')
    fireEvent.change(field, { target: { value: '  /srv/calendars/team.ics  ' } })
    expect(field).toHaveValue('  /srv/calendars/team.ics  ')

    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(apiMocks.saveConfig).toHaveBeenCalledTimes(1))
    expect(savedPayload(0).calendar).toEqual({
      provider: 'ics',
      source: '/srv/calendars/team.ics',
    })
    // Draft cleared, so the field now mirrors what the server accepted.
    await waitFor(() => expect(field).toHaveValue('/srv/calendars/team.ics'))
  })

  it('reports a failed save and leaves the chain usable for the next change', async () => {
    const { notify } = renderView()
    apiMocks.saveConfig.mockRejectedValueOnce(new Error('boom'))

    await pickOption('Task provider', 'GitHub')

    await waitFor(() =>
      expect(notify).toHaveBeenCalledWith('Could not save the settings.', { type: 'error' }),
    )

    // A rejected save must not wedge the queue: the next change still sends.
    fireEvent.click(screen.getByRole('switch', { name: 'Enable Note Taker by default' }))
    await waitFor(() => expect(apiMocks.saveConfig).toHaveBeenCalledTimes(2))
  })

  it('rewrites only the toggled agent and leaves the rest of the roster alone', async () => {
    renderView()

    fireEvent.click(
      await screen.findByRole('switch', { name: 'Enable Sketch Artist by default' }),
    )

    await waitFor(() => expect(apiMocks.saveConfig).toHaveBeenCalledTimes(1))
    expect(savedPayload(0).meeting_agents).toEqual([
      { id: 'note-taker', name: 'Note Taker', widget_type: 'markdown', enabled_by_default: true },
      {
        id: 'sketch-artist',
        name: 'Sketch Artist',
        widget_type: 'html',
        enabled_by_default: false,
      },
    ])
  })

  it('derives a queued save from the config the previous save landed', async () => {
    renderView()

    // Hold the first response open so the second toggle is queued while the
    // first request is still in flight — the race the chain exists for.
    const held: Array<{ resolve: () => void }> = []
    apiMocks.saveConfig.mockImplementationOnce(
      (config: MeetingsConfig) =>
        new Promise<{ config: MeetingsConfig }>(resolve => {
          held.push({ resolve: () => resolve({ config }) })
        }),
    )

    fireEvent.click(await screen.findByRole('switch', { name: 'Enable Note Taker by default' }))
    await waitFor(() => expect(held).toHaveLength(1))
    fireEvent.click(screen.getByRole('switch', { name: 'Enable Sketch Artist by default' }))
    expect(apiMocks.saveConfig).toHaveBeenCalledTimes(1)

    await act(async () => {
      held[0].resolve()
    })

    await waitFor(() => expect(apiMocks.saveConfig).toHaveBeenCalledTimes(2))
    // Both toggles survive: the second payload was built from the first
    // response, not from the render-time snapshot.
    expect(savedPayload(1).meeting_agents.map(agent => [agent.id, agent.enabled_by_default])).toEqual(
      [
        ['note-taker', false],
        ['sketch-artist', false],
      ],
    )
  })
})

describe('Meetings SettingsView — speech dictionary', () => {
  it('offers the empty state until a correction exists', async () => {
    renderView()

    expect(await screen.findByTestId('empty-state-title')).toHaveTextContent('No corrections yet')
  })

  it('refuses a correction that is missing either half', async () => {
    const { notify } = renderView()

    fireEvent.click(await screen.findByRole('button', { name: 'Add' }))
    expect(notify).toHaveBeenCalledWith('Enter what was heard and what it should say.', {
      type: 'error',
    })
    expect(apiMocks.addTerm).not.toHaveBeenCalled()

    // Aliases alone is still incomplete.
    fireEvent.change(screen.getByLabelText('Misheard forms'), { target: { value: 'kiro cru' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add' }))
    expect(apiMocks.addTerm).not.toHaveBeenCalled()
    expect(notify).toHaveBeenCalledTimes(2)
  })

  it('splits the aliases on commas, submits on Enter, and clears both fields', async () => {
    renderView()

    const aliases = await screen.findByLabelText('Misheard forms')
    const correct = screen.getByLabelText('Correct term')
    fireEvent.change(aliases, { target: { value: ' kiro cru , , kero crew ' } })
    fireEvent.change(correct, { target: { value: '  Kiro Crew  ' } })
    fireEvent.keyDown(correct, { key: 'Enter' })

    await waitFor(() =>
      expect(apiMocks.addTerm).toHaveBeenCalledWith('Kiro Crew', ['kiro cru', 'kero crew']),
    )
    await waitFor(() => expect(aliases).toHaveValue(''))
    expect(correct).toHaveValue('')
    // The response replaced the cached list, so the new row renders.
    expect(
      await screen.findByRole('button', { name: 'Remove Kiro Crew' }, { timeout: 5_000 }),
    ).toBeInTheDocument()
  })

  it('ignores every key other than Enter in the correct-term field', async () => {
    renderView()

    const correct = await screen.findByLabelText('Correct term')
    fireEvent.change(screen.getByLabelText('Misheard forms'), { target: { value: 'kiro cru' } })
    fireEvent.change(correct, { target: { value: 'Kiro Crew' } })
    fireEvent.keyDown(correct, { key: 'a' })

    expect(apiMocks.addTerm).not.toHaveBeenCalled()
  })

  it('reports a correction the backend rejected', async () => {
    apiMocks.addTerm.mockRejectedValueOnce(new Error('nope'))
    const { notify } = renderView()

    fireEvent.change(await screen.findByLabelText('Misheard forms'), {
      target: { value: 'kiro cru' },
    })
    fireEvent.change(screen.getByLabelText('Correct term'), { target: { value: 'Kiro Crew' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add' }))

    await waitFor(
      () =>
        expect(notify).toHaveBeenCalledWith('Could not add that correction.', { type: 'error' }),
      { timeout: 5_000 },
    )
  })

  it('removes a correction and falls back to the empty state', async () => {
    apiMocks.dictionary.mockResolvedValue({ terms: [TERM] })
    renderView()

    expect(await screen.findByText('kiro cru, kero crew')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Remove Kiro Crew' }))

    await waitFor(() => expect(apiMocks.removeTerm).toHaveBeenCalledWith('Kiro Crew'))
    expect(await screen.findByTestId('empty-state-title')).toHaveTextContent('No corrections yet')
  })
})
