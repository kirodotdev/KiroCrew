/**
 * DepsSection on a forge with SEPARATE issue / merge-request number spaces
 * (GitLab). The detail pane resolves blockers through the same
 * `resolveLifecycle` as the Graph tab, so issue #5 never reads the lifecycle of
 * an unrelated MR !5 — and the same payload on GitHub still lets the live PR
 * row win.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, render, screen, waitFor } from '@testing-library/react'

import type { DepsResponse } from '../api'

const depsFn = vi.hoisted(() => vi.fn())
const ctx = vi.hoisted(() => ({
  provider: 'gitlab' as 'gitlab' | 'github',
}))

vi.mock('../context', () => ({
  useIssueRadar: () => ({
    active: {
      owner: 'grp',
      repo: 'proj',
      provider: ctx.provider,
      host: ctx.provider === 'gitlab' ? 'gitlab.example.com' : 'github.com',
    },
    // The open-issue list: #6 only. Blocker #5 is CLOSED, so it is absent.
    issues: [{ number: 6, title: 'dependent issue', state: 'open' }],
    // An unrelated OPEN merge request shares the blocker's number.
    pulls: [{ number: 5, title: 'unrelated MR five', state: 'open', merged_at: null }],
    openRef: vi.fn(),
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
    edges: [{ blocked: 6, blocker: 5, source: 'native' }],
    nodes: {
      '5': { kind: 'issue', state: 'closed', title: 'closed issue five' },
      '6': { kind: 'issue', state: 'open', title: 'dependent issue' },
    },
  }
}

function renderFor(number: number, kind: 'issue' | 'pull' = 'issue') {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <DepsSection number={number} kind={kind} />
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  depsFn.mockReset()
})

afterEach(cleanup)

describe('DepsSection — separate number spaces', () => {
  it('on GitLab shows the closed ISSUE blocker, not the MR that shares its number', async () => {
    ctx.provider = 'gitlab'
    depsFn.mockResolvedValue(payload())
    renderFor(6)
    await waitFor(() => expect(screen.getByText(/Blocked by/i)).toBeInTheDocument())
    expect(screen.getByText(/closed issue five/)).toBeInTheDocument()
    expect(screen.queryByText(/unrelated MR five/)).not.toBeInTheDocument()
  })

  it('on GitHub the live PR row still wins for the same payload', async () => {
    ctx.provider = 'github'
    depsFn.mockResolvedValue(payload())
    renderFor(6)
    await waitFor(() => expect(screen.getByText(/Blocked by/i)).toBeInTheDocument())
    expect(screen.getByText(/unrelated MR five/)).toBeInTheDocument()
    expect(screen.queryByText(/closed issue five/)).not.toBeInTheDocument()
  })

  it('on GitLab an MR detail pane does NOT borrow the issue edges that share its iid', async () => {
    // The deps graph is issue-to-issue, so node "6" is an issue. The issue pane
    // for iid 6 shows issue #5 as its blocker; the MR pane for the SAME iid must
    // show nothing, not borrow that row. Render the issue pane first so the query
    // has resolved (proven by the blocker row), then rerender as the MR pane on
    // the same client: with the kind gate off this row would still be present.
    ctx.provider = 'gitlab'
    depsFn.mockResolvedValue(payload())
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const { rerender, container } = render(
      <QueryClientProvider client={qc}>
        <DepsSection number={6} kind="issue" />
      </QueryClientProvider>,
    )
    await waitFor(() => expect(screen.getByText(/closed issue five/)).toBeInTheDocument())
    rerender(
      <QueryClientProvider client={qc}>
        <DepsSection number={6} kind="pull" />
      </QueryClientProvider>,
    )
    await waitFor(() => expect(screen.queryByText(/closed issue five/)).not.toBeInTheDocument())
    expect(screen.queryByText(/Blocked by/i)).not.toBeInTheDocument()
    expect(container).toBeEmptyDOMElement()
  })
})
