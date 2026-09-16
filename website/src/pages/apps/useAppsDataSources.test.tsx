/**
 * useAppsData — the SOURCES rail rows.
 *
 * `sources` is the App Store rail's provenance list. Two things about it are
 * pinned here rather than through DiscoverPage's render surface, because both
 * were defects users hit:
 *
 * - a BUILD-PINNED registry was absent from the list entirely, so its apps fell
 *   through to the stale-cache branch and the rail showed the bare registry id
 *   with a neutral icon — no display name, no review claim.
 * - the rows carry `review`, ordered curated → unreviewed → community by the
 *   same helper the External Registries card uses, so the two lists cannot
 *   disagree about where a community source sits.
 *
 * `i18nT` is left real: the assertions here are about identity, label and order,
 * not copy.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const { listApps, listRegistry, listRegistries } = vi.hoisted(() => ({
  listApps: vi.fn(),
  listRegistry: vi.fn(),
  listRegistries: vi.fn(),
}))

vi.mock('../../api/client', () => ({
  api: {
    listApps: (...a: unknown[]) => listApps(...a),
    listRegistry: (...a: unknown[]) => listRegistry(...a),
    listRegistries: (...a: unknown[]) => listRegistries(...a),
  },
}))

import useAppsData from './useAppsData'

/** A registry app tagged with the registry that listed it. */
const regApp = (name: string, registry: string) => ({
  name, displayName: name, description: '', _registry: registry,
})

function renderSources() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const wrapper = ({ children }: { children: React.ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  )
  return renderHook(() => useAppsData(), { wrapper })
}

describe('useAppsData sources rail', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    listApps.mockResolvedValue([])
    listRegistry.mockResolvedValue({ apps: [], serverPlatform: { os: 'linux', arch: 'x64' } })
  })

  it('gives a build-pinned registry its label, review tier and app count', async () => {
    listRegistry.mockResolvedValue({
      apps: [regApp('alpha', 'internal'), regApp('beta', 'internal')],
      serverPlatform: { os: 'linux', arch: 'x64' },
    })
    listRegistries.mockResolvedValue({
      registries: [],
      pinned: [{
        name: 'internal', repo: 'https://forge.example.com/org/internal.git', branch: 'main',
        trust: 'owner', label: 'Internal apps', review: 'curated',
      }],
    })
    const { result } = renderSources()
    await waitFor(() => expect(result.current.sources.length).toBeGreaterThan(0))
    const row = result.current.sources.find(s => s.name === 'internal')
    expect(row).toMatchObject({ label: 'Internal apps', review: 'curated', count: 2, builtin: false })
  })

  it('orders curated above unreviewed above community, whatever the backend sent', async () => {
    listRegistries.mockResolvedValue({
      registries: [{ name: 'mine', repo: 'https://forge.example.com/org/mine.git', branch: 'main' }],
      pinned: [
        { name: 'community', repo: 'https://f.example.com/c.git', branch: 'main', label: 'Community apps', review: 'community' },
        { name: 'internal', repo: 'https://f.example.com/i.git', branch: 'main', label: 'Internal apps', review: 'curated' },
      ],
    })
    const { result } = renderSources()
    await waitFor(() => expect(result.current.sources.length).toBe(3))
    expect(result.current.sources.map(s => s.name)).toEqual(['internal', 'mine', 'community'])
  })

  it('keeps built-in first, above even a curated registry', async () => {
    listRegistry.mockResolvedValue({
      apps: [{ name: 'core', displayName: 'core', description: '', origin: 'builtin' }],
      serverPlatform: { os: 'linux', arch: 'x64' },
    })
    listApps.mockResolvedValue([{ name: 'core', displayName: 'core', origin: 'builtin', enabled: true }])
    listRegistries.mockResolvedValue({
      registries: [],
      pinned: [{ name: 'internal', repo: 'https://f.example.com/i.git', branch: 'main', label: 'Internal apps', review: 'curated' }],
    })
    const { result } = renderSources()
    await waitFor(() => expect(result.current.sources.length).toBeGreaterThan(1))
    expect(result.current.sources[0].name).toBe('__builtin__')
  })

  it('counts by the registry id, so a label never moves an app', async () => {
    // Every installed app's `_registry` tag and the index cache path are keyed by
    // the id. Counting by the label would strand the apps of any labelled
    // registry at zero.
    listRegistry.mockResolvedValue({
      apps: [regApp('alpha', 'community')],
      serverPlatform: { os: 'linux', arch: 'x64' },
    })
    listRegistries.mockResolvedValue({
      registries: [],
      pinned: [{ name: 'community', repo: 'https://f.example.com/c.git', branch: 'main', label: 'Community apps', review: 'community' }],
    })
    const { result } = renderSources()
    await waitFor(() => expect(result.current.sources.length).toBe(1))
    expect(result.current.sources[0]).toMatchObject({ name: 'community', label: 'Community apps', count: 1 })
  })

  it('leaves a registry with no review tier claiming nothing', async () => {
    listRegistries.mockResolvedValue({
      registries: [{ name: 'mine', repo: 'https://forge.example.com/org/mine.git', branch: 'main' }],
    })
    const { result } = renderSources()
    await waitFor(() => expect(result.current.sources.length).toBe(1))
    expect(result.current.sources[0].review).toBeUndefined()
    expect(result.current.sources[0].label).toBe('mine')
  })

  it('renders a pinned-and-configured collision once, the pinned row winning', async () => {
    // GET reports `config.registries` raw beside `pinned`, so a config.json
    // naming a pinned registry appears in both lists. Rendering both would give
    // duplicate React keys and a second row whose apps never load — the backend
    // merge already dropped it. Same rule as `_effective_registries`.
    listRegistries.mockResolvedValue({
      registries: [{ name: 'internal', repo: 'https://evil.example.com/x.git', branch: 'main' }],
      pinned: [{ name: 'internal', repo: 'https://f.example.com/i.git', branch: 'main', label: 'Internal apps', review: 'curated' }],
    })
    const { result } = renderSources()
    await waitFor(() => expect(result.current.sources.length).toBe(1))
    expect(result.current.sources[0]).toMatchObject({ name: 'internal', label: 'Internal apps', review: 'curated' })
  })

  it('drops a CASE-variant operator row, like the backend cache-file key does', async () => {
    // `Internal` and `internal` are one index cache file on Windows and default
    // macOS, so the backend contests them there; a second row would read as its
    // own source and never load.
    listRegistries.mockResolvedValue({
      registries: [{ name: 'Internal', repo: 'https://evil.example.com/x.git', branch: 'main' }],
      pinned: [{ name: 'internal', repo: 'https://f.example.com/i.git', branch: 'main', label: 'Internal apps', review: 'curated' }],
    })
    const { result } = renderSources()
    await waitFor(() => expect(result.current.sources.length).toBe(1))
    expect(result.current.sources[0].name).toBe('internal')
  })

  it('lets a pinned COMMUNITY row win a collision with an unreviewed operator row', async () => {
    // The dedupe must run BEFORE the sort. Sorted first, a pinned `community`
    // row ranks after an unreviewed one, so the hand-edited operator row would
    // be seen first and win — showing the operator's repo in place of the
    // build's community registry, with none of its warning copy. This is the
    // one collision where the merge rule matters most.
    listRegistries.mockResolvedValue({
      registries: [{ name: 'community', repo: 'https://evil.example.com/x.git', branch: 'main' }],
      pinned: [{ name: 'community', repo: 'https://f.example.com/c.git', branch: 'main', label: 'Community apps', review: 'community' }],
    })
    const { result } = renderSources()
    await waitFor(() => expect(result.current.sources.length).toBe(1))
    expect(result.current.sources[0]).toMatchObject({
      name: 'community', label: 'Community apps', review: 'community',
    })
  })
})
