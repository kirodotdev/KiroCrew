/** Provider-aware links and display vocabulary.
 *
 * Deliberately NOT in `api.ts`: these are pure functions of a `RepoRef`, with no
 * network involved, and tests that mock the api client to control fetching must
 * not simultaneously lose the ability to build a URL. Keeping them here means
 * `vi.mock('../api')` stubs the transport and nothing else.
 *
 * The whole point of routing every link through here is that the providers do
 * NOT share a URL grammar — GitLab nests a project's own pages under `/-/`,
 * Azure DevOps puts a literal `_git` between the project and the repository and
 * addresses work items off the PROJECT with no repository dimension at all, and
 * a self-managed instance is not even on the same host. Concatenating onto
 * `https://github.com/…` produces a link that silently points at a stranger's
 * repo on the public site.
 *
 * ## Why a descriptor TABLE and not a boolean
 *
 * Every builder below used to branch on `isGitlab(ref)`. A boolean has exactly
 * two arms, so the moment a third provider exists it cannot express it: Azure
 * DevOps would take whichever arm it is not, inheriting GitHub's URL shape (no
 * `_git`, work items under `/issues/`) with no error to notice — a link that
 * resolves to a 404, or worse to somebody else's page. The table has one row per
 * provider id, so adding a provider is a row rather than an edit to ~10 branches,
 * and a row that forgets a field fails to compile.
 */

import { i18nT } from '../../../i18n/t'
import { type ItemKind, type RepoRef } from '../api'

/** Provider ids this module knows how to build links for.
 *
 * Narrower than `SourceProvider` only in that it is the KEY SET of the table
 * below — `providerKeyOf` maps anything else (an absent provider on a legacy
 * record, a corrupted config entry) onto `github`, which is what such a record
 * actually is. */
type ProviderKey = 'github' | 'gitlab' | 'azure'

/** Which provider's grammar a ref follows.
 *
 * Exported because `refLinks` PARSES the same three grammars and must dispatch on
 * exactly the same answer this module builds with — two independent notions of
 * "is this Azure" would let a link be built in one shape and parsed in another. */
export function providerKeyOf(ref?: Pick<RepoRef, 'provider'>): ProviderKey {
  const p = ref?.provider
  return p === 'gitlab' || p === 'azure' ? p : 'github'
}

/** Where a page hangs off: the repository's own path, or the account/project
 * path one level up.
 *
 * Azure DevOps needs the distinction. Its work items belong to the PROJECT —
 * `owner` already carries `{org}/{project}` — so their URL does not mention the
 * repository at all and the repo page's own path (which ends in `_git/{repo}`)
 * is not a prefix of it. On GitHub and GitLab every page is repo-scoped, so the
 * field is uniformly `'repo'` there. */
type PageScope = 'repo' | 'project'

/** One provider's URL grammar and vocabulary. */
interface ProviderDescriptor {
  /** Public host used when a ref carries none. */
  defaultHost: string
  /** The repository's own path from the host root, as a template over `{owner}`
   * and `{repo}`. Azure DevOps' literal `_git` segment lives here — it is the
   * one piece a shared `${owner}/${repo}` builder cannot express. */
  repoPath: string
  /** Segment a provider inserts before a project's OWN pages (`-` on GitLab),
   * or null when pages hang directly off the repo path. */
  pageNest: string | null
  /** Path segment addressing one change request. */
  changeSegment: string
  /** Path segment addressing the tracked-item list. */
  issuesSegment: string
  /** What `issuesSegment` hangs off — see {@link PageScope}. */
  issuesScope: PageScope
  /** Extra segment between `issuesSegment` and the item number, or null. Azure
   * DevOps addresses a single work item as `_workitems/edit/<id>`. */
  itemSegment: string | null
  /** Path of the page where repo/project access is administered. */
  membersPath: string
  /** What `membersPath` hangs off. Azure DevOps administers access per PROJECT. */
  membersScope: PageScope
  /** The CLI subcommand noun for a change request (`pr` / `mr`). */
  cliChangeNoun: string
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
  /** The sigil the provider uses to reference one: `#` on GitHub, `!` on GitLab
   * and on Azure DevOps (whose markdown splits `!` for pull requests from `#`
   * for work items, the same way and for the same reason GitLab does). */
  sigil: string
  /** Catalog KEY for the PLURALIZED "{{count}} issues" / "{{count}} work items"
   * footer count.
   *
   * A separate key per provider rather than a noun interpolated into one, because
   * the count and the noun agree grammatically: Russian alone needs four forms,
   * and a `{{count}} {{noun}}` frame can only ever inflect the frame. Each
   * catalog therefore carries exactly the plural categories its own language
   * declares. */
  trackedItemCountKey: string
  /** Catalog KEY for the detail pane's own empty state.
   *
   * A whole sentence per provider rather than the noun interpolated into one
   * frame, because the article has to agree with it: "Select a Issue" is what
   * a `Select a {{item}}` frame produces. This also keeps GitHub's and GitLab's
   * sentence byte-identical to what they had before Azure DevOps existed. */
  emptyDetailKey: string
  /** Catalog KEY for "Issues" / "Work Items" — a heading, a search placeholder,
   * or an empty state.
   *
   * A key rather than a literal, unlike the changeRequest family above: that
   * family is frozen debt the i18n ceilings already account for, and adding to
   * it would ship a fourth English-only noun. Resolved with `i18nT()` where it
   * renders, the same shape as `FILTER_LABEL_KEY` in ChatSidebar. */
  trackedItemPluralTitleKey: string
  /** "GitHub" / "GitLab" / "Azure DevOps". */
  providerName: string
  /** The CLI that owns the credentials: `gh` / `glab` / `az`. */
  cli: string
}

/** URL grammar per provider id.
 *
 * Mirrors `backend/provider.py:_TRACKED_ITEMS_URL` for the work-item shape, so
 * the UI, the notifications and the AI prompts all link to the same page.
 *
 * Grammar only — the display VOCABULARY stays in `providerTerms` below, because
 * Azure DevOps reuses GitHub's change-request nouns verbatim and a per-row copy
 * of them would be two spellings of one truth.
 */
const PROVIDERS: Record<ProviderKey, ProviderDescriptor> = {
  github: {
    defaultHost: 'github.com',
    repoPath: '{owner}/{repo}',
    pageNest: null,
    changeSegment: 'pull',
    issuesSegment: 'issues',
    issuesScope: 'repo',
    itemSegment: null,
    membersPath: 'settings/access',
    membersScope: 'repo',
    cliChangeNoun: 'pr',
  },
  gitlab: {
    defaultHost: 'gitlab.com',
    repoPath: '{owner}/{repo}',
    pageNest: '-',
    changeSegment: 'merge_requests',
    issuesSegment: 'issues',
    issuesScope: 'repo',
    itemSegment: null,
    membersPath: 'project_members',
    membersScope: 'repo',
    cliChangeNoun: 'mr',
  },
  azure: {
    // Always dev.azure.com. Azure DevOps has no self-managed form on this host
    // (Azure DevOps Server is a different product with a different URL shape),
    // so the backend canonicalizes the host and there is nothing to configure.
    defaultHost: 'dev.azure.com',
    // `owner` is `{organization}/{project}`, so this expands to the real
    // three-level path `{org}/{project}/_git/{repo}`.
    repoPath: '{owner}/_git/{repo}',
    pageNest: null,
    changeSegment: 'pullrequest',
    issuesSegment: '_workitems',
    // Work items belong to the project, not the repository — see PageScope.
    issuesScope: 'project',
    itemSegment: 'edit',
    membersPath: '_settings/permissions',
    membersScope: 'project',
    cliChangeNoun: 'pr',
  },
}

/** The table row for a ref's provider.
 *
 * An unknown or absent provider resolves to GitHub, which is what a record
 * persisted before multi-provider support actually is. Falling back cannot leak
 * across providers: a GitHub-shaped link on a corrupted entry is a wrong-looking
 * link, not another provider's page. */
function descriptorOf(ref?: Pick<RepoRef, 'provider'>): ProviderDescriptor {
  return PROVIDERS[providerKeyOf(ref)]
}

/** True when a ref points at GitLab.
 *
 * Kept as a thin helper for the handful of call sites that genuinely ask about
 * GitLab specifically, but it is NO LONGER the dispatch mechanism — see the
 * table's header comment for why a boolean cannot express three providers.
 *
 * Accepts `undefined` deliberately. `provider` is optional on a ref, so "absent"
 * already means public GitHub here — and treating an absent REF the same way
 * removes a crash for callers that render before the active repo resolves (and
 * for tests whose context fixtures build only the fields they exercise). The URL
 * builders below do NOT get this leniency: a link built from nothing would be a
 * wrong link, which is worse than a type error. */
export function isGitlab(ref?: Pick<RepoRef, 'provider'>): boolean {
  return providerKeyOf(ref) === 'gitlab'
}

/** The ref's host, defaulting to the provider's public host for legacy records. */
function hostOf(ref: RepoRef): string {
  return ref.host || descriptorOf(ref).defaultHost
}

/** A provider's PUBLIC host — what a hostless shorthand resolves against, and the
 * host the connect dialog builds a canonical URL on.
 *
 * Exported so the connect flow reads the host out of the same table the link
 * builders use: a second copy would let the dialog connect `github.com` while
 * every later link pointed at `dev.azure.com`. */
export function providerDefaultHost(ref?: Pick<RepoRef, 'provider'>): string {
  return descriptorOf(ref).defaultHost
}

/** The organization half of an Azure DevOps `owner` (`{org}/{project}`).
 *
 * A malformed single-segment owner yields the whole string rather than an empty
 * one, so a bad config entry still produces an addressable organization instead
 * of `https://dev.azure.com//…`. */
function azureOrg(ref: RepoRef): string {
  const slash = ref.owner.indexOf('/')
  return slash < 0 ? ref.owner : ref.owner.slice(0, slash)
}

/** The project half of an Azure DevOps `owner`, or '' when there is none. */
function azureProject(ref: RepoRef): string {
  const slash = ref.owner.indexOf('/')
  return slash < 0 ? '' : ref.owner.slice(slash + 1)
}

/** The repo's landing page on its own host. */
export function repoWebUrl(ref: RepoRef): string {
  const path = descriptorOf(ref).repoPath
    .replace('{owner}', ref.owner)
    .replace('{repo}', ref.repo)
  return `https://${hostOf(ref)}/${path}`
}

/** The account/project path one level up from the repo — the root Azure DevOps
 * hangs its project-scoped pages (work items, permissions) off. */
function projectWebUrl(ref: RepoRef): string {
  return `https://${hostOf(ref)}/${ref.owner}`
}

/** `page` resolved against the scope it belongs to, with the provider's own
 * page-nesting marker applied for repo-scoped pages.
 *
 * The marker is deliberately NOT applied to a project-scoped page: GitLab's
 * `/-/` routes a page within a PROJECT, and there is no provider that nests its
 * account-level pages the same way. */
function scopedPath(ref: RepoRef, scope: PageScope, page: string): string {
  if (scope === 'project') return `${projectWebUrl(ref)}/${page}`
  const nest = descriptorOf(ref).pageNest
  return nest ? `${repoWebUrl(ref)}/${nest}/${page}` : `${repoWebUrl(ref)}/${page}`
}

/** A repo-scoped page: GitLab nests project pages under `/-/`, the others do not. */
function repoPagePath(ref: RepoRef, page: string): string {
  return scopedPath(ref, 'repo', page)
}

/** Link to one commit. */
export function commitUrlFor(ref: RepoRef, sha: string): string {
  return repoPagePath(ref, `commit/${sha}`)
}

/** Link to one issue / work item.
 *
 * Azure DevOps addresses a work item off the PROJECT and through an extra `edit`
 * segment (`_workitems/edit/<id>`), so both the scope and the path shape come
 * from the table rather than from a suffix appended to the repo URL. */
export function issueUrlFor(ref: RepoRef, number: number): string {
  const d = descriptorOf(ref)
  const tail = d.itemSegment ? `${d.itemSegment}/${number}` : `${number}`
  return scopedPath(ref, d.issuesScope, `${d.issuesSegment}/${tail}`)
}

/** Link to the repo's issue / work-item list. */
export function issuesUrlFor(ref: RepoRef): string {
  const d = descriptorOf(ref)
  return scopedPath(ref, d.issuesScope, d.issuesSegment)
}

/** Link to one pull/merge request.
 *
 * The path noun differs, not just the host: GitHub serves `/pull/<n>`, GitLab
 * `/-/merge_requests/<n>`, Azure DevOps `/_git/<repo>/pullrequest/<n>`. */
export function changeUrlFor(ref: RepoRef, number: number): string {
  return repoPagePath(ref, `${descriptorOf(ref).changeSegment}/${number}`)
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
  const d = descriptorOf(ref)
  return scopedPath(ref, d.membersScope, d.membersPath)
}

/** A single react-query cache-key fragment identifying one repo.
 *
 * A bare `owner, repo` pair does not identify a repo: `acme/widget` on GitHub and
 * `acme/widget` on gitlab.com would share one cache entry, so switching between
 * them would render the other one's issues, labels and settings until something
 * invalidated. Including provider + host makes that collision impossible. */
export function repoScopeKey(ref: RepoRef): string {
  return `${ref.provider || 'github'}:${ref.host || 'github.com'}:${ref.owner}/${ref.repo}`
}

/** Display vocabulary for a ref's provider.
 *
 * Mirrors `backend/provider.py:_TERMS`, so the UI, the notifications, and the AI
 * prompts all call a merge request the same thing.
 *
 * Azure DevOps SPREADS the GitHub vocabulary rather than restating it: it calls a
 * change request a pull request in exactly the words GitHub does, and only the
 * reference sigil, the brand and the CLI differ. Retyping the four nouns into a
 * third arm would put two spellings of one truth in one file, which is how
 * "Pull request" and "Pull Request" end up on adjacent screens.
 */
export function providerTerms(ref?: Pick<RepoRef, 'provider'>): ProviderTerms {
  if (providerKeyOf(ref) === 'azure') {
    return {
      ...providerTerms(),
      // Azure DevOps markdown addresses pull requests with `!` and work items
      // with `#` — the same split as GitLab, because the two sequences are
      // independent.
      sigil: '!',
      // Azure DevOps tracks WORK ITEMS, and "Issue" is only one of the types a
      // process template may define (Basic has Issue, Agile has Bug/Task/User
      // Story and no Issue at all). Naming the list "Issues" would therefore be
      // wrong on most templates, not merely differently-spelled.
      emptyDetailKey: 'apps.issueRadar.workspace.select_a_work_item_to_see_its_details',
      trackedItemCountKey: 'apps.issueRadar.components.issueList.work_item',
      trackedItemPluralTitleKey: 'apps.issueRadar.lib.links.tracked_items_work_items',
      providerName: 'Azure DevOps',
      cli: 'az',
    }
  }
  return isGitlab(ref)
    ? {
        changeRequest: 'merge request',
        changeRequestTitle: 'Merge Request',
        changeRequestPlural: 'merge requests',
        changeRequestPluralTitle: 'Merge Requests',
        changeRequestShort: 'MR',
        sigil: '!',
        emptyDetailKey: 'apps.issueRadar.workspace.select_an_issue_to_see_its_details',
        trackedItemCountKey: 'apps.issueRadar.components.issueList.issue',
        trackedItemPluralTitleKey: 'apps.issueRadar.lib.links.tracked_items_issues',
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
        emptyDetailKey: 'apps.issueRadar.workspace.select_an_issue_to_see_its_details',
        trackedItemCountKey: 'apps.issueRadar.components.issueList.issue',
        trackedItemPluralTitleKey: 'apps.issueRadar.lib.links.tracked_items_issues',
        providerName: 'GitHub',
        cli: 'gh',
      }
}

// ── provider CLI commands for agent prompts ─────────────────────────────────
//
// The investigate / review seed prompts tell an agent to read the item with a
// CLI. The command is provider-specific: hard-coding `gh` would, on a GitLab
// item, send the agent to look up a GitLab path on GitHub -- reading a
// stranger's repo or nothing at all, with no error to notice.
//
// See `repoArg` for why GitLab gets a full URL and GitHub keeps `owner/repo`.

/** The CLI that owns credentials for this ref (`gh` / `glab` / `az`). */
function cliFor(ref: RepoRef): string {
  return providerTerms(ref).cli
}

/** What to pass to `--repo`.
 *
 * GitLab gets the project's full URL, because that is the only form carrying the
 * HOST -- a self-managed project is otherwise unaddressable without ambient
 * `GITLAB_HOST` state the agent's shell may not have. GitHub deliberately uses
 * the plain `owner/repo`: that invocation only ever resolves to github.com, and
 * any other form would alter a working path for no gain.
 */
function repoArg(ref: RepoRef): string {
  return isGitlab(ref) ? repoWebUrl(ref) : `${ref.owner}/${ref.repo}`
}

/** The `--org`/`--project` pair every `az` invocation needs.
 *
 * `az` has no `--repo owner/repo` form: an organization is addressed as a URL and
 * the project is a separate flag, which is exactly the identity Azure DevOps
 * splits `owner` into. `--detect false` stops `az` from inferring an
 * organization from the shell's current git remote — the agent may be standing in
 * an unrelated checkout, and inference there reads the WRONG project silently. */
function azureScopeArgs(ref: RepoRef): string {
  return `--org https://${hostOf(ref)}/${azureOrg(ref)} --project ${azureProject(ref)} --detect false`
}

/** Command that prints one issue / work item with its full comment thread. */
export function issueViewCommand(ref: RepoRef, number: number): string {
  if (providerKeyOf(ref) === 'azure') {
    // `az boards work-item show --expand all` is what carries the discussion; the
    // `--comments` flag `gh`/`glab` use does not exist here.
    return `az boards work-item show --id ${number} ${azureScopeArgs(ref)} --expand all`
  }
  return `${cliFor(ref)} issue view ${number} --repo ${repoArg(ref)} --comments`
}

/** Command that prints one pull/merge request with its full comment thread.
 *
 * GitHub calls the noun `pr`, GitLab calls it `mr` -- so the SUBCOMMAND differs,
 * not just the binary. */
export function changeViewCommand(ref: RepoRef, number: number): string {
  const d = descriptorOf(ref)
  if (providerKeyOf(ref) === 'azure') {
    return `az repos pr show --id ${number} ${azureScopeArgs(ref)}`
  }
  return `${cliFor(ref)} ${d.cliChangeNoun} view ${number} --repo ${repoArg(ref)} --comments`
}

/** Command that prints one pull/merge request's diff.
 *
 * Azure DevOps' CLI has no diff subcommand, so the closest honest answer is the
 * PR payload — which carries its source/target refs and its iterations, enough
 * for an agent to fetch the diff itself. Inventing an `az repos pr diff` would
 * hand the agent a command that fails, which is worse than a narrower one that
 * works. */
export function changeDiffCommand(ref: RepoRef, number: number): string {
  const d = descriptorOf(ref)
  if (providerKeyOf(ref) === 'azure') {
    return `az repos pr show --id ${number} ${azureScopeArgs(ref)}`
  }
  return `${cliFor(ref)} ${d.cliChangeNoun} diff ${number} --repo ${repoArg(ref)}`
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
/** The project a repo's tracked items actually belong to, or `''` when they
 * belong to the repository itself.
 *
 * Only Azure DevOps has an answer: its work items hang off the PROJECT and carry
 * no repository dimension at all, so two repos connected from one project show
 * the identical list. `owner` already carries `{organization}/{project}`, so the
 * project is its second segment. Everywhere else the tracked items really are
 * repo-scoped and there is nothing to disclose. */
export function trackedItemProjectScope(ref?: Pick<RepoRef, 'provider' | 'owner'>): string {
  if (providerKeyOf(ref) !== 'azure') return ''
  const segments = (ref?.owner || '').split('/').filter(Boolean)
  return segments.length > 1 ? segments[segments.length - 1] : ''
}

/** Whether this provider can express a repository write AT ALL.
 *
 * Distinct from "this user lacks access", which is what a read-only repo means
 * on GitHub and GitLab. Azure DevOps reports no write access for EVERY repo
 * (backend `azure_client._permissions`): the only authorization signal its
 * `az devops invoke` transport can reach is project team membership, and
 * membership does not imply repository write, so inferring one would over-grant.
 * The distinction has to reach the UI because the remedy differs — one is
 * "get access", the other is "there is no access to get here".
 *
 * Not exported: `readOnlyHint` below is the only thing that needs to ask, and a
 * second exported predicate would invite a caller to re-derive the copy. */
function providerSupportsWrites(ref?: Pick<RepoRef, 'provider'>): boolean {
  return providerKeyOf(ref) !== 'azure'
}

/** The tooltip for a control disabled because the repo cannot be written.
 *
 * `accessHint` is the caller's existing "you need triage/push access" copy,
 * which stays correct wherever access is actually the thing missing. On a
 * provider that supports no writes at all it is replaced, because sending a
 * project administrator after permissions they already hold — and that would
 * change nothing here — is worse than saying plainly where the write works. */
export function readOnlyHint(ref: Pick<RepoRef, 'provider'> | undefined, accessHint: string): string {
  if (providerSupportsWrites(ref)) return accessHint
  return i18nT('apps.issueRadar.lib.links.read_only_here_apply_in_provider', {
    provider: providerTerms(ref).providerName,
  })
}

export function recordIdentityJson(ref: RepoRef, kind: ItemKind = 'issue'): string {
  return (
    `"owner":"${ref.owner}","repo":"${ref.repo}"`
    + `,"provider":"${ref.provider || 'github'}","host":"${ref.host || 'github.com'}"`
    + `,"kind":"${kind}"`
  )
}
