/**
 * What the host page hands down as "the active repository", and why a slug is not
 * an identity.
 *
 * THE DEFECT. `IssueRadarPage` resolved the active repository two ways and both
 * lost the forge. The fallback arm built `{owner, repo}` out of `repos[0]` and
 * dropped that record's provider and host; and the membership test compared owner
 * and repo alone, so a STORED slug-only pointer satisfied it and was handed back
 * unenriched. `loadActiveRepo` accepts such a pointer on purpose -- a value
 * persisted before GitLab support has no forge, and rejecting it would drop the
 * user's repository on upgrade -- so the legacy pointer was never healed even
 * though the connected record beside it carried the missing half.
 *
 * WHY THAT IS NOT MERELY INCOMPLETE. `repoScopeKey` resolves an absent
 * provider/host to public GitHub. A forge-less ref therefore reads as a DIFFERENT
 * repository: every surface keying a cache on `active` filed a GitLab or Azure
 * repository's issues, labels and settings under GitHub's key, and every request
 * carrying the ref omitted the provider the backend needs to answer for the right
 * forge.
 *
 * These assert on what crosses the boundary. `IssueRadarProvider` is stubbed to
 * print the `active` prop it receives, because that prop IS the contract -- every
 * consumer reads it from the context this page builds.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'

const { reposMock, stored } = vi.hoisted(() => ({
  reposMock: vi.fn(),
  stored: { active: null as null | Record<string, unknown> },
}))

vi.mock('./api', () => ({ issueRadarApi: { repos: reposMock } }))

// The persisted pointer. Read through `loadActiveRepo`, which is where a
// slug-only legacy value legitimately enters the app.
vi.mock('./lib/format', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./lib/format')>()
  return {
    ...actual,
    loadActiveRepo: () => stored.active,
    saveActiveRepo: vi.fn(),
    markAutoSelectFirstIssue: vi.fn(),
    patchUiState: vi.fn(),
  }
})

// The probe: what the page hands down is what every consumer will read.
vi.mock('./context', () => ({
  IssueRadarProvider: ({ active }: { active: Record<string, unknown> }) => (
    <span data-testid="active-identity">
      {`${active.owner}/${active.repo}|${active.provider ?? '-'}|${active.host ?? '-'}`}
    </span>
  ),
}))

vi.mock('./Workspace', () => ({ default: () => <span /> }))
vi.mock('./components/RefSheet', () => ({ default: () => <span /> }))
vi.mock('./WelcomeCarousel', () => ({ default: () => <span data-testid="welcome" /> }))
vi.mock('./ConnectRepoModal', () => ({ default: () => <span /> }))

const { QueryClient, QueryClientProvider } = await import('@tanstack/react-query')
const IssueRadarPage = (await import('./IssueRadarPage')).default

const GITLAB = {
  owner: 'group/sub', repo: 'thing', provider: 'gitlab', host: 'gitlab.example.com',
  enabled: true, permissions: { admin: false, maintain: false, push: true, triage: true, pull: true },
}
const GITHUB_SAME_SLUG = {
  owner: 'group/sub', repo: 'thing', provider: 'github', host: 'github.com',
  enabled: true, permissions: { admin: false, maintain: false, push: false, triage: false, pull: true },
}
const LEGACY_GITHUB = {
  // A record written before the forge fields existed: absent means public GitHub.
  owner: 'acme', repo: 'alpha',
  enabled: true, permissions: { admin: false, maintain: false, push: true, triage: true, pull: true },
}

const OTHER_REPO = {
  // An unrelated repository that merely happens to sort first.
  owner: 'other', repo: 'project', provider: 'github', host: 'github.com',
  enabled: true, permissions: { admin: false, maintain: false, push: true, triage: true, pull: true },
}

const renderPage = () => {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={qc}><IssueRadarPage /></QueryClientProvider>)
}

const identity = async () =>
  (await waitFor(() => screen.getByTestId('active-identity'))).textContent

beforeEach(() => {
  vi.clearAllMocks()
  stored.active = null
})

describe('IssueRadarPage — the active repository it hands down', () => {
  it('carries the fallback record\u2019s forge when nothing is stored', async () => {
    // The arm that used to build `{owner, repo}` and throw the rest away.
    reposMock.mockResolvedValue({ repos: [GITLAB] })
    renderPage()
    expect(await identity()).toBe('group/sub/thing|gitlab|gitlab.example.com')
  })

  it('heals a legacy forge-less pointer from the record it matches', async () => {
    // The legacy-upgrade path, exercising the MATCH arm: a pointer persisted before
    // the forge fields existed reads as public GitHub, so `sameRepoRef` pairs it with
    // its explicit-GitHub record and what goes down carries that record's fields
    // instead of the bare pointer. The unrelated repo is first on purpose, so this
    // resolves through the match rather than through `repos[0]`.
    stored.active = { owner: 'group/sub', repo: 'thing' }
    reposMock.mockResolvedValue({ repos: [OTHER_REPO, GITHUB_SAME_SLUG] })
    renderPage()
    expect(await identity()).toBe('group/sub/thing|github|github.com')
  })

  it('falls back to the only connected repository when the stored slug is forge-less', async () => {
    // Named for what it pins: with ONE connected repository `repos[0]` is already the
    // answer, so this does NOT distinguish a match from the fallback -- the forge-less
    // pointer names public GitHub and the lone record is GitLab, so it resolves by
    // falling back. Kept because it is the shape a fresh install upgrades into; the
    // match arm is covered above and non-reassignment below.
    stored.active = { owner: 'group/sub', repo: 'thing' }
    reposMock.mockResolvedValue({ repos: [GITLAB] })
    renderPage()
    expect(await identity()).toBe('group/sub/thing|gitlab|gitlab.example.com')
  })

  it('does not resolve a stored pointer to the same slug on another forge', async () => {
    // A mixed install holding one slug twice. The stored pointer names the GitHub
    // one; the GitLab one is `repos[0]`. A slug-only match would have returned the
    // pointer as-is and addressed neither record deliberately.
    stored.active = { owner: 'group/sub', repo: 'thing', provider: 'github', host: 'github.com' }
    reposMock.mockResolvedValue({ repos: [GITLAB, GITHUB_SAME_SLUG] })
    renderPage()
    expect(await identity()).toBe('group/sub/thing|github|github.com')
  })

  it('never reassigns a legacy GitHub pointer to a same-slug repo on another forge', async () => {
    // A forge-less stored pointer is NOT a pointer of unknown forge: `repoScopeKey`
    // resolves absent fields to public GitHub, so it names github.com/group/sub/thing
    // -- which is why `sameRepoRef` refuses to pair it with a GitLab record (its own
    // test pins that). With no GitHub record connected, the stored repository is not
    // connected, so this must fall back like any other missing one. Resolving it to
    // the same-slug GitLab record instead would reassign the user's repository to a
    // forge they never chose. The unrelated repo is first on purpose: it is what
    // `repos[0]` yields, so this assertion fails if a slug fallback is ever added.
    stored.active = { owner: 'group/sub', repo: 'thing' }
    reposMock.mockResolvedValue({ repos: [OTHER_REPO, GITLAB] })
    renderPage()
    expect(await identity()).toBe('other/project|github|github.com')
  })

  it('falls back when the stored repository is no longer connected', async () => {
    stored.active = { owner: 'gone', repo: 'removed', provider: 'gitlab', host: 'gitlab.example.com' }
    reposMock.mockResolvedValue({ repos: [GITLAB] })
    renderPage()
    expect(await identity()).toBe('group/sub/thing|gitlab|gitlab.example.com')
  })

  it('leaves a legacy GitHub record\u2019s absent fields absent', async () => {
    // The forge fields are OPTIONAL and absent means public GitHub. Resolving must
    // not invent `provider: 'github'` on a record that never carried it -- that
    // would start sending a provider the install never had.
    stored.active = { owner: 'acme', repo: 'alpha' }
    reposMock.mockResolvedValue({ repos: [LEGACY_GITHUB] })
    renderPage()
    expect(await identity()).toBe('acme/alpha|-|-')
  })

  it('shows onboarding, and no identity at all, with no repositories', async () => {
    reposMock.mockResolvedValue({ repos: [] })
    renderPage()
    await waitFor(() => expect(screen.getByTestId('welcome')).toBeTruthy())
    expect(screen.queryByTestId('active-identity')).toBeNull()
  })
})
