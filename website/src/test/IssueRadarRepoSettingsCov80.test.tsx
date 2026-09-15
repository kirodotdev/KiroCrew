import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { RepoRef } from '../apps/issue-radar/api'

/**
 * Companion to IssueRadarRepoSettingsNav.test.tsx, which covers the autosave /
 * conflict-rebase machinery and the crew-protocol scope key. This file covers the
 * rest of the page: the per-repo refresh, the disconnect flow, the live triage
 * counts, the label role editing, the member roster and the failure banners.
 */
const api = {
  labels: vi.fn(),
  getSettings: vi.fn(),
  putSettings: vi.fn(),
  issues: vi.fn(),
  members: vi.fn(),
  disconnect: vi.fn(),
  getCrewSettings: vi.fn(),
  putCrewSettings: vi.fn(),
}
class SettingsConflictError extends Error {
  current: Record<string, unknown>
  constructor(message: string, current: Record<string, unknown>) {
    super(message)
    this.name = 'SettingsConflictError'
    this.current = current
  }
}
const DEFAULTS = {
  triage_labels: [] as string[], unlabeled_is_untriaged: true,
  good_first_issue_labels: [] as string[], notify_on_new_issue: false,
  workspace_path: '', revision: 0,
}
vi.mock('../apps/issue-radar/api', () => ({
  issueRadarApi: api,
  SettingsConflictError,
  DEFAULT_REPO_SETTINGS: DEFAULTS,
}))

const ctx = { value: {} as Record<string, unknown> }
vi.mock('../apps/issue-radar/context', () => ({ useIssueRadar: () => ctx.value }))

// Stub the shared dashboard ProjectPicker: the real one portals to <body>,
// calls the dashboard API (recent-projects / browse-dirs) and computes an anchor
// rect — none of which this suite is testing. The stub renders a button that
// fires `onSelect` with a fixed path when open, so the wiring (Browse opens it,
// a selection saves through `update`) is what gets exercised. The real picker
// has its own coverage in ProjectPicker.test.tsx.
const PICKED_PATH = '/Users/me/code/zzq-org/zzq-pkg'
vi.mock('../components/ProjectPicker', () => ({
  default: ({ open, onSelect }: { open: boolean; onSelect: (p: string) => void }) =>
    open
      ? <button type="button" data-testid="pp-pick" onClick={() => onSelect(PICKED_PATH)}>pick</button>
      : null,
}))

const RepoSettings = (await import('../apps/issue-radar/views/settings/RepoSettings')).default

const openSettings = vi.fn()

const REF: RepoRef = { owner: 'zzq-org', repo: 'zzq-pkg', provider: 'github', host: 'github.com' }

const LABELS = [
  { name: 'zzq-needs-triage', color: 'd73a4a', description: '' },
  { name: 'zzq-good-first-issue', color: '7057ff', description: '' },
  { name: 'zzq-chore', color: 'cfd3d7', description: '' },
]

const ISSUES = [
  { number: 1, title: 'zzq-a', labels: ['zzq-needs-triage'], updated_at: '2026-01-01T00:00:00Z' },
  { number: 2, title: 'zzq-b', labels: [], updated_at: '2026-01-01T00:00:00Z' },
  { number: 3, title: 'zzq-c', labels: ['zzq-good-first-issue'], updated_at: '2026-01-01T00:00:00Z' },
]

function setCtx(over: Record<string, unknown> = {}) {
  ctx.value = {
    repos: [{ ...REF, permissions: { push: true } }],
    active: { owner: REF.owner, repo: REF.repo },
    openSettings,
    openDashboard: vi.fn(),
    switchRepo: vi.fn(),
    ...over,
  }
}

function renderPage(ref: RepoRef = REF) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return {
    qc,
    ...render(
      <QueryClientProvider client={qc}><RepoSettings repoRef={ref} /></QueryClientProvider>,
    ),
  }
}

beforeEach(() => {
  vi.clearAllMocks()
  setCtx()
  api.labels.mockResolvedValue({ owner: REF.owner, repo: REF.repo, labels: LABELS, from_cache: true })
  api.getSettings.mockResolvedValue({
    owner: REF.owner, repo: REF.repo, settings: { ...DEFAULTS },
  })
  api.issues.mockResolvedValue({ owner: REF.owner, repo: REF.repo, issues: ISSUES, from_cache: true })
  api.members.mockResolvedValue({
    owner: REF.owner, repo: REF.repo, source: 'collaborators', from_cache: true,
    members: [
      { login: 'zzq-admin-login', role: 'admin' },
      { login: 'zzq-reader-login', role: 'read' },
      { login: 'zzq-odd-login', role: 'zzq-unmapped-role' },
    ],
  })
  api.getCrewSettings.mockResolvedValue({
    settings: {
      schema: 1, claim_ttl_hours: 48,
      needs_human_label: 'zzq-needs-human', commit_trailer: 'Crew: {name}',
    },
  })
  api.putSettings.mockImplementation(async (_ref: unknown, next: Record<string, unknown>) =>
    ({ owner: REF.owner, repo: REF.repo, settings: { ...next, revision: 1 } }))
})

describe('RepoSettings — per-repo refresh', () => {
  it('re-fetches this repo\'s issues and labels and seeds the shared caches', async () => {
    const fresh = { owner: REF.owner, repo: REF.repo, issues: [ISSUES[0]], from_cache: false }
    const freshLabels = { owner: REF.owner, repo: REF.repo, labels: [LABELS[0]], from_cache: false }
    const { qc } = renderPage()
    await screen.findByRole('button', { name: /Refresh/ })

    api.issues.mockResolvedValue(fresh)
    api.labels.mockResolvedValue(freshLabels)
    await userEvent.click(screen.getByRole('button', { name: /Refresh/ }))

    await waitFor(() => expect(api.issues).toHaveBeenCalledWith(REF, { refresh: true, state: 'open' }))
    expect(api.labels).toHaveBeenCalledWith(REF, { refresh: true })
    const scope = 'github:github.com:zzq-org/zzq-pkg'
    await waitFor(() =>
      expect(qc.getQueryData(['issue-radar', 'issues', scope, 'open'])).toEqual(fresh))
    expect(qc.getQueryData(['issue-radar', 'labels', scope])).toEqual(freshLabels)
  })
})

describe('RepoSettings — disconnect', () => {
  it('asks for confirmation before disconnecting, and can be backed out of', async () => {
    renderPage()
    await userEvent.click(await screen.findByRole('button', { name: /^Disconnect$/ }))
    expect(screen.getByRole('button', { name: /Confirm disconnect/ })).toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: /Cancel/ }))
    expect(api.disconnect).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: /^Disconnect$/ })).toBeInTheDocument()
  })

  it('disconnects locally, then navigates away from a page for a repo that is gone', async () => {
    api.disconnect.mockResolvedValue({ ok: true })
    const { qc } = renderPage()
    const invalidate = vi.spyOn(qc, 'invalidateQueries')

    await userEvent.click(await screen.findByRole('button', { name: /^Disconnect$/ }))
    await userEvent.click(screen.getByRole('button', { name: /Confirm disconnect/ }))

    await waitFor(() => expect(api.disconnect).toHaveBeenCalledWith(REF))
    // The connect picker caches a per-repo `connected` flag, so without dropping
    // it the just-disconnected repo stays greyed out as Connected.
    await waitFor(() => expect(invalidate).toHaveBeenCalledWith({
      queryKey: ['issue-radar', 'recent-repos'],
    }))
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['issue-radar', 'repos'] })
    expect(openSettings).toHaveBeenCalledWith({ kind: 'general', anchor: 'repos' })
  })
})

describe('RepoSettings — label roles and live counts', () => {
  it('counts the issues that match the current triage definition', async () => {
    renderPage()
    // Two of three: one carries a triage label, one has no labels at all.
    await waitFor(() => expect(screen.getByText(/open issues currently need triage/)).toBeInTheDocument())
    const line = screen.getByText(/open issues currently need triage/).parentElement as HTMLElement
    expect(line.textContent).toMatch(/1\s*of\s*3/)
  })

  it('counts the newcomer-friendly issues under the saved definition', async () => {
    api.getSettings.mockResolvedValue({
      owner: REF.owner, repo: REF.repo,
      settings: { ...DEFAULTS, good_first_issue_labels: ['zzq-good-first-issue'] },
    })
    renderPage()
    const line = await screen.findByText(/open issues are marked first-issue-friendly/)
    expect((line.parentElement as HTMLElement).textContent).toMatch(/1/)
  })

  it('adds a label to a role set on click, and removes it on a second click', async () => {
    renderPage()
    await waitFor(() => expect(screen.getByRole('switch', { name: /new issue/i })).toHaveProperty('disabled', false))

    // Both cards list every label, so address the triage card's copy.
    const chips = screen.getAllByTitle('zzq-chore')
    await userEvent.click(chips[0])
    await waitFor(() => expect(api.putSettings).toHaveBeenCalledTimes(1))
    expect((api.putSettings.mock.calls[0][1] as { triage_labels: string[] }).triage_labels)
      .toEqual(['zzq-chore'])

    await userEvent.click(screen.getAllByTitle('zzq-chore')[0])
    await waitFor(() => expect(api.putSettings).toHaveBeenCalledTimes(2))
    expect((api.putSettings.mock.calls[1][1] as { triage_labels: string[] }).triage_labels)
      .toEqual([])
  })

  it('adds every suggested label in one write', async () => {
    renderPage()
    await waitFor(() => expect(screen.getByRole('switch', { name: /new issue/i })).toHaveProperty('disabled', false))
    // The triage card's suggestion button; the good-first-issue card has its own.
    const suggest = screen.getAllByRole('button', { name: /suggested/ })[0]
    await userEvent.click(suggest)

    await waitFor(() => expect(api.putSettings).toHaveBeenCalledTimes(1))
    const sent = api.putSettings.mock.calls[0][1] as {
      triage_labels: string[]; good_first_issue_labels: string[]
    }
    expect(sent.triage_labels).toContain('zzq-needs-triage')
    expect(sent.good_first_issue_labels).toEqual([])
  })

  it('toggles the unlabelled-means-untriaged rule', async () => {
    renderPage()
    const toggle = await screen.findByRole('switch', { name: /no labels/i })
    await waitFor(() => expect(toggle).toHaveProperty('disabled', false))
    await userEvent.click(toggle)
    await waitFor(() => expect(api.putSettings).toHaveBeenCalledTimes(1))
    expect((api.putSettings.mock.calls[0][1] as { unlabeled_is_untriaged: boolean })
      .unlabeled_is_untriaged).toBe(false)
  })
})

describe('RepoSettings — members roster', () => {
  it('lists each member with a localised role, leaving an unmapped role verbatim', async () => {
    renderPage()
    expect(await screen.findByText('zzq-admin-login')).toBeInTheDocument()
    expect(screen.getByText('Admin')).toBeInTheDocument()
    // A read-only collaborator is muted rather than accented.
    const readTag = screen.getByText('Read')
    expect(readTag.className).toContain('text-muted')
    // A role the provider reports with no catalog entry is an identifier, not
    // copy, so it must not be mangled into a translation lookup.
    expect(screen.getByText('zzq-unmapped-role')).toBeInTheDocument()
  })

  it('says so when the roster cannot be read', async () => {
    api.members.mockRejectedValue(new Error('zzq-members-unreachable'))
    renderPage()
    expect(await screen.findByText(/Couldn't load members right now/)).toBeInTheDocument()
  })

  it('distinguishes an empty DERIVED roster from an empty authoritative one', async () => {
    api.members.mockResolvedValue({
      owner: REF.owner, repo: REF.repo, members: [], source: 'derived', from_cache: true,
    })
    const derived = renderPage()
    expect(await screen.findByText(/No members detected among/)).toBeInTheDocument()
    derived.unmount()

    api.members.mockResolvedValue({
      owner: REF.owner, repo: REF.repo, members: [], source: 'collaborators', from_cache: true,
    })
    renderPage()
    expect(await screen.findByText(/No members found for this repo/)).toBeInTheDocument()
  })
})

describe('RepoSettings — workspace path', () => {
  // Typing a path autosaves it as `workspace_path`; the Investigate action reads
  // this to open its chat session in the repo's real working copy.
  it('saves the workspace path the user types', async () => {
    renderPage()
    const input = await screen.findByPlaceholderText('/Users/you/code/owner/repo')
    await waitFor(() => expect(input).toHaveProperty('disabled', false))

    // paste, not type: onChange autosaves, so a per-character type() would queue
    // one write per keystroke and only the last would carry the whole path.
    await userEvent.click(input)
    await userEvent.paste('/Users/me/code/zzq-org/zzq-pkg')

    await waitFor(() => expect(api.putSettings).toHaveBeenCalled())
    const last = api.putSettings.mock.calls.at(-1)![1] as { workspace_path: string }
    expect(last.workspace_path).toBe('/Users/me/code/zzq-org/zzq-pkg')
  })

  it('shows the configured path back in the readout', async () => {
    api.getSettings.mockResolvedValue({
      owner: REF.owner, repo: REF.repo,
      settings: { ...DEFAULTS, workspace_path: '/srv/checkouts/zzq-pkg', revision: 2 },
    })
    renderPage()
    expect(await screen.findByText(/New Investigate sessions will run in/)).toBeInTheDocument()
    expect(screen.getByText('/srv/checkouts/zzq-pkg')).toBeInTheDocument()
  })

  // Browse opens the shared ProjectPicker (stubbed here); a selection saves the
  // chosen folder as workspace_path through the same autosave path as typing.
  it('saves the folder chosen through the Browse picker', async () => {
    renderPage()
    const browse = await screen.findByRole('button', { name: /Browse/ })
    await waitFor(() => expect(browse).toHaveProperty('disabled', false))

    // Picker is closed until Browse is clicked.
    expect(screen.queryByTestId('pp-pick')).not.toBeInTheDocument()
    await userEvent.click(browse)
    await userEvent.click(await screen.findByTestId('pp-pick'))

    await waitFor(() => expect(api.putSettings).toHaveBeenCalled())
    const last = api.putSettings.mock.calls.at(-1)![1] as { workspace_path: string }
    expect(last.workspace_path).toBe(PICKED_PATH)
  })
})

describe('RepoSettings — header and failure banners', () => {
  it('marks a repo the signed-in user cannot write to', async () => {
    setCtx({ repos: [{ ...REF, permissions: { push: false, triage: false } }] })
    renderPage()
    expect(await screen.findByText(/Read-only/i)).toBeInTheDocument()
  })

  it('links out to the repo on its own host, with the provider named', async () => {
    const gitlab: RepoRef = {
      owner: 'zzq-org', repo: 'zzq-pkg', provider: 'gitlab', host: 'gitlab.example.com',
    }
    setCtx({ repos: [{ ...gitlab, permissions: { push: true } }] })
    renderPage(gitlab)
    const link = await screen.findByText(/Open on/)
    expect((link.closest('a') as HTMLAnchorElement).getAttribute('href'))
      .toBe('https://gitlab.example.com/zzq-org/zzq-pkg')
    expect(link.parentElement?.textContent).toContain('GitLab')
  })

  it('reports a settings read failure without pretending the defaults are saved', async () => {
    api.getSettings.mockRejectedValue(new Error('zzq-settings-read-failed'))
    renderPage()
    expect(await screen.findByText(/Couldn't load saved settings/)).toBeInTheDocument()
    expect(screen.getByText(/zzq-settings-read-failed/)).toBeInTheDocument()
    // And every control stays blocked: there is no revision that makes a write safe.
    expect(screen.getByRole('switch', { name: /new issue/i })).toHaveProperty('disabled', true)
  })

  it('reports a save failure and keeps the edit on screen', async () => {
    api.putSettings.mockRejectedValue(new Error('zzq-save-refused'))
    renderPage()
    const toggle = await screen.findByRole('switch', { name: /new issue/i })
    await waitFor(() => expect(toggle).toHaveProperty('disabled', false))
    await userEvent.click(toggle)

    expect(await screen.findByText(/Couldn't save your changes/)).toBeInTheDocument()
    expect(screen.getByText(/zzq-save-refused/)).toBeInTheDocument()
  })
})
