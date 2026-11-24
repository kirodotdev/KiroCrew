/**
 * The mark at a press of a live page: it stands at once, and what takes it away. The socket is the real link module;
 * the input route is a stub that answers what the gateway would; the clock is the test's.
 */
import type { WebGLRenderer } from 'three'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const input = vi.hoisted(() => vi.fn())
const ask = vi.hoisted(() => vi.fn())
vi.mock('../../../api/client', () => ({ api: { browserPanelInput: input, browserPanelAsk: ask } }))

import { applyXrPanelBinary, applyXrPanelFrame, setXrPanelSender } from '../../../hooks/xrPanelLink'
import { PRESS_MARK_MS, StreamSource } from './streamSource'

const renderer = { initTexture: vi.fn() } as unknown as WebGLRenderer

/** One binary frame as the gateway sends it; `round_ms` is the gateway's own round trip of the picture before. */
function picture(seq: number, roundMs: number): ArrayBuffer {
  const header = new TextEncoder().encode(JSON.stringify({ type: 'xr_panel_frame', panel: 'a', stream: 's1', seq, w: 2048, h: 1440, css: [1024, 720], mask: [], round_ms: roundMs }))
  const bytes = [0xff, 0xd8, 0xff, 0xe0]
  const out = new Uint8Array(4 + header.length + bytes.length)
  new DataView(out.buffer).setUint32(0, header.length, false)
  out.set(header, 4)
  out.set(bytes, 4 + header.length)
  return out.buffer
}

const decoded = () => vi.waitFor(() => expect((globalThis.createImageBitmap as ReturnType<typeof vi.fn>).mock.results.every(r => r.type === 'return')).toBe(true)).then(() => Promise.resolve())

let at = 1000
let seq = 900
/** One picture of the page lands in the texture now. */
async function lands(source: StreamSource, roundMs = 94): Promise<void> {
  seq += 1
  applyXrPanelBinary(picture(seq, roundMs))
  await decoded()
  source.beforeDraw?.(renderer, true)
}

async function shown(): Promise<StreamSource> {
  const source = new StreamSource('a', '/chat', 1024, 720)
  applyXrPanelFrame({ op: 'opened', id: 'a', handle: 'p1', css: [1024, 720] })
  applyXrPanelFrame({ op: 'status', id: 'a', status: 'live', why: '' })
  await lands(source)
  return source
}

const press = (source: StreamSource, x = 500, y = 300) => {
  source.pointer({ type: 'down', pointerId: 1, x, y })
  source.pointer({ type: 'up', pointerId: 1, x, y })
}

beforeEach(() => {
  at = 1000
  vi.spyOn(performance, 'now').mockImplementation(() => at)
  input.mockReset()
  input.mockResolvedValue({ ok: true, sent: 2, target: null })
  ask.mockReset()
  ask.mockResolvedValue({ ok: true, answer: null })
  vi.stubGlobal('createImageBitmap', vi.fn(async () => ({ width: 2048, height: 1440, close: vi.fn() })))
  setXrPanelSender(() => {})
})

afterEach(() => {
  setXrPanelSender(null)
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('the mark at a press of a live page', () => {
  it('stands at once where the press was sent, before any answer', async () => {
    const source = await shown()
    expect(source.pressMark()).toBeNull()
    press(source, 500, 300)
    // The same call: no answer of the gateway and no picture has come.
    expect(source.pressMark()).toMatchObject({ x: 500, y: 300 })
    source.dispose()
  })

  it('stays through a picture that was on its way before the press, and goes with the first picture a round after it', async () => {
    const source = await shown()
    at += 5000
    press(source)
    await Promise.resolve()
    // 30 ms after the press a picture lands: the gateway made it before the press reached it (its round is 94 ms).
    at += 30
    await lands(source, 94)
    expect(source.pressMark()).not.toBeNull()
    // 100 ms after the press: a picture the page can have drawn after the press. The mark is gone.
    at += 70
    await lands(source, 94)
    expect(source.pressMark()).toBeNull()
    source.dispose()
  })

  it('at a round of 300 ms waits for a picture 300 ms after the press', async () => {
    const source = await shown()
    await lands(source, 300)
    at += 5000
    press(source)
    await Promise.resolve()
    at += 250
    await lands(source, 300)
    expect(source.pressMark()).not.toBeNull()
    at += 60
    await lands(source, 300)
    expect(source.pressMark()).toBeNull()
    source.dispose()
  })

  it('never stands longer than 600 ms, whatever comes or does not', async () => {
    const source = await shown()
    at += 5000
    press(source)
    at += PRESS_MARK_MS - 1
    expect(source.pressMark()).not.toBeNull()
    at += 1
    expect(source.pressMark()).toBeNull()
    // And it does not come back.
    at += 1000
    expect(source.pressMark()).toBeNull()
    source.dispose()
  })

  it.each([
    ['a guarded control', { ok: true, sent: 0, guarded: true }],
    ['a control that moved', { ok: true, sent: 0, stale: true }],
    ['a picker that was open', { ok: true, sent: 0, picker: true }],
    ['a page that could not be asked', { ok: true, sent: 0, unread: true }],
    ['a send that was cut', { ok: true, sent: 1, lost: true }],
  ])('goes at the gateway\'s answer when the press was not sent: %s', async (_name, answer) => {
    const source = await shown()
    input.mockResolvedValue(answer)
    at += 5000
    press(source)
    expect(source.pressMark()).not.toBeNull()
    await vi.waitFor(() => expect(source.pressMark()).toBeNull())
    source.dispose()
  })

  it('goes when the request fails', async () => {
    const source = await shown()
    input.mockRejectedValue(new Error('no gateway'))
    at += 5000
    press(source)
    await vi.waitFor(() => expect(source.pressMark()).toBeNull())
    source.dispose()
  })

  it('never stands for a press that is not sent: a page with no picture, and a drag', async () => {
    const source = new StreamSource('a', '/chat', 1024, 720)
    applyXrPanelFrame({ op: 'opened', id: 'a', handle: 'p1', css: [1024, 720] })
    applyXrPanelFrame({ op: 'status', id: 'a', status: 'live', why: '' })
    // No picture has landed: the press goes nowhere.
    press(source)
    expect(input).not.toHaveBeenCalled()
    expect(source.pressMark()).toBeNull()
    await lands(source)
    // A press that is cancelled sends nothing.
    source.pointer({ type: 'down', pointerId: 1, x: 500, y: 300 })
    source.pointer({ type: 'cancel', pointerId: 1, x: 500, y: 300 })
    expect(source.pressMark()).toBeNull()
    source.dispose()
  })

  it('shows none on a control that his aim already showed as guarded; a plain control at the same place gets one', async () => {
    const source = await shown()
    at += 5000
    // The move to the place is answered with what stands there: a control that grants an agent permission.
    input.mockResolvedValueOnce({ ok: true, sent: 1, target: { rect: [480, 280, 40, 40], guarded: true } })
    source.pointer({ type: 'move', pointerId: 1, x: 500, y: 300 })
    await vi.waitFor(() => expect(source.target()?.guarded).toBe(true))
    press(source, 500, 300)
    // No ring, in the very call of the press: the gateway's refusal has not come.
    expect(source.pressMark()).toBeNull()
    // The same place under a plain control: the ring stands.
    input.mockResolvedValueOnce({ ok: true, sent: 1, target: { rect: [480, 280, 40, 40] } })
    source.pointer({ type: 'move', pointerId: 1, x: 501, y: 300 })
    await vi.waitFor(() => expect(source.target()?.guarded).toBeFalsy())
    await vi.waitFor(() => expect(source.target()).not.toBeNull())
    press(source, 501, 300)
    expect(source.pressMark()).toMatchObject({ x: 501, y: 300 })
    source.dispose()
  })

  it('stays when the last picture is only decoded again for a layer, a round after the press', async () => {
    const source = await shown()
    at += 5000
    press(source)
    await Promise.resolve()
    // A round and more after the press no new picture came; the panel goes onto a layer, and the last picture is decoded again for it.
    at += 150
    const offered: { done?: () => void; flipped: boolean; width: number; height: number }[] = []
    const layer = { width: 2048, height: 1440, offer: (p: (typeof offered)[number]) => { offered.push(p); return true }, frame: () => { const p = offered.shift(); p?.done?.(); return !!p } }
    source.toLayer?.(layer as never)
    await decoded()
    source.beforeDraw?.(renderer, true)
    layer.frame()
    expect(source.pressMark()).not.toBeNull()
    // A new picture then takes it away.
    await lands(source, 94)
    layer.frame()
    expect(source.pressMark()).toBeNull()
    source.dispose()
  })

  it('a second press moves the mark to the new place, and the first press\'s refusal does not take the new one away', async () => {
    const source = await shown()
    let refuse: (value: unknown) => void = () => {}
    input.mockImplementationOnce(() => new Promise(resolve => { refuse = resolve }))
    at += 5000
    press(source, 100, 100)
    at += 50
    press(source, 700, 400)
    expect(source.pressMark()).toMatchObject({ x: 700, y: 400 })
    refuse({ ok: true, sent: 0, guarded: true })
    await Promise.resolve()
    await Promise.resolve()
    expect(source.pressMark()).toMatchObject({ x: 700, y: 400 })
    source.dispose()
  })
})
