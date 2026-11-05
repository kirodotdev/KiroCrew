// Closing a knowledge item's inline tag editor never strands focus on
// document.body. Enter (save), Escape, and the editor's own Save and Cancel
// buttons all unmount the control that had focus, so each returns focus to the
// Edit button that opened the editor.

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import DetailView from '../pages/knowledge/DetailView'
import * as api from '../pages/knowledge/api'
import type { KnowledgeItem } from '../pages/knowledge/types'
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
const CANCEL = i18nT('pages.knowledge.detailView.cancel')
const TAGS = i18nT('pages.knowledge.detailView.comma_separated_tags')

const nextFrame = () => act(async () => { await new Promise(requestAnimationFrame) })
const tagField = () => screen.queryByLabelText(TAGS)
const trigger = () => screen.getByRole('button', { name: EDIT })
const patches = () => vi.mocked(api.knowledgeApi).mock.calls.filter(([, o]) => (o as RequestInit | undefined)?.method === 'PATCH')

let patchGate: Promise<unknown> | null

beforeEach(() => {
  vi.clearAllMocks()
  patchGate = null
  vi.mocked(api.knowledgeApi).mockImplementation(((path: string, opts?: RequestInit) => {
    if (opts?.method) return patchGate ?? Promise.resolve({ ok: true })
    if (path.endsWith('/related')) return Promise.resolve([])
    return Promise.resolve(ITEM)
  }) as typeof api.knowledgeApi)
})

afterEach(cleanup)

async function openEditor(): Promise<HTMLElement> {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <DetailView itemId="i1" onBack={vi.fn()} />
      <button type="button" data-testid="elsewhere">Elsewhere</button>
    </QueryClientProvider>,
  )
  fireEvent.click(await screen.findByRole('button', { name: EDIT }))
  const input = screen.getByLabelText(TAGS)
  expect(input).toHaveFocus()
  return input
}

describe('knowledge tag editor focus', () => {
  it('Enter saves and returns focus to the Edit button', async () => {
    const input = await openEditor()
    fireEvent.change(input, { target: { value: 'ops, ledger' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => expect(tagField()).toBeNull())
    await waitFor(() => expect(trigger()).toHaveFocus())
    expect(patches()).toHaveLength(1)
  })

  it('Escape closes and returns focus to the Edit button', async () => {
    const input = await openEditor()
    fireEvent.keyDown(input, { key: 'Escape' })
    expect(tagField()).toBeNull()
    await waitFor(() => expect(trigger()).toHaveFocus())
    expect(patches()).toHaveLength(0)
  })

  // Passes on main too: React reuses the Save button's DOM node as the Edit
  // button (same element type at the same child index), so focus never drops.
  // Kept so a markup change that breaks that reuse cannot strand focus.
  it('the Save button saves and returns focus to the Edit button', async () => {
    await openEditor()
    const save = screen.getByRole('button', { name: SAVE })
    act(() => { save.focus() })
    fireEvent.click(save)
    await waitFor(() => expect(tagField()).toBeNull())
    await waitFor(() => expect(trigger()).toHaveFocus())
  })

  it('the Cancel button closes and returns focus to the Edit button', async () => {
    await openEditor()
    const cancel = screen.getByRole('button', { name: CANCEL })
    act(() => { cancel.focus() })
    fireEvent.click(cancel)
    expect(tagField()).toBeNull()
    await waitFor(() => expect(trigger()).toHaveFocus())
  })

  it('a save that resolves after the user moved on leaves focus where they put it', async () => {
    let resolve!: (v: unknown) => void
    patchGate = new Promise((r) => { resolve = r })
    const input = await openEditor()
    fireEvent.keyDown(input, { key: 'Enter' })
    const elsewhere = screen.getByTestId('elsewhere')
    act(() => { elsewhere.focus() })
    await act(async () => { resolve({ ok: true }) })
    await waitFor(() => expect(tagField()).toBeNull())
    await nextFrame()
    expect(elsewhere).toHaveFocus()
  })
})
