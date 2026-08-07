import { readFileSync } from 'node:fs'
import { join } from 'node:path'

import { afterEach, describe, expect, it, vi } from 'vitest'

import { readSlotTranscript } from '../apps/design-tweak/api'

/**
 * The chat-slot API has two responses that both carry a `messages` field, and
 * they mean different things:
 *
 *   POST /api/chat/slots        -> serialize_slot(): `messages` is a COUNT, no queue
 *   GET  /api/chat/slots/{key}  -> prepared message ENTRIES + pending queue
 *
 * Confusing them is silent in both directions: reading the count as a list finds
 * nothing (so every sealed batch looks undelivered and the duplicate resend comes
 * back), and reading it with `.length` yields `undefined` (so an "is the slot
 * empty" guard is always true and re-seeds the session on every mount). Both
 * happened, so both are pinned here.
 */
describe('design-tweak chat-slot response contract', () => {
  const root = process.cwd()
  const api = readFileSync(join(root, 'src/apps/design-tweak/api.ts'), 'utf-8')
  const types = readFileSync(join(root, 'src/apps/design-tweak/types.ts'), 'utf-8')
  const page = readFileSync(join(root, 'src/apps/design-tweak/DesignTweakPage.tsx'), 'utf-8')

  it('types the adopt response `messages` as a number, not a list', () => {
    const block = types.slice(
      types.indexOf('export interface ChatSlotResponse'),
      types.indexOf('}', types.indexOf('export interface ChatSlotResponse')),
    )
    expect(block).toContain('messages?: number')
    expect(block).not.toContain('unknown[]')
  })

  it('reads the transcript from the slot-DETAIL endpoint', () => {
    // The adopt POST cannot answer this question; it has no queue and no entries.
    expect(api).toContain('slotDetailUrl')
    const fn = api.slice(
      api.indexOf('export async function readSlotTranscript'),
      api.indexOf('\n}', api.indexOf('export async function readSlotTranscript')),
    )
    expect(fn).toContain('slotDetailUrl(key)')
    // And it must refuse a response whose messages are not actually a list,
    // rather than treating the count as an empty transcript.
    expect(fn).toContain('Array.isArray(detail.messages)')
  })

  it('gates the session seed on a zero COUNT, never on .length', () => {
    const fn = page.slice(
      page.indexOf('const ensureSlot'),
      page.indexOf('}, [])', page.indexOf('const ensureSlot')),
    )
    expect(fn).toContain('slot?.messages === 0')
    // Strip comments first: the prose above the guard names the old expression
    // to explain the bug, and matching that would be a false positive.
    const code = fn.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/.*$/gm, '')
    expect(code).not.toContain('messages?.length')
  })

  it('shows a send control only for a proven-missing request', () => {
    // `needsDeliveryRetry` alone is "unconfirmed", which includes a batch whose
    // ack was merely lost — offering a send there duplicates every edit.
    expect(page).toContain('sendMissing && comments.length > 0')
    expect(page).not.toContain('needsDeliveryRetry(req) && comments.length > 0')
  })

  it('asks the slot dispatch used, addressed by slot key not raw path', () => {
    const fn = page.slice(
      page.indexOf('const verifyDelivery'),
      page.indexOf('}, [projects, previewId, refresh])', page.indexOf('const verifyDelivery')),
    )
    // Adopt is idempotent BY NAME: a raw path adopts a different, empty slot, so
    // every request reads as undelivered and a junk session is created.
    expect(fn).toContain('readSlotTranscript(\n        slotKeyFor(root),')
    const code = fn.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/.*$/gm, '')
    expect(code).not.toMatch(/readSlotTranscript\(\s*path\b/)
  })

  it('verifies each request against ITS OWN project root', () => {
    const fn = page.slice(
      page.indexOf('const verifyDelivery'),
      page.indexOf('}, [projects, previewId, refresh])', page.indexOf('const verifyDelivery')),
    )
    // A request exists only in the slot its dispatch used. Resolving ONE path and
    // checking every pending request against it reads other projects' requests as
    // missing and offers a duplicate send for delivered work.
    expect(fn).toContain('byRoot')
    const code = fn.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/.*$/gm, '')
    expect(code).not.toMatch(/pending\.find\(\(r\) => r\.projectRoot\)/)
  })

  it('readSlotTranscript refuses a filesystem path as the slot key', () => {
    const fn = api.slice(
      api.indexOf('export async function readSlotTranscript'),
      api.indexOf('\n}', api.indexOf('export async function readSlotTranscript')),
    )
    expect(fn).toContain("slotKey.includes('/')")
    expect(fn).toContain("slotKey.includes('\\\\')")
  })
})

describe('readSlotTranscript behavior', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('makes NO request for a raw path, so no junk slot is adopted', async () => {
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
    expect(await readSlotTranscript('/Users/me/proj', 'Design Tweak')).toBeNull()
    expect(await readSlotTranscript('C:\\Users\\me\\proj', 'Design Tweak')).toBeNull()
    // The point is the absence of the call: adopting is a side effect that would
    // create a session named after a filesystem path.
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('returns null rather than treating a message COUNT as a transcript', async () => {
    // Shape of the adopt response, which is what the code used to read.
    const fetchMock = vi.fn(async () =>
      new Response(JSON.stringify({ key: 'dt-abc', messages: 7 }), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      }),
    )
    vi.stubGlobal('fetch', fetchMock)
    // Detail returns the same count-shaped body -> not a list -> unknown, not empty.
    expect(await readSlotTranscript('dt-abc', 'Design Tweak')).toBeNull()
  })

  it('returns the transcript when the detail read yields real entries', async () => {
    const fetchMock = vi.fn(async (url: string | URL | Request) => {
      const u = String(url)
      const body = u.includes('/api/chat/slots/')
        ? { key: 'dt-abc', messages: [{ role: 'user', content: 'req-1' }], queue: [] }
        : { key: 'dt-abc', messages: 3 }
      return new Response(JSON.stringify(body), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      })
    })
    vi.stubGlobal('fetch', fetchMock)
    const t = await readSlotTranscript('dt-abc', 'Design Tweak')
    expect(Array.isArray(t?.messages)).toBe(true)
    expect(t?.messages).toHaveLength(1)
  })
})
