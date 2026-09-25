/**
 * Telemetry Spend: the first screen answers where the credits went.
 *
 * The tab's whole job is one question — which sessions, which origins, which
 * models took the window's credits — and a 45-row table cannot answer it: the
 * five summary figures sat below the rows, and the origin and model splits were
 * reachable only by a group-by control that REPLACED the table, so no single
 * screen ever held both. This suite pins the shape that does answer it: the
 * headline figures first, both splits visible at once, the table available but
 * folded away.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

import TelemetryPanel from '../pages/TelemetryPanel'

const convo = (over: Record<string, unknown> = {}) => ({
  slot: 'chat-1-1700000000',
  category: 'dashboard',
  channel: 'dashboard',
  title: 'A named conversation',
  credits: 100,
  turns: 10,
  peak_pct: 50,
  span_days: 1,
  first_ts: 1700000000,
  growth_pct_per_turn: 2,
  turns_to_compaction: 20,
  ...over,
})

const row = (name: string, credits: number) => ({
  name,
  credits,
  turns: 10,
  per_turn: credits / 10,
  share_pct: credits / 10,
  delta_pct: null,
})

const cost = (over: Record<string, unknown> = {}) => ({
  window_days: 7,
  credits: 1000,
  turns: 100,
  per_turn: 10,
  prior_credits: 500,
  prior_turns: 50,
  prior_per_turn: 8,
  delta_pct: 100,
  priciest: { credits: 90, slot: 'chat-1-1700000000', ts: '2026-08-05' },
  by_model: [row('opus-5', 800)],
  by_channel: [row('dashboard', 900)],
  by_category: [row('dashboard', 900)],
  context_bands: [],
  conversations: [convo()],
  conversation_count: 1,
  navigable_category: 'dashboard',
  ...over,
})

const payload = (over: Record<string, unknown> = {}) => ({
  enabled: true,
  window_days: 7,
  shard_count: 1,
  metrics_dir: '/metrics',
  startup: null,
  turn: null,
  context: null,
  other: [],
  cost: cost(over),
})

vi.mock('../api/client', () => ({ api: { telemetryStartup: vi.fn() } }))

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
const Wrapper = ({ children }: { children: React.ReactNode }) => (
  <QueryClientProvider client={qc}>
    <MemoryRouter>{children}</MemoryRouter>
  </QueryClientProvider>
)

async function mount(over: Record<string, unknown> = {}) {
  const { api } = await import('../api/client')
  vi.mocked(api.telemetryStartup).mockResolvedValue(payload(over) as never)
  return render(<TelemetryPanel />, { wrapper: Wrapper })
}

/** The disclosure that holds the table, found by its label rather than position. */
const tableToggle = () => screen.getByRole('button', { name: /Full table/ })

beforeEach(() => {
  vi.clearAllMocks()
  qc.clear()
  localStorage.clear()
})

describe('TelemetryPanel — the spend tab opens on the answer', () => {
  it('puts the headline figures above the table, not below it', async () => {
    await mount()
    await waitFor(() => expect(tableToggle()).toBeInTheDocument())
    // Document order is the reading order: every KPI label precedes the
    // disclosure that holds the rows. With the numbers underneath a 45-row
    // table, the first screen carried rows and nothing else.
    const labels = ['Credits', 'Per turn', 'Turns', 'Priciest turn']
    for (const label of labels) {
      const node = screen.getAllByText(label)[0]
      expect(
        node.compareDocumentPosition(tableToggle()) & Node.DOCUMENT_POSITION_FOLLOWING,
      ).toBeTruthy()
    }
  })

  it('carries the change on the total it describes', async () => {
    await mount()
    // The delta reads as a property of the credits figure rather than as a
    // fifth unrelated tile, and the label leads so the no-prior-spend case is
    // still a sentence.
    await waitFor(() => expect(screen.getByText(/vs previous period/)).toBeInTheDocument())
    expect(screen.getByText('+100%')).toBeInTheDocument()
  })

  it('introduces the prior pair with its period before naming its figures', async () => {
    await mount()
    // The tile's own label is "Credits", so the prior pair inherits no period
    // sense from it. Directly under the current total, "credits 500 · turns 50"
    // reads as a component of the 1,000 above it — the qualifier has to come
    // first. Order, not mere presence: both strings render either way.
    const qualifier = await screen.findByText(/vs previous period/)
    const pair = screen.getByText('credits 500 · turns 50')
    expect(
      qualifier.compareDocumentPosition(pair) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy()
  })

  it('states the absence of a prior window instead of a percentage', async () => {
    await mount({ delta_pct: null })
    await waitFor(() => expect(screen.getByText('no prior spend')).toBeInTheDocument())
    expect(screen.queryByText(/vs previous period/)).toBeNull()
  })

  it('shows the origin and model splits at the same time', async () => {
    await mount({
      by_category: [row('dashboard', 900), row('bg', 100)],
      by_model: [row('opus-5', 700), row('haiku-9', 300)],
    })
    // Both questions on one screen. As a group-by over a single table these were
    // mutually exclusive: reading the origin split meant giving up the model one.
    await waitFor(() => expect(screen.getByText('Credits by origin')).toBeInTheDocument())
    expect(screen.getByText('Credits by model')).toBeInTheDocument()
    expect(screen.getByText('all background')).toBeInTheDocument()
    expect(screen.getByText('haiku-9')).toBeInTheDocument()
    // The widths as values, on the renderer these blocks share with the Startup
    // tab's distribution. The scale is per block, which is the point: 100 credits
    // beside a 900 peak is a ninth of the track, while 300 beside a 700 peak is
    // over two fifths, and one shared axis would have flattened the smaller block.
    const widthOf = (label: string) => {
      const row = screen.getByText(label).closest('div.items-center') as HTMLElement
      const fill = row.querySelector('span[style*="width"]') as HTMLElement
      return parseFloat(fill.style.width)
    }
    expect(widthOf('dashboard')).toBe(100)
    expect(widthOf('all background')).toBeCloseTo(11.111, 3)
    expect(widthOf('opus-5')).toBe(100)
    expect(widthOf('haiku-9')).toBeCloseTo(42.857, 3)
  })

  it('keeps the table out of the first screen until it is asked for', async () => {
    await mount()
    await waitFor(() => expect(tableToggle()).toBeInTheDocument())
    expect(tableToggle()).toHaveAttribute('aria-expanded', 'false')
    expect(document.querySelector('table')).toBeNull()

    await userEvent.click(tableToggle())
    await waitFor(() => expect(document.querySelector('table')).not.toBeNull())
    expect(tableToggle()).toHaveAttribute('aria-expanded', 'true')
    // The group-by control and the drilldown expander come with it: the table is
    // moved, not reduced.
    expect(screen.getByRole('button', { name: 'Model' })).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: 'Show per-turn detail' }).length).toBeGreaterThan(0)
  })

  it('reopens the table for a reader who left it open', async () => {
    localStorage.setItem('telemetry:spend-table-open', '1')
    await mount()
    await waitFor(() => expect(document.querySelector('table')).not.toBeNull())
    expect(tableToggle()).toHaveAttribute('aria-expanded', 'true')
  })

  it('says how many rows the closed table holds', async () => {
    await mount({
      conversations: [convo(), convo({ slot: 'chat-2-1700000001', title: 'Second' })],
      conversation_count: 45,
    })
    // The count is what tells a reader whether opening it is worth it, and the
    // ratio says the list is clamped rather than letting "2" pass for the whole
    // window.
    await waitFor(() => expect(within(tableToggle()).getByText('2 / 45')).toBeInTheDocument())
  })

  it('folds the rows past the top five into one', async () => {
    await mount({
      by_model: [
        row('m1', 600),
        row('m2', 500),
        row('m3', 400),
        row('m4', 300),
        row('m5', 200),
        row('m6', 70),
        row('m7', 30),
      ],
    })
    // Two rows folded, their credits summed: the block still accounts for every
    // credit the table does, without seven bars of which two are a pixel wide.
    await waitFor(() => expect(screen.getByText('Other (2)')).toBeInTheDocument())
    expect(screen.getByText('m5')).toBeInTheDocument()
    expect(screen.queryByText('m6')).toBeNull()
    const foldedRow = screen.getByText('Other (2)').closest('div.items-center') as HTMLElement
    expect(within(foldedRow).getByText('100')).toBeInTheDocument()
  })

  it('draws no folded row when every row fits', async () => {
    await mount({ by_model: [row('m1', 600), row('m2', 400)] })
    await waitFor(() => expect(screen.getByText('m1')).toBeInTheDocument())
    expect(screen.queryByText(/^Other \(/)).toBeNull()
  })

  it('links a session bar exactly as the table links its row', async () => {
    await mount()
    // Same two rules as the table's own session column, so a reader cannot find
    // a conversation clickable in one place and inert in the other.
    const link = await screen.findByRole('link', { name: 'A named conversation' })
    expect(link.getAttribute('href')).toContain('sid=chat-1-1700000000')
  })

  it('leaves a session bar inert when the dashboard cannot open it', async () => {
    await mount({
      conversations: [
        convo({ title: 'Asked over Telegram', category: 'telegram', slot: 'telegram:kirocrew:direct:1' }),
      ],
    })
    await waitFor(() => expect(screen.getByText('Asked over Telegram')).toBeInTheDocument())
    expect(screen.queryByRole('link')).toBeNull()
  })

  it('names an untitled session the way the table names it', async () => {
    await mount({ conversations: [convo({ title: undefined })] })
    // A closed conversation has no title to show, and the bar still has to
    // identify itself rather than render a blank label beside a bar.
    await waitFor(() => expect(screen.getByText(/^Untitled/)).toBeInTheDocument())
    expect(screen.getByTitle('chat-1-1700000000')).toBeInTheDocument()
  })

  it('says so rather than drawing an empty block when a grouping has no rows', async () => {
    await mount({ by_model: [], by_category: [] })
    await waitFor(() => expect(screen.getByText('Credits by model')).toBeInTheDocument())
    expect(screen.getAllByText('No spend recorded in this window').length).toBeGreaterThan(0)
    // The shared EmptyState, not a bare line of muted text: this file matches
    // `src/pages/*.tsx`, whose required page structure names EmptyState for an
    // empty list, so a local placeholder is the one thing that must not be here.
    expect(screen.getAllByTestId('empty-state').length).toBeGreaterThan(0)
  })

  it('mutes its own bars for readers without muting the shared renderer by default', async () => {
    await mount()
    await waitFor(() => expect(screen.getByText('Credits by origin')).toBeInTheDocument())
    // The label and the figure either side of a bar already carry the row's facts
    // as text, so reading the bar too announces every row twice.
    const tracks = document.querySelectorAll('[aria-hidden="true"].rounded-sm')
    expect(tracks.length).toBeGreaterThan(0)
    // Opt-in, not the renderer's default: `Histogram` also draws the Startup
    // tab's distribution, which this change must leave exactly as it found it.
    // An absent attribute, never aria-hidden="false" -- the two are not the same
    // to a reader.
    expect(document.querySelectorAll('[aria-hidden="false"]').length).toBe(0)
  })

  it('drops the footnote when the block it describes has no rows', async () => {
    await mount({ conversations: [], conversation_count: 0 })
    await waitFor(() => expect(screen.getByText('Top sessions')).toBeInTheDocument())
    // The footnote explains which bars link to a chat. With no bars it is a
    // caption for nothing, and it reads as a stray line under the empty state.
    expect(screen.queryByText('Named dashboard sessions link to the chat')).toBeNull()
  })
})
