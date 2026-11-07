/**
 * Settings → Notifications → Sources → App sessions: one switch per enabled app
 * (and per disabled app whose mute is still stored), writing
 * `apps.<name>.mute_sessions`.
 *
 * On, the sessions that app opens are muted the way a session's own mute is
 * (`isSlotMuted`, and `session_is_muted` for their notifications). Off is the
 * default, so an app nobody switched alerts exactly as before. The rows write
 * through the same config PATCH and optimistic overlay as the per-app model rows.
 */
import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../../api/client'
import { SettingsCard, SettingsSection, SettingsToggle } from '../../components/settings'
import ErrorNotice from '../../components/ErrorNotice'
import { appDisplayName } from '../../components/appstore/appManifest'
import type { InstalledApp } from '../../components/appstore/types'
import { appsMutingSessions } from '../../hooks/sessionMute'
import { i18nT } from '../../i18n/t'
import { compareText } from '../../i18n/format'
import { setConfigPathValue, useOptimisticConfigPaths } from './useOptimisticConfigPaths'

type Overlay = ReturnType<typeof useOptimisticConfigPaths>

function AppMuteRow({
  app,
  server,
  overlay,
  disabled,
  onFailure,
  onSupersede,
}: {
  app: InstalledApp
  server: boolean
  overlay: Overlay
  disabled: boolean
  onFailure: (path: string, message: string) => void
  onSupersede: (path: string) => void
}) {
  const path = ['apps', app.name, 'mute_sessions'].join('.')
  const name = appDisplayName(app)
  const mut = useMutation(
    overlay.mutationOpts<boolean>({
      queryKey: ['kirocrewConfig'],
      mutationFn: (value: boolean) => api.patchConfig(path, value),
      path: () => path,
      displayValue: value => value,
      applyToCache: (cached, value) => setConfigPathValue(cached, path, value),
      onFailure: () => onFailure(path, i18nT('pages.settings.appSessionMutes.failed_to_save', { app: name })),
      onSupersede,
    }),
  )
  return (
    <SettingsToggle
      label={i18nT('pages.settings.appSessionMutes.row_label', { app: name })}
      checked={overlay.shown(path, server)}
      onChange={value => mut.mutate(value)}
      disabled={disabled}
    />
  )
}

export function AppSessionMuteSection() {
  const qc = useQueryClient()
  const overlay = useOptimisticConfigPaths(qc)
  const [failure, setFailure] = useState<{ path: string; message: string } | null>(null)
  const configQ = useQuery({ queryKey: ['kirocrewConfig'], queryFn: () => api.kirocrewConfig() })
  const appsQ = useQuery({ queryKey: ['apps'], queryFn: () => api.listApps() })
  const muted = appsMutingSessions(configQ.data)
  // Enabled apps, plus a disabled one whose mute is still stored, so the switch
  // that keeps its open sessions quiet stays reachable.
  const rows = (appsQ.data ?? [])
    .filter(app => app.name && (app.enabled || muted.has(app.name)))
    .sort((a, b) => compareText(appDisplayName(a), appDisplayName(b)))
  const loadError = appsQ.isError
    ? i18nT('pages.settings.appModels.load_failed')
    : configQ.isError
      ? i18nT('pages.settings.appSessionMutes.config_load_failed')
      : ''

  return (
    <SettingsSection title={i18nT('pages.settings.appSessionMutes.title')}>
      <div className="text-[12px] text-muted -mt-1 mb-2">{i18nT('pages.settings.appSessionMutes.description')}</div>
      {/* askAgent on: the Sources pane holds no draft, and a refused switch rolls back. */}
      <ErrorNotice
        variant="inline"
        className="mb-2"
        message={failure?.message ?? loadError}
        testId="app-session-mutes-error"
        askAgent
      />
      <SettingsCard>
        {rows.length === 0 && !appsQ.isPending && !appsQ.isError && (
          <div className="text-[13px] text-muted py-2">{i18nT('pages.settings.appModels.none_enabled')}</div>
        )}
        {rows.map(app => (
          <AppMuteRow
            key={app.name}
            app={app}
            server={muted.has(app.name)}
            overlay={overlay}
            disabled={!configQ.isSuccess}
            onFailure={(path, message) => setFailure({ path, message })}
            onSupersede={path => setFailure(prev => (prev?.path === path ? null : prev))}
          />
        ))}
      </SettingsCard>
    </SettingsSection>
  )
}
