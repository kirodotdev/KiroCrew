// Closing a knowledge source's inline rename never strands focus on
// document.body. Enter (save or unchanged), Escape, and the field's own Save
// and Cancel buttons all unmount the control that had focus, so each returns
// focus to the Rename button of the row the rename was opened from.

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import SourcesList from '../pages/knowledge/SourcesList'
import * as api from '../pages/knowledge/api'
import type { Source } from '../pages/knowledge/types'
import { i18nT } from '../i18n/t'

vi.mock('../pages/knowledge/api', () => ({ knowledgeApi: vi.fn() }))

const SOURCES: Source[] = [
  { id: 's1', name: 'First Source', source_type: 'local_file', uri: '/tmp/a.md', sync_status: 'synced', item_count: 1 },
  { id: 's2', name: 'Second Source', source_type: 'local_file', uri: '/tmp/b.md', sync_status: 'synced', item_count: 2 },
]
const RENAME = i18nT('pages.knowledge.sourcesList.rename_source')
const NAME = i18nT('pages.knowledge.sourcesList.source_name')
const SAVE = i18nT('pages.knowledge.sourcesList.save_name')
const CANCEL = i18nT('pages.knowledge.sourcesList.cancel_rename')

const nextFrame = () => act(async () => { await new Promise(requestAnimationFrame) })
const nameField = () => screen.queryByLabelText(NAME)
// The Rename button in the row that shows `name`.
const renameOf = (name: string) => {
  const row = screen.getByText(name).closest('.group\\/name') as HTMLElement
  return within(row).getByRole('button', { name: RENAME })
}
const patches = () => vi.mocked(api.knowledgeApi).mock.calls.filter(([, o]) => (o as RequestInit | undefined)?.method === 'PATCH')

let patchGate: Promise<unknown> | null

beforeEach(() => {
  vi.clearAllMocks()
  patchGate = null
  vi.mocked(api.knowledgeApi).mockImplementation((async (path: string, opts?: RequestInit) => {
    if (opts?.method === 'PATCH') return patchGate ?? { ok: true }
    if (path === '/sources') return SOURCES
    return { ok: true }
  }) as typeof api.knowledgeApi)
})

afterEach(cleanup)

async function openRename(name = 'Second Source'): Promise<HTMLElement> {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <SourcesList onIngest={() => {}} uploadNamespace="" setUploadNamespace={() => {}} namespaces={[]} ingestionJobs={[]} />
      <button type="button" data-testid="elsewhere">Elsewhere</button>
    </QueryClientProvider>,
  )
  await screen.findByText(name)
  fireEvent.click(renameOf(name))
  const input = screen.getByLabelText(NAME)
  expect(input).toHaveFocus()
  return input
}

describe('knowledge source rename focus', () => {
  it('Enter saves and returns focus to that row\'s Rename button', async () => {
    const input = await openRename()
    fireEvent.change(input, { target: { value: 'Renamed' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => expect(nameField()).toBeNull())
    // The list still shows the old name until the refetch lands; the row is the same.
    await waitFor(() => expect(renameOf('Second Source')).toHaveFocus())
    expect(patches()).toHaveLength(1)
  })

  it('Enter on an unchanged name closes and returns focus to the Rename button', async () => {
    const input = await openRename()
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(nameField()).toBeNull()
    await waitFor(() => expect(renameOf('Second Source')).toHaveFocus())
    expect(patches()).toHaveLength(0)
  })

  it('Escape cancels and returns focus to that row\'s Rename button', async () => {
    const input = await openRename()
    fireEvent.change(input, { target: { value: 'Abandoned' } })
    fireEvent.keyDown(input, { key: 'Escape' })
    expect(nameField()).toBeNull()
    await waitFor(() => expect(renameOf('Second Source')).toHaveFocus())
    expect(patches()).toHaveLength(0)
  })

  // Passes on main too: React reuses the Save button's DOM node as the Rename
  // button (same element type at the same child index), so focus never drops.
  // Kept so a markup change that breaks that reuse cannot strand focus.
  it('the Save button saves and returns focus to the Rename button', async () => {
    const input = await openRename()
    fireEvent.change(input, { target: { value: 'Renamed' } })
    const save = screen.getByRole('button', { name: SAVE })
    act(() => { save.focus() })
    fireEvent.click(save)
    await waitFor(() => expect(nameField()).toBeNull())
    await waitFor(() => expect(renameOf('Second Source')).toHaveFocus())
  })

  it('the Cancel button closes and returns focus to the Rename button', async () => {
    await openRename()
    const cancel = screen.getByRole('button', { name: CANCEL })
    act(() => { cancel.focus() })
    fireEvent.click(cancel)
    expect(nameField()).toBeNull()
    await waitFor(() => expect(renameOf('Second Source')).toHaveFocus())
  })

  it('a rename that resolves after the user moved on leaves focus where they put it', async () => {
    let resolve!: (v: unknown) => void
    patchGate = new Promise((r) => { resolve = r })
    const input = await openRename()
    fireEvent.change(input, { target: { value: 'Slow Rename' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    const elsewhere = screen.getByTestId('elsewhere')
    act(() => { elsewhere.focus() })
    await act(async () => { resolve({ ok: true }) })
    await waitFor(() => expect(nameField()).toBeNull())
    await nextFrame()
    expect(elsewhere).toHaveFocus()
  })
})
