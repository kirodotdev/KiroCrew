import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import ChatInput from '../components/ChatInput'
import { stubStripHeights } from './stripHeights'

vi.mock('../utils/isTouchDevice', () => ({ isTouchDevice: () => false }))

const defaultProps = { value: '', onChange: vi.fn(), onSend: vi.fn() }

beforeEach(() => {
  stubStripHeights()
  localStorage.clear()
})

/**
 * Mount an editor-shaped surface: a `contentEditable` content node inside an
 * OPEN shadow root, which is how the file editor renders. A keydown from it
 * reaches a document-level listener RETARGETED — `target` is the shadow host, a
 * plain `<div>`, and `composedPath()[0]` is the node holding the caret.
 *
 * jsdom does not retarget composed shadow events, so the shape is modelled on
 * the event. Chromium and Firefox both report the host.
 */
function editorKeydown(key: string): KeyboardEvent {
  const host = document.createElement('div')
  document.body.appendChild(host)
  const root = host.attachShadow({ mode: 'open' })
  const inner = document.createElement('div')
  inner.setAttribute('contenteditable', 'true')
  inner.setAttribute('role', 'textbox')
  root.appendChild(inner)

  const event = new KeyboardEvent('keydown', { key, bubbles: true, composed: true, cancelable: true })
  Object.defineProperty(event, 'target', { value: host })
  Object.defineProperty(event, 'composedPath', {
    value: () => [inner, root, host, document.body, document.documentElement, document, window],
  })
  return event
}

describe('global "/" shortcut vs an editor inside a shadow root', () => {
  it('leaves "/" to the editor and does not pull focus to the composer', () => {
    renderWithProviders(<ChatInput {...defaultProps} />)
    const ta = screen.getByLabelText('Message input')
    ;(ta as HTMLElement).blur()

    const event = editorKeydown('/')
    document.dispatchEvent(event)

    // The composer must not steal the keystroke, and must not cancel it — a
    // cancelled keydown never reaches the caret, so the slash is lost even if
    // focus happened to stay put.
    expect(document.activeElement).not.toBe(ta)
    expect(event.defaultPrevented).toBe(false)
  })

  it('still focuses the composer when "/" comes from the page body', () => {
    renderWithProviders(<ChatInput {...defaultProps} />)
    const ta = screen.getByLabelText('Message input')
    ;(ta as HTMLElement).blur()

    fireEvent.keyDown(document.body, { key: '/' })

    expect(document.activeElement).toBe(ta)
  })
})
