/**
 * Source-provider registry seam (frontend).
 *
 * Covers the two things a downstream edition needs to hold: that a registered
 * provider's URLs are extracted, labelled and persisted like a built-in, and
 * that the three built-ins are untouched by the seam existing.
 */
import { afterEach, describe, expect, it } from 'vitest'
import type { ChatMessage } from '../types'
import {
  commitRevealedSource,
  commitSourceSelection,
  extractPullRequestLinks,
  findSourceForUrl,
  forgeChipLabel,
  loadRevealedSources,
  loadSourceSelections,
  parseSourceLinkUrl,
  PullRequestLinkIndex,
  registerSourceProvider,
  resetSourceProvidersForTests,
  sourceLinkKey,
  sourceProviderDescriptor,
  type PullRequestLink,
  type SourceProviderDescriptor,
} from '../utils/pullRequestLinks'
import { sourceProviderMeta } from '../utils/sourceProviderMeta'

const CR_URL = 'https://review.acme.example/cr/123'

/** A stand-in for an enterprise edition's internal code-review system: its own
 *  host, its own `CR-123` reference grammar, and only some capabilities. */
const acmeProvider: SourceProviderDescriptor = {
  id: 'acme',
  displayName: 'Acme Review',
  parse(url) {
    if (url.hostname !== 'review.acme.example') return null
    const match = /^\/cr\/(\d+)$/.exec(url.pathname.replace(/\/+$/, ''))
    if (!match) return null
    return {
      url: `https://review.acme.example/cr/${match[1]}`,
      provider: 'acme',
      number: Number(match[1]),
      repo: 'acme',
      kind: 'change',
    }
  },
  chipLabel: link => `CR-${link.number}`,
  refLabel: n => `CR-${n}`,
  capabilities: { checks: false, mergeState: false, resolveThreads: true, comment: true },
}

const assistant = (...content: string[]): ChatMessage[] =>
  content.map(text => ({ role: 'assistant', content: text, cls: '' }))

afterEach(() => {
  resetSourceProvidersForTests()
  localStorage.clear()
})

describe('registerSourceProvider', () => {
  it('registers a provider and exposes its descriptor', () => {
    registerSourceProvider(acmeProvider)
    expect(sourceProviderDescriptor('acme')).toBe(acmeProvider)
  })

  it("drops a descriptor link with kind 'issue' — the issue pipeline is built-in-only", () => {
    registerSourceProvider({
      ...acmeProvider,
      parse: url => {
        const link = acmeProvider.parse(url)
        return link ? { ...link, kind: 'issue' } : null
      },
    })
    expect(parseSourceLinkUrl(CR_URL)).toBeNull()
  })

  it('refuses a built-in id, a duplicate, and a malformed id', () => {
    registerSourceProvider(acmeProvider)
    // reportSeamCollision throws in dev/test builds.
    expect(() => registerSourceProvider({ ...acmeProvider, id: 'github' })).toThrow(/built-in/)
    expect(() => registerSourceProvider(acmeProvider)).toThrow(/already registered/)
    expect(() => registerSourceProvider({ ...acmeProvider, id: 'Acme Review' })).toThrow(/lowercase/)
  })
})

describe('extraction through the registry', () => {
  it('extracts a registered provider url from a transcript', () => {
    registerSourceProvider(acmeProvider)
    expect(extractPullRequestLinks(assistant(`Raised ${CR_URL} for review.`))).toEqual([
      { url: CR_URL, provider: 'acme', number: 123, repo: 'acme', kind: 'change' },
    ])
  })

  it('yields nothing for that url when no provider is registered', () => {
    expect(extractPullRequestLinks(assistant(`Raised ${CR_URL}.`))).toEqual([])
    expect(parseSourceLinkUrl(CR_URL)).toBeNull()
  })

  it('parses the url for the sidebar chip path', () => {
    registerSourceProvider(acmeProvider)
    const link = parseSourceLinkUrl(CR_URL)
    expect(link).toEqual({
      url: CR_URL, provider: 'acme', number: 123, repo: 'acme', kind: 'change',
    })
  })

  it('rejects a link whose provider does not match its descriptor', () => {
    registerSourceProvider({
      ...acmeProvider,
      id: 'liar',
      parse: () => ({
        url: CR_URL, provider: 'acme', number: 1, repo: 'x', kind: 'change',
      } as PullRequestLink),
    })
    expect(parseSourceLinkUrl(CR_URL)).toBeNull()
  })

  it('skips a descriptor that throws instead of breaking the whole scan', () => {
    registerSourceProvider({ ...acmeProvider, id: 'boom', parse: () => { throw new Error('x') } })
    expect(extractPullRequestLinks(assistant(
      `${CR_URL} and https://github.com/acme/widgets/pull/12`,
    ))).toEqual([
      { url: 'https://github.com/acme/widgets/pull/12', provider: 'github', number: 12, repo: 'widgets', kind: 'change' },
    ])
  })

  it('hands each descriptor a fresh URL — one mutating it cannot blind the next', () => {
    // URL is mutable, so without a per-descriptor copy this first descriptor's
    // pathname rewrite would be what the acme parser (registered after it)
    // receives, and the real match would silently never happen.
    registerSourceProvider({
      ...acmeProvider,
      id: 'vandal',
      parse(url) {
        url.pathname = '/defaced'
        return null
      },
    })
    registerSourceProvider(acmeProvider)
    expect(parseSourceLinkUrl(CR_URL)?.provider).toBe('acme')
  })
})

/** A provider that keeps an optional revision pin in its canonical url, the
 *  way an internal review system names one revision of a review. */
const revisionPinnedProvider: SourceProviderDescriptor = {
  ...acmeProvider,
  parse(url) {
    if (url.hostname !== 'review.acme.example') return null
    const match = /^\/cr\/(\d+)(\/revisions\/\d+)?$/.exec(url.pathname.replace(/\/+$/, ''))
    if (!match) return null
    return {
      url: `https://review.acme.example/cr/${match[1]}${match[2] ?? ''}`,
      provider: 'acme',
      number: Number(match[1]),
      repo: 'acme',
      kind: 'change',
    }
  },
}

describe('one change mentioned at several revisions', () => {
  // Revision 1 is OPEN and revision 2 is SHIPPED. The panel fetches the url it
  // lists, so listing revision 2 is what makes the card read Merged.
  const REV_1 = `${CR_URL}/revisions/1`
  const REV_2 = `${CR_URL}/revisions/2`
  const OTHER_PR = 'https://github.com/acme/widgets/pull/12'
  const cr = (url: string): PullRequestLink =>
    ({ url, provider: 'acme', number: 123, repo: 'acme', kind: 'change' })

  it('lists the review once, at its newest revision, in its first place', () => {
    registerSourceProvider(revisionPinnedProvider)
    expect(extractPullRequestLinks(assistant(
      `Published ${REV_1}`,
      `Also opened ${OTHER_PR}`,
      `Published ${REV_2}, now shipped`,
    ))).toEqual([
      cr(REV_2),
      { url: OTHER_PR, provider: 'github', number: 12, repo: 'widgets', kind: 'change' },
    ])
  })

  it('follows the newest revision within one message too', () => {
    registerSourceProvider(revisionPinnedProvider)
    expect(extractPullRequestLinks(assistant(`${REV_1} is superseded by ${REV_2}`)))
      .toEqual([cr(REV_2)])
  })

  it('moves to the new revision when it is appended to a live transcript', () => {
    registerSourceProvider(revisionPinnedProvider)
    const index = new PullRequestLinkIndex()
    const first = assistant(`Published ${REV_1}`, 'Waiting on review.')
    expect(index.update('slot-a', first)).toEqual([cr(REV_1)])
    const next = [...first, ...assistant(`Published ${REV_2}`), ...assistant('Merged.')]
    expect(index.update('slot-a', next)).toEqual([cr(REV_2)])
  })

  it('leaves a review mentioned at one revision exactly as it was', () => {
    registerSourceProvider(revisionPinnedProvider)
    expect(extractPullRequestLinks(assistant(`Published ${REV_1}`))).toEqual([cr(REV_1)])
    expect(extractPullRequestLinks(assistant(`Raised ${CR_URL}`))).toEqual([cr(CR_URL)])
  })

  it('keeps the first mention\'s attribution: a user-pasted review stays a Resource', () => {
    registerSourceProvider(revisionPinnedProvider)
    const messages: ChatMessage[] = [
      { role: 'user', content: `Context: ${REV_1}`, cls: '' },
      ...assistant(`Published ${REV_2}`),
    ]
    expect(extractPullRequestLinks(messages)).toEqual([])
  })

  it('maps a tab remembered at an older revision onto the listed one', () => {
    registerSourceProvider(revisionPinnedProvider)
    const listed = [cr(REV_2)]
    expect(findSourceForUrl(listed, REV_1)).toEqual(cr(REV_2))
    expect(findSourceForUrl(listed, REV_2)).toEqual(cr(REV_2))
    expect(findSourceForUrl(listed, 'https://review.acme.example/cr/124')).toBeUndefined()
    expect(findSourceForUrl(listed, '')).toBeUndefined()
  })

  it('keeps reviews with the same number on different hosts apart', () => {
    registerSourceProvider({
      ...revisionPinnedProvider,
      parse(url) {
        const match = /^\/cr\/(\d+)/.exec(url.pathname)
        if (!match || !url.hostname.endsWith('.acme.example')) return null
        return { url: url.href, provider: 'acme', number: Number(match[1]), repo: 'acme', kind: 'change' }
      },
    })
    expect(extractPullRequestLinks(assistant(
      'https://east.acme.example/cr/1 and https://west.acme.example/cr/1',
    ))).toHaveLength(2)
  })

  it('keys a built-in link on its url, so same-number pull requests stay apart', () => {
    registerSourceProvider(revisionPinnedProvider)
    const a = parseSourceLinkUrl('https://github.com/one/widgets/pull/5')!
    const b = parseSourceLinkUrl('https://github.com/two/widgets/pull/5')!
    expect(sourceLinkKey(a)).toBe(a.url)
    expect(sourceLinkKey(a)).not.toBe(sourceLinkKey(b))
  })
})

describe('chip labels', () => {
  it('labels a registered provider chip from its descriptor', () => {
    registerSourceProvider(acmeProvider)
    expect(forgeChipLabel(parseSourceLinkUrl(CR_URL)!)).toBe('CR-123')
  })

  it('leaves the built-in forge labels unchanged', () => {
    registerSourceProvider(acmeProvider)
    const label = (url: string) => forgeChipLabel(parseSourceLinkUrl(url)!)
    expect(label('https://github.com/o/r/pull/12')).toBe('o/r#12')
    expect(label('https://gitlab.com/g/sub/p/-/merge_requests/5')).toBe('g/sub/p!5')
    expect(label('https://acme.atlassian.net/browse/PROJ-9')).toBeNull()
  })
})

describe('reload canonicalization', () => {
  it('restores a revealed registered-provider link after a reload', () => {
    registerSourceProvider(acmeProvider)
    expect(commitRevealedSource('slot-a', 'change', CR_URL)).toBe(true)
    expect(loadRevealedSources()['slot-a'].change).toEqual(parseSourceLinkUrl(CR_URL))
  })

  it('restores the selected tab for a registered provider', () => {
    registerSourceProvider(acmeProvider)
    expect(commitSourceSelection('slot-a', 'change', CR_URL)).toBe('persisted')
    expect(loadSourceSelections()['slot-a'].change).toBe(CR_URL)
  })

  it('refuses to persist that url when no provider is registered', () => {
    expect(commitRevealedSource('slot-a', 'change', CR_URL)).toBe(false)
    expect(commitSourceSelection('slot-a', 'change', CR_URL)).toBe('unchanged')
  })
})

describe('sourceProviderMeta', () => {
  it('keeps the built-in panel presentation byte-identical', () => {
    const github = sourceProviderMeta('github')
    expect([github.displayName, github.refLabel(12), github.numberLabel(12), github.logo])
      .toEqual(['GitHub', 'PR #12', '#12', 'github'])
    expect(github.capabilities)
      .toEqual({ checks: true, mergeState: true, mergeActions: true, resolveThreads: true, comment: true })

    const gitlab = sourceProviderMeta('gitlab')
    expect([gitlab.displayName, gitlab.refLabel(5), gitlab.numberLabel(5), gitlab.logo])
      .toEqual(['GitLab', 'MR !5', '!5', 'gitlab'])
    expect(gitlab.capabilities)
      .toEqual({ checks: true, mergeState: true, mergeActions: true, resolveThreads: true, comment: true })
  })

  it('shows Gerrit the merge-state banner but no merge actions', () => {
    // Gerrit has no auto-merge: the conflict / rebase-required banner is real,
    // the "Enable auto-merge" and "Mark ready" buttons would only ever fail.
    const gerrit = sourceProviderMeta('gerrit')
    expect(gerrit.capabilities.mergeState).toBe(true)
    expect(gerrit.capabilities.mergeActions).toBe(false)
  })

  it('takes a registered provider label, name and capabilities', () => {
    registerSourceProvider(acmeProvider)
    const meta = sourceProviderMeta('acme')
    expect([meta.displayName, meta.refLabel(123), meta.numberLabel(123), meta.logo])
      .toEqual(['Acme Review', 'CR-123', 'CR-123', null])
    expect(meta.capabilities.checks).toBe(false)
    expect(meta.capabilities.resolveThreads).toBe(true)
    // A descriptor written before `mergeActions` existed meant it by `mergeState`.
    expect(meta.capabilities.mergeActions).toBe(meta.capabilities.mergeState)
  })

  it('fails closed for a provider nothing described', () => {
    const meta = sourceProviderMeta('mystery')
    expect(meta.refLabel(7)).toBe('#7')
    expect(meta.logo).toBeNull()
    expect(meta.capabilities)
      .toEqual({ checks: false, mergeState: false, mergeActions: false, resolveThreads: false, comment: false })
  })

  it('carries a descriptor icon component through to meta, and drops a non-function one', () => {
    const AcmeGlyph = () => null
    registerSourceProvider({ ...acmeProvider, icon: AcmeGlyph })
    expect(sourceProviderMeta('acme').icon).toBe(AcmeGlyph)
    resetSourceProvidersForTests()
    // A descriptor with no icon (and the fail-closed fallback) yields undefined —
    // the renderers read that as "draw the neutral glyph", never another brand.
    registerSourceProvider(acmeProvider)
    expect(sourceProviderMeta('acme').icon).toBeUndefined()
    expect(sourceProviderMeta('mystery').icon).toBeUndefined()
    resetSourceProvidersForTests()
    // A malformed icon value is dropped at the meta boundary rather than thrown
    // at render time, far from the descriptor that supplied it.
    registerSourceProvider({ ...acmeProvider, icon: 'not-a-component' as never })
    expect(sourceProviderMeta('acme').icon).toBeUndefined()
  })
})
