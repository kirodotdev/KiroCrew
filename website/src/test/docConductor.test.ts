// @vitest-environment jsdom
/**
 * The doc conductor layer: markers, the actions a reply carries, the turn a
 * highlight sends, the transcript read back into threads, and what the editor
 * draws for asks and workers.
 */
import { describe, it, expect, afterEach, vi } from 'vitest'
import { act, fireEvent } from '@testing-library/react'
import { EditorState } from '@codemirror/state'
import { EditorView } from '@codemirror/view'
import {
  changeForWrite, conductorExtension, parseMarkers, setDocData, splitReply,
  type ConductorHost, type DocData,
} from '../pages/chat/page/docConductor'
import { buildAskTurn, deriveThreads, docTextForCopy } from '../pages/chat/page/docThreads'
import { tableExtension } from '../pages/chat/page/docTables'
import { rowQuoteLabel } from '../pages/chat/page/docAskRows'
import type { ChatMessage } from '../types'

describe('markers', () => {
  it('pairs inline and own-line markers', () => {
    const text = 'a <!-- ask:x1 -->b<!-- /ask:x1 --> c\n<!-- work:s1 -->\n## H\n<!-- /work:s1 -->'
    const [ask, work] = parseMarkers(text)
    expect(ask).toMatchObject({ kind: 'ask', id: 'x1', ownLine: false })
    expect(work).toMatchObject({ kind: 'work', id: 's1', ownLine: true })
  })
})

describe('writes', () => {
  it('splits a reply into prose and the writes it asks for, and hides a half-streamed block', () => {
    const r = splitReply('Here they are.\n```doc-append\n| # | Title |\n| --- | --- |\n| 1 | A |\n```\nand\n```doc-insert after="## Plan"\n- step\n```')
    expect(r.prose).toBe('Here they are.\n\nand')
    expect(r.actions?.writes).toEqual([
      { mode: 'append', text: '| # | Title |\n| --- | --- |\n| 1 | A |' },
      { mode: 'after', after: '## Plan', text: '- step' },
    ])
    expect(splitReply('Writing…\n```doc-append\n| # |').prose).toBe('Writing…')
    expect(splitReply('Writing…\n```doc-append\n| # |').actions).toBeNull()
  })

  const find = (text: string, a: string) => { const i = text.indexOf(a); return i === -1 ? null : i }
  const apply = (text: string, c: { from: number; insert: string } | null) => (c ? text.slice(0, c.from) + c.insert + text.slice(c.from) : text)

  it('appends at the end and inserts after a line, only ever adding', () => {
    expect(apply('# Doc\nintro', changeForWrite('# Doc\nintro', { mode: 'append', text: 'new' }, find))).toBe('# Doc\nintro\n\nnew\n')
    expect(apply('# Doc\n## Plan\nlater', changeForWrite('# Doc\n## Plan\nlater', { mode: 'after', after: '## Plan', text: '- step' }, find)))
      .toBe('# Doc\n## Plan\n\n- step\nlater')
  })

  it('never writes the same text twice, and skips a line that is gone', () => {
    const once = apply('# Doc', changeForWrite('# Doc', { mode: 'append', text: 'issues' }, find))
    expect(changeForWrite(once, { mode: 'append', text: 'issues' }, find)).toBeNull()
    expect(changeForWrite('# Doc', { mode: 'after', after: '## Gone', text: 'x' }, find)).toBeNull()
  })

  it('a turn carries only what changed: the brief is the session\'s, not the turn\'s', () => {
    const first = buildAskTurn({ id: 'q1', request: 'r', quote: 'q', doc: 'd', title: 'Plan', copy: 'd' })
    expect(first).toBe('[Doc ask q1]\nr\n\n---\nHighlighted:\n"""\nq\n"""\n\n[Doc title] "Plan"\n\n<<<DOC\nd\nDOC>>>')
    const next = buildAskTurn({ id: 'q2', request: 'r', quote: 'q', doc: 'd', title: null, copy: null })
    expect(next).toBe('[Doc ask q2]\nr\n\n---\nHighlighted:\n"""\nq\n"""')
  })

  it('an empty-spot ask quotes the line it sits under; a new highlight alone is no change', () => {
    const doc = '# Plan\n\nfirst line\n<!-- ask:q3 --><!-- /ask:q3 -->\n'
    expect(buildAskTurn({ id: 'q3', request: 'r', quote: '', doc, title: null, copy: null })).toContain('At the spot after: "first line"')
    expect(docTextForCopy(doc)).toBe('# Plan\n\nfirst line\n\n')
  })
})

describe('threads', () => {
  const msg = (role: string, content: string, mid?: string): ChatMessage => ({ role, content, cls: '', meta: mid ? { mid } : undefined })

  it('reads each highlight\'s thread from the transcript', () => {
    const ask = buildAskTurn({ id: 'q1', request: 'expand this', quote: 'Plan', doc: 'Plan', title: 'Plan', copy: 'Plan' })
    expect(ask.startsWith('[Doc ask q1]\nexpand this\n\n---\n')).toBe(true)
    expect(ask).toContain('<<<DOC\nPlan\nDOC>>>')
    const threads = deriveThreads([
      msg('user', 'an ordinary chat line'),
      msg('assistant', 'not about a highlight'),
      msg('user', ask),
      msg('tool', '🔧 session_create'),
      msg('assistant', 'Wrote them down.\n```doc-append\n- issue 1\n```', 'm9'),
    ])
    expect(Object.keys(threads)).toEqual(['q1'])
    expect(threads.q1.messages).toEqual([{ who: 'you', text: 'expand this' }, { who: 'conductor', text: 'Wrote them down.' }])
    expect(threads.q1.actions).toEqual([{ key: 'm9', actions: { writes: [{ mode: 'append', text: '- issue 1' }] } }])
    expect(threads.q1.awaiting).toBe(false)
  })
})

describe('the editor layer', () => {
  let view: EditorView | null = null
  afterEach(() => {
    act(() => { view?.destroy() })
    view = null
    document.body.replaceChildren()
  })

  function make(doc: string, host: Partial<ConductorHost> = {}) {
    const ref = { current: { send: vi.fn(), openSession: vi.fn(), markRead: vi.fn(), ...host } as ConductorHost }
    act(() => {
      view = new EditorView({ parent: document.body, state: EditorState.create({ doc, extensions: [conductorExtension(ref)] }) })
    })
    return { v: view!, host: ref.current }
  }
  const push = (v: EditorView, data: DocData) => act(() => { v.dispatch({ effects: setDocData.of(data) }) })
  const q = (v: EditorView, testId: string) => v.dom.querySelector(`[data-testid="${testId}"]`) as HTMLElement | null

  it('a table row asks through its handle: the row is sent, the markers wrap the table', () => {
    const doc = 'Issues\n\n| # | Title |\n| --- | --- |\n| 1 | Login loop |\n| 2 | Slow boot |\n| 3 | Bad icon |\n\nafter\n'
    const ref = { current: { send: vi.fn(), openSession: vi.fn(), markRead: vi.fn() } as ConductorHost }
    act(() => {
      view = new EditorView({ parent: document.body, state: EditorState.create({ doc, extensions: [tableExtension(), conductorExtension(ref)] }) })
    })
    const v = view!
    const row = (r: number) => v.dom.querySelector(`.cm-table-widget tr[data-r="${r}"]`) as HTMLElement
    const handle = () => q(v, 'table-row-ask') as HTMLButtonElement
    // The handle shows beside the row under the pointer, never the header's.
    fireEvent.mouseEnter(row(0))
    expect(handle().hidden).toBe(true)
    fireEvent.mouseEnter(row(3))
    expect(handle().hidden).toBe(false)
    act(() => { fireEvent.click(handle()) })
    expect(row(3).classList.contains('cm-row-picked')).toBe(true)
    // Shift-click extends to a range.
    fireEvent.mouseEnter(row(4))
    act(() => { fireEvent.click(handle(), { shiftKey: true }) })
    expect([2, 3, 4].map(r => row(r).classList.contains('cm-row-picked'))).toEqual([false, true, true])

    const box = q(v, 'ask-input') as HTMLTextAreaElement
    box.value = 'what are these?'
    act(() => { fireEvent.keyDown(box, { key: 'Enter' }) })
    const text = v.state.doc.toString()
    const [m] = parseMarkers(text)
    // The markers sit on their own lines around the whole table, which stays a table.
    expect(m.ownLine).toBe(true)
    expect(text.slice(m.open.to, m.close.from)).toContain('| 1 | Login loop |')
    expect(v.dom.querySelectorAll('.cm-table-widget')).toHaveLength(1)
    // The conductor is sent the header and the picked rows only.
    const sent = (ref.current.send as ReturnType<typeof vi.fn>).mock.calls[0]
    const lines = (sent[2] as string).split('\n').map(l => l.replace(/\s+/g, ' '))
    expect(lines).toEqual(['| # | Title |', '| --- | --------- |', '| 2 | Slow boot |', '| 3 | Bad icon |'])
    expect(sent[2]).not.toContain('Login loop')
    expect(rowQuoteLabel(sent[2])).toBe('2 · Slow boot (+1)')
    // Sent: nothing stays picked.
    expect(v.dom.querySelectorAll('.cm-row-picked')).toHaveLength(0)
  })

  it('the row handle stays while the pointer crosses the margin to it, and goes once it leaves both', () => {
    vi.useFakeTimers()
    try {
      const ref = { current: { send: vi.fn(), openSession: vi.fn(), markRead: vi.fn() } as ConductorHost }
      act(() => {
        view = new EditorView({ parent: document.body, state: EditorState.create({ doc: '| a | b |\n| - | - |\n| 1 | 2 |\n', extensions: [tableExtension(), conductorExtension(ref)] }) })
      })
      const v = view!
      const wrapper = v.dom.querySelector('.cm-table-widget') as HTMLElement
      const handle = q(v, 'table-row-ask') as HTMLButtonElement
      fireEvent.mouseEnter(v.dom.querySelector('.cm-table-widget tr[data-r="2"]')!)
      // Off the row and across the gap...
      fireEvent.mouseLeave(wrapper)
      act(() => { vi.advanceTimersByTime(150) })
      // ...onto the handle: it stays, however long the pointer rests there.
      fireEvent.mouseEnter(handle)
      act(() => { vi.advanceTimersByTime(2000) })
      expect(handle.hidden).toBe(false)
      // Away from both: it goes.
      fireEvent.mouseLeave(wrapper)
      act(() => { vi.advanceTimersByTime(1000) })
      expect(handle.hidden).toBe(true)
    } finally {
      vi.useRealTimers()
    }
  })

  it('labels a row ask by its row, and only a table quote is a row ask', () => {
    expect(rowQuoteLabel('| # | Title |\n| --- | --- |\n| 7 | Fix it |')).toBe('7 · Fix it')
    expect(rowQuoteLabel('just some text')).toBeNull()
  })

  it('a selection asks the conductor: markers go in and the turn is sent', () => {
    const { v, host } = make('Write the intro here.')
    act(() => { v.dispatch({ selection: { anchor: 10, head: 15 } }) })
    fireEvent.click(q(v, 'ask-conductor')!)
    const box = q(v, 'ask-input') as HTMLTextAreaElement
    box.value = 'make it punchy'
    act(() => { fireEvent.keyDown(box, { key: 'Enter' }) })
    const text = v.state.doc.toString()
    const [m] = parseMarkers(text)
    expect(text.slice(m.open.to, m.close.from)).toBe('intro')
    expect(host.send).toHaveBeenCalledWith(m.id, 'make it punchy', 'intro', text, false)
    // Sent, the text is no longer selected: the caret sits just past the highlight,
    // so what you type next is plain text, not part of it.
    expect(v.state.selection.main.empty).toBe(true)
    expect(v.state.selection.main.head).toBe(m.close.to)
    act(() => { v.dispatch({ changes: { from: v.state.selection.main.head, insert: '!' } }) })
    const [after] = parseMarkers(v.state.doc.toString())
    expect(v.state.doc.toString().slice(after.open.to, after.close.from)).toBe('intro')
    // And the highlight is only underlined, not tinted like a selection.
    expect(v.dom.querySelector('.cm-ask')!.className).not.toContain('cm-ask-active')
  })

  it('blank lines around a selection stay out of the highlight; blank space alone has no button', () => {
    const { v, host } = make('one\n\n\nHi there\n\n\ntwo')
    act(() => { v.dispatch({ selection: { anchor: 4, head: 16 } }) })
    fireEvent.click(q(v, 'ask-conductor')!)
    const box = q(v, 'ask-input') as HTMLTextAreaElement
    box.value = 'hi'
    act(() => { fireEvent.keyDown(box, { key: 'Enter' }) })
    const text = v.state.doc.toString()
    const [m] = parseMarkers(text)
    expect(text.slice(m.open.to, m.close.from)).toBe('Hi there')
    expect(host.send).toHaveBeenCalledWith(m.id, 'hi', 'Hi there', text, false)
    expect(v.dom.querySelector('.cm-ask')!.textContent).toBe('Hi there')
    act(() => { v.dispatch({ selection: { anchor: 4, head: 6 } }) })
    expect(q(v, 'ask-conductor')).toBeNull()
  })

  it('the answer sits under the highlight, collapsed to a margin pin, outside the text', () => {
    const { v } = make('see <!-- ask:q1 -->this<!-- /ask:q1 --> part\nnext')
    push(v, { threads: { q1: { messages: [{ who: 'you', text: 'why?' }], pending: true, unread: false } }, workers: {} })
    const pin = q(v, 'doc-answer-pin')!
    expect(pin.getAttribute('aria-label')).toContain('Thinking…')
    expect(pin.getAttribute('aria-expanded')).toBe('false')
    // Collapsed, there is no bar taking up a line: only the pin.
    expect(q(v, 'doc-answer-bar')).toBeNull()
    // The line reads exactly as written; the answer is a block between lines.
    const lines = [...v.contentDOM.querySelectorAll('.cm-line')].map(l => l.textContent)
    expect(lines).toEqual(['see this part', 'next'])
  })

  it('a click opens the answer: the reply rendered as markdown, the workers, a reply box', async () => {
    const { v, host } = make('see <!-- ask:q1 -->this<!-- /ask:q1 --> part')
    push(v, {
      threads: { q1: {
        messages: [{ who: 'you', text: 'list it' }, { who: 'conductor', text: 'Here:\n\n| a | b |\n| --- | --- |\n| 1 | 2 |' }],
        pending: false, unread: true,
        workers: [{ key: 'w-1', title: 'Draft the plan', state: 'working' }],
      } },
      workers: {},
    })
    const pin = q(v, 'doc-answer-pin')!
    expect(pin.getAttribute('aria-label')).toContain('Here:')
    expect(pin.getAttribute('aria-label')).toContain('1/1 working')
    await act(async () => { fireEvent.mouseEnter(pin) })
    expect(q(v, 'doc-answer-preview')!.textContent).toContain('1 reply')
    await act(async () => { fireEvent.click(pin) })
    expect(host.markRead).toHaveBeenCalledWith('q1')
    expect(q(v, 'doc-answer-bar')!.textContent).toContain('1/1 working')
    expect(q(v, 'doc-answer-preview')).toBeNull()
    expect(v.dom.querySelector('[data-da="md"] table')).not.toBeNull()
    fireEvent.click(q(v, 'doc-answer-worker')!)
    expect(host.openSession).toHaveBeenCalledWith('w-1')
    const box = q(v, 'doc-answer-input') as HTMLTextAreaElement
    await act(async () => { fireEvent.change(box, { target: { value: 'and then?' } }) })
    await act(async () => { fireEvent.keyDown(box, { key: 'Enter' }) })
    expect(host.send).toHaveBeenCalledWith('q1', 'and then?', 'this', v.state.doc.toString(), true)
  })

  it('an open answer keeps its reply draft when a new message arrives', async () => {
    const { v } = make('see <!-- ask:q1 -->this<!-- /ask:q1 --> part')
    const thread = (n: number) => ({ q1: { messages: Array.from({ length: n }, (_, i) => ({ who: 'conductor' as const, text: `m${i}` })), pending: false, unread: false } })
    push(v, { threads: thread(1), workers: {} })
    await act(async () => { fireEvent.click(q(v, 'doc-answer-pin')!) })
    await act(async () => { fireEvent.change(q(v, 'doc-answer-input')!, { target: { value: 'half written' } }) })
    push(v, { threads: thread(2), workers: {} })
    expect((q(v, 'doc-answer-input') as HTMLTextAreaElement).value).toBe('half written')
    expect(v.dom.querySelector('[data-da="body"]')!.textContent).toContain('m1')
  })

  it('pins on one line sit side by side; the bar collapses an open answer back to its pin', async () => {
    const { v } = make('a <!-- ask:q1 -->one<!-- /ask:q1 --> and <!-- ask:q2 -->two<!-- /ask:q2 -->\nnext <!-- ask:q3 -->three<!-- /ask:q3 -->')
    const t = { messages: [{ who: 'conductor' as const, text: 'ok' }], pending: false, unread: false }
    push(v, { threads: { q1: t, q2: t, q3: t }, workers: {} })
    const stacks = [...v.dom.querySelectorAll<HTMLElement>('[data-testid="doc-answer-pin"]')].map(p => p.dataset.stack)
    expect(stacks).toEqual(['0', '1', '0'])
    const pin = q(v, 'doc-answer-pin')!
    await act(async () => { fireEvent.click(pin) })
    expect(pin.getAttribute('aria-expanded')).toBe('true')
    await act(async () => { fireEvent.click(q(v, 'doc-answer-bar')!) })
    expect(q(v, 'doc-answer-bar')).toBeNull()
    expect(pin.getAttribute('aria-expanded')).toBe('false')
  })

  it('typing a new line after a highlight is plain text: the answer is not in the way', () => {
    const { v } = make('see <!-- ask:q1 -->this<!-- /ask:q1 --> part')
    push(v, { threads: { q1: { messages: [{ who: 'conductor', text: 'ok' }], pending: false, unread: true } }, workers: {} })
    act(() => {
      v.dispatch({ selection: { anchor: v.state.doc.length } })
      v.dispatch(v.state.replaceSelection('\nnext line'))
    })
    expect(v.state.doc.toString()).toBe('see <!-- ask:q1 -->this<!-- /ask:q1 --> part\nnext line')
    const lines = [...v.contentDOM.querySelectorAll('.cm-line')].map(l => l.textContent)
    expect(lines).toEqual(['see this part', 'next line'])
  })

  it('a worker section gets a bar along its lines and nothing else', () => {
    const { v } = make('# Doc\n<!-- work:w-1 -->\n## Plan\nstep\n<!-- /work:w-1 -->\nend')
    push(v, { threads: {}, workers: { 'w-1': { title: 'Draft the plan', state: 'working' } } })
    expect(v.dom.querySelectorAll('.cm-work-line.cm-work-working').length).toBe(2)
    expect([...v.contentDOM.querySelectorAll('.cm-line')].map(l => l.textContent)).toEqual(['# Doc', '## Plan', 'step', 'end'])
  })
})
