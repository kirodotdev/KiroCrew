// A busy knowledge button keeps keyboard focus. While its request is in
// flight, and after a failed request, each button stays in the tab order
// (aria-disabled, not native disabled), so focus never falls to the page.
// A native `disabled` button would leave the tab order and strand focus on
// document.body, which these tests pin against.

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import DetailView from '../pages/knowledge/DetailView'
import SourcesList from '../pages/knowledge/SourcesList'
import * as api from '../pages/knowledge/api'
import type { KnowledgeItem } from '../pages/knowledge/types'
import type { Source } from '../pages/knowledge/types'
import { i18nT } from '../i18n/t'

vi.mock('../pages/knowledge/api', () => ({ knowledgeApi: vi.fn() }))
vi.mock('../components/MarkdownRenderer', () => ({
  default: ({ content }: { content: string }) => <div>{content}</div>,
}))

const ITEM: KnowledgeItem = {
  id: 'i1', title: 'Ledger design notes', item_type: 'design_doc', status: 'active',
  content: 'The Ledger keeps every entry.', tags: 'ops',
  created_at: '2026-01-01T00:00:00Z', updated_at: '2026-02-03T00:00:00Z',
}
const EDIT = i18nT('pages.knowledge.detailView.edit')
const SAVE = i18nT('pages.knowledge.detailView.save')
const TAGS = i18nT('pages.knowledge.detailView.comma_separated_tags')
const ARCHIVE = i18nT('pages.knowledge.detailView.archive')
const DELETE = i18nT('pages.knowledge.detailView.delete')
const SAVE_PATTERNS = i18nT('pages.knowledge.sourcesList.save_ignore_patterns')

let gate: { promise: Promise<unknown>; resolve: (v: unknown) => void; reject: (e: unknown) => void } | null

function openGate() {
  let resolve!: (v: unknown) => void
  let reject!: (e: unknown) => void
  const promise = new Promise<unknown>((res, rej) => { resolve = res; reject = rej })
  gate = { promise, resolve, reject }
  return gate
}

const newClient = () =>
  new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })

beforeEach(() => {
  vi.clearAllMocks()
  gate = null
})
afterEach(cleanup)

describe('knowledge DetailView busy buttons keep focus', () => {
  beforeEach(() => {
    vi.mocked(api.knowledgeApi).mockImplementation(((path: string, opts?: RequestInit) => {
      if (opts?.method) return gate ? gate.promise : Promise.resolve({ ok: true })
      if (path.endsWith('/related')) return Promise.resolve([])
      return Promise.resolve(ITEM)
    }) as typeof api.knowledgeApi)
  })

  function renderDetail() {
    render(
      <QueryClientProvider client={newClient()}>
        <DetailView itemId="i1" onBack={vi.fn()} />
      </QueryClientProvider>,
    )
  }

  it('the tag Save button stays focused while the save is in flight', async () => {
    openGate()
    renderDetail()
    fireEvent.click(await screen.findByRole('button', { name: EDIT }))
    const input = screen.getByLabelText(TAGS)
    fireEvent.change(input, { target: { value: 'ops, ledger' } })
    const save = screen.getByRole('button', { name: SAVE })
    act(() => { save.focus() })
    fireEvent.click(save)
    await waitFor(() => expect(save).toHaveAttribute('aria-disabled', 'true'))
    expect(save).toHaveFocus()
    expect(save).not.toBeDisabled()
    await act(async () => { gate!.resolve({ ok: true }); await gate!.promise })
    await waitFor(() => expect(screen.queryByLabelText(TAGS)).toBeNull())
  })

  it('the Archive button stays focused while the request is in flight and after it fails', async () => {
    openGate()
    renderDetail()
    const archive = await screen.findByRole('button', { name: ARCHIVE })
    act(() => { archive.focus() })
    fireEvent.click(archive)
    await waitFor(() => expect(archive).toHaveAttribute('aria-disabled', 'true'))
    expect(archive).toHaveFocus()
    expect(archive).not.toBeDisabled()
    await act(async () => { gate!.reject(new Error('nope')); await gate!.promise.catch(() => {}) })
    await waitFor(() => expect(archive).not.toHaveAttribute('aria-disabled'))
    expect(archive).toHaveFocus()
  })

  it('the Delete button stays focused while the request is in flight', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)
    openGate()
    renderDetail()
    const del = await screen.findByRole('button', { name: DELETE })
    act(() => { del.focus() })
    fireEvent.click(del)
    await waitFor(() => expect(del).toHaveAttribute('aria-disabled', 'true'))
    expect(del).toHaveFocus()
    expect(del).not.toBeDisabled()
    await act(async () => { gate!.resolve({ ok: true }); await gate!.promise })
    confirmSpy.mockRestore()
  })

  it('a cancelled Delete confirm never starts the request', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)
    renderDetail()
    const del = await screen.findByRole('button', { name: DELETE })
    fireEvent.click(del)
    const deletes = vi.mocked(api.knowledgeApi).mock.calls.filter(([, o]) => (o as RequestInit | undefined)?.method === 'DELETE')
    expect(deletes).toHaveLength(0)
    expect(del).not.toHaveAttribute('aria-disabled')
    confirmSpy.mockRestore()
  })
})

describe('knowledge ignore-patterns Save button keeps focus', () => {
  const SOURCES: Source[] = [
    {
      id: 's1', name: 'Notes', source_type: 'local_folder', uri: '/home/u/notes', sync_status: 'synced',
      item_count: 3, properties: JSON.stringify({ recursive: true, ignore_patterns: ['.trash/*'] }),
    },
  ]

  beforeEach(() => {
    vi.mocked(api.knowledgeApi).mockImplementation((async (path: string, opts?: RequestInit) => {
      if (opts?.method) return gate ? gate.promise : { ok: true }
      if (path === '/sources') return SOURCES as unknown as never
      if (path === '/sources/s1/files') return { files: [], total: 0, done: 0, failed: 0, skipped: 0 } as unknown as never
      return { ok: true } as unknown as never
    }) as typeof api.knowledgeApi)
  })

  it('the Save patterns button stays focused while the save is in flight', async () => {
    openGate()
    render(
      <QueryClientProvider client={newClient()}>
        <SourcesList onIngest={() => {}} uploadNamespace="" setUploadNamespace={() => {}} namespaces={[]} ingestionJobs={[]} />
      </QueryClientProvider>,
    )
    await screen.findByText('Notes')
    fireEvent.click(screen.getByLabelText('Expand folder details'))
    const box = screen.getByLabelText('Ignore patterns') as HTMLTextAreaElement
    fireEvent.change(box, { target: { value: '.trash/*\ngenerated/**' } })
    const save = screen.getByRole('button', { name: SAVE_PATTERNS })
    act(() => { save.focus() })
    fireEvent.click(save)
    await waitFor(() => expect(save).toHaveAttribute('aria-disabled', 'true'))
    expect(save).toHaveFocus()
    expect(save).not.toBeDisabled()
    await act(async () => { gate!.resolve({ ok: true }); await gate!.promise })
  })
})
