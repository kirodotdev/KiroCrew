/**
 * StepItemsTable — L1: the RENDERED contract of the items sitting inside one
 * pipeline step, plus its two pure exported helpers (`hasSessions`,
 * `waitedSince`).
 *
 * The behaviours under test are the ones where a wrong render asserts something
 * the DATA does not say:
 *  - an empty step draws the DESIGNED empty state, not a zero-row table (a bare
 *    table header over nothing reads as a rendering bug);
 *  - a pr=null row shows the "not recorded" marker, NOT a broken link — the fold
 *    only records a PR when a structured field named it, and a guessed link is a
 *    confidently-wrong click; a row WITH a pr renders a link whose href carries
 *    that number;
 *  - a labels=[] row says "not cached", NOT "no labels": the labels come from a
 *    cache written only when a human opens the issue elsewhere, so absence means
 *    "we did not look", a different fact from "the issue has none";
 *  - the expanded row carries the item's BASICS (queued, dispatched, last event,
 *    author) and the injected cost table, and does NOT draw an event-trail strip.
 *    The strip is pinned as ABSENT: it answered a question nobody at this level
 *    asks and pushed the cost table below the fold, so a test has to stop it
 *    coming back;
 *  - the cost table renders INSIDE the row through `renderSessions`, which is what
 *    keeps it next to the item it describes instead of at the end of the page;
 *  - `hasSessions` is false for an item with no slot and no previousSlots, and
 *    such a row is NOT given an empty cost table — an empty table looks like lost
 *    data rather than like work that never started.
 *
 * A pure props-in presentational component: no network, no store, no router, so
 * it renders directly. English is installed by the shared setup, so the asserted
 * strings are the real catalog values.
 */
import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, within } from '@testing-library/react'
import type { ReactNode } from 'react'

import StepItemsTable, { hasSessions, waitedSince } from './StepItemsTable'
import type { StepItem } from '../api'

// ── fixtures ──────────────────────────────────────────────────────────────────
function item(over: Partial<StepItem> = {}): StepItem {
  return {
    number: 100,
    title: '',
    labels: [],
    author: '',
    assignees: [],
    comments: 0,
    queuedAt: null,
    dispatchedAt: null,
    resumeCount: 0,
    slot: '',
    previousSlots: [],
    withdrawn: false,
    needsHuman: false,
    pr: null,
    lastEvent: '',
    lastEventAt: null,
    ...over,
  }
}

function renderTable(
  items: StepItem[],
  opts: {
    stepKey?: string
    expanded?: number | null
    onToggleItem?: (n: number) => void
    renderSessions?: (n: number) => ReactNode
    nowMs?: number
  } = {},
) {
  const onToggleItem = opts.onToggleItem ?? vi.fn()
  const renderSessions =
    opts.renderSessions ?? ((n: number) => <div data-testid={`atp-cost-${n}`}>cost table</div>)
  const utils = render(
    <StepItemsTable
      stepKey={opts.stepKey ?? 'implement'}
      items={items}
      expandedItem={opts.expanded ?? null}
      onToggleItem={onToggleItem}
      renderSessions={renderSessions}
      nowMs={opts.nowMs ?? 1_000_000_000}
    />,
  )
  return { ...utils, onToggleItem, renderSessions }
}

const rowFor = (n: number) => screen.getByTestId(`atp-item-${n}`)

describe('StepItemsTable — empty state', () => {
  it('renders the designed empty state, not a zero-row table, for an empty item list', () => {
    renderTable([])
    expect(screen.getByTestId('atp-step-empty')).toBeTruthy()
    // It is NOT the populated list container, and there are no item rows.
    expect(screen.queryByTestId('atp-items-implement')).toBeNull()
    expect(screen.queryByTestId(/^atp-item-\d+$/)).toBeNull()
  })
})

describe('StepItemsTable — PR column', () => {
  it('renders a link whose href carries the PR number for a row WITH a pr', () => {
    renderTable([item({ number: 4820, pr: 5127 })])
    const link = within(rowFor(4820)).getByRole('link')
    expect(link.getAttribute('href')).toContain('5127')
    // A real external PR link, opened in a new tab.
    expect(link.getAttribute('href')).toContain('/pull/5127')
    expect(link.getAttribute('target')).toBe('_blank')
  })

  it('keeps the PR link OUTSIDE the toggle button, so the link is reachable', () => {
    // An anchor nested inside a button is invalid, and the toggle would swallow the
    // click: the operator would expand the row instead of opening the pull request.
    renderTable([item({ number: 4820, pr: 5127 })])
    const row = rowFor(4820)
    const link = within(row).getByRole('link')
    const toggleBtn = within(row).getByTestId('atp-toggle-4820')
    expect(toggleBtn.contains(link)).toBe(false)
  })

  it('renders the "not recorded" marker rather than a broken link for a row with pr=null', () => {
    renderTable([item({ number: 4820, pr: null })])
    const row = rowFor(4820)
    // No link at all — nothing to click into a confidently-wrong PR.
    expect(within(row).queryByRole('link')).toBeNull()
    // The marker is present with its explanatory title.
    expect(within(row).getByTitle('No pull request recorded')).toBeTruthy()
  })
})

describe('StepItemsTable — expansion is controlled by the parent', () => {
  it('calls onToggleItem with the item number when the row toggle is pressed', () => {
    const { onToggleItem } = renderTable([item({ number: 100 })])
    fireEvent.click(screen.getByTestId('atp-toggle-100'))
    expect(onToggleItem).toHaveBeenCalledWith(100)
  })

  it('marks the toggle expanded only for the item the parent says is open', () => {
    renderTable([item({ number: 100 }), item({ number: 200 })], { expanded: 200 })
    expect(screen.getByTestId('atp-toggle-100').getAttribute('aria-expanded')).toBe('false')
    expect(screen.getByTestId('atp-toggle-200').getAttribute('aria-expanded')).toBe('true')
  })
})

describe('StepItemsTable — expanded row basics', () => {
  it('says "Not cached" for labels=[], NOT "no labels" — absence of a cache is not absence of labels', () => {
    renderTable([item({ number: 100, labels: [] })], { expanded: 100 })

    const row = rowFor(100)
    // The Labels definition renders the "not cached" wording …
    expect(within(row).getAllByText('Not cached').length).toBeGreaterThan(0)
    // … and never claims the issue HAS no labels.
    expect(within(row).queryByText(/no labels/i)).toBeNull()
  })

  it('lists real labels when they ARE cached', () => {
    renderTable([item({ number: 100, labels: ['bug', 'p1'] })], { expanded: 100 })
    expect(within(rowFor(100)).getByText('bug, p1')).toBeTruthy()
  })

  it('shows the timing and provenance basics an operator opens a row to read', () => {
    renderTable([item({ number: 100, author: 'octocat', lastEvent: 'implement_start' })], {
      expanded: 100,
    })
    const row = rowFor(100)
    for (const label of ['Queued', 'Dispatched', 'Last event', 'Author']) {
      expect(within(row).getByText(label)).toBeTruthy()
    }
    expect(within(row).getByText('octocat')).toBeTruthy()
    expect(within(row).getByText('implement_start')).toBeTruthy()
  })

  it('does NOT draw an event-trail strip, expanded or collapsed', () => {
    // Pinned as absent on purpose. The strip listed the pipeline's internal event
    // names, which is not the question this level answers, and it pushed the cost
    // table out of view. The backend no longer ships the trail either, so there is
    // nothing to render even by accident. Per-item relationships belong to a
    // dependency view.
    renderTable([item({ number: 100, lastEvent: 'claimed' })], { expanded: 100 })
    expect(screen.queryByTestId('atp-trail-100')).toBeNull()
    // `lastEvent` IS shown as a single fact; a multi-step strip is not.
    expect(screen.getAllByText('claimed')).toHaveLength(1)
  })
})

describe('StepItemsTable — the cost table renders inside the row', () => {
  it('injects renderSessions INSIDE the expanded row, not after the list', () => {
    renderTable([item({ number: 100, slot: 'chat:1' }), item({ number: 200 })], { expanded: 100 })
    const node = screen.getByTestId('atp-cost-100')
    // Containment is the point: appended after the list it sat below every other
    // row, so the answer to "what did THIS item cost" was twenty rows away.
    expect(rowFor(100).contains(node)).toBe(true)
  })

  it('does not render the cost table for a collapsed row', () => {
    renderTable([item({ number: 100, slot: 'chat:1' })], { expanded: null })
    expect(screen.queryByTestId('atp-cost-100')).toBeNull()
  })

  it('shows the no-session note INSTEAD of an empty cost table when the item never opened one', () => {
    const renderSessions = vi.fn(() => <div data-testid="atp-cost-100" />)
    renderTable([item({ number: 100, slot: '', previousSlots: [] })], {
      expanded: 100,
      renderSessions,
    })
    expect(screen.getByText('No sessions for this item')).toBeTruthy()
    // Never asked for the table, so no fetch is paid for and no empty grid renders.
    expect(renderSessions).not.toHaveBeenCalled()
    expect(screen.queryByTestId('atp-cost-100')).toBeNull()
  })

  it('asks for the cost table when only RETIRED slots remain', () => {
    // The spend lives on the retired slots, so a fully-retired item still has a
    // cost table worth opening.
    const renderSessions = vi.fn((n: number) => <div data-testid={`atp-cost-${n}`} />)
    renderTable([item({ number: 100, slot: '', previousSlots: ['chat:old'] })], {
      expanded: 100,
      renderSessions,
    })
    expect(renderSessions).toHaveBeenCalledWith(100)
    expect(screen.getByTestId('atp-cost-100')).toBeTruthy()
  })
})

describe('hasSessions — pure boundary', () => {
  it('is false for an item with no slot and no previousSlots', () => {
    expect(hasSessions(item({ slot: '', previousSlots: [] }))).toBe(false)
  })

  it('is true when a live slot is present', () => {
    expect(hasSessions(item({ slot: 'chat:1', previousSlots: [] }))).toBe(true)
  })

  it('is true when only previousSlots are present (a retired-slot item still has sessions)', () => {
    expect(hasSessions(item({ slot: '', previousSlots: ['chat:old'] }))).toBe(true)
  })
})

describe('waitedSince — pure precedence', () => {
  it('prefers lastEventAt, then dispatchedAt, then queuedAt, else null', () => {
    expect(waitedSince(item({ lastEventAt: 3, dispatchedAt: 2, queuedAt: 1 }))).toBe(3)
    expect(waitedSince(item({ lastEventAt: null, dispatchedAt: 2, queuedAt: 1 }))).toBe(2)
    expect(waitedSince(item({ lastEventAt: null, dispatchedAt: null, queuedAt: 1 }))).toBe(1)
    expect(waitedSince(item({ lastEventAt: null, dispatchedAt: null, queuedAt: null }))).toBeNull()
  })
})
