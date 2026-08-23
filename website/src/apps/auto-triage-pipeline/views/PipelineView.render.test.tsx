/**
 * PipelineView — the RENDERED contract of the auto-triage pipeline board.
 *
 * `views/PipelineView.test.tsx` pins source-level invariants (the SVG coordinate
 * space, the longest-wait interpolation). This file renders the live component
 * against a mocked HTTP seam and asserts what an operator actually sees:
 *
 *  - a populated board draws one lane per work item, each stating its ISSUE
 *    identity (`IS-<n>`) with the PR carried separately as a `#<pr>` chip — the
 *    two must never be the same number wearing each other's label;
 *  - the hover card's phase table: one row per timeline stop, DWELL measured
 *    FORWARD into the next stop, the last stop blank, and an exited lane's exit
 *    as the closing row;
 *  - the NARROW branch, gated on the width the ResizeObserver reports for the
 *    TRACK (not a viewport breakpoint): driven by firing the observer with a
 *    small contentRect, the row stacks, the column-header labels vanish, and the
 *    lane names its own position in words — with the SPECIFIC exit token for an
 *    exited lane, so skipped / yielded / handed-back / preempted stay distinct;
 *  - the queue summary cards, including that EDITING flags danger only when a
 *    SINGLE crew holds more than one editing item (two crews with one each is
 *    legal and must not read as a fault);
 *  - the empty state (a connected repo with `items: []` is a 200, not an error).
 *
 * The one seam is `./api`, mocked so nothing dials. ResizeObserver and matchMedia
 * are stubbed because happy-dom has no layout engine — the FakeResizeObserver
 * lets us DRIVE the measured width the narrow gate reads, exactly the width the
 * component keys the branch off.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import type { CrewFabricItem, CrewFabricResponse, RepoRef } from '../api'

// ── the HTTP seam ────────────────────────────────────────────────────────────
// Declared through vi.hoisted so the vi.mock factory (itself hoisted to the top
// of the module) can close over the same fn instances the tests drive.
const { crewFabric, listConnectedRepos } = vi.hoisted(() => ({
  crewFabric: vi.fn<[RepoRef], Promise<CrewFabricResponse>>(),
  listConnectedRepos: vi.fn<[], Promise<unknown[]>>(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return {
    ...actual,
    autoTriagePipelineApi: { crewFabric, listConnectedRepos },
    // A first-ever visit has no stored preference, so the resolver falls back to
    // the connected list the mock returns.
    loadStoredPreference: () => null,
    saveRepoPreference: () => {},
  }
})

import PipelineView from './PipelineView'
import { CREW_FABRIC_SCHEMA } from '../api'

// ── ResizeObserver we can drive ───────────────────────────────────────────────
// happy-dom has no layout, so getBoundingClientRect().width is 0 (ignored by the
// hook's `px > 1` guard, leaving the seeded wide fallback). The real width only
// ever arrives through the ResizeObserver, so a fake one that records instances
// and lets a test fire a chosen contentRect.width is what drives BOTH branches:
// a wide value keeps the drawing, a small value crosses NARROW_TRACK_W.
class FakeResizeObserver {
  static instances: FakeResizeObserver[] = []
  disconnect = vi.fn()
  observe = vi.fn()
  unobserve = vi.fn()
  constructor(public cb: ResizeObserverCallback) {
    FakeResizeObserver.instances.push(this)
  }
  /** Report a track width to every observer, as the browser would on layout. */
  static emit(width: number) {
    for (const inst of FakeResizeObserver.instances) {
      inst.cb(
        [{ contentRect: { width } } as ResizeObserverEntry],
        inst as unknown as ResizeObserver,
      )
    }
  }
}

const realRO = globalThis.ResizeObserver
const realMatchMedia = globalThis.matchMedia

beforeEach(() => {
  FakeResizeObserver.instances = []
  globalThis.ResizeObserver = FakeResizeObserver as unknown as typeof ResizeObserver
  // Reduced motion ON so the SVG carries no <animateMotion>/<animate> pulse —
  // keeps the rendered tree stable and the assertions about it deterministic.
  globalThis.matchMedia = vi.fn().mockImplementation((query: string) => ({
    matches: true,
    media: query,
    onchange: null,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    addListener: vi.fn(),
    removeListener: vi.fn(),
    dispatchEvent: vi.fn(),
  })) as unknown as typeof matchMedia
  crewFabric.mockReset()
  listConnectedRepos.mockReset()
  listConnectedRepos.mockResolvedValue([{ owner: 'acme', repo: 'demo-repo' }])
  localStorage.clear()
})

afterEach(() => {
  globalThis.ResizeObserver = realRO
  globalThis.matchMedia = realMatchMedia
  vi.clearAllMocks()
})

// ── fixture helpers ───────────────────────────────────────────────────────────
const T = (h: number, m = 0) =>
  `2026-07-30T${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:00Z`

function item(over: Partial<CrewFabricItem> = {}): CrewFabricItem {
  return {
    number: 100,
    crew_id: 'crew-a',
    title: '',
    next: '',
    pr_number: null,
    phase: 'implementing',
    timeline: [{ phase: 'selected', at: T(1) }],
    reopens: 0,
    ...over,
  } as CrewFabricItem
}

function fabric(items: CrewFabricItem[], generatedAt: string | null = T(4)): CrewFabricResponse {
  return {
    schema: CREW_FABRIC_SCHEMA,
    owner: 'acme',
    repo: 'demo-repo',
    provider: 'github',
    host: 'github.com',
    generated_at: generatedAt,
    phases: [],
    items,
  }
}

function renderView() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  })
  return render(
    <QueryClientProvider client={qc}>
      <PipelineView />
    </QueryClientProvider>,
  )
}

/** Mount, wait for the board to actually populate (a lane row, not just the
 * repo-picker button in the chrome), then report the observed track width so the
 * wide/narrow branch is exercised. */
async function mountBoard(items: CrewFabricItem[], opts: { width?: number; generatedAt?: string | null } = {}) {
  crewFabric.mockResolvedValue(fabric(items, opts.generatedAt ?? T(4)))
  const utils = renderView()
  // Wait for the FIRST lane to render — the board has left its loading skeleton
  // and the ColumnHeader (the element the ResizeObserver is attached to) is
  // mounted, so an emit below reaches a live observer.
  const firstLabel = `IS-${items[0].number}`
  await waitFor(() => expect(screen.getByText(firstLabel)).toBeTruthy())
  FakeResizeObserver.emit(opts.width ?? 1500)
  return utils
}

/** The lane list item carrying a given `IS-<n>` label. */
const laneFor = (n: number) => screen.getByText(`IS-${n}`).closest('li') as HTMLElement

/** The <tbody> data rows of the hover card's phase table (the <thead> row of
 * column titles is excluded — it lives in its own <thead>). */
const tableBodyRows = (card: HTMLElement) =>
  Array.from(card.querySelectorAll('tbody tr')) as HTMLElement[]

/** The value cell of the summary StatCard whose label is EXACTLY `label`.
 * "Editing" also appears in the board legend, so a plain getByText is ambiguous;
 * the summary cards are the ones carrying a `stat-card-label` testid. */
function statCardValue(label: string): HTMLElement {
  const labelEl = screen
    .getAllByTestId('stat-card-label')
    .find((el) => (el.textContent ?? '').startsWith(label))
  if (!labelEl) throw new Error(`no summary StatCard labelled "${label}"`)
  const card = labelEl.closest('[data-testid="stat-card"]') as HTMLElement
  return within(card).getByTestId('stat-card-value')
}

describe('PipelineView — populated board', () => {
  it('draws one lane per work item, stating the issue identity with the PR as a separate chip', async () => {
    await mountBoard([
      item({ number: 5179, crew_id: 'crew-a', phase: 'implementing', pr_number: 5127, title: 'zzq alpha' }),
      item({ number: 4820, crew_id: 'crew-b', phase: 'awaiting-ci', pr_number: null, title: 'zzq beta' }),
      item({ number: 4711, crew_id: 'crew-c', phase: 'resolved', pr_number: 4700, title: 'zzq gamma' }),
    ])

    // One <li> per item — no more, no fewer.
    const rows = screen.getAllByText(/^IS-\d+$/).map((el) => el.closest('li'))
    expect(new Set(rows).size).toBe(3)

    // The identity line is the ISSUE number, and the PR rides as its OWN chip.
    // The regression this pins printed the PR number under the IS- label: here
    // 5179 is the issue and 5127 the PR, so the two numbers must appear on the
    // SAME row without the label claiming one is the other.
    const alpha = laneFor(5179)
    expect(within(alpha).getByText('IS-5179')).toBeTruthy()
    expect(within(alpha).getByText('#5127')).toBeTruthy()
    // The issue number is never rendered as a PR chip, nor the PR as the identity.
    expect(within(alpha).queryByText('IS-5127')).toBeNull()
    expect(within(alpha).queryByText('#5179')).toBeNull()

    // A lane with no PR shows no chip at all.
    expect(within(laneFor(4820)).queryByText(/^#\d+$/)).toBeNull()

    // The board meta strip counts the items it drew.
    expect(screen.getByText('WORK ITEMS').nextElementSibling?.textContent).toBe('3')
  })
})

describe('PipelineView — hover card phase table', () => {
  it('measures each row forward to the next stop, blanks the last, and closes an exited lane on its exit', async () => {
    // A lane that ran selected → claimed → implementing, then handed the claim
    // back. Stops (timeline + exit): selected@01, claimed@01:30, implementing@02,
    // handed-back@03. Forward dwells land on the row the time was spent IN:
    //   selected      → 30m   (01:00 → 01:30)
    //   claimed       → 30m   (01:30 → 02:00)
    //   implementing  → 60m   (02:00 → 03:00)  ← spent here before the exit
    //   handed-back   → (blank; it is the last stop)
    await mountBoard([
      item({
        number: 4400,
        crew_id: 'crew-a',
        phase: 'handed-back',
        pr_number: null,
        title: 'zzq handed',
        timeline: [
          { phase: 'selected', at: T(1) },
          { phase: 'claimed', at: T(1, 30) },
          { phase: 'implementing', at: T(2) },
        ],
        exit: { phase: 'handed-back', at: T(3) },
      }),
    ])

    // Hovering the row reveals the tooltip with the full phase table.
    fireEvent.mouseMove(laneFor(4400))
    const card = await screen.findByRole('tooltip')

    // One row per stop: the three spine phases PLUS the exit as the closing row.
    const bodyRows = tableBodyRows(card)
    expect(bodyRows.length).toBe(4)

    const cells = (r: HTMLElement) => within(r).getAllByRole('cell').map((c) => c.textContent)
    // phase | at | dwell  — dwell is the time spent IN that phase, forward.
    expect(cells(bodyRows[0])).toEqual(['selected', '01:00', '+30m'])
    expect(cells(bodyRows[1])).toEqual(['claimed', '01:30', '+30m'])
    expect(cells(bodyRows[2])).toEqual(['implementing', '02:00', '+60m'])
    // The exit is the LAST stop: it closes the table and its own dwell is blank
    // (nothing follows it), so the final spine phase is measured TO it rather
    // than left open.
    expect(cells(bodyRows[3])).toEqual(['handed-back', '03:00', ''])
  })

  it('leaves the last row of a still-running lane blank', async () => {
    // No exit: implementing is the last stop, so its dwell is blank in the table
    // (the open dwell is reported separately, not guessed into the row).
    await mountBoard([
      item({
        number: 4401,
        crew_id: 'crew-a',
        phase: 'implementing',
        timeline: [
          { phase: 'claimed', at: T(1) },
          { phase: 'implementing', at: T(2) },
        ],
      }),
    ])
    fireEvent.mouseMove(laneFor(4401))
    const card = await screen.findByRole('tooltip')
    const bodyRows = tableBodyRows(card)
    const cells = (r: HTMLElement) => within(r).getAllByRole('cell').map((c) => c.textContent)
    expect(cells(bodyRows[0])).toEqual(['claimed', '01:00', '+60m'])
    expect(cells(bodyRows[1])).toEqual(['implementing', '02:00', ''])
  })
})

describe('PipelineView — narrow (measured track width) branch', () => {
  it('stacks the row, drops the column-header labels, and names the lane position in words', async () => {
    await mountBoard(
      [item({ number: 4500, crew_id: 'crew-a', phase: 'implementing', title: 'zzq narrow' })],
      { width: 200 }, // below NARROW_TRACK_W (560)
    )

    // The wide column-header legend is gone at narrow — the header spacer stays
    // mounted (to keep the observer attached) but its phase-column labels and the
    // "PER PHASE" legend title do not render. `AWAIT CI` and `PER PHASE` only ever
    // appear in that header, so their absence proves the legend is gone (the
    // lane's OWN "IMPLEMENT" word, asserted below, is a different element).
    await waitFor(() => expect(screen.queryByText('AWAIT CI')).toBeNull())
    expect(screen.queryByText('PER PHASE')).toBeNull()

    // The row stacks vertically instead of laying card + track side by side.
    expect(laneFor(4500).className).toContain('flex-col')

    // The lane names its OWN position in words: its live phase header + how far
    // along the spine it is. implementing is column index 3 of 8 → "4/8".
    const row = laneFor(4500)
    expect(within(row).getByText('IMPLEMENT')).toBeTruthy()
    expect(within(row).getByText('4/8')).toBeTruthy()
  })

  it('names the SPECIFIC exit token so skipped / yielded / handed-back / preempted stay distinct', async () => {
    const cases: Array<{ phase: CrewFabricItem['phase']; token: string; number: number }> = [
      { phase: 'skipped', token: 'SKIPPED', number: 4601 },
      { phase: 'yielded', token: 'YIELDED', number: 4602 },
      { phase: 'handed-back', token: 'HANDED BACK', number: 4603 },
      { phase: 'preempted', token: 'PREEMPTED', number: 4604 },
    ]
    for (const c of cases) {
      const { unmount } = await mountBoard(
        [item({
          number: c.number,
          crew_id: 'crew-x',
          phase: c.phase,
          timeline: [{ phase: 'claimed', at: T(1) }],
          exit: { phase: c.phase, at: T(2) },
        })],
        { width: 200 },
      )
      const row = laneFor(c.number)
      // The exit token is spoken in the narrow row — and it is the SPECIFIC word,
      // not a single collapsed "exit". Every OTHER token is absent from the row.
      expect(within(row).getByText(c.token)).toBeTruthy()
      for (const other of cases) {
        if (other.token !== c.token) {
          expect(within(row).queryByText(other.token)).toBeNull()
        }
      }
      unmount()
    }
  })
})

describe('PipelineView — queue summary cards', () => {
  it('does not flag EDITING as a fault when two DIFFERENT crews each hold one editing item', async () => {
    await mountBoard([
      item({ number: 100, crew_id: 'crew-a', phase: 'implementing' }),
      item({ number: 200, crew_id: 'crew-b', phase: 'addressing-review' }),
    ])

    // Two editing lanes total, but across two crews — the per-crew cap (1) is not
    // breached, so this is legal and must NOT render in danger red.
    const value = statCardValue('Editing')
    expect(value.textContent).toBe('2')
    expect(value.className).not.toContain('text-danger')
  })

  it('flags EDITING as a fault when a SINGLE crew holds more than one editing item', async () => {
    await mountBoard([
      item({ number: 100, crew_id: 'crew-a', phase: 'implementing' }),
      item({ number: 200, crew_id: 'crew-a', phase: 'addressing-review' }),
    ])

    // Same crew, two editing items — that IS the invariant breach, and only THIS
    // is the danger case.
    const value = statCardValue('Editing')
    expect(value.textContent).toBe('2')
    expect(value.className).toContain('text-danger')
  })

  it('reports in-flight, escalations, and the longest open wait', async () => {
    // generated_at = 04:00; a lane that entered awaiting-ci at 01:00 has waited 3h.
    await mountBoard(
      [
        item({ number: 100, crew_id: 'crew-a', phase: 'awaiting-ci', reopens: 2, timeline: [{ phase: 'awaiting-ci', at: T(1) }] }),
        item({ number: 200, crew_id: 'crew-b', phase: 'implementing', timeline: [{ phase: 'implementing', at: T(3, 30) }] }),
      ],
      { generatedAt: T(4) },
    )

    // Both lanes are live (neither done nor exited).
    expect(statCardValue('In flight').textContent).toBe('2')
    // Escalations sums reopens across lanes.
    expect(statCardValue('Escalations').textContent).toBe('2')
    // Longest wait is the 3h awaiting-ci lane, formatted by the dwell ladder.
    expect(statCardValue('Longest wait').textContent).toBe('3h')
  })
})

describe('PipelineView — empty state', () => {
  it('renders the designed empty state when a connected repo returns items: []', async () => {
    // A connected repo with no crews is a 200 with an empty list, NOT an error —
    // this is the COMMON case, and it must draw the designed empty state.
    crewFabric.mockResolvedValue(fabric([], null))
    renderView()

    await waitFor(() => expect(screen.getByTestId('atp-empty')).toBeTruthy())
    expect(screen.getByTestId('atp-empty-title').textContent).toBe('No crew activity yet')
    // It is the empty state, not the board: no lanes, and not the "no repo"
    // state either (a repo IS connected).
    expect(screen.queryByText(/^IS-\d+$/)).toBeNull()
    expect(screen.queryByTestId('atp-no-repo')).toBeNull()
  })
})
