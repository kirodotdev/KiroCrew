import { screen, waitFor, act } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { renderWithProviders } from './helpers'
import { IssueRadarProvider, useIssueRadar } from '../apps/issue-radar/context'
import Workspace from '../apps/issue-radar/Workspace'

// When a label filter excludes the SELECTED issue/PR, `activeIssue`/`activePull`
// resolve to null (the filtered list has no fallback, by design). The detail pane
// must then say a FILTER is hiding the selection and offer a way out — not render
// the same "Select an issue" placeholder a brand-new, nothing-selected app shows.
// Because the selection AND the filters are both persisted, the generic placeholder
// otherwise survives a tab switch or reload with no hint, which reads as "nothing
// loads". This pins the distinct placeholder + Clear filters affordance for both panes.

const issues = vi.fn()
const labels = vi.fn()
const pulls = vi.fn()

vi.mock('../apps/issue-radar/api', async (importOriginal) => ({
  ...(await importOriginal<object>()),
  issueRadarApi: {
    me: () => Promise.resolve({ login: 'octocat' }),
    issues: (...a: unknown[]) => issues(...a),
    labels: (...a: unknown[]) => labels(...a),
    members: () => Promise.resolve({ members: [] }),
    getSettings: () => Promise.resolve({ settings: null }),
    pulls: (...a: unknown[]) => pulls(...a),
    searchPulls: () => Promise.resolve({ pulls: [] }),
  },
}))

// The detail panes fetch their own item + timeline and pull in redux/router-heavy
// buttons; this test is about Workspace's PLACEHOLDER branching (activeIssue null
// vs present), not detail rendering, so stub them to a marker naming the item.
vi.mock('../apps/issue-radar/components/IssueDetail', () => ({
  default: ({ issue }: { issue: { number: number } }) => <div>issue-detail-{issue.number}</div>,
}))
vi.mock('../apps/issue-radar/components/PrDetail', () => ({
  default: ({ pull }: { pull: { number: number } }) => <div>pr-detail-{pull.number}</div>,
}))

const REPO = { owner: 'kirodotdev', repo: 'Kiro' }

function renderWorkspace(ui?: React.ReactNode) {
  return renderWithProviders(
    <IssueRadarProvider repos={[REPO]} active={REPO} onSwitch={() => {}} onAddRepo={() => {}}>
      <Workspace />
      {ui}
    </IssueRadarProvider>,
  )
}

/** Drives the context the way the rail would: open the issue view, select #2, then
 * toggle the `bug` label (which #2 does NOT carry) so the selection is filtered out. */
function Driver() {
  const { openIssues, setSelectedIssue, toggleLabel } = useIssueRadar()
  return (
    <div>
      <button data-testid="open-issues" onClick={openIssues}>issues</button>
      <button data-testid="select-2" onClick={() => setSelectedIssue(2)}>select 2</button>
      <button data-testid="filter-bug" onClick={() => toggleLabel('bug')}>bug</button>
    </div>
  )
}

beforeEach(() => {
  localStorage.clear()
  labels.mockReset().mockResolvedValue({
    labels: [{ name: 'bug', color: 'ff0000', description: '' }],
  })
  pulls.mockReset().mockResolvedValue({ pulls: [] })
  issues.mockReset().mockResolvedValue({
    issues: [
      { number: 1, title: 'Has bug', state: 'open', labels: ['bug'], updated_at: '2026-07-01T00:00:00Z' },
      { number: 2, title: 'No labels', state: 'open', labels: [], updated_at: '2026-07-02T00:00:00Z' },
    ],
  })
})

afterEach(() => vi.clearAllMocks())

describe('detail pane when the selection is hidden by a filter', () => {
  it('names the filter and offers Clear filters, instead of the generic placeholder', async () => {
    renderWorkspace(<Driver />)
    await waitFor(() => expect(issues).toHaveBeenCalled())

    await act(async () => { screen.getByTestId('open-issues').click() })
    await act(async () => { screen.getByTestId('select-2').click() })
    // #2 is selected and open — the detail pane renders it, not a placeholder.
    await waitFor(() => expect(screen.getByText('issue-detail-2')).toBeInTheDocument())

    // Filter to `bug`, which #2 lacks: it leaves the list and the detail clears.
    await act(async () => { screen.getByTestId('filter-bug').click() })

    await waitFor(() =>
      expect(screen.getByText(/hidden by the active filters/i)).toBeInTheDocument())
    // NOT the empty-app placeholder.
    expect(screen.queryByText('Select an issue to see its details.')).toBeNull()

    // Clearing the filter brings the selection back.
    await userEvent.click(screen.getByRole('button', { name: /clear filters/i }))
    await waitFor(() =>
      expect(screen.queryByText(/hidden by the active filters/i)).toBeNull())
  })

  it('shows the generic placeholder when nothing is selected (no filter active)', async () => {
    renderWorkspace(<Driver />)
    await waitFor(() => expect(issues).toHaveBeenCalled())
    await act(async () => { screen.getByTestId('open-issues').click() })
    // Nothing selected, no filter — the ordinary empty state, not the filter message.
    expect(screen.getByText('Select an issue to see its details.')).toBeInTheDocument()
    expect(screen.queryByText(/hidden by the active filters/i)).toBeNull()
  })
})
