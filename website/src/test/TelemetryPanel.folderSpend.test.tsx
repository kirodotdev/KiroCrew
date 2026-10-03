/**
 * Telemetry Spend: credits by sidebar folder.
 *
 * The backend rolls each session's spend up to the folder it is filed in and
 * sends `by_folder`, one row per folder id with its path as the name and an
 * empty id for Unfiled. These pin the two places the panel shows it -- the bar
 * block and the table's group-by -- and the rules a folder row needs that a
 * category row does not: Unfiled is labelled rather than blank, and two folders
 * with the same name stay two rows.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

import TelemetryPanel from '../pages/TelemetryPanel'

const row = (name: string, credits: number, over: Record<string, unknown> = {}) => ({
  name,
  credits,
  turns: 10,
  per_turn: credits / 10,
  share_pct: credits / 10,
  delta_pct: null,
  ...over,
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
  by_model: [row('opus-5', 1000)],
  by_channel: [row('dashboard', 1000)],
  by_category: [row('dashboard', 1000)],
  by_folder: [
    row('Platform dev › Telemetry', 600, { folder_id: 'f-sub' }),
    row('Ops', 250, { folder_id: 'f-ops' }),
    row('Ops', 100, { folder_id: 'f-ops2' }),
    row('', 50, { folder_id: '' }),
  ],
  context_bands: [],
  conversations: [],
  conversation_count: 0,
  navigable_category: 'dashboard',
  ...over,
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
  vi.mocked(api.telemetryStartup).mockResolvedValue({
    enabled: true,
    window_days: 7,
    shard_count: 1,
    metrics_dir: '/metrics',
    startup: null,
    turn: null,
    context: null,
    other: [],
    cost: cost(over),
  } as never)
  return render(<TelemetryPanel />, { wrapper: Wrapper })
}

const blockOf = (title: string) =>
  screen.getByText(title).closest('div.min-w-0') as HTMLElement

beforeEach(() => {
  vi.clearAllMocks()
  qc.clear()
  localStorage.clear()
})

describe('TelemetryPanel — credits by folder', () => {
  it('draws a folder block with paths, separate same-named folders and Unfiled', async () => {
    await mount()
    await waitFor(() => expect(screen.getByText('Credits by folder')).toBeInTheDocument())
    const block = blockOf('Credits by folder')
    const path = within(block).getByText('Platform dev › Telemetry')
    // A folder name is something the user typed, not a token: default face,
    // as in the sidebar. Category and model labels stay monospace.
    expect(path).not.toHaveClass('font-mono')
    expect(within(blockOf('Credits by model')).getByText('opus-5')).toHaveClass('font-mono')
    // Two folders called Ops are two rows, not one merged bar.
    expect(within(block).getAllByText('Ops')).toHaveLength(2)
    // The no-folder row is named, not a blank label.
    expect(within(block).getByText('Unfiled')).toBeInTheDocument()
    // Parent rows exclude their subfolders; that has to be readable without
    // opening the tip, or the parent row reads as a project total.
    expect(
      within(block).getByText('Each row counts sessions filed directly in that folder, not in its subfolders.'),
    ).toBeInTheDocument()
  })

  it('hides the block and the Folder grouping when no spend sits in a folder', async () => {
    localStorage.setItem('telemetry:spend-table-open', '1')
    // A remembered Folder choice must not leave the table on a grouping that
    // has nothing to show.
    localStorage.setItem('telemetry:spend-group', 'folder')
    await mount({ by_folder: [row('', 1000, { folder_id: '' })] })
    await waitFor(() => expect(screen.getByRole('radio', { name: 'Origin' })).toBeInTheDocument())
    expect(screen.queryByText('Credits by folder')).toBeNull()
    expect(screen.queryByRole('radio', { name: 'Folder' })).toBeNull()
    expect(screen.getByRole('radio', { name: 'Session' })).toHaveAttribute('aria-checked', 'true')
  })

  it('hides the block when the payload carries no folder rows at all', async () => {
    await mount({ by_folder: undefined })
    await waitFor(() => expect(screen.getByText('Credits by model')).toBeInTheDocument())
    expect(screen.queryByText('Credits by folder')).toBeNull()
  })

  it('hides the block when folder rows carry turns but no credits', async () => {
    // A token- or cost-only provider bills no credits: a folder row with zero
    // credits is not spend, and a block of 0% bars answers nothing.
    await mount({
      by_folder: [row('Ops', 0, { folder_id: 'f-ops' }), row('', 1000, { folder_id: '' })],
    })
    await waitFor(() => expect(screen.getByText('Credits by model')).toBeInTheDocument())
    expect(screen.queryByText('Credits by folder')).toBeNull()
  })

  it('says how many credits Unfiled holds past the tracking limit', async () => {
    await mount({
      by_folder: [
        row('Ops', 600, { folder_id: 'f-ops' }),
        row('', 1400, { folder_id: '', capped_credits: 1234, capped_limit: 7500 }),
      ],
    })
    await waitFor(() => expect(screen.getByText('Credits by folder')).toBeInTheDocument())
    // The limit is the payload's, not a number baked into the copy.
    expect(
      within(blockOf('Credits by folder')).getByText(
        /Unfiled includes 1,234 credits from sessions beyond the 7,500 this view can sort into folders\./,
      ),
    ).toBeInTheDocument()
  })

  it('shows no capped-credits note when nothing was capped', async () => {
    await mount({
      by_folder: [
        row('Ops', 600, { folder_id: 'f-ops' }),
        row('', 400, { folder_id: '', capped_credits: 0 }),
      ],
    })
    await waitFor(() => expect(screen.getByText('Credits by folder')).toBeInTheDocument())
    expect(within(blockOf('Credits by folder')).queryByText(/Unfiled includes/)).toBeNull()
  })

  it('offers Folder as a table grouping and remembers it', async () => {
    localStorage.setItem('telemetry:spend-table-open', '1')
    await mount()
    await waitFor(() => expect(screen.getByRole('radio', { name: 'Folder' })).toBeInTheDocument())
    await userEvent.click(screen.getByRole('radio', { name: 'Folder' }))
    const table = await waitFor(() => {
      const t = document.querySelector('table') as HTMLElement
      expect(within(t).getByText('Platform dev › Telemetry')).toBeInTheDocument()
      return t
    })
    expect(within(table).getAllByText('Ops')).toHaveLength(2)
    expect(within(table).getByText('Unfiled')).toBeInTheDocument()
    // The Folder column tip carries the direct-filing rule too: a reader who
    // opens the table without the bar block's note must not read a parent row
    // as a project total.
    const folderHead = within(table)
      .getAllByRole('columnheader')
      .find(th => within(th).queryByText('Folder')) as HTMLElement
    const tip = within(folderHead).getByRole('button', { name: 'More information' })
    expect(tip.getAttribute('title') || '').toContain(
      'Each row counts sessions filed directly in that folder, not in its subfolders.',
    )
    expect(localStorage.getItem('telemetry:spend-group')).toContain('folder')
  })
})
