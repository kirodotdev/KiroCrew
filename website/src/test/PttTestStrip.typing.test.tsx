import { describe, expect, it } from 'vitest'
import { act, render, screen } from '@testing-library/react'

import { PttTestStrip, isTypingTarget } from '../components/PttTestStrip'

/**
 * The test strip listens on the document in the CAPTURE phase so it can see a key
 * press wherever focus happens to be. That reach is also its hazard: a first-run
 * UX review found that typing into a sibling setting (the Language box below it)
 * flashed the strip's amber "that was a different key" state on every keystroke —
 * a false-alarm loop triggered by editing an unrelated row.
 */
describe('isTypingTarget — the strip must ignore typing in text fields', () => {
  const el = (tag: string, contentEditable = false) => {
    const node = document.createElement(tag)
    if (contentEditable) Object.defineProperty(node, 'isContentEditable', { value: true })
    return node
  }

  it('excludes text-entry targets', () => {
    expect(isTypingTarget(el('input'))).toBe(true)
    expect(isTypingTarget(el('textarea'))).toBe(true)
    expect(isTypingTarget(el('div', true))).toBe(true)
  })

  // Picking the shortcut key leaves focus on that dropdown, which is exactly
  // when the user reaches for the strip — and a select takes no character input.
  it('does NOT exclude a select', () => {
    expect(isTypingTarget(el('select'))).toBe(false)
  })

  it('does NOT exclude ordinary page targets', () => {
    expect(isTypingTarget(el('div'))).toBe(false)
    expect(isTypingTarget(el('button'))).toBe(false)
    expect(isTypingTarget(document.body)).toBe(false)
  })

  it('survives a target that is not an element', () => {
    expect(isTypingTarget(null)).toBe(false)
    expect(isTypingTarget(new EventTarget())).toBe(false)
    expect(isTypingTarget(document)).toBe(false)
  })
})

/**
 * A CHORD binding (the Windows/Linux default, Alt+Shift+Space) reaches the
 * document as SEPARATE keydowns: Alt, then Shift, then Space. Only the last one
 * satisfies the binding — the modifiers are non-matching presses on their own.
 * The strip used to keep whichever key it saw FIRST, so the default non-macOS
 * binding could never read as matched: the one surface whose entire job is to
 * prove the shortcut works reported the shipped default as the wrong key.
 */
describe('PttTestStrip — a chord binding is recognised on its completing key', () => {
  const chord = { code: 'Space', alt: true, shift: true }
  const strip = (binding: typeof chord | { code: string }) => (
    <PttTestStrip
      binding={binding}
      mode="hybrid"
      holdMs={500}
      modeLabel="Both"
      fieldLabel="Shortcut key"
    />
  )
  const down = (code: string, init: KeyboardEventInit = {}) => act(() => {
    document.dispatchEvent(new KeyboardEvent('keydown', { code, bubbles: true, cancelable: true, ...init }))
  })
  const up = (code: string, init: KeyboardEventInit = {}) => act(() => {
    document.dispatchEvent(new KeyboardEvent('keyup', { code, bubbles: true, ...init }))
  })

  it('matches Alt+Shift+Space pressed in the natural modifiers-first order', () => {
    render(strip(chord))
    down('AltLeft', { altKey: true })
    down('ShiftLeft', { altKey: true, shiftKey: true })
    down('Space', { altKey: true, shiftKey: true })
    up('Space', { altKey: true, shiftKey: true })
    expect(screen.getByText('Key works')).toBeTruthy()
    expect(screen.queryByText("That's a different key")).toBeNull()
  })

  it('still reports a genuinely wrong key', () => {
    render(strip({ code: 'AltRight' }))
    down('KeyJ')
    up('KeyJ')
    expect(screen.getByText("That's a different key")).toBeTruthy()
  })

  it('does not let a later non-matching key hijack a matched press', () => {
    render(strip({ code: 'AltRight' }))
    down('AltRight', { altKey: true })
    down('KeyE', { altKey: true })
    up('AltRight')
    expect(screen.getByText('Key works')).toBeTruthy()
  })
})
