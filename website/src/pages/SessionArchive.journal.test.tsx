/**
 * #18723: a dashboard component that owns its own `fetch('/api/…')` must route
 * it through the journaled transport, so a network drop (the one failure class
 * with no server-side log — the request never arrived) is recorded with its
 * route and method. Before the fix `SessionArchive` called the browser's `fetch`
 * directly, caught the rejection with `String(error)`, and handed `ErrorNotice`
 * a bare "Failed to fetch" with no entry in the journal — so the Ask-agent
 * hand-off had the message but no endpoint to resolve it to.
 *
 * This renders the REAL component with a rejecting `fetch` and asserts the
 * journal now carries a report for `/api/session/archive`. It is the in-product
 * guard for the fix: route `SessionArchive` back onto raw `fetch` and this
 * reddens.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, waitFor, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import SessionArchive from './SessionArchive'
import {
  recentErrors,
  reportForError,
  consumeChatHandoff,
  __resetErrorJournalForTests,
} from '../utils/errorReport'

beforeEach(() => {
  __resetErrorJournalForTests()
  sessionStorage.clear()
})

function renderArchive() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <SessionArchive />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('SessionArchive transport-rejection journaling (#18723)', () => {
  it('journals the list-load endpoint and method when the fetch rejects with no Response', async () => {
    // A network drop: `fetch` rejects before any Response, the class #9579/#12433
    // named and the one a bare `fetch` never recorded.
    vi.stubGlobal('fetch', vi.fn(async () => { throw new TypeError('Failed to fetch') }) as unknown as typeof fetch)

    const { findByTestId } = renderArchive()

    // The error notice still renders — the component's own catch is untouched.
    await findByTestId('session-archive-list-error')

    // …but now the journal has the route+method the Ask-agent hand-off needs.
    await waitFor(() => {
      const report = recentErrors().find(r => r.endpoint === '/api/session/archive')
      expect(report, 'expected a journal entry for /api/session/archive').toBeTruthy()
      expect(report!.method).toBe('GET')
      expect(report!.code).toBe('network')
      // No HTTP status: nothing answered, so a synthetic one would send a reader
      // to audit a server that never spoke.
      expect(report!.status).toBeUndefined()
    })
  })

  it('pins the report to the rejection so the hand-off resolves THIS endpoint, not a message match', async () => {
    const rejection = new TypeError('Failed to fetch')
    vi.stubGlobal('fetch', vi.fn(async () => { throw rejection }) as unknown as typeof fetch)

    renderArchive()

    // The transport attaches the report to the error it rethrows; a notice reads
    // that first (`reportForError`) rather than resolving a shared "Failed to
    // fetch" message to whichever request journaled last.
    await waitFor(() => {
      expect(reportForError(rejection)?.endpoint).toBe('/api/session/archive')
    })
  })

  it('hands the route and method to the agent — the report reaches the Ask-agent prompt, not a message match', async () => {
    // The whole point of #18723: journaling is not enough if the notice resolves
    // the hand-off by MESSAGE (findReport excludes network drops). The page must
    // pass the pinned report to ErrorNotice so the prompt carries route+method.
    vi.stubGlobal('fetch', vi.fn(async () => { throw new TypeError('Failed to fetch') }) as unknown as typeof fetch)

    renderArchive()

    const askAgent = await screen.findByRole('button', { name: /ask the agent/i })
    await userEvent.click(askAgent)

    // The staged chat prompt names the exact request — GET /api/session/archive.
    const prompt = consumeChatHandoff()
    expect(prompt, 'expected a staged Ask-agent prompt').toBeTruthy()
    expect(prompt).toContain('/api/session/archive')
    expect(prompt).toMatch(/GET\s+\/api\/session\/archive/)
  })
})
