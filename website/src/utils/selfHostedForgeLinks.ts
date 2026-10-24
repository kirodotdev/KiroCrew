import type { PullRequestLink } from './pullRequestLinks'

/** The URL grammars of the two self-hosted forges with no public default host.
 *
 *  Bitbucket Server and Gerrit links are recognized ONLY on hosts the operator
 *  listed (`dashboard.bitbucket_server_hosts` / `dashboard.gerrit_hosts`), so
 *  each grammar comes with the host-set normalizer the parser consults. Both
 *  mirror the backend grammar exactly, so a link this module builds always
 *  re-parses there. `pullRequestLinks.ts` re-exports the host-set functions,
 *  which keeps it the one import path callers use. */

/** Normalize the configured Bitbucket Server hosts to a lookup set.
 * Same normalization as gitlabHostSet — entries are exact `host[:port]` values
 * from operator config (dashboard.bitbucket_server_hosts). Bitbucket Server has
 * no public default host, so a link is recognized only when listed here. */
export function bitbucketServerHostSet(hosts: readonly string[] | undefined): ReadonlySet<string> {
  return new Set(
    (hosts ?? [])
      .map(host => host.trim().toLowerCase().replace(/:443$/, ''))
      .filter(Boolean),
  )
}

/** Bitbucket Server path pieces. Mirrors the backend grammar so a link this
 *  module builds always re-parses there: matched from the END of the path, with
 *  any reverse-proxy context path before `/projects/` kept in the canonical url,
 *  and the UI tab suffix the link was copied from dropped. */
const BITBUCKET_SERVER_TABS: ReadonlySet<string> = new Set(['overview', 'diff', 'commits', 'builds'])
const BITBUCKET_SERVER_KEY_RE = /^[A-Za-z][A-Za-z0-9_]{0,127}$/
const BITBUCKET_SERVER_SLUG_RE = /^[A-Za-z0-9._-]{1,128}$/
const BITBUCKET_SERVER_CONTEXT_RE = /^[A-Za-z0-9._~-]{1,64}$/

export function bitbucketServerLink(origin: string, path: string): PullRequestLink | null {
  let segments = path.split('/')
  if (segments.length > 1 && BITBUCKET_SERVER_TABS.has(segments[segments.length - 1].toLowerCase())) {
    segments = segments.slice(0, -1)
  }
  const n = segments.length
  if (n < 7 || segments[0] !== '' || segments[n - 6] !== 'projects'
    || segments[n - 4] !== 'repos' || segments[n - 2] !== 'pull-requests') return null
  const key = segments[n - 5]
  const slug = segments[n - 3]
  const numberText = segments[n - 1]
  const context = segments.slice(1, n - 6)
  if (!BITBUCKET_SERVER_KEY_RE.test(key) || !BITBUCKET_SERVER_SLUG_RE.test(slug)) return null
  if (slug === '.' || slug === '..') return null
  if (!/^\d{1,15}$/.test(numberText) || Number(numberText) <= 0) return null
  if (context.length > 8) return null
  if (context.some(seg => seg === '.' || seg === '..' || !BITBUCKET_SERVER_CONTEXT_RE.test(seg))) return null
  const number = Number(numberText)
  const prefix = context.map(seg => `/${seg}`).join('')
  return {
    url: `https://${origin}${prefix}/projects/${key}/repos/${slug}/pull-requests/${number}`,
    provider: 'bitbucket_server',
    number,
    repo: slug,
    kind: 'change',
  }
}

/** Normalize the configured Gerrit hosts to a lookup set. Same normalization as
 * gitlabHostSet — entries are exact `host[:port]` values from operator config
 * (dashboard.gerrit_hosts). Gerrit has no public default host, so a link is
 * recognized only when listed here. */
export function gerritHostSet(hosts: readonly string[] | undefined): ReadonlySet<string> {
  return new Set(
    (hosts ?? [])
      .map(host => host.trim().toLowerCase().replace(/:443$/, ''))
      .filter(Boolean),
  )
}

/** Gerrit path pieces. Mirrors the backend grammar so a link this module builds
 *  always re-parses there: `<context>/c/<project>/+/<number>[/<patchset>][/…]`,
 *  anchored on the FIRST `c` segment and the `+` after the project (which may
 *  span several segments). A numeric second segment after the number is the
 *  patchset pin and is kept; a file path, comment anchor or revision range
 *  after it is UI state and is dropped. */
const GERRIT_PROJECT_SEGMENT_RE = /^[A-Za-z0-9._~-]{1,128}$/
const GERRIT_CONTEXT_RE = /^[A-Za-z0-9._~-]{1,64}$/
const GERRIT_MAX_PROJECT_SEGMENTS = 16

export function gerritLink(origin: string, path: string): PullRequestLink | null {
  const segments = path.split('/')
  if (segments.length < 5 || segments[0] !== '') return null
  const cIndex = segments.indexOf('c', 1)
  if (cIndex < 0) return null
  const plusIndex = segments.indexOf('+', cIndex + 2)
  if (plusIndex < 0) return null
  const context = segments.slice(1, cIndex)
  const projectSegments = segments.slice(cIndex + 1, plusIndex)
  const tail = segments.slice(plusIndex + 1)
  if (tail.length === 0) return null
  const numberText = tail[0]
  if (projectSegments.length === 0 || projectSegments.length > GERRIT_MAX_PROJECT_SEGMENTS) return null
  if (projectSegments.some(seg => seg === '.' || seg === '..' || !GERRIT_PROJECT_SEGMENT_RE.test(seg))) return null
  if (!/^\d{1,15}$/.test(numberText) || Number(numberText) <= 0) return null
  if (context.length > 8) return null
  if (context.some(seg => seg === '.' || seg === '..' || !GERRIT_CONTEXT_RE.test(seg))) return null
  const number = Number(numberText)
  let patchset = ''
  if (tail.length > 1 && /^\d{1,6}$/.test(tail[1]) && Number(tail[1]) > 0) {
    patchset = `/${Number(tail[1])}`
  }
  const prefix = context.map(seg => `/${seg}`).join('')
  const project = projectSegments.join('/')
  return {
    url: `https://${origin}${prefix}/c/${project}/+/${number}${patchset}`,
    provider: 'gerrit',
    number,
    repo: project,
    kind: 'change',
  }
}
