/**
 * A staged crewmate dashboard, as the chat links to it and as the side panel
 * opens it.
 *
 * `dashboard_preview` hands the agent `/api/members/<slug>/dashboard?preview=1`
 * (`instance.preview_url` on the gateway). That is a JSON read, so a plain click
 * on it in a transcript is taken into the side panel instead, where the staged
 * page renders beside the chat. The panel reaches it through the same host action
 * an artifact link uses, under a reference no artifact slug can spell: artifact
 * slugs are `[a-z0-9-]` only, so the `:` keeps the two apart.
 */

const REF_PREFIX = 'dashboard-preview:'

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
