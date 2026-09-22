import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

/* ── Mocks: must run before importing the component ── */
const mockApi = vi.hoisted(() => ({
  skills: vi.fn(),
  skill: vi.fn(),
  skillTree: vi.fn(),
  skillFile: vi.fn(),
  createSkill: vi.fn(),
  updateSkill: vi.fn(),
  deleteSkill: vi.fn(),
  skillsPending: vi.fn(),
  skillPendingDetail: vi.fn(),
  approvePendingSkill: vi.fn(),
  dismissPendingSkill: vi.fn(),
  dismissAllPendingSkills: vi.fn(),
}))
vi.mock('../api/client', () => ({ api: mockApi }))

vi.mock('../providers', () => ({
  useProvider: () => ({ labels: { pluginRegistryName: 'Packages' } }),
}))

vi.mock('../components/MarkdownRenderer', () => ({
  default: ({ content }: { content: string }) => <div data-testid="md">{content}</div>,
}))

vi.mock('../components/SkillDirectoryBrowser', () => ({
  default: () => <div data-testid="dir-browser">browser</div>,
}))

// DiffBlock is exercised by its own tests; here we only assert SkillsTab feeds
// it the server-computed unified diff.
vi.mock('../components/DiffBlock', () => ({
  default: ({ code }: { code: string }) => <pre data-testid="diff">{code}</pre>,
}))

import SkillsTab from '../pages/overview/SkillsTab'

function renderWithQuery(qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })) {
  // MemoryRouter: the pending-review panel reads (and clears) the `?review=<slug>`
  // deep link a skill notification points at, so the tab needs a router.
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter><SkillsTab /></MemoryRouter>
    </QueryClientProvider>,
  )
}

const UPDATE_ROW = {
  slug: 'deploy-helper-update',
  name: 'auto/deploy-helper-update',
  description: 'handles the new retry flag',
  has_scripts: false,
  kind: 'update',
  target: 'auto/deploy-helper',
  base_version: 2,
}

const NEW_ROW = {
  slug: 'fresh-skill',
  name: 'auto/fresh-skill',
  description: 'brand new procedure',
  has_scripts: false,
  kind: 'new',
  target: null,
  base_version: null,
}

const DIFF = '--- live\n+++ proposed\n@@ -1,2 +1,2 @@\n-old step\n+new step\n'

/**
 * The failed-action notice's bold lead must name the refused VERB as a complete
 * sentence (so the server's sentence after it reads as a separate clause), and
 * must not claim an outcome. The server's refusal is one 409 covering "not
 * found", "a live skill already exists" and "script validation failed", only some
 * of which leave the candidate pending, so the client can say what was asked and
 * that it was refused -- never whether the skill was installed, discarded, or is
 * gone. (The server's own sentence is untouched; this reads the lead alone.)
 */
function expectFailedActionLead(notice: HTMLElement, lead: string) {
  const strong = notice.querySelector('strong')
  expect(strong).not.toBeNull()
  expect(strong!.textContent!.trim()).toBe(lead)
  // Terminal punctuation is the separator from the server's sentence. A colon
  // is refused by the catalog gate as a sentence fragment, so a full stop it is.
  expect(lead.endsWith('.')).toBe(true)
  expect(strong!.textContent).not.toMatch(/\b(approved|dismissed|discarded|installed|removed|gone|live)\b/i)
}

beforeEach(() => {
  Object.values(mockApi).forEach(m => 'mockReset' in m && m.mockReset())
  mockApi.skills.mockResolvedValue([])
  mockApi.skill.mockResolvedValue({ name: 'x', content: '---\nname: x\n---\nbody' })
  mockApi.skillsPending.mockResolvedValue({ pending: [] })
})

describe('SkillsTab pending updates', () => {
  it('marks an update candidate with an Update badge and names its target', async () => {
    mockApi.skillsPending.mockResolvedValue({ pending: [UPDATE_ROW] })
    renderWithQuery()
    expect(await screen.findByText('Update')).toBeTruthy()
    expect(
      screen.getByText(/Adds new requirements to auto\/deploy-helper/),
    ).toBeTruthy()
  })

  it('shows the server-computed diff with the version transition on Review', async () => {
    mockApi.skillsPending.mockResolvedValue({ pending: [UPDATE_ROW] })
    mockApi.skillPendingDetail.mockResolvedValue({
      name: 'auto/deploy-helper-update',
      content: '## Steps\nnew\n',
      scripts: [],
      diff: DIFF,
      live_body: 'old',
      proposed_body: 'new',
      from_version: 2,
      to_version: 3,
      stale_base: false,
    })
    renderWithQuery()
    fireEvent.click(await screen.findByText('Review'))
    await waitFor(() => expect(screen.getByTestId('diff')).toBeTruthy())
    expect(screen.getByTestId('diff').textContent).toContain('+new step')
    expect(screen.getByText(/v2 → v3/)).toBeTruthy()
  })

  it('blocks approval when the live skill advanced past the update base version', async () => {
    mockApi.skillsPending.mockResolvedValue({ pending: [UPDATE_ROW] })
    mockApi.skillPendingDetail.mockResolvedValue({
      name: 'auto/deploy-helper-update',
      content: '',
      scripts: [],
      diff: DIFF,
      from_version: 5,
      to_version: 6,
      stale_base: true,
    })
    renderWithQuery()
    fireEvent.click(await screen.findByText('Review'))
    expect(
      await screen.findByText(/would undo those newer changes/),
    ).toBeTruthy()
    // The backend refuses a stale approval, so the button must not invite it.
    expect(screen.getByText('Approve').closest('button')!.disabled).toBe(true)
  })

  it('tells the user to dismiss an update whose target is gone', async () => {
    mockApi.skillsPending.mockResolvedValue({ pending: [UPDATE_ROW] })
    mockApi.skillPendingDetail.mockResolvedValue({
      name: 'auto/deploy-helper-update',
      content: '## Steps\nnew\n',
      scripts: [],
      diff: null,
      live_body: null,
      stale_base: false,
    })
    renderWithQuery()
    fireEvent.click(await screen.findByText('Review'))
    expect(
      await screen.findByText(/no longer exists, so there is nothing/),
    ).toBeTruthy()
    expect(screen.queryByTestId('diff')).toBeNull()
    // Approving an orphaned update would 409 — the button must stay disabled.
    expect(screen.getByText('Approve').closest('button')!.disabled).toBe(true)
  })

  it('still renders a plain new candidate as raw SKILL.md, with no badge', async () => {
    mockApi.skillsPending.mockResolvedValue({ pending: [NEW_ROW] })
    mockApi.skillPendingDetail.mockResolvedValue({
      name: 'auto/fresh-skill',
      content: '## Steps\nrun it\n',
      scripts: [],
    })
    renderWithQuery()
    expect(screen.queryByText('Update')).toBeNull()
    fireEvent.click(await screen.findByText('Review'))
    await waitFor(() => expect(screen.getByText(/run it/)).toBeTruthy())
    expect(screen.queryByTestId('diff')).toBeNull()
  })

  it('approves an update through the same approve endpoint', async () => {
    mockApi.skillsPending.mockResolvedValue({ pending: [UPDATE_ROW] })
    mockApi.skillPendingDetail.mockResolvedValue({
      name: 'auto/deploy-helper-update',
      content: '',
      scripts: [],
      diff: DIFF,
      from_version: 2,
      to_version: 3,
      stale_base: false,
    })
    mockApi.approvePendingSkill.mockResolvedValue({ ok: true })
    renderWithQuery()
    fireEvent.click(await screen.findByText('Review'))
    await waitFor(() => expect(screen.getByTestId('diff')).toBeTruthy())
    fireEvent.click(screen.getByText('Approve'))
    await waitFor(() =>
      expect(mockApi.approvePendingSkill).toHaveBeenCalledWith('deploy-helper-update'),
    )
  })

  // ── Approve is gated on review by SHAPE, not by a disabled state ──
  // A collapsed row used to render a greyed-out Approve beside Review with
  // nothing explaining it, so a queue of candidates looked like a wall of broken
  // buttons. The rule (you cannot approve what you have not opened) is now
  // carried by Approve not existing until the review panel does.
  it('offers no Approve button on a collapsed candidate', async () => {
    mockApi.skillsPending.mockResolvedValue({ pending: [NEW_ROW] })
    renderWithQuery()
    expect(await screen.findByText('Review')).toBeTruthy()
    expect(screen.queryByText('Approve')).toBeNull()
  })

  it('reveals Approve only once the candidate body is on screen', async () => {
    mockApi.skillsPending.mockResolvedValue({ pending: [NEW_ROW] })
    mockApi.skillPendingDetail.mockResolvedValue({
      name: 'auto/fresh-skill',
      content: '## Steps\nrun it\n',
      scripts: [],
    })
    mockApi.approvePendingSkill.mockResolvedValue({ ok: true })
    renderWithQuery()
    fireEvent.click(await screen.findByText('Review'))
    // The body must already be rendered when Approve appears -- an Approve that
    // showed up while the detail request was still in flight would be the same
    // approve-what-you-cannot-see problem in a new place.
    const approve = await screen.findByText('Approve')
    expect(screen.getByText(/run it/)).toBeTruthy()
    expect(approve.closest('button')!.disabled).toBe(false)
    fireEvent.click(approve)
    await waitFor(() =>
      expect(mockApi.approvePendingSkill).toHaveBeenCalledWith('fresh-skill'),
    )
  })

  it('hides Approve again when the row is collapsed', async () => {
    mockApi.skillsPending.mockResolvedValue({ pending: [NEW_ROW] })
    mockApi.skillPendingDetail.mockResolvedValue({
      name: 'auto/fresh-skill',
      content: '## Steps\nrun it\n',
      scripts: [],
    })
    renderWithQuery()
    fireEvent.click(await screen.findByText('Review'))
    await screen.findByText('Approve')
    fireEvent.click(screen.getByText('Hide'))
    await waitFor(() => expect(screen.queryByText('Approve')).toBeNull())
  })

  // ── The refusal reason sits WITH the button it disables ──
  // A stale update renders a full diff. With the notice at the top of the panel
  // and Approve at the bottom, the reason has scrolled out of view by the time
  // the user reaches the disabled button -- the same unexplained-disabled shape
  // this row was changed to remove.
  it('renders the stale-base refusal in the same row as the disabled Approve', async () => {
    mockApi.skillsPending.mockResolvedValue({ pending: [UPDATE_ROW] })
    mockApi.skillPendingDetail.mockResolvedValue({
      name: 'auto/deploy-helper-update',
      content: '',
      scripts: [],
      diff: DIFF,
      from_version: 5,
      to_version: 6,
      stale_base: true,
    })
    renderWithQuery()
    fireEvent.click(await screen.findByText('Review'))
    const notice = await screen.findByText(/would undo those newer changes/)
    const approve = screen.getByText('Approve').closest('button')!
    expect(approve.disabled).toBe(true)
    // The SAME parent element, not merely a shared ancestor: an ancestor check
    // passes even with the notice back at the top of the panel and the whole
    // diff between the two, which is the arrangement this pins against.
    expect(notice.parentElement).toBe(approve.parentElement)
  })

  it('renders the gone-target refusal in the same row as the disabled Approve', async () => {
    mockApi.skillsPending.mockResolvedValue({ pending: [UPDATE_ROW] })
    mockApi.skillPendingDetail.mockResolvedValue({
      name: 'auto/deploy-helper-update',
      content: '',
      scripts: [],
      diff: null,
      stale_base: false,
    })
    renderWithQuery()
    fireEvent.click(await screen.findByText('Review'))
    const notice = await screen.findByText(/no longer exists, so there is nothing/)
    const approve = screen.getByText('Approve').closest('button')!
    expect(approve.disabled).toBe(true)
    expect(notice.parentElement).toBe(approve.parentElement)
  })

  // ── A refused action must say so (AUTOSDE errors-use-error-notice) ──
  // The client disables Approve for the refusals it can see, but the backend is
  // the authority and refuses on its own. Before this, the click did nothing
  // visible and the candidate read as ignored.
  //
  // The failure is attributed to the ATTEMPT, naming the slug it was for, and
  // never to a row: this panel outlives its candidates, so an error owned by a
  // row has to be re-judged every time the queue moves, and that judgement is
  // what got attribution wrong twice.
  it('surfaces an approve failure naming the candidate it was for', async () => {
    mockApi.skillsPending.mockResolvedValue({ pending: [NEW_ROW] })
    mockApi.skillPendingDetail.mockResolvedValue({
      name: 'auto/fresh-skill',
      content: '## Steps\nrun it\n',
      scripts: [],
    })
    mockApi.approvePendingSkill.mockRejectedValue(new Error('candidate is no longer pending'))
    renderWithQuery()
    fireEvent.click(await screen.findByText('Review'))
    fireEvent.click(await screen.findByText('Approve'))
    const notice = await screen.findByTestId('pending-action-error')
    expect(notice.textContent).toContain('candidate is no longer pending')
    // Which ACTION was refused, and which candidate it was for, as the bold lead.
    // A bare slug over the server's sentence ("fresh-skill this candidate is no
    // longer pending") left a blind reader unable to tell whether their approval
    // had worked or failed. The verb is the one thing about the outcome the
    // client knows for certain: it rendered the button that was pressed.
    expectFailedActionLead(notice, 'Couldn\'t approve \u201Cfresh-skill\u201D.')
    // …and the server's sentence reads as its own clause after the separator.
    expect(notice.textContent).toContain('Couldn\'t approve \u201Cfresh-skill\u201D. candidate is no longer pending')
    // The hand-off is the point of ErrorNotice on a surface with no draft state.
    expect(screen.getByRole('button', { name: /ask the agent/i })).toBeTruthy()
  })

  it('surfaces a dismiss failure too, on the one attempt surface', async () => {
    mockApi.skillsPending.mockResolvedValue({ pending: [NEW_ROW] })
    mockApi.dismissPendingSkill.mockRejectedValue(new Error('dismiss refused'))
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderWithQuery()
    fireEvent.click(await screen.findByText('Dismiss'))
    const notice = await screen.findByTestId('pending-action-error')
    expect(notice.textContent).toContain('dismiss refused')
    // The lead names THIS verb: a dismiss refusal captioned "Couldn't approve"
    // would be a lie about which button the user pressed.
    expectFailedActionLead(notice, 'Couldn\'t dismiss \u201Cfresh-skill\u201D.')
  })

  it('surfaces a dismiss-all failure', async () => {
    mockApi.skillsPending.mockResolvedValue({ pending: [NEW_ROW] })
    mockApi.dismissAllPendingSkills.mockRejectedValue(new Error('bulk dismiss refused'))
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderWithQuery()
    fireEvent.click(await screen.findByText('Dismiss All'))
    const notice = await screen.findByTestId('pending-action-error')
    expect(notice.textContent).toContain('bulk dismiss refused')
    // No slug to name -- the bulk action has none -- so the lead names the
    // action itself rather than rendering the server's sentence untitled.
    expectFailedActionLead(notice, 'Couldn\'t dismiss all pending skill candidates.')
  })

  it('replaces the previous failure when the attempt is retried', async () => {
    mockApi.skillsPending.mockResolvedValue({ pending: [NEW_ROW] })
    mockApi.skillPendingDetail.mockResolvedValue({
      name: 'auto/fresh-skill',
      content: '## Steps\nrun it\n',
      scripts: [],
    })
    mockApi.approvePendingSkill.mockRejectedValueOnce(new Error('transient refusal'))
    mockApi.approvePendingSkill.mockResolvedValue({ ok: true })
    renderWithQuery()
    fireEvent.click(await screen.findByText('Review'))
    fireEvent.click(await screen.findByText('Approve'))
    const notice = await screen.findByTestId('pending-action-error')
    // The retry goes through the dismiss now: while the refusal is displayed it
    // holds its own row's Approve, so that the button and the sentence beside it
    // never disagree. Dismissing is the acknowledgement that frees the retry.
    const controls = within(notice).getAllByRole('button')
    fireEvent.click(controls[controls.length - 1])
    await waitFor(() =>
      expect((screen.getByText('Approve').closest('button') as HTMLButtonElement).disabled).toBe(false),
    )
    fireEvent.click(screen.getByText('Approve'))
    // A retry must not sit under its own previous refusal.
    await waitFor(() =>
      expect(screen.queryByTestId('pending-action-error')).toBeNull(),
    )
  })

  it('never attributes a failure to a candidate row', async () => {
    // The regression guard for the removed mechanism: a failure that a row owns
    // has to be re-judged whenever the queue changes under it, and the two
    // findings that mechanism drew were both mis-attribution. No row-scoped
    // error surface may come back.
    mockApi.skillsPending.mockResolvedValue({
      pending: [NEW_ROW, { ...NEW_ROW, slug: 'other-skill', name: 'auto/other-skill' }],
    })
    mockApi.dismissPendingSkill.mockRejectedValue(new Error('dismiss refused'))
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    const { container } = renderWithQuery()
    fireEvent.click((await screen.findAllByText('Dismiss'))[0])
    await screen.findByTestId('pending-action-error')
    expect(container.querySelector('[data-testid^="pending-action-error-"]')).toBeNull()
    // Exactly one surface carries it, so a second row cannot echo it.
    expect(screen.getAllByText('dismiss refused')).toHaveLength(1)
  })

  // ── One action at a time, so no failure can be detached and lost ──
  // Each mutation hook renders only its latest call. A second attempt started
  // before the first settles detaches the first, and that request can then fail
  // with nothing showing the failure -- silent refusal again, in the concurrent
  // case. The controls serialise instead of tracking the overlap.
  it('locks every queue action while one is in flight, and shows which row owns it', async () => {
    let settleDismiss!: () => void
    mockApi.skillsPending.mockResolvedValue({
      pending: [NEW_ROW, { ...NEW_ROW, slug: 'other-skill', name: 'auto/other-skill' }],
    })
    mockApi.dismissPendingSkill.mockReturnValue(new Promise(res => { settleDismiss = () => res({ ok: true }) }))
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderWithQuery()
    fireEvent.click((await screen.findAllByText('Dismiss'))[0])

    await waitFor(() =>
      expect((screen.getAllByText('Dismiss')[1].closest('button') as HTMLButtonElement).disabled).toBe(true),
    )
    // Including the row that started it, and the bulk control.
    expect((screen.getAllByText('Dismiss')[0].closest('button') as HTMLButtonElement).disabled).toBe(true)
    expect((screen.getByText('Dismiss All').closest('button') as HTMLButtonElement).disabled).toBe(true)
    // The lock is visible on the row whose action it is, not merely felt.
    // Addressed by test id rather than by CSS class, so restyling the spinner
    // does not fail a test about the affordance existing.
    expect(screen.getByTestId('pending-action-spinner')).toBeTruthy()

    // A second attempt cannot be started while the first is unsettled.
    fireEvent.click(screen.getAllByText('Dismiss')[1])
    expect(mockApi.dismissPendingSkill).toHaveBeenCalledTimes(1)

    settleDismiss()
    // And the lock lifts, so the retry path stays reachable.
    await waitFor(() =>
      expect((screen.getAllByText('Dismiss')[0].closest('button') as HTMLButtonElement).disabled).toBe(false),
    )
  })

  it('sends ONE request for a double-clicked Approve', async () => {
    // `busy` is derived from render state, so it cannot see a second activation
    // delivered in the same task -- and a double-click is exactly that. Measured
    // before the synchronous latch existed: three clicks, three requests. Two
    // requests for one candidate means the loser is refused, and that refusal can
    // be the only thing shown for an approval that in fact succeeded.
    mockApi.skillsPending.mockResolvedValue({ pending: [NEW_ROW] })
    mockApi.skillPendingDetail.mockResolvedValue({
      name: 'auto/fresh-skill',
      content: '## Steps\nrun it\n',
      scripts: [],
    })
    mockApi.approvePendingSkill.mockReturnValue(new Promise(() => {}))
    renderWithQuery()
    fireEvent.click(await screen.findByText('Review'))
    const approve = await screen.findByText('Approve')
    fireEvent.click(approve)
    fireEvent.click(approve)
    fireEvent.click(approve)
    await waitFor(() => expect(mockApi.approvePendingSkill).toHaveBeenCalled())
    expect(mockApi.approvePendingSkill).toHaveBeenCalledTimes(1)
  })

  it('sends ONE request when two different rows are actioned in the same tick', async () => {
    mockApi.skillsPending.mockResolvedValue({
      pending: [NEW_ROW, { ...NEW_ROW, slug: 'other-skill', name: 'auto/other-skill' }],
    })
    mockApi.dismissPendingSkill.mockReturnValue(new Promise(() => {}))
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderWithQuery()
    const buttons = await screen.findAllByText('Dismiss')
    // No await between them: both handlers run before any re-render.
    fireEvent.click(buttons[0])
    fireEvent.click(buttons[1])
    await waitFor(() => expect(mockApi.dismissPendingSkill).toHaveBeenCalled())
    expect(mockApi.dismissPendingSkill).toHaveBeenCalledTimes(1)
  })

  it('still locks the queue after the tab is closed and reopened mid-request', async () => {
    // The lock has to outlive this component. Held in component state it did
    // not: switching Capabilities tabs unmounts the tab, and coming back handed
    // the queue a fresh, unlocked guard while the first request was still in
    // flight. Both halves now read the shared mutation cache, so the reopened
    // tab knows an action is still running -- the controls come back disabled and
    // a click sends nothing.
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    mockApi.skillsPending.mockResolvedValue({ pending: [NEW_ROW] })
    mockApi.dismissPendingSkill.mockReturnValue(new Promise(() => {}))
    vi.spyOn(window, 'confirm').mockReturnValue(true)

    const first = renderWithQuery(qc)
    fireEvent.click(await screen.findByText('Dismiss'))
    await waitFor(() => expect(mockApi.dismissPendingSkill).toHaveBeenCalledTimes(1))

    // Leave the tab while the request is unsettled, then come back to it.
    first.unmount()
    renderWithQuery(qc)
    const dismiss = await screen.findByText('Dismiss')
    await waitFor(() =>
      expect((dismiss.closest('button') as HTMLButtonElement).disabled).toBe(true),
    )
    fireEvent.click(dismiss)
    expect(mockApi.dismissPendingSkill).toHaveBeenCalledTimes(1)
  })

  it('keeps the panel alive to report a failure that lands after the queue empties', async () => {
    // Nothing renders a message for a surface that has already returned null.
    // The last candidate's action can settle after the queue has gone empty --
    // another client resolved it, and the poll came back with nothing.
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      let rejectApprove!: (e: Error) => void
      mockApi.skillsPending.mockResolvedValueOnce({ pending: [NEW_ROW] })
      mockApi.skillPendingDetail.mockResolvedValue({
        name: 'auto/fresh-skill',
        content: '## Steps\nrun it\n',
        scripts: [],
      })
      mockApi.approvePendingSkill.mockReturnValue(new Promise((_res, rej) => { rejectApprove = rej }))
      renderWithQuery()
      fireEvent.click(await screen.findByText('Review'))
      fireEvent.click(await screen.findByText('Approve'))
      await waitFor(() => expect(mockApi.approvePendingSkill).toHaveBeenCalled())

      // The queue empties underneath the in-flight attempt.
      mockApi.skillsPending.mockResolvedValue({ pending: [] })
      await vi.advanceTimersByTimeAsync(31_000)
      await waitFor(() => expect(screen.queryByText('auto/fresh-skill')).toBeNull())

      rejectApprove(new Error('candidate vanished mid-approval'))
      // The message still has somewhere to appear.
      const notice = await screen.findByTestId('pending-action-error')
      expect(notice.textContent).toContain('candidate vanished mid-approval')
    } finally {
      vi.useRealTimers()
    }
  })

  it('still reports a failure that arrived while the tab was closed', async () => {
    // A hook's error dies with the component. Leave the Skills tab while an
    // approve is in flight, and the rejection that lands meanwhile used to be
    // discarded: on return the user was told nothing about an action they
    // started. The mutation cache outlives the mount, so the message is still
    // there when they come back.
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    let rejectApprove!: (e: Error) => void
    mockApi.skillsPending.mockResolvedValue({ pending: [NEW_ROW] })
    mockApi.skillPendingDetail.mockResolvedValue({
      name: 'auto/fresh-skill',
      content: '## Steps\nrun it\n',
      scripts: [],
    })
    mockApi.approvePendingSkill.mockReturnValue(new Promise((_res, rej) => { rejectApprove = rej }))

    const first = renderWithQuery(qc)
    fireEvent.click(await screen.findByText('Review'))
    fireEvent.click(await screen.findByText('Approve'))
    await waitFor(() => expect(mockApi.approvePendingSkill).toHaveBeenCalled())

    // The user leaves the tab, and only then does the request fail.
    first.unmount()
    rejectApprove(new Error('rejected while you were away'))

    renderWithQuery(qc)
    const notice = await screen.findByTestId('pending-action-error')
    expect(notice.textContent).toContain('rejected while you were away')
  })

  it('reconciles the queue against the server when an action is refused', async () => {
    // Otherwise the refusal contradicts the screen: "no longer pending" renders
    // above the very row it names, still carrying a live Approve and the old
    // count, until the 30s poll happens to catch up. A reader shown that does not
    // press the button and cannot tell which half is lying.
    mockApi.skillsPending.mockResolvedValueOnce({ pending: [NEW_ROW] })
    mockApi.approvePendingSkill.mockRejectedValue(new Error('this candidate is no longer pending'))
    mockApi.skillPendingDetail.mockResolvedValue({
      name: 'auto/fresh-skill',
      content: '## Steps\nrun it\n',
      scripts: [],
    })
    // The server's view: it is already gone.
    mockApi.skillsPending.mockResolvedValue({ pending: [] })
    renderWithQuery()
    fireEvent.click(await screen.findByText('Review'))
    fireEvent.click(await screen.findByText('Approve'))

    // The notice arrives AND the stale row goes, so the two cannot contradict.
    const notice = await screen.findByTestId('pending-action-error')
    expect(notice.textContent).toContain('no longer pending')
    await waitFor(() => expect(screen.queryByText('auto/fresh-skill')).toBeNull())
    expect(screen.queryByText('Approve')).toBeNull()
  })

  it('never shows a successor candidate its predecessor\'s reviewed body', async () => {
    // A slug is reusable once its candidate is resolved. Keyed by slug alone, the
    // successor inherited the row instance (so it rendered already expanded) AND
    // the cached detail -- so the panel showed the OLD body while Approve would
    // promote the NEW candidate, bundled scripts and all. That is approving
    // something nobody reviewed, which is the defect this surface exists to stop.
    const FIRST = { ...NEW_ROW, slug: 'recycled', name: 'auto/recycled', created_at: '2026-09-01T00:00:00Z' }
    const SECOND = { ...FIRST, created_at: '2026-09-14T00:00:00Z', description: 'a different procedure entirely' }
    mockApi.skillsPending.mockResolvedValueOnce({ pending: [FIRST] })
    mockApi.skillPendingDetail.mockResolvedValueOnce({
      name: 'auto/recycled',
      content: 'BODY OF THE FIRST CANDIDATE',
      scripts: [],
    })
    mockApi.approvePendingSkill.mockRejectedValue(new Error('this candidate is no longer pending'))
    // The failure reconciles the queue, and the server now answers with the
    // SUCCESSOR staged under the same slug.
    mockApi.skillsPending.mockResolvedValue({ pending: [SECOND] })
    mockApi.skillPendingDetail.mockResolvedValue({
      name: 'auto/recycled',
      content: 'BODY OF THE SECOND CANDIDATE',
      scripts: [{ filename: 'run.sh', content: 'echo unreviewed' }],
    })

    renderWithQuery()
    fireEvent.click(await screen.findByText('Review'))
    await screen.findByText(/BODY OF THE FIRST CANDIDATE/)
    fireEvent.click(screen.getByText('Approve'))

    // The successor arrives. It must come back COLLAPSED, carrying no body and
    // no Approve, so its content has to be opened and read on its own terms.
    await screen.findByText(/a different procedure entirely/)
    await waitFor(() => expect(screen.queryByText(/BODY OF THE FIRST CANDIDATE/)).toBeNull())
    expect(screen.queryByText('Approve')).toBeNull()
    expect(screen.getByText('Review')).toBeTruthy()
  })

  it('does not flash the predecessor\'s body when the successor is opened', async () => {
    // The other half of generation scoping, and the half a collapsed remount does
    // NOT cover: opening the successor hits the detail cache, and keyed by slug
    // alone react-query serves the PREDECESSOR's body synchronously while it
    // refetches -- a window in which the panel shows one candidate's content with
    // Approve live for another's.
    const FIRST = { ...NEW_ROW, slug: 'recycled', name: 'auto/recycled', created_at: '2026-09-01T00:00:00Z' }
    const SECOND = { ...FIRST, created_at: '2026-09-14T00:00:00Z', description: 'a different procedure entirely' }
    mockApi.skillsPending.mockResolvedValueOnce({ pending: [FIRST] })
    mockApi.skillPendingDetail.mockResolvedValueOnce({
      name: 'auto/recycled',
      content: 'BODY OF THE FIRST CANDIDATE',
      scripts: [],
    })
    mockApi.approvePendingSkill.mockRejectedValue(new Error('this candidate is no longer pending'))
    mockApi.skillsPending.mockResolvedValue({ pending: [SECOND] })
    // The successor's detail never settles, so anything rendered in the panel can
    // only have come from the cache.
    mockApi.skillPendingDetail.mockReturnValue(new Promise(() => {}))

    renderWithQuery()
    fireEvent.click(await screen.findByText('Review'))
    await screen.findByText(/BODY OF THE FIRST CANDIDATE/)
    fireEvent.click(screen.getByText('Approve'))
    await screen.findByText(/a different procedure entirely/)

    // Open the successor: nothing of its predecessor may appear, and Approve must
    // not be offered over content that is not the successor's.
    fireEvent.click(screen.getByText('Review'))
    expect(screen.queryByText(/BODY OF THE FIRST CANDIDATE/)).toBeNull()
    expect(screen.queryByText('Approve')).toBeNull()
  })

  it('offers no Approve on a refused row while the queue is still reconciling', async () => {
    // The window a UX reader was shown and refused to act in: the banner says the
    // candidate is no longer pending while its row still carries a live purple
    // Approve, because the reconcile refetch has not landed yet.
    let resolveRefetch!: (v: { pending: typeof NEW_ROW[] }) => void
    mockApi.skillsPending.mockResolvedValueOnce({ pending: [NEW_ROW] })
    mockApi.skillPendingDetail.mockResolvedValue({
      name: 'auto/fresh-skill',
      content: '## Steps\nrun it\n',
      scripts: [],
    })
    mockApi.approvePendingSkill.mockRejectedValue(new Error('this candidate is no longer pending'))
    // Hold the reconcile open so the window can be observed.
    mockApi.skillsPending.mockReturnValue(new Promise(r => { resolveRefetch = r }))
    renderWithQuery()
    fireEvent.click(await screen.findByText('Review'))
    fireEvent.click(await screen.findByText('Approve'))
    await screen.findByTestId('pending-action-error')
    await waitFor(() =>
      expect((screen.getByText('Approve').closest('button') as HTMLButtonElement).disabled).toBe(true),
    )

    // The row survives the reconcile (the failure was transient), so the retry
    // path must still be reachable -- but not while the sentence contradicting it
    // is on screen. The explanation holds the button; dismissing it hands the
    // button back. Before this, the reconcile alone re-enabled Approve underneath
    // "no longer pending", which is the state a blind reader would not touch.
    resolveRefetch({ pending: [NEW_ROW] })
    await waitFor(() => expect(mockApi.skillsPending).toHaveBeenCalledTimes(2))
    expect((screen.getByText('Approve').closest('button') as HTMLButtonElement).disabled).toBe(true)
    const notice = screen.getByTestId('pending-action-error')
    const controls = within(notice).getAllByRole('button')
    fireEvent.click(controls[controls.length - 1])
    await waitFor(() =>
      expect((screen.getByText('Approve').closest('button') as HTMLButtonElement).disabled).toBe(false),
    )
  })

  it('keeps the refusal on screen when the reconcile confirms the row is gone', async () => {
    // The mirror of the case above, and the reason the message is not simply
    // cleared on any successful read: when the row really has left, the sentence
    // is the only account of where it went, so the panel stays mounted to hold it.
    let resolveRefetch!: (v: { pending: typeof NEW_ROW[] }) => void
    mockApi.skillsPending.mockResolvedValueOnce({ pending: [NEW_ROW] })
    mockApi.skillPendingDetail.mockResolvedValue({
      name: 'auto/fresh-skill',
      content: '## Steps\nrun it\n',
      scripts: [],
    })
    mockApi.approvePendingSkill.mockRejectedValue(new Error('this candidate is no longer pending'))
    mockApi.skillsPending.mockReturnValue(new Promise(r => { resolveRefetch = r }))
    renderWithQuery()
    fireEvent.click(await screen.findByText('Review'))
    fireEvent.click(await screen.findByText('Approve'))
    await screen.findByTestId('pending-action-error')

    resolveRefetch({ pending: [] })
    // The row goes; the explanation stays.
    await waitFor(() => expect(screen.queryByText('Review')).toBeNull())
    expect((await screen.findByTestId('pending-action-error')).textContent).toContain(
      'this candidate is no longer pending',
    )
  })

  it('explains a candidate whose body cannot be read', async () => {
    // Opening a row whose detail fetch fails rendered an EMPTY panel: no content,
    // no Approve, no reason. Moving Approve inside `open && detail` made that
    // worse, so the read failure now says what happened, in the server's words.
    mockApi.skillsPending.mockResolvedValue({ pending: [NEW_ROW] })
    mockApi.skillPendingDetail.mockRejectedValue(new Error('candidate directory is unreadable'))
    renderWithQuery()
    fireEvent.click(await screen.findByText('Review'))
    const notice = await screen.findByTestId('pending-detail-error-fresh-skill')
    expect(notice.textContent).toContain('candidate directory is unreadable')
    // And nothing invites approval of a body nobody could load.
    expect(screen.queryByText('Approve')).toBeNull()
  })

  it('says so when a re-read of the body fails, even though the old body is cached', async () => {
    // The case `!detail` could never catch: a failed REFETCH keeps the previous
    // body in cache, so keyed on missing data the failure was silent exactly when
    // it mattered. The stale body is withheld with Approve, because offering to
    // approve content the client could not re-read is the bargain this surface
    // exists to refuse.
    mockApi.skillsPending.mockResolvedValue({ pending: [NEW_ROW] })
    mockApi.skillPendingDetail.mockResolvedValueOnce({
      name: 'auto/fresh-skill',
      content: 'THE BODY AS FIRST READ',
      scripts: [],
    })
    mockApi.approvePendingSkill.mockRejectedValue(new Error('refused'))
    // The reconcile's re-read of the detail fails.
    mockApi.skillPendingDetail.mockRejectedValue(new Error('could not re-read the candidate'))
    renderWithQuery()
    fireEvent.click(await screen.findByText('Review'))
    await screen.findByText(/THE BODY AS FIRST READ/)
    fireEvent.click(screen.getByText('Approve'))

    const notice = await screen.findByTestId('pending-detail-error-fresh-skill')
    expect(notice.textContent).toContain('could not re-read the candidate')
    await waitFor(() => expect(screen.queryByText(/THE BODY AS FIRST READ/)).toBeNull())
    expect(screen.queryByText('Approve')).toBeNull()
  })

  it('keeps a refused row locked, and says why, when the reconcile itself fails', async () => {
    // `useIsFetching` goes quiet whether the refetch succeeded or failed. Keyed on
    // that alone, a failed reconcile handed the refused row its Approve back with
    // the queue still stale — the contradiction again, and now permanent.
    mockApi.skillsPending.mockResolvedValueOnce({ pending: [NEW_ROW] })
    mockApi.skillPendingDetail.mockResolvedValue({
      name: 'auto/fresh-skill',
      content: '## Steps\nrun it\n',
      scripts: [],
    })
    mockApi.approvePendingSkill.mockRejectedValue(new Error('this candidate is no longer pending'))
    // The reconcile cannot reach the server.
    mockApi.skillsPending.mockRejectedValue(new Error('could not refresh the queue'))
    renderWithQuery()
    fireEvent.click(await screen.findByText('Review'))
    fireEvent.click(await screen.findByText('Approve'))

    // The queue read failure is reported rather than swallowed…
    const listNotice = await screen.findByTestId('pending-list-error')
    expect(listNotice.textContent).toContain('could not refresh the queue')
    // …and the refused row does not offer the click the server just rejected.
    await waitFor(() =>
      expect((screen.getByText('Approve').closest('button') as HTMLButtonElement).disabled).toBe(true),
    )
  })

  it('withholds Approve on every row while the queue read is failing', async () => {
    // The lock cannot depend only on the refusal record: `useMutationState`
    // subscribes to the cache, not to individual mutations, so nothing pins an
    // errored one — leave the tab longer than its gcTime while the queue stays
    // down and the record is evicted, taking the per-row lock with it. An
    // unreadable queue means nothing on screen is confirmed, so no row offers
    // Approve, which holds whether or not that record still exists.
    mockApi.skillsPending.mockResolvedValue({
      pending: [NEW_ROW, { ...NEW_ROW, slug: 'other-skill', name: 'auto/other-skill' }],
    })
    mockApi.skillPendingDetail.mockResolvedValue({
      name: 'auto/fresh-skill',
      content: '## Steps\nrun it\n',
      scripts: [],
    })
    renderWithQuery()
    fireEvent.click((await screen.findAllByText('Review'))[0])
    await waitFor(() =>
      expect((screen.getByText('Approve').closest('button') as HTMLButtonElement).disabled).toBe(false),
    )

    // A dismiss on the OTHER row fails, and its reconcile finds the queue
    // unreadable. The refusal record names `other-skill`, so the per-row filter
    // cannot be what disables THIS row's Approve — only the unreadable queue can.
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    mockApi.dismissPendingSkill.mockRejectedValue(new Error('dismiss refused'))
    mockApi.skillsPending.mockRejectedValue(new Error('could not refresh the queue'))
    fireEvent.click(screen.getAllByText('Dismiss')[1])
    await screen.findByTestId('pending-list-error')
    expect((screen.getByText('Approve').closest('button') as HTMLButtonElement).disabled).toBe(true)
  })

  it('scopes a queue read failure to the pending queue, not to the Skills list', async () => {
    // The server's sentence names the whole store ("skills directory is
    // unreadable") while the live Skills list renders confidently right under
    // it. Without a scope the two contradict each other and a reader cannot
    // tell which half is lying. The title pins the failure to THIS queue.
    mockApi.skillsPending.mockRejectedValue(new Error('skills directory is unreadable'))
    renderWithQuery()

    const notice = await screen.findByTestId('pending-list-error')
    expect(within(notice).getByText('Pending review could not be loaded.').tagName).toBe('STRONG')
    expect(notice.textContent).toContain('skills directory is unreadable')
    // The list underneath keeps rendering its own (empty, successfully read)
    // state — the notice does not claim it is broken.
    expect(await screen.findByText('Skills (0)')).toBeInTheDocument()
  })

  it('does not drop one row\'s refusal lock when another row is actioned', async () => {
    // Clearing every retained error on any new action dropped the OTHER row's
    // refusal record, so its Approve re-enabled for the gap until the next list
    // fetch. The clear is scoped to the slug being acted on.
    const OTHER = { ...NEW_ROW, slug: 'other-skill', name: 'auto/other-skill' }
    mockApi.skillsPending.mockResolvedValue({ pending: [NEW_ROW, OTHER] })
    mockApi.skillPendingDetail.mockResolvedValue({
      name: 'auto/fresh-skill',
      content: '## Steps\nrun it\n',
      scripts: [],
    })
    mockApi.approvePendingSkill.mockRejectedValue(new Error('this candidate is no longer pending'))
    mockApi.dismissPendingSkill.mockResolvedValue({ ok: true })
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderWithQuery()

    // Row 1 is refused, with NO successful queue read afterwards (the reconcile is
    // held open), so its record is the only thing withholding its Approve.
    fireEvent.click((await screen.findAllByText('Review'))[0])
    mockApi.skillsPending.mockReturnValue(new Promise(() => {}))
    fireEvent.click(await screen.findByText('Approve'))
    await screen.findByTestId('pending-action-error')
    await waitFor(() =>
      expect((screen.getByText('Approve').closest('button') as HTMLButtonElement).disabled).toBe(true),
    )

    // Acting on the OTHER row must not clear row 1's record and unlock it.
    fireEvent.click(screen.getAllByText('Dismiss')[1])
    await waitFor(() => expect(mockApi.dismissPendingSkill).toHaveBeenCalledWith('other-skill'))
    expect((screen.getByText('Approve').closest('button') as HTMLButtonElement).disabled).toBe(true)
  })

  it('keeps a refused row locked across leaving and reopening the tab', async () => {
    // The lock has to survive the mount, not just the message. Held in component
    // state it did not: the cached stale rows and the queue's error both live in
    // the shared caches, so a remount while the queue was unreadable handed the
    // stale row its Approve back with no successful read having happened at all.
    // Both halves of the comparison are now cache-derived.
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    mockApi.skillsPending.mockResolvedValueOnce({ pending: [NEW_ROW] })
    mockApi.skillPendingDetail.mockResolvedValue({
      name: 'auto/fresh-skill',
      content: '## Steps\nrun it\n',
      scripts: [],
    })
    mockApi.approvePendingSkill.mockRejectedValue(new Error('this candidate is no longer pending'))
    mockApi.skillsPending.mockRejectedValue(new Error('could not refresh the queue'))

    const first = renderWithQuery(qc)
    fireEvent.click(await screen.findByText('Review'))
    fireEvent.click(await screen.findByText('Approve'))
    await screen.findByTestId('pending-action-error')
    await waitFor(() =>
      expect((screen.getByText('Approve').closest('button') as HTMLButtonElement).disabled).toBe(true),
    )

    // Leave the tab and come back while the queue is still unreadable.
    first.unmount()
    renderWithQuery(qc)
    fireEvent.click(await screen.findByText('Review'))
    await waitFor(() =>
      expect((screen.getByText('Approve').closest('button') as HTMLButtonElement).disabled).toBe(true),
    )
  })

  it('keeps a refused row locked even if the user dismisses the message', async () => {
    // The guard used to derive from the failure NOTICE, so tidying the message
    // away switched off the lock while the queue was still stale — a safety
    // property a user could turn off by clicking the X. It now tracks the thing
    // it actually depends on: the server refused this row, and no queue read has
    // succeeded since.
    mockApi.skillsPending.mockResolvedValueOnce({ pending: [NEW_ROW] })
    mockApi.skillPendingDetail.mockResolvedValue({
      name: 'auto/fresh-skill',
      content: '## Steps\nrun it\n',
      scripts: [],
    })
    mockApi.approvePendingSkill.mockRejectedValue(new Error('this candidate is no longer pending'))
    mockApi.skillsPending.mockRejectedValue(new Error('could not refresh the queue'))
    renderWithQuery()
    fireEvent.click(await screen.findByText('Review'))
    fireEvent.click(await screen.findByText('Approve'))
    const notice = await screen.findByTestId('pending-action-error')
    await waitFor(() =>
      expect((screen.getByText('Approve').closest('button') as HTMLButtonElement).disabled).toBe(true),
    )

    // Dismiss the message. The queue is still unreadable, so the lock must hold.
    const controls = within(notice).getAllByRole('button')
    fireEvent.click(controls[controls.length - 1])
    await waitFor(() => expect(screen.queryByTestId('pending-action-error')).toBeNull())
    expect((screen.getByText('Approve').closest('button') as HTMLButtonElement).disabled).toBe(true)
  })

  it('lets the user dismiss the failure, and it stays dismissed', async () => {
    // Cache-owned state is not discarded for us, so the dismiss affordance has to
    // actually remove the entry -- otherwise the notice returns on the next
    // render and the control reads as broken.
    mockApi.skillsPending.mockResolvedValue({ pending: [NEW_ROW] })
    mockApi.dismissPendingSkill.mockRejectedValue(new Error('dismiss refused'))
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderWithQuery()
    fireEvent.click(await screen.findByText('Dismiss'))
    const notice = await screen.findByTestId('pending-action-error')
    // The dismiss affordance is the last control inside the notice (the agent
    // hand-off precedes it).
    const controls = within(notice).getAllByRole('button')
    fireEvent.click(controls[controls.length - 1])
    await waitFor(() => expect(screen.queryByTestId('pending-action-error')).toBeNull())
  })

  it('holds the lock while the explanation is up, and lifts it when dismissed', async () => {
    // CONTRACT CHANGE, on blind-read evidence: this used to assert that a failed
    // action left Approve live as soon as the queue was re-read, so the row could
    // be retried immediately. That is what produced the frame a first-time reader
    // refused to act in -- "this candidate is no longer pending" above a live
    // purple Approve, which reads as a broken screen rather than as permission.
    // The message and the button now move together, and the retry path is the
    // notice's own dismiss, which is next to the sentence explaining why.
    mockApi.skillsPending.mockResolvedValue({ pending: [NEW_ROW] })
    mockApi.skillPendingDetail.mockResolvedValue({
      name: 'auto/fresh-skill',
      content: '## Steps\nrun it\n',
      scripts: [],
    })
    mockApi.approvePendingSkill.mockRejectedValue(new Error('server said no'))
    renderWithQuery()
    fireEvent.click(await screen.findByText('Review'))
    fireEvent.click(await screen.findByText('Approve'))
    const notice = await screen.findByTestId('pending-action-error')
    // Held: the explanation is on screen, so the button it contradicts is not live.
    await waitFor(() =>
      expect((screen.getByText('Approve').closest('button') as HTMLButtonElement).disabled).toBe(true),
    )

    // Dismissing the explanation is what restores the retry -- the user is never
    // stranded, they just have to have seen the reason first.
    const controls = within(notice).getAllByRole('button')
    fireEvent.click(controls[controls.length - 1])
    await waitFor(() => expect(screen.queryByTestId('pending-action-error')).toBeNull())
    await waitFor(() =>
      expect((screen.getByText('Approve').closest('button') as HTMLButtonElement).disabled).toBe(false),
    )
  })

  it('says the queue is empty when the panel stays up only to carry a message', async () => {
    // A refusal that takes the last candidate with it leaves the panel mounted
    // (`owesMessage`) with no heading and no rows -- a lone error banner. A
    // reader shown that could not tell whether the list was hidden because it
    // was empty or because it had failed to load. The confirmed-empty state now
    // says so, in one line, in the place the list would be.
    mockApi.skillsPending.mockResolvedValueOnce({ pending: [NEW_ROW] })
    mockApi.skillPendingDetail.mockResolvedValue({
      name: 'auto/fresh-skill',
      content: '## Steps\nrun it\n',
      scripts: [],
    })
    mockApi.approvePendingSkill.mockRejectedValue(new Error('server said no'))
    // The reconcile finds the queue empty.
    mockApi.skillsPending.mockResolvedValue({ pending: [] })
    renderWithQuery()
    fireEvent.click(await screen.findByText('Review'))
    fireEvent.click(await screen.findByText('Approve'))
    await screen.findByTestId('pending-action-error')
    await waitFor(() => expect(screen.queryByText('auto/fresh-skill')).toBeNull())
    const empty = await screen.findByTestId('pending-queue-empty')
    expect(empty.textContent).toBe('No skill candidates are pending review.')
    // Confirmed empty is not a failure, so the queue-read notice is absent.
    expect(screen.queryByTestId('pending-list-error')).toBeNull()
  })

  it('does not claim the queue is empty when the queue read failed', async () => {
    // The other direction of the same line. A failed read RETAINS the previous
    // data, and the very first read has none, so `pending` is [] in both cases
    // while the queue is in fact unknown. "No candidates pending" over an
    // unreadable queue would be the exact ambiguity the line exists to remove,
    // now stated as a fact.
    mockApi.skillsPending.mockRejectedValue(new Error('skills directory is unreadable'))
    renderWithQuery()
    const listNotice = await screen.findByTestId('pending-list-error')
    expect(listNotice.textContent).toContain('skills directory is unreadable')
    // The panel is up (it owes the read failure) with zero rows -- and it stays
    // silent about emptiness.
    expect(screen.queryByTestId('pending-queue-empty')).toBeNull()
    expect(screen.queryByText('No skill candidates are pending review.')).toBeNull()
  })

  it('points a refused approve at the Skills list without claiming an outcome', async () => {
    // The server's approve refusal is one 409 for three causes -- not found, a
    // live skill already exists, script validation failed -- and only the last
    // leaves the candidate pending. So the client cannot say what happened, and
    // a reader who had just seen the banner said they "never learn whether the
    // approval happened". The next-step line says WHERE the answer is (the Skills
    // list further down the tab) and nothing about WHAT it is.
    mockApi.skillsPending.mockResolvedValue({ pending: [NEW_ROW] })
    mockApi.skillPendingDetail.mockResolvedValue({
      name: 'auto/fresh-skill',
      content: '## Steps\nrun it\n',
      scripts: [],
    })
    mockApi.approvePendingSkill.mockRejectedValue(
      new Error('not found, a live skill already exists, or script validation failed'),
    )
    renderWithQuery()
    fireEvent.click(await screen.findByText('Review'))
    fireEvent.click(await screen.findByText('Approve'))
    const notice = await screen.findByTestId('pending-action-error')
    const next = await screen.findByTestId('pending-action-next-step')
    expect(next.textContent).toBe('The Skills list below shows whether \u201Cfresh-skill\u201D is among your skills.')
    // No outcome claim: none of the words that would assert a fate appear in the
    // line -- the same vocabulary the failed-action lead is held to.
    expect(next.textContent).not.toMatch(
      /\b(approved|installed|discarded|dismissed|removed|gone|live|succeeded|failed|still pending)\b/i,
    )
    // And no promise that the list EXPLAINS the outcome: after a "not found"
    // refusal the row leaves the queue while the list can read "No skills yet",
    // so a line that sent the reader there to "find out what happened" promised
    // an answer the screen could not hold. The list settles one question only --
    // is it among my skills -- and the line is held to that question.
    expect(next.textContent).not.toMatch(/what happened|find out|to see what|to learn/i)
    // It names the place to look, and it names the candidate.
    expect(next.textContent).toMatch(/Skills list/)
    expect(next.textContent).toContain('fresh-skill')
    // It rides on the notice's display decision: dismiss the explanation and the
    // pointer goes with it.
    const controls = within(notice).getAllByRole('button')
    fireEvent.click(controls[controls.length - 1])
    await waitFor(() => expect(screen.queryByTestId('pending-action-error')).toBeNull())
    expect(screen.queryByTestId('pending-action-next-step')).toBeNull()
  })

  it('offers no Skills-list pointer for a refused dismiss', async () => {
    // A refused DISMISS has no such ambiguity worth a pointer: the Skills list
    // cannot say anything about a candidate the user tried to discard. The line
    // is scoped to the approve verb, so a dismiss refusal renders only the banner.
    mockApi.skillsPending.mockResolvedValue({ pending: [NEW_ROW] })
    mockApi.dismissPendingSkill.mockRejectedValue(new Error('internal error'))
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderWithQuery()
    fireEvent.click(await screen.findByText('Dismiss'))
    await screen.findByTestId('pending-action-error')
    expect(screen.queryByTestId('pending-action-next-step')).toBeNull()
  })

  it('says, beside the held Approve, that the displayed refusal holds it and that closing it is the retry', async () => {
    // The refusal lock re-creates, one layer in, the shape this PR exists to
    // remove: an Approve that is disabled with nothing saying why or what brings
    // it back. A reader shown the held button concluded "I can't retry from
    // here". So the hold names its own release, IN THE SAME BLOCK as the button
    // it describes -- and goes with it, because once the message is closed the
    // hold it describes no longer exists.
    mockApi.skillsPending.mockResolvedValue({ pending: [NEW_ROW] })
    mockApi.skillPendingDetail.mockResolvedValue({
      name: 'auto/fresh-skill',
      content: '## Steps\nrun it\n',
      scripts: [],
    })
    mockApi.approvePendingSkill.mockRejectedValue(new Error('server said no'))
    renderWithQuery()
    fireEvent.click(await screen.findByText('Review'))
    fireEvent.click(await screen.findByText('Approve'))
    const notice = await screen.findByTestId('pending-action-error')
    const hold = await screen.findByTestId('pending-action-hold')
    expect(hold.textContent).toBe(
      'Approve is disabled while the error message about \u201Cfresh-skill\u201D is showing. Close that message to try again.',
    )
    // It describes the button and the release, never the candidate's fate, and
    // never that the retry will be accepted.
    expect(hold.textContent).not.toMatch(
      /\b(approved|installed|discarded|dismissed|removed|gone|live|succeeded|failed|still pending|will work|will succeed)\b/i,
    )
    // "Close", never "dismiss": the red Dismiss on the same row discards the
    // candidate, and a reader who took the two for one control would destroy what
    // they meant to retry.
    expect(hold.textContent).not.toMatch(/dismiss/i)
    // It is TRUE while it is shown: the row's Approve is in fact held, and the
    // line sits in the same block as that button rather than under the notice.
    const approveBtn = screen.getByText('Approve').closest('button') as HTMLButtonElement
    expect(approveBtn.disabled).toBe(true)
    expect(hold.parentElement).toBe(approveBtn.parentElement)
    expect(notice.contains(hold)).toBe(false)
    // The release it names is the notice's own close, and the line leaves with
    // the hold it described.
    const controls = within(notice).getAllByRole('button')
    fireEvent.click(controls[controls.length - 1])
    await waitFor(() => expect(screen.queryByTestId('pending-action-error')).toBeNull())
    expect(screen.queryByTestId('pending-action-hold')).toBeNull()
    await waitFor(() =>
      expect((screen.getByText('Approve').closest('button') as HTMLButtonElement).disabled).toBe(false),
    )
  })

  it('shows the hold line only while the Approve it names is on screen', async () => {
    // Approve renders only inside the OPEN review panel -- that gate is this
    // surface's whole premise -- so a sentence about "Approve" is false whenever
    // the row is collapsed. A refused DISMISS is exactly that state: Dismiss sits
    // on the collapsed row, so the refusal lands with no Approve anywhere, and the
    // line used to render under the notice regardless ("the sentence talks about
    // a button that doesn't exist"). Both directions: absent while collapsed,
    // present once Review opens the panel, absent again on Hide.
    mockApi.skillsPending.mockResolvedValue({ pending: [NEW_ROW] })
    mockApi.skillPendingDetail.mockResolvedValue({
      name: 'auto/fresh-skill',
      content: '## Steps\nrun it\n',
      scripts: [],
    })
    mockApi.dismissPendingSkill.mockRejectedValue(new Error('dismiss refused'))
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderWithQuery()
    fireEvent.click(await screen.findByText('Dismiss'))
    await screen.findByTestId('pending-action-error')
    // Collapsed: no Approve, so no sentence about one -- even though this
    // refusal names the row and WOULD hold its Approve.
    expect(screen.queryByText('Approve')).toBeNull()
    expect(screen.queryByTestId('pending-action-hold')).toBeNull()
    // Open: the held button appears, and the explanation appears beside it.
    // The hold is keyed on the SLUG the notice names, not on the verb -- a
    // refused dismiss holds Approve exactly as a refused approve would, while
    // Dismiss itself is gated on `busy` alone and stays live.
    fireEvent.click(screen.getByText('Review'))
    const hold = await screen.findByTestId('pending-action-hold')
    expect(hold.textContent).toContain('fresh-skill')
    const approveBtn = screen.getByText('Approve').closest('button') as HTMLButtonElement
    expect(approveBtn.disabled).toBe(true)
    expect(hold.parentElement).toBe(approveBtn.parentElement)
    expect((screen.getByText('Dismiss').closest('button') as HTMLButtonElement).disabled).toBe(false)
    // Hide: the button leaves, and the sentence about it leaves with it -- while
    // the notice, which still holds the row, stays.
    fireEvent.click(screen.getByText('Hide'))
    await waitFor(() => expect(screen.queryByText('Approve')).toBeNull())
    expect(screen.queryByTestId('pending-action-hold')).toBeNull()
    expect(screen.getByTestId('pending-action-error')).toBeTruthy()
  })

  it('offers no hold line once the reconcile has removed the row it would describe', async () => {
    // A "not found" refusal is the one whose reconcile takes the row away. The
    // notice stays (it is the only account of where the row went), but a line
    // saying "Approve is disabled" over a queue that reads "No skill candidates
    // are pending review." would name a button that no longer exists anywhere.
    let resolveRefetch!: (v: { pending: typeof NEW_ROW[] }) => void
    mockApi.skillsPending.mockResolvedValueOnce({ pending: [NEW_ROW] })
    mockApi.skillPendingDetail.mockResolvedValue({
      name: 'auto/fresh-skill',
      content: '## Steps\nrun it\n',
      scripts: [],
    })
    mockApi.approvePendingSkill.mockRejectedValue(new Error('this candidate is no longer pending'))
    mockApi.skillsPending.mockReturnValue(new Promise(r => { resolveRefetch = r }))
    renderWithQuery()
    fireEvent.click(await screen.findByText('Review'))
    fireEvent.click(await screen.findByText('Approve'))
    await screen.findByTestId('pending-action-error')
    // While the row is still on screen and open, the line is true and present.
    await screen.findByTestId('pending-action-hold')

    resolveRefetch({ pending: [] })
    await waitFor(() => expect(screen.queryByText('Approve')).toBeNull())
    expect(screen.queryByTestId('pending-action-hold')).toBeNull()
    // The notice itself is not what left.
    expect(screen.getByTestId('pending-action-error').textContent).toContain('this candidate is no longer pending')
  })

  it('offers no hold line when the row is open but its body could not be read', async () => {
    // The third way Approve can be absent from an on-screen row: `open` but the
    // detail read failed, so the panel shows the read error and withholds the
    // button. A "present AND open" condition alone would still render the line
    // here; living in the same block as Approve, it cannot.
    mockApi.skillsPending.mockResolvedValue({ pending: [NEW_ROW] })
    mockApi.skillPendingDetail.mockRejectedValue(new Error('candidate directory is unreadable'))
    mockApi.dismissPendingSkill.mockRejectedValue(new Error('dismiss refused'))
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderWithQuery()
    fireEvent.click(await screen.findByText('Dismiss'))
    await screen.findByTestId('pending-action-error')
    fireEvent.click(screen.getByText('Review'))
    await screen.findByTestId('pending-detail-error-fresh-skill')
    expect(screen.queryByText('Approve')).toBeNull()
    expect(screen.queryByTestId('pending-action-hold')).toBeNull()
  })

  it('lets the row\'s own refusal sentence speak alone when it, too, disables Approve', async () => {
    // A stale-base update is refused by the ROW (its diff would undo newer
    // changes), and that sentence already explains the disabled button. If the
    // notice also holds the row, "Close that message to try again" would be
    // false -- closing the notice leaves Approve disabled -- so the hold line
    // yields to the refusal rather than stacking a second, contradicting reason.
    mockApi.skillsPending.mockResolvedValue({ pending: [UPDATE_ROW] })
    mockApi.skillPendingDetail.mockResolvedValue({
      name: 'auto/deploy-helper-update',
      content: '',
      scripts: [],
      diff: DIFF,
      from_version: 5,
      to_version: 6,
      stale_base: true,
    })
    mockApi.dismissPendingSkill.mockRejectedValue(new Error('dismiss refused'))
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderWithQuery()
    fireEvent.click(await screen.findByText('Dismiss'))
    await screen.findByTestId('pending-action-error')
    fireEvent.click(screen.getByText('Review'))
    await screen.findByText(/would undo those newer changes/)
    expect((screen.getByText('Approve').closest('button') as HTMLButtonElement).disabled).toBe(true)
    expect(screen.queryByTestId('pending-action-hold')).toBeNull()
  })

  it('offers no hold line for a refused dismiss-all, which holds no row', async () => {
    // The bulk action names no slug, so `displayedRefusalSlug` is undefined and no
    // row's Approve is withheld by the notice. A line saying "Approve is disabled"
    // would then be false -- there is no held button for it to be about.
    mockApi.skillsPending.mockResolvedValue({ pending: [NEW_ROW] })
    mockApi.skillPendingDetail.mockResolvedValue({
      name: 'auto/fresh-skill',
      content: '## Steps\nrun it\n',
      scripts: [],
    })
    mockApi.dismissAllPendingSkills.mockRejectedValue(new Error('bulk dismiss refused'))
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderWithQuery()
    fireEvent.click(await screen.findByText('Dismiss All'))
    await screen.findByTestId('pending-action-error')
    fireEvent.click(screen.getByText('Review'))
    await waitFor(() =>
      expect((screen.getByText('Approve').closest('button') as HTMLButtonElement).disabled).toBe(false),
    )
    expect(screen.queryByTestId('pending-action-hold')).toBeNull()
  })
})
