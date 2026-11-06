/**
 * Goals on doc headings: a heading's section runs to the next heading of the same
 * or a higher level, its goal is a hidden marker on the next line, and the heading
 * carries the control that sets, changes and clears it.
 */
import { describe, it, expect, afterEach, vi } from 'vitest'
import { act, fireEvent } from '@testing-library/react'
import { EditorState } from '@codemirror/state'
import { EditorView } from '@codemirror/view'
import { markdown, markdownLanguage } from '@codemirror/lang-markdown'
import { docHasGoals, goalExtension, goalMarker, headingSections, isQuietReport, parseGoalMarker, setGoalChecks, setGoalState, type GoalHost } from '../pages/chat/page/docGoals'
import { deriveThreads } from '../pages/chat/page/docThreads'
import type { ChatMessage } from '../types'
import { conductorExtension, setDocData, type ConductorHost } from '../pages/chat/page/docConductor'

let view: EditorView | null = null
afterEach(() => { act(() => { view?.destroy() }); view = null; document.body.replaceChildren(); vi.useRealTimers() })

const make = (doc: string) => {
  act(() => { view = new EditorView({ parent: document.body, state: EditorState.create({ doc, extensions: [markdown({ base: markdownLanguage }), goalExtension()] }) }) })
  return view!
}
const DOC = [
  '# Plan',
  'intro',
  '## Tickets',
  goalMarker({ id: 'gtick1', text: 'Keep statuses current' }),
  '- one',
  '### Blocked',
  '- two',
  '',
  '## Notes',
  '```',
  '## not a heading',
  '```',
  'last',
].join('\n')

describe('doc heading goals', () => {
  it('a section runs to the next heading of its level or higher, holding its subsections', () => {
    const secs = headingSections(EditorState.create({ doc: DOC }))
    expect(secs.map(s => [s.title, s.level, s.line, s.endLine])).toEqual([
      ['Plan', 1, 1, 13],
      ['Tickets', 2, 3, 7],
      ['Blocked', 3, 6, 7],
      ['Notes', 2, 9, 13],
    ])
    expect(secs[1].goal).toEqual({ id: 'gtick1', text: 'Keep statuses current' })
    expect(secs[1].markerLine).toBe(4)
  })

  it('a marker keeps to one line, cannot end its comment early, and an older one with its own rate still reads', () => {
    expect(goalMarker({ id: 'gabc1', text: 'check\nthese --> now' })).toBe('<!-- goal gabc1: check these – now -->')
    expect(goalMarker({ text: 'x' })).toMatch(/^<!-- goal g[a-z0-9]+: x -->$/)
    expect(parseGoalMarker('<!-- goal gabc1: x -->')).toEqual({ id: 'gabc1', text: 'x' })
    // Older markers still read: no id, or a rate of their own.
    expect(parseGoalMarker('<!-- goal every=7: x -->')).toEqual({ text: 'x' })
    expect(parseGoalMarker('<!-- goal: x -->')).toEqual({ text: 'x' })
    expect(parseGoalMarker('<!-- goal:  -->')).toBeNull()
    expect(docHasGoals('## A\n<!-- goal gabc1: keep it -->\n')).toBe(true)
    expect(docHasGoals('## A\n<!-- goal: keep it -->\n')).toBe(true)
    expect(docHasGoals('## A\n')).toBe(false)
  })

  it('the marker line is hidden and the heading shows Watching; other headings offer Set goal', () => {
    const v = make(DOC)
    const lines = [...v.contentDOM.querySelectorAll('.cm-line')].map(l => l.textContent ?? '')
    expect(lines.some(l => l.includes('goal:'))).toBe(false)
    const chip = v.dom.querySelector('[data-testid="doc-goal-chip"]')!
    expect(chip.textContent).toContain('Watching')
    expect(v.dom.querySelectorAll('[data-testid="doc-goal-add"]')).toHaveLength(3)
  })

  it('a goal heading carries its own pin: its report, working during a check, and a reply on its section', async () => {
    const host = { current: { send: vi.fn(), openSession: vi.fn(), markRead: vi.fn() } as ConductorHost }
    const doc = '## Tickets\n<!-- goal gabc1: Keep it current -->\n- one\n## Notes\nx'
    act(() => {
      view = new EditorView({ parent: document.body, state: EditorState.create({ doc, extensions: [markdown({ base: markdownLanguage }), conductorExtension(host), goalExtension()] }) })
    })
    const v = view!
    const pins = () => [...v.dom.querySelectorAll<HTMLElement>('[data-testid="doc-answer-pin"]')]
    // Before any check: a quiet pin on the goal heading only.
    act(() => { v.dispatch({ effects: setDocData.of({ threads: {}, workers: {} }) }) })
    expect(pins()).toHaveLength(1)
    expect(pins()[0].getAttribute('aria-label')).toContain('Not checked yet')
    act(() => { v.dispatch({ effects: setDocData.of({ threads: {}, workers: {}, goalsPending: true }) }) })
    expect(pins()[0].getAttribute('aria-label')).toContain('Thinking')
    act(() => {
      v.dispatch({ effects: setDocData.of({ threads: { 'goal-gabc1': { messages: [{ who: 'conductor', text: 'Two moved.' }], pending: false, unread: true } }, workers: {} }) })
    })
    expect(pins()[0].getAttribute('aria-label')).toContain('Two moved.')
    // Opened, a reply goes on the goal's thread with its section as the quote.
    await act(async () => { fireEvent.click(pins()[0]) })
    expect(host.current.markRead).toHaveBeenCalledWith('goal-gabc1')
    const box = v.dom.querySelector('[data-testid="doc-answer-input"]') as HTMLTextAreaElement
    await act(async () => { fireEvent.change(box, { target: { value: 'which two?' } }) })
    await act(async () => { fireEvent.keyDown(box, { key: 'Enter' }) })
    expect(host.current.send).toHaveBeenCalledWith('goal-gabc1', 'which two?', '## Tickets\n<!-- goal gabc1: Keep it current -->\n- one', doc, true)
  })

  it('the popover shows the page\'s one rate; a rate press goes to the page', () => {
    const host = { current: { setEvery: vi.fn() } as GoalHost }
    act(() => { view = new EditorView({ parent: document.body, state: EditorState.create({ doc: DOC, extensions: [markdown({ base: markdownLanguage }), goalExtension(host)] }) }) })
    const v = view!
    act(() => { v.dispatch({ effects: setGoalState.of({ every: 30 }) }) })
    act(() => { fireEvent.click(v.dom.querySelector('[data-testid="doc-goal-chip"]')!) })
    const checked = v.dom.querySelector('[data-gc="every-opt"][aria-checked="true"]')!
    expect(checked.textContent).toBe('30 min')
    act(() => { fireEvent.click(v.dom.querySelector('[data-gc="every-opt"]')!) })
    expect(host.current.setEvery).toHaveBeenCalledWith(5)
  })

  it('Set goal writes the marker under its heading; Update rewrites it; Stop watching removes it', () => {
    const v = make('## Tickets\n- one')
    act(() => { fireEvent.click(v.dom.querySelector('[data-testid="doc-goal-add"]')!) })
    act(() => { fireEvent.change(v.dom.querySelector('[data-testid="doc-goal-text"]')!, { target: { value: 'Mark the done ones' } }) })
    act(() => { fireEvent.click(v.dom.querySelector('[data-testid="doc-goal-save"]')!) })
    const made = /^## Tickets\n<!-- goal (g[a-z0-9]+): Mark the done ones -->\n- one$/.exec(v.state.doc.toString())
    expect(made).not.toBeNull()

    // Updating keeps the goal's id, so its pin's conversation stays the same one.
    act(() => { fireEvent.click(v.dom.querySelector('[data-testid="doc-goal-chip"]')!) })
    act(() => { fireEvent.change(v.dom.querySelector('[data-testid="doc-goal-text"]')!, { target: { value: 'Keep it short' } }) })
    act(() => { fireEvent.click(v.dom.querySelector('[data-testid="doc-goal-save"]')!) })
    expect(v.state.doc.toString()).toBe(`## Tickets\n<!-- goal ${made![1]}: Keep it short -->\n- one`)

    act(() => { fireEvent.click(v.dom.querySelector('[data-testid="doc-goal-chip"]')!) })
    act(() => { fireEvent.click(v.dom.querySelector('[data-testid="doc-goal-clear"]')!) })
    expect(v.state.doc.toString()).toBe('## Tickets\n- one')
  })
})

describe('moving and copying a goal section', () => {
  const SECTION = '# Tasks\n<!-- goal gtask1: keep it current -->\none\n'
  const ids = (v: EditorView) => [...v.state.doc.toString().matchAll(/<!-- goal (g[a-z0-9]+):/g)].map(m => m[1])

  it('a copy takes a new goal id, so the two sections do not share one pin', () => {
    const v = make(SECTION)
    act(() => { v.dispatch({ changes: { from: v.state.doc.length, insert: SECTION }, userEvent: 'input.paste' }) })
    const [first, second] = ids(v)
    expect(first).toBe('gtask1')
    expect(second).not.toBe('gtask1')
    expect(headingSections(v.state).map(s => s.goal?.text)).toEqual(['keep it current', 'keep it current'])
  })

  it('a cut and paste keeps the id, so the section stays the same goal', () => {
    const v = make(`${SECTION}\n# Other\ntext\n`)
    const end = v.state.doc.toString().indexOf('# Other')
    act(() => { v.dispatch({ changes: [{ from: 0, to: end }, { from: v.state.doc.length, insert: SECTION }], userEvent: 'move.drop' }) })
    expect(ids(v)).toEqual(['gtask1'])
    expect(v.state.doc.toString().startsWith('# Other')).toBe(true)
  })
})

describe('the heartbeat on a watched chip', () => {
  const wake = (ts: string) => ({ role: 'nudge', content: '[auto-nudge cycle 1]\n[Goals] Checking the goal sections', cls: '', ts }) as ChatMessage
  const reply = (content: string, ts: string) => ({ role: 'assistant', content, cls: '', ts }) as ChatMessage

  it('reads a quiet check and one that did something apart, per goal, oldest first', () => {
    const threads = deriveThreads([
      wake('2026-11-03T10:00:00Z'), reply('[goal gtick1]\nNothing new.', '2026-11-03T10:00:30Z'),
      wake('2026-11-03T10:10:00Z'), reply('[goal gtick1]\nMarked #4 done.\n[goal gother]\nNothing new.', '2026-11-03T10:10:40Z'),
    ])
    expect(threads['goal-gtick1'].checks?.map(c => c.acted)).toEqual([false, true])
    expect(threads['goal-gother'].checks?.map(c => c.acted)).toEqual([false])
    expect(threads['goal-gtick1'].checks?.[1].ts).toBe(Date.parse('2026-11-03T10:10:40Z'))
    expect(isQuietReport('Nothing new, all 10 tickets are still To do.')).toBe(true)
    expect(isQuietReport('There is nothing new here')).toBe(false)
  })

  it('draws one bar per check, tall for the ones that did something, and how long ago the last was', () => {
    const now = Date.parse('2026-11-03T12:00:00Z')
    vi.setSystemTime(now)
    const v = make(DOC)
    act(() => { v.dispatch({ effects: setGoalChecks.of({ gtick1: [{ ts: now - 2 * 60_000, acted: false }, { ts: now - 60_000, acted: true }, { ts: now - 120_000, acted: false }] }) }) })
    const bars = [...v.dom.querySelectorAll('[data-testid="doc-goal-beat"] > i')] as HTMLElement[]
    expect(bars.map(b => b.dataset.acted)).toEqual(['false', 'true', 'false'])
    expect(bars.map(b => b.style.height)).toEqual(['4px', '12px', '4px'])
    expect(v.dom.querySelector('[data-testid="doc-goal-ago"]')?.textContent).toMatch(/2/)
    expect(v.dom.querySelector('[data-testid="doc-goal-chip"]')?.getAttribute('aria-label')).toContain('1 of the last 3 checks found something')
  })

  it('shows no bars before the first check', () => {
    const v = make(DOC)
    expect(v.dom.querySelector('[data-testid="doc-goal-beat"]')).toBeNull()
    expect(v.dom.querySelector('[data-testid="doc-goal-ago"]')).toBeNull()
  })
})
