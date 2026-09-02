/**
 * Jira issue URL extraction: the `jira` provider, Atlassian Cloud auto-detection,
 * self-hosted Jira host allowlisting, and interaction with first-mention attribution.
 */
import { describe, expect, it } from 'vitest'
import type { ChatMessage } from '../types'
import {
  extractPullRequestLinks,
  jiraHostSet,
  PullRequestLinkIndex,
} from '../utils/pullRequestLinks'

const messages = (...content: string[]): ChatMessage[] =>
  content.map(text => ({ role: 'assistant', content: text, cls: '' }))

const userMessages = (...content: string[]): ChatMessage[] =>
  content.map(text => ({ role: 'user', content: text, cls: '' }))

describe('Jira link extraction', () => {
  it('extracts Atlassian Cloud Jira issue URLs', () => {
    expect(extractPullRequestLinks(messages(
      'Working on https://acme.atlassian.net/browse/PROJ-123 today.',
    ))).toEqual([
      { url: 'https://acme.atlassian.net/browse/PROJ-123', provider: 'jira', number: 123, repo: 'PROJ', kind: 'issue' },
    ])
  })

  it('extracts multiple Jira issues and deduplicates', () => {
    expect(extractPullRequestLinks(messages(
      'See https://acme.atlassian.net/browse/TEAM-42 and https://acme.atlassian.net/browse/TEAM-99.',
      'Same ticket: https://acme.atlassian.net/browse/TEAM-42',
    ))).toEqual([
      { url: 'https://acme.atlassian.net/browse/TEAM-42', provider: 'jira', number: 42, repo: 'TEAM', kind: 'issue' },
      { url: 'https://acme.atlassian.net/browse/TEAM-99', provider: 'jira', number: 99, repo: 'TEAM', kind: 'issue' },
    ])
  })

  it('handles Jira URLs with query parameters (strips them)', () => {
    expect(extractPullRequestLinks(messages(
      'Check https://acme.atlassian.net/browse/DEV-7?focusedId=10042',
    ))).toEqual([
      { url: 'https://acme.atlassian.net/browse/DEV-7', provider: 'jira', number: 7, repo: 'DEV', kind: 'issue' },
    ])
  })

  it('handles Jira keys with numbers in the project prefix', () => {
    expect(extractPullRequestLinks(messages(
      'Linked to https://org.atlassian.net/browse/A1B2-999',
    ))).toEqual([
      { url: 'https://org.atlassian.net/browse/A1B2-999', provider: 'jira', number: 999, repo: 'A1B2', kind: 'issue' },
    ])
  })

  it('rejects invalid Jira keys (no letters, no number, missing dash)', () => {
    expect(extractPullRequestLinks(messages(
      'https://acme.atlassian.net/browse/PROJ',
      'https://acme.atlassian.net/browse/123',
      'https://acme.atlassian.net/browse/-456',
      'https://acme.atlassian.net/browse/TOOLONGPROJECT-1',
    ))).toEqual([])
  })

  it('normalizes lowercase Jira keys to uppercase', () => {
    expect(extractPullRequestLinks(messages(
      'https://acme.atlassian.net/browse/proj-123',
    ))).toEqual([
      { url: 'https://acme.atlassian.net/browse/PROJ-123', provider: 'jira', number: 123, repo: 'PROJ', kind: 'issue' },
    ])
  })

  it('rejects bare atlassian.net without a subdomain', () => {
    expect(extractPullRequestLinks(messages(
      'https://atlassian.net/browse/PROJ-1',
    ))).toEqual([])
  })

  describe('self-hosted Jira', () => {
    const issue = 'https://jira.internal.corp/browse/OPS-77'

    it('ignores a self-hosted Jira issue when no host is allowlisted', () => {
      expect(extractPullRequestLinks(messages(`Filed ${issue}`))).toEqual([])
    })

    it('extracts a self-hosted Jira issue when its host is allowlisted', () => {
      expect(extractPullRequestLinks(messages(`Filed ${issue}`), [], ['jira.internal.corp'])).toEqual([
        { url: issue, provider: 'jira', number: 77, repo: 'OPS', kind: 'issue' },
      ])
    })

    it('requires port to match exactly', () => {
      const ported = 'https://jira.corp.example:8080/browse/DEV-5'
      expect(extractPullRequestLinks(messages(ported), [], ['jira.corp.example'])).toEqual([])
      expect(extractPullRequestLinks(messages(ported), [], ['jira.corp.example:8080'])).toEqual([
        { url: 'https://jira.corp.example:8080/browse/DEV-5', provider: 'jira', number: 5, repo: 'DEV', kind: 'issue' },
      ])
    })

    it('preserves context-path prefix for self-hosted Jira/Data Center', () => {
      const withPrefix = 'https://jira.example.com/jira/browse/OPS-7'
      expect(extractPullRequestLinks(messages(withPrefix), [], ['jira.example.com'])).toEqual([
        { url: 'https://jira.example.com/jira/browse/OPS-7', provider: 'jira', number: 7, repo: 'OPS', kind: 'issue' },
      ])
    })

    it('preserves deep context-path prefix', () => {
      const deep = 'https://tools.corp.net/infra/jira/browse/INFRA-42'
      expect(extractPullRequestLinks(messages(deep), [], ['tools.corp.net'])).toEqual([
        { url: 'https://tools.corp.net/infra/jira/browse/INFRA-42', provider: 'jira', number: 42, repo: 'INFRA', kind: 'issue' },
      ])
    })
  })

  describe('coexistence with GitHub and GitLab', () => {
    it('extracts Jira, GitHub, and GitLab links from the same message', () => {
      const result = extractPullRequestLinks(messages(
        'Jira: https://team.atlassian.net/browse/FEAT-10, GitHub: https://github.com/org/repo/pull/5, GitLab: https://gitlab.com/org/proj/-/merge_requests/3',
      ))
      expect(result).toEqual([
        { url: 'https://team.atlassian.net/browse/FEAT-10', provider: 'jira', number: 10, repo: 'FEAT', kind: 'issue' },
        { url: 'https://github.com/org/repo/pull/5', provider: 'github', number: 5, repo: 'repo', kind: 'change' },
        { url: 'https://gitlab.com/org/proj/-/merge_requests/3', provider: 'gitlab', number: 3, repo: 'proj', kind: 'change' },
      ])
    })
  })

  describe('first-mention attribution', () => {
    it('user-mentioned Jira issues ARE included (Jira-specific exemption)', () => {
      const result = extractPullRequestLinks([
        ...userMessages('Working on https://acme.atlassian.net/browse/PROJ-50'),
        ...messages('I see PROJ-50, also https://acme.atlassian.net/browse/PROJ-51'),
      ])
      // Both are emitted — Jira issues are surfaced regardless of who mentioned first
      expect(result).toEqual([
        { url: 'https://acme.atlassian.net/browse/PROJ-50', provider: 'jira', number: 50, repo: 'PROJ', kind: 'issue' },
        { url: 'https://acme.atlassian.net/browse/PROJ-51', provider: 'jira', number: 51, repo: 'PROJ', kind: 'issue' },
      ])
    })
  })

  describe('PullRequestLinkIndex incremental', () => {
    it('detects Jira links added in an appended message', () => {
      const index = new PullRequestLinkIndex()
      const initial: ChatMessage[] = [
        { role: 'assistant', content: 'Starting work.', cls: '' },
      ]
      expect(index.update(null, initial)).toEqual([])

      const extended: ChatMessage[] = [
        ...initial,
        { role: 'assistant', content: 'Filed https://acme.atlassian.net/browse/BUG-12', cls: '' },
      ]
      const result = index.update(null, extended)
      expect(result).toEqual([
        { url: 'https://acme.atlassian.net/browse/BUG-12', provider: 'jira', number: 12, repo: 'BUG', kind: 'issue' },
      ])
    })

    it('rescans when jira_hosts allowlist changes', () => {
      const index = new PullRequestLinkIndex()
      const msgs: ChatMessage[] = [
        { role: 'assistant', content: 'See https://jira.acme.dev/browse/CORE-1', cls: '' },
      ]
      // Without allowlist, self-hosted Jira is not recognized
      expect(index.update('s1', msgs, [], [])).toEqual([])
      // Adding the host triggers a rescan
      expect(index.update('s1', msgs, [], ['jira.acme.dev'])).toEqual([
        { url: 'https://jira.acme.dev/browse/CORE-1', provider: 'jira', number: 1, repo: 'CORE', kind: 'issue' },
      ])
    })
  })

  describe('jiraHostSet', () => {
    it('normalizes entries', () => {
      const set = jiraHostSet(['Jira.Corp.Example:443', ' jira.dev ', 'JIRA.INTERNAL'])
      expect(set.has('jira.corp.example')).toBe(true) // :443 stripped
      expect(set.has('jira.dev')).toBe(true)
      expect(set.has('jira.internal')).toBe(true)
    })

    it('returns empty set for undefined', () => {
      expect(jiraHostSet(undefined).size).toBe(0)
    })
  })
})
