import { describe, it, expect, vi, beforeEach } from 'vitest'

import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

// The PR action bars mutate somebody's repository, so every test here asserts on
// what the api layer was ASKED to do — the same contract as the Tagging tests.
//
// The load-bearing assertions, stated up front so a future edit knows what it would
// be removing:
//
//  * Merge is offered only when the provider reports the PR mergeable, and never in
//    bulk. It cannot bypass a gate — the provider enforces branch protection on its
//    own endpoint — but a button on a blocked PR would have only one outcome, and 50
//    irreversible merges from one click is a blast radius no confirmation fixes.
//  * A large bulk selection is CHUNKED on the server's published cap, because the
//    server rejects an over-cap batch outright.
//  * A bulk CLOSE requires the typed confirmation token; the reversible actions do
//    not. That asymmetry is the point — gating everything trains the user to type
//    past it.
//  * Partial failure is rendered per PR, not collapsed into "done".
//  * A read-only repo offers no actions at all, rather than buttons that 403.
const api = {
  setPrState: vi.fn(),
  submitPrReview: vi.fn(),
  addPrComment: vi.fn(),
  mergePr: vi.fn(),
  setPrAutoMerge: vi.fn(),
  pullRuns: vi.fn(),
  pullRunAction: vi.fn(),
  bulkPrAction: vi.fn(),
}
vi.mock('../apps/issue-radar/api', () => ({ issueRadarApi: api }))

const ctx = { value: {} as Record<string, unknown> }
vi.mock('../apps/issue-radar/context', () => ({
  useIssueRadar: () => ctx.value,
}))

const PrActionsBar = (await import('../apps/issue-radar/components/PrActionsBar')).default
const PrBulkBar = (await import('../apps/issue-radar/components/PrBulkBar')).default
const PrRunActions = (await import('../apps/issue-radar/components/PrRunActions')).default
const { BULK_PR_CLOSE_TOKEN, SEQUENTIAL_MERGE_TOKEN } = await import('../apps/issue-radar/components/PrBulkBar')

const REF = { owner: 'o', repo: 'r' }

const PULL = {
  number: 7,
  title: 'Fix the thing',
  url: 'https://github.com/o/r/pull/7',
  state: 'open',
  draft: false,
  labels: [],
  updated_at: '2026-07-01T00:00:00Z',
  created_at: '2026-07-01T00:00:00Z',
  author: 'alice',
  merged_at: null,
  // The head COMMIT. Carried on the LIST row (not just the detail) because a bulk
  // approve pins each verdict to the revision its row was rendered at.
  head_sha: 'abc1234',
}

/** A detail read that knows its head commit — which is what the two VERDICT buttons
 * wait for. A review is a statement about a revision, so until the detail lands there
 * is nothing to pin one to and the bar offers neither. */
const REVIEWABLE = { ...PULL, mergeable: null, mergeable_state: 'unknown', head_sha: 'abc1234' }

function wrap(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>)
}

/** A second row, so a two-PR selection is actually VISIBLE.
 *
 * The bulk bar intersects its selection with the rendered rows — a tick for a PR
 * the active filter hides is dropped rather than acted on — so a fixture that ticks
 * #8 while only #7 is rendered would be testing the guard, not the action. */
const PULL_8 = { ...PULL, number: 8, title: 'Fix the other thing', head_sha: 'def5678' }

function setCtx(over: Record<string, unknown> = {}) {
  ctx.value = {
    active: REF,
    canWrite: true,
    checkedPulls: new Set<number>(),
    sortedPulls: [PULL, PULL_8],
    togglePullChecked: vi.fn(),
    toggleAllPullsChecked: vi.fn(),
    clearCheckedPulls: vi.fn(),
    prBulkMax: 50,
    ...over,
  }
}

beforeEach(() => {
  vi.clearAllMocks()
  setCtx()
  api.setPrState.mockResolvedValue({ ...REF, number: 7, state: 'closed', merged: false, draft: false })
  api.submitPrReview.mockResolvedValue({ ...REF, number: 7, id: 1, state: 'APPROVED', submitted_at: 't' })
  api.addPrComment.mockResolvedValue({ ...REF, number: 7, id: 2, url: 'u', created_at: 't' })
  api.mergePr.mockResolvedValue({ ...REF, number: 7, merged: true, sha: 'abc1234', message: '' })
  api.setPrAutoMerge.mockResolvedValue({ ...REF, number: 7, auto_merge: true, method: 'SQUASH', enabled_at: 't' })
  api.pullRuns.mockResolvedValue({ ...REF, number: 7, runs: [] })
  api.pullRunAction.mockResolvedValue({ ...REF, number: 7, run_id: 1, cancelled: true })
  api.bulkPrAction.mockResolvedValue({ ...REF, action: 'approve', applied: [], failed: [] })
})

describe('PrActionsBar — merge boundaries', () => {
  it('offers Merge only when the provider says the PR is mergeable', async () => {
    // Not a gate bypass — the provider enforces branch protection on its own merge
    // endpoint. But a button on a BLOCKED PR would have only one outcome (a refusal),
    // so it is not offered there; auto-merge is the affordance for that case.
    wrap(<PrActionsBar repoRef={REF} pull={PULL} canWrite />)
    expect(screen.queryByRole('button', { name: /^merge$/i })).toBeNull()
    expect(screen.getByRole('button', { name: /auto-merge/i })).toBeTruthy()
  })

  it('merges pinned to the head commit the pane rendered', async () => {
    const detail = { ...PULL, mergeable: true, mergeable_state: 'clean', draft: false, head_sha: 'abc1234' }
    wrap(<PrActionsBar repoRef={REF} pull={PULL} detail={detail as never} canWrite />)
    await userEvent.click(screen.getByRole('button', { name: /^merge$/i }))
    // The sha is passed, not omitted: without the pin, a push landing between the
    // read and the click would merge code nobody reviewed.
    await waitFor(() => expect(api.mergePr).toHaveBeenCalledWith(REF, 7, 'abc1234', 'SQUASH'))
  })

  it('offers no Merge on a BLOCKED pull request', () => {
    // `mergeable` means only "no conflicts". A PR whose required reviews or checks
    // have not passed is mergeable:true / mergeable_state:"blocked" — which the
    // provider refuses for an ordinary user but HONOURS for an admin holding
    // bypass-branch-protection. Gating on `mergeable` alone offered exactly that
    // account a one-click way to land a PR its own rules had rejected.
    // `unstable` is here too: it does not distinguish a failing REQUIRED check from
    // an optional one, so it cannot be read as "protections satisfied".
    // So is GitLab's LEGACY `can_be_merged`, which comes from the old `merge_status`
    // field and means only "no conflicts" — it knows nothing about unmet approvals or
    // a red required pipeline, so admitting it reproduced this same hole on older
    // servers. Its modern replacement `mergeable` DOES imply those rules are met.
    for (const state of [
      'blocked', 'behind', 'dirty', 'draft', 'unknown', 'unstable', 'can_be_merged',
    ]) {
      const detail = { ...PULL, mergeable: true, mergeable_state: state, head_sha: 'abc1234' }
      const { unmount } = wrap(
        <PrActionsBar repoRef={REF} pull={PULL} detail={detail as never} canWrite />,
      )
      expect(screen.queryByRole('button', { name: /^merge$/i }), state).toBeNull()
      unmount()
    }
  })

  it('offers Merge only on a state that confirms the protections are satisfied', () => {
    for (const state of ['clean', 'has_hooks', 'mergeable']) {
      const detail = { ...PULL, mergeable: true, mergeable_state: state, head_sha: 'abc1234' }
      const { unmount } = wrap(
        <PrActionsBar repoRef={REF} pull={PULL} detail={detail as never} canWrite />,
      )
      expect(screen.getByRole('button', { name: /^merge$/i }), state).toBeTruthy()
      unmount()
    }
  })

  it('offers no Merge until the head commit is known', () => {
    // A mergeable verdict with no sha yet cannot be pinned, so it is not offered.
    const detail = { ...PULL, mergeable: true, mergeable_state: 'clean', draft: false, head_sha: null }
    wrap(<PrActionsBar repoRef={REF} pull={PULL} detail={detail as never} canWrite />)
    expect(screen.queryByRole('button', { name: /^merge$/i })).toBeNull()
  })

  it('waits rather than flashing while mergeability is still computing', () => {
    // The provider computes it lazily and answers null first.
    const detail = { ...PULL, mergeable: null, mergeable_state: 'unknown', head_sha: 'abc1234' }
    wrap(<PrActionsBar repoRef={REF} pull={PULL} detail={detail as never} canWrite />)
    expect(screen.queryByRole('button', { name: /^merge$/i })).toBeNull()
  })

  it('never offers to merge a draft', () => {
    const detail = { ...PULL, mergeable: true, mergeable_state: 'clean', draft: true, head_sha: 'abc1234' }
    wrap(<PrActionsBar repoRef={REF} pull={PULL} detail={detail as never} canWrite />)
    expect(screen.queryByRole('button', { name: /^merge$/i })).toBeNull()
  })

  it('arms the provider auto-merge rather than merging', async () => {
    wrap(<PrActionsBar repoRef={REF} pull={PULL} canWrite />)
    await userEvent.click(screen.getByRole('button', { name: /auto-merge/i }))
    await waitFor(() => expect(api.setPrAutoMerge).toHaveBeenCalledWith(REF, 7, true, 'SQUASH'))
  })

  it('offers to cancel when auto-merge is already armed', async () => {
    const detail = { ...PULL, auto_merge: { method: 'SQUASH', enabled_by: 'bob' } }
    wrap(<PrActionsBar repoRef={REF} pull={PULL} detail={detail as never} canWrite />)
    await userEvent.click(screen.getByRole('button', { name: /cancel auto-merge/i }))
    await waitFor(() => expect(api.setPrAutoMerge).toHaveBeenCalledWith(REF, 7, false, 'SQUASH'))
  })
})

describe('PrActionsBar — lifecycle gating', () => {
  it('offers no actions on a read-only repo', () => {
    wrap(<PrActionsBar repoRef={REF} pull={PULL} canWrite={false} />)
    expect(screen.queryAllByRole('button')).toHaveLength(0)
    // …and says why, rather than rendering an empty bar.
    expect(screen.getByText(/read-only/i)).toBeTruthy()
  })

  it('offers no actions on a merged PR', () => {
    const detail = { ...PULL, merged: true, state: 'closed' }
    wrap(<PrActionsBar repoRef={REF} pull={PULL} detail={detail as never} canWrite />)
    expect(screen.queryAllByRole('button')).toHaveLength(0)
  })

  it('reads the lifecycle from the DETAIL, not the stale list row', () => {
    // The row still says open; the detail knows it was merged elsewhere. Acting on
    // the row would offer "approve" on a finished PR.
    const detail = { ...PULL, merged: true }
    wrap(<PrActionsBar repoRef={REF} pull={PULL} detail={detail as never} canWrite />)
    expect(screen.queryByRole('button', { name: /approve/i })).toBeNull()
  })

  it('offers only reopen on a closed-unmerged PR', () => {
    const detail = { ...PULL, state: 'closed', merged: false }
    wrap(<PrActionsBar repoRef={REF} pull={PULL} detail={detail as never} canWrite />)
    expect(screen.getByRole('button', { name: /reopen/i })).toBeTruthy()
    // Approving or arming a closed PR would be refused by the provider.
    expect(screen.queryByRole('button', { name: /approve/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /auto-merge/i })).toBeNull()
  })
})

describe('PrActionsBar — verdicts that need prose', () => {
  it('opens a composer instead of firing on click', async () => {
    wrap(<PrActionsBar repoRef={REF} pull={PULL} detail={REVIEWABLE as never} canWrite />)
    await userEvent.click(screen.getByRole('button', { name: /request changes/i }))
    expect(api.submitPrReview).not.toHaveBeenCalled()
    expect(screen.getByRole('textbox')).toBeTruthy()
  })

  it('will not submit a change request without a reason', async () => {
    wrap(<PrActionsBar repoRef={REF} pull={PULL} detail={REVIEWABLE as never} canWrite />)
    await userEvent.click(screen.getByRole('button', { name: /request changes/i }))
    // The submit button is the primary one inside the composer.
    const submit = screen.getAllByRole('button').find((b) => /request changes/i.test(b.textContent ?? ''))
    expect(submit).toBeTruthy()
    await userEvent.click(submit!)
    expect(api.submitPrReview).not.toHaveBeenCalled()
  })

  it('submits a change request once a reason is typed', async () => {
    wrap(<PrActionsBar repoRef={REF} pull={PULL} detail={REVIEWABLE as never} canWrite />)
    await userEvent.click(screen.getByRole('button', { name: /request changes/i }))
    await userEvent.type(screen.getByRole('textbox'), 'needs a test')
    const submit = screen.getAllByRole('button').find((b) => /request changes/i.test(b.textContent ?? ''))
    await userEvent.click(submit!)
    await waitFor(() =>
      expect(api.submitPrReview)
        .toHaveBeenCalledWith(REF, 7, 'request_changes', 'needs a test', 'abc1234'))
  })

  it('allows a bodyless approval, because the provider does', async () => {
    wrap(<PrActionsBar repoRef={REF} pull={PULL} detail={REVIEWABLE as never} canWrite />)
    await userEvent.click(screen.getByRole('button', { name: /^approve$/i }))
    const submit = screen.getAllByRole('button').find((b) => /approve/i.test(b.textContent ?? ''))
    await userEvent.click(submit!)
    await waitFor(() =>
      expect(api.submitPrReview)
        .toHaveBeenCalledWith(REF, 7, 'approve', undefined, 'abc1234'))
  })

  it('offers no verdict buttons until the head commit is known', () => {
    // A review is a verdict on a REVISION. With no detail read yet there is no sha to
    // pin one to, so approve / request-changes are not offered — the alternative is a
    // button whose only outcome is the server's `head_sha_required`. Commenting is not
    // a verdict and stays available.
    wrap(<PrActionsBar repoRef={REF} pull={PULL} canWrite />)
    expect(screen.queryByRole('button', { name: /^approve$/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /request changes/i })).toBeNull()
    expect(screen.getByRole('button', { name: /^comment$/i })).toBeTruthy()
  })

  it('pins a verdict to the detail sha, never the list row', async () => {
    // The list row can be minutes old. The pin exists so the approval names the commit
    // the pane actually RENDERED, and the detail read is what supplies that.
    const detail = { ...PULL, mergeable: null, mergeable_state: 'unknown', head_sha: 'fe0fe0f' }
    wrap(<PrActionsBar repoRef={REF} pull={PULL} detail={detail as never} canWrite />)
    await userEvent.click(screen.getByRole('button', { name: /^approve$/i }))
    const submit = screen.getAllByRole('button').find((b) => /approve/i.test(b.textContent ?? ''))
    await userEvent.click(submit!)
    await waitFor(() =>
      expect(api.submitPrReview).toHaveBeenCalledWith(REF, 7, 'approve', undefined, 'fe0fe0f'))
  })

  it('submits the sha showing when the composer OPENED, not the latest polled one', async () => {
    // The detail query POLLS. Reading the live sha at submit time meant a force-push
    // landing while the reviewer typed silently re-pointed the approval at the new head
    // — and the server-side pin cannot catch that, because the request carries the NEW
    // sha and so there is nothing to refuse. Freezing it at open turns the race into the
    // provider's own 422 instead of a recorded verdict on code nobody read.
    const atOpen = { ...PULL, mergeable: null, mergeable_state: 'unknown', head_sha: 'seen111' }
    const afterForcePush = { ...atOpen, head_sha: 'pushed99' }
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const { rerender } = render(
      <QueryClientProvider client={qc}>
        <PrActionsBar repoRef={REF} pull={PULL} detail={atOpen as never} canWrite />
      </QueryClientProvider>,
    )
    await userEvent.click(screen.getByRole('button', { name: /^approve$/i }))
    // …the poll lands mid-composer.
    rerender(
      <QueryClientProvider client={qc}>
        <PrActionsBar repoRef={REF} pull={PULL} detail={afterForcePush as never} canWrite />
      </QueryClientProvider>,
    )
    const submit = screen.getAllByRole('button').find((b) => /approve/i.test(b.textContent ?? ''))
    await userEvent.click(submit!)
    await waitFor(() =>
      expect(api.submitPrReview)
        .toHaveBeenCalledWith(REF, 7, 'approve', undefined, 'seen111'))
  })

  it('picks up the new sha for the NEXT composer after a force-push', async () => {
    // The freeze is per-composer, not permanent: once the reviewer closes and reopens,
    // they are looking at the new head and the verdict should name it.
    const atOpen = { ...PULL, mergeable: null, mergeable_state: 'unknown', head_sha: 'seen111' }
    const afterForcePush = { ...atOpen, head_sha: 'pushed99' }
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const { rerender } = render(
      <QueryClientProvider client={qc}>
        <PrActionsBar repoRef={REF} pull={PULL} detail={atOpen as never} canWrite />
      </QueryClientProvider>,
    )
    await userEvent.click(screen.getByRole('button', { name: /^approve$/i }))
    await userEvent.keyboard('{Escape}')
    rerender(
      <QueryClientProvider client={qc}>
        <PrActionsBar repoRef={REF} pull={PULL} detail={afterForcePush as never} canWrite />
      </QueryClientProvider>,
    )
    await userEvent.click(screen.getByRole('button', { name: /^approve$/i }))
    const submit = screen.getAllByRole('button').find((b) => /approve/i.test(b.textContent ?? ''))
    await userEvent.click(submit!)
    await waitFor(() =>
      expect(api.submitPrReview)
        .toHaveBeenCalledWith(REF, 7, 'approve', undefined, 'pushed99'))
  })

  it('does not carry a failure from one PR onto the next', async () => {
    // The error lives in the hook and the pane is not remounted per PR, so without
    // clearing it a maintainer reads "Action failed · PR #7 is locked" while looking
    // at #8 — a failure notice naming the wrong number, on a healthy PR.
    api.setPrState.mockRejectedValue(new Error('PR #7 is locked'))
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const { rerender } = render(
      <QueryClientProvider client={qc}><PrActionsBar repoRef={REF} pull={PULL} canWrite /></QueryClientProvider>,
    )
    await userEvent.click(screen.getByRole('button', { name: /^close$/i }))
    await waitFor(() => expect(screen.getByText(/action failed/i)).toBeTruthy())

    rerender(
      <QueryClientProvider client={qc}><PrActionsBar repoRef={REF} pull={PULL_8} canWrite /></QueryClientProvider>,
    )
    await waitFor(() => expect(screen.queryByText(/action failed/i)).toBeNull())
  })

  it('does not submit twice when Cmd+Enter is pressed again mid-flight', async () => {
    // The keyboard path can fire while the first request is pending, which posted a
    // duplicate comment or review.
    let resolve: (v: unknown) => void = () => {}
    api.addPrComment.mockImplementation(() => new Promise((r) => { resolve = r }))
    wrap(<PrActionsBar repoRef={REF} pull={PULL} canWrite />)
    await userEvent.click(screen.getByRole('button', { name: /^comment$/i }))
    const box = screen.getByRole('textbox')
    await userEvent.type(box, 'ship it')
    await userEvent.keyboard('{Meta>}{Enter}{/Meta}')
    await waitFor(() => expect(api.addPrComment).toHaveBeenCalledTimes(1))
    await userEvent.keyboard('{Meta>}{Enter}{/Meta}')
    await userEvent.keyboard('{Control>}{Enter}{/Control}')
    expect(api.addPrComment).toHaveBeenCalledTimes(1)
    resolve({ ...REF, number: 7, id: 1, url: 'u', created_at: 't' })
  })

  it('keeps the typed text when the submit fails', async () => {
    api.submitPrReview.mockRejectedValue(new Error('GitHub said no'))
    wrap(<PrActionsBar repoRef={REF} pull={PULL} detail={REVIEWABLE as never} canWrite />)
    await userEvent.click(screen.getByRole('button', { name: /request changes/i }))
    await userEvent.type(screen.getByRole('textbox'), 'a whole paragraph')
    const submit = screen.getAllByRole('button').find((b) => /request changes/i.test(b.textContent ?? ''))
    await userEvent.click(submit!)
    // The error is surfaced AND the prose survives — retyping a paragraph after a
    // transient failure is the thing this avoids.
    await waitFor(() => expect(screen.getByText(/GitHub said no/)).toBeTruthy())
    expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('a whole paragraph')
  })
})

describe('PrBulkBar', () => {
  it('renders nothing with an empty selection', () => {
    setCtx({ checkedPulls: new Set() })
    const { container } = wrap(<PrBulkBar />)
    expect(container.textContent).toBe('')
  })

  it('renders nothing on a read-only repo', () => {
    setCtx({ canWrite: false, checkedPulls: new Set([7, 8]) })
    const { container } = wrap(<PrBulkBar />)
    expect(container.textContent).toBe('')
  })

  it('applies a reversible action directly, with no confirmation step', async () => {
    setCtx({ checkedPulls: new Set([7, 8]) })
    wrap(<PrBulkBar />)
    await userEvent.click(screen.getByRole('button', { name: /^approve$/i }))
    await waitFor(() =>
      expect(api.bulkPrAction).toHaveBeenCalledWith(REF, [7, 8], 'approve', {
        body: undefined,
        // Each PR pinned to the sha of the row the user actually saw.
        headShas: { 7: 'abc1234', 8: 'def5678' },
      }))
  })

  it('will not bulk-approve when a selected row has no head commit', async () => {
    // A partial sha map is refused by the server outright, and approving only the
    // subset that happens to have one would apply the action to fewer PRs than the
    // button's own count claims. So the button waits rather than doing either.
    const noSha = { ...PULL_8, head_sha: null }
    setCtx({ checkedPulls: new Set([7, 8]), sortedPulls: [PULL, noSha] })
    wrap(<PrBulkBar />)
    const approve = screen.getByRole('button', { name: /^approve$/i })
    expect((approve as HTMLButtonElement).disabled).toBe(true)
    await userEvent.click(approve)
    expect(api.bulkPrAction).not.toHaveBeenCalled()
    // The other verbs are unaffected — they act on the PR, not on a revision.
    expect((screen.getByRole('button', { name: /^close$/i }) as HTMLButtonElement).disabled)
      .toBe(false)
  })

  it('approves the sha each row carried WHEN TICKED, not the latest polled one', async () => {
    // Same race as the per-PR composer, on the list: the pulls query polls, so reading
    // `head_sha` at submit time let a force-push between the tick and the click
    // re-target the approval — and the server-side pin cannot catch it, because the
    // request carries the NEW sha. Snapshotting at tick time makes the race the
    // provider's refusal instead of a silent verdict on unseen code.
    setCtx({ checkedPulls: new Set([7, 8]) })
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const { rerender } = render(
      <QueryClientProvider client={qc}><PrBulkBar /></QueryClientProvider>,
    )
    // A poll lands, force-pushing #7, before Apply is pressed.
    setCtx({
      checkedPulls: new Set([7, 8]),
      sortedPulls: [{ ...PULL, head_sha: 'pushed99' }, PULL_8],
    })
    rerender(<QueryClientProvider client={qc}><PrBulkBar /></QueryClientProvider>)
    await userEvent.click(screen.getByRole('button', { name: /^approve$/i }))
    await waitFor(() =>
      expect(api.bulkPrAction).toHaveBeenCalledWith(REF, [7, 8], 'approve', {
        body: undefined,
        headShas: { 7: 'abc1234', 8: 'def5678' },
      }))
  })

  it('sends no sha map for the verbs that are not pinned', async () => {
    setCtx({ checkedPulls: new Set([7, 8]) })
    wrap(<PrBulkBar />)
    await userEvent.click(screen.getByRole('button', { name: /^reopen$/i }))
    await waitFor(() =>
      expect(api.bulkPrAction).toHaveBeenCalledWith(REF, [7, 8], 'reopen', {
        body: undefined, headShas: undefined,
      }))
  })

  it('requires the typed token before a bulk close', async () => {
    setCtx({ checkedPulls: new Set([7, 8]) })
    wrap(<PrBulkBar />)
    await userEvent.click(screen.getByRole('button', { name: /^close$/i }))
    // Nothing sent yet — the confirmation step is open.
    expect(api.bulkPrAction).not.toHaveBeenCalled()

    const confirm = screen.getByRole('textbox')
    await userEvent.type(confirm, 'not-the-token')
    const apply = screen.getAllByRole('button').find((b) => /apply to/i.test(b.textContent ?? ''))
    expect((apply as HTMLButtonElement).disabled).toBe(true)

    await userEvent.clear(confirm)
    await userEvent.type(confirm, BULK_PR_CLOSE_TOKEN)
    await userEvent.click(
      screen.getAllByRole('button').find((b) => /apply to/i.test(b.textContent ?? ''))!,
    )
    await waitFor(() =>
      expect(api.bulkPrAction).toHaveBeenCalledWith(REF, [7, 8], 'close', { body: undefined }))
  })

  it('the confirm token is a code constant, never translated', () => {
    // A translated token would make the action impossible to complete for anyone
    // not typing English — the same rule SchedulePage's bulk delete follows.
    expect(BULK_PR_CLOSE_TOKEN).toBe('close prs')
  })

  // ── arming auto-merge is offered only where the provider accepts it ──────────
  //
  // The regression these pin, in full: the bar used to offer auto-merge for every
  // ticked row, because the LIST row carried no mergeability at all. GitHub refuses to
  // arm a PR that is already mergeable ("Pull request is in clean status") and one that
  // is already merged ("Pull request is already merged"), so a single click on a
  // seven-row selection came back as seven separate failures — six of them for PRs
  // that were simply READY, i.e. the user's actual intent was to merge them.
  describe('auto-merge is partitioned by what the provider will accept', () => {
    const BLOCKED = { ...PULL, mergeable_state: 'blocked', mergeable: true }
    const BLOCKED_8 = { ...PULL_8, mergeable_state: 'blocked', mergeable: true }
    const CLEAN_8 = { ...PULL_8, mergeable_state: 'clean', mergeable: true }

    it('arms only the rows that are still waiting, never the ready ones', async () => {
      // #7 is blocked (arming is meaningful), #8 is clean (GitHub would refuse).
      setCtx({ checkedPulls: new Set([7, 8]), sortedPulls: [BLOCKED, CLEAN_8] })
      wrap(<PrBulkBar />)
      await userEvent.click(
        screen.getAllByRole('button').find((b) => /^auto-merge/i.test(b.textContent ?? ''))!,
      )
      await waitFor(() =>
        // [7] only — NOT [7, 8]. Sending 8 is the defect.
        expect(api.bulkPrAction).toHaveBeenCalledWith(REF, [7], 'auto_merge', {
          body: undefined, headShas: undefined,
        }))
    })

    it('does not arm at all when every selected row is already mergeable', async () => {
      const clean7 = { ...PULL, mergeable_state: 'clean', mergeable: true }
      setCtx({ checkedPulls: new Set([7, 8]), sortedPulls: [clean7, CLEAN_8] })
      wrap(<PrBulkBar />)
      const arm = screen.getAllByRole('button')
        .find((b) => /^auto-merge/i.test(b.textContent ?? ''))!
      expect((arm as HTMLButtonElement).disabled).toBe(true)
      await userEvent.click(arm)
      expect(api.bulkPrAction).not.toHaveBeenCalled()
    })

    it('does not arm a row that has already merged', async () => {
      // The #1265 case: the row was cached while the PR was still open, and the
      // provider answered "Pull request is already merged".
      const merged = { ...PULL, state: 'closed', merged_at: '2026-08-03T06:45:33Z' }
      setCtx({ checkedPulls: new Set([7, 8]), sortedPulls: [merged, BLOCKED_8] })
      wrap(<PrBulkBar />)
      await userEvent.click(
        screen.getAllByRole('button').find((b) => /^auto-merge/i.test(b.textContent ?? ''))!,
      )
      await waitFor(() =>
        expect(api.bulkPrAction).toHaveBeenCalledWith(REF, [8], 'auto_merge', {
          body: undefined, headShas: undefined,
        }))
    })

    it('treats UNKNOWN mergeability as neither armable nor mergeable', async () => {
      // GitHub computes mergeability asynchronously, so a cold read says `unknown`.
      // A gate that cannot tell must refuse rather than guess in either direction.
      const unknown = { ...PULL, mergeable_state: 'unknown', mergeable: null }
      const unknown8 = { ...PULL_8, mergeable_state: 'unknown', mergeable: null }
      setCtx({ checkedPulls: new Set([7, 8]), sortedPulls: [unknown, unknown8] })
      wrap(<PrBulkBar />)
      const arm = screen.getAllByRole('button')
        .find((b) => /^auto-merge/i.test(b.textContent ?? ''))!
      expect((arm as HTMLButtonElement).disabled).toBe(true)
      // ...and no merge button either, since neither verb applies.
      expect(screen.queryByRole('button', { name: /merge \d+ ready/i })).toBeNull()
    })

    it('does not arm a row carrying merged_at even when its state still says open', async () => {
      // The mutation-proof version of the merged case: the test above sets BOTH
      // `state: 'closed'` and `merged_at`, so the `state` branch alone satisfies it and
      // deleting the `merged_at` guard goes unnoticed. A row merged elsewhere can carry
      // the timestamp while its cached `state` is still open — that IS the #1265 shape.
      // `mergeable_state: 'blocked'` is essential: without it the row is excluded as
      // UNKNOWN anyway, and this test would pass even with the `merged_at` guard
      // deleted — it has to be armable on every OTHER axis so the guard is the only
      // thing keeping it out.
      const stale = {
        ...PULL, state: 'open', merged_at: '2026-08-03T06:45:33Z',
        mergeable_state: 'blocked', mergeable: true,
      }
      setCtx({ checkedPulls: new Set([7, 8]), sortedPulls: [stale, BLOCKED_8] })
      wrap(<PrBulkBar />)
      await userEvent.click(
        screen.getAllByRole('button').find((b) => /^auto-merge/i.test(b.textContent ?? ''))!,
      )
      await waitFor(() =>
        expect(api.bulkPrAction).toHaveBeenCalledWith(REF, [8], 'auto_merge', {
          body: undefined, headShas: undefined,
        }))
    })

    it('does not arm a DRAFT — the provider refuses one', async () => {
      // The per-PR bar has always checked `draft`; the bulk bar must not be looser on
      // the same field, or it reproduces the one-refusal-per-row defect for a new state.
      const draft = { ...PULL, draft: true, mergeable_state: 'blocked', mergeable: true }
      setCtx({ checkedPulls: new Set([7, 8]), sortedPulls: [draft, BLOCKED_8] })
      wrap(<PrBulkBar />)
      await userEvent.click(
        screen.getAllByRole('button').find((b) => /^auto-merge/i.test(b.textContent ?? ''))!,
      )
      await waitFor(() =>
        expect(api.bulkPrAction).toHaveBeenCalledWith(REF, [8], 'auto_merge', {
          body: undefined, headShas: undefined,
        }))
    })

    it('does not offer a DRAFT the sequential merge either', () => {
      // A draft reporting `clean` (transiently, or from a stale cache row) would be a
      // button the provider only refuses.
      const draftClean = { ...PULL, draft: true, mergeable_state: 'clean', mergeable: true }
      setCtx({ checkedPulls: new Set([7]), sortedPulls: [draftClean] })
      wrap(<PrBulkBar />)
      expect(screen.queryByRole('button', { name: /merge \d+ ready/i })).toBeNull()
    })

    it('does not arm a CONFLICTING row — no check resolves a conflict', async () => {
      const dirty = { ...PULL, mergeable_state: 'dirty', mergeable: false }
      setCtx({ checkedPulls: new Set([7, 8]), sortedPulls: [dirty, BLOCKED_8] })
      wrap(<PrBulkBar />)
      await userEvent.click(
        screen.getAllByRole('button').find((b) => /^auto-merge/i.test(b.textContent ?? ''))!,
      )
      await waitFor(() =>
        expect(api.bulkPrAction).toHaveBeenCalledWith(REF, [8], 'auto_merge', {
          body: undefined, headShas: undefined,
        }))
    })

    it('does not treat `unstable` as ready to merge', async () => {
      // `unstable` does not distinguish a failing REQUIRED check from an optional
      // one, so it cannot be read as "protections satisfied" — it belongs on the
      // ARM side, which is what auto-merge is for.
      const unstable = { ...PULL, mergeable_state: 'unstable', mergeable: true }
      setCtx({ checkedPulls: new Set([7]), sortedPulls: [unstable] })
      wrap(<PrBulkBar />)
      expect(screen.queryByRole('button', { name: /merge \d+ ready/i })).toBeNull()
      const arm = screen.getAllByRole('button')
        .find((b) => /^auto-merge/i.test(b.textContent ?? ''))!
      expect((arm as HTMLButtonElement).disabled).toBe(false)
    })
  })

  // ── the ready rows get a real merge path, one at a time ──────────────────────
  describe('sequential merge of the ready rows', () => {
    const CLEAN = { ...PULL, mergeable_state: 'clean', mergeable: true }
    const CLEAN_8 = { ...PULL_8, mergeable_state: 'clean', mergeable: true }

    it('requires its own typed token, which is NOT the close token', async () => {
      setCtx({ checkedPulls: new Set([7]), sortedPulls: [CLEAN] })
      wrap(<PrBulkBar />)
      await userEvent.click(screen.getByRole('button', { name: /merge \d+ ready/i }))
      expect(api.mergePr).not.toHaveBeenCalled()

      // Typing the CLOSE token must not arm a merge — the two are irreversible in
      // different ways and must not be satisfiable by the same typing.
      const confirm = screen.getByRole('textbox')
      await userEvent.type(confirm, BULK_PR_CLOSE_TOKEN)
      const apply = screen.getAllByRole('button')
        .find((b) => /apply to/i.test(b.textContent ?? ''))
      expect((apply as HTMLButtonElement).disabled).toBe(true)

      await userEvent.clear(confirm)
      await userEvent.type(confirm, SEQUENTIAL_MERGE_TOKEN)
      await userEvent.click(
        screen.getAllByRole('button').find((b) => /apply to/i.test(b.textContent ?? ''))!,
      )
      await waitFor(() => expect(api.mergePr).toHaveBeenCalledWith(REF, 7, 'abc1234', 'SQUASH'))
    })

    it('merges the sha each row carried WHEN TICKED, not the latest polled one', async () => {
      // The mutation-proof version of the test below: that one never changes a sha, so
      // it passes even if the code reads the LIVE `p.head_sha`. The list polls, so a
      // force-push between the tick and Apply must not re-target the merge — and unlike
      // a review, a merge cannot be undone. The server-side pin cannot save us here:
      // the request would carry the NEW sha, so there is nothing to refuse.
      setCtx({ checkedPulls: new Set([7]), sortedPulls: [CLEAN] })
      const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
      const { rerender } = render(
        <QueryClientProvider client={qc}><PrBulkBar /></QueryClientProvider>,
      )
      // A poll lands, force-pushing #7, before the merge is confirmed.
      setCtx({
        checkedPulls: new Set([7]),
        sortedPulls: [{ ...CLEAN, head_sha: 'pushed99' }],
      })
      rerender(<QueryClientProvider client={qc}><PrBulkBar /></QueryClientProvider>)
      await userEvent.click(screen.getByRole('button', { name: /merge \d+ ready/i }))
      await userEvent.type(screen.getByRole('textbox'), SEQUENTIAL_MERGE_TOKEN)
      await userEvent.click(
        screen.getAllByRole('button').find((b) => /apply to/i.test(b.textContent ?? ''))!,
      )
      // The sha that was on screen when the row was ticked — NOT 'pushed99'.
      await waitFor(() => expect(api.mergePr).toHaveBeenCalledWith(REF, 7, 'abc1234', 'SQUASH'))
    })

    it('merges the set the confirmation NAMED, even if readiness changed meanwhile', async () => {
      // The target set is frozen when the warning renders. Without that, a cold read
      // (which reports `unknown` for roughly half a page) resolving to `clean` during
      // the confirmation turned "This merges 1 pull request" into six real merges.
      const unknown8 = { ...PULL_8, mergeable_state: 'unknown', mergeable: null }
      setCtx({ checkedPulls: new Set([7, 8]), sortedPulls: [CLEAN, unknown8] })
      const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
      const { rerender } = render(
        <QueryClientProvider client={qc}><PrBulkBar /></QueryClientProvider>,
      )
      // Only #7 is ready, so the confirmation is armed over exactly one row.
      await userEvent.click(screen.getByRole('button', { name: /merge 1 ready/i }))
      // Now mergeability finishes computing for #8 and a poll delivers it.
      setCtx({
        checkedPulls: new Set([7, 8]),
        sortedPulls: [CLEAN, { ...PULL_8, mergeable_state: 'clean', mergeable: true }],
      })
      rerender(<QueryClientProvider client={qc}><PrBulkBar /></QueryClientProvider>)
      await userEvent.type(screen.getByRole('textbox'), SEQUENTIAL_MERGE_TOKEN)
      await userEvent.click(
        screen.getAllByRole('button').find((b) => /apply to/i.test(b.textContent ?? ''))!,
      )
      await waitFor(() => expect(api.mergePr).toHaveBeenCalledTimes(1))
      expect(api.mergePr).toHaveBeenCalledWith(REF, 7, 'abc1234', 'SQUASH')
    })

    it('merges each row pinned to the sha it was rendered at, never in bulk', async () => {
      setCtx({ checkedPulls: new Set([7, 8]), sortedPulls: [CLEAN, CLEAN_8] })
      wrap(<PrBulkBar />)
      await userEvent.click(screen.getByRole('button', { name: /merge \d+ ready/i }))
      await userEvent.type(screen.getByRole('textbox'), SEQUENTIAL_MERGE_TOKEN)
      await userEvent.click(
        screen.getAllByRole('button').find((b) => /apply to/i.test(b.textContent ?? ''))!,
      )
      await waitFor(() => expect(api.mergePr).toHaveBeenCalledTimes(2))
      expect(api.mergePr).toHaveBeenNthCalledWith(1, REF, 7, 'abc1234', 'SQUASH')
      expect(api.mergePr).toHaveBeenNthCalledWith(2, REF, 8, 'def5678', 'SQUASH')
      // `merge` is absent from the server's bulk allowlist, so it must never be
      // routed through the bulk endpoint.
      expect(api.bulkPrAction).not.toHaveBeenCalled()
    })

    it('STOPS at the first refusal instead of merging onto a diverged base', async () => {
      // Each merge changes the base branch, so PR #2's mergeability is a function of
      // #1 having landed. Continuing past a failure keeps merging onto a base whose
      // state no longer matches what was reviewed.
      api.mergePr
        .mockRejectedValueOnce(new Error('required status check is failing'))
      setCtx({ checkedPulls: new Set([7, 8]), sortedPulls: [CLEAN, CLEAN_8] })
      wrap(<PrBulkBar />)
      await userEvent.click(screen.getByRole('button', { name: /merge \d+ ready/i }))
      await userEvent.type(screen.getByRole('textbox'), SEQUENTIAL_MERGE_TOKEN)
      await userEvent.click(
        screen.getAllByRole('button').find((b) => /apply to/i.test(b.textContent ?? ''))!,
      )
      await waitFor(() => expect(screen.getByText(/required status check is failing/)).toBeTruthy())
      // #8 was never attempted.
      expect(api.mergePr).toHaveBeenCalledTimes(1)
      expect(api.mergePr).toHaveBeenCalledWith(REF, 7, 'abc1234', 'SQUASH')
    })

    it('is not offered when a ready row has no head commit to pin to', () => {
      // Same rule as bulk approve: an unpinned merge is a merge of whatever got
      // pushed last.
      const noSha = { ...CLEAN, head_sha: null }
      setCtx({ checkedPulls: new Set([7]), sortedPulls: [noSha] })
      wrap(<PrBulkBar />)
      expect(screen.queryByRole('button', { name: /merge \d+ ready/i })).toBeNull()
    })

    it('unticks only the rows that actually merged', async () => {
      api.mergePr
        .mockResolvedValueOnce({ ...REF, number: 7, merged: true, sha: 'abc1234', message: '' })
        .mockRejectedValueOnce(new Error('blocked'))
      const togglePullChecked = vi.fn()
      setCtx({ checkedPulls: new Set([7, 8]), sortedPulls: [CLEAN, CLEAN_8], togglePullChecked })
      wrap(<PrBulkBar />)
      await userEvent.click(screen.getByRole('button', { name: /merge \d+ ready/i }))
      await userEvent.type(screen.getByRole('textbox'), SEQUENTIAL_MERGE_TOKEN)
      await userEvent.click(
        screen.getAllByRole('button').find((b) => /apply to/i.test(b.textContent ?? ''))!,
      )
      await waitFor(() => expect(togglePullChecked).toHaveBeenCalledWith(7))
      // #8 failed, so it stays ticked for a retry.
      expect(togglePullChecked).not.toHaveBeenCalledWith(8)
    })

    it('the merge token is a code constant and differs from the close token', () => {
      expect(SEQUENTIAL_MERGE_TOKEN).toBe('merge prs')
      expect(SEQUENTIAL_MERGE_TOKEN).not.toBe(BULK_PR_CLOSE_TOKEN)
    })

    it('Cancel ABORTS the run, sparing every row not yet sent', async () => {
      // Cancel on an irreversible mass action has to mean something. The merge already
      // in flight cannot be recalled, but the rest can still be spared.
      let release: (v: unknown) => void = () => {}
      api.mergePr.mockImplementationOnce(
        () => new Promise((res) => { release = res }),
      )
      const CLEAN_9 = {
        ...PULL, number: 9, head_sha: 'ghi9999', mergeable_state: 'clean', mergeable: true,
      }
      setCtx({ checkedPulls: new Set([7, 8, 9]), sortedPulls: [CLEAN, CLEAN_8, CLEAN_9] })
      wrap(<PrBulkBar />)
      await userEvent.click(screen.getByRole('button', { name: /merge \d+ ready/i }))
      await userEvent.type(screen.getByRole('textbox'), SEQUENTIAL_MERGE_TOKEN)
      await userEvent.click(
        screen.getAllByRole('button').find((b) => /apply to/i.test(b.textContent ?? ''))!,
      )
      // #7 is in flight. Cancel now.
      await waitFor(() => expect(api.mergePr).toHaveBeenCalledTimes(1))
      await userEvent.click(screen.getByRole('button', { name: /^cancel$/i }))
      release({ ...REF, number: 7, merged: true, sha: 'abc1234', message: '' })
      // #8 and #9 were never sent.
      await waitFor(() => expect(api.mergePr).toHaveBeenCalledTimes(1))
    })

    it('a second Enter mid-run cannot start a concurrent run', async () => {
      // The Apply button disables on `busy`, but the confirm input's Enter handler is a
      // second entry point — two loops advancing independently would merge a row twice
      // and defeat stop-on-first-failure.
      let release: (v: unknown) => void = () => {}
      api.mergePr.mockImplementationOnce(
        () => new Promise((res) => { release = res }),
      )
      setCtx({ checkedPulls: new Set([7]), sortedPulls: [CLEAN] })
      wrap(<PrBulkBar />)
      await userEvent.click(screen.getByRole('button', { name: /merge \d+ ready/i }))
      const confirm = screen.getByRole('textbox')
      await userEvent.type(confirm, SEQUENTIAL_MERGE_TOKEN)
      await userEvent.type(confirm, '{Enter}')
      await waitFor(() => expect(api.mergePr).toHaveBeenCalledTimes(1))
      // Second Enter while the first row is still in flight.
      await userEvent.type(confirm, '{Enter}')
      release({ ...REF, number: 7, merged: true, sha: 'abc1234', message: '' })
      await waitFor(() => expect(api.mergePr).toHaveBeenCalledTimes(1))
    })

    it('caps the run at the server-published bulk cap', () => {
      // The sequential merge does not use the bulk endpoint, so nothing else bounds it —
      // and rule 3's "50 from one click" reasoning applies to a loop too.
      const many = Array.from({ length: 5 }, (_, i) => ({
        ...PULL, number: 100 + i, head_sha: `sha${i}000`,
        mergeable_state: 'clean', mergeable: true,
      }))
      setCtx({
        checkedPulls: new Set(many.map((p) => p.number)),
        sortedPulls: many,
        prBulkMax: 2,
      })
      wrap(<PrBulkBar />)
      // 5 ready rows, cap of 2 -> the button offers 2, not 5.
      expect(screen.getByRole('button', { name: /merge 2 ready/i })).toBeTruthy()
    })
  })

  it('requires a body for a bulk comment', async () => {
    setCtx({ checkedPulls: new Set([7]) })
    wrap(<PrBulkBar />)
    await userEvent.click(screen.getByRole('button', { name: /^comment$/i }))
    const apply = screen.getAllByRole('button').find((b) => /apply to/i.test(b.textContent ?? ''))
    expect((apply as HTMLButtonElement).disabled).toBe(true)

    await userEvent.type(screen.getByRole('textbox'), 'please rebase')
    await userEvent.click(
      screen.getAllByRole('button').find((b) => /apply to/i.test(b.textContent ?? ''))!,
    )
    await waitFor(() =>
      expect(api.bulkPrAction).toHaveBeenCalledWith(REF, [7], 'comment', { body: 'please rebase' }))
  })

  it('reports partial failure per PR instead of collapsing it', async () => {
    api.bulkPrAction.mockResolvedValue({
      ...REF, action: 'approve',
      applied: [{ number: 7 }],
      failed: [{ number: 8, error: 'PR #8 is locked' }],
    })
    const clearCheckedPulls = vi.fn()
    setCtx({ checkedPulls: new Set([7, 8]), clearCheckedPulls })
    wrap(<PrBulkBar />)
    await userEvent.click(screen.getByRole('button', { name: /^approve$/i }))

    // The failing PR is named, so the user knows which row to revisit.
    await waitFor(() => expect(screen.getByText(/PR #8 is locked/)).toBeTruthy())
    // And the selection is KEPT, so the retry does not mean re-ticking by hand.
    expect(clearCheckedPulls).not.toHaveBeenCalled()
  })

  it('unticks only the SUCCEEDED rows, so a retry cannot duplicate a write', async () => {
    // Unticking only the succeeded rows stops a retry from re-applying to rows
    // that already worked. For `comment` a repeat posts a second copy — the one
    // action here whose duplicate is visible to everyone on the PR.
    api.bulkPrAction.mockResolvedValue({
      ...REF, action: 'comment',
      applied: [{ number: 7 }],
      failed: [{ number: 8, error: 'PR #8 is locked' }],
    })
    const togglePullChecked = vi.fn()
    setCtx({ checkedPulls: new Set([7, 8]), togglePullChecked })
    wrap(<PrBulkBar />)
    await userEvent.click(screen.getByRole('button', { name: /^comment$/i }))
    await userEvent.type(screen.getByRole('textbox'), 'please rebase')
    await userEvent.click(
      screen.getAllByRole('button').find((b) => /apply to/i.test(b.textContent ?? ''))!,
    )

    // #7 succeeded → unticked. #8 failed → left selected for the retry.
    await waitFor(() => expect(togglePullChecked).toHaveBeenCalledWith(7))
    expect(togglePullChecked).not.toHaveBeenCalledWith(8)
  })

  it('empties the selection when every row succeeded', async () => {
    // Unticking per-succeeded-row (rather than clearing wholesale) is what makes a
    // retry safe; a fully clean run therefore leaves nothing selected either way.
    api.bulkPrAction.mockResolvedValue({
      ...REF, action: 'approve',
      applied: [{ number: 7 }, { number: 8 }], failed: [],
    })
    const togglePullChecked = vi.fn()
    setCtx({ checkedPulls: new Set([7, 8]), togglePullChecked })
    wrap(<PrBulkBar />)
    await userEvent.click(screen.getByRole('button', { name: /^approve$/i }))
    await waitFor(() => expect(togglePullChecked).toHaveBeenCalledWith(7))
    expect(togglePullChecked).toHaveBeenCalledWith(8)
  })

  it('offers no auto-merge controls on GitLab', () => {
    // GitLab's arm flag rides on the merge endpoint and merges immediately with no
    // pipeline running, so the client refuses it — the buttons would only ever error.
    setCtx({
      active: { owner: 'g', repo: 'p', provider: 'gitlab', host: 'gitlab.com' },
      checkedPulls: new Set([7, 8]),
    })
    wrap(<PrBulkBar />)
    expect(screen.queryByRole('button', { name: /auto-merge/i })).toBeNull()
    // The safe actions are still offered.
    expect(screen.getByRole('button', { name: /^approve$/i })).toBeTruthy()
  })

  it('reports the rows that landed when a later chunk throws', async () => {
    // The accumulator lives outside the try so chunks that already succeeded are
    // still reported when a later chunk throws; otherwise every one would stay
    // selected and a retry would re-apply to it.
    const many = Array.from({ length: 4 }, (_, i) => ({ ...PULL, number: i + 1 }))
    let call = 0
    api.bulkPrAction.mockImplementation(async (_r: unknown, nums: number[]) => {
      if (++call === 2) throw new Error('gateway went away')
      return { ...REF, action: 'comment', applied: nums.map((n) => ({ number: n })), failed: [] }
    })
    const togglePullChecked = vi.fn()
    setCtx({
      checkedPulls: new Set([1, 2, 3, 4]), sortedPulls: many,
      prBulkMax: 2, togglePullChecked,
    })
    wrap(<PrBulkBar />)
    await userEvent.click(screen.getByRole('button', { name: /^comment$/i }))
    await userEvent.type(screen.getByRole('textbox'), 'please rebase')
    await userEvent.click(
      screen.getAllByRole('button').find((b) => /apply to/i.test(b.textContent ?? ''))!,
    )

    // Chunk 1 (#1,#2) landed and is unticked; chunk 2 threw so #3/#4 stay selected.
    await waitFor(() => expect(togglePullChecked).toHaveBeenCalledWith(1))
    expect(togglePullChecked).toHaveBeenCalledWith(2)
    expect(togglePullChecked).not.toHaveBeenCalledWith(3)
    expect(togglePullChecked).not.toHaveBeenCalledWith(4)
  })

  it('never routes a merge through the BULK endpoint', () => {
    // `merge` is absent from the server's `_BULK_PR_ACTIONS`, so no unconditional
    // "Merge" button appears here and nothing sends that verb to `/pulls/bulk`.
    //
    // The ready rows DO have a merge path now — a sequential run over the per-PR route,
    // capped, typed-confirmed, and offered only for rows the provider already reports
    // mergeable (see the "sequential merge" block above). It is absent here because
    // this fixture carries no `mergeable_state`, i.e. every row reads as UNKNOWN.
    setCtx({ checkedPulls: new Set([7, 8]) })
    wrap(<PrBulkBar />)
    expect(screen.queryByRole('button', { name: /^merge$/i })).toBeNull()
    expect(screen.getByRole('button', { name: /^auto-merge$/i })).toBeTruthy()
  })

  it('chunks a large selection on the server-published cap', async () => {
    // The server rejects an over-cap batch outright, so an unchunked "select all" on
    // a big repo would be a flat 400 with nothing applied. The cap comes from the
    // response, never a hardcoded copy.
    const many = Array.from({ length: 7 }, (_, i) => ({ ...PULL, number: i + 1 }))
    api.bulkPrAction.mockImplementation(async (_r: unknown, nums: number[]) => ({
      ...REF, action: 'approve', applied: nums.map((n) => ({ number: n })), failed: [],
    }))
    setCtx({
      checkedPulls: new Set(many.map((p) => p.number)),
      sortedPulls: many,
      prBulkMax: 3,
    })
    wrap(<PrBulkBar />)
    await userEvent.click(screen.getByRole('button', { name: /^approve$/i }))

    await waitFor(() => expect(api.bulkPrAction).toHaveBeenCalledTimes(3))
    const sizes = api.bulkPrAction.mock.calls.map((c: unknown[]) => (c[1] as number[]).length)
    expect(sizes).toEqual([3, 3, 1])
    // Every PR is still covered exactly once — chunking must not drop or duplicate.
    const sent = api.bulkPrAction.mock.calls.flatMap((c: unknown[]) => c[1] as number[])
    expect(sent.sort((a, b) => a - b)).toEqual([1, 2, 3, 4, 5, 6, 7])
    // Each chunk carries ONLY its own rows' shas: the server requires one for every
    // number in the request, so a whole-selection map would be sending shas for PRs
    // that request does not touch.
    for (const call of api.bulkPrAction.mock.calls) {
      const nums = call[1] as number[]
      const opts = call[3] as { headShas?: Record<string, string> }
      expect(Object.keys(opts.headShas ?? {})).toEqual(nums.map(String))
    }
  })

  it('offers no bulk request-changes', () => {
    // A mass change-request with no per-PR reasoning is not actionable feedback,
    // and the server's allowlist agrees — so the button must not exist.
    setCtx({ checkedPulls: new Set([7, 8]) })
    wrap(<PrBulkBar />)
    expect(screen.queryByRole('button', { name: /request changes/i })).toBeNull()
  })

  it('disarms a typed confirmation when the selection is SWAPPED at the same size', async () => {
    // The confirmation is keyed on selection IDENTITY, not COUNT. Swapping
    // 7,8 -> 7,9 (same size) must still drop an armed confirmation, so Apply
    // cannot close a PR the user never confirmed.
    const PULL_9 = { ...PULL, number: 9 }
    setCtx({ checkedPulls: new Set([7, 8]), sortedPulls: [PULL, PULL_8, PULL_9] })
    const { rerender } = wrap(<PrBulkBar />)
    await userEvent.click(screen.getByRole('button', { name: /^close$/i }))
    await userEvent.type(screen.getByRole('textbox'), BULK_PR_CLOSE_TOKEN)

    // Same size, different members.
    setCtx({ checkedPulls: new Set([7, 9]), sortedPulls: [PULL, PULL_8, PULL_9] })
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    rerender(<QueryClientProvider client={qc}><PrBulkBar /></QueryClientProvider>)

    // Back to the action row: the armed confirmation did not survive.
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /^close$/i })).toBeTruthy())
    expect(screen.queryByRole('textbox')).toBeNull()
    expect(api.bulkPrAction).not.toHaveBeenCalled()
  })

  it('never acts on a PR the active filter is hiding', async () => {
    // The selection is intersected with the RENDERED rows. A tick left over from
    // before a search/filter narrowed the list must not be acted on — the checkbox
    // is offered under "you can only mass-act on what you can see".
    setCtx({ checkedPulls: new Set([7, 8]), sortedPulls: [PULL] })
    wrap(<PrBulkBar />)
    await userEvent.click(screen.getByRole('button', { name: /^approve$/i }))
    await waitFor(() =>
      expect(api.bulkPrAction).toHaveBeenCalledWith(REF, [7], 'approve', {
        body: undefined, headShas: { 7: 'abc1234' },
      }))
  })

  it('still reports the outcome after a clean run clears the selection', async () => {
    // A clean run clears the selection; the bar survives on an outcome so the
    // summary still paints. Otherwise it would unmount before the summary
    // rendered, leaving no confirmation and the translated success copy
    // unreachable.
    api.bulkPrAction.mockResolvedValue({ ...REF, action: 'approve', applied: [{ number: 7 }], failed: [] })
    setCtx({ checkedPulls: new Set([7]), clearCheckedPulls: vi.fn() })
    const { rerender } = wrap(<PrBulkBar />)
    await userEvent.click(screen.getByRole('button', { name: /^approve$/i }))

    // Simulate the context clearing the selection, as the real one does.
    setCtx({ checkedPulls: new Set(), clearCheckedPulls: vi.fn() })
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    rerender(<QueryClientProvider client={qc}><PrBulkBar /></QueryClientProvider>)

    await waitFor(() => expect(screen.getByText(/applied to 1/i)).toBeTruthy())
    // …and with nothing selected it offers no actions: a button over zero rows is dead.
    expect(screen.queryByRole('button', { name: /^approve$/i })).toBeNull()
  })
})

describe('bulk selection is scoped to the full repo identity', () => {
  it('clears on scopeKey, not on owner/repo alone', async () => {
    // `acme/widget` exists on GitHub AND on every GitLab instance, so the slug alone
    // does not identify a repo. Keyed on owner/repo, switching between two same-slug
    // repos left the ticks in place and pointed an armed bulk action at unrelated
    // items. Asserted on the source because the effect lives in the context provider,
    // which these tests mock out.
    const src = await import('node:fs').then((fs) =>
      fs.readFileSync('src/apps/issue-radar/context.tsx', 'utf8'))
    const effect = src.slice(src.indexOf('useEffect(() => { setCheckedPulls(new Set()) }'))
      .slice(0, 400)
    expect(effect).toContain('scopeKey')
    // The bare pair must not be the key: it is what let the selection survive.
    expect(effect).not.toMatch(/\[\s*owner,\s*repo\s*,/)
  })
})

describe('PrRunActions', () => {
  it('fetches nothing without a head sha', () => {
    wrap(<PrRunActions repoRef={REF} number={7} headSha={null} canWrite live />)
    expect(api.pullRuns).not.toHaveBeenCalled()
  })

  it('renders nothing on a read-only repo', async () => {
    const { container } = wrap(
      <PrRunActions repoRef={REF} number={7} headSha="abc1234" canWrite={false} live />,
    )
    await waitFor(() => expect(container.textContent).toBe(''))
  })

  it('offers only the actions the server marked possible', async () => {
    api.pullRuns.mockResolvedValue({
      ...REF, number: 7,
      runs: [
        { id: 1, name: 'CI', status: 'in_progress', conclusion: null, url: null, event: null, created_at: null, cancellable: true, rerunnable: false },
        { id: 2, name: 'Build', status: 'completed', conclusion: 'failure', url: null, event: null, created_at: null, cancellable: false, rerunnable: true },
      ],
    })
    wrap(<PrRunActions repoRef={REF} number={7} headSha="abc1234" canWrite live />)

    // The in-flight run can only be cancelled; the finished one only re-run.
    await waitFor(() => expect(screen.getByRole('button', { name: /cancel the CI run/i })).toBeTruthy())
    expect(screen.queryByRole('button', { name: /re-run.*\bCI\b/i })).toBeNull()
    expect(screen.getByRole('button', { name: /re-run Build/i })).toBeTruthy()
    expect(screen.queryByRole('button', { name: /cancel the Build run/i })).toBeNull()
  })

  it('re-runs only the failed jobs of a failed run', async () => {
    api.pullRuns.mockResolvedValue({
      ...REF, number: 7,
      runs: [{ id: 2, name: 'Build', status: 'completed', conclusion: 'failure', url: null, event: null, created_at: null, cancellable: false, rerunnable: true }],
    })
    wrap(<PrRunActions repoRef={REF} number={7} headSha="abc1234" canWrite live />)
    await userEvent.click(await screen.findByRole('button', { name: /re-run Build/i }))
    await waitFor(() =>
      expect(api.pullRunAction).toHaveBeenCalledWith(REF, 7, 2, 'rerun', true))
  })

  it('re-runs everything for a run that did not fail', async () => {
    api.pullRuns.mockResolvedValue({
      ...REF, number: 7,
      runs: [{ id: 3, name: 'Nightly', status: 'completed', conclusion: 'success', url: null, event: null, created_at: null, cancellable: false, rerunnable: true }],
    })
    wrap(<PrRunActions repoRef={REF} number={7} headSha="abc1234" canWrite live />)
    await userEvent.click(await screen.findByRole('button', { name: /re-run Nightly/i }))
    await waitFor(() =>
      expect(api.pullRunAction).toHaveBeenCalledWith(REF, 7, 3, 'rerun', false))
  })
})
