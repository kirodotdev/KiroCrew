import { useQuery } from '@tanstack/react-query'
import { api } from '../../api/client'
import { useLanguage } from '../../i18n/LanguageProvider'

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
 * mounts on a `true` answer is served from this same cache entry, and the
 * `member-dashboard` WS invalidation that refreshes that page refreshes this answer
 * too -- an apply flips the tab without a reload.
 *
 * Pending, refused (the read is owner-gated) or failed all answer `false`: the
 * tab keeps the published view it showed before, so a reader with no adopted page
 * sees no change.
 */
export function useAppliedDashboard(slug: string | null | undefined, member: string | null | undefined, enabled: boolean): boolean {
  const { resolved: locale } = useLanguage()
  const on = enabled && Boolean(slug) && Boolean(member)
  const { data } = useQuery({
    queryKey: ['member-dashboard', slug, member, locale, 'live'],
    queryFn: () => api.memberDashboard(slug as string, member as string, locale),
    enabled: on,
    retry: false,
  })
  return on && typeof data?.instance_version === 'number' && data.instance_version > 0
}
