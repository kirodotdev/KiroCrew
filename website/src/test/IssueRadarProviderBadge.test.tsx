/** Provider identity in the UI: the badge, the host chip, and MR-vs-PR wording.
 *
 * These cover the class of bug that has no error and no crash — just wrong
 * information on screen. A GitLab project under a GitHub mark, two different
 * projects rendering identically because the host is invisible, or a GitLab
 * workspace calling its merge requests "Pull Requests".
 */

import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'

import {
  ProviderLogo,
  ProviderHostTag,
  hasCustomHost,
} from '../apps/issue-radar/components/ProviderBadge'
import { providerTerms } from '../apps/issue-radar/lib/links'
import { parseRepoRef } from '../apps/issue-radar/ConnectPanel'

const GH = { owner: 'acme', repo: 'widget', provider: 'github' as const, host: 'github.com' }
const GL = { owner: 'group/sub', repo: 'proj', provider: 'gitlab' as const, host: 'gitlab.com' }
const SELF = { owner: 'team', repo: 'svc', provider: 'gitlab' as const, host: 'gitlab.acme.internal' }
/** A record persisted before GitLab support: no provider, no host. */
const LEGACY = { owner: 'acme', repo: 'widget' }

function markOf(container: HTMLElement): string | null {
  return container.querySelector('[data-provider-mark]')?.getAttribute('data-provider-mark') ?? null
}

describe('ProviderLogo', () => {
  it('renders each provider’s own mark', () => {
    const { container: gh } = render(<ProviderLogo repoRef={GH} />)
    expect(markOf(gh)).toBe('github')
    const { container: gl } = render(<ProviderLogo repoRef={GL} />)
    expect(markOf(gl)).toBe('gitlab')
  })

  it('treats a legacy record as GitHub rather than rendering nothing', () => {
    const { container } = render(<ProviderLogo repoRef={LEGACY} />)
    expect(markOf(container)).toBe('github')
  })

  it('survives an absent ref instead of crashing the pane', () => {
    // Components can render before the active repo resolves; a thrown error there
    // takes the whole workspace down.
    const { container } = render(<ProviderLogo />)
    expect(markOf(container)).toBe('github')
  })

  it('names the provider for assistive tech, since the mark itself is decorative', () => {
    render(<ProviderLogo repoRef={GL} />)
    expect(screen.getByRole('img', { name: 'GitLab' })).toBeInTheDocument()
  })
})

describe('ProviderHostTag', () => {
  it('is silent for the public hosts, where it would be noise', () => {
    const { container: gh } = render(<ProviderHostTag repoRef={GH} />)
    expect(gh).toBeEmptyDOMElement()
    const { container: gl } = render(<ProviderHostTag repoRef={GL} />)
    expect(gl).toBeEmptyDOMElement()
  })

  it('names a self-managed instance, which is otherwise indistinguishable', () => {
    render(<ProviderHostTag repoRef={SELF} />)
    expect(screen.getByText('gitlab.acme.internal')).toBeInTheDocument()
  })

  it('classifies hosts case-insensitively and tolerates an absent ref', () => {
    expect(hasCustomHost({ host: 'GitLab.com' })).toBe(false)
    expect(hasCustomHost({})).toBe(false)
    expect(hasCustomHost()).toBe(false)
    expect(hasCustomHost({ host: 'gitlab.acme.internal:8443' })).toBe(true)
  })
})

describe('provider vocabulary', () => {
  it('supplies every casing the UI needs, spelled out', () => {
    // Derived casings are how "Merge requestss" happens.
    const gl = providerTerms(GL)
    expect(gl.changeRequest).toBe('merge request')
    expect(gl.changeRequestTitle).toBe('Merge Request')
    expect(gl.changeRequestPlural).toBe('merge requests')
    expect(gl.changeRequestPluralTitle).toBe('Merge Requests')
    const gh = providerTerms(GH)
    expect(gh.changeRequestPluralTitle).toBe('Pull Requests')
  })

  it('falls back to GitHub for an absent ref', () => {
    expect(providerTerms().changeRequestPluralTitle).toBe('Pull Requests')
  })
})

describe('parseRepoRef — connect-dialog shorthand', () => {
  it('keeps a GitLab nested namespace whole', () => {
    // Truncating to the first segment would connect a different project.
    expect(parseRepoRef('https://gitlab.com/group/sub/proj', 'gitlab'))
      .toEqual({ owner: 'group/sub', repo: 'proj' })
  })

  it('resolves a hostless shorthand against the SELECTED provider', () => {
    expect(parseRepoRef('group/proj', 'gitlab')).toEqual({ owner: 'group', repo: 'proj' })
    expect(parseRepoRef('owner/repo', 'github')).toEqual({ owner: 'owner', repo: 'repo' })
  })

  it('strips a pasted GitLab page path down to the project', () => {
    for (const suffix of ['/-/issues', '/-/merge_requests/7', '/-/tree/main']) {
      expect(parseRepoRef(`https://gitlab.com/group/proj${suffix}`, 'gitlab'))
        .toEqual({ owner: 'group', repo: 'proj' })
    }
  })

  it('refuses the other provider’s host for the selected provider', () => {
    expect(parseRepoRef('https://github.com/o/r', 'gitlab')).toBeNull()
    expect(parseRepoRef('https://gitlab.com/g/p', 'github')).toBeNull()
  })

  it('does not shorthand-parse a self-managed host', () => {
    // The client cannot see the operator's allowlist, so guessing would build a
    // canonical URL the server then rejects. Returning null submits the text
    // verbatim and lets the server's allowlist give the honest answer.
    expect(parseRepoRef('https://gitlab.acme.internal/g/p', 'gitlab')).toBeNull()
  })

  it('still drops a .git suffix and trailing slashes', () => {
    expect(parseRepoRef('https://gitlab.com/g/p.git/', 'gitlab'))
      .toEqual({ owner: 'g', repo: 'p' })
  })
})
