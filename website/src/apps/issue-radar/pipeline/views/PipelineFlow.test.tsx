/**
 * PipelineFlow — L0 of the global view: the RENDERED contract of the pipeline
 * row plus its one pure exported helper (`flowWeight`).
 *
 * These assert the things that carry MEANING to an operator, not that a
 * component mounts:
 *  - the live pulse appears on a step with work IN it and is absent on an idle
 *    one — it is what tells an operator a step is moving right now;
 *  - the count noun follows the step's OWN `unit` field (a scanner counts issues,
 *    the implement step counts sessions), and is never a hardcoded word — a
 *    session count labelled "issues" is a lie about what the pipeline did;
 *  - `flowWeight` is a pure function over (recentDone, busiest): its boundaries
 *    (zero, negative, non-finite, the share thresholds) decide connector
 *    thickness, so a wrong boundary silently misreports flow;
 *  - clicking a step card selects THAT step (not its neighbour) — the drill-down
 *    is the whole point of the row;
 *  - the unparseable-events warning row renders only when there ARE unparseable
 *    events; a zero must not draw a warning an operator would chase.
 *
 * PipelineFlow is a pure presentational component (props in, no network, no
 * store, no router), so it renders directly — the query-client/provider ceremony
 * the board-level `PipelineView.render.test.tsx` needs is not required here, and
 * adding it would only obscure what is under test. i18n is initialized to English
 * by the shared test setup, so the asserted strings are the real catalog values.
 */
import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'

import PipelineFlow, { flowWeight, stepLabel, unitLabel } from './PipelineFlow'
import type { OverviewResponse, OverviewStep } from '../api'

// ── fixtures ──────────────────────────────────────────────────────────────────
function step(over: Partial<OverviewStep> = {}): OverviewStep {
  return {
    key: 'scan',
    label: 'Scan',
    unit: 'issues',
    entered: 0,
    done: 0,
    skipped: 0,
    churn: 0,
    recentEntered: 0,
    recentDone: 0,
    inFlight: 0,
    distinctEntered: 0,
    distinctDone: 0,
    routed: [],
    ...over,
  }
}

function overview(steps: OverviewStep[], over: Partial<OverviewResponse> = {}): OverviewResponse {
  return {
    steps,
    totalEvents: 0,
    unparseable: 0,
    unmappedEvents: [],
    firstEventAt: null,
    lastEventAt: null,
    recentHours: 24,
    ...over,
  }
}

function renderFlow(
  ov: OverviewResponse,
  opts: { selectedStep?: string | null; onSelectStep?: (k: string) => void; nowMs?: number } = {},
) {
  const onSelectStep = opts.onSelectStep ?? vi.fn()
  const utils = render(
    <PipelineFlow
      overview={ov}
      selectedStep={opts.selectedStep ?? null}
      onSelectStep={onSelectStep}
      nowMs={opts.nowMs ?? 1_000_000_000}
    />,
  )
  return { ...utils, onSelectStep }
}

/** The step card <button> for a given key. */
const cardFor = (key: string) => screen.getByTestId(`atp-step-${key}`)

describe('PipelineFlow — live indicator', () => {
  it('renders the live pulse on a step with inFlight > 0 and omits it on an idle step', () => {
    renderFlow(
      overview([
        step({ key: 'implement', label: 'Implement', unit: 'sessions', inFlight: 3 }),
        step({ key: 'verify', label: 'Verify', unit: 'sessions', inFlight: 0 }),
      ]),
    )

    // The pulse is a decorative aria-hidden dot inside the card. The live step
    // has one; the idle step has none — so the presence of the dot IS the signal.
    const live = cardFor('implement')
    const idle = cardFor('verify')
    expect(live.querySelector('.motion-safe\\:animate-pulse')).not.toBeNull()
    expect(idle.querySelector('.motion-safe\\:animate-pulse')).toBeNull()
  })
})

describe('PipelineFlow — count noun follows the step unit', () => {
  it('labels a sessions step "sessions" and an issues step "issues", never a hardcoded noun', () => {
    renderFlow(
      overview([
        step({ key: 'scan', label: 'Scan', unit: 'issues', inFlight: 5 }),
        step({ key: 'implement', label: 'Implement', unit: 'sessions', inFlight: 2 }),
      ]),
    )

    // The "in step (unit)" line names the unit from the step's own field. Match
    // the noun as a substring within each card (robust to the surrounding
    // interpolation) — the scan card says "issues", the implement card "sessions",
    // and neither borrows the other's noun.
    const scanText = cardFor('scan').textContent ?? ''
    const implementText = cardFor('implement').textContent ?? ''
    expect(scanText).toContain('issues')
    expect(scanText).not.toContain('sessions')
    expect(implementText).toContain('sessions')

    // And the exported helper is the single source of that noun.
    expect(unitLabel(step({ unit: 'issues' }))).toBe('issues')
    expect(unitLabel(step({ unit: 'sessions' }))).toBe('sessions')
  })

  it('stepLabel translates a known step key and falls back to the server label for an unknown one', () => {
    // A known key resolves to the catalog label.
    expect(stepLabel(step({ key: 'scan', label: 'IGNORED' }))).toBe('Scan')
    // An unknown key (a step added server-side) has no catalog entry, so the
    // server's own label is shown rather than the raw key or a blank.
    expect(stepLabel(step({ key: 'brand_new_step', label: 'Brand New' }))).toBe('Brand New')
    // With neither a catalog entry nor a server label, the key itself is the
    // last-resort label — never an empty cell.
    expect(stepLabel(step({ key: 'brand_new_step', label: '' }))).toBe('brand_new_step')
  })
})

describe('flowWeight — pure boundaries', () => {
  it('is 1 (hairline) when there is no recent flow', () => {
    expect(flowWeight(0, 100)).toBe(1)
  })

  it('is 1 for a negative or non-finite recentDone (untrusted source)', () => {
    expect(flowWeight(-5, 100)).toBe(1)
    expect(flowWeight(Number.NaN, 100)).toBe(1)
    expect(flowWeight(Number.POSITIVE_INFINITY, 100)).toBe(1)
  })

  it('is 1 when the busiest is zero or non-finite (no scale to divide by)', () => {
    expect(flowWeight(10, 0)).toBe(1)
    expect(flowWeight(10, -1)).toBe(1)
    expect(flowWeight(10, Number.NaN)).toBe(1)
  })

  it('climbs the share thresholds: >0 -> 2, >=0.33 -> 3, >=0.66 -> 4', () => {
    // Just above zero share is the thinnest live weight.
    expect(flowWeight(1, 1000)).toBe(2)
    // Just below the 0.33 boundary stays at 2 …
    expect(flowWeight(32, 100)).toBe(2)
    // … and the boundary itself steps to 3.
    expect(flowWeight(33, 100)).toBe(3)
    expect(flowWeight(65, 100)).toBe(3)
    // The 0.66 boundary steps to the max.
    expect(flowWeight(66, 100)).toBe(4)
    // The busiest step (share 1.0) is the max weight.
    expect(flowWeight(100, 100)).toBe(4)
  })
})

describe('PipelineFlow — step selection', () => {
  it('clicking a step card calls onSelectStep with THAT step key', () => {
    const { onSelectStep } = renderFlow(
      overview([
        step({ key: 'scan', label: 'Scan' }),
        step({ key: 'triage', label: 'Triage' }),
        step({ key: 'implement', label: 'Implement', unit: 'sessions' }),
      ]),
    )

    fireEvent.click(cardFor('triage'))
    expect(onSelectStep).toHaveBeenCalledTimes(1)
    expect(onSelectStep).toHaveBeenCalledWith('triage')

    fireEvent.click(cardFor('implement'))
    expect(onSelectStep).toHaveBeenLastCalledWith('implement')
  })

  it('marks the selected step pressed and the others not', () => {
    renderFlow(
      overview([step({ key: 'scan' }), step({ key: 'triage', label: 'Triage' })]),
      { selectedStep: 'triage' },
    )
    expect(cardFor('triage').getAttribute('aria-pressed')).toBe('true')
    expect(cardFor('scan').getAttribute('aria-pressed')).toBe('false')
  })
})

describe('PipelineFlow — unparseable warning row', () => {
  it('renders the warning row when unparseable > 0', () => {
    renderFlow(overview([step({ key: 'scan' })], { unparseable: 7 }))
    const warn = screen.getByTestId('atp-unparseable')
    expect(warn).toBeTruthy()
    // It states the real count, pluralized.
    expect(warn.textContent).toContain('7')
  })

  it('does NOT render the warning row when unparseable is 0', () => {
    renderFlow(overview([step({ key: 'scan' })], { unparseable: 0 }))
    expect(screen.queryByTestId('atp-unparseable')).toBeNull()
  })

  it('DISCLOSES the pre-stamp share of the event count when there is one', () => {
    // The count existed on the payload from the start and was rendered nowhere,
    // which made the total read as fully attributed. This is the disclosure.
    renderFlow(overview([step({ key: 'scan' })], { totalEvents: 4773, unattributedEvents: 4565 }))
    const note = screen.getByTestId('atp-unattributed')
    expect(note.textContent).toContain('4565')
    // And it says WHY, not just the number -- a bare count beside the total would
    // read as a second unexplained metric rather than a qualification of the first.
    expect(note.textContent).toContain('cannot be attributed')
    // And what FOLLOWS from that, which is the load-bearing half now that a scoped
    // fold excludes these events from its step counters. Without this clause the
    // reader sees `SCAN 9` next to a count of 4565 and can only assume the 4565 are
    // folded in somewhere. Pinned separately because it is the sentence a future
    // copy edit would most plausibly trim as redundant.
    expect(note.textContent).toContain('not counted in the steps below')
  })

  it('does NOT disclose anything when the whole trail is stamped', () => {
    // A fully-stamped install must not see a line reading zero: it would invite an
    // operator to chase an ambiguity that does not exist.
    renderFlow(overview([step({ key: 'scan' })], { totalEvents: 208, unattributedEvents: 0 }))
    expect(screen.queryByTestId('atp-unattributed')).toBeNull()
  })

  it('DISCLOSES that the board mixes repositories, naming them', () => {
    // This board folds every repository together and cannot separate them: the
    // dispatch queue keys entries on the issue number alone. Saying so is the
    // honest alternative to a repository selector over ambiguous joins.
    renderFlow(
      overview([step({ key: 'scan' })], {
        repos: [
          { repo: 'acme/alpha', count: 12 },
          { repo: 'acme/beta', count: 3 },
        ],
      }),
    )
    const note = screen.getByTestId('atp-repo-census')
    expect(note.textContent).toContain('2')
    expect(note.textContent).toContain('acme/alpha')
    expect(note.textContent).toContain('acme/beta')
  })

  it('says NOTHING about repositories when the trail names only one', () => {
    // One repository is not a mixture, so a line announcing it would be noise --
    // and would imply a scoping decision the board did not make.
    renderFlow(overview([step({ key: 'scan' })], { repos: [{ repo: 'acme/alpha', count: 9 }] }))
    expect(screen.queryByTestId('atp-repo-census')).toBeNull()
  })

  it('drops the census line rather than the whole board when the field is absent', () => {
    // The client is documented as forward-tolerant and never-throwing. An overview
    // assembled without `repos` must cost one line, not the entire pipeline: a bare
    // `.length` read here took the board down with a TypeError.
    const partial = overview([step({ key: 'scan' })], {})
    delete (partial as { repos?: unknown }).repos
    renderFlow(partial)
    expect(screen.queryByTestId('atp-repo-census')).toBeNull()
    // The board itself still rendered.
    expect(screen.getByTestId('atp-step-scan')).toBeTruthy()
  })
})
