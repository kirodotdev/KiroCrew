/**
 * A decline must not follow the user to a different item.
 *
 * `Workspace.tsx` renders `<IssueDetail issue={activeIssue} />` with NO `key`, so
 * selecting another issue re-renders the SAME component instance with a new prop.
 * While the declined state was a bare boolean it survived that switch: item B
 * rendered the re-run affordance it never earned, and B's first click passed
 * `force`, skipping the guard and creating a session against B -- on an item
 * nobody had declined.
 *
 * The state is now the DECLINED ITEM'S IDENTITY, compared at render, so there is
 * also no window where the wrong label is painted (a reset effect would leave
 * one). This pins the leak closed from the outside: same mounted component, new
 * item, resting affordance.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'

const { api, session } = vi.hoisted(() => ({
  api: { getInvestigation: vi.fn() },
  session: { concludedFor: null as string | null },
}))

vi.mock('../apps/issue-radar/api', () => ({ issueRadarApi: api }))
vi.mock('../apps/issue-radar/lib/investigate', () => ({
  useInvestigate: () => ({
    investigate: vi.fn(),
    busy: false,
    error: null,
    concludedFor: session.concludedFor,
  }),
}))

const InvestigateButton = (await import('../apps/issue-radar/components/InvestigateButton')).default
const { itemKey } = await import('../apps/issue-radar/lib/agentSession')

const REF = { owner: 'acme', repo: 'demo', provider: 'github', host: 'github.com' } as never
const issueAt = (n: number) => ({ number: n, title: `issue ${n}`, labels: [] }) as never

const wrap = (ui: ReactNode) => {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>)
}

describe('a declined click is scoped to the item it was declined on', () => {
  beforeEach(() => {
    vi.resetAllMocks()
    api.getInvestigation.mockResolvedValue({
      investigation: {
        slot_key: 'chat-closed', status: 'resolved', findings: { verdict: 'bug' },
      },
    })
  })

  it('keeps the re-run affordance on the declined item', async () => {
    session.concludedFor = itemKey(REF, 101)
    wrap(<InvestigateButton repoRef={REF} issue={issueAt(101)} />)
    await waitFor(() => expect(screen.getByRole('button', { name: /Start over/i })).toBeTruthy())
  })

  it('does NOT carry it to another item in the same mounted pane', async () => {
    session.concludedFor = itemKey(REF, 101)
    // Re-rendering the SAME element type with a different issue is exactly what
    // selecting another row does, since the pane is not keyed by item.
    const { rerender } = wrap(<InvestigateButton repoRef={REF} issue={issueAt(101)} />)
    await waitFor(() => expect(screen.getByRole('button', { name: /Start over/i })).toBeTruthy())

    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    rerender(
      <QueryClientProvider client={qc}>
        <InvestigateButton repoRef={REF} issue={issueAt(202)} />
      </QueryClientProvider>,
    )
    await waitFor(() => expect(screen.getByRole('button', { name: /Resume/i })).toBeTruthy())
    expect(screen.queryByRole('button', { name: /Start over/i })).toBeNull()
  })

  it('does NOT carry it across kinds on the same number', async () => {
    // GitLab numbers issues and merge requests independently, so issue 5 and MR 5
    // are unrelated items that would otherwise share a key.
    expect(itemKey(REF, 5, 'issue')).not.toBe(itemKey(REF, 5, 'pull'))
  })

  it('does NOT carry it across repositories on the same number', async () => {
    const other = { ...(REF as object), repo: 'other' } as never
    expect(itemKey(REF, 5)).not.toBe(itemKey(other, 5))
  })
})
