/**
 * Session links in a doc, through a real CodeMirror view: a `session:` markdown link
 * draws as a chip with the live title, "@" opens a picker that writes one, and the
 * file keeps plain markdown throughout.
 */
import { describe, it, expect, afterEach, vi } from 'vitest'
import { act, fireEvent } from '@testing-library/react'
import { EditorState } from '@codemirror/state'
import { EditorView } from '@codemirror/view'
import { markdown, markdownLanguage } from '@codemirror/lang-markdown'
import { isMentionOpen, matchSessions, mentionExtension, mentionMarkdown, setMentionSessions } from '../pages/chat/page/docMentions'
import { slashExtension } from '../pages/chat/page/docSlash'
import { spaceAskExtension } from '../pages/chat/page/docSpaceAsk'
import { sessionKind, type SessionRef } from '../pages/chat/page/SessionMention'
import type { ConductorHost } from '../pages/chat/page/docConductor'
import { BOARD_ID_PREFIX } from '../pages/chat/page/board/boardModel'

const SESSIONS: SessionRef[] = [
  { key: 'chat-1', title: 'Release planning', kind: 'chat', running: false },
  { key: 'doc-me', title: 'This doc', kind: 'doc', running: false },
  { key: 'board-1', title: 'Roadmap board', kind: 'board', running: true },
  { key: 'doc-2', title: 'Pricing notes', kind: 'doc', running: false },
]

let view: EditorView | null = null
afterEach(() => { act(() => { view?.destroy() }); view = null; document.body.replaceChildren() })

function make(doc: string, at = doc.length) {
  const host = { current: { send: () => {}, openSession: vi.fn(), markRead: () => {} } as unknown as ConductorHost }
  act(() => {
    view = new EditorView({
      parent: document.body,
      state: EditorState.create({
        doc, selection: { anchor: at },
        extensions: [markdown({ base: markdownLanguage }), spaceAskExtension(host), slashExtension(), mentionExtension(host)],
      }),
    })
    view.dispatch({ effects: setMentionSessions.of({ sessions: SESSIONS, self: 'doc-me' }) })
  })
  return { v: view!, open: host.current.openSession as unknown as ReturnType<typeof vi.fn> }
}

function type(v: EditorView, text: string) {
  for (const ch of text) {
    act(() => {
      const { from, to } = v.state.selection.main
      const handled = v.state.facet(EditorView.inputHandler).some(h => h(v, from, to, ch, () => v.state.update({ changes: { from, to, insert: ch } })))
      if (!handled) v.dispatch({ changes: { from, to, insert: ch }, selection: { anchor: from + 1 } })
    })
  }
}
const key = (v: EditorView, k: string) => act(() => { v.contentDOM.dispatchEvent(new KeyboardEvent('keydown', { key: k, bubbles: true, cancelable: true })) })
const chips = (v: EditorView) => [...v.dom.querySelectorAll<HTMLElement>('[data-testid="session-mention"]')]

describe('session links', () => {
  it('draws a session: link as a chip with the live title and kind, and a click opens it', () => {
    const { v, open } = make('See [old title](session:board-1) for the plan.')
    const [chip] = chips(v)
    expect(chip.dataset.kind).toBe('board')
    expect(chip.textContent).toBe('Roadmap board')
    expect(chip.querySelector('[data-mn="running"]')).not.toBeNull()
    fireEvent.click(chip)
    expect(open).toHaveBeenCalledWith('board-1')
    // The text around it reads as written; the link itself is not shown as text.
    expect(v.contentDOM.textContent).toBe('See Roadmap board for the plan.')
  })

  it('a link to a session that is gone keeps its written title, struck through, and opens nothing', () => {
    const { v, open } = make('[Old chat](session:gone-1)')
    const [chip] = chips(v)
    expect(chip.dataset.kind).toBe('missing')
    expect(chip.textContent).toBe('Old chat')
    fireEvent.click(chip)
    expect(open).not.toHaveBeenCalled()
  })

  it('"@" opens the picker without this doc, filters it, and Enter writes the markdown link', () => {
    const { v } = make('Notes ')
    type(v, '@')
    expect(isMentionOpen(v.state)).toBe(true)
    const keys = () => [...document.querySelectorAll<HTMLElement>('[data-testid="mention-item"]')].map(e => e.dataset.key)
    expect(keys()).toEqual(['chat-1', 'board-1', 'doc-2'])
    type(v, 'pri')
    expect(keys()).toEqual(['doc-2'])
    key(v, 'Enter')
    expect(v.state.doc.toString()).toBe('Notes [Pricing notes](session:doc-2) ')
    expect(chips(v)).toHaveLength(1)
  })

  it('"@" inside a word is text, and a query that matches nothing closes the picker', () => {
    const { v } = make('mail me')
    type(v, '@home')
    expect(isMentionOpen(v.state)).toBe(false)
    expect(v.state.doc.toString()).toBe('mail me@home')
    const w = make('ping ').v
    type(w, '@zz')
    expect(isMentionOpen(w.state)).toBe(false)
    expect(w.state.doc.toString()).toBe('ping @zz')
  })

  it('the "/" menu\'s Link to session opens the same picker', () => {
    const { v } = make('intro\n\n')
    type(v, '/link')
    key(v, 'Enter')
    expect(isMentionOpen(v.state)).toBe(true)
    expect(v.state.doc.toString()).toBe('intro\n\n@')
  })

  it('names kinds from the slot and keeps titles from ending the link', () => {
    expect(sessionKind({})).toBe('chat')
    expect(sessionKind({ doc: `${BOARD_ID_PREFIX}123` })).toBe('board')
    expect(sessionKind({ doc: '0123456789abcdef' })).toBe('doc')
    expect(mentionMarkdown({ key: 'k', title: 'a [b] c' })).toBe('[a  b  c](session:k)')
    // This doc's own session is never offered.
    expect(matchSessions({ sessions: SESSIONS, self: 'doc-me' }, 'this').map(s => s.key)).toEqual([])
    expect(matchSessions({ sessions: SESSIONS, self: 'doc-me' }, 'road').map(s => s.key)).toEqual(['board-1'])
  })
})
