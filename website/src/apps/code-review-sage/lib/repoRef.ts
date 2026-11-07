// Repo identity across GitHub hosts.
//
// A repo is `owner/repo` on github.com and `host/owner/repo` on a GitHub
// Enterprise host. Every surface (the picker, the pinned list, the Reviews tab
// filter, the PR list query) names a repo through these helpers, so two repos
// with the same `owner/repo` on different instances never collapse into one.
// The slug matches the backend's `discovery.repo_slug` and the `repo` field the
// repo-PR listing reports.

export const DEFAULT_HOST = 'github.com'

/** Anything that names a repo. `host` is absent on rows written before
 *  Enterprise support, and those mean github.com. */
export interface RepoRef {
  owner: string
  repo: string
  host?: string
}

/** The canonical host: lowercased, `www.github.com` folded into github.com. */
export function canonicalHost(host: string | undefined | null): string {
  const h = String(host ?? '').trim().toLowerCase()
  if (!h || h === 'www.github.com') return DEFAULT_HOST
  return h
}

export function repoHost(r: RepoRef): string {
  return canonicalHost(r.host)
}

export function isEnterpriseHost(host: string | undefined | null): boolean {
  return canonicalHost(host) !== DEFAULT_HOST
}

/** `owner/repo` on github.com, `host/owner/repo` elsewhere. */
export function repoSlug(r: RepoRef): string {
  const h = repoHost(r)
  return h === DEFAULT_HOST ? `${r.owner}/${r.repo}` : `${h}/${r.owner}/${r.repo}`
}

export function repoUrl(r: RepoRef): string {
  return `https://${repoHost(r)}/${r.owner}/${r.repo}`
}

/** A minimal `{owner, repo[, host]}` for storage and requests. The host is
 *  only written for an Enterprise repo, so github.com values keep exactly the
 *  shape they had before hosts existed. */
export function compactRepo(r: RepoRef): RepoRef {
  const h = repoHost(r)
  return h === DEFAULT_HOST ? { owner: r.owner, repo: r.repo } : { owner: r.owner, repo: r.repo, host: h }
}

/** The host to send with a pin/unpin request: undefined for github.com. */
export function enterpriseHostOf(r: RepoRef): string | undefined {
  const h = repoHost(r)
  return h === DEFAULT_HOST ? undefined : h
}

/** Case-insensitive identity, host included. */
export function sameRepo(a: RepoRef | null | undefined, b: RepoRef | null | undefined): boolean {
  if (!a || !b) return false
  return repoSlug(a).toLowerCase() === repoSlug(b).toLowerCase()
}

/** Normalize what a user typed into the add-repo field into a URL the backend
 *  validates: a full URL passes through, a leading domain-shaped segment
 *  (`ghe.example.com/o/r`) gets a scheme, and a bare `owner/repo` means
 *  github.com. */
export function repoInputToUrl(input: string): string {
  const v = input.trim().replace(/^\/+|\/+$/g, '')
  if (v.includes('://')) return v
  const first = v.split('/', 1)[0] ?? ''
  if (first.includes('.')) return `https://${v}`
  return `https://${DEFAULT_HOST}/${v}`
}

/** The repo slug a PR/repo URL belongs to (see `repoSlug`), lowercased, or
 *  null when the URL is not a recognisable `https://host/owner/repo/...`. */
export function slugFromUrl(url: string): string | null {
  let parsed: URL
  try {
    parsed = new URL(String(url ?? '').trim())
  } catch {
    return null
  }
  const parts = parsed.pathname.split('/').filter(Boolean)
  if (parts.length < 2) return null
  return repoSlug({ host: parsed.hostname, owner: parts[0], repo: parts[1] }).toLowerCase()
}
