import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import '../i18n/all'

import BrowserLiveCard from '../pages/chat/BrowserLiveCard'
import { browserLiveStore } from '../hooks/useBrowserLive'
import { ASIDE_ATTR, CLEARANCE_GAP_PX, TOP_OVERLAY_ATTR, holdPinnedPromptAside, overlayInset, overlayOverlap, pinnedPromptAside } from '../pages/chat/browserLiveClearance'
import type { DecodedBrowserFrame } from '../hooks/useBrowserLive.frame'

/**
 * Taking over grows the card; its header (who is in control, Hand back) must
 * not end up under the transcript's top overlay (the title row and the pinned
 * prompt banner the transcript scrolls under).
 */

const S = 'slot:abc'
const ev = (over: Record<string, unknown> = {}) => ({
  session_key: S, turn_id: 't1', state: 'live', url: 'https://github.com/login', title: 'Sign in',
  browser: 'kiro_chromium', headless: true, steps: [], ...over,
})
const frame = (seq: number): DecodedBrowserFrame => ({
  header: { type: 'browser_frame', session_key: S, stream: '', seq, w: 640, h: 400, mask: [] },
  jpeg: new Uint8Array([1, 2, 3]),
  receivedAt: seq,
})
const rect = (top: number, bottom: number) => ({ top, bottom, left: 0, right: 800, width: 800, height: bottom - top, x: 0, y: top, toJSON: () => ({}) }) as DOMRect

class FakeIO {
  constructor(private cb: IntersectionObserverCallback) {}
  observe(el: Element) { this.cb([{ isIntersecting: true, target: el } as IntersectionObserverEntry], this as unknown as IntersectionObserver) }
  disconnect() {}
  unobserve() {}
  takeRecords() { return [] }
  root = null
  rootMargin = ''
  thresholds = []
}

type Writes = NonNullable<Parameters<typeof browserLiveStore.__setWritesForTests>[0]>
let restore: ReturnType<typeof browserLiveStore.__setWritesForTests>

beforeEach(() => {
  browserLiveStore.__resetForTests()
  const ok = vi.fn(async () => ({ ok: true }))
  const writes: Required<Writes> = { ack: ok, visibility: ok, claim: ok, release: ok, input: ok, open: ok, reopen: ok, close: ok }
  restore = browserLiveStore.__setWritesForTests(writes)
  vi.stubGlobal('IntersectionObserver', FakeIO)
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(null)
})
afterEach(() => {
  // Unmount before the store reset: its notify() would otherwise re-render the
  // still-mounted card outside act() (this hook runs before RTL's own cleanup).
  cleanup()
  browserLiveStore.__setWritesForTests(restore)
  browserLiveStore.__resetForTests()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  vi.useRealTimers()
})

/** A scrolling transcript with the overlay over its top, and the card inside it. */
function mountUnderOverlay(cardTop: number, overlayBottom: number) {
  const overlay = document.createElement('div')
  overlay.setAttribute(TOP_OVERLAY_ATTR, '')
  document.body.appendChild(overlay)
  const scroller = document.createElement('div')
  scroller.style.overflowY = 'auto'
  Object.defineProperty(scroller, 'scrollHeight', { value: 5000 })
  Object.defineProperty(scroller, 'clientHeight', { value: 800 })
  scroller.scrollTop = 1000
  document.body.appendChild(scroller)
  vi.spyOn(overlay, 'getBoundingClientRect').mockReturnValue(rect(0, overlayBottom))
  const view = render(<BrowserLiveCard sessionKey={S} anchorKey="call:1" anchorTs={null} running />, { container: scroller })
  const card = screen.getByTestId('browser-live-card')
  // The card's top moves with the scroller, as layout would move it.
  const start = scroller.scrollTop
  vi.spyOn(card, 'getBoundingClientRect').mockImplementation(() => rect(cardTop + (start - scroller.scrollTop), cardTop + 600))
  return { overlay, scroller, card, view }
}

describe('the grown card keeps its header clear of the top overlay', () => {
  it('measures how far the card sits under the overlay, and nothing when it is clear', () => {
    const card = document.createElement('div')
    const overlay = document.createElement('div')
    overlay.setAttribute('data-testid', 'pinned-prompt')
    document.body.append(card, overlay)
    vi.spyOn(overlay, 'getBoundingClientRect').mockReturnValue(rect(40, 150))
    vi.spyOn(card, 'getBoundingClientRect').mockReturnValue(rect(100, 700))
    expect(overlayOverlap(card)).toBe(150 + CLEARANCE_GAP_PX - 100)
    vi.spyOn(card, 'getBoundingClientRect').mockReturnValue(rect(200, 800))
    expect(overlayOverlap(card)).toBe(0)
    overlay.remove()
    card.remove()
  })

  it('the room a grown card may fill leaves out what the overlay covers', () => {
    const scroller = document.createElement('div')
    const overlay = document.createElement('div')
    overlay.setAttribute(TOP_OVERLAY_ATTR, '')
    document.body.append(scroller, overlay)
    vi.spyOn(scroller, 'getBoundingClientRect').mockReturnValue(rect(40, 840))
    vi.spyOn(overlay, 'getBoundingClientRect').mockReturnValue(rect(0, 150))
    // 150 - 40 covered, plus the gap: the card's height cap shrinks by this much.
    expect(overlayInset(scroller)).toBe(110 + CLEARANCE_GAP_PX)
    overlay.remove()
    expect(overlayInset(scroller)).toBe(0)
    scroller.remove()
  })

  it('taking over scrolls the card down from under the pinned prompt, so its header shows', async () => {
    vi.useFakeTimers()
    act(() => { browserLiveStore.applyEvent(ev()); browserLiveStore.applyFrame(frame(1)) })
    const { scroller, card } = mountUnderOverlay(100, 150)
    expect(overlayOverlap(card)).toBe(58)
    act(() => browserLiveStore.applyEvent(ev({ state: 'in_control' })))
    await act(async () => { vi.advanceTimersByTime(500) })
    expect(scroller.scrollTop).toBe(1000 - 58)
    expect(overlayOverlap(card)).toBe(0)
    expect(screen.getByText('You are in control')).toBeInTheDocument()
  })

  it('the grown frame re-fits under a pinned prompt that appears as the transcript scrolls', async () => {
    act(() => { browserLiveStore.applyEvent(ev({ state: 'in_control' })); browserLiveStore.applyFrame(frame(1)) })
    const scroller = document.createElement('div')
    scroller.style.overflowY = 'auto'
    Object.defineProperty(scroller, 'clientHeight', { value: 800 })
    vi.spyOn(scroller, 'getBoundingClientRect').mockReturnValue(rect(0, 800))
    document.body.appendChild(scroller)
    render(<BrowserLiveCard sessionKey={S} anchorKey="call:1" anchorTs={null} running />, { container: scroller })
    const box = screen.getByTestId('browser-live-frame-box')
    // No banner yet: the whole visible height minus the margin.
    await waitFor(() => expect(box.style.maxHeight).toBe('784px'))
    const banner = document.createElement('div')
    banner.setAttribute('data-testid', 'pinned-prompt')
    vi.spyOn(banner, 'getBoundingClientRect').mockReturnValue(rect(90, 150))
    document.body.appendChild(banner)
    scroller.dispatchEvent(new Event('scroll'))
    // The banner covers 150 px of the top (plus the gap): the frame gives that up,
    // so with the transcript held at its bottom the card's header sits below it.
    await waitFor(() => expect(box.style.maxHeight).toBe(`${800 - (150 + CLEARANCE_GAP_PX) - 16}px`))
    banner.remove()
    scroller.remove()
  })

  it('the pinned-prompt banner steps aside while the person holds the browser, and comes back', async () => {
    act(() => { browserLiveStore.applyEvent(ev()); browserLiveStore.applyFrame(frame(1)) })
    render(<BrowserLiveCard sessionKey={S} anchorKey="call:1" anchorTs={null} running />)
    expect(pinnedPromptAside()).toBe(false)
    act(() => browserLiveStore.applyEvent(ev({ state: 'in_control' })))
    expect(pinnedPromptAside()).toBe(true)
    // index.css hides the banner's whole band while <html> carries the attribute.
    expect(document.documentElement).toHaveAttribute(ASIDE_ATTR)
    const css = readFileSync(resolve(__dirname, '../index.css'), 'utf8')
    expect(css).toMatch(/html\[data-browser-live-takeover\] \[data-pinned-prompt-band\] \{\s*visibility: hidden !important;/)
    act(() => browserLiveStore.applyEvent(ev({ state: 'live' })))
    expect(pinnedPromptAside()).toBe(false)
  })

  it('a banner set aside (visibility hidden) covers nothing', () => {
    const card = document.createElement('div')
    const overlay = document.createElement('div')
    overlay.setAttribute('data-testid', 'pinned-prompt')
    overlay.style.visibility = 'hidden'
    document.body.append(card, overlay)
    vi.spyOn(overlay, 'getBoundingClientRect').mockReturnValue(rect(40, 150))
    vi.spyOn(card, 'getBoundingClientRect').mockReturnValue(rect(100, 700))
    expect(overlayOverlap(card)).toBe(0)
    card.remove()
    overlay.remove()
  })

  it('two cards in control hold the banner aside until both hand back', () => {
    const a = holdPinnedPromptAside()
    const b = holdPinnedPromptAside()
    a()
    a()
    expect(pinnedPromptAside()).toBe(true)
    b()
    expect(pinnedPromptAside()).toBe(false)
  })

  it('a card already clear of the overlay is left where it is', async () => {
    vi.useFakeTimers()
    act(() => { browserLiveStore.applyEvent(ev()); browserLiveStore.applyFrame(frame(1)) })
    const { scroller } = mountUnderOverlay(300, 150)
    act(() => browserLiveStore.applyEvent(ev({ state: 'in_control' })))
    await act(async () => { vi.advanceTimersByTime(500) })
    expect(scroller.scrollTop).toBe(1000)
  })
})
