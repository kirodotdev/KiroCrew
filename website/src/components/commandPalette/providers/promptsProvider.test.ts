import { describe, it, expect, vi } from 'vitest'
import {
  createPromptsProvider,
  type PromptItem,
  type PromptRef,
  type PromptsProviderDeps,
} from './promptsProvider'
import type { Result } from '../types'

/**
 * Unit tests for the pure {@link createPromptsProvider} factory
 * (Search Everywhere — the Prompts provider + its §2 Enter matrix).
 *
 * The factory is hook-free and takes its side effects as injected callbacks, so
 * we pass plain spies and assert: identity, fuzzy listing/filtering over the
 * prompt *name*, and the Enter / ⌘Enter / ⌥Enter wiring. The context-aware
 * `@<fullName>` token + insert-vs-new-session decision is layered on by the
 * `usePromptsProvider` hook through the shared allowlist-resolver entry points
 * (`paletteActions`); the matrix-level routing is asserted in
 * `paletteMatrix.test.ts`. Here we assert the factory forwards the prompt *ref*
 * verbatim to each callback — it never rewrites or path-resolves the name
 * (input validation).
 */

const PROMPTS: PromptItem[] = [
  { name: 'standup', fullName: 'team/standup', description: 'Daily standup', package: 'team' },
  { name: 'review', fullName: 'eng/review', description: 'Code review SOP' },
]

function deps(
  opts: {
    withCmd?: boolean
    withPreview?: boolean
  } = {},
): {
  d: PromptsProviderDeps
  insertPrompt: ReturnType<typeof vi.fn>
  newSessionWithPrompt: ReturnType<typeof vi.fn>
  previewPrompt: ReturnType<typeof vi.fn>
} {
  const insertPrompt = vi.fn()
  const newSessionWithPrompt = vi.fn()
  const previewPrompt = vi.fn()
  return {
    d: {
      fetchPrompts: () => Promise.resolve(PROMPTS),
      insertPrompt,
      newSessionWithPrompt: opts.withCmd === false ? undefined : newSessionWithPrompt,
      previewPrompt: opts.withPreview === false ? undefined : previewPrompt,
    },
    insertPrompt,
    newSessionWithPrompt,
    previewPrompt,
  }
}

async function run(
  p: ReturnType<typeof createPromptsProvider>,
  q: string,
): Promise<Result[]> {
  return Promise.resolve(p.search(q))
}

describe('createPromptsProvider — identity', () => {
  it('exposes the prompts provider id, label, and an icon node', () => {
    const p = createPromptsProvider(deps().d)
    expect(p.id).toBe('prompts')
    expect(p.label).toBe('Prompts')
    expect(p.icon).toBeTruthy()
  })
})

describe('createPromptsProvider — listing + filtering', () => {
  it('lists every prompt on an empty query, all tagged with the provider id', async () => {
    const arr = await run(createPromptsProvider(deps().d), '')
    expect(arr.map((r) => r.title).sort()).toEqual(['review', 'standup'])
    expect(arr.every((r) => r.providerId === 'prompts')).toBe(true)
  })

  it('keys each result by its fullName for stability', async () => {
    const arr = await run(createPromptsProvider(deps().d), '')
    expect(arr.map((r) => r.id).sort()).toEqual(['prompts:eng/review', 'prompts:team/standup'])
  })

  it('fuzzy-filters by prompt name and returns highlight indices', async () => {
    const arr = await run(createPromptsProvider(deps().d), 'stand')
    expect(arr).toHaveLength(1)
    expect(arr[0].title).toBe('standup')
    expect(arr[0].score).toBeGreaterThan(0)
    expect(arr[0].indices.length).toBeGreaterThan(0)
  })

  it('uses description then package as the subtitle', async () => {
    const arr = await run(createPromptsProvider(deps().d), '')
    const standup = arr.find((r) => r.title === 'standup')!
    const review = arr.find((r) => r.title === 'review')!
    expect(standup.subtitle).toBe('Daily standup')
    expect(review.subtitle).toBe('Code review SOP')
  })

  it('returns nothing when no prompt name matches', async () => {
    expect(await run(createPromptsProvider(deps().d), 'zzzqqq')).toEqual([])
  })
})

describe('createPromptsProvider — §2 Enter matrix', () => {
  const expectedRef: PromptRef = { name: 'standup', fullName: 'team/standup', path: undefined }

  it('Enter forwards the prompt ref verbatim to insertPrompt (no FE rewriting)', async () => {
    const { d, insertPrompt } = deps()
    const arr = await run(createPromptsProvider(d), 'standup')
    arr[0].onActivate()
    expect(insertPrompt).toHaveBeenCalledTimes(1)
    expect(insertPrompt).toHaveBeenCalledWith(expectedRef)
  })

  it('⌘Enter forwards the ref to newSessionWithPrompt', async () => {
    const { d, newSessionWithPrompt } = deps()
    const arr = await run(createPromptsProvider(d), 'standup')
    arr[0].onCmdActivate?.()
    expect(newSessionWithPrompt).toHaveBeenCalledWith(expectedRef)
  })

  it('⌥Enter forwards the ref to previewPrompt', async () => {
    const { d, previewPrompt } = deps()
    const arr = await run(createPromptsProvider(d), 'standup')
    arr[0].onAltActivate?.()
    expect(previewPrompt).toHaveBeenCalledWith(expectedRef)
  })

  it('leaves ⌘Enter / ⌥Enter unbound when the host omits those callbacks', async () => {
    const { d } = deps({ withCmd: false, withPreview: false })
    const arr = await run(createPromptsProvider(d), 'standup')
    expect(arr[0].onCmdActivate).toBeUndefined()
    expect(arr[0].onAltActivate).toBeUndefined()
  })

  it('preserves the fullName unchanged so the @token is built from it, never a path', async () => {
    const { d, insertPrompt } = deps()
    const arr = await run(createPromptsProvider(d), 'review')
    arr[0].onActivate()
    const ref = insertPrompt.mock.calls[0][0] as PromptRef
    // The factory hands over the raw fullName; it must not be turned into a
    // filesystem path here (input validation). The hook prefixes "@" — there is no "/.."
    // traversal or absolute-path construction at the FE layer.
    expect(ref.fullName).toBe('eng/review')
    expect(ref.fullName.startsWith('/')).toBe(false)
    expect(ref.fullName.includes('..')).toBe(false)
  })
})
