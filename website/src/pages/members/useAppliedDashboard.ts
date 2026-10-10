import { useQuery } from '@tanstack/react-query'
import { api } from '../../api/client'
import { useLanguage } from '../../i18n/LanguageProvider'

/** What a crewmate's Dashboard tab may draw with the preview off.
 *
 *  `error` is a read that FAILED (a 5xx, a dropped connection, a timeout or a
 *  rate limit), which is not an answer about the crewmate at all; the tab says so
 *  and offers a retry rather than guessing a page. */
export type AppliedDashboardState = 'pending' | 'adopted' | 'default' | 'error'

export interface AppliedDashboard {
  state: AppliedDashboardState
  retry: () => void
}

/** A refusal the gateway gives on purpose -- not the owner (403), no such crewmate
 *  (404), a malformed ask (400) -- rather than a failed read. Duck-typed on
 *  `status` so a mocked client's `Object.assign(new Error(), { status })` counts. */
function isRefusal(e: unknown): boolean {
  const status = typeof e === 'object' && e !== null ? (e as { status?: unknown }).status : undefined
  return typeof status === 'number' && status >= 400 && status < 500 && status !== 408 && status !== 429
}

/**
 * Whether this crewmate has ADOPTED a dashboard page of its own -- a
 * `dashboard_apply` or `dashboard_rollback` landed -- rather than sitting on the
 * default template.
 *
 * The signal is the instance version the dashboard read already carries: the
 * default instance sits at version 0 forever, and every accepted change bumps it.
 * So `instance_version > 0` is the server's own statement that somebody chose this
 * page, which is what lets the Dashboard tab show it with the Feature Preview off.
 *
 * The query key is the one `CrewDynamicDashboard` reads with, so the tab that
 * mounts on `adopted` is served from this same cache entry, and the
 * `member-dashboard` WS invalidation that refreshes that page refreshes this answer
 * too -- an apply flips the tab without a reload.
 *
 * `pending` until the first answer, so the host can hold a neutral loading line
 * rather than draw the published view and then swap it away. A refusal (the read
 * is owner-gated) is `default`: the tab keeps the published view it showed before.
 * Any other failure is `error`, on a refetch as on the first read, except that a
 * held ADOPTED answer outranks a failed refetch. Disabled answers `default`.
 */
export function useAppliedDashboard(slug: string | null | undefined, member: string | null | undefined, enabled: boolean): AppliedDashboard {
  const { resolved: locale } = useLanguage()
  const on = enabled && Boolean(slug) && Boolean(member)
  const { data, isPending, isError, error, refetch } = useQuery({
    queryKey: ['member-dashboard', slug, member, locale, 'live'],
    queryFn: () => api.memberDashboard(slug as string, member as string, locale),
    enabled: on,
    retry: false,
  })
  const retry = () => { void refetch() }
  if (!on) return { state: 'default', retry }
  const adopted = typeof data?.instance_version === 'number' && data.instance_version > 0
  // A held ADOPTED answer outranks a failed refetch, so a blip does not swap the
  // crewmate's page away. Every other failed read -- first read or refetch -- that
  // is not a deliberate refusal is said, because a held `default` cannot tell the
  // reader whether the crewmate adopted a page since.
  if (adopted) return { state: 'adopted', retry }
  if (isError) return { state: isRefusal(error) ? 'default' : 'error', retry }
  if (data) return { state: 'default', retry }
  return { state: isPending ? 'pending' : 'default', retry }
}
