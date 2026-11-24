/**
 * The shared screenshot stub answers `/api/chat/pins` in the gateway's shape.
 *
 * `pinsApi.list` reads `res.pins`. The stub's catch-all answered `[]`, so the
 * pins query never held data: `useChatPins` handed out a new empty list on
 * every render, `isPinned` changed identity each time, and every transcript
 * row re-rendered on each page render. A real install answers `{ pins: [] }`,
 * so render and timing numbers taken against the stub were skewed.
 */
import { describe, it, expect } from 'vitest'
import { stubDashboardApi } from '../../scripts/lib/stub-dashboard-api.mjs'

type Handler = (route: unknown) => Promise<unknown>

async function answer(path: string): Promise<{ status: number; body: unknown }> {
  const handlers: { pattern: string; fn: Handler }[] = []
  const page = {
    routeWebSocket: async () => {},
    route: async (pattern: string, fn: Handler) => { handlers.push({ pattern, fn }) },
    addInitScript: async () => {},
  }
  await stubDashboardApi(page as never)
  const api = handlers.find(h => h.pattern === '**/api/**')!
  let out: { status: number; body: unknown } | null = null
  await api.fn({
    request: () => ({ url: () => `http://127.0.0.1${path}`, method: () => 'GET' }),
    fulfill: async ({ status, body }: { status: number; body: string }) => { out = { status, body: JSON.parse(body) } },
    fallback: async () => {},
  })
  return out!
}

describe('stub-dashboard-api pins route', () => {
  it('answers the list in the gateway shape the client reads', async () => {
    const res = await answer('/api/chat/pins?slot=perf-long')
    expect(res.status).toBe(200)
    expect(res.body).toEqual({ pins: [] })
  })
})
