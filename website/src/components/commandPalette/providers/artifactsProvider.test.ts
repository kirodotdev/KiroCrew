import { describe, it, expect, vi } from 'vitest'
import {
  createArtifactsProvider,
  type ArtifactsProviderDeps,
  type ArtifactsResponse,
} from './artifactsProvider'
import type { Result } from '../types'
import type { Artifact } from '../../../types'

/**
 * Unit tests for the pure {@link createArtifactsProvider} factory
 * (Search Everywhere — Artifacts tab). Side effects are injected, so we pass a
 * plain mock fetch + open spy and assert the mapping (title/subtitle/id),
 * highlight indices, Enter wiring, and empty-query ordering preservation.
 */

function artifact(over: Partial<Artifact>): Artifact {
  return {
    slug: 'a',
    name: 'Artifact',
    kind: 'markdown',
    source: 'chat',
    description: '',
    tags: [],
    version: 1,
    created_at: '',
    updated_at: '',
    ...over,
  }
}

const ARTIFACTS: Artifact[] = [
  artifact({ slug: 'release-notes', name: 'Release Notes', snippet: 'Shipped the command palette' }),
  artifact({ slug: 'cr-queue', name: 'CR Queue', description: 'Open code reviews' }),
]

function deps(artifacts: Artifact[] = ARTIFACTS): {
  d: ArtifactsProviderDeps
  fetchArtifacts: ReturnType<typeof vi.fn>
  openArtifact: ReturnType<typeof vi.fn>
} {
  const fetchArtifacts = vi.fn((): Promise<ArtifactsResponse> => Promise.resolve({ artifacts }))
  const openArtifact = vi.fn()
  return { d: { fetchArtifacts, openArtifact }, fetchArtifacts, openArtifact }
}

async function run(
  p: ReturnType<typeof createArtifactsProvider>,
  q: string,
): Promise<Result[]> {
  return Promise.resolve(p.search(q))
}

describe('createArtifactsProvider — identity', () => {
  it('exposes the artifacts provider id, label, and an icon node', () => {
    const p = createArtifactsProvider(deps().d)
    expect(p.id).toBe('artifacts')
    expect(p.label).toBe('Artifacts')
    expect(p.icon).toBeTruthy()
  })
})

describe('createArtifactsProvider — search mapping', () => {
  it('maps each artifact to a result with slug id, name title, and snippet subtitle', async () => {
    const p = createArtifactsProvider(deps().d)
    const results = await run(p, 'command')
    const rn = results.find(r => r.id === 'artifacts:release-notes')
    expect(rn).toBeDefined()
    expect(rn?.providerId).toBe('artifacts')
    expect(rn?.title).toBe('Release Notes')
    expect(rn?.subtitle).toBe('Shipped the command palette')
  })

  it('falls back to description then kind when no snippet is present', async () => {
    const p = createArtifactsProvider(deps().d)
    const results = await run(p, 'queue')
    const cr = results.find(r => r.id === 'artifacts:cr-queue')
    expect(cr?.subtitle).toBe('Open code reviews')
  })

  it('requests content-search hits via the injected fetch', async () => {
    const { d, fetchArtifacts } = deps()
    const p = createArtifactsProvider(d)
    await run(p, 'palette')
    expect(fetchArtifacts).toHaveBeenCalledWith('palette')
  })

  it('opens the artifact detail page on Enter (onActivate)', async () => {
    const { d, openArtifact } = deps()
    const p = createArtifactsProvider(d)
    const results = await run(p, 'queue')
    results.find(r => r.id === 'artifacts:cr-queue')?.onActivate()
    expect(openArtifact).toHaveBeenCalledWith('cr-queue')
  })

  it('carries highlight indices for a title match', async () => {
    const p = createArtifactsProvider(deps().d)
    const results = await run(p, 'queue')
    const cr = results.find(r => r.id === 'artifacts:cr-queue')
    expect(Array.isArray(cr?.indices)).toBe(true)
    expect((cr?.indices.length ?? 0)).toBeGreaterThan(0)
  })

  it('highlights the substring the server matched, not a fuzzy spread', async () => {
    // The server matches a plain substring. A fuzzy highlight bolds letters the
    // match never used: "revenue" against "Report 1 Revenue" lit up "Re" at the
    // front as well, and nothing on screen explained why.
    const { d } = deps()
    d.fetchArtifacts = async () => ({
      artifacts: [{ slug: 'r1', name: 'Report 1 Revenue', kind: 'widget', description: '', tags: [] }],
    })
    const results = await run(createArtifactsProvider(d), 'revenue')
    const idx = results[0]?.indices ?? []
    // Contiguous, and exactly the run of characters the query occupies.
    expect(idx).toEqual([9, 10, 11, 12, 13, 14, 15])
    expect('Report 1 Revenue'.slice(9, 16).toLowerCase()).toBe('revenue')
  })

  it('puts no title highlight on a body-only match, and highlights the snippet instead', async () => {
    // The palette tab's own search is a CONTENT search, so the server can return a
    // row whose name does not contain the query at all. Before the highlight was
    // keyed on the substring, `fuzzyMatch` still found a scattered subsequence in
    // such a title and bolded letters the match never used. There must be no
    // highlight on a title the query is absent from -- and the snippet, which is
    // where the match actually happened, must carry it.
    //
    // "cost" is a subsequence of "Customer Support Tracker" (C-o-s-t across three
    // words) but not a substring of it, which is what makes this fixture able to
    // tell the two highlight sources apart.
    const { d } = deps([
      artifact({
        slug: 'plan',
        name: 'Customer Support Tracker',
        snippet: 'cost breakdown by region',
      }),
    ])
    const results = await run(createArtifactsProvider(d), 'cost')
    const row = results[0]
    expect('Customer Support Tracker'.toLowerCase()).not.toContain('cost')
    expect(row?.indices).toEqual([])
    expect(row?.subtitleIndices?.length ?? 0).toBeGreaterThan(0)
    // Kept, not dropped: a body-only hit is still a hit.
    expect(row?.id).toBe('artifacts:plan')
  })

  it('preserves backend order on an empty query (no client re-rank)', async () => {
    const p = createArtifactsProvider(deps().d)
    const results = await run(p, '')
    expect(results.map(r => r.id)).toEqual([
      'artifacts:release-notes',
      'artifacts:cr-queue',
    ])
  })
})

describe('createArtifactsProvider — backend relevance order is the score tiebreak (issue #4579)', () => {
  it('preserves backend order for body-only hits (all scores 0) instead of alphabetizing', async () => {
    // Titles are in REVERSE-alphabetical order and share no characters with the
    // query, so every row is a body hit with score 0. The backend ranked these
    // by relevance / mtime; the old name tiebreak returned them alphabetized.
    const items: Artifact[] = [
      artifact({ slug: 'z-art', name: 'Zebra doc', snippet: 'mentions 4579' }),
      artifact({ slug: 'm-art', name: 'Muffin doc', snippet: 'discusses 4579' }),
      artifact({ slug: 'a-art', name: 'Alpha doc', snippet: '4579 details' }),
    ]
    const { d } = deps(items)
    const p = createArtifactsProvider(d)
    const results = await run(p, '4579')
    expect(results).toHaveLength(3)
    expect(results.every(r => r.score === 0)).toBe(true)
    // Backend order, NOT ['Alpha doc', 'Muffin doc', 'Zebra doc'].
    expect(results.map(r => r.title)).toEqual(['Zebra doc', 'Muffin doc', 'Alpha doc'])
  })

  it('still ranks a title match first even when the backend returned it last (bias preserved)', async () => {
    const items: Artifact[] = [
      artifact({ slug: 'u-1', name: 'Unrelated alpha', snippet: 'grid spec' }),
      artifact({ slug: 'u-2', name: 'Unrelated beta', snippet: 'grid spec again' }),
      artifact({ slug: 'g-1', name: 'grid design' }),
    ]
    const { d } = deps(items)
    const p = createArtifactsProvider(d)
    const results = await run(p, 'grid')
    expect(results).toHaveLength(3)
    expect(results[0].title).toBe('grid design')
    expect(results[0].score).toBeGreaterThan(0)
    // The remaining body-only rows keep their backend order between themselves.
    expect(results.slice(1).map(r => r.title)).toEqual(['Unrelated alpha', 'Unrelated beta'])
  })
})

