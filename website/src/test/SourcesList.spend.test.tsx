/**
 * Ongoing indexing-cost visibility on the knowledge sources list.
 *
 * A watched folder keeps spending Kiro requests at idle, and the add-time estimate
 * is gone the moment the dialog closes. These tests pin the two things that make
 * the ongoing cost legible on the list itself: the per-source remaining figure,
 * and the standing notice that indexing costs anything at all.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import SourcesList from '../pages/knowledge/SourcesList'
import * as api from '../pages/knowledge/api'
import type { Source, SourceSpend } from '../pages/knowledge/types'

vi.mock('../pages/knowledge/api', () => ({ knowledgeApi: vi.fn() }))

const spend = (over: Partial<SourceSpend> = {}): SourceSpend => ({
  files_total: 0,
  files_done: 0,
  files_failed: 0,
  files_skipped: 0,
  files_pending: 0,
  chunks_embedded: 0,
  estimated_llm_calls_remaining: 0,
  ...over,
})

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
const wrapper = ({ children }: { children: React.ReactNode }) => (
  <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
)

function renderList(sources: Source[]) {
  vi.mocked(api.knowledgeApi).mockImplementation(async (path: string) => {
    if (path === '/sources') return sources as unknown as never
    return { ok: true } as unknown as never
  })
  return render(
    <SourcesList onIngest={() => {}} uploadNamespace="" setUploadNamespace={() => {}} namespaces={[]} ingestionJobs={[]} />,
    { wrapper },
  )
}

beforeEach(() => {
  queryClient.clear()
  vi.mocked(api.knowledgeApi).mockReset()
})

describe('SourcesList — per-source spend visibility', () => {
  it('shows indexing progress and the Kiro requests still owed, rounded', async () => {
    renderList([{
      id: 's1', name: 'Notes', source_type: 'local_folder', uri: '/tmp/notes',
      sync_status: 'active', item_count: 12,
      spend: spend({
        files_total: 120, files_done: 40, files_pending: 80,
        chunks_embedded: 310, estimated_llm_calls_remaining: 11460,
      }),
    }])
    await screen.findByText('Notes')
    expect(screen.getByText('40/120 files indexed')).toBeTruthy()
    // Two significant figures, not the raw 11,460: the figure is an estimate, so
    // the rendered precision must not outrun the leading ~. The scale word is
    // locale-dependent (K in en, 万 in ja), hence the unit comes from the label.
    expect(screen.getByText('~11K Kiro requests to finish')).toBeTruthy()
  })

  it('names the same billing unit in the row and its tooltip', async () => {
    // The whole point of the figure is comparison against a bill, so the row and
    // the tooltip must not describe the unit two different ways.
    renderList([{
      id: 's1', name: 'Notes', source_type: 'local_folder', uri: '/tmp/notes',
      sync_status: 'active', item_count: 12,
      spend: spend({
        files_total: 120, files_done: 40, files_pending: 80,
        estimated_llm_calls_remaining: 11460,
      }),
    }])
    const row = await screen.findByText('~11K Kiro requests to finish')
    expect(row.getAttribute('title')).toContain('Kiro requests')
    // Engine vocabulary a user cannot act on.
    expect(row.getAttribute('title')).not.toMatch(/sweep/i)
  })

  it('counts skipped files as resolved but never counts failures as progress', async () => {
    // 100 unreadable files with no failure signal read as permanently stalled: the
    // fraction sits short of total forever and the requests-left figure is gone,
    // because a failure is terminal. Skipping IS resolved (the user chose it), so it
    // belongs in the numerator; failing is not, so it is called out instead.
    renderList([{
      id: 's1', name: 'Notes', source_type: 'local_folder', uri: '/tmp/notes',
      sync_status: 'active', item_count: 12,
      spend: spend({
        files_total: 1240, files_done: 1100, files_skipped: 40, files_failed: 100,
        chunks_embedded: 8000,
      }),
    }])
    await screen.findByText('Notes')
    // 1100 done + 40 skipped resolved; the 100 failures are the remaining gap.
    expect(screen.getByText('1,140/1,240 files indexed')).toBeTruthy()
    const failed = screen.getByText('100 failed')
    expect(failed).toBeTruthy()
    // Must read as an error, not as neutral metadata.
    expect(failed.className).toContain('text-danger')
  })

  it('singularizes a lone failure', async () => {
    renderList([{
      id: 's1', name: 'Notes', source_type: 'local_folder', uri: '/tmp/notes',
      sync_status: 'active', item_count: 12,
      spend: spend({ files_total: 10, files_done: 9, files_failed: 1 }),
    }])
    await screen.findByText('Notes')
    expect(screen.getByText('1 failed')).toBeTruthy()
  })

  it('shows no failure count when nothing failed', async () => {
    renderList([{
      id: 's1', name: 'Notes', source_type: 'local_folder', uri: '/tmp/notes',
      sync_status: 'active', item_count: 12,
      spend: spend({ files_total: 40, files_done: 40, chunks_embedded: 310 }),
    }])
    await screen.findByText('Notes')
    expect(screen.queryByText(/failed/)).toBeNull()
  })

  it('carries the gradual-charge caveat in the notice, not only a tooltip', async () => {
    // A title attribute is invisible to touch and keyboard users, so the fact that
    // the charge accrues over time has to live in visible text.
    renderList([])
    const notice = await screen.findByText(/Indexing uses Kiro requests/)
    expect(notice.textContent).toMatch(/spread out over time/)
  })

  it('omits the remaining figure once nothing is outstanding', async () => {
    // A finished source must not keep implying future spend -- that is the same
    // wrong signal as showing none at all, in the other direction.
    renderList([{
      id: 's1', name: 'Notes', source_type: 'local_folder', uri: '/tmp/notes',
      sync_status: 'active', item_count: 12,
      spend: spend({ files_total: 40, files_done: 40, chunks_embedded: 310 }),
    }])
    await screen.findByText('Notes')
    expect(screen.getByText('40/40 files indexed')).toBeTruthy()
    expect(screen.queryByText(/Kiro requests to finish/)).toBeNull()
  })

  it('renders nothing extra for a source with no queued work', async () => {
    renderList([{
      id: 's1', name: 'doc.md', source_type: 'local_file', uri: '/tmp/doc.md',
      sync_status: 'synced', item_count: 3, spend: spend(),
    }])
    await screen.findByText('doc.md')
    expect(screen.queryByText(/files indexed/)).toBeNull()
    expect(screen.queryByText(/Kiro requests to finish/)).toBeNull()
  })

  it('survives a source the API sent without a spend block', async () => {
    // An older gateway, or a response shape trimmed by a proxy: the row must still
    // render rather than throwing on an undefined dereference.
    renderList([{
      id: 's1', name: 'doc.md', source_type: 'local_file', uri: '/tmp/doc.md',
      sync_status: 'synced', item_count: 3,
    }])
    await screen.findByText('doc.md')
    expect(screen.queryByText(/files indexed/)).toBeNull()
  })

  it('states that indexing costs requests even before any source exists', async () => {
    // The notice is the only warning a user who inherited a configured folder ever
    // gets -- they never open the add-source dialog that carries the up-front estimate.
    renderList([])
    expect(await screen.findByText(/Indexing uses Kiro requests/)).toBeTruthy()
  })

  it('keeps the cost notice to a single sentence', async () => {
    // The banner sits above the list on every visit; two sentences of standing
    // caveat is banner blindness fuel. One sentence, every fact retained.
    renderList([])
    const notice = await screen.findByText(/Indexing uses Kiro requests/)
    const text = (notice.textContent ?? '').trim()
    expect(text.endsWith('.')).toBe(true)
    expect((text.match(/\./g) ?? []).length).toBe(1)
  })

  it('failed count on a folder source is a control that reveals which files failed', async () => {
    // The count names a problem whose detail lives in the row's expanded view;
    // inert text strands the user one hidden chevron away from the answer.
    let files = {
      total: 10, done: 8, failed: 2, skipped: 0,
      files: [
        { file_path: '/tmp/notes/a.pdf', status: 'failed', error_message: 'unreadable', mtime: 0, item_count: 0 },
        { file_path: '/tmp/notes/b.pdf', status: 'failed', error_message: 'too large', mtime: 0, item_count: 0 },
      ],
    }
    vi.mocked(api.knowledgeApi).mockImplementation(async (path: string) => {
      if (path === '/sources') return [{
        id: 's1', name: 'Notes', source_type: 'local_folder', uri: '/tmp/notes',
        sync_status: 'active', item_count: 12,
        spend: spend({ files_total: 10, files_done: 8, files_failed: 2 }),
      }] as unknown as never
      if (path === '/sources/s1/files') return files as unknown as never
      return { ok: true } as unknown as never
    })
    render(
      <SourcesList onIngest={() => {}} uploadNamespace="" setUploadNamespace={() => {}} namespaces={[]} ingestionJobs={[]} />,
      { wrapper },
    )
    // A real button: keyboard-reachable, and the visible text IS the accessible
    // name (WCAG 2.5.3 label-in-name); the what-it-does hint rides in title.
    const count = await screen.findByRole('button', { name: '2 failed' })
    expect(count.getAttribute('title')).toBe('Show which files failed')
    // Visibly a control at rest: hover/title never fire on touch, so the
    // affordance must be static, not hover-only.
    expect(count.className).toContain('decoration-dotted')
    // Exactly once: the count has one owner (the meta line), so the same number
    // never shows twice on one row.
    expect(screen.getAllByText('2 failed')).toHaveLength(1)
    fireEvent.click(count)
    // The expanded view opens and names the failed files.
    expect(await screen.findByText('a.pdf')).toBeTruthy()
    expect(screen.getByText('b.pdf')).toBeTruthy()
    // Toggle, matching the chevron: a second click collapses instead of being dead.
    fireEvent.click(count)
    expect(screen.queryByText('a.pdf')).toBeNull()
    // Reopening refetches rather than serving the Infinity-staleTime cache: a
    // failure added by a later scan must appear.
    files = {
      total: 10, done: 7, failed: 3, skipped: 0,
      files: [...files.files, { file_path: '/tmp/notes/c.pdf', status: 'failed', error_message: 'corrupt', mtime: 0, item_count: 0 }],
    }
    fireEvent.click(count)
    expect(await screen.findByText('c.pdf')).toBeTruthy()
  })

  it('failed count stays plain text where no expanded view exists', async () => {
    // An uploaded file has no per-file breakdown to reveal, so promising one with
    // an affordance would be a control that does nothing.
    renderList([{
      id: 's1', name: 'doc.md', source_type: 'local_file', uri: '/tmp/doc.md',
      sync_status: 'synced', item_count: 3,
      spend: spend({ files_total: 4, files_done: 1, files_failed: 3 }),
    }])
    await screen.findByText('doc.md')
    const failed = screen.getByText('3 failed')
    expect(failed.tagName).not.toBe('BUTTON')
    expect(screen.queryByRole('button', { name: '3 failed' })).toBeNull()
  })
})
