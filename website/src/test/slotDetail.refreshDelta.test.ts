/** Turn-end and reconnect refreshes echo the slot-detail cursor (#36194).
 *
 *  The real `api` client and the real store run against a fake gateway that
 *  answers the slot-detail cursor protocol (`slot_detail_delta.py`): a request
 *  with `from/since/digest` gets only the rows from `since` on when the digest of
 *  `[from, since)` still matches, else the whole page. The fake counts the rows
 *  it puts on the wire, so "an unchanged refresh costs ~nothing" is measured.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { configureStore } from '@reduxjs/toolkit'

import { api } from '../api/client'
import chatReducer, { SLOT_DETAIL_MAX_LIMIT, refreshSlot, warmSlotCache } from '../store/chatSlice'

type Row = { role: string; content: string; ts: string; meta: { mid: string } }
const row = (n: number, content = `row ${n}`): Row => ({
  role: n % 2 ? 'assistant' : 'user',
  content,
  ts: new Date(Date.UTC(2026, 0, 1, 0, 0, n)).toISOString(),
  meta: { mid: `m${n}` },
})

/** Fake gateway for one slot: `rows` is the server transcript. */
function fakeGateway(initial: Row[]) {
  const server = { rows: initial, delta: true, reject: false }
  const wire = { requests: [] as string[], rowsSent: 0, notModified: 0 }
  const digest = (rows: Row[], from: number, since: number) =>
    // 32 hex chars, a function of position and content like the real digest
    Array.from(JSON.stringify([from, since, rows.slice(from, since)]))
      .reduce((h, c) => (Math.imul(h, 31) + c.charCodeAt(0)) >>> 0, 7)
      .toString(16).padStart(8, '0').repeat(4)
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    wire.requests.push(url)
    const q = new URL(url, 'http://x').searchParams
    const limit = Number(q.get('limit') ?? server.rows.length)
    if (q.has('before')) {
      const end = Number(q.get('before'))
      const start = Math.max(0, end - limit)
      const messages = server.rows.slice(start, end)
      wire.rowsSent += messages.length
      return new Response(JSON.stringify({ messages, running: false, queue: [], total: server.rows.length, has_more: start > 0, next_before: start }), { status: 200 })
    }
    const total = server.rows.length
    const newFrom = Math.max(0, total - limit)
    const echoed = q.has('digest')
    if (echoed && server.reject) return new Response('{"error":"bad cursor"}', { status: 400 })
    const since = Number(q.get('since'))
    const from = Number(q.get('from'))
    const applies = server.delta && echoed && from <= since && since <= total && newFrom >= from
      && digest(server.rows, from, since) === q.get('digest')
    const serveFrom = applies ? Math.max(since, newFrom) : newFrom
    const messages = server.rows.slice(serveFrom)
    const nextSince = Math.max(newFrom, total - 1)
    const body = {
      messages, running: false, queue: [], total, has_more: newFrom > 0, next_before: newFrom,
      ...(q.get('cursor') === '1' || echoed
        ? { cursor: { from: newFrom, since: nextSince, digest: digest(server.rows, newFrom, nextSince) } }
        : {}),
      ...(applies ? { delta: { from, since } } : {}),
    }
    const text = JSON.stringify(body)
    const etag = 'W/"' + digest([text as unknown as Row], 0, 1) + '"'
    if ((init?.headers as Record<string, string> | undefined)?.['If-None-Match'] === etag) {
      wire.notModified += 1
      return new Response(null, { status: 304, headers: { etag } })
    }
    wire.rowsSent += messages.length
    return new Response(text, { status: 200, headers: { 'content-type': 'application/json', etag } })
  }))
  return { server, wire }
}

const rows = (from: number, to: number) => Array.from({ length: to - from }, (_, i) => row(from + i))

function makeStore(slot: string) {
  const base = chatReducer(undefined, { type: '@@INIT' })
  return configureStore({
    reducer: { chat: chatReducer },
    preloadedState: { chat: { ...base, activeSlot: slot } },
  })
}
const mids = (store: ReturnType<typeof makeStore>) =>
  store.getState().chat.messages.map(m => m.meta?.mid)

describe('slot refresh sends the cursor', () => {
  let slot = ''
  let n = 0
  beforeEach(() => {
    slot = `delta-slot-${n++}` // the held page is per slot key; keep tests apart
  })

  it('an unchanged refresh puts one row on the wire, not the page', async () => {
    const { wire } = fakeGateway(rows(0, 200))
    const store = makeStore(slot)
    await store.dispatch(refreshSlot(slot) as never)
    expect(wire.rowsSent).toBeGreaterThanOrEqual(50) // the floor page
    const first = wire.rowsSent
    await store.dispatch(refreshSlot(slot) as never)
    expect(wire.rowsSent - first).toBe(1)
    expect(wire.requests[1]).toContain('digest=')
    expect(mids(store)).toEqual(rows(0, 200).slice(-store.getState().chat.messages.length).map(r => r.meta.mid))
  })

  it('a second unchanged refresh is a 304: no body at all', async () => {
    const { wire } = fakeGateway(rows(0, 200))
    const store = makeStore(slot)
    await store.dispatch(refreshSlot(slot) as never)
    await store.dispatch(refreshSlot(slot) as never)
    const sent = wire.rowsSent
    const shown = mids(store)
    await store.dispatch(refreshSlot(slot) as never)
    expect(wire.notModified).toBe(1)
    expect(wire.rowsSent).toBe(sent)
    expect(mids(store)).toEqual(shown)
  })

  it('a refresh whose limit grew by the rows it gained still gets a delta, at full width', async () => {
    const { server, wire } = fakeGateway(rows(0, 300))
    const first = await api.chatSlotDetail(slot, 100)
    expect(first.messages).toHaveLength(100)
    server.rows = [...server.rows, row(300), row(301)]
    const before = wire.rowsSent
    const next = await api.chatSlotDetail(slot, 102) // the count-matched limit grew
    expect(wire.rowsSent - before).toBe(3)
    expect(next.messages.map(m => m.meta?.mid).slice(-3)).toEqual(['m299', 'm300', 'm301'])
    expect(next.next_before).toBe(200)
    expect(next.messages).toHaveLength(102)
    expect('delta' in next && next.delta !== undefined).toBe(false)
  })

  it('a narrow probe is a plain read and leaves the held page alone', async () => {
    const { wire } = fakeGateway(rows(0, 300))
    await api.chatSlotDetail(slot, 100)
    await api.chatSlotDetail(slot, 1)
    expect(wire.requests[1]).not.toContain('cursor=')
    expect(wire.requests[1]).not.toContain('digest=')
    const before = wire.rowsSent
    await api.chatSlotDetail(slot, 100)
    expect(wire.requests[2]).toContain('digest=')
    expect(wire.rowsSent - before).toBe(1)
  })

  it('repairs a missed frame: rows added while away arrive in order, no gaps or duplicates', async () => {
    const { server, wire } = fakeGateway(rows(0, 120))
    const store = makeStore(slot)
    await store.dispatch(refreshSlot(slot) as never)
    server.rows = [...server.rows, ...rows(120, 127)] // frames the socket never delivered
    const before = wire.rowsSent
    await store.dispatch(refreshSlot(slot) as never)
    // the re-sent last row plus the 7 missed ones, not the page
    expect(wire.rowsSent - before).toBe(8)
    const got = mids(store)
    expect(got.slice(-8)).toEqual(['m119', 'm120', 'm121', 'm122', 'm123', 'm124', 'm125', 'm126'])
    expect(new Set(got).size).toBe(got.length)
    const ids = got.map(m => Number(String(m).slice(1)))
    expect(ids).toEqual([...ids].sort((a, b) => a - b))
    expect(ids.every((v, i) => i === 0 || v === ids[i - 1] + 1)).toBe(true)
  })

  it('repairs a gap wider than the held page: older pages are walked, nothing is skipped', async () => {
    const { server } = fakeGateway(rows(0, 120))
    const store = makeStore(slot)
    await store.dispatch(refreshSlot(slot) as never)
    server.rows = [...server.rows, ...rows(120, 200)] // more missed rows than the 50 held
    await store.dispatch(refreshSlot(slot) as never)
    const ids = mids(store).map(m => Number(String(m).slice(1)))
    expect(ids.at(-1)).toBe(199)
    expect(ids.every((v, i) => i === 0 || v === ids[i - 1] + 1)).toBe(true)
    expect(ids[0]).toBeLessThanOrEqual(70) // the rows held before the gap are still there
  })

  it('a row edited earlier in the span makes the server answer the whole page, and it shows', async () => {
    const { server, wire } = fakeGateway(rows(0, 150))
    const store = makeStore(slot)
    await store.dispatch(refreshSlot(slot) as never)
    server.rows = server.rows.map((r, i) => (i === 100 ? { ...r, content: 'edited' } : r))
    const before = wire.rowsSent
    await store.dispatch(refreshSlot(slot) as never)
    expect(wire.rowsSent - before).toBeGreaterThanOrEqual(50) // the whole page
    expect(store.getState().chat.messages.find(m => m.meta?.mid === 'm100')?.content).toBe('edited')
  })

  it('a row deleted inside the span shifts it, so the page is whole', async () => {
    const { server, wire } = fakeGateway(rows(0, 150))
    const store = makeStore(slot)
    await store.dispatch(refreshSlot(slot) as never)
    server.rows = server.rows.filter((_, i) => i !== 120)
    const before = wire.rowsSent
    await store.dispatch(refreshSlot(slot) as never)
    expect(wire.rowsSent - before).toBeGreaterThanOrEqual(50) // the whole page
    expect(mids(store)).not.toContain('m120')
  })

  it('a cursor the server rejects falls back to one whole read', async () => {
    const { server, wire } = fakeGateway(rows(0, 130))
    const store = makeStore(slot)
    await store.dispatch(refreshSlot(slot) as never)
    server.reject = true
    server.rows = [...server.rows, row(130)]
    const calls = wire.requests.length
    await store.dispatch(refreshSlot(slot) as never)
    expect(wire.requests.length - calls).toBe(2) // rejected delta, then the whole read
    expect(wire.requests.at(-1)).toContain('cursor=1')
    expect(wire.requests.at(-1)).not.toContain('digest=')
    expect(mids(store).at(-1)).toBe('m130')
  })

  it('a delta that does not answer the cursor sent is read whole', async () => {
    const { server, wire } = fakeGateway(rows(0, 130))
    const store = makeStore(slot)
    await store.dispatch(refreshSlot(slot) as never)
    const real = globalThis.fetch as unknown as (u: string) => Promise<Response>
    vi.stubGlobal('fetch', vi.fn(async (u: string) => {
      const res = await real(u)
      if (!u.includes('digest=')) return res
      const body = await res.json()
      return new Response(JSON.stringify({ ...body, delta: { from: 1, since: 2 } }), { status: 200 })
    }))
    server.rows = [...server.rows, row(130)]
    const calls = wire.requests.length
    await store.dispatch(refreshSlot(slot) as never)
    expect(wire.requests.length - calls).toBe(2)
    expect(mids(store).at(-1)).toBe('m130')
  })
})

describe('a held cursor never narrows a later read', () => {
  // A narrow first read (a one-row probe, a 30-row tail) must not cap the width
  // of a later, wider read of the same slot: each read returns what it asked for.
  for (const first of [1, 30]) {
    for (const later of [30, 80, 500]) {
      if (later <= first) continue
      it(`first read ${first} row(s), then ${later}: ${later} rows come back`, async () => {
        const { server } = fakeGateway(rows(0, 700))
        const slot = `narrow-${first}-${later}`
        expect((await api.chatSlotDetail(slot, first)).messages).toHaveLength(first)
        server.rows = [...server.rows, row(700)]
        const page = await api.chatSlotDetail(slot, later)
        expect(page.messages.map(m => m.meta?.mid)).toEqual(rows(0, 701).slice(-later).map(r => r.meta.mid))
        expect(page.next_before).toBe(701 - later)
      })
    }
  }

  it('a later read wider than the whole transcript returns every row', async () => {
    const { server } = fakeGateway(rows(0, 40))
    const slot = 'narrow-all'
    await api.chatSlotDetail(slot, 1)
    server.rows = [...server.rows, row(40)]
    const page = await api.chatSlotDetail(slot, 500)
    expect(page.messages).toHaveLength(41)
    expect(page.has_more).toBe(false)
  })
})

describe('warmSlotCache without a provable bound', () => {
  it('walks older pages and never issues an unbounded read', async () => {
    const total = 1300
    const all = rows(0, total)
    const urls: string[] = []
    vi.stubGlobal('fetch', vi.fn(async (url: string) => {
      urls.push(url)
      const q = new URL(url, 'http://x').searchParams
      const limit = Number(q.get('limit') ?? total) // an unbounded read would send no limit
      const end = q.has('before') ? Number(q.get('before')) : total
      const start = Math.max(0, end - limit)
      return new Response(JSON.stringify({
        messages: all.slice(start, end), running: false, queue: [], total, has_more: start > 0, next_before: start,
      }), { status: 200 })
    }))
    // A held row with no readable instant: the legacy shape the count-matched rule declines.
    const legacy = { ...row(1200), ts: '' }
    const base = chatReducer(undefined, { type: '@@INIT' })
    const store = configureStore({
      reducer: { chat: chatReducer },
      preloadedState: { chat: { ...base, activeSlot: 'other', slotMessages: { legacy: [legacy] } } },
    })
    await store.dispatch(warmSlotCache('legacy') as never)
    expect(urls.length).toBeGreaterThan(0)
    for (const u of urls) {
      const q = new URL(u, 'http://x').searchParams
      expect(Number(q.get('limit'))).toBeGreaterThan(0)
      expect(Number(q.get('limit'))).toBeLessThanOrEqual(SLOT_DETAIL_MAX_LIMIT)
    }
  })
})
