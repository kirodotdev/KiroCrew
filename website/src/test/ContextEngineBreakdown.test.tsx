/**
 * The agent engine's measurement on the Context breakdown page.
 *
 *  - a turn the engine measured lists its categories, largest first, with history
 *    split into this app's prompts and the user text it did not compose.
 *  - a turn it did not measure says so; it never shows zeros.
 *  - a trace with no measured turn at all renders the panel exactly as before.
 *  - a count that does not add up is shown, not hidden.
 */
import { describe, it, expect, afterEach } from 'vitest'
import { render, screen, cleanup, within } from '@testing-library/react'

import { ContextBreakdownPanel, type ContextTrace, type ContextTurn } from '../pages/ContextBreakdownPanel'
import { engineRows, type EngineMeasurement } from '../pages/ContextEngineBreakdown'

afterEach(cleanup)

const engine = (over: Partial<EngineMeasurement> = {}): EngineMeasurement => ({
  total_chars: 59_333,
  model_calls: 1,
  categories: {
    kiroInstructions: 4_290,
    toolSpecs: 2_865,
    mcpTools: 6_246,
    skillsIndex: 14_563,
    steering: 18_077,
    toolIO: 102,
    history: 12_192,
    workspace: 939,
    other: 59,
  },
  history: { user: 12_173, assistant: 10, thinking: 9, compaction_summary: 0 },
  unknown_chars: 0,
  compacted: false,
  schema_known: true,
  crew_chars: 12_100,
  user_other_chars: 73,
  mismatch_chars: 0,
  ...over,
})

const turn = (over: Partial<ContextTurn> = {}): ContextTurn => ({
  ts: '2026-10-24T00:00:00Z',
  phase: 'per_turn',
  blocks: { your_message: 33, lessons: 4_000 },
  total_chars: 4_033,
  context_used: 2_000,
  context_window: 1_000_000,
  model: 'model-a',
  ordinal: 1,
  ...over,
})

const trace = (turns: ContextTurn[]): ContextTrace => ({
  slot: 'slot-1',
  turns,
  totals: {},
  injected_chars: 0,
  user_chars: 0,
  peak_context_used: 0,
  context_window: 0,
  window_days: 14,
})

describe('engineRows', () => {
  it('lists the categories largest first and splits history at the user text', () => {
    const rows = engineRows(engine())
    expect(rows.map(r => r.id)).toEqual([
      'steering',
      'skillsIndex',
      'history.crew',
      'mcpTools',
      'kiroInstructions',
      'toolSpecs',
      'workspace',
      'toolIO',
      'history.user_other',
      'other',
      'history.assistant',
      'history.thinking',
    ])
    // The rows add up to the request the engine measured.
    expect(rows.reduce((sum, r) => sum + r.chars, 0)).toBe(59_333)
  })

  it('clamps this app’s share to what the engine holds', () => {
    const rows = engineRows(engine({ crew_chars: 20_000, user_other_chars: 0, mismatch_chars: 7_827 }))
    expect(rows.find(r => r.id === 'history.crew')?.chars).toBe(12_173)
    expect(rows.find(r => r.id === 'history.user_other')).toBeUndefined()
  })

  it('keeps a category this version does not know as one row', () => {
    const rows = engineRows(engine({ unknown_chars: 321 }))
    expect(rows.find(r => r.id === 'unknown')?.chars).toBe(321)
  })

  it('names the unknown categories when the trace kept their names', () => {
    const rows = engineRows(engine({ unknown_chars: 321 }), ['futureThing'])
    expect(rows.find(r => r.id === 'unknown')?.label).toMatch(/futureThing/)
  })
})

describe('the engine section on the panel', () => {
  it('shows the selected turn’s categories and time to first output', () => {
    render(
      <ContextBreakdownPanel
        trace={trace([turn({ engine: engine(), ttft_ms: { total_ms: 2_870, setup_ms: 59, model_ms: 1_534 } })])}
        chartWidth={600}
      />,
    )
    const section = screen.getByTestId('engine-breakdown')
    expect(within(section).getByTestId('engine-ttft').textContent).toMatch(/2\.9/)
    expect(within(section).getByTestId('engine-ttft').textContent).toMatch(/1\.6/)
    const rows = within(section).getByTestId('engine-rows').querySelectorAll('[data-engine-row]')
    expect(rows[0].getAttribute('data-engine-row')).toBe('steering')
    expect(within(section).queryByTestId('engine-mismatch')).toBeNull()
    // Crew's own blocks are still listed above it.
    expect(screen.getByTestId('selected-turn-detail').querySelector('[data-category-row="rules"]')).not.toBeNull()
  })

  it('says a turn was not measured instead of drawing zeros', () => {
    render(
      <ContextBreakdownPanel
        trace={trace([turn({ engine: engine() }), turn({ ordinal: 2, engine: null, ttft_ms: null })])}
        chartWidth={600}
      />,
    )
    const section = screen.getByTestId('engine-breakdown')
    expect(within(section).getByTestId('engine-not-measured')).toBeTruthy()
    expect(within(section).queryByTestId('engine-rows')).toBeNull()
  })

  it('leaves a trace the engine never measured exactly as it was', () => {
    render(<ContextBreakdownPanel trace={trace([turn(), turn({ ordinal: 2 })])} chartWidth={600} />)
    expect(screen.queryByTestId('engine-breakdown')).toBeNull()
  })

  it('reports a count that does not add up', () => {
    render(
      <ContextBreakdownPanel
        trace={trace([turn({ engine: engine({ mismatch_chars: 3_000, user_other_chars: 0 }) })])}
        chartWidth={600}
      />,
    )
    expect(screen.getByTestId('engine-mismatch').textContent).toMatch(/3,000/)
  })

  it('says the agent loop summarized earlier turns when it did', () => {
    render(
      <ContextBreakdownPanel
        trace={trace([
          turn({
            engine: engine({
              mismatch_chars: 3_000,
              user_other_chars: 0,
              history: { user: 12_173, assistant: 10, thinking: 9, compaction_summary: 800 },
            }),
          }),
        ])}
        chartWidth={600}
      />,
    )
    expect(screen.getByTestId('engine-mismatch').textContent).toMatch(/because/)
  })

  it('marks numbers sent under a format this version does not know', () => {
    render(<ContextBreakdownPanel trace={trace([turn({ engine: engine({ schema_known: false }) })])} chartWidth={600} />)
    expect(screen.getByTestId('engine-schema-unknown')).toBeTruthy()
  })

  it('says the split is approximate after a turn was sent again', () => {
    render(<ContextBreakdownPanel trace={trace([turn({ engine: engine({ approximate: true }) })])} chartWidth={600} />)
    expect(screen.getByTestId('engine-approximate')).toBeTruthy()
  })
})
