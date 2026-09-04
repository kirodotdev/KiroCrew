// Regression coverage for the Issue Radar connect flow's non-obvious
// behaviours — the ones whose failure modes are silent (a repo that keeps
// connecting after the dialog "closed", a typed URL resubmitted after it
// already succeeded, a checkbox label that toggles the wrong row).
//
// The panel and its state hook are exercised through a minimal host that
// mirrors what the real hosts (WelcomeCarousel / ConnectRepoModal) do: render
// <ConnectPanel> plus a Connect button wired to `flow.submit`.
import { StrictMode } from 'react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const mockConnect = vi.fn()
const mockRecentRepos = vi.fn()
vi.mock('../apps/issue-radar/api', () => ({
  issueRadarApi: {
    connect: (...a: unknown[]) => mockConnect(...a),
    recentRepos: (...a: unknown[]) => mockRecentRepos(...a),
  },
}))

const { default: ConnectPanel, useConnectFlow, repoIdentity, parseRepoRef } = await import('../apps/issue-radar/ConnectPanel')
const { markAutoSelectFirstIssue, consumeAutoSelectFirstIssue } = await import(
  '../apps/issue-radar/lib/format'
)

function repo(fullName: string, connected = false) {
  const [owner, name] = fullName.split('/')
  return {
    full_name: fullName,
    owner,
    repo: name,
    connected,
    contribution_count: 1,
    last_contributed_at: new Date().toISOString(),
  }
}

/** Minimal stand-in for a host card: panel body + the Connect button the real
 * hosts render outside the panel. */
function Host({ onConnected = vi.fn() }: { onConnected?: (r: { owner: string; repo: string }) => void }) {
  const flow = useConnectFlow(onConnected)
  return (
    <div>
      <ConnectPanel flow={flow} />
      <button onClick={flow.submit} disabled={!flow.targets.length || flow.pending}>
        Connect {flow.targets.length}
      </button>
    </div>
  )
}

function renderHost(props: Parameters<typeof Host>[0] = {}) {
  // A fresh client per test: these assertions depend on cache state.
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const invalidate = vi.spyOn(qc, 'invalidateQueries')
  const utils = render(
    <QueryClientProvider client={qc}>
      <Host {...props} />
    </QueryClientProvider>,
  )
  return { ...utils, invalidate }
}

/** Open the GitHub provider and wait for the repo picker to resolve. */
async function openGithub(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole('button', { name: /GitHub/ }))
  await waitFor(() => expect(mockRecentRepos).toHaveBeenCalled())
}

beforeEach(() => {
  mockConnect.mockReset()
  mockRecentRepos.mockReset()
  mockRecentRepos.mockResolvedValue({ repos: [repo('o/alpha'), repo('o/beta')], truncated: false })
})

describe('ConnectPanel provider rows', () => {
  it('lists exactly the sources that can be connected', async () => {
    const user = userEvent.setup()
    renderHost()
    // Every listed row must lead somewhere: an unwired source rendered as a
    // disabled row with a "Soon" badge would occupy a full row of the card while
    // offering the user nothing. Only connectable sources are listed, and their
    // absence is pinned here — adding a dead row would need a decision, not a
    // silent revert.
    for (const name of ['Jira', 'Linear']) {
      expect(screen.queryByRole('button', { name: new RegExp(name) })).toBeNull()
    }
    for (const name of ['GitHub', 'GitLab', 'Azure DevOps']) {
      expect(screen.getByRole('button', { name: new RegExp(name) })).toBeEnabled()
    }
    // Nothing is fetched until a provider is actually opened.
    expect(mockRecentRepos).not.toHaveBeenCalled()
    await openGithub(user)
  })

  it('asks for the selected provider’s own account when GitLab is opened', async () => {
    // The two lists come from different accounts on different CLIs, so opening
    // GitLab must not serve (or re-use) the GitHub picker's results.
    const user = userEvent.setup()
    renderHost()
    await user.click(screen.getByRole('button', { name: /GitLab/ }))
    await waitFor(() => expect(mockRecentRepos).toHaveBeenCalled())
    expect(mockRecentRepos).toHaveBeenLastCalledWith(
      expect.any(Number),
      expect.objectContaining({ provider: 'gitlab' }),
    )
  })

  it('shows only the selected provider’s URL example', async () => {
    // A single combined "github… or gitlab…" placeholder is wider than the
    // input, so it clipped mid-URL and the second provider's form was never
    // legible. Whichever provider is open must see its own complete example and
    // not the other one's.
    const user = userEvent.setup()
    renderHost()

    await openGithub(user)
    const url = () => screen.getByLabelText('Repository URL') as HTMLInputElement
    expect(url().placeholder).toBe('https://github.com/<owner>/<repo>')

    await user.click(screen.getByRole('button', { name: /GitLab/ }))
    await waitFor(() => expect(url().placeholder).toBe('https://gitlab.com/<group>/<project>'))

    // Azure DevOps' path is three levels deep and carries a literal `_git`, so
    // neither of the other two examples tells an Azure user what to paste.
    await user.click(screen.getByRole('button', { name: /Azure DevOps/ }))
    await waitFor(() => expect(url().placeholder)
      .toBe('https://dev.azure.com/<org>/<project>/_git/<repo>'))
  })

  it('asks for the Azure DevOps account when that row is opened', async () => {
    const user = userEvent.setup()
    renderHost()
    await user.click(screen.getByRole('button', { name: /Azure DevOps/ }))
    await waitFor(() => expect(mockRecentRepos).toHaveBeenCalled())
    expect(mockRecentRepos).toHaveBeenLastCalledWith(
      expect.any(Number),
      expect.objectContaining({ provider: 'azure' }),
    )
  })
})

describe('ConnectPanel repo picker', () => {
  it('toggles exactly the clicked row when names differ only in punctuation', async () => {
    // `a.b` and `a-b` both sanitise to the same string; a shared id/htmlFor
    // made one row's label drive the other row's checkbox.
    mockRecentRepos.mockResolvedValue({ repos: [repo('o/a.b'), repo('o/a-b')], truncated: false })
    const user = userEvent.setup()
    renderHost()
    await openGithub(user)

    const first = await screen.findByRole('checkbox', { name: 'Select o/a.b' })
    const second = screen.getByRole('checkbox', { name: 'Select o/a-b' })
    await user.click(first)
    expect(first).toBeChecked()
    expect(second).not.toBeChecked()
  })

  it('counts every ticked repo as a connect target', async () => {
    const user = userEvent.setup()
    renderHost()
    await openGithub(user)

    await user.click(await screen.findByRole('checkbox', { name: 'Select o/alpha' }))
    await user.click(screen.getByRole('checkbox', { name: 'Select o/beta' }))
    expect(screen.getByRole('button', { name: 'Connect 2' })).toBeEnabled()
  })

  it('submits a typed URL alongside ticks, and only once when it duplicates one', async () => {
    const user = userEvent.setup()
    renderHost()
    await openGithub(user)

    await user.click(await screen.findByRole('checkbox', { name: 'Select o/alpha' }))
    // Different spelling, SAME repo: `www.`, mixed case and a `.git` suffix all
    // normalise onto the ticked `o/alpha`, so this is NOT a second target.
    await user.type(screen.getByLabelText('Repository URL'), 'https://www.github.com/O/Alpha.git')
    expect(screen.getByRole('button', { name: 'Connect 1' })).toBeInTheDocument()
  })
})

describe('bulk connect', () => {
  it('hands control back only when every target succeeded', async () => {
    mockConnect.mockImplementation(async (url: string) => {
      if (String(url).includes('beta')) throw new Error('nope')
      return { owner: 'o', repo: 'alpha' }
    })
    const onConnected = vi.fn()
    const user = userEvent.setup()
    const { invalidate } = renderHost({ onConnected })
    await openGithub(user)

    await user.click(await screen.findByRole('checkbox', { name: 'Select o/alpha' }))
    await user.click(screen.getByRole('checkbox', { name: 'Select o/beta' }))
    await user.click(screen.getByRole('button', { name: 'Connect 2' }))

    // Partial failure: the error is surfaced and the dialog must stay open, so
    // the success callback (which unmounts it) never fires.
    await waitFor(() => expect(screen.getByText(/nope/)).toBeInTheDocument())
    expect(onConnected).not.toHaveBeenCalled()
    // The repo that DID connect is dropped from the selection, leaving exactly
    // what still needs a retry.
    await waitFor(() => expect(screen.getByRole('button', { name: 'Connect 1' })).toBeInTheDocument())
    // The picker's "Connected" rows are refreshed even on a partial failure —
    // the dialog stays open and must not offer a repo it just connected.
    const keys = invalidate.mock.calls.map((c) => JSON.stringify(c[0]))
    expect(keys.some((k) => k.includes('recent-repos'))).toBe(true)
    // `repos` is deliberately NOT invalidated here: on first run it is what
    // decides whether onboarding is still mounted, so refreshing it mid-partial
    // -failure would unmount the carousel and take the unread errors with it.
    expect(keys.some((k) => k.includes('[\"issue-radar\",\"repos\"]'))).toBe(false)
  })

  it('clears a typed URL that connected, so it is not resubmitted', async () => {
    mockConnect.mockImplementation(async (url: string) => {
      if (String(url).includes('beta')) throw new Error('nope')
      return { owner: 'o', repo: 'gamma' }
    })
    const user = userEvent.setup()
    renderHost()
    await openGithub(user)

    await user.click(await screen.findByRole('checkbox', { name: 'Select o/beta' }))
    const input = screen.getByLabelText('Repository URL')
    await user.type(input, 'https://github.com/o/gamma')
    await user.click(screen.getByRole('button', { name: 'Connect 2' }))

    await waitFor(() => expect(input).toHaveValue(''))
    // Only the failed tick remains queued.
    expect(screen.getByRole('button', { name: 'Connect 1' })).toBeInTheDocument()
  })

  it('calls onConnected and refreshes the repo list when all targets succeed', async () => {
    mockConnect.mockResolvedValue({ owner: 'o', repo: 'alpha' })
    const onConnected = vi.fn()
    const user = userEvent.setup()
    const { invalidate } = renderHost({ onConnected })
    await openGithub(user)

    await user.click(await screen.findByRole('checkbox', { name: 'Select o/alpha' }))
    await user.click(screen.getByRole('button', { name: 'Connect 1' }))
    await waitFor(() => expect(onConnected).toHaveBeenCalledWith({ owner: 'o', repo: 'alpha' }))
    // Deferred to here, not the partial-success path — see above.
    const keys = invalidate.mock.calls.map((c) => JSON.stringify(c[0]))
    expect(keys.some((k) => k.includes('[\"issue-radar\",\"repos\"]'))).toBe(true)
  })
})

describe('gh setup notice', () => {
  it('replaces the picker AND hides the URL field when gh is unusable', async () => {
    mockRecentRepos.mockResolvedValue({
      repos: [],
      setup_required: 'not_authenticated',
      error: 'gh: not logged in',
    })
    const user = userEvent.setup()
    renderHost()
    await openGithub(user)

    await waitFor(() =>
      expect(screen.getByText(/set up the GitHub CLI/i)).toBeInTheDocument(),
    )
    // Pasting a URL would fail the same way, so there is nothing to offer.
    expect(screen.queryByLabelText('Repository URL')).not.toBeInTheDocument()
  })

  it('names glab — not gh — when the GitLab panel needs setup', async () => {
    // This is the one screen whose job is unblocking the user. Naming the wrong
    // CLI sends them to install `gh`, click "check again", and stay stuck.
    mockRecentRepos.mockResolvedValue({
      repos: [],
      setup_required: 'not_authenticated',
      error: 'glab: not logged in',
    })
    const user = userEvent.setup()
    renderHost()
    await user.click(screen.getByRole('button', { name: /GitLab/ }))
    await waitFor(() => expect(mockRecentRepos).toHaveBeenCalled())

    await waitFor(() =>
      expect(screen.getByText(/set up the GitLab CLI/i)).toBeInTheDocument(),
    )
    expect(screen.queryByText(/set up the GitHub CLI/i)).not.toBeInTheDocument()
    expect(screen.getAllByText('glab').length).toBeGreaterThan(0)
    // The wrong binary must not be named anywhere in the notice.
    expect(screen.queryByText(/^gh$/)).not.toBeInTheDocument()
  })
})

describe('auto-select-first-issue intent', () => {
  it('is consumed only by the repo it was recorded for', () => {
    markAutoSelectFirstIssue({ owner: 'o', repo: 'new' })
    // The previously active repo must not consume it — that would select an
    // issue from the OLD repo while the new one is still refetching.
    expect(consumeAutoSelectFirstIssue({ owner: 'o', repo: 'old' })).toBe(false)
    expect(consumeAutoSelectFirstIssue({ owner: 'o', repo: 'new' })).toBe(true)
    // One-shot: a later render (or a reload) must not re-select.
    expect(consumeAutoSelectFirstIssue({ owner: 'o', repo: 'new' })).toBe(false)
  })

  it('matches case-insensitively, as GitHub names do', () => {
    markAutoSelectFirstIssue({ owner: 'Acme', repo: 'Widget' })
    expect(consumeAutoSelectFirstIssue({ owner: 'acme', repo: 'widget' })).toBe(true)
  })
})

describe('repoIdentity (typed-URL dedupe key)', () => {
  it('normalises every spelling that resolves to the same repo', () => {
    for (const text of [
      'https://github.com/o/alpha',
      'https://github.com/o/alpha/',
      'https://www.github.com/O/Alpha',
      'https://github.com/o/alpha.git',
      'https://github.com/o/alpha.git/',
      'github.com/o/alpha',
      'o/alpha',
    ]) {
      expect(repoIdentity(text)).toBe('o/alpha')
    }
  })

  it('rejects non-GitHub and incomplete references', () => {
    for (const text of ['', '   ', 'https://gitlab.com/o/alpha', 'https://github.com/o', 'not a url at all']) {
      expect(repoIdentity(text)).toBeNull()
    }
  })
})

describe('in-flight connect', () => {
  it('freezes the repo ticks and the URL field while connecting', async () => {
    // submit() snapshots its target list, so a tick added mid-flight would be
    // silently dropped when a full success closes the dialog.
    let release: (v: unknown) => void = () => {}
    mockConnect.mockImplementation(
      () => new Promise((res) => { release = () => res({ owner: 'o', repo: 'alpha' }) }),
    )
    const user = userEvent.setup()
    renderHost()
    await openGithub(user)

    const alpha = await screen.findByRole('checkbox', { name: 'Select o/alpha' })
    await user.click(alpha)
    await user.click(screen.getByRole('button', { name: 'Connect 1' }))

    await waitFor(() => expect(screen.getByLabelText('Repository URL')).toBeDisabled())
    expect(screen.getByRole('checkbox', { name: 'Select o/beta' })).toBeDisabled()
    release(null)
  })
})

describe('canonical URL submitted for a typed reference', () => {
  it('preserves the original casing', async () => {
    // The backend stores owner/repo VERBATIM, so submitting a case-folded name
    // for an already-connected `Acme/Widget` would append a second repo with
    // its own caches and settings. Folding is for comparison only.
    expect(parseRepoRef('Acme/Widget')).toEqual({ owner: 'Acme', repo: 'Widget' })
    expect(repoIdentity('Acme/Widget')).toBe('acme/widget')

    mockRecentRepos.mockResolvedValue({ repos: [], truncated: false })
    mockConnect.mockResolvedValue({ owner: 'Acme', repo: 'Widget' })
    const user = userEvent.setup()
    renderHost()
    await openGithub(user)

    // Shorthand the backend's URL parser would reject, so it must be expanded —
    // but expanded with the case the user typed.
    await user.type(screen.getByLabelText('Repository URL'), 'Acme/Widget')
    await user.click(screen.getByRole('button', { name: 'Connect 1' }))
    await waitFor(() => expect(mockConnect).toHaveBeenCalledWith('https://github.com/Acme/Widget'))
  })

  it('submits unparseable text as typed, so the server error stays honest', async () => {
    mockRecentRepos.mockResolvedValue({ repos: [], truncated: false })
    mockConnect.mockRejectedValue(new Error('bad url'))
    const user = userEvent.setup()
    renderHost()
    await openGithub(user)

    await user.type(screen.getByLabelText('Repository URL'), 'https://gitlab.com/o/alpha')
    await user.click(screen.getByRole('button', { name: 'Connect 1' }))
    await waitFor(() => expect(mockConnect).toHaveBeenCalledWith('https://gitlab.com/o/alpha'))
  })
})

describe('StrictMode', () => {
  it('still connects after the development mount/cleanup/mount cycle', async () => {
    // StrictMode runs effects twice in development. A teardown-only cancel flag
    // latched true on that first cleanup, so every connect bailed before its
    // first request — the dialog looked alive but did nothing.
    mockConnect.mockResolvedValue({ owner: 'o', repo: 'alpha' })
    const onConnected = vi.fn()
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const user = userEvent.setup()
    render(
      <StrictMode>
        <QueryClientProvider client={qc}>
          <Host onConnected={onConnected} />
        </QueryClientProvider>
      </StrictMode>,
    )
    await user.click(screen.getByRole('button', { name: /GitHub/ }))
    await waitFor(() => expect(mockRecentRepos).toHaveBeenCalled())

    await user.click(await screen.findByRole('checkbox', { name: 'Select o/alpha' }))
    await user.click(screen.getByRole('button', { name: 'Connect 1' }))

    await waitFor(() => expect(mockConnect).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(onConnected).toHaveBeenCalledWith({ owner: 'o', repo: 'alpha' }))
  })
})
