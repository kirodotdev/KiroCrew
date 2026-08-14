import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import SourcesList from '../pages/knowledge/SourcesList'
import * as api from '../pages/knowledge/api'
import type { Source } from '../pages/knowledge/types'

vi.mock('../pages/knowledge/api', () => ({ knowledgeApi: vi.fn() }))

const LONG_URI = '/home/user/workplace/AVeryLongPackageName/src/AVeryLongPackageName/deeply/nested/notes'

const sources: Source[] = [
  {
    id: 's1',
    name: 'Private Package Notes',
    source_type: 'local_folder',
    uri: LONG_URI,
    sync_status: 'synced',
    item_count: 378,
    summary_topic: 'Employment dates',
    summary_themes: JSON.stringify([
      'employment-dates', 'override-management', 'data-materialization',
      'backfill-architecture', 'ps-integration',
    ]),
  },
  {
    id: 's2',
    name: 'Artifacts',
    source_type: 'artifact',
    uri: 'artifact://',
    sync_status: 'synced',
    item_count: 33,
  },
]

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
const wrapper = ({ children }: { children: React.ReactNode }) => (
  <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
)

function renderList() {
  return render(
    <SourcesList onIngest={() => {}} uploadNamespace="" setUploadNamespace={() => {}} namespaces={[]} ingestionJobs={[]} />,
    { wrapper },
  )
}

beforeEach(() => {
  queryClient.clear()
  vi.mocked(api.knowledgeApi).mockReset()
  vi.mocked(api.knowledgeApi).mockImplementation(async (path: string) => {
    if (path === '/sources') return sources as unknown as never
    return { ok: true } as unknown as never
  })
})

describe('SourcesList — mobile layout', () => {
  it('stacks the row on narrow viewports and keeps it a row from sm up', async () => {
    renderList()
    const name = await screen.findByText('Private Package Notes')
    // name span → identity inner → identity outer → row wrapper
    const row = name.closest('div')!.parentElement!.parentElement!.parentElement!
    expect(row.className).toContain('flex-col')
    expect(row.className).toContain('sm:flex-row')
  })

  it('truncates a long source URI instead of overflowing the card', async () => {
    renderList()
    const uri = await screen.findByTitle(LONG_URI)
    expect(uri.className).toContain('truncate')
    expect(uri.parentElement!.className).toContain('min-w-0')
  })

  it('keeps the source name truncated so long names cannot push actions offscreen', async () => {
    renderList()
    const name = await screen.findByText('Private Package Notes')
    expect(name.className).toContain('truncate')
  })

  it('renders theme chips on a single line each so they wrap instead of stacking tall', async () => {
    renderList()
    const chip = await screen.findByText('employment-dates')
    expect(chip.className).toContain('whitespace-nowrap')
    expect(chip.parentElement!.className).toContain('flex-wrap')
  })

  it('lets the meta/action cluster wrap at every width so it cannot leave the card', async () => {
    // The cluster holds a VARIABLE number of figures -- item count, word count,
    // indexing progress, remaining Kiro requests -- so a nowrap floor pushes the
    // trailing action button past the card border and squeezes the source name to
    // nothing at mid widths. It must be free to wrap at any width.
    renderList()
    const items = await screen.findByText('378 items')
    const cluster = items.parentElement!
    expect(cluster.className).toContain('flex-wrap')
    expect(cluster.className).not.toContain('flex-nowrap')
  })

  it('keeps item counts and sync dates on one line', async () => {
    renderList()
    expect((await screen.findByText('378 items')).className).toContain('whitespace-nowrap')
    expect((await screen.findByText('33 items')).className).toContain('whitespace-nowrap')
  })

  it('shows the rename affordance without hover on touch viewports', async () => {
    renderList()
    await screen.findByText('Private Package Notes')
    const pencil = screen.getAllByLabelText('Rename source')[0]
    expect(pencil.className).toContain('opacity-100')
    expect(pencil.className).toContain('sm:opacity-0')
  })
})
