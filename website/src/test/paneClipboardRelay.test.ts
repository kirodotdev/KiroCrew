/**
 * Clipboard inside an embedded instance pane (#14854).
 *
 * The pane is a cross-origin iframe and the dashboard's header scopes the
 * clipboard to its own origin, so the async Clipboard API is refused there.
 * Two things must hold:
 *
 * - the pane never CALLS the refused API (each call is a logged
 *   Permissions-Policy violation), and text still lands via the fallback;
 * - an image, which has no fallback, is written by the host window on the
 *   pane's behalf -- only for a host that advertised the relay, addressed to
 *   that host's exact origin, and only when the host answers for this request.
 *
 * The host-side gate (sender frame, active pane, gesture inside the pane, via
 * useFrameOpenLink's `hasGesture`) is covered in InstancesViewport.test.tsx
 * through the real message listener.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../lib/embedded', () => ({ isEmbeddedPane: vi.fn(() => true) }))
import { isEmbeddedPane } from '../lib/embedded'
import {
  PANE_CLIPBOARD_IMAGE_TYPE,
  PANE_CLIPBOARD_MAX_PNG_BYTES,
  PANE_CLIPBOARD_RESULT_TYPE,
  PANE_CLIPBOARD_TIMEOUT_MS,
  PANE_CLIPBOARD_VERSION,
  clipboardRelayAvailable,
  clipboardWriteBlockedByPolicy,
  noteClipboardRelayHost,
  parsePaneClipboardImageRequest,
} from '../lib/paneClipboardRelay'
import { copyImageToClipboard, copyRichToClipboard, copyToClipboard, copyWithOutcome } from '../utils/clipboard'
import { copyImageWithText } from '../pages/chat/share/shareSupport'

const HOST = 'http://localhost:8080'
const png = () => new Blob([new Uint8Array([137, 80, 78, 71])], { type: 'image/png' })

type PolicyDoc = Document & { featurePolicy?: unknown; permissionsPolicy?: unknown }

/** Make this document's policy refuse (or allow) clipboard-write, the way an
 *  embedded pane's does. Uses Chromium's `featurePolicy` probe by default. */
function setPolicy(allowsClipboardWrite: boolean, key: 'featurePolicy' | 'permissionsPolicy' = 'featurePolicy') {
  Object.defineProperty(document as PolicyDoc, key, {
    configurable: true,
    value: { allowsFeature: (f: string) => (f === 'clipboard-write' ? allowsClipboardWrite : true) },
  })
}

function clearPolicy() {
  delete (document as PolicyDoc).featurePolicy
  delete (document as PolicyDoc).permissionsPolicy
}

/** Async clipboard stubs that record every call, so "never called" is checkable. */
function stubAsyncClipboard() {
  const parts: Array<Record<string, unknown>> = []
  class FakeClipboardItem {
    constructor(items: Record<string, unknown>) { parts.push(items) }
  }
  vi.stubGlobal('ClipboardItem', FakeClipboardItem)
  const writeText = vi.fn(async () => {})
  const write = vi.fn(async () => {})
  vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText, write } })
  return { parts, writeText, write }
}

/** Capture what the pane posts to its parent. */
function capturePosts() {
  return vi.spyOn(window.parent, 'postMessage').mockImplementation(() => {})
}

function answer(id: unknown, ok: boolean, over: Partial<MessageEventInit> = {}) {
  window.dispatchEvent(new MessageEvent('message', {
    source: window.parent,
    origin: HOST,
    data: { type: PANE_CLIPBOARD_RESULT_TYPE, v: PANE_CLIPBOARD_VERSION, id, ok },
    ...over,
  }))
}

beforeEach(() => {
  vi.mocked(isEmbeddedPane).mockReturnValue(true)
})

afterEach(() => {
  clearPolicy()
  noteClipboardRelayHost(null, false)
  vi.useRealTimers()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('clipboardWriteBlockedByPolicy', () => {
  it('is false where the engine exposes no policy probe', () => {
    clearPolicy()
    expect(clipboardWriteBlockedByPolicy()).toBe(false)
  })

  it('reads Chromium\'s featurePolicy probe', () => {
    setPolicy(false)
    expect(clipboardWriteBlockedByPolicy()).toBe(true)
    setPolicy(true)
    expect(clipboardWriteBlockedByPolicy()).toBe(false)
  })

  it('prefers the standard permissionsPolicy probe when both exist', () => {
    setPolicy(true, 'featurePolicy')
    setPolicy(false, 'permissionsPolicy')
    expect(clipboardWriteBlockedByPolicy()).toBe(true)
  })

  it('treats a throwing probe as not blocked, so the caller tries as before', () => {
    Object.defineProperty(document as PolicyDoc, 'featurePolicy', {
      configurable: true,
      value: { allowsFeature: () => { throw new Error('boom') } },
    })
    expect(clipboardWriteBlockedByPolicy()).toBe(false)
  })
})

describe('text copy in a policy-blocked pane', () => {
  it('never calls the refused writeText and still copies through the fallback', async () => {
    setPolicy(false)
    const { writeText } = stubAsyncClipboard()
    const exec = vi.fn(() => true)
    Object.defineProperty(document, 'execCommand', { configurable: true, value: exec })

    const outcome = await copyWithOutcome('ls -la')
    expect(writeText).not.toHaveBeenCalled()
    expect(exec).toHaveBeenCalledWith('copy')
    expect(outcome.ok).toBe(true)
    // Same reason a refused call would have produced, so a reason-reading
    // caller (the terminal's Copy key) reaches the same verdict as before.
    expect(outcome.hadAsyncApi).toBe(true)
    expect(outcome.asyncError).toBeInstanceOf(DOMException)
    expect((outcome.asyncError as DOMException).name).toBe('NotAllowedError')
  })

  it('reports false when the fallback cannot copy either', async () => {
    setPolicy(false)
    stubAsyncClipboard()
    Object.defineProperty(document, 'execCommand', { configurable: true, value: vi.fn(() => false) })
    await expect(copyToClipboard('x')).resolves.toBe(false)
  })

  it('skips the refused rich write and carries both flavours in the fallback', async () => {
    setPolicy(false)
    const { write } = stubAsyncClipboard()
    Object.defineProperty(document, 'execCommand', { configurable: true, value: vi.fn(() => true) })
    await expect(copyRichToClipboard('<b>x</b>', 'x')).resolves.toBe(true)
    expect(write).not.toHaveBeenCalled()
  })

  it('leaves the top-level path on the async API', async () => {
    setPolicy(true)
    const { writeText } = stubAsyncClipboard()
    await expect(copyToClipboard('top')).resolves.toBe(true)
    expect(writeText).toHaveBeenCalledWith('top')
  })
})

describe('image copy in a policy-blocked pane', () => {
  it('asks the advertised host to write it, at that host\'s exact origin', async () => {
    setPolicy(false)
    const { write } = stubAsyncClipboard()
    const post = capturePosts()
    noteClipboardRelayHost(HOST, true)
    expect(clipboardRelayAvailable()).toBe(true)

    const copying = copyImageToClipboard(Promise.resolve(png()))
    await vi.waitFor(() => expect(post).toHaveBeenCalledOnce())
    const [msg, target] = post.mock.calls[0] as unknown as [Record<string, unknown>, string]
    expect(target).toBe(HOST)
    expect(msg.type).toBe(PANE_CLIPBOARD_IMAGE_TYPE)
    expect(msg.v).toBe(PANE_CLIPBOARD_VERSION)
    expect((msg.png as Blob).type).toBe('image/png')
    expect(msg).not.toHaveProperty('text')

    answer(msg.id, true)
    await expect(copying).resolves.toBe(true)
    // The pane's own refused API is never touched.
    expect(write).not.toHaveBeenCalled()
  })

  it('reports the host\'s refusal as not copied', async () => {
    setPolicy(false)
    stubAsyncClipboard()
    const post = capturePosts()
    noteClipboardRelayHost(HOST, true)
    const copying = copyImageToClipboard(Promise.resolve(png()))
    await vi.waitFor(() => expect(post).toHaveBeenCalledOnce())
    answer((post.mock.calls[0][0] as { id: string }).id, false)
    await expect(copying).resolves.toBe(false)
  })

  it('ignores answers from another origin, another window or another request', async () => {
    vi.useFakeTimers()
    setPolicy(false)
    stubAsyncClipboard()
    const post = capturePosts()
    noteClipboardRelayHost(HOST, true)
    let result: boolean | undefined
    void copyImageToClipboard(Promise.resolve(png())).then(r => { result = r })
    await vi.waitFor(() => expect(post).toHaveBeenCalledOnce())
    const id = (post.mock.calls[0][0] as { id: string }).id

    answer(id, true, { origin: 'http://localhost:9999' })
    answer(id, true, { source: null })
    answer('someone-else', true)
    await vi.advanceTimersByTimeAsync(0)
    expect(result).toBeUndefined()

    // Nothing valid arrived: the pane gives up and reports "not copied".
    await vi.advanceTimersByTimeAsync(PANE_CLIPBOARD_TIMEOUT_MS)
    expect(result).toBe(false)
  })

  it('stays on today\'s behaviour for a host that never advertised the relay', async () => {
    setPolicy(false)
    const { write } = stubAsyncClipboard()
    const post = capturePosts()
    noteClipboardRelayHost(HOST, false)
    await expect(copyImageToClipboard(Promise.resolve(png()))).resolves.toBe(false)
    expect(post).not.toHaveBeenCalled()
    expect(write).not.toHaveBeenCalled()
  })

  it('never relays to a host origin that is not loopback http', () => {
    noteClipboardRelayHost('https://example.com', true)
    expect(clipboardRelayAvailable()).toBe(false)
    noteClipboardRelayHost('', true)
    expect(clipboardRelayAvailable()).toBe(false)
  })

  it('sends nothing for bytes that failed to load or are not PNG', async () => {
    setPolicy(false)
    stubAsyncClipboard()
    const post = capturePosts()
    noteClipboardRelayHost(HOST, true)
    await expect(copyImageToClipboard(Promise.reject(new Error('404')))).resolves.toBe(false)
    const jpeg = new Blob([new Uint8Array([255, 216])], { type: 'image/jpeg' })
    await expect(copyImageToClipboard(Promise.resolve(jpeg))).resolves.toBe(false)
    expect(post).not.toHaveBeenCalled()
  })

  it('carries the share card\'s caption beside the image', async () => {
    setPolicy(false)
    stubAsyncClipboard()
    const post = capturePosts()
    noteClipboardRelayHost(HOST, true)
    const copying = copyImageWithText(png(), 'look at this')
    await vi.waitFor(() => expect(post).toHaveBeenCalledOnce())
    const msg = post.mock.calls[0][0] as Record<string, unknown>
    expect(msg.text).toBe('look at this')
    answer(msg.id, true)
    await expect(copying).resolves.toBe(true)
  })
})

describe('host side', () => {
  const request = (over: Record<string, unknown> = {}) => ({
    type: PANE_CLIPBOARD_IMAGE_TYPE, v: PANE_CLIPBOARD_VERSION, id: 'clip-1', png: png(), ...over,
  })

  it('accepts exactly a well-formed request', () => {
    const req = parsePaneClipboardImageRequest(request({ text: 'cap' }))
    expect(req?.id).toBe('clip-1')
    expect(req?.text).toBe('cap')
    expect(parsePaneClipboardImageRequest(request())).not.toHaveProperty('text')
  })

  it('rejects anything malformed, oversized or not a PNG', () => {
    const huge = { type: 'image/png', size: PANE_CLIPBOARD_MAX_PNG_BYTES + 1 }
    Object.setPrototypeOf(huge, Blob.prototype)
    for (const bad of [
      null,
      'mc-clipboard-image',
      request({ type: 'mc-clipboard-text' }),
      request({ v: 2 }),
      request({ id: '' }),
      request({ id: 'x'.repeat(65) }),
      request({ id: 7 }),
      request({ png: 'iVBORw0KGgo=' }),
      request({ png: new Blob(['x'], { type: 'text/plain' }) }),
      request({ png: new Blob([], { type: 'image/png' }) }),
      request({ png: huge }),
      request({ text: 42 }),
      request({ text: 'x'.repeat(64 * 1024 + 1) }),
    ]) {
      expect(parsePaneClipboardImageRequest(bad)).toBeNull()
    }
  })
})
