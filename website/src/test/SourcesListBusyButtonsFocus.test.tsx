// A busy source button keeps keyboard focus. While its request is in flight,
// each button stays in the tab order (aria-disabled, not native disabled), so
// focus never falls to the page. A native `disabled` button would leave the
// tab order and strand focus on document.body, which these tests pin against.

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import SourcesList from '../pages/knowledge/SourcesList'
import * as api from '../pages/knowledge/api'
import type { Source } from '../pages/knowledge/types'
import { i18nT } from '../i18n/t'

vi.mock('../pages/knowledge/api', () => ({ knowledgeApi: vi.fn() }))

const SOURCES: Source[] = [
  { id: 's1', name: 'notes.md', source_type: 'local_file', uri: '/tmp/notes.md', sync_status: 'synced', item_count: 1 },
]
const RENAME = i18nT('pages.knowledge.sourcesList.rename_source')
const NAME = i18nT('pages.knowledge.sourcesList.source_name')
const SAVE_NAME = i18nT('pages.knowledge.sourcesList.save_name')
const SYNC = i18nT('pages.knowledge.sourcesList.sync_source')

let gate: { promise: Promise<unknown>; resolve: (v: unknown) => void } | null

function openGate() {
  let resolve!: (v: unknown) => void
  const promise = new Promise<unknown>((res) => { resolve = res })
  gate = { promise, resolve }
  return gate
}

const newClient = () =>
  new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })

function renderList() {
  render(
    <QueryClientProvider client={newClient()}>
      <SourcesList onIngest={() => {}} uploadNamespace="" setUploadNamespace={() => {}} namespaces={[]} ingestionJobs={[]} />
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  gate = null
  vi.mocked(api.knowledgeApi).mockImplementation((async (path: string, opts?: RequestInit) => {
    if (opts?.method) return gate ? gate.promise : { ok: true }
    if (path === '/sources') return SOURCES
    return { ok: true }
  }) as typeof api.knowledgeApi)
})

afterEach(cleanup)

describe('knowledge source busy buttons keep focus', () => {
  it('the Sync button stays focused while the sync is in flight', async () => {
    openGate()
    renderList()
    const sync = await screen.findByRole('button', { name: SYNC })
    act(() => { sync.focus() })
    fireEvent.click(sync)
    await waitFor(() => expect(sync).toHaveAttribute('aria-disabled', 'true'))
    expect(sync).toHaveFocus()
    expect(sync).not.toBeDisabled()
    await act(async () => { gate!.resolve({ ok: true }); await gate!.promise })
    await waitFor(() => expect(sync).not.toHaveAttribute('aria-disabled'))
    expect(sync).toHaveFocus()
  })

  it('a second press while syncing never starts a second sync', async () => {
    openGate()
    renderList()
    const sync = await screen.findByRole('button', { name: SYNC })
    fireEvent.click(sync)
    await waitFor(() => expect(sync).toHaveAttribute('aria-disabled', 'true'))
    fireEvent.click(sync)
    const syncCalls = vi.mocked(api.knowledgeApi).mock.calls.filter(([p, o]) =>
      p === '/sources/s1/sync' && (o as RequestInit | undefined)?.method === 'POST')
    expect(syncCalls).toHaveLength(1)
    await act(async () => { gate!.resolve({ ok: true }); await gate!.promise })
  })

  it('the rename Save button stays focused while the rename saves', async () => {
    openGate()
    renderList()
    await screen.findByText('notes.md')
    fireEvent.click(screen.getByRole('button', { name: RENAME }))
    const input = screen.getByLabelText(NAME)
    fireEvent.change(input, { target: { value: 'renamed.md' } })
    const save = screen.getByRole('button', { name: SAVE_NAME })
    act(() => { save.focus() })
    fireEvent.click(save)
    await waitFor(() => expect(save).toHaveAttribute('aria-disabled', 'true'))
    expect(save).toHaveFocus()
    expect(save).not.toBeDisabled()
    await act(async () => { gate!.resolve({ ok: true }); await gate!.promise })
  })
})
