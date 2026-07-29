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

// Default to assistant-authored messages: under first-mention attribution only
// agent-surfaced PRs are Change sources, so tests asserting that a PR IS
// extracted use assistant content. User-referenced-only PRs are covered
// explicitly in the "first-mention attribution" describe block below.
const messages = (...content: string[]): ChatMessage[] => content.map(text => ({ role: 'assistant', content: text, cls: '' }))

describe('extractPullRequestLinks', () => {
  it('extracts and deduplicates GitHub pull requests in first-seen order', () => {
    const result = extractPullRequestLinks(messages(
      'Review https://github.com/acme/widgets/pull/12.',
      '[same PR](https://github.com/acme/widgets/pull/12) and https://github.com/acme/widgets/pull/14?tab=checks',
    ))
    expect(result).toEqual([
      { url: 'https://github.com/acme/widgets/pull/12', provider: 'github', number: 12, repo: 'widgets', kind: 'change' },
      { url: 'https://github.com/acme/widgets/pull/14', provider: 'github', number: 14, repo: 'widgets', kind: 'change' },
    ])
  })

  it('extracts nested GitLab merge request paths', () => {
    expect(extractPullRequestLinks(messages(
      'See https://gitlab.com/acme/platform/service/-/merge_requests/42!',
    ))).toEqual([
      { url: 'https://gitlab.com/acme/platform/service/-/merge_requests/42', provider: 'gitlab', number: 42, repo: 'service', kind: 'change' },
    ])
  })

  it('does not treat lookalike hosts as providers', () => {
    expect(extractPullRequestLinks(messages(
      'https://github.com.evil.example/acme/widgets/pull/12 and https://example.com/github.com/acme/widgets/pull/13',
    ))).toEqual([])
  })

  describe('self-hosted GitLab', () => {
    const mr = 'https://gitlab.acme.internal/team/platform/api/-/merge_requests/7'

    it('ignores a self-hosted MR when no host is allowlisted', () => {
      expect(extractPullRequestLinks(messages(`Opened ${mr}`))).toEqual([])
    })

    it('extracts a self-hosted MR when its host is allowlisted', () => {
      expect(extractPullRequestLinks(messages(`Opened ${mr}`), ['gitlab.acme.internal'])).toEqual([
        { url: mr, provider: 'gitlab', number: 7, repo: 'api', kind: 'change' },
      ])
    })

    it('requires the port to be allowlisted and matches hosts exactly', () => {
      const ported = 'https://gitlab.acme.internal:8443/team/api/-/merge_requests/9'
      expect(extractPullRequestLinks(messages(ported), ['gitlab.acme.internal'])).toEqual([])
      expect(extractPullRequestLinks(messages(ported), ['gitlab.acme.internal:8443'])).toEqual([
        { url: 'https://gitlab.acme.internal:8443/team/api/-/merge_requests/9', provider: 'gitlab', number: 9, repo: 'api', kind: 'change' },
      ])
      // Suffix and lookalike hosts stay unmatched.
      expect(extractPullRequestLinks(
        messages('https://evil-gitlab.acme.internal/a/b/-/merge_requests/1'),
        ['gitlab.acme.internal'],
      )).toEqual([])
      expect(extractPullRequestLinks(
        messages('https://gitlab.acme.internal.evil.test/a/b/-/merge_requests/1'),
        ['gitlab.acme.internal'],
      )).toEqual([])
    })

    it('accepts the absolute-FQDN form of an allowlisted host', () => {
      // Config entries are dot-normalized by the loader, so extraction must
      // normalize too or a dotted URL is silently dropped.
      expect(extractPullRequestLinks(
        messages('https://gitlab.acme.internal./team/api/-/merge_requests/7'),
        ['gitlab.acme.internal'],
      )).toEqual([
        { url: 'https://gitlab.acme.internal/team/api/-/merge_requests/7', provider: 'gitlab', number: 7, repo: 'api', kind: 'change' },
      ])
    })

    it('treats an explicit :443 entry and URL as the bare host', () => {
      // The URL API drops the default HTTPS port, and the backend now does too.
      expect(extractPullRequestLinks(
        messages('https://gitlab.acme.internal:443/team/api/-/merge_requests/7'),
        ['gitlab.acme.internal'],
      )).toEqual([
        { url: 'https://gitlab.acme.internal/team/api/-/merge_requests/7', provider: 'gitlab', number: 7, repo: 'api', kind: 'change' },
      ])
      expect(extractPullRequestLinks(messages(mr), ['gitlab.acme.internal:443'])).toEqual([
        { url: mr, provider: 'gitlab', number: 7, repo: 'api', kind: 'change' },
      ])
    })

    it('persists and restores self-hosted seen URLs regardless of the allowlist', () => {
      // The seen set is bookkeeping, not authorization: dropping self-hosted URLs
      // here made them look new after a reload and reopened the Changes panel.
      const seen = new Map([['slot-1', new Set([mr])]])
      expect(persistSeenPullRequestLinks(seen)).toBe(true)
      const restored = loadSeenPullRequestLinks()
      expect([...(restored.get('slot-1') ?? [])]).toEqual([mr])
    })

    it('rescans settled messages when the allowlist changes mid-session', () => {
      const index = new PullRequestLinkIndex()
      const history = messages(`Opened ${mr}`, 'still working')
      expect(index.update('slot-1', history)).toEqual([])
      expect(index.update('slot-1', history, ['gitlab.acme.internal'])).toEqual([
        { url: mr, provider: 'gitlab', number: 7, repo: 'api', kind: 'change' },
      ])
    })
  })

  it('detects URLs wrapped in markdown emphasis (regression: trailing ** broke the numeric tail)', () => {
    const url = 'https://github.com/acme/widgets/pull/166'
    for (const wrapped of [`**${url}**`, `*${url}*`, `\`${url}\``, `__${url}__`, `~~${url}~~`]) {
      expect(extractPullRequestLinks(messages(`PR is up: ${wrapped} — fix(tips)`))).toEqual([
        { url, provider: 'github', number: 166, repo: 'widgets', kind: 'change' },
      ])
    }
    // GitLab MRs get the same trim
    expect(extractPullRequestLinks(messages(
      'MR: **https://gitlab.com/acme/platform/-/merge_requests/42**',
    ))).toEqual([
      { url: 'https://gitlab.com/acme/platform/-/merge_requests/42', provider: 'gitlab', number: 42, repo: 'platform', kind: 'change' },
    ])
  })

  it.each([
    ['streaming'],
    ['chunk'],
  ])('defers digit-by-digit %s PR numbers until the message finalizes', (
    transientRole,
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

    // Finalizes to an agent (assistant) message, so the PR becomes a Change source.
    const finalized = index.update('slot-a', [
      { role: 'assistant', content: `${prefix}123`, cls: '' },
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
      role: 'assistant',
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
      { role: 'assistant', content: 'https://github.com/acme/widgets/pull/12', cls: '' },
      middle,
      { role: 'streaming', content: 'working', cls: '' },
    ])
    const links = index.update('slot-a', [
      { role: 'assistant', content: 'https://github.com/acme/widgets/pull/14', cls: '' },
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
      kind: 'change',
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

describe('first-mention attribution (Changes vs Resources)', () => {
  const url = 'https://github.com/acme/widgets/pull/12'

  it('excludes a PR only the user referenced (it belongs in Resources)', () => {
    expect(extractPullRequestLinks([
      { role: 'user', content: `please review ${url}`, cls: '' },
    ])).toEqual([])
  })

  it('includes a PR the agent surfaced', () => {
    expect(extractPullRequestLinks([
      { role: 'assistant', content: `opened ${url}`, cls: '' },
    ]).map(l => l.url)).toEqual([url])
  })

  it('keeps a user-first PR excluded even after the agent echoes it back', () => {
    // User pastes the PR first; the agent quoting it later must NOT reclassify
    // it as a Change — the earlier user mention owns the classification.
    expect(extractPullRequestLinks([
      { role: 'user', content: `look at ${url}`, cls: '' },
      { role: 'assistant', content: `sure, checking ${url} now`, cls: '' },
    ])).toEqual([])
  })

  it('keeps an agent-first PR included even if the user later references it', () => {
    expect(extractPullRequestLinks([
      { role: 'assistant', content: `created ${url}`, cls: '' },
      { role: 'user', content: `thanks, ${url} looks good`, cls: '' },
    ]).map(l => l.url)).toEqual([url])
  })

  it('treats a PR surfaced in tool/thinking output as an agent Change', () => {
    // A PR URL in a tool result (e.g. `gh pr create` output) is agent-surfaced.
    expect(extractPullRequestLinks([
      { role: 'tool', content: `Created pull request ${url}`, cls: '' } as ChatMessage,
    ]).map(l => l.url)).toEqual([url])
  })

  it('splits a mixed transcript into agent Changes only', () => {
    const agentPr = 'https://github.com/acme/widgets/pull/20'
    const userPr = 'https://github.com/acme/widgets/pull/99'
    expect(extractPullRequestLinks([
      { role: 'user', content: `context: ${userPr}`, cls: '' },
      { role: 'assistant', content: `done, opened ${agentPr}`, cls: '' },
    ]).map(l => l.url)).toEqual([agentPr])
  })

  it('index.update applies the same first-mention rule as extraction', () => {
    const index = new PullRequestLinkIndex()
    // User-only mention → no Change source.
    expect(index.update('slot-x', [
      { role: 'user', content: `see ${url}`, cls: '' },
    ])).toEqual([])
    // Agent appends its own PR → only that one surfaces; the user's stays out.
    const agentPr = 'https://github.com/acme/widgets/pull/21'
    expect(index.update('slot-x', [
      { role: 'user', content: `see ${url}`, cls: '' },
      { role: 'assistant', content: `opened ${agentPr}`, cls: '' },
    ]).map(l => l.url)).toEqual([agentPr])
  })

  it('a flood of user-referenced PRs does not starve agent Change sources', () => {
    // MAX user PRs first, then an agent PR. The per-role cap must keep the
    // agent PR from being crowded out of the (bounded) source list.
    const userMsgs = Array.from({ length: MAX_PULL_REQUEST_SOURCES }, (_, i) => ({
      role: 'user',
      content: `https://github.com/acme/widgets/pull/${i + 1}`,
      cls: '',
    } as ChatMessage))
    const agentPr = 'https://github.com/acme/service/pull/500'
    const result = extractPullRequestLinks([
      ...userMsgs,
      { role: 'assistant', content: `opened ${agentPr}`, cls: '' },
    ])
    expect(result.map(l => l.url)).toEqual([agentPr])
  })
})

describe('CJK / fullwidth punctuation after a PR URL (issue #507)', () => {
  const gh = 'https://github.com/kirodotdev/KiroCrew/pull/436'
  const gl = 'https://gitlab.com/acme/platform/-/merge_requests/42'

  // Fullwidth / CJK punctuation is kept as literals (the repo pre-commit hook
  // blocks only CJK ideographs U+4E00-9FFF, not punctuation); the few ideographs
  // needed to prove the scan stops on Han text are written as \u escapes, so the
  // source stays free of literal Chinese words while the runtime strings are the
  // genuine characters.
  it.each([
    ['a fullwidth open paren U+FF08', `PR up: ${gh}（one commit）`],
    ['a fullwidth comma U+FF0C', `done ${gh}，then test`],
    ['an ideographic full stop U+3002', `merged ${gh}。`],
    ['adjacent Han text U+66F4 U+65B0', `see ${gh}\u66F4\u65B0 notes`],
    ['an ideographic space U+3000', `see ${gh}\u3000thanks`],
    ['fullwidth corner brackets U+300C U+300D', `ref 「${gh}」ok`],
  ])('extracts the PR when the URL is followed by %s', (_label, content) => {
    expect(extractPullRequestLinks(messages(content)).map(link => link.url)).toEqual([gh])
  })

  it('extracts a PR wrapped in a fullwidth-parenthesised clause', () => {
    expect(extractPullRequestLinks(messages(`（see ${gh}，ok）`)).map(link => link.url)).toEqual([gh])
  })

  it('extracts a GitLab MR followed by fullwidth punctuation', () => {
    expect(extractPullRequestLinks(messages(`MR up ${gl}，waiting review`)).map(link => link.url)).toEqual([gl])
  })

  it('still parses an ASCII query/fragment tail (no over-trim regression)', () => {
    expect(extractPullRequestLinks(messages(
      `see ${gh}?tab=checks and ${gh}#issuecomment-1`,
    )).map(link => link.url)).toEqual([gh])
  })

  it('separates two PRs joined only by fullwidth punctuation', () => {
    const a = 'https://github.com/acme/widgets/pull/436'
    const b = 'https://github.com/acme/widgets/pull/9'
    // No ASCII space anywhere: the old denylist scan swallowed both URLs into a
    // single un-parseable candidate, so BOTH were lost. The allowlist stops at
    // each fullwidth mark, recovering each URL independently.
    expect(extractPullRequestLinks(messages(`${a}，${b}。`)).map(link => link.url)).toEqual([a, b])
  })

  it('extracts the PR from a realistic CJK assistant message', () => {
    // Runtime string reads (translated): "PR opened: <url>(single commit,
    // Fixes #435), CI all green, merge after approve." Han ideographs are \u
    // escapes; fullwidth punctuation (（），。：) is literal.
    const content =
      `PR \u5DF2\u5F00：${gh}（\u5355 commit，Fixes #435），`
      + `CI \u5168\u7EFF，approve \u540E merge。`
    expect(extractPullRequestLinks(messages(content))).toEqual([
      { url: gh, provider: 'github', number: 436, repo: 'KiroCrew', kind: 'change' },
    ])
  })
})
