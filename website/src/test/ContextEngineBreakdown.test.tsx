/**
 * The newest turn's breakdown as one flat list. Row 1 is always Kiro Crew (its four
 * blocks, open by default); then, largest first, Messages (the engine's history less
 * Kiro Crew's blocks) and every other category, zero rows hidden. The rows add up to
 * the request's characters. The bar draws Kiro Crew first as its own group; the
 * window share sits on a thin bar of its own. A turn that was not split shows Kiro
 * Crew, Your messages and one Rest of the context row, each number with its unit.
 */
import { describe, it, expect, afterEach } from 'vitest'
import { render, screen, cleanup, within, fireEvent } from '@testing-library/react'

import { ContextBreakdownPanel, type ContextTrace } from '../pages/ContextBreakdownPanel'
import { barSegments, breakdownOf, remainderTokens, type EngineMeasurement, type LatestTurn, type Row } from '../pages/ContextEngineBreakdown'

afterEach(cleanup)

const ours = { message: 100, memory: 2_000, rules: 6_000, skills: 3_000, other: 1_000 }

const engine = (over: Partial<EngineMeasurement> = {}): EngineMeasurement => ({
  total_chars: 59_333,
  model_calls: 1,
  categories: {
    kiroInstructions: 4_290,
    toolSpecs: 2_865,
    mcpTools: 6_246,
    agentsIndex: 0,
    skillsIndex: 14_563,
    steering: 18_077,
    toolIO: 102,
    history: 12_192,
    workspace: 939,
    memory: 0,
    other: 59,
  },
  percent: {
    kiroInstructions: 7.2,
    toolSpecs: 4.8,
    mcpTools: 10.5,
    agentsIndex: 0,
    skillsIndex: 24.5,
    steering: 30.5,
    toolIO: 0.2,
    history: 20.6,
    workspace: 1.6,
    memory: 0,
    other: 0.1,
  },
  counts: { agentsIndex: 0, skillsIndex: 28, powersIndex: 0 },
  parts: {
    toolIO: { input: 20, output: 82 },
    workspace: { file_tree: 0, open_files: 0, environment: 939, repositories: 0 },
  },
  window: { size_tokens: 1_000_000, usage_percent: 5.8616 },
  media: { images: 0, image_bytes: 0, documents: 0, document_bytes: 0 },
  history: { user: 12_173, assistant: 10, thinking: 9, compaction_summary: 0 },
  unknown_chars: 0,
  compacted: false,
  schema_known: true,
  crew_chars: 12_100,
  crew_split: ours,
  user_other_chars: 73,
  mismatch_chars: 0,
  ...over,
})

const latest = (over: Partial<LatestTurn> = {}): LatestTurn => ({
  turn: 3,
  phase: 'per_turn',
  ours,
  loop_chars: 59_333 - 12_100,
  loop_estimated: false,
  engine: engine(),
  ttft_ms: null,
  context_used: 2_000,
  context_window: 1_000_000,
  ...over,
})

const trace = (l: LatestTurn | null): ContextTrace => ({
  slot: 'slot-1',
  turns: [],
  totals: {},
  injected_chars: 0,
  user_chars: 0,
  peak_context_used: 0,
  context_window: 0,
  window_days: 14,
  latest: l,
  trend: [],
  chars_per_token: 4,
})


const row = (rows: Row[], id: string) => rows.find(r => r.id === id)!

describe('breakdownOf, a measured turn', () => {
  it('puts Kiro Crew first, then every category by size, adding up to the total', () => {
    const b = breakdownOf(latest())
    expect(b.rows.map(r => r.id)).toEqual([
      'crew',
      'crew.message',
      'steering',
      'skillsIndex',
      'mcpTools',
      'kiroInstructions',
      'toolSpecs',
      'workspace',
      'toolIO',
      'history',
      'other',
    ])
    expect(b.rows.reduce((s, r) => s + (r.chars ?? 0), 0)).toBe(59_333)
    expect(b.overlap).toBe(0)
  })

  it('lists the four Kiro Crew blocks under Kiro Crew', () => {
    const crew = row(breakdownOf(latest()).rows, 'crew')
    expect(crew.label).toBe('Kiro Crew')
    expect(crew.chars).toBe(12_000)
    expect(crew.children!.map(r => r.label)).toEqual(['Kiro Crew rules', 'Kiro Crew skills', 'Kiro Crew memory', 'Kiro Crew session details'])
  })

  it("gives the engine's Other its own pink, never the grey of Rest of the context", () => {
    const other = row(breakdownOf(latest()).rows, 'other')
    expect(other.label).toBe('Other')
    expect(other.color).toBe('var(--ctx-eng-other)')
    const rest = row(breakdownOf(latest({ engine: null, context_used: 9_000 })).rows, 'rest')
    expect(rest.color).toBe('var(--ctx-eng-loop)')
    // The bar draws each part in its row's colour.
    expect(barSegments(breakdownOf(latest()).rows).find(s => s.id === 'other')?.color).toBe('var(--ctx-eng-other)')
  })

  it('puts Your messages right after Kiro Crew; Messages is the rest of the history', () => {
    const b = breakdownOf(latest())
    expect(b.rows[1].label).toBe('Your messages')
    const messages = row(b.rows, 'history')
    expect(messages.label).toBe('Messages')
    expect(messages.chars).toBe(12_192 - 12_000 - 100)
    expect(messages.children!.map(r => [r.label, r.chars])).toEqual([
      ['Other text sent as you', 73],
      ['Replies', 10],
      ['Reasoning', 9],
    ])
  })

  it('holds Messages at 0 and says so when Kiro Crew counts more than the history', () => {
    const b = breakdownOf(latest({ ours: { ...ours, rules: 7_000 } }))
    expect(row(b.rows, 'history')).toBeUndefined()
    expect(b.overlap).toBe(13_000 + 100 - 12_192)
  })

  it('draws Kiro Crew first on the bar, ending its group, then the rest in list order', () => {
    const segments = barSegments(breakdownOf(latest()).rows)
    expect(segments.slice(0, 4).map(s => s.id)).toEqual(['crew.rules', 'crew.skills', 'crew.memory', 'crew.other'])
    expect(segments[3].groupEnd).toBe(true)
    expect(segments[4].id).toBe('crew.message')
    expect(segments[5].id).toBe('steering')
    expect(segments.reduce((s, x) => s + x.share, 0)).toBeCloseTo(1, 2)
  })

  it('reads the window share apart from the bar', () => {
    const b = breakdownOf(latest())
    expect(b.filled).toBeCloseTo(0.058616)
    expect(b.usedTokens).toBe(58_616)
  })

  it('keeps list counts and the Calls / Results split', () => {
    const b = breakdownOf(latest())
    expect(row(b.rows, 'skillsIndex').count).toBe(28)
    expect(row(b.rows, 'toolIO').children!.map(r => [r.label, r.chars])).toEqual([
      ['Results', 82],
      ['Calls', 20],
    ])
    expect(row(b.rows, 'workspace').children).toBeUndefined()
  })
})

describe('remainderTokens', () => {
  it('is the total less the estimate, never below 0, with the overshoot kept', () => {
    expect(remainderTokens(85_339, 12_360)).toEqual({ tokens: 72_979, over: 0 })
    expect(remainderTokens(10_000, 10_000)).toEqual({ tokens: 0, over: 0 })
    expect(remainderTokens(1_000, 1_250)).toEqual({ tokens: 0, over: 250 })
  })
})

describe('breakdownOf, a turn that was not split', () => {
  it('tells every row in tokens, and the rows add up to the total', () => {
    const b = breakdownOf(latest({ engine: null, context_used: 85_311, context_window: 1_000_000 }), [], 4)
    expect(b.mode).toBe('crew')
    expect(b.rows.map(r => [r.id, r.tokens])).toEqual([
      ['crew', 3_000],
      ['crew.message', 25],
      ['rest', 85_311 - 3_025],
    ])
    expect(b.rows.reduce((s, r) => s + (r.tokens ?? 0), 0)).toBe(85_311)
    expect(b.rows.every(r => r.estimated)).toBe(true)
    // Kiro Crew's own total is the sum of its blocks' estimates, so it adds up too.
    const crew = b.rows[0]
    expect(crew.children!.reduce((s, r) => s + (r.tokens ?? 0), 0)).toBe(crew.tokens)
    expect(crew.chars).toBe(12_000)
    expect(b.rows[2].share).toBeCloseTo((85_311 - 3_025) / 85_311)
  })

  it('holds Rest of the context at 0 and flags it when the estimate passes the total', () => {
    const b = breakdownOf(latest({ engine: null, context_used: 2_000, context_window: 1_000_000 }), [], 4)
    expect(row(b.rows, 'rest').tokens).toBe(0)
    expect(b.overlap).toBe(3_025 - 2_000)
  })

  it('stays in characters when the turn reported no token total', () => {
    const b = breakdownOf(latest({ engine: null, context_used: 0 }))
    expect(b.rows.map(r => r.id)).toEqual(['crew', 'crew.message'])
    expect(b.rows.every(r => r.tokens === undefined)).toBe(true)
  })
})

describe('the one breakdown on the panel', () => {
  it('heads with characters and tokens, then the bar, then the window bar', () => {
    render(<ContextBreakdownPanel trace={trace(latest({ ttft_ms: { total_ms: 2_870 } }))} chartWidth={600} />)
    expect(screen.getByTestId('whole-total').textContent).toBe('59,333')
    expect(screen.getByTestId('whole-unit').textContent).toBe('characters · about 58,616 tokens, from the window reading')
    expect(screen.getByTestId('window-usage').textContent).toBe('6% of the 1M-token window')
    const bar = screen.getByTestId('engine-bar')
    expect(bar.compareDocumentPosition(screen.getByTestId('window-bar')) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(screen.getByTestId('engine-caption').textContent).toMatch(/2\.9/)
  })

  it('shows Kiro Crew open by default, and closes it on click', () => {
    render(<ContextBreakdownPanel trace={trace(latest())} chartWidth={600} />)
    const section = screen.getByTestId('engine-breakdown')
    const first = section.querySelector('[data-engine-row]') as HTMLElement
    expect(first.getAttribute('data-engine-row')).toBe('crew')
    expect(section.querySelector('[data-engine-row="crew.rules"]')!.textContent).toMatch(/Kiro Crew rules.*6,000/)
    const toggle = within(first).getByRole('button')
    expect(toggle.getAttribute('aria-expanded')).toBe('true')
    fireEvent.click(toggle)
    expect(section.querySelector('[data-engine-row="crew.rules"]')).toBeNull()
  })

  it('says when Kiro Crew counts more than the messages', () => {
    render(<ContextBreakdownPanel trace={trace(latest({ ours: { ...ours, rules: 7_000 } }))} chartWidth={600} />)
    expect(screen.getByTestId('engine-overlap').textContent).toMatch(/908/)
  })

  it('shows list counts next to the label', () => {
    render(<ContextBreakdownPanel trace={trace(latest())} chartWidth={600} />)
    expect(document.querySelector('[data-engine-row="skillsIndex"]')!.textContent).toMatch(/Skill list \(engine\)\(28\)/)
  })

  it('names a segment on hover in its own line, above the bar', () => {
    render(<ContextBreakdownPanel trace={trace(latest())} chartWidth={600} />)
    const tip = screen.getByTestId('engine-bar-tip')
    fireEvent.pointerEnter(screen.getByTestId('engine-bar').querySelector('[data-engine-seg="steering"]')!)
    expect(tip.textContent).toMatch(/Steering/)
    expect(tip.textContent).toMatch(/18,077/)
  })

  it('shows images and files on their own line', () => {
    render(
      <ContextBreakdownPanel
        trace={trace(latest({ engine: engine({ media: { images: 1, image_bytes: 34_816, documents: 0, document_bytes: 0 } }) }))}
        chartWidth={600}
      />,
    )
    expect(screen.getByTestId('engine-media').textContent).toMatch(/1 image, 34\.8\s?kB/)
  })

  it('shows a turn that was not split in tokens, with characters after, adding up', () => {
    render(<ContextBreakdownPanel trace={trace(latest({ engine: null, context_used: 85_311 }))} chartWidth={600} />)
    expect(screen.getByTestId('whole-total').textContent).toBe('85,311')
    expect(screen.getByTestId('window-usage').textContent).toBe('9% of the 1M-token window')
    const section = screen.getByTestId('engine-breakdown')
    const crew = section.querySelector('[data-engine-row="crew"]')!.textContent
    expect(crew).toMatch(/~3,000 tokens/)
    expect(crew).toMatch(/12,000 characters/)
    expect(section.querySelector('[data-engine-row="rest"]')!.textContent).toMatch(/Rest of the contextHow to split it~82,286 tokens96%/)
    // The bar is drawn against the window: purples, then the grey rest.
    const segs = Array.from(within(section).getByTestId('engine-bar').querySelectorAll('[data-engine-seg]'))
    expect(segs.map(s => s.getAttribute('data-engine-seg')).slice(-1)).toEqual(['rest'])
    expect(screen.getByTestId('engine-caption').textContent).toBe('Figures marked ~ are estimates (characters / 4).')
  })

  it('explains the rest of the context in a tip, on a turn that was not split only', () => {
    render(
      <ContextBreakdownPanel trace={trace(latest({ engine: null, context_used: 85_311 }))} harness="kiro" chartWidth={600} />,
    )
    const rest = document.querySelector('[data-engine-row="rest"]') as HTMLElement
    expect(rest.textContent).toMatch(/^Rest of the context/)
    // The glyph is a real button, so the keyboard reaches it with Tab.
    const glyph = within(rest).getByRole('button', { name: /more information/i })
    expect(glyph.tabIndex).toBe(0)
    glyph.focus()
    expect(document.activeElement).toBe(glyph)
    fireEvent.focus(glyph)
    const tip = screen.getByRole('tooltip')
    expect(tip.textContent).toBe(
      "Kiro CLI doesn't report what's in this part. To see it, open Settings > Agent Harness and pick Kiro Agent. New sessions then use Kiro Agent; this session keeps its current agent.",
    )
    expect(tip.textContent).not.toMatch(/ACP|agent loop/i)
    cleanup()
    render(<ContextBreakdownPanel trace={trace(latest())} chartWidth={600} />)
    expect(document.querySelector('[data-engine-tip]')).toBeNull()
    expect(screen.queryByRole('button', { name: /more information/i })).toBeNull()
  })

  it('names the session\'s own agent in the tip, or says "this session\'s agent" when unknown', () => {
    render(
      <ContextBreakdownPanel trace={trace(latest({ engine: null, context_used: 85_311 }))} harness="claude" chartWidth={600} />,
    )
    fireEvent.focus(screen.getByRole('button', { name: /more information/i }))
    expect(screen.getByRole('tooltip').textContent).toMatch(/^Claude Code doesn't report/)
    cleanup()
    render(<ContextBreakdownPanel trace={trace(latest({ engine: null, context_used: 85_311 }))} chartWidth={600} />)
    fireEvent.focus(screen.getByRole('button', { name: /more information/i }))
    expect(screen.getByRole('tooltip').textContent).toMatch(/^This session's agent doesn't report/)
  })

  it('on Kiro Agent, says the turn is not measured yet instead of asking to switch', () => {
    // A KAS turn still running: its breakdown has not arrived, so the turn reads unsplit.
    render(
      <ContextBreakdownPanel trace={trace(latest({ engine: null, context_used: 85_311 }))} harness="kas" chartWidth={600} />,
    )
    fireEvent.focus(screen.getByRole('button', { name: /more information/i }))
    expect(screen.getByRole('tooltip').textContent).toBe("Estimated. The engine hasn't measured this turn yet.")
    expect(screen.getByRole('tooltip').textContent).not.toMatch(/Settings|Kiro Agent/)
  })

  it('names which tip it is before any hover: waiting on a measurement, or how to get one', () => {
    const unsplit = trace(latest({ engine: null, context_used: 85_311 }))
    const tag = () => document.querySelector('[data-engine-row="rest"] [data-engine-tip-tag]')?.textContent
    render(<ContextBreakdownPanel trace={unsplit} harness="kas" chartWidth={600} />)
    expect(tag()).toBe('Not measured yet')
    cleanup()
    render(<ContextBreakdownPanel trace={unsplit} harness="kiro" chartWidth={600} />)
    expect(tag()).toBe('How to split it')
    cleanup()
    // A harness that could not be read: no tip and no label.
    render(<ContextBreakdownPanel trace={unsplit} harness={null} chartWidth={600} />)
    expect(tag()).toBeUndefined()
  })

  it('names the engine rows apart from the Kiro Crew rows', () => {
    const b = breakdownOf(latest())
    expect(row(b.rows, 'kiroInstructions').label).toBe('Engine system prompt')
    expect(row(b.rows, 'skillsIndex').label).toBe('Skill list (engine)')
    expect(row(b.rows, 'history').children!.find(r => r.id === 'history.user_other')!.label).toBe('Other text sent as you')
  })

  it('in a narrow panel, puts the characters under the tokens so the label keeps its room', () => {
    // The 320px side panel: the stacking is a container query on the row list, so it
    // follows the panel's width rather than the window's.
    render(<ContextBreakdownPanel trace={trace(latest({ engine: null, context_used: 85_311 }))} chartWidth={288} />)
    const rows = screen.getByTestId('engine-rows')
    expect(rows.className).toMatch(/@container\/ctxrows/)
    const crew = rows.querySelector('[data-engine-row="crew"]') as HTMLElement
    const value = crew.querySelector('[data-engine-value]') as HTMLElement
    // Stacked by default (narrow), side by side only from 26rem of panel width.
    expect(value.className).toMatch(/(^| )flex-col( |$)/)
    expect(value.className).toMatch(/@\[26rem\]\/ctxrows:flex-row/)
    expect(value.querySelector('[data-engine-chars]')!.textContent).toBe('12,000 characters')
    // The label may shrink, but never below a readable width.
    const label = crew.querySelector('button') as HTMLElement
    expect(label.className).toMatch(/min-w-\[5rem\]/)
    // And it wraps there instead of being cut; it is one truncated line only when wide.
    const text = crew.querySelector('[data-engine-label]') as HTMLElement
    expect(text.className).not.toMatch(/(^| )truncate( |$)/)
    expect(text.className).toMatch(/@\[26rem\]\/ctxrows:truncate/)
  })

  it('says the total is unknown when the turn reported none', () => {
    render(<ContextBreakdownPanel trace={trace(latest({ engine: null, context_used: 0 }))} chartWidth={600} />)
    expect(screen.getByTestId('whole-total').textContent).toBe('Total not reported')
    expect(screen.queryByTestId('window-bar')).toBeNull()
  })

  it('never says "agent loop", "History" or "Kiro Crew prompts"', () => {
    for (const l of [latest(), latest({ engine: null, context_used: 85_311 })]) {
      const { container, unmount } = render(
        <ContextBreakdownPanel
          trace={{ ...trace(l), trend: [{ turn: 3, ours, loop: 1_000 }], start: { turn: 1, ours, loop: 500 } }}
          chartWidth={600}
        />,
      )
      expect(container.textContent).not.toMatch(/agent loop/i)
      expect(container.textContent).not.toMatch(/History/)
      expect(container.textContent).not.toMatch(/Kiro Crew prompts/)
      unmount()
    }
  })

  it('reports a count that does not add up', () => {
    render(<ContextBreakdownPanel trace={trace(latest({ engine: engine({ mismatch_chars: 3_000, user_other_chars: 0 }) }))} chartWidth={600} />)
    expect(screen.getByTestId('engine-mismatch').textContent).toMatch(/3,000/)
  })

  it('marks numbers sent under a format this version does not know', () => {
    render(<ContextBreakdownPanel trace={trace(latest({ engine: engine({ schema_known: false }) }))} chartWidth={600} />)
    expect(screen.getByTestId('engine-schema-unknown')).toBeTruthy()
  })
})
