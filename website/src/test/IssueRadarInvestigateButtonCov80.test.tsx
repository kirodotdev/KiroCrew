import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { Issue, RepoRef } from '../apps/issue-radar/api'

const api = { getInvestigation: vi.fn() }
vi.mock('../apps/issue-radar/api', () => ({ issueRadarApi: api }))

// The session orchestration (folder → slot → seed → link) is agentSession's job
// and is covered there; here it is a seam, so what matters is WHETHER it is
// called and what the button does with the record it returns.
const investigate = vi.fn()
const session = { busy: false, error: null as Error | null }
vi.mock('../apps/issue-radar/lib/investigate', () => ({
  useInvestigate: () => ({ investigate, busy: session.busy, error: session.error }),
}))

const InvestigateButton = (await import('../apps/issue-radar/components/InvestigateButton')).default

const REF: RepoRef = { owner: 'zzq-org', repo: 'zzq-pkg', provider: 'github', host: 'github.com' }
const ISSUE = { number: 4242, title: 'zzq-issue-title', labels: [] } as unknown as Issue

function renderButton() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return {
    qc,
    ...render(
      <QueryClientProvider client={qc}>
        <InvestigateButton repoRef={REF} issue={ISSUE} />
      </QueryClientProvider>,
    ),
  }
}

beforeEach(() => {
  vi.clearAllMocks()
  session.busy = false
  session.error = null
  api.getInvestigation.mockResolvedValue({
    owner: REF.owner, repo: REF.repo, number: ISSUE.number, investigation: null,
  })
})

describe('InvestigateButton', () => {
  it('offers Investigate once the record lookup says there is no session', async () => {
    renderButton()
    const button = await screen.findByRole('button', { name: /Investigate/ })
    await waitFor(() => expect(button).toHaveProperty('disabled', false))
    expect(api.getInvestigation).toHaveBeenCalledWith(REF, ISSUE.number)
  })

  it('writes the returned record into the cache so the badge is right on return', async () => {
    const saved = { slot_key: 'zzq-slot', status: 'investigating' }
    investigate.mockResolvedValue(saved)
    const { qc } = renderButton()

    const button = await screen.findByRole('button', { name: /Investigate/ })
    await waitFor(() => expect(button).toHaveProperty('disabled', false))
    await userEvent.click(button)

    await waitFor(() => expect(investigate).toHaveBeenCalledWith(REF, ISSUE, null))
    // Written under the scope key, which carries provider + host — a bare
    // owner/repo key would be shared with a same-slug repo on another host.
    const key = ['issue-radar', 'investigation', 'github:github.com:zzq-org/zzq-pkg', 'issue', 4242]
    await waitFor(() => expect(qc.getQueryData(key)).toMatchObject({ investigation: saved }))
    // And the button now offers to resume that session rather than start another.
    expect(await screen.findByRole('button', { name: /Resume/ })).toBeInTheDocument()
  })

  it('leaves the cache alone when the session could not be opened', async () => {
    investigate.mockResolvedValue(null)
    const { qc } = renderButton()
    const button = await screen.findByRole('button', { name: /Investigate/ })
    await waitFor(() => expect(button).toHaveProperty('disabled', false))
    await userEvent.click(button)

    await waitFor(() => expect(investigate).toHaveBeenCalled())
    const key = ['issue-radar', 'investigation', 'github:github.com:zzq-org/zzq-pkg', 'issue', 4242]
    expect((qc.getQueryData(key) as { investigation: unknown }).investigation).toBeNull()
  })

  it('refuses to act while the record lookup is unresolved', async () => {
    // A pending or failed lookup must not read as "no session": clicking would
    // start a second investigation and orphan the first.
    api.getInvestigation.mockImplementation(() => new Promise(() => {}))
    renderButton()
    const button = screen.getByRole('button')
    expect(button).toHaveProperty('disabled', true)
    await userEvent.click(button)
    expect(investigate).not.toHaveBeenCalled()
  })

  it('stays blocked, and says why, when the lookup failed outright', async () => {
    api.getInvestigation.mockRejectedValue(new Error('zzq-record-read-failed'))
    renderButton()
    await waitFor(() => expect(screen.getByRole('button')).toHaveProperty('disabled', true))
    await userEvent.click(screen.getByRole('button'))
    expect(investigate).not.toHaveBeenCalled()
  })

  it('does nothing on a second click while a session is being opened', async () => {
    session.busy = true
    renderButton()
    await waitFor(() => expect(api.getInvestigation).toHaveBeenCalled())
    await userEvent.click(screen.getByRole('button'))
    expect(investigate).not.toHaveBeenCalled()
  })

  it('resumes the recorded session instead of starting a new one', async () => {
    const existing = { slot_key: 'zzq-existing', status: 'investigating' }
    api.getInvestigation.mockResolvedValue({
      owner: REF.owner, repo: REF.repo, number: ISSUE.number, investigation: existing,
    })
    investigate.mockResolvedValue(existing)
    renderButton()

    const button = await screen.findByRole('button', { name: /Resume/ })
    await userEvent.click(button)
    await waitFor(() => expect(investigate).toHaveBeenCalledWith(REF, ISSUE, existing))
  })
})
