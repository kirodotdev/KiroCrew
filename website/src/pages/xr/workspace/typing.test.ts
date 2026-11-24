import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'
import { PAGE_KEYS, keyFate, keysAfter, typed, wantsField } from './typing'

const FIELD = { editable: true, guarded: false }
const BUTTON = { editable: false, guarded: false }
const GUARDED = { editable: true, guarded: true }

describe('the keys of a real keyboard in the flat preview', () => {
  it('turn the head with the arrows and are the browser\'s own otherwise, while no page was pressed', () => {
    expect(keyFate({ key: 'ArrowLeft' }, false, null)).toEqual({ to: 'head' })
    expect(keyFate({ key: 'a' }, false, null)).toEqual({ to: 'browser' })
    expect(keyFate({ key: 'Enter' }, false, FIELD)).toEqual({ to: 'browser' })
  })

  it('go to the page that was pressed last: a letter as text, Enter and Backspace as keys', () => {
    expect(keyFate({ key: 'h' }, true, FIELD)).toEqual({ to: 'text', text: 'h' })
    expect(keyFate({ key: ' ' }, true, FIELD)).toEqual({ to: 'text', text: ' ' })
    expect(keyFate({ key: '好' }, true, FIELD)).toEqual({ to: 'text', text: '好' })
    expect(keyFate({ key: '😀' }, true, FIELD)).toEqual({ to: 'text', text: '😀' })
    expect(keyFate({ key: 'Enter' }, true, FIELD)).toEqual({ to: 'key', key: 'Enter' })
    expect(keyFate({ key: 'Backspace' }, true, FIELD)).toEqual({ to: 'key', key: 'Backspace' })
    // A press on a button of the page: Enter and Tab still go to the page.
    expect(keyFate({ key: 'Enter' }, true, BUTTON)).toEqual({ to: 'key', key: 'Enter' })
    expect(keyFate({ key: 'Tab' }, true, null)).toEqual({ to: 'key', key: 'Tab' })
  })

  it('keep the arrow keys for the head until a field of the page has the focus', () => {
    expect(keyFate({ key: 'ArrowLeft' }, true, null)).toEqual({ to: 'head' })
    expect(keyFate({ key: 'ArrowUp' }, true, BUTTON)).toEqual({ to: 'head' })
    expect(keyFate({ key: 'ArrowLeft' }, true, FIELD)).toEqual({ to: 'key', key: 'ArrowLeft' })
  })

  it('leave a key with Ctrl, Alt or the command key, an input method\'s key and Shift+Tab to the browser', () => {
    expect(keyFate({ key: 'r', ctrlKey: true }, true, FIELD)).toEqual({ to: 'browser' })
    expect(keyFate({ key: 'v', metaKey: true }, true, FIELD)).toEqual({ to: 'browser' })
    expect(keyFate({ key: 'ArrowLeft', altKey: true }, true, FIELD)).toEqual({ to: 'browser' })
    expect(keyFate({ key: 'a', isComposing: true }, true, FIELD)).toEqual({ to: 'browser' })
    expect(keyFate({ key: 'Tab', shiftKey: true }, true, FIELD)).toEqual({ to: 'browser' })
    // A capital letter is typed with Shift, and is text.
    expect(keyFate({ key: 'A', shiftKey: true }, true, FIELD)).toEqual({ to: 'text', text: 'A' })
  })

  it('send no key that the gateway has no name for', () => {
    for (const key of ['F5', 'Shift', 'Control', 'CapsLock', 'Unidentified', 'Process', 'Dead']) expect(keyFate({ key }, true, FIELD)).toEqual({ to: 'browser' })
  })

  it('name the same keys as the gateway does', () => {
    const hub = readFileSync(resolve(__dirname, '../../../../../src/kiro_crew/browser_harness/panel_hub.py'), 'utf8')
    const list = /PANEL_KEYS = frozenset\(\s*\{([^}]*)\}/.exec(hub)?.[1] ?? ''
    const names = [...list.matchAll(/"([A-Za-z]+)"/g)].map(m => m[1])
    expect(names.length).toBeGreaterThan(0)
    expect([...PAGE_KEYS].sort()).toEqual(names.sort())
  })
})

describe('the phone\'s own text field', () => {
  it('shows after a finger\'s press, while the page\'s focus is in a field that takes text', () => {
    expect(wantsField(true, FIELD, false)).toBe(true)
  })

  it('does not show for a mouse, for a button, before the gateway has said where the focus is, or once put away', () => {
    expect(wantsField(false, FIELD, false)).toBe(false)
    expect(wantsField(true, BUTTON, false)).toBe(false)
    expect(wantsField(true, null, false)).toBe(false)
    expect(wantsField(true, FIELD, true)).toBe(false)
  })

  it('does not show for a field inside a surface that grants an agent permission', () => {
    expect(wantsField(true, GUARDED, false)).toBe(false)
  })
})

describe('what a change of the phone\'s field means for the page', () => {
  it('is the new letters, typed one after the other', () => {
    expect(typed('', 'h')).toEqual({ back: 0, text: 'h' })
    expect(typed('h', 'he')).toEqual({ back: 0, text: 'e' })
    expect(typed('hello', 'hello world')).toEqual({ back: 0, text: ' world' })
  })

  it('is that many Backspaces for letters taken away at the end', () => {
    expect(typed('hello', 'hell')).toEqual({ back: 1, text: '' })
    expect(typed('hello', '')).toEqual({ back: 5, text: '' })
  })

  it('takes the last word back and types the corrected one, as a phone\'s keyboard corrects', () => {
    expect(typed('send teh', 'send the ')).toEqual({ back: 2, text: 'he ' })
    expect(typed('ni', '你')).toEqual({ back: 2, text: '你' })
  })

  it('counts a letter outside the basic plane as one, as the page\'s Backspace takes it', () => {
    expect(typed('ok 😀', 'ok ')).toEqual({ back: 1, text: '' })
    expect(typed('😀', '😀😀')).toEqual({ back: 0, text: '😀' })
  })

  it('is nothing when nothing changed', () => {
    expect(typed('same', 'same')).toEqual({ back: 0, text: '' })
  })
})

describe('which page the keys are for', () => {
  const chat = { id: 'page', touch: false }

  it('leaves the keys with the page that has them when an ask\'s panel comes up, and gives them to it after a press on it', () => {
    expect(keysAfter(chat, { cameUp: 'ask-1' })).toBe(chat)
    expect(keysAfter(null, { cameUp: 'ask-1' })).toBeNull()
    expect(keysAfter(chat, { press: { id: 'ask-1', touch: true } })).toEqual({ id: 'ask-1', touch: true })
  })

  it('takes them from every page for a press on empty space, and from a page that closes only when it has them', () => {
    expect(keysAfter(chat, { away: true })).toBeNull()
    expect(keysAfter(chat, { closed: 'page' })).toBeNull()
    // The ask's panel closes by itself after the answer: the person types on in his page.
    expect(keysAfter(chat, { closed: 'ask-1' })).toBe(chat)
  })
})
