/**
 * The import card's name field (rfc-crewmate-share.md §5.4): while a
 * `crewmate.import` card is pending or failed the name is edited on the card
 * before the press, re-previewed after a pause, and the press waits until the
 * card holds the name on screen.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, fireEvent, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { Provider } from 'react-redux'
import { useState } from 'react'
import { store as reduxStore } from '../store'
import { ApiError } from '../api/apiError'
import type { Card } from '../api/cards'
import { i18nT } from '../i18n/t'

const preview = vi.fn()
vi.mock('../api/cards', async importOriginal => {
  const actual = await importOriginal<typeof import('../api/cards')>()
  return { ...actual, cardsApi: { ...actual.cardsApi, preview: (...a: unknown[]) => preview(...a) } }
})

import ChangeCard from './ChangeCard'
import { PREVIEW_DEBOUNCE_MS } from './ImportNameField'
import { problemText } from './refusals'

const NOW = 1_800_000_000_000

const importCard = (over: Partial<Card> = {}): Card => ({
  id: 'imp-1',
  slot_key: 'owner:crewmate-import',
  kind: 'crewmate.import',
  revision: 1,
  status: 'pending',
  risk: 'normal',
  title: 'Import Bot “Atlas 3”',
  changes: [
    { field: 'name', label: 'Name', after: 'Atlas 3' },
    { field: 'description', label: 'Description', after: 'Writes the digest.' },
    { field: 'approval_mode', label: 'Approval', after: 'normal' },
  ],
  editable: ['name'],
  params: { name: 'Atlas 3', format_version: 1, description: 'Writes the digest.' },
  plan: { apply: [{ method: 'POST', path: '/api/agents', body: { name: 'Atlas 3' } }], undo: null },
  created_at: NOW,
  expires_at: NOW + 86_400_000,
  ...over,
})

/** The card as the import dialog holds it: a re-preview's answer replaces it.
 *  `deferred` stores the answer a task after the request settles, as the
 *  change-cards query cache does. */
function Host({ initial, deferred = false }: { initial: Card; deferred?: boolean }) {
  const [card, setCard] = useState(initial)
  const store = (c: Card | null) => {
    if (!c) return
    if (deferred) window.setTimeout(() => setCard(c), 0)
    else setCard(c)
  }
  return <ChangeCard card={card} store={store} refresh={() => {}} remove={() => {}} />
}

function mount(card: Card, { deferred = false } = {}) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <Provider store={reduxStore}>
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <Host initial={card} deferred={deferred} />
        </MemoryRouter>
      </QueryClientProvider>
    </Provider>,
  )
}

/** The card as a second view holds it: its updates come from outside the field. */
function mountShown(card: Card) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const tree = (c: Card) => (
    <Provider store={reduxStore}>
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <ChangeCard card={c} store={() => {}} refresh={() => {}} remove={() => {}} />
        </MemoryRouter>
      </QueryClientProvider>
    </Provider>
  )
  const view = render(tree(card))
  return { show: (c: Card) => view.rerender(tree(c)) }
}

const input = () => screen.getByTestId('change-card-name-input') as HTMLInputElement
const press = () => screen.getByTestId('change-card-apply')
const type = (value: string) => fireEvent.change(input(), { target: { value } })
const waitOut = () => act(async () => { await vi.advanceTimersByTimeAsync(PREVIEW_DEBOUNCE_MS) })
const refusal = (code: string) => new ApiError(409, 'raw server text', JSON.stringify({ error: 'raw server text', code }))

beforeEach(() => {
  preview.mockReset()
  vi.useFakeTimers({ now: NOW })
})

afterEach(() => {
  vi.useRealTimers()
})

describe('the import card name field', () => {
  it('replaces the read-only Name row while the card is pending', () => {
    mount(importCard())
    expect(input().value).toBe('Atlas 3')
    expect(input()).toHaveAccessibleName(i18nT('components.changeCards.field_name'))
    // The Name row is gone: its value now lives only in the field.
    expect(screen.getByTestId('change-card-rows').textContent).not.toContain('Atlas 3')
    expect(press()).toBeEnabled()
  })

  it('shows the field on a failed card too, and keeps the Name row in every other state', () => {
    const { unmount } = mount(importCard({ status: 'failed' }))
    expect(input().value).toBe('Atlas 3')
    unmount()
    mount(importCard({ status: 'applying' }))
    expect(screen.queryByTestId('change-card-name-input')).toBeNull()
    expect(screen.getByTestId('change-card-rows').textContent).toContain('Atlas 3')
  })

  it('re-previews the trimmed name once after the pause, holding the press until the card has it', async () => {
    preview.mockImplementation(async (c: Card, params: Record<string, unknown>) =>
      importCard({ revision: c.revision + 1, params, title: `Import Bot “${String(params.name)}”` }))
    mount(importCard())
    type('  Atlas Copy  ')
    // Typed but not yet on the card: the press waits, and nothing is sent before the pause.
    expect(press()).toBeDisabled()
    await act(async () => { await vi.advanceTimersByTimeAsync(PREVIEW_DEBOUNCE_MS - 1) })
    expect(preview).not.toHaveBeenCalled()
    await act(async () => { await vi.advanceTimersByTimeAsync(1) })
    expect(preview).toHaveBeenCalledTimes(1)
    expect(preview.mock.calls[0][1]).toEqual({ name: 'Atlas Copy', format_version: 1, description: 'Writes the digest.' })
    expect(preview.mock.calls[0][0]).toMatchObject({ id: 'imp-1', revision: 1 })
    expect(press()).toBeEnabled()
    expect(screen.getByRole('heading', { level: 3 })).toHaveTextContent('Atlas Copy')
    expect(input().value).toBe('  Atlas Copy  ')
  })

  it('holds the press while the re-preview is in flight', async () => {
    let answer: (c: Card) => void = () => {}
    preview.mockImplementation((c: Card, params: Record<string, unknown>) =>
      new Promise<Card>(resolve => { answer = () => resolve(importCard({ revision: c.revision + 1, params })) }))
    mount(importCard())
    type('Atlas Copy')
    await waitOut()
    expect(preview).toHaveBeenCalledTimes(1)
    expect(press()).toBeDisabled()
    await act(async () => { answer(importCard()) })
    expect(press()).toBeEnabled()
  })

  it('sends a name typed back during a re-preview once that re-preview lands', async () => {
    let answer: () => void = () => {}
    preview.mockImplementation((c: Card, params: Record<string, unknown>) =>
      new Promise<Card>(resolve => { answer = () => resolve(importCard({ revision: c.revision + 1, params })) }))
    mount(importCard(), { deferred: true })
    const stored = () => act(async () => { await vi.advanceTimersByTimeAsync(0) })
    type('Atlas 3x')
    await waitOut()
    expect(preview).toHaveBeenCalledTimes(1)
    // Back to the card's old name while the card is being renamed to "Atlas 3x".
    type('Atlas 3')
    await act(async () => { answer() })
    expect(press()).toBeDisabled()
    await stored()
    await waitOut()
    expect(preview).toHaveBeenCalledTimes(2)
    expect(preview.mock.calls[1][1]).toMatchObject({ name: 'Atlas 3' })
    await act(async () => { answer() })
    await stored()
    expect(press()).toBeEnabled()
  })

  it('never sends an empty name and says to enter one, as a hint rather than an error', async () => {
    mount(importCard())
    type('   ')
    await waitOut()
    expect(preview).not.toHaveBeenCalled()
    expect(screen.getByTestId('change-card-name-hint')).toHaveTextContent(i18nT('components.changeCards.import_name_empty'))
    expect(input()).toHaveAccessibleDescription(i18nT('components.changeCards.import_name_empty'))
    expect(input()).toHaveAttribute('aria-invalid', 'true')
    // Nothing failed yet, so nothing is drawn as an error.
    expect(screen.queryByTestId('change-card-name-error')).toBeNull()
    expect(screen.queryByRole('alert')).toBeNull()
    expect(press()).toBeDisabled()
  })

  it('takes a name the card gets from elsewhere into an untouched field, and sends nothing back', async () => {
    const view = mountShown(importCard())
    // Another view of the same card renamed it.
    view.show(importCard({ revision: 2, title: 'Import Bot “Atlas Copy”', params: { ...importCard().params, name: 'Atlas Copy' } }))
    expect(input().value).toBe('Atlas Copy')
    await waitOut()
    await waitOut()
    expect(preview).not.toHaveBeenCalled()
    expect(press()).toBeEnabled()
  })

  it('words a taken name in its own line and keeps the press off', async () => {
    preview.mockRejectedValue(refusal('already_exists'))
    mount(importCard())
    type('Atlas 2')
    await waitOut()
    expect(preview).toHaveBeenCalledTimes(1)
    const line = screen.getByTestId('change-card-name-error')
    expect(line).toHaveTextContent(i18nT('components.changeCards.import_name_taken'))
    expect(line.textContent).not.toContain('raw server text')
    expect(screen.queryByTestId('change-card-error')?.textContent ?? '').toBe('')
    expect(press()).toBeDisabled()
    // The refused name is not sent again while it stays typed.
    await waitOut()
    expect(preview).toHaveBeenCalledTimes(1)
  })

  it('words any other refusal of the name as one that cannot be used', async () => {
    preview.mockRejectedValue(refusal('invalid_param'))
    mount(importCard())
    type('Atlas/2')
    await waitOut()
    expect(screen.getByTestId('change-card-name-error')).toHaveTextContent(i18nT('components.changeCards.import_name_invalid'))
    expect(press()).toBeDisabled()
  })

  it('keeps the shared refusal wording for every other card kind', () => {
    expect(problemText({ code: 'already_exists', message: 'An MCP server with that name exists' })).toBe('An MCP server with that name exists')
  })

  it('holds the name to 24 code points, counted as the gateway counts them', () => {
    mount(importCard())
    type('Ω'.repeat(20) + '😀'.repeat(10))
    expect(Array.from(input().value)).toHaveLength(24)
    expect(input().value).toBe('Ω'.repeat(20) + '😀'.repeat(4))
  })
})
