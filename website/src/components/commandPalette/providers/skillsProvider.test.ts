import { describe, it, expect, vi } from 'vitest'
import {
  createSkillsProvider,
  type SkillItem,
  type SkillsProviderDeps,
} from './skillsProvider'
import type { Result } from '../types'

/**
 * Unit tests for the pure {@link createSkillsProvider} factory
 * (Search Everywhere, step 17 — the Skills provider + its §2 Enter matrix). Side
 * effects are injected, so we pass plain spies and assert filtering + the
 * context-aware Enter / ⌘Enter / ⌥Enter wiring + the `$<name>` token format.
 */

const SKILLS: SkillItem[] = [
  { name: 'brazil', description: 'build system' },
  { name: 'crux', description: 'Code reviews' },
]

function deps(hasActiveChat: boolean): {
  d: SkillsProviderDeps
  insertToken: ReturnType<typeof vi.fn>
  newSessionWithToken: ReturnType<typeof vi.fn>
  openSkillDoc: ReturnType<typeof vi.fn>
} {
  const insertToken = vi.fn()
  const newSessionWithToken = vi.fn()
  const openSkillDoc = vi.fn()
  return {
    d: {
      fetchSkills: () => Promise.resolve(SKILLS),
      hasActiveChat,
      insertToken,
      newSessionWithToken,
      openSkillDoc,
    },
    insertToken,
    newSessionWithToken,
    openSkillDoc,
  }
}

async function run(
  p: ReturnType<typeof createSkillsProvider>,
  q: string,
): Promise<Result[]> {
  return Promise.resolve(p.search(q))
}

describe('createSkillsProvider — identity', () => {
  it('exposes the skills provider id, label, and an icon node', () => {
    const p = createSkillsProvider(deps(true).d)
    expect(p.id).toBe('skills')
    expect(p.label).toBe('Skills')
    expect(p.icon).toBeTruthy()
  })
})

describe('createSkillsProvider — listing + filtering', () => {
  it('lists every skill on an empty query, all tagged with the provider id', async () => {
    const arr = await run(createSkillsProvider(deps(true).d), '')
    expect(arr.map((r) => r.title).sort()).toEqual(['brazil', 'crux'])
    expect(arr.every((r) => r.providerId === 'skills')).toBe(true)
  })

  it('fuzzy-filters by skill name and returns highlight indices', async () => {
    const arr = await run(createSkillsProvider(deps(true).d), 'braz')
    expect(arr).toHaveLength(1)
    expect(arr[0].title).toBe('brazil')
    expect(arr[0].score).toBeGreaterThan(0)
    expect(arr[0].indices.length).toBeGreaterThan(0)
  })

  it('returns nothing when no skill name matches', async () => {
    expect(await run(createSkillsProvider(deps(true).d), 'zzzqqq')).toEqual([])
  })
})

describe('createSkillsProvider — §2 Enter matrix', () => {
  it('Enter inserts the $token into the active chat when one is open', async () => {
    const { d, insertToken, newSessionWithToken } = deps(true)
    const arr = await run(createSkillsProvider(d), 'brazil')
    arr[0].onActivate()
    expect(insertToken).toHaveBeenCalledWith('$brazil')
    expect(newSessionWithToken).not.toHaveBeenCalled()
  })

  it('Enter opens a new session seeded with the $token when no chat is active', async () => {
    const { d, insertToken, newSessionWithToken } = deps(false)
    const arr = await run(createSkillsProvider(d), 'brazil')
    arr[0].onActivate()
    expect(newSessionWithToken).toHaveBeenCalledWith('$brazil')
    expect(insertToken).not.toHaveBeenCalled()
  })

  it('⌘Enter always opens a new session seeded with the $token', async () => {
    const { d, newSessionWithToken } = deps(true)
    const arr = await run(createSkillsProvider(d), 'brazil')
    arr[0].onCmdActivate?.()
    expect(newSessionWithToken).toHaveBeenCalledWith('$brazil')
  })

  it('⌥Enter opens the skill doc with the skill ref', async () => {
    const { d, openSkillDoc } = deps(true)
    const arr = await run(createSkillsProvider(d), 'brazil')
    arr[0].onAltActivate?.()
    expect(openSkillDoc).toHaveBeenCalledWith({ name: 'brazil' })
  })
})
