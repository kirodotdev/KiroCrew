/**
 * Session links on the board: a shape's `session:` links draw as chips that open
 * the session, and the shape's textarea gets the doc's "@" picker.
 */
import { describe, it, expect, vi } from 'vitest'
import { useRef, useState } from 'react'
import { act, fireEvent, render, screen } from '@testing-library/react'
import { ShapeLinkText, useMentionPicker, type BoardLinks } from '../pages/chat/page/board/BoardMentions'
import type { SessionRef } from '../pages/chat/page/SessionMention'

const SESSIONS: SessionRef[] = [
  { key: 'chat-1', title: 'Release planning', kind: 'chat', running: false },
  { key: 'board-me', title: 'This board', kind: 'board', running: false },
  { key: 'doc-2', title: 'Pricing notes', kind: 'doc', running: true },
]

function Field({ links, initial = '' }: { links: BoardLinks; initial?: string }) {
  const [text, setText] = useState(initial)
  const field = useRef<HTMLTextAreaElement>(null)
  const picker = useMentionPicker({ value: text, onText: setText, links, field, zoom: 1 })
  return (
    <div>
      <textarea ref={field} aria-label="Shape text" data-testid="field" value={text} onChange={e => picker.onChange(e.target.value, e.target.selectionStart)}
        onKeyDown={e => { picker.onKeyDown(e) }} />
      {picker.menu}
      <output data-testid="value">{text}</output>
    </div>
  )
}

/** Type one character at the end, the way a person would. */
function typeChar(el: HTMLTextAreaElement, ch: string) {
  const next = el.value + ch
  fireEvent.change(el, { target: { value: next, selectionStart: next.length, selectionEnd: next.length } })
}

describe('board session links', () => {
  it('draws a shape\'s session links as chips with the live title, and a click opens one', () => {
    const open = vi.fn()
    render(<div><ShapeLinkText text="See [old](session:doc-2) and [Gone](session:x-9)." links={{ sessions: SESSIONS, self: 'board-me', open }} /></div>)
    const chips = screen.getAllByTestId('session-mention')
    expect(chips.map(c => c.textContent)).toEqual(['Pricing notes', 'Gone'])
    expect(chips[0].getAttribute('data-kind')).toBe('doc')
    expect(chips[0].querySelector('[data-mn="running"]')).not.toBeNull()
    expect(chips[1].getAttribute('data-kind')).toBe('missing')
    fireEvent.click(chips[0])
    fireEvent.click(chips[1])
    expect(open).toHaveBeenCalledTimes(1)
    expect(open).toHaveBeenCalledWith('doc-2')
  })

  it('"@" in a shape\'s text opens the picker without this board, and Enter writes the link', () => {
    render(<Field links={{ sessions: SESSIONS, self: 'board-me', open: () => {} }} initial="Ask " />)
    const el = screen.getByTestId('field') as HTMLTextAreaElement
    typeChar(el, '@')
    const keys = () => screen.queryAllByTestId('mention-item').map(i => i.getAttribute('data-key'))
    expect(keys()).toEqual(['chat-1', 'doc-2'])
    typeChar(el, 'p')
    typeChar(el, 'r')
    expect(keys()).toEqual(['doc-2'])
    act(() => { fireEvent.keyDown(el, { key: 'Enter' }) })
    expect(screen.getByTestId('value').textContent).toBe('Ask [Pricing notes](session:doc-2) ')
    expect(screen.queryByTestId('board-mention-picker')).toBeNull()
  })

  it('"@" inside a word is text, and Escape or no match closes the picker', () => {
    render(<Field links={{ sessions: SESSIONS, self: 'board-me', open: () => {} }} initial="mail" />)
    const el = screen.getByTestId('field') as HTMLTextAreaElement
    typeChar(el, '@')
    expect(screen.queryByTestId('board-mention-picker')).toBeNull()
    typeChar(el, ' ')
    typeChar(el, '@')
    expect(screen.getByTestId('board-mention-picker')).toBeTruthy()
    act(() => { fireEvent.keyDown(el, { key: 'Escape' }) })
    expect(screen.queryByTestId('board-mention-picker')).toBeNull()
    typeChar(el, ' ')
    typeChar(el, '@')
    typeChar(el, 'z')
    expect(screen.queryByTestId('board-mention-picker')).toBeNull()
  })
})
