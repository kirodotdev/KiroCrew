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
/** The chat scrolls: `a` moves by `dy`, then the scroll event fires. */
function scrollChat(a: HTMLElement, dy = -120) {
  const r = a.getBoundingClientRect()
  const top = r.top + dy
  a.getBoundingClientRect = () => ({ ...r, top, y: top, bottom: top + r.height, toJSON() {} })
  fireEvent.scroll(window)
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
    ['scrolls', (a: HTMLElement) => scrollChat(a)],
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

  it('a scroll that does not move the link keeps a show waiting for a loading frame', async () => {
    await mount()
    const a = link()
    fireEvent.pointerOver(a)
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    const el = theFrame()
    const sent = watch(el)
    // Something else scrolls (the transcript settling after a reload): the link stays put.
    fireEvent.scroll(window)
    await act(async () => { await vi.advanceTimersByTimeAsync(400) })
    fromFrame(el, 'ready')
    await waitFor(() => expect(types(sent)).toContain('show'))
    expect(types(sent)).not.toContain('hide')
  })

  it('a scroll, moving the link or not, keeps the card the pointer is in', async () => {
    await mount()
    const a = link()
    const { el, sent } = await hoverReady(a)
    fromFrame(el, 'open')
    fireEvent.pointerOut(a, { relatedTarget: el }) // into the card
    fireEvent.scroll(window)
    expect(el.style.visibility).toBe('visible')
    // The chat scrolling moves the link; the shown card still stays.
    scrollChat(a)
    expect(el.style.visibility).toBe('visible')
    expect(types(sent)).not.toContain('hide')
  })

  it('a shown card survives a scroll that moves its link, and goes on the first pointer move outside it', async () => {
    await mount()
    const a = link()
    const other = document.createElement('div')
    document.body.append(other)
    fireEvent.pointerMove(a, { clientX: 110, clientY: 58 })
    const { el, sent } = await hoverReady(a)
    fromFrame(el, 'open')
    // A streaming reply scrolls the chat: the link moves out from under the pointer.
    scrollChat(a)
    // What the browser reports for that scroll, with the pointer where it was.
    fireEvent.pointerOut(a, { relatedTarget: other, clientX: 110, clientY: 58 })
    fireEvent.pointerMove(other, { clientX: 110, clientY: 58 })
    expect(el.style.visibility).toBe('visible')
    expect(types(sent)).not.toContain('hide')
    expect(types(sent)).not.toContain('leave')
    // The pointer moves, outside the card and the link: the usual leave, then
    // the card closes itself (its leave grace is the app's).
    fireEvent.pointerMove(other, { clientX: 140, clientY: 90 })
    expect(sent.at(-1)).toMatchObject({ type: 'leave' })
    fromFrame(el, 'close')
    expect(el.style.visibility).toBe('hidden')
  })

  it('after such a scroll, a card hovered before any pointer move still goes on the first move outside it', async () => {
    await mount()
    const a = link()
    const other = document.createElement('div')
    document.body.append(other)
    // A reply slid the link under a pointer that has not moved since the page loaded.
    fireEvent.pointerOver(a, { clientX: 110, clientY: 58 })
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    const el = theFrame()
    const sent = watch(el)
    fromFrame(el, 'ready')
    await act(async () => { await vi.advanceTimersByTimeAsync(400) })
    fromFrame(el, 'open')
    scrollChat(a)
    fireEvent.pointerMove(other, { clientX: 110, clientY: 58 })
    expect(types(sent)).not.toContain('leave')
    fireEvent.pointerMove(other, { clientX: 140, clientY: 90 })
    expect(sent.at(-1)).toMatchObject({ type: 'leave' })
  })

  it('after such a scroll, many sub-pixel moves outside the link add up to a leave', async () => {
    await mount()
    const a = link()
    const other = document.createElement('div')
    document.body.append(other)
    fireEvent.pointerMove(a, { clientX: 110, clientY: 58 })
    const { el, sent } = await hoverReady(a)
    fromFrame(el, 'open')
    scrollChat(a)
    // A slow move at high zoom: each step is half a CSS pixel.
    fireEvent.pointerMove(other, { clientX: 110.5, clientY: 58 })
    expect(types(sent)).not.toContain('leave')
    fireEvent.pointerMove(other, { clientX: 111, clientY: 58 })
    expect(sent.at(-1)).toMatchObject({ type: 'leave' })
  })

  it('after such a scroll, a link slid under the still pointer is no hover, and moving onto it leaves the kept card', async () => {
    await mount()
    const a = link()
    fireEvent.pointerMove(a, { clientX: 110, clientY: 58 })
    const { el, sent } = await hoverReady(a)
    fromFrame(el, 'open')
    scrollChat(a)
    const b = link('https://tickets.example.com/t/TK-2')
    // The scroll slides b under the pointer, which has not moved.
    fireEvent.pointerOver(b, { clientX: 110, clientY: 58 })
    await act(async () => { await vi.advanceTimersByTimeAsync(400) })
    expect(types(sent)).not.toContain('leave')
    expect(sent.filter((m) => m.type === 'show')).toHaveLength(1)
    expect(el.style.visibility).toBe('visible')
    // A real move onto b: the kept card gets its leave, and b's hover starts.
    fireEvent.pointerOver(b, { clientX: 112, clientY: 60 })
    expect(sent.at(-1)).toMatchObject({ type: 'leave' })
    await act(async () => { await vi.advanceTimersByTimeAsync(400) })
    expect(sent.filter((m) => m.type === 'show')).toHaveLength(2)
  })

  it('a show still in its hover delay is cancelled by a scroll that moves its link', async () => {
    await mount()
    const a = link()
    const { el, sent } = await hoverReady(a)
    fromFrame(el, 'open')
    const b = link('https://tickets.example.com/t/TK-2')
    fireEvent.pointerOver(b) // the card for a is still shown; b's show is pending
    scrollChat(b)
    await act(async () => { await vi.advanceTimersByTimeAsync(400) })
    expect(sent.filter((m) => m.type === 'show')).toHaveLength(1)
    expect(sent.at(-1)).toMatchObject({ type: 'hide', reason: 'scroll' })
  })

  it('a card unpinned after a later hover still goes on a scroll that leaves the later link in place', async () => {
    await mount()
    const a = link()
    const { el: first } = await hoverReady(a)
    fromFrame(first, 'open')
    fromFrame(first, 'pin', { pinned: true })
    const b = link('https://tickets.example.com/t/TK-2')
    fireEvent.pointerOver(b)
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    const second = [...document.querySelectorAll<HTMLIFrameElement>('iframe[data-link-preview-frame]')].find((f) => f !== first)
    if (!second) throw new Error('no second frame')
    fromFrame(second, 'ready')
    await act(async () => { await vi.advanceTimersByTimeAsync(400) })
    fromFrame(first, 'pin', { pinned: false })
    fireEvent.scroll(window) // b stays put; the unpinned card from a's hover still goes
    expect(first.isConnected).toBe(false)
  })

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
    scrollChat(a)
    expect(el.isConnected).toBe(true)
    expect(el.style.visibility).toBe('visible')
    expect(types(sent)).not.toContain('hide')
    // Unpinned again: Escape takes it away.
    fromFrame(el, 'pin', { pinned: false })
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(sent.at(-1)).toMatchObject({ type: 'hide', reason: 'escape' })
  })

  it('an unpinned card hides on Escape and blur, never on scroll, and leave() reaches the frame', async () => {
    await mount()
    const a = link()
    const other = document.createElement('div')
    document.body.append(other)
    const { el, sent } = await hoverReady(a)
    fromFrame(el, 'open')
    fireEvent.pointerOut(a, { relatedTarget: other })
    expect(types(sent)).toContain('leave')
    scrollChat(a)
    expect(el.style.visibility).toBe('visible')
    expect(types(sent)).not.toContain('hide')
    fireEvent.blur(window)
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    expect(el.style.visibility).toBe('hidden')
    expect(sent.at(-1)).toMatchObject({ type: 'hide', reason: 'blur' })
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
    await act(async () => { await vi.advanceTimersByTimeAsync(mod.LEAVE_GRACE_MS) })
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
      await act(async () => { await vi.advanceTimersByTimeAsync(mod.LEAVE_GRACE_MS) })
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

  it('a failed load shows a card with its reason; Retry loads a fresh frame, and its success shows the normal card', async () => {
    const errors = vi.spyOn(console, 'error').mockImplementation(() => {})
    await mount()
    const a = link()
    fireEvent.pointerOver(a)
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    const first = theFrame()
    await act(async () => { fromFrame(first, 'failed', { message: 'activate threw' }) })
    const card = document.querySelector<HTMLElement>('[data-testid="link-preview-failed"]')
    expect(card).not.toBeNull()
    expect(card?.getAttribute('role')).toBe('group')
    expect(card?.getAttribute('aria-label')).toBeTruthy()
    expect(card?.dataset.cause).toBe('app')
    // The same surface as a card: placed below the link, at a card's width.
    expect(card?.className).toMatch(/rounded-lg/)
    expect(card?.className).toMatch(/bg-bg-elevated/)
    expect(card?.style.top).toBe(`${50 + 16 + 6}px`)
    expect(card?.style.width).toBe('360px')
    expect(card?.querySelector('[role="alert"]')?.textContent).toMatch(/error/i)
    // The pointer moves from the link into the card: the card stays.
    fireEvent.pointerOut(a, { relatedTarget: card })
    expect(document.querySelector('[data-testid="link-preview-failed"]')).not.toBeNull()

    const retry = document.querySelector<HTMLButtonElement>('[data-testid="link-preview-retry"]')
    if (!retry) throw new Error('no Retry')
    await act(async () => { fireEvent.click(retry) })
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    expect(mint).toHaveBeenCalledTimes(2) // inside RETRY_AFTER_MS: only Retry loads it again
    const second = theFrame()
    expect(second).not.toBe(first)
    expect(first.isConnected).toBe(false)
    expect(retry.disabled).toBe(true)
    const sent = watch(second)
    fromFrame(second, 'ready')
    await waitFor(() => expect(sent.find((m) => m.type === 'show')).toMatchObject({ url: 'https://tickets.example.com/t/TK-1' }))
    await waitFor(() => expect(document.querySelector('[data-testid="link-preview-failed"]')).toBeNull())
    fromFrame(second, 'open')
    expect(second.style.visibility).toBe('visible')
    // The hover ends as if the pointer had moved from the link into the app's
    // card: the app is told `leave`, and the same link can be hovered again.
    expect(types(sent).slice(types(sent).indexOf('show'))).toContain('leave')
    const shows = () => types(sent).filter((t) => t === 'show').length
    fireEvent.pointerOver(a)
    await act(async () => { await vi.advanceTimersByTimeAsync(400) })
    expect(shows()).toBe(2)
    errors.mockRestore()
  })

  it("a Retry that completes after the pointer moved to another link of the same app leaves that link's hover alone", async () => {
    const errors = vi.spyOn(console, 'error').mockImplementation(() => {})
    await mount()
    const a = link()
    fireEvent.pointerOver(a)
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    await act(async () => { fromFrame(theFrame(), 'failed', { message: 'boom' }) })
    const retry = document.querySelector<HTMLButtonElement>('[data-testid="link-preview-retry"]')
    if (!retry) throw new Error('no Retry')
    await act(async () => { fireEvent.click(retry) })
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    const second = theFrame()
    const sent = watch(second)
    fireEvent.pointerOver(link('https://tickets.example.com/t/TK-2'))
    fromFrame(second, 'ready') // A's Retry completes inside B's hover delay
    await act(async () => { await vi.advanceTimersByTimeAsync(400) })
    const shows = sent.filter((m) => m.type === 'show')
    expect(shows.at(-1)).toMatchObject({ url: 'https://tickets.example.com/t/TK-2' })
    errors.mockRestore()
  })

  it('a provider refresh closes the failure card, so a Retry never resends a link built from the old list', async () => {
    const errors = vi.spyOn(console, 'error').mockImplementation(() => {})
    const { qc } = await mount()
    fireEvent.pointerOver(link())
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    await act(async () => { fromFrame(theFrame(), 'failed', { message: 'boom' }) })
    const retry = document.querySelector<HTMLButtonElement>('[data-testid="link-preview-retry"]')
    if (!retry) throw new Error('no Retry')
    await act(async () => { fireEvent.click(retry) })
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    const second = theFrame()
    expect(retry.disabled).toBe(true)
    listApps.mockResolvedValue([APP, {
      name: 'other-app', enabled: true,
      manifest: { contributes: { linkPreviews: [{ id: 'cards', entry: 'dist/o.mjs', hosts: ['other.example.com'] }] } },
    }])
    await act(async () => { await qc.invalidateQueries({ queryKey: ['apps'] }) })
    await act(async () => { await vi.advanceTimersByTimeAsync(50) }) // the hover effect re-runs, dropping the pending show
    await act(async () => { fromFrame(second, 'ready') })
    await act(async () => { await vi.advanceTimersByTimeAsync(50) })
    expect(document.querySelector('[data-testid="link-preview-failed"]')).toBeNull()
    expect(sentTo(second).some((m) => m.type === 'show')).toBe(false)
    errors.mockRestore()
  })

  it("moving from a Retry's card onto another app's link drops that Retry's show", async () => {
    const errors = vi.spyOn(console, 'error').mockImplementation(() => {})
    listApps.mockResolvedValue([APP, {
      name: 'other-app', enabled: true,
      manifest: { contributes: { linkPreviews: [{ id: 'cards', entry: 'dist/o.mjs', hosts: ['other.example.com'] }] } },
    }])
    const { mod } = await mount()
    const a = link()
    const b = link('https://other.example.com/x')
    // B's provider fails first, so B is inside its cooldown later.
    fireEvent.pointerOver(b)
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    await act(async () => { fromFrame(theFrame(), 'failed', { message: 'boom' }) })
    fireEvent.pointerOut(b, { relatedTarget: document.body })
    await act(async () => { await vi.advanceTimersByTimeAsync(mod.LEAVE_GRACE_MS) })
    // A fails, Retry starts loading A's frame.
    fireEvent.pointerOver(a)
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    await act(async () => { fromFrame(theFrame(), 'failed', { message: 'boom' }) })
    const card = document.querySelector<HTMLElement>('[data-testid="link-preview-failed"]')
    const retry = document.querySelector<HTMLButtonElement>('[data-testid="link-preview-retry"]')
    if (!card || !retry) throw new Error('no card')
    fireEvent.pointerOut(a, { relatedTarget: card })
    await act(async () => { fireEvent.click(retry) })
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    const second = theFrame()
    const sent = watch(second)
    // Card -> B before A's Retry is ready.
    fireEvent.pointerLeave(card, { relatedTarget: b })
    fireEvent.pointerOver(b)
    fromFrame(second, 'ready')
    await act(async () => { await vi.advanceTimersByTimeAsync(400) })
    expect(sent.some((m) => m.type === 'show')).toBe(false)
    errors.mockRestore()
  })

  it('closing the failure card while a Retry loads drops that Retry: no card comes back', async () => {
    const errors = vi.spyOn(console, 'error').mockImplementation(() => {})
    await mount()
    fireEvent.pointerOver(link())
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    await act(async () => { fromFrame(theFrame(), 'failed', { message: 'boom' }) })
    const retry = document.querySelector<HTMLButtonElement>('[data-testid="link-preview-retry"]')
    if (!retry) throw new Error('no Retry')
    await act(async () => { fireEvent.click(retry) })
    const close = document.querySelector<HTMLButtonElement>('[data-testid="link-preview-failed"] button[aria-label]:not([data-testid])')
    if (!close) throw new Error('no close')
    await act(async () => { fireEvent.click(close) })
    expect(document.querySelector('[data-testid="link-preview-failed"]')).toBeNull()
    await act(async () => { await vi.advanceTimersByTimeAsync(READY_TIMEOUT_MS + 10) })
    expect(document.querySelector('[data-testid="link-preview-failed"]')).toBeNull()
    errors.mockRestore()
  })

  it('the failure card names the cause: a timeout, or an app with no frame', async () => {
    const errors = vi.spyOn(console, 'error').mockImplementation(() => {})
    await mount()
    const a = link()
    fireEvent.pointerOver(a)
    await act(async () => { await vi.advanceTimersByTimeAsync(READY_TIMEOUT_MS + 10) })
    expect(document.querySelector<HTMLElement>('[data-testid="link-preview-failed"]')?.dataset.cause).toBe('timeout')
    expect(document.querySelector('[data-testid="link-preview-failed"] [role="alert"]')?.textContent).toMatch(/too long/i)
    // Escape closes it.
    fireEvent.keyDown(document.querySelector<HTMLElement>('[data-testid="link-preview-retry"]') ?? document, { key: 'Escape' })
    expect(document.querySelector('[data-testid="link-preview-failed"]')).toBeNull()

    mint.mockRejectedValueOnce(new Error('HTTP 404'))
    const retryAfter = (await import('./LinkPreviewLayer')).RETRY_AFTER_MS
    await act(async () => { await vi.advanceTimersByTimeAsync(retryAfter) })
    const b = link('https://tickets.example.com/t/TK-2')
    fireEvent.pointerOver(b)
    await act(async () => { await vi.advanceTimersByTimeAsync(400) })
    expect(document.querySelector<HTMLElement>('[data-testid="link-preview-failed"]')?.dataset.cause).toBe('unavailable')
    errors.mockRestore()
  })

  it('the failure card stays while the pointer crosses the gap from the link into it', async () => {
    const errors = vi.spyOn(console, 'error').mockImplementation(() => {})
    const { mod } = await mount()
    const a = link()
    const gap = a.parentElement as HTMLElement
    fireEvent.pointerOver(a)
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    await act(async () => { fromFrame(theFrame(), 'failed', { message: 'boom' }) })
    const card = document.querySelector<HTMLElement>('[data-testid="link-preview-failed"]')
    if (!card) throw new Error('no card')
    // Link -> the chat around it (the gap) -> the card, slower than one sample.
    fireEvent.pointerOut(a, { relatedTarget: gap })
    await act(async () => { await vi.advanceTimersByTimeAsync(mod.LEAVE_GRACE_MS - 50) })
    fireEvent.pointerEnter(card, { relatedTarget: gap })
    await act(async () => { await vi.advanceTimersByTimeAsync(mod.LEAVE_GRACE_MS * 2) })
    expect(card.isConnected).toBe(true)
    // Leaving the card for somewhere else closes it once the grace has passed.
    fireEvent.pointerLeave(card, { relatedTarget: gap })
    expect(card.isConnected).toBe(true)
    await act(async () => { await vi.advanceTimersByTimeAsync(mod.LEAVE_GRACE_MS) })
    expect(document.querySelector('[data-testid="link-preview-failed"]')).toBeNull()
    errors.mockRestore()
  })

  it('with the app list unreadable, leaving the failure card lets the same link show it again', async () => {
    listApps.mockRejectedValue(new Error('HTTP 500'))
    const mod = await import('./LinkPreviewLayer')
    mod.setLinkPreviewMintForTest(mint)
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(createElement(QueryClientProvider, { client: qc }, createElement(mod.default)))
    await waitFor(() => expect(listApps).toHaveBeenCalledTimes(1))
    await act(async () => { await vi.advanceTimersByTimeAsync(50) })
    const a = link()
    const other = document.createElement('div')
    document.body.append(other)
    fireEvent.pointerOver(a)
    await act(async () => { await vi.advanceTimersByTimeAsync(400) })
    const card = document.querySelector<HTMLElement>('[data-testid="link-preview-failed"]')
    if (!card) throw new Error('no card')
    expect(card.dataset.cause).toBe('unavailable')
    fireEvent.pointerOut(a, { relatedTarget: card })
    fireEvent.pointerLeave(card, { relatedTarget: other })
    await act(async () => { await vi.advanceTimersByTimeAsync(mod.LEAVE_GRACE_MS) })
    expect(document.querySelector('[data-testid="link-preview-failed"]')).toBeNull()
    fireEvent.pointerOver(a)
    await act(async () => { await vi.advanceTimersByTimeAsync(400) })
    expect(document.querySelector('[data-testid="link-preview-failed"]')).not.toBeNull()
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
    const a = link()
    const { el, sent } = await hoverReady(a)
    fromFrame(el, 'regions', { rects: [{ left: 10, top: 20, width: 300, height: 100 }, { left: -50, top: 5, width: 100, height: 10 }] })
    expect([el.style.left, el.style.top, el.style.width, el.style.height]).toEqual(['0px', '0px', `${window.innerWidth}px`, `${window.innerHeight}px`])
    // Off-screen parts are cut to the viewport.
    expect(el.style.clipPath).toBe("path('M 10 20 h 300 v 100 h -300 Z M 0 5 h 50 v 10 h -50 Z')")
    expect(el.style.visibility).toBe('visible')
    fromFrame(el, 'drag', { dx: 30, dy: 30 })
    fromFrame(el, 'resize', { width: 10, height: 10 })
    expect(el.style.width).toBe(`${window.innerWidth}px`)
    // The chat scrolling tells the provider, which decides what goes; the frame stays.
    scrollChat(a)
    expect(sent.at(-1)).toMatchObject({ type: 'hide', reason: 'scroll' })
    expect(el.isConnected).toBe(true)
    fromFrame(el, 'regions', { rects: [] })
    expect(el.style.visibility).toBe('hidden')
    expect(el.style.pointerEvents).toBe('none')
  })

  it('a scroll that keeps another provider\'s shown card still tells the overlay', async () => {
    const DOCS = {
      name: 'docs-app',
      enabled: true,
      manifest: { contributes: { linkPreviews: [{ id: 'docs', entry: 'dist/lp.mjs', hosts: ['docs.example.com'] }] } },
    }
    listApps.mockResolvedValue([APP, DOCS])
    await mount()
    const a = link()
    const { el: overlay, sent: overlaySent } = await hoverReady(a)
    fromFrame(overlay, 'regions', { rects: [{ left: 10, top: 20, width: 300, height: 100 }] })
    const b = link('https://docs.example.com/d/1')
    fireEvent.pointerMove(b, { clientX: 110, clientY: 58 })
    fireEvent.pointerOver(b, { clientX: 112, clientY: 60 })
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    const card = [...document.querySelectorAll<HTMLIFrameElement>('iframe[data-link-preview-frame]')].find((f) => f !== overlay)
    if (!card) throw new Error('no second frame')
    const cardSent = watch(card)
    fromFrame(card, 'ready')
    await act(async () => { await vi.advanceTimersByTimeAsync(400) })
    fromFrame(card, 'open')
    expect(overlaySent.at(-1)).toMatchObject({ type: 'hide', reason: 'other-provider' })
    // The chat scrolls b away: b's card stays, and the overlay hears the scroll.
    scrollChat(b)
    expect(card.style.visibility).toBe('visible')
    expect(cardSent.some((m) => m.type === 'hide')).toBe(false)
    expect(overlaySent.at(-1)).toMatchObject({ type: 'hide', reason: 'scroll' })
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
