import { describe, it, expect } from 'vitest'
import { extractChatLinks, dedupResourceLinks, resourceIdentity, resourceKey } from '../utils/extractChatLinks'
import type { ChatMessage } from '../types'
import type { ExtractedLink } from '../utils/extractChatLinks'

function msg(role: string, content: string): ChatMessage {
  return { role, content, ts: '2026-01-01T00:00:00Z' } as ChatMessage
}

describe('extractChatLinks', () => {
  it('extracts markdown links with text as label', () => {
    const messages = [msg('user', 'Check [Design Doc](https://example.com/abc123)')]
    const links = extractChatLinks(messages)
    expect(links).toHaveLength(1)
    expect(links[0].label).toBe('Design Doc')
    expect(links[0].url).toBe('https://example.com/abc123')
    expect(links[0].type).toBe('other')
    expect(links[0].fromMarkdown).toBe(true)
  })

  it('extracts bare URLs with fallback label', () => {
    const messages = [msg('user', 'see https://github.com/acme/repo/pull/42')]
    const links = extractChatLinks(messages)
    expect(links).toHaveLength(1)
    expect(links[0].label).toBe('#42')
    expect(links[0].type).toBe('cr')
    expect(links[0].fromMarkdown).toBeUndefined()
  })

  it('deduplicates same URL', () => {
    const messages = [
      msg('user', 'https://example.com/abc'),
      msg('assistant', 'https://example.com/abc'),
    ]
    const links = extractChatLinks(messages)
    expect(links).toHaveLength(1)
  })

  it('does not dedup URLs with different fragments', () => {
    const messages = [
      msg('user', 'https://github.com/acme/repo/pull/123/files#diff-1'),
      msg('user', 'https://github.com/acme/repo/pull/123'),
    ]
    const links = extractChatLinks(messages)
    expect(links).toHaveLength(2)
  })

  it('classifies URL types correctly', () => {
    const messages = [msg('user', [
      'https://github.com/acme/repo/pull/111',
      'https://gitlab.com/acme/repo/-/merge_requests/7',
      'https://bitbucket.org/acme/repo/pull-requests/3',
      'https://example.com/other',
    ].join('\n'))]
    const links = extractChatLinks(messages)
    expect(links.map(l => l.type)).toEqual(['cr', 'cr', 'cr', 'other'])
  })

  it('handles 2-letter TLD bare domains', () => {
    const messages = [msg('user', 'check https://cursor.ai for details')]
    const links = extractChatLinks(messages)
    expect(links).toHaveLength(1)
    expect(links[0].url).toBe('https://cursor.ai')
  })

  it('does not include trailing dots in URL', () => {
    const messages = [msg('user', 'see https://example.com/path...')]
    const links = extractChatLinks(messages)
    expect(links).toHaveLength(1)
    expect(links[0].url).not.toContain('...')
  })

  it('skips non-user/assistant messages', () => {
    const messages = [msg('system', 'https://example.com/abc')]
    const links = extractChatLinks(messages)
    expect(links).toHaveLength(0)
  })

  it('uses fallback for short markdown text', () => {
    const messages = [msg('user', '[ab](https://github.com/acme/repo/pull/999)')]
    const links = extractChatLinks(messages)
    expect(links[0].label).toBe('#999') // fallback because 'ab' < 3 chars
  })
})

describe('dedupResourceLinks', () => {
  function link(url: string, type: 'cr' | 'other', label: string): ExtractedLink {
    return { url, type, label, msgIdx: 0 }
  }

  it('collapses the same PR referenced by different sub-paths / fragments', () => {
    // Regression: two distinct URLs for pull/274 previously rendered as two
    // identical "Perf observability readiness telemetry PR" resource rows.
    const links = [
      link('https://github.com/acme/repo/pull/274', 'cr', 'Perf observability readiness telemetry PR'),
      link('https://github.com/acme/repo/pull/274/files', 'cr', 'Perf observability readiness telemetry PR'),
      link('https://github.com/acme/repo/pull/274#issuecomment-1', 'cr', 'Perf observability readiness telemetry PR'),
    ]
    const out = dedupResourceLinks(links)
    expect(out).toHaveLength(1)
    // First occurrence (best label) wins.
    expect(out[0].url).toBe('https://github.com/acme/repo/pull/274')
  })

  it('keeps distinct PRs separate', () => {
    const links = [
      link('https://github.com/acme/repo/pull/274', 'cr', '#274'),
      link('https://github.com/acme/repo/pull/300', 'cr', '#300'),
    ]
    expect(dedupResourceLinks(links)).toHaveLength(2)
  })

  it('canonicalizes GitLab merge_requests and Bitbucket pull-requests', () => {
    expect(resourceIdentity(link('https://gitlab.com/a/b/-/merge_requests/7/diffs', 'cr', 'x')))
      .toBe('https://gitlab.com/a/b/-/merge_requests/7')
    expect(resourceIdentity(link('https://bitbucket.org/a/b/pull-requests/3/overview', 'cr', 'x')))
      .toBe('https://bitbucket.org/a/b/pull-requests/3')
  })

  it('dedups identical non-CR links but keeps different ones', () => {
    const links = [
      link('https://example.com/doc/', 'other', 'Doc'),
      link('https://example.com/doc', 'other', 'Doc'),
      link('https://example.com/other', 'other', 'Other'),
    ]
    expect(dedupResourceLinks(links)).toHaveLength(2)
  })

  it('preserves order of first occurrences', () => {
    const links = [
      link('https://example.com/a', 'other', 'A'),
      link('https://github.com/acme/repo/pull/1', 'cr', '#1'),
      link('https://github.com/acme/repo/pull/1/files', 'cr', '#1'),
      link('https://example.com/b', 'other', 'B'),
    ]
    expect(dedupResourceLinks(links).map(l => l.label)).toEqual(['A', '#1', 'B'])
  })

  it('does NOT collapse non-CR paths that differ only by case (path is case-sensitive)', () => {
    // Regression: identity previously lowercased the whole URL, dropping /docs.
    const links = [
      link('https://example.com/Docs', 'other', 'Docs (caps)'),
      link('https://example.com/docs', 'other', 'docs (lower)'),
    ]
    expect(dedupResourceLinks(links)).toHaveLength(2)
  })

  it('treats scheme + host as case-insensitive but keeps the path intact', () => {
    // Authority differs only by case -> same resource.
    expect(resourceKey('https://Example.COM/path')).toBe(resourceKey('https://example.com/path'))
    // Path case differs -> distinct resources.
    expect(resourceKey('https://example.com/Path')).not.toBe(resourceKey('https://example.com/path'))
  })

  it('resourceKey canonicalizes CR URLs regardless of link type', () => {
    expect(resourceKey('https://github.com/acme/repo/pull/274/files#diff-1'))
      .toBe('https://github.com/acme/repo/pull/274')
    // Same key from a bare source URL lets the Changes-tab filter suppress it.
    expect(resourceKey('https://github.com/acme/repo/pull/274'))
      .toBe(resourceKey('https://github.com/acme/repo/pull/274/commits'))
  })
})
