/**
 * `lib/scrollReveal` marks the box that is scrolling, and only that box, for
 * exactly as long as the stylesheet needs to show its thumb.
 *
 * Deterministic: the linger timer runs on fake timers; every assertion follows a
 * dispatched event or an advanced clock, never a sleep. The MutationObserver
 * delivers in a microtask, so the shadow-root case awaits one.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { installScrollReveal, SCROLL_REVEAL_LINGER_MS, SCROLLING_ATTR } from '../lib/scrollReveal'

const scroll = (el: Element) => el.dispatchEvent(new Event('scroll', { bubbles: false }))

describe('scrollReveal', () => {
  let uninstall: () => void
  beforeEach(() => {
    vi.useFakeTimers()
    document.body.replaceChildren()
    uninstall = installScrollReveal(document)
  })
  afterEach(() => {
    uninstall()
    vi.useRealTimers()
  })

  it('marks the scrolling box and clears it after the linger', () => {
    const box = document.createElement('div')
    document.body.append(box)
    scroll(box)
    expect(box.hasAttribute(SCROLLING_ATTR)).toBe(true)
    vi.advanceTimersByTime(SCROLL_REVEAL_LINGER_MS - 1)
    expect(box.hasAttribute(SCROLLING_ATTR)).toBe(true)
    vi.advanceTimersByTime(1)
    expect(box.hasAttribute(SCROLLING_ATTR)).toBe(false)
  })

  it('a scroll during the linger restarts it, so a continuous scroll never flickers', () => {
    const box = document.createElement('div')
    document.body.append(box)
    scroll(box)
    vi.advanceTimersByTime(SCROLL_REVEAL_LINGER_MS - 10)
    scroll(box)
    vi.advanceTimersByTime(SCROLL_REVEAL_LINGER_MS - 10)
    expect(box.hasAttribute(SCROLLING_ATTR)).toBe(true)
    vi.advanceTimersByTime(10)
    expect(box.hasAttribute(SCROLLING_ATTR)).toBe(false)
  })

  it('marks only the box that scrolled, not its ancestors', () => {
    const outer = document.createElement('div')
    const inner = document.createElement('div')
    outer.append(inner)
    document.body.append(outer)
    scroll(inner)
    expect(inner.hasAttribute(SCROLLING_ATTR)).toBe(true)
    expect(outer.hasAttribute(SCROLLING_ATTR)).toBe(false)
  })

  it('reaches a scroller inside a shadow-drawing host added later', async () => {
    // `scroll` is not composed, so the document listener never sees it; the
    // observer must have attached to the host's own root.
    const host = document.createElement('diffs-container')
    const root = host.attachShadow({ mode: 'open' })
    const code = document.createElement('div')
    code.setAttribute('data-code', '')
    root.append(code)
    document.body.append(host)
    await Promise.resolve()
    scroll(code)
    expect(code.hasAttribute(SCROLLING_ATTR)).toBe(true)
    expect(host.hasAttribute(SCROLLING_ATTR)).toBe(false)
  })

  it('finds a host nested inside an added subtree', async () => {
    const wrapper = document.createElement('section')
    const host = document.createElement('file-tree-container')
    const root = host.attachShadow({ mode: 'open' })
    const list = document.createElement('div')
    root.append(list)
    wrapper.append(host)
    document.body.append(wrapper)
    await Promise.resolve()
    scroll(list)
    expect(list.hasAttribute(SCROLLING_ATTR)).toBe(true)
  })

  it('stops marking once uninstalled', () => {
    const box = document.createElement('div')
    document.body.append(box)
    uninstall()
    scroll(box)
    expect(box.hasAttribute(SCROLLING_ATTR)).toBe(false)
    uninstall = () => {}
  })
})
