/**
 * Context Breakdown panel: the properties that make the chart honest.
 *
 *  - every backend block label lands in exactly one of the five categories, and
 *    an unknown label goes to "other" rather than vanishing from the total.
 *  - the five every-turn boilerplate blocks collapse into one disclosure row.
 *  - only the newest 30 turns are drawn; older ones are counted, not dropped.
 *  - selecting a turn (click or arrow keys) drives the detail below the chart,
 *    and the newest turn is selected by default.
 *  - a session-start turn is listed above the chart, never drawn in it, so its
 *    size cannot pin the y-axis; axis labels are strided so they never overprint.
 *  - an un-recorded session degrades to a readable empty state, not a crash.
 */
import { describe, it, expect, afterEach } from 'vitest'
import { render, screen, cleanup, within, fireEvent } from '@testing-library/react'

import {
  ContextBreakdownPanel,
  groupBlocks,
  categorise,
  categoryOf,
  niceTicks,
  axisLabelIndices,
  CATEGORIES,
  CATEGORY_OF,
  EVERY_TURN_MEMBERS,
  MAX_CHART_TURNS,
  USER_LABEL,
  type ContextTrace,
  type ContextTurn,
} from '../pages/ContextBreakdownPanel'

afterEach(cleanup)

/**
 * Budget for the two placement tests that dynamically import
 * `pages/chat/SidePanel`.
 *
 * Whichever of them runs first pays that module graph's resolve+transform, which
 * exceeded the 15s global default on a Windows checkout — so both failed for
 * every Windows contributor while passing on CI's Linux runner. The import stays
 * DYNAMIC deliberately: hoisting it to a static top-level import moved the same
 * work into the file's import phase and took the whole file from 32s to 259s
 * (measured), because then every test in it waits on that graph. This is a real,
 * bounded cost rather than a hang, so a per-test budget is the honest fix; the
 * global default stays tight for everything else.
 */
const SIDE_PANEL_IMPORT_TIMEOUT_MS = 45_000

const turn = (over: Partial<ContextTurn> = {}): ContextTurn => ({
  ts: '2026-08-04T00:00:00Z',
  phase: 'per_turn',
  blocks: { request_header: 1576, your_message: 6 },
  total_chars: 1582,
  context_used: 2000,
  context_window: 200000,
  model: 'claude',
  ...over,
})

const trace = (over: Partial<ContextTrace> = {}): ContextTrace => ({
  slot: '578c537a',
  turns: [],
  totals: {},
  injected_chars: 0,
  user_chars: 0,
  peak_context_used: 0,
  context_window: 0,
  window_days: 14,
  ...over,
})

/** A turn whose blocks sum to `total_chars`, so the chart's stack is honest. */
const turnOf = (blocks: Record<string, number>, over: Partial<ContextTurn> = {}): ContextTurn =>
  turn({ blocks, total_chars: Object.values(blocks).reduce((a, b) => a + b, 0), ...over })

describe('categoryOf / categorise — five plain-language buckets', () => {
  it('maps every listed label to one of the five categories', () => {
    for (const [label, cat] of Object.entries(CATEGORY_OF)) {
      expect(CATEGORIES).toContain(cat)
      expect(categoryOf(label)).toBe(cat)
    }
    expect(categoryOf(USER_LABEL)).toBe('message')
    expect(categoryOf('lessons')).toBe('rules')
    expect(categoryOf('response_preferences')).toBe('rules')
    expect(categoryOf('loaded_skill')).toBe('skills')
    expect(categoryOf('semantic_memory')).toBe('memory')
    expect(categoryOf('task_facts')).toBe('memory')
  })

  it('sends the every-turn members, unclassified and unknown labels to "other"', () => {
    for (const member of EVERY_TURN_MEMBERS) expect(categoryOf(member)).toBe('other')
    expect(categoryOf('unclassified')).toBe('other')
    expect(categoryOf('some_future_block')).toBe('other')
  })

  it('never drops an unknown label from the total', () => {
    const cats = categorise({ your_message: 10, memory: 100, brand_new_label: 7, request_header: 3 })
    const sum = Object.values(cats).reduce((a, b) => a + b, 0)
    expect(sum).toBe(120)
    expect(cats.other).toBe(10)
    expect(cats.message).toBe(10)
    expect(cats.memory).toBe(100)
    // Every category is present so the stack always has five layers.
    expect(Object.keys(cats).sort()).toEqual([...CATEGORIES].sort())
  })
})

describe('groupBlocks — every-turn bucket', () => {
  it('collapses exactly the five every-turn members into one label', () => {
    const grouped = groupBlocks({
      surface: 100,
      working_folder: 50,
      request_header: 200,
      reply_format_rules: 30,
      user_display: 20,
      memory: 1000,
      your_message: 6,
    })
    expect(grouped.every_turn).toBe(400)
    expect(grouped.memory).toBe(1000)
    expect(grouped[USER_LABEL]).toBe(6)
    for (const member of EVERY_TURN_MEMBERS) {
      expect(grouped[member]).toBeUndefined()
    }
  })

  it('leaves a trace with no every-turn members untouched', () => {
    expect(groupBlocks({ memory: 10, loaded_skill: 20 })).toEqual({ memory: 10, loaded_skill: 20 })
  })
})

describe('niceTicks — three to four round gridlines', () => {
  it('rounds the ceiling up to a whole step and starts at zero', () => {
    expect(niceTicks(2400)).toEqual([0, 1000, 2000, 3000])
    expect(niceTicks(116652)).toEqual([0, 50000, 100000, 150000])
    expect(niceTicks(7)).toEqual([0, 5, 10])
  })

  it('keeps the tick count small for any magnitude', () => {
    for (const max of [1, 13, 999, 4321, 87654, 1234567]) {
      const ticks = niceTicks(max)
      expect(ticks[0]).toBe(0)
      expect(ticks[ticks.length - 1]).toBeGreaterThanOrEqual(max)
      expect(ticks.length).toBeGreaterThanOrEqual(3)
      expect(ticks.length).toBeLessThanOrEqual(6)
    }
  })

  it('is defensive about an all-zero trace', () => {
    expect(niceTicks(0)).toEqual([0, 1])
  })
})

describe('ContextBreakdownPanel rendering', () => {
  const sixTurns = () =>
    trace({
      turns: [
        turnOf({ your_message: 120, memory: 1500, lessons: 700, agent_instructions: 300, skill_index: 280, request_header: 100 }, { phase: 'session_start' }),
        turnOf({ your_message: 80, memory: 600, lessons: 800, skill_index: 240, request_header: 80 }),
        turnOf({ your_message: 80, memory: 800, lessons: 800, skill_index: 240, request_header: 80 }),
        turnOf({ your_message: 80, memory: 1200, lessons: 500, response_preferences: 100, critical_rules: 200, skill_index: 240, request_header: 80 }),
        turnOf({ your_message: 40, memory: 1200, lessons: 500, skill_index: 240, loaded_skill: 4000, request_header: 80 }),
        turnOf({ your_message: 80, memory: 1200, lessons: 500, skill_index: 240, request_header: 80, mystery_block: 60 }),
      ],
    })

  it('draws one stacked layer per category, bottom to top', () => {
    const { container } = render(<ContextBreakdownPanel trace={sixTurns()} />)
    const layers = Array.from(container.querySelectorAll('svg g[data-category]')).map(g => g.getAttribute('data-category'))
    expect(layers).toEqual([...CATEGORIES])
    expect(container.querySelector('svg')).toHaveAttribute('role', 'img')
  })

  it('selects the newest turn by default and shows its total, delta and categories', () => {
    render(<ContextBreakdownPanel trace={sixTurns()} />)
    const detail = screen.getByTestId('selected-turn-detail')
    expect(within(detail).getByText(/Turn 6 · latest/)).toBeInTheDocument()
    // 2,160 now vs 6,060 before: the loaded skill left the context.
    expect(within(detail).getByText('2,160')).toBeInTheDocument()
    expect(within(detail).getByText(/−3,900 vs previous/)).toBeInTheDocument()
    // The unknown block is counted under "Other", not lost.
    const other = detail.querySelector('[data-category-row="other"]') as HTMLElement
    expect(other).not.toBeNull()
    expect(within(other).getByText('140')).toBeInTheDocument()
    expect(within(other).getByText('Everything else')).toBeInTheDocument()
    // Zero-size categories are hidden: turn 6 has no full skill loaded, but
    // skill_index is a skill, so the skills row still shows.
    expect(detail.querySelector('[data-category-row="skills"]')).not.toBeNull()
    // The delta is information, not a verdict: muted in both directions.
    expect(within(detail).getByText(/−3,900 vs previous/).className).toContain('text-muted')
    const pressed = screen.getAllByRole('button', { pressed: true })
    expect(pressed).toHaveLength(1)
    expect(pressed[0]).toHaveAttribute('data-turn', '6')
  })

  it('moves the detail to a clicked turn and back with the arrow keys', () => {
    render(<ContextBreakdownPanel trace={sixTurns()} />)
    const turn4 = screen.getByRole('button', { name: /Turn 4:/ })
    fireEvent.click(turn4)
    const detail = screen.getByTestId('selected-turn-detail')
    expect(within(detail).getByText('Turn 4')).toBeInTheDocument()
    expect(within(detail).queryByText(/latest/)).toBeNull()
    expect(within(detail).getByText(/\+400 vs previous/)).toBeInTheDocument()
    expect(turn4).toHaveAttribute('aria-pressed', 'true')

    // One tab stop: only the pressed button is tabbable.
    expect(turn4).toHaveAttribute('tabindex', '0')
    expect(screen.getByRole('button', { name: /Turn 5:/ })).toHaveAttribute('tabindex', '-1')
    fireEvent.keyDown(turn4, { key: 'ArrowRight' })
    expect(screen.getByRole('button', { name: /Turn 5:/ })).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByRole('button', { name: /Turn 5:/ })).toHaveAttribute('tabindex', '0')
    expect(within(detail).getByText('Turn 5')).toBeInTheDocument()
    fireEvent.keyDown(screen.getByRole('button', { name: /Turn 5:/ }), { key: 'ArrowLeft' })
    fireEvent.keyDown(screen.getByRole('button', { name: /Turn 4:/ }), { key: 'ArrowLeft' })
    expect(screen.getByRole('button', { name: /Turn 3:/ })).toHaveAttribute('aria-pressed', 'true')
  })

  it('shows no delta on the first turn and "same as previous" on an unchanged one', () => {
    render(
      <ContextBreakdownPanel
        trace={trace({
          turns: [turnOf({ your_message: 10, memory: 90 }), turnOf({ your_message: 10, memory: 90 })],
        })}
      />,
    )
    const detail = screen.getByTestId('selected-turn-detail')
    expect(within(detail).getByText(/same as previous/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /Turn 1:/ }))
    expect(within(detail).queryByText(/previous/)).toBeNull()
  })

  it('expands a category into its raw blocks, with the every-turn members merged', () => {
    render(<ContextBreakdownPanel trace={sixTurns()} />)
    fireEvent.click(screen.getByText(/Turn 1 · session start/))
    const detail = screen.getByTestId('selected-turn-detail')
    const rules = detail.querySelector('[data-category-row="rules"]') as HTMLElement
    const toggle = within(rules).getByRole('button')
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    fireEvent.click(toggle)
    expect(toggle).toHaveAttribute('aria-expanded', 'true')
    expect(within(rules).getByText('Corrections you taught it')).toBeInTheDocument()
    expect(within(rules).getByText('Agent instructions')).toBeInTheDocument()

    const other = detail.querySelector('[data-category-row="other"]') as HTMLElement
    fireEvent.click(within(other).getByRole('button'))
    expect(within(other).getByText('Every-turn instructions')).toBeInTheDocument()
    expect(within(other).queryByText(/request header/i)).toBeNull()
  })

  it('labels the task-facts block from the catalog, distinct from the facts-you-told-it block', () => {
    // `task_facts` rides along on every session under the default inject_activity,
    // so it earns a catalog entry: the humanised id ("Task facts") would render in
    // English beside translated siblings and read as a twin of "Facts you told it".
    render(<ContextBreakdownPanel trace={trace({ turns: [turnOf({ your_message: 80, semantic_memory: 300, task_facts: 200 })] })} />)
    const detail = screen.getByTestId('selected-turn-detail')
    const memory = detail.querySelector('[data-category-row="memory"]') as HTMLElement
    fireEvent.click(within(memory).getByRole('button'))
    expect(within(memory).getByText('Facts recalled for this task')).toBeInTheDocument()
    expect(within(memory).getByText('Facts you told it')).toBeInTheDocument()
    expect(within(memory).queryByText('Task facts')).toBeNull()
  })

  it('draws only the newest 30 turns and counts the rest', () => {
    const many = Array.from({ length: MAX_CHART_TURNS + 5 }, (_, i) => turnOf({ your_message: 10 + i, memory: 100 }))
    render(<ContextBreakdownPanel trace={trace({ turns: many })} />)
    expect(screen.getByText('5 earlier turns not shown')).toBeInTheDocument()
    expect(screen.getByText(`Last ${MAX_CHART_TURNS} turns`)).toBeInTheDocument()
    expect(screen.queryByText(/^All /)).toBeNull()
    expect(screen.getAllByRole('button', { name: /^Turn \d+:/ })).toHaveLength(MAX_CHART_TURNS)
    expect(screen.queryByRole('button', { name: /^Turn 5:/ })).toBeNull()
    expect(screen.getByRole('button', { name: /^Turn 6:/ })).toBeInTheDocument()
    // The newest turn is still "latest" even though earlier ones are hidden.
    expect(screen.getByText(`Turn ${MAX_CHART_TURNS + 5} · latest`)).toBeInTheDocument()
  })

  it('carries neither a whole-window estimate nor a credits column', () => {
    render(
      <ContextBreakdownPanel
        trace={trace({
          turns: [turnOf({ memory: 4000, your_message: 40 })],
          peak_context_used: 5000,
        })}
      />,
    )
    expect(screen.queryByText(/estimate/i)).toBeNull()
    expect(screen.queryByText(/Not measured/i)).toBeNull()
    expect(screen.queryByText('Credits')).toBeNull()
  })

  it('renders a readable empty state for a session with no recorded turns', () => {
    render(<ContextBreakdownPanel trace={trace({ turns: [] })} />)
    expect(screen.getByText(/No context breakdown recorded/i)).toBeInTheDocument()
  })

  it('shows a loading state before the first payload', () => {
    const { container } = render(<ContextBreakdownPanel trace={undefined} isLoading />)
    expect(within(container).getByText(/Loading context breakdown/i)).toBeInTheDocument()
  })
})

describe('axisLabelIndices — labels that never overprint', () => {
  it('labels every turn when there is room', () => {
    expect(axisLabelIndices(6, 662, 5)).toEqual([0, 1, 2, 3, 4, 5])
  })

  it('strides, keeps the selected and the last turn, and drops colliding neighbours', () => {
    // 176px of plot fits 4 labels; 30 turns -> stride 8 -> 0, 8, 16, 24 (+ last).
    expect(axisLabelIndices(30, 176, 29)).toEqual([0, 8, 16, 29])
    // Selecting turn 9 (index 8) keeps it and drops nothing else; selecting index
    // 10 drops the strided 8 and 16 that would sit on top of it.
    expect(axisLabelIndices(30, 176, 8)).toEqual([0, 8, 16, 29])
    expect(axisLabelIndices(30, 176, 10)).toEqual([0, 10, 29])
    for (let sel = 0; sel < 30; sel++) {
      expect(axisLabelIndices(30, 176, sel).length).toBeLessThanOrEqual(Math.floor(176 / 44) + 2)
    }
  })
})

describe('session-start turns sit above the chart', () => {
  const withStart = () =>
    trace({
      turns: [
        turnOf({ your_message: 120, memory: 30_000, lessons: 6_000, agent_instructions: 2_000, skill_index: 280, request_header: 100 }, { phase: 'session_start' }),
        turnOf({ your_message: 80, memory: 600, lessons: 800, skill_index: 240, request_header: 80 }),
        turnOf({ your_message: 80, memory: 1_000, lessons: 800, skill_index: 240, request_header: 80 }),
      ],
    })

  it('renders the start turn as a selectable row, not as a point on the chart', () => {
    const { container } = render(<ContextBreakdownPanel trace={withStart()} />)
    const row = container.querySelector('[data-start-row]') as HTMLElement
    expect(row).not.toBeNull()
    expect(row).toHaveAttribute('data-turn', '1')
    expect(row).toHaveAttribute('aria-pressed', 'false')
    expect(row.textContent).toContain('Turn 1 · session start')
    expect(row.textContent).toContain('38,500 characters')
    // The chart only carries the two regular turns.
    expect(screen.getByText('All 2 turns')).toBeInTheDocument()
    expect(screen.getByText(/pick a turn/)).toBeInTheDocument()
    expect(container.querySelector('svg [data-axis-label="1"]')).toBeNull()
    expect(screen.queryByRole('button', { name: /^Turn 1:/ })).toBeNull()
    // Its size does not pin the y-axis: the top tick stays near the regular turns.
    const ticks = Array.from(container.querySelectorAll('svg text.tabular-nums'))
      .map(t => Number((t.textContent ?? '').replace(/,/g, '')))
      .filter(n => Number.isFinite(n))
    expect(Math.max(...ticks)).toBeLessThan(10_000)
    expect(screen.getByText(/shown above the chart so later turns stay readable/)).toBeInTheDocument()
  })

  it('selecting the start row drives the detail like any other turn', () => {
    const { container } = render(<ContextBreakdownPanel trace={withStart()} />)
    fireEvent.click(container.querySelector('[data-start-row]') as HTMLElement)
    expect(container.querySelector('[data-start-row]')).toHaveAttribute('aria-pressed', 'true')
    const detail = screen.getByTestId('selected-turn-detail')
    expect(within(detail).getByText('Turn 1')).toBeInTheDocument()
    expect(within(detail).getByText('38,500')).toBeInTheDocument()
    expect(within(detail).queryByText(/previous/)).toBeNull()
    // Only one turn is pressed at a time, across the row and the chart: the
    // chart draws no marker, no value label and presses no column for it.
    const pressed = screen.getAllByRole('button', { pressed: true })
    expect(pressed).toHaveLength(1)
    expect(pressed[0]).toHaveAttribute('data-start-row')
    expect(screen.queryByTestId('selected-turn-marker')).toBeNull()
    expect(screen.queryByTestId('selected-turn-value')).toBeNull()
    expect(container.querySelectorAll('svg circle')).toHaveLength(0)
    // The keyboard still enters the chart at its newest turn.
    expect(screen.getByRole('button', { name: /^Turn 3:/ })).toHaveAttribute('tabindex', '0')
    fireEvent.keyDown(screen.getByRole('button', { name: /^Turn 3:/ }), { key: 'ArrowLeft' })
    expect(screen.getByRole('button', { name: /^Turn 2:/ })).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByTestId('selected-turn-marker')).toBeInTheDocument()
  })
})

describe('thirty turns in a narrow side panel', () => {
  const thirty = () =>
    trace({ turns: Array.from({ length: MAX_CHART_TURNS }, (_, i) => turnOf({ your_message: 10 + i, memory: 100 + (i % 7) * 40 })) })

  it('draws at most floor(plotWidth / 44) + 2 axis labels at 264px', () => {
    const { container } = render(<ContextBreakdownPanel trace={thirty()} chartWidth={264} />)
    const plotWidth = 264 - 60 - 28
    const labels = container.querySelectorAll('svg [data-axis-label]')
    expect(labels.length).toBeLessThanOrEqual(Math.floor(plotWidth / 44) + 2)
    expect(labels.length).toBeGreaterThanOrEqual(2)
    // The selected (last) turn is always labelled.
    expect(container.querySelector(`svg [data-axis-label="${MAX_CHART_TURNS}"]`)).not.toBeNull()
  })

  it('keeps every turn reachable: one pointer surface maps a click to the nearest turn', () => {
    const { container } = render(<ContextBreakdownPanel trace={thirty()} chartWidth={264} />)
    const surface = screen.getByTestId('turn-pointer-surface')
    const plotWidth = 264 - 60 - 28
    const columnWidth = plotWidth / (MAX_CHART_TURNS - 1)
    // JSDOM reports the wrapper at x=0, so clientX is a plot-relative offset.
    fireEvent.click(surface, { clientX: 60 + columnWidth * 10 })
    expect(screen.getByRole('button', { name: /^Turn 11:/ })).toHaveAttribute('aria-pressed', 'true')
    expect(container.querySelectorAll('button[data-turn]')).toHaveLength(MAX_CHART_TURNS)
    // Arrow keys still walk the per-turn buttons.
    fireEvent.keyDown(screen.getByRole('button', { name: /^Turn 11:/ }), { key: 'ArrowLeft' })
    expect(screen.getByRole('button', { name: /^Turn 10:/ })).toHaveAttribute('aria-pressed', 'true')
  })

  it('has no pointer surface and a value label when the columns are wide', () => {
    render(<ContextBreakdownPanel trace={trace({ turns: thirty().turns.slice(0, 6) })} chartWidth={720} />)
    expect(screen.queryByTestId('turn-pointer-surface')).toBeNull()
    expect(screen.getByTestId('selected-turn-value')).toBeInTheDocument()
  })

  it('drops the value label below 480px — the detail repeats the number', () => {
    const t = thirty()
    render(<ContextBreakdownPanel trace={t} chartWidth={390} />)
    expect(screen.queryByTestId('selected-turn-value')).toBeNull()
    expect(screen.getByTestId('selected-turn-detail')).toHaveTextContent(String(t.turns[t.turns.length - 1].total_chars))
  })
})

describe('placement: a per-session tab, not a global page', () => {
  // These two dynamically import `pages/chat/SidePanel`, whose transform is
  // charged to whichever test triggers it first. That exceeded the 15s default on
  // a Windows checkout, so both failed there while passing on CI's Linux runner.
  // The import stays DYNAMIC on purpose: hoisting it to a static top-level import
  // moved the same work into the file's import phase and took the file from 32s to
  // 259s (measured), because every test then waits on that graph. A per-test budget
  // is the cheaper half of that trade -- it is not masking a hang, the work is real
  // and bounded.
  it('is registered as a side-panel view next to Logs', async () => {
    const { PINNED_VIEWS } = await import('../hooks/usePanelTabs')
    const { NEW_MENU_LABEL_KEY, NEW_MENU_DESC_KEY } = await import('../pages/chat/SidePanel')
    // Opened from the + menu like Logs — not auto-pinned, since a session the
    // user never inspects should not carry a permanent extra tab.
    expect(PINNED_VIEWS).not.toContain('context')
    // Both maps are keyed by ViewKind, so a missing entry is a build error; this
    // asserts the pair exists so the menu row can never render label-less.
    expect(NEW_MENU_LABEL_KEY.context).toBeTruthy()
    expect(NEW_MENU_DESC_KEY.context).toBeTruthy()
  }, SIDE_PANEL_IMPORT_TIMEOUT_MS)

  it('is hidden from the + menu unless Developer Mode is on', async () => {
    const { newMenuSections } = await import('../pages/chat/SidePanel')
    const kinds = (o: { devMode: boolean; terminalEnabled: boolean }) =>
      newMenuSections({ ...o, summaryEnabled: true }).flatMap(g => g.items).map(i => i.kind)
    // Dev mode off: Context breakdown is not offered — it is a developer surface.
    expect(kinds({ devMode: false, terminalEnabled: true })).not.toContain('context')
    // Dev mode on: it appears (right after Logs, closing the diagnostics group).
    const on = kinds({ devMode: true, terminalEnabled: true })
    expect(on).toContain('context')
    expect(on.indexOf('context')).toBe(on.indexOf('logs') + 1)
    // The gate is independent of the Terminal gate, and it covers Logs as
    // well: both diagnostics views are Developer-Mode-only, so with dev mode off
    // neither is offered no matter what Terminal is doing.
    expect(kinds({ devMode: false, terminalEnabled: false })).not.toContain('logs')
    expect(kinds({ devMode: true, terminalEnabled: false })).toContain('logs')
    expect(kinds({ devMode: false, terminalEnabled: false })).not.toContain('terminal')
  }, SIDE_PANEL_IMPORT_TIMEOUT_MS)

  it('carries no session picker — the tab IS the session', () => {
    render(
      <ContextBreakdownPanel
        trace={trace({
          slot: 'chat-1',
          turns: [turnOf({ memory: 900, your_message: 100 }, { phase: 'session_start' })],
          context_window: 1_000_000,
        })}
        isLoading={false}
      />,
    )
    expect(screen.queryByRole('combobox')).toBeNull()
  })
})
