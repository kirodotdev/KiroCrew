import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

/**
 * The highlight worker's message contract. The worker exists so a
 * catastrophically-backtracking highlight only delays this reply instead of
 * freezing the UI, so what matters here is that every request gets exactly one
 * reply carrying the SAME id — including when highlighting throws, where the
 * plain-text fallback must still answer rather than leave the caller hanging.
 */
type Reply = { id: number; html: string }

let posted: Reply[]
let handler: (e: { data: { id: number; code: string; lang?: string } }) => void

beforeEach(async () => {
  posted = []
  vi.resetModules()
  const ctx = self as unknown as { postMessage: unknown; onmessage: unknown }
  ctx.postMessage = (msg: Reply) => { posted.push(msg) }
  ctx.onmessage = null
  await import('../utils/hljsWorker')
  handler = ctx.onmessage as typeof handler
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe('hljsWorker', () => {
  it('installs a message handler on import', () => {
    expect(typeof handler).toBe('function')
  })

  it('highlights with the requested language and echoes the request id', () => {
    handler({ data: { id: 7, code: 'const zzz = 1', lang: 'javascript' } })
    expect(posted).toHaveLength(1)
    expect(posted[0].id).toBe(7)
    // Real hljs markup, not a passthrough.
    expect(posted[0].html).toContain('hljs-keyword')
  })

  it('falls back to auto-detection for an unregistered language', () => {
    handler({ data: { id: 8, code: 'def zzz():\n    return 1', lang: 'not-a-language' } })
    expect(posted[0].id).toBe(8)
    expect(posted[0].html).toContain('<span')
  })

  it('auto-detects when no language is given', () => {
    handler({ data: { id: 9, code: '{"zzz": 1}' } })
    expect(posted[0].id).toBe(9)
    expect(posted[0].html.length).toBeGreaterThan(0)
  })

  it('replies with empty html (plain-text fallback) when highlighting throws', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    // A non-string body makes hljs throw internally; the worker must still answer.
    handler({ data: { id: 10, code: undefined as unknown as string, lang: 'javascript' } })
    expect(posted).toEqual([{ id: 10, html: '' }])
    expect(warn).toHaveBeenCalled()
  })
})
