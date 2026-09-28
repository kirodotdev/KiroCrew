/**
 * CrewList — column 2 of the crews surface.
 *
 * What is asserted, and what deliberately is not: the roster is queried by CREW
 * NAME (data the test supplies) and by test id, never by rendered English. The
 * `apps.issueRadar.views.crews.*` catalog keys are populated in a separate change,
 * so an assertion on copy here would be an assertion about the state of the
 * translation files rather than about this component.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { Crew } from '../apps/issue-radar/api'

const ctx = { value: {} as Record<string, unknown> }
vi.mock('../apps/issue-radar/context', () => ({
  useIssueRadar: () => ctx.value,
}))

const CrewList = (await import('../apps/issue-radar/components/CrewList')).default

const setCrewView = vi.fn()
const setCrewFilter = vi.fn()
const onCreate = vi.fn()

/** A crew record with only the fields this list reads; `status` is the field the
 * crews ROUTE adds on top of the store record (see `_crew_status` in
 * `crew_routes.py`), which is why it is cast rather than declared. */
const crew = (over: Partial<Crew> & { status?: string }): Crew => ({
  schema: 1,
  id: 'c1',
  name: 'Andromeda',
  avatar_seed: 'Andromeda',
  avatar_variant: null,
  agent: 'kirocrew',
  model: '',
  extra_prompt: '',
  labels: [],
  auto_resolve_conflicts: false,
  auto_merge: false,
  unattended: false,
  max_open: 3,
  worktree_root: '',
  slot_key: '',
  enabled: true,
  paused_reason: '',
  created_at: '2026-08-01T00:00:00Z',
  retired_at: null,
  ...over,
} as Crew)

const ROSTER = [
  crew({ id: 'c1', name: 'Andromeda', status: 'working', labels: ['area: dashboard'] }),
  crew({ id: 'c2', name: 'Whirlpool', status: 'idle' }),
  crew({ id: 'c3', name: 'Triangulum', status: 'paused', enabled: false, paused_reason: 'Paused by you' }),
]

beforeEach(() => {
  vi.clearAllMocks()
  ctx.value = {
    crews: ROSTER,
    crewsLoading: false,
    crewsError: null,
    crewView: { kind: 'crew', id: 'c1' },
    setCrewView,
    // Selecting a row also drills into the detail on a narrow viewport, so the
    // fake context has to carry the pane state the real one hosts.
    listDetail: { isMobile: false, showList: true, showDetail: true, openDetail: vi.fn(), closeDetail: vi.fn() },
    crewFilter: 'all',
    setCrewFilter,
    // The sort controls live in the rail; this column only applies the result.
    crewSortKey: 'status',
    crewSortDir: 'asc',
  }
})

describe('CrewList', () => {
  it('marks the selected crew as current, not merely styled', () => {
    // aria-current is what a screen reader announces; a background colour alone
    // leaves the selection invisible to one.
    ctx.value = { ...ctx.value, crewView: { kind: 'crew', id: 'c2' } }
    render(<CrewList onCreate={onCreate} />)
    expect(screen.getByTestId('crew-row-c2').getAttribute('aria-current')).toBe('page')
    expect(screen.getByTestId('crew-row-c1').getAttribute('aria-current')).toBeNull()
  })

  it('selecting a row addresses that crew by id', async () => {
    render(<CrewList onCreate={onCreate} />)
    await userEvent.click(screen.getByText('Whirlpool'))
    expect(setCrewView).toHaveBeenCalledWith({ kind: 'crew', id: 'c2' })
  })

  it('offers the create control above the roster, and only reports the intent', async () => {
    // This column owns the ONE way to hire a crew; the dialog itself lives in
    // Workspace, so the button must not try to open anything itself.
    render(<CrewList onCreate={onCreate} />)
    const create = screen.getByTestId('crew-create')
    const first = screen.getByTestId('crew-row-c1')
    // DOCUMENT_POSITION_FOLLOWING — the first crew comes after the control.
    expect(create.compareDocumentPosition(first) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    await userEvent.click(create)
    expect(onCreate).toHaveBeenCalledTimes(1)
    expect(setCrewView).not.toHaveBeenCalled()
  })

  it('keeps the create control reachable under a filter that hides every row', () => {
    // Hiring is how an empty or fully-filtered roster is recovered from, so it
    // cannot be filtered away with the rows.
    ctx.value = { ...ctx.value, crews: [ROSTER[0]], crewFilter: 'paused' }
    const { unmount } = render(<CrewList onCreate={onCreate} />)
    expect(screen.queryByTestId('crew-row-c1')).toBeNull()
    expect(screen.getByTestId('crew-create')).toBeTruthy()
    unmount()

    // And on a repo with no crews at all, where it is the only thing to do.
    ctx.value = { ...ctx.value, crews: [], crewFilter: 'all' }
    render(<CrewList onCreate={onCreate} />)
    expect(screen.getByTestId('crew-create')).toBeTruthy()
  })

  it('orders the roster by the active sort and flips with its direction', () => {
    // The CONTROLS moved to the rail, but applying the order is still this
    // column's job. Ascending `status` is the activity order — a crew with work in
    // flight leads, and a paused one sinks.
    ctx.value = { ...ctx.value, crewSortKey: 'status', crewSortDir: 'asc' }
    const { unmount } = render(<CrewList onCreate={onCreate} />)
    const idsOf = () => screen.getAllByTestId(/^crew-row-c/).map((n) => n.getAttribute('data-testid'))
    const asc = idsOf()
    unmount()

    ctx.value = { ...ctx.value, crewSortDir: 'desc' }
    render(<CrewList onCreate={onCreate} />)
    expect(idsOf()).toEqual([...asc].reverse())
  })

  it('the Paused filter reads the crew record, so its rows match its count', () => {
    // `paused` is derived from enabled/retired_at — the backend's own flag — rather
    // than from the single-valued `status`, which is what keeps the chip's tally and
    // the rows it shows in agreement.
    ctx.value = { ...ctx.value, crewFilter: 'paused' }
    render(<CrewList onCreate={onCreate} />)
    expect(screen.getByTestId('crew-row-c3')).toBeTruthy()
    expect(screen.queryByTestId('crew-row-c1')).toBeNull()
    expect(screen.queryByTestId('crew-row-c2')).toBeNull()
  })

  it('a paused crew shows its reason rather than a generic label', () => {
    render(<CrewList onCreate={onCreate} />)
    expect(screen.getByText('Paused by you')).toBeTruthy()
  })

  it('empties to the roster empty state, and to a filtered one when rows are hidden', () => {
    const { unmount } = render(<CrewList onCreate={onCreate} />)
    expect(screen.queryByTestId('crew-list-empty')).toBeNull()
    unmount()

    // Nothing matches the filter, but the repo does have crews.
    ctx.value = { ...ctx.value, crews: [ROSTER[0]], crewFilter: 'paused' }
    const filtered = render(<CrewList onCreate={onCreate} />)
    expect(screen.getByTestId('crew-list-empty')).toBeTruthy()
    const filteredTitle = screen.getByTestId('crew-list-empty-title').textContent
    filtered.unmount()

    // No crews at all — a different message, since there is nothing to unfilter.
    ctx.value = { ...ctx.value, crews: [], crewFilter: 'all' }
    render(<CrewList onCreate={onCreate} />)
    expect(screen.getByTestId('crew-list-empty-title').textContent).not.toBe(filteredTitle)
  })

  it('surfaces a load failure instead of rendering an empty roster', () => {
    ctx.value = { ...ctx.value, crews: [], crewsError: new Error('crew store unreadable') }
    render(<CrewList onCreate={onCreate} />)
    expect(screen.getByText('crew store unreadable')).toBeTruthy()
    // An error is not an empty roster: offering the "you have no crews yet"
    // pitch here would invite creating one when the store simply could not be read.
    expect(screen.queryByTestId('crew-list-empty')).toBeNull()
  })
})
