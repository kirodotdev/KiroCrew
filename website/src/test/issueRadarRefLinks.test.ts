import { describe, it, expect } from 'vitest'
import {
  parseRepoRef, refUrl, refKey, linkifyIssueRefs, placeholderIssue, placeholderPull,
} from '../apps/issue-radar/lib/refLinks'
import type { RepoRef } from '../apps/issue-radar/api'

/** A repo identity for the tests. `provider`/`host` absent means public GitHub,
 * exactly as a pre-GitLab persisted record reads. */
const gh = (owner: string, repo: string): RepoRef => ({ owner, repo })
const gl = (owner: string, repo: string, host = 'gitlab.com'): RepoRef =>
  ({ owner, repo, provider: 'gitlab', host })

// The parser is the gate that decides whether a click LEAVES the app. Anything
// it wrongly claims hijacks a link the user expected to open on GitHub, so the
// negative cases matter as much as the positive ones.
describe('parseRepoRef', () => {
  const OWNER = 'kirodotdev'
  const REPO = 'KiroCrew'

  it('resolves an issue URL in the same repo', () => {
    expect(parseRepoRef(`https://github.com/${OWNER}/${REPO}/issues/533`, gh(OWNER, REPO)))
      .toEqual({ kind: 'issue', number: 533 })
  })

  it('resolves a pull-request URL in the same repo', () => {
    expect(parseRepoRef(`https://github.com/${OWNER}/${REPO}/pull/548`, gh(OWNER, REPO)))
      .toEqual({ kind: 'pull', number: 548 })
  })

  it('accepts the /pulls/<n> spelling', () => {
    expect(parseRepoRef(`https://github.com/${OWNER}/${REPO}/pulls/548`, gh(OWNER, REPO)))
      .toEqual({ kind: 'pull', number: 548 })
  })

  it('ignores trailing segments, query strings and comment fragments', () => {
    const base = `https://github.com/${OWNER}/${REPO}`
    expect(parseRepoRef(`${base}/pull/548/files`, gh(OWNER, REPO))).toEqual({ kind: 'pull', number: 548 })
    expect(parseRepoRef(`${base}/pull/548/commits/abc123`, gh(OWNER, REPO))).toEqual({ kind: 'pull', number: 548 })
    expect(parseRepoRef(`${base}/issues/12#issuecomment-99`, gh(OWNER, REPO))).toEqual({ kind: 'issue', number: 12 })
    expect(parseRepoRef(`${base}/issues/12?foo=bar`, gh(OWNER, REPO))).toEqual({ kind: 'issue', number: 12 })
  })

  it('matches owner/repo case-insensitively', () => {
    expect(parseRepoRef('https://github.com/KiroDotDev/kirocrew/issues/7', gh(OWNER, REPO)))
      .toEqual({ kind: 'issue', number: 7 })
  })

  it('accepts the www host', () => {
    expect(parseRepoRef(`https://www.github.com/${OWNER}/${REPO}/issues/7`, gh(OWNER, REPO)))
      .toEqual({ kind: 'issue', number: 7 })
  })

  it('rejects another repo or another owner', () => {
    expect(parseRepoRef(`https://github.com/${OWNER}/OtherRepo/issues/1`, gh(OWNER, REPO))).toBeNull()
    expect(parseRepoRef(`https://github.com/someone/${REPO}/issues/1`, gh(OWNER, REPO))).toBeNull()
  })

  it('rejects non-GitHub hosts, including an Enterprise host with the same path', () => {
    expect(parseRepoRef(`https://acme.ghe.com/${OWNER}/${REPO}/issues/1`, gh(OWNER, REPO))).toBeNull()
    expect(parseRepoRef(`https://github.com.evil.test/${OWNER}/${REPO}/issues/1`, gh(OWNER, REPO))).toBeNull()
    expect(parseRepoRef(`https://gist.github.com/${OWNER}/${REPO}/issues/1`, gh(OWNER, REPO))).toBeNull()
  })

  it('rejects non-http(s) protocols', () => {
    expect(parseRepoRef(`javascript:alert(1)//github.com/${OWNER}/${REPO}/issues/1`, gh(OWNER, REPO))).toBeNull()
    expect(parseRepoRef(`vscode://github.com/${OWNER}/${REPO}/issues/1`, gh(OWNER, REPO))).toBeNull()
  })

  it('rejects paths that are not an issue or PR', () => {
    const base = `https://github.com/${OWNER}/${REPO}`
    expect(parseRepoRef(`${base}/discussions/4`, gh(OWNER, REPO))).toBeNull()
    expect(parseRepoRef(`${base}/commit/abc123`, gh(OWNER, REPO))).toBeNull()
    expect(parseRepoRef(`${base}/issues`, gh(OWNER, REPO))).toBeNull()
    expect(parseRepoRef(`${base}/issues/new`, gh(OWNER, REPO))).toBeNull()
    expect(parseRepoRef(`${base}/issues/12abc`, gh(OWNER, REPO))).toBeNull()
    expect(parseRepoRef(`${base}/issues/0`, gh(OWNER, REPO))).toBeNull()
    expect(parseRepoRef(`${base}/issues/-3`, gh(OWNER, REPO))).toBeNull()
  })

  it('rejects a number too large to be a safe integer', () => {
    expect(parseRepoRef(`https://github.com/${OWNER}/${REPO}/issues/99999999999999999999`, gh(OWNER, REPO)))
      .toBeNull()
  })

  it('rejects relative and malformed hrefs', () => {
    expect(parseRepoRef(`/${OWNER}/${REPO}/issues/1`, gh(OWNER, REPO))).toBeNull()
    expect(parseRepoRef('issues/1', gh(OWNER, REPO))).toBeNull()
    expect(parseRepoRef('', gh(OWNER, REPO))).toBeNull()
    expect(parseRepoRef(null, gh(OWNER, REPO))).toBeNull()
    expect(parseRepoRef(undefined, gh(OWNER, REPO))).toBeNull()
  })

  it('rejects everything when the active repo is unknown', () => {
    expect(parseRepoRef(`https://github.com/${OWNER}/${REPO}/issues/1`, gh('', REPO))).toBeNull()
    expect(parseRepoRef(`https://github.com/${OWNER}/${REPO}/issues/1`, gh(OWNER, ''))).toBeNull()
  })
})

describe('refUrl / refKey', () => {
  it('builds the canonical GitHub URL per kind', () => {
    expect(refUrl(gh('o', 'r'), { kind: 'issue', number: 5 })).toBe('https://github.com/o/r/issues/5')
    expect(refUrl(gh('o', 'r'), { kind: 'pull', number: 5 })).toBe('https://github.com/o/r/pull/5')
  })

  it('keys issue and PR of the same number apart', () => {
    expect(refKey({ kind: 'issue', number: 5 })).not.toBe(refKey({ kind: 'pull', number: 5 }))
  })

  it('round-trips through the parser', () => {
    const ref = { kind: 'pull', number: 42 } as const
    expect(parseRepoRef(refUrl(gh('o', 'r'), ref), gh('o', 'r'))).toEqual(ref)
  })
})

describe('linkifyIssueRefs', () => {
  const O = 'kirodotdev'
  const R = 'KiroCrew'
  const link = (n: number) => `[#${n}](https://github.com/${O}/${R}/issues/${n})`

  it('rewrites a bare shorthand reference', () => {
    expect(linkifyIssueRefs('duplicate of #533 probably', gh(O, R)))
      .toBe(`duplicate of ${link(533)} probably`)
  })

  it('rewrites several, including at the very start of the source', () => {
    expect(linkifyIssueRefs('#1 and #22, plus #333.', gh(O, R)))
      .toBe(`${link(1)} and ${link(22)}, plus ${link(333)}.`)
  })

  it('handles a reference at a line start and inside a list item', () => {
    expect(linkifyIssueRefs('- see #7\n#8 too', gh(O, R)))
      .toBe(`- see ${link(7)}\n${link(8)} too`)
  })

  it('leaves fenced code alone', () => {
    const src = 'before #1\n```\nnot #2 here\n```\nafter #3'
    expect(linkifyIssueRefs(src, gh(O, R)))
      .toBe(`before ${link(1)}\n\`\`\`\nnot #2 here\n\`\`\`\nafter ${link(3)}`)
  })

  it('does not let a SHORTER inner run close a longer fence', () => {
    // A four-backtick fence is closed only by a run of >= 4, so the inner ``` is
    // content (the usual way to show a fenced example). Closing on it would unmask
    // the rest of the block.
    const src = '````\n```\n#5 inside\n```\n````\nafter #6'
    expect(linkifyIssueRefs(src, gh(O, R))).toBe(`\`\`\`\`\n\`\`\`\n#5 inside\n\`\`\`\n\`\`\`\`\nafter ${link(6)}`)
  })

  it('does not let a tilde run close a backtick fence', () => {
    expect(linkifyIssueRefs('```\n~~~\n#5\n', gh(O, R))).toBe('```\n~~~\n#5\n')
  })

  it('leaves a tilde fence alone', () => {
    expect(linkifyIssueRefs('~~~\n#9\n~~~', gh(O, R))).toBe('~~~\n#9\n~~~')
  })

  it('leaves inline code alone', () => {
    expect(linkifyIssueRefs('use `--flag #5` now', gh(O, R))).toBe('use `--flag #5` now')
  })

  it('respects code-span delimiter LENGTH, so a backtick inside a ``span`` is safe', () => {
    // A run of N backticks is closed only by a run of exactly N (CommonMark), so a
    // double-backtick span may contain a single backtick. Masking that ends at the
    // inner backtick would leave the rest of the span exposed to rewriting.
    expect(linkifyIssueRefs('use ``code ` #5`` now', gh(O, R))).toBe('use ``code ` #5`` now')
    expect(linkifyIssueRefs('a ```x ` #7``` b', gh(O, R))).toBe('a ```x ` #7``` b')
    // An UNBALANCED run is not a code span, so text after it is still linkified.
    expect(linkifyIssueRefs('stray ` and #9', gh(O, R))).toBe(`stray \` and ${link(9)}`)
  })

  it('does not touch an existing markdown link', () => {
    const src = `see [#5](https://github.com/${O}/${R}/issues/5)`
    expect(linkifyIssueRefs(src, gh(O, R))).toBe(src)
  })

  it('does not touch a REFERENCE-STYLE link or its definition line', () => {
    // `[label][ref]` and the `[ref]: url` line it resolves through are links too:
    // rewriting inside the label nests a link, and rewriting the definition breaks
    // the destination.
    expect(linkifyIssueRefs('see [fixes #5][a]', gh(O, R))).toBe('see [fixes #5][a]')
    expect(linkifyIssueRefs('[a]: https://example.test/x#5', gh(O, R))).toBe('[a]: https://example.test/x#5')
    expect(linkifyIssueRefs('  [a]: /p#5 "t"', gh(O, R))).toBe('  [a]: /p#5 "t"')
    // A shortcut reference is bracketed, so the leading `[` already excludes it.
    expect(linkifyIssueRefs('see [#5]', gh(O, R))).toBe('see [#5]')
    // ... but a definition-looking line that is really prose still linkifies.
    expect(linkifyIssueRefs('and #5 after', gh(O, R))).toBe(`and ${link(5)} after`)
  })

  it('does not touch a reference inside a link label', () => {
    const src = '[fixes #5](https://example.test/x)'
    expect(linkifyIssueRefs(src, gh(O, R))).toBe(src)
  })

  it('rewrites a reference wrapped in parentheses', () => {
    // The overwhelmingly common shape in a real body — `the Windows lane (#421)
    // uses Squirrel` — and GitHub linkifies it, so an opening paren must not
    // suppress the rewrite.
    expect(linkifyIssueRefs('the Windows lane (#421) uses Squirrel', gh(O, R)))
      .toBe(`the Windows lane (${link(421)}) uses Squirrel`)
    expect(linkifyIssueRefs('(#5)', gh(O, R))).toBe(`(${link(5)})`)
    expect(linkifyIssueRefs('see (#5, #6)', gh(O, R))).toBe(`see (${link(5)}, ${link(6)})`)
  })

  it('still leaves an UNBALANCED link target alone', () => {
    // A well-formed `[a](#5)` is masked, so only a leftover `](#5)` reaches the
    // scan — rewriting it would nest a link inside a link.
    expect(linkifyIssueRefs('a](#5)', gh(O, R))).toBe('a](#5)')
    expect(linkifyIssueRefs('[a](#5)', gh(O, R))).toBe('[a](#5)')
  })

  it('does not touch an autolink or a URL fragment', () => {
    const src = `<https://github.com/${O}/${R}/issues/12#issuecomment-9>`
    expect(linkifyIssueRefs(src, gh(O, R))).toBe(src)
    const bare = `https://github.com/${O}/${R}/issues/12#issuecomment-9`
    expect(linkifyIssueRefs(bare, gh(O, R))).toBe(bare)
  })

  it('does not touch a raw HTML attribute', () => {
    const src = '<a href="/x#5">y</a>'
    expect(linkifyIssueRefs(src, gh(O, R))).toBe(src)
  })

  it('does not touch an HTML entity or a cross-repo shorthand', () => {
    expect(linkifyIssueRefs('&#123; literal', gh(O, R))).toBe('&#123; literal')
    expect(linkifyIssueRefs('other/repo#5 elsewhere', gh(O, R))).toBe('other/repo#5 elsewhere')
    expect(linkifyIssueRefs('KiroCrew#5 elsewhere', gh(O, R))).toBe('KiroCrew#5 elsewhere')
  })

  it('treats an all-digit run as a reference even when it could be a hex colour', () => {
    // GitHub linkifies `#123456` in a body too, and a repo with six-figure issue
    // numbers is real (kirodotdev/Kiro is past 10k), so length cannot decide.
    // A colour written the way people actually write one (`#1a2b3c`, or in code
    // ticks) is unaffected.
    expect(linkifyIssueRefs('colour #1a2b3c here', gh(O, R))).toBe('colour #1a2b3c here')
    expect(linkifyIssueRefs('colour `#123456` here', gh(O, R))).toBe('colour `#123456` here')
    expect(linkifyIssueRefs('issue #123456 here', gh(O, R))).toBe(`issue ${link(123456)} here`)
  })

  it('does not treat a markdown heading as a reference', () => {
    expect(linkifyIssueRefs('# Title\n## 2 things', gh(O, R))).toBe('# Title\n## 2 things')
  })

  it('returns the source untouched when there is nothing to do', () => {
    expect(linkifyIssueRefs('no refs here', gh(O, R))).toBe('no refs here')
    expect(linkifyIssueRefs('', gh(O, R))).toBe('')
    expect(linkifyIssueRefs('#5', gh('', R))).toBe('#5')
  })

  it('produces links the parser accepts', () => {
    const out = linkifyIssueRefs('fixes #42', gh(O, R))
    const href = /\(([^)]+)\)/.exec(out)![1]
    expect(parseRepoRef(href, gh(O, R))).toEqual({ kind: 'issue', number: 42 })
  })
})

describe('GitLab references', () => {
  // On a GitLab project a github.com URL is not a degraded link — it is a link to
  // a DIFFERENT repo that may not even be the user's — so these are the cases that
  // must not regress.
  const G = gl('platform/team-tools', 'widget-service')
  const SELF = gl('group', 'project', 'gitlab.acme.internal')

  it('resolves an issue URL under the /-/ page path', () => {
    expect(parseRepoRef(
      'https://gitlab.com/platform/team-tools/widget-service/-/issues/12', G,
    )).toEqual({ kind: 'issue', number: 12 })
  })

  it('resolves a merge-request URL to the change-request pane', () => {
    expect(parseRepoRef(
      'https://gitlab.com/platform/team-tools/widget-service/-/merge_requests/8', G,
    )).toEqual({ kind: 'pull', number: 8 })
  })

  it('rejects a path missing the /-/ segment', () => {
    // `…/widget-service/issues/12` is not an issue path on GitLab; claiming it
    // would open a pane for an item the link does not address.
    expect(parseRepoRef(
      'https://gitlab.com/platform/team-tools/widget-service/issues/12', G,
    )).toBeNull()
  })

  it('rejects a sibling project inside the same nested namespace', () => {
    expect(parseRepoRef(
      'https://gitlab.com/platform/team-tools/other-service/-/issues/12', G,
    )).toBeNull()
  })

  it('rejects the same project path on a DIFFERENT host', () => {
    // The same group/project exists on gitlab.com and on every self-managed
    // instance; they are unrelated repos.
    expect(parseRepoRef('https://gitlab.com/group/project/-/issues/3', SELF)).toBeNull()
    expect(parseRepoRef('https://gitlab.acme.internal/group/project/-/issues/3', SELF))
      .toEqual({ kind: 'issue', number: 3 })
  })

  it('does not fold www onto a self-managed host', () => {
    // github.com's www is an official alias; `www.<anything-else>` may be a
    // different server, so the fold is GitHub-only.
    expect(parseRepoRef('https://www.gitlab.acme.internal/group/project/-/issues/3', SELF))
      .toBeNull()
  })

  it('rejects a github.com link on a GitLab repo', () => {
    expect(parseRepoRef(
      'https://github.com/platform/team-tools/widget-service/issues/12', G,
    )).toBeNull()
  })

  it('builds refUrl on the repo own host and provider path grammar', () => {
    expect(refUrl(G, { kind: 'issue', number: 5 }))
      .toBe('https://gitlab.com/platform/team-tools/widget-service/-/issues/5')
    expect(refUrl(G, { kind: 'pull', number: 5 }))
      .toBe('https://gitlab.com/platform/team-tools/widget-service/-/merge_requests/5')
    expect(refUrl(SELF, { kind: 'issue', number: 5 }))
      .toBe('https://gitlab.acme.internal/group/project/-/issues/5')
  })

  it('linkifies a bare #123 onto GitLab, never github.com', () => {
    const out = linkifyIssueRefs('duplicate of #123', G)
    expect(out).toContain('https://gitlab.com/platform/team-tools/widget-service/-/issues/123')
    expect(out).not.toContain('github.com')
    // …and the link it produced is one the parser claims back.
    const href = /\(([^)]+)\)/.exec(out)![1]
    expect(parseRepoRef(href, G)).toEqual({ kind: 'issue', number: 123 })
  })

  it('leaves GitLab merge-request shorthand alone', () => {
    // `!8` is a merge request, but `!` is also ordinary prose and image syntax; a
    // wrong claim on a click is worse than plain text.
    expect(linkifyIssueRefs('see !8 for the fix', G)).toBe('see !8 for the fix')
  })

  it('gives placeholder rows a GitLab url', () => {
    expect(placeholderIssue(G, 9).url)
      .toBe('https://gitlab.com/platform/team-tools/widget-service/-/issues/9')
    expect(placeholderPull(G, 9).url)
      .toBe('https://gitlab.com/platform/team-tools/widget-service/-/merge_requests/9')
  })
})

describe('placeholder rows', () => {
  it('carries the number, an EMPTY title (the pane skeletons it) and the canonical url', () => {
    const iss = placeholderIssue(gh('o', 'r'), 9)
    expect(iss.number).toBe(9)
    expect(iss.title).toBe('')
    expect(iss.url).toBe('https://github.com/o/r/issues/9')
    expect(iss.labels).toEqual([])

    // No state unless the caller knows it: guessing 'open' would let the issue
    // pane offer "Close as completed" on an already-closed issue.
    expect(iss.state).toBeUndefined()
    expect(placeholderIssue(gh('o', 'r'), 9, 'closed').state).toBe('closed')

    const pr = placeholderPull(gh('o', 'r'), 9)
    expect(pr.number).toBe(9)
    expect(pr.title).toBe('')
    expect(pr.url).toBe('https://github.com/o/r/pull/9')
    expect(pr.draft).toBe(false)
    expect(pr.state).toBe('open')
    expect(placeholderPull(gh('o', 'r'), 9, 'closed').state).toBe('closed')
  })
})
