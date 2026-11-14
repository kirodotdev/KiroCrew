/**
 * `sourceTabQualifier` treats two spellings of one GitHub repository as one
 * project. GitHub resolves owner and repo names case-insensitively, so a strip
 * holding `kirodotdev/KiroCrew#1` and `kirodotdev/kirocrew#2` names one project
 * and needs no `owner/repo` prefix on either chip. GitLab paths stay
 * case-sensitive, and a real second project still qualifies every chip.
 */
import { describe, it, expect } from 'vitest'
import { sourceTabQualifier } from '../utils/sourceProviderMeta'
import type { SourceLinkRef } from '../utils/pullRequestLinks'

const link = (provider: string, url: string, number: number): SourceLinkRef =>
  ({ provider, url, number, kind: 'change' }) as SourceLinkRef

describe('sourceTabQualifier GitHub case folding', () => {
  it('reads two spellings of one GitHub repository as one project', () => {
    const a = link('github', 'https://github.com/kirodotdev/KiroCrew/pull/1', 1)
    const b = link('github', 'https://github.com/kirodotdev/kirocrew/pull/2', 2)
    const qualify = sourceTabQualifier([a, b])
    expect(qualify(a)).toBeNull()
    expect(qualify(b)).toBeNull()
  })

  it('still qualifies a real second repository, in the spelling the link carries', () => {
    const a = link('github', 'https://github.com/kirodotdev/KiroCrew/pull/1', 1)
    const b = link('github', 'https://github.com/kirodotdev/kirocrew/pull/2', 2)
    const c = link('github', 'https://github.com/acme/widgets/pull/3', 3)
    const qualify = sourceTabQualifier([a, b, c])
    expect(qualify(c)).toBe('acme/widgets')
    expect(qualify(a)?.toLowerCase()).toBe('kirodotdev/kirocrew')
    expect(qualify(b)?.toLowerCase()).toBe('kirodotdev/kirocrew')
  })

  it('keeps GitLab paths that differ only in case as two projects', () => {
    const a = link('gitlab', 'https://gitlab.com/Group/Project/-/merge_requests/1', 1)
    const b = link('gitlab', 'https://gitlab.com/group/project/-/merge_requests/2', 2)
    const qualify = sourceTabQualifier([a, b])
    expect(qualify(a)).toBe('Group/Project')
    expect(qualify(b)).toBe('group/project')
  })

  it('names the host when a GitHub and a GitLab project share one path', () => {
    const a = link('github', 'https://github.com/Acme/Widgets/pull/1', 1)
    const b = link('gitlab', 'https://gitlab.com/Acme/Widgets/-/merge_requests/2', 2)
    const qualify = sourceTabQualifier([a, b])
    expect(qualify(a)).not.toBe(qualify(b))
  })

  it('keeps the host when the shared path differs only in case', () => {
    const a = link('github', 'https://github.com/Acme/Widgets/pull/1', 1)
    const b = link('gitlab', 'https://gitlab.com/acme/widgets/-/merge_requests/2', 2)
    const qualify = sourceTabQualifier([a, b])
    expect(qualify(a)).toContain('github.com')
    expect(qualify(b)).toContain('gitlab.com')
  })
})
