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
  type TrendRow,
} from '../pages/ContextBreakdownPanel'
import type { LatestTurn } from '../pages/ContextEngineBreakdown'

afterEach(cleanup)

const turn = (over: Partial<ContextTurn> = {}): ContextTurn => ({
  phase: 'per_turn',
  blocks: { request_header: 1576, your_message: 6 },
  total_chars: 1582,
  ordinal: 0,
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

const latestOf = (over: Partial<LatestTurn> = {}): LatestTurn => ({
  turn: 6,
  phase: 'per_turn',
  ours: { message: 80, memory: 1200, rules: 500, skills: 240, other: 140 },
  loop_chars: 6000,
  loop_estimated: true,
  engine: null,
  ttft_ms: null,
  context_used: 2000,
  context_window: 200000,
  ...over,
})

const rowOf = (turn: number, loop = 1000): TrendRow => {
  const ours = { message: 80, memory: 100 * turn, rules: 500, skills: 240, other: 140 }
  return {
    turn,
    ours,
    loop,
    // Each kept turn carries its own whole view, as the trace handler builds it.
    detail: latestOf({
      turn,
      phase: turn === 1 ? 'session_start' : 'per_turn',
      ours,
      context_used: 1000 * turn,
      blocks: { your_message: 80, memory: 100 * turn, lessons: 500, skill_index: 240, surface: 140 },
    }),
  }
}

describe('ContextBreakdownPanel rendering, from the fold\'s bounded state', () => {
  const fiveTurns = () =>
    trace({
      latest: latestOf(),
      trend: [2, 3, 4, 5, 6].map(n => rowOf(n)),
      start: rowOf(1, 20000),
      earlier: 1,
      turn_count: 6,
      chars_per_token: 4,
    })

  it('draws one band per category and one for the rest of the context, bottom to top', () => {
    const { container } = render(<ContextBreakdownPanel trace={fiveTurns()} chartWidth={600} />)
    const layers = Array.from(container.querySelectorAll('svg g[data-category]')).map(g => g.getAttribute('data-category'))
    expect(layers).toEqual([...CATEGORIES, 'loop'])
    // One real button per plotted turn, so any kept turn can be picked.
    expect(container.querySelectorAll('button[data-turn]:not([data-start-row])')).toHaveLength(5)
  })

  it('heads an unsplit newest turn with its own token total, not Kiro Crew blocks alone', () => {
    render(<ContextBreakdownPanel trace={fiveTurns()} chartWidth={600} />)
    const detail = screen.getByTestId('selected-turn-detail')
    expect(within(detail).getByText(/Turn 6 · latest/)).toBeInTheDocument()
    expect(screen.getByTestId('whole-total').textContent).not.toMatch(/^2,160/)
    // Kiro Crew is the first row, with its total.
    const crew = detail.querySelector('[data-engine-row="crew"]') as HTMLElement
    expect(crew.textContent).toMatch(/Kiro Crew.*2,080/)
  })

  it('lists only the chart parts that are non-zero in the plotted turns', () => {
    const rows = [2, 3].map(n => ({ ...rowOf(n), ours: { ...rowOf(n).ours, memory: 0 } }))
    render(<ContextBreakdownPanel trace={trace({ latest: latestOf({ turn: 3 }), trend: rows })} chartWidth={600} />)
    const legend = screen.getByTestId('trend').textContent ?? ''
    expect(legend).not.toContain('Kiro Crew memory')
    expect(legend).toContain('Kiro Crew rules')
    expect(legend).toContain('Rest of the context')
  })

  it('lists the newest session-start turn above the chart and counts it among the earlier turns', () => {
    render(<ContextBreakdownPanel trace={fiveTurns()} chartWidth={600} />)
    const startRow = document.querySelector('[data-start-row]') as HTMLElement
    expect(startRow.textContent).toContain('Turn 1 · session start')
    // The only earlier turn is the start row above, so no line repeats it.
    expect(screen.queryByTestId('earlier-turns')).toBeNull()
  })

  it('says how many turns precede the ring, exactly as the fold counted them', () => {
    const rows = Array.from({ length: MAX_CHART_TURNS }, (_, i) => rowOf(i + 46))
    render(<ContextBreakdownPanel trace={trace({ latest: latestOf({ turn: 75 }), trend: rows, earlier: 45 })} chartWidth={600} />)
    expect(screen.getByText('Turns 1–45 earlier')).toBeInTheDocument()
    expect(screen.getByText(`Last ${MAX_CHART_TURNS} turns`)).toBeInTheDocument()
  })

  it('lists the rest of an unsplit turn as one grey row in estimated tokens', () => {
    render(<ContextBreakdownPanel trace={fiveTurns()} chartWidth={600} />)
    const rest = document.querySelector('[data-engine-row="rest"]') as HTMLElement
    expect(rest.textContent).toMatch(/^Rest of the contextHow to split it~\d/)
    expect(screen.getByTestId('engine-caption').textContent).toBe('Figures marked ~ are estimates (characters / 4).')
  })

  it('renders a readable empty state for a session with no recorded turns', () => {
    render(<ContextBreakdownPanel trace={trace({ turns: [] })} />)
    expect(screen.getByText(/No context breakdown recorded/i)).toBeInTheDocument()
  })

  it('does not promise data while recording is switched off', () => {
    render(<ContextBreakdownPanel trace={trace({ turns: [], recording: false, env_file: '/srv/crew/.env' })} />)
    expect(screen.getByText(/Context breakdown is not recorded while the crew log is off\. Remove KIROCREW_CREW_LOG from \/srv\/crew\/\.env/)).toBeInTheDocument()
    expect(screen.queryByText(/yet/)).toBeNull()
  })

  it('shows a loading state before the first payload', () => {
    const { container } = render(<ContextBreakdownPanel trace={undefined} isLoading />)
    expect(within(container).getByText(/Loading context breakdown/i)).toBeInTheDocument()
  })
})

describe('picking a past turn shows that turn whole, as the newest one is shown', () => {
  const sixTurns = () =>
    trace({
      latest: latestOf({ turn: 6, blocks: { your_message: 80, memory: 1200 } }),
      trend: [2, 3, 4, 5, 6].map(n => rowOf(n)),
      start: rowOf(1, 20000),
      earlier: 1,
      turn_count: 6,
      chars_per_token: 4,
    })

  it('moves the detail to a clicked turn and back with the arrow keys', () => {
    render(<ContextBreakdownPanel trace={sixTurns()} chartWidth={600} />)
    const turn4 = screen.getByRole('button', { name: /^Turn 4:/ })
    fireEvent.click(turn4)
    const detail = () => screen.getByTestId('selected-turn-detail')
    expect(within(detail()).getByText('Turn 4')).toBeInTheDocument()
    expect(within(detail()).queryByText(/latest/)).toBeNull()
    // Turn 4's own occupancy reading heads its panel.
    expect(screen.getByTestId('whole-total').textContent).toBe('4,000')
    expect(turn4).toHaveAttribute('aria-pressed', 'true')
    // One tab stop: only the pressed button is tabbable.
    expect(turn4).toHaveAttribute('tabindex', '0')
    expect(screen.getByRole('button', { name: /^Turn 5:/ })).toHaveAttribute('tabindex', '-1')
    fireEvent.keyDown(turn4, { key: 'ArrowRight' })
    expect(screen.getByRole('button', { name: /^Turn 5:/ })).toHaveAttribute('aria-pressed', 'true')
    expect(within(detail()).getByText('Turn 5')).toBeInTheDocument()
    fireEvent.keyDown(screen.getByRole('button', { name: /^Turn 5:/ }), { key: 'ArrowLeft' })
    fireEvent.keyDown(screen.getByRole('button', { name: /^Turn 4:/ }), { key: 'ArrowLeft' })
    expect(screen.getByRole('button', { name: /^Turn 3:/ })).toHaveAttribute('aria-pressed', 'true')
    // The newest column is the newest turn itself.
    fireEvent.click(screen.getByRole('button', { name: /^Turn 6:/ }))
    expect(within(detail()).getByText(/Turn 6 · latest/)).toBeInTheDocument()
  })

  it('selecting the start row drives the detail like any other turn', () => {
    const { container } = render(<ContextBreakdownPanel trace={sixTurns()} chartWidth={600} />)
    const row = container.querySelector('[data-start-row]') as HTMLElement
    expect(row).toHaveAttribute('aria-pressed', 'false')
    fireEvent.click(row)
    expect(row).toHaveAttribute('aria-pressed', 'true')
    const detail = screen.getByTestId('selected-turn-detail')
    expect(within(detail).getByText('Turn 1 · session start')).toBeInTheDocument()
    // Only one turn is pressed at a time, across the row and the chart: the chart
    // draws no marker for it.
    const pressed = screen.getAllByRole('button', { pressed: true })
    expect(pressed).toHaveLength(1)
    expect(pressed[0]).toHaveAttribute('data-start-row')
    expect(screen.queryByTestId('selected-turn-marker')).toBeNull()
    // The keyboard still enters the chart at its newest turn.
    expect(screen.getByRole('button', { name: /^Turn 6:/ })).toHaveAttribute('tabindex', '0')
    fireEvent.keyDown(screen.getByRole('button', { name: /^Turn 6:/ }), { key: 'ArrowLeft' })
    expect(screen.getByRole('button', { name: /^Turn 5:/ })).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByTestId('selected-turn-marker')).toBeInTheDocument()
  })

  it('keeps the picked turn when a new turn shifts the full ring, and says so once it leaves', () => {
    // Ring 2..31, the newest turn 31. Turn 12 is picked.
    const ring = (from: number) => Array.from({ length: MAX_CHART_TURNS }, (_, i) => rowOf(from + i))
    const traceAt = (from: number) =>
      trace({ latest: latestOf({ turn: from + MAX_CHART_TURNS - 1 }), trend: ring(from), earlier: from - 1 })
    const { rerender } = render(<ContextBreakdownPanel trace={traceAt(2)} chartWidth={600} />)
    fireEvent.click(screen.getByRole('button', { name: /^Turn 12:/ }))
    const detail = () => screen.getByTestId('selected-turn-detail')
    expect(within(detail()).getByText('Turn 12')).toBeInTheDocument()
    // Turn 32 completes: every column moves one left. The panel stays on turn 12.
    rerender(<ContextBreakdownPanel trace={traceAt(3)} chartWidth={600} />)
    expect(within(detail()).getByText('Turn 12')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /^Turn 12:/ })).toHaveAttribute('aria-pressed', 'true')
    expect(screen.queryByTestId('picked-gone')).toBeNull()
    // Ten more turns and turn 12 has left the ring: the newest turn, with a note.
    rerender(<ContextBreakdownPanel trace={traceAt(13)} chartWidth={600} />)
    expect(within(detail()).getByText('Turn 42 · latest')).toBeInTheDocument()
    expect(screen.getByTestId('picked-gone').textContent).toBe('Turn 12 has left the chart, so the newest turn is shown.')
  })

  it('names the start row as a picker and marks it while its turn is shown', () => {
    const { container } = render(<ContextBreakdownPanel trace={sixTurns()} chartWidth={600} />)
    const row = screen.getByRole('button', { name: 'Show turn 1 (session start)' })
    expect(row).toBe(container.querySelector('[data-start-row]'))
    expect(within(row).getByTestId('start-row-state').textContent).toBe('Show')
    fireEvent.click(row)
    expect(row).toHaveAttribute('aria-pressed', 'true')
    expect(within(row).getByTestId('start-row-state').textContent).toBe('Shown above')
    // The row states the same size the turn's own headline does.
    expect(within(row).getByTestId('start-row-size').textContent).toBe(`${screen.getByTestId('whole-total').textContent} tokens`)
  })

  it('calls a session-start turn that is also the newest one thing in both places', () => {
    const start = rowOf(1, 20000)
    render(
      <ContextBreakdownPanel
        trace={trace({ latest: latestOf({ turn: 1, phase: 'session_start' }), trend: [], start })}
        chartWidth={600}
      />,
    )
    const detail = screen.getByTestId('selected-turn-detail')
    expect(within(detail).getByText('Turn 1 · session start · latest')).toBeInTheDocument()
    const row = screen.getByRole('button', { name: 'Show turn 1 (session start)' })
    // It is the turn shown, so the row is marked, and its size is the headline's.
    expect(row).toHaveAttribute('aria-pressed', 'true')
    expect(within(row).getByTestId('start-row-size').textContent).toBe(`${screen.getByTestId('whole-total').textContent} tokens`)
  })

  it('keeps every turn reachable: one pointer surface maps a click to the nearest turn', () => {
    const rows = Array.from({ length: MAX_CHART_TURNS }, (_, i) => rowOf(i + 2))
    const { container } = render(
      <ContextBreakdownPanel trace={trace({ latest: latestOf({ turn: 31 }), trend: rows })} chartWidth={264} />,
    )
    const surface = screen.getByTestId('turn-pointer-surface')
    const plotWidth = 264 - 60 - 28
    const columnWidth = plotWidth / (MAX_CHART_TURNS - 1)
    // JSDOM reports the wrapper at x=0, so clientX is a plot-relative offset.
    fireEvent.click(surface, { clientX: 60 + columnWidth * 10 })
    expect(screen.getByRole('button', { name: /^Turn 12:/ })).toHaveAttribute('aria-pressed', 'true')
    expect(container.querySelectorAll('button[data-turn]')).toHaveLength(MAX_CHART_TURNS)
    expect(within(screen.getByTestId('selected-turn-detail')).getByText('Turn 12')).toBeInTheDocument()
    // Arrow keys still walk the per-turn buttons.
    fireEvent.keyDown(screen.getByRole('button', { name: /^Turn 12:/ }), { key: 'ArrowLeft' })
    expect(screen.getByRole('button', { name: /^Turn 11:/ })).toHaveAttribute('aria-pressed', 'true')
  })

  it('a past turn shows its own block names and its own engine rows', () => {
    const measured = {
      ...rowOf(3),
      detail: latestOf({
        turn: 3,
        loop_estimated: false,
        loop_chars: 50_000,
        engine: {
          total_chars: 52_000, model_calls: 1,
          categories: { history: 1_900, steering: 30_000, skillsIndex: 20_100 },
          percent: { history: 3.7, steering: 57.7, skillsIndex: 38.6 },
          counts: { skillsIndex: 28 }, parts: {}, window: null, media: null,
          history: { user: 1_900, assistant: 0, thinking: 0, compaction_summary: 0 },
          unknown_chars: 0, compacted: false, schema_known: true, approximate: false,
          crew_chars: 1_200, crew_split: { message: 80, memory: 300, rules: 500, skills: 240, other: 80 },
          user_other_chars: 700, mismatch_chars: 0,
        },
        blocks: { your_message: 80, task_facts: 300, critical_rules: 500, loaded_skill: 240 },
      }),
    }
    render(
      <ContextBreakdownPanel
        trace={trace({ latest: latestOf({ turn: 4, blocks: { your_message: 9 } }), trend: [rowOf(2), measured, rowOf(4)] })}
        chartWidth={600}
      />,
    )
    fireEvent.click(screen.getByRole('button', { name: /^Turn 3:/ }))
    const detail = screen.getByTestId('selected-turn-detail')
    // The engine's own rows for turn 3, not the newest turn's.
    expect(screen.getByTestId('whole-total').textContent).toBe('52,000')
    expect(detail.querySelector('[data-engine-row="steering"]')!.textContent).toMatch(/30,000/)
    expect(detail.querySelector('[data-engine-row="skillsIndex"]')!.textContent).toMatch(/\(28\)/)
    // Its own blocks, by name.
    fireEvent.click(within(detail).getByRole('button', { name: /blocks sent this turn/ }))
    const names = Array.from(detail.querySelectorAll('[data-block]')).map(li => li.getAttribute('data-block'))
    expect(names).toEqual(['critical_rules', 'task_facts', 'loaded_skill', 'your_message'])
    expect(within(detail).getByText('Must-follow rules')).toBeInTheDocument()
    expect(within(detail).getByText('Facts recalled for this task')).toBeInTheDocument()
  })

  it('counts every block sent in the heading, before grouping and past the list', () => {
    // Two every-turn members list as one row, and still count as the two blocks sent.
    const blocks = { your_message: 80, task_facts: 300, surface: 40, request_header: 60 }
    const latest = latestOf({ blocks, blocks_omitted: 6 })
    render(<ContextBreakdownPanel trace={trace({ latest, trend: [rowOf(2), rowOf(3)] })} chartWidth={600} />)
    // A disclosure that looks like one: right chevron while shut, down once open.
    const toggle = screen.getByRole('button', { name: '10 Kiro Crew blocks sent this turn' })
    expect(toggle.getAttribute('aria-expanded')).toBe('false')
    expect(toggle.querySelector('[data-testid="blocks-chevron"]')!.getAttribute('class')).toMatch(/chevron-right/)
    fireEvent.click(toggle)
    expect(toggle.querySelector('[data-testid="blocks-chevron"]')!.getAttribute('class')).toMatch(/chevron-down/)
    const listed = Array.from(document.querySelectorAll('[data-block]')).map(li => li.getAttribute('data-block'))
    expect(listed).toEqual(['task_facts', 'every_turn', 'your_message'])
    expect(screen.getByTestId('blocks-omitted').textContent).toBe('6 more blocks not shown')
    // The count of the rest is a note, not a control.
    expect(screen.getByTestId('blocks-omitted').tagName).toBe('LI')
    expect(screen.getByTestId('blocks-omitted').getAttribute('class')).toMatch(/cursor-default/)
  })

  it("asks for a picked past turn the trace does not carry, and shows that turn's read", () => {
    // As the gateway sends a row: the chart's numbers only.
    const slim = (turn: number): TrendRow => {
      const { detail: _detail, ...rest } = rowOf(turn)
      return rest
    }
    const asked: Array<number | null> = []
    const onPastTurn = (n: number | null) => asked.push(n)
    const t = trace({ latest: latestOf({ turn: 4 }), trend: [slim(2), slim(3), slim(4)] })
    const { rerender } = render(<ContextBreakdownPanel trace={t} chartWidth={600} onPastTurn={onPastTurn} />)
    expect(asked.at(-1)).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: /^Turn 3:/ }))
    expect(asked.at(-1)).toBe(3)
    // While it is read: the turn's name and a loading line, never the newest turn's panel.
    expect(screen.getByTestId('past-turn-read').textContent).toMatch(/Turn 3/)
    expect(screen.queryByTestId('selected-turn-detail')).toBeNull()
    // The read lands: that turn's own panel.
    const read = latestOf({ turn: 3, context_used: 3_000, blocks: { your_message: 80, lessons: 500 } })
    rerender(<ContextBreakdownPanel trace={t} chartWidth={600} onPastTurn={onPastTurn} pastTurn={{ ordinal: 3, detail: read, loading: false, error: null }} />)
    expect(screen.getByTestId('whole-total').textContent).toBe('3,000')
    // A failed read says so in place.
    rerender(<ContextBreakdownPanel trace={t} chartWidth={600} onPastTurn={onPastTurn} pastTurn={{ ordinal: 3, detail: null, loading: false, error: 'turn is not kept' }} />)
    expect(within(screen.getByTestId('past-turn-read')).getByText('turn is not kept')).toBeInTheDocument()
    // Back on the newest turn, nothing is asked for.
    fireEvent.click(screen.getByRole('button', { name: /^Turn 4:/ }))
    expect(asked.at(-1)).toBeNull()
  })

  it('always offers the way back to a running newest turn that is not a column yet', () => {
    // Turn 9 is still running: the chart stops at turn 8.
    const t = trace({ latest: latestOf({ turn: 9 }), trend: [rowOf(6), rowOf(7), rowOf(8)] })
    render(<ContextBreakdownPanel trace={t} chartWidth={600} />)
    const heading = () => screen.getByTestId('selected-turn-detail').querySelector('strong')!.textContent
    expect(heading()).toBe('Turn 9 · latest')
    expect(screen.queryByTestId('show-latest')).toBeNull()
    // The chart says why the newest turn has no column.
    expect(screen.getByTestId('chart-running').textContent).toBe('Turn 9 is still running and not charted yet.')
    fireEvent.click(screen.getByRole('button', { name: /^Turn 7:/ }))
    expect(heading()).toBe('Turn 7')
    // The button names the newest turn and brings it back.
    fireEvent.click(screen.getByTestId('show-latest'))
    expect(heading()).toBe('Turn 9 · latest')
    expect(screen.queryByTestId('show-latest')).toBeNull()
    // So do Escape and Home, from a focused column.
    for (const key of ['Escape', 'Home']) {
      const column = screen.getByRole('button', { name: /^Turn 6:/ })
      fireEvent.click(column)
      expect(heading()).toBe('Turn 6')
      fireEvent.keyDown(column, { key })
      expect(heading()).toBe('Turn 9 · latest')
    }
  })

  it('says the unit changes when an unmeasured turn heads in tokens over a character chart', () => {
    // Turn 9 not measured yet: its headline is the token reading; the ring was measured.
    const t = trace({ latest: latestOf({ turn: 9, context_used: 38_500 }), trend: [rowOf(6), rowOf(7), rowOf(8)] })
    render(<ContextBreakdownPanel trace={t} chartWidth={600} />)
    expect(screen.getByTestId('chart-unit').textContent).toBe('Unit: characters')
    expect(screen.getByTestId('unit-switch').textContent).toBe('The headline is in tokens; the chart below counts characters.')
  })

  it('speaks tokens in the chart when the panel does, on a harness that never splits', () => {
    const flat = (turn: number): TrendRow => ({ ...rowOf(turn, 0) })
    const t = trace({ latest: latestOf({ turn: 4, context_used: 49_000 }), trend: [flat(2), flat(3), flat(4)] })
    render(<ContextBreakdownPanel trace={t} chartWidth={600} />)
    expect(screen.getByTestId('chart-unit').textContent).toBe('Unit: tokens (estimated)')
    // Turn 3 holds 1,260 characters of Kiro Crew blocks: about 315 tokens at 4 a token.
    expect(screen.getByRole('button', { name: /^Turn 3:/ }).getAttribute('aria-label')).toBe('Turn 3: about 315 tokens')
    cleanup()
    // A measured ring stays in characters.
    render(<ContextBreakdownPanel trace={trace({ latest: latestOf({ turn: 4 }), trend: [rowOf(2), rowOf(3), rowOf(4)] })} chartWidth={600} />)
    expect(screen.getByTestId('chart-unit').textContent).toBe('Unit: characters')
  })

  it('fills each token column up to its own harness total with the rest band', () => {
    // Turns 2 and 4 reported a total; turn 3 did not.
    const flat = (turn: number, used: number): TrendRow => ({ ...rowOf(turn, 0), used })
    const t = trace({
      latest: latestOf({ turn: 4, context_used: 84_000 }),
      trend: [flat(2, 80_000), flat(3, 0), flat(4, 84_000)],
    })
    render(<ContextBreakdownPanel trace={t} chartWidth={600} />)
    const aria = (n: number) => screen.getByRole('button', { name: new RegExp(`^Turn ${n}:`) }).getAttribute('aria-label')
    expect(aria(4)).toBe('Turn 4: about 84,000 tokens')
    expect(aria(2)).toBe('Turn 2: about 80,000 tokens')
    // No total: Kiro Crew's own estimate only (1,260 characters, about 315 tokens).
    expect(aria(3)).toBe('Turn 3: about 315 tokens')
    expect(document.querySelector('[data-category="loop"]')).not.toBeNull()
    expect(document.querySelector('[data-legend="loop"]')!.textContent).toBe('Rest of the context')
    expect(screen.getByTestId('chart-no-total').textContent).toBe(' Turns with no reported total are hatched.')
    // Turn 3 is a hatched gap: its column is marked, and the top line breaks there.
    const hatched = document.querySelectorAll('[data-testid="no-total-column"]')
    expect(Array.from(hatched).map(h => h.getAttribute('data-turn'))).toEqual(['2'])
    expect(document.querySelectorAll('[data-category="loop"] polygon')).toHaveLength(2)
    // A token chart heads in tokens too: no unit note.
    expect(screen.queryByTestId('unit-switch')).toBeNull()
    cleanup()
    // Every turn reported a total: no caption about missing ones.
    render(
      <ContextBreakdownPanel
        trace={trace({ latest: latestOf({ turn: 4, context_used: 84_000 }), trend: [flat(2, 80_000), flat(4, 84_000)] })}
        chartWidth={600}
      />,
    )
    expect(screen.queryByTestId('chart-no-total')).toBeNull()
  })

  it('heads the start row with the size the trace sends for it', () => {
    const { detail: _detail, ...start } = rowOf(1)
    render(
      <ContextBreakdownPanel
        trace={trace({ latest: latestOf({ turn: 4 }), trend: [rowOf(2), rowOf(3), rowOf(4)], start: { ...start, figure: { n: 85_312, unit: 'tokens' } } })}
        chartWidth={600}
      />,
    )
    expect(screen.getByTestId('start-row-size').textContent).toBe('85,312 tokens')
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

describe('placement: a per-session tab, not a global page', () => {
  // These import the menu's owner module, not the `pages/chat/SidePanel` facade:
  // the owner reaches about a hundred modules, the facade about fifteen hundred
  // (framer-motion, react-markdown, katex, highlight.js), whose transform cost
  // exceeded the 15s default on a Windows checkout. The facade re-exports these
  // same bindings (`SidePanel.owners.test.tsx` pins that), so nothing is lost.
  it('is registered as a side-panel view next to Logs', async () => {
    const { PINNED_VIEWS } = await import('../hooks/usePanelTabs')
    const { NEW_MENU_LABEL_KEY, NEW_MENU_DESC_KEY } = await import('../pages/chat/SidePanelAddMenu')
    // Opened from the + menu like Logs — not auto-pinned, since a session the
    // user never inspects should not carry a permanent extra tab.
    expect(PINNED_VIEWS).not.toContain('context')
    // Both maps are keyed by ViewKind, so a missing entry is a build error; this
    // asserts the pair exists so the menu row can never render label-less.
    expect(NEW_MENU_LABEL_KEY.context).toBeTruthy()
    expect(NEW_MENU_DESC_KEY.context).toBeTruthy()
  })

  it('is offered in the + menu right after Logs, whatever Terminal is doing', async () => {
    const { newMenuSections } = await import('../pages/chat/SidePanelAddMenu')
    const kinds = (o: { terminalEnabled: boolean }) =>
      newMenuSections({ ...o, summaryEnabled: true }).flatMap(g => g.items).map(i => i.kind)
    for (const terminalEnabled of [false, true]) {
      const offered = kinds({ terminalEnabled })
      expect(offered).toContain('logs')
      expect(offered.indexOf('context')).toBe(offered.indexOf('logs') + 1)
    }
    expect(kinds({ terminalEnabled: false })).not.toContain('terminal')
  })

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
