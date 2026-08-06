/**
 * MigrationPage — Full page component for orphaned builtin apps.
 *
 * Route: /apps/migrate/:name
 * Shows migration guidance, install button, or cleanup option depending on state.
 */
import { useState } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import {
  AlertTriangle, Download, CheckCircle, RefreshCw, Trash2, ArrowRight, Database, X,
} from 'lucide-react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { api } from '../api/client'
import { PageHeader, Card, CardTitle, Btn, Badge, ContentSkeleton } from '../components/ui'

import { i18nT } from '../i18n/t'
import { appDisplayName } from '../components/appstore/appManifest'
type AppInfo = {
  name: string
  displayName: string
  version: string
  enabled: boolean
  origin?: string
  migratedTo?: string
  orphaned?: boolean
}

type RegistryApp = {
  name: string
  displayName: string
  installed: boolean
}

type MigrationState = 'loading' | 'available' | 'not-in-registry' | 'already-installed' | 'error'

export default function MigrationPage() {
  const { name } = useParams<{ name: string }>()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [error, setError] = useState('')
  const [cleanedUp, setCleanedUp] = useState(false)

  // React Query: fetch migration state
  const { data: migrationData, isLoading, error: queryError, refetch } = useQuery({
    queryKey: ['apps', 'migration', name],
    queryFn: async () => {
      const info: AppInfo = await api.getApp(name!)
      const migTarget = info.migratedTo || ''
      const parsed = migTarget.includes(':') ? migTarget.split(':').slice(1).join(':') : name!
      const registryData = await api.listRegistry()
      const registryApps: RegistryApp[] = registryData.apps || []
      const standaloneInRegistry = registryApps.find(a => a.name === parsed)
      let state: MigrationState = 'not-in-registry'
      if (standaloneInRegistry?.installed) state = 'already-installed'
      else if (standaloneInRegistry) state = 'available'
      return { appInfo: info, targetName: parsed, state }
    },
    enabled: !!name,
  })

  const appInfo = migrationData?.appInfo || null
  const targetName = migrationData?.targetName || ''
  const state: MigrationState = isLoading ? 'loading' : (queryError ? 'error' : (migrationData?.state || 'loading'))
  const displayError = queryError
    ? (queryError instanceof Error ? queryError.message : '') || i18nT('pages.migrationPage.failed_to_load_app_info')
    : error

  // Cleanup mutation
  const cleanupMutation = useMutation({
    mutationFn: () => api.migrateCleanup(name!),
    onSuccess: () => {
      setCleanedUp(true)
      window.dispatchEvent(new Event('mc:apps-changed'))
      queryClient.invalidateQueries({ queryKey: ['apps'] })
    },
    onError: (e: unknown) => {
      setError((e instanceof Error && e.message) || i18nT('pages.migrationPage.cleanup_failed'))
    },
  })

  if (!name) return null

  const displayName = appDisplayName({ name, displayName: appInfo?.displayName })

  return (
    <>
      <PageHeader title={i18nT('pages.migrationPage.app_migration')} subtitle={i18nT('pages.migrationPage.migration_guide_for', { name: displayName })} />
      <div className="px-6 pb-8 overflow-y-auto flex-1 min-h-0">

        {displayError && (
          <div className="mb-4 bg-danger/10 border border-danger/20 rounded-lg p-3 flex items-center gap-3 animate-rise">
            <span className="text-danger text-sm flex-1">{displayError}</span>
            <button aria-label={queryError ? i18nT('pages.migrationPage.retry') : i18nT('pages.migrationPage.dismiss_error')} className="text-danger/60 hover:text-danger text-sm" onClick={() => { if (queryError) refetch(); else setError('') }}>
              {queryError ? <RefreshCw size={14} /> : <X size={14} />}
            </button>
          </div>
        )}

        {cleanedUp ? (
          <Card>
            <div className="flex flex-col items-center py-8 gap-4">
              <CheckCircle size={48} className="text-ok" />
              <div className="text-lg font-medium text-text">{i18nT('pages.migrationPage.cleanup_complete')}</div>
              <div className="text-[13px] text-muted text-center max-w-md">
                {i18nT('pages.migrationPage.the_old_builtin_entry_has_been_removed_your_data')}
              </div>
              <Btn primary onClick={() => navigate('/apps')}>
                {i18nT('pages.migrationPage.back_to_apps')} <ArrowRight size={14} />
              </Btn>
            </div>
          </Card>
        ) : state === 'loading' ? (
          <Card>
            <ContentSkeleton rows={4} />
          </Card>
        ) : (
          <>
            {/* Migration explanation */}
            <Card>
              <CardTitle>
                <AlertTriangle size={16} className="text-warn" />
                {displayName} {i18nT('pages.migrationPage.has_moved_to_a_standalone_app')}
              </CardTitle>
              <p className="text-[13px] text-muted leading-relaxed mb-4">
                {i18nT('pages.migrationPage.this_feature_was_previously_built_into_kirocrew')}
              </p>

              {/* Data preservation notice */}
              <div className="flex items-start gap-3 bg-ok/5 border border-ok/20 rounded-lg p-3 mb-4">
                <Database size={16} className="text-ok shrink-0 mt-0.5" />
                <div className="text-[13px] text-text">
                  {i18nT('pages.migrationPage.your_data_has_been_preserved_and_will_be_availab')}
                </div>
              </div>

              {/* State-specific content */}
              {state === 'available' && (
                <div className="flex items-center gap-3 mt-4">
                  <Btn primary onClick={() => navigate(`/apps/detail/${encodeURIComponent(targetName)}`)}>
                    <Download size={14} /> {i18nT('pages.migrationPage.install_from_apps')}
                  </Btn>
                  <span className="text-[13px] text-muted">
                    {i18nT('pages.migrationPage.install_the_standalone_version_to_continue_using')}
                  </span>
                </div>
              )}

              {state === 'not-in-registry' && (
                <div className="bg-bg-elevated border border-border rounded-lg p-4 mt-4">
                  <div className="text-[13px] text-muted mb-3">
                    {i18nT('pages.migrationPage.coming_soon_this_app_is_not_yet_available_in_app')}
                  </div>
                  <Btn onClick={() => refetch()}>
                    <RefreshCw size={14} /> {i18nT('pages.migrationPage.refresh')}
                  </Btn>
                </div>
              )}

              {state === 'already-installed' && (
                <div className="mt-4 space-y-4">
                  <div className="flex items-center gap-3">
                    <Badge variant="ok">
                      <CheckCircle size={12} /> {i18nT('pages.migrationPage.migration_complete')}
                    </Badge>
                    <span className="text-[13px] text-muted">
                      {i18nT('pages.migrationPage.the_standalone_version_is_installed_and_ready_to')}
                    </span>
                  </div>
                  <div className="border-t border-border pt-4">
                    <div className="text-[13px] text-muted mb-3">
                      {i18nT('pages.migrationPage.you_can_clean_up_the_old_builtin_entry_to_remove')}
                    </div>
                    <Btn
                      danger
                      onClick={() => cleanupMutation.mutate()}
                      disabled={cleanupMutation.isPending}
                    >
                      <Trash2 size={14} /> {cleanupMutation.isPending ? i18nT('pages.migrationPage.cleaning_up') : i18nT('pages.migrationPage.clean_up_old_entry')}
                    </Btn>
                  </div>
                </div>
              )}
            </Card>
          </>
        )}
      </div>
    </>
  )
}
