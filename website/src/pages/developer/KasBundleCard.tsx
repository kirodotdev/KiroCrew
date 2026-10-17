import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { CheckCircle2, Download, Loader2, RotateCcw } from 'lucide-react'
import { useState } from 'react'

import { api, type KasBundleStatus } from '../../api/client'
import ErrorNotice from '../../components/ErrorNotice'
import { SettingsToggle } from '../../components/settings'
import { SchemaRestartBadge } from '../../components/settingRef/RestartRequiredBadge'
import { Btn } from '../../components/ui'
import { fmtBytes, fmtPercent } from '../../i18n/format'
import { i18nT } from '../../i18n/t'

/** The config field this card's switch writes. */
export const KAS_BUNDLE_CONFIG_KEY = 'agent.kas_bundle'

const STATUS_QUERY_KEY = ['kas-bundle'] as const

/** How often the card re-reads status while an install is running. */
const INSTALL_POLL_MS = 1000

/**
 * Settings > Agent Harness, inside the KAS row: run Kiro Crew's own KAS engine
 * instead of reaching it through kiro-cli.
 *
 * The switch is `agent.kas_bundle` (an ordinary config PATCH). It can only be
 * turned ON once the pinned engine is installed, because an "on" with nothing
 * installed refuses every KAS session -- the card offers the install instead.
 * Turning it OFF is always allowed, so a broken install never traps anyone on
 * this route.
 *
 * The install runs in the gateway; this card starts it and polls status once a
 * second while it runs. Status is the single authority: nothing here mirrors
 * the server's answer in local state.
 *
 * `agent.kas_bundle` is boot-only (the running KAS process keeps its route until
 * it is replaced), so a saved switch is not yet an applied one. The card says so
 * in its own words after a save -- what restarts and that it has not happened --
 * rather than leaving the restart pill to imply it.
 */
export function KasBundleCard() {
  const qc = useQueryClient()
  const statusQ = useQuery<KasBundleStatus>({
    queryKey: STATUS_QUERY_KEY,
    queryFn: () => api.kasBundleStatus(),
    retry: false,
    staleTime: 0,
    refetchInterval: query => (query.state.data?.installing ? INSTALL_POLL_MS : false),
  })
  const status = statusQ.data

  const installMut = useMutation({
    mutationFn: () => api.kasBundleInstall(),
    onSuccess: data => qc.setQueryData(STATUS_QUERY_KEY, data),
  })

  // Saved but not applied: true from the first successful save until this card
  // unmounts, because the setting cannot take effect while this gateway runs.
  const [restartPending, setRestartPending] = useState(false)
  const toggleMut = useMutation({
    mutationFn: (value: boolean) => api.patchConfig(KAS_BUNDLE_CONFIG_KEY, value),
    onSuccess: () => {
      setRestartPending(true)
      void qc.invalidateQueries({ queryKey: STATUS_QUERY_KEY })
      void qc.invalidateQueries({ queryKey: ['kirocrewConfig'] })
    },
  })

  // The poll stops on its own once the server reports the install finished.
  const installing = status?.installing === true || installMut.isPending

  if (!status) {
    // The same recovery the tab offers when its own config read fails: a read
    // that failed once is not a dead end, and a page reload is not the remedy.
    return statusQ.isError ? (
      <div className="space-y-2" data-testid="kas-bundle-card">
        <ErrorNotice
          message={i18nT('pages.developer.kasBundleCard.status_unavailable')}
          variant="inline"
          askAgent
        />
        <Btn type="button" onClick={() => void statusQ.refetch()} data-testid="kas-bundle-retry">
          {i18nT('pages.developer.agentBackendTab.retry')}
        </Btn>
      </div>
    ) : null
  }

  const installed = status.installed
  const canTurnOn = installed !== null && !installing
  const progress = status.progress
  const progressText =
    progress.total > 0
      ? i18nT('pages.developer.kasBundleCard.installing_progress', {
          version: status.runs_version,
          percent: fmtPercent(progress.done / progress.total),
          done: fmtBytes(progress.done),
          total: fmtBytes(progress.total),
        })
      : i18nT('pages.developer.kasBundleCard.installing', { version: status.runs_version })

  return (
    <div className="space-y-2" data-testid="kas-bundle-card">
      <div className="flex flex-wrap items-center gap-2">
        <div className="min-w-0 flex-1">
          <SettingsToggle
            configKey={KAS_BUNDLE_CONFIG_KEY}
            label={i18nT('pages.developer.kasBundleCard.toggle_label')}
            description={i18nT('pages.developer.kasBundleCard.toggle_description')}
            checked={status.enabled}
            onChange={value => toggleMut.mutate(value)}
            disabled={toggleMut.isPending || (!status.enabled && !canTurnOn)}
          />
        </div>
        {/* Its own notice below says what is pending once a save lands, so the
            generic pill is hidden then. Hidden in place, not unmounted: the row
            keeps its width and the switch does not move under the pointer. */}
        <span className={restartPending ? 'invisible' : undefined} aria-hidden={restartPending || undefined} data-testid="kas-bundle-restart-pill">
          <SchemaRestartBadge configKey={KAS_BUNDLE_CONFIG_KEY} />
        </span>
      </div>

      {restartPending && (
        <div
          className="flex items-center gap-2 text-[12px] text-warn"
          role="status"
          data-testid="kas-bundle-restart-pending"
        >
          <RotateCcw className="lucide-inline" aria-hidden="true" />
          <span>{i18nT('pages.developer.kasBundleCard.saved_restart_pending')}</span>
        </div>
      )}

      <div className="flex flex-wrap items-center gap-2 text-[12px]" data-testid="kas-bundle-state">
        {installing ? (
          <>
            <Loader2 className="lucide-inline animate-spin text-muted" aria-hidden="true" />
            <span role="status" aria-live="polite">{progressText}</span>
          </>
        ) : installed ? (
          <>
            {/* Green only while the engine is what runs: under an OFF switch, or
                after a save the gateway has not restarted onto yet, a green tick
                says "running" before it is true. */}
            <CheckCircle2
              className={`lucide-inline ${status.enabled && !restartPending ? 'text-ok' : 'text-muted'}`}
              aria-hidden="true"
              data-testid="kas-bundle-installed-tick"
            />
            <span>
              {installed.bundled
                ? i18nT('pages.developer.kasBundleCard.installed_bundled', {
                    version: installed.version,
                  })
                : i18nT('pages.developer.kasBundleCard.installed', { version: installed.version })}
            </span>
            {/* A green line under an OFF switch reads as "already running" without this. */}
            {!status.enabled && (
              <span className="text-muted" data-testid="kas-bundle-off-note">
                {i18nT('pages.developer.kasBundleCard.off_note')}
              </span>
            )}
          </>
        ) : (
          <>
            <span className="text-muted">
              {i18nT('pages.developer.kasBundleCard.not_installed', { version: status.runs_version })}
            </span>
            <Btn type="button" primary onClick={() => installMut.mutate()} data-testid="kas-bundle-install">
              <Download className="lucide-inline" aria-hidden="true" />
              {i18nT('pages.developer.kasBundleCard.install')}
            </Btn>
          </>
        )}
      </div>

      {/* No hand-off draft to lose: the install and the switch are both server state.
          A failed re-read comes first: with an earlier status still cached, the
          lines above are that stale answer, and this notice is what says so. */}
      <ErrorNotice
        message={
          statusQ.isError
            ? i18nT('pages.developer.kasBundleCard.status_unavailable')
            : status.error
              ? status.error
              : installMut.isError
                ? i18nT('pages.developer.kasBundleCard.install_failed')
                : toggleMut.isError
                  ? i18nT('pages.developer.kasBundleCard.save_failed')
                  : undefined
        }
        variant="inline"
        askAgent
      />
    </div>
  )
}
