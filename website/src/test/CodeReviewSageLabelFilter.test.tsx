// Filtering the PR list by GitHub label (issue #3788).
//
// The list is the review queue, so the property that matters is not "the chips
// work" but "nothing is hidden that the user did not ask to hide". Every case
// below therefore asserts the WHOLE visible set, not just that one expected PR
// is present: a test that only looks for its subject passes just as well when a
// second, unrelated PR has silently vanished from the queue.
//
// This drives the REAL provider and the REAL PrPickList rather than
// re-implementing the narrowing in the test body.
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { SageProvider, useSage } from '../apps/code-review-sage/context'
import PrPickList from '../apps/code-review-sage/components/PrPickList'
import { sageApi } from '../apps/code-review-sage/api'

vi.mock('../apps/code-review-sage/api', () => ({
  sageApi: {
    runs: vi.fn(),
    runReport: vi.fn(),
    settings: vi.fn(),
    pinnedRepos: vi.fn(),
    recentRepos: vi.fn(),
    repoPrs: vi.fn(),
    myRepos: vi.fn(),
    learnings: vi.fn(),
  },
}))

const mockApi = sageApi as unknown as Record<string, ReturnType<typeof vi.fn>>

/** Four PRs over three labels, deliberately not a one-to-one mapping:
 *  - `docs` alone            -> #1
 *  - `docs` AND `typescript` -> #2   (so OR and AND give different answers)
 *  - `typescript` alone      -> #3
 *  - no labels at all        -> #4   (the row every narrowing must hide)
 *  Four rows and overlapping sets are the minimum that can tell an OR filter
 *  from an AND one and from a filter keyed on the wrong row. */
function pr(number: number, title: string, labels: string[]) {
  return {
    url: `https://github.com/o/r/pull/${number}`,
    number,
    title,
    head_sha: `sha${number}`,
    author: 'ann',
    updated_at: '2026-07-01T00:00:00Z',
    draft: false,
    change_id: `CR-${number}`,
    reviewed: false,
    reviewed_stale: false,
    labels,
  }
}

const PRS = [
  pr(1, 'alpha only docs', ['docs']),
  pr(2, 'bravo docs and ts', ['docs', 'typescript']),
  pr(3, 'charlie only ts', ['typescript']),
  pr(4, 'delta unlabelled', []),
]

const ALL_TITLES = PRS.map((p) => p.title)

/** Every PR title currently rendered, read off the row buttons' accessible
 *  names. Locating by the row's own identity — never by the title a case is
 *  about to assert — is what stops a case passing while it reads a different,
 *  healthy row. */
function visibleTitles(): string[] {
  return screen.getAllByRole('button', { name: /^Open pull request #/ })
    .map((b) => (b.getAttribute('aria-label') ?? '').replace(/^Open pull request #\d+: /, ''))
}

/** The collapsed trigger that opens the label picker. There is deliberately no
 *  chip per label: `AUTOSDE.yaml`'s `max-two-buttons-per-row` caps a horizontal
 *  group at two buttons, so the labels live behind one control. */
function labelTrigger() {
  // The trigger's accessible name comes from an EXISTING catalog key rather
  // than a new one; see the component for why.
  return screen.getByRole('button', { name: 'Labels' })
}

/** Open the picker if it is not already open, then return a label's menu item.
 *  Radix keeps the menu open across selections (the component prevents the
 *  default dismiss), so a multi-label case does not have to re-open it. */
// Radix opens a DropdownMenu on `pointerdown`, not on `click`, and jsdom
// implements neither the pointer-capture APIs Radix probes nor
// `scrollIntoView`, which its focus management calls. Both stubs are local to
// this file on purpose: a shared setup shim would change how every other
// suite's Radix components behave.
if (!Element.prototype.hasPointerCapture) {
  Element.prototype.hasPointerCapture = () => false
  Element.prototype.setPointerCapture = () => {}
  Element.prototype.releasePointerCapture = () => {}
}
Element.prototype.scrollIntoView ??= () => {}

async function openPicker() {
  if (!screen.queryByRole('menu')) {
    // `button: 0` and no modifier keys: Radix ignores a secondary or
    // ctrl-clicked pointerdown, so a bare event would silently not open it.
    fireEvent.pointerDown(labelTrigger(), { button: 0, ctrlKey: false, pointerId: 1 })
  }
  await waitFor(() => expect(screen.getByRole('menu')).toBeTruthy())
}

async function pickLabel(name: string) {
  await openPicker()
  fireEvent.click(screen.getByRole('menuitem', { name: new RegExp(`^${name}\\b`) }))
  // The menu is MODAL, as every dropdown in this app is, so while it is open
  // Radix marks the list behind it aria-hidden and a role query cannot see the
  // rows. Dismissing first is also what a real user does: pick, close, read the
  // narrowed queue. Asserting it is still open BEFORE dismissing pins the
  // multi-select behaviour -- without the preventDefault in the component,
  // Radix would have closed it on select and this would fail.
  expect(screen.queryByRole('menu')).toBeTruthy()
  fireEvent.keyDown(screen.getByRole('menu'), { key: 'Escape' })
  await waitFor(() => expect(screen.queryByRole('menu')).toBeNull())
}

function labelItem(name: string) {
  return screen.getByRole('menuitem', { name: new RegExp(`^${name}\\b`) })
}

function Harness() {
  const { setActiveRepo, activeRepo } = useSage()
  return (
    <div>
      <button onClick={() => setActiveRepo({ owner: 'o', repo: 'r' })}>activate</button>
      <button onClick={() => setActiveRepo({ owner: 'o', repo: 'other' })}>activate other</button>
      <span data-testid="active">{activeRepo ? `${activeRepo.owner}/${activeRepo.repo}` : 'none'}</span>
      <PrPickList />
    </div>
  )
}

function mount(node = <Harness />) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } },
  })
  return render(
    <MemoryRouter>
      <QueryClientProvider client={qc}>
        <SageProvider initialRunId={null}>{node}</SageProvider>
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

/** Activates the repo and waits for its PRs to land. */
async function armed(prs = PRS) {
  mockApi.repoPrs.mockResolvedValue({ repo: 'o/r', prs, count: prs.length })
  mount()
  await userEvent.click(screen.getByText('activate'))
  await waitFor(() => expect(visibleTitles()).toHaveLength(prs.length))
}

describe('Code Review Sage: filter pull requests by label', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
    mockApi.runs.mockResolvedValue({ runs: [] })
    mockApi.runReport.mockResolvedValue({ ready: false })
    mockApi.settings.mockResolvedValue({ settings: {}, pool: null, reviewer: null })
    mockApi.recentRepos.mockResolvedValue({ repos: [] })
    mockApi.myRepos.mockResolvedValue({ repos: [] })
    mockApi.learnings.mockResolvedValue({ learnings: [] })
    // The provider drops an active repo that is not pinned, so both repos the
    // switch case uses have to be here.
    mockApi.pinnedRepos.mockResolvedValue({
      repos: [{ owner: 'o', repo: 'r' }, { owner: 'o', repo: 'other' }],
    })
    mockApi.repoPrs.mockResolvedValue({ repo: 'o/r', prs: PRS, count: PRS.length })
  })

  it('offers one chip per distinct label, counted, and none for an unlabelled PR', async () => {
    await armed()
    await openPicker()
    expect(labelItem('docs')).toBeTruthy()
    expect(labelItem('typescript')).toBeTruthy()
    // Counts are PRs carrying the label: docs on #1 and #2, typescript on #2/#3.
    expect(labelItem('docs').textContent).toContain('2')
    expect(labelItem('typescript').textContent).toContain('2')
    // The unlabelled PR contributes no chip, and nothing invents one.
    expect(screen.queryByRole('button', { name: /^delta\b/ })).toBeNull()
  })

  it('narrows to the PRs carrying the selected label and hides the rest', async () => {
    await armed()
    await pickLabel('docs')
    await waitFor(() => expect(visibleTitles().sort())
      .toEqual(['alpha only docs', 'bravo docs and ts']))
    // The complement is the real assertion: charlie and delta must be GONE, not
    // merely un-asserted.
    expect(visibleTitles()).not.toContain('charlie only ts')
    expect(visibleTitles()).not.toContain('delta unlabelled')
    expect(labelTrigger().textContent).toContain('docs')
  })

  it('ORs two selected labels rather than requiring both', async () => {
    await armed()
    await pickLabel('docs')
    await pickLabel('typescript')
    // OR gives three PRs. AND would give only #2, which is why the fixture has a
    // PR carrying both and PRs carrying one each.
    await waitFor(() => expect(visibleTitles().sort())
      .toEqual(['alpha only docs', 'bravo docs and ts', 'charlie only ts']))
    expect(visibleTitles()).not.toContain('delta unlabelled')
  })

  it('restores the full list when the last selected label is toggled off', async () => {
    await armed()
    await pickLabel('docs')
    await waitFor(() => expect(visibleTitles()).toHaveLength(2))
    await pickLabel('docs')
    // An empty selection means "no narrowing" — this is what stands in for an
    // "All" reset chip, so it has to be exercised.
    await waitFor(() => expect(visibleTitles().sort()).toEqual([...ALL_TITLES].sort()))
    await openPicker()
    expect(labelItem('docs')).toBeTruthy()
  })

  it('composes with the text filter instead of replacing it', async () => {
    await armed()
    await pickLabel('docs')
    await userEvent.type(screen.getByLabelText('Filter pull requests'), 'bravo')
    // Text AND label: docs narrows to #1/#2, "bravo" leaves #2.
    await waitFor(() => expect(visibleTitles()).toEqual(['bravo docs and ts']))
  })

  it('says the filter matched nothing, not that the repo has no open PRs', async () => {
    // A label that exists on a draft-free repo but matches nothing once the text
    // box also narrows: the empty state must blame the filter.
    await armed()
    await pickLabel('docs')
    await userEvent.type(screen.getByLabelText('Filter pull requests'), 'charlie')
    await waitFor(() =>
      expect(screen.getByText('No pull requests match your filter.')).toBeTruthy())
    expect(screen.queryByText('No open pull requests here.')).toBeNull()
  })

  it('drops the selection when the repo changes', async () => {
    await armed()
    await pickLabel('docs')
    await waitFor(() => expect(visibleTitles()).toHaveLength(2))

    // The other repo carries `docs` TOO -- which is the case that needs the
    // explicit clear. A repo with no such label would be covered by the
    // intersection guard alone, so this fixture is what makes the clear the only
    // thing standing between a switch and a silently pre-filtered queue.
    mockApi.repoPrs.mockResolvedValue({
      repo: 'o/other',
      prs: [pr(9, 'echo elsewhere', ['docs']), pr(10, 'foxtrot elsewhere', ['infra'])],
      count: 2,
    })
    await userEvent.click(screen.getByText('activate other'))
    await waitFor(() => expect(screen.getByTestId('active').textContent).toBe('o/other'))
    // Both PRs, not just the `docs` one: the new repo arrives unnarrowed.
    await waitFor(() => expect(visibleTitles().sort())
      .toEqual(['echo elsewhere', 'foxtrot elsewhere']))
    await openPicker()
    expect(labelItem('docs')).toBeTruthy()
  })

  it('keeps every chip offered while the text box narrows, and only moves the counts', async () => {
    // The chip row is derived from the FULL list on purpose. If it were derived
    // from the narrowed one, typing could remove the very chip that is selected —
    // leaving a filter active with no control left to clear it.
    await armed()
    await pickLabel('typescript')
    await userEvent.type(screen.getByLabelText('Filter pull requests'), 'charlie')
    await waitFor(() => expect(visibleTitles()).toEqual(['charlie only ts']))
    // `docs` is on no visible PR now, yet it is still offered (at count 0) and
    // `typescript` is still there to be switched off.
    expect(labelTrigger().textContent).toContain('typescript')
    await openPicker()
    expect(labelItem('docs').textContent).toContain('0')
    // And clearing the selection still works from here.
    await pickLabel('typescript')
    await openPicker()
    await waitFor(() => expect(labelItem('typescript')).toBeTruthy())
  })

  it('shows no label control at all when nothing is labelled', async () => {
    await armed([pr(1, 'alpha', []), pr(2, 'bravo', [])])
    // The TRIGGER must be absent, not merely its items: a repo that does not use
    // labels should not gain a control that can only ever open an empty menu.
    // Asserting on menu items alone would pass either way, since a closed menu
    // renders none.
    expect(screen.queryByRole('button', { name: 'Labels' })).toBeNull()
    expect(screen.queryByRole('menuitem', { name: /^docs\b/ })).toBeNull()
    expect(visibleTitles().sort()).toEqual(['alpha', 'bravo'])
  })

  it('survives a cached PR row that predates the labels field', async () => {
    // `RepoPr.labels` is optional because lib/persist.ts serves a 24h snapshot as
    // initialData, so a row cached before this change carries NO labels key at
    // all -- not an empty array. Reading it without a fallback throws and takes
    // the whole review queue down, so the queue must still render and the
    // control must still offer the labels the other rows do carry.
    const stale = { ...pr(9, 'cached before labels existed', []) } as Record<string, unknown>
    delete stale.labels
    await armed([pr(1, 'alpha only docs', ['docs']), stale as ReturnType<typeof pr>])
    expect(visibleTitles().sort()).toEqual(['alpha only docs', 'cached before labels existed'])
    await pickLabel('docs')
    expect(visibleTitles()).toEqual(['alpha only docs'])
  })

  it('stops narrowing, rather than emptying the queue, when a label disappears', async () => {
    // Selecting a label and then refetching a list that no longer carries it is
    // what a deleted/renamed upstream label or a merged last PR looks like. The
    // wrong answer is an empty review queue, because that hides work.
    await armed()
    await pickLabel('docs')
    await waitFor(() => expect(visibleTitles()).toHaveLength(2))

    mockApi.repoPrs.mockResolvedValue({
      repo: 'o/r',
      prs: [pr(3, 'charlie only ts', ['typescript']), pr(4, 'delta unlabelled', [])],
      count: 2,
    })
    await userEvent.click(screen.getByLabelText('Refresh pull requests'))
    await waitFor(() => expect(visibleTitles().sort())
      .toEqual(['charlie only ts', 'delta unlabelled']))
    // `docs` is gone from the repo, so it neither narrows nor offers a chip.
    expect(screen.queryByRole('menuitem', { name: /^docs\b/ })).toBeNull()
  })
})
