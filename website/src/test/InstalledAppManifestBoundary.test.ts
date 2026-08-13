/**
 * The installed-app manifest boundary (#3706).
 *
 * Two claims, each the reason a `!` assertion was deleted rather than moved:
 *
 *  1. `/api/apps` payloads are normalized in `client.ts`, so every consumer —
 *     the Apps page, the left rail, the command palette, the migration check —
 *     receives a manifest with its lists present. Normalizing in one queryFn
 *     would leave the other three reading raw records.
 *  2. `appNavTarget` derives its page ONCE, so "is this app navigable" and
 *     "here is its page" cannot disagree. That divergence — a guard in one
 *     expression, a `manifest!.ui!.pages![0]` in another — is what produced
 *     #3689.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { api } from '../api/client'
import { appNavTarget, isAppNavigable, type AppNavRecord } from '../appNav'

function res(body: unknown) {
  return {
    ok: true,
    status: 200,
    headers: { get: (k: string) => (k.toLowerCase() === 'content-type' ? 'application/json' : null) },
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as unknown as Response
}

describe('/api/apps normalizes at the fetch boundary', () => {
  let fetchMock: ReturnType<typeof vi.fn>

  beforeEach(() => {
    fetchMock = vi.fn(async () => res([]))
    vi.stubGlobal('fetch', fetchMock)
  })
  afterEach(() => { vi.unstubAllGlobals() })

  it('fills manifest lists for a record the gateway sent without them', async () => {
    fetchMock.mockResolvedValueOnce(res([{ name: 'bare', version: '1.0.0', enabled: true }]))
    const apps = await api.listApps()
    expect(apps[0].manifest.agents).toEqual([])
    expect(apps[0].manifest.skills).toEqual([])
    expect(apps[0].manifest.crons).toEqual([])
    // Read the way the Apps page's uninstall dialog reads it — no gate.
    expect(apps[0].manifest.crons.map((c: { name: string }) => c.name)).toEqual([])
  })

  it('normalizes the single-app payload the detail page loads', async () => {
    fetchMock.mockResolvedValueOnce(res({ name: 'solo', manifest: { skills: ['skills/a.md', 7] } }))
    const app = await api.getApp('solo')
    expect(app.manifest.skills).toEqual(['skills/a.md'])
    expect(app.manifest.agents).toEqual([])
  })

  it('leaves published contents alone', async () => {
    fetchMock.mockResolvedValueOnce(res([{ name: 'real', manifest: { agents: ['agents/a.json'], hidden: true } }]))
    const apps = await api.listApps()
    expect(apps[0].manifest.agents).toEqual(['agents/a.json'])
    expect(apps[0].manifest.hidden).toBe(true)
  })
})

describe('appNavTarget derives its page once', () => {
  const withPages = (pages: unknown): AppNavRecord => ({
    name: 'x', enabled: true, manifest: { ui: { pages } } as AppNavRecord['manifest'],
  })

  it('agrees with isAppNavigable on every shape of a missing page', () => {
    // The regression guard: any input where one of these two answers "yes" and
    // the other "no" is the drift that #3689 crashed on.
    const cases: AppNavRecord[] = [
      { name: 'x', enabled: true },
      { name: 'x', enabled: true, manifest: {} },
      { name: 'x', enabled: true, manifest: { ui: {} } },
      withPages([]),
      withPages(undefined),
      { name: 'x', enabled: false, manifest: { ui: { pages: [{ route: '/apps/x' }] } } },
      { name: 'x', manifest: { ui: { pages: [{ route: '/apps/x' }] } } },
    ]
    for (const app of cases) {
      expect(appNavTarget(app)).toBeNull()
      expect(isAppNavigable(app)).toBe(false)
    }
  })

  it('resolves the first page when there is one', () => {
    const target = appNavTarget({
      name: 'x', enabled: true, origin: 'builtin',
      manifest: { ui: { pages: [{ route: '/x', label: 'X', icon: 'Box' }] } },
    })
    expect(isAppNavigable({ name: 'x', enabled: true, manifest: { ui: { pages: [{ route: '/x' }] } } })).toBe(true)
    expect(target?.route).toBe('/x')
    expect(target?.iconName).toBe('Box')
  })
})
