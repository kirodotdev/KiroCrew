import { describe, it, expect, afterEach, beforeEach, vi } from 'vitest'
import { createElement } from 'react'
import { render, waitFor, act, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { APP_FRAME_TAG } from '../appFrame/frameProtocol'
import { BURST_CAPACITY, MAX_MESSAGE_BYTES, readFrameMessage } from './linkPreviewProtocol'
import { FRAME_SANDBOX, MAX_PINNED, READY_TIMEOUT_MS } from './linkPreviewFrames'
import { resolveLinkPreviews } from './linkPreviewRegistry'
import { fromFrame, installFrameWindows, postFrom, sentTo, type Sent } from '../test/LinkPreviewFramesTestUtils'

const listApps = vi.fn()
vi.mock('../api/client', () => ({ api: { listApps: () => listApps() } }))

const APP = {
  name: 'cards-app',
  enabled: true,
  manifest: { contributes: { linkPreviews: [{ id: 'cards', entry: 'dist/lp.mjs', hosts: ['tickets.example.com'] }] } },
}

const mint = vi.fn(async () => ({ frame: '/app-frame/cards-app/lp/f/tok', capability: 'cap-1', ttl: 3600 }))

/** The frame the layer created, the messages it was sent, and a way to answer as it. */
function theFrame() {
  const el = document.querySelector<HTMLIFrameElement>('iframe[data-link-preview-frame]')
  if (!el) throw new Error('no frame')
  return el
}
installFrameWindows()
const watch = sentTo
const post = postFrom
const types = (sent: Sent[]) => sent.map((m) => m.type)

async function mount() {
  const mod = await import('./LinkPreviewLayer')
  mod.setLinkPreviewMintForTest(mint)
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(createElement(QueryClientProvider, { client: qc }, createElement(mod.default)))
  await waitFor(() => expect(listApps).toHaveBeenCalled())
  await act(async () => { await vi.advanceTimersByTimeAsync(50) })
  return { mod, qc }
}
/** A link in a chat message: inside the scope a MarkdownRenderer with textPreviews draws. */
function link(href = 'https://tickets.example.com/t/TK-1', scoped = true) {
  const a = document.createElement('a')
  a.href = href
  a.textContent = 'TK-1'
  a.getBoundingClientRect = () => ({ left: 100, top: 50, width: 40, height: 16, right: 140, bottom: 66, x: 100, y: 50, toJSON() {} })
  const holder = document.createElement('div')
  if (scoped) holder.setAttribute('data-link-preview-scope', '')
  holder.append(a)
  document.body.append(holder)
  return a
}
/** Hover `a`, let the frame load and answer `ready`, and return what it was sent. */
async function hoverReady(a: HTMLElement) {
  fireEvent.pointerOver(a)
  await act(async () => { await vi.advanceTimersByTimeAsync(0) })
  const el = theFrame()
  const sent = watch(el)
  fromFrame(el, 'ready')
  await act(async () => { await vi.advanceTimersByTimeAsync(400) })
  return { el, sent }
}

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true })
  listApps.mockResolvedValue([APP])
})
afterEach(async () => {
  const mod = await import('./LinkPreviewLayer')
  mod.setLinkPreviewMintForTest()
  listApps.mockReset()
  mint.mockClear()
  document.body.replaceChildren()
  vi.useRealTimers()
})

describe('LinkPreviewLayer: the card runs in a sandboxed frame', () => {
  it('the frame is sandboxed to scripts only, and the app module never loads in this document', async () => {
    await mount()
    const { el } = await hoverReady(link())
    expect(el.getAttribute('sandbox')).toBe(FRAME_SANDBOX)
    expect(FRAME_SANDBOX).toBe('allow-scripts')
    expect(el.getAttribute('sandbox')).not.toMatch(/allow-same-origin/)
    expect(el.getAttribute('src')).toBe('/app-frame/cards-app/lp/f/tok')
    expect(el.getAttribute('referrerpolicy')).toBe('no-referrer')
    expect(mint.mock.calls[0][0]).toBe('cards-app')
  })

  it('the frame gets the capability and theme, then the URL and the anchor rect, never an element', async () => {
    await mount()
    const a = link()
    const { sent } = await hoverReady(a)
    expect(sent[0]).toMatchObject({ [APP_FRAME_TAG]: 1, type: 'init', capability: 'cap-1', theme: 'light' })
    const show = sent.find((m) => m.type === 'show')
    expect(show).toMatchObject({ url: 'https://tickets.example.com/t/TK-1', rect: { left: 100, top: 50, width: 40, height: 16 } })
    expect(Object.values(show ?? {}).some((v) => v instanceof Node)).toBe(false)
  })

  it('a slow frame still gets show() once it is ready', async () => {
    await mount()
    const a = link()
    fireEvent.pointerOver(a)
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    const el = theFrame()
    const sent = watch(el)
    await act(async () => { await vi.advanceTimersByTimeAsync(2000) })
    expect(types(sent)).toEqual(['init'])
    fromFrame(el, 'ready')
    await waitFor(() => expect(types(sent)).toContain('show'))
  })

  for (const [how, dismiss] of [
    ['leaves', (a: HTMLElement, other: HTMLElement) => fireEvent.pointerOut(a, { relatedTarget: other })],
    ['scrolls', () => fireEvent.scroll(window)],
    ['presses Escape', () => fireEvent.keyDown(document, { key: 'Escape' })],
  ] as const) {
    it(`a show still waiting for a loading frame is dropped when the user ${how}`, async () => {
      await mount()
      const other = document.createElement('div')
      document.body.append(other)
      const a = link()
      fireEvent.pointerOver(a)
      await act(async () => { await vi.advanceTimersByTimeAsync(0) })
      const el = theFrame()
      const sent = watch(el)
      await act(async () => { await vi.advanceTimersByTimeAsync(400) }) // the delay passes; the frame is still loading
      dismiss(a, other)
      fromFrame(el, 'ready')
      await act(async () => { await vi.advanceTimersByTimeAsync(50) })
      expect(types(sent)).not.toContain('show')
    })
  }

  it('a frame removed while loading does not report a load failure later', async () => {
    const errors = vi.spyOn(console, 'error').mockImplementation(() => {})
    const { qc } = await mount()
    fireEvent.pointerOver(link())
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    listApps.mockResolvedValue([{ ...APP, enabled: false }])
    await act(async () => { await qc.invalidateQueries({ queryKey: ['apps'] }) })
    await waitFor(() => expect(document.querySelector('iframe[data-link-preview-frame]')).toBeNull())
    await act(async () => { await vi.advanceTimersByTimeAsync(READY_TIMEOUT_MS + 10) })
    expect(errors).not.toHaveBeenCalled()
    expect(document.querySelector('[data-testid="link-preview-failed"]')).toBeNull()
    errors.mockRestore()
  })

  it('the dashboard places, sizes, shows and hides the frame; pin, drag and dock move it', async () => {
    await mount()
    const a = link()
    const { el, sent } = await hoverReady(a)
    expect(el.style.visibility).toBe('hidden')
    fromFrame(el, 'resize', { width: 300, height: 120 })
    fromFrame(el, 'open')
    expect(el.style.visibility).toBe('visible')
    expect(el.style.pointerEvents).toBe('auto')
    // Below the anchor, at its left edge.
    expect([el.style.left, el.style.top, el.style.width, el.style.height]).toEqual(['100px', '72px', '300px', '120px'])
    // A card cannot size itself past the viewport.
    fromFrame(el, 'resize', { width: 100_000, height: 100_000 })
    expect(parseInt(el.style.width, 10)).toBeLessThanOrEqual(window.innerWidth - 16)
    fromFrame(el, 'resize', { width: 300, height: 120 })
    fromFrame(el, 'drag', { dx: 30, dy: 10 })
    expect([el.style.left, el.style.top]).toEqual(['130px', '82px'])
    fromFrame(el, 'dock', { edge: 'right' })
    expect(el.style.left).toBe(`${window.innerWidth - 300 - 8}px`)
    // Pinned: the chat scrolling does not take it away.
    fromFrame(el, 'pin', { pinned: true })
    fireEvent.scroll(window)
    expect(el.isConnected).toBe(true)
    expect(el.style.visibility).toBe('visible')
    expect(types(sent)).not.toContain('hide')
    // Unpinned again: the next scroll does.
    fromFrame(el, 'pin', { pinned: false })
    fireEvent.scroll(window)
    expect(sent.at(-1)).toMatchObject({ type: 'hide', reason: 'scroll' })
  })

  it('an unpinned card hides on scroll and Escape, and leave() reaches the frame', async () => {
    await mount()
    const a = link()
    const other = document.createElement('div')
    document.body.append(other)
    const { el, sent } = await hoverReady(a)
    fromFrame(el, 'open')
    fireEvent.pointerOut(a, { relatedTarget: other })
    expect(types(sent)).toContain('leave')
    fireEvent.scroll(window)
    expect(el.style.visibility).toBe('hidden')
    expect(sent.at(-1)).toMatchObject({ type: 'hide', reason: 'scroll' })
    fromFrame(el, 'open')
    fromFrame(el, 'escape') // Escape pressed inside the card
    expect(el.style.visibility).toBe('hidden')
    expect(sent.at(-1)).toMatchObject({ type: 'hide', reason: 'escape' })
  })

  it('a plain key reaches a showing card, never while the dashboard takes text or with a modifier', async () => {
    await mount()
    const { el, sent } = await hoverReady(link())
    fireEvent.keyDown(document, { key: 'p', code: 'KeyP' })
    expect(types(sent)).not.toContain('key') // not showing yet
    fromFrame(el, 'open')
    fireEvent.keyDown(document, { key: 'p', code: 'KeyP' })
    expect(sent.at(-1)).toMatchObject({ type: 'key', key: 'p', code: 'KeyP', shift: false })
    const n = sent.length
    fireEvent.keyDown(document, { key: 'c', metaKey: true })
    const box = document.createElement('textarea')
    document.body.append(box)
    box.focus()
    fireEvent.keyDown(box, { key: 'p' })
    expect(sent.length).toBe(n)
    box.remove()
    // The code editor's editable node sits in a shadow root: document.activeElement
    // is only its host, so the guard has to look inside.
    const host = document.createElement('div')
    document.body.append(host)
    const inner = document.createElement('div')
    inner.contentEditable = 'true'
    inner.tabIndex = 0
    host.attachShadow({ mode: 'open' }).append(inner)
    inner.focus()
    fireEvent.keyDown(inner, { key: 'p', composed: true })
    fireEvent.keyDown(document, { key: 'p' })
    expect(sent.length).toBe(n)
  })

  it('a pinned card keeps its frame; the next hover gets a new one, and pins are capped', async () => {
    await mount()
    const first = await hoverReady(link())
    fromFrame(first.el, 'open')
    fromFrame(first.el, 'pin', { pinned: true })
    const other = document.createElement('div')
    document.body.append(other)
    const b = link('https://tickets.example.com/t/TK-2')
    fireEvent.pointerOver(other)
    fireEvent.pointerOver(b)
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    const frames = [...document.querySelectorAll('iframe[data-link-preview-frame]')]
    expect(frames).toHaveLength(2)
    expect(frames[0]).toBe(first.el)
    expect(MAX_PINNED).toBeGreaterThan(1)
  })

  it('a link outside a chat message never reaches an app', async () => {
    await mount()
    fireEvent.pointerOver(link('https://tickets.example.com/t/TK-9', false))
    await act(async () => { await vi.advanceTimersByTimeAsync(400) })
    expect(document.querySelector('iframe[data-link-preview-frame]')).toBeNull()
    expect(mint).not.toHaveBeenCalled()
  })

  it("a frame's failure is shown only under its own hover, never under a later one or after leaving", async () => {
    const errors = vi.spyOn(console, 'error').mockImplementation(() => {})
    listApps.mockResolvedValue([APP, {
      name: 'other-app', enabled: true,
      manifest: { contributes: { linkPreviews: [{ id: 'cards', entry: 'dist/o.mjs', hosts: ['other.example.com'] }] } },
    }])
    await mount()
    const a = link()
    const b = link('https://other.example.com/x')
    fireEvent.pointerOver(a)
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    const first = theFrame()
    // Straight onto the other app's link: the hover in progress is now b's.
    fireEvent.pointerOver(b)
    await act(async () => { await vi.advanceTimersByTimeAsync(10) })
    await act(async () => { fromFrame(first, 'failed', { message: 'boom' }) })
    expect(document.querySelector('[data-testid="link-preview-failed"]')).toBeNull()
    errors.mockRestore()
  })

  it('an app list that cannot be read says so under a hovered chat link, and asks again', async () => {
    listApps.mockRejectedValue(new Error('HTTP 500'))
    const mod = await import('./LinkPreviewLayer')
    mod.setLinkPreviewMintForTest(mint)
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(createElement(QueryClientProvider, { client: qc }, createElement(mod.default)))
    await waitFor(() => expect(listApps).toHaveBeenCalledTimes(1))
    await act(async () => { await vi.advanceTimersByTimeAsync(50) })
    const a = link()
    fireEvent.pointerOver(a)
    await act(async () => { await vi.advanceTimersByTimeAsync(400) })
    expect(document.querySelector('[data-testid="link-preview-failed"] [role="alert"]')).not.toBeNull()
    expect(listApps).toHaveBeenCalledTimes(2)
    fireEvent.pointerOut(a, { relatedTarget: document.body })
    expect(document.querySelector('[data-testid="link-preview-failed"]')).toBeNull()
  })

  it('messages from any other window are ignored', async () => {
    await mount()
    const { el } = await hoverReady(link())
    fromFrame(window, 'open')
    fromFrame(null, 'open')
    expect(el.style.visibility).toBe('hidden')
    post({ type: 'open' }, el.contentWindow)
    expect(el.style.visibility).toBe('hidden')
  })

  it('a frame that never says ready fails: a notice under the link, and a retry only after RETRY_AFTER_MS', async () => {
    const errors = vi.spyOn(console, 'error').mockImplementation(() => {})
    const { mod } = await mount()
    const a = link()
    const other = document.createElement('div')
    document.body.append(other)
    fireEvent.pointerOver(a)
    await act(async () => { await vi.advanceTimersByTimeAsync(READY_TIMEOUT_MS + 10) })
    expect(document.querySelector('iframe[data-link-preview-frame]')).toBeNull()
    expect(document.querySelector('[data-testid="link-preview-failed"] [role="alert"]')).not.toBeNull()
    expect(mint).toHaveBeenCalledTimes(1)
    const hover = async () => {
      fireEvent.pointerOut(a, { relatedTarget: other })
      fireEvent.pointerOver(other)
      fireEvent.pointerOver(a)
      await act(async () => { await vi.advanceTimersByTimeAsync(400) })
    }
    await hover()
    expect(mint).toHaveBeenCalledTimes(1) // inside the retry window
    await act(async () => { await vi.advanceTimersByTimeAsync(mod.RETRY_AFTER_MS) })
    await hover()
    expect(mint).toHaveBeenCalledTimes(2)
    errors.mockRestore()
  })

  it('a frame that reports failed is removed at once', async () => {
    const errors = vi.spyOn(console, 'error').mockImplementation(() => {})
    await mount()
    fireEvent.pointerOver(link())
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    const el = theFrame()
    fromFrame(el, 'failed', { message: 'module exports no activate(ctx)' })
    expect(el.isConnected).toBe(false)
    errors.mockRestore()
  })

  it('disabling the app hides and disposes its card and removes the frame', async () => {
    const { qc } = await mount()
    const { el, sent } = await hoverReady(link())
    fromFrame(el, 'open')
    listApps.mockResolvedValue([{ ...APP, enabled: false }])
    await act(async () => { await qc.invalidateQueries({ queryKey: ['apps'] }) })
    await waitFor(() => expect(el.isConnected).toBe(false))
    expect(sent.slice(-2)).toMatchObject([{ type: 'hide', reason: 'disabled' }, { type: 'dispose' }])
  })

  it('an app update replaces its frame, so the next hover loads the new module', async () => {
    const { qc } = await mount()
    const first = await hoverReady(link())
    fromFrame(first.el, 'open')
    // Same provider id and host, a new install of the app.
    listApps.mockResolvedValue([{ ...APP, version: '2.0.0', installedAt: '2026-10-31T13:00:00Z' }])
    await act(async () => { await qc.invalidateQueries({ queryKey: ['apps'] }) })
    await waitFor(() => expect(first.el.isConnected).toBe(false))
    expect(first.sent.slice(-2)).toMatchObject([{ type: 'hide', reason: 'disabled' }, { type: 'dispose' }])
    const second = await hoverReady(link('https://tickets.example.com/t/TK-2'))
    expect(second.el).not.toBe(first.el)
    expect(mint).toHaveBeenCalledTimes(2)
    // An unrelated refresh with the same install keeps the frame.
    await act(async () => { await qc.invalidateQueries({ queryKey: ['apps'] }) })
    expect(second.el.isConnected).toBe(true)
    // A same-version Sync replaces the files: only updatedAt moves.
    listApps.mockResolvedValue([{ ...APP, version: '2.0.0', installedAt: '2026-10-31T13:00:00Z', updatedAt: '2026-10-31T14:00:00Z' }])
    await act(async () => { await qc.invalidateQueries({ queryKey: ['apps'] }) })
    await waitFor(() => expect(second.el.isConnected).toBe(false))
  })

  it('a capabilityExpired message is answered with a fresh capability', async () => {
    await mount()
    const { el, sent } = await hoverReady(link())
    mint.mockResolvedValueOnce({ frame: '/app-frame/cards-app/lp/f/tok2', capability: 'cap-2', ttl: 3600 })
    fromFrame(el, 'capabilityExpired')
    await waitFor(() => expect(sent.at(-1)).toMatchObject({ type: 'capability', capability: 'cap-2' }))
  })
})

describe('LinkPreviewLayer: dismissal retires the show', () => {
  it('a card that was shown but not yet opened is told to hide, and its late open is dropped', async () => {
    await mount()
    const { el, sent } = await hoverReady(link())
    const show = sent.filter((m) => m.type === 'show').at(-1)!
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(sent.at(-1)).toMatchObject({ type: 'hide', reason: 'escape' })
    // The provider's fetch finishes after the dismissal and opens that show's card.
    fromFrame(el, 'open', { invocation: show.invocation })
    fromFrame(el, 'resize', { invocation: show.invocation, width: 200, height: 100 })
    expect(el.style.visibility).toBe('hidden')
  })
})

describe('LinkPreviewLayer: the protocol is bounded', () => {
  it('every show is numbered, and a card message for an older show is dropped', async () => {
    await mount()
    const a = link()
    const { el, sent } = await hoverReady(a)
    expect(sent.filter((m) => m.type === 'show').map((m) => m.invocation)).toEqual([1])
    // A second show for another link of the same app.
    const other = document.createElement('div')
    document.body.append(other)
    fireEvent.pointerOut(a, { relatedTarget: other })
    const b = link('https://tickets.example.com/t/TK-2')
    fireEvent.pointerOver(b)
    await act(async () => { await vi.advanceTimersByTimeAsync(400) })
    expect(sent.filter((m) => m.type === 'show').map((m) => m.invocation)).toEqual([1, 2])
    fromFrame(el, 'open', { invocation: 1 }) // answers the first show: stale
    expect(el.style.visibility).toBe('hidden')
    fromFrame(el, 'open', { invocation: 2 })
    expect(el.style.visibility).toBe('visible')
    fromFrame(el, 'resize', { invocation: 1, width: 500, height: 500 })
    expect(el.style.width).not.toBe('500px')
  })

  it('a message over the byte limit is dropped unread', async () => {
    await mount()
    const { el } = await hoverReady(link())
    fromFrame(el, 'open', { pad: 'x'.repeat(MAX_MESSAGE_BYTES) })
    expect(el.style.visibility).toBe('hidden')
    fromFrame(el, 'open')
    expect(el.style.visibility).toBe('visible')
  })

  it('a burst past the allowance is dropped until it refills', async () => {
    await mount()
    const { el } = await hoverReady(link())
    for (let i = 0; i < BURST_CAPACITY + 10; i++) fromFrame(el, 'resize', { width: 300, height: 120 })
    fromFrame(el, 'open') // past the allowance: dropped
    expect(el.style.visibility).toBe('hidden')
    await act(async () => { await vi.advanceTimersByTimeAsync(1000) })
    fromFrame(el, 'open')
    expect(el.style.visibility).toBe('visible')
  })

  it('a frame that navigates itself away is closed', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    await mount()
    const { el } = await hoverReady(link())
    el.dispatchEvent(new Event('load')) // a second load: no longer the card document
    expect(el.isConnected).toBe(false)
    expect(warn).toHaveBeenCalledTimes(1)
    warn.mockRestore()
  })

  it('the hovered URL goes without its query and fragment unless the app opts in with fullUrl', async () => {
    await mount()
    const { sent } = await hoverReady(link('https://tickets.example.com/t/TK-1?sig=secret#frag'))
    expect(sent.find((m) => m.type === 'show')).toMatchObject({ url: 'https://tickets.example.com/t/TK-1' })
  })

  it('with fullUrl the module gets the whole URL, once the owner approved it', async () => {
    const manifest = { contributes: { linkPreviews: [{ ...APP.manifest.contributes.linkPreviews[0], fullUrl: true }] } }
    // Asked for but still waiting on the App Detail page: stripped.
    const waiting = resolveLinkPreviews([{ ...APP, manifest, linkPreviewGrant: { hosts: ['tickets.example.com'], textPatterns: [], fullUrl: false } }])
    expect(waiting[0].fullUrl).toBe(false)
    expect(resolveLinkPreviews([{ ...APP, manifest }])[0].fullUrl).toBe(false)
    listApps.mockResolvedValue([{ ...APP, manifest, linkPreviewGrant: { hosts: ['tickets.example.com'], textPatterns: [], fullUrl: true } }])
    await mount()
    const { sent } = await hoverReady(link('https://tickets.example.com/t/TK-1?sig=secret#frag'))
    expect(sent.find((m) => m.type === 'show')).toMatchObject({ url: 'https://tickets.example.com/t/TK-1?sig=secret#frag' })
  })
})

describe('LinkPreviewLayer: overlay mode (a provider with windows of its own)', () => {
  it('covers the viewport, is clipped to the reported regions, and ignores card messages', async () => {
    await mount()
    const { el, sent } = await hoverReady(link())
    fromFrame(el, 'regions', { rects: [{ left: 10, top: 20, width: 300, height: 100 }, { left: -50, top: 5, width: 100, height: 10 }] })
    expect([el.style.left, el.style.top, el.style.width, el.style.height]).toEqual(['0px', '0px', `${window.innerWidth}px`, `${window.innerHeight}px`])
    // Off-screen parts are cut to the viewport.
    expect(el.style.clipPath).toBe("path('M 10 20 h 300 v 100 h -300 Z M 0 5 h 50 v 10 h -50 Z')")
    expect(el.style.visibility).toBe('visible')
    fromFrame(el, 'drag', { dx: 30, dy: 30 })
    fromFrame(el, 'resize', { width: 10, height: 10 })
    expect(el.style.width).toBe(`${window.innerWidth}px`)
    // The chat scrolling tells the provider, which decides what goes; the frame stays.
    fireEvent.scroll(window)
    expect(sent.at(-1)).toMatchObject({ type: 'hide', reason: 'scroll' })
    expect(el.isConnected).toBe(true)
    fromFrame(el, 'regions', { rects: [] })
    expect(el.style.visibility).toBe('hidden')
    expect(el.style.pointerEvents).toBe('none')
  })
})

describe('linkPreviewProtocol', () => {
  it('reads only the messages it defines, with their fields coerced', () => {
    const t = { [APP_FRAME_TAG]: 1 }
    expect(readFrameMessage({ ...t, type: 'resize', invocation: 2, width: '9', height: 5 })).toEqual({ type: 'resize', invocation: 2, width: 0, height: 5 })
    expect(readFrameMessage({ ...t, type: 'dock', edge: 'top' })).toEqual({ type: 'dock', invocation: -1, edge: null })
    expect(readFrameMessage({ ...t, type: 'pin', invocation: 1.5, pinned: 'yes' })).toEqual({ type: 'pin', invocation: -1, pinned: false })
    expect(readFrameMessage({ ...t, type: 'textPatternsOff', ids: ['a', 1, 'b'] })).toEqual({ type: 'textPatternsOff', ids: ['a', 'b'] })
    expect(readFrameMessage({ ...t, type: 'navigate', url: 'https://x' })).toBeNull()
    expect(readFrameMessage({ type: 'open' })).toBeNull()
    expect(readFrameMessage('open')).toBeNull()
  })
})
