/**
 * A staged crewmate dashboard, as the chat links to it and as the side panel
 * opens it.
 *
 * `dashboard_preview` hands the agent `/members?member=<name>&dashboard=preview`
 * (`instance.preview_url` on the gateway); older transcripts hold the earlier
 * `/api/members/<slug>/dashboard?preview=1` JSON read. A plain click on either in
 * a transcript is taken into the side panel instead, where the staged page renders
 * beside the chat. The panel reaches it through the same host action an artifact
 * link uses, under a reference no artifact slug can spell: artifact slugs are
 * `[a-z0-9-]` only, so the `:` keeps the two apart. The page link names the crew
 * by NAME, so its reference carries the name and the host resolves the slug from
 * the roster (slugs are lossy and are never guessed from a name).
 */

import { DASHBOARD_PREVIEW_PARAM, DASHBOARD_PREVIEW_VALUE } from '../pages/members/dashboardPreview'

const REF_PREFIX = 'dashboard-preview:'
const MEMBER_REF_PREFIX = 'dashboard-preview-member:'

/** The gateway's member-slug grammar (`members._SLUG_RE`). */
const MEMBER_SLUG_RE = /^[a-z0-9](?:[a-z0-9-]{0,78}[a-z0-9])?$/

const PREVIEW_PATH_RE = /^\/api\/members\/([^/]+)\/dashboard$/

/** The crewmate slug a same-origin staged-dashboard link names, or null. */
export function dashboardPreviewSlugFromHref(href: string | null | undefined): string | null {
  if (!href) return null
  let url: URL
  try { url = new URL(href, window.location.origin) } catch { return null }
  // Another origin's path of the same shape is somebody else's page, not this
  // gateway's staged one.
  if (url.origin !== window.location.origin) return null
  if (url.searchParams.get('preview') !== '1') return null
  const m = PREVIEW_PATH_RE.exec(url.pathname)
  if (!m) return null
  let slug = m[1]
  try { slug = decodeURIComponent(slug) } catch { return null }
  return MEMBER_SLUG_RE.test(slug) ? slug : null
}

/** The exact crew name a same-origin `/members?member=<name>&dashboard=preview`
 *  link names, or null. */
export function dashboardPreviewMemberFromHref(href: string | null | undefined): string | null {
  if (!href) return null
  let url: URL
  try { url = new URL(href, window.location.origin) } catch { return null }
  if (url.origin !== window.location.origin) return null
  if (url.pathname !== '/members') return null
  if (url.searchParams.get(DASHBOARD_PREVIEW_PARAM) !== DASHBOARD_PREVIEW_VALUE) return null
  const member = url.searchParams.get('member')
  return member ? member : null
}

/** The panel reference `openArtifact` takes for a staged page named by crew name. */
export function dashboardPreviewMemberRef(member: string): string {
  return MEMBER_REF_PREFIX + encodeURIComponent(member)
}

/** The crew name a member reference names, or null for any other reference. */
export function dashboardPreviewMemberFromRef(ref: string | null | undefined): string | null {
  if (!ref || !ref.startsWith(MEMBER_REF_PREFIX)) return null
  try {
    const member = decodeURIComponent(ref.slice(MEMBER_REF_PREFIX.length))
    return member ? member : null
  } catch { return null }
}

/** The panel reference `openArtifact` takes for a crewmate's staged page. */
export function dashboardPreviewRef(slug: string): string {
  return REF_PREFIX + slug
}

/** The crewmate slug a panel reference names, or null for an ordinary artifact slug. */
export function dashboardPreviewSlugFromRef(ref: string | null | undefined): string | null {
  if (!ref || !ref.startsWith(REF_PREFIX)) return null
  const slug = ref.slice(REF_PREFIX.length)
  return MEMBER_SLUG_RE.test(slug) ? slug : null
}
