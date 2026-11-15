import { describe, it, expect, onTestFinished } from 'vitest'
import { isSplitChord } from '../hooks/splitChord'

const setPlatform = (value: string) => {
  const prev = navigator.platform
  onTestFinished(() => Object.defineProperty(navigator, 'platform', { value: prev, configurable: true }))
  Object.defineProperty(navigator, 'platform', { value, configurable: true })
}

const key = (init: KeyboardEventInit) => new KeyboardEvent('keydown', { key: 'd', ...init })

describe('isSplitChord', () => {
  it('on a Mac, splits on Cmd+D and leaves Ctrl+D to the text field', () => {
    setPlatform('MacIntel')
    expect(isSplitChord(key({ metaKey: true }))).toBe(true)
    expect(isSplitChord(key({ ctrlKey: true }))).toBe(false)
  })

  it('elsewhere, splits on Ctrl+D or Cmd+D', () => {
    setPlatform('Win32')
    expect(isSplitChord(key({ ctrlKey: true }))).toBe(true)
    expect(isSplitChord(key({ key: 'D', ctrlKey: true }))).toBe(true)
    expect(isSplitChord(key({ metaKey: true }))).toBe(true)
  })

  it('never splits with Shift or Alt held, or on another key', () => {
    setPlatform('Win32')
    expect(isSplitChord(key({ ctrlKey: true, shiftKey: true }))).toBe(false)
    expect(isSplitChord(key({ ctrlKey: true, altKey: true }))).toBe(false)
    expect(isSplitChord(key({ key: 'k', ctrlKey: true }))).toBe(false)
    expect(isSplitChord(key({}))).toBe(false)
  })
})
