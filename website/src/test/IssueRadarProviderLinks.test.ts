/** Provider-aware link building.
 *
 * These functions build every link from the ref's own provider and host instead
 * of concatenating a path onto `https://github.com/`. For a GitLab project a
 * github.com-only builder produces a link to a DIFFERENT repo — a same-named one
 * on the public site, or a 404 — with no error to notice, so each shape is
 * pinned here.
 */

import { describe, it, expect } from 'vitest'

import {
  changeDiffCommand,
  changeUrlFor,
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
/** A legacy record with no provider and no host. */
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
    // The prompts name the provider's own CLI: hard-coding `gh` would send the
    // agent to look up a GitLab path on GitHub -- reading a stranger's repo or
    // nothing, silently.
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

/** Azure DevOps: `owner` carries `{organization}/{project}`, the repository sits
 * behind a literal `_git`, and work items belong to the PROJECT rather than the
 * repository. None of that is expressible as "the not-GitHub branch", which is
 * why the builders dispatch on a per-provider table — these assertions are what
 * pin that the third provider gets its OWN shapes rather than inheriting GitHub's
 * or GitLab's.
 */
const AZ = {
  owner: 'contoso/Payments',
  repo: 'ledger',
  provider: 'azure' as const,
  host: 'dev.azure.com',
}

describe('Azure DevOps link shapes', () => {
  it('puts the literal _git between the project and the repository', () => {
    // Without `_git` the path is a project page, not the repo — a 404 at best.
    expect(repoWebUrl(AZ)).toBe('https://dev.azure.com/contoso/Payments/_git/ledger')
  })

  it('addresses a pull request as pullrequest under the repo path', () => {
    expect(changeUrlFor(AZ, 7))
      .toBe('https://dev.azure.com/contoso/Payments/_git/ledger/pullrequest/7')
  })

  it('addresses work items off the PROJECT, with no repository in the path', () => {
    // A work item is project-scoped, so appending to the repo URL (which ends in
    // `_git/ledger`) would build a page that does not exist.
    expect(issueUrlFor(AZ, 42))
      .toBe('https://dev.azure.com/contoso/Payments/_workitems/edit/42')
    expect(issuesUrlFor(AZ)).toBe('https://dev.azure.com/contoso/Payments/_workitems')
  })

  it('does not borrow GitLab’s /-/ nesting', () => {
    expect(commitUrlFor(AZ, 'abc1234'))
      .toBe('https://dev.azure.com/contoso/Payments/_git/ledger/commit/abc1234')
    expect(commitUrlFor(AZ, 'abc1234')).not.toContain('/-/')
  })

  it('administers access at the project level', () => {
    expect(membersUrlFor(AZ)).toBe('https://dev.azure.com/contoso/Payments/_settings/permissions')
  })

  it('is not GitLab, even though it shares GitLab’s ! sigil', () => {
    // `isGitlab` survives as a helper but must not be the dispatch mechanism.
    expect(isGitlab(AZ)).toBe(false)
    expect(providerTerms(AZ).sigil).toBe('!')
  })

  it('names Azure DevOps and its own CLI', () => {
    expect(providerTerms(AZ).providerName).toBe('Azure DevOps')
    expect(providerTerms(AZ).cli).toBe('az')
    // Azure DevOps calls them pull requests, not merge requests.
    expect(providerTerms(AZ).changeRequestPluralTitle).toBe('Pull Requests')
  })

  it('builds az invocations that carry org AND project, and refuse inference', () => {
    // `az` has no `--repo owner/repo`: the organization is a URL and the project a
    // separate flag. `--detect false` matters because the agent may be standing in
    // an unrelated checkout, and `az` would otherwise read THAT project silently.
    const view = changeViewCommand(AZ, 7)
    expect(view).toContain('az repos pr show --id 7')
    expect(view).toContain('--org https://dev.azure.com/contoso')
    expect(view).toContain('--project Payments')
    expect(view).toContain('--detect false')
    expect(view).not.toContain('--repo ')

    const item = issueViewCommand(AZ, 42)
    expect(item).toContain('az boards work-item show --id 42')
    expect(item).toContain('--expand all')
  })

  it('records the azure provider so findings land in its own ledger', () => {
    expect(JSON.parse(`{${recordIdentityJson(AZ, 'pull')}}`)).toEqual({
      owner: 'contoso/Payments', repo: 'ledger',
      provider: 'azure', host: 'dev.azure.com', kind: 'pull',
    })
  })
})
