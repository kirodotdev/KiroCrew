/**
 * The CI-failure toggle on a repo's settings page is shown exactly for the forges
 * the background watch covers (backend `ci_watch.CI_WATCH_PROVIDERS`: GitHub and
 * GitLab), and its copy speaks the forge's own words. Before this the toggle was
 * gated on GitHub alone, so a GitLab project could never opt in even though the
 * backend accepted the setting; and the copy said "pull requests" / "GitHub
 * Actions" / "gh" on every forge.
 *
 * The page's data is stubbed at the api module: the settings query resolves to the
 * defaults, every other query resolves empty, so the page renders its notification
 * card with no network and no provider CLI.
 */
import { describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

vi.mock('../../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../api')>()
  return {
    ...actual,
    issueRadarApi: {
      ...actual.issueRadarApi,
      labels: vi.fn(async () => ({ labels: [] })),
      getSettings: vi.fn(async () => ({ settings: { ...actual.DEFAULT_REPO_SETTINGS }, revision: 1 })),
      issues: vi.fn(async () => ({ issues: [] })),
      members: vi.fn(async () => ({ members: [] })),
      getCrewSettings: vi.fn(async () => ({})),
    },
  }
})

vi.mock('../../context', () => ({
  useIssueRadar: () => ({
    repos: [],
    active: null,
    openSettings: vi.fn(),
    openDashboard: vi.fn(),
    switchRepo: vi.fn(),
  }),
}))

vi.mock('./CrewProtocolSettings', () => ({ default: () => <span data-testid="crew-protocol" /> }))
vi.mock('../../../../components/ProjectPicker', () => ({ default: () => null }))

const { default: RepoSettings, CI_WATCH_PROVIDERS } = await import('./RepoSettings')

function renderFor(repoRef: { owner: string; repo: string; provider?: 'github' | 'gitlab' | 'azure'; host?: string }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <RepoSettings repoRef={repoRef} />
    </QueryClientProvider>,
  )
}

/** The notification card has rendered once the new-issue toggle is on screen. */
async function settled() {
  await waitFor(() => expect(screen.getAllByRole('switch').length).toBeGreaterThan(0))
}

describe('RepoSettings — the CI-failure toggle is shown for the forges the watch covers', () => {
  it('mirrors the backend set: GitHub and GitLab, not Azure', () => {
    expect([...CI_WATCH_PROVIDERS].sort()).toEqual(['github', 'gitlab'])
  })

  it('renders the toggle for a GitLab project, in merge-request words', async () => {
    renderFor({ owner: 'grp', repo: 'proj', provider: 'gitlab', host: 'gitlab.acme.internal' })
    await settled()
    const toggle = screen.getByRole('switch', { name: /CI fails on one of my merge requests in grp\/proj/ })
    expect(toggle).toBeInTheDocument()
    expect(screen.getByText(/Reads GitLab pipeline runs only on your own open merge requests/)).toBeInTheDocument()
    expect(screen.getByText(/through your glab sign-in/)).toBeInTheDocument()
    expect(screen.queryByText(/GitHub Actions/)).toBeNull()
  })

  it('renders the toggle for a GitHub repo, in pull-request words', async () => {
    renderFor({ owner: 'o', repo: 'r' })
    await settled()
    expect(screen.getByRole('switch', { name: /CI fails on one of my pull requests in o\/r/ })).toBeInTheDocument()
    expect(screen.getByText(/Reads GitHub Actions runs only on your own open pull requests/)).toBeInTheDocument()
    expect(screen.getByText(/through your gh sign-in/)).toBeInTheDocument()
  })

  it('hides the toggle for an Azure DevOps repo', async () => {
    renderFor({ owner: 'org', repo: 'proj', provider: 'azure', host: 'dev.azure.com' })
    await settled()
    expect(screen.queryByRole('switch', { name: /CI fails/ })).toBeNull()
    expect(screen.queryByText(/Reads .* runs only on your own open/)).toBeNull()
  })
})
