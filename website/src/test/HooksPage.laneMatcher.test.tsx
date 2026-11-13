/**
 * The `SessionLaneChanged` matcher on the new-hook card.
 *
 * The lane matcher is a whole-string glob over `added:<id>;` / `removed:<id>;`, keyed
 * on a lane's tag id, which no other screen shows. So the form states the grammar,
 * offers the lanes by name and writes the selector with the id, and says so when the
 * lane list cannot be read rather than offering an empty control.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import '../i18n/all'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

const createHook = vi.fn().mockResolvedValue({})
let chatTags: () => Promise<unknown> = async () => []

vi.mock('../api/client', () => ({
  api: new Proxy({} as Record<string, unknown>, {
    get: (_t, prop: string) => {
      if (prop === 'hooks') return vi.fn(async () => ({ hooks: [] }))
      if (prop === 'createHook') return createHook
      if (prop === 'chatTags') return () => chatTags()
      return vi.fn().mockResolvedValue({})
    },
  }),
}))

vi.mock('../providers', () => ({
  useProvider: () => ({
    id: 'acp',
    capabilities: { hooks: false },
    labels: { hooksSection: 'Provider hooks' },
    fetchProviderHooks: () => Promise.resolve({}),
  }),
}))

import HooksPage from '../pages/HooksPage'

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <HooksPage />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

/** Open the "New Hook" card and pick `SessionLaneChanged`. */
async function openLaneForm() {
  fireEvent.click(await screen.findByRole('button', { name: '+ New Hook' }))
  fireEvent.click(await screen.findByLabelText('Event'))
  fireEvent.click(await screen.findByRole('option', { name: 'SessionLaneChanged' }))
  await waitFor(() => expect(screen.getByLabelText('Event')).toHaveTextContent('SessionLaneChanged'))
}

const GRAMMAR = 'A session entering a lane matches *added:<id>;* and one leaving it *removed:<id>;*. A bare lane id matches nothing.'

beforeEach(() => {
  vi.clearAllMocks()
  chatTags = async () => [
    { id: 'review', name: 'Review', color: '#f59e0b', order: 1, status: true },
    { id: 'urgent', name: 'Urgent', color: '#ef4444', order: 2 },
    { id: '3f9a0c1b2d4e', name: 'Shipped', color: '#10b981', order: 3, status: true },
  ]
})

describe('hooks page — SessionLaneChanged matcher', () => {
  it('states the lane grammar in glob mode, and only there', async () => {
    renderPage()
    expect(await screen.findByRole('button', { name: '+ New Hook' })).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: '+ New Hook' }))
    // A prompt hook keeps the generic copy.
    expect(screen.queryByText(GRAMMAR)).toBeNull()

    fireEvent.click(await screen.findByLabelText('Event'))
    fireEvent.click(await screen.findByRole('option', { name: 'SessionLaneChanged' }))
    expect(await screen.findByText(GRAMMAR)).toBeTruthy()
    expect(screen.getByPlaceholderText('Matcher (optional, e.g. *added:<id>;*)')).toBeTruthy()

    // A regex is not a glob: the selectors above would be wrong advice there.
    fireEvent.click(screen.getByLabelText('Matcher mode'))
    fireEvent.click(await screen.findByRole('option', { name: 'regex' }))
    await waitFor(() => expect(screen.queryByText(GRAMMAR)).toBeNull())
  })

  it('offers the lanes by name and writes the entering selector with the lane id', async () => {
    renderPage()
    await openLaneForm()

    // Radix DropdownMenuTrigger opens on keyboard activation (Enter) in jsdom.
    fireEvent.keyDown(await screen.findByRole('button', { name: 'Insert a lane' }), { key: 'Enter' })
    const items = await screen.findAllByRole('menuitem')
    // Status tags only, in board order: an ordinary tag is not a lane and never fires.
    expect(items.map(i => i.textContent)).toEqual(['Review', 'Shipped'])

    fireEvent.click(screen.getByRole('menuitem', { name: 'Shipped' }))
    await waitFor(() => expect(screen.getByLabelText('Matcher')).toHaveValue('*added:3f9a0c1b2d4e;*'))

    fireEvent.change(screen.getByPlaceholderText('Hook name'), { target: { value: 'h' } })
    fireEvent.change(screen.getByPlaceholderText(/hook fired/), { target: { value: 'true' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(createHook).toHaveBeenCalledTimes(1))
    expect(createHook.mock.calls[0][0]).toMatchObject({
      event: 'SessionLaneChanged', matcher: '*added:3f9a0c1b2d4e;*', matcher_mode: 'glob',
    })
  })

  it('reports a failed lane load instead of an empty control', async () => {
    chatTags = async () => { throw new Error('HTTP 503') }
    renderPage()
    await openLaneForm()

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('Lanes could not be loaded')
    expect(alert).toHaveTextContent('HTTP 503')
    expect(screen.queryByRole('button', { name: 'Insert a lane' })).toBeNull()
    // The grammar still applies: a selector can be typed by hand.
    expect(screen.getByText(GRAMMAR)).toBeTruthy()
  })
})
