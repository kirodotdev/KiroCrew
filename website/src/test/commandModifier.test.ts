/**
 * The platform command modifier test.
 *
 * The bug this locks: a `metaKey || ctrlKey` test is satisfied when BOTH are
 * held, so shortcut handlers matched macOS's own Cmd+Ctrl chords and their
 * `preventDefault()` swallowed them — Control+Command+F (Toggle Full Screen)
 * opened an in-app find bar instead of toggling fullscreen.
 */
import { describe, it, expect } from 'vitest'
import { hasCommandModifier } from '../utils/commandModifier'

const ev = (metaKey: boolean, ctrlKey: boolean) => ({ metaKey, ctrlKey })

describe('hasCommandModifier', () => {
  it('is true for Cmd alone', () => {
    expect(hasCommandModifier(ev(true, false))).toBe(true)
  })

  it('is true for Ctrl alone', () => {
    expect(hasCommandModifier(ev(false, true))).toBe(true)
  })

  it('is false for neither', () => {
    expect(hasCommandModifier(ev(false, false))).toBe(false)
  })

  it('is false for both — Cmd+Ctrl is an OS-reserved chord', () => {
    expect(hasCommandModifier(ev(true, true))).toBe(false)
  })
})
