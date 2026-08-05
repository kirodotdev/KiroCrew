import { describe, it, expect, vi } from 'vitest'
import {
  createKnowledgeProvider,
  type KnowledgeProviderDeps,
  type KnowledgeRef,
  type KnowledgeSearchItem,
} from './knowledgeProvider'
import type { Result } from '../types'

/**
 * Unit tests for the pure {@link createKnowledgeProvider} factory
 * (Search Everywhere — the Knowledge provider + its §2 Enter matrix).
 *
 * The backend (`/api/knowledge/search-for-context`) already does the relevance
 * filtering, so the provider's {@link fuzzyMatch} pass is only for highlight
 * indices + a client-side title-match rank bias — it must never DROP a backend
 * hit. We assert that, the open / attach-as-context wiring (Enter / ⌘Enter),
 * and that the fetch receives the trimmed query.
 */

const ITEMS: KnowledgeSearchItem[] = [
  { id: 'k1', title: 'Brazil build system', summary: 'how brazil works' },
  // Title does NOT contain "brazil"; only the body matched server-side. Must
  // still be returned (with a neutral score), never dropped.
  { id: 'k2', title: 'Deployment runbook', source: 'wiki', content: 'mentions brazil' },
]

function deps(
  opts: { withAttach?: boolean; items?: KnowledgeSearchItem[] } = {},
): {
  d: KnowledgeProviderDeps
  fetchKnowledge: ReturnType<typeof vi.fn>
  openEntry: ReturnType<typeof vi.fn>
  attachAsContext: ReturnType<typeof vi.fn>
} {
  const items = opts.items ?? ITEMS
  const fetchKnowledge = vi.fn(async () => ({ results: items }))
  const openEntry = vi.fn()
  const attachAsContext = vi.fn()
  return {
    d: {
      fetchKnowledge,
      openEntry,
      attachAsContext: opts.withAttach === false ? undefined : attachAsContext,
    },
    fetchKnowledge,
    openEntry,
    attachAsContext,
  }
}

async function run(
  p: ReturnType<typeof createKnowledgeProvider>,
  q: string,
): Promise<Result[]> {
  return Promise.resolve(p.search(q))
}

describe('createKnowledgeProvider — identity', () => {
  it('exposes the knowledge provider id, label, and an icon node', () => {
    const p = createKnowledgeProvider(deps().d)
    expect(p.id).toBe('knowledge')
    expect(p.label).toBe('Knowledge')
    expect(p.icon).toBeTruthy()
  })
})

describe('createKnowledgeProvider — backend hits', () => {
  it('maps every backend hit to a result (never drops a body-only match)', async () => {
    const arr = await run(createKnowledgeProvider(deps().d), 'brazil')
    expect(arr.map((r) => r.id).sort()).toEqual(['knowledge:k1', 'knowledge:k2'])
    expect(arr.every((r) => r.providerId === 'knowledge')).toBe(true)
  })

  it('passes the trimmed query to the fetcher', async () => {
    const { d, fetchKnowledge } = deps()
    await run(createKnowledgeProvider(d), '  brazil  ')
    expect(fetchKnowledge).toHaveBeenCalledWith('brazil')
  })

  it('ranks title matches ahead of body-only matches', async () => {
    const arr = await run(createKnowledgeProvider(deps().d), 'brazil')
    // k1 title contains "brazil" → score > 0; k2 title does not → neutral 0.
    expect(arr[0].id).toBe('knowledge:k1')
    expect(arr[0].score).toBeGreaterThan(0)
    expect(arr[0].indices.length).toBeGreaterThan(0)
    const k2 = arr.find((r) => r.id === 'knowledge:k2')!
    expect(k2.score).toBe(0)
    expect(k2.indices).toEqual([])
  })

  it('uses summary then source as the subtitle', async () => {
    const arr = await run(createKnowledgeProvider(deps().d), 'brazil')
    const k1 = arr.find((r) => r.id === 'knowledge:k1')!
    const k2 = arr.find((r) => r.id === 'knowledge:k2')!
    expect(k1.subtitle).toBe('how brazil works')
    expect(k2.subtitle).toBe('wiki')
  })

  it('falls back to the id as the title when the hit has none', async () => {
    const { d } = deps({ items: [{ id: 'k9', title: '' }] })
    const arr = await run(createKnowledgeProvider(d), '')
    expect(arr[0].title).toBe('k9')
  })

  it('returns an empty list when the backend returns no results', async () => {
    const { d } = deps({ items: [] })
    expect(await run(createKnowledgeProvider(d), 'anything')).toEqual([])
  })
})

describe('createKnowledgeProvider — §2 Enter matrix', () => {
  const expectedRef: KnowledgeRef = { id: 'k1', title: 'Brazil build system' }

  it('Enter opens the entry with its {id, title} ref', async () => {
    const { d, openEntry } = deps()
    const arr = await run(createKnowledgeProvider(d), 'brazil')
    arr.find((r) => r.id === 'knowledge:k1')!.onActivate()
    expect(openEntry).toHaveBeenCalledWith(expectedRef)
  })

  it('⌘Enter attaches the entry as context when the host supplies the callback', async () => {
    const { d, attachAsContext } = deps()
    const arr = await run(createKnowledgeProvider(d), 'brazil')
    arr.find((r) => r.id === 'knowledge:k1')!.onCmdActivate?.()
    expect(attachAsContext).toHaveBeenCalledWith(expectedRef)
  })

  it('leaves ⌘Enter unbound when no attach-as-context callback is supplied', async () => {
    const { d } = deps({ withAttach: false })
    const arr = await run(createKnowledgeProvider(d), 'brazil')
    expect(arr.find((r) => r.id === 'knowledge:k1')!.onCmdActivate).toBeUndefined()
  })
})
