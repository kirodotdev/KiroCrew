import { describe, it, expect, vi } from 'vitest'
import type { Terminal } from '@xterm/xterm'
import { version as xtermVersion } from '@xterm/xterm/package.json'
import { pressTerminalKey, SOFT_KEYS } from '../utils/terminalKeys'

const TAB_KEY = SOFT_KEYS.find(k => k.aria === 'Tab')!

/** A terminal stand-in: `pressTerminalKey` only touches `textarea`. */
function makeTerm(opened = true) {
  const textarea = document.createElement('textarea')
  document.body.appendChild(textarea)
  const seen: KeyboardEvent[] = []
  textarea.addEventListener('keydown', e => seen.push(e))
  const term = { get textarea() { return opened ? textarea : null } } as unknown as Terminal
  return { term, textarea, seen }
}

describe('pressTerminalKey', () => {
  it('dispatches a keydown at xterm\'s textarea carrying the key identity', () => {
    const { term, seen } = makeTerm()
    expect(pressTerminalKey(term, TAB_KEY)).toBe(true)
    expect(seen).toHaveLength(1)
    expect(seen[0].key).toBe('Tab')
    expect(seen[0].bubbles).toBe(true)
    expect(seen[0].cancelable).toBe(true)
  })

  /**
   * The load-bearing assumption. xterm's key evaluator switches on `keyCode`
   * (`case 9:` for Tab), and `keyCode` is a legacy field the DOM spec omits from
   * `KeyboardEventInit` — engines honour it only as an extension. If an engine
   * ever drops it, every soft key silently becomes a no-op with no error, so the
   * assumption is pinned here rather than discovered in production.
   */
  it('carries keyCode through the event init, which xterm switches on', () => {
    const { term, seen } = makeTerm()
    pressTerminalKey(term, TAB_KEY)
    expect(seen[0].keyCode).toBe(9)
    expect(seen[0].which).toBe(9)
  })

  it('sets ctrlKey only for control combinations', () => {
    const { term, seen } = makeTerm()
    const ctrlC = SOFT_KEYS.find(k => k.aria === 'Control C')!
    pressTerminalKey(term, ctrlC)
    pressTerminalKey(term, TAB_KEY)
    expect(seen[0].ctrlKey).toBe(true)
    expect(seen[0].key).toBe('c')
    expect(seen[1].ctrlKey).toBe(false)
  })

  it('focuses the textarea so the press is not lost to the button that sent it', () => {
    const { term, textarea } = makeTerm()
    const focus = vi.spyOn(textarea, 'focus')
    pressTerminalKey(term, TAB_KEY)
    expect(focus).toHaveBeenCalled()
  })

  it('reports failure when the terminal has not been opened yet', () => {
    const { term, seen } = makeTerm(false)
    expect(pressTerminalKey(term, TAB_KEY)).toBe(false)
    expect(seen).toHaveLength(0)
  })

  it('offers exactly the keys a touch keyboard cannot produce', () => {
    expect(SOFT_KEYS.map(k => k.aria)).toEqual([
      'Escape', 'Tab', 'Left arrow', 'Down arrow', 'Up arrow', 'Right arrow', 'Control C',
    ])
  })

  /**
   * Named keys are spelled out; movement keys use arrow symbols. Order is
   * pinned too — `← ↓ ↑ →` matches vi's hjkl row so the two history keys sit
   * together in the middle.
   *
   * The arrow labels carry no accessible name of their own, which is why every
   * key also has an `aria` string (asserted above and applied by
   * TerminalKeyBar).
   */
  it('labels the keys in the pinned scheme and order', () => {
    expect(SOFT_KEYS.map(k => k.label)).toEqual([
      'esc', 'tab', '←', '↓', '↑', '→', 'ctrl-c',
    ])
  })

  /**
   * Pins the xterm version alongside the keyCode contract: the `case 9` Tab
   * branch this feature relies on is internal, so a major bump should re-verify
   * it rather than pass silently (same rationale as CliPanel.fontRefit.test.ts).
   */
  it('pins the xterm version whose key evaluator switches on keyCode', () => {
    expect(xtermVersion).toMatch(/^5\.5\./)
  })
})
