/**
 * Telemetry panel: what the page remembers, and what its Context rows are called.
 *
 * Two properties, both of which regress silently because the page still WORKS
 * without them -- it just forgets, or names a row after an internal id:
 *
 *  1. The tab and both group-by controls survive a remount. The sort inside those
 *     tables was already persisted by `useSortableTable`, so a reader who sorted
 *     the Startup table by p90 came back to Spend-by-session with the sort intact
 *     and the tab gone. A stored value that is no longer a valid choice must fall
 *     back rather than select nothing: the segment list is filtered by which data
 *     exists, so `startup` can be remembered on a machine whose startup shard has
 *     since gone away.
 *  2. Context rows show the conversation's TITLE, joined from the spend payload on
 *     the shared slot, and fall back to the raw slot when there is no title to
 *     join. The occupancy payload has never carried a title, and the two
 *     measurements cover different windows, so the fallback is a normal outcome
 *     rather than an error path.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

import TelemetryPanel from '../pages/TelemetryPanel'

const session = (over: Record<string, unknown> = {}) => ({
  slot: 'chat-7-1700000700',
  turns: 12,
  peak_pct: 95,
  used: 152341,
  window: 200000,
  agent: 'kirocrew',
  model: 'opus-5',
  surface: 'dashboard',
  ts: '2026-08-05T00:00:00Z',
  ...over,
})

const convo = (over: Record<string, unknown> = {}) => ({
  slot: 'chat-7-1700000700',
  category: 'dashboard',
  channel: 'dashboard',
  title: 'Porting the trash containment',
  credits: 100,
  turns: 10,
  peak_pct: 50,
  span_days: 1,
  first_ts: 1700000000,
  growth_pct_per_turn: 2,
  turns_to_compaction: 20,
  ...over,
})

const cost = (over: Record<string, unknown> = {}) => ({
  window_days: 7,
  credits: 1000,
  turns: 100,
  per_turn: 10,
  prior_credits: 500,
  prior_turns: 50,
  prior_per_turn: 10,
  delta_pct: 100,
  priciest: { credits: 90, slot: 'chat-7-1700000700', ts: '2026-08-05' },
  by_model: [{ name: 'opus-5', credits: 800, turns: 80, per_turn: 10, share_pct: 80, delta_pct: 12 }],
  by_channel: [{ name: 'dashboard', credits: 900, turns: 90, per_turn: 10, share_pct: 90, delta_pct: 8 }],
  by_category: [{ name: 'bg', credits: 900, turns: 90, per_turn: 10, share_pct: 90, delta_pct: 8 }],
  context_bands: [],
  conversations: [convo()],
  conversation_count: 1,
  navigable_category: 'dashboard',
  ...over,
})

const context = (over: Record<string, unknown> = {}) => ({
  turns: 40,
  p50_pct: 40,
  p90_pct: 80,
  max_pct: 95,
  window_days: 14,
  sessions: [session()],
  ...over,
})

const payload = (over: Record<string, unknown> = {}) => ({
  enabled: true,
  window_days: 7,
  shard_count: 1,
  metrics_dir: '/tmp/metrics',
  startup: null,
  turn: null,
  context: context(),
  cost: cost(),
  other: [],
  ...over,
})

vi.mock('../api/client', () => ({ api: { telemetryStartup: vi.fn() } }))

const Wrapper = ({ children }: { children: React.ReactNode }) => (
  <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <MemoryRouter>{children}</MemoryRouter>
  </QueryClientProvider>
)

async function mount(over: Record<string, unknown> = {}) {
  const { api } = await import('../api/client')
  vi.mocked(api.telemetryStartup).mockResolvedValue(payload(over) as never)
  const view = render(<TelemetryPanel />, { wrapper: Wrapper })
  return view
}

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
})

describe('TelemetryPanel — the page remembers where you were', () => {
  it('reopens on the tab that was last selected', async () => {
    // Seeded rather than clicked: the segmented control collapses to a single
    // trigger at jsdom's zero width, so driving it here would test the control's
    // collapse behaviour rather than this page's memory. What persistence means
    // for a reader is READ ON MOUNT, and that is what this asserts; the write
    // half is covered by the group-by test below, which shares the same effect.
    localStorage.setItem('telemetry:tab', 'context')
    await mount()

    // The Context surface is the one on screen...
    await waitFor(() => expect(screen.getByText(/152,341|152341/)).toBeTruthy())
    // ...and the Spend surface is not, so the stored tab actually selected rather
    // than merely being readable.
    expect(screen.queryByRole('button', { name: /Per turn/ })).toBeNull()
  })

  it('reopens on the group-by that was last selected', async () => {
    const first = await mount()
    await waitFor(() => expect(screen.getByRole('button', { name: /model/i })).toBeTruthy())
    await userEvent.click(screen.getByRole('button', { name: /model/i }))
    await waitFor(() => expect(localStorage.getItem('telemetry:spend-group')).toBe('model'))
    first.unmount()

    await mount()
    // `opus-5` is a by_model row label; the session grouping does not have it.
    await waitFor(() => expect(screen.getByText('opus-5')).toBeTruthy())
  })

  it('falls back when the remembered tab is not a choice this payload offers', async () => {
    // A machine that had startup data yesterday and does not today: the segment
    // list is built from what exists, so the stored value names no segment.
    localStorage.setItem('telemetry:tab', 'startup')
    await mount({ startup: null })
    // Spend is the fallback, and it renders rather than leaving an empty frame.
    await waitFor(() => expect(screen.getByText('Porting the trash containment')).toBeTruthy())
  })

  it('ignores a stored value that is not one of the choices at all', async () => {
    localStorage.setItem('telemetry:spend-group', 'not-a-grouping')
    await mount()
    // The session grouping is the fallback, so the conversation title is on screen.
    await waitFor(() => expect(screen.getByText('Porting the trash containment')).toBeTruthy())
  })
})

describe('TelemetryPanel — Context rows name the conversation', () => {
  it('shows the title joined from the spend rows on the shared slot', async () => {
    localStorage.setItem('telemetry:tab', 'context')
    await mount()
    await waitFor(() => expect(screen.getByText(/152,341|152341/)).toBeTruthy())
    // The occupancy row is labelled by the conversation, not by its slot.
    expect(screen.getByText('Porting the trash containment')).toBeTruthy()
    expect(screen.queryByText('chat-7-1700000700')).toBeNull()
  })

  it('falls back to the raw slot when no spend row carries a title', async () => {
    localStorage.setItem('telemetry:tab', 'context')
    // The windows differ (14d occupancy, 7d spend), so an occupancy row with no
    // spend row is a normal outcome rather than a broken payload.
    await mount({ cost: cost({ conversations: [convo({ slot: 'chat-9-1700009900' })] }) })
    await waitFor(() => expect(screen.getByText(/152,341|152341/)).toBeTruthy())
    expect(screen.getByText('chat-7-1700000700')).toBeTruthy()
  })

  it('names a joined row with no title the way Spend does, not by its slot', async () => {
    localStorage.setItem('telemetry:tab', 'context')
    // A title exists only while a conversation is open, so a closed one joins on the
    // slot and still has nothing to show. It used to fall back to the raw slot, which
    // meant the same conversation read "Untitled conversation on <date>" in Spend and
    // a bare id here -- its rows could not be matched across the two tabs.
    await mount({ cost: cost({ conversations: [convo({ title: undefined })] }) })
    await waitFor(() => expect(screen.getByText(/152,341|152341/)).toBeTruthy())
    expect(screen.getByText(/untitled/i)).toBeTruthy()
    expect(screen.queryByText('chat-7-1700000700')).toBeNull()
  })

  it('links a titled dashboard row to its conversation, as Spend does', async () => {
    localStorage.setItem('telemetry:tab', 'context')
    // The task on this tab is "find which conversation is near compaction and go deal
    // with it". A title that is a link one tab over and inert here dead-ends exactly
    // that, so the affordance is shared.
    await mount()
    await waitFor(() => expect(screen.getByText(/152,341|152341/)).toBeTruthy())
    const link = screen.getByRole('link', { name: 'Porting the trash containment' })
    expect(link.getAttribute('href')).toContain('sid=chat-7-1700000700')
  })

  it('does not link a row whose category has nowhere to go', async () => {
    localStorage.setItem('telemetry:tab', 'context')
    // A Telegram thread is a real, often titled conversation, but `?sid=` resolves
    // against the live dashboard slot list -- so linking it lands on "not found".
    await mount({ cost: cost({ conversations: [convo({ category: 'telegram' })] }) })
    await waitFor(() => expect(screen.getByText(/152,341|152341/)).toBeTruthy())
    expect(screen.getByText('Porting the trash containment')).toBeTruthy()
    expect(screen.queryByRole('link', { name: 'Porting the trash containment' })).toBeNull()
    // And it reads at Spend's weight, not the body default: Spend renders every
    // non-link title muted, so the same conversation would otherwise look more
    // prominent here than there while being less useful.
    expect(screen.getByText('Porting the trash containment').className).toContain('text-muted')
  })

  it('does not link when the payload names no navigable category', async () => {
    localStorage.setItem('telemetry:tab', 'context')
    // Fails CLOSED: without the emptiness guard a payload missing
    // `navigable_category` would satisfy the comparison for every category-less row
    // and link all of them.
    await mount({
      cost: cost({ navigable_category: '', conversations: [convo({ category: '' })] }),
    })
    await waitFor(() => expect(screen.getByText(/152,341|152341/)).toBeTruthy())
    expect(screen.queryByRole('link', { name: 'Porting the trash containment' })).toBeNull()
  })
})
