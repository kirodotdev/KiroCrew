import { describe, expect, it, vi } from 'vitest'
import type { Page, Route } from 'playwright'
import {
  AGENT_CONFIG_FIXTURE,
  KIROCREW_CONFIG_FIXTURE,
  stubDashboardApi,
} from '../../scripts/lib/stub-dashboard-api.mjs'

async function responseFor(path: string) {
  let handler: ((route: Route) => unknown) | undefined
  const page = {
    routeWebSocket: vi.fn(),
    route: vi.fn((_pattern: string, next: (route: Route) => unknown) => { handler = next }),
    addInitScript: vi.fn(),
  }
  await stubDashboardApi(page as unknown as Page)
  const fulfill = vi.fn()
  await handler?.({
    request: () => ({ url: () => `http://localhost${path}` }),
    fulfill,
  } as unknown as Route)
  return JSON.parse(fulfill.mock.calls[0][0].body)
}

describe('shared dashboard screenshot config fixtures', () => {
  it('serves the complete Kiro Crew config shape before the catch-all', async () => {
    const body = await responseFor('/api/config/kirocrew')
    expect(body).toEqual(KIROCREW_CONFIG_FIXTURE)
    expect(Object.keys(body.agents)).toContain('kirocrew')
    expect(Object.keys(body.workspaces)).toContain('default')
    expect(Object.keys(body.memory_stores)).toContain('default')
  })

  it('serves an agent config object before the catch-all', async () => {
    await expect(responseFor('/api/agent/config')).resolves.toEqual(AGENT_CONFIG_FIXTURE)
  })
})
