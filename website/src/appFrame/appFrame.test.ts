/**
 * The shared app-frame contract on the dashboard side: the frame's attributes, the
 * mint, the invocation reader and the host invocation state machine.
 */
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest'
import { installFrameWindows, sentTo } from '../test/LinkPreviewFramesTestUtils'
import {
  APP_FRAME_TAG,
  MAX_OFFER_CHARS,
  displaySafe,
  offerRefusal,
  readInvocationMessage,
} from './frameProtocol'
import { HostInvocation } from './invocation'
import { FRAME_SANDBOX, createSandboxedFrame, mintAppFrame } from './sandboxFrame'

const t = { [APP_FRAME_TAG]: 1 }

describe('createSandboxedFrame', () => {
  it('grants scripts only, sends no credentials or referrer, and allows no features', () => {
    const el = createSandboxedFrame('Notes')
    expect(el.getAttribute('sandbox')).toBe(FRAME_SANDBOX)
    expect(FRAME_SANDBOX).toBe('allow-scripts')
    expect(el.hasAttribute('credentialless')).toBe(true)
    expect(el.getAttribute('referrerpolicy')).toBe('no-referrer')
    expect(el.getAttribute('allow')).toBe('')
  })
})

describe('mintAppFrame', () => {
  afterEach(() => { vi.unstubAllGlobals() })
  it('posts to the (point, id) mint and accepts only an /app-frame/ URL', async () => {
    const fetch = vi.fn(async () => new Response(JSON.stringify({ frame: '/app-frame/a/linkPreviews/c/f/t', frameId: 'f', capability: 'c', ttl: 900 })))
    vi.stubGlobal('fetch', fetch)
    const minted = await mintAppFrame('a', 'linkPreviews', 'c', 'fid')
    expect(fetch.mock.calls[0][0]).toBe('/api/apps/a/frames/linkPreviews/c?frame=fid')
    expect(minted).toEqual({ frame: '/app-frame/a/linkPreviews/c/f/t', frameId: 'f', capability: 'c', ttl: 900 })
    fetch.mockResolvedValueOnce(new Response(JSON.stringify({ frame: 'https://evil.example/x' })))
    await expect(mintAppFrame('a', 'linkPreviews', 'c')).rejects.toThrow()
    await expect(mintAppFrame('a', 'link-previews', 'c')).rejects.toThrow()
    await expect(mintAppFrame('a', 'linkPreviews', '../x')).rejects.toThrow()
  })
})

describe('invocation messages', () => {
  it('reads only tagged, known messages and normalizes their fields', () => {
    expect(readInvocationMessage({ type: 'ready' })).toBeNull()
    expect(readInvocationMessage({ ...t, type: 'show' })).toBeNull()
    expect(readInvocationMessage({ ...t, type: 'result.offer', invocationId: 'i', seq: 0, kind: 'paste', value: 3 }))
      .toEqual({ type: 'result.offer', invocationId: 'i', seq: -1, kind: null, value: '' })
    expect(readInvocationMessage({ ...t, type: 'invocation.resize', invocationId: 'i', width: 'x', height: Infinity }))
      .toEqual({ type: 'invocation.resize', invocationId: 'i', width: 0, height: 0 })
  })
  it('allows only bounded copies and https opens', () => {
    expect(offerRefusal('copy', 'x'.repeat(MAX_OFFER_CHARS))).toBeNull()
    expect(offerRefusal('copy', 'x'.repeat(MAX_OFFER_CHARS + 1))).toBe('too_large')
    expect(offerRefusal('open', 'https://notes.example.com/1')).toBeNull()
    for (const bad of ['http://notes.example.com', 'javascript:alert(1)', 'https://u:p@notes.example.com', 'nope']) {
      expect(offerRefusal('open', bad)).toBe('not_allowed')
    }
    expect(offerRefusal(null, 'x')).toBe('not_allowed')
  })
  it('strips controls and bidi overrides from text the host shows', () => {
    expect(displaySafe('a\u202eb\u0007c\td\ne')).toBe('abc\td\ne')
  })
})

describe('HostInvocation', () => {
  beforeAll(() => installFrameWindows())
  afterEach(() => { vi.useRealTimers() })

  const make = (cb: Partial<ConstructorParameters<typeof HostInvocation>[3]> = {}) => {
    const el = document.createElement('iframe')
    const onOffer = vi.fn()
    const inv = new HostInvocation(el, 'test.invoke', 1000, { onOffer, ...cb })
    return { el, inv, onOffer }
  }
  const offer = (inv: HostInvocation, seq: number, kind: 'copy' | 'open' | null, value: string, id = inv.id) =>
    inv.handle({ type: 'result.offer', invocationId: id, seq, kind, value })

  it('sends the point invoke with a host-minted id and the deadline', () => {
    const { el, inv } = make()
    inv.start({ text: 'hi', invocationId: 'forged', type: 'other' })
    expect(sentTo(el)).toEqual([expect.objectContaining({ type: 'test.invoke', invocationId: inv.id, deadlineMs: 1000, text: 'hi' })])
    expect(inv.id).not.toBe('forged')
  })

  it('acks a current offer once per rising seq and refuses everything else', () => {
    const { el, inv, onOffer } = make()
    offer(inv, 1, 'copy', 'early')
    expect(sentTo(el).at(-1)).toEqual(expect.objectContaining({ type: 'result.error', code: 'stale' }))
    inv.start({})
    offer(inv, 1, 'copy', 'one')
    expect(sentTo(el).at(-1)).toEqual(expect.objectContaining({ type: 'result.ack', invocationId: inv.id, seq: 1 }))
    offer(inv, 1, 'copy', 'again')
    offer(inv, 2, 'copy', 'x', 'someone-else')
    expect(sentTo(el).slice(-2).map((m) => m.code)).toEqual(['stale', 'stale'])
    offer(inv, 3, 'open', 'javascript:alert(1)')
    expect(sentTo(el).at(-1)).toEqual(expect.objectContaining({ code: 'not_allowed' }))
    offer(inv, 4, 'copy', 'x'.repeat(MAX_OFFER_CHARS + 1))
    expect(sentTo(el).at(-1)).toEqual(expect.objectContaining({ code: 'too_large' }))
    expect(onOffer.mock.calls).toEqual([['copy', 'one']])
  })

  it('passes resize, close and error for the current invocation only', () => {
    const onResize = vi.fn()
    const onClose = vi.fn()
    const onError = vi.fn()
    const { inv } = make({ onResize, onClose, onError })
    inv.start({})
    inv.handle({ type: 'invocation.resize', invocationId: 'other', width: 1, height: 2 })
    inv.handle({ type: 'invocation.close', invocationId: 'other' })
    expect(onResize).not.toHaveBeenCalled()
    expect(onClose).not.toHaveBeenCalled()
    inv.handle({ type: 'invocation.resize', invocationId: inv.id, width: 1, height: 2 })
    inv.handle({ type: 'invocation.close', invocationId: inv.id })
    expect(onResize).toHaveBeenCalledWith(1, 2)
    expect(onClose).toHaveBeenCalledTimes(1)
    inv.handle({ type: 'invocation.error', invocationId: inv.id, message: 'boom' })
    expect(onError).toHaveBeenCalledWith('boom')
    expect(inv.state).toBe('done')
  })

  it('cancels at the deadline, which each resize or accepted offer extends', () => {
    vi.useFakeTimers()
    const onDeadline = vi.fn()
    const { el, inv } = make({ onDeadline })
    inv.start({})
    vi.advanceTimersByTime(900)
    inv.handle({ type: 'invocation.resize', invocationId: inv.id, width: 1, height: 1 })
    vi.advanceTimersByTime(900)
    offer(inv, 1, 'copy', 'x')
    vi.advanceTimersByTime(900)
    expect(onDeadline).not.toHaveBeenCalled()
    vi.advanceTimersByTime(200)
    expect(onDeadline).toHaveBeenCalledTimes(1)
    expect(sentTo(el).at(-1)).toEqual({ [APP_FRAME_TAG]: 1, type: 'invoke.cancel', invocationId: inv.id, reason: 'deadline' })
  })

  it('sends invoke.cancel once, and only for an open invocation', () => {
    const idle = make()
    idle.inv.cancel('closed')
    expect(sentTo(idle.el)).toEqual([])
    // A cancelled invocation never starts.
    idle.inv.start({})
    expect(sentTo(idle.el)).toEqual([])
    const { el, inv } = make()
    inv.start({})
    inv.cancel('closed')
    inv.cancel('closed')
    expect(sentTo(el).filter((m) => m.type === 'invoke.cancel')).toHaveLength(1)
  })
})
