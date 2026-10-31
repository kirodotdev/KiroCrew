import { describe, it, expect } from 'vitest'

import {
  decodeBrowserFrame, encodeBrowserFrame, FrameRing, MAX_HEADER_BYTES,
  type DecodedBrowserFrame,
} from '../hooks/useBrowserLive.frame'

/** A few bytes that stand in for a JPEG; the decoder never looks inside. */
const JPEG = new Uint8Array([0xff, 0xd8, 0xff, 0xe0, 1, 2, 3, 0xff, 0xd9])

const header = (over: Partial<{ session_key: string; seq: number; w: number; h: number; mask: [number, number, number, number][] }> = {}) =>
  ({ session_key: 'sess-1', seq: 7, w: 1280, h: 800, ...over })

/** Encode with an arbitrary header object, so the tests can send malformed ones. */
function rawFrame(headerObj: unknown, jpeg: Uint8Array = JPEG): ArrayBuffer {
  const json = new TextEncoder().encode(JSON.stringify(headerObj))
  const out = new Uint8Array(4 + json.byteLength + jpeg.byteLength)
  new DataView(out.buffer).setUint32(0, json.byteLength, false)
  out.set(json, 4)
  out.set(jpeg, 4 + json.byteLength)
  return out.buffer
}

describe('decodeBrowserFrame', () => {
  it('round-trips the relay encoding: header fields, mask and the JPEG bytes', () => {
    const buf = encodeBrowserFrame(header({ mask: [[10, 20, 100, 30]] }), JPEG)
    const f = decodeBrowserFrame(buf, 1234)
    expect(f).not.toBeNull()
    expect(f!.header).toEqual({ type: 'browser_frame', session_key: 'sess-1', stream: '', seq: 7, w: 1280, h: 800, mask: [[10, 20, 100, 30]] })
    expect(Array.from(f!.jpeg)).toEqual(Array.from(JPEG))
    expect(f!.receivedAt).toBe(1234)
  })

  it('reads the header length as big-endian', () => {
    const buf = encodeBrowserFrame(header(), JPEG)
    const len = new DataView(buf).getUint32(0, false)
    expect(len).toBe(new TextEncoder().encode(JSON.stringify({ type: 'browser_frame', ...header(), stream: '', mask: [] })).byteLength)
  })

  it('treats a missing mask as no mask', () => {
    const f = decodeBrowserFrame(rawFrame({ type: 'browser_frame', ...header() }))
    expect(f!.header.mask).toEqual([])
  })

  it.each<[string, ArrayBuffer]>([
    ['too short to hold a length prefix', new Uint8Array([0, 0]).buffer],
    ['header length past the end', (() => { const b = new Uint8Array(12); new DataView(b.buffer).setUint32(0, 500, false); return b.buffer })()],
    ['header length zero', (() => { const b = new Uint8Array(12); return b.buffer })()],
    ['header length above the cap', (() => { const b = new Uint8Array(16); new DataView(b.buffer).setUint32(0, MAX_HEADER_BYTES + 1, false); return b.buffer })()],
    ['header is not JSON', (() => { const j = new TextEncoder().encode('{nope'); const b = new Uint8Array(4 + j.length + 3); new DataView(b.buffer).setUint32(0, j.length, false); b.set(j, 4); return b.buffer })()],
    ['wrong type', rawFrame({ type: 'computer_use_frame', ...header() })],
    ['no session key', rawFrame({ type: 'browser_frame', ...header({ session_key: '' }) })],
    ['negative seq', rawFrame({ type: 'browser_frame', ...header({ seq: -1 }) })],
    ['fractional seq', rawFrame({ type: 'browser_frame', ...header({ seq: 1.5 }) })],
    ['zero width', rawFrame({ type: 'browser_frame', ...header({ w: 0 }) })],
    ['absurd height', rawFrame({ type: 'browser_frame', ...header({ h: 100_000 }) })],
    ['mask rect with three numbers', rawFrame({ type: 'browser_frame', ...header(), mask: [[1, 2, 3]] })],
    ['mask rect outside the frame', rawFrame({ type: 'browser_frame', ...header(), mask: [[2000, 0, 1, 1]] })],
    ['mask that is not an array', rawFrame({ type: 'browser_frame', ...header(), mask: 'none' })],
    ['no JPEG bytes after the header', rawFrame({ type: 'browser_frame', ...header() }, new Uint8Array(0))],
  ])('drops a frame whose %s', (_label, buf) => {
    expect(decodeBrowserFrame(buf)).toBeNull()
  })

  it('refuses a header that is not valid UTF-8 rather than guessing', () => {
    const bad = new Uint8Array([0xff, 0xfe, 0x7b, 0x7d])
    const b = new Uint8Array(4 + bad.length + 2)
    new DataView(b.buffer).setUint32(0, bad.length, false)
    b.set(bad, 4)
    expect(decodeBrowserFrame(b.buffer)).toBeNull()
  })
})

describe('FrameRing', () => {
  const frame = (seq: number, bytes = 10): DecodedBrowserFrame => ({
    header: { type: 'browser_frame', session_key: 's', seq, w: 10, h: 10, mask: [] },
    jpeg: new Uint8Array(bytes),
    receivedAt: seq,
  })

  it('keeps the newest frames up to the count cap', () => {
    const ring = new FrameRing(3, 10_000)
    for (let i = 0; i < 5; i++) ring.push(frame(i))
    expect(ring.all().map(f => f.header.seq)).toEqual([2, 3, 4])
    expect(ring.last?.header.seq).toBe(4)
    expect(ring.length).toBe(3)
  })

  it('evicts by bytes too, but always keeps the newest frame', () => {
    const ring = new FrameRing(100, 25)
    ring.push(frame(1, 10))
    ring.push(frame(2, 10))
    ring.push(frame(3, 10))
    expect(ring.all().map(f => f.header.seq)).toEqual([2, 3])
    expect(ring.byteLength).toBe(20)
    ring.push(frame(4, 1000))
    expect(ring.all().map(f => f.header.seq)).toEqual([4])
  })

  it('all() is a snapshot: a later push does not change it', () => {
    const ring = new FrameRing()
    ring.push(frame(1))
    const snap = ring.all()
    ring.push(frame(2))
    expect(snap.length).toBe(1)
  })
})

describe('the stream id on the header', () => {
  it('is carried through, and a header without one reads as the empty stream', () => {
    const buf = encodeBrowserFrame({ ...header(), stream: 'a1b2c3d4' }, JPEG)
    expect(decodeBrowserFrame(buf)!.header.stream).toBe('a1b2c3d4')
    expect(decodeBrowserFrame(rawFrame({ type: 'browser_frame', ...header() }))!.header.stream).toBe('')
  })

  it('refuses a stream id that is not a short string', () => {
    expect(decodeBrowserFrame(rawFrame({ type: 'browser_frame', ...header(), stream: 7 }))).toBeNull()
    expect(decodeBrowserFrame(rawFrame({ type: 'browser_frame', ...header(), stream: 'x'.repeat(65) }))).toBeNull()
  })
})

describe('FrameRing.clear', () => {
  it('drops every frame and the byte count', () => {
    const ring = new FrameRing(10, 1_000_000)
    ring.push(decodeBrowserFrame(encodeBrowserFrame(header(), JPEG))!)
    expect(ring.length).toBe(1)
    ring.clear()
    expect(ring.length).toBe(0)
    expect(ring.byteLength).toBe(0)
    expect(ring.last).toBeNull()
  })
})
