// Cross-reference links inside an issue/PR body or comment.
//
// A GitHub markdown body routinely links to OTHER issues and PRs. A plain
// external link (target=_blank → browser) breaks the triage loop: you lose the
// list, the filters, and the pane you were reading. When the link points at the
// repo Issue Radar already has connected, the app renders the target itself in a
// bottom sheet instead (see components/RefSheet.tsx).
//
// This module is the pure half of that: URL → { kind, number }, plus the
// placeholder list rows the sheet passes to the detail panes while their real
// detail streams in. Kept dependency-free (no React, no DOM) so it is directly
// unit-testable.
import { maskInlineCode } from '../../../hooks/useBlockAssembler'
import type { Issue, PullRequest, RepoRef as RepoIdentity } from '../api'
import { changeUrlFor, issueUrlFor, providerKeyOf, repoWebUrl } from './links'

/** Which detail pane renders a reference target. */
export type RefKind = 'issue' | 'pull'

/** A resolved same-repo reference — what the sheet needs to open a target. */
export interface RepoRef {
  kind: RefKind
  number: number
}

/** Path segment → pane, per provider. GitHub's canonical PR path is `/pull/<n>`;
 * `/pulls/<n>` is accepted because it redirects there and appears in hand-written
 * links. */
const KIND_BY_SEGMENT: Record<string, RefKind> = {
  issues: 'issue',
  pull: 'pull',
  pulls: 'pull',
}

/** GitLab nests both under `/-/` and calls the change request a
 * `merge_requests`. */
const GITLAB_KIND_BY_SEGMENT: Record<string, RefKind> = {
  issues: 'issue',
  merge_requests: 'pull',
}

/** Azure DevOps calls a tracked item a WORK ITEM and serves it from the project
 * (`_workitems`), while a pull request lives under the repository's own `_git`
 * path as `pullrequest`. Two different scopes, which is why the parser cannot
 * just swap this map in and reuse one path prefix. */
const AZURE_KIND_BY_SEGMENT: Record<string, RefKind> = {
  _workitems: 'issue',
  pullrequest: 'pull',
}

/** The segment map for each provider's grammar, keyed the same way the link
 * BUILDERS are (see `links.ts`) so a URL cannot be built in one shape and parsed
 * in another. */
const SEGMENT_MAPS: Record<string, Record<string, RefKind>> = {
  github: KIND_BY_SEGMENT,
  gitlab: GITLAB_KIND_BY_SEGMENT,
  azure: AZURE_KIND_BY_SEGMENT,
}

/** Whether a link's host is the repo's own host.
 *
 * `www.github.com` is folded onto `github.com` because it is an official alias
 * for the same repo, and links in the wild use it. That fold is deliberately NOT
 * applied to any other host: on a self-managed instance `www.<host>` may be a
 * different server entirely, and inventing an alias rule there would open a pane
 * bound to the connected project for an item that is not it. */
function sameHost(hrefHost: string, repoHost: string, repo: RepoIdentity): boolean {
  const a = hrefHost.toLowerCase()
  const b = repoHost.toLowerCase()
  if (a === b) return true
  if (providerKeyOf(repo) !== 'github') return false
  const fold = (h: string) => (h === 'www.github.com' ? 'github.com' : h)
  return fold(a) === fold(b) && fold(b) === 'github.com'
}

/**
 * Resolve `href` to a reference INTO the ACTIVE repo, or null when it points
 * anywhere else (another repo, another host, another provider, a discussion, a
 * non-numeric path).
 *
 * Matching is anchored on the repo's OWN web URL rather than on a hard-coded
 * github.com set, because a repo's host is part of its identity: the same
 * `group/project` path exists on gitlab.com and on every self-managed instance,
 * and opening one in a pane bound to the other would show the wrong item's
 * labels, roster and permissions. An Enterprise/self-managed host therefore
 * matches when it IS the active repo's host, and never otherwise.
 *
 * Only absolute http(s) URLs match. A relative href is deliberately not resolved
 * against any base: in a body it resolves against the *repo* page, which this
 * module has no way to distinguish from a dashboard-relative link, and guessing
 * wrong would hijack an unrelated click.
 *
 * Owner/repo are compared case-insensitively (both providers treat the path that
 * way for lookup, so `/KiroDotDev/KiroCrew/issues/5` is the same target as the
 * lowercase form). GitLab's nested namespace is compared as a whole, so a
 * sibling project one level up cannot match; Azure DevOps' `{org}/{project}` pair
 * is compared the same way, so a sibling project in the same organization cannot
 * match either. Trailing segments (`/files`, `/commits`), query strings and
 * `#note-…` fragments are ignored — they all address the same issue or change
 * request.
 */
export function parseRepoRef(
  href: string | null | undefined,
  repo: RepoIdentity | null | undefined,
): RepoRef | null {
  if (!href || !repo?.owner || !repo?.repo) return null

  const provider = providerKeyOf(repo)
  let url: URL
  let base: URL
  try {
    url = new URL(href)
    base = new URL(repoWebUrl(repo))
  } catch {
    return null  // relative or malformed — not a resolvable cross-reference
  }
  if (url.protocol !== 'https:' && url.protocol !== 'http:') return null
  if (!sameHost(url.hostname, base.hostname, repo)) return null
  if (url.port !== base.port) return null

  const segments = url.pathname.split('/').filter(Boolean)
  // The prefix every in-scope link shares. On GitHub and GitLab that is the
  // repository's own path. On Azure DevOps it is the PROJECT (`owner`, which
  // carries `{org}/{project}`): a work item is addressed off the project with no
  // repository in the path at all, so anchoring on the repo page — whose path ends
  // in `_git/{repo}` — would reject every work-item link.
  const baseSegments = provider === 'azure'
    ? repo.owner.split('/').filter(Boolean)
    : base.pathname.split('/').filter(Boolean)
  // The prefix must match in full: on GitLab it is a nested namespace and on Azure
  // DevOps an org/project pair, so comparing only the first segment would accept a
  // different project in the same group or organization.
  if (segments.length < baseSegments.length + 2) return null
  for (let i = 0; i < baseSegments.length; i++) {
    if (segments[i].toLowerCase() !== baseSegments[i].toLowerCase()) return null
  }

  let rest = segments.slice(baseSegments.length)
  // GitLab addresses a project's own pages under `/-/`; a link without it is not
  // an issue/MR path on that provider.
  if (provider === 'gitlab') {
    if (rest[0] !== '-') return null
    rest = rest.slice(1)
  } else if (provider === 'azure' && rest[0] === '_git') {
    // A repo-scoped Azure page. The repository name is part of the path here, so it
    // has to match: `/{org}/{project}/_git/other/pullrequest/5` is a DIFFERENT
    // repository in the same project, and claiming it would open this repo's pane
    // for another repo's PR.
    //
    // Compared EXACTLY, not case-folded. Azure repository names are not documented
    // as case-insensitive, so `Ledger` and `ledger` may be two repositories; folding
    // them would let one repo's pane claim the other's link. This matches
    // `repoIdentity` and the backend's `store.name_compare_key`, which both fold for
    // GitHub alone.
    if (!rest[1] || rest[1] !== repo.repo) return null
    rest = rest.slice(2)
  }

  const [kindSegment, ...tail] = rest
  const kind = SEGMENT_MAPS[provider][kindSegment]
  if (!kind) return null
  // Azure DevOps' canonical single-work-item path inserts `edit`
  // (`_workitems/edit/<id>`). The bare `_workitems/<id>` form also resolves and is
  // written by hand, so both are accepted.
  const numberSegment = provider === 'azure' && tail[0] === 'edit' ? tail[1] : tail[0]
  if (!numberSegment || !/^[0-9]+$/.test(numberSegment)) return null
  const number = Number(numberSegment)
  if (!Number.isSafeInteger(number) || number <= 0) return null

  return { kind, number }
}

/** The canonical URL for a reference ON THE REPO'S OWN PROVIDER — used for the
 * sheet's "open externally" escape hatch and as the placeholder row's `url` until
 * the real detail (which carries the provider's own url) lands. */
export function refUrl(repo: RepoIdentity, ref: RepoRef): string {
  return ref.kind === 'pull' ? changeUrlFor(repo, ref.number) : issueUrlFor(repo, ref.number)
}

/** Stable identity for a stack entry (also the React key). */
export function refKey(ref: RepoRef): string {
  return `${ref.kind}-${ref.number}`
}

/** Regions of a markdown source that must never be rewritten: whole markdown
 * link/image constructs (`[label](target)`) — rewriting inside a link label would
 * nest one link inside another — plus autolinks and raw HTML tags. Fenced code and
 * inline code are handled separately (see `maskFences` / `maskInlineCode`). */
const MASKED_REGIONS = [
  /!?\[[^\]\n]*\]\([^)\n]*\)/g,           // inline link / image
  /!?\[[^\]\n]*\]\[[^\]\n]*\]/g,          // reference-style link / image
  /^[ \t]{0,3}\[[^\]\n]+\]:[^\n]*/gm,     // link reference DEFINITION line
  /<[^<>\n]+>/g,                          // autolink, raw HTML tag
]

/** Blank out every line inside a ``` / ~~~ fence (fence lines included), keeping
 * line lengths so match indices stay valid against the original.
 *
 * The opener's LENGTH is tracked, not just its character: per CommonMark a fence
 * is closed only by a run of the same character that is at least as long, so a
 * four-backtick fence may contain a three-backtick line (the usual way to show a
 * fenced example). Closing on the shorter run would unmask the rest of the block
 * and let its content be rewritten. An UNCLOSED fence masks to end of source,
 * which is what a truncated body wants. */
function maskFences(source: string): string {
  const lines = source.split('\n')
  let fenceChar: string | null = null
  let fenceLen = 0
  for (let i = 0; i < lines.length; i++) {
    const marker = /^[ \t]*(`{3,}|~{3,})/.exec(lines[i])
    const blank = ' '.repeat(lines[i].length)
    if (fenceChar === null) {
      if (marker) {
        fenceChar = marker[1][0]
        fenceLen = marker[1].length
        lines[i] = blank
      }
    } else {
      lines[i] = blank
      if (marker && marker[1][0] === fenceChar && marker[1].length >= fenceLen) {
        fenceChar = null
        fenceLen = 0
      }
    }
  }
  return lines.join('\n')
}

/** Replace every masked region with spaces, preserving length so match indices
 * from the masked copy stay valid against the original.
 *
 * Inline code goes through the shared `maskInlineCode`, which is CommonMark-correct
 * about delimiter LENGTH: a run of N backticks is closed only by a run of exactly
 * N, so ``` ``a ` #5`` ``` is masked as one span rather than ending at the inner
 * backtick and leaving `#5` exposed. */
function maskMarkdown(source: string): string {
  let masked = maskFences(source)
  masked = masked.split('\n').map((line) => maskInlineCode(line)).join('\n')
  for (const re of MASKED_REGIONS) {
    masked = masked.replace(re, (m) => ' '.repeat(m.length))
  }
  return masked
}

/** A shorthand issue reference: `#123`.
 *
 * Rejected when PRECEDED by a character that makes it something else — a word
 * character or `/` (a URL fragment such as `…/issues/12#issuecomment-9`, or a
 * cross-repo `owner/repo#5`), `&` (`&#123;`, an HTML entity), `[` (markdown link
 * syntax the mask may not have caught), or another `#`.
 *
 * An opening `(` is ALLOWED: `the Windows lane (#421) uses …` is ordinary prose
 * and by far the more common shape, and GitHub linkifies it. The one `(` that is
 * markdown — a link TARGET, `](#421)` — is rejected separately in the scan loop
 * by looking at the character before the paren, because a well-formed link is
 * already masked and only an unbalanced leftover can reach here.
 *
 * Rejected when FOLLOWED by a word character, so a hex colour (`#1a2b3c`) is not
 * read as `#1`. An all-digit run IS taken as a reference — GitHub does the same,
 * and a repo with six-figure issue numbers is ordinary, so length cannot decide.
 */
const SHORTHAND_RE = /(^|[^\w/&[#])#(\d{1,7})(?!\w)/g

/**
 * Rewrite bare `#123` references into real markdown links to this repo, so they
 * render as links — and pick up the in-app reference affordance — exactly like a
 * pasted full URL. GitHub renders the same shorthand on its own web UI; the raw
 * markdown the API returns carries only the literal text.
 *
 * The target is always the `/issues/<n>` form. On GitHub the shorthand does not
 * say which it is: the reference UI resolves issue-vs-PR from the ref summary, and
 * GitHub itself redirects `/issues/<n>` to `/pull/<n>` for a PR, so the link is
 * still correct if it is ever followed externally. On GitLab `#<n>` is
 * unambiguous — a merge request is referenced as `!<n>` — so the issue path is
 * exactly right there. GitLab's `!<n>` shorthand is deliberately NOT linkified:
 * `!` is also ordinary prose and markdown image syntax, and a wrong claim on a
 * click is worse than leaving it plain text.
 *
 * Code, autolinks, raw HTML and existing markdown links are masked out first, so
 * nothing inside them is rewritten.
 */
export function linkifyIssueRefs(source: string, repo: RepoIdentity | null | undefined): string {
  if (!source || !repo?.owner || !repo?.repo) return source
  if (!source.includes('#')) return source
  const masked = maskMarkdown(source)

  const edits: Array<{ start: number; end: number; text: string }> = []
  SHORTHAND_RE.lastIndex = 0
  let m: RegExpExecArray | null
  while ((m = SHORTHAND_RE.exec(masked)) !== null) {
    const lead = m[1] ?? ''
    const start = m.index + lead.length
    const end = m.index + m[0].length
    // `](#421)` is a link TARGET, not a reference. A well-formed link is masked
    // already, so this only catches an unbalanced leftover — but rewriting one
    // would nest a link inside a link. The check reads the MASKED copy so a `]`
    // that belongs to some other masked construct does not count.
    if (lead === '(' && masked[m.index - 1] === ']') continue
    const number = Number(m[2])
    if (!Number.isSafeInteger(number) || number <= 0) continue
    edits.push({ start, end, text: `[#${number}](${refUrl(repo, { kind: 'issue', number })})` })
  }
  if (edits.length === 0) return source

  let out = ''
  let pos = 0
  for (const e of edits) {
    out += source.slice(pos, e.start) + e.text
    pos = e.end
  }
  return out + source.slice(pos)
}

/** A placeholder LIST row for an issue the list may not hold (a closed issue, or
 * one outside the current filters). The detail panes render `detail?.x ?? row.x`
 * throughout, so every field here is only the pre-fetch first paint. The title is
 * deliberately EMPTY: the panes render a skeleton wherever a field is missing, so
 * a placeholder must not fabricate one (a literal `#<n>` title would read as real
 * content for the length of the fetch). */
export function placeholderIssue(
  repo: RepoIdentity, number: number, state?: string,
): Issue {
  return {
    number,
    title: '',
    url: refUrl(repo, { kind: 'issue', number }),
    labels: [],
    comments: 0,
    updated_at: '',
    // Left UNDEFINED unless the caller knows it. Defaulting to 'open' would make
    // the pane offer "Close as completed" on an already-closed issue and let that
    // write clobber its state_reason; the panes gate their write actions on having
    // an authoritative state (see awaitingFirstPaint).
    state,
  }
}

/** The PR twin of `placeholderIssue`. `state` is the reference summary's when the
 * caller has it; it drives only the first-paint pill and the poll rate, and the
 * real detail corrects both on arrival. The PR pane has no state-write action, so
 * unlike the issue side there is nothing here to clobber. */
export function placeholderPull(
  repo: RepoIdentity, number: number, state?: string,
): PullRequest {
  return {
    number,
    title: '',
    url: refUrl(repo, { kind: 'pull', number }),
    state: state ?? 'open',
    draft: false,
    labels: [],
    updated_at: '',
  }
}
