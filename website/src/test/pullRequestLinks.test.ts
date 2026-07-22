import { describe, expect, it } from 'vitest'
import type { ChatMessage } from '../types'
import {
  extractPullRequestLinks,
  loadSeenPullRequestLinks,
  MAX_PULL_REQUEST_SOURCES,
  persistSeenPullRequestLinks,
  PullRequestLinkIndex,
  recordNewPullRequestLinks,
} from '../utils/pullRequestLinks'

const messages = (...content: string[]): ChatMessage[] => content.map(text => ({ role: 'user', content: text, cls: '' }))

describe('extractPullRequestLinks', () => {
  it('extracts and deduplicates GitHub pull requests in first-seen order', () => {
    const result = extractPullRequestLinks(messages(
      'Review https://github.com/acme/widgets/pull/12.',
      '[same PR](https://github.com/acme/widgets/pull/12) and https://github.com/acme/widgets/pull/14?tab=checks',
    ))
    expect(result).toEqual([
      { url: 'https://github.com/acme/widgets/pull/12', provider: 'github', number: 12, repo: 'widgets' },
      { url: 'https://github.com/acme/widgets/pull/14', provider: 'github', number: 14, repo: 'widgets' },
    ])
  })

  it('extracts nested GitLab merge request paths', () => {
    expect(extractPullRequestLinks(messages(
      'See https://gitlab.com/acme/platform/service/-/merge_requests/42!',
    ))).toEqual([
      { url: 'https://gitlab.com/acme/platform/service/-/merge_requests/42', provider: 'gitlab', number: 42, repo: 'service' },
    ])
  })

  it('does not treat lookalike hosts as providers', () => {
    expect(extractPullRequestLinks(messages(
      'https://github.com.evil.example/acme/widgets/pull/12 and https://example.com/github.com/acme/widgets/pull/13',
    ))).toEqual([])
  })

  it('detects URLs wrapped in markdown emphasis (regression: trailing ** broke the numeric tail)', () => {
    const url = 'https://github.com/acme/widgets/pull/166'
    for (const wrapped of [`**${url}**`, `*${url}*`, `\`${url}\``, `__${url}__`, `~~${url}~~`]) {
      expect(extractPullRequestLinks(messages(`PR is up: ${wrapped} — fix(tips)`))).toEqual([
        { url, provider: 'github', number: 166, repo: 'widgets' },
      ])
    }
    // GitLab MRs get the same trim
    expect(extractPullRequestLinks(messages(
      'MR: **https://gitlab.com/acme/platform/-/merge_requests/42**',
    ))).toEqual([
      { url: 'https://gitlab.com/acme/platform/-/merge_requests/42', provider: 'gitlab', number: 42, repo: 'platform' },
    ])
  })

  it.each([
    ['streaming', 'assistant'],
    ['chunk', 'user'],
  ])('defers digit-by-digit %s PR numbers until the message finalizes to %s', (
    transientRole,
    finalRole,
  ) => {
    const index = new PullRequestLinkIndex()
    const seen = new Map<string, Set<string>>()
    const prefix = 'Review https://github.com/acme/widgets/pull/'

    for (const number of ['1', '12', '123']) {
      const links = index.update('slot-a', [
        { role: transientRole, content: `${prefix}${number}`, cls: '' },
      ])
      expect(links).toEqual([])
      expect(recordNewPullRequestLinks(seen, 'slot-a', links)).toBe(false)
    }

    const finalized = index.update('slot-a', [
      { role: finalRole, content: `${prefix}123`, cls: '' },
    ])
    expect(finalized.map(link => link.url)).toEqual([
      'https://github.com/acme/widgets/pull/123',
    ])
    expect(recordNewPullRequestLinks(seen, 'slot-a', finalized)).toBe(true)
  })

  it.each([
    ['whitespace', 'streaming', ' '],
    ['punctuation', 'streaming', '.'],
    ['closing markdown delimiter', 'chunk', ')'],
  ])('keeps a transient URL hidden after an explicit %s', (_label, role, delimiter) => {
    const index = new PullRequestLinkIndex()
    const content = `https://github.com/acme/widgets/pull/123${delimiter}`
    expect(index.update('slot-a', [{ role, content, cls: '' }])).toEqual([])

    const finalized = index.update('slot-a', [{ role: 'assistant', content, cls: '' }])
    expect(finalized.map(link => link.url)).toEqual([
      'https://github.com/acme/widgets/pull/123',
    ])
  })

  it('rescans only the changing tail during streaming', () => {
    let historicalReads = 0
    const historical = {
      role: 'user',
      cls: '',
      get content() {
        historicalReads += 1
        return 'Review https://github.com/acme/widgets/pull/12'
      },
    } as ChatMessage
    const index = new PullRequestLinkIndex()

    index.update('slot-a', [historical, { role: 'streaming', content: 'working', cls: '' }])
    const links = index.update('slot-a', [
      historical,
      { role: 'streaming', content: 'working https://gitlab.com/acme/api/-/merge_requests/7 ', cls: '' },
    ])

    expect(historicalReads).toBe(1)
    expect(links.map(link => link.url)).toEqual([
      'https://github.com/acme/widgets/pull/12',
    ])

    const finalized = index.update('slot-a', [
      historical,
      { role: 'assistant', content: 'working https://gitlab.com/acme/api/-/merge_requests/7 ', cls: '' },
    ])
    expect(finalized.map(link => link.url)).toEqual([
      'https://github.com/acme/widgets/pull/12',
      'https://gitlab.com/acme/api/-/merge_requests/7',
    ])
  })

  it('does not settle an earlier stream when tool or stop events append', () => {
    const index = new PullRequestLinkIndex()
    const thinking = { role: 'thinking', content: 'checking', cls: '' } as ChatMessage
    const stop = { role: 'stop', content: '', cls: '' } as ChatMessage
    const prefix = 'https://github.com/acme/widgets/pull/'
    const firstStream = { role: 'streaming', content: `${prefix}1`, cls: '' } as ChatMessage

    expect(index.update('slot-a', [firstStream])).toEqual([])
    expect(index.update('slot-a', [firstStream, thinking])).toEqual([])

    const extendedStream = { role: 'streaming', content: `${prefix}123`, cls: '' } as ChatMessage
    expect(index.update('slot-a', [extendedStream, thinking])).toEqual([])
    expect(index.update('slot-a', [extendedStream, thinking, stop])).toEqual([])

    const finalized = { role: 'assistant', content: `${prefix}123`, cls: '' } as ChatMessage
    expect(index.update('slot-a', [finalized, thinking, stop]).map(link => link.url)).toEqual([
      'https://github.com/acme/widgets/pull/123',
    ])
  })

  it('rebuilds after a non-tail message edit', () => {
    const index = new PullRequestLinkIndex()
    const middle = { role: 'assistant', content: 'middle', cls: '' } as ChatMessage

    index.update('slot-a', [
      { role: 'user', content: 'https://github.com/acme/widgets/pull/12', cls: '' },
      middle,
      { role: 'streaming', content: 'working', cls: '' },
    ])
    const links = index.update('slot-a', [
      { role: 'user', content: 'https://github.com/acme/widgets/pull/14', cls: '' },
      middle,
      { role: 'streaming', content: 'still working', cls: '' },
    ])

    expect(links.map(link => link.url)).toEqual([
      'https://github.com/acme/widgets/pull/14',
    ])
  })

  it('retains only the first capped sources across extraction, indexing, and seen state', () => {
    const many = Array.from({ length: MAX_PULL_REQUEST_SOURCES + 20 }, (_, index) => ({
      role: 'assistant',
      content: `https://github.com/acme/widgets/pull/${index + 1}`,
      cls: '',
    } as ChatMessage))
    const extracted = extractPullRequestLinks(many)
    const indexed = new PullRequestLinkIndex().update('slot-a', many)
    const seen = new Map<string, Set<string>>()

    expect(extracted).toHaveLength(MAX_PULL_REQUEST_SOURCES)
    expect(indexed).toEqual(extracted)
    expect(extracted.at(-1)?.number).toBe(MAX_PULL_REQUEST_SOURCES)
    expect(recordNewPullRequestLinks(seen, 'slot-a', extracted)).toBe(true)
    expect(recordNewPullRequestLinks(seen, 'slot-a', [{
      url: 'https://github.com/acme/widgets/pull/999',
      provider: 'github',
      number: 999,
      repo: 'widgets',
    }])).toBe(false)
    expect(seen.get('slot-a')?.size).toBe(MAX_PULL_REQUEST_SOURCES)
  })

  it('keeps seen links per slot when switching away and back', () => {
    const seen = new Map<string, Set<string>>()
    const first = extractPullRequestLinks(messages('https://github.com/acme/widgets/pull/12'))
    const second = extractPullRequestLinks(messages('https://github.com/acme/widgets/pull/14'))

    expect(recordNewPullRequestLinks(seen, 'slot-a', first)).toBe(true)
    expect(recordNewPullRequestLinks(seen, 'slot-b', second)).toBe(true)
    expect(recordNewPullRequestLinks(seen, 'slot-a', first)).toBe(false)
    expect(recordNewPullRequestLinks(seen, 'slot-a', [...first, ...second])).toBe(true)
  })

  it('restores seen links after remount without rediscovering historical sources', () => {
    localStorage.clear()
    const first = extractPullRequestLinks(messages('https://github.com/acme/widgets/pull/12'))
    const second = extractPullRequestLinks(messages('https://gitlab.com/acme/api/-/merge_requests/7'))

    const mounted = loadSeenPullRequestLinks()
    expect(recordNewPullRequestLinks(mounted, 'slot-a', first)).toBe(true)
    expect(persistSeenPullRequestLinks(mounted)).toBe(true)

    const remounted = loadSeenPullRequestLinks()
    expect(recordNewPullRequestLinks(remounted, 'slot-a', first)).toBe(false)
    expect(recordNewPullRequestLinks(remounted, 'slot-a', second)).toBe(true)
    localStorage.clear()
  })

  it('fails closed when persisted seen-source state is malformed', () => {
    localStorage.setItem('mc-pr-source-seen-v1', '{not-json')
    expect(loadSeenPullRequestLinks()).toEqual(new Map())
    localStorage.setItem('mc-pr-source-seen-v1', JSON.stringify({ slot: ['not-an-array'] }))
    expect(loadSeenPullRequestLinks()).toEqual(new Map())
    localStorage.clear()
  })
})
