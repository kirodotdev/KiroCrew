import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { CrewSettings, RepoRef } from '../apps/issue-radar/api'

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

/** The repo-wide crew protocol, one document per SCOPE. The two differ in every
 *  field, so a value on screen names the repository it came from. */
const PROTOCOL: Record<string, CrewSettings> = {
  github: {
    schema: 1,
    claim_ttl_hours: 48,
    needs_human_label: 'crew: needs human',
    commit_trailer: 'Crew: {name}',
  },
  gitlab: {
    schema: 1,
    claim_ttl_hours: 12,
    needs_human_label: 'crew: ask a human',
    commit_trailer: 'Crew: {name} <{id}>',
  },
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
  // Per-ref, not a fixed document: the crew protocol card is asserted to pick up
  // the settings of the repo it is currently mounted for.
  api.getCrewSettings.mockImplementation(async (ref: RepoRef) => ({
    settings: PROTOCOL[ref.provider ?? 'github'],
  }))
  api.putCrewSettings.mockImplementation(async (ref: RepoRef, patch: Partial<CrewSettings>) => ({
    settings: { ...PROTOCOL[ref.provider ?? 'github'], ...patch },
  }))
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
    // Every toggle autosaves. Two quick clicks send two writes built on the same
    // revision: the first succeeds, the second 409's, and clearing the draft
    // would throw away the newer edit — the user's last click silently undone.
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
    // Rapid edits A then B: if A's response replaced the pending draft, B would
    // re-send A's document and the user's latest change would silently vanish.
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
    // without switching would show the Tagging dashboard for a different
    // repository, so label writes would go to the wrong repo.
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

/**
 * The crew protocol card is identified by the repo SCOPE, not by the slug.
 *
 * That card holds an uncommitted draft until the server answers, and KEEPS the
 * text when a write fails — which is what makes an unkeyed instance dangerous
 * rather than merely untidy. `SettingsView` routes to this page with a key of
 * `owner/repo`, so opening the same slug on a different provider or host
 * re-renders THIS page instead of mounting a new one: with no scope key on the
 * card, a retained draft survives that navigation and the next retry writes one
 * repository's typed value into another repository's settings.
 *
 * `rerender` with a new ref at the same tree position is exactly that navigation.
 * The card's input is re-queried after every navigation, never captured before
 * one: a remount replaces the node, and a stale reference reports the old
 * instance's value forever.
 */
describe('RepoSettings — the crew protocol card is keyed by scope', () => {
  const GITHUB: RepoRef = { owner: 'o', repo: 'widget', provider: 'github', host: 'github.com' }
  const GITLAB: RepoRef = { owner: 'o', repo: 'widget', provider: 'gitlab', host: 'gitlab.example.com' }

  const ttl = () => screen.getByTestId('crew-desk-claim-ttl') as HTMLInputElement
  const state = () => screen.getByTestId('crew-desk-protocol-status').getAttribute('data-state')

  /** Render the page for one ref, and return the navigation the rail performs. */
  function open(ref: RepoRef) {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const view = render(
      <QueryClientProvider client={qc}><RepoSettings repoRef={ref} /></QueryClientProvider>,
    )
    return (next: RepoRef) => view.rerender(
      <QueryClientProvider client={qc}><RepoSettings repoRef={next} /></QueryClientProvider>,
    )
  }

  /** Set the claim TTL and commit it, the way a blur does. `fireEvent`, matching
   *  the card's own tests: user-event enforces a number input's validity as it
   *  types, so an intermediate value cannot be set. */
  const commitTtl = (value: string) => {
    fireEvent.change(ttl(), { target: { value } })
    fireEvent.blur(ttl())
  }

  /** A REFUSED write of `24` on the GitHub scope, leaving the draft on screen. */
  async function failedDraft() {
    api.putCrewSettings.mockRejectedValue(new Error('403 forbidden'))
    const navigate = open(GITHUB)
    await waitFor(() => expect(ttl().value).toBe('48'))
    commitTtl('24')
    await waitFor(() => expect(state()).toBe('failed'))
    // Retained — the precondition that makes the missing key reachable.
    expect(ttl().value).toBe('24')
    return navigate
  }

  it('drops a retained draft when the scope changes', async () => {
    const navigate = await failedDraft()

    // The same slug on another provider and host: a different repository.
    navigate(GITLAB)

    // This repo's saved value, never the draft typed against the other one. A
    // reused instance still shows '24', and blurring the field then writes that
    // number to a repository the user never typed it for.
    await waitFor(() => expect(ttl().value).toBe('12'))
    // And the only write attempted is still the original one, addressed to the
    // repo it was typed for.
    expect(api.putCrewSettings).toHaveBeenCalledTimes(1)
    expect(api.putCrewSettings).toHaveBeenCalledWith(GITHUB, { claim_ttl_hours: 24 })
  })

  it('keeps a retained draft across a re-render that is not a scope change', async () => {
    const navigate = await failedDraft()

    // A new ref OBJECT for the same repository — a parent re-render, not a
    // navigation. The draft is the only copy of what was typed, so it has to
    // survive: a key that changes per render, or per ref identity, discards it
    // here and the failed value is gone.
    navigate({ ...GITHUB })

    await waitFor(() => expect(ttl().value).toBe('24'))
  })

  it('clears a stale failure when the scope changes', async () => {
    const navigate = await failedDraft()
    navigate(GITLAB)

    // The failure described a write to the repository just navigated away from.
    // Left standing, it reads as this repository refusing an edit nobody made
    // here.
    await waitFor(() => expect(state()).toBe('idle'))
  })

  it('clears an in-flight save indicator when the scope changes', async () => {
    // A write that never answers. The indicator belongs to the old repository, so
    // the new one must not open underneath a spinner for a write it has no part
    // in.
    api.putCrewSettings.mockImplementation(() => new Promise(() => {}))
    const navigate = open(GITHUB)
    await waitFor(() => expect(ttl().value).toBe('48'))
    commitTtl('24')
    await waitFor(() => expect(state()).toBe('saving'))

    navigate(GITLAB)

    await waitFor(() => expect(state()).toBe('idle'))
    await waitFor(() => expect(ttl().value).toBe('12'))
  })
})
