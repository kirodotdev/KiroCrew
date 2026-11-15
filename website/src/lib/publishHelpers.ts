// Pure helpers for the publish surface, kept out of `components/PublishHub.tsx`.
//
// The library list (`LiveSiteLink`) and the publication badge are on the first
// load and need only these functions. Importing them from the PublishHub
// component pulled its whole chunk into the first load as well; from here they
// cost only their own few lines.

import type { DeploySitesResponse } from '../api/client'
import type { Site } from '../hooks/useDirectDeploy'

/**
 * Whether a provider row is the Artifact Deploy destination (publish to a public
 * HTTPS URL in the operator's own AWS account).
 *
 * Identified by its endpoint or id rather than by its display label, which is
 * translated and would stop matching in every language but English. An app row
 * declares the deploy endpoint; the core registry row carries the `deploy-web`
 * id. A row that is neither is some other destination and is never filtered.
 */
export function isPublicWebDeploy(p: { id: string; app?: { endpoint?: string } }): boolean {
  if (p.id === 'deploy-web') return true
  return (p.app?.endpoint ?? '').startsWith('/api/deploy/')
}

/** The longest site id the public-web deploy stores. */
const SITE_ID_MAX = 64

/**
 * The site id the public-web deploy stores for an artifact slug.
 *
 * The panel sends `site_id: artifact.slug`, and the deploy route normalizes it
 * (`_safe_site_id` in the deploy handlers): lowercased, every character other
 * than a letter, digit, `-` or `_` replaced by `-`, leading and trailing `-`/`_`
 * stripped, then cut to 64 characters. A slug longer than that is stored
 * truncated, so comparing the raw slug would miss its site.
 */
export function deploySiteIdFor(slug: string): string {
  const normalized = Array.from(slug.trim().toLowerCase(), c => (/[\p{L}\p{N}_-]/u.test(c) ? c : '-')).join('')
  return Array.from(normalized.replace(/^[-_]+|[-_]+$/g, '')).slice(0, SITE_ID_MAX).join('')
}

/**
 * The live public-web site this artifact already has, or `null`.
 *
 * Reads the deploy's own listing, which comes from the AWS account's resource
 * tags. A site removed by teardown, by the expiry reaper, or outside the
 * dashboard is not listed, so the answer cannot go stale the way a stored copy
 * of the link would. No listing, an unconfigured deploy, and no site under this
 * artifact's id all answer `null`, which leaves the panel on its destination list.
 */
export function findLiveSite(
  resp: DeploySitesResponse | null | undefined,
  slug: string,
): Site | null {
  if (!resp || resp.configured === false || !Array.isArray(resp.sites)) return null
  const id = deploySiteIdFor(slug)
  if (!id) return null
  return resp.sites.find(s => !!s && s.site_id === id) ?? null
}

/**
 * Read the OUTCOME of a publish response, across the two shapes an endpoint returns.
 *
 * * The deploy-style shape — `{url}` / `{public_url}` — used by `/api/deploy/deploy`.
 * * The artifact shape — the serialized artifact carrying a `publication` block —
 *   returned by `POST /api/artifacts/{slug}/publish`, which is where an app
 *   provider lands when it hands the confirmed publish to the core route (the
 *   supported way to reuse the core's single publish authorization + audit).
 *
 * Returns `null` for anything unrecognized so the caller reports an explicit
 * error instead of rendering a blank one.
 *
 * **HTTP 200 is not success on the artifact shape.** `publish_sync.publish()`
 * treats the version push as best-effort: on a RE-publish it captures the push
 * error, persists it as `publication.last_error` and returns normally, so the
 * route answers 200 with a publication whose remote content is stale. Reading
 * that as "Published!" would be the same class of lie as the blank error this
 * function replaces, in the opposite direction — so a non-empty `last_error` is
 * an error outcome, carrying the provider's own (already redacted) message.
 *
 * A success whose destination exposes no browsable URL yields `{url: ''}` —
 * success WITHOUT a link, which is why a caller must not infer success from a
 * non-empty url.
 *
 * A non-empty `publication.notice` rides ALONGSIDE a success outcome, never as
 * an error: it means the publish succeeded but the link is not usable yet (e.g.
 * CloudFront still rolling out). It is deliberately NOT folded into `error` —
 * `last_error` is checked first and wins, so a real failure is still an error;
 * a notice-only publication is a success carrying a warn line beside the link.
 */
export function readPublishOutcome(
  data: Record<string, unknown> | null | undefined,
): { url: string; notice?: string; notice_code?: string } | { error: string } | null {
  if (!data || typeof data !== 'object') return null
  const direct = data.url ?? data.public_url
  if (typeof direct === 'string' && direct) return { url: direct }
  const pub = data.publication
  // `publication: null` (an UNpublished artifact) is deliberately not success.
  if (pub && typeof pub === 'object') {
    const lastError = (pub as { last_error?: unknown }).last_error
    if (typeof lastError === 'string' && lastError.trim()) return { error: lastError }
    const viewUrl = (pub as { view_url?: unknown }).view_url
    const noticeRaw = (pub as { notice?: unknown }).notice
    const notice = typeof noticeRaw === 'string' && noticeRaw.trim() ? noticeRaw : undefined
    // The discriminator travels WITH the notice: it is meaningless without one,
    // so it is only surfaced when a notice is actually present.
    const codeRaw = (pub as { notice_code?: unknown }).notice_code
    const notice_code = notice && typeof codeRaw === 'string' && codeRaw.trim() ? codeRaw : undefined
    return {
      url: typeof viewUrl === 'string' ? viewUrl : '',
      ...(notice ? { notice } : {}),
      ...(notice_code ? { notice_code } : {}),
    }
  }
  return null
}

/** The four catalog keys a render site offers for a publish notice: the
 *  existing rolling-out line, the disabled-distribution remedy, the
 *  origin-mismatch remedy, and the neutral fallback that promises no time. */
export interface PublishNoticeKeys {
  rolling_out: string
  distribution_disabled: string
  origin_mismatch: string
  notice_generic: string
}

/**
 * Selects the catalog key for a publish notice from its machine `notice_code`.
 *
 * The backend used to hand back only free text, and the UI printed one fixed
 * "still rolling out — reachable in a few minutes" line for every notice. That
 * is a lie for a DISABLED distribution, whose links never resolve until a human
 * re-enables it — so a time promise there sends the user off to wait for
 * something that will not happen. The `notice_code` discriminator lets each case
 * get its own copy, and anything unrecognised (or an empty-but-present notice)
 * falls back to a neutral line that promises no time at all — never a time
 * promise the code does not justify.
 */
export function publishNoticeKey(keys: PublishNoticeKeys, noticeCode: string | undefined): string {
  switch (noticeCode) {
    case 'rolling_out':
      return keys.rolling_out
    case 'distribution_disabled':
      return keys.distribution_disabled
    case 'origin_mismatch':
      return keys.origin_mismatch
    default:
      // 'unknown', empty, or any value a newer backend introduces.
      return keys.notice_generic
  }
}
