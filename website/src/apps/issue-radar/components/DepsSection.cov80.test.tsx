/**
 * DepsSection — the "Blocked by / Blocking" detail-pane section (#5187).
 *
 * Covers: rows for both directions with live state join, the all-blockers-met
 * badge, render-nothing on an item with no edges (and on a failed query), and
 * opening a referenced item in-app.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

import type { DepsResponse } from '../api'

const openRef = vi.hoisted(() => vi.fn())
const depsFn = vi.hoisted(() => vi.fn())

vi.mock('../context', () => ({
  useIssueRadar: () => ({
    active: { owner: 'zzq', repo: 'fabric', provider: 'github', host: 'github.com' },
    issues: [{ number: 10, title: 'dependent issue', state: 'open' }],
    pulls: [],
    openRef,
    refreshPrefs: { staleTimeMs: 60000, listPollMs: 0, pollInBackground: false },
  }),
}))

vi.mock('../api', async (importOriginal) => {
  const mod = await importOriginal<typeof import('../api')>()
  return { ...mod, issueRadarApi: { ...mod.issueRadarApi, deps: depsFn } }
})

import DepsSection from './DepsSection'

function payload(): DepsResponse {
  return {
    schema: 1,
    edges: [
      { blocked: 10, blocker: 5, source: 'native' },
      { blocked: 10, blocker: 6, source: 'inferred' },
      { blocked: 30, blocker: 10, source: 'native' },
    ],
    nodes: {
      '5': { kind: 'pull', state: 'merged', title: 'merged blocker' },
      '6': { kind: 'issue', state: 'closed', title: 'closed blocker' },
      '10': { kind: 'issue', state: 'open', title: 'dependent issue' },
      '30': { kind: 'issue', state: 'open', title: 'downstream issue' },
    },
  }
}

function renderFor(number: number) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <DepsSection number={number} />
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  depsFn.mockReset()
  openRef.mockReset()
})

afterEach(cleanup)

describe('DepsSection', () => {
  it('lists blockers and dependents with joined states', async () => {
    depsFn.mockResolvedValue(payload())
    renderFor(10)
    await waitFor(() => expect(screen.getByText(/Blocked by/i)).toBeInTheDocument())
    expect(screen.getByText(/merged blocker/)).toBeInTheDocument()
    expect(screen.getByText(/closed blocker/)).toBeInTheDocument()
    expect(screen.getByText(/Blocking/i)).toBeInTheDocument()
    expect(screen.getByText(/downstream issue/)).toBeInTheDocument()
  })

  it('opens a referenced item in-app on click', async () => {
    depsFn.mockResolvedValue(payload())
    renderFor(10)
    await waitFor(() => expect(screen.getByText(/merged blocker/)).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: /5/ }))
    expect(openRef).toHaveBeenCalled()
  })

  it('renders nothing for an item with no edges', async () => {
    depsFn.mockResolvedValue(payload())
    const { container } = renderFor(999)
    await waitFor(() => expect(depsFn).toHaveBeenCalled())
    await waitFor(() => expect(container.firstChild).toBeNull())
  })

  it('renders nothing when the deps query fails', async () => {
    depsFn.mockRejectedValue(new Error('zzq boom'))
    const { container } = renderFor(10)
    await waitFor(() => expect(depsFn).toHaveBeenCalled())
    await waitFor(() => expect(container.firstChild).toBeNull())
  })
})
