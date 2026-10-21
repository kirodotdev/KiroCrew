import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent, act, within } from '@testing-library/react'
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
vi.mock('../api/client', () => ({
  api: mockApi,
  ApiError: class ApiError extends Error {
    status: number
    body: string
    constructor(status: number, message: string, body = '') {
      super(message)
      this.name = 'ApiError'
      this.status = status
      this.body = body
    }
  },
}))

vi.mock('../providers', () => ({
  useProvider: () => ({ labels: { pluginRegistryName: 'Packages' } }),
}))

vi.mock('../components/MarkdownRenderer', () => ({
  default: ({ content }: { content: string }) => <div data-testid="md">{content}</div>,
}))

vi.mock('../components/SkillDirectoryBrowser', () => ({
  default: () => <div data-testid="dir-browser">browser</div>,
}))

import SkillsTab from '../pages/overview/SkillsTab'

let qcRef: QueryClient | null = null

function renderWithQuery() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  qcRef = qc
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter><SkillsTab /></MemoryRouter>
    </QueryClientProvider>,
  )
}

/** The redacted report shape the backend serves for a flagged candidate. */
const REPORT = { 'evil.py': ["dynamic exec/import: eval()"] }

const FLAGGED_ROW = {
  slug: 'evil-helper',
  name: 'auto/evil-helper',
  description: 'does bad things',
  has_scripts: true,
  kind: 'new',
  target: null,
  base_version: null,
  script_validation: { ok: false, report: REPORT },
}

const CLEAN_ROW = {
  slug: 'good-helper',
  name: 'auto/good-helper',
  description: 'does good things',
  has_scripts: true,
  kind: 'new',
  target: null,
  base_version: null,
  script_validation: { ok: true, report: {} },
}

const FLAGGED_DETAIL = {
  name: 'auto/evil-helper',
  content: '## Steps\n1. go\n',
  scripts: [{ filename: 'evil.py', content: "x = eval('1+1')\n" }],
  script_validation: { ok: false, report: REPORT },
  content_sha256: 'c'.repeat(64),
}

/** The flagged candidate as it appears in the LIVE list once approved elsewhere. */
const LIVE_SKILL = {
  key: 'auto/evil-helper',
  name: 'auto/evil-helper',
  description: 'does bad things',
  source: 'local',
}

/* The three sentences a not-found approve refusal can settle on. The hedge is
 * honest only while the live-list refetch is pending or failed; once it settles,
 * the notice must state the outcome ONCE instead of hedging and then answering
 * itself a line below. */
const HEDGED =
  'Candidate “auto/evil-helper” is no longer pending — it was approved or removed in another window or by your Kiro Crew agent, so there was nothing left to approve.'
const SETTLED_DISMISSED =
  'Candidate “auto/evil-helper” is no longer pending — it was removed in another window or by your Kiro Crew agent and does not appear in the Skills list below, so there was nothing left to approve. A replacement candidate can be proposed again later.'
const SETTLED_APPROVED =
  'Candidate “auto/evil-helper” is no longer pending — it was already approved in another window or by your Kiro Crew agent and now appears in the Skills list below, so there was nothing left to approve.'

/** Ceiling for a wait that spans chained queries (Skills → pending list → detail). */
const CHAINED_QUERY_TIMEOUT_MS = 5000

beforeEach(() => {
  vi.clearAllMocks()
  mockApi.skills.mockResolvedValue([])
  mockApi.skillTree.mockResolvedValue({ tree: [] })
  mockApi.skillPendingDetail.mockResolvedValue(FLAGGED_DETAIL)
})

describe('pending-card script-validation verdict (issue #10861)', () => {
  it('renders a fails-validation badge from the list entry, before any click', async () => {
    mockApi.skillsPending.mockResolvedValue({ pending: [FLAGGED_ROW, CLEAN_ROW] })
    renderWithQuery()
    await waitFor(() => expect(screen.getByText('auto/evil-helper')).toBeInTheDocument())
    // Flagged card carries the badge; the clean card does not.
    expect(screen.getAllByText('fails validation')).toHaveLength(1)
  })

  it('shows the expandable findings warning in the expanded review panel', async () => {
    mockApi.skillsPending.mockResolvedValue({ pending: [FLAGGED_ROW] })
    renderWithQuery()
    await waitFor(() => expect(screen.getByText('auto/evil-helper')).toBeInTheDocument())
    fireEvent.click(screen.getByText('Review'))
    // ONE heading across both warning states (pre-click and post-refusal);
    // the refusal prediction + fix action live in the visible hint.
    await waitFor(() =>
      expect(
        screen.getByText('Validation findings'),
      ).toBeInTheDocument(),
    )
    expect(screen.getByText(/Fix the scripts, then approve/)).toBeInTheDocument()
    // The findings themselves are inside the <details> body.
    expect(screen.getByText("dynamic exec/import: eval()")).toBeInTheDocument()
    // Approve stays clickable — the server is the authority.
    const approve = screen.getByText('Approve').closest('button')
    await waitFor(() => expect(approve).not.toBeDisabled())
  })

  it('renders the refusal reason and findings after a failed approve', async () => {
    mockApi.skillsPending.mockResolvedValue({ pending: [FLAGGED_ROW] })
    const body = JSON.stringify({
      error: 'script validation failed',
      code: 'script_validation_failed',
      report: REPORT,
    })
    // Duck-typed ApiError shape (status + body), per api/apiError.ts.
    mockApi.approvePendingSkill.mockRejectedValue(
      Object.assign(new Error('script validation failed'), { status: 422, body }),
    )
    renderWithQuery()
    await waitFor(() => expect(screen.getByText('auto/evil-helper')).toBeInTheDocument())
    fireEvent.click(screen.getByText('Review'))
    const approve = screen.getByText('Approve').closest('button') as HTMLButtonElement
    await waitFor(() => expect(approve).not.toBeDisabled())
    fireEvent.click(approve)
    await waitFor(() =>
      expect(
        screen.getByText('Approval refused: the bundled scripts failed validation — approving again without changes returns this same refusal. Fix the scripts, then approve.'),
      ).toBeInTheDocument(),
    )
    // The findings are the 422's OWN, and they render INSIDE the error notice.
    // `errors-use-error-notice` decides by where a value comes from, so the
    // same list in a hand-rolled box beside the notice would be a second error
    // surface — one that throws away the endpoint/status context the notice
    // recovers for the agent hand-off.
    const findings = screen.getAllByText('dynamic exec/import: eval()')
    expect(findings).toHaveLength(1)
    expect(screen.getByRole('alert')).toContainElement(findings[0])
    // Exactly one copy, because the pre-approval PREDICTION is withdrawn once
    // the outcome it predicted is on screen: a poll-time snapshot must not sit
    // beside findings the server just computed on the live tree, where the
    // stale list is indistinguishable from the fresh one.
    expect(screen.queryByText('Validation findings')).not.toBeInTheDocument()
  })

  it('keeps the pre-approval prediction when the refusal is NOT about validation', async () => {
    // The prediction is withdrawn only by the outcome it PREDICTED. A refusal
    // with any other code (here: a live skill already holds the name) has not
    // superseded it, so the flagged candidate's findings must still be on
    // offer — the user still has to fix them. Pins the gate to the refusal
    // CODE: narrowing it to a bare `!approveRefusal` would silently drop the
    // prediction for every unrelated failure.
    mockApi.skillsPending.mockResolvedValue({ pending: [FLAGGED_ROW] })
    const body = JSON.stringify({
      error: 'a live skill with this name already exists',
      code: 'live_skill_exists',
    })
    mockApi.approvePendingSkill.mockRejectedValue(
      Object.assign(new Error('conflict'), { status: 409, body }),
    )
    renderWithQuery()
    await waitFor(() => expect(screen.getByText('auto/evil-helper')).toBeInTheDocument())
    fireEvent.click(screen.getByText('Review'))
    const approve = screen.getByText('Approve').closest('button') as HTMLButtonElement
    await waitFor(() => expect(approve).not.toBeDisabled())
    fireEvent.click(approve)
    await waitFor(() =>
      expect(screen.getByRole('alert')).toBeInTheDocument(),
    )
    // Prediction survives, and its findings are NOT inside the error notice —
    // they describe something that has not failed, which the rule excludes.
    expect(screen.getByText('Validation findings')).toBeInTheDocument()
    const findings = screen.getAllByText('dynamic exec/import: eval()')
    expect(findings).toHaveLength(1)
    expect(screen.getByRole('alert')).not.toContainElement(findings[0])
  })

  it('maps a live-exists refusal to its own message', async () => {
    mockApi.skillsPending.mockResolvedValue({ pending: [CLEAN_ROW] })
    mockApi.skillPendingDetail.mockResolvedValue({
      name: 'auto/good-helper',
      content: 'body',
      scripts: [], content_sha256: 'c'.repeat(64),
      script_validation: { ok: true, report: {} },
    })
    const body = JSON.stringify({ error: 'a live skill with this name already exists', code: 'live_skill_exists' })
    mockApi.approvePendingSkill.mockRejectedValue(
      Object.assign(new Error('conflict'), { status: 409, body }),
    )
    renderWithQuery()
    await waitFor(() => expect(screen.getByText('auto/good-helper')).toBeInTheDocument())
    fireEvent.click(screen.getByText('Review'))
    const approve = screen.getByText('Approve').closest('button') as HTMLButtonElement
    await waitFor(() => expect(approve).not.toBeDisabled())
    fireEvent.click(approve)
    await waitFor(() =>
      expect(
        screen.getByText('Approval refused: a live skill with this name already exists.'),
      ).toBeInTheDocument(),
    )
    // No findings list for a non-validation refusal.
    expect(screen.queryByText("dynamic exec/import: eval()")).not.toBeInTheDocument()
  })

  it('explains a changed-since-read refusal and reads the candidate again', async () => {
    mockApi.skillsPending.mockResolvedValue({ pending: [CLEAN_ROW] })
    mockApi.skillPendingDetail.mockResolvedValue({
      name: 'auto/good-helper',
      content: 'body',
      scripts: [],
      script_validation: { ok: true, report: {} },
      content_sha256: 'd'.repeat(64),
    })
    const body = JSON.stringify({ error: 'changed', code: 'pending_skill_changed' })
    mockApi.approvePendingSkill.mockRejectedValue(
      Object.assign(new Error('conflict'), { status: 409, body }),
    )
    renderWithQuery()
    // Two chained queries: the Skills query must settle before the pending
    // panel mounts and starts the pending-list query that renders this row.
    await waitFor(() => expect(screen.getByText('auto/good-helper')).toBeInTheDocument(), {
      timeout: CHAINED_QUERY_TIMEOUT_MS,
    })
    fireEvent.click(screen.getByText('Review'))
    const approve = screen.getByText('Approve').closest('button') as HTMLButtonElement
    // The detail query starts only once Review opens the row.
    await waitFor(() => expect(approve).not.toBeDisabled(), { timeout: CHAINED_QUERY_TIMEOUT_MS })
    const readsBefore = mockApi.skillPendingDetail.mock.calls.length
    fireEvent.click(approve)
    await waitFor(() =>
      expect(mockApi.approvePendingSkill).toHaveBeenCalledWith(CLEAN_ROW.slug, 'd'.repeat(64)),
    )
    await waitFor(() =>
      expect(
        screen.getByText(
          'Approval refused: this candidate changed after you read it. It has been read again. Review it, then approve.',
        ),
      ).toBeInTheDocument(),
    )
    // The refusal invalidates the detail query, which then reads again.
    await waitFor(
      () => expect(mockApi.skillPendingDetail.mock.calls.length).toBeGreaterThan(readsBefore),
      { timeout: CHAINED_QUERY_TIMEOUT_MS },
    )
  })

  it('dismissing the candidate clears its refusal, so a re-staged slug starts clean', async () => {
    mockApi.skillsPending.mockResolvedValue({ pending: [FLAGGED_ROW] })
    const body = JSON.stringify({
      error: 'script validation failed',
      code: 'script_validation_failed',
      report: REPORT,
    })
    mockApi.approvePendingSkill.mockRejectedValue(
      Object.assign(new Error('script validation failed'), { status: 422, body }),
    )
    mockApi.dismissPendingSkill.mockResolvedValue({ dismissed: true })
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)
    try {
      renderWithQuery()
      await waitFor(() => expect(screen.getByText('auto/evil-helper')).toBeInTheDocument())
      fireEvent.click(screen.getByText('Review'))
      const approve = screen.getByText('Approve').closest('button') as HTMLButtonElement
      await waitFor(() => expect(approve).not.toBeDisabled())
      fireEvent.click(approve)
      await waitFor(() =>
        expect(
          screen.getByText('Approval refused: the bundled scripts failed validation — approving again without changes returns this same refusal. Fix the scripts, then approve.'),
        ).toBeInTheDocument(),
      )
      // Dismiss the candidate; the row (still rendered from the stale list
      // mock) must no longer carry the previous candidate's refusal notice.
      fireEvent.click(screen.getByText('Dismiss'))
      await waitFor(() => expect(mockApi.dismissPendingSkill).toHaveBeenCalledWith('evil-helper'))
      await waitFor(() =>
        expect(
          screen.queryByText('Approval refused: the bundled scripts failed validation — approving again without changes returns this same refusal. Fix the scripts, then approve.'),
        ).not.toBeInTheDocument(),
      )
    } finally {
      confirmSpy.mockRestore()
    }
  })

  it('a failed dismiss surfaces a named error notice instead of failing silently', async () => {
    mockApi.skillsPending.mockResolvedValue({ pending: [FLAGGED_ROW] })
    mockApi.dismissPendingSkill.mockRejectedValue(
      Object.assign(new Error('pending skill not found'), {
        status: 404,
        body: JSON.stringify({ error: 'pending skill not found', code: 'pending_skill_not_found' }),
      }),
    )
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)
    try {
      renderWithQuery()
      await waitFor(() => expect(screen.getByText('auto/evil-helper')).toBeInTheDocument())
      fireEvent.click(screen.getByText('Dismiss'))
      await waitFor(() => expect(mockApi.dismissPendingSkill).toHaveBeenCalledWith('evil-helper'))
      // The mutation's rejection must not vanish: the panel renders the
      // localized failure through ErrorNotice (errors-use-error-notice —
      // the value originates in a rejected mutation, so the shared error
      // surface with its Ask-agent hand-off is mandatory), NAMING the
      // candidate and mapping the coded reason to catalog text. The live
      // list (mocked empty) settles, so the reason states the DISMISSAL
      // outright rather than "approved or removed".
      await waitFor(() =>
        expect(
          screen.getByText('Dismiss of “auto/evil-helper” failed (this candidate is no longer pending — it was already removed in another window or by your Kiro Crew agent and does not appear in the Skills list below; a replacement candidate can be proposed again later).'),
        ).toBeInTheDocument(),
      )
      expect(screen.getByRole('alert').textContent).not.toContain('approved or removed')
      // The outcome resolved (handled elsewhere — nothing is broken), so the
      // hand-off follows it instead of naming a "failure".
      expect(
        screen.getByText('Ask the agent what happened to this candidate'),
      ).toBeInTheDocument()
      // And the queue is re-read so the stale row cannot contradict the
      // notice (same recovery as the approve path's not-found branch).
      expect(mockApi.skillsPending.mock.calls.length).toBeGreaterThan(1)
      // A retry starts clean: the next dismiss click clears the stale notice
      // before the new mutation settles.
      mockApi.dismissPendingSkill.mockResolvedValue({ dismissed: true })
      fireEvent.click(screen.getByText('Dismiss'))
      await waitFor(() =>
        expect(
          screen.queryByText('Dismiss of “auto/evil-helper” failed (this candidate is no longer pending — it was already removed in another window or by your Kiro Crew agent and does not appear in the Skills list below; a replacement candidate can be proposed again later).'),
        ).not.toBeInTheDocument(),
      )
    } finally {
      confirmSpy.mockRestore()
    }
  })

  it('a not-found dismiss keeps the open wording while the live list cannot answer', async () => {
    mockApi.skillsPending.mockResolvedValue({ pending: [FLAGGED_ROW] })
    // Initial load succeeds; the refetch the not-found branch triggers FAILS,
    // so the app does not know which way the candidate went — the reason must
    // say so, and must not claim a dismissal off an empty-by-error list.
    mockApi.skills.mockResolvedValueOnce([]).mockRejectedValue(new Error('boom'))
    mockApi.dismissPendingSkill.mockRejectedValue(
      Object.assign(new Error('pending skill not found'), {
        status: 404,
        body: JSON.stringify({ error: 'pending skill not found', code: 'pending_skill_not_found' }),
      }),
    )
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)
    try {
      renderWithQuery()
      await waitFor(() => expect(screen.getByText('auto/evil-helper')).toBeInTheDocument())
      fireEvent.click(screen.getByText('Dismiss'))
      await waitFor(() =>
        expect(
          screen.getByText('Dismiss of “auto/evil-helper” failed (this candidate is no longer pending — approved or removed in another window or by your Kiro Crew agent).'),
        ).toBeInTheDocument(),
      )
      await waitFor(() => expect(mockApi.skills.mock.calls.length).toBeGreaterThan(1))
      // The refetch has failed; the hedge stands and no outcome is asserted.
      expect(
        screen.getByText('Dismiss of “auto/evil-helper” failed (this candidate is no longer pending — approved or removed in another window or by your Kiro Crew agent).'),
      ).toBeInTheDocument()
      expect(screen.getByRole('alert').textContent).not.toContain('already dismissed')
      expect(screen.getByRole('alert').textContent).not.toContain('already approved')
    } finally {
      confirmSpy.mockRestore()
    }
  })

  it('a not-found dismiss resolves to "already approved" when the skill went live', async () => {
    mockApi.skillsPending.mockResolvedValue({ pending: [FLAGGED_ROW] })
    // The live list's refetch now holds the candidate under its name: it was
    // approved elsewhere, and the notice says exactly that. Flipped right
    // before the click, not by call count — the tab reads the same list at
    // mount and a count-based mock would seat the skill before the refusal.
    let live = false
    mockApi.skills.mockImplementation(() => Promise.resolve(live ? [LIVE_SKILL] : []))
    mockApi.dismissPendingSkill.mockRejectedValue(
      Object.assign(new Error('pending skill not found'), {
        status: 404,
        body: JSON.stringify({ error: 'pending skill not found', code: 'pending_skill_not_found' }),
      }),
    )
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)
    try {
      renderWithQuery()
      await waitFor(() => expect(screen.getByText('auto/evil-helper')).toBeInTheDocument())
      live = true
      fireEvent.click(screen.getByText('Dismiss'))
      await waitFor(() =>
        expect(
          screen.getByText('Dismiss of “auto/evil-helper” failed (this candidate is no longer pending — it was already approved in another window or by your Kiro Crew agent and now appears in the Skills list below).'),
        ).toBeInTheDocument(),
      )
      expect(screen.getByRole('alert').textContent).not.toContain('approved or removed')
    } finally {
      confirmSpy.mockRestore()
    }
  })

  it('a not-found approve refusal survives the refetch that removes its row', async () => {
    // First fetch renders the card; the refetch the refusal triggers returns
    // an empty queue, unmounting the row and any per-row notice with it.
    mockApi.skillsPending
      .mockResolvedValueOnce({ pending: [FLAGGED_ROW] })
      .mockResolvedValue({ pending: [] })
    // The live list the refusal branch refetches: the candidate is NOT in it,
    // so the notice resolves to a dismissal.
    mockApi.skills.mockResolvedValue([])
    mockApi.approvePendingSkill.mockRejectedValue(
      Object.assign(new Error('pending skill not found'), {
        status: 404,
        body: JSON.stringify({ error: 'pending skill not found', code: 'pending_skill_not_found' }),
      }),
    )
    renderWithQuery()
    await waitFor(() => expect(screen.getByText('auto/evil-helper')).toBeInTheDocument())
    fireEvent.click(screen.getByText('Review'))
    const approve = screen.getByText('Approve').closest('button') as HTMLButtonElement
    await waitFor(() => expect(approve).not.toBeDisabled())
    fireEvent.click(approve)
    // The row disappears (queue re-read as empty) …
    await waitFor(() => expect(screen.queryByText('auto/evil-helper')).not.toBeInTheDocument())
    // … but the refusal does NOT: it moved to the panel, so the user never
    // mistakes the removal for a successful approve. "Approved or dismissed"
    // is RESOLVED from the refetched live list — the candidate never appeared
    // there — and the notice states the dismissal ONCE, in the message.
    await waitFor(() =>
      expect(
        screen.getByText(SETTLED_DISMISSED),
      ).toBeInTheDocument(),
    )
    const alert = screen.getByRole('alert')
    // The click still did nothing; that is kept in the settled sentence.
    expect(alert.textContent).toContain('nothing left to approve')
    // One statement per screen: no hedge above the answer, and no separate
    // resolution line repeating it below (the shot-05 "hedges then answers
    // itself" reading).
    expect(alert.textContent).not.toContain('approved or removed')
    expect(alert.textContent).not.toContain('It was dismissed')
    expect(screen.queryByText(HEDGED)).not.toBeInTheDocument()
    // The hand-off follows the resolved state: nothing is broken once the
    // outcome is known, so the link must not name a "failure" — it asks
    // what happened to the candidate instead.
    expect(
      screen.getByText('Ask the agent what happened to this candidate'),
    ).toBeInTheDocument()
    expect(screen.queryByText('Ask the agent about this failure')).not.toBeInTheDocument()
  })

  it('a not-found approve refusal resolves to "already approved" when the skill went live', async () => {
    mockApi.skillsPending
      .mockResolvedValueOnce({ pending: [FLAGGED_ROW] })
      .mockResolvedValue({ pending: [] })
    // The refetch the refusal triggers finds the candidate live under its
    // name — it was approved elsewhere. Flipped right before the click (see
    // the dismiss-path twin for why not a call count).
    let live = false
    mockApi.skills.mockImplementation(() => Promise.resolve(live ? [LIVE_SKILL] : []))
    mockApi.approvePendingSkill.mockRejectedValue(
      Object.assign(new Error('pending skill not found'), {
        status: 404,
        body: JSON.stringify({ error: 'pending skill not found', code: 'pending_skill_not_found' }),
      }),
    )
    renderWithQuery()
    await waitFor(() => expect(screen.getByText('auto/evil-helper')).toBeInTheDocument())
    fireEvent.click(screen.getByText('Review'))
    const approve = screen.getByText('Approve').closest('button') as HTMLButtonElement
    await waitFor(() => expect(approve).not.toBeDisabled())
    live = true
    fireEvent.click(approve)
    await waitFor(() =>
      expect(
        screen.getByText(SETTLED_APPROVED),
      ).toBeInTheDocument(),
    )
    const alert = screen.getByRole('alert')
    expect(alert.textContent).not.toContain('approved or removed')
    expect(alert.textContent).not.toContain('It was approved')
    expect(screen.queryByText(HEDGED)).not.toBeInTheDocument()
  })

  it('an update candidate handled elsewhere keeps the honest hedge, never a guessed outcome', async () => {
    // An UPDATE's outcome is NOT observable from live-list membership: its
    // target exists whether the update was applied or dismissed. Resolving
    // by the candidate name calls every approved update "dismissed";
    // resolving by the target calls every dismissed update "approved". So
    // updates never resolve — the hedge is the only honest sentence.
    const UPDATE_ROW = {
      ...FLAGGED_ROW,
      slug: 'deploy-helper-update',
      name: 'auto/deploy-helper-update',
      kind: 'update',
      target: 'auto/deploy-helper',
    }
    mockApi.skillsPending
      .mockResolvedValueOnce({ pending: [UPDATE_ROW] })
      .mockResolvedValue({ pending: [] })
    // An update row's Approve enables only once the detail carries an
    // applicable preview (a diff against a live, un-moved target).
    mockApi.skillPendingDetail.mockResolvedValue({
      ...FLAGGED_DETAIL,
      name: 'auto/deploy-helper-update',
      diff: '--- live\n+++ proposed\n@@ -1 +1 @@\n-old\n+new',
      stale_base: false,
    })
    // The live list carries the TARGET either way — a dismissed update's
    // target is there too, which is exactly why membership proves nothing.
    mockApi.skills.mockResolvedValue([{ ...LIVE_SKILL, name: 'auto/deploy-helper' }])
    mockApi.approvePendingSkill.mockRejectedValue(
      Object.assign(new Error('pending skill not found'), {
        status: 404,
        body: JSON.stringify({ error: 'pending skill not found', code: 'pending_skill_not_found' }),
      }),
    )
    renderWithQuery()
    await waitFor(() => expect(screen.getByText('auto/deploy-helper-update')).toBeInTheDocument())
    fireEvent.click(screen.getByText('Review'))
    const approve = screen.getByText('Approve').closest('button') as HTMLButtonElement
    await waitFor(() => expect(approve).not.toBeDisabled())
    fireEvent.click(approve)
    // The hedge stands for good: no false "already approved" (the target
    // being live proves nothing) and no false "already dismissed".
    await waitFor(() => expect(screen.getByRole('alert')).toBeInTheDocument())
    expect(screen.getByRole('alert').textContent).toContain('approved or removed')
    expect(screen.getByRole('alert').textContent).not.toContain('already approved')
    expect(screen.getByRole('alert').textContent).not.toContain('already dismissed')
  })

  it('keeps the open wording while the live-list refetch is still in flight', async () => {
    mockApi.skillsPending
      .mockResolvedValueOnce({ pending: [FLAGGED_ROW] })
      .mockResolvedValue({ pending: [] })
    // The refetch never settles: the app does not yet know which way the
    // candidate went, so the hedge is the honest sentence — and no outcome
    // may be asserted off the stale (empty) cache in the meantime.
    mockApi.skills.mockResolvedValueOnce([]).mockReturnValue(new Promise<never>(() => {}))
    mockApi.approvePendingSkill.mockRejectedValue(
      Object.assign(new Error('pending skill not found'), {
        status: 404,
        body: JSON.stringify({ error: 'pending skill not found', code: 'pending_skill_not_found' }),
      }),
    )
    renderWithQuery()
    await waitFor(() => expect(screen.getByText('auto/evil-helper')).toBeInTheDocument())
    fireEvent.click(screen.getByText('Review'))
    const approve = screen.getByText('Approve').closest('button') as HTMLButtonElement
    await waitFor(() => expect(approve).not.toBeDisabled())
    fireEvent.click(approve)
    await waitFor(() => expect(screen.getByText(HEDGED)).toBeInTheDocument())
    await waitFor(() => expect(mockApi.skills.mock.calls.length).toBeGreaterThan(1))
    // Still pending: the hedge stands, unresolved.
    expect(screen.getByText(HEDGED)).toBeInTheDocument()
    expect(screen.queryByText(SETTLED_DISMISSED)).not.toBeInTheDocument()
    expect(screen.queryByText(SETTLED_APPROVED)).not.toBeInTheDocument()
  })

  it('a failed live-list fetch keeps the open wording instead of claiming a dismissal', async () => {
    mockApi.skillsPending
      .mockResolvedValueOnce({ pending: [FLAGGED_ROW] })
      .mockResolvedValue({ pending: [] })
    // Initial load succeeds (the tab renders); the REFETCH the refusal branch
    // triggers fails, so `data` falls back to the empty cache — which must
    // NOT be presented as "not approved". The notice keeps its open wording.
    mockApi.skills.mockResolvedValueOnce([]).mockRejectedValue(new Error('boom'))
    mockApi.approvePendingSkill.mockRejectedValue(
      Object.assign(new Error('pending skill not found'), {
        status: 404,
        body: JSON.stringify({ error: 'pending skill not found', code: 'pending_skill_not_found' }),
      }),
    )
    renderWithQuery()
    await waitFor(() => expect(screen.getByText('auto/evil-helper')).toBeInTheDocument())
    fireEvent.click(screen.getByText('Review'))
    const approve = screen.getByText('Approve').closest('button') as HTMLButtonElement
    await waitFor(() => expect(approve).not.toBeDisabled())
    fireEvent.click(approve)
    await waitFor(() => expect(screen.getByText(HEDGED)).toBeInTheDocument())
    // Wait for the failed refetch to have happened, then re-check: the hedge
    // must SURVIVE the failure, not just precede the answer.
    await waitFor(() => expect(mockApi.skills.mock.calls.length).toBeGreaterThan(1))
    expect(screen.getByText(HEDGED)).toBeInTheDocument()
    expect(screen.queryByText(SETTLED_DISMISSED)).not.toBeInTheDocument()
    expect(screen.queryByText(SETTLED_APPROVED)).not.toBeInTheDocument()
  })

  it('the panel notice is evicted when its subject slug is restaged', async () => {
    // Fetch 1 renders the card; fetch 2 (after the refusal) is empty; fetch 3
    // brings a RESTAGED candidate under the same slug with a new created_at —
    // the stale "no longer pending" notice must not sit above it.
    mockApi.skillsPending
      .mockResolvedValueOnce({ pending: [FLAGGED_ROW] })
      .mockResolvedValueOnce({ pending: [] })
      .mockResolvedValue({ pending: [{ ...FLAGGED_ROW, created_at: '2026-09-15T21:00:00Z' }] })
    mockApi.approvePendingSkill.mockRejectedValue(
      Object.assign(new Error('pending skill not found'), {
        status: 404,
        body: JSON.stringify({ error: 'pending skill not found', code: 'pending_skill_not_found' }),
      }),
    )
    renderWithQuery()
    await waitFor(() => expect(screen.getByText('auto/evil-helper')).toBeInTheDocument())
    fireEvent.click(screen.getByText('Review'))
    const approve = screen.getByText('Approve').closest('button') as HTMLButtonElement
    await waitFor(() => expect(approve).not.toBeDisabled())
    fireEvent.click(approve)
    // The live list (mocked empty) settles, so the notice reads as dismissed.
    await waitFor(() => expect(screen.getByText(SETTLED_DISMISSED)).toBeInTheDocument())
    // The restage poll returns the slug with a fresh created_at.
    await act(() => qcRef!.invalidateQueries({ queryKey: ['skills-pending'] }))
    await waitFor(() => expect(screen.getByText('auto/evil-helper')).toBeInTheDocument())
    await waitFor(() => expect(screen.queryByText(SETTLED_DISMISSED)).not.toBeInTheDocument())
    expect(screen.queryByText(HEDGED)).not.toBeInTheDocument()
  })

  it('names sibling rows a recovery refetch dropped, in a polite live region', async () => {
    // The queue holds the acted-on candidate AND a sibling. A not-found approve
    // triggers the recovery refetch, which returns a queue missing BOTH: the
    // subject (resolved by the click) and the sibling (resolved out-of-band in
    // the same window). The subject's fate is narrated by the panel notice; the
    // sibling would otherwise vanish with no trace, so a muted line names it.
    mockApi.skillsPending
      .mockResolvedValueOnce({ pending: [FLAGGED_ROW, CLEAN_ROW] })
      .mockResolvedValue({ pending: [] })
    // Live list has neither, so the subject resolves to a dismissal.
    mockApi.skills.mockResolvedValue([])
    mockApi.approvePendingSkill.mockRejectedValue(
      Object.assign(new Error('pending skill not found'), {
        status: 404,
        body: JSON.stringify({ error: 'pending skill not found', code: 'pending_skill_not_found' }),
      }),
    )
    renderWithQuery()
    // Skills query must settle before the pending panel mounts and runs the
    // pending-list query that renders this row — give the chain its ceiling.
    await waitFor(() => expect(screen.getByText('auto/evil-helper')).toBeInTheDocument(), {
      timeout: CHAINED_QUERY_TIMEOUT_MS,
    })
    // Two rows each render a Review and a (disabled-until-open) Approve; scope
    // to the FLAGGED row's own container so we act only on it.
    const subjectRow = screen.getByText('auto/evil-helper').closest('div.rounded-md.border') as HTMLElement
    fireEvent.click(within(subjectRow).getByText('Review'))
    const approve = within(subjectRow).getByText('Approve').closest('button') as HTMLButtonElement
    // The detail query starts only once Review opens the row.
    await waitFor(() => expect(approve).not.toBeDisabled(), { timeout: CHAINED_QUERY_TIMEOUT_MS })
    fireEvent.click(approve)
    // The departed sibling is named in a muted line …
    await waitFor(() =>
      expect(
        screen.getByText(
          '1 other candidate also left the queue — approved or removed in another window or by your Kiro Crew agent: auto/good-helper.',
        ),
      ).toBeInTheDocument(),
    )
    // … inside a POLITE live region (announced, no focus theft) …
    const line = screen.getByText(
      '1 other candidate also left the queue — approved or removed in another window or by your Kiro Crew agent: auto/good-helper.',
    )
    const region = line.closest('[aria-live]') as HTMLElement
    expect(region).not.toBeNull()
    expect(region).toHaveAttribute('aria-live', 'polite')
    // … and the subject itself is narrated by the panel notice, never counted
    // among the departed siblings.
    expect(screen.getByText(SETTLED_DISMISSED)).toBeInTheDocument()
    expect(line.textContent).not.toContain('auto/evil-helper')
  })

  it('shows no sibling line when the acted-on candidate was the only row', async () => {
    // A lone row leaving is the subject's own departure, narrated by the panel
    // notice — there is no sibling to announce, so the muted line never renders.
    mockApi.skillsPending
      .mockResolvedValueOnce({ pending: [FLAGGED_ROW] })
      .mockResolvedValue({ pending: [] })
    mockApi.skills.mockResolvedValue([])
    mockApi.approvePendingSkill.mockRejectedValue(
      Object.assign(new Error('pending skill not found'), {
        status: 404,
        body: JSON.stringify({ error: 'pending skill not found', code: 'pending_skill_not_found' }),
      }),
    )
    renderWithQuery()
    // Skills → pending-list chain settles before this row renders.
    await waitFor(() => expect(screen.getByText('auto/evil-helper')).toBeInTheDocument(), {
      timeout: CHAINED_QUERY_TIMEOUT_MS,
    })
    fireEvent.click(screen.getByText('Review'))
    const approve = screen.getByText('Approve').closest('button') as HTMLButtonElement
    // The detail query starts only once Review opens the row.
    await waitFor(() => expect(approve).not.toBeDisabled(), { timeout: CHAINED_QUERY_TIMEOUT_MS })
    fireEvent.click(approve)
    await waitFor(() => expect(screen.getByText(SETTLED_DISMISSED)).toBeInTheDocument())
    expect(screen.queryByText(/also left the queue/)).not.toBeInTheDocument()
  })

  it('never attributes a sibling the user later resolves HERE to the recovery refetch', async () => {
    // The departed set is FROZEN at the first settled refetch. Queue A + B;
    // approving A hits not-found and snapshots siblings [B], but the recovery
    // refetch still shows B (nothing left out-of-band), so the frozen departed
    // set is empty and no line renders. The user THEN dismisses B here and it
    // succeeds: B leaves the live queue by the user's own action. A naive
    // re-diff against the live list would now announce "B was also removed in
    // another window", a lie about a local action — the freeze must prevent it.
    mockApi.skillsPending
      .mockResolvedValueOnce({ pending: [FLAGGED_ROW, CLEAN_ROW] }) // initial
      .mockResolvedValueOnce({ pending: [FLAGGED_ROW, CLEAN_ROW] }) // recovery refetch: B still here
      .mockResolvedValue({ pending: [FLAGGED_ROW] })                // after the local dismiss of B
    mockApi.skills.mockResolvedValue([])
    mockApi.approvePendingSkill.mockRejectedValue(
      Object.assign(new Error('pending skill not found'), {
        status: 404,
        body: JSON.stringify({ error: 'pending skill not found', code: 'pending_skill_not_found' }),
      }),
    )
    mockApi.dismissPendingSkill.mockResolvedValue({ dismissed: true })
    renderWithQuery()
    // Skills → pending-list chain settles before this row renders.
    await waitFor(() => expect(screen.getByText('auto/evil-helper')).toBeInTheDocument(), {
      timeout: CHAINED_QUERY_TIMEOUT_MS,
    })
    const subjectRow = screen.getByText('auto/evil-helper').closest('div.rounded-md.border') as HTMLElement
    fireEvent.click(within(subjectRow).getByText('Review'))
    const approve = within(subjectRow).getByText('Approve').closest('button') as HTMLButtonElement
    // The detail query starts only once Review opens the row.
    await waitFor(() => expect(approve).not.toBeDisabled(), { timeout: CHAINED_QUERY_TIMEOUT_MS })
    fireEvent.click(approve)
    // The notice settles; B was still pending at the refetch, so no line.
    await waitFor(() => expect(screen.getByText(SETTLED_DISMISSED)).toBeInTheDocument())
    expect(screen.queryByText(/also left the queue/)).not.toBeInTheDocument()
    // Now dismiss B HERE. window.confirm is stubbed to proceed.
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)
    const siblingRow = screen.getByText('auto/good-helper').closest('div.rounded-md.border') as HTMLElement
    fireEvent.click(within(siblingRow).getByText('Dismiss'))
    // B leaves the live queue by the user's own successful dismiss …
    await waitFor(() => expect(screen.queryByText('auto/good-helper')).not.toBeInTheDocument())
    // … and is NEVER announced as removed-elsewhere: the frozen set stays empty.
    expect(screen.queryByText(/also left the queue/)).not.toBeInTheDocument()
    confirmSpy.mockRestore()
  })

  it('excludes a sibling resolved HERE even when a later not-found reads a stale queue', async () => {
    // The race GPT/Opus flagged: B is dismissed locally and leaves the queue,
    // then a not-found approve on A captures its sibling list. The name of a
    // locally-resolved candidate is remembered independently of panelError
    // (captured in onMutate, held in a ref), so A's capture excludes B even
    // though B's success handler could not look it up in the live queue.
    mockApi.skillsPending
      .mockResolvedValueOnce({ pending: [FLAGGED_ROW, CLEAN_ROW] }) // initial
      .mockResolvedValueOnce({ pending: [FLAGGED_ROW] })            // after B dismissed locally
      .mockResolvedValue({ pending: [] })                           // after A's not-found refetch
    mockApi.skills.mockResolvedValue([])
    mockApi.approvePendingSkill.mockRejectedValue(
      Object.assign(new Error('pending skill not found'), {
        status: 404,
        body: JSON.stringify({ error: 'pending skill not found', code: 'pending_skill_not_found' }),
      }),
    )
    mockApi.dismissPendingSkill.mockResolvedValue({ dismissed: true })
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderWithQuery()
    // Skills → pending-list chain settles before this row renders.
    await waitFor(() => expect(screen.getByText('auto/good-helper')).toBeInTheDocument(), {
      timeout: CHAINED_QUERY_TIMEOUT_MS,
    })
    // Dismiss B locally; its name is recorded as resolved-here.
    const siblingRow = screen.getByText('auto/good-helper').closest('div.rounded-md.border') as HTMLElement
    fireEvent.click(within(siblingRow).getByText('Dismiss'))
    await waitFor(() => expect(screen.queryByText('auto/good-helper')).not.toBeInTheDocument())
    // Now approve A → not-found. Its sibling capture must exclude B.
    const subjectRow = screen.getByText('auto/evil-helper').closest('div.rounded-md.border') as HTMLElement
    fireEvent.click(within(subjectRow).getByText('Review'))
    const approve = within(subjectRow).getByText('Approve').closest('button') as HTMLButtonElement
    // The detail query starts only once Review opens the row.
    await waitFor(() => expect(approve).not.toBeDisabled(), { timeout: CHAINED_QUERY_TIMEOUT_MS })
    fireEvent.click(approve)
    await waitFor(() => expect(screen.getByText(SETTLED_DISMISSED)).toBeInTheDocument())
    // B was resolved here, so it is never attributed to another window.
    expect(screen.queryByText(/also left the queue/)).not.toBeInTheDocument()
    confirmSpy.mockRestore()
  })

  it('Dismiss All records its candidates as resolved-here, so none is announced as removed elsewhere', async () => {
    // Dismiss All is a local resolution of EVERY pending candidate. Its success
    // handler records them all in the resolved-here set (so a racing not-found
    // capture can never name one on the sibling line) and empties the queue —
    // the panel closes with no sibling line ever shown.
    mockApi.skillsPending
      .mockResolvedValueOnce({ pending: [FLAGGED_ROW, CLEAN_ROW] }) // initial
      .mockResolvedValue({ pending: [] })                           // after Dismiss All
    mockApi.skills.mockResolvedValue([])
    mockApi.dismissAllPendingSkills.mockResolvedValue({ dismissed_count: 2 })
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderWithQuery()
    // Skills → pending-list chain settles before this row renders.
    await waitFor(() => expect(screen.getByText('auto/good-helper')).toBeInTheDocument(), {
      timeout: CHAINED_QUERY_TIMEOUT_MS,
    })
    fireEvent.click(screen.getByText('Dismiss All'))
    await waitFor(() => expect(mockApi.dismissAllPendingSkills).toHaveBeenCalled())
    // The queue empties and no out-of-band sibling line is ever announced for a
    // candidate the user bulk-dismissed here.
    await waitFor(() => expect(screen.queryByText('auto/good-helper')).not.toBeInTheDocument())
    expect(screen.queryByText(/also left the queue/)).not.toBeInTheDocument()
    confirmSpy.mockRestore()
  })

  it('excludes a sibling whose local dismiss is STILL IN FLIGHT when the recovery refetch settles', async () => {
    // The invariant's reason for existence: a name enters the resolved-here set
    // at onMutate — before the request leaves — so a not-found approve whose
    // recovery refetch settles WHILE B's dismiss is still pending still
    // excludes B. B's dismiss is held open here; A's not-found refetch returns
    // a queue without B and freezes departed; B must not be on the line even
    // though B's own success has not returned yet.
    let releaseDismiss!: (v: unknown) => void
    const dismissPending = new Promise(res => { releaseDismiss = res })
    mockApi.skillsPending
      .mockResolvedValueOnce({ pending: [FLAGGED_ROW, CLEAN_ROW] }) // initial
      .mockResolvedValue({ pending: [] })                           // A's not-found refetch: both gone
    mockApi.skills.mockResolvedValue([])
    mockApi.dismissPendingSkill.mockReturnValue(dismissPending)     // B's dismiss hangs
    mockApi.approvePendingSkill.mockRejectedValue(
      Object.assign(new Error('pending skill not found'), {
        status: 404,
        body: JSON.stringify({ error: 'pending skill not found', code: 'pending_skill_not_found' }),
      }),
    )
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderWithQuery()
    // Skills → pending-list chain settles before this row renders.
    await waitFor(() => expect(screen.getByText('auto/good-helper')).toBeInTheDocument(), {
      timeout: CHAINED_QUERY_TIMEOUT_MS,
    })
    // Start dismissing B — onMutate marks B resolving-locally; the request hangs.
    const siblingRow = screen.getByText('auto/good-helper').closest('div.rounded-md.border') as HTMLElement
    fireEvent.click(within(siblingRow).getByText('Dismiss'))
    // Approve A → not-found; its recovery refetch (empty) settles while B is
    // still in flight and freezes departed.
    const subjectRow = screen.getByText('auto/evil-helper').closest('div.rounded-md.border') as HTMLElement
    fireEvent.click(within(subjectRow).getByText('Review'))
    const approve = within(subjectRow).getByText('Approve').closest('button') as HTMLButtonElement
    // The detail query starts only once Review opens the row.
    await waitFor(() => expect(approve).not.toBeDisabled(), { timeout: CHAINED_QUERY_TIMEOUT_MS })
    fireEvent.click(approve)
    await waitFor(() => expect(screen.getByText(SETTLED_DISMISSED)).toBeInTheDocument())
    // B was marked at onMutate, so it is excluded even though its dismiss has
    // not returned — no false "removed in another window" for the user's action.
    expect(screen.queryByText(/also left the queue/)).not.toBeInTheDocument()
    // Let B's dismiss finish; still no sibling line.
    await act(async () => { releaseDismiss({ dismissed: true }); await dismissPending })
    expect(screen.queryByText(/also left the queue/)).not.toBeInTheDocument()
    confirmSpy.mockRestore()
  })
})
