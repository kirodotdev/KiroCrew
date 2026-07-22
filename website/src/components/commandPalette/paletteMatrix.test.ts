import { describe, it, expect, vi, beforeEach } from 'vitest'

/**
 * CommandPalette §2 Enter-matrix test (step 19).
 *
 * Asserts the per-result-type Enter / ⌘Enter / ⌥Enter behavior across the v1
 * corpus (Skills · Prompts · Knowledge) and, critically, the **context-aware
 * insert-vs-new-session fallback** for invokable rows.
 *
 * Security spine (input validation): invokable activation MUST
 * route through the shipped allowlist-resolver entry point
 * ({@link resolveInvokableEnter} in `paletteActions`) and MUST NOT build any
 * filesystem path from raw input — the FE only ever emits a plain `$skill` /
 * `@prompt` token string, and the backend submit path does the allowlisted
 * resolution. We therefore **mock `resolveInvokableEnter`** so we can prove the
 * Skills provider's primary-Enter routes through it (with the exact
 * `hasActiveChat` flag + the verbatim token), rather than reaching for the
 * sinks directly or constructing a path.
 */

const H = vi.hoisted(() => ({
  resolveCalls: [] as Array<{
    hasActiveChat: boolean
    token: string
    sinks: { insertToken: (t: string) => void; newSessionWithToken: (t: string) => void }
  }>,
}))

// Mock the allowlist-resolver entry point. The fake records every call (so we
// can assert routing + the verbatim token) and preserves the real §2 branch
// semantics (active chat → insert; no chat → new session) so behavior
// assertions still hold.
vi.mock('./paletteActions', () => ({
  resolveInvokableEnter: (
    hasActiveChat: boolean,
    token: string,
    sinks: { insertToken: (t: string) => void; newSessionWithToken: (t: string) => void },
  ) => {
    H.resolveCalls.push({ hasActiveChat, token, sinks })
    return hasActiveChat ? () => sinks.insertToken(token) : () => sinks.newSessionWithToken(token)
  },
  // Hook form is never invoked in this pure-factory test; provide a stub so the
  // module shape is complete for any transitive importer.
  usePaletteActions: () => {
    throw new Error('usePaletteActions must not be called in the matrix unit test')
  },
}))

import { createSkillsProvider, type SkillsProviderDeps } from './providers/skillsProvider'
import {
  createPromptsProvider,
  type PromptRef,
  type PromptsProviderDeps,
} from './providers/promptsProvider'
import {
  createKnowledgeProvider,
  type KnowledgeProviderDeps,
  type KnowledgeRef,
} from './providers/knowledgeProvider'
import type { Result } from './types'

const SKILLS = [{ name: 'brazil', description: 'build system' }]
const PROMPTS = [{ name: 'standup', fullName: 'team/standup', description: 'daily' }]
const KNOWLEDGE = { results: [{ id: 'k1', title: 'Brazil notes', summary: 's' }] }

async function only(p: { search: (q: string) => Promise<Result[]> | Result[] }, q: string): Promise<Result> {
  const arr = await Promise.resolve(p.search(q))
  return arr[0]
}

function skillDeps(hasActiveChat: boolean): {
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

beforeEach(() => {
  H.resolveCalls.length = 0
})

describe('§2 matrix — Skills (insert-token) routes through the allowlist resolver', () => {
  it('Enter with an active chat: routes through resolveInvokableEnter and inserts the $token', async () => {
    const { d, insertToken, newSessionWithToken } = skillDeps(true)
    const r = await only(createSkillsProvider(d), 'brazil')
    r.onActivate()

    // Activation went through the allowlist-resolver entry point, not a direct
    // sink call wired by the provider.
    expect(H.resolveCalls).toHaveLength(1)
    expect(H.resolveCalls[0].hasActiveChat).toBe(true)
    expect(insertToken).toHaveBeenCalledWith('$brazil')
    expect(newSessionWithToken).not.toHaveBeenCalled()
  })

  it('Enter with no active chat: resolver fallback opens a new session seeded with the $token', async () => {
    const { d, insertToken, newSessionWithToken } = skillDeps(false)
    const r = await only(createSkillsProvider(d), 'brazil')
    r.onActivate()

    expect(H.resolveCalls).toHaveLength(1)
    expect(H.resolveCalls[0].hasActiveChat).toBe(false)
    expect(newSessionWithToken).toHaveBeenCalledWith('$brazil')
    expect(insertToken).not.toHaveBeenCalled()
  })

  it('only ever hands the resolver a plain token — never a path built from raw input', async () => {
    const { d } = skillDeps(true)
    const r = await only(createSkillsProvider(d), 'brazil')
    r.onActivate()

    const { token } = H.resolveCalls[0]
    expect(token).toBe('$brazil')
    // Input validation: no filesystem path is constructed at the FE layer.
    expect(token.startsWith('/')).toBe(false)
    expect(token.includes('/')).toBe(false)
    expect(token.includes('\\')).toBe(false)
    expect(token.includes('..')).toBe(false)
  })

  it('⌘Enter always opens a new session seeded with the $token', async () => {
    const { d, newSessionWithToken } = skillDeps(true)
    const r = await only(createSkillsProvider(d), 'brazil')
    r.onCmdActivate?.()
    expect(newSessionWithToken).toHaveBeenCalledWith('$brazil')
  })

  it('⌥Enter previews the skill doc (read path, separate from Enter)', async () => {
    const { d, openSkillDoc } = skillDeps(true)
    const r = await only(createSkillsProvider(d), 'brazil')
    r.onAltActivate?.()
    expect(openSkillDoc).toHaveBeenCalledWith({ name: 'brazil' })
    // Preview is not an invokable activation — it must not touch the resolver.
    expect(H.resolveCalls).toHaveLength(0)
  })
})

describe('§2 matrix — Prompts (insert-token)', () => {
  function deps(): {
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
        newSessionWithPrompt,
        previewPrompt,
      },
      insertPrompt,
      newSessionWithPrompt,
      previewPrompt,
    }
  }
  const ref: PromptRef = { name: 'standup', fullName: 'team/standup', path: undefined }

  it('Enter inserts (host-injected context-aware handler), ⌘Enter new-session, ⌥Enter preview', async () => {
    const { d, insertPrompt, newSessionWithPrompt, previewPrompt } = deps()
    const r = await only(createPromptsProvider(d), 'standup')
    r.onActivate()
    r.onCmdActivate?.()
    r.onAltActivate?.()
    expect(insertPrompt).toHaveBeenCalledWith(ref)
    expect(newSessionWithPrompt).toHaveBeenCalledWith(ref)
    expect(previewPrompt).toHaveBeenCalledWith(ref)
  })
})

describe('§2 matrix — Knowledge (open-knowledge)', () => {
  function deps(withAttach: boolean): {
    d: KnowledgeProviderDeps
    openEntry: ReturnType<typeof vi.fn>
    attachAsContext: ReturnType<typeof vi.fn>
  } {
    const openEntry = vi.fn()
    const attachAsContext = vi.fn()
    return {
      d: {
        fetchKnowledge: async () => KNOWLEDGE,
        openEntry,
        attachAsContext: withAttach ? attachAsContext : undefined,
      },
      openEntry,
      attachAsContext,
    }
  }
  const ref: KnowledgeRef = { id: 'k1', title: 'Brazil notes' }

  it('Enter opens the entry; ⌘Enter attaches it as chat context', async () => {
    const { d, openEntry, attachAsContext } = deps(true)
    const r = await only(createKnowledgeProvider(d), 'brazil')
    r.onActivate()
    r.onCmdActivate?.()
    expect(openEntry).toHaveBeenCalledWith(ref)
    expect(attachAsContext).toHaveBeenCalledWith(ref)
  })

  it('⌘Enter is unbound when the host supplies no attach-as-context callback', async () => {
    const { d } = deps(false)
    const r = await only(createKnowledgeProvider(d), 'brazil')
    expect(r.onCmdActivate).toBeUndefined()
  })
})
