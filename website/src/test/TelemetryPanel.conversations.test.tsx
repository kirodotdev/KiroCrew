/**
 * Telemetry panel: a conversation row may only be a link when it can be opened.
 *
 * `ChatPage` resolves `?sid=` against the LIVE slot list
 * (`filteredSlots.some(s => s.key === urlSid)`) and, when the slot is absent,
 * reports `Session "…" not found` from a 5s timer. A row is unnamed precisely
 * BECAUSE its slot is gone — titles live on the in-memory slot and are not
 * persisted — so rendering those rows as links pointed the user's click at a
 * guaranteed error that arrives five seconds later. Roughly half a real ranking
 * is unnamed, which made the failure the common case rather than an edge.
 *
 * The open rows navigate through the router rather than a bare `href`: the app
 * switches slots in place everywhere else, and a plain anchor reloads the whole
 * SPA to reach the same destination.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

import TelemetryPanel from '../pages/TelemetryPanel'
import { i18next } from '../i18n/all'
import { i18nT } from '../i18n/t'

const convo = (over: Record<string, unknown> = {}) => ({
  slot: 'chat-1-1700000000',
  channel: 'dashboard',
  category: 'dashboard',
  credits: 100,
  turns: 10,
  peak_pct: 42,
  span_days: 1,
  first_ts: 1700000000,
  growth_pct_per_turn: null,
  turns_to_compaction: null,
  ...over,
})

const resp = (conversations: Record<string, unknown>[], extra: Record<string, unknown> = {}) => ({
  enabled: true,
  window_days: 14,
  shard_count: 3,
  metrics_dir: '/tmp/metrics',
  startup: null,
  turn: null,
  context: null,
  other: [],
  cost: {
    window_days: 7,
    credits: 1000,
    turns: 100,
    per_turn: 10,
    prior_credits: 500,
    prior_turns: 50,
    prior_per_turn: 10,
    delta_pct: 100,
    by_category: [],
  priciest: { credits: 50, slot: 'chat-1-1700000000', ts: '2026-08-01' },
    by_model: [],
    by_channel: [],
    context_bands: [],
    conversations,

    navigable_category: 'dashboard',
    conversation_count: conversations.length,
    ...extra,
  },
})

vi.mock('../api/client', () => ({
  api: { telemetryStartup: vi.fn() },
}))

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
const Wrapper = ({ children }: { children: React.ReactNode }) => (
  <QueryClientProvider client={qc}>
    <MemoryRouter>{children}</MemoryRouter>
  </QueryClientProvider>
)

async function mount(conversations: Record<string, unknown>[], extra: Record<string, unknown> = {}) {
  const { api } = await import('../api/client')
  vi.mocked(api.telemetryStartup).mockResolvedValue(resp(conversations, extra) as never)
  render(<TelemetryPanel />, { wrapper: Wrapper })
}

describe('TelemetryPanel — conversations by spend', () => {
  beforeEach(() => {
    qc.clear()
    vi.clearAllMocks()
  })

  it('links a named conversation to its chat', async () => {
    await mount([convo({ title: 'Telemetry cost page' })])
    const link = await screen.findByRole('link', { name: 'Telemetry cost page' })
    expect(link.getAttribute('href')).toContain('sid=chat-1-1700000000')
  })

  it("labels the previous-period pair with its units, not two bare numbers", async () => {
    await mount([convo({ title: 'Telemetry cost page' })])
    await screen.findByRole('link', { name: 'Telemetry cost page' })
    // The 'vs previous period' stat's sub-line used to render `500 · 50` — two
    // figures with nothing saying what either one counts. Label-before-number
    // on purpose: the unit words carry no plural agreement, so a prior window
    // of exactly 1 turn cannot render "1 turns" in any locale.
    expect(screen.getByText('credits 500 · turns 50')).toBeInTheDocument()
  })

  it('renders an unnamed conversation as text, never as a link', async () => {
    await mount([convo()])
    // The row is present, and still identified by its slot, through the shared
    // tip rather than a native title the desktop app may not paint…
    await waitFor(() => expect(screen.getByText(/^Untitled/)).toHaveAccessibleDescription('chat-1-1700000000'))
    expect(screen.getByText(/^Untitled/)).toHaveAttribute('title', '')
    // …and carries no anchor: its slot is closed, so a click could only ever
    // land on ChatPage's delayed "Session not found".
    expect(screen.queryByRole('link')).toBeNull()
  })

  it('treats spend as data on its own, with no OTEL shard present', async () => {
    // The payload here carries no startup/turn/context — only cost. Those come
    // from the OTEL shards while cost comes from the per-turn usage rows, so a
    // machine with spend recorded but no shard yet must still get the spend
    // surface rather than an "empty" notice.
    await mount([convo({ title: 'Open one' })])
    await screen.findByRole('link', { name: 'Open one' })
  })

  it('shows the spend surface even while the OTEL switch is off', async () => {
    // `telemetry.enabled` defaults to false. Spend comes from the per-turn usage
    // rows, which are written regardless of that switch, so the off-state must
    // still render the ranking -- otherwise the whole surface ships invisible to
    // anyone who never turned telemetry on.
    const { api } = await import('../api/client')
    const payload = resp([convo({ title: 'Open one' })])
    vi.mocked(api.telemetryStartup).mockResolvedValue({ ...payload, enabled: false } as never)
    render(<TelemetryPanel />, { wrapper: Wrapper })
    await screen.findByRole('link', { name: 'Open one' })
  })

  it('keeps the two kinds independent in one ranking', async () => {
    await mount([convo({ title: 'Open one' }), convo({ slot: 'chat-2-1700000001' })])
    await screen.findByRole('link', { name: 'Open one' })
    // Exactly one link for two rows — the closed row stayed inert.
    expect(screen.getAllByRole('link')).toHaveLength(1)
  })

  it('does not link a conversation the dashboard cannot open', async () => {
    // The bug this rule exists for. A Telegram thread IS a conversation and it
    // usually HAS a title, so the old "titled -> link it" rule rendered it as a
    // link to /chat?sid=<key> — which ChatPage resolves against dashboard slots
    // only, so the click landed on `Session "…" not found` after a 5s timeout.
    // Linkability is a property of the route, not of having a name.
    await mount([
      convo({
        title: 'Asked over Telegram',
        channel: 'telegram',
        category: 'telegram',
        slot: 'telegram:kirocrew:direct:874',
      }),
    ])
    await waitFor(() => expect(screen.getByText('Asked over Telegram')).toBeInTheDocument())
    expect(screen.queryByRole('link')).not.toBeInTheDocument()
    // Still named, though — falling back to "Untitled" would lose the one piece
    // of identity the row has.
    expect(screen.getByText('Asked over Telegram')).toHaveAccessibleDescription('telegram:kirocrew:direct:874')
  })

  describe('Group by Purpose', () => {
    const row = (name: string, credits: number) => ({
      name,
      credits,
      turns: 2,
      per_turn: credits / 2,
      share_pct: credits,
      delta_pct: null,
    })

    async function openPurpose() {
      await screen.findByRole('button', { name: /full table/i })
      fireEvent.click(screen.getByRole('button', { name: /full table/i }))
      fireEvent.click(await screen.findByRole('radio', { name: 'Purpose' }))
    }

    beforeEach(() => window.localStorage.clear())

    it('lists what each kind of turn cost when Purpose is chosen', async () => {
      await mount([convo()], { by_purpose: [row('turn', 70), row('helper', 20), row('automation', 10)] })
      await openPurpose()
      expect(await screen.findByText('chat')).toBeInTheDocument()
      expect(await screen.findByText('helper')).toBeInTheDocument()
      expect(screen.getByText('automation')).toBeInTheDocument()
    })

    it('translates the row names, not only the header, in another language', async () => {
      await i18next.changeLanguage('de')
      try {
        await mount([convo()], {
          by_purpose: [row('turn', 40), row('helper', 30), row('automation', 20), row('unknown', 10)],
        })
        const fullTable = i18nT('pages.telemetryPanel.full_table')
        fireEvent.click(await screen.findByRole('button', { name: name => name.startsWith(fullTable) }))
        fireEvent.click(await screen.findByRole('radio', { name: i18nT('pages.telemetryPanel.purpose_col') }))
        expect(await screen.findByText('Hilfsaufruf')).toBeInTheDocument()
        expect(screen.getByText('Automatisierung')).toBeInTheDocument()
        expect(screen.getByText('Unbekannt')).toBeInTheDocument()
        expect(screen.queryByText('helper')).toBeNull()
        expect(screen.queryByText('automation')).toBeNull()
      } finally {
        await i18next.changeLanguage('en')
      }
    })

    const kinds = [
      row('turn', 40),
      row('helper_title', 20),
      row('helper_summary', 15),
      row('helper_memory', 10),
      row('helper', 5),
    ]

    async function openPurposeTable() {
      const fullTable = i18nT('pages.telemetryPanel.full_table')
      fireEvent.click(await screen.findByRole('button', { name: n => n.startsWith(fullTable) }))
      fireEvent.click(await screen.findByRole('radio', { name: i18nT('pages.telemetryPanel.purpose_col') }))
    }

    it('names the helper kinds as their own rows', async () => {
      await mount([convo()], { by_purpose: kinds })
      await openPurposeTable()
      expect(await screen.findByText('helper: titles')).toBeInTheDocument()
      expect(screen.getByText('helper: summaries')).toBeInTheDocument()
      expect(screen.getByText('helper: memory upkeep')).toBeInTheDocument()
      expect(screen.getByText('helper')).toBeInTheDocument()
    })

    it('translates the helper kinds, never showing a raw kind name', async () => {
      await i18next.changeLanguage('de')
      try {
        await mount([convo()], { by_purpose: kinds })
        await openPurposeTable()
        expect(await screen.findByText('Hilfsaufruf: Titel')).toBeInTheDocument()
        expect(screen.getByText('Hilfsaufruf: Zusammenfassungen')).toBeInTheDocument()
        expect(screen.getByText('Hilfsaufruf: Speicherpflege')).toBeInTheDocument()
        for (const raw of ['helper_title', 'helper_summary', 'helper_memory']) {
          expect(screen.queryByText(raw)).toBeNull()
        }
      } finally {
        await i18next.changeLanguage('en')
      }
    })

    it('shows a purpose name this build does not know as it arrived', async () => {
      await mount([convo()], { by_purpose: [row('turn', 70), row('compaction', 30)] })
      await openPurpose()
      expect(await screen.findByText('compaction')).toBeInTheDocument()
    })

    it('offers no Purpose segment when an older gateway sends no by_purpose', async () => {
      await mount([convo()])
      fireEvent.click(await screen.findByRole('button', { name: /full table/i }))
      await screen.findByRole('radio', { name: 'Model' })
      expect(screen.queryByRole('radio', { name: 'Purpose' })).toBeNull()
    })

    it('lets the four groupings wrap on a narrow card instead of clipping them', async () => {
      await mount([convo()], { by_purpose: [row('turn', 70)] })
      await openPurpose()
      // happy-dom computes no layout, so the contract is pinned on the classes that
      // produce it, as SegmentedControl.wrapHug.test.tsx does.
      const group = screen.getByRole('radio', { name: 'Purpose' }).closest('[role="radiogroup"]')
      expect(group?.classList.contains('flex-wrap')).toBe(true)
      expect(group?.classList.contains('max-w-full')).toBe(true)
    })

    it('shows the session grouping when Purpose was remembered but the data has none', async () => {
      window.localStorage.setItem('telemetry:spend-group', 'purpose')
      await mount([convo()])
      fireEvent.click(await screen.findByRole('button', { name: /full table/i }))
      expect(await screen.findByRole('radio', { name: 'Session' })).toBeChecked()
    })
  })
})
