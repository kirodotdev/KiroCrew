import { i18nT } from '../i18n/t'
import type { ProjectBundle } from '../types'

type ProjectHealthStatus = ProjectBundle['health']['status']

/** One place that names a Project's health, so the Projects page badge and
 *  the sidebar's create menu cannot drift. `review_stale` is a warning, not
 *  an error: the checkout is present, but executable surfaces changed and
 *  await review. */
export function projectHealthBadge(status: ProjectHealthStatus): { variant: 'ok' | 'warn' | 'err'; label: string } {
  if (status === 'healthy') return { variant: 'ok', label: i18nT('pages.projectBundlesPage.healthy') }
  if (status === 'review_stale') return { variant: 'warn', label: i18nT('pages.projectBundlesPage.review_needed') }
  if (status === 'sources_unavailable') return { variant: 'err', label: i18nT('pages.projectBundlesPage.source_unavailable') }
  return { variant: 'err', label: i18nT('pages.projectBundlesPage.unavailable') }
}

/** Why a non-healthy Project cannot start a session, as one sentence that
 *  ends in what to do about it — the same explanation the Projects page's
 *  notices carry, for a surface (a menu row) that has room only for a
 *  tooltip. `null` for a healthy Project: there is nothing to explain. */
export function projectHealthWhy(status: ProjectHealthStatus): string | null {
  if (status === 'healthy') return null
  if (status === 'review_stale') return i18nT('pages.projectBundlesPage.review_stale_help')
  if (status === 'sources_unavailable') return i18nT('pages.projectBundlesPage.sources_unavailable_why')
  return i18nT('pages.projectBundlesPage.manifest_unavailable_help')
}
