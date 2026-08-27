/**
 * Shared action contract for the App Store pages.
 *
 * DiscoverPage and LibraryPage act on the same apps through the same
 * endpoints, so the pieces that define *how* an action behaves — detail
 * navigation, the trust-consent target, the single enable path, and the
 * query-error banner state — live here. Two inline copies of this plumbing
 * is the drift shape that produced the AppsPage/AppDetailPage
 * author-precedence bug; one shared hook cannot contradict itself.
 *
 * Page-specific flows (uninstall confirmation, Update All, install success
 * messaging) stay in the pages: they differ by design, not by accident.
 */

import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../../api/client'
import { i18nT } from '../../i18n/t'
import { recordEvent } from '../../rum'
import { useTrustGate } from '../../components/appstore/TrustAppModal'
import type { TrustAppTarget } from '../../components/appstore/TrustAppModal'
import type { AppsData } from './useAppsData'

type AppActionsInput = Pick<
  AppsData,
  'apps' | 'browseApps' | 'appsError' | 'registryError' | 'announceAppsChanged'
>

export function useAppActions({
  apps, browseApps, appsError, registryError, announceAppsChanged,
}: AppActionsInput) {
  const navigate = useNavigate()
  const [error, setError] = useState('')
  const [dismissedQueryError, setDismissedQueryError] = useState(false)

  // A NEW query failure re-arms the banner after an earlier dismissal.
  useEffect(() => { if (appsError || registryError) setDismissedQueryError(false) }, [appsError, registryError])
  const displayError = error
    || (!dismissedQueryError && appsError ? (appsError as Error)?.message || i18nT('pages.appsPage.failed_to_load_apps') : '')
    || (!dismissedQueryError && registryError ? (registryError as Error)?.message || i18nT('pages.appsPage.failed_to_load_registry') : '')
  const dismissError = () => { setError(''); setDismissedQueryError(true) }

  // Cmd/Ctrl-click opens the detail page in a new tab.
  const openDetail = (name: string, e?: React.MouseEvent | React.KeyboardEvent) => {
    if (e && (e.metaKey || e.ctrlKey)) { window.open(`/apps/detail/${name}`, '_blank', 'noopener,noreferrer'); return }
    navigate(`/apps/detail/${name}`)
  }
  // autoAction travels as router STATE, never a query param — a URL-reachable
  // trigger would let a cross-site navigation start a privileged install.
  const getApp = (name: string) => navigate(`/apps/detail/${name}`, { state: { autoAction: 'install' } })
  const updateApp = (name: string) => navigate(`/apps/detail/${name}`, { state: { autoAction: 'update' } })

  // Provenance the consent modal shows. For an installed app, the server-bound
  // source wins over today's registry row; for an install prompt, the registry's
  // server-resolved clone target is the authority.
  const trustTarget = (name: string): TrustAppTarget => {
    const row = browseApps.find(a => a.name === name)
    const installed = apps.find(a => a.name === name)
    if (installed) return {
      name,
      displayName: installed.displayName,
      trustRepository: installed.trustRepository,
      origin: installed.origin,
      _registry: row?._registry,
    }
    if (row) return {
      name: row.name,
      displayName: row.displayName,
      trustRepository: row.trustRepository,
      origin: row.origin,
      _registry: row._registry,
    }
    return { name }
  }

  /** The single enable path — shared by the cards and the trust retry. */
  const runEnable = async (name: string) => {
    await api.enableApp(name)
    recordEvent('app_enable', { app: name })
    announceAppsChanged()
  }

  const trust = useTrustGate(runEnable)

  return {
    setError, displayError, dismissError,
    openDetail, getApp, updateApp, trustTarget, runEnable, trust,
  }
}
