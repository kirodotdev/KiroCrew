/**
 * Issue-url extraction: the `kind` discriminator, host allowlisting for
 * self-managed GitLab, and the interaction with first-mention attribution.
 *
 * These live beside pullRequestLinks.test.ts rather than inside it because they
 * exercise the SECOND kind the (deliberately shared) extractor now emits — one
 * scan, one dedup map, two panels.
 */
import { describe, expect, it } from 'vitest'
import type { ChatMessage } from '../types'
import {
  extractPullRequestLinks,
  partitionSourceLinks,
  PullRequestLinkIndex,
} from '../utils/pullRequestLinks'

/** Agent-authored by default: under first-mention attribution only
 *  agent-surfaced links reach the rich panels. */
const messages = (...content: string[]): ChatMessage[] =>
  content.map(text => ({ role: 'assistant', content: text, cls: '' }))

const userMessages = (...content: string[]): ChatMessage[] =>
  content.map(text => ({ role: 'user', content: text, cls: '' }))

describe('issue link extraction', () => {
  it('derives kind from the GitHub path segment', () => {
    expect(extractPullRequestLinks(messages(
      'Filed https://github.com/acme/widgets/issues/9 for the crash; fix in https://github.com/acme/widgets/pull/12.',
    ))).toEqual([
      { url: 'https://github.com/acme/widgets/issues/9', provider: 'github', number: 9, repo: 'widgets', kind: 'issue' },
      { url: 'https://github.com/acme/widgets/pull/12', provider: 'github', number: 12, repo: 'widgets', kind: 'change' },
    ])
  })

  it('derives kind from the GitLab path marker, including nested groups', () => {
    expect(extractPullRequestLinks(messages(
      'See https://gitlab.com/acme/platform/service/-/issues/42 and https://gitlab.com/acme/platform/service/-/merge_requests/43.',
    ))).toEqual([
      { url: 'https://gitlab.com/acme/platform/service/-/issues/42', provider: 'gitlab', number: 42, repo: 'service', kind: 'issue' },
      { url: 'https://gitlab.com/acme/platform/service/-/merge_requests/43', provider: 'gitlab', number: 43, repo: 'service', kind: 'change' },
    ])
  })

  it('canonicalizes an issue url with a comment fragment and query', () => {
    expect(extractPullRequestLinks(messages(
      'Context: https://github.com/acme/widgets/issues/9#issuecomment-77',
    ))).toEqual([
      { url: 'https://github.com/acme/widgets/issues/9', provider: 'github', number: 9, repo: 'widgets', kind: 'issue' },
    ])
  })

  it('rejects GitHub paths that are neither pull nor issues', () => {
    expect(extractPullRequestLinks(messages(
      'https://github.com/acme/widgets/discussions/9 and https://github.com/acme/widgets/releases/9',
    ))).toEqual([])
  })

  describe('self-managed GitLab hosts', () => {
    const issue = 'https://gitlab.acme.internal/team/platform/api/-/issues/7'

    it('ignores a self-managed issue when no host is allowlisted', () => {
      expect(extractPullRequestLinks(messages(`Filed ${issue}`))).toEqual([])
    })

    it('extracts a self-managed issue when its host is allowlisted', () => {
      expect(extractPullRequestLinks(messages(`Filed ${issue}`), ['gitlab.acme.internal'])).toEqual([
        { url: issue, provider: 'gitlab', number: 7, repo: 'api', kind: 'issue' },
      ])
    })

    it('does not accept a lookalike host that merely contains an allowlisted one', () => {
      const lookalike = 'https://gitlab.acme.internal.evil.example/team/api/-/issues/7'
      expect(extractPullRequestLinks(messages(lookalike), ['gitlab.acme.internal'])).toEqual([])
    })
  })

  it('keeps a user-pasted GitHub issue OUT of the rich surface (first-mention attribution)', () => {
    const issue = 'https://github.com/acme/widgets/issues/9'
    // GitHub/GitLab issues follow the same agent-first rule as PRs.
    // User-pasted issues are context, not a panel source.
    expect(extractPullRequestLinks([
      ...userMessages(`Look at ${issue}`),
      ...messages(`Reading ${issue} now`),
    ])).toEqual([])
    // Positive control: the same issue mentioned by the agent FIRST is emitted.
    expect(extractPullRequestLinks(messages(`Reading ${issue}`))).toEqual([
      { url: issue, provider: 'github', number: 9, repo: 'widgets', kind: 'issue' },
    ])
  })

  it('does not emit an issue while the streaming tail may still be extending it', () => {
    const index = new PullRequestLinkIndex()
    const settled: ChatMessage = { role: 'assistant', content: 'working', cls: '' }
    const streaming: ChatMessage = { role: 'streaming', content: 'https://github.com/acme/widgets/issues/1', cls: '' }
    expect(index.update('slot', [settled, streaming])).toEqual([])
    const done: ChatMessage = { role: 'assistant', content: 'https://github.com/acme/widgets/issues/12', cls: '' }
    expect(index.update('slot', [settled, done])).toEqual([
      { url: 'https://github.com/acme/widgets/issues/12', provider: 'github', number: 12, repo: 'widgets', kind: 'issue' },
    ])
  })
})

describe('partitionSourceLinks', () => {
  it('splits by kind while preserving first-seen order within each half', () => {
    const links = extractPullRequestLinks(messages([
      'https://github.com/acme/widgets/issues/9',
      'https://github.com/acme/widgets/pull/12',
      'https://github.com/acme/widgets/issues/10',
    ].join('\n')))
    const { changes, issues } = partitionSourceLinks(links)
    expect(changes.map(l => l.number)).toEqual([12])
    expect(issues.map(l => l.number)).toEqual([9, 10])
  })

  it('returns two empty halves for an empty input', () => {
    expect(partitionSourceLinks([])).toEqual({ changes: [], issues: [] })
  })
})

describe('kind derivation is not reachable via inherited keys', () => {
  // An object-literal lookup (`GITHUB_SEGMENT_KIND[segment]`) also resolves
  // keys inherited from Object.prototype, so these paths would yield a link
  // whose kind is neither 'change' nor 'issue' -- filed under Changes, then
  // rejected by the backend with a 400.
  it.each(['constructor', 'toString', 'valueOf', 'hasOwnProperty', '__proto__'])(
    'drops /owner/repo/%s/5 instead of emitting an unknown kind',
    segment => {
      const links = extractPullRequestLinks(
        messages(`https://github.com/acme/widgets/${segment}/5`),
      )
      expect(links).toEqual([])
    },
  )

  it('still accepts the two real segments', () => {
    const links = extractPullRequestLinks(messages([
      'https://github.com/acme/widgets/pull/12',
      'https://github.com/acme/widgets/issues/9',
    ].join('\n')))
    expect(links.map(l => l.kind)).toEqual(['change', 'issue'])
  })
})
