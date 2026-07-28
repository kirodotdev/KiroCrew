/** Provider-aware link building.
 *
 * These are the functions that stop the app doing what it used to do everywhere:
 * concatenate a path onto `https://github.com/`. For a GitLab project that
 * produced a link to a DIFFERENT repo — a same-named one on the public site, or
 * a 404 — with no error to notice, so each shape is pinned here.
 */

import { describe, it, expect } from 'vitest'

import {
  changeDiffCommand,
  changeViewCommand,
  commitUrlFor,
  isGitlab,
  issueUrlFor,
  issueViewCommand,
  issuesUrlFor,
  membersUrlFor,
  providerTerms,
  recordIdentityJson,
  repoWebUrl,
  userUrlFor,
} from '../apps/issue-radar/lib/links'

const GH = { owner: 'acme', repo: 'widget', provider: 'github' as const, host: 'github.com' }
const GL = { owner: 'group/sub', repo: 'proj', provider: 'gitlab' as const, host: 'gitlab.com' }
const SELF = {
  owner: 'team',
  repo: 'svc',
  provider: 'gitlab' as const,
  host: 'gitlab.acme.internal:8443',
}
/** A record written before GitLab support: no provider, no host. */
const LEGACY = { owner: 'acme', repo: 'widget' }

describe('isGitlab', () => {
  it('is false for GitHub and for a legacy record', () => {
    expect(isGitlab(GH)).toBe(false)
    expect(isGitlab(LEGACY)).toBe(false)
  })

  it('is true for GitLab', () => {
    expect(isGitlab(GL)).toBe(true)
  })
})

describe('repoWebUrl', () => {
  it('uses the ref host, including a self-managed one with a port', () => {
    expect(repoWebUrl(GH)).toBe('https://github.com/acme/widget')
    expect(repoWebUrl(GL)).toBe('https://gitlab.com/group/sub/proj')
    expect(repoWebUrl(SELF)).toBe('https://gitlab.acme.internal:8443/team/svc')
  })

  it('defaults a legacy record to public GitHub', () => {
    expect(repoWebUrl(LEGACY)).toBe('https://github.com/acme/widget')
  })
})

describe('deep links', () => {
  it('nests GitLab project pages under /-/ and GitHub pages directly', () => {
    expect(commitUrlFor(GH, 'abc1234')).toBe('https://github.com/acme/widget/commit/abc1234')
    expect(commitUrlFor(GL, 'abc1234')).toBe('https://gitlab.com/group/sub/proj/-/commit/abc1234')
  })

  it('builds issue links per provider', () => {
    expect(issueUrlFor(GH, 42)).toBe('https://github.com/acme/widget/issues/42')
    expect(issueUrlFor(GL, 42)).toBe('https://gitlab.com/group/sub/proj/-/issues/42')
    expect(issuesUrlFor(SELF)).toBe('https://gitlab.acme.internal:8443/team/svc/-/issues')
  })

  it('scopes a user profile to the repo host, not github.com', () => {
    // On a self-managed instance the author may not exist on any public site.
    expect(userUrlFor(SELF, 'alice')).toBe('https://gitlab.acme.internal:8443/alice')
    expect(userUrlFor(GH, 'alice')).toBe('https://github.com/alice')
  })

  it('points at each provider’s own access-administration page', () => {
    expect(membersUrlFor(GH)).toBe('https://github.com/acme/widget/settings/access')
    expect(membersUrlFor(GL)).toBe('https://gitlab.com/group/sub/proj/-/project_members')
  })

  it('keeps a nested GitLab namespace intact', () => {
    // Truncating the namespace would address a different project.
    expect(issueUrlFor(GL, 1)).toContain('/group/sub/proj/')
  })
})

describe('providerTerms', () => {
  it('says merge request for GitLab and pull request for GitHub', () => {
    expect(providerTerms(GL).changeRequest).toBe('merge request')
    expect(providerTerms(GL).changeRequestShort).toBe('MR')
    expect(providerTerms(GH).changeRequest).toBe('pull request')
    expect(providerTerms(GH).changeRequestShort).toBe('PR')
  })

  it('uses each provider’s reference sigil', () => {
    expect(providerTerms(GL).sigil).toBe('!')
    expect(providerTerms(GH).sigil).toBe('#')
  })

  it('names the CLI that owns the credentials', () => {
    expect(providerTerms(GL).cli).toBe('glab')
    expect(providerTerms(GH).cli).toBe('gh')
  })

  it('treats a legacy record as GitHub', () => {
    expect(providerTerms(LEGACY).providerName).toBe('GitHub')
  })
})

describe('provider CLI commands for agent prompts', () => {
  it('names the CLI that actually owns the credentials', () => {
    // The prompts used to hard-code `gh`, so a GitLab item sent the agent to look
    // up a GitLab path on GitHub -- reading a stranger's repo or nothing, silently.
    expect(issueViewCommand(GH, 42)).toContain('gh issue view 42')
    expect(issueViewCommand(GL, 42)).toContain('glab issue view 42')
  })

  it('uses each provider’s own noun for a change request', () => {
    // The SUBCOMMAND differs, not just the binary: `gh pr` vs `glab mr`.
    expect(changeViewCommand(GH, 7)).toContain('gh pr view 7')
    expect(changeViewCommand(GL, 7)).toContain('glab mr view 7')
    expect(changeDiffCommand(GH, 7)).toContain('gh pr diff 7')
    expect(changeDiffCommand(GL, 7)).toContain('glab mr diff 7')
  })

  it('asks for the comment thread, which is where the substance is', () => {
    expect(issueViewCommand(GL, 1)).toContain('--comments')
    expect(changeViewCommand(GL, 1)).toContain('--comments')
    // A diff has no comments to request.
    expect(changeDiffCommand(GL, 1)).not.toContain('--comments')
  })

  it('addresses a self-managed GitLab project by full URL, so host AND port travel', () => {
    // `owner/repo` alone cannot say WHICH instance, and the agent's shell may
    // carry no GITLAB_HOST. A non-default port has to survive too -- an instance
    // on :8443 is a different server from the same host on :443.
    expect(changeViewCommand(SELF, 7))
      .toContain('--repo https://gitlab.acme.internal:8443/team/svc')
  })

  it('leaves the GitHub invocation in its proven owner/repo form', () => {
    // Changing a working path for symmetry's sake would be a regression risk with
    // no benefit -- gh only ever resolves to github.com anyway.
    expect(issueViewCommand(GH, 42)).toContain('--repo acme/widget')
    expect(issueViewCommand(GH, 42)).not.toContain('https://')
  })
})

describe('recordIdentityJson', () => {
  it('carries provider and host so the findings land in the right ledger', () => {
    // The record endpoint keys on provider+host; omitting them is treated as
    // public GitHub, so a GitLab investigation would overwrite a same-slug
    // GitHub repo's notes.
    const json = recordIdentityJson(GL)
    expect(json).toContain('"provider":"gitlab"')
    expect(json).toContain('"host":"gitlab.com"')
    expect(json).toContain('"owner":"group/sub"')
  })

  it('is valid JSON object content', () => {
    expect(() => JSON.parse(`{${recordIdentityJson(SELF)}}`)).not.toThrow()
    expect(JSON.parse(`{${recordIdentityJson(SELF)}}`)).toEqual({
      owner: 'team', repo: 'svc', provider: 'gitlab', host: 'gitlab.acme.internal:8443',
      kind: 'issue',
    })
  })

  it('defaults a legacy ref to public GitHub rather than emitting empty fields', () => {
    expect(JSON.parse(`{${recordIdentityJson(LEGACY)}}`)).toEqual({
      owner: 'acme', repo: 'widget', provider: 'github', host: 'github.com', kind: 'issue',
    })
  })

  it('carries the item KIND, because a number alone is not an identity on GitLab', () => {
    // Issue #5 and merge request !5 are unrelated items on GitLab. A PUT without
    // the kind records against the ISSUE with that number, so an agent reviewing
    // an MR would overwrite the issue's findings.
    expect(JSON.parse(`{${recordIdentityJson(GL, 'pull')}}`).kind).toBe('pull')
    expect(JSON.parse(`{${recordIdentityJson(GL)}}`).kind).toBe('issue')
  })
})
