/** Provider-aware links and display vocabulary.
 *
 * Deliberately NOT in `api.ts`: these are pure functions of a `RepoRef`, with no
 * network involved, and tests that mock the api client to control fetching must
 * not simultaneously lose the ability to build a URL. Keeping them here means
 * `vi.mock('../api')` stubs the transport and nothing else.
 *
 * The whole point of routing every link through here is that GitHub and GitLab
 * do NOT share a URL grammar — GitLab nests a project's own pages under `/-/`,
 * and a self-managed instance is not even on the same host. Concatenating onto
 * `https://github.com/…` (which is what the app used to do everywhere) produces
 * a link that silently points at a stranger's repo on the public site.
 */

import { type ItemKind, type RepoRef } from '../api'

/** True when a ref points at GitLab (drives MR-vs-PR wording and link shape).
 *
 * Accepts `undefined` deliberately. `provider` is optional on a ref, so "absent"
 * already means public GitHub here — and treating an absent REF the same way
 * removes a crash for callers that render before the active repo resolves (and
 * for tests whose context fixtures build only the fields they exercise). The URL
 * builders below do NOT get this leniency: a link built from nothing would be a
 * wrong link, which is worse than a type error. */
export function isGitlab(ref?: Pick<RepoRef, 'provider'>): boolean {
  return ref?.provider === 'gitlab'
}

/** The ref's host, defaulting to public GitHub for legacy records. */
function hostOf(ref: RepoRef): string {
  return ref.host || 'github.com'
}

/** The repo's landing page on its own host. */
export function repoWebUrl(ref: RepoRef): string {
  return `https://${hostOf(ref)}/${ref.owner}/${ref.repo}`
}

/** GitLab nests project pages under `/-/`; GitHub does not. */
function repoPagePath(ref: RepoRef, page: string): string {
  return isGitlab(ref) ? `${repoWebUrl(ref)}/-/${page}` : `${repoWebUrl(ref)}/${page}`
}

/** Link to one commit. */
export function commitUrlFor(ref: RepoRef, sha: string): string {
  return repoPagePath(ref, `commit/${sha}`)
}

/** Link to one issue. */
export function issueUrlFor(ref: RepoRef, number: number): string {
  return repoPagePath(ref, `issues/${number}`)
}

/** Link to the repo's issue list. */
export function issuesUrlFor(ref: RepoRef): string {
  return repoPagePath(ref, 'issues')
}

/** Link to one pull/merge request.
 *
 * The path noun differs, not just the host: GitHub serves `/pull/<n>`, GitLab
 * `/-/merge_requests/<n>`. */
export function changeUrlFor(ref: RepoRef, number: number): string {
  return repoPagePath(ref, isGitlab(ref) ? `merge_requests/${number}` : `pull/${number}`)
}

/** Link to a user's profile on the repo's host.
 *
 * Host-scoped, not github.com: on a self-managed GitLab the author of an issue
 * is a user of THAT instance and may not exist on any public site.
 */
export function userUrlFor(ref: RepoRef, login: string): string {
  return `https://${hostOf(ref)}/${login}`
}

/** Link to the page where repo access is administered. */
export function membersUrlFor(ref: RepoRef): string {
  return isGitlab(ref)
    ? `${repoWebUrl(ref)}/-/project_members`
    : `${repoWebUrl(ref)}/settings/access`
}

/** A single react-query cache-key fragment identifying one repo.
 *
 * Replaces the bare `owner, repo` pair that every cache key used while GitHub was
 * the only provider. It no longer identifies a repo: `acme/widget` on GitHub and
 * `acme/widget` on gitlab.com would share one cache entry, so switching between
 * them would render the other one's issues, labels and settings until something
 * invalidated. Including provider + host makes that collision impossible. */
export function repoScopeKey(ref: RepoRef): string {
  return `${ref.provider || 'github'}:${ref.host || 'github.com'}:${ref.owner}/${ref.repo}`
}

export interface ProviderTerms {
  /** "pull request" / "merge request" — mid-sentence. */
  changeRequest: string
  /** "Pull Request" / "Merge Request" — a title or a label. */
  changeRequestTitle: string
  /** "pull requests" / "merge requests" — mid-sentence plural. */
  changeRequestPlural: string
  /** "Pull Requests" / "Merge Requests" — a heading or a placeholder.
   *
   * Every casing is spelled out rather than derived at the call site: deriving it
   * would put a capitalize/pluralize helper in front of user-visible copy, which
   * is how "Merge requestss" and "merge Request" happen. */
  changeRequestPluralTitle: string
  /** "PR" / "MR". */
  changeRequestShort: string
  /** The sigil the provider uses to reference one: `#` on GitHub, `!` on GitLab. */
  sigil: string
  /** "GitHub" / "GitLab". */
  providerName: string
  /** The CLI that owns the credentials: `gh` / `glab`. */
  cli: string
}

/** Display vocabulary for a ref's provider.
 *
 * Mirrors `backend/provider.py:_TERMS`, so the UI, the notifications, and the AI
 * prompts all call a merge request the same thing.
 */
export function providerTerms(ref?: Pick<RepoRef, 'provider'>): ProviderTerms {
  return isGitlab(ref)
    ? {
        changeRequest: 'merge request',
        changeRequestTitle: 'Merge Request',
        changeRequestPlural: 'merge requests',
        changeRequestPluralTitle: 'Merge Requests',
        changeRequestShort: 'MR',
        sigil: '!',
        providerName: 'GitLab',
        cli: 'glab',
      }
    : {
        changeRequest: 'pull request',
        changeRequestTitle: 'Pull Request',
        changeRequestPlural: 'pull requests',
        changeRequestPluralTitle: 'Pull Requests',
        changeRequestShort: 'PR',
        sigil: '#',
        providerName: 'GitHub',
        cli: 'gh',
      }
}

// ── provider CLI commands for agent prompts ─────────────────────────────────
//
// The investigate / review seed prompts tell an agent to read the item with a
// CLI. Those commands used to be hard-coded to `gh`, which on a GitLab item sent
// the agent to look up a GitLab path on GitHub -- reading a stranger's repo or
// nothing at all, with no error to notice.
//
// See `repoArg` for why GitLab gets a full URL and GitHub keeps `owner/repo`.

/** The CLI that owns credentials for this ref (`gh` / `glab`). */
function cliFor(ref: RepoRef): string {
  return providerTerms(ref).cli
}

/** What to pass to `--repo`.
 *
 * GitLab gets the project's full URL, because that is the only form carrying the
 * HOST -- a self-managed project is otherwise unaddressable without ambient
 * `GITLAB_HOST` state the agent's shell may not have. GitHub deliberately keeps
 * the plain `owner/repo` it has always used: that invocation is proven, only ever
 * resolves to github.com, and changing it would alter a working path for no gain.
 */
function repoArg(ref: RepoRef): string {
  return isGitlab(ref) ? repoWebUrl(ref) : `${ref.owner}/${ref.repo}`
}

/** Command that prints one issue with its full comment thread. */
export function issueViewCommand(ref: RepoRef, number: number): string {
  return `${cliFor(ref)} issue view ${number} --repo ${repoArg(ref)} --comments`
}

/** Command that prints one pull/merge request with its full comment thread.
 *
 * GitHub calls the noun `pr`, GitLab calls it `mr` -- so the SUBCOMMAND differs,
 * not just the binary. */
export function changeViewCommand(ref: RepoRef, number: number): string {
  const noun = isGitlab(ref) ? 'mr' : 'pr'
  return `${cliFor(ref)} ${noun} view ${number} --repo ${repoArg(ref)} --comments`
}

/** Command that prints one pull/merge request's diff. */
export function changeDiffCommand(ref: RepoRef, number: number): string {
  const noun = isGitlab(ref) ? 'mr' : 'pr'
  return `${cliFor(ref)} ${noun} diff ${number} --repo ${repoArg(ref)}`
}

/** The identity fields an agent must echo back when recording an investigation.
 *
 * The record endpoint keys on provider + host, so a write that omits them is
 * treated as public GitHub. On a GitLab item that silently writes into -- and can
 * overwrite -- a same-slug GitHub repo's investigation ledger. Emitting them in
 * the prompt is what makes the agent's write land in the right tree.
 *
 * `kind` is part of that identity for the same reason: on GitLab, issue `#5` and
 * merge request `!5` are unrelated items, so a write without it records against
 * the ISSUE with that number. It is emitted explicitly rather than relying on the
 * server default, because the cost of being wrong is another item's record.
 *
 * These are emitted as JSON fragments because the seed prompt shows the agent the
 * exact argument object to pass to `issue_radar_record_investigation`. */
export function recordIdentityJson(ref: RepoRef, kind: ItemKind = 'issue'): string {
  return (
    `"owner":"${ref.owner}","repo":"${ref.repo}"`
    + `,"provider":"${ref.provider || 'github'}","host":"${ref.host || 'github.com'}"`
    + `,"kind":"${kind}"`
  )
}
