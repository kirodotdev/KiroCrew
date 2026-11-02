// GitHub Enterprise repos are first-class in Code Review Sage: a repo's identity
// carries its host, so the same owner/repo on github.com and on an Enterprise
// instance stay two repos in the picker, the pinned list and the Reviews tab.
import { describe, expect, it } from 'vitest'

import { repoOfRun, sageSourceLink } from './lib/format'
import {
  canonicalHost, isEnterpriseHost, repoInputToUrl, repoSlug, repoUrl, sameRepo,
  slugFromUrl,
} from './lib/repoRef'

describe('repo identity across hosts', () => {
  it('keeps github.com slugs bare and qualifies Enterprise slugs', () => {
    expect(repoSlug({ owner: 'o', repo: 'r' })).toBe('o/r')
    expect(repoSlug({ owner: 'o', repo: 'r', host: 'www.github.com' })).toBe('o/r')
    expect(repoSlug({ owner: 'o', repo: 'r', host: 'ACME.ghe.com' })).toBe('acme.ghe.com/o/r')
  })

  it('builds the repo URL on the repo host', () => {
    expect(repoUrl({ owner: 'o', repo: 'r' })).toBe('https://github.com/o/r')
    expect(repoUrl({ owner: 'o', repo: 'r', host: 'acme.ghe.com' })).toBe('https://acme.ghe.com/o/r')
  })

  it('treats the same slug on two hosts as two repos', () => {
    expect(sameRepo({ owner: 'o', repo: 'r' }, { owner: 'O', repo: 'R', host: 'github.com' })).toBe(true)
    expect(sameRepo({ owner: 'o', repo: 'r' }, { owner: 'o', repo: 'r', host: 'acme.ghe.com' })).toBe(false)
    expect(sameRepo(null, { owner: 'o', repo: 'r' })).toBe(false)
  })

  it('canonicalizes hosts', () => {
    expect(canonicalHost(undefined)).toBe('github.com')
    expect(canonicalHost('www.github.com')).toBe('github.com')
    expect(isEnterpriseHost('github.com')).toBe(false)
    expect(isEnterpriseHost('acme.ghe.com')).toBe(true)
  })

  it('turns typed input into a URL on the right host', () => {
    expect(repoInputToUrl('o/r')).toBe('https://github.com/o/r')
    expect(repoInputToUrl('/o/r/')).toBe('https://github.com/o/r')
    expect(repoInputToUrl('acme.ghe.com/o/r')).toBe('https://acme.ghe.com/o/r')
    expect(repoInputToUrl('https://acme.ghe.com/o/r/pull/3')).toBe('https://acme.ghe.com/o/r/pull/3')
  })

  it('derives the slug from a change URL', () => {
    expect(slugFromUrl('https://github.com/O/R/pull/1')).toBe('o/r')
    expect(slugFromUrl('https://acme.ghe.com/o/r/pull/1')).toBe('acme.ghe.com/o/r')
    expect(slugFromUrl('not a url')).toBeNull()
    expect(slugFromUrl('https://acme.ghe.com/only')).toBeNull()
  })
})

describe('sageSourceLink on Enterprise hosts', () => {
  it('builds a provider link for a GHE pull request', () => {
    expect(sageSourceLink('https://acme.ghe.com/o/r/pull/79')).toMatchObject({
      provider: 'github', number: 79, repo: 'o/r', kind: 'change',
      url: 'https://acme.ghe.com/o/r/pull/79',
    })
  })

  it('still refuses non pull request shapes', () => {
    expect(sageSourceLink('https://acme.ghe.com/o/r/issues/79')).toBeNull()
    expect(sageSourceLink('http://github.com/o/r/pull/1')).toBeNull()
  })
})

describe('repoOfRun on Enterprise hosts', () => {
  it('attributes a pasted GHE PR run to its host-qualified repo', () => {
    expect(repoOfRun({ changes: ['https://acme.ghe.com/o/r/pull/9'] })).toBe('acme.ghe.com/o/r')
  })

  it('keeps github.com and gitlab.com runs on the bare slug', () => {
    expect(repoOfRun({ changes: ['https://github.com/o/r/pull/9'] })).toBe('o/r')
    expect(repoOfRun({ changes: ['https://gitlab.com/o/r/-/merge_requests/2'] })).toBe('o/r')
  })

  it('uses the stored repo of a repo-wide run, host included', () => {
    expect(repoOfRun({ repo: 'acme.ghe.com/o/r', changes: [] })).toBe('acme.ghe.com/o/r')
  })
})
