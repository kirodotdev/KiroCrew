/**
 * isEmbeddedPane — "am I the full dashboard inside an iframe?".
 *
 * Both branches matter: a top-level window must NOT be treated as embedded (or
 * the instances switcher disappears from the real dashboard), and a
 * cross-origin parent — which makes `window.top` throw — must be treated as
 * embedded (fail-closed, so a remote can never nest another remote).
 */
import { describe, expect, it, afterEach } from 'vitest'
import { isEmbeddedPane } from './embedded'

const realTop = Object.getOwnPropertyDescriptor(window, 'top')

function stubTop(get: () => unknown) {
  Object.defineProperty(window, 'top', { configurable: true, get })
}

afterEach(() => {
  if (realTop) Object.defineProperty(window, 'top', realTop)
  else delete (window as unknown as Record<string, unknown>).top
})

describe('isEmbeddedPane', () => {
  it('is false at the top level (window.self === window.top)', () => {
    stubTop(() => window)
    expect(isEmbeddedPane()).toBe(false)
  })

  it('is true when framed by a different window', () => {
    stubTop(() => ({ zzqOtherWindow: true }))
    expect(isEmbeddedPane()).toBe(true)
  })

  it('is true when reading window.top throws (cross-origin parent)', () => {
    stubTop(() => {
      throw new DOMException('zzq blocked a frame with a different origin')
    })
    expect(isEmbeddedPane()).toBe(true)
  })
})
