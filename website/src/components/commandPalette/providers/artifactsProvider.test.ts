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

  it('preserves backend order on an empty query (no client re-rank)', async () => {
    const p = createArtifactsProvider(deps().d)
    const results = await run(p, '')
    expect(results.map(r => r.id)).toEqual([
      'artifacts:release-notes',
      'artifacts:cr-queue',
    ])
  })
})
