/**
 * The new/edit-hook card's lifecycle-event picker, after the native `<select>`
 * was replaced by `SimpleSelect` (Radix Select).
 *
 * Two things the migration changed and this pins:
 *  - the control now HAS an accessible name (it had none as a native select),
 *    reusing the page's existing "Event" catalog key;
 *  - an event value the picker doesn't offer (a legacy or hand-edited hook)
 *    shows the stored value on the trigger. A native select silently rendered
 *    the FIRST option while state held the stale value, so saving an untouched
 *    form appeared to change the event.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

const createHook = vi.fn().mockResolvedValue({})
const updateHook = vi.fn().mockResolvedValue({})
let hooksPayload: { hooks: unknown[] } = { hooks: [] }

vi.mock('../api/client', () => ({
  api: new Proxy({} as Record<string, unknown>, {
    get: (_t, prop: string) => {
      if (prop === 'hooks') return vi.fn(async () => hooksPayload)
      if (prop === 'createHook') return createHook
      if (prop === 'updateHook') return updateHook
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

/** Open the "New Hook" card and return its event picker trigger. */
async function openForm() {
  fireEvent.click(await screen.findByRole('button', { name: '+ New Hook' }))
  return screen.findByLabelText('Event')
}

beforeEach(() => {
  vi.clearAllMocks()
  hooksPayload = { hooks: [] }
})

describe('hooks page — lifecycle event picker', () => {
  it('labels the picker and shows the default event', async () => {
    renderPage()
    const trigger = await openForm()
    expect(trigger.tagName).toBe('BUTTON')
    expect(trigger).toHaveTextContent('UserPromptSubmit')
  })

  it('offers every lifecycle event and commits the pick', async () => {
    renderPage()
    const trigger = await openForm()

    // Radix Select: open, then click — a `change` on the trigger does nothing.
    fireEvent.click(trigger)
    await waitFor(() => expect(screen.getAllByRole('option')).toHaveLength(5))
    expect(screen.getAllByRole('option').map(o => o.textContent)).toEqual([
      'AgentSpawn', 'UserPromptSubmit', 'PreToolUse', 'PostToolUse', 'Stop',
    ])

    fireEvent.click(screen.getByRole('option', { name: 'PreToolUse' }))
    await waitFor(() => expect(screen.getByLabelText('Event')).toHaveTextContent('PreToolUse'))

    // The matcher placeholder switches to the tool-filter copy, proving the
    // pick reached the form state and not just the trigger's own label.
    expect(screen.getByPlaceholderText(/Matcher \(tool filter/)).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(createHook).toHaveBeenCalledTimes(1))
    expect(createHook.mock.calls[0][0]).toMatchObject({ event: 'PreToolUse' })
  })

  it('shows a stored event the picker no longer offers instead of the first option', async () => {
    hooksPayload = {
      hooks: [{
        id: 'h1', name: 'legacy', event: 'agentSpawn', matcher: '', command: 'true',
        timeout: 30, enabled: true, last_run: 0, last_status: '', run_count: 0,
      }],
    }
    renderPage()

    fireEvent.click(await screen.findByRole('button', { name: 'Edit' }))
    const trigger = await screen.findByLabelText('Event')
    expect(trigger).toHaveTextContent('agentSpawn')
    expect(trigger).not.toHaveTextContent('AgentSpawn')

    // Saving without touching the picker must not silently rewrite the event.
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(updateHook).toHaveBeenCalledTimes(1))
    expect(updateHook.mock.calls[0][1]).toMatchObject({ event: 'agentSpawn' })
  })
})
