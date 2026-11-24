import { fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { PhoneField } from './PhoneField'

const onText = vi.fn()
const onKey = vi.fn()
const onDone = vi.fn()

function field(): HTMLInputElement {
  render(<PhoneField onText={onText} onKey={onKey} onDone={onDone} />)
  return screen.getByTestId('xr-type-field') as HTMLInputElement
}
/** What an input method does as it composes: the field holds the signs so far, and no key or text is the page's yet. */
function compose(el: HTMLInputElement, soFar: string) {
  fireEvent.compositionStart(el)
  fireEvent.change(el, { target: { value: soFar } })
}
/** The input method is done: the event says which signs it committed. (The test's document has no composition event of its own to carry them.) */
function commit(el: HTMLInputElement, signs: string) {
  const done = new Event('compositionend', { bubbles: true })
  Object.defineProperty(done, 'data', { value: signs })
  fireEvent(el, done)
}
const enters = () => onKey.mock.calls.filter(([key]) => key === 'Enter').length

beforeEach(() => { vi.useFakeTimers(); onText.mockReset(); onKey.mockReset(); onDone.mockReset() })
afterEach(() => { vi.useRealTimers() })

describe('the phone\'s own text field and an input method', () => {
  it('sends the signs and no Enter when the Enter comes while it composes, and the commit after', () => {
    const el = field()
    compose(el, '你好')
    expect(onText).not.toHaveBeenCalled()
    // The Enter that takes the candidate, as most browsers send it: while the composition is still on.
    fireEvent.keyDown(el, { key: 'Enter', isComposing: true, keyCode: 229 })
    commit(el, '你好')
    expect(onText.mock.calls).toEqual([['你好']])
    expect(enters()).toBe(0)
    expect(el.value).toBe('你好')
  })

  it('sends the signs and no Enter when the commit comes first and the Enter after it as a plain key, as on an iPhone', () => {
    const el = field()
    compose(el, '你好')
    commit(el, '你好')
    // WebKit: the composition has ended, and the Enter that took the candidate arrives as a key like any other.
    const plain = fireEvent.keyDown(el, { key: 'Enter', isComposing: false, keyCode: 13 })
    expect(onText.mock.calls).toEqual([['你好']])
    expect(enters()).toBe(0)
    // It is taken from the browser too: nothing else acts on it.
    expect(plain).toBe(false)
    expect(el.value).toBe('你好')
  })

  it('sends the person\'s own Enter once the input method is done', () => {
    const el = field()
    compose(el, '你好')
    commit(el, '你好')
    fireEvent.keyDown(el, { key: 'Enter', keyCode: 13 })
    expect(enters()).toBe(0)
    vi.advanceTimersByTime(60)
    fireEvent.keyDown(el, { key: 'Enter', keyCode: 13 })
    expect(enters()).toBe(1)
    expect(el.value).toBe('')
  })

  it('sends a plain Enter with no composition, and what was typed before it', () => {
    const el = field()
    fireEvent.change(el, { target: { value: 'hello' } })
    fireEvent.keyDown(el, { key: 'Enter', keyCode: 13 })
    expect(onText.mock.calls).toEqual([['hello']])
    expect(onKey.mock.calls).toEqual([['Enter']])
    expect(el.value).toBe('')
  })

  it('sends the Enter after a Korean syllable: that input method has no candidate to take', () => {
    const el = field()
    compose(el, '안')
    commit(el, '안')
    fireEvent.keyDown(el, { key: 'Enter', keyCode: 13 })
    expect(onText.mock.calls).toEqual([['안']])
    expect(enters()).toBe(1)
  })

  it('sends Backspace as a key only from an empty field that is not composing', () => {
    const el = field()
    fireEvent.keyDown(el, { key: 'Backspace' })
    expect(onKey.mock.calls).toEqual([['Backspace']])
    compose(el, '')
    fireEvent.keyDown(el, { key: 'Backspace', isComposing: true, keyCode: 229 })
    expect(onKey).toHaveBeenCalledTimes(1)
  })

  it('is a plain text field with sentence capitals for a field of the page that is no password field', () => {
    const el = field()
    expect(el.type).toBe('text')
    expect(el.getAttribute('autocapitalize')).toBe('sentences')
    expect(el.hasAttribute('autocorrect')).toBe(false)
  })

  it('is a password box for a password field of the page: no capitals, no suggestions, nothing to learn', () => {
    render(<PhoneField onText={onText} onKey={onKey} onDone={onDone} secret />)
    const el = screen.getByTestId('xr-type-field') as HTMLInputElement
    expect(el.type).toBe('password')
    expect(el.getAttribute('autocapitalize')).toBe('none')
    expect(el.getAttribute('autocorrect')).toBe('off')
    expect(el.getAttribute('spellcheck')).toBe('false')
    expect(el.getAttribute('autocomplete')).toBe('off')
    // What is typed still goes to the page as it is typed.
    fireEvent.change(el, { target: { value: 'hunter2' } })
    expect(onText.mock.calls).toEqual([['hunter2']])
  })
})

describe('the phone\'s own text field while the page waits for its fresh picture (D49, REVIEW-17 small 6)', () => {
  it('keeps his line and sends nothing, and sends it all when he sends again once the page takes it', () => {
    let waits = true
    render(<PhoneField onText={onText} onKey={onKey} onDone={onDone} held={() => waits} />)
    const el = screen.getByTestId('xr-type-field') as HTMLInputElement
    fireEvent.change(el, { target: { value: 'hello' } })
    fireEvent.keyDown(el, { key: 'Enter', keyCode: 13 })
    expect(onText).not.toHaveBeenCalled()
    expect(enters()).toBe(0)
    expect(el.value).toBe('hello')
    // The page takes input again: nothing goes by itself.
    waits = false
    expect(onText).not.toHaveBeenCalled()
    fireEvent.keyDown(el, { key: 'Enter', keyCode: 13 })
    expect(onText.mock.calls).toEqual([['hello']])
    expect(enters()).toBe(1)
    expect(el.value).toBe('')
  })
})
