import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const api = {
  labels: vi.fn(),
  getSettings: vi.fn(),
  putSettings: vi.fn(),
  issues: vi.fn(),
  members: vi.fn(),
  disconnect: vi.fn(),
}
class SettingsConflictError extends Error {
  current: Record<string, unknown>
  constructor(message: string, current: Record<string, unknown>) {
    super(message)
    this.name = 'SettingsConflictError'
    this.current = current
  }
}
vi.mock('../apps/issue-radar/api', () => ({
  issueRadarApi: api,
  SettingsConflictError,
  DEFAULT_REPO_SETTINGS: {
    triage_labels: [], unlabeled_is_untriaged: true,
    good_first_issue_labels: [], notify_on_new_issue: false, revision: 0,
  },
}))

const ctx = { value: {} as Record<string, unknown> }
vi.mock('../apps/issue-radar/context', () => ({ useIssueRadar: () => ctx.value }))

const RepoSettings = (await import('../apps/issue-radar/views/settings/RepoSettings')).default

const switchRepo = vi.fn()
const openDashboard = vi.fn()

function setCtx(active: { owner: string; repo: string }) {
  ctx.value = {
    repos: [
      { owner: 'o', repo: 'active-one', permissions: { push: true } },
      { owner: 'o', repo: 'other-one', permissions: { push: true } },
    ],
    active,
    switchRepo,
    openDashboard,
    openSettings: vi.fn(),
  }
}

beforeEach(() => {
  vi.clearAllMocks()
  api.labels.mockResolvedValue({ owner: 'o', repo: 'other-one', labels: [], from_cache: true })
  api.getSettings.mockResolvedValue({
    owner: 'o', repo: 'other-one',
    settings: {
      triage_labels: [], unlabeled_is_untriaged: true,
      good_first_issue_labels: [], notify_on_new_issue: false, revision: 0,
    },
  })
  api.issues.mockResolvedValue({ owner: 'o', repo: 'other-one', issues: [], from_cache: true })
  api.members.mockResolvedValue({ owner: 'o', repo: 'other-one', members: [], source: 'derived', from_cache: true })
})

function renderFor(repo: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  // One stable ref object: an inline literal would be a new identity on every
  // render, which is not how the app passes it (the ref comes from context).
  const ref = { owner: 'o', repo }
  return render(
    <QueryClientProvider client={qc}><RepoSettings repoRef={ref} /></QueryClientProvider>,
  )
}

describe('RepoSettings — autosave', () => {
  it('keeps the newest edit when a save conflicts', async () => {
    // Every toggle autosaves. Two quick clicks used to send two writes built on
    // the same revision: the first succeeded, the second 409'd, and clearing the
    // draft threw away the newer edit — the user's last click silently undone.
    setCtx({ owner: 'o', repo: 'other-one' })
    const sent: Record<string, unknown>[] = []
    let call = 0
    api.putSettings.mockImplementation(async (_ref: unknown, next: Record<string, unknown>) => {
      sent.push(next)
      call += 1
      // The FIRST write is rejected as stale, as another tab would cause.
      if (call === 1) {
        throw new SettingsConflictError('changed elsewhere', {
          triage_labels: ['from-other-tab'], unlabeled_is_untriaged: true,
          good_first_issue_labels: [], notify_on_new_issue: false, revision: 9,
        })
      }
      return { owner: 'o', repo: 'other-one', settings: { ...next, revision: 10 } }
    })
    renderFor('other-one')

    await waitFor(() => expect(screen.getByRole('switch', { name: /new issue/i })).toBeTruthy())
    await userEvent.click(screen.getByRole('switch', { name: /new issue/i }))

    await waitFor(() => expect(api.putSettings).toHaveBeenCalledTimes(2))
    // The retry carries BOTH sides: the other tab's label and this tab's toggle.
    const retry = sent[1] as { triage_labels: string[]; notify_on_new_issue: boolean; revision: number }
    expect(retry.triage_labels).toEqual(['from-other-tab'])
    expect(retry.notify_on_new_issue).toBe(true)
    expect(retry.revision).toBe(9)
  })

  it('sends the revision it read on every save', async () => {
    setCtx({ owner: 'o', repo: 'other-one' })
    api.putSettings.mockImplementation(async (_ref: unknown, next: Record<string, unknown>) =>
      ({ owner: 'o', repo: 'other-one', settings: { ...next, revision: 1 } }))
    renderFor('other-one')

    await waitFor(() => expect(screen.getByRole('switch', { name: /new issue/i })).toBeTruthy())
    await userEvent.click(screen.getByRole('switch', { name: /new issue/i }))
    await waitFor(() => expect(api.putSettings).toHaveBeenCalled())
    // Required by the server; omitting it is a 400.
    expect((api.putSettings.mock.calls[0][1] as { revision: number }).revision).toBe(0)
  })

  it('keeps an edit whose save failed when a later edit conflicts', async () => {
    // Save A fails, then edit B hits a 409. Rebasing only B's keys dropped A
    // entirely — from the draft and from what got persisted. Dirty keys must
    // accumulate until a save actually lands.
    setCtx({ owner: 'o', repo: 'other-one' })
    const sent: Record<string, unknown>[] = []
    let call = 0
    api.putSettings.mockImplementation(async (_ref: unknown, next: Record<string, unknown>) => {
      sent.push({ ...next })
      call += 1
      if (call === 1) throw new Error('network died')          // edit A fails
      if (call === 2) {                                        // edit B conflicts
        throw new SettingsConflictError('changed elsewhere', {
          triage_labels: ['from-other-tab'], unlabeled_is_untriaged: true,
          good_first_issue_labels: [], notify_on_new_issue: false, revision: 9,
        })
      }
      return { owner: 'o', repo: 'other-one', settings: { ...next, revision: 10 } }
    })
    renderFor('other-one')

    await waitFor(() => expect(screen.getByRole('switch', { name: /new issue/i })).toBeTruthy())
    await userEvent.click(screen.getByRole('switch', { name: /new issue/i }))   // A
    await waitFor(() => expect(sent).toHaveLength(1))
    await userEvent.click(screen.getByRole('switch', { name: /no labels/i }))   // B
    await waitFor(() => expect(api.putSettings).toHaveBeenCalledTimes(3))

    // The rebased write must carry BOTH the failed edit A and edit B, on top of
    // the other tab's label.
    const rebased = sent[2] as {
      triage_labels: string[]; notify_on_new_issue: boolean; unlabeled_is_untriaged: boolean
    }
    expect(rebased.notify_on_new_issue).toBe(true)      // edit A, whose save failed
    expect(rebased.unlabeled_is_untriaged).toBe(false)  // edit B
    expect(rebased.triage_labels).toEqual(['from-other-tab'])
  })

  it('does not write anything before the settings have loaded', async () => {
    // Until the query resolves, `settings` is DEFAULT_REPO_SETTINGS at revision 0
    // — and a pre-revision config on disk also normalizes to 0, so a PUT built
    // from the defaults is accepted as current and wipes the saved label roles.
    setCtx({ owner: 'o', repo: 'other-one' })
    let releaseSettings: (v: unknown) => void = () => {}
    api.getSettings.mockImplementation(() => new Promise((r) => { releaseSettings = r }))
    renderFor('other-one')

    // The controls render, but disabled, and clicking must not write.
    await waitFor(() => expect(screen.getByRole('switch', { name: /new issue/i })).toBeTruthy())
    const toggle = screen.getByRole('switch', { name: /new issue/i })
    expect(toggle).toHaveProperty('disabled', true)
    await userEvent.click(toggle)
    expect(api.putSettings).not.toHaveBeenCalled()

    // Once the real settings land, editing works and carries THEIR revision.
    releaseSettings({
      owner: 'o', repo: 'other-one',
      settings: {
        triage_labels: ['saved-role'], unlabeled_is_untriaged: false,
        good_first_issue_labels: [], notify_on_new_issue: false, revision: 3,
      },
    })
    api.putSettings.mockImplementation(async (_ref: unknown, next: Record<string, unknown>) =>
      ({ owner: 'o', repo: 'other-one', settings: { ...next, revision: 4 } }))
    await waitFor(() =>
      expect(screen.getByRole('switch', { name: /new issue/i })).toHaveProperty('disabled', false))
    await userEvent.click(screen.getByRole('switch', { name: /new issue/i }))

    await waitFor(() => expect(api.putSettings).toHaveBeenCalledTimes(1))
    const sent = api.putSettings.mock.calls[0][1] as { revision: number; triage_labels: string[] }
    expect(sent.revision).toBe(3)
    expect(sent.triage_labels).toEqual(['saved-role'])
  })

  it('stops treating a key as dirty once the server agrees with it', async () => {
    // Save A lands while B is queued; B fails; another tab then changes A's field
    // and C conflicts. If A stayed dirty, the retry would restore this tab's stale
    // value over theirs.
    setCtx({ owner: 'o', repo: 'other-one' })
    const sent: Record<string, unknown>[] = []
    let call = 0
    api.putSettings.mockImplementation(async (_ref: unknown, next: Record<string, unknown>) => {
      sent.push({ ...next })
      call += 1
      if (call === 1) {
        // A succeeds.
        return { owner: 'o', repo: 'other-one', settings: { ...next, revision: 1 } }
      }
      if (call === 2) throw new Error('network died')   // B fails
      // C conflicts, and the other tab has since flipped A's field back.
      throw new SettingsConflictError('changed elsewhere', {
        triage_labels: [], unlabeled_is_untriaged: true,
        good_first_issue_labels: [], notify_on_new_issue: false, revision: 5,
      })
    })
    renderFor('other-one')

    await waitFor(() => expect(screen.getByRole('switch', { name: /new issue/i })).toBeTruthy())
    await userEvent.click(screen.getByRole('switch', { name: /new issue/i }))   // A
    await waitFor(() => expect(sent).toHaveLength(1))
    await userEvent.click(screen.getByRole('switch', { name: /no labels/i }))   // B (fails)
    await waitFor(() => expect(sent).toHaveLength(2))
    await userEvent.click(screen.getByRole('switch', { name: /no labels/i }))   // C (conflicts)
    await waitFor(() => expect(api.putSettings).toHaveBeenCalledTimes(4))

    // A's field was persisted and the server has since changed it, so this tab
    // must NOT push its old value back.
    const rebased = sent[3] as { notify_on_new_issue: boolean }
    expect(rebased.notify_on_new_issue).toBe(false)
  })

  it('never reverts an external field from a queued save', async () => {
    // After a conflict rebase advances the revision, sending the whole local draft
    // would submit this tab's STALE copy of the other tab's fields under an
    // accepted revision — silently reverting them. Every payload must be built
    // from the newest server document plus this tab's dirty keys.
    setCtx({ owner: 'o', repo: 'other-one' })
    const sent: Record<string, unknown>[] = []
    let call = 0
    api.putSettings.mockImplementation(async (_ref: unknown, next: Record<string, unknown>) => {
      sent.push({ ...next })
      call += 1
      if (call === 1) {
        // Another tab appended a label and moved the revision.
        throw new SettingsConflictError('changed elsewhere', {
          triage_labels: ['from-other-tab'], unlabeled_is_untriaged: true,
          good_first_issue_labels: [], notify_on_new_issue: false, revision: 9,
        })
      }
      return { owner: 'o', repo: 'other-one', settings: { ...next, revision: 9 + call } }
    })
    renderFor('other-one')

    await waitFor(() => expect(screen.getByRole('switch', { name: /new issue/i })).toBeTruthy())
    await userEvent.click(screen.getByRole('switch', { name: /new issue/i }))
    await waitFor(() => expect(api.putSettings).toHaveBeenCalledTimes(2))
    // A SECOND, unrelated edit queued after the rebase.
    await userEvent.click(screen.getByRole('switch', { name: /no labels/i }))
    await waitFor(() => expect(api.putSettings).toHaveBeenCalledTimes(3))

    const last = sent[2] as { triage_labels: string[]; notify_on_new_issue: boolean }
    // The other tab's label must still be there — this is the regression.
    expect(last.triage_labels).toEqual(['from-other-tab'])
    expect(last.notify_on_new_issue).toBe(true)
  })

  it('does not let an earlier save overwrite a newer queued edit', async () => {
    // Rapid edits A then B: A's response used to replace the pending draft, so B
    // re-sent A's document and the user's latest change silently vanished.
    setCtx({ owner: 'o', repo: 'other-one' })
    const sent: Record<string, unknown>[] = []
    let releaseFirst: () => void = () => {}
    const firstHeld = new Promise<void>((r) => { releaseFirst = r })
    let call = 0
    api.putSettings.mockImplementation(async (_ref: unknown, next: Record<string, unknown>) => {
      sent.push({ ...next })
      call += 1
      if (call === 1) await firstHeld
      return { owner: 'o', repo: 'other-one', settings: { ...next, revision: call } }
    })
    renderFor('other-one')

    await waitFor(() => expect(screen.getByRole('switch', { name: /new issue/i })).toBeTruthy())
    // Edit A — held open server-side.
    await userEvent.click(screen.getByRole('switch', { name: /new issue/i }))
    await waitFor(() => expect(sent).toHaveLength(1))
    // Edit B while A is still in flight.
    await userEvent.click(screen.getByRole('switch', { name: /no labels/i }))
    releaseFirst()

    await waitFor(() => expect(api.putSettings).toHaveBeenCalledTimes(2))
    // The second write must carry BOTH edits, not a re-send of A.
    const second = sent[1] as { notify_on_new_issue: boolean; unlabeled_is_untriaged: boolean }
    expect(second.notify_on_new_issue).toBe(true)
    expect(second.unlabeled_is_untriaged).toBe(false)
  })
})

describe('RepoSettings — Open Tagging', () => {
  it('switches to this repo before navigating when it is not the active one', async () => {
    // The settings page can be open for a repo that is NOT active. Navigating
    // without switching showed the Tagging dashboard for a different repository,
    // so label writes would have gone to the wrong repo.
    setCtx({ owner: 'o', repo: 'active-one' })
    renderFor('other-one')

    await waitFor(() => expect(screen.getByRole('button', { name: /Open Tagging/ })).toBeTruthy())
    await userEvent.click(screen.getByRole('button', { name: /Open Tagging/ }))

    expect(switchRepo).toHaveBeenCalledWith({ owner: 'o', repo: 'other-one' })
    expect(openDashboard).toHaveBeenCalledWith('tagging')
    // Order matters: switching after navigating would render the wrong repo first.
    expect(switchRepo.mock.invocationCallOrder[0])
      .toBeLessThan(openDashboard.mock.invocationCallOrder[0])
  })

  it('does not switch when the repo is already active', async () => {
    // switchRepo resets the saved issue and PR filters, which would be a
    // surprising side effect of navigating within the repo you are already on.
    setCtx({ owner: 'o', repo: 'other-one' })
    renderFor('other-one')

    await waitFor(() => expect(screen.getByRole('button', { name: /Open Tagging/ })).toBeTruthy())
    await userEvent.click(screen.getByRole('button', { name: /Open Tagging/ }))

    expect(switchRepo).not.toHaveBeenCalled()
    expect(openDashboard).toHaveBeenCalledWith('tagging')
  })
})
