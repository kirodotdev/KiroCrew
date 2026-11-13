import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'
import { installFrameWindows, postFrom, sentTo } from '../test/LinkPreviewFramesTestUtils'
import { APP_FRAME_TAG, MAX_HOST_MESSAGE_BYTES } from '../appFrame/frameProtocol'
import { FRAME_SANDBOX } from '../appFrame/sandboxFrame'
import { COMPOSER_INVOKE, COMPOSER_READY_MS, ComposerActionHost, type ComposerActionHostOptions } from './composerActionHost'

beforeAll(() => installFrameWindows())

const minted = { frame: '/app-frame/snip/composerActions/insert/fffffffffffff/t', frameId: 'fffffffffffff', capability: 'cap', ttl: 600 }

function make(over: Partial<ComposerActionHostOptions> = {}) {
  const container = document.createElement('div')
  document.body.appendChild(container)
  const onEnd = vi.fn()
  const onText = vi.fn((): void | 'not_allowed' => undefined)
  const host = new ComposerActionHost({
    appName: 'snip', actionId: 'insert', container, theme: () => 'light', deadlineMs: 1000,
    onText, onEnd, mint: async () => minted, ...over,
  })
  return { host, onEnd, onText }
}

const from = (host: ComposerActionHost, data: Record<string, unknown>) => postFrom({ [APP_FRAME_TAG]: 1, ...data }, host.el.contentWindow)
const sent = (host: ComposerActionHost) => sentTo(host.el)
const invokeOf = (host: ComposerActionHost) => sent(host).find((m) => m.type === COMPOSER_INVOKE) as Record<string, unknown>

async function open(host: ComposerActionHost) {
  host.start()
  // The test DOM fires the frame's first load itself.
  await vi.waitFor(() => expect(sent(host).some((m) => m.type === 'init')).toBe(true))
  from(host, { type: 'ready' })
  return invokeOf(host).invocationId as string
}
const offer = (host: ComposerActionHost, invocationId: string, seq: number, value: string, kind = 'insert') =>
  from(host, { type: 'result.offer', invocationId, seq, kind, value })

beforeEach(() => vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'queueMicrotask'] }))
afterEach(() => { vi.useRealTimers(); document.body.replaceChildren() })

describe('ComposerActionHost', () => {
  it('is a sandboxed frame of the minted document that is handed nothing from the composer', async () => {
    const { host } = make()
    const id = await open(host)
    expect(host.el.getAttribute('sandbox')).toBe(FRAME_SANDBOX)
    expect(host.el.getAttribute('src')).toBe(minted.frame)
    const invoke = invokeOf(host)
    expect(Object.keys(invoke).sort()).toEqual([APP_FRAME_TAG, 'deadlineMs', 'invocationId', 'type'].sort())
    expect(typeof id).toBe('string')
  })

  it('takes one insert, acks it and removes the frame', async () => {
    const { host, onText } = make()
    const id = await open(host)
    offer(host, id, 1, 'hi\r\nyo')
    expect(onText).toHaveBeenCalledWith('hi\nyo')
    expect(sent(host).find((m) => m.type === 'result.ack')).toMatchObject({ invocationId: id, seq: 1 })
    await vi.runAllTimersAsync()
    expect(host.el.isConnected).toBe(false)
    expect(sent(host).some((m) => m.type === 'invoke.cancel')).toBe(false)
  })

  it('refuses a second insert from the same click', async () => {
    const { host, onText } = make()
    const id = await open(host)
    offer(host, id, 1, 'one')
    offer(host, id, 2, 'two')
    expect(onText).toHaveBeenCalledTimes(1)
    expect(sent(host).at(-1)).toMatchObject({ type: 'result.error', seq: 2, code: 'stale' })
  })

  it.each([
    ['another kind', 'copy', 'x', 'not_allowed'],
    ['over the host cap', 'insert', 'x'.repeat(32_001), 'too_large'],
    ['empty once made safe', 'insert', '\u202e\u0007', 'not_allowed'],
  ])('refuses an offer of %s', async (_n, kind, value, code) => {
    const { host, onText } = make()
    const id = await open(host)
    offer(host, id, 1, value, kind)
    expect(onText).not.toHaveBeenCalled()
    expect(sent(host).at(-1)).toMatchObject({ type: 'result.error', code })
  })

  it('passes on the point refusing the text', async () => {
    const { host } = make({ onText: () => 'not_allowed' })
    const id = await open(host)
    offer(host, id, 1, 'x')
    expect(sent(host).at(-1)).toMatchObject({ type: 'result.error', code: 'not_allowed' })
  })

  it('strips bidi overrides and controls before handing the text on', async () => {
    const { host, onText } = make()
    const id = await open(host)
    offer(host, id, 1, 'a\u202eb\u0007c')
    expect(onText).toHaveBeenCalledWith('abc')
  })

  it('drops text that draws nothing, so the person sends only what they saw', async () => {
    const { host, onText } = make()
    const id = await open(host)
    // Tag characters spelling "hi", zero-width space and joiner, BOM, a variation selector.
    offer(host, id, 1, 'Sum\u{E0068}\u{E0069}mar\u200Bize\u200D\uFEFF\uFE0F')
    expect(onText).toHaveBeenCalledWith('Summarize')
  })

  it('refuses an offer that is only text that draws nothing', async () => {
    const { host, onText } = make()
    const id = await open(host)
    offer(host, id, 1, '\u200B\u{E0041}')
    expect(onText).not.toHaveBeenCalled()
  })

  it('fails a frame that never says ready', () => {
    const { host, onEnd } = make()
    host.start()
    vi.advanceTimersByTime(COMPOSER_READY_MS + 1)
    expect(onEnd).toHaveBeenCalledWith('failed', 'ready')
  })

  it('ends at the deadline and sends cancel', async () => {
    const { host, onEnd } = make()
    await open(host)
    vi.advanceTimersByTime(1001)
    expect(sent(host).some((m) => m.type === 'invoke.cancel')).toBe(true)
    expect(onEnd).toHaveBeenCalledWith('deadline', undefined)
  })

  it('tears down on an oversized message, a flood or a self-navigation', async () => {
    const big = make()
    await open(big.host)
    from(big.host, { type: 'invocation.resize', pad: 'x'.repeat(MAX_HOST_MESSAGE_BYTES) })
    expect(big.onEnd).toHaveBeenCalledWith('failed', 'too_large')
    const flood = make()
    await open(flood.host)
    for (let i = 0; i < 200; i++) from(flood.host, { type: 'escape' })
    expect(flood.onEnd).toHaveBeenCalledWith('failed', 'rate')
    const nav = make()
    await open(nav.host)
    nav.host.el.dispatchEvent(new Event('load'))
    expect(nav.onEnd).toHaveBeenCalledWith('failed', 'navigated')
  })

  it('cancels on the person closing it', async () => {
    const { host, onEnd } = make()
    const id = await open(host)
    host.cancel()
    expect(sent(host).find((m) => m.type === 'invoke.cancel')).toMatchObject({ invocationId: id, reason: 'cancelled' })
    expect(onEnd).toHaveBeenCalledWith('cancelled', undefined)
    expect(host.el.isConnected).toBe(false)
  })

  it('ignores messages from another window', async () => {
    const { host, onText } = make()
    const id = await open(host)
    postFrom({ [APP_FRAME_TAG]: 1, type: 'result.offer', invocationId: id, seq: 1, kind: 'insert', value: 'x' }, window)
    expect(onText).not.toHaveBeenCalled()
  })
})
