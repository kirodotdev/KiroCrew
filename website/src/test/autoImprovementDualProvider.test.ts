/**
 * Dual-provider derivation: commit links and PR-vs-MR labels come from the
 * CONFIGURED target URL, never a hardcoded host.
 *
 * `commitUrlOf` used to build `https://github.com/<repo>/commit/<sha>` from the
 * `owner/name` display string, which rendered a GitHub link for a finding
 * committed to a GitLab project. It now derives the web base from `target_url`
 * (the validated https URL setup persisted): github.com takes `/commit/<sha>`,
 * every other host setup accepts is a GitLab and takes `/-/commit/<sha>`. When
 * the target is missing or unparseable it returns null and the row renders no
 * link — the "never guess a host" rule the old implementation already pinned.
 */
import { describe, it, expect } from 'vitest'

import { commitUrlOf } from '../apps/auto-improvement/AutoImprovementPage'
import {
  isMergeRequestUrl,
  changeRequestNoun,
  changeRequestAbbrev,
  kindLabel,
  prPrompt,
  findingPrompt,
  rulerPrompt,
} from '../apps/auto-improvement/lib/prompts'

/** A committed ledger row: the direct-commit path stores a bare sha in `cr`. */
const committed = (sha: string) => ({ fp: 'f1', kind: 'perf', target: 'x', status: 'committed', cr: sha })

describe('commitUrlOf (commit links derive from the configured target URL)', () => {
  it('builds a GitHub commit url from a github.com target', () => {
    expect(commitUrlOf(committed('1537c449'), 'https://github.com/octo/repo'))
      .toBe('https://github.com/octo/repo/commit/1537c449')
  })

  it('builds a GitLab commit url (`/-/commit/`) for a gitlab.com target', () => {
    expect(commitUrlOf(committed('1537c449'), 'https://gitlab.com/group/project'))
      .toBe('https://gitlab.com/group/project/-/commit/1537c449')
  })

  it('keeps a nested GitLab group path intact', () => {
    // A GitLab project can live under nested groups; the whole path is the repo.
    expect(commitUrlOf(committed('abcdef0'), 'https://gitlab.com/group/sub/project'))
      .toBe('https://gitlab.com/group/sub/project/-/commit/abcdef0')
  })

  it('treats a self-hosted (non-github.com) host as GitLab', () => {
    // Setup accepts exactly one GitHub host, so any other allowed host is a
    // GitLab instance — including self-hosted ones with no "gitlab" in the name.
    expect(commitUrlOf(committed('abcdef0'), 'https://git.example.com/team/proj'))
      .toBe('https://git.example.com/team/proj/-/commit/abcdef0')
  })

  it('normalizes a `.git` suffix and a trailing slash off the stored url', () => {
    expect(commitUrlOf(committed('abcdef0'), 'https://github.com/o/r.git'))
      .toBe('https://github.com/o/r/commit/abcdef0')
    expect(commitUrlOf(committed('abcdef0'), 'https://gitlab.com/g/p/'))
      .toBe('https://gitlab.com/g/p/-/commit/abcdef0')
  })

  it('returns null rather than guessing when the target is missing or unparseable', () => {
    expect(commitUrlOf(committed('abcdef0'), '')).toBeNull()
    expect(commitUrlOf(committed('abcdef0'), 'not a url')).toBeNull()
    expect(commitUrlOf(committed('abcdef0'), 'owner/repo')).toBeNull()
  })

  it('refuses a non-https target', () => {
    expect(commitUrlOf(committed('abcdef0'), 'http://github.com/o/r')).toBeNull()
  })

  it('refuses a bare host with no repository path', () => {
    expect(commitUrlOf(committed('abcdef0'), 'https://github.com')).toBeNull()
    expect(commitUrlOf(committed('abcdef0'), 'https://github.com/')).toBeNull()
  })

  it('only links a real sha, not a queue id or a url stored in the same field', () => {
    expect(commitUrlOf(committed('queued-3'), 'https://github.com/o/r')).toBeNull()
    expect(
      commitUrlOf(
        { fp: 'f', kind: 'bug', target: 'x', status: 'committed', pr: 'https://github.com/o/r/pull/1' },
        'https://github.com/o/r',
      ),
    ).toBeNull()
  })
})

describe('PR-vs-MR label derivation (the URL is the provider signal)', () => {
  const MR = 'https://gitlab.com/g/p/-/merge_requests/12'
  const PR = 'https://github.com/o/r/pull/12'

  it('recognizes the GitLab merge-request path segment', () => {
    expect(isMergeRequestUrl(MR)).toBe(true)
    expect(isMergeRequestUrl(PR)).toBe(false)
    expect(isMergeRequestUrl(undefined)).toBe(false)
  })

  it('picks the provider noun and abbreviation from the url', () => {
    expect(changeRequestNoun(MR)).toBe('merge request')
    expect(changeRequestNoun(PR)).toBe('pull request')
    expect(changeRequestAbbrev(MR)).toBe('MR')
    expect(changeRequestAbbrev(PR)).toBe('PR')
    // No url means no provider signal; default to the GitHub-side terms.
    expect(changeRequestNoun()).toBe('pull request')
  })

  it('kindLabel titles a GitLab subject "MR" and everything else unchanged', () => {
    expect(kindLabel('pr', MR)).toBe('MR')
    expect(kindLabel('pr', PR)).toBe('PR')
    expect(kindLabel('pr')).toBe('PR')
    expect(kindLabel('ruler')).toBe('Ruler')
  })

  it('the seed prompt for a GitLab MR says "merge request", not "pull request"', () => {
    const p = prPrompt({ number: 12, title: 't', url: MR })
    expect(p).toContain(`Help me land this draft merge request: ${MR}`)
    expect(p).toContain('This MR was drafted')
    expect(p.toLowerCase()).toContain('this merge request is a draft by design')
  })

  it('the finding prompt labels its link with the provider noun', () => {
    const mr = findingPrompt({ fingerprint: 'ab', kind: 'perf', target: 'x', status: 'filed', pr: MR })
    expect(mr).toContain(`Merge request: ${MR}`)
    const pr = findingPrompt({ fingerprint: 'ab', kind: 'perf', target: 'x', status: 'filed', pr: PR })
    expect(pr).toContain(`Pull request: ${PR}`)
  })

  it('a surface with no subject url gets the neutral both-providers constraint', () => {
    const p = rulerPrompt({ status: 'calibrated' })
    expect(p.toLowerCase()).toContain('every pull or merge request this loop drafts')
  })
})
