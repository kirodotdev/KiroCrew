import { afterEach, describe, expect, it, vi } from 'vitest'
import type { ChatMessage } from '../types'
import {
  adoptSourceSelections,
  commitSourceSelection,
  extractPullRequestLinks,
  isSourceSelectionKey,
  commitRevealedSource,
  loadRevealedSources,
  loadSeenPullRequestLinks,
  loadSourceSelections,
  MAX_PULL_REQUEST_SOURCES,
  parseSourceLinkUrl,
  persistSeenPullRequestLinks,
  PullRequestLinkIndex,
  recordNewPullRequestLinks,
  sourceSelection,
  withSourceSelection,
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
      // The URL API drops the default HTTPS port, and the backend does too.
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

/* ── parseSourceLinkUrl ────────────────────────────────────────────────────
 * The one-url entry point, for callers holding a url but no transcript: the
 * sidebar chips, whose links the BACKEND scanned. Its job is to hand back the
 * SAME canonical shape the transcript extractor produces, so a revealed chip and
 * a scanned link are interchangeable in the panel's list. */
/* ── Revealed-source persistence ───────────────────────────────────────────
 * A revealed link is frequently one the extractor deliberately excludes, while
 * the SELECTION pointing at it is already durable. Holding the link in memory
 * only meant a reload remembered the url but could no longer produce it, and the
 * Changes reconciliation then normalised onto a different pull request. */
describe('revealed source persistence', () => {
  const PR = 'https://github.com/acme/widgets/pull/12'
  const ISSUE = 'https://github.com/acme/widgets/issues/9'
  const link = (url: string) => parseSourceLinkUrl(url)!

  afterEach(() => { localStorage.clear() })

  it('round-trips a revealed link per slot and per kind', () => {
    expect(commitRevealedSource('slot-a', 'change', PR)).toBe(true)
    expect(commitRevealedSource('slot-a', 'issue', ISSUE)).toBe(true)
    expect(commitRevealedSource('slot-b', 'change', PR)).toBe(true)

    const restored = loadRevealedSources()
    expect(restored['slot-a'].change).toEqual(link(PR))
    expect(restored['slot-a'].issue).toEqual(link(ISSUE))
    expect(restored['slot-b']).toEqual({ change: link(PR) })
  })

  it('writes ONE key per field so a sibling window cannot delete a reveal', () => {
    // A popped-out session shares this localStorage. A whole-map write publishes
    // this window's stale view of the slots it is NOT looking at, so the later
    // write deleted the other window's reveal and the reload it was meant to
    // survive swapped the panel anyway.
    //
    // Enumerated via key(i), not Object.keys: Storage keys are not own enumerable
    // properties, which is also why the loader walks it this way.
    const revealedKeys = () => {
      const keys: string[] = []
      for (let i = 0; i < localStorage.length; i += 1) {
        const k = localStorage.key(i)
        if (k?.startsWith('mc-pr-source-revealed:')) keys.push(k)
      }
      return keys.sort()
    }

    commitRevealedSource('slot-a', 'change', PR)
    expect(revealedKeys()).toEqual(['mc-pr-source-revealed:change:slot-a'])

    // Simulate the sibling window: it never saw slot-a, and writing its own
    // reveal must leave slot-a's key untouched.
    commitRevealedSource('slot-b', 'issue', ISSUE)
    expect(revealedKeys()).toEqual([
      'mc-pr-source-revealed:change:slot-a',
      'mc-pr-source-revealed:issue:slot-b',
    ])
    const restored = loadRevealedSources()
    expect(restored['slot-a']).toEqual({ change: link(PR) })
    expect(restored['slot-b']).toEqual({ issue: link(ISSUE) })
  })

  it('starts empty and tolerates junk instead of injecting it', () => {
    expect(loadRevealedSources()).toEqual({})
    const key = 'mc-pr-source-revealed:change:slot-a'
    for (const junk of ['{not-json', 'null', '[]', '{"u":5}', '{"u":""}', '{}']) {
      localStorage.setItem(key, junk)
      expect(loadRevealedSources()).toEqual({})
    }
  })

  it('ignores a malformed storage key', () => {
    localStorage.setItem('mc-pr-source-revealed:bogus:slot-a', JSON.stringify({ u: PR, t: 1 }))
    localStorage.setItem('mc-pr-source-revealed:change:', JSON.stringify({ u: PR, t: 1 }))
    localStorage.setItem('mc-pr-source-revealed:change', JSON.stringify({ u: PR, t: 1 }))
    expect(loadRevealedSources()).toEqual({})
  })

  it('drops a url that is not a canonical provider link', () => {
    // Non-canonical and non-provider urls are re-derived, fail, and are skipped;
    // storage is untrusted so nothing is taken on trust.
    for (const bad of [
      'https://github.com/acme/widgets/pull/12?tab=files',
      'javascript:alert(1)//github.com/a/b/pull/1',
      'https://github.com/acme/widgets',
    ]) {
      localStorage.setItem('mc-pr-source-revealed:change:slot-a', JSON.stringify({ u: bad, t: 1 }))
      expect(loadRevealedSources()).toEqual({})
    }
    // Positive control: the canonical form of the first one DOES restore.
    localStorage.setItem('mc-pr-source-revealed:change:slot-a', JSON.stringify({ u: PR, t: 1 }))
    expect(loadRevealedSources()['slot-a'].change?.url).toBe(PR)
  })

  it('refuses a url filed under the wrong kind', () => {
    // A 'change' key holding an issue url would inject into the panel the other
    // kind owns.
    localStorage.setItem('mc-pr-source-revealed:change:slot-a', JSON.stringify({ u: ISSUE, t: 1 }))
    localStorage.setItem('mc-pr-source-revealed:issue:slot-b', JSON.stringify({ u: PR, t: 1 }))
    expect(loadRevealedSources()).toEqual({})
  })

  it('restores a self-hosted GitLab link without the allowlist', () => {
    // The allowlist arrives asynchronously from dashboard config, so applying it
    // here would drop every self-hosted reveal on reload. The host was vetted at
    // reveal time and the backend re-validates before any provider call.
    const mr = 'https://gitlab.acme.internal/team/api/-/merge_requests/7'
    expect(commitRevealedSource('slot-a', 'change', mr)).toBe(true)
    expect(loadRevealedSources()['slot-a'].change?.url).toBe(mr)
  })

  it('caps on READ by recency without deleting another slot\'s key', () => {
    for (let i = 0; i < 40; i += 1) commitRevealedSource(`slot-${i}`, 'change', PR)
    const restored = Object.keys(loadRevealedSources())
    expect(restored).toHaveLength(32)
    // Newest wins the cap...
    expect(restored).toContain('slot-39')
    expect(restored).not.toContain('slot-0')
    // ...but the capped-out key is still on disk: a prune pass computes its doomed
    // set from a walk, and a sibling window can refresh one of those slots before
    // the removals run (see loadSourceSelections).
    expect(localStorage.getItem('mc-pr-source-revealed:change:slot-0')).not.toBeNull()
  })

  it('never stamps below what is already stored', () => {
    // A clock stepping backwards would otherwise sort a brand-new reveal below the
    // read cap.
    commitRevealedSource('slot-a', 'change', PR)
    const first = JSON.parse(localStorage.getItem('mc-pr-source-revealed:change:slot-a')!).t
    const spy = vi.spyOn(Date, 'now').mockReturnValue(0)
    try {
      commitRevealedSource('slot-b', 'issue', ISSUE)
    } finally {
      spy.mockRestore()
    }
    const second = JSON.parse(localStorage.getItem('mc-pr-source-revealed:issue:slot-b')!).t
    expect(second).toBeGreaterThan(first)
  })

  it('does not let a crafted future stamp outrank a real reveal', () => {
    // `Number.isFinite` alone admits Number.MAX_VALUE, and MAX_VALUE + 1 ===
    // MAX_VALUE, so a crafted entry could tie a genuine write and — being earlier
    // in a stable sort — cap the genuine one out. Storage is untrusted, so a stamp
    // that could not have come from a real Date.now() forfeits its recency.
    // Covers the ceiling exactly, which a clamped genuine write would otherwise
    // tie, as well as values above it.
    for (const crafted of [Number.MAX_VALUE, 4102444800000, 4102444800001]) {
      localStorage.clear()
      for (let i = 0; i < 32; i += 1) {
        localStorage.setItem(
          `mc-pr-source-revealed:change:crafted-${i}`,
          JSON.stringify({ u: PR, t: crafted }),
        )
      }
      commitRevealedSource('real-slot', 'issue', ISSUE)

      const restored = loadRevealedSources()
      // The genuine reveal survives the 32-slot cap; a crafted slot is displaced.
      expect(restored['real-slot'], `crafted t=${crafted}`).toEqual({ issue: link(ISSUE) })
      expect(Object.keys(restored)).toHaveLength(32)
      // The crafted entries stay READABLE — a corrupt stamp costs recency, not the
      // link itself.
      localStorage.removeItem('mc-pr-source-revealed:issue:real-slot')
      expect(loadRevealedSources()['crafted-0']).toEqual({ change: link(PR) })
    }
  })

  it('rejects an oversized raw entry before parsing it', () => {
    localStorage.setItem(
      'mc-pr-source-revealed:change:slot-a',
      JSON.stringify({ u: PR, pad: 'x'.repeat(4096) }),
    )
    expect(loadRevealedSources()).toEqual({})
  })
})

describe('parseSourceLinkUrl', () => {
  it('returns the same canonical shape the extractor produces', () => {
    const url = 'https://github.com/acme/widgets/pull/12'
    expect(parseSourceLinkUrl(url)).toEqual(extractPullRequestLinks(messages(url))[0])
  })

  it('classifies kind from the url, for both providers', () => {
    expect(parseSourceLinkUrl('https://github.com/acme/widgets/pull/12'))
      .toMatchObject({ provider: 'github', number: 12, repo: 'widgets', kind: 'change' })
    expect(parseSourceLinkUrl('https://github.com/acme/widgets/issues/9'))
      .toMatchObject({ provider: 'github', number: 9, repo: 'widgets', kind: 'issue' })
    expect(parseSourceLinkUrl('https://gitlab.com/acme/service/-/merge_requests/4'))
      .toMatchObject({ provider: 'gitlab', number: 4, repo: 'service', kind: 'change' })
    expect(parseSourceLinkUrl('https://gitlab.com/acme/service/-/issues/8'))
      .toMatchObject({ provider: 'gitlab', number: 8, repo: 'service', kind: 'issue' })
  })

  it('canonicalises the url, dropping query, fragment and www', () => {
    expect(parseSourceLinkUrl('https://www.github.com/acme/widgets/pull/12?tab=files#diff-1')?.url)
      .toBe('https://github.com/acme/widgets/pull/12')
  })

  it('rejects anything that is not a permitted pull request / issue url', () => {
    // Attribution does NOT apply here — this parses one url, it does not decide
    // whose mention it was — but the host allowlist still does.
    expect(parseSourceLinkUrl('https://github.com/acme/widgets')).toBeNull()
    expect(parseSourceLinkUrl('https://github.com.evil.example/acme/widgets/pull/12')).toBeNull()
    expect(parseSourceLinkUrl('javascript:alert(1)//github.com/a/b/pull/1')).toBeNull()
    expect(parseSourceLinkUrl('not a url')).toBeNull()
  })

  it('accepts a self-managed GitLab host only when it is allowlisted', () => {
    const mr = 'https://gitlab.acme.internal/team/api/-/merge_requests/7'
    expect(parseSourceLinkUrl(mr)).toBeNull()
    expect(parseSourceLinkUrl(mr, ['gitlab.acme.internal']))
      .toMatchObject({ provider: 'gitlab', number: 7, repo: 'api', kind: 'change' })
  })
})

describe('per-slot focused source tab', () => {
  const prA = 'https://github.com/acme/widgets/pull/11'
  const prB = 'https://github.com/acme/widgets/pull/12'
  const issue = 'https://github.com/acme/widgets/issues/9'

  afterEach(() => { localStorage.clear() })

  it('keeps each slot and each kind independent', () => {
    let sel = withSourceSelection({}, 'slot-a', 'change', prB)
    sel = withSourceSelection(sel, 'slot-a', 'issue', issue)
    sel = withSourceSelection(sel, 'slot-b', 'change', prA)

    expect(sourceSelection(sel, 'slot-a', 'change')).toBe(prB)
    expect(sourceSelection(sel, 'slot-a', 'issue')).toBe(issue)
    expect(sourceSelection(sel, 'slot-b', 'change')).toBe(prA)
    // A slot that never selected anything reads as "no selection", so the
    // caller's first-wins fallback applies.
    expect(sourceSelection(sel, 'slot-c', 'change')).toBe('')
    expect(sourceSelection(sel, null, 'change')).toBe('')
  })

  it('returns the same object when nothing changes so a state update bails', () => {
    const sel = withSourceSelection({}, 'slot-a', 'change', prB)
    expect(withSourceSelection(sel, 'slot-a', 'change', prB)).toBe(sel)
    // No slot, and clearing a slot that has nothing stored, are both no-ops —
    // the latter keeps PR-free sessions from each accumulating an empty entry.
    expect(withSourceSelection(sel, null, 'change', prA)).toBe(sel)
    expect(withSourceSelection(sel, 'slot-z', 'change', '')).toBe(sel)
  })

  it('restores the selection a slot had before it was left', () => {
    expect(commitSourceSelection('slot-a', 'change', prB)).toBe('persisted')
    expect(commitSourceSelection('slot-a', 'issue', issue)).toBe('persisted')

    // Fresh mount (reload / route change): slot-a comes back on PR 12, not on
    // whichever link the transcript happens to mention first.
    const restored = loadSourceSelections()
    expect(sourceSelection(restored, 'slot-a', 'change')).toBe(prB)
    expect(sourceSelection(restored, 'slot-a', 'issue')).toBe(issue)
  })

  it('writes one key per (slot, kind) so a write can never touch another field', () => {
    // The concurrency invariant: nothing is stored as a shared blob, so a write
    // has no other field in scope to drop. Two windows can only collide on the
    // exact same key.
    commitSourceSelection('slot-a', 'change', prA)
    commitSourceSelection('slot-a', 'issue', issue)
    commitSourceSelection('slot-b', 'change', prB)

    const keys: string[] = []
    for (let index = 0; index < localStorage.length; index += 1) {
      const key = localStorage.key(index)
      if (key?.startsWith('mc-pr-source-sel:')) keys.push(key)
    }
    keys.sort()
    expect(keys).toEqual([
      'mc-pr-source-sel:change:slot-a',
      'mc-pr-source-sel:change:slot-b',
      'mc-pr-source-sel:issue:slot-a',
    ])
    for (const key of keys) expect(isSourceSelectionKey(key)).toBe(true)
    expect(isSourceSelectionKey('mc-panel-tabs:slot-a')).toBe(false)
  })

  it('survives two windows committing different slots', () => {
    // Cross-document safety is STRUCTURAL here, not timing-dependent: with one
    // key per field there is no read-modify-write for a concurrent document to
    // interleave with, so no ordering of two windows' writes can drop a field
    // neither of them named. (A single-threaded test cannot reproduce the real
    // interleaving — the gap between another document's read and its write — so
    // the invariant is pinned by the one-key-per-field test above; this case
    // covers the ordinary two-window outcome.)
    commitSourceSelection('slot-a', 'change', prA)
    commitSourceSelection('slot-b', 'change', prB)

    const merged = loadSourceSelections()
    expect(sourceSelection(merged, 'slot-a', 'change')).toBe(prA)
    expect(sourceSelection(merged, 'slot-b', 'change')).toBe(prB)
  })

  it('fails closed on malformed or non-canonical persisted selections', () => {
    localStorage.setItem('mc-pr-source-sel:change:slot-a', '{not-json')
    localStorage.setItem('mc-pr-source-sel:change:slot-b', JSON.stringify(['not-an-object']))
    localStorage.setItem('mc-pr-source-sel:change:slot-c', JSON.stringify({ u: 42 }))
    // Wrong host, and a canonical-looking url with a query tail, are both refused.
    localStorage.setItem('mc-pr-source-sel:change:slot-d', JSON.stringify({ u: 'https://evil.example/acme/widgets/pull/3' }))
    localStorage.setItem('mc-pr-source-sel:change:slot-e', JSON.stringify({ u: `${prA}?tab=files` }))
    // An unknown kind segment is not a field this store owns.
    localStorage.setItem('mc-pr-source-sel:bogus:slot-f', JSON.stringify({ u: prA }))
    expect(loadSourceSelections()).toEqual({})
  })

  it('removes the key when a selection is cleared', () => {
    expect(commitSourceSelection('slot-a', 'change', prA)).toBe('persisted')
    expect(commitSourceSelection('slot-a', 'change', '')).toBe('persisted')
    expect(loadSourceSelections()).toEqual({})
    expect(localStorage.getItem('mc-pr-source-sel:change:slot-a')).toBeNull()
  })

  it('caps stored slots by write recency, pruning the oldest', () => {
    // Recency is the stored timestamp, not key order — independent keys have no
    // insertion order to trim by. Stamps are forced so the test does not depend
    // on wall-clock resolution.
    const now = Date.now()
    for (let index = 0; index < 40; index += 1) {
      localStorage.setItem(
        `mc-pr-source-sel:change:slot-${index}`,
        JSON.stringify({ u: `https://github.com/acme/widgets/pull/${index + 1}`, t: now + index }),
      )
    }
    const restored = loadSourceSelections()
    expect(Object.keys(restored)).toHaveLength(32)
    expect(sourceSelection(restored, 'slot-39', 'change')).toBe('https://github.com/acme/widgets/pull/40')
    expect(sourceSelection(restored, 'slot-0', 'change')).toBe('')
  })

  it('keeps a re-selected slot when the cap has to drop one', () => {
    // Re-selecting a tab in an OLD session must move it out of the eviction
    // queue, so the cap never evicts the session the user is actively reading.
    // Seeded in the PAST (oldest first) so the fresh commits below are newest.
    const past = Date.now() - 100_000
    for (let index = 0; index < 32; index += 1) {
      localStorage.setItem(
        `mc-pr-source-sel:change:slot-${index}`,
        JSON.stringify({ u: `https://github.com/acme/widgets/pull/${index + 1}`, t: past + index }),
      )
    }
    // Touch the oldest slot, then add a 33rd so the cap has to drop exactly one.
    expect(commitSourceSelection('slot-0', 'change', prB)).toBe('persisted')
    expect(commitSourceSelection('slot-32', 'change', prA)).toBe('persisted')

    const restored = loadSourceSelections()
    expect(Object.keys(restored)).toHaveLength(32)
    expect(sourceSelection(restored, 'slot-0', 'change')).toBe(prB)
    expect(sourceSelection(restored, 'slot-32', 'change')).toBe(prA)
    // The now-oldest untouched slot falls outside the cap on READ. Its key is
    // deliberately left in place: deleting another slot's key by snapshot can
    // race a sibling window that just refreshed it, so nothing prunes here.
    expect(sourceSelection(restored, 'slot-1', 'change')).toBe('')
    expect(localStorage.getItem('mc-pr-source-sel:change:slot-1')).not.toBeNull()
  })

  it('keeps a fresh selection visible when the clock steps backwards', () => {
    // A stamp taken straight from a clock that has moved BACKWARD (NTP step,
    // resumed VM, user setting the date) would sort below 32 older slots, so the
    // read cap would hide the selection the user just made and the tab would
    // reset on reload. The written stamp is clamped above whatever is stored.
    const future = Date.now() + 10_000_000
    for (let index = 0; index < 32; index += 1) {
      localStorage.setItem(
        `mc-pr-source-sel:change:slot-${index}`,
        JSON.stringify({ u: `https://github.com/acme/widgets/pull/${index + 1}`, t: future + index }),
      )
    }
    expect(commitSourceSelection('slot-new', 'change', prA)).toBe('persisted')

    const restored = loadSourceSelections()
    expect(Object.keys(restored)).toHaveLength(32)
    expect(sourceSelection(restored, 'slot-new', 'change')).toBe(prA)
  })

  it('merges a commit into fresh storage instead of republishing a stale snapshot', () => {
    // Two chat windows on one origin (a popped-out session shares this
    // localStorage). Window B mounted BEFORE window A made its selection, so
    // B's in-memory snapshot does not know about slot-a. Writing B's whole map
    // back would delete slot-a; committing one slot against fresh storage keeps
    // both.
    expect(commitSourceSelection('slot-a', 'change', prA)).toBe('persisted')
    expect(commitSourceSelection('slot-b', 'change', prB)).toBe('persisted')

    const restored = loadSourceSelections()
    expect(sourceSelection(restored, 'slot-a', 'change')).toBe(prA)
    expect(sourceSelection(restored, 'slot-b', 'change')).toBe(prB)
  })

  it('reports whether a commit reached storage', () => {
    expect(commitSourceSelection('slot-a', 'change', prA)).toBe('persisted')
    // Re-committing the same value writes again, on purpose: the rewrite is what
    // refreshes recency (see the capped-out re-selection test below), so
    // 'unchanged' now means only "no write was attempted at all".
    expect(commitSourceSelection('slot-a', 'change', prA)).toBe('persisted')
    expect(commitSourceSelection('slot-z', 'change', '')).toBe('unchanged')
    expect(commitSourceSelection(null, 'change', prB)).toBe('unchanged')
  })

  it('refreshes recency when a capped-out slot is re-selected', () => {
    // Aged-out slots keep their keys but are excluded on read. Re-picking the url
    // already stored for such a slot has to rewrite it, or the click can never
    // pull the slot back inside the cap and the tab resets on every reload.
    const aged = 'slot-aged'
    commitSourceSelection(aged, 'change', prA)
    // Push it out of the cap with MAX_PERSISTED_SOURCE_SLOTS newer slots.
    for (let i = 0; i < 32; i += 1) {
      commitSourceSelection(`slot-new-${i}`, 'change', `https://github.com/o/r/pull/${i + 100}`)
    }
    expect(loadSourceSelections()[aged]).toBeUndefined()

    // The user clicks the tab for the url that is ALREADY stored for that slot.
    expect(commitSourceSelection(aged, 'change', prA)).toBe('persisted')
    expect(loadSourceSelections()[aged]?.change).toBe(prA)
  })

  it('reports failed when storage refuses the write', () => {    // 'failed' must be distinguishable from 'unchanged': only the former means
    // storage now DISAGREES with what the caller intended.
    const setItem = Storage.prototype.setItem
    Storage.prototype.setItem = () => {
      throw new DOMException('full', 'QuotaExceededError')
    }
    try {
      expect(commitSourceSelection('slot-a', 'change', prA)).toBe('failed')
    } finally {
      Storage.prototype.setItem = setItem
    }
  })

  it('does not adopt a stored value whose own write was refused', () => {
    // The user moved slot-a to PR 12 but the write was dropped, so storage still
    // holds PR 11 — a value that IS in this window's transcript, so the
    // availability rule alone would take it back and silently revert the tab.
    commitSourceSelection('slot-a', 'change', prA)
    const mine = withSourceSelection(loadSourceSelections(), 'slot-a', 'change', prB)

    const naive = adoptSourceSelections(mine, 'slot-a', { change: [prA, prB] })
    expect(sourceSelection(naive, 'slot-a', 'change')).toBe(prA)

    const guarded = adoptSourceSelections(mine, 'slot-a', { change: [prA, prB] }, { 'slot-a': { change: true } })
    expect(guarded).toBe(mine)
    expect(sourceSelection(guarded, 'slot-a', 'change')).toBe(prB)
  })

  it('honors a refused write for a slot that is not on screen', () => {
    // A refused write is equally lost whether or not the user happens to be
    // looking at that session, so the ledger is not scoped to the active slot.
    commitSourceSelection('slot-b', 'change', prB)
    const mine = withSourceSelection(loadSourceSelections(), 'slot-b', 'change', prA)

    // slot-a is on screen; slot-b's own write was refused, so storage still
    // holds prB while this window has moved on to prA.
    const adopted = adoptSourceSelections(
      mine, 'slot-a', { change: [prA, prB] }, { 'slot-b': { change: true } },
    )
    expect(sourceSelection(adopted, 'slot-b', 'change')).toBe(prA)
    // Without the ledger entry the sibling's stored value wins for that slot.
    const unguarded = adoptSourceSelections(mine, 'slot-a', { change: [prA, prB] })
    expect(sourceSelection(unguarded, 'slot-b', 'change')).toBe(prB)
  })

  it('adopts a sibling window write, including for the active slot', () => {
    // This window mounted with slot-a on PR 11; a sibling then moved slot-a to
    // PR 12 and picked a tab in slot-b. Both windows see both PRs, so PR 12 is
    // usable here and the sibling's choice wins.
    const mine = withSourceSelection({}, 'slot-a', 'change', prA)
    commitSourceSelection('slot-a', 'change', prB)
    commitSourceSelection('slot-b', 'change', prA)

    const adopted = adoptSourceSelections(mine, 'slot-a', { change: [prA, prB] })
    expect(sourceSelection(adopted, 'slot-a', 'change')).toBe(prB)
    expect(sourceSelection(adopted, 'slot-b', 'change')).toBe(prA)
  })

  it('refuses an active-slot value this window cannot show', () => {
    // The loop guard. Two windows on the SAME session with DIVERGENT transcripts
    // (a sibling has a newer message mentioning another PR). Adopting a url this
    // window has no tab for would make its reconciliation overwrite the
    // sibling's choice, which the sibling then overwrites back — an unbounded
    // cross-window write loop. Keeping our own value ends it: nothing changed
    // here, so nothing gets committed.
    const mine = withSourceSelection({}, 'slot-a', 'change', prA)
    commitSourceSelection('slot-a', 'change', prB)

    const adopted = adoptSourceSelections(mine, 'slot-a', { change: [prA] })
    expect(adopted).toBe(mine)
    expect(sourceSelection(adopted, 'slot-a', 'change')).toBe(prA)
  })

  it('keeps the active slot when storage has no entry for it', () => {
    // Stands in for a dropped write (safeSetItem false under quota pressure):
    // storage never learned about this window's own selection. Adopting the
    // storage view wholesale would yank the tab the user is looking at.
    const mine = withSourceSelection({}, 'slot-a', 'change', prA)
    commitSourceSelection('slot-b', 'change', prB)

    const adopted = adoptSourceSelections(mine, 'slot-a', { change: [prA] })
    expect(sourceSelection(adopted, 'slot-a', 'change')).toBe(prA)
    expect(sourceSelection(adopted, 'slot-b', 'change')).toBe(prB)
    // A slot this window is NOT showing gets no such protection — the sibling's
    // view of it is the truth, and this window's reconciliation never writes it.
    expect(sourceSelection(adoptSourceSelections(mine, 'slot-b', { change: [prB] }), 'slot-a', 'change')).toBe('')
  })

  it('returns the same object when a sibling write changes nothing here', () => {
    commitSourceSelection('slot-a', 'change', prA)
    const mine = loadSourceSelections()
    expect(adoptSourceSelections(mine, 'slot-a', { change: [prA] })).toBe(mine)
    // An unrelated key being cleared leaves storage identical, so still a bail.
    expect(adoptSourceSelections(mine, null)).toBe(mine)
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
    // No ASCII space anywhere: the allowlist stops at each fullwidth mark,
    // recovering each URL independently. A denylist scan would swallow both
    // URLs into a single un-parseable candidate and lose them.
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
