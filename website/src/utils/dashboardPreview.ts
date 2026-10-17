/**
 * A staged dashboard, as the chat links to it and as the side panel opens it.
 *
 * `dashboard_preview` hands the agent one of these links:
 * `/members?member=<name>&dashboard=preview`, the page for a crewmate's staged
 * page (`instance.preview_url` on the gateway); the earlier JSON read
 * `/api/members/<slug>/dashboard?preview=1`, which older transcripts still hold;
 * or `/api/chat/slots/<slot>/dashboard?preview=1` for a root session's own page
 * (`instance.session_preview_url`). A plain click on any of them in a transcript
 * is taken into the side panel instead, where the staged page renders beside the
 * chat. The panel reaches it through the same host action an artifact link
 * uses, under a reference no artifact slug can spell: artifact slugs are
 * `[a-z0-9-]` only, so the `:` keeps the two apart. The page link names the crew
 * by NAME, so its reference carries the name and the panel resolves the slug from
 * the roster (slugs are lossy and are never guessed from a name).
 */

import { DASHBOARD_PREVIEW_PARAM, DASHBOARD_PREVIEW_VALUE } from '../pages/members/dashboardPreview'

const REF_PREFIX = 'dashboard-preview:'
/** A session's staged page. A member slug cannot contain `:`, so this prefix can
 *  never be read as a member reference whose slug starts with `session`. */
const SESSION_REF_PREFIX = REF_PREFIX + 'session:'
/** A crewmate's staged page named by its exact crew name (percent-encoded). */
const MEMBER_NAME_REF_PREFIX = REF_PREFIX + 'member:'

/** The gateway's member-slug grammar (`members._SLUG_RE`). */
const MEMBER_SLUG_RE = /^[a-z0-9](?:[a-z0-9-]{0,78}[a-z0-9])?$/

/** A slot key as a reference carries it: no separator, no whitespace, bounded. */
const SLOT_KEY_RE = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$/

const PREVIEW_PATH_RE = /^\/api\/members\/([^/]+)\/dashboard$/
const SESSION_PREVIEW_PATH_RE = /^\/api\/chat\/slots\/([^/]+)\/dashboard$/

/** Whose staged page a link or a panel reference names. */
export type DashboardPreviewTarget =
  | { kind: 'member'; slug: string }
  | { kind: 'member-name'; member: string }
  | { kind: 'session'; slot: string }

function decoded(segment: string): string | null {
  try { return decodeURIComponent(segment) } catch { return null }
}

/** The staged page a same-origin preview link names, or null. */
export function dashboardPreviewTargetFromHref(href: string | null | undefined): DashboardPreviewTarget | null {
  if (!href) return null
  let url: URL
  try { url = new URL(href, window.location.origin) } catch { return null }
  // Another origin's path of the same shape is somebody else's page, not this
  // gateway's staged one.
  if (url.origin !== window.location.origin) return null
  // The page link `dashboard_preview` hands out now, which names the crew.
  if (url.pathname === '/members') {
    if (url.searchParams.get(DASHBOARD_PREVIEW_PARAM) !== DASHBOARD_PREVIEW_VALUE) return null
    const member = url.searchParams.get('member')
    return member ? { kind: 'member-name', member } : null
  }
  if (url.searchParams.get('preview') !== '1') return null
  const member = PREVIEW_PATH_RE.exec(url.pathname)
  if (member) {
    const slug = decoded(member[1])
    return slug !== null && MEMBER_SLUG_RE.test(slug) ? { kind: 'member', slug } : null
  }
  const session = SESSION_PREVIEW_PATH_RE.exec(url.pathname)
  if (session) {
    const slot = decoded(session[1])
    return slot !== null && SLOT_KEY_RE.test(slot) ? { kind: 'session', slot } : null
  }
  return null
}

/** The crewmate slug a same-origin staged-dashboard link names, or null. */
export function dashboardPreviewSlugFromHref(href: string | null | undefined): string | null {
  const target = dashboardPreviewTargetFromHref(href)
  return target?.kind === 'member' ? target.slug : null
}

/** The panel reference `openArtifact` takes for a staged page. */
export function dashboardPreviewRef(target: string | DashboardPreviewTarget): string {
  if (typeof target === 'string') return REF_PREFIX + target
  if (target.kind === 'member') return REF_PREFIX + target.slug
  if (target.kind === 'member-name') return MEMBER_NAME_REF_PREFIX + encodeURIComponent(target.member)
  return SESSION_REF_PREFIX + target.slot
}

/** The staged page a panel reference names, or null for an ordinary artifact slug. */
export function dashboardPreviewTargetFromRef(ref: string | null | undefined): DashboardPreviewTarget | null {
  if (!ref || !ref.startsWith(REF_PREFIX)) return null
  if (ref.startsWith(SESSION_REF_PREFIX)) {
    const slot = ref.slice(SESSION_REF_PREFIX.length)
    return SLOT_KEY_RE.test(slot) ? { kind: 'session', slot } : null
  }
  if (ref.startsWith(MEMBER_NAME_REF_PREFIX)) {
    const member = decoded(ref.slice(MEMBER_NAME_REF_PREFIX.length))
    return member ? { kind: 'member-name', member } : null
  }
  const slug = ref.slice(REF_PREFIX.length)
  return MEMBER_SLUG_RE.test(slug) ? { kind: 'member', slug } : null
}

/** The crewmate slug a panel reference names, or null for an ordinary artifact slug. */
export function dashboardPreviewSlugFromRef(ref: string | null | undefined): string | null {
  const target = dashboardPreviewTargetFromRef(ref)
  return target?.kind === 'member' ? target.slug : null
}

/** The crew name a member-name reference names, or null for any other reference. */
export function dashboardPreviewMemberFromRef(ref: string | null | undefined): string | null {
  const target = dashboardPreviewTargetFromRef(ref)
  return target?.kind === 'member-name' ? target.member : null
}

/** The window event a preview link's open fires, carrying the panel reference it opened. */
const PREVIEW_OPENED_EVENT = 'kirocrew:dashboard-preview-opened'

/** Say a preview link was opened again. A side panel that already handed its last
 *  preview over to the live page is mounted under the same reference, so it hears
 *  this and goes back to reading the staged page. */
export function announceDashboardPreviewOpened(ref: string): void {
  window.dispatchEvent(new CustomEvent<string>(PREVIEW_OPENED_EVENT, { detail: ref }))
}

/** Listen for {@link announceDashboardPreviewOpened} on *ref*. Returns the unsubscribe. */
export function onDashboardPreviewOpened(ref: string, cb: () => void): () => void {
  const listener = (event: Event) => {
    if ((event as CustomEvent<string>).detail === ref) cb()
  }
  window.addEventListener(PREVIEW_OPENED_EVENT, listener)
  return () => window.removeEventListener(PREVIEW_OPENED_EVENT, listener)
}
